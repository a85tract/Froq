"""U's decision path: the golden model of se_u_unit (part B; round rules 41, 42 and 44 (U v4),
``).

The unit sits beside the threshold controller and answers the scheduler's epoch requests ACT or CARRY. It works per
pass: after the engine's Phase A of pass k (whose REGISTER, RECONFIDENCE and RESOLVE events it mirrors), it takes
the window events of round k into the ETA register file, then answers round k's epoch requests in arrival order.
Round k's messages are those that entered their queues before the k-th strobe, as for the engine's own queues.

Definitions (the hardware contract; the simulator's U must use the same for the cycle-level replay):
- Per site class c and boundary b <= B_MAX: S (early script duration), R (rebased repair duration), P1 (price),
  placed, clean, demand (decoder concurrency of the early script plus its repair). CONFIG sel 6.
- S_D = S at the placed boundary of minimum P1 in [cur, reach] (ties: the earliest); none placed: no estimate.
- c = 1 - omc / 2^20 (Q0.20, from REGISTER / RECONFIDENCE). The gain of an ACT at b, relative to the same D
  continuation (rule 44c), exact in Q.GAIN_F = Q.28 (c in Q0.20 times rounds in Q.ETA_F):
    eta_v = max(now, max over the value's root windows of their ETA) + path; a = max(now + act_lat, readiness of b);
    W_b = max(0, eta_v - a - S_b), the stall that acting at b removes, saturated at W_MAX (below 2^17 rounds);
    tail_b = max(0, a + S_b - eta_v), with the same a, clamped at T_MAX (2^18 rounds: the gain is then below 0 either
    way, so no decision changes); both saturations are part of the definition (rule 44c as amended at 16:33), and
    W's can change a decision, for stalls of 2^17 rounds or more (two W above it compare equal);
    gain_b = c (W_b + S_D) - (1 - c) R_b - tail_b = c (W_b + S_D + R_b) - R_b - tail_b (S_b is not charged again).
  E1 (at installation, at b*, rule 41e) acts iff eligible and gain_b* > delta and (E2's gain at s, with s's
  readiness, is not estimable or gain_b* >= it); E2 (at D-nci's stop s) acts iff eligible and gain_s > delta (b is
  the request's). delta: CONFIG sel 7 (Q16.20) with client_on; client off: every request is CARRY.
- Eligibility (first failure is the cause): a live entry, m_hat = 1, b placed, an S_D, clean_b, the scheduler's
  cells-free bit, its readiness valid, the pool's full-decode queue empty, idle cores >= the ACT's excess demand
  max(0, demand_b - held), an estimable ETA (at both epochs, rule 44c). held is the frame's reservation that the
  EPOCH request carries (a lazy frame's, which the pool counts from registration; 0 otherwise, rule 44a): an ACT
  keeps it and adds only the excess, so U debits only the excess, within the pass and in its history. Idle cores: pl_idle latched at the round's strobe (the pool counts an ACT of pass j
  from pass j + act_lat), less the demand of U's own ACTs of the last act_lat - 1 passes (a history of H_MAX
  passes, so act_lat <= H_MAX; an ACT stays in it whatever becomes of it), at least 0, then reduced by each ACT of
  the round (rule 41c), each by its excess demand.
- The carry rule's outstanding sum (rule 44a as amended at 04:58; the u_xs port). The scheduler chooses a frame's carry
  rule when the frame opens: lazy iff B - busy - reserved - this sum >= h x demand_lazy(class), a comparison it makes
  itself. After pass k the sum is the potential excess max(0, demand_b - held) of every request of passes
  k - act_lat + 2.. k, whatever U answered it: a request is outstanding for the scheduler until its effect round,
  act_lat rounds, and the simulator opens round k's frames before it sends round k's requests (whose pass is
  k + 1). demand_b is the request's class's record at its b; a request whose value U holds no entry, or with
  b > B_MAX, counts 0 (U knows no class for it). A pass holds at most 128 requests (the EPOCH queue's depth).
- ETA register file: one slot per decoding window, absolute rounds in Q.ETA_F. NEW(w, own, preds) / UPD(w, own) /
  DONE(w, own: decoded, the estimate final) / COMMIT(w). NEW, UPD and DONE stamp the window with the round.
  ETA(w) = max(own(w), the ETAs of its unverified live predecessors), valid iff those are valid, it names at most P_MAX
  predecessors and it is not stale; a verified window has ETA 0, valid. Freshness: a live, unverified window that is
  not DONE is stale in pass k iff its stamp < kms(k) = max(0, k + act_lat - stale_max), i.e. it would be older than
  stale_max at round k + act_lat, the latest round at which an ACT decided in pass k may take effect. After pass k's
  window events, every window that is stale and not yet marked is marked and recomputed in ascending slot order, each
  as an event (with the budget below); a NEW, UPD or DONE clears the mark. So invalidity reaches everything
  downstream of a stale window: a value is estimable only if every unverified ancestor of its roots is fresh. After an event its window is recomputed; if
  that changed its (ETA, valid) -- a NEW always counts as changed -- its affected successors become pending, and the
  lowest pending slot is recomputed, its affected successors joining when it changed, at most BUDGET recomputes per
  event. The successors of x are the live, unverified slots naming x; y is affected by x's change from (e_o, v_o) to
  (e_n, v_n) iff the change is a NEW, or v_n != v_o, or e_n > e_o and ETA(y) < e_n, or e_n <= e_o and ETA(y) <= e_o:
  a valid y outside these would recompute to the values it holds, and an invalid y's ETA is never read (it stays
  invalid until a recompute finds its predecessors valid: its own update, or a validity change upstream). If slots
  remain pending, every slot reachable from them (all successors) is marked invalid before anything reads the file.
  A value's ETA is estimable iff it has 1..R_MAX root windows, each live and valid.

U v6, each switch off by default, so U v4 is unchanged:
- `q_wait` (51a, as corrected by rule 50b): the full-decode queue check passes iff the queue is empty (U v4's snapshot,
  pool["fe"]) or the queue's expected wait at the request, pool["wq"] (rule 30's Wq for a window queued now behind
  every queued one, Q.ETA_F), is at most act_lat. It is never stricter than U v4's check: rule 30's term counts the
  busy decoders' remaining service even with nothing queued, so alone it refused requests U v4 passed.
- The ETA floor (51b): a request may carry `floor`, an absolute round (the scheduler's t_end + Lambda) when the verdict's
  decode windows are not all built; its `roots` are then the built windows' roots, possibly none. eta_v =
  max(max(now, the roots' ETAs) + path, floor). A root not live or not valid still makes eta unavailable (rule 41a).
- `repair_terms` (Eq. (1)'s missing terms): the tables carry XS and XR, the rounds from a script's end until its
  results are decoded and delivered (its last window's data, forward buffer and mean service, plus the path), of the
  script at b and of its repair at k_b. gain_b = c (W_b + S_D + XS_D) - (1 - c) (R_b + XR_b) - tail_b, with XS_D
  the XS of S_D's boundary: a right action saves the deferral script and its results' decoding and delivery, a wrong
  one pays the repair and its results'.
- `theta` (T6, rule 54): the score test is replaced by c >= theta (Q0.20) at both epochs, every eligibility check kept,
  so its decision opportunities are U's.
- `trace_checks` (rule 56a, records only): every request's check vector, in `chk`."""
import collections

