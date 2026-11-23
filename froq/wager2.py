"""the decision: v5's FORWARD/WAIT recurrence in critical-path rounds, with the branch beta and COMMIT, as
  specifies it. Task. The section numbers below are the spec's. The
options c0ban and kscale are the spec's amendment, off by default; r1, r2, lam and scope0
are the repair R1-R3', off by default too, r4 is iteration 1 (16.7), off by
default, r5 is iteration 1 as redefined (16.8), off by default, xic is iteration 2 (16.9), off by
default, p1 is iteration 2's backlog load level (16.10), off by default, and fixed is the fixed-advance
configuration (16.11), off by default.

One object per run (`ScoutDeviceManager._w2`), made by `_defer_setup` when an arm's `defer.wager2` is set. Its options:

  setting "main" (the decision), or an exact case's complete setting (section 7): "aa" Always Advance (rho = 0, kappa = 0,
           COMMIT not authorised, the c-state c0 forced; the arm's rule lazy, so the cap is D-lazy's), "awf" Advance
           While Flat (the same tables; the arm's rule no_cost_increase, so the cap is D-nci's own stop), "wfd"
           Wait-for-Decoder (registration disabled: no value is deferred, and arm D's D-only branches take N's path)
  bins the age bins below the horizon: "u" uniform in rounds, "q" equal-mass (section 6.1)
  ckappa c_kappa: kappa = c_kappa x mu of the key's producer class (section 4.3)
  horizon "a" (Lambda's q99) or "b" (its q99.9) (section 2)
  greedy the recurrence truncated to one action (section 10): each action, then wait for the verdict
  c0ban the amendment's c0 COMMIT ban: COMMIT is not in the sweep's legal set in the c-state c0 (no prediction in use)
  kscale "mu": kappa = c_kappa x mu (section 4.3); "mu/B": the amendment's kappa = c_kappa x mu / B, B the pool's cores
  r1 R1, the booked COMMIT (16.1): its admission horizon H_R1 in rounds; 0 = off (COMMIT only once every prefix
           member has started and a_p <= now + ell, F_5's rule)
  r2 R2, prices only (16.2): COMMIT's d_C gains a round and rho_0, rho_1 the start-wait residuals W_0, W_1 of the run's
           (d, budget level), from froq/data/wager2_r2.json (`W2R2`)
  lam R3's lambda (16.3): the recovery scripts' decoding work as a load surrogate, lam x theta_l x U / B rounds; 0 = off
  scope0 R3' (16.4): "lazy" (as before) or "nci": on beta = 0 the walk's table stops at the static walk's D-nci stop
  r4 R4 (16.7): the table's bin index is eta's, rule 30's causal estimate of the rounds left until the verdict, in
           place of the age's; each eta bin's entries come from a sweep under Lambda conditioned on the bin's range
  r5 R5 (16.8), saturation pricing: in the load bin l0 a beta = 0 ADVANCE may end only at or before the static
           walk's D-nci stop (Q = +inf on the l0 columns' blocks past it); l1, l2 and beta = 1 as before
  xic xi_C (16.9), the settlement price of an early COMMIT: None (off), "cell" or "program"; g_C gains xi_C rounds,
           read from the fit file (`--w2-xic`) by the mode's fallback chain and truncated at 0; d_C's duration unchanged
  p1 P1 (16.10), the backlog load level: l0 when B or more ready windows wait after the round's starts
           (`device._runnable_pending`), else l1 if f < 0.25, else l2; the tables never see it
  fixed the fixed-advance configuration (16.11): None (off), "aa" (v2-AA) or "awf" (v2-AWF); ADVANCE first wherever
           the next lane is within s, D-lazy's limit (the frame's stop) or D-nci's stop (`_nci_stop`), and admissible
           now; elsewhere the row's order of WAIT and COMMIT; the tables never see it
  vq round 7's V2q (plan v3 section 3(a); deploy): None (off) or the margin m, "inf", a margin of the fit file
           ("m0".. "m3") or a number of rounds >= 0. The tables gain the decoder backlog level b (`_runnable_pending`
           in the tick, beside the load bin, binned by the fit file's edges) as a column index; the recovery's delivery
           is priced XS_k + Delta(l, b, k) (k = 0 the ADVANCE's misprediction branch, XS; k = 1 the wrong COMMIT's, XR);
           and a value registers only if V(0 | c0, l, b) + m < J_N(l, b), J_N the same recurrence with FORWARD priced at
           +inf at every age (WAIT at age 0 is N's path, for good: `admit`). m = "inf" registers nothing: `wfd`'s path,
           no tables built
  vqd0 V2q-D0 (3(d)): Delta = 0; the edges, m and the admission kept, the tables and bits rebuilt
  noadv V2q-noA (3(d)): ADVANCE priced at +inf in the builder (both halves; tables and admission bits rebuilt); also
           without vq (plan v2 5.1: V2*-Sd-noA)
  nocom V2q-noC (3(d)): COMMIT priced at +inf in the builder (no rebased key; tables and admission bits rebuilt); also
           without vq (V2*-Sd-noC)
  co overhead price: None (off) or the price file's path (`--w2-co`, `W2Co`). Every
           ADVANCE Q the sweep makes (both halves; the stationary tail, below the horizon and the jumps) gains c_o rounds,
           the run's program-cell's attributable rounds per started ADVANCE step in its seed-1 run with price off and
           margin 0 (`ledger.deferral.r9.wq.adv`), static; c_o = 0 (S = 0, flagged) changes nothing
  mg per-program margin (2.3): "0" (off), "d" or "4d" rounds, added to ADVANCE's cost with the price. An
           infinite margin is N's run, which the manifest substitutes before setup (`_noreg` acts before registration);
           no option of this module makes it
  lf live-frame limit k: None (no limit, v15's design) or an integer k >= 1. A value
           that arrives while k frames are live is not registered and waits for its decode for good. The device's gate
           applies it (`_defer_register`, SER's refusal at k; run.py's `--w2-lf` sets both); this option records it
  coa commit on arrival, COA: None (off) or the threshold theta (`--w2-coa`). The
           value registers and gets its frame as without it. Before the splice the decision is COMMIT if the prediction
           in use (the c-state's, `_pred`) is m = 1 with p1 >= theta and COMMIT passes its checks at the frame's boundary
           (`_commit_ok`), else WAIT; ADVANCE is never eligible. After the splice it admits the rebased walk to its cap
           (ADVANCE eligible to the cap wherever admissible now, as the fixed rule aa), then waits. The recurrence, its
           margins and its ranking take no part. A confident m = 0 never commits: it waits for its decoded outcome

The parts, in the spec's order:
  - Lambda (`W2Lambda`): the frozen Kaplan-Meier survival per (d, budget level, Lambda-key), the horizons H, P(T >= H)
    and the constant tail hazard h (section 2; fitted on the training runs);
  - the static profiles (`site_profiles`): each site's static walk (`build_contract(deferral=True)`, no other frame's
    claims, no instruction started, as U's tables) and, at every boundary b where a commit can be built, the static
    rebased walk from b -- everything the sweep reads (section 6.1). They depend on the program, d, the code and the
    latency table only, so a run can read them from a cache file (`--w2-profiles`);
  - the keys and slots (`Tables`): rebased keys first, walk keys, merged by identical tables and allocated 64 + 64
    in decreasing order of what they cover; a site without a walk slot is refused, a (site, b) without a rebased slot
    has COMMIT unauthorised at b (section 6.1);
  - the backward sweep (`_sweep`): section 5.1 below the horizon, 5.2's stationary tail at and beyond it, the jumps over
    gaps in a frame's mask (section 6.3), the survival-weighted bin means and the preference orders with the jump flag
    in the 3-bit field's two unused codes (sections 6.2-6.3). Ties are differences of at most 2^-16 rounds.
    Arithmetic is float64 (the spec's fixed point would only make ties exact; float64's rounding at these magnitudes
    is far below the tie tolerance, and the computation is deterministic);
  - the run-time decision (`W2State.decide`, section 1.3): once a round, in the deferral phase, from the frame's cut
    (F_4's `_defer_current`) at its actual age; the admission limit r°, the take-back with `_defer_unadmit`, and COMMIT
    through the decision's path in H (the device's `_w2_act`), never at a modeled future age."""
from __future__ import annotations

import bisect
import collections
import gzip
import hashlib
import json
import math
import os
from types import SimpleNamespace

import numpy as np

from .contract import build_contract
from .u_tables import UTables, dnci_stop, peak_concurrency, script_windows, settle_rounds

LAMBDA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "wager2_lambda.json")
W2_DEFAULTS = dict(setting="main", bins="u", ckappa=1.0, horizon="a", greedy=False, c0ban=False, kscale="mu",
                   r1=0, r2=False, lam=0.0, scope0="lazy", r4=False, r5=False, xic=None, p1=False, fixed=None,
                   vq=None, vqd0=False, noadv=False, nocom=False, vqjn=False, vqr=False, co=None, mg="0", lf=None, coa=None, sd=None)
REPAIR = ("r1", "r2", "lam", "scope0", "r4", "r5", "xic", "p1", "fixed", "vq", "vqd0", "noadv", "nocom", "vqjn", "vqr", "co", "mg", "lf",
          "coa", "sd")    # (section 16's options: in the ledger only when not their default; sd, the start-delay file)
SD_FIELDS = ["sd_branch", "sd_A", "sd_c", "sd_M", "sd_charge", "rec_p", "rec_D", "rec_a", "x_vlane", "x_first_ok", "x_target",
             "x_start", "x_end"]  # (the diag row's fields under --w2-sd, after diagrec.REPAIR_FIELDS; `W2State._sd_fields`)
R2_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "wager2_r2.json")
R2_MIN_ITEMS, R2_MIN_PROGRAMS = 20, 2                 # (16.2: a fitted (d, level, branch) has 20 items from 2 programs)
XIC_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "wager2_xic.json")
XIC_MIN_COMMITS, XIC_MIN_PROGRAMS = 20, 2             # (16.9: a fitted cell has 20 early commits from 2 programs)
XIC_MODES = ("cell", "program")
FIXED_RULES = ("aa", "awf")                           # (16.11: Always Advance, Advance While Flat)
VQ_MARGINS = ("m0", "m1", "m2", "m3")                 # (V2q: the fit file's margins, plan v3 3(a))
VQ_RCAP = 3                  # ((b), frozen: the admission's reach index min(r, 3), r the certified boundaries past f0
                             #  admissible now; the last bin's walk is capped at 3 such boundaries, never the full walk)
VQ_QCAP = 4096                                        # (the round-7 records' raw backlog count stops here)
CO_MARGINS = ("0", "d", "4d")                         # (plan v2 2.3: the margin's finite candidates, 0, d and 4d rounds)
CO_FILE_MARGINS = CO_MARGINS + ("inf",)               # (the price file's margin field; "inf" is N's run, the manifest's rule)
SETTINGS = ("main", "aa", "awf", "wfd")
NP, NE, NC, NL, CAP, SLOTS = 17, 8, 6, 3, 16, 64      # lanes, age bins, c-states, load bins, the walk cap, slots a half
ELL, Q_MAX = 1, 7                                     # COMMIT's decision latency (4.2); the queue check's q_max (1.1)
TIE = 2.0 ** -16                                      # a tie: a difference of at most 2^-16 rounds (6.3)
C_REP = (None, 0.01, 0.25, 0.8, 0.99, 0.999)          # each c-state's representative pi_1; c0 the key's prior (3)
C_EDGES = (0.05, 0.5, 0.95, 0.995)                    # c1 [0, .05), c2 [.05, .5), c3 [.5, .95), c4 [.95, .995), c5
L_EDGES = (0.05, 0.25)                                # l0 f < .05, l1 .05 <= f < .25, l2 f >= .25 (4.3)
L_REP = (0.0, 0.15, None)                             # each bin's representative F_l / B (None: no shortfall)
ORDER0 = ("AWC", "ACW", "WAC", "WCA", "CAW", "CWA", "AWC", "ACW")   # the 3-bit codes; 6 and 7: no jump (6.3)
CODE0 = {o: i for i, o in enumerate(ORDER0[:6])}
ORDER1 = ("AW", "WA")                                 # the beta = 1 half's 1-bit codes
RANK = {"A": 0, "W": 1, "C": 2}
PROFILE_VERSION = 3          # (2: R3's script work U and R3's D-nci stop snci; 3: the rebased walk cut at CAP --
                             #  the cache file's name carries the version, so no earlier cache is read)
RECORDS_MAX = 200000
AGE_SAT = 1023              # the hardware's age register: 10 bits, saturating at 1,023; a lookup reads min(age, 1,023)
AGE_WRAP = 1 << 16          # (counted: values that would have wrapped a 16-bit round counter, as before a1c8e79)


# ------------------------------------------------------------------------------------------------------------ Lambda
class LamModel:
    """One Lambda-key's survival at one horizon setting: S(e) = P(T > e) from the KM below H, then P(T >= H) (1 - h)^(e - H
    + 1) (section 2, rules 5-6)."""

    def __init__(self, entry: dict, horizon: str):
        self.H = int(entry["H"][horizon])
        self.PH = float(entry["P_ge_H"][horizon])
        self.h = float(entry["h"][horizon])
        assert self.H >= 8 and 0.0 < self.PH <= 1.0 and 0.0 < self.h < 1.0, (entry["key"], self.H, self.PH, self.h)
        self.s = 1.0 - self.h
        S = np.ones(self.H)
        pts = entry["S"]
        k = 0
        cur = 1.0
        for t in range(self.H):
            while k < len(pts) and pts[k][0] <= t:
                cur = float(pts[k][1]); k += 1
            S[t] = cur
        assert abs(S[self.H - 1] - self.PH) < 1e-12, ("P(T >= H) is S(H - 1)", entry["key"], S[self.H - 1], self.PH)
        assert np.all(S > 0.0), entry["key"]
        self.S = S
        self.key = entry["key"]

    def surv(self, n: int) -> np.ndarray:
        """S(e) for e = 0 .. n - 1, the tail beyond H."""
        out = np.empty(n)
        m = min(n, self.H)
        out[:m] = self.S[:m]
        if n > self.H:
            k = np.arange(self.H, n) - self.H + 1
            out[self.H:] = self.PH * self.s ** k
        return out

    def mean_residual(self, n: int) -> np.ndarray:
        """M(e) = sum_{t >= e} S(t) / S(e) for e = 0 .. n - 1 (1/h from H on)."""
        out = np.full(n, 1.0 / self.h)
        tail = self.PH * self.s / self.h                     # sum_{t >= H} S(t)
        suf = np.cumsum(self.S[::-1])[::-1]                  # sum_{t = e}^{H - 1} S(t)
        m = min(n, self.H)
        out[:m] = (suf[:m] + tail) / self.S[:m]
        return out

    def edges(self, bins: str) -> tuple:
        """The age bins' upper edges e_1 .. e_7 (e_7 = H): uniform ceil(j H / 7), or equal-mass at j / 7 of the mass below H
        (section 6.1), strictly increasing."""
        H = self.H
        if bins == "u":
            ed = [int(math.ceil(j * H / 7.0)) for j in range(1, 7)]
        else:
            m = 1.0 - self.PH
            cdf = 1.0 - self.S
            ed = []
            for j in range(1, 7):
                t = int(np.searchsorted(cdf, j / 7.0 * m - 1e-15, side="left"))
                ed.append(max(1, min(t, H - 1)))
        out, prev = [], 0
        for j, x in enumerate(ed):
            x = max(x, prev + 1)
            x = min(x, H - (7 - (j + 1)))
            out.append(x)
            prev = x
        out.append(H)
        assert all(a < b for a, b in zip([0] + out, out)), (self.key, out)
        return tuple(out)


class EtaLaw:
    """R4 (16.7): one eta bin's verdict-arrival law, Lambda's T conditioned on a <= T < b (b None: T >= a), a >= 1 since a
    decision comes before its verdict, in rounds r since the decision: S_j(r) = (S(max(r, a - 1)) - S(b - 1)) / (S(a - 1)
    - S(b - 1)), with S(-1) = 1.  A bounded law ends at the bin's last age with mass t: its H is t, and S_j = 0 from t on.
    A bin without mass under Lambda takes T >= a alone (`empty`), as the tail bin [H, inf) does; those keep the key's H
    and its constant tail hazard h.  The sweep reads H, S, PH, h, s, surv and mean_residual, as of a LamModel."""

    def __init__(self, model: LamModel, a: int, b=None):
        self.key, self.h, self.s = model.key, model.h, model.s
        a = max(1, int(a))
        Sm = model.surv(max(model.H, a if b is None else int(b)) + 1)

        def S(t):
            return 1.0 if t < 0 else float(Sm[t])
        self.bounded = b is not None and S(a - 1) - S(int(b) - 1) > 0.0
        self.empty = b is not None and not self.bounded
        if self.bounded:
            b = int(b)
            mass = S(a - 1) - S(b - 1)
            self.H = max(x for x in range(a, b) if S(x - 1) > S(x))          # (the bin's last age with mass)
            self.S = np.array([max(0.0, (S(max(r, a - 1)) - S(b - 1)) / mass) for r in range(self.H)])
            self.PH = 0.0
        else:
            Sa = S(a - 1)
            self.H = model.H
            self.S = np.array([S(max(r, a - 1)) / Sa for r in range(model.H)])
            self.PH = float(self.S[-1])
        assert self.H >= 1 and np.all(self.S > 0.0), (self.key, a, b)

    def surv(self, n: int) -> np.ndarray:
        if not self.bounded:
            return LamModel.surv(self, n)
        out = np.zeros(n)
        m = min(n, self.H)
        out[:m] = self.S[:m]
        return out

    def mean_residual(self, n: int) -> np.ndarray:
        if not self.bounded:
            return LamModel.mean_residual(self, n)
        out = np.zeros(n)                                                  # (0 where S_j = 0: never weighted)
        suf = np.cumsum(self.S[::-1])[::-1]
        m = min(n, self.H)
        out[:m] = suf[:m] / self.S[:m]
        return out


def eta_laws(model: LamModel, edges) -> list:
    """R4 (16.7): the eight eta bins' laws on a half's age-bin edges e_1 < ... < e_7 = H: bin 0 is [0, e_1), bin j
    [e_j, e_{j+1}) and bin 7 [H, inf)."""
    ed = [int(x) for x in edges]
    assert len(ed) == NE - 1 and ed[-1] == model.H and all(x < y for x, y in zip([0] + ed, ed)), (model.key, ed)
    lo = [0] + ed
    return [EtaLaw(model, lo[j], ed[j] if j < NE - 1 else None) for j in range(NE)]


def eta_index(eta: float) -> int:
    """R4 (16.7): eta-hat = min(floor(eta), 1,023), the value the age register's comparator reads in place of the age."""
    assert eta >= 0.0, eta
    return min(int(math.floor(eta)), AGE_SAT)


def _r4_means(sw) -> tuple:
    """R4 (16.7): the eight bins' sweeps (each with q0) as the bin means `_codes` reads, {(j, p)} and {(j, p, b')}."""
    assert len(sw) == NE, len(sw)
    means, jmeans = {}, {}
    for j, (_V, _Vi, m, jm) in enumerate(sw):
        means.update({(j, p): x for (_z, p), x in m.items()})
        jmeans.update({(j, p, b2): x for (_z, p, b2), x in jm.items()})
    return means, jmeans


