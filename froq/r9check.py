"""Round 9's bounded check (sections 4.2-4.5 on the plan gate's
findings 4 and 5; the record section 9). Off by default: a run without
`--r8-event-budget` builds none of this and is.

Rule (a)'s decisions stay. This mode bounds and charges the work the check port does for them:

- An event is one decision the check port makes: a registration's certificate (`reg`), a FORWARD's (`fwd`, the decision's
  what-ifs included), a revision's (`rev`), and the candidate guard's check of an ordinary instruction that puts a
  new patch on a guarded cell (`plc`, a new placement permission). `rel` is the same check for a release placement
  that no earlier grant covered (below). Every memory access an event makes is counted under the fixed
  representation of plan v2 4.2 (`ACCESS`); the count is the event's work.
- The budget: an event whose walk part would pass B accesses stops and takes the safe answer (wait, refuse, hold),
  as caps did; in this mode B replaces both caps (`R8_CAP`, `R7_GUARD_CAP`) inside events. A `rel` event's
  budget is B_rel, which no walk confined to the window can reach (`b_rel`), so a budget hit never refuses one.
- Release work is admitted without a walk: settlement scripts and admitted members as in (no check), and the
  covered placements of a live certificate's release chains (`cover`), each checked, when the grant that installed
  the certificate was made, against every live frame's holdable work, and counted from then on as a placement
  permission by every later certificate (`perms`). r9-guard-close: an authorization stands only if every placement its
  walks followed is covered (else the safe answer: nothing is installed); a grant that guards a cell later covers the
  live chains' placements onto it or waits (`_r9_guard_new`); and a cover lasts while any live frame's chains hold it
  (`frame_end`). r9-guard-close 2: a placement a script's DISCARD creates (the cell's next original instruction,
  which puts a new patch there because the script freed the cell) is release work too (`vac`, `_r9_vacate`): its
  patch's release is the release of the patch the script discarded, so the cell is held until that release as before.
  So a live frame's release placements are covered when they arise, and a `rel` check (an uncovered placement on a
  live chain) is what is left over, counted (`events`). r9-guard-close 4: a member a walk reads that can later become
  an ordinary instruction (its frame's end returns it unadmitted, a rebase drops it, a prepared rebase releases it) is
  a potential guarded placement: an authorization covers its placements onto guarded cells, or is refused.
- The window: W entries, in program order from the oldest unstarted original instruction (head) to the youngest one
  any check has read (tail), plus the entries every live frame reserved at its authorization for its settlement
  scripts (the longest script over the boundaries it may reach). A check that would read past the window stops
  (the safe answer); an authorization whose reservation does not fit waits. Reserved entries are never shared, so
  a settlement script never meets a full window.
- The port: an event's service is c0 + c1 x accesses cycles of the check port (hwlimits: the status port, improved).
  One that does not fit the round's room keeps the port busy into the next rounds (`HWLimits.r9_take`), and its
  answer is applied when its service ends: the requester asks again each round and gets the stored answer then
  (`tickets`). While the port is busy every other grant waits behind it. r9-guard-close 3: a stored answer applies
  only to the request it answered (`covers`), and an answer whose service has ended keeps the port until its requester
  takes it (`unapplied`). r9-guard-close 4: the port serves its requests in the round's order, as evaluated, except
  that a request the port has refused for K rounds is aged (`age_round`): the aged requests go first, oldest promotion
  first, before every fresh use of the port (`turn`).

Records (`summary`): accesses per event by kind (histograms of the walk part, the update part and the total), events
per round, budget and window hits by kind, the window's peak occupancy, release placements admitted without a walk,
the `rel` checks, the port's waits and multi-round services, and the second port's peak accesses per round."""
from __future__ import annotations

import collections
import math

INF = float("inf")
KINDS = ("reg", "fwd", "rev", "plc", "rel")
K_AGE = 4096                        # (r9-guard-close 4: a request the port has refused for this many rounds is aged)
WAIT = object()                     # (a grant's answer is not ready: the port is busy or the event is in service)

