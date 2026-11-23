"""(plan v2 sections 4.4 and 8; the plan gate's disposition 15, F15): `---records`, records only.

What a time breakdown and the queue and admission-stall distributions need, and nothing the run reads:

- requests (4.4): every controller request of the hardware-limit mode (`hwlimits.HWLimits`: registrations, commit checks
  and reserves, splices and rebases, decides, frame ends, script ends, by the limit file's kind names) and every guard
  check of the dispatcher (kind `guard_check`), by kind: the count, and the distributions in rounds of the wait (grant
  less submission), the service (completion less grant) and the total (completion less submission). Submission is the
  round the producer submitted it (a guard check: the round the dispatcher first asked); grant, the round it entered
  the operation FIFO (a guard check: the round it was served); completion, the round its last operation completed and
  its effect applied. Cancelled requests are counted by kind.
- admission stalls (4.4): per value that came to registration (arm D), [first attempt, outcome round, outcome
  ("admitted" or "gave_up", None if the run ended first), refused attempts]; the stall is outcome less first attempt.
- instructions (8, F15): per instruction (spliced script instructions included), its ready round (the round after its
  last predecessor ended: DAG edges and completion conditions; 0 without one; one less for a zero-duration instruction,
  which the dispatch stamps start = end = the round before its own), start, end, the cause that held it on its
  last waiting round (`CAUSES`; "" when it started when ready) with the raw reason, and its binding predecessor: for a
  wait on a held cell, the instruction running on it; for a value's or frame's hold, the value (`who_v`); for a
  completion condition, the instruction it waited; when it started at its ready round (or the wait names no
  holder), the predecessor that ended last. `who_r`, for every instruction: the predecessor that ended last
  (DAG edges and completion conditions, the latest end; of those ending in that round, the highest index, as `who_i`
  takes it), None when none ended: the instruction whose end made it ready, so a walk back need not guess it at a
  value's or frame's hold, where `who_i` names none.
- completion (8): the run's completion is the round the last value settled (the ledger's `settled_round`), which can
  come after the last instruction ends; with it the last instruction end, the last delivery and settlement (round and
  value), the last spliced script instruction's end and the last controller request's completion (round and kind), so
  the walk can give the interval after the last instruction to a terminal decode or a terminal controller wait.
- the workspace union (plan 7.3, cap levels): once a syndrome round, the non-program cells a patch
  occupies (the cells `peak_nonprogram_cells` counts) and the non-program cells a live frame's reservation holds (SER-R's
  and CSR's held cells; none without a reservation), with "non-program" read as the cap's own count reads it (W-E's
  `--cap-hard`: a cell whose role is not "program", a held cell not yet occupied included). A cell both occupied and
  held counts once. `ws_union`: the peak over rounds of the union, the first round it was reached with its parts there
  (occupied, held, both), and each part's own peak over rounds (`occupied_peak` equals the ledger's
  `peak_nonprogram_cells`; `held_peak` counts held non-program cells once a round, unlike the reservation's own
  `held_peak`, which counts every held cell at each take).
- queue occupancy (plan 9's "the controller's event queue held at most [k] requests (p99 [p])"): for each
  controller queue, a round's value is the most requests it held at any point of the round (what it carried, and
  what entered). `queues`: per queue, `peak` (the largest round's value) and `peak_round` (the first round of it),
  `p99` (the 99th percentile of the rounds' values, nearest rank: the value at rank ceil(99 n / 100) of the n rounds in
  ascending order), `rounds` (n: every round from the queue's first to the run's last that the controller served) and
  `hist` ({value: rounds}, as `requests`' histograms, so runs pool by adding them). The queues, from the hardware-limit
  mode's hooks (`hwlimits.HWLimits._r17_occ`): `event_queue`, the operation FIFO (32 events; plan 9's event queue);
  `event_waiting`, the events submitted and not yet in it (the deferral phase's, which wait for `serve`, and a stalled
  producer's); `splice_queue` and `hbm_tags`, the FIFO's splices (8) and its events waiting for their HBM fetch (64);
  `guard_check_queue`, as built, the dispatcher's guard checks queued for the operation port (improved has no such
  queue: a check is served in the round asked or asked again, so it stays 0). From the write queue (`wq.WQ.round`):
  `write_queue`, its entries (O), whose round's most is O before the next round's retirement, since O grows only by
  `issue` and falls only there. `event_queue`'s peak is the hardware-limit mode's `peaks.queue` (0 where it has
  none), and `write_queue`'s the write queue's `peak.O`.

Off (the default), none of this exists and nothing changes. On, the run's outcome is the same: the hooks only read."""
from __future__ import annotations

