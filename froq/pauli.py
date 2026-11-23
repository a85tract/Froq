"""Pauli-product algebra and the canonical residual E = P * Q of the Route A contracts (design Sec. 2.2).

Why a separate module: the builder (froq/contract.py), the C4 checker and the tests all need the same three facts about a
residual -- how a Pauli propagates through a Clifford (multiplication with phases), whether a measurement commutes with it
(the signed-commutation test) and what the canonical key of a state is (the lookup key of the C2 / C3 tables).  Keeping the
algebra here keeps the builder a table walker, which is what the design demands ("the builder only looks them up").

Conventions.  A Pauli product is a mapping wire -> 'X' | 'Y' | 'Z' plus a phase i^k (k mod 4); the identity is the empty
mapping.  A quarter-turn Q = e^{s i pi A / 4} is stored as (s, A) with s in {+1, -1} and A a Pauli product WITHOUT phase;
S = e^{-i pi Z / 4} up to a global phase, S^dagger = e^{+i pi Z / 4}.  The library Lib of the design is {I} u {quarter-turns
about a weight <= 2 Pauli product}.  Keys: "P=+X(q0)Z(q1)|Q=-Z(q0)Z(q1)", "P=I|Q=I" (wires in sorted order; the sign of P is
kept because the same rule that moves a Pauli through a Clifford moves Q's axis, where the sign matters).

Symbolic frames.  After a destructive measurement or an outcome-dependent frame the Pauli factor P depends on classical
bits: P = P_fixed * prod_b G_b^{bit_b}.  `Residual.cond` holds the generators G_b keyed by result id; Q is always fixed
(an outcome-dependent rotation sign is normalised away: e^{-i pi A/4} = A e^{+i pi A/4} up to phase, so the sign flip
becomes a conditional Pauli factor A).  A transition that would make Q's axis itself outcome-dependent is a fence.

Canonical representative (the certifier's rule, froq/semantics/certify.py `Canon`).  The split E = P Q is not unique:
P rot(s, A) = (P A) rot(-s, A) up to phase, and these two are the ONLY decompositions with Q in Lib (rot(s, A) rot(-s', A')
is a Pauli only for A' = A).  certify.canonicalize returns the first candidate in the order (weight(P), P key, Q key) with
the key format "X0.Z1" over numbered wires; the two candidates' P keys differ, so the Q key never decides.
`canonical_split` implements exactly that comparison (tested against certify.canonicalize over the whole 2- and 3-wire
domain in froq/test_contract_semantics.py), `Residual.canonical` applies it with the wires numbered in sorted order of the
residual's support, and the C2 lookup applies it in the template's own wire numbering so the key matches the table's.
"""
from __future__ import annotations

from dataclasses import dataclass, field

_MUL = {("X", "Y"): ("Z", 1), ("Y", "Z"): ("X", 1), ("Z", "X"): ("Y", 1),
        ("Y", "X"): ("Z", 3), ("Z", "Y"): ("X", 3), ("X", "Z"): ("Y", 3)}


@dataclass(frozen=True)
class Pauli:
    ops: tuple = ()          # sorted tuple of (wire, 'X'|'Y'|'Z')
    phase: int = 0           # i^phase

    @staticmethod
    def of(d: dict, phase: int = 0) -> "Pauli":
        return Pauli(tuple(sorted((w, p) for w, p in d.items() if p != "I")), phase % 4)

    def as_dict(self) -> dict:
        return dict(self.ops)

    @property
    def wires(self) -> frozenset:
        return frozenset(w for w, _ in self.ops)

    @property
    def weight(self) -> int:
        return len(self.ops)

    def is_identity(self) -> bool:
        return not self.ops

    def restrict(self, wires) -> "Pauli":
        return Pauli(tuple((w, p) for w, p in self.ops if w in wires), 0)

    def without(self, wires) -> "Pauli":
        return Pauli(tuple((w, p) for w, p in self.ops if w not in wires), self.phase)

    def rename(self, mapping: dict) -> "Pauli":
        return Pauli.of({mapping.get(w, w): p for w, p in self.ops}, self.phase)

    def mul(self, other: "Pauli") -> "Pauli":
        a, b = self.as_dict(), other.as_dict()
        out, ph = {}, (self.phase + other.phase) % 4
        for w in set(a) | set(b):
            if w in a and w in b:
                if a[w] == b[w]:
                    continue
                r, k = _MUL[(a[w], b[w])]
                out[w] = r; ph = (ph + k) % 4
            else:
                out[w] = a.get(w) or b.get(w)
        return Pauli.of(out, ph)

    def commutes(self, other: "Pauli") -> bool:
        a, b = self.as_dict(), other.as_dict()
        return sum(1 for w in set(a) & set(b) if a[w] != b[w]) % 2 == 0

    def unsigned(self) -> "Pauli":
        return Pauli(self.ops, 0)

    def sign(self) -> int:
        """The real sign of the phase for a Hermitian product (phase 0 -> +1, phase 2 -> -1); i or -i means non-Hermitian."""
        assert self.phase in (0, 2), self
        return 1 if self.phase == 0 else -1

    def key(self, with_sign: bool = True) -> str:
        body = "".join("%s(%s)" % (p, w) for w, p in self.ops) or "I"
        if not with_sign:
            return body
        return ("+" if self.phase == 0 else "-" if self.phase == 2 else "i^%d" % self.phase) + body

    @staticmethod
    def parse(s: str, wires_map: dict | None = None) -> "Pauli":
        """Inverse of key(): '+X(q0)Z(q1)', '-Z(a)', 'I', 'Z(q)' (no sign = +)."""
        s = s.strip()
        phase = 0
        if s[:1] in "+-":
            phase = 0 if s[0] == "+" else 2; s = s[1:]
        if s == "I" or s == "":
            return Pauli((), phase)
        ops = {}
        for tok in s.split(")"):
            tok = tok.strip()
            if not tok:
                continue
            p, w = tok[0], tok[2:]
            assert tok[1] == "(" and p in "XYZ", s
            if wires_map is not None:
                w = wires_map[w]
            ops[w] = p
        return Pauli.of(ops, phase)


