"""part P3: fixture tests for the X-side teleported S (SX) in the converter and the snippet cache.

The slicer output is synthetic, in note v2 6.1's form: the sidecar holds
four records, RequestYState (near_patch q, near_boundary "X"), MultiBodyMeasure {y: X, q: X}, MeasureSinglePatch {y: Z}
and Gate X on q; in the slice JSON y's cell sits right of q, and q's right edge and y's left edge are X (Dashed) and
stitched at the merge's slice. No slicer runs: `run_slicer` or `subprocess.run` is the patched boundary, as in
test_compile's FailureClassificationTests. Real SX snippets wait for the SX binary (P1) and the warming (P5).
Run from the repo root: python -m unittest froq.semantics.test_compile_sx"""
from __future__ import annotations

import copy
import json
import os
import pickle
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from .. import SWIPER_ROOT  # noqa: F401  (puts third_party/swiper on sys.path)
from . import compile as C

N = 2                                       # program patches 0 and 1
Q, Y = 0, 2                                 # SX on patch 0; its |Y> takes the first fresh id, n_qubits + 0
QCELL, Q1CELL = (1, 1), (0, 3)
ROWS, COLS = 3, 4
SX_LINES = ["RequestYState 2 0:X", "MultiBodyMeasure 2:X,0:X", "MeasureSinglePatch 2 Z", "LogicalPauli 0 X"]
ANNOT = " ; // %gadget=0,kind=SX,member=0,role=script"
SIDES = {"right": ((0, 1), "Right", "Left"), "left": ((0, -1), "Left", "Right"), "below": ((1, 0), "Bottom", "Top")}
EDGES = {"Top": "Solid", "Bottom": "Solid", "Left": "Dashed", "Right": "Dashed"}   # X (rough) edges left and right
# 6.1's refusal: the wave scheduler's retry error around the X-side request's own text, then libstdc++'s abort line
LAST = "RequestYState 2 1:X; Cannot prepare Y state: 1 has no free neighbour to its right X edge"
SX_REFUSAL_STDERR = ("terminate called after throwing an instance of 'std::runtime_error'\n"
                     "  what():  Could not apply instruction after max retries:\nRequestYState 2 1:X\nCaused by:\n"
                     + LAST + "\n")


def _cell(pid, edges, activity=None):
    return {"patch_type": "Qubit", "text": f"Id: {pid}", "edges": dict(edges),
            "activity": {"activity_type": activity} if activity else None}


def sx_slices(side="right", q_edges=None, y_edges=None):
    """Four slices: 0 the |Y> appears beside q; 1 the merge, the two facing edges stitched; 2 y read out
    (Measurement); 3 y gone.  Patch 1 sits apart."""
    (dr, dc), q_face, y_face = SIDES[side]
    ycell = (QCELL[0] + dr, QCELL[1] + dc)
    qe, ye = {**EDGES, **(q_edges or {})}, {**EDGES, **(y_edges or {})}
    out = []
    for s in range(4):
        g = [[None] * COLS for _ in range(ROWS)]
        q, y = dict(qe), dict(ye)
        if s == 1:
            q[q_face] += "Stiched"
            y[y_face] += "Stiched"
        g[QCELL[0]][QCELL[1]] = _cell(Q, q)
        g[Q1CELL[0]][Q1CELL[1]] = _cell(1, EDGES)
        if s <= 2:
            g[ycell[0]][ycell[1]] = _cell(Y, y, "Measurement" if s == 2 else None)
        out.append(g)
    return out


