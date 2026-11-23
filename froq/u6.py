"""U v6 (round, gap analysis `` on `scoutq`, rules 50-52 and 56): the carry rule by time, U's new action inputs, the closed-loop u_xs and the attribution records.

One object per run (`ScoutDeviceManager._u6`), made by `_u_setup` when an arm's `defer.unified` carries a `v6` dict. Its
options, each a configuration of the one controller (froq/arms.py names them):

  kappa_c rule 50a's c (> 0): kappa = c x mu(v), the price of the pool's shortfall in rounds per core, with mu(v) the
             mean full-decode service time of the value's class, read from the latency table (`mu_class`)
  H rule 50's hold cost: "0", "d/4" or "d/2" rounds
  mhat0 51c: m_hat = 0 predictions are registered too, for the carry decision only (p_corr = 1 - c)
  q_wait 51a, as corrected by rule 50b: the queue check passes if the queue is empty (U v4's snapshot) or its
             expected wait at the request is at most act_lat (the golden's `q_wait`; never stricter than U v4's check)
  eta_floor 51b: eta = max(ETA, t_end + Lambda) while the verdict's decode windows are not all built
  repair Eq. (1)'s repair terms (the tables' XS and XR, the golden's `repair_terms`)
  loop rule 52: "closed" (the scheduler reads U's exported u_xs, tagged with its source round, with the stale veto;
             each pass's timing is model of the RTL, `froq/u6_timing.py`) or "instant" (the
             scheduler's own outstanding sum, at once)
  client False: U6-off -- no prediction, no REGISTER, no request, no strip job, p_corr = the base rate (rule 50d's
             prior; U's ETA file still runs)
  w, pcorr, reconsider
             "zero", "one" and False reproduce U v4's carry decision (rule 50's exact case: W = 0, H = 0, p_corr = 1, no
             reconsideration); defaults "time", "client" and True
  theta T6 (rule 54): the threshold c >= theta in place of U's score (the golden's `theta`)
  slack, cs, stop0_cleanup
             rule 50c: "ewma" (slack_f, the forecast) or "instant" (U v4's slack at registration); C_s on or off; the
             stop-0 cleanup of the arms whose client is off (U6-off). U v4's conditions take "instant", False, False.

The carry decision (rule 50, as rule 50c prices the lazy walk), at the frame's registration, in the scheduler, from its
own inputs: the frame takes the lazy walk iff

    W(v) >= H + C_q(v) + C_s(v),
    W(v) = max(0, eta(v) - t_nci(v)) - max(0, eta(v) - t_f(v)),
    C_q(v) = kappa(v) * max(0, h * p_corr(v) * demand_lazy(v) - slack_f(v)), kappa(v) = c * mu(v) (rule 50a)
    C_s(v) = w_s * p_corr(v) * max(0, (S_f(v) + XS_f(v)) - (S_0(v) + XS_0(v))), w_s in {0, 0.5, 1} (rule 50d)

with t_nci the round D-nci's walk would reach its stop and t_f the round the lazy walk would reach its fence (its top),
both under the static schedule (`_reach`: the members' causal lower bounds plus their schedule durations, from the
decision round; never a sampled duration), eta U's verdict-round estimate with 51b's floor, slack_f the slack
forecast B - busy_ewma - the reservations' decode work not yet started - u_xs (busy_ewma: the pool's busy cores averaged
with a time constant of d rounds, `tick_busy`), and S + XS the script's execution and settlement at the fence (f) and at
D-nci's stop (0), from the tables (`UTables.script_cost`). p_corr is c for an installed m_hat = 1, 1 - c for an
m_hat = 0 under 51c, and without a prediction rule 50d's prior P(m = 1) of the value's (d, producer decode class), fitted
on seed-1 D-nci runs and frozen (`froq/data/u6_prior.json`, `prior_of`). Without an eta (invalid, stale or capacity-limited windows,
which 51b does not floor) W and H are dropped: lazy iff slack_f >= h * p_corr * demand_lazy. A stale u_xs (closed loop:
older than U_STALE_D x d rounds from its source round) vetoes the lazy walk before the test, and the frame may not be
reconsidered. One reconsideration: when a prediction is installed after the decision, a frame on D-nci's walk whose
value is unresolved and which has not materialized takes the test again, once, on the remaining path, with the slack
of that round; if it goes lazy, its lazy demand is reserved then. A lazy frame never goes back.

Lambda (51b): the median verdict latency t_verdict - t_end, per verdict class and (d, B), from `froq/data/u6_lambda.json`
(fitted on seed-1 D-nci runs; a stub until then, which floors nothing). The verdict class is the producer's decode
class (rule 50a): its instruction's name, patch count and duration in rounds. It is not U's site class: that class
names the frame's walk (rule 44a), which the carry decision is about to choose. mu(v), the class's mean full-decode
service time (rule 50a), lays the producer instruction's decode windows out as the tables lay out a script's
(`u_tables.script_windows`: chunks of d rounds, one window per merged region or per patch, each with its forward
buffer) and averages the latency table's mean service of their volume classes (`_u_mu`, clamped to the largest class)."""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os

from . import u6_timing
from .device import H_INSTALL, LINK

U6_DEFAULTS = dict(kappa_c=1.0, H="0", mhat0=False, q_wait=True, eta_floor=True, repair=True, loop="closed", client=True,
                   w="time", pcorr="client", reconsider=True, theta=None, slack="ewma", cs=True, stop0_cleanup=True, ws=1.0)
U6_H = ("0", "d/4", "d/2")
# rule 50b's diagnosis, counterfactuals only, never a candidate's option: cf_stop0 leaves undeferred, as D-nci
# does, a value whose frame takes D-nci's walk and stops at boundary 0 (U v4 keeps that frame so that E2 can act at 0);
# cf_nolazy refuses the lazy walk (the carry decision is always D-nci's walk).  Off by default and absent from the
# options record unless on, so no other arm's result changes.
U6_CF = ("cf_stop0", "cf_nolazy")
# round 6 (amendment 1), exploratory, never a candidate's option: xi0 prices Eq. (1)'s XS and XR at 0 in the commit score
# alone (the golden's `xi0`; the tables, classes and records keep them); like cf_*, it enters the options only when on
U6_R6 = ("xi0",)
LAMBDA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "u6_lambda.json")
PEAK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "u6_peak_demand.json")
PRIOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "u6_prior.json")
BUDGET_LEVELS = ("0.5P", "0.75P", "P", "1.5P")
LEVEL_FACTOR = {"0.5P": 0.5, "0.75P": 0.75, "P": 1.0, "1.5P": 1.5}
GUARDED_SEEDS = (1, 28, 30, 34, 35, 36)  # rule 53: the selection's seed and the evaluation's (fixed)
U6_LOG_MAX = 200000                      # each attribution record kept in the summary (rule 56), per kind; past it a
                                         # record is dropped and counted (`records:truncated:<kind>`)
