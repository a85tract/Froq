"""Rule 30's causal verdict-time estimator (gap analysis; section 9).

H-lead and H-nci-lead trigger only if the estimated number of rounds left until the value's verdict exceeds lambda = d
when the trigger would hold. The estimate reads state the controller observes and nothing else:
- the windows the value's producer instruction is decoded in (SWIPER-SIM's sliding windows: every window whose parent
  instructions include the producer), each in one state: waiting for data, queued, in service, decoded and waiting on
  a predecessor's commit, or verified;
- a window's latency class (the latency table's rule on its volume) and the rounds it has spent in service;
- its position in the decoder queue, and the busy and free full decoders (c of them);
- the predecessors in the window DAG whose commit its verification waits.

With mu_k the table's mean service time of class k in rounds (the mean of max(1, ceil(sample)), the service the
simulator realises), the formulas are rule 30's:
- waiting for data: (end - now) + mu_k + Wq(the current queue), with end the round the window's data are complete
  under the windowing (its commit regions' end, plus d rounds of forward buffer where the patch continues);
- queued: Wq(the windows ahead of it) + mu_k;
- in service: max(0, mu_k - elapsed);
- decoded, waiting on a predecessor's commit: 0 for itself;
- Wq(S) = (sum of mu over the windows in S + sum over busy decoders of max(0, mu - elapsed)) / c, and 0 with unlimited
  decoders (a ready window starts at once);
- a window's estimate is the maximum of its own and its unverified predecessors' (they can decode at the same time),
  and the value's is the maximum over its windows, plus the delivery path.

The estimator never reads a sampled duration (the decoder's remaining-rounds counters, `_active_window_progress`'s
values) or a window that has not arrived. `froq.test_h3` checks both: permuting the sampled durations of the decodes in
service, and adding or removing windows that have not arrived, leave every estimate unchanged.

The decoder's queue under a cap is oldest window first (`froq.device._OldestFirst`): the windows ahead of a queued
window are the queued windows with a smaller index. `estimate` is the pure function over a `Snapshot`; `snapshot` reads
it from a running device (the simulator's window manager and decoder, through `froq.device.LINK`)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

WAITING, QUEUED, SERVICE, DECODED, VERIFIED = "waiting_for_data", "queued", "in_service", "decoded", "verified"


@dataclass
class Window:
    """One window the value's decode needs.  `key` identifies it in the snapshot (the decoder's task index, or a
    negative key for a window not yet submitted); `preds` are the keys of the windows its verification waits on."""
    key: int
    state: str
    mu: float
    elapsed: float = 0.0          # in service: rounds since its decode started
    end: int = 0                  # waiting for data: the round its data are complete
    ahead: float = 0.0            # queued: the sum of mu over the queued windows ahead of it
    preds: tuple = ()


@dataclass
class Snapshot:
    """What the estimate reads: the value's windows (by key, including the predecessors they wait on), the queue's
    total mu, the busy decoders' (mu, elapsed), the number of full decoders (None = unlimited), the round, the path,
    and whether the verdict is already decoded (decoded_at >= 0)."""
    now: int
    windows: dict = field(default_factory=dict)
    roots: tuple = ()
    queue_mu: float = 0.0
    busy: tuple = ()
    c: int | None = None
    path: int = 0
    decoded_at: int = -1


def wq(snap: Snapshot, ahead_mu: float) -> float:
    """Rule 30's queue term: (the mu of the windows ahead + the busy decoders' remaining mean service) / c."""
    if not snap.c:
        return 0.0
    return (ahead_mu + sum(max(0.0, mu - el) for mu, el in snap.busy)) / snap.c


def own(snap: Snapshot, w: Window) -> float:
    """A window's own estimate, by its state."""
    if w.state == WAITING:
        return max(0, w.end - snap.now) + w.mu + wq(snap, snap.queue_mu)
    if w.state == QUEUED:
        return wq(snap, w.ahead) + w.mu
    if w.state == SERVICE:
        return max(0.0, w.mu - w.elapsed)
    return 0.0                                                         # decoded (waits on predecessors) or verified


