"""Round 18, part P2: the SX certificates.

  * SX and SXdg pass C1 on all four records, each with weight 1/4; each (u, w) branch, computed here without the
    certifier's Circuit class, gives the intended rotation with its frame;
  * the eight changed C3 entries (one-wire X and Y keys on wires 0 and 1) are the table of 6.3, pass the Choi check, and
    carry the old file's script as a certified `fallback`; S SX S is H; every other entry equals `c3_recovery.json`'s;
  * SX's Pauli map is stim's SQRT_X (X to X, Y to Z, Z to -Y), SXdg's its inverse;
  * the four old certificate files are the files of 31b96bc (sha256) and the old pipeline still regenerates their content.
Nothing here writes a certificate file."""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import unittest

import numpy as np

from froq.semantics import certify as C

OLD_SHA256 = {      # 31b96bc, the code that ran
    "c1_templates.json": "04a861587fe6d7f12b74931d1de1427ba8212e4e299c0dc39466ccd15f29872c",
    "c2_propagation.json": "191e4bfeebba93e7c801ea7f151bc6e38a1ce3d008bb3c11af19977b588a9087",
    "c3_recovery.json": "a98777fcb891ec48e07ebdd60803de67214b46ae8975de23b420d5a9ddcdf5d2",
    "c3_recovery_v5.json": "8ab2c1b143694fef88e5a91ba5f9bde42d113b80368055489d2651ea50eda682",
}


def load(name):
    with open(os.path.join(C.CERT_DIR, name)) as fh:
        return json.load(fh)


def norm(obj):
    return json.loads(json.dumps(obj))


def frame_of(letter, wire):
    return {"template": "frame", "pauli": f"{letter}{wire}"}


def table(w):
    """6.3's table for wire w: key -> (sx script, shortened script).  `S`, `SX`, `H` members on wire w."""
    m = lambda t: {"template": t, "wires": [w]}
    return {
        f"rot(+1,X{w})": ([m("SX")], [m("S"), m("H"), m("S"), frame_of("X", w)]),
        f"rot(-1,X{w})": ([m("SX"), frame_of("X", w)], [m("S"), m("H"), m("S")]),
        f"rot(+1,Y{w})": ([m("S"), m("SX"), m("S"), frame_of("X", w)], [m("H"), frame_of("X", w)]),
        f"rot(-1,Y{w})": ([m("S"), m("SX"), m("S"), frame_of("Z", w)], [m("H"), frame_of("Z", w)]),
    }


TABLE = {**table(0), **table(1)}