def sx_sidecar(kind="SX", near_boundary="X", mbm_obs=None, msp_obs=None, gate_op="X", gate=True, negative=False,
               dagger=False):
    """The sidecar 6.1 describes for the four lines of one SX member (member 0) on patch Q."""
    prov = {"gate_idx": 0, "gadget_id": 0, "gadget_kind": kind, "is_dagger": dagger, "role": "script"}
    req = {"idx": 1, "op": "RequestYState", "patches": [Y, Q], "near_patch": Q, "state": "Y", "slice": 0, "app_seq": 1}
    if near_boundary is not None:
        req["near_boundary"] = near_boundary
    lli = [{"idx": 0, "op": "DeclareLogicalQubitPatches", "patches": list(range(N)), "slice": 0, "app_seq": 0,
            "provenance": None}, req,
           {"idx": 2, "op": "MultiBodyMeasure", "patches": [Y, Q], "observable": mbm_obs or {str(Y): "X", str(Q): "X"},
            "is_negative": negative, "slice": 1, "app_seq": 2},
           {"idx": 3, "op": "MeasureSinglePatch", "patches": [Y], "observable": msp_obs or {str(Y): "Z"},
            "is_negative": False, "slice": 2, "app_seq": 3}]
    if gate:
        lli.append({"idx": 4, "op": "Gate", "patches": [Q], "single_qubit_op": gate_op, "slice": 2, "app_seq": 4})
    for r in lli[1:]:
        r["provenance"] = dict(prov)
    return {"core_qubits": list(range(N)), "compiler": "fixture: note v2 6.1's records", "flags": [], "lli": lli}


def raw_meta(sidecar):
    """The sidecar as the converter reads it, without the snippet path's line check (the requests' clients set as
    `snippet_meta_from_sidecar` sets them)."""
    meta = copy.deepcopy(sidecar)
    for r in meta["lli"]:
        if r["op"] == "RequestYState":
            r["clients"] = [r["near_patch"]]
    return meta


def convert(sidecar, slices):
    conv = C.Converter(raw_meta(sidecar), C.SliceStore(iter(slices)), "snippet")
    return conv.run()


def fake_run_slicer(slices, sidecar, seen):
    """Stands in for `run_slicer`: records the LLI file it is given and writes the fixture's slice JSON and sidecar."""
    def run(slicer, args, timeout_s=C.SLICER_TIMEOUT_S, cwd=None):
        with open(args[args.index("-i") + 1]) as f:
            seen.append(f.read())
        with open(args[args.index("-o") + 1], "w") as f:
            json.dump(slices, f)
        with open(args[args.index("--metaout") + 1], "w") as f:
            json.dump(sidecar, f)
        return ""
    return run