class W2Lambda:
    """The frozen Lambda file: Lambda-keys by (d, budget level, producer decode class), and each key's entry."""

    def __init__(self, path=None):
        path = path or LAMBDA_FILE
        raw = open(path, "rb").read()
        doc = json.loads(raw)
        self.path, self.sha = path, hashlib.sha256(raw).hexdigest()
        self.prov = doc.get("provenance", {})
        self.frozen = bool(self.prov.get("frozen"))
        self.keymap = {(int(x["d"]), str(x["level"]), str(x["vclass"])): str(x["key"]) for x in doc.get("classmap", [])}
        self.entries = {(int(e["d"]), str(e["level"]), str(e["key"])): e for e in doc.get("entries", [])}
        self._models: dict = {}

    def key_of(self, d: int, level: str, vclass: str):
        k = self.keymap.get((int(d), level, vclass))
        if k is None and not self.frozen and (int(d), level, "default") in self.entries:
            return "default"                                              # (the tests' stub: one key for every class)
        if k is None and self.frozen:
            k = self.unseen_key(int(d), level, vclass)[0]
        return k

    def unseen_key(self, d: int, level: str, vclass: str):
        """(key, how) for a class the frozen fit never saw (`froq.u6.unseen_class`): the key of the largest
        seen merge at or below it when the keys' median verdict latencies never fall as the merge grows; otherwise the
        (d, level)'s rare key."""
        from .u6 import unseen_class
        seen = {c: float(self.entries[(dd, lv, k)]["q"]["0.5"]) for (dd, lv, c), k in self.keymap.items() if dd == d and lv == level}
        c, how = unseen_class(seen, vclass)
        k = self.keymap[(d, level, c)] if c is not None else "rare"
        if how == "none" or (d, level, k) not in self.entries:
            raise AssertionError("the decision: the frozen Lambda (sha256 %s) has no key for %r" % (self.sha, (d, level, vclass)))
        return k, how

    def model(self, d: int, level: str, key: str, horizon: str) -> LamModel:
        mk = (int(d), level, key, horizon)
        m = self._models.get(mk)
        if m is None:
            m = self._models[mk] = LamModel(self.entries[(int(d), level, key)], horizon)
        return m


class W2R2:
    """R2's file (spec 16.2; fitted on seed-1 records): the start-wait residual W per
    (d, budget level, branch), in rounds, with its items and programs. `value` returns a setting's W and how it was set:
    "fitted" (at least 20 items from at least 2 programs), "interpolated" linearly in the budget factor between the
    fitted budgets around it at the same d, or "nearest" (the nearest fitted budget at that d, the lower on a tie). A d
    with no fitted budget takes rule 4 (amendment): "d_interpolated" linearly in d between the
    nearest fitted distances below and above at the same level and branch, "d_one_side" that side's W when only one
    exists, and "zero" (W = 0, F_5's price) when neither does; the label says so, so W is never 0 by omission."""

    def __init__(self, path=None):
        self.path = path or R2_FILE
        raw = open(self.path, "rb").read()
        doc = json.loads(raw)
        assert doc.get("version") == 1, ("the decision's R2 file: unknown version", self.path)
        self.sha = hashlib.sha256(raw).hexdigest()
        self.frozen = bool(doc.get("frozen"))
        self.fit = {(int(e["d"]), str(e["level"]), int(e["branch"])): float(e["W"]) for e in doc["entries"]
                    if int(e["items"]) >= R2_MIN_ITEMS and int(e["programs"]) >= R2_MIN_PROGRAMS}

    def value(self, d: int, level: str, branch: int) -> tuple:
        from .u6 import LEVEL_FACTOR
        k = (int(d), level, int(branch))
        if k in self.fit:
            return self.fit[k], "fitted"
        f = LEVEL_FACTOR[level]
        have = sorted((LEVEL_FACTOR[lv], w) for (dd, lv, br), w in self.fit.items() if dd == int(d) and br == int(branch))
        lo, hi = [x for x in have if x[0] < f], [x for x in have if x[0] > f]
        if lo and hi:
            (f0, w0), (f1, w1) = lo[-1], hi[0]
            return w0 + (w1 - w0) * (f - f0) / (f1 - f0), "interpolated"
        if have:
            return min(have, key=lambda x: (abs(x[0] - f), x[0]))[1], "nearest"
        ds = sorted(dd for (dd, lv, br) in self.fit if lv == level and br == int(branch))   # (rule 4: the distance step)
        lo, hi = [x for x in ds if x < int(d)], [x for x in ds if x > int(d)]
        if lo and hi:
            (d0, w0), (d1, w1) = [(x, self.fit[(x, level, int(branch))]) for x in (lo[-1], hi[0])]
            return w0 + (w1 - w0) * (int(d) - d0) / (d1 - d0), "d_interpolated"
        if lo or hi:
            return self.fit[((lo[-1:] or hi)[0], level, int(branch))], "d_one_side"
        return 0.0, "zero"


class W2Xi:
    """xi_C's file (spec 16.9; fitted on seed-1 records): the settlement price of an
    early COMMIT, in rounds per commit, per (program, d, budget level), per (d, level) cell, per d pooled and pooled over
    all, each with its early commits and programs. `value` returns (xi_C, its label, the untruncated value) by the
    mode's chain: "program" (program mode only: the program's entry, at least 20 commits), "cell", "d_pooled",
    "all_pooled" (each at least 20 commits from at least 2 programs), else "zero"; xi_C is the value truncated at 0."""

    def __init__(self, path=None):
        self.path = path or XIC_FILE
        with open(self.path, "rb") as f:
            raw = f.read()
        doc = json.loads(raw)
        assert doc.get("version") == 1, ("the decision's xi_C file: unknown version", self.path)
        self.sha = hashlib.sha256(raw).hexdigest()
        self.frozen = bool(doc.get("frozen"))
        ok = lambda e, n: int(e["commits"]) >= XIC_MIN_COMMITS and int(e["programs"]) >= n
        self.prog = {(str(e["program"]), int(e["d"]), str(e["level"])): float(e["xi"]) for e in doc["programs"] if ok(e, 1)}
        self.cell = {(int(e["d"]), str(e["level"])): float(e["xi"]) for e in doc["cells"] if ok(e, XIC_MIN_PROGRAMS)}
        self.dpool = {int(e["d"]): float(e["xi"]) for e in doc["d_pooled"] if ok(e, XIC_MIN_PROGRAMS)}
        a = doc.get("all_pooled")
        self.allpool = float(a["xi"]) if a and ok(a, XIC_MIN_PROGRAMS) else None

    def value(self, mode: str, program, d: int, level: str) -> tuple:
        assert mode in XIC_MODES, ("the decision: xi_C's mode", mode)
        chain = [("program", self.prog.get((str(program), int(d), str(level))))] if mode == "program" else []
        chain += [("cell", self.cell.get((int(d), str(level)))), ("d_pooled", self.dpool.get(int(d))), ("all_pooled", self.allpool)]
        for how, x in chain:
            if x is not None:
                return max(0.0, x), how, x
        return 0.0, "zero", 0.0


# ------------------------------------------------------------------------------------------------------------ profiles
class W2Vq:
    """V2q's fit (plan v3 section 3(a)), a file outside the tree (`--w2-vq`): {"edges": [<= 2 ints >= 1, ascending],
    "delta": [l][b][k] rounds (NL load bins, len(edges) + 2 backlog levels, the classes k = 0, 1), "margins": {"m0": ..},
    "frozen": bool}.  Backlog level 0 is an empty backlog; a positive count q is level 1 + (the edges below q)."""

    def __init__(self, path):
        assert path, "V2q at a finite margin needs its fit file (--w2-vq)"
        with open(path, "rb") as f:
            raw = f.read()
        self.path, self.sha = path, hashlib.sha256(raw).hexdigest()
        j = json.loads(raw)
        self.frozen = bool(j.get("frozen"))
        self.edges = [int(x) for x in j["edges"]]
        assert len(self.edges) <= 2 and all(x >= 1 for x in self.edges) and self.edges == sorted(set(self.edges)), ("V2q's edges", self.edges)
        self.NB = len(self.edges) + 2
        self.delta = np.array(j["delta"], dtype=float)
        assert self.delta.shape == (NL, self.NB, 2) and np.all(np.isfinite(self.delta)), ("V2q's Delta is [l][b][k]", self.delta.shape)
        self.margins = {k: float(x) for k, x in j.get("margins", {}).items()}
        assert set(self.margins) <= set(VQ_MARGINS) and all(x >= 0 for x in self.margins.values()), ("V2q's margins", self.margins)

    def level(self, q: int) -> int:
        return 0 if q <= 0 else 1 + bisect.bisect_left(self.edges, q)

    def margin(self, spec) -> float:
        if spec in VQ_MARGINS:
            assert spec in self.margins, ("V2q: the fit file has no margin", spec)
            return self.margins[spec]
        return float(spec)


class W2Co:
    """(plan v2 2.2-2.3): the overhead price's file, outside the tree (`--w2-co`; fitted on
    X2's seed-1 runs, price off and margin 0): {"version": 1, "kind": "wager2_co",
    "frozen": bool, "cells": {"<program>:<d>:<level>": {"co": rounds, "S": steps, "A": rounds, "flag": "S0" or None,
    "margin": None or one of CO_FILE_MARGINS..}}}. c_o = A / S, the attributable rounds over the started ADVANCE steps
    of the program-cell's run (`ledger.deferral.r9.wq.adv`); 0, flagged "S0", where S = 0. The margin is the selection's
    (None until it is made); the run's margin is its own option `mg`, which the manifest sets from it."""

    def __init__(self, path):
        assert path, "the decision's price needs its file (--w2-co)"
        with open(path, "rb") as f:
            raw = f.read()
        doc = json.loads(raw)
        assert doc.get("version") == 1 and doc.get("kind") == "wager2_co", ("the decision's price file: unknown version or kind", path)
        self.path, self.sha, self.frozen = path, hashlib.sha256(raw).hexdigest(), bool(doc.get("frozen"))
        self.cells = {}
        for k, e in doc["cells"].items():
            S, A, co = int(e["S"]), int(e["A"]), float(e["co"])
            assert S >= 0 and A >= 0 and (S or not A) and co == (A / S if S else 0.0) and (e.get("flag") == "S0") == (S == 0), \
                ("the decision's price file: an entry that is not A / S", k, e)
            assert e.get("margin") is None or e["margin"] in CO_FILE_MARGINS, ("the decision's price file: a margin", k, e)
            self.cells[k] = dict(co=co, S=S, A=A, flag=e.get("flag"), file_margin=e.get("margin"))

    def value(self, bench, d: int, level: str) -> dict:
        key = "%s:%d:%s" % (bench, int(d), level)
        e = self.cells.get(key)
        assert e is not None, ("the decision: the price file has no entry for the run's program-cell", self.path, key)
        return dict(key=key, **e)


class W2Sd:
    """(start-delay note v2 2.2-2.3): the correction's frozen file, outside the tree (`--w2-sd`; fitted on
    NEW controls at seeds 7154-7156): {"version": 1, "kind": "wager2_sd", "frozen":
    bool, "c": {"<d>": rounds}, "M": {"<d>": rounds}, "inputs": {.. sha256}}. c(d) is the in-flight offset of the start wait
    W = max(0, A(e) + c(d)) and M(d) the settlement that replaces XS on the walk half."""

    def __init__(self, path):
        assert path, "the decision's start-delay correction needs its file (--w2-sd)"
        with open(path, "rb") as f:
            raw = f.read()
        doc = json.loads(raw)
        assert doc.get("version") in (1, 2) and doc.get("kind") == "wager2_sd", ("the decision's start-delay file: unknown version or kind", path)
        self.path, self.sha, self.frozen = path, hashlib.sha256(raw).hexdigest(), bool(doc.get("frozen"))
        self.c = {int(k): float(v) for k, v in doc["c"].items()}
        self.M = {int(k): float(v) for k, v in doc["M"].items()}
        assert set(self.c) == set(self.M) and all(math.isfinite(v) for v in self.c.values()) and \
            all(math.isfinite(v) and v >= 0 for v in self.M.values()), ("the decision's start-delay file: c(d) and M(d)", path)
        # (full-scope note v2 3.2): version 2 adds "off", the distances whose fit had no eligible configuration
        # for c or for M; there the whole correction is off and the run takes the current prices.  Version 1 has none.
        self.off = {int(k) for k in doc["off"]} if doc["version"] == 2 else set()
        assert not self.off & set(self.c), ("the decision's start-delay file: a distance both fitted and off", path)

    def value(self, d: int) -> tuple | None:
        """(c(d), M(d)), or None where the file turns the correction off at d (version 2's "off")."""
        if int(d) in self.off:
            return None
        assert int(d) in self.c, ("the decision: the start-delay file has no entry for the run's d", self.path, d)
        return self.c[int(d)], self.M[int(d)]


def sd_resid(Sf: np.ndarray, D: int, H: int) -> np.ndarray:
    """(note v2 2.2): A(e) for e = 0.. H - 1, the expected rest of an advance of D rounds begun at age e given that the
    verdict lands in it, E[e + D - T | e < T <= e + D] = sum_{u=1}^{D-1} (S(e) - S(e + u)) / (S(e) - S(e + D)), from the survival
    Sf (at least H + D entries; past the horizon it is the tail's, so an advance that crosses H reads it); 0 where
    S(e + D) = S(e), an advance no verdict can land in (its branch weighs 1 - r_D = 0)."""
    D = int(D)
    e = np.arange(H)
    C = np.concatenate(([0.0], np.cumsum(Sf[:H + D])))                    # (C[k] = S(0) + .. + S(k - 1))
    den = Sf[e] - Sf[e + D]
    num = (D - 1) * Sf[e] - (C[e + D] - C[e + 1])
    out = np.zeros(H)
    ok = den > 0
    out[ok] = np.clip(num[ok] / den[ok], 0.0, max(D - 1.0, 0.0))
    return out


def sd_resid_tail(s: float, D: int) -> float:
    """A in the stationary tail (constant hazard 1 - s), the same at every age: sum_{u=1}^{D-1} (1 - s^u) / (1 - s^D); 0 when
    s^D = 1."""
    D = int(D)
    den = 1.0 - s ** D
    if D <= 1 or not den > 0:
        return 0.0
    return float(min(max(float(np.sum(1.0 - s ** np.arange(1, D))) / den, 0.0), D - 1.0))


def sd_resid_at(S: np.ndarray, a: int, D: int) -> float:
    """A at one start age a (the diag row, the fit tool), summed over the advance's rounds: E[a + D - T | a < T <= a + D] with
    P(T = t) = S(t - 1) - S(t); 0 at zero arrival mass. The training check's `acond`, term for term."""
    t = np.arange(a + 1, a + D + 1)
    pm = S[t - 1] - S[t]
    den = float(pm.sum())
    if den <= 0:
        return 0.0
    return float(((a + D - t) * pm).sum() / den)


def margin_rounds(mg: str, d: int) -> int:
    """The margin m in rounds (plan v2 2.3): "0", "d" (one logical cycle, d rounds) or "4d"."""
    assert mg in CO_MARGINS, ("the decision's margin is 0, d or 4d; an infinite margin is N's run, which the manifest substitutes"
                              "before setup (plan v2 2.3)", mg)
    return {"0": 0, "d": int(d), "4d": 4 * int(d)}[mg]


def _digest(steps) -> list:
    return [hashlib.sha1(repr((tuple(s.instrs), s.residual_key)).encode()).hexdigest()[:16] for s in steps]


def _shared_prefix(steps, dig) -> bool:
    """A live walk's steps and a static walk's digests agree as far as both go (U's `matches`, either walk the longer)."""
    n = min(len(steps), len(dig))
    return _digest(steps[:n]) == dig[:n]


def producer_mu(dev, v) -> float:
    """Rule 50a's mu(v) (froq/u6.py, `U6State.mu_class`): the mean full-decode service time of the producer's decode windows."""
    ws, _end = script_windows(SimpleNamespace(instrs=[dev.schedule_instructions[v.producer_idx].instruction]), int(dev.d_t), 0,
                              dev._u_mu)
    return float((sum(m for _a, m in ws) / len(ws)) if ws else dev._u_mu(2 * int(dev.d_t)))


def producer_vclass(dev, v) -> str:
    si = dev.schedule_instructions[v.producer_idx]
    return "%s/%d/%d" % (si.instruction.name, len(si.instruction.patches), int(dev._true_durations[v.producer_idx]))


def site_profiles(dev) -> dict:
    """{vid: profile} of every site with a static walk (section 6.1): per absolute lane 0.. 16 of the walk, placed,
    the script's duration S, its settlement XS, its demand N0, the commit's demand NC (U's `demand`: the early script,
    then its cheapest repair), clean (U's flag for the lazy walk: what the rebased frame holds, the lazy frame holds),
    the step durations, the steps' digests, and for each b where a commit can be built the rebased walk from b. Version
    2 adds each script's decoding work U, the sum of its windows' own mean services in core-rounds (R3), on the
    walk and on each rebased walk, and snci, the static walk's D-nci stop on its feasible lanes (R3'). The rebased walk
    at b keeps no boundary past absolute CAP, as the live frames (rule 66: the rebase's j + jj <= WALK_CAP; `rebased`
    keeps CAP - b lanes): built with max(1, CAP - b) steps it holds one at 17 when b = 16, so its prices are cut at CAP
    and its stop (clean), its cheapest repair (NC, Rres) read boundary 16 at most."""
    ut = UTables(dev, None, repair=True)
    d, path, mu = int(dev.d_t), int(dev.path), dev._u_mu
    sx = dev.sem.tables.library == "sx"                                # the lanes carry `form` (the sx library's choice), else no field
    out = {}
    for vid in sorted(dev.values):
        c = ut.walk(vid)
        if not c.has_site:
            ut.forget(vid)
            continue
        v = dev.values[vid]
        r = ut._prices(c)
        F = min(c.F, CAP)
        z = lambda: [0] * NP
        feas, S, XS, N0, NCd, clean, U0 = [False] * NP, z(), z(), z(), z(), [False] * NP, [0.0] * NP
        form = [None] * NP
        for j in range(F + 1):
            low = ut._boundary(c, j)
            if r[j] is None or low is None:
                continue
            feas[j] = True
            form[j] = low.form
            ws, end = script_windows(low, d, 0, mu)
            S[j], XS[j], N0[j] = int(low.duration), int(settle_rounds(ws, end, path)), int(peak_concurrency(ws))
            U0[j] = float(sum(m for _a, m in ws))
        top = max(j for j in range(F + 1) if feas[j]) if any(feas[:F + 1]) else None
        if top is None:
            ut.forget(vid)
            continue
        snci = dnci_stop([r[j] if feas[j] else None for j in range(F + 1)])[0]
        held_d = ut._held(c, vid, top)
        reb = {}
        for b in range(top + 1):
            if not feas[b]:
                continue
            c2 = build_contract(dev.es, dev.sem, dev.sched_view, dev._specs[vid], False, max(1, CAP - b), deferral=True, claimed=(),
                                rebase_at=b, state=ut._state0)
            if c2.rebase is None or c2.entry_recovery is None:
                continue
            r2 = [x if b + k <= CAP else None for k, x in enumerate(ut._prices(c2))]   # (none past CAP, rule 66)
            _s2, top2 = dnci_stop(r2)
            if top2 is None:
                continue
            clean[b] = bool(ut._held(c2, vid, top2, extra_bits=c2.held_bits) <= held_d)
            kb = min((k for k in range(top2 + 1) if r2[k] is not None), key=lambda k: (r2[k], k))
            low_b, rep = ut._boundary(c, b), ut._boundary(c2, kb)
            ws0, end0 = script_windows(low_b, d, 0, mu)
            ws2, _e2 = script_windows(rep, d, end0, mu)
            NCd[b] = int(peak_concurrency(ws0 + ws2))
            F1 = min(b + c2.F, CAP)
            f1, R, XR, N1, U1 = [False] * NP, z(), z(), z(), [0.0] * NP
            form1 = [None] * NP
            for p in range(b, F1 + 1):
                k = p - b
                lk = ut._boundary(c2, k) if k <= c2.F else None
                if k > len(r2) - 1 or r2[k] is None or lk is None:
                    continue
                f1[p] = True
                form1[p] = lk.form
                wk, ek = script_windows(lk, d, 0, mu)
                R[p], XR[p], N1[p] = int(lk.duration), int(settle_rounds(wk, ek, path)), int(peak_concurrency(wk))
                U1[p] = float(sum(m for _a, m in wk))
            reb[b] = dict(F1=F1, feas=f1, R=R, XR=XR, N=N1, d=[int(s.duration) for s in c2.steps[:F1 - b]],
                          Rres=int(rep.duration if rep is not None else 0), dig=_digest(c2.steps[:F1 - b]), U=U1,
                          **({"form": form1} if sx else {}))
        out[vid] = dict(vc=producer_vclass(dev, v), mu=producer_mu(dev, v), F=F, top=top, feas=feas, S=S, XS=XS, N0=N0, NC=NCd,
                        clean=clean, d=[int(s.duration) for s in c.steps[:F]], dig=_digest(c.steps[:F]),
                        reb={str(b): x for b, x in reb.items()}, U=U0, snci=snci, **({"form": form} if sx else {}))
        ut.forget(vid)
    return out