class TestSXTemplates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.T = C.build_templates_sx()
        cls.c1 = C.certify_c1(cls.T)

    def test_c1_pass_with_quarter_weights(self):
        for tid in ("SX", "SXdg"):
            with self.subTest(template=tid):
                e = self.c1[tid]
                self.assertIs(e["PASS"], True)
                self.assertEqual(sorted(e["lambda_sq"]), ["00", "01", "10", "11"])
                self.assertEqual(list(e["lambda_sq"].values()), [0.25] * 4)
                self.assertEqual(e["sum_lambda_sq"], 1.0)
                self.assertEqual(e["outcome_bits"], ["u", "w"])

    def test_intended_unitaries(self):
        self.assertTrue(np.allclose(self.T["SX"].intended, C.rot(-1, "X")))
        self.assertTrue(np.allclose(self.T["SXdg"].intended, C.rot(+1, "X")))
        self.assertTrue(np.allclose(C._GATE_OF["SX"], C.rot(-1, "X")))
        self.assertEqual(self.c1["SX"]["intended_unitary"], "SX = rot(-1,X)")
        self.assertEqual(self.c1["SXdg"]["intended_unitary"], "SX^dagger = rot(+1,X)")

    def test_four_branches_with_their_frames(self):
        """K_(u,w) = <w|_y (I + (-1)^u X_q X_y)/2 |Y>_y from numpy alone (wire 0 = q, wire 1 = y); the frame X^f(u,w)
        makes it exactly the template's Kraus operator and proportional to the intended rotation, and no other Pauli
        does."""
        X, I2 = C.PAULI["X"], C.I2
        for tpl, intended, frame_fn in (("SX", C.rot(-1, "X"), lambda u, w: 1 ^ u ^ w),
                                        ("SXdg", C.rot(+1, "X"), lambda u, w: u ^ w)):
            for u, w in itertools.product((0, 1), repeat=2):
                with self.subTest(template=tpl, u=u, w=w):
                    proj = 0.5 * (np.eye(4) + (-1) ** u * np.kron(X, X))
                    bra = C.EIGVEC[("Z", w)].conj().reshape(1, 2)
                    K = np.kron(I2, bra) @ proj @ np.kron(I2, C.KET_Y.reshape(2, 1))
                    framed = C.PAULI["X"] @ K if frame_fn(u, w) else K
                    self.assertTrue(np.allclose(framed, self.T[tpl].kraus[(u, w)]))
                    lam = C.proportional(framed, intended)
                    self.assertIsNotNone(lam)
                    self.assertAlmostEqual(abs(lam) ** 2, 0.25)
                    works = [p for p in "IXYZ" if C.proportional(C.PAULI[p] @ K, intended) is not None]
                    self.assertEqual(works, ["X" if frame_fn(u, w) else "I"])

    def test_pauli_map_is_stims_sqrt_x(self):
        X, Y, Z = (C.PAULI[p] for p in "XYZ")
        for sign, expect in ((-1, (X, Z, -Y)), (+1, (X, -Z, Y))):
            U = C.rot(sign, "X")
            for P, out in zip((X, Y, Z), expect):
                self.assertTrue(np.allclose(U @ P @ U.conj().T, out))

    def test_matches_stim_tableau_and_unitary(self):
        try:
            import stim
        except ImportError:
            self.skipTest("stim is not installed")
        sx = stim.Tableau.from_named_gate("SQRT_X")
        self.assertEqual((sx.x_output(0), sx.y_output(0), sx.z_output(0)),
                         (stim.PauliString("X"), stim.PauliString("Z"), stim.PauliString("-Y")))
        for name, sign in (("SQRT_X", -1), ("SQRT_X_DAG", +1)):
            U = stim.Tableau.from_named_gate(name).to_unitary_matrix(endian="big")
            self.assertIsNotNone(C.proportional(np.asarray(U, dtype=complex), C.rot(sign, "X")), name)

    def test_old_templates_unchanged(self):
        old = C.build_templates()
        self.assertEqual(list(self.T)[:len(old)], list(old))
        self.assertEqual(list(self.T)[len(old):], ["SX", "SXdg"])
        for tid, t in old.items():
            self.assertEqual(sorted(self.T[tid].kraus), sorted(t.kraus))
            for rec, K in t.kraus.items():
                self.assertTrue(np.array_equal(self.T[tid].kraus[rec], K), tid)


class TestSXRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.T = C.build_templates_sx()
        cls.new = load("c3_recovery_sx.json")
        cls.old = load("c3_recovery.json")

    def test_eight_changed_entries_and_their_fallbacks(self):
        self.assertEqual(len(TABLE), 8)
        for qkey, (sx_script, old_script) in TABLE.items():
            with self.subTest(Q=qkey):
                e, o = self.new[qkey], self.old[qkey]
                self.assertEqual(o["script"], old_script)                 # the table's shortened column is the old file's
                self.assertEqual(e["script"], sx_script)
                self.assertIs(e["PASS"], True)
                self.assertIs(e["channel_equals_conj_by_Qdagger"], True)
                self.assertIs(e["trace_preserving"], True)
                self.assertEqual(e["frame_after_rule"], o["frame_after_rule"])
                fb = e["fallback"]
                self.assertEqual(fb["script"], old_script)
                for k in ("n_private_bits", "member_count", "channel_equals_conj_by_Qdagger", "trace_preserving", "PASS"):
                    self.assertEqual(fb[k], o[k], k)                      # its own Choi result is the old entry's
                self.assertIs(fb["PASS"], True)

    def test_scripts_are_q_dagger_by_their_unitaries(self):
        """Independent of the Choi code: the members' intended unitaries with the frame multiply to Q^dagger."""
        for qkey, (sx_script, old_script) in TABLE.items():
            n = 1 + max(C.parse_qkey(qkey)[1])
            target = C.lib_matrix(qkey, n).conj().T
            for name, script in (("sx", sx_script), ("fallback", old_script)):
                W = np.eye(2 ** n, dtype=complex)
                for m in script:
                    if m["template"] == "frame":
                        letters = ["I"] * n
                        letters[int(m["pauli"][1:])] = m["pauli"][0]
                        W = C.pauli_matrix("".join(letters)) @ W
                    else:
                        W = C.embed(C._GATE_OF[m["template"]], m["wires"], n) @ W
                with self.subTest(Q=qkey, script=name):
                    self.assertIsNotNone(C.proportional(W, target))

    def test_s_sx_s_is_h_up_to_the_named_pauli(self):
        W = C.S_GATE @ C.rot(-1, "X") @ C.S_GATE
        self.assertIsNotNone(C.proportional(W, C.H_GATE))
        # the table's frames: X after it is rot(-1, Y) and Z after it is rot(+1, Y), the same as after H
        self.assertIsNotNone(C.proportional(C.PAULI["X"] @ W, C.rot(-1, "Y")))
        self.assertIsNotNone(C.proportional(C.PAULI["Z"] @ W, C.rot(+1, "Y")))
        for w in (0, 1):
            t = table(w)
            for key, letter in ((f"rot(+1,Y{w})", "X"), (f"rot(-1,Y{w})", "Z")):
                self.assertEqual(t[key][0][-1], frame_of(letter, w))
                self.assertEqual(t[key][0][-1], t[key][1][-1])

    def test_every_other_entry_equals_the_old_file(self):
        self.assertEqual(list(self.new), list(self.old))
        for qkey, entry in self.old.items():
            if qkey in TABLE:
                continue
            with self.subTest(Q=qkey):
                self.assertEqual(self.new[qkey], entry)
                self.assertNotIn("fallback", self.new[qkey])
        self.assertEqual(sorted(k for k, e in self.new.items() if "fallback" in e), sorted(TABLE))

    def test_every_entry_passes(self):
        for qkey, e in self.new.items():
            with self.subTest(Q=qkey):
                self.assertIs(e["PASS"], True)

    def test_sx_script_and_fallback_hooks(self):
        for qkey in self.old:
            changed = qkey in TABLE
            self.assertEqual(C.sx_recovery_script.fallback(qkey), C.recovery_script(qkey) if changed else None)
            self.assertEqual(norm(C.sx_recovery_script(qkey)), TABLE[qkey][0] if changed else self.old[qkey]["script"])

    def test_the_files_are_the_codes_outputs(self):
        res = C.run_all_sx(write=False)
        self.assertEqual(norm(res["c1"]), load("c1_templates_sx.json"))
        self.assertEqual(norm(res["c3"]), self.new)

    def test_c1_sx_file_keeps_the_old_entries(self):
        old, new = load("c1_templates.json"), load("c1_templates_sx.json")
        self.assertEqual(list(new), list(old)[:-2] + ["SX", "SXdg"] + list(old)[-2:])    # RESET and MEASURE stay last
        for tid, e in old.items():
            self.assertEqual(new[tid], e, tid)


class TestOldCertificatesUntouched(unittest.TestCase):
    def test_sha256_of_the_old_files(self):
        for name, h in OLD_SHA256.items():
            with open(os.path.join(C.CERT_DIR, name), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), h, name)

    def test_the_old_pipeline_regenerates_the_old_content(self):
        """The refactor of certify_c3 and the new entries in certify_c1 leave the old tables' content as it was."""
        T = C.build_templates()
        self.assertEqual(norm(C.certify_c1(T)), load("c1_templates.json"))
        self.assertEqual(norm(C.certify_c3(T)), load("c3_recovery.json"))
        self.assertEqual(norm(C.certify_c3(T, 2, C.literal_recovery_script)), load("c3_recovery_v5.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