# rule 56a's per-value classes: requested, or the reason a value never requested an ACT -- the plan's six (no
# prediction, a late prediction, m_hat = 0, an unsupported residual, a fence, an overlapping cone) and no request (a
# registered value never asked: E1's reason, E2 never reached); "unrecorded" where the run kept no reason (counted)
VALUE_CLASSES = ("requested", "no_prediction", "late_prediction", "mhat0", "unsupported_residual", "fence", "overlapping_cone",
                 "no_request", "unrecorded")
# rule 56a's refusal causes, in the order the record reports the first one
REFUSAL_ORDER = ("no_prediction", "unsupported_residual", "fence", "overlapping_cone", "no_workspace", "queue", "headroom",
                 "eta", "capacity", "gain")
E1_DEFERRED = "deferred"                 # an E1 the unit answers net2: no refusal, the ACT waits for E2 (request records)


def check_budget_level(bench, d, level, budget, decode_scale=1.0):
    """(ok, why): --budget-level against --budget (the code gate's MED 4). B must be ceil(level x P), P the program's
    peak decoder demand at d (seed-1 stages, `froq/data/u6_peak_demand.json`); a program the file does not hold
    is unchecked (ok None). X16 (reliability-.md 6.1): under --decode-scale X > 1, B / X must be (B = 183 at
    X = 3 is P's 61 for QPE-10 at d = 17): there the level names the capacity. (part B; F1,
    option (ii)): under --decode-scale X < 1, B itself must be, as at X = 1 (the same hardware, the cell's own level and
    its frozen fits; B = 69 at X = 0.5 and 0.25 is 1.5P of QFT-10's 46 at d = 13): below 1 the level names the cores."""
    doc = json.load(open(PEAK_FILE))
    P = (doc.get("P") or {}).get("%s:%d" % (bench, int(d)))
    if P is None:
        return None, "no peak demand for %s at d = %d" % (bench, d)
    want = int(math.ceil(LEVEL_FACTOR[level] * int(P)))
    x = float(decode_scale)
    if x == 1.0:
        return (budget is not None and int(budget) == want), "B = %s, but %s of P = %d is %d" % (budget, level, P, want)
    if x < 1.0:
        return ((budget is not None and int(budget) == want),
                "B = %s at X = %g (below 1 the level names the cores), but %s of P = %d is %d" % (budget, x, level, P, want))
    return ((budget is not None and abs(int(budget) / x - want) < 1e-9),
            "B / X = %s / %g, but %s of P = %d is %d" % (budget, x, level, P, want))


class _Table(dict):
    """A frozen table keyed by verdict class, with its file (`path`) and, for the prior, each class's counts (`counts`:
    (m = 1 values, values)), which the rule for an unseen class reads (`unseen_class`)."""
    path, counts = None, None


RARE_N = 100       # a class with fewer resolved values than this is rare (the decision's Lambda pools them as its "rare" key)


def vclass_parts(vclass: str):
    """(name, merge size, duration) of a verdict class "NAME/patches/duration"."""
    name, m, dur = vclass.split("/")
    return name, int(m), dur


def unseen_class(seen: dict, vclass: str):
    """The rule for a verdict class the frozen fits never saw, wherever an arm reads Lambda or the prior: `seen` maps the classes seen at one (d, level) (the prior: one
    d) to their statistic (Lambda's median verdict latency; the prior's P(m = 1)). Decoding time grows with the merge,
    so when that statistic never falls as the merge grows within the class's family (its name and duration), the
    unseen class takes the largest seen merge at or below it: (that class, "nearest"); otherwise (None, "rare"), and
    the caller takes the (d, level)'s rare pool. A class whose family the fits never saw at all is not covered: (None,
    "none"), and the caller fails the run as before (and LOW 3)."""
    name, m, dur = vclass_parts(vclass)
    fam = sorted((vclass_parts(c)[1], c) for c in seen if vclass_parts(c)[0] == name and vclass_parts(c)[2] == dur)
    if not fam:
        return None, "none"
    vals = [seen[c] for _m, c in fam]
    below = [c for mm, c in fam if mm <= m]
    if below and all(a <= b for a, b in zip(vals, vals[1:])):
        return below[-1], "nearest"
    return None, "rare"


def _w2_sibling(path: str) -> str:
    """the decision's Lambda file fitted with U v6's (the same seed-1 D-nci references): u6_lambda*.json -> wager2_lambda*.json."""
    d, b = os.path.split(path)
    assert b.startswith("u6_lambda"), path
    return os.path.join(d, "wager2_lambda" + b[len("u6_lambda"):])


def lambda_unseen(tab, d: int, level: str, vclass: str):
    """(Lambda, how) for a class U v6's frozen Lambda lacks (`unseen_class`): the nearest seen class's, or the (d, level)'s
    rare pool's median verdict latency. U v6's file keeps one median per class and no pool, so the pool's median is the
    rare key's of the decision's Lambda fitted on the same references (its "q" at 0.5: the classes with fewer than RARE_N
    values at that (d, level))."""
    seen = {c: v for (dd, lv, c), v in tab.items() if dd == int(d) and lv == level}
    c, how = unseen_class(seen, vclass)
    if c is not None or how == "none":
        return (seen[c] if c is not None else None), how
    doc = json.load(open(_w2_sibling(tab.path)))
    for e in doc.get("entries", []):
        if int(e["d"]) == int(d) and str(e["level"]) == level and e["key"] == "rare":
            return float(e["q"]["0.5"]), how
    raise AssertionError("U v6, 51b: no rare pool at %r in %s" % ((d, level), _w2_sibling(tab.path)))


def prior_unseen(tab, d: int, vclass: str):
    """(P(m = 1), how) for a class the frozen prior lacks (`unseen_class`): the nearest seen class's, or the pooled rare
    classes' at d (the m = 1 share of every value of the classes with fewer than RARE_N values)."""
    seen = {c: v for (dd, c), v in tab.items() if dd == int(d)}
    c, how = unseen_class(seen, vclass)
    if c is not None or how == "none":
        return (seen[c] if c is not None else None), how
    m1 = n = 0
    for (dd, c_), (a, b) in tab.counts.items():
        if dd == int(d) and b < RARE_N:
            m1, n = m1 + a, n + b
    assert n > 0, ("U v6, 50d: no rare pool at d = %d" % d)
    return m1 / float(n), how


def lambda_table(path=None):
    """(the table {(d, budget level, verdict class): Lambda in rounds}, its provenance and sha256) from the data file."""
    path = path or LAMBDA_FILE
    raw = open(path, "rb").read()
    doc = json.loads(raw)
    tab = _Table()
    tab.path = path
    for e in doc.get("entries", []):
        tab[(int(e["d"]), str(e["B"]), str(e["vclass"]))] = float(e["lambda"])
    return tab, doc.get("provenance", {}), hashlib.sha256(raw).hexdigest()


