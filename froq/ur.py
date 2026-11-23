"""Logical undo-fixup-redo (UR), routed like compensation.

The baseline of the matched comparison (Stalls and Spequlation's recovery): at boundary b of a walk the recovery undoes
the walked gates in reverse order, applies the producer's correction (the fixup) and redoes the gates. Under
`contract.RECOVERY_MODE == "ur_routed"` every boundary's recovery is UR's logical gate list instead of Froq's C3
script, lowered by the same converter router on the same layout (`Semantics.lower_ur`), so it is an ordinary `Lowered`
with its instructions, cells, duration, patch-rounds and semantic document, and the splice, workspace sizing, guard,
progress rule, decoder pool, hold, the decision's profiles and the reliability classes take it unchanged. The legacy
`undo_redo` mode (copies of the program's own instructions at their placement) is a different thing, left as is.

The kept gates. `URState.step` is `ur_static.analyse`'s rule, moved here unchanged and run as the walk emits each step: a step that leaves the residual's axis unchanged
(and keeps its Q) commutes with the pending correction up to a Pauli and is dropped; a step that changes it is kept when
every atom of the step but the producer's own is a Clifford gadget whose C1 action is unitary on every branch (UNITARY,
not the classically controlled CZ) or passive (PASSIVE); otherwise UR has no script there (`changes:<row>`). A
measurement or discard on a kept gadget's wire also leaves UR without a script (`destroyed_wire:<kind>`). A kept gadget
equal to the last kept gadget that shares a wire with it (the same row on the same wires) cancels it (the pair rule).
A boundary where UR has no script ends the walk there (`ur_no_script:<reason>`), as compensation's walk ends where its script has
no placement; none is expected in r8's library (`ur-static.md` section 5).

The list (`ur_members`). undo: the kept gadgets inverted, last kept first; then the fixup: the C3 script of the fixup
residual on the producer's wire (the entry residual E_0, whose script is the producer's correction: S, or S then a Z
frame); then redo: the kept gadgets in walk order. Gadgets are written in the router's members (S, H,
CNOT_cf_patched) and Pauli frames: S^dagger = S then Z, CZ = H(t) CNOT H(t), CNOT_tf_dual = CNOT (the same intended
unitary), a Pauli = a frame. The list with its frames is V Fix V^dagger, V the kept gadgets' product: a quarter-turn
about V Z_w V^dagger, which `sign_frame` checks is the residual's axis. When its sign is not the one the residual needs
(the dropped steps flip it, or a cancelled S pair leaves a Z) the list ends with one more frame, that axis, so the list
equals Q^dagger exactly and `frame_after` follows by compensation's own rule (Q^dagger P^dagger Q times the list's frame
members commuted to its end).

COMMIT (H's rebased walks). A commit at boundary j ran UR's list there; its undo is that list's inverse, which is the
same kept gadgets around the inverted fixup. The rebased walk therefore continues the UR state of boundary j (the same
kept chain, so the pair rule and the destroyed-wire test see the prefix's gadgets) with the fixup residual inverted
(`URState.rebased`); its boundary 0 is that inverse list."""
from __future__ import annotations

from .contract import FEEDBACK_ROW, local_pkey, local_qkey, parse_pkey
from .pauli import Pauli, inverse_residual

MEAS = ("measure_product", "measure_single")
UNITARY = {"S", "Sdg", "H", "X", "Y", "Z", "CNOT_cf_patched", "CNOT_tf_dual", "CZ"}   # C1: ~ U on every branch, U = U^-1 up to a Pauli
PASSIVE = {"frame_update", "init", "io", "transfer", "rotate_boundary", "busy_region", "T2_skip", "T2_S"}


def amap_of(es) -> dict:
    """atom id -> Atom over every gadget of the stream (ur_static's `amap`), built once per stream."""
    cache = es.__dict__.setdefault("_ur_amap", {})
    if "amap" not in cache:
        cache["amap"] = {at.atom_id: at for gid in es.gadgets for at in es.atoms_of(gid)}
    return cache["amap"]


def gadget(es, t, s, amap):
    """The step's gadget: its positive atom (the last one) as (C2 row, program wires by template position)."""
    a = s.atoms[-1]
    if a in amap:
        at = amap[a]
        return s.template, tuple(list(at.inputs)[:len(t.wires_of(at.template_id))])
    return es.events[a].kind, tuple(sorted(es.events[a].wires))


def kinds(es, s, amap, own_gadget):
    """Every atom of the step but the producer's own, as ('atom', C1 template, row) or ('event', kind, wires)."""
    out = []
    for a in s.atoms:
        if a in amap and amap[a].gadget_id == own_gadget:
            continue
        if a in amap:
            out.append(("atom", amap[a].template_id, s.template if a == s.atoms[-1] else amap[a].template_id))
        elif a in es.events:
            out.append(("event", es.events[a].kind, frozenset(es.events[a].wires)))
    return out