I = Pauli()


def conj_quarter(A: Pauli, s: int, P: Pauli) -> Pauli:
    """Q^dagger P Q for Q = e^{s i pi A/4}: P if [P, A] = 0, else the Pauli -s * i * P * A (a Pauli because {P, A} = 0)."""
    if P.commutes(A):
        return P
    out = P.mul(A)
    # e^{-s i pi A/4} P e^{+s i pi A/4} = P e^{s i pi A/2} = P (s i A) = s i P A
    return Pauli(out.ops, (out.phase + (1 if s > 0 else 3)) % 4)


def pkey(P: Pauli, pos: dict) -> str:
    """The certifier's Pauli key over numbered wires: 'X0.Z1' (parts in ascending index order), 'I' for the identity.
    Factors on wires missing from `pos` are left out (the caller restricts P first when that matters)."""
    parts = ["%s%d" % (p, pos[w]) for w, p in P.ops if w in pos]
    parts.sort(key=lambda s: int(s[1:]))
    return ".".join(parts) if parts else "I"


def canonical_split(P: Pauli, s: int, A: Pauli, pos: dict):
    """certify.canonicalize for the operator P rot(s, A), symbolically: of its two decompositions (P, rot(s, A)) and
    (P A, rot(-s, A)) return the phase-free P' and sign s' of the one that comes first in (weight(P'), P' key) with the wires
    numbered by `pos` (every wire of P and A must be in `pos`).  Q = I: P is the unique decomposition."""
    if A.is_identity():
        return P.unsigned(), 1
    alt = P.mul(A)
    if (alt.weight, pkey(alt, pos)) < (P.weight, pkey(P, pos)):
        return alt.unsigned(), -s
    return P.unsigned(), s