def profiles_path(dirname: str, bench: str, d: int, ident: str) -> str:
    return os.path.join(dirname, "w2profiles_%s_d%d_%s_v%d.json.gz" % (bench, int(d), ident[:16], PROFILE_VERSION))


def load_or_build_profiles(dev, dirname=None, bench=None, ident=None):
    """(profiles, provenance): from the cache file of this (program, d, inputs) if it exists, else computed (and written
    atomically when a directory is given)."""
    if dirname and ident:
        p = profiles_path(dirname, bench, int(dev.d_t), ident)
        if os.path.exists(p):
            doc = json.loads(gzip.decompress(open(p, "rb").read()))
            assert doc.get("version") == PROFILE_VERSION and doc.get("identity") == ident and doc.get("d") == int(dev.d_t), p
            return {int(k): x for k, x in doc["profiles"].items()}, dict(source="cache", file=p, identity=ident)
    prof = site_profiles(dev)
    prov = dict(source="computed", identity=ident)
    if dirname and ident:
        os.makedirs(dirname, exist_ok=True)
        p = profiles_path(dirname, bench, int(dev.d_t), ident)
        tmp = "%s.tmp%d" % (p, os.getpid())
        with open(tmp, "wb") as f:
            f.write(gzip.compress(json.dumps(dict(version=PROFILE_VERSION, identity=ident, bench=bench, d=int(dev.d_t),
                                                  profiles={str(k): x for k, x in prof.items()}), separators=(",", ":")).encode(), mtime=0))
        os.replace(tmp, p)
        prov["file"] = p
    return prof, prov


# (diagnostics, the results): the profile reads by use. The decision's prices are
# what the tables charge: each lane's script S, settlement XS, decoder demand N0 and work U, the commit's demand NC in
# its price g_C, R5's stop snci, and each rebased walk's R, XR, N and U.  The walk's structure (vc, mu, F, top, feas,
# clean, d, dig, each rebased walk's F1, feas, d and dig) and what the run itself reads are operational: the commit's
# headroom (`_commit_ok`, NC), the reservation's demand and end (`commit_args`, NC, S and Rres), the walks' digests.
PRICE_FIELDS, PRICE_FIELDS_REB = ("S", "XS", "N0", "U"), ("R", "XR", "N", "U")


def load_profile_file(path: str, d: int, bench=None, ident=None):
    """(profiles, provenance) from an explicit file (`--w2-profile-file`, `--w2-price-profile-file`) in place of the
    cache's key lookup.  The file's own identity is recorded beside the run's; they differ where the file is another
    library's (arm D's prices)."""
    with open(path, "rb") as f:
        raw = f.read()
    doc = json.loads(gzip.decompress(raw))
    assert doc.get("version") == PROFILE_VERSION and doc.get("d") == int(d), ("the decision: a profile file of another version or d", path)
    assert bench is None or doc.get("bench") in (None, bench), ("the decision: a profile file of another program", path, doc.get("bench"))
    prov = dict(source="file", file=path, sha256=hashlib.sha256(raw).hexdigest(), identity=doc.get("identity"), run_identity=ident,
                same_identity=bool(ident is not None and doc.get("identity") == ident))
    return {int(k): x for k, x in doc["profiles"].items()}, prov


def price_merge(ops: dict, price: dict):
    """(profiles, counts): arm D's profiles, prices only.  Each site's profile from `ops` (the scripts that run) with the
    price fields from `price`: PRICE_FIELDS on the lanes feasible in both, NC at the boundaries with a rebased walk in
    both, snci, and each rebased walk's PRICE_FIELDS_REB on its lanes feasible in both.  Where `price` has no such lane,
    walk or site, `ops`' value stays and is counted."""
    st = collections.Counter()
    out = {}
    for vid, po in ops.items():
        pp = price.get(vid)
        if pp is None:
            st["site:ops_only"] += 1
            out[vid] = po
            continue
        st["sites"] += 1
        if pp["vc"] != po["vc"] or pp["mu"] != po["mu"] or pp["feas"] != po["feas"] or pp["d"] != po["d"]:
            st["site:structure_differs"] += 1
        m = dict(po)
        for f in PRICE_FIELDS:
            if f not in po:
                continue
            a = list(po[f])
            for j in range(len(a)):
                if po["feas"][j]:
                    if pp["feas"][j] and f in pp:
                        st["lane:%s:%s" % (f, "same" if a[j] == pp[f][j] else "price")] += 1
                        a[j] = pp[f][j]
                    else:
                        st["lane:%s:ops_kept" % f] += 1
            m[f] = a
        nc = list(po["NC"])
        for bs in po["reb"]:
            b = int(bs)
            if bs in pp["reb"]:
                nc[b] = pp["NC"][b]
            else:
                st["NC:ops_kept"] += 1
        m["NC"] = nc
        if "snci" in pp:
            st["snci:%s" % ("same" if pp["snci"] == po.get("snci") else "price")] += 1
            m["snci"] = pp["snci"]
        reb = {}
        for bs, ro in po["reb"].items():
            rp = pp["reb"].get(bs)
            if rp is None:
                st["reb:ops_kept"] += 1
                reb[bs] = ro
                continue
            r2 = dict(ro)
            for f in PRICE_FIELDS_REB:
                if f not in ro:
                    continue
                a = list(ro[f])
                for q in range(len(a)):
                    if ro["feas"][q]:
                        if q < len(rp["feas"]) and rp["feas"][q] and f in rp:
                            a[q] = rp[f][q]
                        else:
                            st["reb_lane:%s:ops_kept" % f] += 1
                r2[f] = a
            reb[bs] = r2
        m["reb"] = reb
        out[vid] = m
    return out, dict(sorted(st.items()))


# ------------------------------------------------------------------------------------------------------------ the sweep
def _order(q: dict, legal: dict) -> str:
    """The preference order of the actions by their Q-values, ties (within TIE) to the rank A < W < C; an illegal action
    has Q = +inf and sorts last (section 6.3)."""
    rem = ["A", "W", "C"]
    val = {a: (q[a] if legal[a] else math.inf) for a in rem}
    out = []
    while rem:
        m = min(val[a] for a in rem)
        cand = [a for a in rem if (val[a] <= m + TIE if m < math.inf else val[a] == math.inf)]
        a = min(cand, key=RANK.get)
        out.append(a)
        rem.remove(a)
    return "".join(out)


def _sweep(model: LamModel, lanes: dict, X: np.ndarray, beta: int, greedy: bool, commit=None, bins_edges=None, cmask=None,
           q0=False, ca=0.0, sd=None):
    """One key half's backward sweep (sections 5.1-5.2, 6.3) over the CL = 18 (c-state, load bin) combinations at once.
    sd (start-delay note v2 2.2; the walk half only): dict(pi (CL), c) -- every ADVANCE Q (the stationary tail's,
    below the horizon, the jumps' in both) gains pi (1 - r_D) max(0, A(e) + c), A the rest of that advance (`sd_resid`,
    `sd_resid_tail`); None adds nothing (bit for bit as before).

    lanes: fl (the feasible lanes, ascending), top, nxt {p: next feasible}, D {p: block duration}, jumps {p: [(b', D')]};
    and with R5 (16.8) r5 = (s_nci, (CL) bool, the l0 columns): there a block that ends past s_nci, the step from p
    to nxt(p) or a jump to b', is illegal, Q = +inf, below and beyond the horizon
    X: (NP, CL) the wrong bet's cost at each lane, (1 - pi_beta) rho'_beta
    commit (beta = 0): {p: (gC (CL), X1 (CL), dC, Vr (H, CL), Vrinf (CL))} for the lanes where COMMIT is legal
    cmask: (CL) bool, the combinations where those lanes' COMMIT is legal (None: all of them). The c0 ban (the spec's
    amendment) clears c0's: there COMMIT's Q is +inf, an illegal action's, below and beyond the horizon
    Returns (Vrow (H, CL) and Vinf (CL) per lane as dict of the full V, the bin means {(j, p): {action: (CL)}}, jumps'
    bin means {(j, p, b'): (CL)}).
    q0 (R4, 16.7): model is an eta bin's law (`EtaLaw`), bins_edges is not read, and the means are each action's Q at
    e = 0, the decision, keyed as bin 0's: {(0, p): {action: (CL)}} and {(0, p, b'): (CL)}.
    ca: ADVANCE's added cost in rounds, the overhead price c_o plus the margin m, in every ADVANCE
    Q the sweep makes (the stationary tail's, below the horizon, and the jumps' in both), so V, the bin means and the codes
    carry it; 0 adds nothing (bit for bit as before)."""
    H, s, h = model.H, model.s, model.h
    fl, top, nxt, Dn, jumps = lanes["fl"], lanes["top"], lanes["nxt"], lanes["D"], lanes["jumps"]
    r5 = lanes.get("r5")                                                  # (R5, 16.8: (s_nci, the l0 columns), or None)
    noadv = bool(lanes.get("noadv"))                                      # (V2q-noA: ADVANCE priced at +inf)
    commit = commit or {}
    CL = X.shape[1]
    durs = [Dn[p] for p in fl if p != top] + [x[2] for x in commit.values()] + [dj for p in fl for _b, dj in jumps.get(p, ())] + [1]
    E = H + max(durs) + 2
    Sf = model.surv(E)
    Mres = model.mean_residual(E) if greedy else None
    inf = math.inf
    # the stationary tail (5.2): V_inf per lane, from the top down
    Vinf = np.zeros((NP, CL))
    Qinf = {}
    for p in sorted(fl, reverse=True):
        W = 1.0 / h + X[p]
        A = np.full(CL, inf)
        if p != top:
            n, dd = nxt[p], Dn[p]
            cont = (1.0 / h + X[n]) if greedy else Vinf[n]
            QA = (1.0 - s ** dd) * X[n] + s ** dd * cont
            if sd is not None:                                            # (pi (1 - r_D) W, the tail's A)
                QA = QA + (1.0 - s ** dd) * max(0.0, sd_resid_tail(s, dd) + sd["c"]) * sd["pi"]
            if ca:
                QA = QA + ca                                              # (ADVANCE's price c_o + m, 2.2)
            if r5 is not None and n > r5[0]:
                QA = np.where(r5[1], inf, QA)                             # (R5: past s_nci, illegal in l0)
            if noadv:
                QA = np.full(CL, inf)
            A = np.minimum(A, QA)
        else:
            QA = None
        QC = None
        if p in commit:
            gC, X1, dC, _Vr, Vrinf = commit[p]
            cont = (1.0 / h + X1) if greedy else Vrinf
            QC = gC + (1.0 - s ** dC) * X1 + s ** dC * cont
            if cmask is not None:
                QC = np.where(cmask, QC, inf)
            A = np.minimum(A, QC)
        Vinf[p] = np.minimum(A, W)
        QW = 1.0 + h * X[p] + s * ((1.0 / h + X[p]) if greedy else Vinf[p])
        Qinf[p] = dict(A=QA, W=QW, C=QC)
    for p in fl:
        for b2, dj in jumps.get(p, ()):
            cont = (1.0 / h + X[b2]) if greedy else Vinf[b2]
            Qinf[(p, b2)] = (1.0 - s ** dj) * X[b2] + s ** dj * cont
            if sd is not None:                                            # (the jump's rest, to b')
                Qinf[(p, b2)] = Qinf[(p, b2)] + (1.0 - s ** dj) * max(0.0, sd_resid_tail(s, dj) + sd["c"]) * sd["pi"]
            if ca:
                Qinf[(p, b2)] = Qinf[(p, b2)] + ca                        # (a jump is one ADVANCE, priced once)
            if r5 is not None and b2 > r5[0]:
                Qinf[(p, b2)] = np.where(r5[1], inf, Qinf[(p, b2)])
            if noadv:
                Qinf[(p, b2)] = np.full(CL, inf)
    # below the horizon (5.1): V[e] for e = H - 1 .. 0, every lane at once
    V = np.zeros((E, NP, CL))
    V[H:] = Vinf
    fla = np.array(fl, dtype=int)
    adv = np.array([p for p in fl if p != top], dtype=int)
    advn = np.array([nxt[p] for p in adv], dtype=int)
    advd = np.array([Dn[p] for p in adv], dtype=int)
    clan = sorted(commit)
    cdur = np.array([commit[p][2] for p in clan], dtype=int)
    cg = np.array([commit[p][0] for p in clan]) if clan else None
    cx1 = np.array([commit[p][1] for p in clan]) if clan else None
    if clan:
        Vr = np.zeros((len(clan), E, CL))
        for i, p in enumerate(clan):
            _g, _x, _d, vr, vrinf = commit[p]
            Vr[i, :H] = vr[:H]
            Vr[i, H:] = vrinf
    jl = [(p, b2, dj) for p in fl for b2, dj in jumps.get(p, ())]
    jp = np.array([x[0] for x in jl], dtype=int)
    jb = np.array([x[1] for x in jl], dtype=int)
    jd = np.array([x[2] for x in jl], dtype=int)
    if sd is not None:                                                    # (W(e) = max(0, A(e) + c) per advance, e < H)
        Wd = {D_: np.maximum(0.0, sd_resid(Sf, D_, H) + sd["c"]) for D_ in set(advd.tolist()) | set(jd.tolist())}
        WA = np.array([Wd[x] for x in advd.tolist()]) if len(adv) else None
        WJ = np.array([Wd[x] for x in jd.tolist()]) if jl else None
    if r5 is not None:                                                    # (R5: the l0 columns' blocks past s_nci)
        r5A, r5J = np.outer(advn > r5[0], r5[1]), np.outer(jb > r5[0], r5[1])
    ed = bins_edges if not q0 else (H,) * (NE - 1)
    bin_of = np.array([bisect.bisect_right(ed, e) for e in range(H)], dtype=int)
    SQ = {a: np.zeros((NE, NP, CL)) for a in "AWC"}
    SQJ = np.zeros((NE, max(1, len(jl)), CL))
    SS = np.zeros(NE)
    Xf = X[fla]
    for e in range(H - 1, -1, -1):
        se = Sf[e]
        j = bin_of[e]
        r1 = Sf[e + 1] / se
        contW = (Mres[e + 1] + Xf) if greedy else V[e + 1, fla]
        QW = 1.0 + (1.0 - r1) * Xf + r1 * contW
        best = QW.copy()
        SQ["W"][j, fla] += se * QW
        if len(adv):
            rD = (Sf[e + advd] / se)[:, None]
            contA = (Mres[e + advd][:, None] + X[advn]) if greedy else V[e + advd, advn]
            QA = (1.0 - rD) * X[advn] + rD * contA
            if sd is not None:
                QA = QA + (1.0 - rD) * WA[:, e][:, None] * sd["pi"]       # (pi (1 - r_D) W(n | e))
            if ca:
                QA = QA + ca                                              # (ADVANCE's price c_o + m)
            if r5 is not None:
                QA = np.where(r5A, inf, QA)
            if noadv:
                QA = np.full_like(QA, inf)
            idx = np.searchsorted(fla, adv)
            best[idx] = np.minimum(best[idx], QA)
            SQ["A"][j, adv] += se * QA
        if clan:
            rC = (Sf[e + cdur] / se)[:, None]
            contC = (Mres[e + cdur][:, None] + cx1) if greedy else Vr[np.arange(len(clan)), e + cdur]
            QC = cg + (1.0 - rC) * cx1 + rC * contC
            if cmask is not None:
                QC = np.where(cmask, QC, inf)
            idx = np.searchsorted(fla, np.array(clan))
            best[idx] = np.minimum(best[idx], QC)
            SQ["C"][j, np.array(clan)] += se * QC
        if jl:
            rJ = (Sf[e + jd] / se)[:, None]
            contJ = (Mres[e + jd][:, None] + X[jb]) if greedy else V[e + jd, jb]
            QJ = (1.0 - rJ) * X[jb] + rJ * contJ
            if sd is not None:
                QJ = QJ + (1.0 - rJ) * WJ[:, e][:, None] * sd["pi"]       # (the jump's pi (1 - r_D) W(b' | e))
            if ca:
                QJ = QJ + ca                                              # (the jump's ADVANCE, priced once)
            if r5 is not None:
                QJ = np.where(r5J, inf, QJ)
            if noadv:
                QJ = np.full_like(QJ, inf)
            SQJ[j] += se * QJ
        if q0 and e == 0:                                                  # (R4: each action's Q at the decision)
            Z = {a: np.zeros((NP, CL)) for a in "AWC"}
            Z["W"][fla] = QW
            if len(adv):
                Z["A"][adv] = QA
            if clan:
                Z["C"][np.array(clan)] = QC
            ZJ = QJ if jl else None
        V[e, fla] = best
        SS[j] += se
    if q0:
        return V[:H], Vinf, {(0, p): {a: Z[a][p] for a in "AWC"} for p in fl}, {(0, p, b2): ZJ[i] for i, (p, b2, _dj) in enumerate(jl)}
    means = {}
    jmeans = {}
    for j in range(NE):
        for p in fl:
            if j < NE - 1:
                if SS[j] <= 0:
                    raise AssertionError(("an empty age bin", model.key, j, ed))
                means[(j, p)] = {a: SQ[a][j, p] / SS[j] for a in "AWC"}
            else:
                qi = Qinf[p]
                means[(j, p)] = {a: qi[a] for a in "AWC"}
        for i, (p, b2, _dj) in enumerate(jl):
            jmeans[(j, p, b2)] = (SQJ[j, i] / SS[j]) if j < NE - 1 else Qinf[(p, b2)]
    return V[:H], Vinf, means, jmeans


def _codes(lanes: dict, means: dict, jmeans: dict, beta: int, commit_lanes, CL: int) -> np.ndarray:
    """The table half's codes (CL, NE, NP) from the bin means (section 6.3).  A lane that is not statically feasible is
    no decision point and is written ADVANCE first, code 0 (the scan passes it).  At a feasible lane an action that is
    not legal (ADVANCE at the half's last lane, COMMIT where it is not authorised, and at c0 under the c0 ban, whose bin
    means the sweep made +inf) has Q = +inf and sorts last.  On beta = 0 an ADVANCE-first order takes code 6 or 7 when
    some jump ranks after an alternative."""
    fl, top, jumps = set(lanes["fl"]), lanes["top"], lanes["jumps"]
    out = np.zeros((CL, NE, NP), dtype=np.int8)
    for j in range(NE):
        for p in range(NP):
            if p not in fl:
                continue
            m = means[(j, p)]
            legal = dict(A=(p != top), W=True, C=(beta == 0 and p in commit_lanes))
            for cl in range(CL):
                q = {a: (float(m[a][cl]) if m[a] is not None and legal[a] else math.inf) for a in "AWC"}
                o = _order(q, legal)
                if beta == 1:
                    out[cl, j, p] = 0 if o.index("A") < o.index("W") else 1
                    continue
                code = CODE0[o]
                if o[0] == "A" and jumps.get(p):
                    alt = min(q["W"], q["C"])
                    if any(float(jmeans[(j, p, b2)][cl]) > alt + TIE for b2, _dj in jumps[p]):
                        code += 6
                out[cl, j, p] = code
    return out


