"""Round 10's bounded write queue (sections 6.2-6.5 on the plan
gate's findings F3 and F4; the record). Off by default: a run without
`--wq` builds none of this and is. It needs round 9's bounded check (`--r8-event-budget`), whose port it extends.

Port 2 owns every write to a table a check reads (the window, the cell table, the frame table, the chain, cover,
placement and hold marks). Each write is one entry of a FIFO of C entries; port 2 retires up to w entries a round, in
order. Every read sees the tables with the queued writes applied (forwarding), so the run's semantic state changes when
its event happens, as in r9, and the queue changes timing only: admissions wait for capacity, a request too large for
the queue takes its safe answer, and every dependent read of a check pays f more cycles (c1 + f an access).

The invariant: O + R <= C at every point, where O is the entries queued and R the writes owed (reserved and not yet
issued). Every write is issued against a reservation (`issue`); an owner that issues more than it reserved fails the
run (`strict`). Reservations are made only by admission (`admit`, which checks O + R + n <= C) or moved between owners
(`move`, which leaves O + R unchanged), so O + R grows only at an admission that fits.

Owners: ("ev", key), a grant's service (its batch and its consequences, settled when it is evaluated); ("in", idx), an
instruction's start and completion (and a script DISCARD's placement marks); ("fr", vid), a frame's consequences (its
verdict, its end, its hold marks); ("sc", vid), a frame's settlement scripts. Long-lived owners (frames, scripts,
members and orphans) are summed in `long`, which a grant's acceptance keeps at most C - H (`long_fits`), so that once the
queue drains and the short-lived reservations (started ordinary instructions' completions) are issued, any admission of
at most H entries fits. Admissions larger than H are refused outright (`too_big`), never waited."""
from __future__ import annotations

import collections

INF = float("inf")
EV1 = ("S", "D", "V", "P")                              # one-cycle events: a start, a completion, a verdict, a placement
ADV_KEEP = 4096                                         # the started ADVANCE steps a run's records list (all are summed)
G_KEEP = 200000                                         # G's step rows a run lists (the rows past it are counted)
G_KIND = {2: "port", 3: "wq", 4: "check", 6: "dep"}     # (a step record's column -> its charge's kind)


def parse(spec) -> dict | None:
    """`--wq C=..,w=..,e=..,f=..,b=..,H=..` (C, e, b and H may be `inf`): None when off."""
    if spec in (None, "", "off"):
        return None
    out = dict(C=None, w=286, e=None, f=0, b=None, H=None)
    for part in str(spec).split(","):
        k, _, v = part.partition("=")
        k = k.strip()
        assert k in out, ("--wq: unknown key", k, spec)
        out[k] = None if v.strip() in ("inf", "none", "") else int(v)
    return out


def tag(d: dict | None) -> str:
    if d is None:
        return "off"
    return ",".join("%s=%s" % (k, "inf" if d.get(k) is None else d[k]) for k in ("C", "w", "e", "f", "b", "H"))


class Overdraw(AssertionError):
    pass


