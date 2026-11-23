"""Unit tests for implementation, findings 1 and 2, on froq/contract.py and
froq/pauli.py.

Finding 1 -- the C3 scripts' frame members are kept in `Lowered.frames` (script order, positioned among the physical
members) and composed into `Lowered.frame_end`; `Step.frame_after` = Q^dagger P^dagger Q times that composition. Proof:
for every Q in Lib and every Pauli P, the physical members executed as the certifier's intended template unitaries followed
by the composed frame equal E^dagger up to phase -- and, as the negative control, do NOT without the composition whenever the
script has a frame member (S S = Z, not I).

Finding 2 -- the walker canonicalises the COMPLETE residual (template wires + the spectator wire) with the certifier's rule,
looks up the full C2 key, never overwrites the row's output Q, and enforces signed-Z (T rows) and Lib membership after every
transition. Checks: `canonical_split` against `certify.canonicalize` over the whole 2-wire domain and against the
certifier's canonical set on 3 wires; the three residuals reproduced in the review; random (template, P, Q) transitions
against an independent numerical computation with the templates' Kraus operators on the complete register (template wires,
spectator, one far wire): K_r E = E'_r K_f(r) with every E'_r a Pauli times a Lib quarter-turn and a common Q'.

  python -m froq.test_contract_semantics"""
from __future__ import annotations

import copy, itertools, os, random, unittest

import numpy as np

from . import SWIPER_ROOT  # noqa: F401  (puts upstream on the path)
from swiper.lattice_surgery_schedule import LatticeSurgerySchedule
from .contract import Fence, ScheduleView, Semantics, Tables, Walker, build_contract, local_pkey, parse_pkey, read_lss, values_of
from .events import Atom, EventStream
from .pauli import I, Pauli, Residual, absorb_sign, canonical_split, pkey
from .semantics import certify

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "tests_fixtures")
D = 5
TABLES = Tables()
TEMPLATES = certify.build_templates()
CLIFFORD_ROWS = ["X", "Y", "Z", "H", "S", "Sdg", "CNOT_cf_patched", "CZ"]
T_ROWS = ["T1", "T2[pred=asis]", "T2[pred=flipped]"]


# ------------------------------------------------------------------------------------------------------ numeric helpers
def letters_of(P: Pauli, order) -> str:
    d = P.as_dict()
    return "".join(d.get(w, "I") for w in order)


def matrix_of(res: Residual, order, bits: dict | None = None) -> np.ndarray:
    """P (times the generators whose bit is 1) times rot(s, A) on the register `order`."""
    P = res.P
    for b, g in res.cond:
        if bits and bits.get(b):
            P = P.mul(g)
    M = certify.pauli_matrix(letters_of(P, order))
    if res.has_q:
        M = M @ certify.rot(res.qsign, letters_of(res.axis, order))
    return M


def pauli_coefficients(M: np.ndarray, n: int) -> dict:
    """M = sum_P c_P P; the non-zero c_P by letters."""
    d = 2 ** n
    out = {}
    for letters in itertools.product("IXYZ", repeat=n):
        letters = "".join(letters)
        c = np.trace(certify.pauli_matrix(letters) @ M) / d
        if abs(c) > 1e-9:
            out[letters] = c
    return out


def decompose_lib(M: np.ndarray, n: int):
    """Direct decomposition of a (scaled) unitary M as P rot(s, A) with A of weight <= 2, canonical by the certifier's rule:
    such an operator has exactly one or two Pauli terms of equal magnitude (P and P A), and a unitary with two equal terms is
    a quarter-turn (unitarity forces the relative phase to +-i).  Returns (P letters, s, A letters) or None."""
    if not certify.is_scaled_unitary(M):
        return None
    co = pauli_coefficients(M, n)
    if len(co) == 1:
        (p,) = co
        return p, 0, "I" * n
    if len(co) != 2:
        return None
    (p1, c1), (p2, c2) = co.items()
    if abs(abs(c1) - abs(c2)) > 1e-8:
        return None
    P1, P2 = Pauli.of({i: l for i, l in enumerate(p1)}), Pauli.of({i: l for i, l in enumerate(p2)})
    A = P1.mul(P2)                                        # P1 P2 = i^phase A
    if A.weight > 2:
        return None
    ratio = (c2 / c1) * (1j ** A.phase)                   # M ~ P1 (I + ratio A), ratio = i s
    if abs(ratio.real) > 1e-8 or abs(abs(ratio.imag) - 1) > 1e-8:
        return None
    s = 1 if ratio.imag > 0 else -1
    pos = {i: i for i in range(n)}
    Pc, sc = canonical_split(P1, s, A.unsigned(), pos)
    return letters_of(Pc, range(n)), sc, letters_of(A, range(n))


