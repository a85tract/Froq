"""Latency tables in the engine's stored form (plan Sec. 1, 4.1).

A table is two columns of length T+1: S[j] = P(delay > j) in Q1.24 and
R[j] = sum_{i>=j} S[i] in Q11.24, with S[T] = R[T] = 0.  Index conventions
used by the composer (plan Sec. 1): S[q] = m = 1.0 and R[q] = R[0] - q*m for
q < 0; S[q] = R[q] = 0 beyond the table.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
from .fx import S_ONE, F_S


@dataclass
class Table:
    S: list[int]      # length T+1, Q1.24, monotone non-increasing, S[T] == 0
    R: list[int]      # length T+1, Q11.24, R[j] = sum_{i>=j} S[i], R[T] == 0
    T: int
    support: int      # one past the last j with S[j] > 0  (0 for an empty table)

    def S_at(self, j: int) -> int:
        if j < 0:
            return S_ONE
        if j >= self.T:
            return 0
        return self.S[j]

    def R_at(self, j: int) -> int:
        if j < 0:
            return self.R[0] + (-j) * S_ONE
        if j >= self.T:
            return 0
        return self.R[j]


def validate(S: list[int], T: int) -> None:
    assert len(S) == T + 1, "S must have T+1 entries"
    assert S[T] == 0, "support exceeds T (S[T] != 0): table rejected"
    assert S[0] <= S_ONE, "S[0] > 1"
    for j in range(T):
        assert S[j] >= S[j + 1] >= 0, "S not monotone"


def from_S(S: list[int], T: int) -> Table:
    validate(S, T)
    R = [0] * (T + 1)
    acc = 0
    for j in range(T, -1, -1):
        acc += S[j]
        R[j] = acc
    support = 0
    for j in range(T):
        if S[j] > 0:
            support = j + 1
    return Table(S=S, R=R, T=T, support=support)


def from_pmf(pmf: dict[int, float], T: int) -> Table:
    """pmf over integer delays (>= 0).  Quantises S[j] = P(delay > j) to Q1.24.
    A pmf whose support reaches T is rejected (S[T] must be 0)."""
    S = []
    for j in range(T + 1):
        p = sum(w for t, w in pmf.items() if t > j)
        S.append(int(round(p * S_ONE)))
    S[0] = min(S[0], S_ONE)
    for j in range(1, T + 1):        # rounding can break monotonicity by 1 LSB
        S[j] = min(S[j], S[j - 1])
    return from_S(S, T)


def to_pmf(tbl: Table) -> dict[int, float]:
    """Exact pmf of the quantised table (for cross-checks against float code)."""
    out = {}
    prev = S_ONE                     # S[-1] = 1
    for j in range(tbl.T + 1):
        w = prev - tbl.S_at(j)
        if w > 0:
            out[j] = w / S_ONE
        prev = tbl.S_at(j)
    return out


# ---------------------------------------------------------------- pmf shapes
def normalize(p):
    z = sum(p.values())
    return {t: w / z for t, w in p.items() if w > 0}


def ramp_pmf(lo: int, hi: int):
    return normalize({t: (t - lo + 1) for t in range(lo, hi + 1)})


def uniform_pmf(lo: int, hi: int):
    return normalize({t: 1.0 for t in range(lo, hi + 1)})


def geometric_tail_pmf(lo: int, mean_extra: float, cap: int):
    q = 1.0 / (1.0 + mean_extra)
    return normalize({lo + k: q * (1 - q) ** k for k in range(0, cap - lo + 1)})


def impulse_pmf(t: int):
    return {t: 1.0}
