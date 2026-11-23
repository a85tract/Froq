"""golden_engine (plan Sec. 9): a cycle-level model of the control plane of
se_engine_top -- ingress queues, the tick sequencer, the entry table's three
phases, the job queue / directory / line metadata, and the builder's timing
(the datapath itself is golden.compose + golden.solve). It replays a workload
of timed messages and emits the egress messages with the tick in which the
RTL's egress FIFO head shows them. G3 compares this with the RTL tick by tick.

Timing constants mirror the RTL (se_entry_table / se_jobq_dir / se_builder):
  strobe at cycle s -> entry table A_NEXT at s+2; a message costs L, X, NEXT
  (3 cycles); each worklist op 2 + WORK_LAT cycles plus one closing WORK_L; Phase B
  starts 3 cycles after a_done, one entry per cycle, N+3 cycles of builder
  pause; a job enqueued at cycle X is selectable from X+2; take = KEY, LOOKUP,
  ALLOC; the builder is busy from start+5 for `cycles` enabled cycles
  (cycles = 24 + 2E direct, 24 + E + chunks*(203 + E) otherwise -- the G5 pipelines: the solver's stage B split and
  its stage B3 (products registered raw, then summed),
  the composer's lump term over three setup cycles, the PRELOAD endpoint register, the window sample register, OMEGA read and stream stage,
  and the three-stage lane reduction).

Threshold build (RTL se_thr_top): Engine(cfg, P, threshold=True). No table store, builder, job queue or
pool: the entry pass installs every LIVE entry with a one-column line valid for this pass, whose bit is
omc <= cfg.theta[site_class] (c >= theta, both held as 1 - x in Q0.20; an unwritten class has theta 1, i.e. 0), and
the entry FORWARDs at its boundary k iff that bit is set and k < F. Everything else -- queues, sequencer, Phase A,
ACCEPT, RESOLVE / RECOVER -- is the full engine's, except that REGISTER checks no latency table."""
from __future__ import annotations
from dataclasses import dataclass, field
from .fx import S_ONE
from .tables import Table
from .compose import compose_direct, compose_leaf, compose_entry, Line
from .solve import SiteClass, solve as qsolve, F_MAX

N = 64; L = 256; SETS = L // 4; E_MAX = 256; P_C = 64; T_LEN = 1024; D_MAX_CFG = 7
STAMP_MASK = (1 << 64) - 1   # 64-bit FIFO stamps (RTL STAMP_W; round 17 #1)
import os as _os
import copy as _copy
import bisect
SEL_LAT = int(_os.environ.get("SE_SEL_LAT", "7"))   # job-selection pipeline latency (RTL se_jobq_dir SEL_LAT = 4; env for RTL experiments)
LOOKUP_LAT = int(_os.environ.get("SE_LOOKUP_LAT", "2"))   # 1: the take's LOOKUP registers the compare and acts one cycle later (RTL T_LOOKUP2);
    # 2: a LOOKUP0 cycle registers the set index and the directory row before the compare (G5 batch 29)
JQ_LAT = int(_os.environ.get("SE_JQ_LAT", "1"))         # 1: the job queue registers the entry table's events once (G5)
ZERO_LAT = int(_os.environ.get("SE_ZERO_LAT", "1"))     # 1: a line that lost its last reference is listed one cycle later (RTL two-stage detection, G5)
PEND_BYPASS = int(_os.environ.get("SE_PEND_BYPASS", "0"))   # 1: ALLOC may take a line the cycle it is appended; 0: only once it is in a list (G5)
ALLOC_LAT = ZERO_LAT + (0 if PEND_BYPASS else 1)       # cycles after the last release before ALLOC can take the line
LIST_ENGINE = int(_os.environ.get("SE_LIST_ENGINE", "1"))   # 1: the RTL list engine -- APPENDs queued two cycles after the
    # zero (the RTL's detection stage and pending registers), one list operation executed per cycle (a splice or an
    # eviction POP takes the cycle), a line allocatable / evictable the cycle after its APPEND executed (G5 batch 19)
LQ_DEPTH = 32
KEY_LAT = int(_os.environ.get("SE_KEY_LAT", "1"))
WORK_LAT = int(_os.environ.get("SE_WORK_LAT", "1"))
RES_LAT = int(_os.environ.get("SE_RES_LAT", "1"))         # 1: a matched RESOLVE is three cycles (RES_L, RES_M registers the entry, RES_X acts); unmatched stay two -- G5 batch 30       # 1: a worklist op is three cycles (WORK_L, WORK_M registers the target entry, WORK_X acts) -- G5 batch 26         # 1: the take's KEY step is two cycles -- the entry, its upstream and the slot are
    # registered in the first (with the upstream index captured at selection), the key is formed from those copies in the second (G5)
# builder cycle model constants (direct base, setup, per-chunk); env for RTL experiments.  The solver's SETUP takes F_MAX + 2
# cycles, so both bases grow with F_MAX: 24 at F_MAX = 4, 28 at 8 (tests.calibrate_builder measures the RTL against this)
_cyc = _os.environ.get("SE_CYC", "%d,%d,203" % (24 + F_MAX - 4, 24 + F_MAX - 4)).split(",")
CYC_DIRECT, CYC_SETUP, CYC_CHUNK = int(_cyc[0]), int(_cyc[1]), int(_cyc[2])
SNAPQ = 16               # snapshot queue depth: backlog capacity SNAPQ - 1 rounds (RTL se_engine_top)
KEY_W = 125; SET_W = 6
LC_FREE, LC_LIVE, LC_RECOVERING = 0, 1, 2
UP_NONE, UP_LEAF, UP_ENTRY = 0, 1, 2
JM_REG, JM_REBUILD = 0, 1
JS_QUEUED, JS_WAIT_UPSTREAM, JS_WAIT_LINE, JS_BUILDING = 0, 1, 2, 3
LS_FREE, LS_BUILDING, LS_READY = 0, 1, 2
RJ_FULL, RJ_UNKNOWN, RJ_UP_NOT_LIVE, RJ_TOO_DEEP, RJ_IN_USE, RJ_EPOCH = 0, 1, 2, 3, 4, 5
TICK_MASK = (1 << 32) - 1


def age_sat(now, origin):
    a = (now - origin) & TICK_MASK
    return 2047 if a > 2047 else a


def e_job_abs(now, e_r, E=E_MAX):
    off = (now - e_r) & TICK_MASK
    return 0 if off >= E else E - off


@dataclass
class SiteInst:
    site_class: int; self_table: int; up_kind: int; up_table: int; omc_link: int
    valid: bool = True


@dataclass
class Config:
    classes: dict = field(default_factory=dict)     # id -> SiteClass
    insts: dict = field(default_factory=dict)       # id -> SiteInst
    tables: dict = field(default_factory=dict)      # id -> Table
    scripts: dict = field(default_factory=dict)     # (class, pos) -> id
    d_max: int = 4
    E: int = E_MAX                                  # horizon of a registration job (CONFIG, Sec. 3.6)
    epoch: int = 0
    invalid_tables: set = field(default_factory=set)   # tables the store refused (malformed CONFIG)
    invalid_classes: set = field(default_factory=set)
    malformed: list = field(default_factory=list)      # (TB command, index) pairs emitted before COMMIT
    theta: dict = field(default_factory=dict)          # threshold build: site class -> 1 - theta in Q0.20 (CONFIG sel 5)

QUEUE_DEPTH = 32


class Entry:
    __slots__ = ("lc", "inflight", "completion_tick", "k", "ready", "line", "e_b", "e_job", "value_id",
                 "site_class", "omc", "gen", "depth", "e_r", "up_kind", "leaf_armed", "up_table", "up_age_origin",
                 "up_handle", "up_entry", "up_gen", "up_valid", "omc_link", "self_table", "self_age_origin", "fwd_issued")

    def __init__(self):
        for f in self.__slots__:
            setattr(self, f, 0)


class Slot:
    __slots__ = ("valid", "state", "gen", "mode", "e_b", "e_job", "enq_tick", "pinned", "line", "ready_tick", "visible_cycle", "pinned_at")

    def __init__(self):
        self.visible_cycle = 0; self.valid = False; self.state = 0; self.gen = 0; self.mode = 0; self.e_b = 0; self.e_job = 0
        self.enq_tick = 0; self.pinned = False; self.line = 0; self.ready_tick = None
        self.pinned_at = -9