class SXLoweringTests(unittest.TestCase):
    """`lower_script`, `parse_lli_text` and the sidecar check: SX's four lines, and every old line as it was."""

    def test_lower_script_writes_6_1s_four_lines(self):
        self.assertEqual(C.lower_script([{"template": "SX", "wires": [Q]}], N), [l + ANNOT for l in SX_LINES])
        self.assertEqual(C._SNIPPET_MEMBER_KINDS["SX"], "SX")
        self.assertIn("SX", C.TEMPLATE_IDS)
        # one running count of fresh ids across S, SX and CNOT members
        lines = C.lower_script([{"template": "S", "wires": [1]}, {"template": "SX", "wires": [0]},
                                {"template": "CNOT_cf_patched", "wires": [0, 1]}], 5)
        self.assertEqual([l.split(" ; ")[0] for l in lines[4:8]],
                         ["RequestYState 6 0:X", "MultiBodyMeasure 6:X,0:X", "MeasureSinglePatch 6 Z", "LogicalPauli 0 X"])
        self.assertEqual(lines[8].split(" ; ")[0], "Init 7 |+> 1:X")
        self.assertTrue(all(l.endswith("kind=SX,member=1,role=script") for l in lines[4:8]))

    def test_old_members_lines_unchanged(self):
        """31b96bc's lines for H, S and CNOT_cf_patched, byte for byte (their snippet keys hash these texts)."""
        got = C.lower_script([{"template": "S", "wires": [1]}, {"template": "CNOT_cf_patched", "wires": [0, 1]},
                              {"template": "H", "wires": [1]}], 5)
        a = lambda m, k: f" ; // %gadget={m},kind={k},member={m},role=script"
        self.assertEqual(got, [l + a(0, "S") for l in ("RequestYState 5 1", "MultiBodyMeasure 5:Z,1:Z",
                                                        "MeasureSinglePatch 5 X", "LogicalPauli 1 Z")]
                         + [l + a(1, "CNOT") for l in ("Init 6 |+> 1:X", "MultiBodyMeasure 0:Z,6:Z",
                                                       "MultiBodyMeasure 6:X,1:X", "MeasureSinglePatch 6 Z")]
                         + ["HGate 1" + a(2, "H")])
        with self.assertRaises(C.ConverterError):
            C.lower_script([{"template": "SXdg", "wires": [0]}], N)

    def test_parse_x_side_request(self):
        rec = C.parse_lli_text("RequestYState 2 0:X ; // %gadget=0,kind=SX,member=0,role=script")
        self.assertEqual(rec, {"op": "RequestYState", "patches": [2], "clients": [], "near_patch": 0, "state": "Y",
                               "near_boundary": "X"})
        # the old form parses as before: no near_boundary key at all
        self.assertEqual(C.parse_lli_text("RequestYState 2 0"),
                         {"op": "RequestYState", "patches": [2], "clients": [], "near_patch": 0, "state": "Y"})
        self.assertEqual(C.parse_lli_text("RequestMagicState 3 1"),
                         {"op": "RequestMagicState", "patches": [3], "clients": [], "near_patch": 1, "state": "Magic"})
        for bad in ("RequestYState 2 0:Z", "RequestYState 2 0:", "RequestMagicState 2 0:X"):
            with self.assertRaises(C.ConverterError, msg=bad):
                C.parse_lli_text(bad)
        self.assertEqual(len(C._snippet_members(SX_LINES)), 1)

    def test_sidecar_must_state_the_side_it_was_given(self):
        member_lines = C._snippet_members(C.lower_script([{"template": "SX", "wires": [Q]}], N))
        meta = C.snippet_meta_from_sidecar(sx_sidecar(), member_lines, N)
        self.assertEqual(meta["lli"][1]["clients"], [Q])
        with self.assertRaisesRegex(C.ConverterError, "near_boundary"):
            C.snippet_meta_from_sidecar(sx_sidecar(near_boundary=None), member_lines, N)
        # a plain S line whose record claims the X side
        s_lines = C._snippet_members(C.lower_script([{"template": "S", "wires": [Q]}], N))
        side = sx_sidecar(kind="S", mbm_obs={str(Y): "Z", str(Q): "Z"}, msp_obs={str(Y): "X"}, gate_op="Z")
        with self.assertRaisesRegex(C.ConverterError, "near_boundary"):
            C.snippet_meta_from_sidecar(side, s_lines, N)
        # a missing frame line: the written member has four lines, the sidecar three
        with self.assertRaisesRegex(C.ConverterError, "missing explicit records"):
            C.snippet_meta_from_sidecar(sx_sidecar(gate=False), member_lines, N)


