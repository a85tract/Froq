"""UR's own reach.

Under `--ur-reach own` (contract.UR_REACH, a UR recovery mode only) a walk passes the propagation library's weight cap
(`Residual.in_lib`'s weight <= 2) and the C2 rows that fence a weight-growing step (a CNOT or CZ whose output axis leaves
the weight-2 library), up to UR's declared 8-wire limit. Every transition there is a C2 row certified on the fly by the
certifier's own rule, `certify.propagate`: K_r E = E'_r K_f(r) for every record r of the template's C1 instrument, with
E'_r = P'_r Q' and Q' independent of r (the instrument identity), the relabel f and the canonical choice exactly as
`certify.c2_table_for` makes them. The only difference from C2 is the Lib of the decomposition: every axis on the
template's wires and its spectator (`certify.Canon(n, max_weight=n)`), so the row's input and output axes may have weight
up to n_t + 1.

One spectator stands for every wire outside the template that the axis reaches (`SPECTATOR`): with A = A_in (x) A_out,
A_out a Pauli on wires the template does not touch, the algebra {I, A_out} is isomorphic to {I, Z_s} and every operator
of the identity acts on the template's wires and on A_out only (the P factors outside the template commute with the
gadget and are carried apart), so the identity on n_t + 1 wires is the identity on all of them. `test_ur_reach`
checks that reduction numerically against the full wire set.

The recovery is unchanged: UR's list at every boundary is still built from its kept chain (`ur.py`), and `ur.sign_frame`
requires the kept chain's axis V Z_w V^dagger to be the walk's certified axis, so the two derivations of the axis agree at
every boundary or the build raises. C4 checks the walk again (`check_contracts.rederive` runs these rows), the list's
operator (`ur.operator_check`, up to 8 wires), the frame rule past C3's keys (`frame_rule`, numerically) and the
recovery's footprint; C5a checks every exported region of at most 6 wires and 12 public bits numerically."""
from __future__ import annotations

SPECTATOR = "~s"                                    # the one spectator wire of an own-reach key (no program wire has this name)

_ROWS: dict = {}
_KINDS: dict = {}


def _kind(row_id: str):
    """(Kraus by record, template wires, bit names, relabel candidates, signed-Z wire, relabel text) of a C2 table,
    exactly as `certify.certify_c2` builds it."""
    if row_id in _KINDS:
        return _KINDS[row_id]
    import numpy as np
    from .semantics import certify as C
    T = _templates()
    if row_id in ("X", "Y", "Z", "H", "S", "Sdg", "CNOT_cf_patched", "CZ"):
        t = T[row_id]
        k = (t.kraus, t.n, t.bits, C.relabel_candidates, None, None)
    elif row_id in ("T1", "T"):
        t = T["T1" if row_id == "T1" else "T_ideal"]
        k = (t.kraus, 1, t.bits, C.relabel_candidates, 0, None)
    elif row_id in ("T2[pred=asis]", "T2[pred=flipped]"):
        sigma = 0 if row_id == "T2[pred=asis]" else 1

        def cands(records, sigma=sigma):
            yield ("identity" if sigma == 0 else (1,)), {r: (r[0] ^ sigma,) for r in records}
        k = ({(0,): np.eye(2, dtype=complex), (1,): C.S_GATE}, 1, ["u"], cands, 0, "predicate u read as u xor %d" % sigma)
    elif row_id == "CZ[pred=asis]":
        k = ({(0,): np.eye(4, dtype=complex), (1,): C.CZ_GATE}, 2, ["m"], lambda records: iter([("identity", {r: r for r in records})]), None,
             "predicate m read as recorded (the measured bit of an earlier MEASURE, not in D)")
    else:
        raise KeyError(row_id)
    _KINDS[row_id] = k
    return k


_T: dict = {}


def _templates():
    if not _T:
        from .semantics import certify
        _T.update(certify.build_templates())
    return _T


def residual_matrix(key: str, n: int):
    """'P=X0.Z1|Q=rot(+1,X0Z2)' on n local wires as a matrix (certify's conventions)."""
    from .semantics import certify as C
    pk, qk = key[2:].split("|Q=")
    L = ["I"] * n
    if pk != "I":
        for part in pk.split("."):
            L[int(part[1:])] = part[0]
    M = C.pauli_matrix("".join(L))
    if qk != "I":
        s, ax = C.parse_qkey(qk)
        M = M @ C.rot(s, "".join(ax.get(i, "I") for i in range(n)))
    return M


