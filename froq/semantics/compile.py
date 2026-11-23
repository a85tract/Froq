"""Route A WP3: lattice-surgery compiler output -> SWIPER-SIM schedule (.lss) + semantic event stream (.events.json).

Inputs (all produced by a modified liblsqecc slicer, run with SWIPER's flags):
  * the per-slice JSON (``-o``), streamed slice by slice (the big programs are up to 1 GB);
  * the sidecar written by ``--metaout``: one record per lattice-surgery instruction (LLI) with its operation,
    operands, application slice and provenance {gate_idx, gadget_id, gadget_kind, is_dagger, role}.
Instruction identity (which cells form a merge, which merge is a T injection, which S is the conditional S of a T
gadget, which init is a CNOT mediator) is taken from the sidecar plus the ``operation_id`` the fork stamps on every
routing / busy cell of the slice JSON; only the merge geometry (faces from stitched edges, as SWIPER's ``_get_merge``)
is read from the cells. Frames and predicates are NOT in the compiler stream (it emits the T gadget's S and the S
gadget's Z unconditionally); they are added from the certified template definitions
(froq/semantics/certificates/c1_templates.json): T1 frame Z_q^w, T2 = correction S predicated on u, S frame
Z_q^(u xor w), CNOT_cf_patched frame Z_c^v X_t^(u xor w); SX (c1_templates_sx.json) frame X_q^(1 xor u xor w).

Physical work without a SWIPER instruction (H followed by a boundary rotation) is emitted as one ``IDLE`` of
``Duration.D`` (one slice = d rounds) per region cell per slice, followed by ``DISCARD`` of the borrowed neighbour
cell, so B and C see the same physical work. Resource / mediator preparation is covered by the group's ``MERGE``
exactly as SWIPER's own converter models it (``LatticeSurgerySchedule.S`` / ``merge``).

Library API for WP4 (recovery scripts): ``compile_snippet``.

CLI:
  python -m froq.semantics.compile --qasm P.qasm --out-dir DIR [--slicer PATH] [--allow-cz-composite]
                                  [--allow-reset-noop]
writes DIR/<bench>.lss, DIR/<bench>.events.json, DIR/<bench>.manifest.json (+ the slice JSON, sidecar, sliced LLI)."""
from __future__ import annotations

import argparse
import atexit
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import OrderedDict

from .. import SWIPER_ROOT  # noqa: F401  (puts third_party/swiper on sys.path)
from swiper.lattice_surgery_schedule import Duration, Instruction, LatticeSurgerySchedule

CONVERTER_VERSION = "scoutq-wp3-compile/0.3"
EVENTS_SCHEMA = "scoutq-events/1"
LAYOUT_FLAGS = ("-L", "edpc", "--disttime", "1", "--nostagger", "-P", "wave")
FORK_SLICER = "lsqecc_slicer"   # the modified liblsqecc slicer (not part of this release); E2E_SNIPPET_SLICER names it
SLICER_TIMEOUT_S = 600
SNIPPET_TIMEOUT_S = 120
ULIMIT_F_BLOCKS = 2_000_000  # `ulimit -f` in 1 KiB blocks: 2 GB output cap (a runaway slicer once filled the disk)
CERT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certificates", "c1_templates.json")

# Template ids are WP2's certificate names (c1_templates.json; SX: c1_templates_sx.json).
TEMPLATE_IDS = ("T", "T1", "T2", "S", "CNOT_cf_patched", "H", "X", "Y", "Z", "CZ", "SX")
PROGRAM_STATE = None  # program wires start in the (unknown) program input state

# Compiler LLI variants; anything else (BellPairInit, BellBasedCNOT, Reset unless allowed) is outside the inventory.
ALLOWED_OPS = {"Gate", "RotateSingleCellPatch", "RequestMagicState", "RequestYState", "MultiBodyMeasure",
               "MeasureSinglePatch", "Init", "BusyRegion", "DeclareLogicalQubitPatches"}


class ConverterError(Exception):
    """A structural problem: template outside the inventory, sidecar / slice JSON disagreement, unrepresentable
    geometry.  The converter fails loudly instead of guessing."""


class PlacementRefused(ConverterError):
    """The slicer RAN and reported it could not place / route the script: an uncaught std:runtime_error from the
    fork's placement / routing machinery (no free cell for a state, no room to route, an unroutable EDPC crossing).
    This is the ONLY compilation outcome a contract may fence on (`no_recovery_workspace`); every other
    ConverterError -- a snippet-cache miss without a slicer, a missing binary, a timeout, malformed provenance -- is
    an infrastructure failure and must abort the run, never silently drop speculation sites (impl,
    findings A2/B1). (note v2 6.2): `rc`, `marker` (the first `_PLACEMENT_REFUSAL_MARKERS` text in the slicer's
    stderr) and `last_line` (the message's last line) describe the refusal; a snippets_sx refusal entry keeps them."""

    def __init__(self, msg: str = "", rc: int | None = None, marker: str | None = None, last_line: str | None = None):
        super().__init__(msg)
        self.rc, self.marker, self.last_line = rc, marker, last_line

    def __reduce__(self):
        return type(self), (str(self), self.rc, self.marker, self.last_line)


# The what() texts the fork's placement / routing machinery throws (dense_patch_computation.cpp, wave_scheduler.cpp);
# an uncaught one aborts the process after libstdc++ prints the "terminate called ..." line.
_PLACEMENT_REFUSAL_MARKERS = (
    "Could not apply instruction after max retries:",     # wave_scheduler.cpp: the router kept refusing
    "Could not apply proximate instruction:",             # wave_scheduler.cpp
    "Could not do S gate routing on",                     # dense_patch_computation.cpp
    "Couldn't find room to route",
    "has no free neighbour",                              # cannot rotate: no free cell next to the target
    "is not free, cannot prepare state",
    "is not free, cannot move",
    "is not free or marked for EDPC, cannot extend",
    "is not free, cannot extend",
    ", cannot measure",
    ", cannot merge",
    "not on lattice",
    "Route invalid for target qubit in EDPC",
    "Route invalid for control qubit in EDPC",
    "Attempting to route through a cell that already contains a patch",
    "Vertices cannot be crossed by more than two paths in EDPC",
    " is active, cannot ",
)


def _is_placement_refusal(returncode: int, stderr: str) -> bool:
    """A nonzero slicer exit is a genuine geometric refusal only when the process died on an uncaught
    std::runtime_error whose text comes from the placement / routing machinery.  A timeout (rc 124 from `timeout`),
    a file-limit kill (rc 153) or a usage / IO refusal (rc 255) is infrastructure, never geometry."""
    if returncode in (124, 153):
        return False
    return "terminate called after throwing an instance of 'std::runtime_error'" in stderr and any(
        m in stderr for m in _PLACEMENT_REFUSAL_MARKERS)


# ----------------------------------------------------------------------------------------------------------- slicer
def run_slicer(slicer: str, args: list[str], timeout_s: int = SLICER_TIMEOUT_S, cwd: str | None = None) -> str:
    """Run the slicer bounded by `ulimit -f` and `timeout` (both mandatory for every invocation)."""
    cmd = f"ulimit -f {ULIMIT_F_BLOCKS} && exec timeout {int(timeout_s)} " + shlex.join([slicer, *args])
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        msg = f"slicer failed (rc={r.returncode}): {shlex.join([slicer, *args])}\n{r.stderr[-2000:]}"
        if _is_placement_refusal(r.returncode, r.stderr):
            raise PlacementRefused(msg, rc=r.returncode, last_line=msg.strip().splitlines()[-1],
                                   marker=next(m for m in _PLACEMENT_REFUSAL_MARKERS if m in r.stderr))
        raise ConverterError(msg)
    return r.stdout


# ----------------------------------------------------------------------------------------------- slice JSON stream
_WS_COMMA = re.compile(r"[\s,]*")


def iter_slices(path: str, chunk: int = 1 << 24):
    """Yield the slices of a slicer JSON file one at a time (the file is a JSON array; never load it whole)."""
    dec = json.JSONDecoder()
    with open(path, "r") as f:
        buf = f.read(chunk)
        pos = re.compile(r"\s*").match(buf, 0).end()
        if buf[pos:pos + 1] != "[":
            raise ConverterError(f"{path}: not a JSON array of slices")
        pos += 1
        while True:
            pos = _WS_COMMA.match(buf, pos).end()
            if pos >= len(buf):
                more = f.read(chunk)
                if not more:
                    return
                buf, pos = buf[pos:] + more, 0
                continue
            if buf[pos] == "]":
                return
            try:
                obj, end = dec.raw_decode(buf, pos)
            except json.JSONDecodeError:
                more = f.read(chunk)
                if not more:
                    raise
                buf, pos = buf[pos:] + more, 0
                continue
            yield obj
            pos = end
            if pos > chunk:
                buf, pos = buf[pos:], 0


class SliceView:
    """One slice: non-null cells, patch id -> cell, operation ids of routing / busy cells, orientations."""

    __slots__ = ("idx", "cells", "ids", "opid", "orient")

    def __init__(self, idx: int, raw):
        self.idx = idx
        self.cells: dict[tuple[int, int], dict] = {}
        self.ids: dict[int, tuple[int, int]] = {}
        self.opid: dict[tuple[int, int], int] = {}
        self.orient: dict[int, str] = {}
        for r, row in enumerate(raw):
            for c, cell in enumerate(row):
                if not cell:
                    continue
                pos = (r, c)
                self.cells[pos] = cell
                pt = cell.get("patch_type")
                text = cell.get("text", "") or ""
                if pt == "Qubit" and text.startswith("Id: "):
                    pid = int(text[4:])
                    if pid in self.ids:
                        raise ConverterError(f"slice {idx}: patch id {pid} appears twice")
                    self.ids[pid] = pos
                    self.orient[pid] = orientation(cell)
                oid = cell.get("operation_id")
                if oid is not None and pt == "Ancilla":
                    self.opid[pos] = int(oid)

    def activity(self, pos):
        cell = self.cells.get(pos)
        return None if cell is None else (cell.get("activity") or {}).get("activity_type")


def orientation(cell: dict) -> str:
    """Boundary types Top/Bottom/Left/Right as one letter each: S = smooth (Z), D = rough (X), N = none/other."""
    out = []
    for side in ("Top", "Bottom", "Left", "Right"):
        e = (cell.get("edges") or {}).get(side, "None")
        out.append("S" if e.startswith("Solid") else "D" if e.startswith("Dashed") else "N")
    return "".join(out)


class SliceStore:
    """Sliding window over the slice stream: keeps [current - 1, current + lookahead]."""

    LOOKAHEAD = 4

    def __init__(self, it):
        self._it = iter(it)
        self._views: dict[int, SliceView] = {}
        self._next = 0
        self.exhausted = False

    def get(self, s: int) -> SliceView | None:
        if s < 0:
            return None
        while s >= self._next and not self.exhausted:
            try:
                raw = next(self._it)
            except StopIteration:
                self.exhausted = True
                break
            self._views[self._next] = SliceView(self._next, raw)
            self._next += 1
        return self._views.get(s)

    def count(self) -> int:
        while not self.exhausted:
            self.get(self._next)
        return self._next

    def drop_before(self, s: int):
        for k in [k for k in self._views if k < s]:
            del self._views[k]