def lib_key(dec) -> str:
    p, s, a = dec
    qk = "I" if s == 0 else certify.rot_key(s, a)
    return certify.residual_key(certify.pauli_key(p), qk)


def numeric_transition(kraus_emb: dict, E: np.ndarray, n: int, candidates):
    """The certifier's identity, recomputed directly on the complete register: the first relabel f (in candidate order,
    preferring an r-independent P') with K_r E = E'_r K_f(r) for all r, every E'_r a Pauli times a Lib quarter-turn and a
    common Q'.  Returns (f_name, f, {r: (p, s, a)}) or None."""
    d = 2 ** n
    fallback = None
    for fname, f in candidates:
        decs = {}
        ok = True
        for r, K in kraus_emb.items():
            B = kraus_emb[f[r]]
            nb = np.vdot(B, B).real / d
            Epp = K @ E @ B.conj().T / nb
            if np.linalg.norm(K @ E - Epp @ B) > 1e-8:
                ok = False; break
            dec = decompose_lib(Epp, n)
            if dec is None:
                ok = False; break
            decs[r] = dec
        if ok and len({(s, a) for _, s, a in decs.values()}) == 1:
            if len({p for p, _, _ in decs.values()}) == 1:
                return fname, f, decs
            fallback = fallback or (fname, f, decs)
    return fallback


def make_atom(row_id: str, wires: list) -> Atom:
    tid = "T2" if row_id.startswith("T2") else row_id
    bits = TABLES.bits_of(row_id)
    return Atom(atom_id="g#1" if tid == "T2" else "g#0", gadget_id="g", template_id=tid, events=("ev",),
                predicate_ids=frozenset(["u"]) if tid == "T2" else frozenset(), inputs=list(wires), outputs=list(wires),
                result_bits={b: b for b in bits}, segment=1 if tid == "T2" else 0)


def kraus_of(row_id: str):
    """{record: K on the template wires}, bit names, relabel candidates (as the certifier builds the row)."""
    if row_id.startswith("T2"):
        sigma = 1 if "flipped" in row_id else 0
        k = {(0,): np.eye(2, dtype=complex), (1,): certify.S_GATE}
        return k, ["u"], [(("identity" if sigma == 0 else (1,)), {r: (r[0] ^ sigma,) for r in k})]
    t = TEMPLATES[row_id]
    return t.kraus, list(t.bits), list(certify.relabel_candidates(t.kraus.keys()))


def load_fixture():
    es = EventStream.load(os.path.join(FIX, "fixture3.events.json"))
    lss = LatticeSurgerySchedule.from_str(read_lss(os.path.join(FIX, "fixture3.lss")), generate_dag_incrementally=True).full_schedule()
    return es, ScheduleView(lss.instructions, lss.to_dag(D), D)