class URState:
    """UR's state along one walk: the kept gadgets [(row, wires, instrs, b)], the fixup residual (on local wire 0) and
    ur_static's counters.  `block` is None while UR has a script."""

    def __init__(self, fix, strict: bool = False):
        self.fix = fix
        self.kept: list = []
        self.dropped = 0
        self.pairs = 0
        self.block = None
        self.strict = strict          # (own reach): the producer's wire, which the fixup acts, counts as a kept wire

    def rebased(self) -> "URState":
        """The state of an H rebased walk at its boundary 0 (a commit's undo): the same kept chain, the fixup inverted."""
        r = URState(inverse_residual(self.fix), self.strict)
        r.kept, r.dropped, r.pairs = list(self.kept), self.dropped, self.pairs
        return r

    def step(self, es, t, s, amap, own_gadget, prev, res, b, instrs):
        """ur_static.analyse's update for walked step b (s: its atoms and template; prev / res: the residual before /
        after it; instrs: its .lss instructions other than the producer's own), unchanged."""
        ks = kinds(es, s, amap, own_gadget)
        row, wires = gadget(es, t, s, amap)
        changed = res.axis.unsigned() != prev.axis.unsigned() or res.has_q != prev.has_q
        kept, block = self.kept, self.block
        if block is None:
            kw = set().union(*(set(g[1]) for g in kept)) if kept else set()
            if self.strict:
                kw |= set(self.fix.axis.wires)
            for k in ks:
                if k[0] == "event" and k[1] in MEAS + ("discard",) and k[2] & kw:
                    block = "destroyed_wire:" + k[1]
            if block is None and changed:
                bad = [k for k in ks if not ((k[0] == "atom" and k[1] in UNITARY and k[2] != FEEDBACK_ROW) or k[1] in PASSIVE)]
                if bad:
                    block = "changes:" + (bad[0][2] if bad[0][0] == "atom" else bad[0][1])
                else:
                    g = (row, wires, instrs, b)
                    j = next((j for j in range(len(kept) - 1, -1, -1) if set(kept[j][1]) & set(wires)), None)
                    if j is not None and kept[j][0] == row and kept[j][1] == wires:
                        kept.pop(j)
                        self.pairs += 1
                    else:
                        kept.append(g)
            elif block is None:
                self.dropped += 1
        self.block = block

    def gates(self) -> tuple:
        """The kept chain as ((row, wires), ...) in walk order (the gate-list pass compares it with ur_static's)."""
        return tuple((g[0], tuple(g[1])) for g in self.kept)


def _gadget_members(row: str, wires: tuple, inverse: bool):
    """A kept gadget (or its inverse) as router members and frames on program wires; None when it has no lowering."""
    if row in ("CNOT_cf_patched", "CNOT_tf_dual"):
        return [("CNOT_cf_patched", tuple(wires))]
    if row == "H":
        return [("H", tuple(wires))]
    if row == "CZ":
        return [("H", tuple(wires[1:])), ("CNOT_cf_patched", tuple(wires)), ("H", tuple(wires[1:]))]
    if row in ("S", "Sdg"):                                      # S^dagger = S then Z (C3's rot(-1, Z) script)
        return [("S", tuple(wires))] + ([("frame", Pauli.of({wires[0]: "Z"}))] if (row == "S") == inverse else [])
    if row in ("X", "Y", "Z"):
        return [("frame", Pauli.of({wires[0]: row}))]
    return None


def unlowered(state: URState):
    """The first kept gadget's row that has no router lowering, or None."""
    return next((g[0] for g in state.kept if _gadget_members(g[0], g[1], True) is None), None)


def ur_members(t, state: URState, wire: str):
    """UR's logical list on program wires: [(template, wires) | ("frame", Pauli)], or (None, row) for a kept gadget with
    no lowering.  The fixup is the C3 script of `state.fix` (a quarter-turn about Z on the producer's `wire`)."""
    out = []
    for g in reversed(state.kept):
        m = _gadget_members(g[0], g[1], True)
        if m is None:
            return None, g[0]
        out += m
    fix = state.fix
    assert fix.axis.wires == {wire} and fix.P.is_identity() and not fix.cond, (fix, wire)
    entry = t.script(local_qkey(fix.qsign, fix.axis, {wire: 0}))
    if entry is None:
        return None, "fixup"
    for m in entry["script"]:
        if m["template"] == "frame":
            if m.get("predicate") is not None:
                return None, "fixup_predicated_frame"
            out.append(("frame", parse_pkey(m["pauli"], {0: wire})))
        else:
            out.append((m["template"], tuple(wire for _ in m["wires"])))
    for g in state.kept:
        out += _gadget_members(g[0], g[1], False)
    return out, None


