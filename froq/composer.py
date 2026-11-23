"""Resolution-timetable composition (paper eq. 2) and latency families.

pmfs are dicts {rounds: prob} over non-negative integer delays.  The
composer turns calibrated stage pmfs plus live pipeline state into an
absolute-round Timetable; an event (upstream re-decode observed at t_ev)
rebuilds the timetable as t_ev + a fresh self-decode draw.
"""
from __future__ import annotations
import math
from .timetable import Timetable


# ------------------------------------------------------------ pmf tools
def normalize(p: dict[int, float]) -> dict[int, float]:
    z = sum(p.values())
    return {t: w / z for t, w in p.items() if w > 0}


def shift(p: dict[int, float], k: int) -> dict[int, float]:
    return {t + k: w for t, w in p.items()}


def mix(parts: list[tuple[float, dict[int, float]]]) -> dict[int, float]:
    out: dict[int, float] = {}
    for a, p in parts:
        for t, w in p.items():
            out[t] = out.get(t, 0.0) + a * w
    return out


def convolve(a: dict[int, float], b: dict[int, float]) -> dict[int, float]:
    out: dict[int, float] = {}
    for t1, w1 in a.items():
        for t2, w2 in b.items():
            out[t1 + t2] = out.get(t1 + t2, 0.0) + w1 * w2
    return out


def conditional_remaining(p: dict[int, float], age: int) -> dict[int, float]:
    """D^(age): remaining delay given `age` rounds already elapsed."""
    tail = {t - age: w for t, w in p.items() if t > age}
    if not tail:                       # ran past the table: due next round
        return {1: 1.0}
    return normalize(tail)


def mean(p: dict[int, float]) -> float:
    return sum(t * w for t, w in p.items()) / sum(p.values())


# ------------------------------------------------------ latency families
def ramp_pmf(lo: int, hi: int) -> dict[int, float]:
    """Linearly rising pmf on [lo, hi] (the paper's D_self shape)."""
    ws = {t: (t - lo + 1) for t in range(lo, hi + 1)}
    return normalize(ws)


def uniform_pmf(lo: int, hi: int) -> dict[int, float]:
    return normalize({t: 1.0 for t in range(lo, hi + 1)})


def geometric_tail_pmf(lo: int, mean_extra: float, cap: int) -> dict[int, float]:
    """lo + Geometric(mean_extra) truncated at cap: a tail-heavy decoder."""
    q = 1.0 / (1.0 + mean_extra)
    ws = {lo + k: q * (1 - q) ** k for k in range(0, cap - lo + 1)}
    return normalize(ws)


def decode_latency_pmf(window_rounds: int, regime: str,
                       jitter: dict[int, float] | None = None) -> dict[int, float]:
    """Decode time of a window of `window_rounds` rounds of syndrome.

    Base: SWIPER's r-factor model (decode time = r * window rounds).
    'calibrated': r = 0.4 with a +-30% triangular spread (stand-in until
        calibrate_latency.py drops measured PyMatching tables here).
    'tail': r = 1.0 with a geometric tail (mean 50% extra), the slow /
        congested regime.
    """
    if regime == "calibrated":
        base = max(1, round(0.4 * window_rounds))
        lo, hi = max(1, math.floor(0.7 * base)), math.ceil(1.3 * base)
        tri = {t: (min(t - lo, hi - t) + 1.0) for t in range(lo, hi + 1)}
        p = normalize(tri)
    elif regime == "tail":
        base = max(1, round(1.0 * window_rounds))
        p = geometric_tail_pmf(base, 0.5 * base, cap=base * 4)
    else:
        raise ValueError(regime)
    if jitter:
        p = normalize(convolve(p, jitter))
    return p


# ------------------------------------------------------------- composer
def compose(e_r: int, d_up_rem: dict[int, float], c_up: float,
            d_self: dict[int, float], E: int) -> Timetable:
    """Paper eq. (2): accepted branch lands with the upstream window;
    rejected branch adds a fresh self-decode, shift-and-add."""
    accept = {t: w for t, w in d_up_rem.items()}
    reject = convolve(d_up_rem, d_self)
    lam = mix([(c_up, accept), (1.0 - c_up, reject)])
    return Timetable(shift(lam, e_r), E=E)


def rebuild_on_reject(t_ev: int, d_self: dict[int, float], E: int) -> Timetable:
    """Upstream rejected its boundary at t_ev: the value's window is
    decoded again from scratch."""
    return Timetable(shift(d_self, t_ev), E=E)