def _lanes(feas, top, durs, base: int = 0) -> dict:
    """Feasible lanes, next feasible lanes, block durations and jumps (section 6.3) of one walk on absolute lanes; durs[k]
    is the duration of the step from lane base + k."""
    fl = [p for p in range(NP) if feas[p] and p <= top]
    nxt, D, jumps = {}, {}, {}
    cum = {base: 0}
    for k, dk in enumerate(durs):
        cum[base + k + 1] = cum[base + k] + int(dk)
    for i, p in enumerate(fl):
        if p == top:
            continue
        n = fl[i + 1]
        nxt[p], D[p] = n, cum[n] - cum[p]
        js = [(b2, cum[b2] - cum[p]) for b2 in fl[i + 2:]]
        if js:
            jumps[p] = js
    return dict(fl=fl, top=top, nxt=nxt, D=D, jumps=jumps)


def kappa(opts: dict, mu: float, B) -> float:
    """kappa in rounds per missing core (section 4.3): c_kappa x mu_k; under the amendment's kscale "mu/B", c_kappa x
    mu_k / B, the expected wait for the next of B busy cores to free.  Without a decoder cap (B None) nothing is short."""
    k = float(opts["ckappa"]) * float(mu)
    return k / float(B) if (opts["kscale"] == "mu/B" and B is not None) else k


# ------------------------------------------------------------------------------------------------------------ the tables
class Tables:
    """The run's keys, slots and tables (section 6): built in the deferral phase of the program's first registration,
    before it (a pre-pass on the static walks: nothing in them depends on the run's own state)."""

    def __init__(self, profiles: dict, lam: W2Lambda, prior: dict, prior_frozen: bool, d: int, level: str, B, opts: dict,
                 settle: str, r2w=None, xic=None, vq=None, slots=None, hold=None, ca=None, sd=None):
        """R2's (W_0, W_1) in rounds (section 16.2), or None with R2 off; xic: xi_C in rounds (16.9), or None; vq: V2q's
        dict(NB, delta [l][b][k], m) (plan v3 3(a)), or None; slots: (walk, rebased) profile slots, SLOTS each by default
        (plan v2 5.2: rule 66's limits file sets them); hold: H, the realized hold in rounds that replaces every lane's
        XS, or None (XS); ca: ADVANCE's added cost in rounds, the overhead price plus the margin, in every sweep's ADVANCE (both halves, the jumps included), or None (0: as before)."""
        self.S0, self.S1 = (int(slots[0]), int(slots[1])) if slots is not None else (SLOTS, SLOTS)
        self.hold = None if hold is None else float(hold)
        self.opts = {k: v for k, v in opts.items() if k not in ("p1", "fixed", "co", "mg", "lf", "sd") and (k not in ("r4", "r5", "xic", "vq", "vqd0", "noadv", "nocom", "vqjn", "vqr") or v)}   # (R4, R5, xi_C off: as
        # before; P1, 16.10, a run's classification, never)
        setting = opts["setting"]
        self.zero = setting in ("aa", "awf")                    # rho = 0, kappa = 0, COMMIT not authorised (7)
        cA = self.ca = float(ca) if ca else 0.0                 # (plan v2 2.2-2.3: ADVANCE's c_o + m in rounds, V2*'s:
        assert math.isfinite(cA) and cA >= 0 and not (cA and (self.zero or vq is not None)), ("ADVANCE's price", ca, setting)   #  no exact case's, no V2q's)
        sk = dict(ca=cA) if cA else {}                          # (off: every sweep's call exactly as before)
        greedy = bool(opts["greedy"])
        r4 = bool(opts.get("r4", False))                                  # (R4, 16.7: the eta-indexed table)
        self.sd = None if sd is None else (float(sd[0]), float(sd[1]))    # (start-delay note v2 2.2: (c(d), M(d)), or None)
        assert self.sd is None or (settle == "decode" and not self.zero and vq is None and not r4), \
            ("the decision's start-delay correction: the decoded settlement, setting main, no V2q, no R4", settle, setting)
        assert not (r4 and opts.get("r5")), ("the decision: R5 with R4 is not defined (16.8)", opts)
        law_cache = {}

        def laws_of(key, model, ed):
            if key not in law_cache:                                       # (a walk key and its rebased keys share the
                law_cache[key] = eta_laws(model, ed)                       #  site's Lambda-key, so its laws)
                self.stats["r4:empty_bins"] += sum(x.empty for x in law_cache[key])
            return law_cache[key]
        horizon, bins = opts["horizon"], opts["bins"]
        decode = settle == "decode"
        NB = self.NB = 1 if vq is None else int(vq["NB"])                 # (V2q: the backlog levels, a column index)
        CL = NC * NL * NB                                                  # (column (c, l, b): (c NL + l) NB + b)
        cmask = np.array([c != 0 for c in range(NC) for _l in range(NL) for _b in range(NB)]) if opts["c0ban"] else None   # (the c0 ban)
        l0 = np.array([l == 0 for _c in range(NC) for l in range(NL) for _b in range(NB)])     # (R5, 16.8: the six l0 columns)
        noadv = bool(opts.get("noadv"))                                    # (V2q-noA, V2q-noC: the action priced at +inf)
        nocom = bool(opts.get("nocom"))
        self.vq = vq is not None
        if vq is not None:                                                 # (V2q: Delta(l, b, k) per column, k = 0 and 1)
            assert decode and not self.zero and not r4, ("V2q prices the decoded delivery: the hold, setting main, no R4", settle, setting)
            dl = [np.array([float(vq["delta"][l][b][k]) for _c in range(NC) for l in range(NL) for b in range(NB)]) for k in (0, 1)]
            mvq = float(vq["m"])
        jnn, self.vqr = vq is not None and bool(opts.get("vqjn")), vq is not None and bool(opts.get("vqr"))   # ((a) and (b))
        self.stats = collections.Counter()
        Freps = [0.0 if B is None else (None if f is None else f * float(B)) for f in L_REP]

        def sf(N, l):
            Fl = Freps[l]
            if B is None or Fl is None:
                return 0.0                                                # (no decoder cap: no shortfall)
            return max(0.0, float(N) - Fl)

        def pis(prior_p):
            return np.array([(prior_p if c == 0 else C_REP[c]) for c in range(NC) for _l in range(NL) for _b in range(NB)])

        def lks(l_of):
            return np.array([l_of(l) for _c in range(NC) for l in range(NL) for _b in range(NB)])

        # section 16: R2's prices, R3's work surrogate and R3''s scope; with each at its default nothing below changes
        lam3 = float(opts["lam"])
        r3 = lam3 > 0 and B is not None and not self.zero
        nci = opts["scope0"] == "nci"
        W0, W1 = (float(r2w[0]), float(r2w[1])) if r2w is not None else (None, None)
        r5 = bool(opts.get("r5", False)) and B is not None and not self.zero   # (R5, 16.8: not where no run reads l0)
        if r3 or nci or r5:
            need = (("U",) if r3 else ()) + (("snci",) if nci or r5 else ())
            bad = sorted(vid for vid, pr in profiles.items() if any(x not in pr for x in need))
            assert not bad, ("the decision: R3 or R3' needs version-2 profiles (U, snci); rebuild the cache", need, bad[:5])

        def work(N, U):
            """R3 (16.3): lambda x theta_l(sigma) x U(sigma) / B rounds per load bin, theta_l = sf_l(N) / N the share of
            the script's windows that find no free core in bin l, U the sum of its windows' own mean services."""
            return lks(lambda l: lam3 * (sf(N, l) / float(N) if N > 0 else 0.0) * float(U) / float(B))

        def rident(key, p1, mu, b, rb):
            return (key, p1, mu, b, rb["F1"], tuple(rb["feas"]), tuple(rb["d"]), tuple(rb["R"]),
                    tuple(rb["XR"]) if decode else (), tuple(rb["N"])) + ((tuple(rb["U"]),) if r3 else ())

        site = {}
        for vid, pr in sorted(profiles.items()):
            vc = pr["vc"]
            key = lam.key_of(d, level, vc)
            if lam.frozen and (d, level, vc) not in lam.keymap:            # a class the fit never saw
                self.stats["lambda_unseen_values"] += 1
                self.stats["lambda_unseen:%s:%s" % (vc, lam.unseen_key(d, level, vc)[1])] += 1
            p1 = prior.get((d, vc))
            if p1 is None:
                if prior_frozen:
                    from .u6 import prior_unseen
                    p1, how = prior_unseen(prior, d, vc)
                    if p1 is None:
                        raise AssertionError("the decision: the frozen prior has no entry for %r" % ((d, vc),))
                    self.stats["prior_unseen_values"] += 1
                    self.stats["prior_unseen:%s:%s" % (vc, how)] += 1
                else:
                    p1 = 0.5
            site[vid] = (key, float(p1), float(pr["mu"]))
        # --- rebased keys (beta = 1), first
        reb_ident = {}
        for vid, pr in sorted(profiles.items()):
            if self.zero or nocom:                                          # (V2q-noC: no rebased key, no COMMIT)
                break
            key, p1, mu = site[vid]
            for bs, rb in sorted(pr["reb"].items(), key=lambda kv: int(kv[0])):
                b = int(bs)
                if not (pr["feas"][b] and pr["clean"][b]):
                    continue
                reb_ident.setdefault(rident(key, p1, mu, b, rb), []).append((vid, b))
        reb_res = {}
        for ident, pairs in reb_ident.items():
            key, p1, mu, b, F1, f1, d1, R, XR, N1 = ident[:10]
            U1 = ident[10] if r3 else None
            model = lam.model(d, level, key, horizon)
            assert model.H <= AGE_SAT, ("the decision: a horizon past the 10-bit age register", d, level, key, horizon, model.H)
            top1 = max(p for p in range(NP) if f1[p])
            ln = _lanes(f1, top1, d1, base=b)
            if noadv:
                ln["noadv"] = True
            kap = kappa(opts, mu, B)
            X = np.zeros((NP, CL))
            pi = pis(p1)
            for p in ln["fl"]:
                rho = R[p] + (XR[p] if decode else 0)
                if W1 is not None:
                    rho = rho + W1                                        # (R2: rho_1's start-wait residual)
                load = kap * lks(lambda l: sf(N1[p], l))
                if r3:
                    load = load + work(N1[p], U1[p])                      # (R3: the repair's work, weighted below by 1 - pi)
                X[p] = (1.0 - pi) * (rho + load) if vq is None else (1.0 - pi) * (rho + dl[1] + load)   # (V2q: XR + Delta(l, b, 1))
            ed = model.edges(bins)
            if r4:                                                         # (R4, 16.7: a sweep per eta bin, entries at r = 0)
                sw = [_sweep(law, ln, X, 1, greedy, None, None, q0=True, **sk) for law in laws_of(key, model, ed)]
                means, jmeans = _r4_means(sw)
                codes = _codes(ln, means, jmeans, 1, (), CL)
                reb_res[ident] = dict(V=None, Vinf=None, V4=[(x[0][:, b, :].copy(), x[1][b].copy()) for x in sw], X1=X[b].copy(),
                                      codes=codes, edges=ed, pairs=pairs, fence=top1)
            else:
                V, Vinf, means, jmeans = _sweep(model, ln, X, 1, greedy, None, ed, **sk)
                codes = _codes(ln, means, jmeans, 1, (), CL)
                reb_res[ident] = dict(V=V[:, b, :].copy(), Vinf=Vinf[b].copy(), X1=X[b].copy(), codes=codes, edges=ed, pairs=pairs,
                                      fence=top1)
            self.stats["sweeps:rebased"] += 1
        # merge identical tables, allocate SLOTS by coverage
        self.reb_slot_of, reb_tab = {}, {}
        for ident, res in reb_res.items():
            tb = _canon(res["codes"], res["edges"], res["fence"])
            reb_tab.setdefault(tb, []).append(ident)
        order = sorted(reb_tab.items(), key=lambda kv: (-sum(len(reb_res[i]["pairs"]) for i in kv[1]),
                                                        min(min(reb_res[i]["pairs"]) for i in kv[1])))
        self.codes1 = np.zeros((self.S1, CL, NE, NP), dtype=np.int8)
        self.edges1 = np.zeros((self.S1, 7), dtype=np.int32)
        self.fence1 = np.zeros(self.S1, dtype=np.int32)
        reb_ok = {}
        for s_, (tb, idents) in enumerate(order):
            if s_ >= self.S1:
                for i in idents:
                    self.stats["commit_no_slot"] += len(reb_res[i]["pairs"])
                continue
            r0 = reb_res[idents[0]]
            self.codes1[s_], self.edges1[s_], self.fence1[s_] = r0["codes"], r0["edges"], r0["fence"]
            for i in idents:
                reb_ok[i] = s_
                for pair in reb_res[i]["pairs"]:
                    self.reb_slot_of[pair] = s_
        self.K1 = len(order)
        self.K1_keys = len(reb_res)
        # --- walk keys (beta = 0)
        walk_ident = {}
        self.reb_of = {}                                                  # (vid, b) -> its rebased ident (allocated)
        for vid, pr in sorted(profiles.items()):
            key, p1, mu = site[vid]
            com = []
            for b in range(NP):
                ident = None
                if not self.zero and b <= pr["top"] and pr["feas"][b] and pr["clean"][b] and str(b) in pr["reb"]:
                    ri = rident(key, p1, mu, b, pr["reb"][str(b)])
                    if ri in reb_ok:
                        ident = ri
                        self.reb_of[(vid, b)] = ri
                com.append(ident)
            S = (0,) * NP if self.zero else tuple(pr["S"])
            XS = (((self.hold,) * len(pr["XS"]) if self.hold is not None else tuple(pr["XS"]))   # (H in place of XS)
                  if (decode and not self.zero) else ())
            top = min(pr["top"], pr["snci"]) if nci else pr["top"]    # (R3': on beta = 0 the table stops at D-nci's stop)
            wi = (key, p1, mu, top, tuple(pr["feas"]), tuple(pr["d"]), S, XS, tuple(pr["N0"]), tuple(pr["NC"]), tuple(com)) + \
                ((tuple(pr["U"]),) if r3 else ()) + ((int(pr["snci"]),) if r5 else ())     # (R5: s_nci keys the entry)
            walk_ident.setdefault(wi, []).append(vid)
        walk_res = {}
        for wi, vids in walk_ident.items():
            key, p1, mu, top, feas, durs, S, XS, N0, NCd, com = wi[:11]
            U0 = wi[11] if r3 else None
            s5 = wi[-1] if r5 else None
            model = lam.model(d, level, key, horizon)
            assert model.H <= AGE_SAT, ("the decision: a horizon past the 10-bit age register", d, level, key, horizon, model.H)
            ln = _lanes(feas, top, durs, base=0)
            if noadv:
                ln["noadv"] = True
            if r5 and s5 < top:                                            # (R5, 16.8: in l0, no ADVANCE past s_nci)
                ln["r5"] = (s5, l0)
                self.stats["r5:keys"] += 1
            kap = 0.0 if self.zero else kappa(opts, mu, B)
            pi = pis(p1)
            X = np.zeros((NP, CL))
            for p in ln["fl"]:
                rho = S[p] + (XS[p] if XS else 0)
                if W0 is not None:
                    rho = rho + W0                                        # (R2: rho_0's start-wait residual)
                if self.sd is not None:
                    rho = S[p] + self.sd[1]                               # (note v2 2.2: S + M(d); no XS, no W_0, whose place
                                                                          #  the ADVANCE's start wait takes in the sweep)
                load = kap * lks(lambda l: sf(N0[p], l))
                if r3:
                    load = load + work(N0[p], U0[p])                      # (R3: the materialization's work, weighted by pi)
                X[p] = pi * (rho + load) if vq is None else pi * (rho + (dl[0] if p != ln["fl"][0] else 0.0) + load)   # (V2q: XS +
                # Delta(l, b, 0) on every lane an ADVANCE reaches; the registration lane's recovery is N's own S, no Delta)
            commit = {}
            for p in ln["fl"]:
                ri = com[p]
                if ri is None:
                    continue
                rr = reb_res[ri]
                dC = ELL + S[p] + (1 if W0 is not None else 0)             # (R2: the early script's measured +1 round. Plan-v3: no XS
                                                                           #  under the hold, where a correct COMMIT is released at the early
                                                                           #  script's end, before its results are decoded)
                gC = dC + kap * lks(lambda l: sf(NCd[p], l))              # (kappa: the commit's reservation demand N_C, as before)
                if r3:
                    gC = gC + work(N0[p], U0[p])                          # (R3: the early script's work, the same script, for certain)
                if xic is not None:
                    gC = gC + xic                                         # (16.9: xi_C, a price of d_C, not its duration)
                commit[p] = (gC, rr["X1"], dC, rr["V"], rr["Vinf"])
            ed = model.edges(bins)
            if r4:                                                         # (R4: COMMIT reads the rebased key's V in the same bin)
                assert all(com[p][0] == key for p in commit), ("R4: a commit's rebased key has the site's Lambda-key", key)
                sw = [_sweep(law, ln, X, 0, greedy, {p: c[:3] + reb_res[com[p]]["V4"][jj] for p, c in commit.items()}, None, cmask,
                             q0=True, **sk) for jj, law in enumerate(laws_of(key, model, ed))]
                means, jmeans = _r4_means(sw)
            else:
                _V, _Vi, means, jmeans = _sweep(model, ln, X, 0, greedy, commit, ed, cmask, **sk,
                                                **({} if self.sd is None else dict(sd=dict(pi=pi, c=self.sd[0]))))
            codes = _codes(ln, means, jmeans, 0, set(commit), CL)
            walk_res[wi] = dict(codes=codes, edges=ed, vids=vids, fence=top)
            if vq is not None:                                             # (V2q's admission bit: V(0 | c0, l, b) + m < J_N(l, b))
                fl = ln["fl"]
                f0 = fl[0]
                XN = X
                if jnn:                                                    # ((a): J_N at N's own cost. N runs its correction S
                    XN = X.copy()                                          #  after the verdict, at f0 with its decoding load, and nothing
                    lN = kap * lks(lambda l: sf(N0[f0], l))                #  waits for the correction's decode: no XS, no R2's W_0)
                    if r3:
                        lN = lN + work(N0[f0], U0[f0])
                    XN[f0] = pi * (S[f0] + lN)
                    self.stats["vq:jn_terminal_cut"] += int(np.any(XN[f0] != X[f0]))
                VN = _sweep(model, _lanes(feas, f0, durs, base=0), XN, 0, greedy, None, ed, cmask)[0]   # (J_N: WAIT only)
                v0, jn = _V[0, f0, :NL * NB].reshape(NL, NB), VN[0, f0, :NL * NB].reshape(NL, NB)       # (c0's columns)
                if self.vqr:                                               # ((b): V(0 | c0, l, b, r), the walk capped at the r-th
                    v0r = np.empty((NL, NB, VQ_RCAP + 1))                  #  certified boundary past f0; the last bin's at VQ_RCAP)
                    for rr in range(VQ_RCAP + 1):
                        if rr >= len(fl) - 1:                              # (the walk has no more boundaries: the full walk)
                            v0r[:, :, rr] = v0
                            continue
                        lr = _lanes(feas, fl[rr], durs, base=0)
                        if noadv:
                            lr["noadv"] = True
                        if r5 and s5 < fl[rr]:
                            lr["r5"] = (s5, l0)
                        Vr = _sweep(model, lr, X, 0, greedy, {p: x for p, x in commit.items() if p <= fl[rr]}, ed, cmask)[0]
                        v0r[:, :, rr] = Vr[0, f0, :NL * NB].reshape(NL, NB)
                        self.stats["vq:sweeps_capped"] += 1
                    bits = v0r + mvq < jn[:, :, None]
                    bits[:, :, 0] = False                                  # (zero reach is rejected)
                    walk_res[wi]["adm"] = (v0r, jn, bits)
                else:
                    walk_res[wi]["adm"] = (v0, jn, v0 + mvq < jn)
                self.stats["vq:sweeps_wait_only"] += 1
            self.stats["sweeps:walk"] += 1
        walk_tab = {}
        for wi, res in walk_res.items():
            walk_tab.setdefault(_canon(res["codes"], res["edges"], res["fence"]) + (res["adm"][2].tobytes() if vq is not None else b""),
                                []).append(wi)
        order = sorted(walk_tab.items(), key=lambda kv: (-sum(len(walk_res[i]["vids"]) for i in kv[1]),
                                                         min(min(walk_res[i]["vids"]) for i in kv[1])))
        self.codes0 = np.zeros((self.S0, CL, NE, NP), dtype=np.int8)
        self.edges0 = np.zeros((self.S0, 7), dtype=np.int32)
        self.fence0 = np.zeros(self.S0, dtype=np.int32)
        self.walk_slot_of = {}
        self.adm0 = np.zeros((self.S0, NL, NB) + ((VQ_RCAP + 1,) if self.vqr else ()), dtype=bool)   # (V2q: the admission bits of each
        # walk slot; (b): by the reach index too)
        self.adm_v = {}                                                    # (V2q, records: each site's (V(0), J_N) by (l, b))
        for s_, (tb, idents) in enumerate(order):
            if s_ >= self.S0:
                for i in idents:
                    self.stats["sites_no_slot"] += len(walk_res[i]["vids"])
                continue
            r0 = walk_res[idents[0]]
            self.codes0[s_], self.edges0[s_], self.fence0[s_] = r0["codes"], r0["edges"], r0["fence"]
            if vq is not None:
                self.adm0[s_] = r0["adm"][2]
            for i in idents:
                for vid in walk_res[i]["vids"]:
                    self.walk_slot_of[vid] = s_
                    if vq is not None:
                        self.adm_v[vid] = walk_res[i]["adm"][:2]
        self.K0 = len(order)
        self.K0_keys = len(walk_res)
        self.n_sites = len(profiles)
        if self.zero:                                                      # section 7: every legal ADVANCE first, code 0
            bad = 0
            for wi, res in walk_res.items():
                top_ = wi[3]
                legal_a = [p for p in range(NP) if p != top_]                  # (the last lane's ADVANCE is not legal: WAC)
                bad += int(np.count_nonzero(res["codes"][:, :, legal_a]))
                bad += int(np.count_nonzero(res["codes"][:, :, top_] != CODE0["WAC"]))
            assert bad == 0, ("the decision's exact-case tables: an entry that does not put a legal ADVANCE first", bad)
        self.profiles = profiles
        self.site = site
        top = max(int(self.edges0.max(initial=0)), int(self.edges1.max(initial=0)))
        assert top <= AGE_SAT, ("the decision: an age-bin edge past the 10-bit age register (1,023)", top)

    def summary(self) -> dict:
        return dict(sites=self.n_sites, K0=self.K0, K0_keys=self.K0_keys, K1=self.K1, K1_keys=self.K1_keys,
                    shared_walk_keys=self.K0_keys - self.K0, shared_rebased_keys=self.K1_keys - self.K1,
                    sites_no_slot=int(self.stats["sites_no_slot"]), commit_no_slot=int(self.stats["commit_no_slot"]),
                    walk_sites=len(self.walk_slot_of), commit_pairs=len(self.reb_slot_of),
                    stats={k: int(v) for k, v in sorted(self.stats.items())}, **({"ca": self.ca} if self.ca else {}),
                    **({"sd": dict(c=self.sd[0], M=self.sd[1])} if getattr(self, "sd", None) is not None else {}),
                    **({"NB": self.NB, "admit_bits": int(self.adm0[:self.K0].sum()), "admit_bits_of": int(min(self.K0, self.S0) * NL * self.NB * (VQ_RCAP + 1 if self.vqr else 1)),
                        **({"reach_bins": VQ_RCAP + 1} if self.vqr else {})}
                       if self.vq else {}))


    def image(self) -> dict:
        """The ROW/SLOT image the RTL loads (`hw/rtl/decision_unit.sv`, `hw/golden/w2_table.py`): rows[r], r = 8(18k + 3c + l) + j,
        holds the 17 lanes' 4-bit fields, lane p at bits [4p + 3:4p], the beta = 0 code in bits [2:0] and the beta = 1
        bit in bit 3; slots[k] = [(edges, fence) of the beta = 0 half, (edges, fence) of the beta = 1 half]."""
        assert self.NB == 1, "the backlog-indexed image (V2q) is section 3(f)'s, not yet the RTL's"
        assert self.S0 == self.S1, ("the image pairs the two halves slot by slot", self.S0, self.S1)
        f = self.codes0.astype(np.int64) | (self.codes1.astype(np.int64) << 3)          # (SLOTS, C*L, E, P)
        rows = [0] * (self.S0 * NC * NL * NE)
        for k in range(self.S0):
            for cl in range(NC * NL):
                for j in range(NE):
                    val = 0
                    for p_ in range(NP):
                        val |= int(f[k, cl, j, p_]) << (4 * p_)
                    rows[8 * (18 * k + cl) + j] = val
        slots = [[([int(x) for x in self.edges0[k]], int(self.fence0[k])), ([int(x) for x in self.edges1[k]], int(self.fence1[k]))]
                 for k in range(self.S0)]
        return dict(rows=rows, slots=slots)


