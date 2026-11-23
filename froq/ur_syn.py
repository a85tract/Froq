"""Synthesized UR and peephole UR.

Round 9's routed UR (`ur.py`, `--recovery ur_routed`) places the logical list undo, fixup, redo (and a closing frame)
as it stands. Round 10 strengthens it the way an engineer without the certified library would, in two recovery modes:

- `ur_peep`, peephole UR (a named variant): the list after `peephole`, which commutes gates on disjoint wires, cancels
  adjacent inverse pairs across the undo, fixup and redo seams, merges adjacent single-wire Cliffords and repeats to a
  fixed point, keeping the order of gates that do not commute. It does not close the synthesis opportunity.
- `ur_syn`, synthesized UR: the composite Clifford operator of the whole list on its wires (at most 8) is taken as a
  tableau and resynthesized by elimination (`elimination`: the algorithm of stim's `Tableau.to_circuit` with
  method="elimination", written over this module's tableau, since stim is not part of the simulator's environment),
  mapped to the router's members (H, S, CX = CNOT_cf_patched; Pauli products become frames), then given the peephole
  pass. It never consults the library's certified words. Per snippet the recovery keeps whichever of this list and
  the peephole list has fewer placed rounds under the router (`contract.Semantics.lower_ur`; ties: fewer patch-rounds,
  then the synthesized list). If the synthesis recovers compensation's circuit, it keeps it.

Every list here is a member list as `ur.script_of` writes it: dict(template=H|S|CNOT_cf_patched, wires=[local index...])
or dict(template="frame", pauli=<local key>), and the output of both passes is its physical members followed by one
frame (none when it is the identity). Both passes keep the composite operator exactly (`Tableau.of_members` before and
after are equal, signs included; asserted), so the list still equals Q^dagger with its frame, and C4's
`ur.operator_check` checks it as before.

The tableau. `Tableau` holds the signed images of X_k and Z_k under the operator, each a Hermitian Pauli string with a
sign (x bits, z bits, sign bit); `append` conjugates every image by a gate applied after the operator (stim's
`inplace_scatter_append`), by CHP's update rules for H, S and CX, and by sign flips for a Pauli; `test_ur_syn` checks the
rules against C1's intended unitaries (`ur._conj_local`)."""
from __future__ import annotations

from .pauli import Pauli

GATES = ("H", "S", "CNOT_cf_patched")
_MEMBER_OF = {"H": "H", "S": "S", "CX": "CNOT_cf_patched"}


# ----------------------------------------------------------------------------------------------- Pauli strings as bits
def _g(x1, z1, x2, z2) -> int:
    """The power of i in P1 P2 for single-wire Hermitian Paulis (x, z) = X^x Z^z with (1, 1) = Y (Aaronson-Gottesman)."""
    if not x1 and not z1:
        return 0
    if x1 and z1:
        return z2 - x2
    if x1:
        return z2 * (2 * x2 - 1)
    return x2 * (1 - 2 * z2)


def pmul(a: tuple, b: tuple, n: int) -> tuple:
    """(x, z, e) * (x', z', e') for i^e times a Hermitian string: the product, as (x, z, e) with e mod 4."""
    e = a[2] + b[2]
    for k in range(n):
        e += _g((a[0] >> k) & 1, (a[1] >> k) & 1, (b[0] >> k) & 1, (b[1] >> k) & 1)
    return a[0] ^ b[0], a[1] ^ b[1], e % 4


def anticommute(a: tuple, b: tuple) -> int:
    return (bin(a[0] & b[1]).count("1") + bin(a[1] & b[0]).count("1")) & 1


def letters_of(p: tuple, n: int) -> str:
    return "".join("IXZY"[((p[0] >> k) & 1) + 2 * ((p[1] >> k) & 1)] for k in range(n))


def of_letters(letters: str, e: int = 0) -> tuple:
    x = z = 0
    for k, c in enumerate(letters):
        if c in "XY":
            x |= 1 << k
        if c in "ZY":
            z |= 1 << k
    return x, z, e % 4


def of_pkey(pk: str, n: int) -> tuple:
    """A certifier key ('X0.Z1', 'I') over local wires as (x, z, 0)."""
    L = ["I"] * n
    if pk != "I":
        for part in pk.split("."):
            L[int(part[1:])] = part[0]
    return of_letters("".join(L))