FRAC = 20
ETA_F = 8
B_MAX = 16
P_MAX = 4
R_MAX = 4
BUDGET = 16
H_MAX = 16                                               # passes of U's ACT-demand history (rule 41c): act_lat <= H_MAX
GAIN_F = FRAC + ETA_F                                    # the gains' fraction bits (rule 44c)
W_MAX = (1 << 25) - 1                                    # W_b's saturation (Q.ETA_F): c x (W_b + S_D + R_b) is 21 x 26 bits
T_MAX = 1 << 26                                          # tail_b's clamp (Q.ETA_F): c x (...) < 2^46 <= T_MAX << FRAC
ONE = 1 << FRAC

(C_ACT, C_OFF, C_NOENT, C_MHAT0, C_NOTPLACED, C_NOSD, C_UNCLEAN, C_CELLS, C_NOTREADY, C_FULLQ, C_IDLE, C_ETA, C_SCORE,
 C_NET2) = range(14)
CAUSE = ("act", "off", "noent", "mhat0", "notplaced", "nosd", "unclean", "cells", "notready", "fullq", "idle", "eta",
         "score", "net2")


class UTable:
    __slots__ = ("S", "R", "P1", "placed", "clean", "demand", "XS", "XR")

    def __init__(self, S=0, R=0, P1=0, placed=False, clean=False, demand=0, XS=0, XR=0):
        self.S, self.R, self.P1, self.placed, self.clean, self.demand = S, R, P1, placed, clean, demand
        self.XS, self.XR = XS, XR                        # U v6, Eq. (1)'s repair terms (0 unless the tables carry them)