class SXGadgetTests(unittest.TestCase):
    """`_sx_gadget` and the S group: one SX converts; each failed check raises."""

    def test_sx_gadget_and_s_group(self):
        schedule, doc, stats = convert(sx_sidecar(), sx_slices())
        (g,) = [e for e in doc["events"] if e["kind"] == "gadget"]
        self.assertEqual((g["template_id"], g["gadget_kind"], g["is_dagger"], g["source_gate_id"]), ("SX", "SX", False, 0))
        self.assertEqual((g["inputs"], g["outputs"], g["result_bits"]), (["q0"], ["q0"], {"u": "r2", "w": "r3"}))
        self.assertIsNone(g["predicate"])
        self.assertEqual(stats["inventory_templates"], {"SX": 1})
        ev = {e["event_id"]: e for e in doc["events"]}
        kinds = [ev[e]["kind"] for e in g["events"]]
        self.assertEqual(kinds, ["init", "measure_product", "measure_single", "discard", "frame_update", "frame_update"])
        init, mp, ms = (ev[e] for e in g["events"][:3])
        self.assertEqual((init["state"], init["near"], init["role"], init["cell"]), ("Y", "q0", "resource", [1, 2]))
        self.assertEqual((mp["signed_observable"], mp["sign"], mp["result_id"], mp["cells"]),
                         ({"w2": "X", "q0": "X"}, 1, "r2", []))
        self.assertEqual((ms["observable"], ms["wire"], ms["result_id"]), ("Z", "w2", "r3"))
        # the frame: X_q^(1 xor u xor w) as the XOR of its members (validate_events' rule), all on q
        frames = [ev[e] for e in g["events"] if ev[e]["kind"] == "frame_update"]
        bits, const = set(), 0
        for f in frames:
            self.assertEqual((f["wire"], f["pauli"], f["lli"]), ("q0", "X", [4]))
            if f["predicate"] is None:
                const ^= 1
            else:
                self.assertEqual(f["predicate"]["op"], "xor")
                bits ^= set(f["predicate"]["args"])
        self.assertEqual((bits, const), ({"r2", "r3"}, 1))
        # SWIPER's S group, unconditioned: MERGE q|y, Y_MEAS y, IDLE q, DISCARD y; then the two traced discards
        ins = schedule.instructions
        self.assertEqual([i.name for i in ins], ["MERGE", "Y_MEAS", "IDLE", "DISCARD", "DISCARD", "DISCARD"])
        group = ins[:4]
        self.assertTrue(all(i.group_name == "CONDITIONAL_S" and not i.conditioned_on_idx for i in group))
        self.assertTrue(all(i.group_instr_indices == frozenset(range(4)) for i in group))
        self.assertEqual([set(i.patches) for i in group], [{QCELL, (1, 2)}, {(1, 2)}, {QCELL}, {(1, 2)}])
        self.assertEqual(set(ins[0].merge_faces), {(QCELL, (1, 2))})
        self.assertEqual(doc["lss_map"][mp["event_id"]], [0, 2])
        self.assertEqual(doc["lss_map"][init["event_id"]], [0])
        self.assertEqual(doc["lss_map"][ms["event_id"]], [1, 2])

    def assert_fails(self, sidecar, slices, pattern):
        with self.assertRaisesRegex(C.ConverterError, pattern):
            convert(sidecar, slices)

    def test_wrong_side(self):
        self.assert_fails(sx_sidecar(near_boundary=None), sx_slices(), "X side")      # requested on no side
        self.assert_fails(sx_sidecar(), sx_slices("left"), "not the right neighbour")
        self.assert_fails(sx_sidecar(), sx_slices("below", q_edges={"Bottom": "Dashed"}, y_edges={"Top": "Dashed"}),
                          "not the right neighbour")

    def test_z_edge(self):
        self.assert_fails(sx_sidecar(), sx_slices(q_edges={"Right": "Solid"}), "do not face X to X")
        self.assert_fails(sx_sidecar(), sx_slices(y_edges={"Left": "Solid"}), "do not face X to X")

    def test_wrong_observable(self):
        self.assert_fails(sx_sidecar(mbm_obs={str(Y): "Z", str(Q): "Z"}), sx_slices(), "SX observables")
        self.assert_fails(sx_sidecar(mbm_obs={str(Y): "X", str(Q): "Z"}), sx_slices(), "SX observables")
        self.assert_fails(sx_sidecar(msp_obs={str(Y): "X"}), sx_slices(), "SX observables")
        self.assert_fails(sx_sidecar(negative=True), sx_slices(), "positive")

    def test_missing_or_wrong_frame_line(self):
        self.assert_fails(sx_sidecar(gate=False), sx_slices(), "SX must lower to")
        self.assert_fails(sx_sidecar(gate_op="Z"), sx_slices(), "SX must lower to")

    def test_foreign_kinds_fail_loudly(self):
        self.assert_fails(sx_sidecar(kind="SXdg"), sx_slices(), "SXdg is foreign")
        self.assert_fails(sx_sidecar(dagger=True), sx_slices(), "no dagger")