def estimate(snap: Snapshot) -> float:
    """Rounds until the value's verdict: the maximum over its windows of max(own, its predecessors'), plus the path.
    Every predecessor chain is followed to its end, however long, with an explicit stack; a
    window met again on the current chain (no DAG has one) adds nothing further."""
    if snap.decoded_at >= 0:
        return float(max(0, snap.decoded_at + snap.path - snap.now))
    memo: dict = {}

    def value(k) -> float:
        w = snap.windows.get(k)
        return 0.0 if w is None or w.state == VERIFIED else own(snap, w)

    def total(root) -> float:
        if root in memo:
            return memo[root]
        on_chain, stack = {root}, [(root, iter(_preds(snap, root)))]
        acc = {root: value(root)}
        while stack:
            k, it = stack[-1]
            p = next(it, None)
            if p is None:
                stack.pop(); on_chain.discard(k)
                memo[k] = acc.pop(k)
                if stack:
                    parent = stack[-1][0]
                    acc[parent] = max(acc[parent], memo[k])
                continue
            if p in memo:
                acc[k] = max(acc[k], memo[p])
            elif p not in on_chain:
                on_chain.add(p); acc[p] = value(p)
                stack.append((p, iter(_preds(snap, p))))
        return memo[root]

    return max((total(k) for k in snap.roots), default=0.0) + snap.path


def _preds(snap: Snapshot, k):
    w = snap.windows.get(k)
    return () if w is None or w.state == VERIFIED else w.preds