class UUnit:
    def __init__(self, n_w=256):
        self.n_w = n_w
        self.tab = {}                                    # (class, b) -> UTable
        self.delta, self.client_on, self.path, self.stale_max, self.act_lat = 0, False, 0, 0, 0
        self.k_now = 0                                   # the pass being processed (freshness)
        self.ent = {}                                    # engine entry index -> [vid, class, omc, mhat]
        self.alive = [False] * n_w; self.verified = [False] * n_w
        self.own = [0] * n_w; self.stamp = [0] * n_w; self.eta = [0] * n_w; self.valid = [False] * n_w
        self.exact = [False] * n_w; self.sm = [False] * n_w     # DONE (never stale); marked stale by the scan
        self.preds = [()] * n_w
        self.namers = [set() for _ in range(n_w)]         # slot -> slots naming it among their first P_MAX predecessors
        self.taps = []                                   # (pass, kind, ...) from the engine's Phase A, in pass order
        self.tap_i = 0                                   # the next tap to apply
        self.out = []                                    # (pass, vid, epoch, b, act, cause)
        self.nets = []                                   # (pass, vid, gain1, gain2, est2, eta) per scored request (Q.GAIN_F)
        self.act_hist = {}                               # pass -> the demand of its ACTs (rule 41c)
        self.px_hist = {}                                # pass -> its requests' potential excess (rule 44a, u_xs)
        self.xs = []                                     # (pass k, the outstanding sum after it: passes k - act_lat + 2 .. k)
        self.no_stall = False                            # ablation (rule 33a, 6): W_b = 0 at both epochs, the ETA
                                                         # requirement and the tail term kept (off in every other arm)
        self.q_wait = False                              # U v6, 51a: the queue's expected wait against act_lat
        self.repair_terms = False                        # U v6, Eq. (1)'s repair decoding and delivery terms
        self.theta = None                                # T6: the threshold c >= theta (Q0.20) in place of the score
        self.elig_lat = None                             # round 6 (B): the eligibility horizon (51a's queue check, the
                                                         # stale scan) held apart from act_lat; None: act_lat
        self.xi0 = False                                 # round 6 (amendment 1): Eq. (1)'s XS_D and XR at 0 in the score
        self.trace_checks = False                        # rule 56a: record every request's check vector (records only)
        self.chk = []                                    # (pass, vid, epoch, the checks) when trace_checks
        self.stats = collections.Counter()

    # ------------------------------------------------------------------ configuration
    def set_table(self, cls, b, **kw):
        self.tab[(cls, b)] = UTable(**kw)

    # ------------------------------------------------------------------ the engine's taps (golden/engine.py)
    def tap(self, p, kind, *args):
        self.taps.append((p, kind) + args)

    def _apply_taps(self, k):
        """The taps of passes up to k, in order (the engine emits them in pass order)."""
        i, taps = self.tap_i, self.taps
        while i < len(taps) and taps[i][0] <= k:
            t = taps[i]; i += 1
            kind = t[1]
            if kind == "REG":
                _, _, idx, vid, cls, omc, mhat = t
                self.ent[idx] = [vid, cls, omc, mhat]
            elif kind == "RC":
                _, _, idx, omc = t
                if idx in self.ent:
                    self.ent[idx][2] = omc
            elif kind == "END":
                self.ent.pop(t[2], None)
        self.tap_i = i

    # ------------------------------------------------------------------ the ETA register file
    def _succ(self, x):
        return [y for y in self.namers[x] if self.alive[y] and not self.verified[y]]

    def elig(self):
        """The eligibility horizon: act_lat, or the horizon a round-6 arm holds apart from it (`elig_lat`)."""
        return self.act_lat if self.elig_lat is None else self.elig_lat
    def kms(self, k):
        return max(0, k + self.elig() - self.stale_max)

    def _stale(self, y):
        return self.alive[y] and not self.verified[y] and not self.exact[y] and self.stamp[y] < self.kms(self.k_now)

    def _recompute(self, y):
        if self.verified[y]:
            new = (0, True)
        else:
            e, v = self.own[y], len(self.preds[y]) <= P_MAX and not self._stale(y)
            for p in self.preds[y][:P_MAX]:
                if self.alive[p] and not self.verified[p]:
                    e = max(e, self.eta[p]); v = v and self.valid[p]
            new = (e, v)
        old = (self.eta[y], self.valid[y])
        self.eta[y], self.valid[y] = new
        return new != old, old, new

    def _affected(self, x, old, new, forced=False):
        (e_o, v_o), (e_n, v_n) = old, new
        return [y for y in self._succ(x)
                if forced or v_n != v_o or (self.eta[y] < e_n if e_n > e_o else self.eta[y] <= e_o)]

    def _propagate(self, w, old, new, forced=False):
        pend = set(self._affected(w, old, new, forced)); budget = BUDGET
        while pend and budget:
            y = min(pend); pend.discard(y); budget -= 1; self.stats["recomputes"] += 1
            ch, o, n = self._recompute(y)
            if ch:
                pend.update(self._affected(y, o, n))
        if pend:
            self.stats["budget_exhausted"] += 1
            seen = set()
            while pend:
                y = min(pend); pend.discard(y)
                if y in seen:
                    continue
                seen.add(y); self.valid[y] = False; self.stats["invalidated"] += 1
                pend.update(z for z in self._succ(y) if z not in seen)

    def win_event(self, k, op, w, own=0, preds=()):
        if op == "NEW":
            self.alive[w], self.verified[w], self.own[w], self.stamp[w] = True, False, own, k
            self.exact[w], self.sm[w] = False, False
            for p in self.preds[w][:P_MAX]:
                self.namers[p].discard(w)
            self.preds[w] = tuple(preds)                 # (more than P_MAX: the window is invalid; the slot's old ETA
            for p in self.preds[w][:P_MAX]:
                self.namers[p].add(w)
            ch, o, n = self._recompute(w)                #  stays until the recompute, as in the RTL)
            self._propagate(w, o, n, forced=True)
            return
        if not self.alive[w]:
            self.stats["win_unmatched"] += 1
            return
        if op in ("UPD", "DONE"):
            self.own[w], self.stamp[w], self.exact[w], self.sm[w] = own, k, op == "DONE", False
        elif op == "COMMIT":
            self.verified[w] = True
        ch, o, n = self._recompute(w)
        if ch:
            self._propagate(w, o, n)

    def eta_of(self, k, roots, floor=None):
        """(estimable, eta in Q.ETA_F) of a value with these root windows, in round k.  U v6 (51b): with `floor` (an
        absolute round: the verdict's windows are not all built, and these are the built ones' roots, possibly none),
        eta = max(max(k, the roots' ETAs) + path, floor); a root not live or not valid still makes it unavailable."""
        if (not roots and floor is None) or len(roots) > R_MAX:
            return False, 0
        e = k << ETA_F
        for r in roots:
            if not self.alive[r] or not self.valid[r]:
                return False, 0
            if self.verified[r]:
                continue
            e = max(e, self.eta[r])
        e += self.path << ETA_F
        return True, (e if floor is None else max(e, floor << ETA_F))

    # ------------------------------------------------------------------ epochs
    def _sd(self, cls, cur, reach):
        best = self._sd_x(cls, cur, reach)
        return None if best is None else best[0]

    def _sd_x(self, cls, cur, reach):
        """(S_D, XS_D): the script at the placed boundary of least P1 in [cur, min(reach, B_MAX)], ties to the earliest,
        with its results' decoding and delivery (U v6); None if none is placed."""
        best = None
        for b in range(cur, min(reach, B_MAX) + 1):
            t = self.tab.get((cls, b))
            if t is not None and t.placed and (best is None or t.P1 < best[0]):
                best = (t.P1, t.S, t.XS)
        return None if best is None else best[1:]

    def epoch(self, k, req, pool):
        """One epoch request; returns (act, cause).  `pool` holds this round's latched status (idle is reduced here)."""
        if self.trace_checks:
            self.chk.append((k, req["vid"], req["epoch"], self.checks(k, req, pool)))
        vid, ep, b = req["vid"], req["epoch"], req["b"]
        if not self.client_on:
            return 0, C_OFF
        idx = next((i for i, e in self.ent.items() if e[0] == vid), None)
        if idx is None:
            return 0, C_NOENT
        _, cls, omc, mhat = self.ent[idx]
        if not mhat:
            return 0, C_MHAT0
        t = self.tab.get((cls, b))
        if t is None or not t.placed:
            return 0, C_NOTPLACED
        sdx = self._sd_x(cls, req["cur"], req["reach"])
        if sdx is None:
            return 0, C_NOSD
        if not t.clean:
            return 0, C_UNCLEAN
        if not req["cells_free"]:
            return 0, C_CELLS
        if not req["ready_v"]:
            return 0, C_NOTREADY
        if not self._queue_ok(pool):
            return 0, C_FULLQ
        dem = max(0, t.demand - req.get("held", 0))                 # the ACT's excess over the frame's reservation
        if pool["idle"] < dem:
            return 0, C_IDLE
        c = ONE - omc
        fl = req.get("floor")                                        # (51b; without one, U v4's call exactly)
        est, eta = self.eta_of(k, req["roots"]) if fl is None else self.eta_of(k, req["roots"], fl)
        if not est:                                                  # both epochs need eta (rule 44c)
            return 0, C_ETA
        gain = self._gain_fn(k, cls, c, eta, sdx)
        dq = self.delta << ETA_F
        if self.theta is not None:                                   # T6: the threshold in place of the score
            self.nets.append((k, vid, 0, 0, 0, eta))
            if c < self.theta:
                return 0, C_SCORE
        elif ep == 1:                                                # E1 at b*, against E2's gain at s
            g1 = gain(b, req["ready"])
            g2 = gain(req["s"], req["s_ready"]) if req["s_ready_v"] else None
            self.nets.append((k, vid, g1, g2 if g2 is not None else 0, int(g2 is not None), eta))
            if g1 <= dq:
                return 0, C_SCORE
            if g2 is not None and g1 < g2:
                return 0, C_NET2
        else:                                                        # E2 at D-nci's stop b
            g2 = gain(b, req["ready"])
            self.nets.append((k, vid, 0, g2, 1, eta))
            if g2 <= dq:
                return 0, C_SCORE
        pool["idle"] -= dem
        return 1, C_ACT

    def _queue_ok(self, pool):
        """The full-decode queue check: U v4's empty-queue snapshot; with 51a (rule 50b) the snapshot or the queue's
        expected wait at the request no longer than act_lat, so that it is expected to be clear by the ACT's effect
        round -- never stricter than U v4's."""
        if self.q_wait:
            return bool(pool["fe"]) or pool["wq"] <= (self.elig() << ETA_F)
        return bool(pool["fe"])

    def _gain_fn(self, k, cls, c, eta, sdx):
        """Rule 44c's gain of an ACT at bb with readiness `ready`, in Q.GAIN_F (None if bb is not placed), with S_D and
        XS_D from `sdx`; with `repair_terms`, Eq. (1)'s decoding and delivery of the deferral script's and the repair's
        results (U v6)."""
        sd, xd = sdx
        if self.xi0:                                     # (round 6, amendment 1: XS_D and XR priced at 0)
            xd = 0

        def gain(bb, ready):
            tb = self.tab.get((cls, bb))
            if tb is None or not tb.placed:
                return None
            a = max(k + self.act_lat, ready) << ETA_F
            w = 0 if self.no_stall else min(max(0, eta - a - (tb.S << ETA_F)), W_MAX)
            tl = min(max(0, a + (tb.S << ETA_F) - eta), T_MAX)
            if self.repair_terms:
                xr = 0 if self.xi0 else tb.XR
                return c * (w + ((sd + xd + tb.R + xr) << ETA_F)) - ((tb.R + xr) << GAIN_F) - (tl << FRAC)
            return c * (w + ((sd + tb.R) << ETA_F)) - (tb.R << GAIN_F) - (tl << FRAC)
        return gain

    def checks(self, k, req, pool):
        """Rule 56a (records only): every check of a request, each on its own, with the pool as this request sees it
        (no state changes): live entry, m_hat, placed, S_D, clean, cells, readiness, queue, idle, ETA, and the score."""
        out = dict(entry=False)
        idx = next((i for i, e in self.ent.items() if e[0] == req["vid"]), None)
        if idx is None:
            return out
        _, cls, omc, mhat = self.ent[idx]
        t = self.tab.get((cls, req["b"]))
        sdx = self._sd_x(cls, req["cur"], req["reach"])
        dem = max(0, (t.demand if t is not None else 0) - req.get("held", 0))
        fl = req.get("floor")                                        # (51b; without one, U v4's call exactly)
        est, eta = self.eta_of(k, req["roots"]) if fl is None else self.eta_of(k, req["roots"], fl)
        out.update(entry=True, mhat=bool(mhat), placed=bool(t is not None and t.placed), sd=sdx is not None,
                   clean=bool(t is not None and t.clean), cells=bool(req["cells_free"]), ready=bool(req["ready_v"]),
                   queue=self._queue_ok(pool), idle=pool["idle"] >= dem, eta=bool(est), eta_q8=(eta if est else None))
        if est and out["placed"] and out["sd"]:
            g = self._gain_fn(k, cls, ONE - omc, eta, sdx)
            g1 = g(req["b"], req["ready"])
            out["gain"] = g1
            out["score"] = (ONE - omc >= self.theta) if self.theta is not None else (g1 is not None and g1 > (self.delta << ETA_F))
        return out

    def potential_excess(self, req):
        """Rule 44a as amended: a request's potential excess max(0, demand_b - held), whatever its answer; 0 if U holds
        no entry for its value (it knows no class) or b > B_MAX."""
        e = next((e for e in self.ent.values() if e[0] == req["vid"]), None)
        t = self.tab.get((e[1], req["b"])) if e is not None and req["b"] <= B_MAX else None
        return max(0, t.demand - req.get("held", 0)) if t is not None else 0

    # ------------------------------------------------------------------ one pass
    def run_pass(self, k, wins, reqs, pool):
        self.k_now = k
        self._apply_taps(k)
        for op, w, own, preds in wins:
            self.win_event(k, op, w, own, preds)
        # the stale scan: windows that went stale (and are not yet marked), lowest slot first, each as an event
        kms = self.kms(k)
        for y in [y for y in range(self.n_w) if self.alive[y] and not self.verified[y] and not self.exact[y]
                  and not self.sm[y] and self.stamp[y] < kms]:
            self.sm[y] = True; self.stats["stale"] += 1
            ch, o, n = self._recompute(y)
            if ch:
                self._propagate(y, o, n)
        assert 0 <= self.act_lat <= H_MAX, "act_lat %d outside 0..%d" % (self.act_lat, H_MAX)
        pool = dict(pool)
        pool["idle"] = max(0, pool["idle"] - sum(self.act_hist.get(k - j, 0) for j in range(1, self.act_lat)))
        idle0 = pool["idle"]
        self.px_hist[k] = sum(self.potential_excess(req) for req in reqs)   # (the entries are this pass's throughout)
        for req in reqs:
            act, cause = self.epoch(k, req, pool)
            self.out.append((k, req["vid"], req["epoch"], req["b"], act, cause))
            self.stats["act" if act else "carry"] += 1
        self.act_hist[k] = idle0 - pool["idle"]          # this pass's ACT excess: U debits it in passes k+1 .. k+act_lat-1
        for j in [j for j in self.act_hist if j <= k - H_MAX]:
            del self.act_hist[j]
        self.xs.append((k, sum(self.px_hist.get(k - j, 0) for j in range(self.act_lat - 1))))
        for j in [j for j in self.px_hist if j <= k - H_MAX]:
            del self.px_hist[j]