# ------------------------------------------------------------------------------------------------------ canonical form
class TestCanonical(unittest.TestCase):
    def test_split_equals_certify_canonicalize_two_wires(self):
        """Every (P, Q) on two wires: the symbolic rule returns certify.canonicalize of the operator."""
        cn = certify.canon(2)
        for letters in cn.paulis:
            P = Pauli.of({i: l for i, l in enumerate(letters)})
            for qkey, Qm in cn.lib:
                s, supp = certify.parse_qkey(qkey)
                A = Pauli.of(supp)
                Pc, sc = canonical_split(P, s or 1, A, {0: 0, 1: 1})
                ours = certify.residual_key(pkey(Pc, {0: 0, 1: 1}), "I" if A.is_identity() else certify.rot_key(sc, letters_of(A, range(2))))
                self.assertEqual(ours, certify.canonicalize(certify.pauli_matrix(letters) @ Qm, 2), (letters, qkey))

    def test_split_in_certifier_canonical_set_three_wires(self):
        """Every (P, Q) on three wires (the C2 key domain of the two-wire templates): the rule's output is a member of the
        certifier's canonical set and represents the same operator."""
        cn = certify.canon(3)
        res = cn.residuals()
        pos = {0: 0, 1: 1, 2: 2}
        n_checked = 0
        for letters in cn.paulis:
            P = Pauli.of({i: l for i, l in enumerate(letters)})
            Pm = certify.pauli_matrix(letters)
            for qkey, Qm in cn.lib:
                s, supp = certify.parse_qkey(qkey)
                A = Pauli.of(supp)
                Pc, sc = canonical_split(P, s or 1, A, pos)
                ours = certify.residual_key(pkey(Pc, pos), "I" if A.is_identity() else certify.rot_key(sc, letters_of(A, range(3))))
                self.assertIn(ours, res, (letters, qkey))
                self.assertIsNotNone(certify.proportional(res[ours][2], Pm @ Qm), (letters, qkey, ours))
                n_checked += 1
        self.assertEqual(n_checked, 64 * 73)

    def test_residual_canonical_examples(self):
        Z, X, Y = (Pauli.of({"q0": p}) for p in "ZXY")
        self.assertEqual(Residual(Z, (), -1, Z).canonical().full_key(), "P=+I|Q=+Z(q0)")         # Z rot(-1, Z) ~ rot(+1, Z)
        self.assertEqual(Residual(X, (), -1, Z).canonical().full_key(), "P=+X(q0)|Q=-Z(q0)")     # X before Y in the key order
        self.assertEqual(Residual(Y, (), 1, Z).canonical().full_key(), "P=+X(q0)|Q=-Z(q0)")      # the same operator
        ZZ = Pauli.of({"a": "Z", "b": "Z"})
        self.assertEqual(Residual(Pauli.of({"b": "Z"}), (), -1, ZZ).canonical().full_key(), "P=+Z(a)|Q=+Z(a)Z(b)")
        r = Residual(Z, (("m", Pauli.of({"q0": "X"}, 3)),), -1, Z).canonical()                   # generators phase-free, unchanged
        self.assertEqual(r.full_key(), "P=+I|Q=+Z(q0)|m:+X(q0)")
        self.assertEqual(r.canonical(), r)

    def test_absorb_sign_is_canonical(self):
        """A fixed sign flip of the S residual rot(-1, Z) gives Z rot(-1, Z) = rot(+1, Z) with the conditional generator
        untouched (replaces the non-canonical expectation of test_route_a.TestPauli.test_sign_absorption)."""
        a = absorb_sign(Residual(I, (), -1, Pauli.of({"q": "Z"})), frozenset(["b"]), True)
        self.assertEqual(a.P.ops, ()); self.assertEqual(a.qsign, 1); self.assertEqual(dict(a.cond)["b"].ops, (("q", "Z"),))