LAMBDA_MISMATCH_SCALES = (0.8, 1.25)     # round 7: the decoding speeds a Lambda fitted at another speed may serve


def lambda_scale_of(path) -> float:
    """The decoding speed a Lambda file was fitted at: its provenance's `full_decode_scale` (the fit scripts'
    --full-decode-scale; a file without one was fitted at 1)."""
    with open(path) as f:
        return float((json.load(f).get("provenance") or {}).get("full_decode_scale", 1.0))


def lambda_scale_ok(path, full_decode_scale, flag, mismatch=False):
    """a Lambda file named by path (--u6-lambda, --w2-lambda) must have been fitted at the run's decoding speed.
    Before a path was refused under --full-decode-scale and not checked at 1; no run that accepted changes.
    Round 7's --lambda-mismatch takes a file fitted at another speed, at decode x0.8 and x1.25 only."""
    got = lambda_scale_of(path)
    assert got == float(full_decode_scale) or (mismatch and float(full_decode_scale) in LAMBDA_MISMATCH_SCALES), \
        "%s %s was fitted at --full-decode-scale %g, the run has %g" % (flag, path, got, float(full_decode_scale))
    return path


def lambda_file_for(full_decode_scale=1.0, path=None, mismatch=False):
    """The Lambda file a run reads: `path` (--u6-lambda; None, the default file) with the decoding speed off;
    under rule 57's --full-decode-scale X, the variant's own file, `u6_lambda_fds<X>.json`, fitted on D-nci references
    run under that switch (froq/tools/u6_lambda_fit.py --full-decode-scale), so that U's verdict estimates come from the
    decoder the run has. Refused: an X without a fitted file; --u6-lambda is taken at any X if fitted at X; round 7's
    --lambda-mismatch: at X = 0.8 or 1.25, `path` or the default file whatever its speed."""
    if path is not None:                 # (an explicit file -- the hold's fits are passed by path -- of the run's speed)
        return lambda_scale_ok(path, full_decode_scale, "--u6-lambda", mismatch)
    if float(full_decode_scale) == 1.0 or (mismatch and float(full_decode_scale) in LAMBDA_MISMATCH_SCALES):
        return path
    f = os.path.join(os.path.dirname(LAMBDA_FILE), "u6_lambda_fds%s.json" % ("%g" % float(full_decode_scale)).replace(".", "p"))
    assert os.path.exists(f), "no Lambda fitted for --full-decode-scale %g: %s" % (float(full_decode_scale), f)
    return f


def prior_table(path=None):
    """Rule 50d's prior: (the table {(d, producer decode class): P(m = 1)}, its provenance and sha256) from the data file
    (`froq/tools/u6_prior_fit.py`)."""
    path = path or PRIOR_FILE
    raw = open(path, "rb").read()
    doc = json.loads(raw)
    tab = _Table({(int(e["d"]), str(e["vclass"])): float(e["p"]) for e in doc.get("entries", [])})
    tab.path = path
    tab.counts = {(int(e["d"]), str(e["vclass"])): (int(e.get("m1", 0)), int(e.get("n", 0))) for e in doc.get("entries", [])}
    return tab, doc.get("provenance", {}), hashlib.sha256(raw).hexdigest()


def noframe_class(why: str) -> str:
    """Rule 56a: a value that got no frame, by the deferral registration's refusal reason (`_defer_native`'s key)."""
    if why == "decoded_before_registration":
        return "late_prediction"                         # (its verdict came first: no prediction could be in time)
    if any(s in why for s in ("fence_on_admitted_step", "fence_on_prepared_rebase", "fence_cuts_frame", "blocks_frame_workspace",
                              "executed_cone_work")):
        return "overlapping_cone"                        # (its walk would hold, or be held by, another live frame's)
    if why.startswith("no_step:") and any(s in why for s in ("fence", "measurement", "nomeas", "output_commit")):
        return "fence"                                   # (its contract ends at a fence before any step)
    if why.startswith("no_step:") or why in ("no_contract", "no_cached_placement"):
        return "unsupported_residual"                    # (no placed, usable boundary for its residual)
    return "unrecorded"                                  # (never defaulted: counted, records:unrecorded)