def hold_value(path: str, bench: str, d: int, level: str) -> dict:
    """(plan v2 5.1): H of (program, d, budget level) from its file, {"H": {"<program>:<d>:<level>": {"H": rounds or None,
    "how": "own" | "pooled" | "xs", "n": frames}}, "frozen": bool}. "xs" (fewer than 30 contradicted frames at the cell in
    all) keeps XS: value None. The file must hold the run's cell."""
    import hashlib
    raw = open(path, "rb").read()
    doc = json.loads(raw)
    key = "%s:%d:%s" % (bench, int(d), level)
    e = (doc.get("H") or {}).get(key)
    assert e is not None, ("the H variant: the H file has no entry for the run's cell", path, key)
    how = e.get("how")
    assert how in ("own", "pooled", "xs") and (how == "xs") == (e.get("H") is None), ("the H variant: a malformed entry", key, e)
    return dict(key=key, value=(None if e.get("H") is None else float(e["H"])), how=how, n=e.get("n"),
                sha256=hashlib.sha256(raw).hexdigest(), frozen=bool(doc.get("frozen")))


def _canon(codes: np.ndarray, edges, fence: int) -> bytes:
    """A half's identity for the merge (section 6.1): its rows and every per-slot field, the age-bin edges and the fence;
    two halves share a slot only when all agree."""
    return codes.tobytes() + repr((tuple(int(x) for x in edges), int(fence))).encode()