class LineMeta:
    __slots__ = ("state", "rc", "priv", "ver", "hasway", "set", "way", "stamp", "S", "Om", "E", "acts", "completing", "zero_at", "listed_at")

    def __init__(self, idx):
        self.state = LS_FREE; self.rc = 0; self.priv = False; self.ver = 0; self.hasway = False; self.completing = False
        self.zero_at = -9   # cycle the line last lost its final reference (test tuning: the RTL lists it one cycle later than the golden)
        self.listed_at = -9 # LIST_ENGINE: cycle its APPEND executed (None: not in a list); every line starts free and listed
        self.set = 0; self.way = 0; self.stamp = idx; self.S = None; self.Om = 0; self.E = 0; self.acts = None


def pack_key(k):
    """key_t packing (MSB first) -> int, then the XOR fold into SET_W bits."""
    fields = [(k["epoch"], 8), (k["mode"], 1), (k["site_class"], 6), (k["up_kind"], 2), (k["up_table"], 6),
              (k["up_age_q"], 11), (k["up_line"], 8), (k["delta"], 9), (k["up_version"], 8), (k["omc_link"], 20),
              (k["self_table"], 6), (k["self_age_q"], 11), (k["omc"], 20), (k["e_job"], 9)]
    v = 0
    for val, w in fields:
        v = (v << w) | (val & ((1 << w) - 1))
    return v


def fold(v):
    s = 0
    for b in range(0, KEY_W, SET_W):
        s ^= (v >> b) & ((1 << SET_W) - 1)
    return s