# ------------------------------------------------------------------------------------------------------ finding 1: frames
class TestRecoveryFrames(unittest.TestCase):
    def test_composed_frame_recovers_every_residual(self):
        """For every Q in Lib (2 wires) and every Pauli P: the C3 script's PHYSICAL members as the certifier's intended
        template unitaries, followed by the composed frame (Q^dagger P^dagger Q times the script's frame members commuted
        to the end), equal E^dagger up to phase.  Negative control: without the script frames the identity fails for every
        script that has one."""
        order = ("a", "b")
        n_frames = 0
        for qkey, Qm in certify.canon(2).lib:
            entry = TABLES.c3[qkey]
            frames, frame_end = TABLES.script_frames(qkey, {0: "a", 1: "b"})
            U = np.eye(4, dtype=complex)
            for m in entry["script"]:
                if m["template"] != "frame":
                    U = certify.embed(TEMPLATES[m["template"]].intended, m["wires"], 2) @ U
            self.assertEqual(len(frames), sum(1 for m in entry["script"] if m["template"] == "frame"))
            s, supp = certify.parse_qkey(qkey)
            A = Pauli.of({order[i]: p for i, p in supp.items()})
            for letters in certify.canon(2).paulis:
                res = Residual(Pauli.of({order[i]: l for i, l in enumerate(letters) if l != "I"}), (), s or 1, A)
                E = certify.pauli_matrix(letters) @ Qm
                fixed, cond = res.recovery_frame()
                self.assertEqual(cond, ())
                total = fixed.mul(frame_end).unsigned()
                self.assertIsNotNone(certify.proportional(certify.pauli_matrix(letters_of(total, order)) @ U, E.conj().T), (qkey, letters))
                if not frame_end.is_identity():
                    self.assertIsNone(certify.proportional(certify.pauli_matrix(letters_of(fixed, order)) @ U, E.conj().T), (qkey, letters))
            n_frames += len(frames)
        self.assertGreater(n_frames, 0)

    def test_frame_positions_and_composition(self):
        back = {0: "a", 1: "b"}
        frames, end = TABLES.script_frames("rot(-1,Z0)", back)                 # S^dagger recovery = S gadget then Z
        self.assertEqual(frames, ((1, Pauli.of({"a": "Z"}), None),)); self.assertEqual(end, Pauli.of({"a": "Z"}))
        self.assertEqual(TABLES.script_frames("rot(-1,X0)", back), ((), I))    # Q^dagger = rot(+1, X) = S H S exactly
        frames, end = TABLES.script_frames("rot(+1,X0)", back)                 # rot(-1, X) = X S H S: one frame at the end
        self.assertEqual(frames, ((3, Pauli.of({"a": "X"}), None),)); self.assertEqual(end, Pauli.of({"a": "X"}))
        frames, end = TABLES.script_frames("rot(+1,Y0)", back)                 # rot(-1, Y) = X H: one H, frame X
        self.assertEqual(frames, ((1, Pauli.of({"a": "X"}), None),)); self.assertEqual(end, Pauli.of({"a": "X"}))
        frames, end = TABLES.script_frames("rot(-1,Y0Y1)", back)               # S(a) CNOT(b->a) H(b) CNOT(b->a) S(a), then X X
        self.assertEqual([(i, F.key()) for i, F, _ in frames], [(5, "+X(a)X(b)")])
        self.assertEqual(end, Pauli.of({"a": "X", "b": "X"}))
        self.assertEqual(TABLES.script_frames("rot(+1,Z0Z1)", back), ((), I))

    def test_lowered_and_step_frames_on_fixture(self):
        es, sched = load_fixture()
        sem = Semantics(compile_backend="fixture")
        c = build_contract(es, sem, sched, values_of(es)[0], True, 8)
        s0 = c.steps[0]                                                          # residual S = rot(-1, Z): script S, frame Z
        self.assertEqual(s0.residual.q_key(), "-Z(q0)")
        self.assertEqual(s0.recovery.frames, ((1, Pauli.of({"q0": "Z"}), None),))
        self.assertEqual(s0.recovery.frame_end, Pauli.of({"q0": "Z"})); self.assertEqual(s0.frame_after, (Pauli.of({"q0": "Z"}), ()))
        sx = next(s for s in c.steps if s.residual.q_key() == "-X(q0)")           # after the H: rot(-1, X): S H S, no frame
        self.assertEqual(sx.recovery.frames, ()); self.assertEqual(sx.frame_after[0], I)
        # the frame rule C4 must apply: C3's Q^dagger P^dagger Q table times the lowered script's composed frame
        for s in c.steps:
            wires = sorted(s.residual.axis.wires); pos = {w: i for i, w in enumerate(wires)}; back = {i: w for w, i in pos.items()}
            qkey = "rot(%+d,%s)" % (s.residual.qsign, "".join("%s%d" % (p, i) for i, p in sorted((pos[w], p) for w, p in s.residual.axis.ops)))
            table = TABLES.c3[qkey]["frame_after_rule"]["table"]
            expect = parse_pkey(table[local_pkey(s.residual.P, pos)], back).mul(s.recovery.frame_end)
            self.assertEqual(local_pkey(expect, pos), local_pkey(s.frame_after[0], pos), s.k)
        d = c.to_dict()["steps"][0]
        self.assertEqual((d["recovery_frames"], d["recovery_frame_end"], d["frame_after"][0]), ([[1, "+Z(q0)", None]], "+Z(q0)", "+Z(q0)"))