# ------------------------------------------------------------------------------------------------------------ run time
class W2State:
    """the decision's state for one run (see the module docstring)."""
    age_max, _censored = 0, False                                         # (the 16-bit age; the values censored at the cut)
    r1, r2, r2w = 0, None, None                                           # (section 16: R1's horizon and R2's fit, off)

    r4, _eta = False, None                    # (R4's defaults, 16.7: off, for a state made without __init__ or restored)
    r5 = False                                # (R5's, 16.8)
    p1 = False                                # (P1's, 16.10)
    fixed = None                              # (the fixed advance's, 16.11)
    cof = cov = None                          # (overhead price and margin, plan v2 2.2-2.3: off)
    coa = None                                # (commit on arrival, plan 3.2: off)
    sdf = sdv = None                          # (start-delay correction, note v2 2.2: off)

    vqf = vqm = None                                                      # (round 7's V2q and records: off in a W2State the tests
    rec7, backlog_q, blevel, _shadow, vqr, r7d, _shadow_c = False, 0, 0, {}, False, False, {}                    #  build without __init__)

    def __init__(self, dev, opts: dict, lambda_path=None, prior_path=None, budget_level=None, records=False, profiles_dir=None,
                 profile_ident=None, bench=None, r2_path=None, xic_path=None, vq_path=None, records7=False, hold_path=None,
                 profile_file=None, price_profile_file=None, diag=None):
        self.hold_path, self.holdv = hold_path, None                      # (plan v2 5.1: H's file, off by default)
        self.profile_file, self.price_profile_file = profile_file, price_profile_file   # (explicit files; None: as before)
        self.diag = diag                                                  # (the device's DiagRecords, or None)
        self._prof_ops = None                                             # (the profiles the run executes; tab.profiles unless
                                                                          #  a price file is given)
        o = dict(W2_DEFAULTS)
        o.update(opts or {})
        unknown = set(o) - set(W2_DEFAULTS)
        assert not unknown, ("the decision: unknown options", sorted(unknown))
        assert o["setting"] in SETTINGS and o["bins"] in ("u", "q") and o["horizon"] in ("a", "b"), o
        assert float(o["ckappa"]) > 0 and o["c0ban"] in (False, True) and o["kscale"] in ("mu", "mu/B"), o
        assert isinstance(o["r1"], int) and o["r1"] >= 0 and o["r2"] in (False, True) and float(o["lam"]) >= 0, o
        assert o["scope0"] in ("lazy", "nci"), o
        assert o["r4"] in (False, True) and o["r5"] in (False, True) and not (o["r4"] and o["r5"]), o
        assert o["xic"] is None or o["xic"] in XIC_MODES, o
        assert o["p1"] in (False, True), o
        assert o["fixed"] is None or o["fixed"] in FIXED_RULES, o
        vq = o["vq"]                                                      # (round 7's V2q: its margin, plan v3 3(a))
        assert vq is None or vq == "inf" or vq in VQ_MARGINS or (type(vq) in (int, float) and math.isfinite(vq) and vq >= 0), o
        assert all(o[k] in (False, True) for k in ("vqd0", "noadv", "nocom")), o
        assert vq != "inf" or not (o["vqd0"] or o["noadv"] or o["nocom"]), ("the removal settings need an arm that registers", o)
        assert vq is not None or not o["vqd0"], ("V2q-D0 needs V2q's margin (noA and noC also on V2*-Sd itself)", o)
        assert o["vqjn"] in (False, True) and o["vqr"] in (False, True) and (vq is not None or not (o["vqjn"] or o["vqr"])), \
            ("(a) and (b) are V2q's", o)
        assert o["xic"] != "program" or bench is not None, ("the decision: xi_C's program mode needs the program", o)
        assert o["co"] is None or (isinstance(o["co"], str) and o["co"] and bench is not None), ("the price's file and program", o)
        margin_rounds(o["mg"], 0)                                         # (2.3: 0, d or 4d; infinite is N's run)
        assert o["lf"] is None or (type(o["lf"]) is int and o["lf"] >= 1), ("the live-frame limit k is an integer >= 1", o)
        assert (o["co"] is None and o["mg"] == "0") or vq is None, ("the price and the margin are V2*'s", o)   # (fixed rules allowed with the price and margin: the comparison of Section 7.2)
        assert o["setting"] == "main" or all(o[k] == W2_DEFAULTS[k] for k in REPAIR if k != "lf"), ("section 16's options are the decision's own", o)
        assert o["coa"] is None or (type(o["coa"]) in (int, float) and math.isfinite(o["coa"]) and o["coa"] > 0 and o["r1"] == 0
                                    and o["fixed"] is None and vq is None and not o["nocom"]), ("COA: theta > 0 on V2*'s decision", o)
        # (code check, MED: lf, the live-frame limit, is the device's gate and no option of the controller's, so any
        #  setting may carry it -- `aa` and `awf` too, which legacy SER runs; it stays in REPAIR for the ledger, 2120)
        self.dev, self.opts = dev, o
        self.r1 = int(o["r1"])                                            # (16.1: R1's admission horizon; 0 = off)
        self.r4, self._eta = bool(o["r4"]), None                          # (16.7: R4, and its eta reader, made on first use)
        self.r5 = bool(o["r5"])                                           # (16.8: R5, which only the tables apply)
        self.p1 = bool(o["p1"])                                           # (16.10: P1, which only the tick applies)
        self.fixed = o["fixed"]                                           # (16.11: the fixed advance rule, which only decide applies)
        self.coa = None if o["coa"] is None else float(o["coa"])         # (COA: its theta, which only decide applies)
        self.r2 = W2R2(r2_path) if o["r2"] else None                      # (16.2: R2's fit)
        self.r2w = None
        self.xi = W2Xi(xic_path) if o["xic"] is not None else None       # (16.9: xi_C's fit)
        self.xiv = None
        self.cof = W2Co(o["co"]) if o["co"] is not None else None        # (plan v2 2.2: the overhead price's file)
        self.cov = None
        assert o["sd"] is None or (o["setting"] == "main" and vq is None and not o["r4"]), \
            ("--w2-sd: the decision's setting main, without V2q or R4 (start-delay note v2 2.2)", o)
        self.sdf = W2Sd(o["sd"]) if o["sd"] is not None else None        # (the start-delay file, c(d) and M(d))
        self.sdv = None
        self._r1_seen, self._r1_refused = set(), set()                    # (16.1's delay and refusal counts: (vid, round) once)
        self._r1_reg_seen = set()                                         # (16.1's registration-delay count: (vid, round) once)
        self.setting = o["setting"]
        self.noreg = self.setting == "wfd" or vq == "inf"               # (V2q at m = inf registers nothing: wfd's path)
        self.vqf = W2Vq(vq_path) if vq not in (None, "inf") else None    # (V2q's fit: the edges, Delta and the margins)
        self.vqm = self.vqf.margin(vq) if self.vqf is not None else None
        self.rec7 = bool(records7)                                        # (round 7's records-only counters, --r7-records)
        self.backlog_q, self.blevel, self._qhist, self._vq_seen, self._shadow = 0, 0, [], {}, {}
        self.vqr = bool(o["vqr"])                                         # ((b): the admission reads the readiness query's r)
        self.r7d = bool(o["vqjn"] or o["vqr"])                            # (admission: its records-only later-COMMIT shadow)
        self._shadow_c = {}
        self.c0 = self.setting in ("aa", "awf")
        self.level = budget_level
        self.records = bool(records)
        self.profiles_dir, self.ident, self.bench = profiles_dir, profile_ident, bench
        self.lambda_path, self.prior_path = lambda_path, prior_path
        self.tab = None
        self.stats = collections.Counter()
        self.rec = collections.defaultdict(list)
        self.alpha = 1.0 - math.exp(-1.0 / float(dev.d_t))
        self.busy_ewma = None
        self.load_bin = 2                                                 # (round t reads the bin left by round t - 1)
        self.resv: dict = {}                                              # rid -> reservation (a commit's decoder cores)
        self.resv_n = 0
        self.prov = {}
        self.B = None
        self.free_cores = None                                            # F (section 4.3), as of the last tick
        self._lk_round, self._lk_n, self.lk_max = None, 0, 0              # spec 9: the lookups of the current round
        self.last = None                                                  # the last lookup: its inputs and outputs
        self.lam = self.prior = None
        if not self.noreg:
            assert budget_level is not None, "the decision keys Lambda by the run's budget level (--budget-level)"
            self.lam = W2Lambda(lambda_path)
            from .u6 import prior_table
            self.prior, self.prior_prov, self.prior_sha = prior_table(prior_path)
            self.prior_frozen = bool(self.prior_prov.get("frozen"))

    @property
    def prof_ops(self) -> dict:
        """the profiles the run executes (its operational reads); the tables' own unless a price file is given."""
        p = getattr(self, "_prof_ops", None)
        return p if p is not None else self.tab.profiles

    # -- the tables, before the first registration
    def ensure_built(self):
        if self.tab is not None or self.noreg:
            return
        dev = self.dev
        dev.__dict__.setdefault("_u_mu_cache", {})
        dev.__dict__.setdefault("_u_mu_w", {})
        if getattr(self, "profile_file", None):                           # (an explicit file, not the key lookup)
            prof, self.prov = load_profile_file(self.profile_file, int(dev.d_t), self.bench, self.ident)
        else:
            prof, self.prov = load_or_build_profiles(dev, self.profiles_dir, self.bench, self.ident)
        self._prof_ops = prof
        if getattr(self, "price_profile_file", None):                     # (arm D: prices from this file, the rest the run's)
            pp, pprov = load_profile_file(self.price_profile_file, int(dev.d_t), self.bench, self.ident)
            prof, mst = price_merge(self.prof_ops, pp)
            self.prov = dict(self.prov, prices=dict(pprov, merge=mst))
        dec = _link().get("decoder")
        B = dev._u_cores(dec) if dec is not None else None
        self.B = B
        if self.hold_path is not None:                                    # (plan v2 5.1: H of the run's program and cell)
            self.holdv = hold_value(self.hold_path, self.bench, int(dev.d_t), self.level)
        slots = None                                                      # (rule 66's file: 64 walk keys and 64 rebased keys;
        if dev._hw is not None:                                           #  plan v2 5.2: the file's counts, a setting)
            slots = (dev._hw.cap_walk_keys, dev._hw.cap_rebased_keys)
        if self.r2 is not None:                                           # (16.2: W_0 and W_1 of the run's d and budget level)
            (w0, h0), (w1, h1) = self.r2.value(int(dev.d_t), self.level, 0), self.r2.value(int(dev.d_t), self.level, 1)
            self.r2w = dict(W0=w0, W1=w1, how0=h0, how1=h1)
        if self.xi is not None:                                           # (16.9: xi_C of the run's program, d and budget level)
            v, how, raw = self.xi.value(self.opts["xic"], self.bench, int(dev.d_t), self.level)
            self.xiv = dict(mode=self.opts["xic"], value=v, how=how, raw=raw, truncated=raw < 0)
        if self.cof is not None or self.opts["mg"] != "0":                # (plan v2 2.2-2.3: ADVANCE's c_o + m, static per
            d_ = int(dev.d_t)                                             #  program, d and budget level)
            c = self.cof.value(self.bench, d_, self.level) if self.cof is not None else dict(co=0.0)
            m = margin_rounds(self.opts["mg"], d_)
            self.cov = dict(c, margin=self.opts["mg"], m=m, ca=c["co"] + m)
        if self.sdf is not None and self.sdf.value(int(dev.d_t)) is not None:   # (note v2 2.2: c and M of the run's d;
            c_, m_ = self.sdf.value(int(dev.d_t))                         #  off at d by a version 2 file, the current prices)
            self.sdv = dict(c=c_, M=m_)
            if self.diag is not None:
                self.diag.repair_extra = list(SD_FIELDS)                  # (the diag row's added fields)
        self.tab = Tables(prof, self.lam, self.prior, self.prior_frozen, int(dev.d_t), self.level, B, self.opts, dev.defer_cfg["settle"],
                          r2w=(None if self.r2w is None else (self.r2w["W0"], self.r2w["W1"])),
                          vq=(None if self.vqf is None else dict(NB=self.vqf.NB, m=self.vqm,
                                                                    delta=(0.0 * self.vqf.delta if self.opts["vqd0"] else self.vqf.delta))),
                          **({} if self.xiv is None else dict(xic=self.xiv["value"])), slots=slots,
                          **({} if self.holdv is None or self.holdv["value"] is None else dict(hold=self.holdv["value"])),
                          **({} if self.cov is None else dict(ca=self.cov["ca"])),
                          **({} if self.sdv is None else dict(sd=(self.sdv["c"], self.sdv["M"]))))
        if self.lam.frozen:
            missing = sorted({(int(dev.d_t), self.level, x["vc"]) for x in prof.values()
                              if self.lam.key_of(int(dev.d_t), self.level, x["vc"]) is None})
            assert not missing, ("the decision: the frozen Lambda lacks keys this run needs", missing[:8])

    # -- the c-state and the load bin (sections 3, 4.3)
    def cstate(self, v, fr) -> int:
        if self.c0:
            return 0
        p = self._pred(v, fr)
        return 0 if p is None else 1 + bisect.bisect_right(C_EDGES, p[1])

    def _pred(self, v, fr):
        """The prediction in use for the frame's value now, as (m, p1) with p1 its probability of outcome 1, or None (the
        c-state c0: none published, or not yet in use)."""
        dev = self.dev
        pr, m, c, rc = dev._dview(v)
        if pr < 0 or m is None:
            return None
        from .device import H_INSTALL
        t0 = fr.reg_round if pr <= fr.reg_round else pr + H_INSTALL
        if dev.current_round < t0:
            return None
        if rc is not None and rc >= 0 and dev.current_round < rc + H_INSTALL:
            c = dev._rc_prev_c.get(v.vid, c)                               # (a revision applies from one round after it)
        return m, (float(c) if m else 1.0 - float(c))

    def tick(self, dec, started):
        """Once a round, after the round's full decodes have started (the decoder's hook): busy_ewma, the reservations'
        started windows, and the load bin the next round reads (the one-round lag, section 4.3)."""
        dev = self.dev
        now = dec._current_round
        pool = dev._pool
        busy = len(dec._active_window_progress) + (pool.busy(now) if pool is not None and not pool.preemptive else 0)
        if self.busy_ewma is None:
            self.busy_ewma = float(busy)
        else:
            self.busy_ewma += self.alpha * (busy - self.busy_ewma)
        if started and self.resv:
            byvid = {r["vid"]: r for r in self.resv.values()}
            for t in started:
                task = dec._get_task_or_none(t)
                if task is None:
                    continue
                for vid in {dev._recovery_owner.get(i) for i in task.window.parent_instr_idx}:
                    r = byvid.get(vid)
                    if r is not None:
                        r["started"] += 1
        for rid in [k for k, r in self.resv.items() if r["end"] < now and not r["pending"]]:
            self.release(rid, "duration")                                 # (a commit not yet spliced keeps its demand)
        if self.r1:                                                       # (16.1's validation: the booked cores the pool withholds)
            self.stats["r1:resv_pending_core_rounds"] += self.counted_pending(now)
        B = self.B
        if not B or self.tab is None:
            self.load_bin = 2
            return
        unstarted = sum(max(0, r["demand"] - r["started"]) for r in self.resv.values() if now >= r["from"])
        self.free_cores = B - self.busy_ewma - unstarted
        f = self.free_cores / float(B)
        if self.p1:                                                       # (P1, 16.10: l0 when B ready windows wait)
            q = dev._runnable_pending(dec, int(B) if math.isfinite(B) else 0)
            self.stats["p1:backlog"] += int(q >= B)
            self.stats["p1:freecore_l0"] += int(f < L_EDGES[0])
            self.load_bin = 0 if q >= B else (1 if f < L_EDGES[1] else 2)
        else:
            self.load_bin = 0 if f < L_EDGES[0] else (1 if f < L_EDGES[1] else 2)
        if self.vqf is not None or self.rec7:                             # (V2q's backlog level b, read with the load bin, and the
            q = dev._runnable_pending(dec, VQ_QCAP if self.rec7 else (self.vqf.edges[-1] + 1 if self.vqf.edges else 1))  # records')
            if self.rec7 and q != self.backlog_q:
                self._qhist.append((now, q))                              # (records: the count's changes, for b at completion)
            self.backlog_q = q
            if self.vqf is not None:
                self.blevel = self.vqf.level(q)
                self.stats["vq:b%d" % self.blevel] += 1
            if self._shadow or self._shadow_c:
                self._shadow_tick(now)
        self.stats["load_bin:%d" % self.load_bin] += 1

    def counted(self, now) -> int:
        """The cores the pool counts for the commits' reservations now (from each commit's effect round)."""
        return sum(r["demand"] for r in self.resv.values() if now >= r["from"])

    def counted_pending(self, now) -> int:
        """The part of `counted` that belongs to commits not yet spliced (R1's bookings): cores withheld, none used."""
        return sum(r["demand"] for r in self.resv.values() if r["pending"] and now >= r["from"])

    def strip_blocked(self, now, idle: int, pool):
        """16.1's validation, before the round's strip starts: the strip windows ready to start that found no core only
        because of the bookings' counted cores (an upper estimate: the windows' own start conditions are not rechecked)."""
        bk = self.counted_pending(now)
        if bk:
            n = pool.ready_count()
            self.stats["r1:strip_blocked"] += max(0, min(n, max(0, idle + bk)) - min(n, max(0, idle)))

    def admit_blocked(self, fr):
        """16.1's validation: the frame cannot admit its next block this round (the steps up to its next feasible boundary
        within its limit, which `_defer_admit` admits at once).  The frame-round counts, once, if the bookings' own holds
        stop it: with them lifted the block would be admissible.  A booking's own holds are its prepared rebased
        contract's (`h_next.new_holds`, less the frame's fence and unadmitted members): F_5's rule would not hold them in
        that round, while it would hold the booked frame's own steps from p on, so those are not lifted."""
        dev = self.dev
        key = (fr.vid, dev.current_round)
        if key in self._r1_seen:
            return
        lift = {}
        for r in self.resv.values():
            f2 = dev._frames.get(r["vid"]) if r["pending"] and r["vid"] != fr.vid else None
            if f2 is not None and f2.h_next is not None:
                own = f2.fence | f2.unadm
                for i in f2.h_next.new_holds:
                    if i not in own:
                        lift.setdefault(i, set()).add(f2.vid)
        if not lift:
            return
        j, virt = fr.admitted, set()
        while j < fr.limit and dev._defer_admissible(fr, j, virt, lift):
            virt |= fr.members[j]
            j += 1
            if fr.feas[j]:
                self._r1_seen.add(key)
                self.stats["r1:admit_blocked"] += 1
                return

    def _booking_adds(self):
        """The pending bookings' additions (16.1): their added holds {instruction: booked frames} (each prepared rebased
        contract's, less the frame's own fence and unadmitted members), and the extra claims and extra members
        {instruction: booked frame} that each booking still owns."""
        dev = self.dev
        holds, claims, members = {}, set(), {}
        for r in self.resv.values():
            f2 = dev._frames.get(r["vid"]) if r["pending"] else None
            nxt = f2.h_next if f2 is not None else None
            if nxt is None:
                continue
            own = f2.fence | f2.unadm
            for i in nxt.new_holds:
                if i not in own:
                    holds.setdefault(i, set()).add(f2.vid)
            claims.update(e for e in nxt.extra_claims if dev._defer_claim.get(e) == f2.vid)
            members.update((i, f2.vid) for i in nxt.extra_members if dev._defer_member.get(i) == f2.vid)
        return holds, claims, members

    def _own_steps(self, i, booked):
        """The live frames other than `booked` whose own steps have instruction i: its original ownership."""
        dev = self.dev
        return [dev._frames[x] for x in sorted(dev._live_frames) if x != booked and i in dev._frames[x].kof]

    def register_blocked(self, v, events=(), group=(), fence=None, overlap=None):
        """16.1's validation: value v's registration is stopped this round.  It waits (a T2 event claimed, or an
        instruction of its group a member or held, by a live frame); or its fence refuses it (`fence`: a step of a
        prepared rebased contract); or its walk stops at its first atom with no step (`overlap`: the atom's events, some
        claimed by live frames).  The value-round counts, once, if the pending bookings' additions are all that stop it:
        with their added holds, extra claims and extra members removed, nothing would.  The original ownership stays, an
        extra member that another live frame's own step has included, and so does any other blocker.  An attribution at
        the same observed state, as `admit_blocked`'s, and an upper estimate: a booking F_5 would also have made counts
        until it splices.  The two refusals, which end the value's deferral, are also counted apart."""
        dev = self.dev
        key = (v.vid, dev.current_round)
        if key in self._r1_reg_seen:
            return
        holds, claims, members = self._booking_adds()
        if not (holds or claims or members):
            return
        if overlap is not None:
            mine = [e for e in overlap if e in dev._defer_claim]
            if not mine or any(e not in claims for e in mine):
                return
        elif fence is None:
            if any(e in dev._defer_claim and e not in claims for e in events):
                return
            for i in group:
                if i in dev._defer_member and (i not in members or self._own_steps(i, members[i])):
                    return
                h = dev._defer_hold_of.get(i)
                if h and h - holds.get(i, set()):
                    return
        else:
            for i in fence:
                o = dev._defer_member.get(i)
                fas = [] if o is None else (self._own_steps(i, o) if i in members else [dev._frames[o]])
                if any(dev._defer_fence_verdict(fa, fa.kof.get(i)) in ("refuse", "refuse_prepared") for fa in fas):
                    return
        self._r1_reg_seen.add(key)
        self.stats["r1:register_blocked"] += 1
        if overlap is not None or fence is not None:
            self.stats["r1:register_refused_" + ("overlap" if overlap is not None else "prepared")] += 1

    def outstanding(self) -> int:
        """The cores every pending and running commit still needs: its demand less its windows that have started, from
        the commit's own round (COMMIT's headroom check, spec 1.1)."""
        return sum(max(0, r["demand"] - r["started"]) for r in self.resv.values())

    def reserve(self, vid, demand, now, end, start=None) -> int:
        """A commit's reservation: its demand from the effect round, until its early script and repair can have ended;
        pending until its splice, which moves the end with a delayed splice (`spliced`).  R1 (16.1): a booking's pool count
        starts at `start`, the earliest round its script can start (the booking's readiness bound, at least now + ell)."""
        rid = self.resv_n
        self.resv_n += 1
        t1 = now + ELL if start is None else int(start)
        self.resv[rid] = dict(vid=vid, demand=int(demand), started=0, **{"from": t1}, end=int(end), pending=True,
                              dur=int(end) - t1, t0=now)
        self.stats["reservations"] += 1
        return rid

    def spliced(self, rid):
        r = self.resv.get(rid)
        if r is not None and r["pending"]:
            now = self.dev.current_round
            r["pending"], r["end"] = False, max(r["end"], now + r["dur"])
            if now > r["from"]:
                self.stats["splices_delayed"] += 1
                self.stats["splice_delay_rounds"] += now - r["from"]
            if self.r1:                                                   # (16.1's validation: the pending excess P, and its bound)
                P = now - r["t0"] - ELL
                self.stats["r1:spliced"] += 1
                self.stats["r1:pending_excess_rounds"] += P
                self.stats["r1:within_bound"] += int(P <= self.r1 - ELL)

    def release(self, rid, why):
        if self.resv.pop(rid, None) is not None:
            self.stats["reservation:end:" + why] += 1

    # -- registration (section 1.3)
    def register(self, v, fr) -> str | None:
        """At D's registration, before its first-block check: the frame's slot, masks and first decision; or why the site
        is refused."""
        tab = self.tab
        slot = tab.walk_slot_of.get(v.vid)
        if slot is None:
            self.stats["refused:no_slot" if v.vid in tab.profiles else "refused:no_profile"] += 1
            return "wager2_no_slot" if v.vid in tab.profiles else "wager2_no_profile"
        pr = tab.profiles[v.vid]
        F = min(fr.c.F, CAP)
        assert _shared_prefix(fr.c.steps[:F], pr["dig"]), ("the decision: the frame's walk and the static walk differ", v.vid)
        mask = [bool(fr.feas[p]) if p <= F else False for p in range(NP)]
        self._extension(mask, int(tab.fence0[slot]), 0)
        fr.w2 = dict(slot=slot, half=0, base=0, mask=mask, static=list(pr["feas"]), edges=tuple(int(x) for x in tab.edges0[slot]),
                     round=-1, lim=-1, last=None, commit=None, vid=v.vid, blocked=0, events=[])
        fr.w2["gap"] = [bool(fr.w2["static"][p] and not fr.w2["mask"][p]) for p in range(NP)]
        if self.fixed == "awf":
            fr.w2["nci"] = _nci_stop(fr)                                  # (16.11: D-nci's stop, set with the walk)
        return None

    def _extension(self, mask, fence: int, beta: int):
        """MED 3's amendment: a live walk whose boundaries run past the table's fence F (the
        half's last statically priced boundary) is counted on its branch; those lanes stop the walk, since ADVANCE is
        eligible only within the fence, so no ADVANCE is ever unpriced."""
        ext = sum(1 for p in range(NP) if mask[p] and p > fence)
        if ext:
            self.stats["walk_longer:beta%d" % beta] += 1
            self.stats["walk_longer_lanes:beta%d" % beta] += ext

    def rebased(self, fr, b: int):
        """At the splice (`_h_rebase`): the frame moves to (site, b)'s rebased key, half beta = 1, absolute lanes from b."""
        tab = self.tab
        w = fr.w2
        s_ = tab.reb_slot_of[(w["vid"], b)]
        rb = tab.profiles[w["vid"]]["reb"][str(b)]
        n = min(fr.c.F, CAP - b)
        assert _shared_prefix(fr.c.steps[:n], rb["dig"]), ("the decision: the rebased walk and its static walk differ", fr.vid, b)
        mask = [False] * NP
        for k in range(n + 1):
            mask[b + k] = bool(fr.feas[k])
        self._extension(mask, int(tab.fence1[s_]), 1)
        w.update(slot=s_, half=1, base=b, mask=mask, static=list(rb["feas"]), edges=tuple(int(x) for x in tab.edges1[s_]))
        w["gap"] = [bool(w["static"][p] and not w["mask"][p]) for p in range(NP)]
        if self.fixed == "awf":
            w["nci"] = _nci_stop(fr)                                      # (16.11: the rebased walk's D-nci stop, from b)

    # -- the decision (section 1.3)
    def decide(self, fr, patches_in_use, registration=False, booked=False, pending=False):
        """The round's decision for a carrying frame: the admission limit r° and the action at the real stop r.  ADVANCE
        is eligible at a lane when its block ends within min(adm, cap, F): D's admissible run, the frame's cap and the
        slot half's fence (the RTL's `hw/rtl/decision_unit.sv` and `hw/golden/w2_table.py`), and the jump rule allows it.

        booked (R1, 16.1): the round's one lookup of a booked frame, from the same row.  The booking stands before the
        prefix is complete unless F_5's rule (COMMIT eligible only if F_5's checks 2 and 3 pass) takes ADVANCE at the
        boundary; once the prefix is complete, only if the round's action with COMMIT eligible ("booked") is COMMIT.
        Returns ("booked", stands, r°), r° the admission limit F_5's rule sets, which the frame takes on a cancel.
        pending (R1): the same lookup for a frame whose COMMIT waits for rule 66's commit check (held at p, not yet booked).
        Under R1 every lookup keeps its row at p (`r1_row`), which the commit check's completion in the same round reuses."""
        dev = self.dev
        now = dev.current_round
        w = fr.w2
        v = dev.values[fr.vid]
        tab = self.tab
        t0 = dev.schedule_instructions[v.producer_idx].end_round
        e = now - t0
        self._age(fr, e)
        if e >= AGE_SAT:
            self.stats["age_saturated_lookups"] += 1
        c = self.cstate(v, fr)
        l = self.load_bin if not self.c0 else 2
        if self.r5 and l == 0:
            self.stats["r5:l0_lookups"] += 1                              # (R5, 16.8: a lookup of an l0 row)
        if self.r4:                                                        # (R4, 16.7: eta's bin in place of the age's)
            j = bisect.bisect_right(w["edges"], self.eta_hat(v))
            self.stats["r4:eta_bin%d" % j] += 1
        else:
            j = bisect.bisect_right(w["edges"], min(e, AGE_SAT))           # (the 10-bit register saturates at 1,023)
        half, slot, base = w["half"], w["slot"], w["base"]
        codes = (tab.codes0 if half == 0 else tab.codes1)[slot, (c * NL + l) * getattr(tab, "NB", 1) + self.blevel, j]
        self.stats["lookups"] += 1
        if dev._hw is not None:
            dev._hw.charge_lookup()                                       # (round 7, 3(f): the lookup's cycles, if charged)
        if self._lk_round != now:
            self._lk_round, self._lk_n = now, 0
        self._lk_n += 1
        self.lk_max = max(self.lk_max, self._lk_n)
        if dev._hw is not None:                                           # spec 9: at most 64 lookups a round (the mode asserts it)
            assert self._lk_n <= 64, ("the decision: more than 64 lookups in round", now)
        cut = 0 if registration else dev._defer_current(fr)
        cap = min(fr.stop, CAP - base)                                    # D's limit, and the walk cap (rule 66)
        fence = int((tab.fence0 if half == 0 else tab.fence1)[slot]) - base
        lim_s = min(cap, fence)
        fx = self.fixed
        if fx is not None:                                                # (16.11: the rule's s in place of min(cap, F_beta))
            lim_s = cap if fx == "aa" else min(cap, w["nci"])
        coa = None                                                        # (COA, plan 3.2: the row's order, fixed)
        if self.coa is not None:
            if half == 1:
                coa, lim_s = "AW", cap                                    # (after the splice: the rebased walk to its cap)
            else:
                pu = self._pred(v, fr)
                trig = pu is not None and bool(pu[0]) and pu[1] >= self.coa
                if trig:
                    self.stats["coa:trigger_lookups"] += 1
                elif pu is not None and not pu[0] and 1.0 - pu[1] >= self.coa:
                    self.stats["coa:m0_confident_lookups"] += 1           # (a confident m = 0: it waits)
                coa = "CW" if trig else "W"
        adm = self._admissible(fr, cap)                                   # D's admission conditions, as `_defer_admit` reads them
        mask, gap = w["mask"], w["gap"]
        lanes = [k for k in range(cut, cap + 1) if mask[base + k]]
        assert lanes, ("the decision: no boundary of the frame's mask between its cut and its cap", fr.vid, cut, cap, base)
        r0 = r = None
        act = act_r = None
        p_first = lanes[0]
        if coa is not None and half == 0 and p_first > cut:
            self.stats["coa:stand_past_cut_lookups"] += 1                 # (records: WAIT stands at the first usable boundary)
        assert not (booked or pending) or (half == 0 and p_first == (fr.target if booked else w.get("pending"))), \
            ("the decision's R1: a booking off its boundary", fr.vid, p_first, fr.target, w.get("pending"))
        com_memo = {}
        at_r = None
        first = hold = None
        for i, k in enumerate(lanes):
            code = int(codes[base + k])
            order = ORDER0[code] if half == 0 else ORDER1[code]
            kn = lanes[i + 1] if i + 1 < len(lanes) else None
            jump = kn is not None and any(gap[base + q] for q in range(k + 1, kn))
            within = kn is not None and kn <= lim_s
            if coa is not None:                                           # (COA: COMMIT or WAIT before the splice, ADVANCE
                order, adv_static = coa, within                           #  to the cap after it)
            elif fx is not None:                                          # (16.11: ADVANCE first, eligible by the rule alone;
                order = "A" + order.replace("A", "")                      # WAIT and COMMIT in the row's order)
                adv_static = within
            else:
                adv_static = within and (not jump or (half == 0 and code in (0, 1)))
            adv_real = adv_static and kn <= adm
            if kn is not None and jump and within and not adv_static and order.index("A") < order.index("W"):
                self.stats["jump_prohibited" if half == 1 else "jump_removed"] += 1
            if i == 0 and self.r1:                                        # (16.1: the boundary's row and F_5's hold there)
                first = (order, adv_real)
                hold = _first(order.replace("C", ""), adv_real, _never) == "W"

            def com_ok():
                if "x" not in com_memo:
                    if booked or pending:                                 # (16.1: F_5's checks 2 and 3 at a booked boundary)
                        com_memo["x"] = k == p_first and self._f5_ready(fr, k, patches_in_use)
                    else:
                        com_memo["x"] = (half == 0 and k == p_first and not registration
                                         and (self._commit_ok(fr, k, patches_in_use, hold) if self.r1
                                              else self._commit_ok(fr, k, patches_in_use)))
                return com_memo["x"]

            if r0 is None:
                a0 = _first(order, adv_static, com_ok)
                if a0 != "A":
                    r0, act = k, a0
            if r is None:
                a1 = _first(order, adv_real, com_ok)
                if a1 != "A":
                    r, act_r = k, a1
                    x_nj = within and kn <= adm                               # (jr: ADVANCE here, were the jump allowed)
                    at_r = dict(code=code, adv=int(adv_real), jump=int(jump), jr=int(_first(order, x_nj, com_ok) == "A"))
            if r0 is not None and r is not None:
                break
        if r0 is None:                                                    # (unreachable: the last lane's ADVANCE is never eligible)
            r0, act = lanes[-1], "W"
        if r is None:
            r, act_r = r0, act
            at_r = dict(code=int(codes[base + r]), adv=0, jump=0, jr=0)
        w["round"] = now
        if self.r1:                                                       # (16.1: the round's row at p, for the check's completion)
            w["r1_row"] = dict(round=now, order=first[0], adv=first[1], hold=hold, r0=r0,
                               stands=bool(act_r == "C" and r == p_first and not registration))
        self.last = dict(round=now, vid=fr.vid, t0=t0, e=e, c=c, l=l, j=j, half=half, slot=slot, base=base, cut=cut, adm=adm, cap=cap,
                         fence=fence + base, cok=com_memo.get("x"), p=p_first, r0=r0, r=r, act=act_r, registration=registration,
                         mask=sum(1 << q for q in range(NP) if mask[q]), gap=sum(1 << q for q in range(NP) if gap[q]), **at_r)
        if self.vqf is not None or self.rec7:
            self.last.update(b=self.blevel, q=self.backlog_q)
            w["lq"] = (l, self.blevel, self.backlog_q)                   # (round-7 records: the frame's last decision's l and b)
        if booked or pending:
            dev_complete = all(dev._defer_complete(i) for q in range(p_first) for i in fr.members[q])
            r1act = _first(first[0], first[1], lambda: True)              # (COMMIT eligible: booked)
            f5act = act_r if r == p_first else "A"                        # (F_5's rule at the boundary)
            stands = (r1act == "C") if dev_complete else (f5act != "A")
            pre = "r1:" if booked else "r1:pending_"
            self.stats[pre + ("booked_lookups" if booked else "lookups")] += 1
            if not stands:
                self.stats[pre + "redecided_" + ("complete" if dev_complete else "advance")] += 1
            w["r1_row"]["stands"] = stands
            rec = w.get("commit_rec")
            if booked and dev_complete and rec is not None and rec.get("complete") is None:
                rec["complete"] = now                                     # (the first round the prefix is complete)
            self.last["act"] = "C" if stands else ("A" if r0 > p_first else "W")
            self._log(fr, 0)
            return ("booked", stands, r0)
        if act_r == "C" and r == p_first:
            self.stats["decide:commit"] += 1
            w["lim"] = r
            self._log(fr, 0)
            return ("commit", r)
        back = 0
        if fr.admitted > r0:
            if not dev._resv_back_ok(fr, r0):                             # (SER-R: a take-back by choice is made only once the floor
                self.stats["take_back_refused"] += 1                      #  and its marks are secured; else nothing is taken back, the decision
                fr.limit = w["lim"] = fr.admitted                         #  still binds admission, and the take-back is retried next round)
                self._log(fr, 0)
                return ("limit", fr.admitted)
            back = fr.admitted - r0
            dev._defer_unadmit(fr, r0)
            self.stats["take_back"] += 1
        fr.limit = w["lim"] = r0
        self._log(fr, back)
        return ("limit", r0)

    def eta_hat(self, v) -> int:
        """R4 (16.7): eta-hat = min(floor(eta), 1,023), eta rule 30's causal estimate of the rounds left until v's verdict,
        read at the decision through `froq.verdict_eta.Reader` (H-lead's reader; it reads no sampled duration and no window
        that has not arrived).  The reader is made again when the device's LINK is not its own (a restored checkpoint)."""
        if self._eta is None or self._eta.link is not _link():
            from .verdict_eta import Reader
            self._eta = Reader(self.dev, _link())
        x = eta_index(self._eta.estimate(v))
        self.stats["r4:eta_lookups"] += 1
        if x >= AGE_SAT:
            self.stats["r4:eta_saturated"] += 1
        return x

    def _f5_ready(self, fr, k: int, patches_in_use) -> bool:
        """F_5's COMMIT checks 2 and 3 at k (section 1.1): every prefix member has started, and a_k <= now + ell."""
        dev = self.dev
        if any(i not in dev._defer_started for q in range(k) for i in fr.members[q]):
            return False
        a = dev._u_ready(fr, k, patches_in_use)[0]
        return a is not None and a <= dev.current_round + ELL

    def r1_commit_now(self, fr, k: int, patches_in_use) -> bool:
        """R1 (16.1): a completed commit check books k only if the round's lookup (its row at p) gives COMMIT there, with
        R1's eligibility checked now and the hold rule read from the same row."""
        row = fr.w2["r1_row"]
        return row["stands"] and _first(row["order"], row["adv"],
                                        lambda: self._commit_ok(fr, k, patches_in_use, row["hold"])) == "C"

    def _admissible(self, fr, cap: int) -> int:
        """The furthest boundary D's `_defer_admit` reaches now from the admission point, block by block (its own test,
        `_defer_admissible`, with the blocks before counted as admitted)."""
        dev = self.dev
        jj, virt, best = fr.admitted, set(), fr.admitted
        while jj < cap and dev._defer_admissible(fr, jj, virt):
            virt |= fr.members[jj]
            jj += 1
            if fr.feas[jj]:
                if (getattr(dev, "_r7_guard", False) or getattr(dev, "_r7_reserve", 0)) and dev._r7_refuses(fr, best, jj, count=False):
                    break                                                  # (round 7's guard or T reserve, as `_defer_admit`)
                best = jj
        return best

    def _commit_ok(self, fr, k: int, patches_in_use, hold=None) -> bool:
        """COMMIT's run-time eligibility at the decision boundary k (section 1.1); the prepared rebased contract is kept for
        the act.  R1 (16.1) replaces checks 2 and 3 by a known readiness a_p <= now + H_R1, and books only where F_5's rule
        would hold k too: F_5's checks 2 and 3 pass, or `hold` (the round's first eligible action without COMMIT is WAIT).
        A booking needs its prefix admitted (a booked frame admits nothing more) and reads the bound with zero_start."""
        dev = self.dev
        st = self.stats
        w = fr.w2
        if fr.rebased or (w["vid"], k) not in self.tab.reb_slot_of:
            st["commit:not_legal"] += 1
            return False
        if not self.r1:
            if any(i not in dev._defer_started for q in range(k) for i in fr.members[q]):
                st["commit:readiness_unknown"] += 1
                return False
            a, _ws, why = dev._u_ready(fr, k, patches_in_use)
            if a is None or a > dev.current_round + ELL:
                st["commit:not_ready"] += 1
                return False
        else:
            if fr.admitted < k:                                           # (a booked frame admits nothing more: its prefix must be)
                st["commit:r1_not_admitted"] += 1
                return False
            a, _ws, why = dev._u_ready(fr, k, patches_in_use, zero_start=True)
            if a is None:
                st["commit:readiness_unknown"] += 1
                return False
            if a > dev.current_round + self.r1:
                st["commit:r1_horizon"] += 1
                return False
            f5 = a <= dev.current_round + ELL and all(i in dev._defer_started for q in range(k) for i in fr.members[q])
            assert hold is not None, ("the decision's R1: a COMMIT check without its round's hold rule", fr.vid, k)
            if not f5 and not hold:
                st["commit:r1_would_advance"] += 1                        # (F_5's rule would advance here: no booking)
                return False
            w["a_hat"], w["f5"] = int(a), bool(f5)
        if not dev._u_cells_free(fr, k):
            st["commit:cells"] += 1
            return False
        if not fr.feas[k] or dev._defer_ws_blocked(fr, k, patches_in_use, splice=False):
            st["commit:pinned"] += 1
            return False
        dec = _link().get("decoder")
        if dec is not None:
            fe = not dev._pending_any(dec)
            if not fe:
                if _queue_over(dev, dec):
                    st["commit:queue"] += 1
                    return False
            c_ = dev._u_cores(dec)
            if c_:
                busy = len(dec._active_window_progress)
                if dev._pool is not None and not dev._pool.preemptive:
                    busy += dev._pool.busy(dec._current_round)
                idle = c_ - busy - self.outstanding()                     # (net of every pending and running commit)
                if idle < self.prof_ops[w["vid"]]["NC"][k]:                # (operational, the run's own profile)
                    st["commit:headroom"] += 1
                    if self.r1 and idle + sum(max(0, r["demand"] - r["started"]) for r in self.resv.values() if r["pending"]) \
                            >= self.prof_ops[w["vid"]]["NC"][k]:
                        self._r1_refusal(fr)                              # (16.1's validation: the bookings' demand decided it)
                    return False
        if len(self.resv) >= SLOTS or (dev._hw is not None and not dev._hw.resv_slot_free()):
            st["commit:resv_full"] += 1
            if self.r1 and any(r["pending"] for r in self.resv.values()):
                self._r1_refusal(fr)
            return False
        if k in w.setdefault("bad", set()):
            st["commit:rebase_permanent"] += 1
            return False
        nxt, why2, perm = dev._h_prepare_core(fr, k, patches_in_use)
        if nxt is None:
            st["commit:rebase:" + why2] += 1
            if perm:
                w["bad"].add(k)                                           # (as H's `h_bad`: a permanent reason marks k)
            return False
        rb = self.prof_ops[w["vid"]]["reb"][str(k)]
        if not _shared_prefix(nxt.c.steps[:min(nxt.c.F, CAP - k)], rb["dig"]):
            st["commit:rebase_mismatch"] += 1
            return False
        w["nxt"] = nxt
        return True

    def _r1_refusal(self, fr):
        """16.1's validation: a commit the bookings refused, once a frame a round (a revalidation in the same round, at the
        commit check's completion, is the same refusal)."""
        key = (fr.vid, self.dev.current_round)
        if key not in self._r1_refused:
            self._r1_refused.add(key)
            self.stats["r1:commit_refused_by_booking"] += 1

    def commit_args(self, fr, k: int):
        """(the prepared rebased contract, the reservation's demand, end round and pool start) for the act at k.  R1
        (16.1): the pool counts a booking from max(now + ell, a_p), the earliest round its script can start."""
        w = fr.w2
        pr = self.prof_ops[w["vid"]]                                      # (operational, the scripts that run)
        now = self.dev.current_round
        rb = pr["reb"][str(k)]
        t1 = max(now + ELL, int(w.pop("a_hat", now + ELL))) if self.r1 else now + ELL
        return w.pop("nxt"), pr["NC"][k], t1 + pr["S"][k] + rb["Rres"], (t1 if self.r1 else None)

    DECISION_FIELDS = ("round", "vid", "e", "c", "l", "j", "half", "slot", "cut", "p", "r0", "r", "act", "code", "adv", "cok",
                       "jump", "jr", "taken_back")

    def _log(self, fr, back: int):
        """A decision that changes the frame's admission or action (spec 14): its inputs and outputs, the entry's code at
        r, the eligibility bits (ADVANCE's, COMMIT's checks), whether r's block is a jump, jr, and the steps taken back.
        A decision that changes nothing is counted, not recorded."""
        w, x = fr.w2, self.last
        key = (x["r0"], x["r"], x["act"], x["c"], x["l"], x["j"], x["half"], x["slot"]) + ((x["b"],) if self.vqf is not None else ())
        if key != w["last"] or back:
            self.stats["decision_changes"] += 1
            w["last"] = key
            if self.records:
                self._rec("decisions", [x["round"], fr.vid, x["e"], x["c"], x["l"], x["j"], x["half"], x["slot"], x["cut"], x["p"],
                                        x["r0"], x["r"], x["act"], x["code"], x["adv"], x["cok"], x["jump"], x["jr"], back])
            if self.rec7:
                self._rec("r7_decisions", [x["round"], fr.vid, x["e"], x["c"], x["l"], x["b"], x["q"], x["half"], x["r0"], x["r"], x["act"], back])

    def _rec(self, kind: str, row):
        if len(self.rec[kind]) < RECORDS_MAX:
            self.rec[kind].append(row)
        else:
            self.stats["records:truncated:" + kind] += 1

    # -- spec 14's records (behind --w2-records): per commit, per value (J_v's parts, Lambda's calibration), errors
    def commit_rec(self, fr, b: int, c: int, fetch: bool) -> dict:
        """A commit's record, completed when it ends (its outcome: spliced, then done; or a cancel's reason)."""
        w = fr.w2
        pi = C_REP[c] if c > 0 else self.tab.site[w["vid"]][1]
        r = dict(round=self.dev.current_round, vid=fr.vid, b=b, c=c, pi=pi, slot=self.tab.reb_slot_of.get((w["vid"], b)),
                 fetch=bool(fetch), outcome=None, splice=None)
        if self.rec7:
            w["commit_lq"] = w.get("lq")                                  # (round-7 records: the commit decision's l and b)
        if self.records:
            self._rec("commits", r)
        return r

    def admit(self, v, r=None) -> bool:
        """V2q's admission at registration (plan v3 3(a)): in `_defer_register` after the delivered check, before the frame
        and every predictor read, reservation or commit for the value. True lets it register: V(0 | c0, l, b) + m < J_N(l,
        b), its walk slot's bit at the tick's l and b; False keeps it on N's path for good. Decided once per value (a
        registration retried next round keeps it); a value without a walk slot is left to `register`'s refusal, as before.
         (b): r is the readiness query's count (`reach`), and the bit is read at (l, b, min(r, VQ_RCAP)); r = 0 is
        rejected. The query and this lookup run later in `_defer_register`, after D's own refusals and before W2's slot."""
        vid = v.vid
        assert (r is not None) == self.vqr, ("(b): the admission reads r exactly when the setting is", vid, r)
        if vid in self._vq_seen:
            return self._vq_seen[vid]
        self.ensure_built()
        tab = self.tab
        slot = tab.walk_slot_of.get(vid)
        l, b = self.load_bin, self.blevel
        rq = None if r is None else min(int(r), VQ_RCAP)
        ok = True if slot is None else bool(tab.adm0[slot, l, b] if rq is None else tab.adm0[slot, l, b, rq])
        self._vq_seen[vid] = ok
        if slot is not None:
            if self.dev._hw is not None:
                self.dev._hw.charge_lookup()                              # (the admission table's lookup, if charged)
            self.stats["vq:admitted" if ok else "vq:rejected"] += 1
            if rq is not None:
                self.stats["vq:reach%d:%s" % (rq, "admitted" if ok else "rejected")] += 1
            if self.rec7 and not ok:
                self._shadow[vid] = (slot, self.dev.current_round)       # (the shadow evaluation follows it to its verdict)
                if self.r7d:
                    self._shadow_c[vid] = (slot, self.dev.current_round) # (and to its first later COMMIT, records only)
            if self.rec7:
                v0, jn = tab.adm_v[vid]
                self._rec("r7_admission", [self.dev.current_round, vid, slot, l, b, self.backlog_q,
                                           round(float(v0[l, b] if rq is None else v0[l, b, rq]), 4), round(float(jn[l, b]), 4), self.vqm,
                                           int(ok)] + ([int(r)] if rq is not None else []))
        return ok

    def reach(self, fr) -> int:
        """(b)'s readiness query: r, the certified boundaries past the walk's first lane f0 that D could admit now. It
        runs in `_defer_register` on the registration's candidate frame (the contract, its feasible mask, cap and stop:
        D's computations, which hold, claim and reserve nothing), before W2's slot and first decision -- the value's first
        predictor read -- and before the cuts, holds, claims and the reservation. The rules are the decision's admissible
        run at the first decision's cap (`_admissible`): D's dependency and hold test block by block, the frame's feasible
        mask, and round 7's guard and T reserve, uncounted. A boundary counts if both the frame's mask and the table's
        walk have it, so the walk capped at the r-th lane past f0 never reaches past the reachable boundary. Reads only."""
        self.ensure_built()
        tab = self.tab
        slot = tab.walk_slot_of.get(fr.vid)
        if slot is None:
            return 0
        st, fence, F = tab.profiles[fr.vid]["feas"], int(tab.fence0[slot]), min(fr.c.F, CAP)
        best = self._admissible(fr, min(fr.stop, CAP))
        fl = [p for p in range(NP) if st[p] and p <= fence]
        return sum(1 for p in fl[1:] if p <= min(best, F) and fr.feas[p])

    def _shadow_tick(self, now):
        """Round 7's shadow evaluation (plan v3 3(a); records only, for seed 4's traces): each value V2q rejected, until its
        decoded delivery, is looked up as if registered -- the published prediction's c-state (no new predictor work: the
        device holds every value's prediction), the tick's l and b, its age bin -- at its walk's first lane; the first
        round whose order puts ADVANCE or COMMIT before WAIT is recorded, and the value leaves the evaluation."""
        dev = self.dev
        for vid in sorted(self._shadow):
            v = dev.values[vid]
            slot, r0 = self._shadow[vid]
            if v.delivered:
                del self._shadow[vid]
                self._rec("r7_shadow", [vid, r0, None, None, None, None, None, None])
                continue
            e, cs, order, _m = self._shadow_look(v, slot, now)
            if order[0] != "W":
                del self._shadow[vid]
                self._rec("r7_shadow", [vid, r0, now, e, cs, self.load_bin, self.blevel, order[0]])
        for vid in sorted(self._shadow_c):                                 # (records only: the first round whose order at f0
            v = dev.values[vid]                                            #  puts COMMIT before WAIT -- a later, prediction-enabled
            slot, r0 = self._shadow_c[vid]                                 #  COMMIT the admission gave up -- with the prediction m and
            if v.delivered:                                                #  the truth; COMMIT's eligibility checks are not evaluated)
                del self._shadow_c[vid]
                self._rec("r7_shadow_c", [vid, r0, None, None, None, None, None, None, None, None])
                continue
            e, cs, order, m = self._shadow_look(v, slot, now)
            if order.index("C") < order.index("W"):
                del self._shadow_c[vid]
                self._rec("r7_shadow_c", [vid, r0, now, e, cs, self.load_bin, self.blevel, order, None if m is None else int(m),
                                          int(v.truth)])

    def _shadow_look(self, v, slot: int, now: int) -> tuple:
        """The shadow evaluation's lookup of a rejected value: (age, c-state, the order at its walk's first lane, m)."""
        from .device import H_INSTALL
        dev, tab = self.dev, self.tab
        pr, m, c, _rc = dev._dview(v)
        cs = 0 if (pr < 0 or m is None or now < pr + H_INSTALL) else 1 + bisect.bisect_right(C_EDGES, float(c) if m else 1.0 - float(c))
        e = now - dev.schedule_instructions[v.producer_idx].end_round
        j = bisect.bisect_right(tuple(int(x) for x in tab.edges0[slot]), min(e, AGE_SAT))
        f0 = next(p for p in range(NP) if tab.profiles[v.vid]["feas"][p])
        return e, cs, ORDER0[int(tab.codes0[slot, (cs * NL + self.load_bin) * tab.NB + self.blevel, j, f0])], m

    def q_at(self, rnd):
        """Round-7 records: the backlog count the tick read for round rnd (its last change at or before rnd)."""
        i = bisect.bisect_right(self._qhist, (rnd, math.inf)) - 1
        return self._qhist[i][1] if i >= 0 else 0

    def _recovery_row(self, fr, rec, spliced: bool, com) -> list:
        """Round-7 records (plan v3 3(a)), one per recovery: the class k (0 the misprediction's script S, 1 the wrong
        commit's repair R), the lane, (l, b, count) at the decision that set it, the count at its end, recovery_end_round,
        done_round (the decoded delivery under the hold), the priced XS_k, Delta(l, b, k) and the shortfall share
        kappa x max(0, N - F_l)."""
        k = 1 if spliced else 0
        w = fr.w2
        lq = (w.get("commit_lq") if k else w.get("lq")) or (None, None, None)
        pr = self.tab.profiles[w["vid"]]
        p = int(w.get("base", 0)) + int(fr.admitted)                     # (the frame's admitted boundary, on absolute lanes)
        if k:
            rb = pr["reb"].get(str(com["b"])) or pr["reb"].get(com["b"]) or {}
            XS, N = (rb.get("XR") or [0] * NP), (rb.get("N") or [0] * NP)
            forms = rb.get("form")
        else:
            XS, N = pr["XS"], pr["N0"]
            forms = pr.get("form")
            if self.tab.hold is not None:
                XS = [int(round(self.tab.hold))] * NP                      # (the priced hold is H)
        p = min(max(p, 0), NP - 1)
        l, bl = lq[0], lq[1]
        dl = None
        if self.vqf is not None and l is not None:
            dl = 0.0 if self.opts["vqd0"] else round(float(self.vqf.delta[l, bl, k]), 4)
        _key, _p1, mu = self.tab.site[w["vid"]]
        kap = kappa(self.opts, mu, self.B) if not self.c0 else 0.0
        Fl = None if (self.B is None or l is None or L_REP[l] is None) else L_REP[l] * float(self.B)
        short = 0.0 if Fl is None else kap * max(0.0, float(N[p]) - Fl)
        end, done = rec.get("recovery_end_round"), rec.get("done_round", -1)
        row = [fr.vid, k, p, l, bl, lq[2], None if end is None else self.q_at(end), end, done, int(XS[p]), dl, round(short, 4)]
        return row + [forms[p] if forms else None] if "form" in pr else row            # (sx library only: the lane's form)

    def _diag_row(self, fr, rec, spliced: bool, com) -> list:
        """(records only, froq/diagrec.py): a repair's delivery (verdict, the script's start and end, the settlement's
        end, the decoder's queue and service within the settlement) beside the charges the decision used for its lane, as
        the tables price them: W (R2's W_0, or W_1 for a wrong commit's repair), S or R, XS or XR, N, U, kappa, the
        shortfall at the decision's load bin and the load term kappa x shortfall + R3's work; and the lane's script and
        settlement in the profile the run executes."""
        dev, k, w = self.dev, (1 if spliced else 0), fr.w2
        lq = (w.get("commit_lq") if k else w.get("lq")) or (None, None, None)
        l = lq[0]
        pr, po = self.tab.profiles[w["vid"]], self.prof_ops[w["vid"]]
        p = min(max(int(w.get("base", 0)) + int(fr.admitted), 0), NP - 1)
        if k:
            rb = pr["reb"].get(str(com["b"])) or {}
            ro = po["reb"].get(str(com["b"])) or {}
            S, XS, N, U, Sr, XSr, forms = rb.get("R"), rb.get("XR"), rb.get("N"), rb.get("U"), ro.get("R"), ro.get("XR"), ro.get("form")
            W = None if self.r2w is None else self.r2w["W1"]
        else:
            S, XS, N, U, Sr, XSr, forms = pr["S"], pr["XS"], pr["N0"], pr.get("U"), po["S"], po["XS"], po.get("form")
            W = None if self.r2w is None else self.r2w["W0"]
        at = lambda a: (a[p] if a is not None and p < len(a) else None)
        _key, _p1, mu = self.tab.site[w["vid"]]
        kap = kappa(self.opts, mu, self.B) if not self.c0 else 0.0
        Fl = None if (self.B is None or l is None or L_REP[l] is None) else L_REP[l] * float(self.B)
        n_, u_ = at(N), at(U)
        short = 0.0 if (Fl is None or n_ is None) else max(0.0, float(n_) - Fl)
        lam3 = float(self.opts["lam"])
        work = lam3 * (short / float(n_)) * float(u_ or 0.0) / float(self.B) if (lam3 > 0 and self.B and n_) else 0.0
        ins = [dev.schedule_instructions[i] for i in rec.get("instrs", ())]
        start = min(t.start_round for t in ins) if ins and all(t.start_round != -1 for t in ins) else None
        q, ds, nw, na = self.diag.delivery(_link().get("decoder"), rec, fr.vid)
        row = [fr.vid, k, p, l, fr.verdict_round, start, rec.get("recovery_end_round"), rec.get("done_round", -1), q, ds, nw, na,
               W, at(S), at(XS), n_, u_, round(kap, 6), round(short, 4), round(kap * short + work, 4), at(Sr), at(XSr), at(forms)]
        if getattr(self, "sdv", None) is not None:
            row += self._sd_fields(fr, row, k, p, start, pr)              # (the corrected charge)
        return row

    def _sd_fields(self, fr, row, k, p, start, pr) -> list:
        """(start-delay note v2 2.2 and section 4, records only). A wrong default's repair (k = 0) under
        `--w2-sd` is charged S + M(d) + load + W: W = max(0, A + c(d)) when a step was in flight at the verdict (its script
        started after the verdict's round, the training check's rule) and 0 otherwise. The row's W and XS become that W and
        M(d) (R2's W_0 and the profile's XS are no charge of this run's walk half), and SD_FIELDS add the branch, A, c, M, the
        charge, the reconstruction the charge reads (`_lanes`' advance into the lane from the previous feasible lane: that
        lane, D = cum[lane] - cum[p] and the start age a = start - D - t_end, as the training check takes them) and the execution
        path's own record (`_defer_advance`: the lane at the verdict, the decide's first usable and chosen boundaries, and the
        first start and last end of the instructions of the steps from p to the lane). k = 1 rows keep W_1 and XR."""
        if k != 0:
            return [None] * len(SD_FIELDS)
        dev, w = self.dev, fr.w2
        c, M = self.sdv["c"], self.sdv["M"]
        base, V = int(w.get("base", 0)), fr.verdict_round
        top = min(int(pr["top"]), int(pr["snci"])) if self.opts["scope0"] == "nci" else int(pr["top"])
        fl = [q for q in range(NP) if pr["feas"][q] and q <= top]
        rp = rD = ra = A = None
        if p in fl and fl.index(p) > 0:
            rp = fl[fl.index(p) - 1]
            rD = int(sum(int(x) for x in pr["d"][rp:p]))
        infl = start is not None and V is not None and V >= 0 and start > V
        branch = "wait" if not infl else ("advance" if rD else "advance_unreconstructed")
        Wc = 0.0
        if branch == "advance":
            t_end = dev.schedule_instructions[dev.values[fr.vid].producer_idx].end_round
            ra = max(0, start - rD - t_end)
            key = self.tab.site[w["vid"]][0]
            A = sd_resid_at(self.lam.model(int(dev.d_t), self.level, key, self.opts["horizon"]).surv(ra + rD + 2), ra, rD)
            Wc = max(0.0, A + c)
        row[12], row[14] = round(Wc, 6), M
        charge = (row[13] or 0) + M + row[19] + Wc
        steps = range(max(0, rp - base), max(0, p - base)) if rp is not None else range(max(0, p - base - 1), max(0, p - base))
        ins = [dev.schedule_instructions[i] for s_ in steps if s_ < len(fr.members) for i in fr.members[s_]]
        st = [t.start_round for t in ins if t.start_round != -1]
        en = [t.end_round for t in ins]
        x_start = min(st) if st else None
        x_end = max(en) if en and all(x != -1 for x in en) else None
        lane = lambda j: (base + int(j)) if j is not None and int(j) >= 0 else None   # noqa: E731
        return [branch, None if A is None else round(A, 6), c, M, round(charge, 6), rp, rD, ra, lane(fr.p_verdict),
                lane(fr.first_ok), lane(fr.target), x_start, x_end]

    def _age(self, fr, e: int):
        self.age_max = max(self.age_max, e)
        if e >= AGE_WRAP:
            self.stats["age_16bit"] += 1

    def age_check(self, fr):
        """The age of a registered value at its frame's end and at the cut (and, in `decide`, at its lookups): the largest
        is recorded, and the values that reach 2^16 rounds are counted. The hardware's age register is 10 bits and
        saturates at 1,023, so a lookup reads min(age, 1,023) and nothing wraps."""
        dev = self.dev
        self._age(fr, dev.current_round - dev.schedule_instructions[dev.values[fr.vid].producer_idx].end_round)

    def forecast(self) -> float | None:
        """The free-core forecast F the load bin reads (section 4.3), in cores, as of this round; None without a cap."""
        return self.free_cores

    def blocked_tick(self, fr):
        """J_v's B_v: a round in which the frame waits at its admission point with its prefix complete (records only)."""
        dev = self.dev
        if all(dev._defer_complete(i) for k in range(fr.admitted) for i in fr.members[k]):
            fr.w2["blocked"] = fr.w2.get("blocked", 0) + 1

    def verdict(self, fr):
        """At the frame's verdict: a wrong bet that resolves (spec 14's error clustering): the round, the kind (a wrong
        commit, beta = 1 and m = 0; a wrong default, beta = 0 and m = 1), the load bin and the full-decode queue."""
        if not self.records:
            return
        dev = self.dev
        m = bool(dev.values[fr.vid].truth)
        beta = 1 if fr.rebased else 0                                     # (a commit before its splice: verdict first, beta 0)
        if beta == 1 and not m or beta == 0 and m:
            dec = _link().get("decoder")
            q = sum(1 for t in dec._pending_decode_tasks if dec._get_task_or_none(t) is not None) if dec is not None else None
            self._rec("errors", [dev.current_round, fr.vid, "wrong_commit" if beta else "wrong_default", self.load_bin, q])

    def value_end(self, fr, how: str):
        """A registered value's record at its frame's end (spec 14): its Lambda-key and verdict age (Lambda's calibration,
        censored when the run ended first), its slot, its commit, J_v's parts B_v, R_v and Lc_v, and the decoding events
        Lc_v sums (kind, round, demand N, forecast F)."""
        if not (self.records or self.rec7):
            return
        dev = self.dev
        w = fr.w2
        v = dev.values[fr.vid]
        t_end = dev.schedule_instructions[v.producer_idx].end_round
        key, p1, mu = self.tab.site[w["vid"]]
        com = w.get("commit_rec")
        B = int(w.get("blocked", 0))
        if com is not None and com.get("splice") is not None and fr.h_early_done >= 0:   # (`spliced`)
            B += fr.h_early_done - com["round"]                           # (the commit's latency and early script)
            if self.r1 and com.get("complete") is not None:               # (R1: the prefix's execution after the booking is
                B -= max(0, com["complete"] - com["round"])               #  useful work, not blocked time, 16.1)
        rec = v.recovery or {}
        R = 0
        spliced = com is not None and com.get("splice") is not None
        mat = how == "materialized" or (how == "censored" and fr.state == "spliced")   # (a materialization at the cut, not yet
        wrong = (spliced and not v.truth) or (not spliced and mat)                       #  retired or settled: its repair)
        if wrong and rec.get("kind") == "script":
            ins = [dev.schedule_instructions[i] for i in rec.get("instrs", ())]
            if ins and all(t.end_round != -1 for t in ins):
                R = max(t.end_round for t in ins) - min(t.start_round for t in ins)
                if dev.defer_cfg.get("settle") == "decode" and rec.get("done_round", -1) >= 0 and rec.get("recovery_end_round") is not None:
                    R += max(0, rec["done_round"] - rec["recovery_end_round"])   # (its settlement, J_v's R_v; 0 under none)
        kap = kappa(self.opts, mu, self.B) if not self.c0 else 0.0     # (the tables' kappa, on the arm's scale)
        ev = w.get("events", [])
        Lc = sum(kap * max(0.0, n - f) for _k, _r, n, f in ev if f is not None)
        if self.rec7 and wrong and rec.get("kind") == "script":
            self._rec("r7_recoveries", self._recovery_row(fr, rec, spliced, com))
        if getattr(self, "diag", None) is not None and wrong and rec.get("kind") == "script":
            self.diag.repairs.append(self._diag_row(fr, rec, spliced, com))   # (records only)
        if not self.records:
            return
        self._rec("values", [fr.vid, key, t_end, fr.reg_round, fr.verdict_round,
                             (fr.verdict_round - t_end) if fr.verdict_round >= 0 else None, int(bool(v.truth)), how, w["slot"],
                             None if com is None else com["b"], None if com is None else com["outcome"], B, R, round(Lc, 3), ev])
        if com is not None:
            self._rec("calib", [fr.vid, com["c"], com["pi"], int(bool(v.truth)), com["outcome"]])

    def censor(self):
        """At the cut, once: the 16-bit age of the values still registered, and (records) their value records, censored."""
        if self._censored:
            return
        self._censored = True
        dev = self.dev
        for vid in sorted(dev._live_frames):
            fr = dev._frames[vid]
            if fr.w2 is not None:
                self.age_check(fr)
                self.value_end(fr, "censored")
                self.stats["values_censored"] += 1

    def summary(self) -> dict:
        self.censor()
        opts = {k: v for k, v in self.opts.items() if k not in REPAIR or v != W2_DEFAULTS[k]}   # (16: as before when off)
        out = dict(options=opts, stats={k: v for k, v in sorted(self.stats.items()) if v})
        if self.r2 is not None:
            out["r2"] = dict(file=os.path.basename(self.r2.path), sha256=self.r2.sha, frozen=self.r2.frozen, **(self.r2w or {}))
        if self.xi is not None:
            out["xic"] = dict(file=os.path.basename(self.xi.path), sha256=self.xi.sha, frozen=self.xi.frozen, **(self.xiv or {}))
        if self.holdv is not None:                                        # (plan v2 5.1: the H variant's H)
            out["hold"] = dict(file=os.path.basename(self.hold_path), **self.holdv)
        if self.cov is not None:                                          # (plan v2 2.2-2.3: the price and the margin)
            out["price"] = dict(**({} if self.cof is None else dict(file=os.path.basename(self.cof.path), sha256=self.cof.sha,
                                                                     frozen=self.cof.frozen)), **self.cov)
        if self.sdf is not None:                                          # (start-delay note v2 2.3: the frozen constants)
            out["sd"] = dict(file=os.path.basename(self.sdf.path), sha256=self.sdf.sha, frozen=self.sdf.frozen,
                             **(self.sdv or ({"off": True} if self.sdf.off else {})))   # (off at the run's d, version 2)
        if self.vqf is not None:
            out["vq"] = dict(file=os.path.basename(self.vqf.path), sha256=self.vqf.sha, frozen=self.vqf.frozen, edges=self.vqf.edges,
                             m=self.vqm, margin=self.opts["vq"], d0=bool(self.opts["vqd0"]))
        if self.rec7:
            out["r7_records"] = dict(
                decision_fields=["round", "vid", "e", "c", "l", "b", "q", "half", "r0", "r", "act", "back"],
                admission_fields=["round", "vid", "slot", "l", "b", "q", "V0", "J_N", "m", "admitted"] + (["r"] if self.vqr else []),
                recovery_fields=["vid", "k", "lane", "l", "b", "q", "q_end", "recovery_end_round", "done_round", "XS_k", "Delta", "shortfall"]
                + (["form"] if self.tab is not None and any("form" in pr for pr in self.tab.profiles.values()) else []),   # (sx only)
                shadow_fields=["vid", "rejected_round", "forward_round", "age", "c", "l", "b", "action"],
                **({"shadow_c_fields": ["vid", "rejected_round", "commit_round", "age", "c", "l", "b", "order", "m", "truth"]} if self.r7d else {}),
                **{k[3:]: self.rec[k] for k in ("r7_decisions", "r7_admission", "r7_recoveries", "r7_shadow", "r7_shadow_c") if k in self.rec})
        if self.tab is not None:
            out["tables"] = self.tab.summary()
            out["profiles"] = self.prov
        if self.lam is not None:
            out["lambda"] = dict(sha256=self.lam.sha, frozen=self.lam.frozen, budget_level=self.level, entries=len(self.lam.entries))
            out["prior"] = dict(sha256=self.prior_sha, frozen=self.prior_frozen)
        out["lookups_per_round_max"] = self.lk_max
        out["age_max_rounds"] = self.age_max
        if self.records:
            out["records"] = dict(decision_fields=list(self.DECISION_FIELDS), decisions=self.rec["decisions"],
                                  commits=self.rec["commits"],
                                  value_fields=["vid", "key", "t_end", "reg_round", "verdict_round", "verdict_age", "m", "how",
                                                "slot", "commit_b", "commit_outcome", "B", "R", "Lc", "events"],
                                  values=self.rec["values"], calib_fields=["vid", "c", "pi", "m", "outcome"], calib=self.rec["calib"],
                                  error_fields=["round", "vid", "kind", "load_bin", "queue"], errors=self.rec["errors"])
        return out