import collections

# the raw hold reasons of the dispatch (device.py `blocked`) by class
CAUSES = {
    "patch": "cell",                                                    # a cell held by a running instruction
    "completion": "dependency",                                         # a completion condition on another instruction
    "native": "decode", "no_prediction": "decode", "class_miss": "decode", "policy": "decode", "fence": "decode",
    "feedback": "decode", "not_ready": "decode",                        # a decoded outcome (the value's, a fence's, feedback's)
    "defer_stop": "reservation", "defer_dependency": "reservation", "defer_step": "reservation",
    "defer_materialize": "reservation", "defer_script_cell": "reservation", "defer_guard": "reservation",
    "defer_rule_a": "reservation",                                      # a live frame's hold (its stop, its reservation)
    "workspace": "workspace", "workspace_cap": "workspace", "ws_accept": "workspace", "reserved": "workspace",
    "overlap": "workspace",
    "hw_guard_check": "controller", "wq_aged": "controller", "wq_hold": "controller", "wq_e": "controller",
    "kept": "controller",                                               # a controller port, queue or check
    "magic": "t_supply", "magic_accept": "t_supply",                    # a T state
    "planned": "other",                                                 # the start-time planner's later start
}


def cause_of(why: str) -> str:
    return CAUSES.get(why, "other")