# ------------------------------------------------------------------------------------------------ reading a device
class Reader:
    """Reads Snapshots from a running device.  The per-round parts (the busy decoders, the queue order and its prefix
    sums, the producers' unsubmitted windows) are built once per round on first use."""

    def __init__(self, dev, link):
        self.dev, self.link = dev, link
        self._round = None
        self._mu_cache: dict = {}

    # -- the latency table's mean service of a volume, without the table's own range record (`volume_class` writes it)
    def mu(self, volume: int) -> float:
        cls = self._class(volume)
        if cls not in self._mu_cache:
            pmf = self.dev._eta_table.pmf_rounds(cls)
            self._mu_cache[cls] = sum(r * p for r, p in pmf.items())
        return self._mu_cache[cls]

    def _class(self, volume: int) -> int:
        t = self.dev._eta_table
        return min(max(t.floor, math.ceil(volume / t.d)), t.classes[-1])

    def _refresh(self):
        dev, now = self.dev, self.dev.current_round
        if self._round == now:
            return
        self._round = now
        dec = self.link.get("decoder")
        sim = self.link.get("simulator")
        self.dec, self.wm = dec, (getattr(sim, "_window_manager", None) if sim is not None else None)
        self.c = (dec.max_parallel_processes or None) if dec is not None else None
        busy, self._busy_keys = [], set()
        if dec is not None:
            for t in dec._active_window_progress:                        # the KEYS only: which windows are in service
                task = dec._get_task(t)
                busy.append((self.mu(task.window.total_spacetime_volume()), float(dec._current_round - task.decoding_start_time)))
                self._busy_keys.add(t)
        self.busy = tuple(sorted(busy))                                  # (sorted: no dependence on the dict's order)
        q = sorted(dec._pending_decode_tasks) if dec is not None else []
        self._queue = q
        self._qpos = {t: i for i, t in enumerate(q)}
        self._qprefix, s = [0.0], 0.0
        for t in q:
            s += self.mu(dec._get_task(t).window.total_spacetime_volume())
            self._qprefix.append(s)
        self.queue_mu = s
        # windows not yet submitted to the decoder that hold a producer's rounds: the window manager's unconstructed
        # windows, and the window builder's rounds not yet in any window (by instruction)
        self._unbuilt: dict = {}
        if self.wm is not None:
            for wdw, idx in self.wm._unconstructed_window_indices.items():
                for i in wdw.parent_instr_idx:
                    if i >= 0:
                        self._unbuilt.setdefault(i, []).append(("window", idx, wdw))
            b = self.wm.window_builder
            groups: dict = {}
            for ri in b._waiting_rounds:
                r = b._all_rounds[ri]
                if r is not None and r.instruction_idx >= 0:
                    g = groups.setdefault((r.instruction_idx, r.patch), [r.round, r.round])
                    g[0], g[1] = min(g[0], r.round), max(g[1], r.round)
            for (i, patch), (r0, r1) in sorted(groups.items()):
                self._unbuilt.setdefault(i, []).append(("rounds", patch, (r0, r1)))

    def _waiting(self, key: int, regions, buffers, d: int) -> Window:
        """A window waiting for data: its end under the sliding windowing (each commit region's end, plus a forward
        buffer of d rounds where the patch continues) and its class from the volume it will have."""
        end, vol = 0, sum(b.duration for b in buffers)
        for cr in regions:
            e = cr.round_start + cr.duration
            vol += cr.duration
            if not cr.discard_after:
                e += d
                if not any(b.patch == cr.patch and b.round_start == cr.round_start + cr.duration for b in buffers):
                    vol += d                                             # its forward buffer, not appended yet
            end = max(end, e)
        return Window(key=key, state=WAITING, mu=self.mu(vol), end=end)

    def snapshot(self, v) -> Snapshot:
        dev = self.dev
        self._refresh()
        snap = Snapshot(now=dev.current_round, queue_mu=self.queue_mu, busy=self.busy, c=self.c, path=dev.path,
                        decoded_at=(v.decoded_round if v.decoded_round >= 0 else -1))
        if snap.decoded_at >= 0:
            return snap
        dec, d, p = self.dec, dev.d_t, v.producer_idx
        roots, wins = [], snap.windows

        def task_window(t):
            if t in wins:
                return
            task = dec._get_task_or_none(t)
            if task is None:
                return
            if task.completed_decoding:
                st = VERIFIED if t not in dec._decoded_unverified_tasks else DECODED
                w = Window(key=t, state=st, mu=0.0)
            elif t in self._busy_keys:
                w = Window(key=t, state=SERVICE, mu=self.mu(task.window.total_spacetime_volume()),
                           elapsed=float(dec._current_round - task.decoding_start_time))
            else:
                i = self._qpos.get(t)
                w = Window(key=t, state=QUEUED, mu=self.mu(task.window.total_spacetime_volume()),
                           ahead=(self._qprefix[i] if i is not None else self.queue_mu))
            if w.state != VERIFIED:
                w.preds = tuple(sorted(x for x in dec._window_idx_dag.predecessors(t)))
            wins[t] = w
            for x in w.preds:
                task_window(x)

        if dec is not None:
            for t in sorted(dec._instruction_tasks.get(p, ())):
                roots.append(t)
                task_window(t)
        neg = -1
        for kind, a, b in self._unbuilt.get(p, ()):
            if kind == "window":
                w = self._waiting(neg, b.commit_region, b.buffer_regions, d)
                w.preds = tuple(sorted(x for x in self.wm.window_dag.predecessors(a) if dec is not None and dec._get_task_or_none(x) is not None))
            else:                                                        # rounds not yet in a window: one commit region of at
                r0, r1 = b                                               # least d rounds on the patch
                w = Window(key=neg, state=WAITING, mu=self.mu(max(r1 + 1, r0 + d) - r0 + d), end=max(r1 + 1, r0 + d) + d)
            wins[neg] = w
            roots.append(neg)
            for x in w.preds:
                task_window(x)
            neg -= 1
        if not roots:                                                    # nothing visible yet: one window after the producer's
            e = dev.schedule_instructions[p].end_round                   # end (conservative: it cannot be decoded sooner)
            e = e if e >= 0 else snap.now
            wins[neg] = Window(key=neg, state=WAITING, mu=self.mu(2 * d), end=e + d)
            roots.append(neg)
        snap.roots = tuple(roots)
        return snap

    def estimate(self, v) -> float:
        return estimate(self.snapshot(v))