# ------------------------------------------------------------------------------------------------------ finding 2: walker
class TestWalkerCanonical(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.es, cls.sched = load_fixture()
        cls.w = Walker(cls.es, TABLES)

    def t2(self, E, L, D_fixed, row_id, wire="q0"):
        return self.w.template(E, frozenset(L), frozenset(D_fixed), frozenset(), make_atom(row_id, [wire]), row_id)

    def test_reproduced_T2_flipped_pure_X_creates_Q(self):
        """Review defect 1: T2[pred=flipped] on P=X0|Q=I must give P=X0|Q=rot(+1,Z0) (the walker returned Q=I)."""
        E, L, Df, Dc = self.t2(Residual(Pauli.of({"q0": "X"}), (), 1, I), ["q0"], ["u"], "T2[pred=flipped]")
        self.assertEqual(E.full_key(), "P=+X(q0)|Q=+Z(q0)"); self.assertEqual(L, frozenset(["q0"]))

    def test_reproduced_spectator_Q_uses_full_row(self):
        """Review defect 2: P=X0.X1|Q=rot(+1,Z1) with T2 on wire 0: the full two-wire C2 row fences for the flipped
        predicate (the walker admitted it); for the as-is predicate the row's Q is kept and u becomes a frame dependency."""
        E = Residual(Pauli.of({"q0": "X", "q1": "X"}), (), 1, Pauli.of({"q1": "Z"}))
        with self.assertRaises(Fence) as cm:
            self.t2(E, ["q0", "q1"], ["u"], "T2[pred=flipped]")
        self.assertEqual(cm.exception.reason, "c2_fence:T2[pred=flipped]:P=X0.X1|Q=rot(+1,Z1)")
        E2, L, Df, Dc = self.t2(E, ["q0", "q1"], [], "T2[pred=asis]")
        self.assertEqual(E2.full_key(), "P=+X(q0)X(q1)|Q=+Z(q1)|u:+Z(q0)"); self.assertEqual(Dc, frozenset(["u"]))

    def test_reproduced_Z_times_Sdg_canonical_before_H(self):
        """Review defect 3: Z0 rot(-1,Z0) canonicalises to rot(+1,Z0); the H lookup must not fence with no_c2_key."""
        E = Residual(Pauli.of({"q0": "Z"}), (), -1, Pauli.of({"q0": "Z"}))
        self.assertEqual(E.canonical().full_key(), "P=+I|Q=+Z(q0)")
        E2, L, Df, Dc = self.w.template(E, frozenset(["q0"]), frozenset(), frozenset(), make_atom("H", ["q0"]), "H")
        self.assertEqual(E2.full_key(), "P=+I|Q=+X(q0)")

    def test_Q_entirely_outside_the_template(self):
        """A Q on two wires the template does not touch commutes through; a Q the row creates would multiply it -- two
        quarter-turns are not in Lib -- so T2[pred=flipped] on P=X0 fences while T2[pred=asis] keeps the old Q."""
        E = Residual(Pauli.of({"q0": "X"}), (), 1, Pauli.of({"q1": "Z", "q2": "Z"}))
        with self.assertRaises(Fence) as cm:
            self.t2(E, ["q0", "q1", "q2"], ["u"], "T2[pred=flipped]")
        self.assertTrue(cm.exception.reason.startswith("q_product_not_in_lib:"), cm.exception.reason)
        E2, L, Df, Dc = self.t2(E, ["q0", "q1", "q2"], [], "T2[pred=asis]")
        self.assertEqual(E2.full_key(), "P=+X(q0)|Q=+Z(q1)Z(q2)|u:+Z(q0)")

    def test_signed_Z_enforced_independently_of_the_row_result(self):
        """A T row whose result is not a fence but whose signed-Z test is null (a mutated table) must still fence."""
        t = copy.deepcopy(TABLES)
        t.c2["T1"]["P=I|Q=rot(+1,X0)"] = {"result": "P=I|Q=rot(+1,X0)", "relabel": "identity", "signed_Z": None}
        w = Walker(self.es, t)
        with self.assertRaises(Fence) as cm:
            w.template(Residual(I, (), 1, Pauli.of({"q0": "X"})), frozenset(["q0"]), frozenset(), frozenset(), make_atom("T1", ["q0"]), "T1")
        self.assertEqual(cm.exception.reason, "t_signed_z:T1:P=I|Q=rot(+1,X0)")

    def test_random_transitions_match_direct_numerics(self):
        """Random (template row, P, Q) on the complete register -- template wires, the spectator, one far wire (P factors
        that commute through; Q may lie on the spectator only, or on spectator + far wire) -- compared with the identity
        recomputed directly from the templates' Kraus operators: fence iff no relabel gives canonical E'_r with a common
        Q'; otherwise the same Q', the same canonical P'_r for every record r (the walker's generators evaluated at r) and
        the same relabel (the walker's D bookkeeping)."""
        rng = random.Random(20260913)
        n_ok = n_fence = 0
        for row_id in CLIFFORD_ROWS + T_ROWS:
            kraus, bits, cands = kraus_of(row_id)
            n_t = len(TABLES.wires_of("T2" if row_id.startswith("T2") else row_id))
            wires = ["w%d" % i for i in range(n_t)]
            order = wires + ["s", "f"]
            n = len(order)
            kraus_emb = {r: certify.embed(K, list(range(n_t)), n) for r, K in kraus.items()}
            L = frozenset(order)
            D_in = frozenset(["u"]) if row_id == "T2[pred=flipped]" else frozenset()
            atom = make_atom(row_id, wires)
            for _ in range(24):
                P = Pauli.of({w: rng.choice("IXYZ") for w in order})
                k = rng.choice([0, 1, 1, 2, 2, 2])
                A = Pauli.of({w: rng.choice("XYZ") for w in rng.sample(order, k)})
                E = Residual(P, (), rng.choice([1, -1]) if k else 1, A)
                Em = matrix_of(E, order)
                expect = numeric_transition(kraus_emb, Em, n, cands)
                try:
                    E2, L2, Df, Dc = self.w.template(E, L, D_in, frozenset(), atom, row_id)
                except Fence as f:
                    self.assertIsNone(expect, (row_id, E.full_key(), f.reason, expect and lib_key(next(iter(expect[2].values())))))
                    if f.reason.startswith("t_signed_z"):
                        self.assertIsNone(certify.signed_z(Em, 0, n))
                    n_fence += 1
                    continue
                self.assertIsNotNone(expect, (row_id, E.full_key(), E2.full_key()))
                fname, f, decs = expect
                self.assertEqual(E2.canonical(), E2); self.assertTrue(E2.in_lib(L2)); self.assertEqual(L2, L)
                # relabel: the walker's D changes (T2 rows: the row itself carries the flip)
                if row_id.startswith("T2"):
                    mask = {"u": 1 if "flipped" in row_id else 0}
                else:
                    self.assertFalse(Dc & set(bits), (row_id, E.full_key(), Dc))             # no external generator: nothing conditional
                    mask = {b: (1 if b in Df - D_in else 0) for b in bits}
                self.assertEqual({r: tuple(x ^ mask[b] for x, b in zip(r, bits)) for r in kraus}, f, (row_id, E.full_key()))
                # the residual after the gadget, per record: P' G^bits Q' must be the direct decomposition
                for r in kraus:
                    got = decompose_lib(matrix_of(E2, order, dict(zip(bits, r))), n)
                    self.assertEqual(got, decs[r], (row_id, E.full_key(), r, E2.full_key()))
                n_ok += 1
        self.assertGreater(n_ok, 60); self.assertGreater(n_fence, 20)

    def test_generator_input_through_T1(self):
        """An input generator (frame conditioned on an external bit m) through T1.  A generator anticommuting with Z would
        relabel u iff m = 1: the walker's standing rule fences that (`cond_relabel_through_T`, conservative).  A generator
        commuting with Z propagates with the identity relabel, and the identity K_r E_m = E'_{m,r} K_r holds for both m
        with the fixed part's spectator Q kept and canonical."""
        order = ["w0", "s", "f"]
        kraus, bits, cands = kraus_of("T1")
        kraus_emb = {r: certify.embed(K, [0], 3) for r, K in kraus.items()}
        with self.assertRaises(Fence) as cm:
            self.w.template(Residual(I, (("m", Pauli.of({"w0": "X"})),), 1, Pauli.of({"s": "Z"})), frozenset(order), frozenset(), frozenset(),
                            make_atom("T1", ["w0"]), "T1")
        self.assertEqual(cm.exception.reason, "cond_relabel_through_T:P=X0|Q=I")
        E = Residual(Pauli.of({"f": "X"}), (("m", Pauli.of({"w0": "Z", "s": "X"})),), -1, Pauli.of({"s": "Z"}))
        E2, L2, Df, Dc = self.w.template(E, frozenset(order), frozenset(), frozenset(), make_atom("T1", ["w0"]), "T1")
        self.assertEqual((Df, Dc), (frozenset(), frozenset()))
        self.assertEqual(E2.full_key(), "P=+X(f)|Q=-Z(s)|m:+X(s)Z(w0)")           # ops sorted by wire name
        for m in (0, 1):
            for r, K in kraus_emb.items():
                lhs = K @ matrix_of(E, order, {"m": m})
                rhs = matrix_of(E2, order, {"m": m}) @ K
                self.assertIsNotNone(certify.proportional(lhs, rhs), (m, r))


if __name__ == "__main__":
    unittest.main(verbosity=2)