def script_of(t, kept, fix, res, variant: str = "routed"):
    """UR's list at a boundary with residual `res`, from its kept gadgets ((row, wires)...) in walk order and its
    fixup residual `fix` (on the producer's wire): (the members on local wire indices as C3 writes them, the program
    wire of each index, whether the sign frame closes it), or (None, the row without a lowering, None).
    `Semantics.lower_ur` places it; C4 re-derives it from a Lowered's record (`Lowered.ur`).
    `variant` "peep" is the list after the peephole pass, "syn" its composite operator resynthesized (froq/ur_syn.py);
    "routed" is round 9's list."""
    st = URState(fix)
    st.kept = [(r, tuple(w), None, None) for r, w in kept]
    wire = next(iter(fix.axis.wires))
    mem, why = ur_members(t, st, wire)
    if mem is None:
        return None, why, None
    sign = sign_frame(st, wire, res)
    if sign is not None:
        mem = mem + [("frame", sign)]
    wires = sorted({wire}.union(*(set(w) for _r, w in kept)))
    pos = {w: i for i, w in enumerate(wires)}
    script = tuple(dict(template="frame", pauli=local_pkey(m[1], pos)) if m[0] == "frame" else dict(template=m[0], wires=[pos[w] for w in m[1]])
                   for m in mem)
    if variant != "routed":
        from . import ur_syn
        assert variant in ("peep", "syn"), variant
        script = tuple(ur_syn.peephole_checked(list(script), len(wires)) if variant == "peep" else ur_syn.synthesize(list(script), len(wires)))
    return script, wires, sign is not None


MAX_OPERATOR_WIRES = 8


def operator_check(script, wires, frame_end, res):
    """The list as an operator (C1's intended unitaries of its physical members, then the composed frame `frame_end`)
    against Q^dagger for the residual's Q (contract.py: E^dagger = (Q^dagger P^dagger Q) Q^dagger, the first factor
    being compensation's own frame rule): True / False, or None above MAX_OPERATOR_WIRES wires."""
    import numpy as np
    from .semantics import certify
    n = len(wires)
    if n > MAX_OPERATOR_WIRES:
        return None
    T = _templates()
    U = np.eye(2 ** n, dtype=complex)
    for m in script:
        if m["template"] != "frame":
            U = certify.embed(T[m["template"]].intended, m["wires"], n) @ U
    d = frame_end.as_dict()
    F = certify.pauli_matrix("".join(d.get(w, "I") for w in wires))
    A = res.axis.as_dict()
    want = certify.rot(-res.qsign, "".join(A.get(w, "I") for w in wires))
    return certify.proportional(F @ U, want) is not None


# ------------------------------------------------------------------------------------------- the list's sign (signed Paulis)
_CONJ: dict = {}


def _conj_local(row: str, letters: str):
    """U P U^dagger for the template's intended unitary U (froq.semantics.certify, C1) and the local Pauli `letters` (one
    letter per template wire): (sign, letters')."""
    key = (row, letters)
    if key not in _CONJ:
        import itertools
        import numpy as np
        from .semantics import certify
        tmpl = _templates()[row]
        U = tmpl.intended
        R = U @ certify.pauli_matrix(letters) @ U.conj().T
        n = len(letters)
        hit = None
        for cand in itertools.product("IXYZ", repeat=n):
            c = np.trace(certify.pauli_matrix("".join(cand)).conj().T @ R) / 2 ** n
            if abs(abs(c) - 1) < 1e-9:
                assert abs(c.imag) < 1e-9, (row, letters, c)
                hit = (1 if c.real > 0 else -1, "".join(cand))
                break
        assert hit is not None, (row, letters)
        _CONJ[key] = hit
    return _CONJ[key]


_TEMPLATES: dict = {}


def _templates():
    if not _TEMPLATES:
        from .semantics import certify
        _TEMPLATES.update(certify.build_templates())
    return _TEMPLATES


def signed_through(row: str, P: Pauli, wires: tuple) -> Pauli:
    """P -> U P U^dagger with its sign, for a kept gadget's intended unitary on `wires` (program wires by template position)."""
    d = P.as_dict()
    letters = "".join(d.get(w, "I") for w in wires)
    if letters == "I" * len(wires):
        return P
    sgn, out = _conj_local(row, letters)
    rest = P.without(set(wires))
    loc = Pauli.of({w: l for w, l in zip(wires, out) if l != "I"}, 0 if sgn > 0 else 2)
    return rest.mul(loc)


def sign_frame(state: URState, wire: str, res):
    """The Pauli that closes UR's list at a boundary with residual `res` (None when the list is already Q^dagger), after
    checking that the list's quarter-turn is about the residual's axis.  The list is V Fix V^dagger with Fix = rot(-s, Z_w)
    (C3's script of the fixup residual rot(s, Z_w) with its frames) and V the kept gadgets' product, so it is
    rot(-s * e, A) with V Z_w V^dagger = e A; the residual needs rot(-q, A) for its own Q = rot(q, A), and rot(q, A) is
    A rot(-q, A) up to phase."""
    P = Pauli.of({wire: "Z"})
    for g in state.kept:
        P = signed_through(g[0], P, g[1])
    if P.unsigned() != res.axis.unsigned():
        raise RuntimeError("UR's list is a quarter-turn about %s, the residual's axis is %s (kept %s)"
                           % (P.key(), res.axis.key(), [g[:2] for g in state.kept]))
    sigma = -state.fix.qsign * P.sign()
    return None if sigma == -res.qsign else res.axis.unsigned()