class R17Records:
    held_by = None                              # (idx -> {raw reason: waiting rounds}, a dict only with --diag-records)

    def __init__(self):
        self.req: dict = {}                     # kind -> {"n", "cancelled", "wait", "service", "total"} (Counters)
        self.chk: dict = {}                     # instruction -> the round its guard check was first asked
        self.reg: dict = {}                     # vid -> [first attempt, outcome round, outcome, refused attempts]
        self.wait: dict = {}                    # instruction -> (round, raw reason, holder instruction, holder value)
        self.round_why: dict = {}               # this round's waiting instructions -> raw reason (dispatch)
        self.last_req = (-1, None)              # (round, kind) of the last request completed
        self.ws = dict(peak=0, round=-1, occupied=0, held=0, both=0, occupied_peak=0, held_peak=0)   # (the workspace union)
        self.occ_q: dict = {}                   # queue -> {round (open), cur (its most so far), hist, peak, peak_round}

    # -- requests
    def _kind(self, kind):
        k = self.req.get(kind)
        if k is None:
            k = self.req[kind] = dict(n=0, cancelled=0, wait=collections.Counter(), service=collections.Counter(),
                                      total=collections.Counter())
        return k

    def done(self, kind, sub: int, grant: int, now: int):
        k = self._kind(kind)
        k["n"] += 1
        k["wait"][int(grant) - int(sub)] += 1
        k["service"][int(now) - int(grant)] += 1
        k["total"][int(now) - int(sub)] += 1
        if now >= self.last_req[0]:
            self.last_req = (int(now), kind)

    def ev_done(self, ev, now: int):
        self.done(ev["kind"], ev["round"], ev.get("granted", ev["round"]), now)

    def ev_cancel(self, ev):
        self._kind(ev["kind"])["cancelled"] += 1

    def check(self, idx: int, now: int, ok: bool):
        first = self.chk.setdefault(idx, now)
        if ok:
            del self.chk[idx]
            self.done("guard_check", first, now, now)

    # -- admission stalls
    def registration(self, vid: int, got, now: int):
        row = self.reg.setdefault(int(vid), [int(now), None, None, 0])
        if got is None:
            row[3] += 1
        else:
            row[1], row[2] = int(now), ("admitted" if got else "gave_up")

    # -- the workspace union (plan 7.3)
    def ws_round(self, now: int, occupied, held):
        """Once a syndrome round: `occupied`, the round's occupied non-program cells; `held`, the held non-program cells.
        Their union counts a cell in both once; the first round of the peak keeps its parts."""
        occupied, held = set(occupied), set(held)
        w = self.ws
        u = len(occupied | held)
        if u > w["peak"]:
            w.update(peak=u, round=int(now), occupied=len(occupied), held=len(held), both=len(occupied & held))
        w["occupied_peak"] = max(w["occupied_peak"], len(occupied))
        w["held_peak"] = max(w["held_peak"], len(held))

    # -- queue occupancy
    def occ(self, now: int, sizes: dict):
        """A point of round `now` at which each queue named in `sizes` holds that many requests.  A queue's open round
        keeps the most it held; it closes, one round of the histogram, when a point of a later round comes (`queues`
        counts the last one open)."""
        now = int(now)
        for k, n in sizes.items():
            n = int(n)
            q = self.occ_q.get(k)
            if q is None:
                self.occ_q[k] = q = dict(round=now, cur=n, hist=collections.Counter(), peak=n, peak_round=now)
            elif now > q["round"]:
                q["hist"][q["cur"]] += 1
                q["round"], q["cur"] = now, n
            elif n > q["cur"]:
                q["cur"] = n                                            # (this round's, or an earlier one's: the open one)
            if n > q["peak"]:
                q["peak"], q["peak_round"] = n, q["round"]

    def queues(self) -> dict:
        """Per queue: peak and its first round, the 99th percentile of the rounds' values, the rounds and their histogram."""
        out = {}
        for k, q in sorted(self.occ_q.items()):
            h = collections.Counter(q["hist"])
            h[q["cur"]] += 1                                            # (the open round: the last)
            out[k] = dict(peak=int(q["peak"]), peak_round=int(q["peak_round"]), p99=_rank(h, 99), rounds=sum(h.values()),
                          hist=_hist(h))
        return out

    # -- instructions
    def note(self, idx: int, why: str):
        self.round_why[idx] = why

    def tally(self, dev):
        """After the round's dispatch: each instruction still waiting keeps this round's reason and its holder."""
        si, now = dev.schedule_instructions, dev.current_round
        for idx, why in self.round_why.items():
            t = si[idx]
            if t.start_round != -1 and t.start_round <= now:
                continue
            hi = hv = None
            if why == "patch":
                pts = set(t.instruction.patches)
                own = [j for j in dev._active_instructions if pts.intersection(si[j].instruction.patches)]
                hi = min(own) if own else None
            elif why == "completion":
                pend = [c for c in t.instruction.conditioned_on_completion_idx if si[c].end_round == -1]
                hi = min(pend) if pend else None
            elif cause_of(why) in ("decode", "reservation"):
                hv = dev._dg_vid(idx, why)
            self.wait[idx] = (int(now), why, hi, hv)
            if self.held_by is not None:                               # (--diag-records: every waiting round by its
                c = self.held_by.get(idx)                              #  reason, not only the last; froq/diagrec.py)
                if c is None:
                    c = self.held_by[idx] = {}
                c[why] = c.get(why, 0) + 1
        self.round_why = {}

    def dump(self, dev) -> dict:
        si = dev.schedule_instructions
        preds = dev._adj()[0]
        n = len(si)
        ready, start, end, cause, raw, who_i, who_v, who_r = [], [], [], [], [], [], [], []
        for i, t in enumerate(si):
            ps = set(preds.get(i, ())) | set(t.instruction.conditioned_on_completion_idx)
            ends = [(int(si[p].end_round), p) for p in ps if si[p].end_round != -1]
            r = max(e for e, _p in ends) + 1 if ends else 0
            if dev._instruction_durations[i] == 0:
                r -= 1                                          # (a zero-duration one is stamped the round before its dispatch)
            last = max(ends)[1] if ends else None
            who_r.append(last)                                  # (the predecessor that ended last, every instruction)
            ready.append(r)
            s, e = int(t.start_round), int(t.end_round)
            start.append(s)
            end.append(e)
            w = self.wait.get(i)
            if w is not None and s != -1 and s > r:
                _rd, why, hi, hv = w
                cause.append(cause_of(why))
                raw.append(why)
                who_i.append(hi if hi is not None else (last if hv is None else None))
                who_v.append(hv)
            else:
                cause.append("")
                raw.append("")
                who_i.append(last)
                who_v.append(None)
        L = dev.ledger
        vals = list(dev.values.values())
        dl = max(((int(v.delivered_round), int(v.vid)) for v in vals if v.delivered_round >= 0), default=(-1, None))
        st = max(((int(v.settled_round), int(v.vid)) for v in vals if v.settled_round >= 0), default=(-1, None))
        scr = max((end[i] for i in range(dev.n_original, n)), default=-1)
        completion = dict(completion=int(L.settled_round) if L.settled_round >= 0 else int(dev.current_round),
                          settled_round=int(L.settled_round), program_done_round=int(L.program_done_round),
                          rounds=int(dev.current_round), last_instr_end=max(end, default=-1),
                          last_delivery=list(dl), last_settlement=list(st), last_script_end=int(scr),
                          last_request=list(self.last_req))
        wq = getattr(dev, "_wq", None)
        if wq is not None and getattr(wq, "r17", None) is self and wq._rnd is not None:
            self.occ(wq._rnd, dict(write_queue=wq.O))           # (the write queue's open round, O its most so far)
        return dict(format="scoutq-r17-records/1", n_original=int(dev.n_original),
                    requests={k: dict(n=x["n"], cancelled=x["cancelled"], wait=_hist(x["wait"]), service=_hist(x["service"]),
                                      total=_hist(x["total"])) for k, x in sorted(self.req.items())},
                    guard_checks_open=len(self.chk),
                    admission=dict(fields=["vid", "first", "outcome_round", "outcome", "refused"],
                                   rows=[[vid] + row for vid, row in sorted(self.reg.items())]),
                    instr=dict(ready=ready, start=start, end=end, cause=cause, reason=raw, who_i=who_i, who_v=who_v,
                               who_r=who_r),
                    completion=completion, ws_union=dict(self.ws), queues=self.queues())

    def summary(self, rec: dict) -> dict:
        """What the result itself carries (the full record is a side file): requests, admission stalls by outcome, the
        completion, the instruction causes counted, the workspace union and the queues' occupancy."""
        adm = collections.Counter(r[3] or "open" for r in rec["admission"]["rows"])
        stall = collections.Counter((r[2] - r[1]) for r in rec["admission"]["rows"] if r[2] is not None)
        return dict(requests=rec["requests"], admission=dict(outcomes=dict(sorted(adm.items())), stall=_hist(stall),
                                                             refused=sum(r[4] for r in rec["admission"]["rows"])),
                    completion=rec["completion"],
                    causes=dict(sorted(collections.Counter(c for c in rec["instr"]["cause"] if c).items())),
                    ws_union=rec["ws_union"], queues=rec["queues"])


def _hist(c: collections.Counter) -> dict:
    return {str(k): int(v) for k, v in sorted(c.items())}


def _rank(c: collections.Counter, p: int):
    """The p-th percentile of a {value: count} histogram, nearest rank: the value at rank ceil(p n / 100) of its n counts
    in ascending order (in integers); None when empty."""
    n = sum(c.values())
    if not n:
        return None
    k, acc = -(-p * n // 100), 0
    for v in sorted(c):
        acc += c[v]
        if acc >= k:
            return int(v)
