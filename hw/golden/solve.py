"""Solver golden (plan Sec. 1, 4.3, 5): the survival-scaled backward sweep
in the exact fixed-point formats, one column at a time, F_max+1 lanes.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import os as _os
from .fx import (F_OMC, F_XY, F_NS, F_S, V_MAX, BELIEF_FLOOR_SHIFT, rnd, sat)

F_MAX = int(_os.environ.get("SE_F_MAX", "4"))   # mirrors se_pkg::F_MAX (`SE_F_MAX): 4, or 8 for the K = 6 / 8 engine variant
F_W = (F_MAX + 1).bit_length() if F_MAX >= 2 else 1   # se_pkg::F_W = clog2(F_MAX + 1): 3 at F_MAX = 4, 4 at 8


@dataclass
class SiteClass:
    F: int
    d: list[int]          # durations in ticks, len F, 1 <= d_k <= 64
    r: list[int]          # recovery prices (integer patch-rounds), len F+1
    settle_dst: list[bool]  # per edge: True = settles at the destination
    ns_q: int             # n_s in Q8.8

    def r_fwd(self, k: int) -> int:
        return self.r[k + 1] if self.settle_dst[k] else self.r[k]


@dataclass
class Solved:
    A: list[list[int]]    # A[j][k] for j in 0..E_job, k in 0..F_MAX-1 (1 = FORWARD)
    Vh: list[list[int]]   # Vh[j][k] Q22.24, k in 0..F (lane F = fence)
    flags: dict = field(default_factory=dict)


def setup_xy(site: SiteClass, omc: int):
    """x_k = (1-c) r_fwd(k), y_k = (1-c) r_k in Q16.16, rounded once."""
    x = [rnd(omc * site.r_fwd(k), F_OMC - F_XY) for k in range(site.F)]
    y = [rnd(omc * site.r[k], F_OMC - F_XY) for k in range(site.F + 1)]
    return x, y


def solve(site: SiteClass, omc: int, S: list[int], Om: int) -> Solved:
    """S: Q1.24 list of length E_job+1 (S[E_job] = mass at/after the last
    column), Om: Q13.24.  Returns the action table and Vhat."""
    E = len(S) - 1
    F, ns = site.F, site.ns_q
    x, y = setup_xy(site, omc)
    flags = {}
    Vh = [[0] * (F + 1) for _ in range(E + 1)]
    A = [[0] * max(F_MAX, F) for _ in range(E + 1)]   # width follows a site with F > F_MAX (an F_MAX = 8 engine variant, E5's K sweep)
    floor = S[0]                              # WAIT where S[j] << 20 < S[0]

    def prod_xy(coef, ds):                    # Q16.16 x Q1.24 -> Q22.24
        return rnd(coef * ds, F_XY + F_S - F_S)   # >> 16
    def prod_ns(v):                           # Q8.8 x Q?.24 -> Q?.24
        return rnd(ns * v, F_NS)

    for k in range(F + 1):                    # seed column
        Vh[E][k] = sat(prod_ns(Om) + prod_xy(y[k], S[E]), V_MAX, flags, "v_sat")
    for j in range(E - 1, -1, -1):
        j1 = j + 1
        nsS = prod_ns(S[j])
        dS1 = S[j] - S[j1]
        # fence lane: the wait branch alone
        Vh[j][F] = sat(Vh[j1][F] + nsS + prod_xy(y[F], dS1), V_MAX, flags, "v_sat")
        below = (S[j] << BELIEF_FLOOR_SHIFT) < floor
        for k in range(F):
            j2 = min(j + site.d[k], E)
            # horizon rule (design note Sec. 4, MA-11 / MB-14): the forward edge is not available when
            # it would complete past the job horizon (j + d_k > E); completion exactly at E is allowed
            over = j + site.d[k] > E
            pF = prod_xy(x[k], S[j] - S[j2])
            pW = nsS + prod_xy(y[k], dS1)
            qF = pF + Vh[j2][k + 1]
            qW = pW + Vh[j1][k]
            if below or over:
                A[j][k], v = 0, qW
            else:
                fwd = qF <= qW                # ties -> FORWARD
                A[j][k], v = int(fwd), (qF if fwd else qW)
            Vh[j][k] = sat(v, V_MAX, flags, "v_sat")
    return Solved(A=A, Vh=Vh, flags=flags)


def evaluate(site: SiteClass, omc: int, S: list[int], Om: int,
             A: list[list[int]]) -> list[list[int]]:
    """Policy evaluation in the same fixed-point recurrence: the cost of
    following A instead of the optimum (used by the G2 regret check in
    float; here kept for symmetry)."""
    E = len(S) - 1
    F, ns = site.F, site.ns_q
    x, y = setup_xy(site, omc)
    Vp = [[0] * (F + 1) for _ in range(E + 1)]
    for k in range(F + 1):
        Vp[E][k] = rnd(ns * Om, F_NS) + rnd(y[k] * S[E], F_XY)
    for j in range(E - 1, -1, -1):
        j1 = j + 1
        nsS = rnd(ns * S[j], F_NS)
        Vp[j][F] = Vp[j1][F] + nsS + rnd(y[F] * (S[j] - S[j1]), F_XY)
        for k in range(F):
            j2 = min(j + site.d[k], E)
            assert not (A[j][k] and j + site.d[k] > E), "policy forwards past the horizon (horizon rule)"
            qF = rnd(x[k] * (S[j] - S[j2]), F_XY) + Vp[j2][k + 1]
            qW = nsS + rnd(y[k] * (S[j] - S[j1]), F_XY) + Vp[j1][k]
            Vp[j][k] = qF if A[j][k] else qW
    return Vp