def _nci_stop(fr) -> int:
    """D-nci's stop on the frame's current walk (16.11): rule no_cost_increase's loop in `_defer_register` and `_h_rebase`
    on the same frame, the first feasible boundary past which the correction ladder r rises, else the top."""
    js = [j for j in range(fr.top + 1) if fr.feas[j]]
    for a, b in zip(js, js[1:]):
        if fr.r[b] > fr.r[a]:
            return a
    return fr.top


def _never() -> bool:
    return False


def _first(order: str, adv_ok: bool, com_ok) -> str:
    """The first eligible action of an order (WAIT is always eligible; COMMIT's check runs only if reached)."""
    for a in order:
        if a == "A":
            if adv_ok:
                return "A"
        elif a == "W":
            return "W"
        elif com_ok():
            return "C"
    return "W"


def _queue_over(dev, dec) -> bool:
    """COMMIT's queue check: rule 30's wait for the full-decode windows ahead (their mu, Q24.8) exceeds Q_MAX rounds. The
    sum stops at the first prefix whose wait exceeds it; every mu is positive and the wait is monotone in the sum, so the
    answer is the full sum's (s2-slow: the full sum was O(backlog) per check, 85% of a starved run's time at d = 15)."""
    busy, lim, ahead = dev._u_busy(dec), Q_MAX << 8, 0
    for t in dec._pending_decode_tasks:
        if dec._get_task_or_none(t) is not None:
            ahead += dev._u_task_mu(dec, t)
            if dev._u_wait(dec, ahead, busy) > lim:
                return True
    return dev._u_wait(dec, ahead, busy) > lim


def _link() -> dict:
    from .device import LINK
    return LINK