class U6State:
    """U v6's options and state for one run (see the module's docstring)."""

    def __init__(self, dev, opts: dict, instr: bool = False, lambda_path=None, budget_level=None, prior_path=None):
        o = dict(U6_DEFAULTS)
        cf = {k: bool((opts or {}).get(k, False)) for k in U6_CF + U6_R6}
        o.update({k: x for k, x in (opts or {}).items() if k not in U6_CF + U6_R6})
        unknown = set(o) - set(U6_DEFAULTS)
        assert not unknown, ("U v6: unknown options", sorted(unknown))
        self.dev = dev
        self.kappa_c = float(o["kappa_c"])
        assert self.kappa_c > 0, "rule 50: kappa > 0"
        assert str(o["H"]) in U6_H, o["H"]
        self.H_name = str(o["H"])
        self.H = {"0": 0.0, "d/4": dev.d_t / 4.0, "d/2": dev.d_t / 2.0}[self.H_name]
        self.mhat0, self.q_wait, self.eta_floor, self.repair = bool(o["mhat0"]), bool(o["q_wait"]), bool(o["eta_floor"]), bool(o["repair"])
        assert o["loop"] in ("closed", "instant"), o["loop"]
        self.loop = o["loop"]
        self.client = bool(o["client"])
        assert o["w"] in ("time", "zero") and o["pcorr"] in ("client", "one"), (o["w"], o["pcorr"])
        self.w_mode, self.pcorr_mode, self.reconsider = o["w"], o["pcorr"], bool(o["reconsider"])
        self.theta = None if o["theta"] is None else float(o["theta"])
        assert self.theta is None or 0.0 <= self.theta <= 1.0, o["theta"]
        assert o["slack"] in ("ewma", "instant"), o["slack"]
        self.slack_mode, self.cs, self.stop0_cleanup = o["slack"], bool(o["cs"]), bool(o["stop0_cleanup"])
        self.ws = float(o["ws"])                                        # rule 50d: C_s's critical-path share
        assert 0.0 <= self.ws <= 1.0, o["ws"]
        # rule 50d's prior: p_corr without an installed prediction, P(m = 1) of the value's (d, producer decode class);
        # read only where p_corr comes from the client (U v4's conditions take p_corr = 1)
        self.prior, self.prior_prov, self.prior_sha = ({}, {}, None)
        if self.pcorr_mode == "client":
            self.prior, self.prior_prov, self.prior_sha = prior_table(prior_path)
        self.prior_frozen = bool(self.prior_prov.get("frozen"))
        # rule 50c: busy_ewma, the pool's busy-core count averaged exponentially with a time constant of d rounds (alpha
        # = 1 - exp(-1/d) a round), started at the first round's count (`tick_busy`)
        self.alpha = 1.0 - math.exp(-1.0 / float(dev.d_t))
        self.busy_ewma, self._ewma_k, self._ewma_last = None, None, None
        self.cf_stop0, self.cf_nolazy = cf["cf_stop0"], cf["cf_nolazy"]
        self.xi0 = cf["xi0"]
        assert not self.xi0 or self.repair, "xi0 zeroes Eq. (1)'s repair terms in the score: repair must be on"
        self.opts = dict(o, **{k: True for k, x in cf.items() if x})
        self.instr = bool(instr)
        self.stats = collections.Counter()
        # 51b: Lambda, and the run's budget level that keys it
        self._unseen_lam, self._unseen_prior, self._unseen_how = {}, {}, {}  # (classes the fits never saw)
        self.lam, self.lam_prov, self.lam_sha = ({}, {}, None)
        self.budget_level = budget_level
        if self.eta_floor:
            self.lam, self.lam_prov, self.lam_sha = lambda_table(lambda_path)
            assert budget_level in BUDGET_LEVELS, ("51b keys Lambda by the run's budget level", budget_level)
        self.lam_frozen = bool(self.lam_prov.get("frozen"))   # a frozen Lambda must cover every key
        self._vclass_of: dict = {}
        self._mu_of: dict = {}                           # verdict class -> mu (rule 50a)
        # rule 52: PassClock chains the passes; the closed loop's u_xs exports, (first round of use, pass k,
        # value), oldest first, and the latest usable with its source round k - 1; and, for the carry decision's eta,
        # the ETA file as each pass left it, (first round it can be read, pass k, the file), and the latest completed
        # one (the code gate's MED 3)
        self.closed = self.loop == "closed"
        self.needs_eta = self.w_mode == "time"           # (U v4's conditions read no eta)
        self.needs_clock = self.closed or self.needs_eta
        self._xs = collections.deque()
        self._xs_seen = (0, None)                        # (value, its source round k - 1) of the latest usable export
        self._files = collections.deque()
        self._file_seen = None                           # (pass k, alive, valid, verified, eta) of the last completed pass
        self._file_last = None                           # the file as the last pass left it (shared while unchanged)
        self._clock = u6_timing.clock()
        self._pass_k = None                              # the last pass's k
        self._lag_keys = ["pass:lag_rounds:%d" % i for i in range(10)]
        self.timing = u6_timing.describe()
        from .device import _golden_u
        self._eta_f = _golden_u().ETA_F
        # rule 56's records (when `instr`)
        self.rec = collections.defaultdict(list)
        self._last_decision = None                       # (vid, kind, rule, term) of the last carry decision

    def unit_inputs(self):
        """U v6's inputs to U's unit, for the replay records' header (rule 50s runs every candidate with --u-replay), or
        None when none is on (U v4's conditions: U v4's format, bit for bit)."""
        if not (self.q_wait or self.eta_floor or self.repair or self.mhat0 or self.theta is not None):
            return None
        return dict(q_wait=self.q_wait, eta_floor=self.eta_floor, repair_terms=self.repair, mhat0=self.mhat0, theta=self.theta,
                    loop=self.loop, timing=self.timing, budget_level=self.budget_level, lambda_sha=self.lam_sha)

    # ------------------------------------------------------------------ inputs
    def vclass(self, v) -> str:
        """The verdict class of v (51b): its producer's instruction name, patch count and duration in rounds."""
        x = self._vclass_of.get(v.producer_idx)
        if x is None:
            dev = self.dev
            si = dev.schedule_instructions[v.producer_idx]
            x = self._vclass_of[v.producer_idx] = "%s/%d/%d" % (si.instruction.name, len(si.instruction.patches),
                                                                int(dev._true_durations[v.producer_idx]))
        return x                                         # (the schedule's duration, as froq/tools/u6_lambda_fit.py reads it)

    def mu_class(self, v) -> float:
        """Rule 50a's mu(v): the mean full-decode service time, in rounds, of the value's class (the producer's decode class,
        51b's key), read from the latency table: the producer instruction's decode windows laid out as the tables lay out
        a script's (`u_tables.script_windows`), each window's mean service by the table's class rule (`_u_mu`),
        averaged.  One number per class and run."""
        key = self.vclass(v)
        mu = self._mu_of.get(key)
        if mu is None:
            from types import SimpleNamespace
            from .u_tables import script_windows
            dev = self.dev
            ws, _end = script_windows(SimpleNamespace(instrs=[dev.schedule_instructions[v.producer_idx].instruction]),
                                      int(dev.d_t), 0, dev._u_mu)
            mu = self._mu_of[key] = (sum(m for _a, m in ws) / len(ws)) if ws else float(dev._u_mu(2 * int(dev.d_t)))
        return mu

    def lam_of(self, v):
        """Lambda of v's key (d, budget level, producer decode class); None without 51b or with the stub. A frozen Lambda
        that lacks the key fails the run (the selection and the evaluation never meet a key
        the fit missed; froq/tools/u6_lambda_fit.py checks the coverage beforehand)."""
        if not self.eta_floor:
            return None
        key = (int(self.dev.d_t), self.budget_level, self.vclass(v))
        lam = self.lam.get(key)
        if lam is None and self.lam_frozen:
            lam = self._unseen_lam.get(key)                                # (an unseen class: `check_lambda`'s rule)
            if lam is None:
                raise AssertionError("U v6, 51b: the frozen Lambda (sha256 %s) has no entry for %r" % (self.lam_sha, key))
        return lam

    def check_lambda(self):
        """At the run's start (the re-check's LOW 3): a frozen Lambda must have the key of every value this run can meet,
        (d, budget level, producer decode class), so a gap fails the run at once instead of when the key is first
        needed, possibly hours in."""
        if not (self.eta_floor and self.lam_frozen):
            return
        d = int(self.dev.d_t)
        keys = [(d, self.budget_level, self.vclass(v)) for v in self.dev.values.values()]
        for key in sorted({k for k in keys if k not in self.lam and k not in self._unseen_how}):
            self._unseen_lam[key], self._unseen_how[key] = lambda_unseen(self.lam, d, self.budget_level, key[2])
        missing = sorted({k for k in keys if k not in self.lam and self._unseen_how[k] == "none"})
        assert not missing, "U v6, 51b: the frozen Lambda (sha256 %s) lacks %d key(s) this run needs: %s" % (
            self.lam_sha, len(missing), missing[:8])
        st = getattr(self.dev, "_u_stats", None)                          # (a merge size the fit never saw takes the frozen
        for key in keys:                                                   #  rule: counted per value and class)
            if key not in self.lam and st is not None:
                st["lambda_unseen_values"] += 1
                st["lambda_unseen:%s:%s" % (key[2], self._unseen_how[key])] += 1

    def installed(self, v, fr):
        """The decision client's prediction (m_hat, c) in use now under H's install rule, or None."""
        if not self.client:
            return None
        dev = self.dev
        pr, m, c, _rc = dev._dview(v)
        if pr < 0 or m is None:
            return None
        t0 = fr.reg_round if pr <= fr.reg_round else pr + H_INSTALL
        return (bool(m), float(c), t0) if dev.current_round >= t0 else None

    def pred_at_registration(self, v, fr):
        """The prediction in use at the frame's registration (rule 50's prediction registration), (m_hat, c), or None:
        none published by then, or an m_hat = 0 without 51c."""
        got = self.installed(v, fr)
        if got is None:
            return None
        m, c, _t0 = got
        return (m, c) if (m or self.mhat0) else None

    def p_corr(self, fr) -> float:
        """Rule 50's p_corr: c for an installed m_hat = 1, 1 - c for an m_hat = 0 registered under 51c; without one, rule
        50d's prior (`prior_of`); 1 under U v4's conditions."""
        if self.pcorr_mode == "one":
            return 1.0
        if fr.u6_pred is None:
            return self.prior_of(fr.vid)
        m, c = fr.u6_pred
        return c if m else 1.0 - c

    def prior_of(self, vid) -> float:
        """Rule 50d: P(m = 1) of the value's (d, producer decode class) from the frozen prior; 1 (rule 50's p_corr) with no
        entry in a prior that is not frozen (the stub); a frozen prior that lacks the key fails the run."""
        if not self.prior:
            return 1.0
        key = (int(self.dev.d_t), self.vclass(self.dev.values[vid]))
        p = self.prior.get(key)
        if p is None:
            if self.prior_frozen:
                p = self._unseen_prior.get(key)                            # (an unseen class: `check_prior`'s rule)
                if p is None:
                    raise AssertionError("U v6, 50d: the frozen prior (sha256 %s) has no entry for %r" % (self.prior_sha, key))
                return p
            return 1.0
        return p

    def check_prior(self):
        """At the run's start, as for Lambda (the re-check's LOW 3): a frozen prior must have every value's key."""
        if not (self.prior and self.prior_frozen):
            return
        d = int(self.dev.d_t)
        keys = [(d, self.vclass(v)) for v in self.dev.values.values()]
        for key in sorted({k for k in keys if k not in self.prior and k not in self._unseen_how}):
            self._unseen_prior[key], self._unseen_how[key] = prior_unseen(self.prior, d, key[1])
        missing = sorted({k for k in keys if k not in self.prior and self._unseen_how[k] == "none"})
        assert not missing, "U v6, 50d: the frozen prior (sha256 %s) lacks %d key(s) this run needs: %s" % (
            self.prior_sha, len(missing), missing[:8])
        st = getattr(self.dev, "_u_stats", None)
        for key in keys:
            if key not in self.prior and st is not None:
                st["prior_unseen_values"] += 1
                st["prior_unseen:%s:%s" % (key[1], self._unseen_how[key])] += 1

    def roots(self, v):
        """(roots, unbuilt, why, windows) of v's verdict for U's ETA file: the slots of the producer's unverified windows
        with no successor among them, and those windows.  With some of the producer's rounds in no window yet (unbuilt),
        the built windows' roots, possibly none; a window outside the file, or more than four roots, leaves no roots
        (capacity, rule 41a)."""
        dev = self.dev
        dec = LINK.get("decoder")
        p = v.producer_idx
        if dec is None:
            return [], False, "no_decoder", []
        unbuilt = bool(dev._u_unbuilt_parts(p))
        T = [t for t in sorted(dec._instruction_tasks.get(p, ())) if not dec._is_verified_task(t, treat_none_as_true=True)]
        if not T:
            return [], unbuilt, ("unbuilt" if unbuilt else "none_unverified"), []
        if not all(dev._u_in_file(t) for t in T):
            return [], False, "outside_file", []
        Ts, g = set(T), dec._window_idx_dag
        ws = [t for t in T if not any(y in Ts for y in g.successors(t))]
        if len(ws) > 4:
            return [], False, "over_4", []
        return [dev._u_slot[t] for t in ws], unbuilt, ("unbuilt" if unbuilt else "built"), ws

    def eta(self, v):
        """(eta in rounds or None, the register file's own eta or None, why): U's verdict-round estimate for the carry
        decision, from the ETA file as the last completed pass left it (the code gate's MED 3: in round r the scheduler
        reads at cycle t_dec, before the pass that runs in round r ends), with 51b's floor while the windows are not
        all built.  A root window whose NEW that pass had not yet taken is not in the file it reads: unavailable."""
        dev = self.dev
        uu = dev._uu
        now = dev.current_round
        roots, unbuilt, why, ws = self.roots(v)
        if why in ("outside_file", "over_4", "no_decoder"):
            return None, None, why
        if why == "none_unverified":
            return float(now + uu.path), float(now + uu.path), why             # every window verified: the verdict is due
        raw = None
        if roots:
            f = self._file()
            if f is None:
                return None, None, "no_pass"
            k, alive, valid, verified, etas = f
            newr = dev._u_newr
            if any(newr.get(t, k) > k - 1 for t in ws):
                return None, None, "not_in_file_yet"                          # (its NEW is in a pass not yet completed)
            ef = self._eta_f
            e = now << ef
            for r in roots:
                if not alive[r] or not valid[r]:
                    return None, None, "invalid"
                if not verified[r]:
                    e = max(e, etas[r])
            raw = (e + (uu.path << ef)) / float(1 << ef)
        if not unbuilt:
            return raw, raw, why
        lam = self.lam_of(v)
        if lam is None:
            return None, raw, "unbuilt_no_lambda"
        floor = dev.schedule_instructions[v.producer_idx].end_round + lam
        base = raw if raw is not None else float(now + uu.path)
        return max(base, floor), raw, "floored"

    def request_fields(self, v) -> dict:
        """51b's fields of an EPOCH request: the built windows' roots and, while some are unbuilt, the floor t_end + Lambda
        (rounded up: the unit compares integral rounds). Unbuilt windows without a Lambda entry (the stub) send no roots,
        so the unit finds the ETA unavailable, as U v4 and rule 41a."""
        roots, unbuilt, why, _ws = self.roots(v)
        st = self.dev._u_stats
        st["roots:" + why] += 1
        out = dict(roots=roots, _roots_why=why)
        if unbuilt:
            lam = self.lam_of(v)
            if lam is not None:
                out["floor"] = int(math.ceil(self.dev.schedule_instructions[v.producer_idx].end_round + lam))
                st["roots:unbuilt_floored"] += 1
            else:
                out.update(roots=[], _roots_why="unbuilt_no_lambda")
                st["roots:unbuilt_no_lambda"] += 1
        return out

    def uxs(self):
        """(u_xs, its source round or None, stale): rule 52's closed loop -- the latest export of U's outstanding sum that
        is usable in this round (PassClock: the scheduler reads at cycle t_dec) -- or round 3's instantaneous sum.
        The source round is k - 1 for pass k, the round of its pass-start snapshot (u_xs_src, as the U v6 RTL latches
        it), and the age is measured from it: now - (k - 1), the pass's own latency included."""
        dev = self.dev
        if self.loop == "instant" or not dev._u_client:
            return (dev._u4_outstanding() if dev._u_client else 0), dev.current_round, False
        now = dev.current_round
        self._fold(now)
        x, src = self._xs_seen
        if src is None:
            return 0, None, False                                       # (no pass yet: nothing outstanding)
        return x, src, (now - src) > dev._uu.stale_max

    def pass_end(self, k: int, work: dict, eng: dict):
        """The unit's pass k (at the end of round k - 1), by model of the RTL (`froq/u6_timing.py`): work, the pass's
        window events by kind, requests, taps and the ETA file's work (`pass_cycles`'s inputs); eng, the round's engine
        messages (`engine_cycles`'s). PassClock chains the passes (phase A, then the pass one cycle after it or after
        the previous pass, one at a time): the pass's u_xs export -- the golden's, the potential excess of the requests
        of passes k - act_lat + 2.. k -- tagged with its source round k - 1, is usable from the first read at or after
        its export (4 cycles after the pass ends); the ETA file the pass leaves, from the first read at or after the
        pass's end (the code gate's MED 3). A read is at cycle t_dec of a round, so both are usable from round k +
        rounds_after(cycle), never in the round in which the pass ends unless it ends by that round's read. When a pass
        runs past a round's read, RTL read port waits (er_busy) and then returns the newer file, while this reads
        the file of the last pass completed by the read, one pass older: conservative, never a look ahead (the
        re-check's LOW 2)."""
        if not self.needs_clock:
            return
        if k == self._pass_k:                          # (the device's hook again in one round: only in the decode drain
            self.stats["pass:repeated"] += 1            #  after the program's end, where no decision reads either)
            return
        dev = self.dev
        uu = dev._uu
        now = dev.current_round
        self._fold(now)
        _start, end, _export, first = self._clock.step(k, u6_timing.engine_cycles(**eng), u6_timing.pass_cycles(**work))
        self._pass_k = k
        if self.closed and dev._u_client:
            h, x = uu.px_hist, 0
            for j in range(k - uu.act_lat + 2, k + 1):
                x += h.get(j, 0)
            self._xs.append((first, k, x))
            self.stats[self._lag_keys[min(9, first - k)]] += 1
        if self.needs_eta:
            if self._file_last is None or getattr(uu, "k_now", None) == k:     # (the golden ran pass k: a new file)
                self._file_last = (tuple(uu.alive), tuple(uu.valid), tuple(uu.verified), tuple(uu.eta))
            self._files.append((k + u6_timing.rounds_after(end), k, self._file_last))

    def _fold(self, now: int):
        """Take the exports and the ETA files usable by round `now`: the latest of each is what a decision reads."""
        xs, fs = self._xs, self._files
        while xs and xs[0][0] <= now:
            _f, k, x = xs.popleft()
            self._xs_seen = (x, k - 1)                 # (its source round)
        while fs and fs[0][0] <= now:
            _f, k, f = fs.popleft()
            self._file_seen = (k,) + f

    def _file(self):
        """(pass k, alive, valid, verified, eta) of the ETA file as the last completed pass left it, or None."""
        self._fold(self.dev.current_round)
        return self._file_seen

    def tick_busy(self, now: int, busy: int):
        """Rule 50c's busy_ewma, once a round from the scheduler's own count of the pool's busy cores (the slack's: the busy
        full decodes, and the strip jobs on a non-preemptive pool): it starts at the first round's count, then each round
        e += alpha (busy - e) with alpha = 1 - exp(-1/d), a time constant of d rounds; rounds the device did not visit
        held the last count (the closed form of the same update)."""
        if self.busy_ewma is None:
            self.busy_ewma, self._ewma_k, self._ewma_last = float(busy), now, busy
            return
        k = now - self._ewma_k
        if k <= 0:
            return
        e, last = self.busy_ewma, self._ewma_last
        if k > 1:
            e = last + (e - last) * (1.0 - self.alpha) ** (k - 1)
        self.busy_ewma = e + self.alpha * (busy - e)
        self._ewma_k, self._ewma_last = now, busy

    def slack_f(self, dev, uxs: int):
        """Rule 50c's slack forecast at a lazy frame's landing round: B - busy_ewma - the decode work not yet started of
        the reservations the pool counts (the ACTs' and the earlier lazy frames', `_u_unstarted`) - u_xs (the outstanding
        requests' potential excess, rule 52); U v4's instantaneous slack (`slack`) under slack = "instant".  None with
        unlimited decoders."""
        if self.slack_mode == "instant":
            return self.slack(dev, uxs)
        dec = LINK.get("decoder")
        c = dev._u_cores(dec) if dec is not None else None
        if not c:
            return None
        busy = self.busy_ewma
        if busy is None:                                               # (before the first round's count: the count now)
            busy = len(dec._active_window_progress)
            if dev._pool is not None and not dev._pool.preemptive:
                busy += dev._pool.busy(dec._current_round)
        return c - busy - dev._u_unstarted() - uxs

    def c_s(self, v, fr, s_nci: int, p: float) -> float:
        """Rule 50c's C_s(v), with rule 50d's share w_s: w_s p_corr(v) max(0, (S_f + XS_f) - (S_0 + XS_0)), the critical-path
        latency the lazy walk adds
        over D-nci's when a correction is needed -- the script's execution and settlement at the lazy frame's fence (its
        top) against D-nci's at its stop, from the tables' S and XS at those boundaries (`UTables.script_cost`); 0 when
        either has no record (counted)."""
        tab = self.dev._u_tab
        f, z = tab.script_cost(v.vid, fr.top), tab.script_cost(v.vid, s_nci)
        if f is None or z is None:
            self.stats["cs:no_record"] += 1
            return 0.0
        return self.ws * p * max(0, f - z)

    def slack(self, dev, uxs: int):
        """The pool's idle cores for the carry decision (rule 44a's, U v4): B less the busy full decodes (a strip job's
        core counts as idle under 42a), less the reservations the pool counts, less u_xs; None with unlimited decoders."""
        dec = LINK.get("decoder")
        c = dev._u_cores(dec) if dec is not None else None
        if not c:
            return None
        busy = len(dec._active_window_progress)
        if dev._pool is not None and not dev._pool.preemptive:
            busy += dev._pool.busy(dec._current_round)
        return c - busy - dev._u4_committed() - uxs

    # ------------------------------------------------------------------ the static schedule
    def reach(self, fr, b: int) -> int:
        """The round the frame's walk would reach boundary b under the static schedule, from now: every member of the
        steps before b complete -- an ended one at its end + 1, a running one at its start + duration, one not started
        at its causal lower bound (`_u_est` with the walk's limit at b) + its schedule duration; at least now."""
        dev = self.dev
        now = dev.current_round
        if b <= 0:
            return now
        est, preds, si, dur = {}, dev._adj()[0], dev.schedule_instructions, dev._instruction_durations
        t = now
        for i in sorted(i for k in range(min(b, len(fr.members))) for i in fr.members[k]):
            tp = si[i]
            if tp.end_round != -1:
                t = max(t, tp.end_round + 1)
                continue
            if i in dev._defer_started:
                t = max(t, tp.start_round + dur[i])
                continue
            e = now + (1 if i in fr.unadm else 0)
            for p in preds.get(i, ()):
                tq = si[p]
                if tq.end_round != -1:
                    q = tq.end_round + 1
                elif p in dev._defer_started:
                    q = tq.start_round + dur[p]
                elif p in est:
                    q = est[p] + dur[p]
                else:
                    q = now + dur[p]
                e = max(e, q)
            est[i] = e
            t = max(t, e + dur[i])
        return t

    # ------------------------------------------------------------------ rule 50's decision
    def decide(self, v, fr, s_nci: int, kind: str = "registration") -> str:
        """The carry rule by time (rule 50, as rule 50c prices the lazy walk): "lazy" iff W >= H + C_q + C_s, with C_q =
        kappa max(0, h p_corr demand_lazy - slack_f) and C_s the lazy walk's added script latency (`c_s`); recorded.
        Under U v4's conditions (W = 0, H = 0, p_corr = 1, C_s off, the instantaneous slack) it is U v4's rule."""
        dev = self.dev
        h = dev._u4_h
        if h is None:
            return "no_cost_increase"
        if h == 0:
            return "lazy"
        rec = dict(vid=v.vid, round=dev.current_round, kind=kind, pred=("none" if fr.u6_pred is None else int(fr.u6_pred[0])))
        if self.cf_nolazy:                                             # ('s counterfactual (b): no lazy walk)
            return self._decided(fr, "no_cost_increase", "cf_nolazy", rec)
        x, src, stale = self.uxs()
        if stale:
            fr.u6_noreco = True
            return self._decided(fr, "no_cost_increase", "stale", rec, age=dev.current_round - src)
        sl = self.slack_f(dev, x)
        if sl is None:
            return self._decided(fr, "lazy", "unlimited", rec)          # (unlimited decoders: lazy, as U v4)
        dem = dev._u4_demand_lazy(v.vid)
        p = self.p_corr(fr)
        short = max(0.0, h * p * dem - sl)
        rec.update(slack=round(sl, 3), demand=dem, p_corr=round(p, 6), shortfall=round(short, 3))
        if self.w_mode == "zero":
            W = 0.0
        else:
            eta, raw, why = self.eta(v)
            if self.instr:
                self._eta_rec(v, eta, raw, why, kind)
            if eta is None:
                rec["eta_why"] = why
                return self._decided(fr, "lazy" if sl >= h * p * dem else "no_cost_increase", "eta_unavailable", rec)
            t_nci = self.reach(fr, s_nci)
            if eta <= t_nci and not self.instr:                       # (W = 0 whatever t_f, since t_f >= t_nci: the
                t_f, W = None, 0.0                                     #  fence's reach only for the records)
            else:
                t_f = t_nci if s_nci >= fr.top else self.reach(fr, fr.top)
                W = max(0.0, eta - t_nci) - max(0.0, eta - t_f)
            rec.update(eta=round(eta, 2), t_nci=t_nci, t_f=t_f, W=round(W, 2))
        if W < self.H:
            return self._decided(fr, "no_cost_increase", "H", rec)
        cq = 0.0
        if short > 0:                                                  # C_q, the queue cost when it lands
            if W <= self.H:                                            # (C_q > 0 then decides: kappa is read only where it
                return self._decided(fr, "no_cost_increase", "shortfall", rec)   # can, a shortfall with W > H)
            kv = self.kappa_c * self.mu_class(v)                       # rule 50a's kappa = c x mu(v), rounds a core
            cq = kv * short
            rec.update(kappa=round(kv, 4), C_q=round(cq, 3))
            if W < self.H + cq:
                return self._decided(fr, "no_cost_increase", "shortfall", rec)
        cs = self.c_s(v, fr, s_nci, p) if (self.cs and self.ws > 0) else 0.0   # C_s, read only where it can decide
        if self.cs and self.ws > 0:
            rec["C_s"] = round(cs, 3)
        if W < self.H + cq + cs:
            return self._decided(fr, "no_cost_increase", "script", rec)
        return self._decided(fr, "lazy", "W" if (short > 0 or self.H > 0 or cs > 0) else "no_shortfall", rec)

    def _decided(self, fr, rule: str, term: str, rec: dict, **extra) -> str:
        self.stats["carry:%s:%s:%s" % (rec["kind"], rule, term)] += 1
        self._last_decision = (rec["vid"], rec["kind"], rule, term)
        if self.instr:
            rec.update(rule=rule, term=term, **extra)
            self._log("carry", rec)
        return rule

    def decision_refused(self, vid: int, why: str):
        """The registration refused the frame after its carry decision (its later checks): the decision is counted and
        recorded apart, as "registration_refused"."""
        last = self._last_decision
        if last is None or last[0] != vid:
            return
        _v, kind, rule, term = last
        self.stats["carry:%s:%s:%s" % (kind, rule, term)] -= 1
        self.stats["carry:refused:%s:%s" % (rule, term)] += 1
        car = self.rec.get("carry")
        if self.instr and car and car[-1]["vid"] == vid and car[-1]["kind"] == kind:
            car[-1].update(kind="registration_refused", refused=why)
        self._last_decision = None

    def _log(self, kind: str, item):
        """Append one rule 56 record, or count it as truncated past U6_LOG_MAX (the summary flags it)."""
        if len(self.rec[kind]) < U6_LOG_MAX:
            self.rec[kind].append(item)
        else:
            self.stats["records:truncated:" + kind] += 1

    # ------------------------------------------------------------------ registration (rule 50, 51c)
    def on_install(self, v, fr, patches_in_use) -> bool:
        """In a carrying frame's round (U's `_u_round`), once its prediction is installed: prediction registration (m_hat
        = 1 always, m_hat = 0 under 51c), the one reconsideration, then U v4's REGISTER and E1 for m_hat = 1, or the
        m_hat = 0 REGISTER (no request).  Returns False until the installation (nothing to do yet)."""
        dev = self.dev
        got = self.installed(v, fr)
        if got is None:
            return False
        m, c, t0 = got
        if not m and not self.mhat0:
            dev._u_reg[v.vid] = None                                       # (m_hat = 0 without 51c: not registered)
            self.stats["install:mhat0_unregistered"] += 1
            return True
        fr.u6_pred = (m, c)
        if (self.reconsider and fr.rule == "no_cost_increase" and t0 > fr.reg_round and not fr.u6_noreco
                and not fr.u6_reco and fr.state == "carry" and not v.delivered):
            fr.u6_reco = True
            if self.decide(v, fr, fr.stop, kind="reconsider") == "lazy":
                self.switch_lazy(fr)
        if m:
            dev._u_register(v, fr)
            if dev._u_reg[v.vid] is not None:
                dev._u_epoch1(fr, patches_in_use)
        else:
            dev._u6_register_mhat0(v, fr)
        return True

    def switch_lazy(self, fr):
        """The reconsideration's switch to the lazy walk: the frame carries to its top from now, its lazy demand reserved
        at this round; residual, fences and reservations kept (round 3's 44f')."""
        dev = self.dev
        fr.rule = "lazy"
        fr.stop = fr.top
        fr.limit = max(fr.limit, fr.top)
        if dev._u4_lazy_on:
            dev._u4_lazy_reserve(fr)
        self.stats["reconsider:switched"] += 1

    # ------------------------------------------------------------------ rule 56's records (instrumentation on)
    def _eta_rec(self, v, eta, raw, why, kind):
        self._log("eta", dict(vid=v.vid, round=self.dev.current_round, kind=kind, vclass=self.vclass(v),
                              t_end=self.dev.schedule_instructions[v.producer_idx].end_round,
                              eta=(None if eta is None else round(eta, 2)), raw=(None if raw is None else round(raw, 2)), why=why))

    def request_rec(self, k, req, chk, cause_name):
        """An ACT request (m_hat = 1): accepted, or the first refusal cause in rule 56a's order.  An ETA unavailable for
        a window outside the file or more than four roots is the unit's capacity, also where 51b is off (R).  An E1 that
        passes every check but which the unit answers net2 (E2's gain at s is larger) is not accepted: it is "deferred"
        to E2 (round 5's hygiene)."""
        why = None
        if not chk.get("entry"):
            why = "capacity"
        else:
            fails = dict(unsupported_residual=not (chk["placed"] and chk["sd"]), fence=not chk["clean"],
                         overlapping_cone=(not chk["ready"] and req.get("_ready_why") == "prefix"),
                         no_workspace=(not chk["cells"] or (not chk["ready"] and req.get("_ready_why") != "prefix")),
                         queue=not chk["queue"], headroom=not chk["idle"],
                         eta=(not chk["eta"] and req.get("_roots_why") not in ("outside_file", "over_4")),
                         capacity=(not chk["eta"] and req.get("_roots_why") in ("outside_file", "over_4")),
                         gain=not chk.get("score", False))
            why = next((c for c in REFUSAL_ORDER[1:] if fails[c]), None)
            if why is None and cause_name == "net2":
                why = E1_DEFERRED
        self._log("requests", dict(vid=req["vid"], k=k, epoch=req["epoch"], b=req["b"], accepted=why is None, refusal=why,
                                   unit_cause=cause_name, gain=chk.get("gain"), eta_q8=chk.get("eta_q8"), floor=req.get("floor"),
                                   roots_why=req.get("_roots_why")))

    def summary(self) -> dict:
        dev = self.dev
        recs = self._records() if self.instr else None                 # (before the stats: it counts unrecorded reasons)
        out = dict(options={k: v for k, v in self.opts.items()}, H_rounds=self.H, timing=self.timing,
                   stats={k: v for k, v in sorted(self.stats.items()) if v})
        if self.eta_floor:
            out["lambda"] = dict(sha256=self.lam_sha, provenance=self.lam_prov, entries=len(self.lam), budget_level=self.budget_level,
                                 frozen=self.lam_frozen)
        if self.prior_sha is not None:                                 # rule 50d's prior (p_corr without a prediction)
            out["prior"] = dict(sha256=self.prior_sha, entries=len(self.prior), frozen=self.prior_frozen)
        if recs is not None:
            out["records"] = recs
            out["records_truncated"] = {k[len("records:truncated:"):]: v for k, v in self.stats.items()
                                        if k.startswith("records:truncated:") and v}
        return out

    def _records(self) -> dict:
        """Rule 56's records: every value (could it request an ACT, and if not why), every carry decision, every ACT
        request, every accepted ACT, the score's calibration inputs, eta's error, and the executed paths."""
        dev = self.dev
        acts = {a["vid"]: a for a in dev._u_acts} if getattr(dev, "_u_acts", None) else {}
        req_vids = {r["vid"] for r in self.rec["requests"]}
        values = []
        for vid in sorted(dev.values):
            v = dev.values[vid]
            fr = dev._frames.get(vid)
            pr, m, c, _rc = dev._dview(v) if self.client else (-1, None, None, -1)
            if fr is None:
                why = dev._u6_noframe.get(vid, "unrecorded")
                cls = noframe_class(why)
            elif vid in req_vids:
                why = cls = "requested"
            elif pr < 0 or m is None:
                why = cls = "no_prediction"
            elif not m:
                why, cls = "mhat0", "mhat0"
            else:
                reg = dev._u_reg.get(vid) if self.client else None
                if vid not in getattr(dev, "_u_reg", {}):
                    why, cls = "installed_after_verdict_or_end", "late_prediction"
                elif reg is None:
                    why, cls = "register_skipped", "unsupported_residual"
                else:
                    why, cls = dev._u6_noreq.get(vid, "unrecorded"), "no_request"   # (registered, never asked: E1's
            if cls == "unrecorded" or why == "unrecorded":                         #  reason, E2 never reached)
                self.stats["records:unrecorded"] += 1
            values.append([vid, cls, why])
        out = dict(value_fields=["vid", "class", "why"], values=values, carry=self.rec["carry"], requests=self.rec["requests"],
                   eta=[dict(e, verdict=dev.values[e["vid"]].delivered_round) for e in self.rec["eta"]],
                   paths=self.rec["paths"])
        out["accepted"] = [dict(vid=a["vid"], epoch=a["epoch"], b=a["b"], executed=True, how=a["how"], cancel=a["cancel"])
                           for a in dev._u_acts[:U6_LOG_MAX]] if acts else []
        out["accepted"] += [dict(vid=d[0], epoch=d[1], b=d[2], executed=False, how="discarded", cancel="%s:%s" % (d[3], d[4]))
                            for d in self.rec["discards"]]
        out["calibration"] = [dict(r, verdict=dev.values[r["vid"]].delivered_round, how=(acts.get(r["vid"]) or {}).get("how"),
                                   done=(acts.get(r["vid"]) or {}).get("done"), splice=(acts.get(r["vid"]) or {}).get("splice"))
                              for r in self.rec["calib"]]
        return out