class SXSnippetCacheTests(unittest.TestCase):
    """`compile_snippet` for SX, and 6.2's refusal entries.  Each test has its own cache directory and dummy slicer, and
    forgets the module's in-memory state before and after."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="froq_sx_")
        self.cache_dir = os.path.join(self.tmp, "snippets_sx")
        self.slicer = os.path.join(self.tmp, "lsqecc_slicer")
        with open(self.slicer, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(self.slicer, 0o755)
        patches = [mock.patch.object(C, "SNIPPET_CACHE_DIR", self.cache_dir),
                   mock.patch.object(C, "_SNIPPET_PERSIST", True),
                   mock.patch.object(C, "_SNIPPET_FLUSH_EVERY", 1),
                   mock.patch.object(C, "_SNIPPET_REFUSALS", False)]
        patches += [mock.patch.dict(d, clear=True) for d in self.memory()]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    @staticmethod
    def memory():
        return (C._SNIPPET_CACHE, C._SNIPPET_DISK, C._SNIPPET_DISK_STAT, C._SNIPPET_DISK_BUILDER, C._SNIPPET_PENDING,
                C._FORK_META)

    def forget(self):
        """A new process: nothing held in memory."""
        for d in self.memory():
            d.clear()

    def file_doc(self):
        with open(os.path.join(self.cache_dir, f"{N}.json")) as f:
            return json.load(f)

    @staticmethod
    def refusing_run(rc=134, stderr=SX_REFUSAL_STDERR):
        def run(cmd, *a, **kw):
            if cmd[:1] == ["bash"]:
                return subprocess.CompletedProcess(cmd, rc, "", stderr)
            return subprocess.CompletedProcess(cmd, 128, "", "")        # fork_commit's git: no repository here
        return run

    def test_sx_snippet_round_trip(self):
        seen = []
        script = [{"template": "SX", "wires": [Q]}]
        with mock.patch.dict(os.environ, {"E2E_SNIPPET_SLICER": self.slicer}):
            with mock.patch.object(C, "run_slicer", side_effect=fake_run_slicer(sx_slices(), sx_sidecar(), seen)):
                snip = C.compile_snippet(script, N)
                self.assertIs(C.compile_snippet(script, N), snip)               # held in memory: no second run
        self.assertEqual(seen, ["DeclareLogicalQubitPatches 0,1\n" + "\n".join(l + ANNOT for l in SX_LINES) + "\n"])
        self.assertEqual(snip.lli, [l + ANNOT for l in SX_LINES])
        self.assertEqual([i.name for i in snip.instructions], ["MERGE", "Y_MEAS", "IDLE", "DISCARD"])
        self.assertTrue(all(i.group_name == "CONDITIONAL_S" and not i.conditioned_on_idx for i in snip.instructions))
        self.assertEqual(snip.cells, [QCELL, (1, 2)])
        self.assertEqual(snip.n_slices, 4)
        gad = [e for e in snip.events["events"] if e["kind"] == "gadget"]
        self.assertEqual([(g["template_id"], g["source_gate_id"]) for g in gad], [("SX", 0)])
        # stored as an ordinary entry under the builder's sha; a run without a slicer reads it back
        C.snippet_cache_flush()
        doc = self.file_doc()
        self.assertEqual(doc["slicer_sha256"], C.sha256_file(self.slicer))
        (entry,) = doc["snippets"].values()
        self.assertNotIn("refused", entry)
        self.forget()
        with mock.patch.dict(os.environ, {"E2E_SNIPPET_SLICER": os.path.join(self.tmp, "absent")}):
            again = C.compile_snippet(script, N)
        self.assertEqual(again.lss_text, snip.lss_text)
        self.assertEqual(json.dumps(again.events, sort_keys=True), json.dumps(snip.events, sort_keys=True))
        self.assertEqual((again.cells, again.n_slices, again.lli), (snip.cells, snip.n_slices, snip.lli))

    def test_lli_text_path_gives_kind_sx(self):
        """An X-side RequestYState in LLI text starts a member of kind SX; the text is the list path's, so is the key."""
        seen = []
        with mock.patch.dict(os.environ, {"E2E_SNIPPET_SLICER": self.slicer}):
            with mock.patch.object(C, "run_slicer", side_effect=fake_run_slicer(sx_slices(), sx_sidecar(), seen)):
                snip = C.compile_snippet("\n".join(SX_LINES) + "\n", N)
        self.assertEqual(snip.lli, C.lower_script([{"template": "SX", "wires": [Q]}], N))
        self.assertEqual(C._snippet_disk_key(C.LAYOUT_FLAGS, "\n".join(snip.lli), slicer=self.slicer, n_qubits=N),
                         next(iter(self.file_doc()["snippets"])))

    def test_cached_refusal_read_back(self):
        C._SNIPPET_REFUSALS = True                                    # what use_snippet_cache("snippets_sx", True) sets
        script = [{"template": "SX", "wires": [1]}]
        with mock.patch.dict(os.environ, {"E2E_SNIPPET_SLICER": self.slicer}):
            with mock.patch.object(C.subprocess, "run", side_effect=self.refusing_run()):
                with self.assertRaises(C.PlacementRefused) as live:
                    C.compile_snippet(script, N)
        self.assertEqual((live.exception.rc, live.exception.marker, live.exception.last_line),
                         (134, "Could not apply instruction after max retries:", LAST))
        C.snippet_cache_flush()
        doc = self.file_doc()
        (key, entry), = doc["snippets"].items()
        self.assertEqual(entry["refused"], {"rc": 134, "marker": "Could not apply instruction after max retries:",
                                            "last_line": LAST})
        self.assertEqual(entry["lli"], C.lower_script(script, N))
        self.assertEqual(entry["slicer_sha256"], C.sha256_file(self.slicer))
        self.assertEqual(doc["slicer_sha256"], entry["slicer_sha256"])      # the file keeps one builder
        # a new process with no slicer, the switch off: the cached refusal is the same refusal, not a miss
        self.forget()
        C._SNIPPET_REFUSALS = False
        with mock.patch.dict(os.environ, {"E2E_SNIPPET_SLICER": os.path.join(self.tmp, "absent")}):
            with self.assertRaises(C.PlacementRefused) as cached:
                C.compile_snippet(script, N)
        self.assertIn(key, str(cached.exception))
        self.assertEqual((cached.exception.rc, cached.exception.marker, cached.exception.last_line),
                         (134, "Could not apply instruction after max retries:", LAST))
        record = lambda e: str(e).strip().splitlines()[-1][:200]       # contract.py's compile_failures line
        self.assertEqual(record(cached.exception), record(live.exception))
        self.assertEqual(record(live.exception), LAST)
        self.assertTrue(C._is_placement_refusal(134, SX_REFUSAL_STDERR))

    def test_refusal_not_kept_when_switched_off(self):
        """Library off, and every directory but snippets_sx: a refusal raises and nothing is stored, as; a run
        without a slicer then meets a miss, which is infrastructure."""
        script = [{"template": "SX", "wires": [1]}]
        with mock.patch.dict(os.environ, {"E2E_SNIPPET_SLICER": self.slicer}):
            with mock.patch.object(C.subprocess, "run", side_effect=self.refusing_run()):
                with self.assertRaises(C.PlacementRefused):
                    C.compile_snippet(script, N)
        C.snippet_cache_flush()
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, f"{N}.json")))
        self.forget()
        with mock.patch.dict(os.environ, {"E2E_SNIPPET_SLICER": os.path.join(self.tmp, "absent")}):
            with self.assertRaises(C.ConverterError) as cm:
                C.compile_snippet(script, N)
        self.assertNotIsInstance(cm.exception, C.PlacementRefused)
        self.assertIn("snippet cache miss", str(cm.exception))

    def test_infrastructure_failures_are_never_kept(self):
        C._SNIPPET_REFUSALS = True
        script = [{"template": "SX", "wires": [1]}]
        other = ("terminate called after throwing an instance of 'std::runtime_error'\n"
                 "  what():  basic_string::substr: __pos (which is 5) > this->size()\n")
        with mock.patch.dict(os.environ, {"E2E_SNIPPET_SLICER": self.slicer}):
            for rc, err in ((124, SX_REFUSAL_STDERR), (153, SX_REFUSAL_STDERR), (134, other)):
                with mock.patch.object(C.subprocess, "run", side_effect=self.refusing_run(rc, err)):
                    with self.assertRaises(C.ConverterError) as cm:
                        C.compile_snippet(script, N)
                self.assertNotIsInstance(cm.exception, C.PlacementRefused)
        C.snippet_cache_flush()
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, f"{N}.json")))

    def test_malformed_refusal_entry_is_infrastructure(self):
        script = [{"template": "SX", "wires": [1]}]
        text = "\n".join(C.lower_script(script, N))
        key = C._snippet_disk_key(C.LAYOUT_FLAGS, text, slicer=self.slicer, n_qubits=N)
        os.makedirs(self.cache_dir)
        good = {"rc": 134, "marker": "Could not apply instruction after max retries:", "last_line": LAST}
        for bad in ({**good, "rc": "134"}, {**good, "marker": "not a marker"}, {**good, "last_line": ""},
                    {**good, "last_line": "two\nlines"}, {"rc": 134}, "refused"):
            self.forget()
            with open(os.path.join(self.cache_dir, f"{N}.json"), "w") as f:
                json.dump({"schema": C.SNIPPET_CACHE_SCHEMA, "n_qubits": N, "slicer_sha256": C.sha256_file(self.slicer),
                           "fork_commit": None, "snippets": {key: {"refused": bad, "lli": text.splitlines()}}}, f)
            with mock.patch.dict(os.environ, {"E2E_SNIPPET_SLICER": self.slicer}):
                with self.assertRaisesRegex(C.ConverterError, "malformed refusal") as cm:
                    C.compile_snippet(script, N)
            self.assertNotIsInstance(cm.exception, C.PlacementRefused)

    def test_placement_refused_keeps_its_fields(self):
        e = C.PlacementRefused("slicer failed (rc=134): x\n" + LAST, rc=134, marker="has no free neighbour",
                               last_line=LAST)
        back = pickle.loads(pickle.dumps(e))
        self.assertEqual((type(back), str(back), back.rc, back.marker, back.last_line),
                         (C.PlacementRefused, str(e), 134, "has no free neighbour", LAST))
        plain = C.PlacementRefused("slicer failed (rc=134): x")                 # the old one-argument form
        self.assertEqual((str(plain), plain.rc, plain.marker, plain.last_line), ("slicer failed (rc=134): x", None, None,
                                                                                None))