# the representation's per-operation access counts (plan v2 4.3)
ACCESS = dict(perm=1,               # root enumeration: a permission read
              cell=1,               # a (permission, cell) or a workspace cell's cell-table read (occupant, first unstarted
                                    # user, the mask of the live certificates whose workspace holds it)
              release=1,            # a release lookup (+1 when the release is a DISCARD in the cell table)
              visit=3,              # a node: the tag (probe and set), the entry read, and the tag's clearing at the walk's end
              edge=2,               # a predecessor: the tag probe and the stack push
              next_user=1)          # the next user of a cell after an instruction (the entry's per-cell index)
# the grant's updates (plan v2 4.3): per window insert the entry's word (1) and, for each of the instruction's cells, the
# cell's entry and the link from its previous user (2): 1 + 2k for k cells, at most 1 + 2C; 1 per workspace cell when a
# certificate is installed; 1 per reserved script entry; 1 per chain mark (an entry a certificate's walks read)

# The representation's cells and predecessors per window entry: the maxima over the nine programs' instructions and the
# scripts their runs lowered, at d = 13, seed 4 (.md section 9.2: C from Adder-32, P from QPE-20). An instruction
# beyond them (a splice can add predecessors) is fenced: a check that meets it stops with the safe answer, and it is
# counted (`beyond`).
C_REP, P_REP = 51, 37


def parse_bound(x):
    """--r8-event-budget / --r8-window: a positive integer, or "inf" (unbounded, the counters on)."""
    if x is None:
        return None
    if isinstance(x, str) and x.strip().lower() in ("inf", "unbounded"):
        return INF
    v = int(x)
    assert v >= 1, ("a bound is a positive integer or inf", x)
    return v


def bound_tag(x) -> str:
    return "inf" if x == INF else "%d" % int(x)


class CoverageError(AssertionError):
    """the coverage assertion. An ordinary placement onto a guarded cell that no live cover
    holds, by an instruction on a live certificate's chains, would need a `rel` event (`ScoutDeviceManager._r9_dispatch`):
    the run fails there, as a strict write queue fails at an overdraw (`wq.Overdraw`), counted `cov_violation`."""


class Stop(Exception):
    """An event's walk reached its budget ("budget"), the window's end ("window") or an instruction the representation
    cannot hold ("beyond")."""
    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


class Event:
    __slots__ = ("kind", "budget", "acc", "upd", "visited", "followed", "full", "vid", "gn", "gc", "rm", "mpl", "cut", "ins", "inst")

    def __init__(self, kind, budget, vid=None):
        self.kind, self.budget, self.vid = kind, budget, vid
        self.acc, self.upd = 0, 0
        self.ins, self.inst = 0, False     # (the window inserts in the update part; whether the event installed anything)
        self.visited, self.followed = set(), set()
        self.gn = []                # (r9-guard-close 3: `_r9_guard_new`'s covers for other frames, installed if the grant stands)
        self.gc = frozenset()       # (r9-guard-close 4: the cells the grant guards once it stands, the members its revision
        self.rm = frozenset()       #  drops from its own frame, and the members' placements it must cover)
        self.mpl = set()
        self.cut = False            # (r9-guard-close 5: a stop cut the coverage scan short, its member enumeration included:
                                    #  what it did not reach is unknown, so the event stands on nothing it found)
        self.full = True            # (the event's walks read every live frame's holdable work: its followed placements
                                    #  are covered; a revision's pin test reads only the new holds)