# --------------------------------------------------------------------------------------------- merge geometry walk
def walk_region(view: SliceView, start: tuple[int, int]):
    """SWIPER's `_get_merge` geometry: follow stitched (Qubit) / joined (Ancilla) edges from a data cell.
    Returns (data_cells, routing_cells, merge_faces)."""

    def is_m(pos, side):
        cell = view.cells.get(pos)
        if cell is None:
            return False
        e = cell["edges"][side]
        pt = cell.get("patch_type")
        return ("Stiched" in e) if pt == "Qubit" else ("Join" in e) if pt == "Ancilla" else False

    faces = set()
    data, routing = [start], []
    cur, from_dir = start, None
    visited = {start}
    while True:
        r, c = cur
        nxt = None
        for side, d, delta in (("Bottom", "top", (1, 0)), ("Top", "bottom", (-1, 0)),
                               ("Right", "left", (0, 1)), ("Left", "right", (0, -1))):
            if is_m(cur, side) and from_dir != d:
                nxt = ((r + delta[0], c + delta[1]), {"top": "bottom", "bottom": "top", "left": "right",
                                                       "right": "left"}[d])
                faces.add((cur, nxt[0]))
                break
        if nxt is None:
            break
        cur, from_dir = nxt
        if cur in visited:
            raise ConverterError(f"slice {view.idx}: merge walk from {start} loops at {cur}")
        visited.add(cur)
        pt = (view.cells.get(cur) or {}).get("patch_type")
        if pt == "Ancilla":
            routing.append(cur)
        elif pt == "Qubit":
            data.append(cur)
        else:
            raise ConverterError(f"slice {view.idx}: merge walk from {start} hits {cur} of type {pt}")
    return data, routing, faces


def adjacent(a, b) -> bool:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1


# ------------------------------------------------------------------------------------------------- sidecar records
def rec_target(rec: dict) -> int:
    """Operand patch of a request / init / single-patch op (the sidecar's `patches` includes the clients)."""
    clients = set(rec.get("clients") or [])
    cands = [p for p in rec["patches"] if p not in clients]
    if rec["op"] in ("RequestMagicState", "RequestYState", "Init"):
        if len(cands) != 1:
            raise ConverterError(f"LLI {rec['idx']} ({rec['op']}): cannot identify the target among {rec['patches']}")
        return cands[0]
    return rec["patches"][0]


def rec_obs(rec: dict) -> dict[int, str]:
    return {int(k): v for k, v in rec["observable"].items()}


@dataclasses.dataclass
class Gadget:
    gid: str
    template_id: str
    gadget_kind: str
    gate_idx: int | None
    is_dagger: bool
    qubits: list[int]                       # program patch ids in the gate's operand order
    members: list[int]                      # direct member LLI indices (innermost gadget of each)
    parent: str | None = None
    children: list[str] = dataclasses.field(default_factory=list)
    predicate: dict | None = None           # for T2 and its S: executed iff xor(u) = 1
    followups: list[dict] = dataclasses.field(default_factory=list)  # BusyRegion records of this gadget
    resource: int | None = None             # magic / Y / mediator patch id
    bits: dict = dataclasses.field(default_factory=dict)   # bit name -> result id
    events: list[str] = dataclasses.field(default_factory=list)
    lss: dict = dataclasses.field(default_factory=dict)    # e.g. {"t1_merge_idx": int}
    order_key: int = 0
    cond: dict | None = None                # w10: the classical condition of a classically controlled parent (a
                                            # conditional CZ's H / CNOT members): their EVENTS run under it, the member
                                            # gadgets stay unpredicated so the CZ is one atom


class GadgetTree:
    """Decomposes the sidecar's provenance groups into certified templates (fails loudly outside the inventory)."""

    def __init__(self, meta: dict):
        self.gadgets: "OrderedDict[str, Gadget]" = OrderedDict()
        self.of_lli: dict[int, str] = {}        # LLI idx -> innermost gadget id
        self.skipped: set[int] = set()          # LLI indices with no events (BusyRegion followups, dropped resets)
        self.deviations = {"tdg_as_t": 0, "sdg_as_s": 0, "reset_dropped": 0, "cz_composite": 0}
        self._counter = 0
        self.core = set(meta.get("core_qubits") or [])
        # w10 (classical feedback): source gate index of every `measure` -> its MeasureSinglePatch LLI, so a
        # classically controlled CZ can name the result it is predicated on (the sidecar's provenance `condition`)
        self.measure_lli_of_gate: dict[int, int] = {}
        self.feedback: list[dict] = []          # one record per classically controlled CZ (document `classical_feedback`)
        for rec in meta["lli"]:
            prov = rec.get("provenance") or {}
            if prov.get("gadget_kind") == "MEASURE":
                if prov["gate_idx"] in self.measure_lli_of_gate:
                    raise ConverterError(f"measure gate {prov['gate_idx']} lowers to more than one LLI")
                self.measure_lli_of_gate[prov["gate_idx"]] = rec["idx"]
        groups: "OrderedDict[int, list[dict]]" = OrderedDict()
        for rec in meta["lli"]:
            op = rec["op"]
            if op == "DeclareLogicalQubitPatches":
                self.skipped.add(rec["idx"])
                continue
            if op not in ALLOWED_OPS and op != "Reset":
                raise ConverterError(f"LLI {rec['idx']}: operation {op} is outside the certified inventory "
                                     f"(Bell-based CNOT / catalytic S / PatchReset are rejected by design Sec. 1.3)")
            prov = rec.get("provenance")
            if not prov or prov.get("gadget_id") is None:
                raise ConverterError(f"LLI {rec['idx']} ({op}) has no provenance record in the sidecar")
            groups.setdefault(prov["gadget_id"], []).append(rec)
        for gadget_id, recs in groups.items():
            recs.sort(key=lambda r: r["idx"])
            self._decompose(gadget_id, recs)
        for g in reversed(list(self.gadgets.values())):   # children are created after their parent
            if g.parent is not None:
                p = self.gadgets[g.parent]
                p.order_key = g.order_key if p.order_key < 0 else min(p.order_key, g.order_key)

    # -- helpers -------------------------------------------------------------------------------------------------
    def _new(self, template_id, kind, gate_idx, is_dagger, qubits, members, parent=None, predicate=None) -> Gadget:
        gid = f"g{self._counter}"
        self._counter += 1
        g = Gadget(gid, template_id, kind, gate_idx, is_dagger, list(qubits), list(members), parent=parent,
                   predicate=predicate, order_key=min(members) if members else -1)
        self.gadgets[gid] = g
        for idx in members:
            self.of_lli[idx] = gid
        if parent is not None:
            self.gadgets[parent].children.append(gid)
        return g

    @staticmethod
    def _shape(recs) -> list[str]:
        out = []
        for r in recs:
            out.append(r["op"] + (":" + r["single_qubit_op"] if r["op"] == "Gate" else ""))
        return out

    def _fail(self, gadget_id, recs, why):
        raise ConverterError(f"gadget {gadget_id} ({recs[0]['provenance']['gadget_kind']}, gate {recs[0]['provenance']['gate_idx']}): "
                             f"{why}; members: {self._shape(recs)} at LLI {[r['idx'] for r in recs]}")

    # -- per kind --------------------------------------------------------------------------------------------------
    def _decompose(self, gadget_id, recs):
        prov = recs[0]["provenance"]
        kind, gate_idx, dagger = prov["gadget_kind"], prov["gate_idx"], bool(prov.get("is_dagger"))
        core = [r for r in recs if r["op"] != "BusyRegion"]
        followups = [r for r in recs if r["op"] == "BusyRegion"]
        for r in followups:
            self.skipped.add(r["idx"])
        if kind in ("T", "Tdg"):
            if kind == "Tdg":
                self.deviations["tdg_as_t"] += 1
            self._t_gadget(gadget_id, core, gate_idx, dagger)
        elif kind in ("S", "Sdg"):
            if kind == "Sdg":
                self.deviations["sdg_as_s"] += 1
            self._s_gadget(gadget_id, core, gate_idx, dagger, parent=None, predicate=None)
        elif kind == "SX":
            self._sx_gadget(gadget_id, core, gate_idx, dagger)
        elif kind == "SXdg":
            # SX^dagger is the SX member followed by a frame X member (C3); the runtime never writes this kind
            self._fail(gadget_id, core, "kind SXdg is foreign LLI: SX^dagger is lowered as SX plus a frame X member")
        elif kind == "CNOT":
            self._cnot_gadget(gadget_id, core, gate_idx, parent=None)
        elif kind == "H":
            g = self._h_gadget(gadget_id, core, gate_idx, parent=None)
            g.followups = followups
        elif kind in ("X", "Y", "Z"):
            if self._shape(core) != [f"Gate:{kind}"]:
                self._fail(gadget_id, core, f"a {kind} gadget must be exactly one SingleQubitOp {kind}")
            self._new(kind, kind, gate_idx, dagger, [core[0]["patches"][0]], [core[0]["idx"]])
        elif kind == "CZ":
            self.deviations["cz_composite"] += 1
            self._cz_gadget(gadget_id, core, followups, gate_idx)
        elif kind == "MEASURE":
            # w10: `measure q[i] -> m[0]`, a destructive Z readout of a program patch; its bit is a program result that a
            # later classically controlled CZ may be predicated on (no frame: the patch leaves the lattice)
            if self._shape(core) != ["MeasureSinglePatch"] or core[0].get("is_negative"):
                self._fail(gadget_id, core, "MEASURE must be exactly one MeasureSinglePatch (positive sign)")
            q = core[0]["patches"][0]
            if rec_obs(core[0]) != {q: "Z"} or q not in self.core:
                self._fail(gadget_id, core, "MEASURE must read a program patch in Z")
            self._new("MEASURE", "MEASURE", gate_idx, False, [q], [core[0]["idx"]])
            self.deviations["measurements_kept"] = self.deviations.get("measurements_kept", 0) + 1
        elif kind == "RESET":
            # The compiler applies PatchReset as a NO-OP (dense_patch_computation.cpp): nothing is measured or
            # re-initialised, zero slices.  W_p has no resets; they are counted in the deviation table.
            if self._shape(core) != ["Reset"]:
                self._fail(gadget_id, core, "RESET must be exactly one PatchReset")
            self.deviations["reset_dropped"] += 1
            self.skipped.add(core[0]["idx"])
        else:
            self._fail(gadget_id, core, f"gadget kind {kind} is outside the certified inventory")

    def _t_gadget(self, gadget_id, core, gate_idx, dagger):
        shape = self._shape(core)
        want = ["RequestMagicState", "MultiBodyMeasure", "MeasureSinglePatch", "RequestYState", "MultiBodyMeasure",
                "MeasureSinglePatch", "Gate:Z"]
        if shape != want:
            self._fail(gadget_id, core, f"T gadget must lower to {want}")
        q = core[0]["near_patch"]
        m = rec_target(core[0])
        if rec_obs(core[1]) != {q: "Z", m: "Z"} or rec_obs(core[2]) != {m: "X"}:
            self._fail(gadget_id, core, "T1 observables must be Z_q Z_m then X_m")
        gT = self._new("T", "T" if not dagger else "Tdg", gate_idx, dagger, [q], [])
        gT1 = self._new("T1", "T1", gate_idx, dagger, [q], [r["idx"] for r in core[:3]], parent=gT.gid)
        gT1.resource = m
        gT2 = self._new("T2", "T2", gate_idx, dagger, [q], [], parent=gT.gid)
        # T2's predicate (xor u) is bound to T1's result id when the ZZ merge event is created.
        gT2.predicate = {"op": "xor", "args": [f"r{core[1]['idx']}"]}
        self._s_gadget(gadget_id, core[3:], gate_idx, dagger, parent=gT2.gid, predicate=gT2.predicate)

    def _s_gadget(self, gadget_id, core, gate_idx, dagger, parent, predicate):
        want = ["RequestYState", "MultiBodyMeasure", "MeasureSinglePatch", "Gate:Z"]
        if self._shape(core) != want:
            self._fail(gadget_id, core, f"teleported S must lower to {want}")
        q = core[0]["near_patch"]
        y = rec_target(core[0])
        if rec_obs(core[1]) != {q: "Z", y: "Z"} or rec_obs(core[2]) != {y: "X"} or core[3]["patches"][0] != q:
            self._fail(gadget_id, core, "S observables must be Z_y Z_q then X_y, frame Z on q")
        g = self._new("S", "S" if not dagger else "Sdg", gate_idx, dagger, [q], [r["idx"] for r in core],
                      parent=parent, predicate=predicate)
        g.resource = y
        return g

    def _sx_gadget(self, gadget_id, core, gate_idx, dagger):
        """(note v2 6.1, 6.3): the X-side teleported S, a recovery script member (never nested, never a dagger).
        A |Y> patch requested on the target's X side, X_y X_q (u), y read in Z (w); frame X_q^(1 xor u xor w)."""
        want = ["RequestYState", "MultiBodyMeasure", "MeasureSinglePatch", "Gate:X"]
        if self._shape(core) != want:
            self._fail(gadget_id, core, f"SX must lower to {want}")
        if dagger:
            self._fail(gadget_id, core, "SX carries no dagger (SX^dagger is SX plus a frame X member)")
        q = core[0]["near_patch"]
        y = rec_target(core[0])
        if core[0].get("near_boundary") != "X":
            self._fail(gadget_id, core, f"SX's Y state must be requested on its target's X side "
                                        f"(near_boundary {core[0].get('near_boundary')!r})")
        if rec_obs(core[1]) != {q: "X", y: "X"} or rec_obs(core[2]) != {y: "Z"} or core[3]["patches"][0] != q:
            self._fail(gadget_id, core, "SX observables must be X_y X_q then Z_y, frame X on q")
        if core[1].get("is_negative") or core[2].get("is_negative"):
            self._fail(gadget_id, core, "SX's measurements must be positive (C1's frame rule)")
        g = self._new("SX", "SX", gate_idx, False, [q], [r["idx"] for r in core])
        g.resource = y
        return g

    def _cnot_gadget(self, gadget_id, core, gate_idx, parent):
        want = ["Init", "MultiBodyMeasure", "MultiBodyMeasure", "MeasureSinglePatch"]
        if self._shape(core) != want:
            self._fail(gadget_id, core, f"CNOT must lower to {want}")
        a = rec_target(core[0])
        if core[0].get("state") != "Plus":
            self._fail(gadget_id, core, "CNOT mediator must be initialised in |+>")
        o1, o2, o3 = rec_obs(core[1]), rec_obs(core[2]), rec_obs(core[3])
        if set(o1.values()) == {"X"} and set(o2.values()) == {"Z"}:
            self._fail(gadget_id, core, "target-first CNOT (X_a X_t before Z_c Z_a) is not certified")
        if o1.get(a) != "Z" or set(o1.values()) != {"Z"} or o2.get(a) != "X" or set(o2.values()) != {"X"}:
            self._fail(gadget_id, core, "control-first CNOT must measure Z_c Z_a then X_a X_t")
        c = next(p for p in o1 if p != a)
        t = next(p for p in o2 if p != a)
        if o3 != {a: "Z"}:
            self._fail(gadget_id, core, f"mediator readout is {o3}; only the Z readout (CNOT_cf_patched) is "
                                        "certified -- the unpatched X readout is a deliberately failing C1 case")
        g = self._new("CNOT_cf_patched", "CNOT", gate_idx, False, [c, t], [r["idx"] for r in core], parent=parent)
        g.resource = a
        return g

    def _h_gadget(self, gadget_id, core, gate_idx, parent):
        shape = self._shape(core)
        if shape not in (["Gate:H"], ["Gate:H", "RotateSingleCellPatch"]):
            self._fail(gadget_id, core, "H must lower to SingleQubitOp H (+ RotateSingleCellPatch)")
        q = core[0]["patches"][0]
        if len(core) == 2 and core[1]["patches"][0] != q:
            self._fail(gadget_id, core, "rotation target differs from the H target")
        return self._new("H", "H", gate_idx, False, [q], [r["idx"] for r in core], parent=parent)

    def _cz_gadget(self, gadget_id, core, followups, gate_idx):
        # H_t (+rot), Init a, Z_c Z_a, X_a X_t, Z_a readout, H_t (+rot): the compiler's CZ lowering.
        shape = self._shape(core)
        i = 0
        def take_h():
            nonlocal i
            n = 2 if i + 1 < len(core) and shape[i + 1] == "RotateSingleCellPatch" else 1
            part = core[i:i + n]
            i += n
            return part
        if not shape or shape[0] != "Gate:H":
            self._fail(gadget_id, core, "CZ must start with SingleQubitOp H on the target")
        h1 = take_h()
        cn = core[i:i + 4]
        i += 4
        if i >= len(core) or shape[i] != "Gate:H":
            self._fail(gadget_id, core, "CZ must end with SingleQubitOp H on the target")
        h2 = take_h()
        if i != len(core):
            self._fail(gadget_id, core, "CZ has unexpected extra members")
        t = h1[0]["patches"][0]
        gCZ = self._new("CZ", "CZ", gate_idx, False, [], [])
        g1 = self._h_gadget(gadget_id, h1, gate_idx, parent=gCZ.gid)
        gc = self._cnot_gadget(gadget_id, cn, gate_idx, parent=gCZ.gid)
        g2 = self._h_gadget(gadget_id, h2, gate_idx, parent=gCZ.gid)
        if gc.qubits[1] != t or g2.qubits[0] != t:
            self._fail(gadget_id, core, "CZ: the H gates must act on the CNOT target")
        gCZ.qubits = list(gc.qubits)
        conds = {json.dumps(r["provenance"].get("condition"), sort_keys=True) for r in core}
        if len(conds) != 1:
            self._fail(gadget_id, core, f"CZ members disagree on their classical condition: {sorted(conds)}")
        cond = core[0]["provenance"].get("condition")
        if cond is not None:
            # w10: `if(m==1) cz`: the CZ runs iff the measurement's bit is 1.  The gadget carries the predicate (one atom,
            # predicated on an EXTERNAL result); its members' events inherit it; every member LLI must carry the fork's
            # scheduling dependency on the measured patch (it orders the CZ after the measurement in the slices).
            meas = self.measure_lli_of_gate.get(cond.get("gate_idx"))
            if meas is None or meas >= min(r["idx"] for r in core):
                self._fail(gadget_id, core, f"the condition {cond} names no earlier measurement")
            if any(r.get("classical_deps") != [cond["patch"]] for r in core):
                self._fail(gadget_id, core, f"every member must depend on the measured patch {cond['patch']}")
            pred = pred_xor(f"r{meas}")
            gCZ.predicate = pred
            for child in (g1, gc, g2):
                child.cond = pred
            self.deviations["conditional_cz"] = self.deviations.get("conditional_cz", 0) + 1
            self.feedback.append({"gadget": gCZ.gid, "result_id": f"r{meas}", "measure_lli": meas,
                                  "measure_gate_idx": cond["gate_idx"], "register": cond.get("register"),
                                  "measured_patch": cond["patch"]})
        # followups (BusyRegion) belong to whichever rotation precedes them on that patch
        for f in sorted(followups, key=lambda r: r["idx"]):
            owner = g1 if (len(h1) == 2 and (len(h2) < 2 or f["slice"] <= h2[1]["slice"])) else g2
            owner.followups.append(f)