class UseSnippetCacheTests(unittest.TestCase):
    """`use_snippet_cache`: refusal entries only for snippets_sx, and every call sets the switch."""

    def test_switch(self):
        held = [mock.patch.dict(d, clear=True) for d in SXSnippetCacheTests.memory()]   # a switch clears them
        with mock.patch.object(C, "SNIPPET_CACHE_DIR", C.SNIPPET_CACHE_DIR), \
                mock.patch.object(C, "_SNIPPET_REFUSALS", False), held[0], held[1], held[2], held[3], held[4], held[5]:
            base = os.path.dirname(C.SNIPPET_CACHE_DIR)
            for subdir in ("snippets", "snippets_ur"):
                with self.assertRaises(ValueError):
                    C.use_snippet_cache(subdir, cache_refusals=True)
            self.assertFalse(C._SNIPPET_REFUSALS)
            C.use_snippet_cache("snippets_sx", cache_refusals=True)
            self.assertEqual((C.SNIPPET_CACHE_DIR, C._SNIPPET_REFUSALS), (os.path.join(base, "snippets_sx"), True))
            C.use_snippet_cache("snippets_sx")                                  # same directory, switch off
            self.assertFalse(C._SNIPPET_REFUSALS)
            C.use_snippet_cache("snippets_sx", cache_refusals=True)
            C.use_snippet_cache("snippets")                                     # what set_recovery_mode calls
            self.assertEqual((C.SNIPPET_CACHE_DIR, C._SNIPPET_REFUSALS), (os.path.join(base, "snippets"), False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