class R9:
    """One run's bounded check (`ScoutDeviceManager._r9`)."""

    bmax = INF                          # (a grant's update part, its batch of queued writes, stops past this)
    cov = False                         # (plan v2 1.4: cov=enforce, E1 at every grant; off: rule)
    wq_on = False                       # (the write queue is; records only here)

    def __init__(self, B, W, c0: int = 0, c1: int = 0, C: int = C_REP, P: int = P_REP):
        self.B = B if B is not None else INF
        self.W = W if W is not None else INF
        self.c0, self.c1 = int(c0), int(c1)
        self.C, self.P = int(C), int(P)                 # the representation's cells and predecessors per entry
        self.ev = None
        self.head, self.tail = 0, -1
        self.resv: dict = {}                            # vid -> reserved script entries (live frames only count)
        self.chain: dict = {}                           # vid -> instructions its certificates' walks read (its chains)
        self.cover: dict = {}                           # vid -> {(instruction, cell)}: its covered release placements
        self.cover_ver = 0
        self.vac: set = set()                           # {(instruction, cell)}: placements a script's DISCARD made
                                                        #  (r9-guard-close 2: release work until the instruction starts)
        self.vrun: set = set()                          # {cell}: cov=enforce (the re-check's HIGH): cells a script's
                                                        #  DISCARD freed, one mark each, whose run's placement is release
                                                        #  work until a start puts a patch there or discards it
        self.tickets: dict = {}                         # key -> (answer, the round its service ends, the request it answers)
        self.K = K_AGE
        self.agew: dict = {}                            # key -> [first refusal, last ask]: the requests the port refused
        self.aged: dict = {}                            # key -> the round it was aged, in that order: served first
        self._susp: dict = {}                           # key -> (its last round, age, aged round, place)
        self._ag_round = None
        self._kept: set = set()
        self.stats = collections.Counter()
        self.hist = {k: collections.Counter() for k in KINDS}       # total accesses (walk + updates) per event
        self.hist_walk = {k: collections.Counter() for k in KINDS}
        self.hist_upd = {k: collections.Counter() for k in KINDS}
        self.per_round = collections.Counter()          # events in a round -> rounds
        self._rnd, self._rnd_n = None, 0
        self.peak = dict(occupancy=0, span=0, reserved=0, round=None)
        self.port2_peak, self.port2_round = 0, None
        self._p2_rnd, self._p2_acc = None, 0
        self.max_service = dict(cycles=0, rounds=0, kind=None)
        self._hold_round, self._hold = None, set()
        self.size = None                                # instruction -> (its cells, its predecessors): set by the device
        self.beyond = set()                             # (the instructions met that the representation cannot hold)
        self.inserts = 0
        self.p0 = None                                  # (the program's predecessors per instruction as compiled: the first event's)
        self.waiting, self._qp = {}, None               # (records only: the requests the port has not served, r9-guard-close)
        self.qhist = collections.defaultdict(collections.Counter)
        self.qgone = collections.defaultdict(collections.Counter)
        self.svc_hist = collections.defaultdict(collections.Counter)

    # -- the bounds
    def b_rel(self) -> float:
        """B_rel: a `rel` event checks one instruction's guarded cells (at most C), each with a cell read, its next user,
        its release lookup and three walks (r6's pin, next-user test, rule (a)'s release), every walk confined to
        the window: at most W nodes, each read once with its predecessors and its cells' lookups."""
        if self.W == INF:
            return INF
        walk = self.W * (ACCESS["visit"] + ACCESS["edge"] * self.P + self.C * (ACCESS["cell"] + 2 * ACCESS["release"]))
        return self.C * (ACCESS["cell"] + ACCESS["next_user"] + 2 * ACCESS["release"] + 3 * walk)

    # -- the event's charges (inside an event only; outside, the walks keep caps and charge nothing)
    def upd(self, ev, n: int):
        """n more writes in the event's update part, its batch on the write queue: past the cap the event stops (the
        safe answer), as the walk budget stops it; the writes are not made."""
        if ev.upd + n > self.bmax:
            raise Stop("wq")
        ev.upd += n

    def charge(self, n: int):
        ev = self.ev
        if ev.acc + n > ev.budget:
            ev.acc = ev.budget                          # (the walk part stops at B: the access past it is not made)
            raise Stop("budget")
        ev.acc += n

    def node(self, x: int, n0: int):
        """A walk reads entry x: an instruction the representation cannot hold stops it; one past the window's tail is
        inserted with the entries before it, in program order (the update part: 1 + 2k each, k its cells; 1 + 2C when
        no size is known), if the window holds them."""
        ev = self.ev
        ev.visited.add(x)
        if self.size is not None:
            c, p = self.size(x)                         # (read at every visit: a splice can add predecessors later)
            if c > self.C or p > self.P:
                self.beyond.add(x)
                raise Stop("beyond")
        if x < n0 and x > self.tail:
            if (x - self.head + 1) + self.reserved() > self.W:
                raise Stop("window")
            lo = max(self.tail, self.head - 1)
            k = x - lo
            n = k + 2 * (sum(self.size(i)[0] for i in range(lo + 1, x + 1)) if self.size is not None else k * self.C)
            self.upd(ev, n)
            ev.ins += n
            self.inserts += k
            self.tail = x
        self.charge(ACCESS["visit"])

    def reserved(self) -> int:
        return sum(self.resv.values())

    def occupancy(self) -> int:
        return max(0, self.tail - self.head + 1) + self.reserved()

    def note_peak(self, now):
        occ = self.occupancy()
        if occ > self.peak["occupancy"]:
            self.peak = dict(occupancy=occ, span=max(0, self.tail - self.head + 1), reserved=self.reserved(), round=now)

    def advance_head(self, started, n0: int):
        h = self.head
        while h < n0 and h in started:
            h += 1
        self.head = h

    # -- reservations (an authorization's settlement scripts)
    def reserve(self, vid, n: int, now) -> bool:
        """Reserve n entries for frame vid (replacing its old reservation); False, and nothing changes, if they do not
        fit beside the span the checks have read and the other frames' reservations."""
        old = self.resv.get(vid, 0)
        if max(0, self.tail - self.head + 1) + self.reserved() - old + n > self.W:
            return False
        self.resv[vid] = n
        self.resv_max = max(getattr(self, "resv_max", 0), int(n))
        self.note_peak(now)
        return True

    _pruned = None

    def prune(self, now):
        """Drop the stored answers whose service ended more than two rounds ago with no requester asking again (a frame
        that decided otherwise, a value decoded meanwhile): the port's time was spent, the answer is discarded."""
        self._pruned = now
        for k in [k for k, t in self.tickets.items() if t[1] < now - 2]:
            del self.tickets[k]
            self.stats["tickets_dropped:" + k[0]] += 1

    # -- the port's order (r9-guard-close 3)
    @staticmethod
    def covers(old, new) -> bool:
        """Does the request a stored answer was computed for (`old`) cover the request made now (`new`)?  Each is (the
        parts that must be equal, the sets that may only shrink: guard cells, held work); None, a request not recorded."""
        return old is None or new is None or (old[0] == new[0] and all(n <= o for o, n in zip(old[1], new[1])))

    def unapplied(self, now, key=None) -> bool:
        """An answer whose service has ended and that its requester has not taken keeps the port: no other request is
        served until it is taken, or dropped two rounds on (`prune`), so a grant applies on the state its check saw. A
        the decision what-if's (a FORWARD not counted) installs and applies nothing, so it does not keep the port."""
        if self.tickets and self._pruned != now:
            self.prune(now)
        for k, t in self.tickets.items():
            if t[1] <= now and k != key and not (k[0] == "fwd" and len(k) > 4 and not k[4]):
                if (k, t[1]) not in self._kept:                    # (r9-guard-close 4, records: the answers that kept it)
                    self._kept.add((k, t[1]))
                    self.stats["kept_answers"] += 1
                return True
        return False

    # -- K-aging (r9-guard-close 4; answer 2)
    def age_round(self, now):
        """Once a round.  A request the port refused whose requester has not asked for two rounds has left (from its last
        ask + 3 on, as an untaken answer is dropped): its age goes, an aged one's turn too.  A request refused K or more
        rounds ago that is still pending is aged, after every request aged before it (in a round, oldest first refusal
        first): the aged requests are served in that order, before any fresh one."""
        if self._ag_round == now:
            return
        self._ag_round = now
        if not self.agew:
            return
        for k in [k for k, w in self.agew.items() if w[1] < now - 2]:
            del self.agew[k]
            self.stats["age_left"] += 1
            if self.aged.pop(k, None) is not None:
                self.stats["aged_left"] += 1
        for k, _w in sorted(((k, w) for k, w in self.agew.items() if now - w[0] >= self.K and k not in self.aged),
                            key=lambda kw: kw[1][0]):
            self.aged[k] = now
            self.stats["aged_promotions"] += 1

    def turn(self, key, now) -> bool:
        """May request `key` use the port now: no aged request is pending, or `key` is the oldest aged one (aged requests
        go before every fresh use of the port, `r9_take` included: a fresh request is not served at all meanwhile)."""
        self.age_round(now)
        return not self.aged or next(iter(self.aged)) == key

    def aged_head(self, key) -> bool:
        """Is `key` the oldest aged request (its service overrides the legacy aged-check reservation, `ck_resv`)?"""
        return bool(self.aged) and next(iter(self.aged)) == key

    def refused(self, key, now):
        """The port refused request `key` this round (a service owed, an untaken answer, an aged turn or no room): its age
        runs from its first refusal while its requester keeps asking (its identity: the requester's kind and frame, a
        counted FORWARD apart from a what-if, an instruction's guard check)."""
        w = self.agew.get(key)
        if w is None:
            self.agew[key] = [now, now]
        else:
            w[1] = now

    def served(self, key):
        if self.agew.pop(key, None) is not None and self.aged.pop(key, None) is not None:
            self.stats["aged_served"] += 1

    # -- (F1): an instruction's request, ("gc", i), is one request through its guard check, its placement's check and
    #    its start's admission to the write queue: a check served does not end it while the admission is still to come
    def suspend(self, key, now):
        """A check of `key` is served and its start's admission is still to come.  The port sees it served, as `served`
        (r9's records unchanged), and its age is kept aside: back if the write queue then refuses the admission
        (`resume`), this round, or a later one while the instruction's own check stays in service (`carry`); gone
        otherwise (an admission, or another hold ending the request, as in r9).  An age already aside stays as it is."""
        self._susp = {k: v for k, v in self._susp.items() if v[0] >= now}
        w = self.agew.get(key)
        if w is None:
            return
        self._susp[key] = (now, w, self.aged.get(key), list(self.aged).index(key) if key in self.aged else 0)
        self.served(key)

    def carry(self, key, now):
        """The instruction's own check is in service (a placement's walk over several rounds): the age set aside stays
        aside through the next round (each entry: the last round it may come back in)."""
        v = self._susp.get(key)
        if v is not None and v[0] >= now:
            self._susp[key] = (now + 1,) + v[1:]

    def alive(self, key, now) -> bool:
        """Has the request its age: running, or set aside through this round?"""
        return key in self.agew or self._susp.get(key, (-1,))[0] >= now

    def resume(self, key, now):
        """The write queue refuses the admission of `key`, whose check this round's `suspend` set aside: its age is back,
        its first refusal kept, and an aged request in its place among the aged (before any refusal updates it)."""
        v = self._susp.pop(key, None)
        if v is None or v[0] < now or key in self.agew:
            return False
        self.agew[key] = v[1]
        if v[2] is not None:
            self.stats["aged_served"] -= 1                                 # (not served after all: `served` counts it again)
            items = list(self.aged.items())
            items.insert(min(v[3], len(items)), (key, v[2]))
            self.aged.clear()
            self.aged.update(items)
        return True

    def hold_now(self, vid, now):
        """A registration's reservation, made before its frame is live, stays through the round it was made in."""
        if self._hold_round != now:
            self._hold_round, self._hold = now, set()
        self._hold.add(vid)

    _coll_round, _coll = None, frozenset()

    def collected(self, vid, now):
        """(a deliberate change): frame vid's stored registration answer was collected this round, and its
        frame is not live yet (`_defer_register` makes it live later in the round). Its reservation stays through the
        round, as a reservation made this round does (`hold_now`). `sweep` dropped it when another event was
        served in between, and the frame then lived without its reserved entries, so its settlement could meet a full
        window; counted as `resv_kept_collected` where would have dropped it."""
        self.hold_now(vid, now)
        if self._coll_round != now:
            self._coll_round, self._coll = now, set()
        self._coll.add(vid)

    def sweep(self, live, now):
        """Drop the reservation of a frame that is not live, has no event in service and was not reserved this round (a
        registration that did not go on)."""
        keep = {k[1] for k in self.tickets if k[0] in ("reg", "rev")}
        if self._coll_round == now and self._coll:                  # (records: the reservations sweep dropped here)
            self.stats["resv_kept_collected"] += sum(1 for v in self._coll if v in self.resv and v not in live)
            self._coll = set()
        if self._hold_round == now:
            keep |= self._hold
        out = [v for v in self.resv if v not in live and v not in keep]
        for vid in out:
            del self.resv[vid]
        return out                                      # (the frames whose registration did not go)

    def frame_end(self, vid, live=(), started=()) -> int:
        """A frame's end: its reservation, chains and covers go.  r9-guard-close: a covered placement that has not
        started and that another live frame's chains hold stays covered for those frames (unless a live frame covers it
        already), so it stays a placement permission of later certificates (`perms`) and its release still waits on
        nothing held: a cover lasts while any live frame needs it.  Returns the covers kept (one mark each)."""
        self.resv.pop(vid, None)
        self.chain.pop(vid, None)
        c = self.cover.pop(vid, None)
        kept = 0
        if c:
            self.cover_ver += 1
            others = [y for y in live if y != vid]
            for xq in c:
                if xq[0] in started or any(xq in self.cover.get(y, ()) for y in others):
                    continue
                for y in others:
                    ch = self.chain.get(y)
                    if ch and xq[0] in ch:
                        self.cover.setdefault(y, set()).add(xq)
                        kept += 1
        if kept:
            self.stats["cover_kept"] += kept
        return kept

    # -- release work
    def covered(self, idx, cell, live) -> bool:
        if (idx, cell) in self.vac:
            return True
        for vid in live:
            c = self.cover.get(vid)
            if c and (idx, cell) in c:
                return True
        return False

    def on_chain(self, idx, live) -> bool:
        for vid in live:
            c = self.chain.get(vid)
            if c and idx in c:
                return True
        return False

    def install(self, vid, visited, followed_covered):
        """A certificate's (or an admitted placement's) chains: the instructions its walks read, and the placements on
        them it covers.  Counted as the event's update part: one access per entry marked."""
        if visited:
            self.chain.setdefault(vid, set()).update(visited)
        if followed_covered:
            c = self.cover.setdefault(vid, set())
            n = len(c)
            c.update(followed_covered)
            if len(c) != n:
                self.cover_ver += 1
            if self.wq_on:                              # (records: the queue's cover representation needs each cover
                ch = self.chain.get(vid, ())            #  on the chain of the frame that holds it)
                self.stats["wq_cover_off_chain"] += sum(1 for (x, _q) in followed_covered if x not in ch)

    def perms(self, live, started) -> dict:
        """The covered release placements as placement permissions, {instruction: (None, -1, {})}: no member exception
        (an ordinary instruction belongs to no frame's steps), as an ended frame's members are counted."""
        out = {}
        for vid in live:
            for (x, _q) in self.cover.get(vid, ()):
                if x not in started:
                    out[x] = (None, -1, {})
        if self.vac:                                    # (r9-guard-close 2: a script-made placement, until it starts)
            self.vac = {xq for xq in self.vac if xq[0] not in started}
            for (x, _q) in self.vac:
                out[x] = (None, -1, {})
        return out

    # -- records
    def record(self, ev: Event, now):
        tot = ev.acc + ev.upd
        self.hist[ev.kind][tot] += 1
        self.hist_walk[ev.kind][ev.acc] += 1
        self.hist_upd[ev.kind][ev.upd] += 1
        if self._rnd != now:
            if self._rnd is not None:
                self.per_round[self._rnd_n] += 1
            self._rnd, self._rnd_n = now, 0
        self._rnd_n += 1

    def port2(self, now, n: int):
        """The tables' second port (the changes that are not grants): its accesses this round, the peak kept."""
        if self._p2_rnd != now:
            self._p2_rnd, self._p2_acc = now, 0
        self._p2_acc += n
        if self._p2_acc > self.port2_peak:
            self.port2_peak, self.port2_round = self._p2_acc, now

    # -- queued waiting at the port (records only, r9-guard-close): a request the port did not serve in a round waits;
    #    the rounds from its first refusal to its service, by kind ("gc_rel": a covered release placement's guard check,
    #    "gc": another guard check; the event kinds), and those whose requester stopped asking (two rounds away)
    def qwait(self, kind, key, now):
        self._qprune(now)
        w = self.waiting.get(key)
        if w is None:
            self.waiting[key] = [kind() if callable(kind) else kind, now, now]
        else:
            w[2] = now

    def qserve(self, key, now):
        self._qprune(now)
        w = self.waiting.pop(key, None)
        if w is not None:
            self.qhist[w[0]][now - w[1]] += 1

    def _qprune(self, now):
        if self._qp == now:
            return
        self._qp = now
        for k in [k for k, w in self.waiting.items() if w[2] < now - 2]:
            w = self.waiting.pop(k)
            self.qgone[w[0]][w[2] - w[1] + 1] += 1

    def service(self, kind, cycles: int, rounds: int):
        self.svc_hist[kind][int(rounds)] += 1
        if cycles > self.max_service["cycles"]:
            self.max_service = dict(cycles=int(cycles), rounds=int(rounds), kind=kind)

    @staticmethod
    def _pct(h: collections.Counter, q: float):
        n = sum(h.values())
        if not n:
            return None
        k, acc = math.ceil(q * n), 0
        for v in sorted(h):
            acc += h[v]
            if acc >= k:
                return v
        return max(h)

    def summary(self, round_cycles: int = 286) -> dict:
        if self._rnd is not None and self._rnd_n:
            pr = collections.Counter(self.per_round)
            pr[self._rnd_n] += 1
        else:
            pr = self.per_round
        grants = collections.Counter()
        for k in ("reg", "fwd", "rev", "plc"):
            grants.update(self.hist[k])
        return dict(B=bound_tag(self.B), W=bound_tag(self.W), c0=self.c0, c1=self.c1, C=self.C, P=self.P,
                    B_rel=(None if self.b_rel() == INF else int(self.b_rel())),
                    events={k: sum(self.hist[k].values()) for k in KINDS},
                    acc_hist={k: {str(a): n for a, n in sorted(self.hist[k].items())} for k in KINDS if self.hist[k]},
                    acc_walk_hist={k: {str(a): n for a, n in sorted(self.hist_walk[k].items())} for k in KINDS if self.hist_walk[k]},
                    acc_upd_hist={k: {str(a): n for a, n in sorted(self.hist_upd[k].items())} for k in KINDS if self.hist_upd[k]},
                    grant_acc_max=(max(grants) if grants else 0), grant_acc_p9999=self._pct(grants, 0.9999),
                    grant_walk_max=max([max(self.hist_walk[k]) for k in ("reg", "fwd", "rev", "plc") if self.hist_walk[k]] or [0]),
                    events_per_round={str(a): n for a, n in sorted(pr.items())},
                    window_peak=dict(self.peak), window_head=self.head, window_tail=self.tail,
                    resv_max=int(getattr(self, "resv_max", 0)), inserts=self.inserts, beyond=len(self.beyond), P0=self.p0,
                    port2_peak=self.port2_peak, port2_peak_round=self.port2_round, port2_room=round_cycles,
                    max_service=dict(self.max_service),
                    queue_wait_hist={k: {str(a): n for a, n in sorted(h.items())} for k, h in sorted(self.qhist.items())},
                    queue_left_hist={k: {str(a): n for a, n in sorted(h.items())} for k, h in sorted(self.qgone.items())},
                    queue_waiting_at_end=len(self.waiting),
                    K=self.K, aged_at_end=len(self.aged), age_pending_at_end=len(self.agew),
                    service_rounds_hist={k: {str(a): n for a, n in sorted(h.items())} for k, h in sorted(self.svc_hist.items())},
                    stats=dict(sorted(self.stats.items())))