# --------------------------------------------------------------------------------------------------------- events
def pred_xor(*rids) -> dict:
    return {"op": "xor", "args": list(rids)}


def pred_and(*preds) -> dict:
    ps = [p for p in preds if p is not None]
    if not ps:
        return None
    return ps[0] if len(ps) == 1 else {"op": "and", "args": ps}


def pred_results(pred) -> list[str]:
    if pred is None:
        return []
    out = []
    for a in pred["args"]:
        out.extend(pred_results(a) if isinstance(a, dict) else [a])
    return out


@dataclasses.dataclass
class Holder:
    token: tuple          # ("L", patch_id) or ("R", lli_idx)
    release: str | None   # event id that frees the cell (None for a lifetime still alive)


class Converter:
    """One program: sidecar + slice stream -> (LatticeSurgerySchedule, events document, statistics)."""

    def __init__(self, meta: dict, slices: SliceStore, bench: str):
        self.meta = meta
        self.slices = slices
        self.bench = bench
        self.tree = GadgetTree(meta)
        self.core = list(meta["core_qubits"])
        self.rec = {r["idx"]: r for r in meta["lli"]}
        self.schedule = LatticeSurgerySchedule()
        self.events: list[dict] = []
        self.ev: dict[str, dict] = {}
        self.lss_map: dict[str, list[int]] = {}
        self.lifetimes: "OrderedDict[str, dict]" = OrderedDict()
        self.wires: "OrderedDict[str, dict]" = OrderedDict()
        self.last_on_wire: dict[str, str] = {}
        self.holders: dict[tuple[int, int], Holder] = {}
        self.results: "OrderedDict[str, dict]" = OrderedDict()
        self.boundaries: dict[str, list] = {}
        self.pending_init: dict[int, str] = {}      # mediator / Y patch -> init event (mapped to the group's MERGE)
        self.pending_h: dict[int, str] = {}         # patch -> clifford H event awaiting its rotation's IDLEs
        self.s_group: dict[int, tuple] = {}          # Y patch -> (merge, y_meas, idle, discard) lss indices
        self.correction_done: set[str] = set()
        self.stats = {"cnot_placements": {}, "deviations": self.tree.deviations, "counts": {}}

    # ------------------------------------------------------------------------------------------- small helpers
    def wire_of(self, pid: int) -> str:
        return f"q{pid}" if pid in self.core else f"w{pid}"

    def lifetime_of(self, pid: int) -> str:
        return f"L{pid}"

    def _new_event(self, kind, slice_idx, gadget: str | None, lli=(), **fields) -> dict:
        eid = f"e{len(self.events):07d}"
        e = {"event_id": eid, "kind": kind, "slice": slice_idx, "gadget": gadget, "lli": list(lli), "deps": []}
        e.update(fields)
        self.events.append(e)
        self.ev[eid] = e
        if gadget is not None:
            self.tree.gadgets[gadget].events.append(eid)
        return e

    def _dep(self, e: dict, on: str | None, kind: str):
        if on is None or on == e["event_id"]:
            return
        for d in e["deps"]:
            if d["on"] == on and d["kind"] == kind:
                return
        e["deps"].append({"on": on, "kind": kind})

    def _touch_wire(self, e: dict, wire: str):
        self._dep(e, self.last_on_wire.get(wire), "quantum")
        self.last_on_wire[wire] = e["event_id"]
        e.setdefault("wires", [])
        if wire not in e["wires"]:
            e["wires"].append(wire)

    def _map(self, e: dict, idxs):
        idxs = list(idxs)
        if not idxs:
            return
        self.lss_map.setdefault(e["event_id"], []).extend(idxs)

    def _idle_region(self, cells, duration):
        """One IDLE instruction covering several cells at once (a rotation's busy region)."""
        self.schedule._add_instruction(Instruction("IDLE", len(self.schedule.instructions), frozenset(tuple(c) for c in cells), duration))

    def _emit(self, fn, *a, **kw) -> list[int]:
        """Call a LatticeSurgerySchedule method and return the indices of the instructions it appended."""
        before = len(self.schedule.instructions)
        fn(*a, **kw)
        return list(range(before, len(self.schedule.instructions)))

    def _acquire(self, cell, token, e: dict):
        prev = self.holders.get(cell)
        if prev is not None and prev.token != token:
            rel = prev.release
            if prev.token[0] == "L":
                rel = self.lifetimes[self.lifetime_of(prev.token[1])]["discard_event"]
            if rel is None:
                raise ConverterError(f"cell {cell} acquired by {e['event_id']} ({e['kind']}, slice {e['slice']}) "
                                     f"while occupant {prev.token} is still alive")
            self._dep(e, rel, "physical")
        self.holders[cell] = Holder(token, None if token[0] == "L" else e["event_id"])

    def _new_lifetime(self, pid, role, init_event: dict, cell):
        L = self.lifetime_of(pid)
        if L in self.lifetimes:
            raise ConverterError(f"patch id {pid} initialised twice (lifetime {L}); reset-in-place is not allowed")
        w = self.wire_of(pid)
        self.wires.setdefault(w, {"role": role, "patch_id": pid, "lifetimes": []})["lifetimes"].append(L)
        self.lifetimes[L] = {"patch_id": pid, "wire": w, "role": role, "init_event": init_event["event_id"],
                             "discard_event": None, "cell": list(cell)}
        init_event["lifetime"] = L
        init_event["wire"] = w
        init_event["role"] = role
        init_event["cell"] = list(cell)
        self._touch_wire(init_event, w)
        self._acquire(cell, ("L", pid), init_event)
        return L

    def _result(self, e: dict, rid: str):
        e["result_id"] = rid
        self.results[rid] = {"producer": e["event_id"], "kind": e["kind"]}

    def _locate(self, pid: int, s: int) -> tuple[int, int]:
        """Cell of a patch around slice s (a patch inside a boundary rotation is invisible for 3 slices)."""
        for k in (s, s - 1, s + 1, s + 2, s + 3, s + 4):
            v = self.slices.get(k)
            if v is not None and pid in v.ids:
                return v.ids[pid]
        raise ConverterError(f"patch {pid} not found in slices {s - 1}..{s + 4}")

    def _view(self, s: int) -> SliceView:
        v = self.slices.get(s)
        if v is None:
            raise ConverterError(f"slice {s} missing from the slice JSON")
        return v

    def _predicate_for(self, g: Gadget) -> dict | None:
        return g.predicate

    @staticmethod
    def _pred(g: Gadget) -> dict | None:
        """The execution predicate of g's events: its own predicate and (w10) the classical condition inherited from a
        classically controlled parent.  Without such a parent this is g.predicate, so other programs convert unchanged."""
        return pred_and(g.predicate, g.cond)

    # ------------------------------------------------------------------------------------------------- program
    def run(self):
        # program wires: initial placement (slice -1), then the io input commit
        for pid in self.core:
            cell = self._locate(pid, 0)
            e = self._new_event("init", -1, None, state=PROGRAM_STATE)
            self._new_lifetime(pid, "program", e, cell)
            io = self._new_event("io", -1, None, io_kind="input", wire=self.wire_of(pid), commit_event=e["event_id"])
            self._touch_wire(io, self.wire_of(pid))
        # application order: the fork's global application counter `app_seq` (unstamped Paulis / corrective Z's
        # would be misordered against measurements by idx alone); idx as fallback for synthesized sidecars
        applied = sorted((r for r in self.meta["lli"] if r["idx"] not in self.tree.skipped),
                         key=lambda r: (r["slice"], r.get("app_seq", r["idx"]), r["idx"]))
        cur = -1
        for rec in applied:
            s = rec["slice"]
            if s != cur:
                self._record_orientations(cur + 1, s)
                self.slices.drop_before(s - 1)
                cur = s
            handler = getattr(self, "_op_" + rec["op"])
            handler(rec, self.tree.gadgets[self.tree.of_lli[rec["idx"]]])
        n_slices = self.slices.count()
        self._record_orientations(cur + 1, n_slices)
        self._finish(n_slices)
        return self.schedule, self._document(n_slices), self.stats

    def _record_orientations(self, s0, s1):
        for s in range(max(s0, 0), s1):
            v = self.slices.get(s)
            if v is None:
                return
            for pid, o in v.orient.items():
                L = self.lifetime_of(pid)
                hist = self.boundaries.setdefault(L, [])
                if not hist or hist[-1][1] != o:
                    hist.append([s, o])

    # ------------------------------------------------------------------------------------------------- handlers
    def _op_RequestMagicState(self, rec, g: Gadget):
        s, m = rec["slice"], rec_target(rec)
        v = self._view(s)
        if m not in v.ids:
            raise ConverterError(f"LLI {rec['idx']}: magic state {m} not bound in slice {s}")
        cell = v.ids[m]
        e = self._new_event("init", s, g.gid, [rec["idx"]], state="T", near=self.wire_of(rec["near_patch"]),
                            predicate=self._pred(g))
        self._new_lifetime(m, "resource", e, cell)
        self._map(e, self._emit(self.schedule.inject_T, [cell]))

    def _op_RequestYState(self, rec, g: Gadget):
        s, y = rec["slice"], rec_target(rec)
        v = self._view(s)
        if y not in v.ids:
            raise ConverterError(f"LLI {rec['idx']}: Y state {y} not placed in slice {s}")
        if g.parent is not None and g.parent not in self.correction_done:
            self._correction(self.tree.gadgets[g.parent], s)
        e = self._new_event("init", s, g.gid, [rec["idx"]], state="Y", near=self.wire_of(rec["near_patch"]),
                            predicate=self._pred(g))
        self._new_lifetime(y, "resource", e, v.ids[y])
        self._add_pred_deps(e, self._pred(g))
        self.pending_init[y] = e["event_id"]

    def _correction(self, gT2: Gadget, s):
        """T2: the conditional S, predicated on T1's ZZ result (no physical work; realised by the S members)."""
        q = gT2.qubits[0]
        e = self._new_event("correction", s, gT2.gid, gate="S", wire=self.wire_of(q), lifetime=self.lifetime_of(q),
                            predicate=gT2.predicate)
        self._touch_wire(e, self.wire_of(q))
        self._add_pred_deps(e, gT2.predicate)
        self.correction_done.add(gT2.gid)

    def _add_pred_deps(self, e, pred):
        for rid in pred_results(pred):
            if rid not in self.results:
                raise ConverterError(f"{e['event_id']}: predicate uses result {rid} before it is produced")
            self._dep(e, self.results[rid]["producer"], "classical")

    def _op_Init(self, rec, g: Gadget):
        s, a = rec["slice"], rec_target(rec)
        v = self._view(s)
        if a not in v.ids:
            raise ConverterError(f"LLI {rec['idx']}: mediator {a} not placed in slice {s}")
        cell = v.ids[a]
        e = self._new_event("init", s, g.gid, [rec["idx"]], state=rec.get("state"), predicate=self._pred(g))
        self._new_lifetime(a, "mediator", e, cell)
        self._add_pred_deps(e, g.cond)
        self.pending_init[a] = e["event_id"]
        # resolved placement for the deviation table (design Sec. 1.1: placement recorded per CNOT)
        pnt = rec.get("place_next_to") or {}
        c, t = g.qubits
        resolved = "next_to_target" if adjacent(cell, self._locate(t, s)) else \
            "next_to_control" if adjacent(cell, self._locate(c, s)) else "elsewhere"
        key = f"requested={pnt.get('id') is not None and ('target' if pnt.get('id') == t else 'control')}|resolved={resolved}"
        self.stats["cnot_placements"][key] = self.stats["cnot_placements"].get(key, 0) + 1
        e["placement"] = {"requested": pnt or None, "resolved": resolved}

    def _op_MultiBodyMeasure(self, rec, g: Gadget):
        s, idx = rec["slice"], rec["idx"]
        v = self._view(s)
        obs = rec_obs(rec)
        p1, p2 = list(obs)
        for p in (p1, p2):
            if p not in v.ids:
                raise ConverterError(f"LLI {idx}: operand {p} not on the lattice in slice {s}")
        c1, c2 = v.ids[p1], v.ids[p2]
        data, routing, faces = walk_region(v, c1)
        if set(data) != {c1, c2}:
            raise ConverterError(f"LLI {idx}: merge walk from {c1} reaches {data}, expected {{{c1}, {c2}}}")
        for cell in routing:
            oid = v.opid.get(cell)
            if oid != idx:
                raise ConverterError(f"LLI {idx}: routing cell {cell} carries operation_id {oid}")
        sign = -1 if rec.get("is_negative") else 1
        e = self._new_event("measure_product", s, g.gid, [idx],
                            signed_observable={self.wire_of(p1): obs[p1], self.wire_of(p2): obs[p2]}, sign=sign,
                            lifetimes=[self.lifetime_of(p1), self.lifetime_of(p2)],
                            operand_cells=[list(c1), list(c2)], cells=[list(c) for c in routing],
                            predicate=self._pred(g))
        self._result(e, f"r{idx}")
        for p in (p1, p2):
            self._touch_wire(e, self.wire_of(p))
        for cell in routing:
            self._acquire(cell, ("R", idx), e)
        self._add_pred_deps(e, self._pred(g))
        # bit naming per certificate: u = first merge, v = second merge (CNOT)
        g.bits["u" if "u" not in g.bits else "v"] = f"r{idx}"
        if g.template_id in ("S", "SX"):
            q, y = g.qubits[0], g.resource
            cq, cy = v.ids[q], v.ids[y]
            if routing or not adjacent(cq, cy):
                raise ConverterError(f"LLI {idx}: the {g.template_id} gadget's Y state {cy} is not adjacent to {cq} "
                                     f"(routing {routing}); SWIPER's CONDITIONAL_S group cannot represent it")
            if g.template_id == "SX":
                # (note v2 6.1, 6.3): y sits on q's right neighbour, and the merge joins q's right edge to y's
                # left edge, both X (orientation letter D = rough = X); the slicer does not throw on a mismatched
                # local merge, so the converter checks it.  Emitted as SWIPER's S group, unconditioned.
                if cy != (cq[0], cq[1] + 1):
                    raise ConverterError(f"LLI {idx}: SX's Y state {cy} is not the right neighbour of its target {cq}")
                if v.orient[q][3] != "D" or v.orient[y][2] != "D":
                    raise ConverterError(f"LLI {idx}: SX's merge edges do not face X to X (target {cq} TBLR "
                                         f"{v.orient[q]}, Y state {cy} TBLR {v.orient[y]})")
            cond = None
            if g.parent is not None:
                gT = self.tree.gadgets[self.tree.gadgets[g.parent].parent]
                cond = gT.lss["t1_merge_idx"]
                if not cond:
                    raise ConverterError("T1 MERGE at index 0 cannot be a SWIPER condition")
            idxs = self._emit(self.schedule.S, cq, cy, cond)
            self.s_group[y] = tuple(idxs)
            self._map(e, idxs[:1])
            self._map(self.ev[self.pending_init.pop(y)], idxs[:1])
        else:
            idxs = self._emit(self.schedule.merge, [c1, c2], routing, faces)
            self._map(e, idxs)
            if g.template_id == "T1":
                self.tree.gadgets[g.parent].lss["t1_merge_idx"] = idxs[0]
            for p in (p1, p2):
                if p in self.pending_init:
                    self._map(self.ev[self.pending_init.pop(p)], idxs[:1])

    def _op_MeasureSinglePatch(self, rec, g: Gadget):
        s, idx = rec["slice"], rec["idx"]
        v = self._view(s)
        (p, P), = rec_obs(rec).items()
        if p not in v.ids:
            raise ConverterError(f"LLI {idx}: measured patch {p} not on the lattice in slice {s}")
        cell = v.ids[p]
        if v.activity(cell) != "Measurement":
            raise ConverterError(f"LLI {idx}: cell {cell} of patch {p} has no Measurement activity in slice {s}")
        L, w = self.lifetime_of(p), self.wire_of(p)
        sign = -1 if rec.get("is_negative") else 1
        e = self._new_event("measure_single", s, g.gid, [idx], wire=w, lifetime=L, observable=P, sign=sign,
                            destructive=True, cell=list(cell), predicate=self._pred(g))
        self._result(e, f"r{idx}")
        self._touch_wire(e, w)
        self._add_pred_deps(e, self._pred(g))
        d = self._new_event("discard", s, g.gid, [idx], lifetime=L, wire=w, discard_kind="measured", cell=list(cell),
                            predicate=self._pred(g))
        self._touch_wire(d, w)
        self.lifetimes[L]["discard_event"] = d["event_id"]
        if p in self.s_group:
            m, ym, idle, disc = self.s_group.pop(p)
            self._map(e, [ym, idle])
            self._map(d, [disc])
            # the group's IDLE is the target patch waiting during the Y readout: it belongs to the merge too
            merge_ev = self.ev[self.results[g.bits["u"]]["producer"]]
            self._map(merge_ev, [idle])
        else:
            idxs = self._emit(self.schedule.discard, [cell])
            self._map(e, idxs)
            self._map(d, idxs)
        g.bits["m" if g.template_id == "MEASURE" else "w"] = f"r{idx}"
        # frames from the certified template definitions (the compiler stream carries none)
        if g.template_id == "T1":
            self._frame(g, s, g.qubits[0], "Z", pred_xor(g.bits["w"]))
        elif g.template_id == "CNOT_cf_patched":
            c, t = g.qubits
            self._frame(g, s, c, "Z", pred_xor(g.bits["v"]))
            self._frame(g, s, t, "X", pred_xor(g.bits["u"], g.bits["w"]))

    def _frame(self, g: Gadget, s, q, pauli, pred, lli=()):
        pred = pred_and(self._pred(g), pred)
        e = self._new_event("frame_update", s, g.gid, lli, wire=self.wire_of(q), lifetime=self.lifetime_of(q),
                            pauli=pauli, predicate=pred)
        self._touch_wire(e, self.wire_of(q))
        self._add_pred_deps(e, pred)
        return e

    def _op_Gate(self, rec, g: Gadget):
        s, idx, q, op = rec["slice"], rec["idx"], rec["patches"][0], rec["single_qubit_op"]
        w, L = self.wire_of(q), self.lifetime_of(q)
        if op == "H":
            cell = self._locate(q, s)
            e = self._new_event("clifford", s, g.gid, [idx], wire=w, lifetime=L, gate="H", cell=list(cell),
                                predicate=self._pred(g))
            self._touch_wire(e, w)
            self._add_pred_deps(e, self._pred(g))
            if len(g.members) == 2 and self.rec[g.members[1]]["op"] == "RotateSingleCellPatch":
                self.pending_h[q] = e["event_id"]
            else:
                if s not in (self._view(s).idx,) or q not in self._view(s).ids:
                    raise ConverterError(f"LLI {idx}: H target {q} not visible in slice {s}")
                self._map(e, self._emit(self.schedule.idle, [cell], Duration.D))
        elif op == "S":
            raise ConverterError(f"LLI {idx}: an unexpanded SingleQubitOp S (catalytic mode?) is outside the inventory")
        elif g.template_id == "S":
            # the S gadget's frame: Z_q^(u xor w) (and the T2 predicate when nested)
            self._frame(g, s, q, op, pred_xor(g.bits["u"], g.bits["w"]), lli=[idx])
        elif g.template_id == "SX":
            # SX's frame X_q^(1 xor u xor w) (C1) as the XOR of two frame members, X on xor(u, w) and X
            # unconditioned: a predicate holds result ids only
            self._frame(g, s, q, op, pred_xor(g.bits["u"], g.bits["w"]), lli=[idx])
            self._frame(g, s, q, op, None, lli=[idx])
        else:
            e = self._new_event("clifford", s, g.gid, [idx], wire=w, lifetime=L, gate=op, cell=list(self._locate(q, s)),
                                predicate=self._pred(g))
            self._touch_wire(e, w)
            self._add_pred_deps(e, self._pred(g))

    def _op_RotateSingleCellPatch(self, rec, g: Gadget):
        s, idx, q = rec["slice"], rec["idx"], rec["patches"][0]
        w, L = self.wire_of(q), self.lifetime_of(q)
        v = self._view(s)
        follow = sorted((f for f in g.followups if f["slice"] > s), key=lambda f: f["slice"])
        k = 1 + sum(1 for f in follow if f.get("steps_to_clear", 0) > 0)
        region = sorted(c for c, o in v.opid.items() if o == idx)
        if len(region) != 2:
            raise ConverterError(f"LLI {idx}: rotation region in slice {s} is {region}, expected two cells")
        back = self.slices.get(s + k)
        if back is None or q not in back.ids or back.ids[q] not in region:
            raise ConverterError(f"LLI {idx}: patch {q} does not return into {region} at slice {s + k}")
        own = back.ids[q]
        borrowed = [c for c in region if c != own]
        prev = self.slices.get(s - 1)
        e = self._new_event("rotate_boundary", s, g.gid, [idx], wire=w, lifetime=L, cell=list(own),
                            busy_region=[list(c) for c in region], duration=k,
                            orientation_from=(prev.orient.get(q) if prev else None), orientation_to=back.orient[q],
                            predicate=self._pred(g))
        self._touch_wire(e, w)
        self._add_pred_deps(e, self._pred(g))
        for cell in borrowed:
            self._acquire(cell, ("R", idx), e)
        idxs = []
        for j in range(k):
            # ONE two-cell IDLE per busy slice (SWIPER's `idle()` would emit one instruction per patch, and the borrowed
            # neighbour's IDLE would then float free of the rotating patch in upstream's patch-based DAG -- integrator fix
            # after the C6 monitor flagged such a floating IDLE)
            step = self._emit(self._idle_region, region, Duration.D)
            idxs.extend(step)
            if j == 0 and q in self.pending_h:
                self._map(self.ev[self.pending_h.pop(q)], step)
        idxs.extend(self._emit(self.schedule.discard, borrowed))
        self._map(e, idxs)

    def _op_BusyRegion(self, rec, g):  # follow-ups are consumed by their rotation
        raise ConverterError(f"LLI {rec['idx']}: stray BusyRegion (not a rotation follow-up) is not supported")

    # -------------------------------------------------------------------------------------------------- finish
    def _finish(self, n_slices: int):
        alive = [L for L, d in self.lifetimes.items() if d["discard_event"] is None]
        for L in alive:
            d = self.lifetimes[L]
            if d["role"] != "program":
                raise ConverterError(f"{d['role']} lifetime {L} (patch {d['patch_id']}) is never measured")
            w, pid = d["wire"], d["patch_id"]
            cell = self._locate(pid, n_slices - 1)
            io = self._new_event("io", n_slices, None, io_kind="output", wire=w, commit_event=self.last_on_wire[w])
            self._touch_wire(io, w)
            e = self._new_event("discard", n_slices, None, lifetime=L, wire=w, discard_kind="traced", cell=list(cell))
            self._touch_wire(e, w)
            d["discard_event"] = e["event_id"]
            self._map(e, self._emit(self.schedule.discard, [cell]))
        if self.pending_init or self.pending_h or self.s_group:
            raise ConverterError(f"unconsumed pending state: init {self.pending_init} h {self.pending_h} s {self.s_group}")
        if self.schedule._get_final_active_patches():
            raise ConverterError(f"patches left active: {self.schedule._get_final_active_patches()}")
        self._bind_feedback()
        # gadget entries (grouping only, no deps), after all executable events, in first-member order;
        # a parent's result_bits aggregates its children's (child template prefix, e.g. "S.u")
        def depth(g):
            return 0 if g.parent is None else 1 + depth(self.tree.gadgets[g.parent])
        for g in sorted(self.tree.gadgets.values(), key=depth, reverse=True):   # children before parents
            if g.parent is not None:
                parent = self.tree.gadgets[g.parent]
                for name, rid in g.bits.items():
                    plain = g.template_id in ("T1", "CNOT_cf_patched") and name not in parent.bits
                    parent.bits.setdefault(name if plain else f"{g.template_id}.{name}", rid)
        for g in sorted(self.tree.gadgets.values(), key=lambda g: g.order_key):
            self._new_event("gadget", None, None, gadget_id=g.gid, source_gate_id=g.gate_idx, template_id=g.template_id,
                            gadget_kind=g.gadget_kind, is_dagger=g.is_dagger, events=list(g.events),
                            children=list(g.children), parent=g.parent, inputs=[self.wire_of(q) for q in g.qubits],
                            outputs=[self.wire_of(q) for q in g.qubits], result_bits=dict(g.bits),
                            predicate=g.predicate)
        inv = {}
        for g in self.tree.gadgets.values():
            inv[g.template_id] = inv.get(g.template_id, 0) + 1
        kinds = {}
        for g in self.tree.gadgets.values():
            if g.parent is None:
                kinds[g.gadget_kind] = kinds.get(g.gadget_kind, 0) + 1
        self.stats["inventory_templates"] = inv
        self.stats["inventory_gadget_kinds"] = kinds
        self.stats["counts"] = {"slices": n_slices, "lss_instructions": len(self.schedule.instructions),
                                "events": len(self.events), "lli": len(self.meta["lli"]),
                                "lli_skipped": len(self.tree.skipped)}

    def _flat_events(self, g: Gadget) -> list[str]:
        out = list(g.events)
        for c in g.children:
            out += self._flat_events(self.tree.gadgets[c])
        return out

    def _bind_feedback(self):
        """w10 (classical feedback): every classically controlled CZ's .lss instructions form one conditional group
        (`CONDITIONAL_CZ`, like SWIPER's CONDITIONAL_S) conditioned on the DECODE of its measurement.  SWIPER-SIM models a
        single-patch readout as a zero-round DISCARD, so the outcome is known when the measured patch's last region is
        decoded: the condition (the decode anchor) is the last instruction before the readout that generates syndrome
        rounds on the measured cell.  The group must follow the readout in the .lss (the fork's scheduling dependency)."""
        self.feedback_records = []
        pos = {e["event_id"]: i for i, e in enumerate(self.events)}
        for fb in self.tree.feedback:
            gcz = self.tree.gadgets[fb["gadget"]]
            meas = self.ev[self.results[fb["result_id"]]["producer"]]
            if meas["kind"] != "measure_single" or self.tree.gadgets[meas["gadget"]].template_id != "MEASURE":
                raise ConverterError(f"{gcz.gid}: its predicate {fb['result_id']} is not a MEASURE readout")
            members = sorted(self._flat_events(gcz), key=pos.get)
            group = sorted({i for eid in members for i in self.lss_map.get(eid, ())})
            (readout,) = self.lss_map[meas["event_id"]]
            cell = tuple(meas["cell"])
            ins = self.schedule.instructions
            anchor = next((i for i in range(readout - 1, -1, -1) if cell in ins[i].patches
                           and (isinstance(ins[i].duration, Duration) or ins[i].duration > 0)), None)
            if not group or anchor is None or min(group) <= readout:
                raise ConverterError(f"{gcz.gid}: cannot bind the conditional group {group} to the readout {readout} "
                                     f"(decode anchor {anchor})")
            if ins[anchor].group_instr_indices or ins[anchor].conditioned_on_idx:
                raise ConverterError(f"{gcz.gid}: the decode anchor {anchor} is itself conditional")
            for i in group:
                if ins[i].conditioned_on_idx or ins[i].group_instr_indices or ins[i].conditioned_on_completion_idx:
                    raise ConverterError(f"{gcz.gid}: .lss instruction {i} is already conditional")
                ins[i] = dataclasses.replace(ins[i], conditioned_on_idx=frozenset([anchor]),
                                             group_instr_indices=frozenset(group), group_name="CONDITIONAL_CZ")
            ins[anchor] = dataclasses.replace(ins[anchor], conditional_dependencies=ins[anchor].conditional_dependencies | frozenset(group))
            self.feedback_records.append(dict(fb, measurement_event=meas["event_id"], readout_lss=readout,
                                              decode_anchor=anchor, lss_group=group, events=members))
        self.stats["classical_feedback"] = len(self.feedback_records)

    def _document(self, n_slices: int) -> dict:
        reverse: dict[int, list[str]] = {}
        for eid, idxs in self.lss_map.items():
            for i in idxs:
                reverse.setdefault(i, []).append(eid)
        return {
            "schema": EVENTS_SCHEMA, "converter_version": CONVERTER_VERSION, "bench": self.bench,
            "compiler": self.meta.get("compiler"), "flags": self.meta.get("flags"),
            "conventions": {
                "measurement_bit": "0 <-> +1 eigenvalue of the signed observable (sign * product of the factors)",
                "cells": "[row, col] as in the .lss patches",
                "slice": "event application slice; one slice = Duration.D = d rounds; -1 = before the program, "
                         "n_slices = after the last slice",
                "order": "the events list is one topological order; only deps are normative",
                "deps": "quantum = consecutive events on the same wire; classical = result producer -> consumer; "
                        "physical = cell release -> acquire (successive occupants of the same cell)",
                "predicate": "{op: xor|and, args: [result ids | predicates]}; an event with a predicate runs iff it is 1",
                "bits": "u = first merge, v = second merge (CNOT), w = the resource / mediator readout",
                "roles": "program = source qubit (io events); resource = magic / Y state; mediator = CNOT ancilla; "
                         "routing = merge cells (measure_product.cells)",
                "boundary_orientation": "per lifetime: [[first slice, TBLR letters S=smooth(Z) D=rough(X) N=none]]",
            },
            "core_qubits": self.core,
            "wires": self.wires, "lifetimes": self.lifetimes, "results": self.results,
            "boundary_orientation": self.boundaries,
            "n_slices": n_slices, "n_lss_instructions": len(self.schedule.instructions),
            "events": self.events,
            "lss_map": self.lss_map,
            "lss_reverse_map": {str(k): v for k, v in sorted(reverse.items())},
            # w10: present only for programs with classical feedback, so every other document is unchanged
            **({"classical_feedback": self.feedback_records} if self.feedback_records else {}),
        }


