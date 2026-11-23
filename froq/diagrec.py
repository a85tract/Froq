"""(diagnostics; the results review's findings 4, 6-8 and 12): `--diag-records`, records only.

What the diagnosis of the sx library's cell pattern needs beside the r7 and records, and nothing the run reads:

- repairs: one row per repair that the decision's r7 records count (a wrong default's script S at its lane, k = 0, or a
  wrong commit's repair R, k = 1): the verdict's round, the script's start and end, the settlement's end (the decoded
  delivery under the hold), and within the settlement [script end, done) the rounds in which a window of the script
  waited for a decoder (ready, not started) and the rounds in which one was decoding; beside them the charges the
  decision used for that lane as its profile priced them: the start-wait residual (R2's W_0, or W_1 for k = 1), the
  script's rounds (S or R), its settlement (XS or XR), its decoder demand N and work U, kappa, the shortfall
  max(0, N - F_l) and the load term kappa x shortfall + R3's work at the decision's load bin l, and the same lane's
  script and settlement in the profile the run executes (they differ only under a price file: arm D).
- the planner: each instruction the start-time planner held ("planned") and then started, at its start round: its
  planned start, the successor whose predicted start bound it (upstream's recurrence starts a startup instruction its
  duration before its earliest successor's predicted start), that successor's predicted start, and the predecessor
  that bound the successor (the latest predicted end among its other predecessors), with that predecessor's state
  (running, ended or waiting) and whether it is a spliced script instruction. An instruction with no walk in its start
  round (the planner's "surely later" shortcut) has no successor.
- holds: each held instruction's waiting rounds by the dispatch's raw reason (`tally`, every round counted, not
  only the last reason), as [idx, {reason: rounds}].

The rows go to a side file beside the result (`<result>.diag.json.gz`); the result carries its digest. Off (the
default) none of this exists. On, the run's outcome is the same: the hooks only read."""
from __future__ import annotations

import collections

REPAIR_FIELDS = ["vid", "k", "lane", "l", "verdict", "start", "end", "done", "q_settle", "dec_settle", "windows", "attempts",
                 "W", "S", "XS", "N", "U", "kappa", "short", "load", "S_run", "XS_run", "form"]
PLAN_FIELDS = ["idx", "start", "planned", "succ", "succ_start", "pred", "pred_end", "pred_state", "pred_script"]


def covered(iv, a, b) -> int:
    """The rounds of [a, b) inside the union of the half-open intervals iv."""
    if a is None or b is None or b <= a:
        return 0
    n, cur = 0, a
    for s, e in sorted(iv):
        s, e = max(s, cur), min(e, b)
        if e > s:
            n += e - s
            cur = e
    return n


class DiagRecords:
    def __init__(self):
        self.rwin: dict = {}          # vid -> [(task, ready round, start round, its script instructions)] per decode start
        self.repairs: list = []
        self.repair_extra: list = []  # (the fields a repair row adds under --w2-sd, wager2.SD_FIELDS; none when off)
        self.plan: list = []
        self.planned_seen: set = set()

    # -- the decoder: a window's decode starts (the latency draw), with the round it entered the pending queue
    def win_start(self, dev, task, tid, added):
        rl = dev._recovery_local
        own = collections.defaultdict(list)
        for i in task.window.parent_instr_idx:
            x = rl.get(i)
            if x is not None:
                own[x[0]].append(i)
        if own:
            st = int(task.decoding_start_time)
            for vid, ins in own.items():
                self.rwin.setdefault(vid, []).append((tid, added, st, tuple(ins)))

    def delivery(self, dec, rec, vid):
        """(q_settle, dec_settle, windows, attempts): the windows of the repair's script, their decode starts, and the
        rounds of [script end, done) in which one of them waited for a decoder or was decoding (the last attempt's run
        ends at the task's completion, else at done)."""
        ins = set(rec.get("instrs", ()))
        end, done = rec.get("recovery_end_round"), rec.get("done_round", -1)
        wait, last = [], {}
        n = 0
        for tid, added, st, own in self.rwin.get(vid, ()):
            if not ins.intersection(own):
                continue
            n += 1
            if added is not None and st > added:
                wait.append((added, st))
            last[tid] = st
        run = []
        for tid, st in last.items():
            t = dec._get_task_or_none(tid) if dec is not None else None
            c = getattr(t, "decoding_completion_time", -1) if t is not None else -1
            run.append((st, c if c is not None and c >= st else done))
        if done is None or done < 0:
            return None, None, len(last), n
        return covered(wait, end, done), covered(run, end, done), len(last), n

    # -- the planner: at the start of an instruction it held
    def plan_start(self, dev, idx, st):
        preds, succs = dev._adj()
        pd, now, si = dev._plan_durations, int(dev.current_round), dev.schedule_instructions
        j = js = None
        for s in succs.get(idx, ()):
            if s in st and (js is None or st[s] < js):
                j, js = s, st[s]
        k = ke = kst = None
        if j is not None:
            act = dev._active_instructions
            for p in preds.get(j, ()):
                if p == idx:
                    continue
                if p in st:
                    e = st[p] + pd[p]
                elif si[p].end_round != -1:
                    e = si[p].end_round + 1
                elif p in act:
                    e = act[p] + now
                else:
                    continue
                if ke is None or e > ke:
                    k, ke = p, e
            if k is not None:
                kst = "running" if k in act else ("ended" if si[k].end_round != -1 else "waiting")
        self.plan.append([int(idx), now, st.get(idx), j, js, k, ke, kst, None if k is None else int(k >= dev.n_original)])

    def dump(self, dev) -> dict:
        r17 = getattr(dev, "_r17", None)
        hb = getattr(r17, "held_by", None) if r17 is not None else None
        return dict(format="scoutq-t032j-diag/1", n_original=int(dev.n_original), repair_fields=REPAIR_FIELDS + list(self.repair_extra), repairs=self.repairs,
                    plan_fields=PLAN_FIELDS, plan=self.plan,
                    held_by=None if hb is None else [[int(i), dict(sorted(c.items()))] for i, c in sorted(hb.items())])

    def summary(self) -> dict:
        return dict(repairs=len(self.repairs), plan=len(self.plan),
                    repairs_open=sum(1 for r in self.repairs if r[7] is None or r[7] < 0))