@dataclass(frozen=True)
class Residual:
    """E = P_fixed * prod_b G_b^{bit_b} * Q with Q = e^{s i pi A/4} or the identity (A empty)."""
    P: Pauli = I                                # phase kept but irrelevant for a frame
    cond: tuple = ()                            # sorted tuple of (result_id, Pauli) generators
    qsign: int = 1
    axis: Pauli = I                             # unsigned Pauli; empty = Q is the identity

    @property
    def has_q(self) -> bool:
        return not self.axis.is_identity()

    def in_lib(self, L=None) -> bool:
        """Lib membership: Q = I or a quarter-turn about a weight <= 2 product; with L given, the axis must lie inside L."""
        if not self.has_q:
            return True
        if self.axis.weight > 2:
            return False
        return L is None or self.axis.wires <= frozenset(L)

    def cond_map(self) -> dict:
        return dict(self.cond)

    def with_cond(self, cm: dict) -> "Residual":
        cm = {b: g for b, g in cm.items() if not g.is_identity()}
        return Residual(self.P, tuple(sorted(cm.items())), self.qsign, self.axis)

    def key(self) -> str:
        return "P=%s|Q=%s" % (self.P.key(), self.q_key())

    def q_key(self) -> str:
        if not self.has_q:
            return "I"
        return ("+" if self.qsign > 0 else "-") + self.axis.key(with_sign=False)

    def full_key(self) -> str:
        """key() plus the conditional generators: the state C4 compares after every transition."""
        return self.key() + "".join("|%s:%s" % (b, g.key()) for b, g in self.cond)

    def support(self) -> frozenset:
        w = set(self.P.wires) | set(self.axis.wires)
        for _, g in self.cond:
            w |= g.wires
        return frozenset(w)

    @staticmethod
    def parse(key: str, wires_map: dict | None = None) -> "Residual":
        pk, qk = key.split("|Q=")
        assert pk.startswith("P="), key
        P = Pauli.parse(pk[2:], wires_map)
        if qk.strip() == "I":
            return Residual(P, (), 1, I)
        s = 1 if qk[0] == "+" else -1
        A = Pauli.parse(qk[1:], wires_map)
        return Residual(P, (), s, A.unsigned())

    def canonical(self) -> "Residual":
        """The canonical representative (design Sec. 2.2, the certifier's rule): P and the generators phase-free (frames are
        Paulis up to phase), empty generators dropped, and the (P, Q) split chosen by `canonical_split` with the wires
        numbered in sorted order of the residual's support -- so e.g. Z rot(-1, Z) becomes I rot(+1, Z).  The generators
        are untouched by the split: P G^b rot(s, A) = +-(P A) G^b rot(-s, A), the sign a b-dependent global phase."""
        cond = tuple((b, g.unsigned()) for b, g in self.cond if not g.is_identity())
        if not self.has_q:
            return Residual(self.P.unsigned(), cond, 1, I)
        supp = sorted(self.P.wires | self.axis.wires)
        P, s = canonical_split(self.P, self.qsign, self.axis.unsigned(), {w: i for i, w in enumerate(supp)})
        return Residual(P, cond, s, self.axis.unsigned())

    def recovery_frame(self) -> tuple:
        """The frame applied after the physical script Q^dagger: Q^dagger P^dagger Q (fixed part) and the same for every
        conditional generator (design Sec. 2.2: E^dagger = (Q^dagger P^dagger Q) Q^dagger)."""
        if not self.has_q:
            return self.P.unsigned(), tuple((b, g.unsigned()) for b, g in self.cond)
        fixed = conj_quarter(self.axis, self.qsign, self.P).unsigned()
        cond = tuple((b, conj_quarter(self.axis, self.qsign, g).unsigned()) for b, g in self.cond)
        return fixed, cond


def absorb_sign(res: Residual, flip_bits: frozenset, fixed_flip: bool) -> Residual:
    """Normalise an outcome-dependent rotation sign: e^{-s i pi A/4} = A e^{+s i pi A/4} (up to phase), so a fixed flip
    multiplies P by A and a flip conditioned on bit b adds A to b's generator."""
    P, cm = res.P, res.cond_map()
    if fixed_flip:
        P = P.mul(res.axis)
    for b in flip_bits:
        cm[b] = cm.get(b, I).mul(res.axis)
    return Residual(P, (), res.qsign, res.axis).with_cond(cm).canonical()


def inverse_residual(res: Residual) -> Residual:
    """E^{-1} in canonical form (the hybrid mode H: section 3). With E = X rot(s, A), X the
    Pauli part P prod_b G_b^{bit_b}: E^{-1} = E^dagger up to phase = (Q^dagger X Q) Q^dagger, i.e. the Pauli factors of
    `recovery_frame` (conjugation by Q is a homomorphism, so each generator maps separately), the same axis and the
    opposite sign. A residual without Q is its own inverse (a Pauli, up to phase)."""
    if not res.has_q:
        return Residual(res.P, res.cond, 1, I).canonical()
    fixed, cond = res.recovery_frame()
    return Residual(fixed, (), -res.qsign, res.axis).with_cond(dict(cond)).canonical()


def inverse_residual_check(res: Residual) -> Residual:
    """E^{-1} by a second route, for the certificate check (C4 rebased mode): E^{-1} = rot(-s, A) X = X (X^dagger rot(-s, A) X),
    and X^dagger rot(-s, A) X = rot(-s, A) or rot(+s, A) as X commutes or anticommutes with A.  A generator that
    anticommutes with A flips the sign when its bit is 1, which `absorb_sign` normalises into a conditional factor A."""
    if not res.has_q:
        return Residual(res.P, res.cond, 1, I).canonical()
    flips = frozenset(b for b, g in res.cond if not g.commutes(res.axis))
    return absorb_sign(Residual(res.P, res.cond, -res.qsign, res.axis), flips, not res.P.commutes(res.axis))