# ---------------------------------------------------------------------------------------------------------- driver
def fork_commit(slicer: str | None = None) -> str | None:
    """Git commit of the compiler fork (the modified liblsqecc), for the manifest fingerprint: the repository
    of the slicer that ran (w10: given with --slicer, e.g. the w10 extension; default FORK_SLICER).  A machine that has
    the fork's binary but not its checkout states the commit in E2E_SNIPPET_FORK_COMMIT."""
    try:
        out = subprocess.run(["git", "-C", os.path.dirname(os.path.dirname(slicer or FORK_SLICER)), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        out = None
    return out or os.environ.get("E2E_SNIPPET_FORK_COMMIT") or None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def schedule_text(schedule: LatticeSurgerySchedule) -> str:
    return "\n".join(str(i) for i in schedule.instructions)


def convert_program(qasm: str, out_dir: str, slicer: str = FORK_SLICER, bench: str | None = None) -> dict:
    """Slice `qasm` with SWIPER's flags, convert, write DIR/<bench>.{lss,events.json,manifest.json}; return the manifest."""
    bench = bench or os.path.splitext(os.path.basename(qasm))[0]
    os.makedirs(out_dir, exist_ok=True)
    slices_path = os.path.join(out_dir, f"{bench}.slices.json")
    meta_path = os.path.join(out_dir, f"{bench}.meta.json")
    run_slicer(slicer, ["-q", "-i", qasm, *LAYOUT_FLAGS, "-o", slices_path, "--metaout", meta_path])
    return convert_from_files(slices_path, meta_path, out_dir, bench, qasm=qasm, slicer=slicer)


def convert_from_files(slices_path: str, meta_path: str, out_dir: str, bench: str, qasm: str | None = None,
                       slicer: str | None = None) -> dict:
    """Convert an existing slice JSON + sidecar pair (e.g. <bench>.{json,meta.json}).
    Without an explicit `qasm`, the program's QASM is taken from froq/schedules/qasm/<bench>.qasm so the manifest never
    carries a null QASM hash (implementation)."""
    os.makedirs(out_dir, exist_ok=True)
    if qasm is None:
        qasm = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schedules", "qasm", bench + ".qasm")
        if not os.path.exists(qasm):
            raise ConverterError(f"no QASM given and the program's QASM is absent: {qasm} "
                                 "(the manifest must hash the QASM the schedule was generated from)")
    with open(meta_path) as f:
        meta = json.load(f)
    conv = Converter(meta, SliceStore(iter_slices(slices_path)), bench)
    schedule, doc, stats = conv.run()
    lss_path = os.path.join(out_dir, f"{bench}.lss")
    with open(lss_path, "w") as f:
        f.write(schedule_text(schedule))
    events_path = os.path.join(out_dir, f"{bench}.events.json")
    with open(events_path, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    gate_names = {}
    for g in meta.get("gates", []):
        gate_names[g["name"]] = gate_names.get(g["name"], 0) + 1
    argv = meta.get("argv") or []
    slicer = slicer or (argv[0] if argv else None)
    manifest = {
        "bench": bench, "converter_version": CONVERTER_VERSION, "events_schema": EVENTS_SCHEMA,
        "slicer": slicer, "slicer_sha256": sha256_file(slicer) if slicer and os.path.exists(slicer) else None,
        "slicer_argv": argv, "compiler": dict(meta.get("compiler") or {}, fork_commit=fork_commit(slicer)), "compiler_flags": meta.get("flags"),
        "inputs": {"slices_json": os.path.abspath(slices_path), "meta": os.path.abspath(meta_path), "qasm": qasm},
        "fingerprints": {"qasm": sha256_file(qasm), "slices_json": sha256_file(slices_path),
                         "meta": sha256_file(meta_path), "lss": sha256_file(lss_path), "events": sha256_file(events_path)},
        "inventory_templates": stats["inventory_templates"], "inventory_gadget_kinds": stats["inventory_gadget_kinds"],
        "source_gate_counts": gate_names,
        "deviations": {**stats["deviations"], "cnot_placements": stats["cnot_placements"],
                       "terminal_measurements": 0},
        "counts": stats["counts"],
    }
    if stats.get("classical_feedback"):
        # w10: the measurement-based uncomputations kept (fork SCOUTQ_FORK_W10.md; `classical_feedback` in the events)
        manifest["classical_feedback"] = {"conditional_cz": stats["classical_feedback"],
                                          "groups": [{k: r[k] for k in ("gadget", "result_id", "register", "measured_patch",
                                                                        "readout_lss", "decode_anchor", "lss_group")}
                                                     for r in doc["classical_feedback"]]}
    with open(os.path.join(out_dir, f"{bench}.manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    return manifest


# ----------------------------------------------------------------------------------------------- snippet library
@dataclasses.dataclass
class Snippet:
    """A recovery script lowered on the program's layout (WP4 reads cells, duration and price P1 from here)."""
    lli: list[str]                           # the LLI lines sent to the slicer (after the Declare line)
    schedule: LatticeSurgerySchedule         # SWIPER object; `total_duration(d)`, `total_instruction_volume(d)`
    instructions: list[Instruction]          # == schedule.instructions (0-based idx)
    cells: list[tuple[int, int]]             # every cell any instruction touches
    n_slices: int
    events: dict                             # the snippet's own event document (same schema as a program's)

    @property
    def lss_text(self) -> str:
        return schedule_text(self.schedule)


_SNIPPET_CACHE: dict = {}
_LLI_TEXT_RE = re.compile(r"\s*\[.*\]\s*$")

# Persistent snippet cache: one JSON file per qubit count under froq/schedules/snippets, key = sha256 of (builder
# slicer sha256, layout flags, LLI text).  Snippet results are distance-independent (durations are symbolic), so a
# cache warmed on the Linux box lets a machine without the slicer binary (the Mac, ubuntu pods) lower every script
# the builder needs; with no local binary the key uses the builder sha recorded at the warmed file's top level.
SNIPPET_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schedules", "snippets")
# /2 binds entries to the building slicer's sha256 (K10: provenance is annotated into the LLI text and carried by
# the sidecar; /1 entries attributed provenance by stream order, which is not derivable under the new rule, so the
# schema bump invalidates them).
SNIPPET_CACHE_SCHEMA = "scoutq-snippet-cache/2"
_SNIPPET_DISK: dict = {}                                     # n_qubits -> {key: entry}, as read from disk
_SNIPPET_DISK_STAT: dict = {}                                # n_qubits -> the file identity _SNIPPET_DISK was read from
_SNIPPET_DISK_BUILDER: dict = {}                             # n_qubits -> slicer_sha256 recorded at the file's top level
_SNIPPET_PERSIST = os.environ.get("E2E_SNIPPET_CACHE", "1") not in ("0", "off", "no")
_SNIPPET_FLUSH_EVERY = max(1, int(os.environ.get("E2E_SNIPPET_FLUSH_EVERY", "1")))   # warmers batch writes (atexit flushes)
_SNIPPET_PENDING: dict = {}                                  # n_qubits -> {key: entry} written since the last flush
_FORK_META: dict = {}                                        # slicer path -> (fork_commit, slicer_sha256)
_SNIPPET_REFUSALS = False                                    # store genuine refusals (snippets_sx only, 6.2)


def _snippet_cache_path(n_qubits: int) -> str:
    return os.path.join(SNIPPET_CACHE_DIR, "%d.json" % n_qubits)


def use_snippet_cache(subdir: str = "snippets", cache_refusals: bool = False) -> None:
    """The persistent cache directory, froq/schedules/<subdir> (the ur_routed recovery mode reads and warms
    its own, `snippets_ur`, so compensation's files and the fingerprint they enter stay as they are). A switch flushes
    what is pending and forgets what was read from the other directory, the in-memory snippets included (a
    snippet is always served from the directory the run reads); staying put changes nothing.
     (note v2 6.2): `cache_refusals`, for the sx library's `snippets_sx` only, stores a genuine placement refusal
    met while warming under its key as {"refused": {"rc", "marker", "last_line"}}, so a run without a slicer meets
    the same PlacementRefused instead of a miss. Every call sets it, so a later call without it switches it off. A
    cached refusal is read as a refusal wherever it is found; only `snippets_sx` is ever given one."""
    global SNIPPET_CACHE_DIR, _SNIPPET_REFUSALS
    if cache_refusals and subdir != "snippets_sx":
        raise ValueError(f"refusal entries are kept in snippets_sx only, not in {subdir}")
    _SNIPPET_REFUSALS = bool(cache_refusals)
    new = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schedules", subdir)
    if new == SNIPPET_CACHE_DIR:
        return
    snippet_cache_flush()
    for d in (_SNIPPET_DISK, _SNIPPET_DISK_STAT, _SNIPPET_DISK_BUILDER, _SNIPPET_PENDING, _SNIPPET_CACHE):
        d.clear()
    SNIPPET_CACHE_DIR = new


def _snippet_builder_sha(slicer: str, n_qubits: int | None):
    """sha256 of the slicer binary whose cache entries this process may use: the local binary when it exists;
    otherwise the builder recorded at the warmed cache file's top level (the portability contract: a machine
    without the binary trusts the entries warmed on one that has it).  None when neither is known."""
    if os.path.exists(slicer):
        if slicer not in _FORK_META:
            _FORK_META[slicer] = (fork_commit(), sha256_file(slicer))
        return _FORK_META[slicer][1]
    if n_qubits is None:
        return None
    if _snippet_file_stat(n_qubits) != _SNIPPET_DISK_STAT.get(n_qubits):
        disk = _snippet_disk_read(n_qubits)
        _SNIPPET_DISK.setdefault(n_qubits, {}).update(disk)
    return _SNIPPET_DISK_BUILDER.get(n_qubits)


def _snippet_disk_key(layout_flags, lli_text: str, slicer: str | None = None, n_qubits: int | None = None):
    slicer = slicer or os.environ.get("E2E_SNIPPET_SLICER") or FORK_SLICER
    sha = _snippet_builder_sha(slicer, n_qubits)
    if sha is None:
        return None                                            # no local binary and no warmed file: unkeyable
    return hashlib.sha256(json.dumps([sha, list(layout_flags), lli_text], separators=(",", ":")).encode()).hexdigest()


def _snippet_file_stat(n_qubits: int):
    """Identity of the cache file as it is on disk now: (inode, mtime, size).  A writer renames a fresh file over it,
    so any change by another process changes the inode."""
    try:
        st = os.stat(_snippet_cache_path(n_qubits))
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _snippet_disk_read(n_qubits: int) -> dict:
    stat = _snippet_file_stat(n_qubits)                     # stat first: a replace during the read only costs a re-read
    try:
        with open(_snippet_cache_path(n_qubits)) as f:
            doc = json.load(f)
        if doc.get("schema") == SNIPPET_CACHE_SCHEMA:
            _SNIPPET_DISK_STAT[n_qubits] = stat
            _SNIPPET_DISK_BUILDER[n_qubits] = doc.get("slicer_sha256")
            return doc["snippets"]
    except (OSError, ValueError, KeyError):
        pass
    _SNIPPET_DISK_STAT[n_qubits] = stat
    _SNIPPET_DISK_BUILDER[n_qubits] = None
    return {}


def _snippet_disk_get(n_qubits: int, dkey: str):
    """The cached entry, or None.  A miss re-reads the file when it has changed since this process read it: a
    concurrent warmer may have grown it (writes are atomic, so a partial file is never observed).  Without the stat
    guard a warming run re-parses the whole growing file once per new snippet, which is quadratic in its size."""
    hit = _SNIPPET_DISK.get(n_qubits, {}).get(dkey)
    if hit is not None:
        return hit
    if n_qubits in _SNIPPET_DISK_STAT and _snippet_file_stat(n_qubits) == _SNIPPET_DISK_STAT[n_qubits]:
        return None                                         # unchanged since it was read: the miss is real
    disk = _snippet_disk_read(n_qubits)
    _SNIPPET_DISK.setdefault(n_qubits, {}).update(disk)     # merge: never drop entries pending in this process
    return disk.get(dkey)


def _snippet_from_entry(entry: dict) -> "Snippet":
    schedule = LatticeSurgerySchedule.from_str(entry["lss_text"])
    return Snippet(list(entry["lli"]), schedule, list(schedule.instructions),
                   [tuple(c) for c in entry["cells"]], entry["n_slices"], entry["events"])


def _snippet_to_entry(snip: "Snippet", slicer: str, layout_flags) -> dict:
    if slicer not in _FORK_META:
        _FORK_META[slicer] = (fork_commit(), sha256_file(slicer))
    commit, sha = _FORK_META[slicer]
    return {"lli": list(snip.lli), "lss_text": snip.lss_text, "cells": [list(c) for c in snip.cells],
            "n_slices": snip.n_slices, "events": snip.events, "layout_flags": list(layout_flags),
            "fork_commit": commit, "slicer_sha256": sha}


def _refusal_to_entry(exc: PlacementRefused, lines: list[str], slicer: str, layout_flags) -> dict:
    """(note v2 6.2): a genuine placement refusal as a snippets_sx entry. It carries the builder fields every entry
    carries, so the file keeps one builder sha (`snippet_cache_flush`) and a machine without the slicer keys by it."""
    if slicer not in _FORK_META:
        _FORK_META[slicer] = (fork_commit(), sha256_file(slicer))
    commit, sha = _FORK_META[slicer]
    return {"refused": {"rc": exc.rc, "marker": exc.marker, "last_line": exc.last_line}, "lli": list(lines),
            "layout_flags": list(layout_flags), "fork_commit": commit, "slicer_sha256": sha}


def _refusal_from_entry(entry: dict, dkey: str) -> PlacementRefused:
    """The PlacementRefused a cached refusal stands for.  Its message ends with the stored last line, the line a
    contract records for a refusal (`contract.py`'s `compile_failures`), so a cache-only run records what the warming
    run did.  A malformed refusal entry is a ConverterError (infrastructure, never a fence)."""
    r = entry.get("refused")
    if not (isinstance(r, dict) and isinstance(r.get("rc"), int) and r.get("marker") in _PLACEMENT_REFUSAL_MARKERS
            and isinstance(r.get("last_line"), str) and r["last_line"].strip() and "\n" not in r["last_line"]):
        raise ConverterError(f"snippet cache entry {dkey}: malformed refusal record {r!r}")
    return PlacementRefused(f"slicer refused (cached refusal {dkey}, rc={r['rc']}): {r['marker']}\n{r['last_line']}",
                            rc=r["rc"], marker=r["marker"], last_line=r["last_line"])


def snippet_cache_flush():
    """Merge pending entries into froq/schedules/snippets/<n_qubits>.json: flock the file, re-read, merge, then
    replace it atomically (tmp + rename).  Re-read + merge under the lock makes concurrent writers safe; the lock is
    what makes the merge race-free, which a bare re-read + merge would not be."""
    for n_qubits, pend in list(_SNIPPET_PENDING.items()):
        if not pend:
            continue
        os.makedirs(SNIPPET_CACHE_DIR, exist_ok=True)
        path = _snippet_cache_path(n_qubits)
        with open(path + ".lock", "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                snippets = _snippet_disk_read(n_qubits)
                snippets.update(pend)
                builders = {e.get("slicer_sha256") for e in snippets.values()}
                commits = {e.get("fork_commit") for e in snippets.values()}
                builder_sha = builders.pop() if len(builders) == 1 else None
                tmp = "%s.tmp.%d" % (path, os.getpid())
                with open(tmp, "w") as f:
                    json.dump({"schema": SNIPPET_CACHE_SCHEMA, "n_qubits": n_qubits, "snippets": snippets,
                               "slicer_sha256": builder_sha,
                               "fork_commit": commits.pop() if len(commits) == 1 else None},
                              f, separators=(",", ":"))
                os.replace(tmp, path)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        pend.clear()
        _SNIPPET_DISK[n_qubits] = snippets
        _SNIPPET_DISK_STAT[n_qubits] = _snippet_file_stat(n_qubits)   # what we just wrote is what we hold
        _SNIPPET_DISK_BUILDER[n_qubits] = builder_sha


atexit.register(snippet_cache_flush)


def _snippet_disk_store(n_qubits: int, dkey: str, entry: dict):
    _SNIPPET_PENDING.setdefault(n_qubits, {})[dkey] = entry
    _SNIPPET_DISK.setdefault(n_qubits, {})[dkey] = entry
    if sum(len(p) for p in _SNIPPET_PENDING.values()) >= _SNIPPET_FLUSH_EVERY:
        snippet_cache_flush()


def snippet_cache_identity(n_qubits: int) -> dict:
    """The identity of the persistent snippet cache a run of an `n_qubits` program consumes, recorded in the result
    fingerprint (impl, A2/B1(c)): the cache schema, the sha256 of the cache file as it is on disk and
    the builder slicer sha256 recorded at the file's top level (the portability contract: entries are trusted because
    that builder produced them). `file` / `builder_slicer_sha` are None when the disk layer is off or no warmed file
    exists; with the real backend such a run cannot complete (a cache miss raises) and its fingerprint never pairs
    with a run that consumed a real cache."""
    rec = dict(schema=SNIPPET_CACHE_SCHEMA, n_qubits=n_qubits, file=None, builder_slicer_sha=None)
    path = _snippet_cache_path(n_qubits)
    if not (_SNIPPET_PERSIST and os.path.isfile(path)):
        return rec
    rec["file"] = sha256_file(path)[:16]
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(4096).decode("utf-8", "replace")
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        rec["file"] = None
        return rec
    m = re.search(r'"schema"\s*:\s*"([^"]+)"', head)
    if not m or m.group(1) != SNIPPET_CACHE_SCHEMA:
        rec["schema"] = m.group(1) if m else None      # a foreign / corrupt file: record what it claims, never silently
        return rec
    m = re.search(r'"slicer_sha256"\s*:\s*"([0-9a-f]{16,64})"', tail)
    rec["builder_slicer_sha"] = m.group(1)[:16] if m else None
    return rec


_SNIPPET_MEMBER_KINDS = {"H": "H", "S": "S", "CNOT_cf_patched": "CNOT", "SX": "SX"}   # script template -> gadget_kind


def _snippet_annotation(member: int, kind: str) -> str:
    """The provenance annotation on every LLI line a snippet is built from (the QASM parser's `; // %...` syntax;
    the fork's LLI parser fills LSInstruction::provenance from it -- SCOUTQ_FORK.md Sec. 1.4).  `member` is the
    script member index; for LLI input the sidecar carries it in `gate_idx`."""
    return f" ; // %gadget={member},kind={kind},member={member},role=script"


def lower_script(script: list[dict], n_qubits: int) -> list[str]:
    """C3 script members {"template": "H"|"S"|"SX"|"CNOT_cf_patched", "wires": [...]} -> explicit LLI lines.
    The teleported S is written out (RequestYState...): LLI input is the lowered stream, there is no gate-level
    expansion to invoke. SX (note v2 6.1) is written the same way with its |Y> on q's X side
    (`RequestYState y q:X`); SX^dagger is SX plus a frame X member, which writes no line. H is left to the
    compiler, whose boundary-rotation stream injects the RotateSingleCellPatch for LLI input as well (verified: an
    explicit rotation would be doubled), so a recovery script carries the same physical work per H as the program does.
    Every line ends with a `_snippet_annotation` provenance annotation, so the fork's --metaout sidecar states
    which script member each record -- explicit or injected follow-up -- belongs to (K10: operation ids and
    app_seq identify and order operations; ownership is never reconstructed from stream order)."""
    lines, fresh = [], n_qubits
    for i, m in enumerate(script):
        t, w = m["template"], m["wires"]
        if t == "H":
            member = [f"HGate {w[0]}"]
        elif t == "S":
            q, y = w[0], fresh
            fresh += 1
            member = [f"RequestYState {y} {q}", f"MultiBodyMeasure {y}:Z,{q}:Z", f"MeasureSinglePatch {y} X",
                      f"LogicalPauli {q} Z"]
        elif t == "SX":
            q, y = w[0], fresh
            fresh += 1
            member = [f"RequestYState {y} {q}:X", f"MultiBodyMeasure {y}:X,{q}:X", f"MeasureSinglePatch {y} Z",
                      f"LogicalPauli {q} X"]
        elif t == "CNOT_cf_patched":
            c, tq, a = w[0], w[1], fresh
            fresh += 1
            member = [f"Init {a} |+> {tq}:X", f"MultiBodyMeasure {c}:Z,{a}:Z", f"MultiBodyMeasure {a}:X,{tq}:X",
                      f"MeasureSinglePatch {a} Z"]
        else:
            raise ConverterError(f"script template {t} is not a certified recovery member (H, S, SX, CNOT_cf_patched)")
        annot = _snippet_annotation(i, _SNIPPET_MEMBER_KINDS[t])
        lines += [l + annot for l in member]
    return lines


def parse_lli_text(text: str) -> dict:
    """Parse one printed LLI (the compiler's `operator<<` form) into a sidecar-shaped record (no provenance).
    A trailing `; ...` comment -- including the `; // %...` provenance annotation -- is ignored."""
    text = text.split(";", 1)[0].strip()
    base = _LLI_TEXT_RE.sub("", text)
    parts = base.split(" ")
    name = parts[0]
    if name == "DeclareLogicalQubitPatches":
        return {"op": name, "patches": [int(x) for x in parts[1].split(",")]}
    if name == "MeasureSinglePatch":
        neg = parts[2].startswith("-")
        return {"op": name, "patches": [int(parts[1])], "observable": {parts[1]: parts[2].lstrip("-")}, "is_negative": neg}
    if name == "MultiBodyMeasure":
        d = parts[1]
        neg = d.startswith("-")
        obs = dict(p.split(":") for p in d.lstrip("-").split(","))
        return {"op": name, "patches": [int(k) for k in obs], "observable": obs, "is_negative": neg}
    if name == "Init":
        rec = {"op": name, "patches": [int(parts[1])], "clients": [],
               "state": {"|0>": "Zero", "0": "Zero", "|+>": "Plus", "+": "Plus"}[parts[2]]}
        if len(parts) > 3:
            pid, op = parts[3].split(":")
            rec["place_next_to"] = {"id": int(pid), "op": op}
        return rec
    if name in ("RequestMagicState", "RequestYState"):
        near, colon, side = parts[2].partition(":")
        rec = {"op": name, "patches": [int(parts[1])], "clients": [], "near_patch": int(near),
               "state": "Magic" if name == "RequestMagicState" else "Y"}
        if colon:
            # (note v2 6.1): `RequestYState y q:X` asks for the |Y> on q's X side; any other side is a parse error
            if name != "RequestYState" or side != "X":
                raise ConverterError(f"cannot parse LLI text: {text!r} (only RequestYState takes a side, and only X)")
            rec["near_boundary"] = "X"
        return rec
    m = re.match(r"^([XYZHS])Gate$", name)
    if m:
        return {"op": "Gate", "patches": [int(parts[1])], "single_qubit_op": m.group(1)}
    if name == "LogicalPauli":
        # an LLI-input frame Pauli; the compiler records it as a Gate record in the sidecar
        return {"op": "Gate", "patches": [int(parts[1])], "single_qubit_op": parts[2]}
    if name == "RotateSingleCellPatch":
        return {"op": name, "patches": [int(parts[1])]}
    if name == "BusyRegion":
        cells = [{"row": int(r), "col": int(c)} for r, c in re.findall(r"\((\d+),(\d+)\)", text)]
        steps = int(re.search(r"StepsToClear\((\d+)\)", text).group(1))
        return {"op": name, "patches": [], "cells": cells, "steps_to_clear": steps}
    if name in ("BellPairInit", "BellBasedCNOT", "Reset"):
        return {"op": name, "patches": []}
    raise ConverterError(f"cannot parse LLI text: {text!r}")


def _snippet_members(lines: list[str]) -> list[list[str]]:
    """Group a snippet's explicit LLI lines into script members by template arity: one line for a gate, four for a
    teleported S or SX (RequestYState starts it) or a patched CNOT (Init starts it).  Raises on anything else."""
    out, i = [], 0
    while i < len(lines):
        rec = parse_lli_text(lines[i])
        if rec["op"] == "Gate" and rec.get("single_qubit_op") in ("H", "X", "Y", "Z"):
            n = 1
        elif rec["op"] in ("RequestYState", "Init"):
            n = 4
        else:
            raise ConverterError(f"snippet line {i}: {lines[i]!r} does not start a certified template")
        if i + n > len(lines):
            raise ConverterError(f"snippet line {i}: {lines[i]!r} starts a {n}-line template but only "
                                 f"{len(lines) - i} lines remain")
        out.append(lines[i:i + n])
        i += n
    return out


def snippet_meta_from_sidecar(sidecar: dict, member_lines: list[list[str]], n_qubits: int) -> dict:
    """The fork's `--metaout` sidecar of an LLI-input snippet run; ownership is taken from the sidecar itself.

    K10: every line `lower_script` / `compile_snippet` writes carries a provenance
    annotation and the fork's injected RotateSingleCellPatch / BusyRegion follow-ups inherit it, so each record's
    `provenance` states its script member directly (`gate_idx` = the script member index for LLI input,
    SCOUTQ_FORK.md Sec. 1.4/9). Nothing is reconstructed from stream order: `app_seq` only orders operations.
    Raises when a script record lacks provenance or names a member outside the script. Per member, the explicit
    records (in idx order, the order the stream was consumed) must be exactly the lines written for that member --
    ownership comes from the annotation; the sequence check only verifies the compiler applied what was written."""
    recs = []
    for r in sidecar["lli"]:
        if r is None:
            raise ConverterError("snippet: an LLI record was never applied")
        recs.append(dict(r))
    if list(sidecar.get("core_qubits") or []) != list(range(n_qubits)):
        raise ConverterError(f"snippet: sidecar core_qubits {sidecar.get('core_qubits')} != 0..{n_qubits - 1}")
    n_members = len(member_lines)
    want = {m: [parse_lli_text(l) for l in lines] for m, lines in enumerate(member_lines)}
    seen = {m: 0 for m in want}
    for rec in recs:
        if rec["op"] == "DeclareLogicalQubitPatches":
            continue                                        # the layout declaration is not a script member
        prov = rec.get("provenance")
        if not prov:
            raise ConverterError(f"snippet: sidecar record {rec['idx']} ({rec['op']}) carries no provenance: the "
                                 "snippet was not lowered from annotated lines (fork Sec. 1.4 support required)")
        member = prov.get("gate_idx")
        if not isinstance(member, int) or not 0 <= member < n_members:
            raise ConverterError(f"snippet: sidecar record {rec['idx']} ({rec['op']}) names script member "
                                 f"{member}, but the script has {n_members} members")
        rec["provenance"] = dict(prov)
        if rec["op"] in ("RotateSingleCellPatch", "BusyRegion"):
            continue                                        # injected follow-up: ownership inherited from its H
        k = seen[member]
        if k >= len(want[member]):
            raise ConverterError(f"snippet: more explicit records for member {member} than its "
                                 f"{len(want[member])} written lines")
        w = want[member][k]
        line = member_lines[member][k]
        if rec["op"] != w["op"] or w["patches"][0] not in rec["patches"]:
            raise ConverterError(f"snippet: sidecar record {rec['idx']} ({rec['op']} {rec['patches']}) does not "
                                 f"match line {k + 1} of member {member}: {line!r}")
        if w["op"] == "Gate" and rec.get("single_qubit_op") != w.get("single_qubit_op"):
            raise ConverterError(f"snippet: sidecar record {rec['idx']} ({rec.get('single_qubit_op')}) does not "
                                 f"match line {k + 1} of member {member}: {line!r}")
        # the program path's rec_target() convention: the near / client patches are listed in `clients`
        if rec["op"] in ("RequestMagicState", "RequestYState"):
            if rec.get("near_patch") != w["near_patch"]:
                raise ConverterError(f"snippet: sidecar record {rec['idx']} near_patch {rec.get('near_patch')} != "
                                     f"line {k + 1} of member {member}: {line!r}")
            # the fork writes `near_boundary` only for the X-side request, so an old line never carries one
            if rec.get("near_boundary") != w.get("near_boundary"):
                raise ConverterError(f"snippet: sidecar record {rec['idx']} near_boundary {rec.get('near_boundary')!r} "
                                     f"!= line {k + 1} of member {member}: {line!r}")
            rec["clients"] = [rec["near_patch"]]
        elif rec["op"] == "Init":
            rec["clients"] = [x for x in rec["patches"] if x != w["patches"][0]]
        seen[member] = k + 1
    missing = {m: len(want[m]) - k for m, k in seen.items() if k != len(want[m])}
    if missing:
        raise ConverterError(f"snippet: the sidecar is missing explicit records (unmatched lines per member: {missing})")
    meta = dict(sidecar)
    meta["lli"] = recs
    return meta


def compile_snippet(script, n_qubits: int, slicer: str = FORK_SLICER, layout_flags=LAYOUT_FLAGS,
                    work_dir: str | None = None) -> Snippet:
    """Lower a recovery script on the layout of an `n_qubits` program and return SWIPER-style instructions with cells.

    `script`: a C3 member list [{"template": "H"|"S"|"SX"|"CNOT_cf_patched", "wires": [program patch ids]}...]
    or an LLI text (str) that uses only those templates' instructions. The fork slicer is run in LLI mode with
    `DeclareLogicalQubitPatches 0..n-1` so the program's qubits sit at the same layout cells as in the program;
    resource / mediator ids start at n_qubits. Every LLI line is annotated with its script member
    (`_snippet_annotation`), and the metadata takes ownership from the fork's `--metaout` sidecar
    (`snippet_meta_from_sidecar`): the sidecar's `provenance` -- explicit records and inherited follow-ups alike --
    states the member; operation ids and `app_seq` only identify and order operations. `E2E_SNIPPET_SLICER`
    overrides `slicer` (portability tests point it at a nonexistent path to force cache-only operation). Results
    are cached in memory by (slicer, flags, n_qubits, LLI text), and persistently in
    froq/schedules/snippets/<n_qubits>.json keyed by sha256 of (builder slicer sha256, flags, LLI text): a
    persistent hit returns the Snippet without running the slicer; a miss with no executable slicer on this machine
    raises ConverterError naming the key. A slicer that ran and reported it cannot place / route the script raises
    PlacementRefused; a timeout or any other slicer failure raises ConverterError (infrastructure, never a fence).
     (note v2 6.2): where `use_snippet_cache("snippets_sx", cache_refusals=True)` switched it, that refusal is
    also stored under the key, and a later hit on it raises PlacementRefused with the stored last line."""
    slicer = os.environ.get("E2E_SNIPPET_SLICER") or slicer
    if isinstance(script, str):
        raw = []
        for l in script.strip().splitlines():
            l = l.split(";", 1)[0].strip()                # comments/annotations are rewritten below
            if l and not l.startswith("Declare"):
                raw.append(l)
        member_lines = _snippet_members(raw)
        lines = []
        for i, ml in enumerate(member_lines):             # annotate each line with its script member
            rec = parse_lli_text(ml[0])
            if rec["op"] == "RequestYState":                 # an X-side request starts an SX member
                kind = "SX" if rec.get("near_boundary") == "X" else "S"
            else:
                kind = "CNOT" if rec["op"] == "Init" else rec["single_qubit_op"]
            lines += [l + _snippet_annotation(i, kind) for l in ml]
    else:
        lines = lower_script(script, n_qubits)
        member_lines = _snippet_members(lines)
    lli_text = "\n".join(lines)
    key = (slicer, tuple(layout_flags), n_qubits, lli_text)
    if key in _SNIPPET_CACHE:
        return _SNIPPET_CACHE[key]
    dkey = _snippet_disk_key(layout_flags, lli_text, slicer=slicer, n_qubits=n_qubits)
    if _SNIPPET_PERSIST and dkey is not None:
        entry = _snippet_disk_get(n_qubits, dkey)
        if entry is not None:
            if "refused" in entry:                          # a refusal the sx library's warming kept (6.2)
                raise _refusal_from_entry(entry, dkey)
            snip = _snippet_from_entry(entry)
            _SNIPPET_CACHE[key] = snip
            return snip
    if not (os.path.isfile(slicer) and os.access(slicer, os.X_OK)):
        raise ConverterError("snippet cache miss and no slicer on this machine: warm the cache on the Linux box "
                             "(key %s, n_qubits=%d, cache file %s, slicer %r)"
                             % (dkey or "unkeyable: no local slicer and no warmed cache file", n_qubits,
                                _snippet_cache_path(n_qubits), slicer))
    tmp = tempfile.mkdtemp(prefix="snippet_", dir=work_dir)
    src = os.path.join(tmp, "snippet.lli")
    with open(src, "w") as f:
        f.write(f"DeclareLogicalQubitPatches {','.join(str(i) for i in range(n_qubits))}\n{lli_text}\n")
    js, mp = (os.path.join(tmp, n) for n in ("slices.json", "meta.json"))
    try:
        run_slicer(slicer, ["-i", src, *layout_flags, "-o", js, "--metaout", mp], SNIPPET_TIMEOUT_S)
    except PlacementRefused as exc:
        if _SNIPPET_REFUSALS and _SNIPPET_PERSIST and dkey is not None:
            _snippet_disk_store(n_qubits, dkey, _refusal_to_entry(exc, lines, slicer, layout_flags))
        raise
    with open(mp) as f:
        sidecar = json.load(f)
    n_slices = sum(1 for _ in iter_slices(js))
    meta = snippet_meta_from_sidecar(sidecar, member_lines, n_qubits)
    conv = Converter(meta, SliceStore(iter_slices(js)), "snippet")
    schedule, doc, _ = conv.run()
    # program wires are not discarded by a snippet: drop the trailing io/discard the converter adds for them
    keep = [i for i in schedule.instructions if not (i.name == "DISCARD" and doc["lss_reverse_map"].get(str(i.idx))
                                                     and all(doc["lss_map"][e] == [i.idx] and conv.ev[e]["kind"] == "discard"
                                                             and conv.ev[e].get("discard_kind") == "traced"
                                                             for e in doc["lss_reverse_map"][str(i.idx)]))]
    trimmed = LatticeSurgerySchedule()
    for ins in keep:
        trimmed._add_instruction(ins)
    cells = sorted({c for ins in keep for c in ins.patches})
    snip = Snippet(lines, trimmed, list(trimmed.instructions), cells, n_slices, doc)
    _SNIPPET_CACHE[key] = snip
    if _SNIPPET_PERSIST and dkey is not None:
        _snippet_disk_store(n_qubits, dkey, _snippet_to_entry(snip, slicer, layout_flags))
    return snip


# ------------------------------------------------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--qasm", help="QASM program: run the slicer, then convert")
    src.add_argument("--slices", help="existing slice JSON (with --meta): convert without slicing")
    ap.add_argument("--meta", help="sidecar written by --metaout (with --slices)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--slicer", default=FORK_SLICER)
    ap.add_argument("--bench", default=None)
    a = ap.parse_args(argv)
    if a.qasm:
        m = convert_program(a.qasm, a.out_dir, a.slicer, a.bench)
    else:
        if not a.meta:
            ap.error("--slices requires --meta")
        bench = a.bench or os.path.basename(a.slices).split(".")[0]
        m = convert_from_files(a.slices, a.meta, a.out_dir, bench)
    print(json.dumps({k: m[k] for k in ("bench", "counts", "inventory_templates", "deviations")}, indent=1))


if __name__ == "__main__":
    main()
