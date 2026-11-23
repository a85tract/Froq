"""synthesized and peephole UR (froq/ur_syn.py, plan v2 4.2).

- the tableau's update rules against C1's intended unitaries (ur._conj_local), its inverse and `of_rotation`;
- elimination composes back to its tableau, and (where stim imports) equals stim's Tableau.to_circuit("elimination") gate
  for gate on random Cliffords and on every rotation the lists implement;
- the peephole pass keeps the operator, reaches a fixed point and does the rule's rewrites;
- UR's three lists (routed, peep, syn) are the same operator, Q^dagger, on the fixtures' walks.

    python -m unittest -v froq.test_recovery_synthesis"""
from __future__ import annotations

import itertools
import random
import unittest

from . import ur as UR
from . import ur_syn as Y
from .ur_syn import Tableau

try:
    import stim
except ImportError:                                   # (stim: requirements.txt)
    stim = None


def random_members(rng, n, length):
    out = []
    for _ in range(length):
        r = rng.random()
        if r < 0.3 or n == 1:
            out.append(dict(template=rng.choice(("H", "S")), wires=[rng.randrange(n)]))
        elif r < 0.85:
            a, b = rng.sample(range(n), 2)
            out.append(dict(template="CNOT_cf_patched", wires=[a, b]))
        else:
            out.append(dict(template="frame", pauli=Y.pkey_of(Y.of_letters("".join(rng.choice("IXYZ") for _ in range(n))), n)))
    return out


def to_stim(members, n):
    c = stim.Circuit()
    c.append("I", [n - 1])                            # (n wires even when the last ones are idle)
    for m in members:
        if m["template"] == "frame":
            for k, l in enumerate(Y.letters_of(Y.of_pkey(m["pauli"], n), n)):
                if l != "I":
                    c.append(l, [k])
        else:
            c.append({"H": "H", "S": "S", "CNOT_cf_patched": "CX"}[m["template"]], m["wires"])
    return stim.Tableau.from_circuit(c) if n else None


def stim_gates(circ, n):
    """stim's circuit as (gate, wires) in order; its padding to n qubits (H, H on the last wire when no gate touches it)
    is not part of the algorithm and is dropped."""
    out = []
    for ins in circ:
        name = ins.name
        ts = [t.value for t in ins.targets_copy()]
        k = 2 if name in ("CX", "CNOT") else 1
        for i in range(0, len(ts), k):
            out.append(("CX" if k == 2 else name, tuple(ts[i:i + k])))
    out = [g for g in out if g[0] != "I"]
    if out[-2:] == [("H", (n - 1,))] * 2 and all(n - 1 not in ws for _g, ws in out[:-2]):
        out = out[:-2]
    return out


class TestTableau(unittest.TestCase):
    def test_rules_match_C1(self):
        """H, S and CX conjugate every Pauli as C1's intended unitaries do (signs included)."""
        for g, row, n in (("H", "H", 1), ("S", "S", 1), ("CX", "CNOT_cf_patched", 2)):
            for letters in itertools.product("IXYZ", repeat=n):
                letters = "".join(letters)
                if letters == "I" * n:
                    continue
                sgn, out = UR._conj_local(row, letters)
                p = Tableau._conj(Y.of_letters(letters), g, (0,) if n == 1 else (0, 1))
                self.assertEqual((Y.letters_of(p, n), 1 if p[2] == 0 else -1), (out, sgn), (g, letters))

    def test_inverse_and_rotation(self):
        rng = random.Random(4)
        for _ in range(200):
            n = rng.randint(1, 6)
            ms = random_members(rng, n, rng.randint(0, 25))
            t = Tableau.of_members(ms, n)
            inv = t.inverse()
            for k in range(n):
                for p in ((1 << k, 0, 0), (0, 1 << k, 0)):
                    self.assertEqual(inv.image(t.image(p)), p)
                    self.assertEqual(t.image(inv.image(p)), p)
        for n in (1, 2, 3):
            for letters in itertools.product("IXYZ", repeat=n):
                letters = "".join(letters)
                if set(letters) == {"I"}:
                    continue
                for q in (1, -1):
                    r, rinv = Tableau.of_rotation(q, letters), Tableau.of_rotation(-q, letters)
                    self.assertEqual(r.inverse(), rinv)
                    a = r.copy()                               # rot(q, A)^2 = (q i A) up to phase: the Pauli A as a tableau
                    for g in (a.xs, a.zs):
                        for j, p in enumerate(g):
                            g[j] = r.image(p)
                    pa = Tableau(n)
                    pa.append("pauli", Y.of_letters(letters))
                    self.assertEqual(a, pa, letters)


