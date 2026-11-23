"""Composer golden (plan Sec. 1, 4.2, 5): the value's survival S[0..E_job]
into the Q8.48 accumulator and the overflow moment Omega (Q13.48), for the
three job shapes -- direct (no upstream), LEAF upstream, ENTRY upstream.

The arithmetic mirrors the RTL datapath: D lanes preloaded per 64-bin chunk,
one column per cycle, the rejected-branch sum of a chunk truncated once to
Q8.48 after the (1-c_up) scale, the accepted term added on chunk 0 only, and
the rounding to Q1.24 / Q13.24 done once when the solver reads the result.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .fx import (S_ONE, OMC_ONE, F_OMC, F_S, ACC_MAX, OMACC_MAX, OM_MAX, block_shift,
                 sat, rnd, trunc)
from .tables import Table

P_C = 64            # composer lanes (bins per chunk)


@dataclass
class Line:
    """A policy-pool line as a dependent composes from it (plan Sec. 1)."""
    S: list[int]      # Q1.24, length E+1; S[E] is the lump mass
    Om: int           # Q13.24
    E: int


@dataclass
class Composed:
    acc: list[int]            # Q8.48, length E_job+1 (index j)
    om_acc: int               # Q13.48
    shift: int
    exhausted: bool
    chunks: int
    flags: dict = field(default_factory=dict)

    def S_q(self) -> list[int]:
        """What the solver reads: round half up to Q1.24."""
        return [rnd(a, F_S) for a in self.acc]

    def Om_q(self) -> int:
        v = rnd(self.om_acc, F_S)
        if v > OM_MAX:                       # a saturated accumulator rounds above Q13.24: clamp, flag (round 14 #3)
            self.flags["om_sat"] = True; return OM_MAX
        return v


class _Upstream:
    """The conditioned upstream as the lanes/reads see it: S^(a)[j]*2^sh,
    R^(a)[j]*2^sh, with the exhausted impulse synthesised on every path."""

    def __init__(self, S_at, R_at, age: int, s_a: int, L_raw: int):
        self.age = age
        if s_a == 0:
            self.exhausted, self.sh = True, 0
            self.L = 1
        else:
            self.exhausted, self.sh = False, block_shift(s_a)
            self.L = max(1, L_raw)
        self._S_at, self._R_at = S_at, R_at

    def S(self, j: int) -> int:           # j >= 0
        if self.exhausted:
            return S_ONE if j == 0 else 0
        return self._S_at(self.age + j) << self.sh

    def R(self, j: int) -> int:           # j >= 0
        if self.exhausted:
            return S_ONE if j == 0 else 0
        return self._R_at(self.age + j) << self.sh

    def D(self, l: int) -> int:           # l >= 1; lanes beyond L are zero
        if self.exhausted:
            return S_ONE if l == 1 else 0
        if l > self.L:
            return 0                      # an ENTRY line's lump is folded in closed form
        return self.S(l - 1) - self.S(l)


def _run(up: _Upstream, omc_up: int, self_tbl: Table, E_job: int,
         lump_S: int = 0, lump_Om: int = 0, lump: bool = False) -> Composed:
    """Shared datapath for LEAF and ENTRY upstreams."""
    assert 1 <= E_job
    c_up = OMC_ONE - omc_up
    acc = [0] * (E_job + 1)
    om_acc = 0
    flags = {}
    chunks = (up.L + P_C - 1) // P_C
    # mass of an ENTRY upstream's line beyond the aligned horizon (plan Sec. 1),
    # folded in closed form together with chunk 0:
    #   S[j] += (1-c) * S_1[E_jobB] * m_self          for every j
    #   Om   += (1-c) * (S_1[E_jobB] * R_self[0] + m_self * Om_1)
    lump_s_term = trunc(omc_up * (lump_S * S_ONE), F_OMC) if lump else 0
    lump_om_term = (trunc(omc_up * (lump_S * self_tbl.R_at(0) + S_ONE * lump_Om), F_OMC)
                    if lump else 0)
    for q in range(chunks):
        lanes = [up.D(P_C * q + 1 + i) for i in range(P_C)]
        # Omega term of this chunk: dot product with R_self[E_job - l]
        rej_R = sum(lanes[i] * self_tbl.R_at(E_job - (P_C * q + 1 + i))
                    for i in range(P_C))
        term = trunc(omc_up * rej_R, F_OMC)
        if q == 0:
            term += (c_up * up.R(E_job)) << (F_S - F_OMC)     # Q11.44 -> .48
            term += lump_om_term
        om_acc = sat(om_acc + term, OMACC_MAX, flags, "om_sat")
        # column stream
        for j in range(E_job, -1, -1):
            rej = sum(lanes[i] * self_tbl.S_at(j - (P_C * q + 1 + i))
                      for i in range(P_C))
            term = trunc(omc_up * rej, F_OMC)
            if q == 0:
                term += (c_up * up.S(j)) << (F_S - F_OMC)     # Q1.44 -> .48
                term += lump_s_term
            acc[j] = sat(acc[j] + term, ACC_MAX, flags, "acc_sat")
    return Composed(acc=acc, om_acc=om_acc, shift=up.sh, exhausted=up.exhausted,
                    chunks=chunks, flags=flags)


def compose_direct(self_tbl: Table, age: int, E_job: int) -> Composed:
    """No upstream: the value's own table conditioned by its age."""
    assert 1 <= E_job
    s_a = self_tbl.S_at(age)
    flags = {}
    if s_a == 0:
        acc = [0] * (E_job + 1)
        acc[0] = S_ONE << F_S
        return Composed(acc=acc, om_acc=0, shift=0, exhausted=True, chunks=0,
                        flags=flags)
    sh = block_shift(s_a)
    acc = [(self_tbl.S_at(age + j) << sh) << F_S for j in range(E_job + 1)]
    om = (self_tbl.R_at(age + E_job) << sh) << F_S
    return Composed(acc=acc, om_acc=sat(om, OMACC_MAX, flags, "om_sat"),
                    shift=sh, exhausted=False, chunks=0, flags=flags)


def compose_leaf(up_tbl: Table, up_age: int, omc_up: int, self_tbl: Table,
                 E_job: int) -> Composed:
    """LEAF upstream: calibrated table conditioned by up_age, mixed with the
    fresh self table."""
    up = _Upstream(up_tbl.S_at, up_tbl.R_at, up_age, up_tbl.S_at(up_age),
                   up_tbl.support - up_age)
    return _run(up, omc_up, self_tbl, E_job)


def compose_entry(line: Line, delta: int, omc_link: int, self_tbl: Table,
                  E_job: int) -> Composed:
    """ENTRY upstream: the upstream's stored line at offset delta; horizons
    aligned (E_job == line.E - delta), the lump folded in closed form."""
    assert E_job == line.E - delta and E_job >= 1
    def S_at(j):
        return line.S[j] if 0 <= j <= line.E else (S_ONE if j < 0 else 0)
    def R_at(j):         # only R_1[E_jobB] == Om_1 is ever read
        assert j == line.E
        return line.Om
    s_a = S_at(delta)
    up = _Upstream(S_at, R_at, delta, s_a, E_job)
    if up.exhausted:
        return _run(up, omc_link, self_tbl, E_job)
    lump_S = line.S[line.E] << up.sh
    lump_Om = line.Om << up.sh
    return _run(up, omc_link, self_tbl, E_job, lump_S=lump_S, lump_Om=lump_Om,
                lump=True)