class Engine:
    def __init__(self, cfg: Config, P: int, threshold: bool = False):
        self.cfg = cfg
        self.P = P
        self.thr = threshold     # the threshold build (se_thr_top): no builder, job queue, pool or tables
        self.diag = None         # a list that receives ("IN", cycle, type, msg) and ("PASS", cycle, round, counts)
        self.u_tap = None        # fn(pass, kind, *args) -- se_entry_table's taps for U (golden.u_unit)
        self.ent = [Entry() for _ in range(N)]
        self.slots = [Slot() for _ in range(N)]
        self.lines = [LineMeta(i) for i in range(L)]
        self.dir = [[None] * 4 for _ in range(SETS)]        # (key_int, line) or None
        self.gstamp = L
        self.tick = 0           # free-running strobe count (ingress stamps, egress log)
        self.proc = 0           # the round being processed (latched per pass)
        self.snapq = []         # snapshots awaiting a pass: one per strobe (RTL depth SNAPQ, overflow -> FATAL)
        self.proc_at = {}                                     # not used
        # queues: list of (visible_cycle, msg); FIFO order by push
        self.q = {t: [] for t in ("REG", "RES", "LV", "RC", "RD", "ACC")}
        self.src = {t: [] for t in ("REG", "RES", "LV", "RC", "RD", "ACC")}   # producer-side queues
        self.written = {t: 0 for t in ("REG", "RES", "LV", "RC", "RD")}         # cumulative writes (visible)
        self.popped = {t: 0 for t in ("REG", "RES", "LV", "RC", "RD")}
        # (stress): the Phase-A queues pop on the entry table's registered pulse, the cycle after the message's
        # X cycle, and the producer sees the slot through the FIFO's registered count one cycle later still: a slot
        # freed in X cycle c takes a waiting message in cycle c + 2 (the ACCEPT queue pops combinationally: c + 1)
        self.last_pop = {t: -9 for t in ("REG", "RES", "LV", "RC", "RD")}
        self.out = []                                         # (tick, type, tuple)
        self.counters = dict(registrations=0, rejects=0, unmatched=0, suppressed=0, rebuilds=0, cascades=0,
                             hits=0, builds=0, evicts=0, privates=0, lv_accepted=0, overruns=0, stale_accepts=0)
        # sequencer
        self.seq = "C"; self.strobe_cycles = set()
        self.a = None            # Phase A FSM state
        self.a_start = -1
        self.strobe_dbg = []
        self.acc_stale_n = 0     # ACCEPTs queued at the last strobe, still to be discarded
        self.dbg_backlog_hits = 0
        self.dbg_alloc_at = []           # (cycle, line, source, the line's listed_at) of every allocation
        self.dbg_alloc_pending_at = []   # cycles of the `alloc_pending` hits (scenario pre-passes qualify the intended one)
        self.dbg_cfg_take_st = []        # the take FSM's state at every accepted CONFIG commit (`config_paused_take`)
        self.dbg_unref_completions = 0
        self.elig_hist = []       # eligible() per cycle (the RTL's selection pipeline, SEL_LAT)
        self.done_pending = []    # (cycle, line): completions whose zero-reference stamp follows that cycle's releases
        self.lq_pend = []         # LIST_ENGINE: (cycle, line, priv) APPENDs to enqueue at that cycle (if the line is still unreferenced)
        self.lq = []              # LIST_ENGINE: the operation queue (line, priv), one executed per cycle
        self.lq_hwm = 0; self.lq_busy_at = -1   # occupancy high-water mark; the cycle a splice occupies the engine
        self.race = {}           # golden-only counters of the rare take/complete races (for test tuning)
        self.b = None            # Phase B state
        self.pause_until = -1    # builder paused for cycles < pause_until (Phase B)
        # builder / take
        self.take = {"st": "IDLE"}
        self.bld = None          # dict when busy: start_vis, cycles, progress, line, job
        self.bld_done_cycle = None   # cycle at which bld_done is visible (first enabled cycle >= META+1)
        self.pending_done = None     # META cycle reached; waiting for an enabled cycle to publish done
        self.rc_zero_events = []
        self.deferred = {}
        self.jq_pending = []
        self.emit_next = []
        self.emit_at = []
        self.trace = []
        self.last_bld_en = True

    # ------------------------------------------------------------ helpers
    def rec_tick(self):
        """Strobe count a record made in this cycle belongs to (a strobe in this
        very cycle counts: the record is 'after' it)."""
        return self.tick + (1 if self.cyc in self.strobe_cycles else 0)

    def push(self, cycle, typ, msg):
        """A message written into the ingress FIFO at `cycle` (visible from cycle+1)."""
        self.q[typ].append((cycle + 1, dict(msg, ing_tick=self.tick_at_cycle(cycle))))

    def tick_at_cycle(self, c):
        # the tick register increments at the strobe cycle (visible from s+1)
        return self.tick if c not in self.strobe_cycles else self.tick   # caller pushes after the strobe

    def emit(self, cycle, typ, data):
        """Egress messages reach the testbench's log two cycles after the entry table
        raises them (registered pulse, FIFO write, head visible); log them with the tick
        of that cycle so an overrun straddling a strobe attributes them as the RTL does."""
        if typ in ("REGD", "REL", "REC", "ACT"):
            self.emit_at.append((cycle + 2, typ, data))
        else:
            self.out.append((self.tick, typ, data))
            self.trace.append((cycle, self.tick, typ, data))

    # ------------------------------------------------------------ lookups
    def find_vid(self, vid):
        for i, e in enumerate(self.ent):
            if e.lc != LC_FREE and e.value_id == vid:
                return i
        return None

    def children_of(self, idx, gen):
        return [i for i, e in enumerate(self.ent)
                if e.lc == LC_LIVE and e.up_kind == UP_ENTRY and e.up_valid and e.up_entry == idx and e.up_gen == gen]

    def handle_matches(self, h):
        return [i for i, e in enumerate(self.ent) if e.lc == LC_LIVE and e.leaf_armed and e.up_handle == h]

    # ------------------------------------------------------------ reference counts
    def list_engine_step(self, c):
        """LIST_ENGINE: one list operation per cycle.  A splice (a hit on a listed line last cycle) or this cycle's
        eviction POP takes the cycle; otherwise the queue's head APPEND executes -- a cancelled entry costs its cycle and
        does nothing -- and the line is in its list (stamped, allocatable / evictable) from the next cycle."""
        if not self.lq:
            return
        if self.lq_busy_at == c or self.lq_evicted:               # the splice / the eviction POP takes the cycle
            k = "lq_stall_splice" if self.lq_busy_at == c else "lq_stall_pop"
            self.race.setdefault(k, 0); self.race[k] += 1
            return
        l, pv, valid, at = self.lq.pop(0)
        if at < c:                                                # waited behind another operation (a backlog)
            self.race.setdefault("lq_backlog", 0); self.race["lq_backlog"] += 1
        if valid:
            m = self.lines[l]
            assert m.rc == 0 and m.listed_at is None and m.priv == pv, ("list engine: stale APPEND", c, l)
            m.stamp = self.gstamp; self.gstamp = (self.gstamp + 1) & STAMP_MASK; m.listed_at = c

    def rc_defer(self, deltas):
        """Entry-table events (rel_valid, jq_enq/cancel, inst_take) are registered:
        the job queue applies their reference-count changes one cycle later, and
        the counts are visible the cycle after that."""
        if deltas and not self.thr:
            self.jq_pending.append((self.cyc + 2 + JQ_LAT, None, lambda d=dict(deltas): d))

    def jq_busy_entries(self, apply_cycle):
        """Entries whose slot the entry table writes at `apply_cycle` (the RTL's jq_enq /
        jq_cancel / inst_take is asserted in the cycle before the slot registers change)."""
        return {e for (cc, e, f) in self.jq_pending if cc == apply_cycle and e is not None}

    def rc_delta(self, deltas):
        """Apply a dict line -> delta in one cycle with the RTL's zero-transition rule."""
        nst = 0                                    # stamps handed out: ascending line order, as the RTL's loop
        for l in sorted(deltas):
            d = deltas[l]
            if d == 0:
                continue
            m = self.lines[l]
            n = m.rc + d
            assert n >= 0, ("refcount underflow", l)
            m.rc = n
            if n == 0 and m.state == LS_READY and not m.completing:
                m.zero_at = self.cyc
                if LIST_ENGINE:
                    m.listed_at = None; self.lq_pend.append([self.cyc + 2, l, m.priv, False])
                else:
                    m.stamp = (self.gstamp + nst) & STAMP_MASK; nst += 1
                if m.priv:
                    m.state = LS_FREE
        self.gstamp = (self.gstamp + nst) & STAMP_MASK   # a same-cycle completion then takes the next stamp

    # ------------------------------------------------------------ job queue events
    def jq_enq(self, e, gen, mode, e_job, wait_up, deltas):
        if self.thr:
            return                                           # no job queue: the threshold policy decides in the entry pass
        tick = self.proc
        def apply():
            s = self.slots[e]
            d = {}
            if s.valid and s.pinned:
                if s.pinned_at == self.cyc - 1 - JQ_LAT:      # the pin landed on the edge that registered this event
                    self.race.setdefault("pin_event_collision", 0); self.race["pin_event_collision"] += 1
                d[s.line] = -1
            s.valid = True; s.state = JS_WAIT_UPSTREAM if wait_up else JS_QUEUED; s.gen = gen; s.mode = mode
            s.e_b = tick; s.e_job = e_job; s.enq_tick = tick; s.pinned = False; s.ready_tick = None
            s.visible_cycle = 0
            return d
        self.jq_pending.append((self.cyc + 2 + JQ_LAT, e, apply))

    def jq_cancel(self, e, deltas):
        if self.thr:
            return
        def apply():
            s = self.slots[e]
            d = {}
            if s.valid and s.pinned:
                if s.pinned_at == self.cyc - 1 - JQ_LAT:      # the pin landed on the edge that registered this event
                    self.race.setdefault("pin_event_collision", 0); self.race["pin_event_collision"] += 1
                d[s.line] = -1
            s.valid = False; s.pinned = False
            return d
        self.jq_pending.append((self.cyc + 2 + JQ_LAT, e, apply))

    def jq_inst_take(self, e, installed):
        if self.thr:
            return
        def apply():
            s = self.slots[e]
            d = {}
            if not installed and s.valid and s.pinned:
                d[s.line] = -1
            s.valid = False; s.pinned = False
            return d
        self.jq_pending.append((self.cyc + 2 + JQ_LAT, e, apply))

    # ------------------------------------------------------------ Phase A ops
    def op_apply(self, w, is_rej, op_tick, deltas):
        """A_WORK_X on entry w.  Returns the children to cascade."""
        e = self.ent[w]
        kids = self.children_of(w, e.gen) if e.lc == LC_LIVE else []
        self.emit(self.cyc, "OP", (w, int(is_rej), e.lc, e.up_kind, e.up_entry, e.up_gen, len(kids), e.gen))
        if e.lc != LC_LIVE:
            return []
        old_gen = e.gen
        e.gen = (e.gen + 1) & 0xFFFF
        if e.ready:
            deltas[e.line] = deltas.get(e.line, 0) - 1
        e.ready = False; e.e_b = self.proc
        if is_rej:
            e.up_kind = UP_NONE; e.depth = 1                 # the chain is this value alone now
            e.self_age_origin = self.proc if ((self.proc - op_tick) & TICK_MASK) >> 31 else op_tick
            self.counters["rebuilds"] += 1
            ej = e_job_abs(self.proc, e.e_r, self.cfg.E)
            if ej == 0:
                self.jq_cancel(w, deltas); self.counters["suppressed"] += 1
            else:
                self.jq_enq(w, e.gen, JM_REBUILD, ej, False, deltas)
        else:
            self.counters["cascades"] += 1
            # follows the parent -- never through a stale pointer to a recycled slot
            e.depth = self.ent[e.up_entry].depth + 1 if (e.up_kind == UP_ENTRY and e.up_valid) else 1
            if e.up_kind == UP_ENTRY:
                if e.up_valid:
                    e.up_gen = self.ent[e.up_entry].gen
                self.jq_enq(w, e.gen, JM_REG, 0, e.up_valid and not self.ent[e.up_entry].ready, deltas)
            else:
                ej = e_job_abs(self.proc, e.e_r, self.cfg.E)
                if ej == 0:
                    self.jq_cancel(w, deltas); self.counters["suppressed"] += 1
                else:
                    self.jq_enq(w, e.gen, JM_REG, ej, False, deltas)
        return kids

    # ------------------------------------------------------------ Phase A FSM (one call per cycle)
    def phase_a_step(self):
        a = self.a
        c = self.cyc
        deltas = {}
        st = a["st"]
        if st == "NEXT":
            for typ in ("REG", "RES", "LV", "RC", "RD"):
                if a["n"][typ] > 0:
                    a["st"] = typ + "_L"; a["cur"] = typ
                    return
            a["st"] = "DONE"; a["done_cycle"] = c        # a_done pulses this cycle
            if self.u_tap is not None:
                self.u_tap(self.proc, "ADONE")
            return
        if st.endswith("_L") and st != "WORK_L":
            if st == "RES_L" and RES_LAT:                        # the RTL registers a matched RESOLVE's entry in A_RES_M
                i = self.find_vid(self.q["RES"][0][1]["vid"])
                if i is not None and self.ent[i].lc == LC_LIVE:
                    a["st"] = "RES_M"; return
            a["st"] = st[:-2] + "_X"
            return
        if st == "RES_M":
            a["st"] = "RES_X"; return
        if st == "WORK_L":
            todo = a["todo_rej"] | a["todo_casc"] | a["todo_depth"]
            if not todo:
                a["st"] = "NEXT"; return
            w = min(todo)
            a["w"] = w; a["w_rej"] = w in a["todo_rej"]
            a["w_depth"] = w not in a["todo_rej"] and w not in a["todo_casc"]; a["st"] = "WORK_M" if WORK_LAT else "WORK_X"
            return
        if st == "WORK_M":                                        # the RTL registers the target entry; nothing else happens
            a["st"] = "WORK_X"
            return
        if st == "WORK_X":
            w = a["w"]
            a["todo_rej"].discard(w); a["todo_casc"].discard(w); a["todo_depth"].discard(w)
            if a["w_depth"]:
                # depth-only fix-up after an agreed parent retired: no re-solve, no gen, no job
                e = self.ent[w]
                kids = self.children_of(w, e.gen) if e.lc == LC_LIVE else []
                self.emit(c, "OP", (w, 0, e.lc, e.up_kind, e.up_entry, e.up_gen, len(kids), e.gen))   # the testbench logs every worklist op
                if e.lc == LC_LIVE:
                    e.depth = self.ent[e.up_entry].depth + 1 if (e.up_kind == UP_ENTRY and e.up_valid) else 1
                    for k in kids:
                        if k != w:
                            a["todo_depth"].add(k)
                a["st"] = "WORK_L"
                return
            kids = self.op_apply(w, a["w_rej"], a["op_tick"], deltas)
            for k in kids:
                if k != w:
                    a["todo_casc"].add(k)
            self.rc_defer(deltas)
            a["st"] = "WORK_L"
            return
        # ---- X cycles
        typ = st[:-2]
        vis, msg = self.q[typ].pop(0)
        self.popped[typ] += 1
        self.last_pop[typ] = c
        assert vis <= c, ("message not yet visible", typ, vis, c)
        a["n"][typ] -= 1
        a["st"] = "NEXT"
        if typ == "REG":
            self.do_register(msg, deltas)
        elif typ == "RES":
            idx = self.find_vid(msg["vid"])
            if idx is None or self.ent[idx].lc != LC_LIVE:
                self.counters["unmatched"] += 1
            else:
                e = self.ent[idx]
                if self.u_tap is not None:
                    self.u_tap(self.proc, "END", idx)
                if e.ready:
                    deltas[e.line] = deltas.get(e.line, 0) - 1
                self.jq_cancel(idx, deltas)
                kids_all = self.children_of(idx, e.gen)
                for kk in kids_all:
                    self.ent[kk].up_valid = False
                if msg["verdict"]:
                    e.lc = LC_FREE; e.ready = False
                    self.emit(c, "REL", (e.value_id,))    # the children keep their lines and their (composition) depth
                else:
                    sc = self.cfg.classes[e.site_class]
                    pos = e.k + 1 if (e.inflight and sc.settle_dst[e.k]) else e.k
                    e.lc = LC_RECOVERING; e.ready = False
                    self.emit(c, "REC", (e.value_id, e.site_class, pos, self.cfg.scripts.get((e.site_class, pos), 0)))
                    kids = kids_all
                    if kids:
                        a["todo_rej"] = set(kids); a["todo_casc"] = set(); a["op_tick"] = msg["ing_tick"]
                        a["st"] = "WORK_L"
        elif typ == "LV":
            m = self.handle_matches(msg["handle"])
            if not m:
                self.counters["unmatched"] += 1
            elif msg["verdict"]:
                self.counters["lv_accepted"] += 1
            else:
                a["todo_rej"] = set(m); a["todo_casc"] = set(); a["op_tick"] = msg["ing_tick"]; a["st"] = "WORK_L"
        elif typ == "RC":
            idx = self.find_vid(msg["vid"])
            if idx is None or self.ent[idx].lc != LC_LIVE:
                self.counters["unmatched"] += 1
            else:
                self.ent[idx].omc = msg["omc"]
                if self.u_tap is not None:
                    self.u_tap(self.proc, "RC", idx, msg["omc"])
                a["todo_rej"] = set(); a["todo_casc"] = {idx}; a["op_tick"] = msg["ing_tick"]; a["st"] = "WORK_L"
        elif typ == "RD":
            idx = self.find_vid(msg["vid"])
            if idx is None or self.ent[idx].lc != LC_RECOVERING:
                self.counters["unmatched"] += 1
            else:
                self.ent[idx].lc = LC_FREE
        self.rc_defer(deltas)

    def do_register(self, msg, deltas):
        c = self.cyc
        vid = msg["vid"]
        si = self.cfg.insts.get(msg["inst"])
        in_use = self.find_vid(vid) is not None
        up_idx = self.find_vid(msg["upvid"])
        up_live = up_idx is not None and self.ent[up_idx].lc == LC_LIVE
        free = [i for i, e in enumerate(self.ent) if e.lc == LC_FREE]
        if in_use:
            cause = RJ_IN_USE
        elif (si is None or not si.valid or si.site_class in self.cfg.invalid_classes
              or si.site_class not in self.cfg.classes
              or (not self.thr and (si.self_table in self.cfg.invalid_tables
                                    or si.self_table not in self.cfg.tables            # never written: RTL tab_valid low
                                    or (si.up_kind == UP_LEAF and (si.up_table in self.cfg.invalid_tables
                                                                   or si.up_table not in self.cfg.tables))))):
            cause = RJ_UNKNOWN
        elif si.up_kind == UP_ENTRY and not up_live:
            cause = RJ_UP_NOT_LIVE
        elif (self.ent[up_idx].depth + 1 if si.up_kind == UP_ENTRY else 1) > self.cfg.d_max \
                or (si.up_kind == UP_ENTRY and self.ent[up_idx].depth == D_MAX_CFG):
            cause = RJ_TOO_DEEP
        elif not free:
            cause = RJ_FULL
        else:
            cause = None
        if cause is not None:
            self.counters["rejects"] += 1
            self.emit(c, "REGD", (vid, 0, cause)); return
        i = free[0]; e = self.ent[i]
        up_eff = up_idx if up_idx is not None else 0
        up_gen_at_reg = self.ent[up_eff].gen           # read before this entry's own gen increments
        e.lc = LC_LIVE; e.inflight = 0; e.k = 0; e.ready = False; e.value_id = vid; e.site_class = si.site_class
        e.omc = msg["omc"]; e.gen = (e.gen + 1) & 0xFFFF
        e.depth = self.ent[up_idx].depth + 1 if si.up_kind == UP_ENTRY else 1
        e.e_r = self.proc; e.e_b = self.proc
        e.up_kind = si.up_kind; e.leaf_armed = (si.up_kind == UP_LEAF); e.up_table = si.up_table
        base = self.proc if ((self.proc - msg["ing_tick"]) & TICK_MASK) >> 31 else msg["ing_tick"]   # min(proc, ing), wrap-aware
        e.up_age_origin = (base - msg["upage"]) & TICK_MASK
        # the RTL's value CAM yields index 0 when nothing matches; mirror it so
        # the (irrelevant for non-ENTRY kinds) fields compare equal in traces
        e.up_handle = msg["handle"]; e.up_entry = up_eff
        e.up_gen = up_gen_at_reg; e.up_valid = (si.up_kind == UP_ENTRY)
        e.omc_link = si.omc_link; e.self_table = si.self_table
        e.self_age_origin = (base - msg["selfage"]) & TICK_MASK
        e.fwd_issued = False
        self.counters["registrations"] += 1
        if self.u_tap is not None:
            self.u_tap(self.proc, "REG", i, vid, si.site_class, msg["omc"], msg.get("mhat", 0))
        self.emit(c, "NEWE", (i, up_eff))
        self.emit(c, "REGD", (vid, 1, 0))
        self.jq_enq(i, e.gen, JM_REG, self.cfg.E, si.up_kind == UP_ENTRY and not self.ent[up_idx].ready, deltas)

    # ------------------------------------------------------------ Phase B (one entry per cycle)
    def phase_b_step(self):
        b = self.b; c = self.cyc
        i = b["i"]
        if i < N:
            e = self.ent[i]; s = self.slots[i]
            deltas = {}
            # install
            if self.thr:
                # threshold: every LIVE entry gets this pass's one-column line; its bit is c >= theta of the class
                if e.lc == LC_LIVE:
                    bit = 1 if e.omc <= self.cfg.theta.get(e.site_class, 0) else 0
                    e.ready = True; e.line = bit; e.e_b = self.proc; e.e_job = 1
                    self.emit_next.append(("INST", (i, bit, self.proc, 1)))
            elif s.valid and s.state == JS_WAIT_LINE and s.ready_tick is not None and ((s.ready_tick - self.proc) & TICK_MASK) >> 31:   # ready_tick < proc, wrap-aware
                installed = (e.lc == LC_LIVE and e.gen == s.gen)
                if installed:
                    e.ready = True; e.line = s.line; e.e_b = s.e_b; e.e_job = s.e_job
                    self.emit_next.append(("INST", (i, s.line, s.e_b, s.e_job)))
                self.jq_inst_take(i, installed)
            # advance
            if e.lc == LC_LIVE and e.inflight and e.completion_tick == self.proc:
                e.inflight = 0; e.k += 1
            # decide
            if e.lc == LC_LIVE and not e.inflight:
                F = self.cfg.classes[e.site_class].F
                jcol = (self.proc - e.e_b) & TICK_MASK
                fwd = 0
                if e.ready and e.k < F and jcol < e.e_job:
                    fwd = e.line if self.thr else self.lines[e.line].acts[jcol][e.k]
                b["pending"].append((e.value_id, fwd, e.k, i))
            self.rc_defer(deltas)
            b["i"] += 1
        else:
            b["done_cycle"] = c
        # stage 1: emit the previous entry's action (one cycle later)
        if b["pending"] and b["pending"][0][3] < i:
            vid, fwd, k, idx = b["pending"].pop(0)
            self.emit(c, "ACT", (vid, fwd, k, self.proc & 0xFF))
            if fwd:
                self.ent[idx].fwd_issued = True

    # ------------------------------------------------------------ Phase C: ACCEPT
    def accept_step(self, live):
        c = self.cyc
        if not self.q["ACC"] or self.q["ACC"][0][0] > c:
            return
        if not live:
            if self.acc_stale_n == 0:
                return                                   # arrived after the strobe: waits for the window
            self.acc_stale_n -= 1
            self.q["ACC"].pop(0)
            self.counters["stale_accepts"] += 1
            return
        _, msg = self.q["ACC"].pop(0)
        idx = self.find_vid(msg["vid"])
        e = self.ent[idx] if idx is not None else None
        if (e is not None and e.lc == LC_LIVE and not e.inflight and e.ready and e.k == msg["k"]
                and msg["tag"] == (self.proc & 0xFF) and e.fwd_issued):
            sc = self.cfg.classes[e.site_class]
            e.inflight = 1; e.fwd_issued = False
            e.completion_tick = (self.proc + sc.d[e.k]) & TICK_MASK
        else:
            self.counters["unmatched"] += 1

    # ------------------------------------------------------------ builder / take FSM
    def eligible(self):
        best = None
        for i in range(N):
            s = self.slots[i]
            if not s.valid:
                continue
            ok = False
            if s.state == JS_QUEUED:
                ok = True
            elif s.state == JS_WAIT_UPSTREAM:
                e = self.ent[i]; u = self.ent[e.up_entry]
                good = e.up_valid and (u.lc == LC_LIVE and u.gen == e.up_gen)
                stale = e.up_kind == UP_ENTRY and not good
                ready = e.up_kind == UP_ENTRY and good and u.ready
                dead = e.up_kind == UP_ENTRY and good and not u.ready and not self.slots[e.up_entry].valid
                ok = ready or stale or dead
            if ok and (best is None or ((s.enq_tick - self.slots[best].enq_tick) & TICK_MASK) >> 31):   # older first, wrap-aware
                best = i
        return best

    def quiescent(self):
        """The RTL's cfg_ok: no entry, no job, builder idle, Phase C with no snapshot pending, nothing queued."""
        bld_idle = self.bld is None or self.bld["phase"] == self.bld["cycles"] + 2   # completion visible this cycle: busy low
        return (all(e.lc == LC_FREE for e in self.ent) and not any(s.valid for s in self.slots) and bld_idle
                and self.seq == "C" and not self.snapq             # (the take FSM's state is not a condition: a cancelled
                                                                   # selection may sit in T_KEY through a pause -- GPT-6 round 29)
                and not any(self.q.values()) and not any(self.src.values()))

    def form_key(self, i, snap=None):
        """The key and job of slot i's entry; `snap` (KEY_LAT) supplies the entry, its upstream, the upstream slot's
        validity and the slot as registered one cycle earlier (line versions and table supports are read live)."""
        if snap is None:
            e = self.ent[i]; s = self.slots[i]; u = self.ent[e.up_entry]; up_slot_valid = self.slots[e.up_entry].valid
        else:
            e, u, up_slot_valid, s = snap
        k = dict(epoch=self.cfg.epoch, mode=s.mode, site_class=e.site_class, up_kind=UP_NONE, up_table=0, up_age_q=0,
                 up_line=0, delta=0, up_version=0, omc_link=e.omc_link, self_table=e.self_table, self_age_q=0,
                 omc=e.omc, e_job=0)
        job = dict(mode=0, self_age=0, up_tab=e.up_table, up_age=0, up_line=0, delta=0, e_job=0, suppress=False, park=False)
        good = e.up_valid and (u.lc == LC_LIVE and u.gen == e.up_gen)
        if e.up_kind == UP_ENTRY and good and u.ready:
            d = (s.e_b - u.e_b) & TICK_MASK
            job["mode"] = 2; k["up_kind"] = UP_ENTRY; k["up_line"] = u.line; job["up_line"] = u.line
            k["up_version"] = self.lines[u.line].ver; k["delta"] = d & 0x1FF; job["delta"] = d & 0x1FF
            if d >= u.e_job:
                job["suppress"] = True
            else:
                job["e_job"] = u.e_job - d; k["e_job"] = u.e_job - d
        elif e.up_kind == UP_ENTRY and good and not u.ready and not up_slot_valid:
            job["suppress"] = True                       # upstream dead (no job, not ready): permanent WAIT
        elif e.up_kind == UP_ENTRY and good and not u.ready:
            job["park"] = True                           # upstream alive, not READY yet: not takeable now
        elif e.up_kind == UP_ENTRY:
            job["mode"] = 1; k["up_kind"] = UP_LEAF; k["up_table"] = 0; job["up_tab"] = 0
            k["up_age_q"] = 2047; job["up_age"] = 2047
            ej = e_job_abs(s.e_b, e.e_r, self.cfg.E); job["e_job"] = ej; k["e_job"] = ej
            if ej == 0:
                job["suppress"] = True
        elif e.up_kind == UP_LEAF:
            job["mode"] = 1; k["up_kind"] = UP_LEAF; k["up_table"] = e.up_table
            aq = min(age_sat(s.e_b, e.up_age_origin), self.cfg.tables[e.up_table].support)
            k["up_age_q"] = aq; job["up_age"] = aq; job["e_job"] = s.e_job; k["e_job"] = s.e_job
        else:
            job["mode"] = 0
            aq = min(age_sat(s.e_b, e.self_age_origin), self.cfg.tables[e.self_table].support)
            k["self_age_q"] = aq; job["self_age"] = aq; job["e_job"] = s.e_job; k["e_job"] = s.e_job
        return k, job

    def build_job(self, i, job, line):
        """Run the datapath golden for the job and fill the line."""
        e = self.ent[i]
        self_t = self.cfg.tables[e.self_table]
        E = job["e_job"]
        if job["mode"] == 0:
            g = compose_direct(self_t, job["self_age"], E)
        elif job["mode"] == 1:
            up_t = self.cfg.tables.get(job["up_tab"], self_t)    # a stale ENTRY names table 0 at age 2047: exhausted -> the impulse, whatever the table
            g = compose_leaf(up_t, job["up_age"], e.omc_link, self_t, E)
        else:
            lm = self.lines[job["up_line"]]
            g = compose_entry(Line(S=lm.S, Om=lm.Om, E=lm.E), job["delta"], e.omc_link, self_t, E)
        sc = self.cfg.classes[e.site_class]
        omc_key = self.take["key"]["omc"]                 # the confidence captured with the key (round 14 #1)
        sol = qsolve(sc, omc_key, g.S_q(), g.Om_q())
        m = self.lines[line]
        m.S = g.S_q(); m.Om = g.Om_q(); m.E = E; m.acts = sol.A
        chunks = 0 if job["mode"] == 0 else g.chunks
        cycles = CYC_DIRECT + 2 * E if job["mode"] == 0 else CYC_SETUP + E + chunks * (CYC_CHUNK + E)
        return cycles

    def builder_step(self):
        """One enabled cycle of the take FSM and the builder.  After ALLOC at an
        enabled cycle c0: the builder sees start at the next enabled cycle, is
        busy (CSTART) from the one after, META is the `cycles`-th busy cycle,
        and bld_done is visible at the enabled cycle after META, when the line
        turns READY and the take FSM is still blocked."""
        c = self.cyc
        t = self.take
        done_now = False
        if self.bld is not None:
            b = self.bld
            b["phase"] += 1                      # 1: start seen, 2: CSTART (progress 1), ...
            # the builder's `cycles` counter excludes the CSTART cycle itself, so
            # CSTART..META spans cycles+1 enabled cycles and done follows one later
            if b["phase"] - 1 == b["cycles"] + 2:
                self.on_bld_done(); self.bld = None; done_now = True
        st = t["st"]
        if st == "IDLE":
            if self.bld is None and not done_now:
                sel = self.elig_hist[-1 - SEL_LAT] if len(self.elig_hist) > SEL_LAT else None   # the RTL's pipelined selection sees the state SEL_LAT cycles back
                if sel is not None:
                    t["st"] = "KEY"; t["idx"] = sel; t["up_idx"] = self.ent[sel].up_entry   # (KEY_LAT: the RTL's T_IDLE capture)
        elif st == "KEY":
            i = t["idx"]; s = self.slots[i]
            if not s.valid or s.state not in (JS_QUEUED, JS_WAIT_UPSTREAM) or (KEY_LAT and self.ent[i].up_entry != t["up_idx"]):
                t["st"] = "IDLE"
            elif KEY_LAT:
                # the RTL's T_KEY1: register the entry, its upstream, the slot and the upstream's slot validity
                e = self.ent[i]
                t["snap"] = (_copy.copy(e), _copy.copy(self.ent[e.up_entry]), self.slots[e.up_entry].valid, _copy.copy(s))
                t["gen"] = s.gen; t["st"] = "KEY2"
            else:
                t["key"], t["job"] = self.form_key(i)
                t["gen"] = s.gen
                if t["job"]["park"]:
                    s.state = JS_WAIT_UPSTREAM; t["st"] = "IDLE"
                else:
                    t["st"] = "LOOKUP0" if LOOKUP_LAT >= 2 else "LOOKUP"
        elif st == "KEY2":
            i = t["idx"]; s = self.slots[i]
            if not s.valid or s.gen != t["gen"]:
                self.race.setdefault("key_abort", 0); self.race["key_abort"] += 1
                t["st"] = "IDLE"
            else:
                t["key"], t["job"] = self.form_key(i, t["snap"])
                if t["job"]["park"]:
                    s.state = JS_WAIT_UPSTREAM; t["st"] = "IDLE"
                else:
                    t["st"] = "LOOKUP0" if LOOKUP_LAT >= 2 else "LOOKUP"
        elif st == "LOOKUP0":                                    # the RTL registers the set index and the directory row
            i = t["idx"]; s = self.slots[i]
            if not s.valid or s.gen != t["gen"] or i in self.jq_busy_entries(c + 1):
                self.race.setdefault("lookup_abort", 0); self.race["lookup_abort"] += 1
                t["st"] = "IDLE"
            else:
                t["st"] = "LOOKUP"
        elif st == "LOOKUP":
            i = t["idx"]; s = self.slots[i]; job = t["job"]
            if not s.valid or s.gen != t["gen"] or i in self.jq_busy_entries(c + 1):
                self.race.setdefault("lookup_abort", 0); self.race["lookup_abort"] += 1
                t["st"] = "IDLE"
            elif job["suppress"] and not LOOKUP_LAT:              # with a registered lookup the RTL suppresses in T_LOOKUP2 (round 19 #1)
                s.valid = False; self.counters["suppressed"] += 1; t["st"] = "IDLE"
            else:
                kint = pack_key(t["key"]); sidx = fold(kint)
                hit = None
                for w in range(4):
                    ent = self.dir[sidx][w]
                    if ent is not None and ent[0] == kint:
                        hit = ent[1]
                if LOOKUP_LAT:                       # the RTL registers the compare and acts in T_LOOKUP2
                    t["hit"] = hit; t["kint"] = kint; t["set"] = sidx; t["st"] = "LOOKUP2"; return
                if hit is not None:
                    self.counters["hits"] += 1
                    self.emit(c, "HIT", (i, hit, self.lines[hit].state))
                    s.state = JS_WAIT_LINE; s.pinned = True; s.line = hit; s.e_job = job["e_job"]
                    s.pinned_at = c                       # test tuning: a job-queue event registered on this edge meets the pin (RTL tk_wr_pin bypass)
                    s.ready_tick = self.rec_tick() if self.lines[hit].state == LS_READY else None
                    self.rc_delta({hit: +1})
                    t["st"] = "IDLE"
                else:
                    t["kint"] = kint; t["set"] = sidx; t["st"] = "ALLOC"
        elif st == "LOOKUP2":
            i = t["idx"]; s = self.slots[i]; job = t["job"]
            if not s.valid or s.gen != t["gen"] or i in self.jq_busy_entries(c + 1):
                self.race.setdefault("lookup_abort", 0); self.race["lookup_abort"] += 1
                t["st"] = "IDLE"
            elif job["suppress"]:
                s.valid = False; self.counters["suppressed"] += 1; t["st"] = "IDLE"
            else:
                hit = t["hit"]
                if hit is not None:
                    self.counters["hits"] += 1
                    if self.lines[hit].zero_at == c - ZERO_LAT:        # the RTL's pending list append is squashed by this hit
                        self.race.setdefault("hit_pending", 0); self.race["hit_pending"] += 1
                    elif ZERO_LAT and self.lines[hit].zero_at == c:      # ... or its stage-2 zero candidate is
                        self.race.setdefault("hit_zero_stage", 0); self.race["hit_zero_stage"] += 1
                    elif LIST_ENGINE and self.lines[hit].rc == 0 and self.lines[hit].listed_at is None:
                        self.race.setdefault("hit_queued", 0); self.race["hit_queued"] += 1   # its APPEND is in the queue: cancelled
                        if self.lq and self.lq[0][0] == hit and self.lq[0][2] and self.lq_busy_at != c:
                            self.race.setdefault("hit_exec_kill", 0); self.race["hit_exec_kill"] += 1   # ... at the head, executing this cycle
                        elif any(e[0] == hit and e[2] for e in self.lq):
                            self.race.setdefault("hit_queued_wait", 0); self.race["hit_queued_wait"] += 1   # ... waiting: the cancel proper
                    if LIST_ENGINE and self.lines[hit].listed_at is not None:
                        self.lq_busy_at = c + 1                            # the splice takes the engine's next cycle
                    if LIST_ENGINE:
                        self.lines[hit].listed_at = None
                        for e in self.lq_pend:                             # the RTL's stage kills ...
                            if e[1] == hit: e[3] = True
                        for e in self.lq:                                  # ... and queue cancel
                            if e[0] == hit: e[2] = False
                    if any(cc == c + 1 and e is None and f().get(hit, 0) < 0 for (cc, e, f) in self.jq_pending):
                        # a release of the hit line lands on the RTL edge of the hit: the zero detection must count both
                        self.race.setdefault("hit_release_same_edge", 0); self.race["hit_release_same_edge"] += 1
                    elif not LIST_ENGINE:
                        # the hit line is the evictable tail and another shared line is being appended behind it on this
                        # edge: the RTL's split removal must take that line as the successor (rm_app bypass)
                        listed = [l for l in range(L) if self.lines[l].state == LS_READY and self.lines[l].rc == 0
                                  and not self.lines[l].priv and self.lines[l].zero_at < c - ZERO_LAT and l != hit]
                        m_hit = self.lines[hit]
                        newer = [l for l in listed if ((self.gstamp - self.lines[l].stamp) & STAMP_MASK) < ((self.gstamp - m_hit.stamp) & STAMP_MASK)]
                        appending = any(self.lines[l].zero_at == c - ZERO_LAT and not self.lines[l].priv and self.lines[l].rc == 0 for l in range(L) if l != hit)
                        if m_hit.rc == 0 and not newer and appending:
                            self.race.setdefault("hit_tail_append", 0); self.race["hit_tail_append"] += 1
                    self.emit(c, "HIT", (i, hit, self.lines[hit].state))
                    s.state = JS_WAIT_LINE; s.pinned = True; s.line = hit; s.e_job = job["e_job"]
                    s.pinned_at = c                       # test tuning: a job-queue event registered on this edge meets the pin (RTL tk_wr_pin bypass)
                    s.ready_tick = self.rec_tick() if self.lines[hit].state == LS_READY else None
                    self.rc_delta({hit: +1})
                    t["st"] = "IDLE"
                else:
                    t["st"] = "ALLOC"
        elif st == "ALLOC":
            i = t["idx"]; s = self.slots[i]; job = t["job"]
            if not s.valid or s.gen != t["gen"] or i in self.jq_busy_entries(c + 1):
                self.race.setdefault("alloc_abort", 0); self.race["alloc_abort"] += 1
                t["st"] = "IDLE"; return
            if LIST_ENGINE:
                # a line is in a list from the cycle after its APPEND executed
                free = [l for l in range(L) if self.lines[l].state == LS_FREE and self.lines[l].listed_at is not None and self.lines[l].listed_at < c]
                evict = [l for l in range(L) if self.lines[l].state == LS_READY and self.lines[l].rc == 0 and not self.lines[l].priv
                         and self.lines[l].listed_at is not None and self.lines[l].listed_at < c]
            else:
                # (a line that lost its last reference is invisible to ALLOC for ALLOC_LAT cycles: the RTL's detection stages and,
                # without the pending bypass, the append itself)
                free = [l for l in range(L) if self.lines[l].state == LS_FREE and self.lines[l].zero_at <= c - ALLOC_LAT]
                evict = [l for l in range(L) if self.lines[l].state == LS_READY and self.lines[l].rc == 0 and not self.lines[l].priv
                         and self.lines[l].zero_at <= c - ALLOC_LAT]
            if not free and not evict:
                return                                     # pool exhausted: retry
            def older(l):                        # modular distance from the current stamp counter
                return ((self.gstamp - self.lines[l].stamp) & STAMP_MASK), -l
            if free:
                nl = max(free, key=older)
            else:
                nl = max(evict, key=older)
                self.counters["evicts"] += 1
                self.lq_evicted = True                        # the eviction POP takes the list engine's cycle
                m = self.lines[nl]
                if m.hasway and m.set == t["set"]:
                    self.race.setdefault("same_set_evict", 0); self.race["same_set_evict"] += 1
                if m.hasway:
                    self.dir[m.set][m.way] = None
            m = self.lines[nl]
            self.dbg_alloc_at.append((c, nl, "free" if free else "evict", m.listed_at if LIST_ENGINE else m.zero_at))
            if self.ent[i].omc != t["key"]["omc"]:                 # a RECONFIDENCE landed between KEY and ALLOC (round 14 #1)
                self.race.setdefault("omc_mid_take", 0); self.race["omc_mid_take"] += 1
            if (m.listed_at == c - 1) if LIST_ENGINE else (m.zero_at == c - ALLOC_LAT):   # taken the first cycle it may
                self.race.setdefault("alloc_pending", 0); self.race["alloc_pending"] += 1
                self.dbg_alloc_pending_at.append(c)
            m.state = LS_BUILDING; m.rc = 1; m.ver = (m.ver + 1) & 0xFF
            m.listed_at = None
            ways = self.dir[t["set"]]
            fw = None
            for w in range(4):
                if ways[w] is None:
                    fw = w; break               # the lowest-index free way (plan Sec. 4.4)
            if fw is not None:
                ways[fw] = (t["kint"], nl); m.hasway = True; m.set = t["set"]; m.way = fw; m.priv = False
            else:
                m.hasway = False; m.priv = True; self.counters["privates"] += 1
            s.state = JS_BUILDING; s.pinned = True; s.line = nl; s.e_job = job["e_job"]; s.ready_tick = None
            cycles = self.build_job(i, job, nl)
            self.counters["builds"] += 1
            e = self.ent[i]
            self.emit_next.append(("BLD", (job["mode"], e.self_table, job["self_age"], job["up_tab"], job["up_age"], e.omc_link,
                                           job["e_job"], job["up_line"], job["delta"], nl, self.take["key"]["omc"])))
            self.bld = dict(phase=0, cycles=cycles, line=nl)
            # the builder is paused from the next cycle: Phase A reaches DONE this cycle (the entry table steps after the
            # builder within a cycle, so "NEXT with nothing left" is the DONE cycle) or already did
            a_ = self.a
            if a_ is not None and self.seq == "A" and (a_["st"] == "DONE" or (a_["st"] == "NEXT" and not any(a_["n"].values())
                                                          and not (a_["todo_rej"] or a_["todo_casc"] or a_["todo_depth"]))):
                self.race.setdefault("alloc_at_pause", 0); self.race["alloc_at_pause"] += 1
            t["st"] = "IDLE"

    def on_bld_done(self):
        nl = self.bld["line"]; m = self.lines[nl]
        self.trace.append((self.cyc, self.tick, "DONE", (nl,)))
        m.state = LS_READY
        busy = self.jq_busy_entries(self.cyc + 1)     # slots the entry table replaces/cancels in the RTL's done cycle
        for i, s in enumerate(self.slots):
            if s.valid and s.pinned and s.line == nl and s.state in (JS_BUILDING, JS_WAIT_LINE) and i in busy:
                self.race.setdefault("done_excl", 0); self.race["done_excl"] += 1
            if s.valid and s.pinned and s.line == nl and s.state in (JS_BUILDING, JS_WAIT_LINE) and i not in busy:
                s.state = JS_WAIT_LINE; s.ready_tick = self.rec_tick()
        # The RTL stamps an unreferenced completion at the completion edge, AFTER the releases applied at that edge;
        # those releases reach this model one cycle later (rc_defer), so the zero check and the stamp are deferred
        # to that cycle and run after rc_delta (GPT-6 round 18 #1).  Until then the line is not evictable.
        m.completing = True
        self.done_pending.append((self.cyc + 1, nl))

    # ------------------------------------------------------------ main loop
    def run(self, workload, n_ticks, strobe0=20):
        return self.run_reactive(workload, n_ticks, strobe0, None, None)

    def run_reactive(self, workload, n_ticks, strobe0, acc_off, accept_fn, acc_log=None):
        """workload: dict tick -> list of (offset_cycle, type, msg). Messages are
        pushed at cycle strobe(tick) + offset. With accept_fn, every FORWARD
        action of tick t is answered by an ACCEPT pushed at strobe(t) + acc_off + n
        when accept_fn(vid, k, t) is true (the testbench's scheduler model).
        acc_log: a list that receives (cycle, vid, k, tag) of every ACCEPT the scheduler model pushes, so that
        a stateful scheduler's answers can be replayed as explicit ACCEPT messages.
        Returns the egress list [(tick, type, data)]."""
        P = self.P
        cycle_end = strobe0 + P * (n_ticks + 1)
        sched = {}
        for t, lst in workload.items():
            for off, typ, msg in lst:
                sched.setdefault(strobe0 + P * t + off, []).append((typ, msg))
        strobes = {strobe0 + P * t: t for t in range(n_ticks + 1)}
        self.strobe_cycles = set(strobes)
        cur_strobe = None; self.fwd_q = []          # FORWARD actions the scheduler has seen and not yet answered (per tick)
        ACC_K = 0                                  # cycles between an action reaching the log and its ACCEPT push
        self.fatal = False
        # threshold build: skip the cycles in which nothing can happen (a speed-up only; SE_THR_NOSKIP=1 runs every cycle,
        # and tests.thr_checks shows both give the same egress)
        skip = self.thr and not int(_os.environ.get("SE_THR_NOSKIP", "0"))
        busy_at = sorted(set(sched)) if skip else []
        skip_to = -1
        for c in range(cycle_end):
            if c < skip_to:
                continue
            self.cyc = c
            if self.emit_next:
                for typ, data in self.emit_next:
                    self.emit(c, typ, data)
                self.emit_next = []
            if self.emit_at:
                due = [(cc, typ, d) for (cc, typ, d) in self.emit_at if cc <= c]
                self.emit_at = [(cc, typ, d) for (cc, typ, d) in self.emit_at if cc > c]
                for cc, typ, d in due:
                    self.out.append((self.tick, typ, d)); self.trace.append((c, self.tick, typ, d))
                    if typ == "ACT" and d[1] == 1:
                        self.fwd_q.append((c, self.tick, d))       # visible to the testbench's scheduler from this cycle
            # job-queue side effects of earlier entry-table events land now
            if self.jq_pending:
                due = [f for (cc, e, f) in self.jq_pending if cc <= c]
                self.jq_pending = [(cc, e, f) for (cc, e, f) in self.jq_pending if cc > c]
                merged = {}
                for f in due:
                    for l, d in f().items():
                        merged[l] = merged.get(l, 0) + d
                if merged:
                    self.rc_delta(merged)
            if self.done_pending:
                due = [l for (cc, l) in self.done_pending if cc <= c]
                self.done_pending = [(cc, l) for (cc, l) in self.done_pending if cc > c]
                for nl in due:                            # after this cycle's releases: the RTL's completion order
                    m = self.lines[nl]; m.completing = False
                    if m.rc == 0 and m.state == LS_READY:
                        if LIST_ENGINE:
                            m.listed_at = None; self.lq_pend.append([c + 2, nl, m.priv, False])
                        else:
                            m.stamp = self.gstamp; self.gstamp = (self.gstamp + 1) & STAMP_MASK   # shared -> evictable, private -> free
                        m.zero_at = c
                        self.dbg_unref_completions += 1
                        if m.priv:
                            m.state = LS_FREE
            if LIST_ENGINE and self.lq_pend:
                # the RTL enqueues its pending registers' lines (a line hit meanwhile was dropped before the queue)
                due = [e for e in self.lq_pend if e[0] <= c]
                self.lq_pend = [e for e in self.lq_pend if e[0] > c]
                for cc, l, pv, killed in due:
                    if not killed:
                        self.lq.append([l, pv, True, c])
                assert len(self.lq) <= LQ_DEPTH, ("list operation queue overflow", c)
                self.lq_hwm = max(self.lq_hwm, len(self.lq))
            self.lq_evicted = False
            if c in strobes:
                # the scheduler answers this tick's FORWARDs from acc_off on; unanswered ones are dropped at the strobe
                cur_strobe = c; self.fwd_q = []
            if accept_fn is not None and cur_strobe is not None and c >= cur_strobe + acc_off and self.fwd_q and self.fwd_q[0][0] + ACC_K <= c:
                # the testbench's scheduler: one ACCEPT per cycle from acc_off on, for every FORWARD it has logged so far --
                # including those logged after acc_off (GPT-6 round 16 #1); it echoes the ACTION's tag
                cc_seen, tt, data = self.fwd_q.pop(0)
                vid, fwd, k, tag = data
                if accept_fn(vid, k, tt):
                    sched.setdefault(c, []).append(("ACC", dict(vid=vid, k=k, tag=tag)))
                    if acc_log is not None:
                        acc_log.append((c, vid, k, tag))
            self.cyc = c
            # ---- ingress: producers hold a message at the port until its queue has room
            #      (32 entries; the count is the RTL's registered occupancy at this cycle)
            for typ, msg in sched.get(c, []):
                if typ not in ("CFG", "THW"):
                    self.src[typ].append(msg)    # every same-cycle arrival is presented before CONFIG is judged
            for typ, msg in sched.get(c, []):
                if typ == "THW":                 # threshold build: a class's threshold written during the run
                    if self.quiescent():
                        self.cfg.theta[msg["c"]] = msg["omc"]
                    continue                     # refused while busy (flag only)
                if typ == "CFG":
                    if self.quiescent():         # accepted: epoch bump, directory and pool flushed (Sec. 4.1)
                        self.dbg_cfg_take_st.append(self.take["st"])
                        self.cfg.epoch = (self.cfg.epoch + 1) & 0xFF
                        self.lines = [LineMeta(i) for i in range(L)]; self.dir = [[None] * 4 for _ in range(SETS)]; self.gstamp = L
                        # the flush also drops the list engine's deferred work (the RTL clears pend_v / lq_v) -- GPT-6 round 28 #1
                        self.lq = []; self.lq_pend = []; self.done_pending = []; self.lq_busy_at = -1
                        self.bld = None; self.pending_done = None; self.bld_done_cycle = None   # a completion in this cycle is superseded by the flush
                    continue                     # refused while busy (flag only)
            # the snapshot of a strobe counts what was written BEFORE this cycle
            if c in strobes:
                snap_now = dict(self.written)
            for typ in ("REG", "RES", "LV", "RC", "RD", "ACC"):
                occ = len(self.q[typ]) + (typ != "ACC" and self.last_pop[typ] == c - 1)   # (last cycle's pop: not yet visible)
                if self.src[typ] and occ < QUEUE_DEPTH:
                    msg = self.src[typ].pop(0)
                    self.q[typ].append((c + 1, dict(msg, ing_tick=(self.tick + (1 if c in strobes else 0)) & TICK_MASK)))   # same-edge: the round the strobe starts
                    if self.diag is not None:
                        self.diag.append(("IN", c, typ, msg))
                    if typ != "ACC":
                        self.written[typ] += 1
            # ---- strobe: tick register increments (visible from c+1)
            strobe = c in strobes
            # sequencer (registered, so decisions use the state at the start of the cycle)
            bld_en = self.seq not in ("B", "B_GO")     # the RTL's S_B spans b_go .. b_done (N+3 cycles)
            if bld_en != self.last_bld_en:
                self.trace.append((c, self.tick, "PAUSE", (int(bld_en),)))
            self.last_bld_en = bld_en
            acc_window = (self.seq == "C") and not strobe and not self.snapq
            # a pass starts the cycle after its snapshot was queued (the RTL's snap_n is a register)
            if self.seq == "C" and self.snapq:
                cut = self.snapq.pop(0)
                counts = {typ: cut[typ] - self.popped[typ] for typ in cut}   # earlier passes drained theirs
                self.proc = (self.proc + 1) & TICK_MASK
                self.seq = "A"; self.a = {"st": "GO", "n": counts, "todo_rej": set(), "todo_casc": set(), "todo_depth": set(), "op_tick": 0}
                self.a_start = c
                if self.diag is not None:
                    self.diag.append(("PASS", c, self.proc, dict(counts)))
            elif self.seq == "A":
                if self.a["st"] == "DONE":
                    self.seq = "B_GO"; self.b = {"i": 0, "pending": [], "done_cycle": None, "go": c}
            elif self.seq == "B_GO":
                self.seq = "B"                           # b_go visible; the entry table starts next cycle
            elif self.seq == "B":
                if self.b["done_cycle"] is not None and self.b["done_cycle"] < c:
                    self.seq = "C"
            if strobe:
                # the snapshot of this strobe: the write cutoffs (what had been written before now)
                if len(self.snapq) >= SNAPQ:             # backlog beyond the envelope: FATAL (Secs. 3.2, 3.4); the engine freezes
                    self.fatal = True
                    break
                backlog = bool(self.snapq)                # an older snapshot still waiting: this one queues behind it
                self.snapq.append(snap_now)
                if self.seq != "C" or self.a_start == c or backlog:
                    self.counters["overruns"] += 1
                if self.seq == "C" and self.a_start != c and backlog:
                    self.dbg_backlog_hits += 1              # the b_done-cycle strobe behind a queued snapshot
                # ACCEPTs already queued at the strobe answer the previous round: stale (the RTL's acq_cnt)
                self.acc_stale_n = sum(1 for (v, _) in self.q["ACC"] if v <= c)
                self.strobe_dbg.append((c, self.seq, self.a_start, None if self.a is None else self.a.get("done_cycle"), None if self.b is None else self.b.get("done_cycle"), len(self.snapq)))
            if not self.thr:                             # (the threshold build has no job queue, builder or list engine)
                self.elig_hist.append(self.eligible()); del self.elig_hist[:-8]   # the selection pipeline's snapshot history
                # ---- builder domain first: it sees the entry table's registers as they
                #      were at the end of the previous cycle (the RTL's registered view)
                if bld_en:
                    self.builder_step()
                if LIST_ENGINE:
                    self.list_engine_step(c)             # every cycle: the list engine does not pause with the builder
            # ---- entry table
            if self.a is not None and self.seq in ("A", "B_GO", "B", "C"):
                a = self.a
                if a["st"] == "GO":
                    if c >= self.a_start + 2:
                        for e in self.ent:
                            e.fwd_issued = False
                        a["st"] = "NEXT"; self.phase_a_step()
                elif a["st"] not in ("DONE",):
                    self.phase_a_step()
            if self.seq == "B" and self.b is not None:
                if c >= self.b["go"] + 2:
                    if self.b["done_cycle"] is None:
                        self.phase_b_step()
                    elif self.b["pending"]:
                        vid, fwd, k, idx = self.b["pending"].pop(0)
                        self.emit(c, "ACT", (vid, fwd, k, self.proc & 0xFF))
                        if fwd:
                            self.ent[idx].fwd_issued = True
            # ACCEPTs: live only in the accept window; anything queued outside it is stale
            self.accept_step(acc_window and (self.a is None or self.a["st"] == "DONE"))
            # ---- tick register update at the strobe (visible next cycle)
            if strobe:
                self.tick = (self.tick + 1) & TICK_MASK   # the RTL's tick register: +1 per strobe, visible next cycle
            if skip and self.seq == "C" and not self.snapq and not self.emit_next and not self.emit_at \
                    and not any(self.q.values()) and not any(self.src.values()):
                # idle until the next strobe, the next scheduled message, or the scheduler's ACCEPT window
                nxt = strobe0 + P * ((c + 1 - strobe0 + P - 1) // P) if c + 1 > strobe0 else strobe0
                k_ = bisect.bisect_right(busy_at, c)
                if k_ < len(busy_at):
                    nxt = min(nxt, busy_at[k_])
                if self.fwd_q and accept_fn is not None and cur_strobe is not None:
                    nxt = min(nxt, max(c + 1, cur_strobe + acc_off))
                skip_to = nxt
        return self.out