class WQ:
    """One run's write queue (`ScoutDeviceManager._wq`)."""
    r17 = None                                          # (---records: the entries held a round; records only)
    _wf = None                                          # (--ctrl-slow s: port 2's w / s, a Fraction; None: w)

    def set_slow(self, s):
        """(`--ctrl-slow s`; plan v2 4.2 A): port 2 retires w / s entries a round, with carry: round r has
        floor((r + 1) w / s) - floor(r w / s) cycles, so every span of rounds gets its exact share. Off: w a round."""
        from fractions import Fraction
        assert Fraction(s) > 1 and self._wf is None, s
        self._wf = Fraction(self.w) / Fraction(s)

    def _wspan(self, a: int, n: int) -> int:
        """Port 2's cycles in the n rounds from round a: w each, or under --ctrl-slow the carried share."""
        if self._wf is None:
            return self.w * n
        return (a + n) * self._wf.numerator // self._wf.denominator - a * self._wf.numerator // self._wf.denominator

    def __init__(self, C=None, w: int = 286, e=None, f: int = 0, b=None, H=None, strict: bool = True):
        self.C = INF if C is None else int(C)
        self.w = int(w)
        self.e = INF if e is None else int(e)
        self.f = int(f)
        self.bmax = INF if b is None else int(b)        # a grant's batch: its update part, at most this (a stop past it)
        self.H = (0 if self.C == INF else self.C // 2) if H is None else int(H)   # the headroom long-lived owners leave
        assert self.C == INF or (self.H <= self.C and self.bmax != INF), ("H above C, or C finite and b not", self.H, self.C, b)
        self.strict = strict
        self.O = 0
        self.fifo = collections.deque()                 # [issue round, n]: the entries queued, oldest first
        self.R = 0
        self.own: dict = {}                             # owner -> writes reserved and not yet issued
        self.lng: set = set()                           # the long-lived owners
        self.long = 0                                   # their reservations, summed
        self.aged_wait = None                           # the aged request that waits for capacity: served first
        self.stats = collections.Counter()
        self.h_dem = collections.Counter()              # entries issued in a round -> rounds
        self.h_O = collections.Counter()                # O at a round's end, after retirement -> rounds
        self.h_OR = collections.Counter()               # the round's peak of O + R -> rounds
        self.h_long = collections.Counter()             # long at a round's end -> rounds
        self.h_lat = collections.Counter()              # rounds from issue to retirement -> entries
        self.h_held = collections.defaultdict(collections.Counter)   # kind -> rounds a start or grant waited for capacity
        self.ox_max = collections.Counter()             # kind -> the largest consequences an admission reserved
        self.batch_max = collections.Counter()          # kind -> the largest batch issued
        self.peak = dict(O=0, O_round=None, OR=0, OR_round=None, long=0, long_round=None)
        self.waiting: dict = {}                         # key -> [kind, first round refused, last round]
        self._rnd, self._dem, self._pOR, self._erm = None, 0, 0, INF
        self.adv: dict = {}                             # vid -> step -> [decision, answer, port, queue, check, last round, dep]
        self.adv_orph: dict = {}                        # (vid, step, n) -> an ended frame's collected record, until a member starts
        self.adv_orph_of: dict = {}                     # instruction -> the kept records its first start ends (every owner's)
        self.adv_done = []                              # started steps: [vid, step, decision, answer, start, port, queue, check, dep]
        self.adv_sum = collections.Counter()
        self.adv_key: dict = {}                         # vid -> (admitted, limit) at its last decision
        self.g_st: dict = {}                            # G (records only): (vid, rebase, step) -> [decision, admitted, admissions,
                                                        #  answer, start, taken back, charges [first round, rounds, kind]]
        self.g_ep: dict = {}                            # G: vid -> its walk's rebases (H's early splice numbers its steps anew)
        self.on_over = None                             # G (records only): called once, at the first round O + R passed C, with
                                                        #  the write that passed it ((owner, kind), `issue`'s; None from `admit`)
        self.h_adv = collections.defaultdict(collections.Counter)
        self.h_ev = collections.defaultdict(collections.Counter)   # kind -> an event's cycles -> events (e finite)
        self.h_cyc = collections.Counter()              # a round's event cycles plus port 1's writes -> rounds (e finite)
        self.port1 = None                               # (the device's: port 1 waits an event's cycles past its first)
        self._p1f = collections.Counter()               # (later rounds -> port 1's writes of a service still running then)
        self._lane, self.lane_max, self.lane_rounds = 0, 0, 0   # (the event lane's backlog, cycles carried to the next round)
        self.first_over = None                          # (not strict: the first round O + R passed C)
        self._cyc = 0

    # -- capacity
    def fits(self, n) -> bool:
        return self.O + self.R + n <= self.C

    @property
    def holds(self) -> bool:
        """Can the queue hold a start (C or e finite)?  Unbounded, it never refuses one."""
        return self.C != INF or self.e != INF

    def long_fits(self, n) -> bool:
        return self.long + n <= self.C - self.H

    def _note(self, now, who=None):
        s = self.O + self.R
        if s > self.C and self.first_over is None:
            self.first_over = now
            if self.on_over is not None:
                self.on_over(now, who)
        if s > self._pOR:
            self._pOR = s
        if s > self.peak["OR"]:
            self.peak.update(OR=s, OR_round=now)
        if self.O > self.peak["O"]:
            self.peak.update(O=self.O, O_round=now)
        if self.long > self.peak["long"]:
            self.peak.update(long=self.long, long_round=now)

    def _add(self, owner, n, long):
        if n <= 0:
            return
        self.own[owner] = self.own.get(owner, 0) + n
        if long or owner in self.lng:
            self.lng.add(owner)
            self.long += n

    def admit(self, owner, n: int, now, long: bool = False) -> bool:
        """Reserve n writes for `owner` if they fit: O + R + n <= C.  False, and nothing changes, otherwise."""
        if not self.fits(n):
            return False
        self._add(owner, n, long)
        self.R += n
        self._note(now)
        return True

    def has(self, owner) -> int:
        return self.own.get(owner, 0)

    def move(self, src, dst, n: int, long: bool = False):
        """Move n reserved writes from src to dst: O + R is unchanged."""
        if n <= 0:
            return
        have = self.own.get(src, 0)
        if have < n:
            self._overdraw("move:%s" % (src[0],), src, n, have)
            n = have
        self.own[src] = have - n
        if src in self.lng:
            self.long -= n
        if not self.own[src]:
            del self.own[src]
            self.lng.discard(src)
        self._add(dst, n, long)

    def issue(self, owner, n: int, kind: str, now):
        """n writes of `kind` enter the queue, drawn from `owner`'s reservation."""
        if n <= 0:
            return
        if self.O + self.R >= self.C:
            self.stats["at_capacity:" + kind] += n      # (records: owed writes that entered with the queue full)
        have = self.own.get(owner, 0)
        if have < n:
            self._overdraw(kind, owner, n, have)
            self.R += n - have                          # (not strict: counted, and the run goes on as if they were reserved)
            if owner in self.lng:
                self.long += n - have
            self.own[owner] = have = n
        self.own[owner] = have - n
        if owner in self.lng:
            self.long -= n
        if not self.own[owner]:
            del self.own[owner]
            self.lng.discard(owner)
        self.R -= n
        self.O += n
        if self.fifo and self.fifo[-1][0] == now:
            self.fifo[-1][1] += n
        else:
            self.fifo.append([now, n])
        self._dem += n
        self.stats["w:" + kind] += n
        self._note(now, (owner, kind))
        if kind in EV1:
            self.event(kind, n)

    def release(self, owner) -> int:
        """`owner`'s unused reservation goes back."""
        n = self.own.pop(owner, 0)
        if owner in self.lng:
            self.lng.discard(owner)
            self.long -= n
        self.R -= n
        if n:
            self.stats["released:" + owner[0]] += n
        return n

    def _overdraw(self, kind, owner, n, have):
        self.stats["overdraw:" + kind] += 1
        self.stats["overdraw_writes"] += n - have
        if self.strict:
            raise Overdraw(("an owner issued more writes than it reserved", kind, owner, n, have))

    # -- rounds
    def round(self, now):
        """Once a round, at its start: port 2 retired up to w entries during the round before, oldest first; that
        round's records close."""
        if self._rnd == now:
            return
        if self.r17 is not None and self._rnd is not None:
            self.r17.occ(self._rnd, dict(write_queue=self.O))       # (records only: O before the retirement,
                                                                    #  the round's most, as only `issue` raises it)
        if self._rnd is not None:
            k = min(self.O, self._wspan(self._rnd, max(1, now - self._rnd)))   # (a round the device skipped retired its w too)
            self.O -= k
            while k:
                c = self.fifo[0]
                t = min(k, c[1])
                self.h_lat[self._rnd - c[0]] += t
                c[1] -= t
                k -= t
                if not c[1]:
                    self.fifo.popleft()
            self.h_dem[self._dem] += 1
            self.h_O[self.O] += 1
            self.h_OR[self._pOR] += 1
            self.h_long[self.long] += 1
            if self.e != INF:
                self.h_cyc[self._cyc] += 1
                self._lane = max(0, self._lane + self._cyc - self._wspan(self._rnd, max(1, now - self._rnd)))
                if self._lane:
                    self.lane_rounds += 1
                    self.lane_max = max(self.lane_max, self._lane)
        last, self._rnd, self._dem, self._pOR, self._cyc = self._rnd, now, 0, self.O + self.R, 0
        if last is not None and now == last + 1:
            self._cyc += self._p1f.pop(now, 0)                          # (the round after the last: only its bucket is due)
        else:
            for r in [r for r in self._p1f if r <= now]:                # (rounds skipped: theirs too)
                self._cyc += self._p1f.pop(r)
        self._erm = self.e * self._wspan(now, 1)        # (the round's enqueue room for admitted starts: e a cycle)

    def e_take(self, n) -> bool:
        if n > self._erm:
            return False
        self._erm -= n
        return True

    # -- events: the event logic makes one event a cycle, its writes on e lanes
    def event(self, kind, n: int, rows=()) -> int:
        """An event of n writes (any but a grant's batch, which port 1 writes during its charged service) takes
        max(ceil(n / e), L8) cycles, L8 the largest number of its window rows (`rows`: instruction indices) with the same
        row mod 8.  Readers must not see it half made: port 1 waits its cycles past the first (`port1`).  Records the
        cycles per kind and per round.  Only when e is finite."""
        if self.e == INF or n <= 0:
            return 0
        c = self.ev_cyc(n, rows)
        self.h_ev[kind][c] += 1
        self._cyc += c
        if c > 1 and self.port1 is not None:
            self.port1(c - 1)
        return c

    def ev_cyc(self, n: int, rows=()) -> int:
        """An event's cycles: max(ceil(n / e), L8) (e finite)."""
        l8 = max(collections.Counter(r % 8 for r in rows).values(), default=0)
        return max(-(-int(n) // self.e), l8)

    def p1(self, n: int, s: int = 0, a: int = 0, room: int = 0, R: int = 0):
        """Port 1's writes, a grant's batch of n (one an access of s cycles), each can delay one event by a cycle.  F3:
        recorded by their position in the service, in the round each starts in: the update part follows the walk's a
        accesses, so write j starts s (a + j) cycles into the service, which had `room` cycles of this round and then
        R a round (`HWLimits.r9_take`).  F7: nothing when e is unbounded or nothing is written, and no empty rounds."""
        n = int(n)
        if self.e == INF or n <= 0:
            return
        if s <= 0 or R <= 0:
            self._p1_add(0 if room > 0 or R <= 0 else 1, n)
            return
        rnd = lambda x: 0 if x < room else 1 + (x - room) // R
        for r in range(rnd(s * a), rnd(s * (a + n - 1)) + 1):
            lo, hi = (0, room) if r == 0 else (room + (r - 1) * R, room + r * R)
            self._p1_add(r, min(n, -(-hi // s) - a) - max(0, -(-lo // s) - a))

    def _p1_add(self, r: int, m: int):
        if m > 0:
            if r:
                self._p1f[self._rnd + r] += m
            else:
                self._cyc += m

    # -- waiting (records): a start or grant the queue held, from its first hold to its admission
    def wait(self, kind, key, now):
        w = self.waiting.get(key)
        if w is None:
            self.waiting[key] = [kind, now, now]
            self.stats["held:" + kind] += 1
        else:
            w[2] = now
        self.stats["held_rounds:" + kind] += 1

    def keep(self, key, now):
        """A held request that goes on through its own checks (F1's, its age kept: `ScoutDeviceManager._wq_keep`): its
        wait goes on, so `prune` does not end it; no refusal is counted."""
        w = self.waiting.get(key)
        if w is not None:
            w[2] = now

    def served(self, key, now):
        w = self.waiting.pop(key, None)
        if w is not None:
            self.h_held[w[0]][now - w[1]] += 1
        if self.aged_wait == key:
            self.aged_wait = None

    def prune(self, now):
        for k in [k for k, w in self.waiting.items() if w[2] < now - 2]:
            w = self.waiting.pop(k)
            self.stats["left:" + w[0]] += 1

    # -- records: per started ADVANCE step (plan v2 2.2 and 6.2), for the decision's overhead price c_o = A / S
    def adv_decide(self, vid, a: int, lim: int, now):
        """Frame vid may admit its steps a to lim - 1 (its policy's decision this round): a step's decision round is the
        first round of the episode in which it stays within the limit; one the limit leaves before it starts is dropped."""
        if self.adv_key.get(vid) == (a, lim):
            return                                      # (unchanged since its last decision)
        self.adv_key[vid] = (a, lim)
        recs = self.adv.setdefault(vid, {})
        for q in [q for q in recs if q >= lim]:
            del recs[q]
            self.adv_sum["dropped"] += 1
        for q in range(a, lim):
            if q not in recs:
                recs[q] = [now, None, 0, 0, 0, None, 0]
                g = self._g(vid, q)
                if g[0] is None:
                    g[0] = now

    def adv_held(self, vid, a: int, b: int, now, why, apart=()):
        """The block a to b - 1's counted FORWARD did not admit it this round: "port" (queued behind the port's service,
        in service past its round, refused by the port, or stopped by the check's budget or window, and asking again),
        "wq" (the write queue's admission) or "check" (the check's rules refused it: the workspace guard or rule (a), a
        wait for something else, not attributable).  Each step is charged the round once.  `apart` (the code check's
        F5): the block's steps that wait this round for something outside the request anyway (device.py's
        `_wq_adv_apart`: a predecessor or a completion not yet ended, a cell in use); their port or queue round is
        recorded as "dep", not attributable."""
        col = {"port": 2, "wq": 3, "check": 4}.get(why)
        recs = self.adv.get(vid)
        if col is None or not recs:
            return
        for q in range(a, b):
            r = recs.get(q)
            if r is not None and r[5] != now:
                c = 6 if col != 4 and q in apart else col
                r[c] += 1
                r[5] = now
                self._g_charge(vid, q, now, G_KIND[c])

    def adv_answer(self, vid, a: int, b: int, now):
        recs = self.adv.get(vid)
        for q in range(a, b):
            r = recs.get(q) if recs else None
            if r is not None:
                r[1] = now
                g = self._g(vid, q)
                if g[3] is None:
                    g[3] = now

    def adv_start(self, vid, q, now):
        """A member of step q of frame vid starts: the step's first start ends its record, if its grant was collected."""
        recs = self.adv.get(vid)
        r = recs.pop(q, None) if recs else None
        if r is not None:
            self._adv_end(vid, q, r, now)

    def adv_orph_start(self, i, now):
        """Instruction i starts: every record an ended frame's end kept for a step of i's (`adv_drop`) ends here, once,
        whichever frames owned i since (the code check's F6, and its re-check: a later frame can take i in and end too);
        a record another member's start ended already is gone."""
        for key in sorted(self.adv_orph_of.pop(i, ())):
            r = self.adv_orph.pop(key, None)
            if r is not None:
                self.adv_sum["orphan_started"] += 1
                self._adv_end(key[0], key[1], r, now)
                self.g_start(key[0], key[1], now)

    def _adv_end(self, vid, q, r, now):
        if r[1] is None:
            self.adv_sum["started_unanswered"] += 1                     # (admitted otherwise: its registration)
            return
        s = self.adv_sum
        s["S"] += 1
        s["A_port"] += r[2]
        s["A_wq"] += r[3]
        s["A_check"] += r[4]
        s["A_dep"] += r[6]
        self.h_adv["answer"][r[1] - r[0]] += 1
        self.h_adv["start"][now - r[1]] += 1
        self.h_adv["attributable"][r[2] + r[3]] += 1
        if len(self.adv_done) < ADV_KEEP:
            self.adv_done.append([vid, q, r[0], r[1], now, r[2], r[3], r[4], r[6]])

    def adv_drop(self, vid, keep=None):
        """Frame vid ends, or never went live: its records go (dropped), but those of the collected steps in `keep`
        ({step: its members the frame's end leaves authorized until they start}, device.py's `_r8_orph`; the code check's
        F6): each stays, with its association to every one of those members, until the first of them starts
        (`adv_orph_start`).  The associations outlast later owners: another frame may take a member in and end too."""
        recs = self.adv.pop(vid, None) or {}
        keep = keep or {}
        for q, r in recs.items():
            if keep.get(q) and r[1] is not None:
                key = (vid, q, self.adv_sum["kept"])
                self.adv_sum["kept"] += 1
                self.adv_orph[key] = r
                for i in keep[q]:
                    self.adv_orph_of.setdefault(i, set()).add(key)
            else:
                self.adv_sum["dropped"] += 1
        self.adv_key.pop(vid, None)

    def adv_summary(self) -> dict:
        s = self.adv_sum
        h = lambda c: {str(a): n for a, n in sorted(c.items())}
        return dict(S=s["S"], A=s["A_port"] + s["A_wq"], A_port=s["A_port"], A_wq=s["A_wq"], A_check=s["A_check"],
                    A_dep=s["A_dep"], orphan_started=s["orphan_started"], kept=s["kept"],
                    dropped=s["dropped"], started_unanswered=s["started_unanswered"],
                    open_end=sum(len(r) for r in self.adv.values()) + len(self.adv_orph),   # (records open at the end:
                    open_answered=sum(r[1] is not None for rs in self.adv.values() for r in rs.values()) + len(self.adv_orph),
                                                                                    #  the rest are decisions never answered)
                    hist={k: h(c) for k, c in sorted(self.h_adv.items())},
                    fields=["vid", "step", "decision", "answer", "start", "port", "wq", "check", "dep"], steps=self.adv_done,
                    truncated=s["S"] > len(self.adv_done))

    # -- G's step rows (plan v2 A1.3 item 5; records only, nothing reads them back)
    def _g(self, vid, q):
        return self.g_st.setdefault((vid, self.g_ep.get(vid, 0), q), [None, None, 0, None, None, 0, []])

    def g_rebase(self, vid):
        """Frame vid now carries a rebased walk (H's early splice, `fr.rebased`): its steps are numbered from 0 again, and
        their rows are the next walk's (`rebase` 1)."""
        self.g_ep[vid] = self.g_ep.get(vid, 0) + 1

    def _g_charge(self, vid, q, now, kind):
        ch = self._g(vid, q)[6]
        if ch and ch[-1][2] == kind and ch[-1][0] + ch[-1][1] == now:
            ch[-1][1] += 1
        else:
            ch.append([now, 1, kind])

    def g_admit(self, vid, a: int, b: int, now):
        """Frame vid admits its steps a to b - 1 (`_defer_admit`, wherever the admission comes from)."""
        for q in range(a, b):
            g = self._g(vid, q)
            if g[1] is None:
                g[1] = now
            g[2] += 1

    def g_back(self, vid, j: int, b: int):
        """Frame vid takes its admitted steps j to b - 1 back (`_defer_unadmit`)."""
        for q in range(j, b):
            self._g(vid, q)[5] += 1

    def g_start(self, vid, q, now):
        """A member of step q of frame vid starts: the step's first start."""
        if q is None:
            return
        g = self._g(vid, q)
        if g[4] is None:
            g[4] = now

    def g_summary(self) -> dict:
        """One row per step a frame's policy decided or its frame admitted, of its walk (`rebase`: 0, or 1 after H's early
        splice): the round of its first decision, of its first admission and how many, of its first collected grant
        (`answer`), of its first member's start, its take-backs, and each round its own request held it (`adv_held`), as
        runs [first round, rounds, kind]."""
        rows = [[v, h, q] + g[:6] + [g[6]] for (v, h, q), g in sorted(self.g_st.items())]
        return dict(fields=["vid", "rebase", "step", "decision", "admitted", "admissions", "answer", "start", "taken_back", "charges"],
                    charge_fields=["first", "rounds", "kind"], rows=rows[:G_KEEP], truncated=max(0, len(rows) - G_KEEP))

    # -- records
    def summary(self, pend=()) -> dict:
        h = lambda c: {str(a): n for a, n in sorted(c.items())}
        num = lambda x: None if x == INF else int(x)
        return dict(C=num(self.C), w=self.w, **({"w_slow": str(self._wf)} if self._wf is not None else {}),
                    e=num(self.e), f=self.f, b=num(self.bmax), H=self.H, first_over=self.first_over,
                    peak=dict(self.peak), O_end=self.O, R_end=self.R, long_end=self.long, owners_end=len(self.own),
                    demand_hist=h(self.h_dem), O_hist=h(self.h_O), OR_peak_hist=h(self.h_OR), long_hist=h(self.h_long),
                    latency_hist=h(self.h_lat), held_hist={k: h(c) for k, c in sorted(self.h_held.items())},
                    ox_max=dict(self.ox_max), batch_max=dict(self.batch_max), waiting_at_end=len(self.waiting),
                    stats=dict(sorted(self.stats.items())), adv=self.adv_summary(), ev=self.ev_summary(pend),
                    g_steps=self.g_summary())

    def ev_summary(self, pend=()) -> dict | None:
        """F3: with the open round, closed on copies (nothing retired), its pending events `pend` ((n, rows) each, the
        device's hold changes not yet charged) included."""
        if self.e == INF:
            return None
        h = lambda c: {str(a): n for a, n in sorted(c.items())}
        hc, lm, lr = collections.Counter(self.h_cyc), self.lane_max, self.lane_rounds
        if self._rnd is not None:
            cyc = self._cyc + sum(self.ev_cyc(n, rows) for n, rows in pend if n > 0)
            hc[cyc] += 1
            lane = max(0, self._lane + cyc - self._wspan(self._rnd, 1))
            if lane:
                lm, lr = max(lm, lane), lr + 1
        w = self.w if self._wf is None else self._wf                    # (--ctrl-slow: port 2's w / s)
        return dict(cycles={k: h(c) for k, c in sorted(self.h_ev.items())}, round_hist=h(hc),
                    round_max=max(hc, default=0), rounds_over_w=sum(n for c, n in hc.items() if c > w),
                    lane_max=lm, lane_rounds=lr)
