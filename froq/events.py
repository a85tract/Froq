"""The semantic event stream `<bench>.events.json` (design Sec. 1.3) as the builder, the device and the checker read it.

Why this exists: the .lss carries coordinates and durations only; every semantic decision of Route A (what a MERGE
measures, which wire a patch carries, which results a predicate consumes, where a gadget begins and ends) is read from the
event stream, and ONLY its dependency edges are normative (the list order is one topological order).  This module loads the
file once, builds the indices everyone needs (events by id, by wire, by result id; deps graph; gadget nesting; lss_map both
ways) and segments gadgets into ATOMS (Sec. 2.4: a certified gadget or gadget segment; any member whose execution depends on
a decoded result -- a predicate -- opens a new segment, so the T gadget is T1 + T2).

Schema accepted (the converter's format is followed where it exists; deviations are listed in the WP4 report):
  {"header": {...}, "events": [event, ...], "gadgets": {gadget_id: gadget} (optional; kind="gadget" entries in the list are
   accepted too), "lss_map": {event_id: [lss idx, ...]}, "lss_reverse": {lss idx: [event_id, ...]} (optional, derived)}
  event: {event_id, kind, deps: [{on, kind}], gadget: innermost gadget id (or nested object with gadget_id), ...kind fields}
  gadget: {gadget_id, template_id, events: [event ids and nested gadget ids], inputs: {local wire: program wire},
           outputs: {...}, result_bits: {local result name: result id}, predicate (for a nested conditional gadget)}
  predicate: any JSON structure; the result ids it references are every string that is a known result id.
Roles: `init.role` per lifetime; program wires = the `io` events' wires (roles are declarations, never run lengths).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import networkx as nx

QUANTUM_KINDS = {"init", "measure_product", "measure_single", "clifford", "rotate_boundary", "busy_region", "transfer", "discard"}
PHYSICAL_KINDS = {"init", "measure_product", "measure_single", "clifford", "rotate_boundary", "busy_region", "transfer", "discard"}
CONSUMER_KINDS = {"frame_update", "correction"}


@dataclass
class Event:
    event_id: str
    kind: str
    raw: dict
    deps: list                      # [(on, kind)]
    gadget: str | None              # innermost gadget id
    wires: frozenset                # wires the event acts on (empty for classical-only events)
    result_id: str | None
    predicate_ids: frozenset        # result ids the event's predicate references (empty = unconditional)
    lifetimes: frozenset
    pos: int                        # position in the list (one topological order)


@dataclass
class Gadget:
    gadget_id: str
    template_id: str
    members: list                   # event ids and nested gadget ids in list order
    inputs: dict                    # local wire name -> program wire
    outputs: dict
    result_bits: dict               # local result name -> result id
    parent: str | None
    predicate_ids: frozenset        # a nested conditional gadget's predicate (e.g. the T gadget's S on u)
    raw: dict


@dataclass
class Atom:
    """A certified gadget or gadget segment: its member events, its C2 template id, its wires, and its .lss instructions."""
    atom_id: str
    gadget_id: str
    template_id: str                # the C2 table's id for this segment ("T1", "T2", "CNOT_cf", ...)
    events: tuple                   # member event ids (executable members, nested gadgets flattened)
    predicate_ids: frozenset        # decode dependency that opens the segment (empty for the first segment)
    inputs: dict
    outputs: dict
    result_bits: dict
    segment: int                    # 0-based segment index within the gadget


class EventStream:
    def __init__(self, doc: dict):
        self.doc = doc
        self.header = doc.get("header", {})
        self.events: dict[str, Event] = {}
        self.order: list[str] = []
        self.gadgets: dict[str, Gadget] = {}
        self.result_ids: set[str] = set()
        self.lifetime_wire: dict[str, str] = {}
        self.lifetime_cells: dict[str, tuple] = {}
        self.lifetime_role: dict[str, str] = {}
        self.lifetime_init: dict[str, str] = {}
        self.lifetime_end: dict[str, str] = {}
        self._load()

    @staticmethod
    def load(path: str) -> "EventStream":
        return EventStream(json.load(open(path)))

    # ------------------------------------------------------------------ loading
    def _load(self):
        doc = self.doc
        raw_gadgets = dict(doc.get("gadgets", {}))
        for e in doc["events"]:
            if e.get("kind") == "gadget":
                g = e.get("gadget", e) if isinstance(e.get("gadget"), dict) else e
                raw_gadgets[g["gadget_id"]] = g
        self.wire_table = doc.get("wires", {})                    # scoutq-events/1: wire -> {role, patch_id, lifetimes}
        self.lifetime_table = doc.get("lifetimes", {})            # lifetime -> {wire, role, cell, init_event, discard_event}
        self.patch_id = {w: t.get("patch_id") for w, t in self.wire_table.items()}
        # result ids first: predicates are matched against them
        for e in doc["events"]:
            if e.get("kind") == "gadget":
                continue
            if e.get("result_id"):
                self.result_ids.add(e["result_id"])
        for g in raw_gadgets.values():
            self.result_ids.update((g.get("result_bits") or {}).values())
        def wires_list(x):                                        # inputs / outputs: a list of program wires by template position
            return list(x.values()) if isinstance(x, dict) else list(x or [])
        for g in raw_gadgets.values():
            members = list(g.get("events", [])) + [c for c in g.get("children", []) if c not in g.get("events", [])]
            self.gadgets[g["gadget_id"]] = Gadget(gadget_id=g["gadget_id"], template_id=g["template_id"], members=members,
                                                  inputs=wires_list(g.get("inputs")), outputs=wires_list(g.get("outputs", g.get("inputs"))),
                                                  result_bits=dict(g.get("result_bits", {})), parent=g.get("parent"),
                                                  predicate_ids=self._pred_ids(g.get("predicate")), raw=g)
        for gid, g in self.gadgets.items():
            for m in g.members:
                if m in self.gadgets and self.gadgets[m].parent is None:
                    self.gadgets[m].parent = gid
        # lifetimes from init events (cells, role, wire), completed by the top-level lifetime table when present
        def cells_of(e):
            """An init event's ONE canonical cell field is `cell` (round-9): the `cells` list this
            used to PREFER is an unvalidated alias -- adding it beside a legitimate `cell` moved a program patch into
            the resource class while G-R2, C4, C5a and every runtime check stayed clean. G-R2's `check_schema` now
            reports `cells` on an init as `[schema]`; the hand-written `tests_fixtures/fixture3` predates the
            converter and names the cell `cells`, so that list is read only when there is no `cell` at all."""
            if e.get("cell"): return (tuple(e["cell"]),)
            if e.get("cells"): return tuple(tuple(c) for c in e["cells"])
            return ()
        for e in doc["events"]:
            if e.get("kind") == "init":
                lt = e["lifetime"]
                self.lifetime_wire[lt] = e.get("wire")
                self.lifetime_cells[lt] = cells_of(e)
                self.lifetime_role[lt] = e.get("role", "unknown")
                self.lifetime_init[lt] = e["event_id"]
            if e.get("kind") == "discard":
                self.lifetime_end[e["lifetime"]] = e["event_id"]
        for lt, t in self.lifetime_table.items():
            self.lifetime_wire.setdefault(lt, t.get("wire")); self.lifetime_role.setdefault(lt, t.get("role", "unknown"))
            if not self.lifetime_cells.get(lt) and t.get("cell"): self.lifetime_cells[lt] = (tuple(t["cell"]),)
            if t.get("init_event"): self.lifetime_init.setdefault(lt, t["init_event"])
            if t.get("discard_event"): self.lifetime_end.setdefault(lt, t["discard_event"])
        pos = 0
        for e in doc["events"]:
            if e.get("kind") == "gadget":
                continue
            gid = e.get("gadget")
            if isinstance(gid, dict):
                gid = gid.get("gadget_id")
            ev = Event(event_id=e["event_id"], kind=e["kind"], raw=e, deps=[(d["on"], d.get("kind", "quantum")) for d in e.get("deps", [])],
                       gadget=gid, wires=self._wires_of(e), result_id=e.get("result_id"), predicate_ids=self._pred_ids(e.get("predicate")),
                       lifetimes=self._lifetimes_of(e), pos=pos)
            self.events[ev.event_id] = ev; self.order.append(ev.event_id); pos += 1
        # indices
        self.dag = nx.DiGraph()
        self.dag.add_nodes_from(self.order)
        for ev in self.events.values():
            for on, kind in ev.deps:
                assert on in self.events, "dep on unknown event %s (of %s)" % (on, ev.event_id)
                self.dag.add_edge(on, ev.event_id, kind=kind)
        self.by_wire: dict[str, list[str]] = {}
        self.by_wire_pos: dict[str, list[int]] = {}
        self.by_result: dict[str, str] = {}
        self.consumers: dict[str, list[str]] = {}
        for eid in self.order:
            ev = self.events[eid]
            for w in ev.wires:
                self.by_wire.setdefault(w, []).append(eid); self.by_wire_pos.setdefault(w, []).append(ev.pos)
            if ev.result_id:
                self.by_result[ev.result_id] = eid
            for r in ev.predicate_ids:
                self.consumers.setdefault(r, []).append(eid)
        self._sort_members()
        for g in self.gadgets.values():
            for r in g.predicate_ids:
                for m in self.flatten(g.gadget_id):
                    self.consumers.setdefault(r, []).append(m)
        lm = doc.get("lss_map", {})
        self.lss_map: dict[str, tuple] = {eid: tuple(int(i) for i in lm.get(eid, ())) for eid in self.order}
        self.lss_reverse: dict[int, list[str]] = {}
        for eid, idxs in self.lss_map.items():
            for i in idxs:
                self.lss_reverse.setdefault(i, []).append(eid)
        self.io_outputs_of_wire: dict[str, list] = {}
        self.io_outputs_of_result: dict[str, list] = {}
        for eid in self.order:
            ev = self.events[eid]
            if ev.kind == "io" and ev.raw.get("io_kind") == "output":
                self.io_outputs_of_wire.setdefault(ev.raw.get("wire"), []).append(eid)
                commit = ev.raw.get("commit_event")
                if commit in self.events and self.events[commit].result_id:
                    self.io_outputs_of_result.setdefault(self.events[commit].result_id, []).append(eid)
        self.program_wires: list[str] = sorted({e["wire"] for e in doc["events"] if e.get("kind") == "io"} | set(self.header.get("program_wires", []))
                                               | {w for w, t in self.wire_table.items() if t.get("role") == "program"})
        self.roles_by_wire: dict[str, str] = {w: t.get("role", "unknown") for w, t in self.wire_table.items()}
        for lt, w in self.lifetime_wire.items():
            if w is not None:
                self.roles_by_wire.setdefault(w, self.lifetime_role[lt])
        for w in self.program_wires:
            self.roles_by_wire[w] = "program"
        # lifetimes as position intervals (bisection for wire_cell / occupied_cells; cell -> lifetimes for roles)
        self._lt_by_wire: dict[str, list] = {}
        self.lifetimes_by_cell: dict[tuple, list] = {}
        for lt, init in self.lifetime_init.items():
            if init not in self.events:
                continue
            i0 = self.events[init].pos
            end = self.lifetime_end.get(lt)
            i1 = self.events[end].pos if end in self.events else 10 ** 9
            self._lt_by_wire.setdefault(self.lifetime_wire.get(lt), []).append((i0, i1, lt))
            for c in self.lifetime_cells.get(lt, ()):
                self.lifetimes_by_cell.setdefault(c, []).append((i0, i1, self.lifetime_role.get(lt, "unknown")))
        for v in self._lt_by_wire.values():
            v.sort()
        import numpy as _np
        ivs = [(i0, i1, lt) for v in self._lt_by_wire.values() for (i0, i1, lt) in v]
        self._lt_i0 = _np.array([x[0] for x in ivs], dtype=_np.int64); self._lt_i1 = _np.array([x[1] for x in ivs], dtype=_np.int64)
        self._lt_ids = [x[2] for x in ivs]

    def _pred_ids(self, pred) -> frozenset:
        if pred is None:
            return frozenset()
        found = set()

        def walk(x):
            if isinstance(x, str):
                if x in self.result_ids: found.add(x)
            elif isinstance(x, dict):
                for v in x.values(): walk(v)
            elif isinstance(x, (list, tuple)):
                for v in x: walk(v)
        walk(pred)
        return frozenset(found)

    def _wires_of(self, e: dict) -> frozenset:
        """The event's wires, DERIVED from the primary fields -- never trusted from the redundant `wires` list.
        Round-8 review (K21, A1b): a present `wires` field that disagrees with the derivation raises ValueError
        (G-R2's `check_wires` reports the same inconsistency as `[wires]`): the builder must never see a different
        wire set than the validator did -- a CNOT's members with `wires` cleared previously made the builder's
        frontier skip a real CNOT.  `clifford` / `correction` events, whose primary field IS `wires`, are derived
        from their lifetime's wire when the lifetime is known (the fixture's lifetime-less Clifford keeps its own
        field)."""
        k = e["kind"]
        if k == "measure_product":
            out = frozenset(e["signed_observable"].keys())
        elif k in ("measure_single", "init", "discard", "transfer", "frame_update", "io"):
            out = frozenset([e["wire"]]) if e.get("wire") is not None else frozenset()
        elif k in ("clifford", "correction"):
            w = self.lifetime_wire.get(e.get("lifetime"))
            out = frozenset([w]) if w else frozenset(e.get("wires", []))
        elif k in ("rotate_boundary", "busy_region"):
            w = self.lifetime_wire.get(e.get("lifetime"))
            out = frozenset([w]) if w else frozenset()
        else:
            out = frozenset(e.get("wires", []))
        have = e.get("wires")
        if have is not None and frozenset(have) != out:
            raise ValueError("event %s (%s) declares wires %s, the primary fields give %s"
                             % (e.get("event_id"), k, have, sorted(out)))
        return out

    def _lifetimes_of(self, e: dict) -> frozenset:
        out = set()
        if e.get("lifetime"): out.add(e["lifetime"])
        for key in ("lifetimes", "from_lifetimes", "to_lifetimes"):
            out.update(e.get(key, []))
        return frozenset(out)

    # ------------------------------------------------------------------ queries
    def flatten(self, gadget_id: str) -> list[str]:
        """Executable member events of a gadget, nested gadgets expanded, in list order (memoised)."""
        cache = self.__dict__.setdefault("_flat", {})
        if gadget_id in cache:
            return cache[gadget_id]
        out = []
        for m in self.gadgets[gadget_id].members:
            if m in self.gadgets:
                out.extend(self.flatten(m))
            elif m in self.events:
                out.append(m)
        out = sorted(out, key=lambda eid: self.events[eid].pos)
        cache[gadget_id] = out
        return out

    def _sort_members(self):
        """Members (events and nested gadgets) in list order, so predicate segmentation follows the stream."""
        def pos_of(m):
            if m in self.events:
                return self.events[m].pos
            fl = self.flatten(m)
            return self.events[fl[0]].pos if fl else 10 ** 9
        for g in self.gadgets.values():
            g.members.sort(key=pos_of)

    def outermost(self, gadget_id: str) -> str:
        g = self.gadgets[gadget_id]
        return gadget_id if g.parent is None else self.outermost(g.parent)

    def observable(self, eid: str):
        """(Pauli, sign) measured by a measurement event, program wires."""
        from .pauli import Pauli
        e = self.events[eid].raw
        if e["kind"] == "measure_product":
            return Pauli.of(dict(e["signed_observable"])), int(e.get("sign", 1))
        if e["kind"] == "measure_single":
            return Pauli.of({e["wire"]: e["observable"]}), int(e.get("sign", 1))
        return None, 1

    def wire_cell(self, wire: str, at_eid: str):
        """Cells of the lifetime carrying `wire` when event `at_eid` runs (init before, discard not before)."""
        pos = self.events[at_eid].pos
        best = None
        for i0, i1, lt in self._lt_by_wire.get(wire, ()):
            if i0 <= pos <= i1:
                best = lt
            elif i0 > pos:
                break
        return self.lifetime_cells.get(best, ()) if best else ()

    def occupied_cells(self, at_eid: str) -> frozenset:
        """Cells of every lifetime alive at `at_eid` (a static estimate of the layout at that point)."""
        pos = self.events[at_eid].pos
        import numpy as _np
        hit = _np.nonzero((self._lt_i0 <= pos) & (pos <= self._lt_i1))[0]
        cells = set()
        for i in hit:
            cells.update(self.lifetime_cells.get(self._lt_ids[i], ()))
        return frozenset(cells)

    def next_on_wire(self, wire: str, after_pos: int):
        """The first event on `wire` with position > after_pos (None when there is none)."""
        import bisect
        lst = self.by_wire.get(wire)
        if not lst:
            return None
        i = bisect.bisect_right(self.by_wire_pos[wire], after_pos)
        return lst[i] if i < len(lst) else None

    def role_at_cell(self, cell: tuple, pos: int) -> str | None:
        for i0, i1, role in self.lifetimes_by_cell.get(tuple(cell), ()):
            if i0 <= pos <= i1:
                return role
        return None

    def atoms_of(self, gadget_id: str) -> list[Atom]:
        """Segment a gadget: consecutive members up to (not including) the first member whose execution depends on a decoded
        result open segment 0; every such member (a nested predicated gadget or a predicated event) opens the next segment,
        which runs to the next decode-dependent member.  Template ids of segments: a single-segment gadget keeps its
        template id; a two-segment gadget "T" gets "T1" / "T2" (the certifier's ids for the T injection's segments)."""
        cache = self.__dict__.setdefault("_atoms", {})
        if gadget_id in cache:
            return cache[gadget_id]
        g = self.gadgets[gadget_id]
        segments: list[list] = [[]]
        preds: list[frozenset] = [g.predicate_ids]           # a predicated gadget: its first segment runs under that predicate
                                                             # (M2 / N2: it used to start unconditional)
        for m in g.members:
            if m in self.gadgets:
                sub = self.gadgets[m]
                if sub.predicate_ids:
                    segments.append([]); preds.append(sub.predicate_ids)
                segments[-1].extend(self.flatten(m))
            else:
                ev = self.events[m]
                if ev.predicate_ids and ev.kind not in CONSUMER_KINDS:
                    segments.append([]); preds.append(ev.predicate_ids)
                segments[-1].append(m)
        segments_p = [(s, p) for s, p in zip(segments, preds) if s]
        out = []
        n = len(segments_p)
        child_by_events = {tuple(self.flatten(m)): self.gadgets[m].template_id for m in g.members if m in self.gadgets}
        for i, (s, p) in enumerate(segments_p):
            # a segment that is exactly one nested gadget carries that gadget's template id (the converter names the
            # T injection's segments T1 / T2 itself); otherwise a two-segment gadget "T" gets "T1" / "T2"
            tid = child_by_events.get(tuple(s)) or (g.template_id if n == 1 else "%s%d" % (g.template_id, i + 1))
            out.append(Atom(atom_id="%s#%d" % (gadget_id, i), gadget_id=gadget_id, template_id=tid, events=tuple(s), predicate_ids=p,
                            inputs=g.inputs, outputs=g.outputs, result_bits=g.result_bits, segment=i))
        cache[gadget_id] = out
        return out

    def lss_of(self, eids) -> tuple:
        out = set()
        for eid in eids:
            out.update(self.lss_map.get(eid, ()))
        return tuple(sorted(out))

    def descendants(self, eid: str) -> set:
        return nx.descendants(self.dag, eid)

    def t_gadgets(self) -> list[tuple]:
        """Speculated values (design Sec. 2.1): a gadget whose nested conditional gadget is predicated on one of ITS OWN
        results -- the T injection's conditional S on the ZZ parity.  Returns (gadget_id, predicted result id, producing
        event id, corrected wire, the T2 atom).

        Round-8 review (K21, A1a): the corrected wire is DERIVED from the T2's S merge program operand (the
        program-role wire of the unique measure_product inside the T2 segment) -- the declared `outputs[0]` was
        consumed but never validated, so a T gadget whose output pointed at another program wire ran the recovery on
        that wire.  A disagreement, a missing outputs[0] or an unidentifiable S operand refuses the document with
        ValueError; G-R2's `check_wires` reports the same inconsistency as `[wires]`."""
        out = []
        for gid, g in self.gadgets.items():
            for m in g.members:
                if m in self.gadgets and self.gadgets[m].predicate_ids and self.gadgets[m].predicate_ids <= set(g.result_bits.values()):
                    rid = next(iter(self.gadgets[m].predicate_ids))
                    producer = self.by_result[rid]
                    atoms = self.atoms_of(gid)
                    t2 = next(a for a in atoms if a.predicate_ids == self.gadgets[m].predicate_ids)
                    merges = [eid for eid in t2.events if self.events[eid].kind == "measure_product"]
                    prog = [w for eid in merges for w in self.events[eid].raw.get("signed_observable", ())
                            if self.roles_by_wire.get(w) == "program"]
                    if len(merges) != 1 or len(prog) != 1:
                        raise ValueError("%s: the T2's S merge program operand is not identifiable (merges %s, "
                                         "program wires %s)" % (gid, merges, prog))
                    wire = prog[0]
                    declared = g.outputs[0] if g.outputs else None
                    if wire != declared:
                        raise ValueError("%s: the corrected wire derived from the T2's S merge is %s, the gadget "
                                         "declares outputs[0] %s" % (gid, wire, declared))
                    out.append((gid, rid, producer, wire, t2))
        out.sort(key=lambda t: self.events[t[2]].pos)
        return out