def pkey_of(p: tuple, n: int) -> str:
    parts = ["%s%d" % (c, k) for k, c in enumerate(letters_of(p, n)) if c != "I"]
    return ".".join(parts) if parts else "I"


# ------------------------------------------------------------------------------------------------------- the tableau
class Tableau:
    """A Clifford on n wires as the signed images of X_k (`xs[k]`) and Z_k (`zs[k]`): (x bits, z bits, e), e in {0, 2}."""

    def __init__(self, n: int):
        self.n = n
        self.xs = [(1 << k, 0, 0) for k in range(n)]
        self.zs = [(0, 1 << k, 0) for k in range(n)]

    def copy(self) -> "Tableau":
        t = Tableau(self.n)
        t.xs, t.zs = list(self.xs), list(self.zs)
        return t

    def __eq__(self, other) -> bool:
        return isinstance(other, Tableau) and (self.n, self.xs, self.zs) == (other.n, other.xs, other.zs)

    def is_identity(self) -> bool:
        return self == Tableau(self.n)

    @staticmethod
    def _conj(p: tuple, gate: str, ws) -> tuple:
        """U p U^dagger for a gate U applied on wires `ws` (CHP's rules; e stays 0 or 2)."""
        x, z, e = p
        if gate == "H":
            a = ws[0]
            xa, za = (x >> a) & 1, (z >> a) & 1
            e ^= 2 * (xa & za)
            x, z = (x & ~(1 << a)) | (za << a), (z & ~(1 << a)) | (xa << a)
        elif gate == "S":
            a = ws[0]
            xa, za = (x >> a) & 1, (z >> a) & 1
            e ^= 2 * (xa & za)
            z ^= xa << a
        elif gate == "CX":
            a, b = ws
            xa, za, xb, zb = (x >> a) & 1, (z >> a) & 1, (x >> b) & 1, (z >> b) & 1
            e ^= 2 * (xa & zb & (xb ^ za ^ 1))
            x ^= xa << b
            z ^= zb << a
        else:
            raise KeyError(gate)
        return x, z, e

    def append(self, gate: str, ws) -> None:
        """The operator followed by `gate` on wires `ws` (H, S, CX(control, target), or a Pauli given as (x, z, e))."""
        if gate == "pauli":
            P = ws
            self.xs = [(p[0], p[1], p[2] ^ 2 * anticommute(p, P)) for p in self.xs]
            self.zs = [(p[0], p[1], p[2] ^ 2 * anticommute(p, P)) for p in self.zs]
            return
        self.xs = [self._conj(p, gate, ws) for p in self.xs]
        self.zs = [self._conj(p, gate, ws) for p in self.zs]

    def image(self, p: tuple) -> tuple:
        """U p U^dagger for a Pauli (x, z, e) (e any power of i)."""
        out = (0, 0, p[2])
        for k in range(self.n):
            xk, zk = (p[0] >> k) & 1, (p[1] >> k) & 1
            if xk and zk:                                   # Y_k = i X_k Z_k
                out = pmul(out, pmul(self.xs[k], self.zs[k], self.n), self.n)
                out = (out[0], out[1], (out[2] + 1) % 4)
            elif xk:
                out = pmul(out, self.xs[k], self.n)
            elif zk:
                out = pmul(out, self.zs[k], self.n)
        return out

    def inverse(self) -> "Tableau":
        """U^-1: its image of X_j has X_k iff U(Z_k) anticommutes with X_j's preimage ... by the symplectic rule
        (U^-1(P) has X_k iff P anticommutes with U(Z_k), Z_k iff P anticommutes with U(X_k)), the sign fixed by U(U^-1(P)) = P."""
        n, out = self.n, Tableau(self.n)
        for gens, want in ((out.xs, lambda j: (1 << j, 0, 0)), (out.zs, lambda j: (0, 1 << j, 0))):
            for j in range(n):
                P = want(j)
                x = sum(anticommute(P, self.zs[k]) << k for k in range(n))
                z = sum(anticommute(P, self.xs[k]) << k for k in range(n))
                img = self.image((x, z, 0))
                assert img[0] == P[0] and img[1] == P[1] and img[2] in (0, 2), (img, P)
                gens[j] = (x, z, img[2])                       # U(q) = -P -> U^-1(P) = -q
        return out

    @staticmethod
    def of_members(members, n: int) -> "Tableau":
        """The operator of a member list (applied in order): H, S, CNOT_cf_patched (CX) and frames."""
        t = Tableau(n)
        for m in members:
            if m["template"] == "frame":
                t.append("pauli", of_pkey(m["pauli"], n))
            elif m["template"] == "CNOT_cf_patched":
                t.append("CX", tuple(m["wires"]))
            elif m["template"] in ("H", "S"):
                t.append(m["template"], tuple(m["wires"]))
            else:
                raise KeyError(m["template"])
        return t

    @staticmethod
    def of_rotation(qsign: int, letters: str) -> "Tableau":
        """rot(qsign, A) = exp(qsign i pi A / 4) for the Pauli string `letters`: P -> P if [P, A] = 0, else qsign i A P."""
        n = len(letters)
        A = of_letters(letters)
        t = Tableau(n)
        for gens in (t.xs, t.zs):
            for j, p in enumerate(gens):
                if anticommute(p, A):
                    q = pmul(A, p, n)                          # rot P rot^dagger = rot^2 P = (qsign i A) P for {P, A} = 0
                    gens[j] = (q[0], q[1], (q[2] + (1 if qsign > 0 else 3)) % 4)
        return t