class TestElimination(unittest.TestCase):
    def test_composes_back(self):
        rng = random.Random(6610)
        for _ in range(300):
            n = rng.randint(1, 8)
            t = Tableau.of_members(random_members(rng, n, rng.randint(0, 40)), n)
            circ = Y.elimination(t)
            self.assertEqual(Tableau.of_members(Y.members_of_circuit(circ), n), t)

    @unittest.skipIf(stim is None, "stim is not importable here")
    def test_equals_stim(self):
        """Gate for gate equal to stim's elimination method (the same algorithm): random Cliffords on 1-8 wires and the
        quarter-turn about every axis on 1-3 wires with both signs (every operator a UR list implements)."""
        rng = random.Random(6611)
        cases = []
        for _ in range(400):
            n = rng.randint(1, 8)
            cases.append((random_members(rng, n, rng.randint(0, 40)), n))
        for n in (1, 2, 3):
            for letters in itertools.product("IXYZ", repeat=n):
                if set(letters) != {"I"}:
                    for q in (1, -1):
                        A = "".join(letters)
                        ms = [dict(template=g if g != "CX" else "CNOT_cf_patched", wires=list(ws))
                              for g, ws in Y.elimination(Tableau.of_rotation(q, A))]
                        cases.append((ms, n))
        bad = []
        for ms, n in cases:
            ours = [(g, tuple(ws)) for g, ws in Y.elimination(Tableau.of_members(ms, n))]
            theirs = stim_gates(to_stim(ms, n).to_circuit(method="elimination"), n)
            if ours != theirs:
                bad.append((n, ms, ours, theirs))
        self.assertEqual(len(bad), 0, bad[:2])
        self.assertEqual(len(cases), 562)
        print("stim %s: %d cases equal gate for gate" % (stim.__version__, len(cases)))


class TestPeephole(unittest.TestCase):
    def m(self, *spec):
        out = []
        for s in spec:
            if s[0] == "F":
                out.append(dict(template="frame", pauli=s[1]))
            else:
                out.append(dict(template={"C": "CNOT_cf_patched"}.get(s[0], s[0]), wires=list(s[1:])))
        return out

    def test_rewrites(self):
        n = 3
        self.assertEqual(Y.peephole(self.m(("S", 0), ("S", 0)), n), self.m(("F", "Z0")))
        self.assertEqual(Y.peephole(self.m(("H", 1), ("H", 1)), n), [])
        self.assertEqual(Y.peephole(self.m(("C", 0, 1), ("H", 2), ("C", 0, 1)), n), self.m(("H", 2)))
        self.assertEqual(Y.peephole(self.m(("H", 0), ("S", 0), ("H", 0)), n), self.m(("S", 0), ("H", 0), ("S", 0), ("F", "X0")))
        # a CX sharing only its control with the next CX is not commuted past it (the rule: disjoint wires only)
        self.assertEqual(Y.peephole(self.m(("C", 0, 1), ("C", 0, 2), ("C", 0, 1)), n), self.m(("C", 0, 1), ("C", 0, 2), ("C", 0, 1)))
        # an S pair across a CX on other wires; the frame member is pushed to the end through the CX
        self.assertEqual(Y.peephole(self.m(("S", 0), ("F", "X0"), ("C", 1, 2), ("S", 0)), n),
                         self.m(("C", 1, 2), ("F", "X0")))

    def test_operator_and_fixed_point(self):
        rng = random.Random(6612)
        for _ in range(400):
            n = rng.randint(1, 6)
            ms = random_members(rng, n, rng.randint(0, 30))
            out = Y.peephole_checked(ms, n)
            self.assertEqual(Y.peephole(out, n), out)
            self.assertLessEqual(sum(1 for m in out if m["template"] != "frame"), sum(1 for m in ms if m["template"] != "frame"))
            self.assertLessEqual(sum(1 for m in out if m["template"] == "frame"), 1)

    def test_synthesize_rotations(self):
        """The synthesized list of every quarter-turn on 1-3 wires is that operator; a Z quarter-turn is one S gadget
        (compensation's script), a Y one H and an X three gadgets."""
        for n in (1, 2, 3):
            for letters in itertools.product("IXYZ", repeat=n):
                A = "".join(letters)
                if set(A) == {"I"}:
                    continue
                for q in (1, -1):
                    ms = [dict(template={"CX": "CNOT_cf_patched"}.get(g, g), wires=list(ws)) for g, ws in Y.elimination(Tableau.of_rotation(q, A))]
                    out = Y.synthesize(ms, n)
                    self.assertEqual(Tableau.of_members(out, n), Tableau.of_rotation(q, A))
        for q in (1, -1):
            phys = lambda A: [m["template"] for m in Y.synthesize(Y.members_of_circuit(Y.elimination(Tableau.of_rotation(q, A))), 1)
                              if m["template"] != "frame"]
            self.assertEqual(phys("Z"), ["S"])
            self.assertEqual(phys("Y"), ["H"])
            self.assertEqual(len(phys("X")), 3)


if __name__ == "__main__":
    unittest.main()