def ext_row(row_id: str, key: str):
    """The C2 row of `row_id` for the residual `key` on the template's wires and one spectator, certified on the fly
    (module docstring): the same entry `certify.c2_table_for` makes, with every axis in Lib.  None when the residual has
    no canonical decomposition there.  Cached per (row, key)."""
    ck = (row_id, key)
    if ck in _ROWS:
        return _ROWS[ck]
    import numpy as np
    from .semantics import certify as C
    kraus, n_t, bits, cands_fn, t_wire, rel_text = _kind(row_id)
    n = n_t + 1
    cn = C.canon(n, max_weight=n)
    kraus_emb = {r: C.embed(K, list(range(n_t)), n) for r, K in kraus.items()}
    E = residual_matrix(key, n)
    if cn.decompose(E) is None:
        _ROWS[ck] = None
        return None
    res = C.propagate(kraus_emb, E, cn, cands_fn(kraus_emb.keys()))
    entry = {}
    if res is None:
        entry["result"] = "fence"
    else:
        fname, f, decs = res
        qprime = next(iter(decs.values()))[1]
        ps = {r: p for r, (p, _) in decs.items()}
        if len(set(ps.values())) == 1:
            entry["result"] = C.residual_key(next(iter(ps.values())), qprime)
        else:
            p_letters = {}
            for r, pk_ in ps.items():
                letters = ["I"] * n
                if pk_ != "I":
                    for part in pk_.split("."):
                        letters[int(part[1:])] = part[0]
                p_letters[r] = "".join(letters)
            formula = C.pauli_formula(p_letters, bits) if len({len(r) for r in ps}) == 1 else None
            entry["result"] = {"Q": qprime, "P_formula": formula, "P_table": {C.record_str(r): p for r, p in ps.items()}}
        if fname == "identity":
            entry["relabel"] = rel_text or "identity"
        else:
            entry["relabel"] = rel_text or {"xor_mask": {b: m for b, m in zip(bits, fname)},
                                            "table": {C.record_str(r): C.record_str(v) for r, v in f.items()}}
    if t_wire is not None:
        sz = C.signed_z(E, t_wire, n)
        entry["signed_Z"] = None if sz is None else sz
    entry["own_reach"] = True
    _ROWS[ck] = entry
    return entry


def unspec(P, A_out):
    """A Pauli on program wires and the spectator: its spectator letter (Z or I) replaced by A_out (the outside part of the
    input axis).  An X or Y there would not be in the algebra {I, A_out}: it cannot occur (asserted)."""
    from .pauli import Pauli
    d = P.as_dict()
    if SPECTATOR not in d:
        return P
    assert d[SPECTATOR] == "Z", (P, A_out)
    return Pauli(P.without({SPECTATOR}).ops, P.phase).mul(A_out.unsigned())


_FRAME: dict = {}


def frame_rule(qsign: int, axis: str, p: str) -> str:
    """Q^dagger P^dagger Q for Q = rot(qsign, axis) as Pauli letters, numerically (C3's frame_after_rule for any weight):
    C4's check (5) past C3's keys."""
    ck = (qsign, axis, p)
    if ck not in _FRAME:
        import itertools
        import numpy as np
        from .semantics import certify as C
        n = len(axis)
        Q = C.rot(qsign, axis)
        Pm = C.pauli_matrix(p)
        R = Q.conj().T @ Pm.conj().T @ Q
        hit = None
        if n <= 6:
            for cand in itertools.product("IXYZ", repeat=n):
                if C.proportional(R, C.pauli_matrix("".join(cand))) is not None:
                    hit = "".join(cand)
                    break
        else:                                         # (7-8 wires: read the Pauli off its action, then confirm it)
            for cand in _pauli_candidates(R, n):
                if C.proportional(R, C.pauli_matrix(cand)) is not None:
                    hit = cand
                    break
        assert hit is not None, (qsign, axis, p)
        _FRAME[ck] = hit
    return _FRAME[ck]


def _pauli_candidates(R, n):
    """The Pauli letters R can be proportional to: its support pattern fixes the X part, its phases on the basis fix Z."""
    import numpy as np
    col = np.abs(R[:, 0])
    j = int(np.argmax(col))
    xs = [(j >> (n - 1 - k)) & 1 for k in range(n)]
    for zs in __import__("itertools").product((0, 1), repeat=n):
        yield "".join("IXZY"[x + 2 * z] for x, z in zip(xs, zs))