# ------------------------------------------------------------------------------- elimination (stim's algorithm, ported)
def elimination(tab: Tableau) -> list:
    """A circuit over H, S, CX implementing `tab` (gates in application order, as (name, wires)): stim's
    `tableau_to_circuit_elimination_method` step for step.  It eliminates the inverse: `remaining` starts as tab^-1 and
    every recorded gate is appended to it, until it is the identity, so the recorded gates compose to `tab`."""
    rem = tab.inverse()
    n = tab.n
    out = []

    def apply(g, *ws):
        rem.append(g, ws)
        out.append((g, ws))

    def x_out(i, o):
        p = rem.xs[i]
        return ((p[0] >> o) & 1) + 2 * ((p[1] >> o) & 1)

    def z_out(i, o):
        p = rem.zs[i]
        return ((p[0] >> o) & 1) + 2 * ((p[1] >> o) & 1)

    for col in range(n):
        pivot = next(r for r in range(col, n) if x_out(col, r) and z_out(col, r) and x_out(col, r) != z_out(col, r))
        if pivot != col:                                   # move the pivot to the diagonal
            apply("CX", pivot, col)
            apply("CX", col, pivot)
            apply("CX", pivot, col)
        if z_out(col, col) == 3:                           # the pivot to X / Z
            apply("S", col)
        if z_out(col, col) != 2:
            apply("H", col)
        if x_out(col, col) != 1:
            apply("S", col)
        for r in range(col + 1, n):                        # the other terms of the X observable
            if x_out(col, r) == 3:
                apply("S", r)
        for r in range(col + 1, n):
            if x_out(col, r) == 2:
                apply("H", r)
        for r in range(col + 1, n):
            if x_out(col, r):
                apply("CX", col, r)
        for r in range(col + 1, n):                        # the other terms of the Z observable
            if z_out(col, r) == 3:
                apply("S", r)
        for r in range(col + 1, n):
            if z_out(col, r) == 1:
                apply("H", r)
        for r in range(col + 1, n):
            if z_out(col, r):
                apply("CX", r, col)
    zsign = [rem.zs[k][2] == 2 for k in range(n)]          # the signs (stim: X = H S S H, Z = S S)
    for k in range(n):
        if zsign[k]:
            apply("H", k)
    for k in range(n):
        if zsign[k]:
            apply("S", k)
            apply("S", k)
    for k in range(n):
        if zsign[k]:
            apply("H", k)
    for k in range(n):
        if rem.xs[k][2] == 2:
            apply("S", k)
            apply("S", k)
    assert rem.is_identity(), "elimination left a non-identity remainder"
    return out


def members_of_circuit(circ) -> list:
    """Elimination's gates as router members (CX -> CNOT_cf_patched: control, target)."""
    return [dict(template=_MEMBER_OF[g], wires=list(ws)) for g, ws in circ]


# --------------------------------------------------------------------------------------------------------- the peephole
# The single-wire Clifford group modulo Paulis has six classes; each run is replaced by its class's shortest word over
# H and S (fewest H among the shortest: H is the dearer member, `certify.template_timing`) times a Pauli frame.
_WORDS = ((), ("S",), ("H",), ("S", "H"), ("H", "S"), ("S", "H", "S"))


def _one_wire(word) -> Tableau:
    t = Tableau(1)
    for g in word:
        t.append(g, (0,))
    return t


def _class(t: Tableau) -> tuple:
    return tuple((p[0], p[1]) for p in t.xs + t.zs)


_WORD_OF = {_class(_one_wire(w)): w for w in _WORDS}
assert len(_WORD_OF) == 6


def _merge(word) -> tuple:
    """A run of single-wire gates (names, in order) as (the class's shortest word, the Pauli letter P with run = P word)."""
    t = _one_wire(word)
    w = _WORD_OF[_class(t)]
    for P in "IXYZ":
        u = _one_wire(w)
        if P != "I":
            u.append("pauli", of_letters(P))
        if u == t:
            return w, P
    raise AssertionError(word)


def _cost(word) -> tuple:
    return len(word), sum(1 for g in word if g == "H")


def _push_frames(members, n: int):
    """The physical members in order and the product of every frame member conjugated to the end (unsigned)."""
    phys, end = [], (0, 0, 0)
    for m in members:
        if m["template"] == "frame":
            end = pmul(end, of_pkey(m["pauli"], n), n)
        else:
            g = "CX" if m["template"] == "CNOT_cf_patched" else m["template"]
            end = Tableau._conj(end, g, tuple(m["wires"])) if end[0] or end[1] else end
            phys.append((g, tuple(m["wires"])))
    return phys, (end[0], end[1], 0)


def peephole(members, n: int) -> list:
    """The peephole pass over a member list on n local wires (module docstring): its physical members and one frame."""
    phys, end = _push_frames(members, n)
    changed = True
    while changed:
        changed = False
        # cancel a CX with the next gate sharing a wire with it when that gate is the same CX (only disjoint gates between)
        i = 0
        while i < len(phys):
            g, ws = phys[i]
            if g == "CX":
                j = next((j for j in range(i + 1, len(phys)) if set(phys[j][1]) & set(ws)), None)
                if j is not None and phys[j] == phys[i]:
                    del phys[j], phys[i]
                    changed = True
                    continue
            i += 1
        # merge each maximal run of single-wire gates on one wire (only gates on other wires between them)
        i = 0
        while i < len(phys):
            g, ws = phys[i]
            if g == "CX":
                i += 1
                continue
            w = ws[0]
            run = [i]
            for j in range(i + 1, len(phys)):
                if w in phys[j][1]:
                    if phys[j][0] == "CX":
                        break
                    run.append(j)
            word = tuple(phys[j][0] for j in run)
            new, P = _merge(word)
            if _cost(new) < _cost(word):
                for j in reversed(run):
                    del phys[j]
                for k, h in enumerate(new):
                    phys.insert(i + k, (h, (w,)))
                if P != "I":                                   # the Pauli after the merged word, conjugated to the end
                    q = of_letters("".join(P if k == w else "I" for k in range(n)))
                    for h in phys[i + len(new):]:
                        q = Tableau._conj(q, h[0], h[1])
                    end = pmul(end, q, n)
                    end = (end[0], end[1], 0)
                changed = True
                i += len(new)
                continue
            i += 1
    out = [dict(template=_MEMBER_OF.get(g, g), wires=list(ws)) for g, ws in phys]
    if end[0] or end[1]:
        out.append(dict(template="frame", pauli=pkey_of(end, n)))
    return out


def same_operator(a, b, n: int) -> bool:
    """Two member lists are the same operator up to a global phase (signed tableaux equal)."""
    return Tableau.of_members(a, n) == Tableau.of_members(b, n)


_SYN: dict = {}


def synthesize(members, n: int) -> list:
    """Synthesized UR's list: the composite operator of `members` resynthesized by elimination, then the peephole pass."""
    key = (tuple((m["template"], tuple(m.get("wires", ())), m.get("pauli")) for m in members), n)
    if key not in _SYN:
        tab = Tableau.of_members(members, n)
        out = peephole(members_of_circuit(elimination(tab)), n)
        assert Tableau.of_members(out, n) == tab, "synthesis changed the operator"
        _SYN[key] = out
    return [dict(m) for m in _SYN[key]]


def peephole_checked(members, n: int) -> list:
    out = peephole(members, n)
    assert same_operator(out, members, n), "the peephole pass changed the operator"
    return out
