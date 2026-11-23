"""The Froq policy (v4): for every speculated value a stored FORWARD / WAIT table over (round, boundary) computed by the
RTL's bit-exact golden (hw/golden: the DIRECT-path composer conditioning the class table by age, then the fixed-point
survival-scaled backward sweep with the engine's belief floor and tie rule), installed and read the way the RTL does.

Engine tick model (RTL plan Sec. 3.1 / 3.2 / 4.5 / 4.6, hw/rtl/se_pkg.sv, hw/README.md):
  REGISTER a published prediction registers the value at a SITE-INSTANCE ADDRESS (queue depth 32 per round; the entry
             table holds 64 -- a full table REJECTs, modelled as the client waiting a round) and enqueues its first solve
             job. The client can only register what it has configured, so a value is registered only if its contract has
             a site (a positive-duration atom, F >= 1; review round-1 finding 5), its site class is in the codebook (a
             class miss was registered all the same before round-2 finding A1, and the policy then built a class the
             client had never configured), that class fits the RTL's fields (F <= F_MAX edges of 1..64 ticks, 16-bit
             prices, n_s in Q8.8, at most 64 site classes and 64 latency tables) and an instance address is free.
             Addresses are allocated from the 1024 of se_pkg.sv (INST_W = 10 bits), each carrying the (site class,
             latency table) pair it was configured with, and freed for reuse at RESOLVE / RECOVERY_DONE; the value id is
             never used as an address. Anything else means no solve, no line, no live entry: the value WAITs for the
             definitive value (design Sec. 2.4).
  builder ONE builder, jobs FIFO, 24 + 2 E_rel enabled cycles per job (28 + 2 E_rel on the F_MAX = 8 variant used for K = 6 / 8);
             it is paused during the entry pass (N + 8 = 72
             cycles per round), so it has P_b = cycles_per_round - 72 enabled cycles per round; a job finished in round r is
             installed by the pass of round r + 1 and readable from then. Every build is the DIRECT path: the client
             configures, per volume class, one latency table holding the composed law of eq. (2) (accepted and rejected
             decoder branches; 7 classes <= N_TABLES = 64) and REGISTER names the producer's class -- known at compile time
             from a profile of the same schedule under native SWIPER (`class_profile`, written by the N run), else the real
             windows when they exist, else provisional. A reset decode attempt is not an event: the stored table is indexed by absolute
             round and its composed law already carries the rejected branch (ageing the same table, plan Sec. 3.2 / 8). RESOLVE cancels the queued job and frees the
             table; a running job finishes and is discarded. The registered table is immutable (RTL: self_table is fixed
             at REGISTER): a real window class larger than the profiled one is recorded as a mismatch, never rebuilt.
  COMPOSE the engine composes a DIRECT job when the take FSM builds it (form_key at the take's KEY step, build_job at
             ALLOC; hw/golden/engine.py:606-666), with every input frozen into the job-queue slot at the enqueue pass
             (jq_enq, engine.py:283-296): s.e_b = the label of the pass that processed the REGISTER, s.e_job = the
             remaining horizon at that pass. self_age = min(age_sat(s.e_b, self_age_origin), table.support)
             (engine.py:642) with self_age_origin = min(register pass, ingress tick) - selfage (engine.py:504-511); a
             REGISTER of device round r enters at tick r + 1 and is processed by the pass labelled r + 2
             (ENGINE_TICK_OFFSET, froq/test_route_a.py:391), so self_age = (r - producer_end) + 1 -- one column older
             than the enqueue round's age (the shift that made golden lines WAIT where the policy FORWARDed, K1b).
             E_rel = s.e_job: the configured horizon E = 256 for the registration job (engine.py:516, jq_e_job = e_cfg,
             24 + 512 cycles), E - the rounds since registration for an event rebuild (e_job_abs, engine.py:337/351);
             build cycles = CYC_DIRECT + 2 E_rel (engine.py:665). The installed line is indexed from the enqueue pass:
             e.e_b = s.e_b at install (engine.py:529), the entry pass reads column jcol = proc - e.e_b
             (engine.py:538-541), and a directory HIT keeps the entry's own s.e_b (engine.py:743/793). The policy
             mirrors this: _enqueue freezes the slot fields (age, E_rel, site, class) and _compose_job composes and
             solves when the builder takes the job.
  ACTION the entry pass emits one ACTION per live entry per round at the entry's CURRENT boundary (the device asks only
             for that boundary, after the previous step completed at its encoded duration); every query about the same
             (entry, boundary) in a round is that one ACTION (memoised). FORWARD only on an explicit "F"; "W" and an
             exhausted table ("-") are WAIT.
  records per round: live entries (REGISTER.. RESOLVE / RECOVERY_DONE), policy lines (installed tables of unresolved
             entries + the one being built), pending jobs, ACTIONs, REGISTERs, and an estimated tick overrun (3 cycles per
             Phase-A message + the 72-cycle pass beyond the round's cycles). Tables are not shared between values whose
             site and timetable coincide (the RTL's directory does): the charged build service is an upper bound.
Timetable = the producer's window collects `collect` more rounds, then the path, then a PyMatching decode (the simulator's
latency table at the volume class of the largest window covering the producer, under the simulator's own lookup rule),
composed with the accepted / rejected upstream branches as eq. (2) and conditioned on the rounds already elapsed.

The SWIPER client (client="swiper") publishes the merge parity SWIPER's windows decoded at T_emit
(before verification) with a per-value confidence: REGISTER carries that value's omc (the engine takes omc per entry,
hw/golden/engine.py do_register), the one latency table the client configures (class 0) holds the trained law of the
delay from T_emit to delivery (`gap_pmf`, from the seed-1 training run), the self age counts from T_emit, and a reset of
one of the value's own windows after T_emit is a RECONFIDENCE (the engine's RC message: the entry's omc replaced, its
table rebuilt), charged to the builder like any rebuild. The modeled client (the default) is unchanged: every job
carries the one configured omc."""
from __future__ import annotations

import math, os, sys

from . import composer                                          # the float eq. (2) law that the client's table holds
HW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hw")
if HW not in sys.path:
    sys.path.insert(0, HW)
from golden import tables as gtables                            # noqa: E402  the RTL's bit-exact golden (hw/golden): fixed-point
from golden.compose import compose_direct                      # noqa: E402  table conditioning by age (DIRECT path) and the
from golden.solve import SiteClass, solve as solve_fixed       # noqa: E402  survival-scaled backward sweep with its belief floor
from golden.fx import F_OMC, BELIEF_FLOOR_SHIFT                # noqa: E402

T_LEN = 1024                                                    # hw/rtl/se_pkg.sv: latency table length
POLICY_MODES = ("dp", "forward", "threshold", "greedy")         # the action policy is pluggable


def site_key(sc) -> tuple:
    """A site class as the client's CONFIG record identifies it (hw/golden/engine.py `SiteClass`): two values share a
    configured class exactly when these fields coincide."""
    return (int(sc.F), tuple(int(x) for x in sc.d), tuple(int(x) for x in sc.r),
            tuple(bool(x) for x in sc.settle_dst), int(sc.ns_q))


class FixedPolicy:
    """The stored action table of one build: A[j][k] for j = rounds since the build round, as the RTL's solver wrote it,
    with the installed site's step durations so the runtime path re-enforces the horizon rule (finding 12, design Sec. 4:
    FORWARD is forbidden whenever j + d_k > E_job, equality allowed -- the same rule the golden and RTL solvers apply)."""
    def __init__(self, built: int, solved, E_job: int, F: int, durations=(), mode: str = "dp"):
        self.built, self.A, self.E_job, self.F = built, solved.A, E_job, F
        self.flags = solved.flags
        self.durations = tuple(durations)
        # "dp" / "greedy": the table the solver wrote (golden.solve.solve / solve_greedy); "forward": FORWARD at every
        # admissible cell (threshold-style admission, E5).  The threshold mode holds no table (ScoutPolicy._tf)
        assert mode in ("dp", "forward", "greedy"), mode
        self.mode = mode

    def act(self, now: int, k: int) -> str:
        return self.act_why(now, k)[0]

    def act_why(self, now: int, k: int) -> tuple:
        """(action, source) -- the source names what decided (WAIT history): "table" the solved table, "forward"
        the forward mode, and for a WAIT the table's limits force: "depth" (k >= F), "expired" (j outside [0, E_job)),
        "horizon" (j + d_k > E_job)."""
        j = now - self.built
        if k >= self.F:
            return "W", "depth"
        if j >= self.E_job or j < 0:
            return "W", "expired"
        if k < len(self.durations) and j + self.durations[k] > self.E_job:
            return "W", "horizon"                                  # a stale or supplied FORWARD past the horizon is WAIT
        if self.mode == "forward":
            return "F", "forward"                                  # threshold-style admission: the contract and the horizon are the only limits
        return ("F" if self.A[j][k] else "W"), "table"


def solve_greedy(site: SiteClass, omc: int, S: list, Om: int):
    """The greedy one-step rule of the round-1 gap analysis (concern 2), on the DP's own inputs and fixed-point formats:
    at column j and boundary k, FORWARD iff the expected contradiction cost at the destination, (1 - c) r_fwd(k), is at
    most the idle cost of waiting for the expected remaining resolution time, n_s E[T - j | T >= j], where
    E[T - j | T >= j] = (S[j] + ... + S[E - 1] + Om) / S[j] under the conditioned arrival law the DP sweeps (Om: the
    survival mass beyond the last column, the DP's seed).  Exact integer comparison, 1 - c = omc 2^-20 and
    n_s = ns_q / 256:  omc r_fwd(k) 256 S[j] <= ns_q (sum S + Om) 2^20.  The belief floor and the horizon rule apply as in
    golden.solve.solve: WAIT where S[j] 2^20 < S[0] and wherever the step would end past E.  (The earlier line's myopic
    rule, 85a336a, compared the price INCREASE r_{k+1} - r_k with the idle rounds saved during the step; this is the
    rule the gap analysis states.)"""
    from golden.solve import Solved, F_MAX as GF_MAX
    E = len(S) - 1
    A = [[0] * max(GF_MAX, site.F) for _ in range(E + 1)]
    tail = Om                                                      # Om + sum_{i >= j} S[i], built backwards
    for j in range(E - 1, -1, -1):
        tail += S[j]
        if (S[j] << BELIEF_FLOOR_SHIFT) < S[0]:
            continue
        for k in range(site.F):
            if j + site.d[k] <= E:
                A[j][k] = int(omc * site.r_fwd(k) * 256 * S[j] <= site.ns_q * tail * (1 << F_OMC))
    return Solved(A=A, Vh=[], flags={})


def solve_variant(site: SiteClass, omc: int, S: list, Om: int, ns_lanes: list | None = None, saturate: bool = True):
    """golden.solve.solve with two simulated-policy variants (not the RTL): `ns_lanes`, a waiting cost n_s per lane
    k = 0..F (Q8.8) instead of the site's one n_s -- n(p): the program patches idling while the value waits at boundary k
    -- and `saturate` = False, the same recurrence, roundings and tie rule without the Q22.24 saturation of Vhat (the
    flag v_sat_avoided counts the entries the RTL would have saturated). With ns_lanes = None and saturate = True it is
    golden.solve.solve, entry for entry."""
    from golden.solve import Solved, setup_xy, F_MAX as GF_MAX
    from golden.fx import F_XY, F_NS, F_S, V_MAX, rnd, sat
    E = len(S) - 1
    F = site.F
    ns = list(ns_lanes) if ns_lanes is not None else [site.ns_q] * (F + 1)
    assert len(ns) == F + 1, (ns, F)
    x, y = setup_xy(site, omc)
    flags = {}
    Vh = [[0] * (F + 1) for _ in range(E + 1)]
    A = [[0] * max(GF_MAX, F) for _ in range(E + 1)]
    floor = S[0]

    def clip(v):
        if saturate:
            return sat(v, V_MAX, flags, "v_sat")
        if v > V_MAX:
            flags["v_sat_avoided"] = flags.get("v_sat_avoided", 0) + 1
        return v

    prod_xy = lambda coef, ds: rnd(coef * ds, F_XY + F_S - F_S)
    for k in range(F + 1):
        Vh[E][k] = clip(rnd(ns[k] * Om, F_NS) + prod_xy(y[k], S[E]))
    for j in range(E - 1, -1, -1):
        j1 = j + 1
        dS1 = S[j] - S[j1]
        Vh[j][F] = clip(Vh[j1][F] + rnd(ns[F] * S[j], F_NS) + prod_xy(y[F], dS1))
        below = (S[j] << BELIEF_FLOOR_SHIFT) < floor
        for k in range(F):
            j2 = min(j + site.d[k], E)
            over = j + site.d[k] > E
            qF = prod_xy(x[k], S[j] - S[j2]) + Vh[j2][k + 1]
            qW = rnd(ns[k] * S[j], F_NS) + prod_xy(y[k], dS1) + Vh[j1][k]
            if below or over:
                A[j][k], v = 0, qW
            else:
                fwd = qF <= qW
                A[j][k], v = int(fwd), (qF if fwd else qW)
            Vh[j][k] = clip(v)
    return Solved(A=A, Vh=Vh, flags=flags)


from .device import FORWARD, WAIT, LINK
from .values import VState
from .latency import LatencyTable

N_ENTRIES, L_LINES, REGISTER_QUEUE, PASS_CYCLES, MSG_CYCLES = 64, 256, 32, 64 + 8, 3
# hw/rtl/se_pkg.sv: the client configures at most N_SITES site classes and N_TABLES latency tables, and REGISTER names one
# of N_INST site instances (INST_W = 10 bits).  A site class holds F <= F_MAX edges of D_MAX ticks, prices of PRICE_W bits
# and n_s in NS_W bits of Q8.8; a value whose site does not fit cannot be configured at all, so it never speculates.
N_SITES, N_INST, N_TABLES, D_MAX, PRICE_W, NS_W = 64, 1024, 64, 64, 16, 16


class ScoutPolicy:
    def __init__(self, distance: int, table: LatencyTable, K: int = 4, c: float = 0.9, c_up: float = 0.9,
                 collection_rounds: int | None = None, path_rounds: int = 0,
                 controller_mhz: float | None = 270.0, horizon: int = 256, class_profile: dict | None = None, policy_mode: str = "dp",
                 theta: float | None = None, ns_mode: str = "site", saturate: bool = True, n_sites: int = 64,
                 client: str = "modeled", gap_pmf: dict | None = None, install_latency: int = 1, rc_latency: int = 1):
        self.d, self.K, self.c, self.c_up = distance, K, c, c_up
        assert client in ("modeled", "swiper", "strip"), client
        self.client = client                                          # "modeled": one configured c; "swiper", "strip": c per value
        if client == "swiper" and not gap_pmf:
            raise ValueError("the SWIPER client needs the trained law of the delay from T_emit to delivery (gap_pmf)")
        if client == "strip" and not gap_pmf:                         # the verdict's delay after the merge
            raise ValueError("the strip client needs the trained law of the verdict's delay after the merge's end (gap_pmf)")
        self.gap_pmf = {int(k): float(w) for k, w in (gap_pmf or {}).items()}   # gap in rounds -> weight (seed-1 training run)
        self.reconfidences = 0; self._rc_log: dict = {}; self._rc_round: list = []   # RC messages: vid -> [(round, omc)], this round's
        assert policy_mode in POLICY_MODES, policy_mode
        # "dp" (the solved table), "forward" (E5: ARTERY-style threshold admission) and "greedy" (`solve_greedy`) run
        # through the builder, so their construction and install timing are the DP's.  "threshold" is table-free (the
        # earlier line's `threshold_fixed`, v9 commit ab76147): no builder, no table and no horizon; REGISTER stores the
        # value's advertised confidence, and from REGISTER + install_latency on (BUILDING until then) the entry FORWARDs
        # at every boundary iff that confidence >= theta, else WAITs.  An RC replaces the stored confidence the same way, from
        # RC + rc_latency. The two latencies are the threshold path's deadlines (gap analysis rule 41b': installation
        # 5 rounds and RECONFIDENCE 8 in the main convention, 1 and 1 in the original protocol); a decision whose value
        # RESOLVEs before its deadline never takes effect, a miss, counted by kind.
        assert (theta is not None) == (policy_mode == "threshold"), "theta is the threshold mode's parameter (and only its)"
        assert int(install_latency) >= 0 and int(rc_latency) >= 0, (install_latency, rc_latency)
        self.policy_mode, self.theta, self.install_latency, self.rc_latency = policy_mode, theta, int(install_latency), int(rc_latency)
        self._tf: dict = {}                                             # threshold: vid -> [first decision round, stored confidence]
        self._tf_kind: dict = {}                                        # threshold: vid -> the pending decision's kind (install, rc)
        self.tf_installs = self.tf_forward = self.tf_wait = 0
        self.tf_missed = dict(install=0, rc=0)                          # decisions whose value resolved before their deadline
        # simulated-policy variants (solve_variant; the RTL solves with one n_s per site and saturates Vhat):
        # ns_mode "boundary" prices waiting at boundary k with n_s(k) = the program wires of the support L_k
        assert ns_mode in ("site", "boundary"), ns_mode
        assert policy_mode == "dp" or (ns_mode == "site" and saturate), "variants apply to the DP solve"
        self.ns_mode, self.saturate = ns_mode, bool(saturate)
        self.n_sites = int(n_sites)                                     # site classes the client may configure (RTL: N_SITES = 64; varies it)
        # hw/rtl: the solver's SETUP takes F_MAX + 2 cycles; K <= 4 runs on the F_MAX = 4 engine (24 + 2 E cycles per build),
        # K = 6 / 8 on the F_MAX = 8 variant (28 + 2 E; tests.calibrate_builder at SE_F_MAX=8)
        self.build_base = 24 + (4 if K > 4 else 0)
        self.F_MAX = 8 if K > 4 else 4                                  # the engine variant this K runs on (se_pkg.sv F_MAX)
        self.class_profile = class_profile or {}                        # producer instruction -> window class (compile-time knowledge)
        self.collect = distance if collection_rounds is None else collection_rounds
        self.path, self.controller_mhz, self.horizon = path_rounds, controller_mhz, horizon
        self.table = table
        self.omc = self.omc_of(c)                                          # 1 - c in Q0.20, as CONFIG loads it
        self._tables: dict[int, object] = {}                              # volume class -> the client's configured latency table
        self.table_clipped = 0.0; self.solver_flags: dict[str, int] = {}; self.exhausted_builds = 0
        self.lines_hist: dict[int, int] = {}                              # policy lines held -> number of rounds
        self.cycles_per_round = (controller_mhz or 0.0) * float(getattr(table, "round_us", 1.0))   # cycles per round = MHz x round duration (1 us by default)
        self.builder_cycles_per_round = max(self.cycles_per_round - PASS_CYCLES, 1.0)
        # per value
        self.class_of_value: dict[int, int] = {}                         # vid -> volume class the current table was built with
        self.E_abs: dict[int, int] = {}                                  # vid -> absolute horizon fixed at registration
        self._installed: dict[int, dict] = {}                            # vid -> {policy, cls, installed}
        self._registered: dict[int, dict] = {}                           # vid -> {inst, cls, selfage, site} at REGISTER (the summary's controller trace)
        # the configured site classes and site instances (finding A1).  An instance address is 10 bits and holds the
        # (site class, latency table) a REGISTER names, so an address is reused only by a value needing the SAME pair --
        # the configuration is immutable, as in the RTL; it is freed for reuse at RESOLVE / RECOVERY_DONE.
        self.site_class_idx: dict[tuple, int] = {}                       # site class tuple -> configured class index < N_SITES
        self._inst_cfg: dict[int, tuple] = {}                            # instance address -> (site class index, latency table)
        self._inst_free: dict[tuple, list] = {}                          # ... -> addresses configured with it and not in use
        self._inst_of: dict[int, int] = {}                               # vid -> its instance address while its entry is live
        self.instances_live_max = 0; self.instance_pool_full = 0
        self.site_limit_refusals = 0; self.site_limit_reasons: dict[str, int] = {}; self.class_miss_refusals = 0
        self._dev = None                                                 # the device under observation (the trace's per-value state)
        # the builder
        self.queue: list[dict] = []                                      # FIFO of jobs {vid, gen, cls, age, site, cycles, E_rel, enqueued, kind}; policy is None until the builder's take composes it
        self.current: dict | None = None; self.current_left = 0.0
        self.staged: list[tuple] = []                                    # jobs finished this round, installed by the next pass
        self.build_log: list[dict] = []
        # per-round records
        self._memo: dict[tuple, str] = {}; self._round_seen = -1
        self.wait_why: dict[tuple, str] = {}                             # (vid, k) -> source of this round's WAIT (WAIT history)
        self.gen: dict[int, int] = {}                                    # vid -> generation (bumped by events and RESOLVE)
        self.builds = self.rebuilds = self.class_rebuilds = self.cancelled = self.discarded = self.provisional = self.expired = 0
        self.multi_boundary_rounds = 0                                   # rounds in which one entry was asked at two boundaries (must be 0)
        self._mismatch: set[int] = set(); self.profile_mismatches = 0     # values whose real window class exceeded the registered one
        self.actions_emitted_max = 0; self.actions_emitted_total = 0     # ACTIONs the pass emits (live entries at REST, WAITs included)
        self.decisions = self.waits = self.interior_waits = self.build_rounds_total = 0
        self.rounds_sampled = 0; self.max_live_entries = 0; self.max_lines = 0; self.entries_over_64 = 0; self.lines_over_256 = 0
        self.pending_max = 0; self.actions_per_round_max = 0; self.registrations_over_16 = 0; self.tick_overruns = 0
        self.occupancy_hist: dict[int, int] = {}

    # ------------------------------------------------------------------ inputs
    @staticmethod
    def omc_of(c: float) -> int:
        """1 - c in Q0.20, saturated: the omc a CONFIG (modeled client) or a REGISTER / RC (SWIPER client) carries."""
        return min((1 << F_OMC) - 1, int(round((1 - c) * (1 << F_OMC))))

    def _omc_value(self, v) -> int:
        return self.omc if self.client == "modeled" else self.omc_of(v.confidence)

    def _age_origin(self, dev, v) -> int:
        """The round the value's table is aged from: the producer's completion (modeled client), T_emit (SWIPER client)."""
        return v.t_emit if self.client == "swiper" else dev.schedule_instructions[v.producer_idx].end_round

    def _site(self, dev, v) -> SiteClass:
        """The contract as the engine's site class: F forward edges of 1..64 ticks, integer recovery prices, destination
        settlement, n_s in Q8.8 (hw/golden/solve.py)."""
        c = dev.contracts[v.vid]
        # Route A: durations are the atoms' exact physical lengths (1..64), prices the codebook's P3 for the value's site class
        # (>= the lowered script's P1 at its reserved placement; the exact P1 when no codebook is configured), n_s the number
        # of program-wire patches idling while the value waits (design Sec. 2.4)
        prices = v.site_prices if v.site_prices is not None else c.prices
        return SiteClass(F=c.F, d=list(c.durations), r=[int(round(x)) for x in prices],
                         settle_dst=[True] * c.F, ns_q=int(round(c.n_s * 256)))

    def _ns_lanes(self, dev, v) -> list:
        """n(p): the waiting cost per lane k = 0..F in Q8.8, the program wires of the value's support at boundary k --
        the producer's wire at k = 0 (the contract's n_s), the support after step k - 1 for k >= 1 (the fence lane F
        included) -- the patches the fence set holds idle while the value waits there."""
        c = dev.contracts[v.vid]
        prog = lambda L: sum(1 for w in L if dev.es.roles_by_wire.get(w) == "program") or 1
        return [int(round(c.n_s * 256))] + [int(round(prog(st.L) * 256)) for st in c.steps]

    def _class_table(self, cls: int):
        """The client's configured latency table for a volume class: the composed law of eq. (2) -- the decoder's accepted
        branch (collect + path + decode) and its rejected branch (a fresh decode) with c_up -- over delays since the producer
        completed, quantised to Q1.24 with T = 1024 columns.  Mass beyond the table is clipped into its last column (counted)."""
        if cls not in self._tables and self.client in ("swiper", "strip"):
            # the strip client's one table: the trained law of the decode stamp - the producer's completion over every
            # value, plus the path, in rounds from the producer's completion (the modeled client's origin); no composition
            # either: the full decoder's verification time, backlog included, is what the training run measured
            # the SWIPER client's one table: the trained law of T_verified + path - T_emit over the values that publish
            # (gap >= 1), in rounds from T_emit; no composition (the decode it waits for is the ancestors' verification)
            tot = sum(self.gap_pmf.values()); pmf = {}
            for g, w in self.gap_pmf.items():
                tt = min(int(g) + self.path, T_LEN - 2)
                if tt != int(g) + self.path: self.table_clipped += w / tot
                pmf[tt] = pmf.get(tt, 0.0) + w / tot
            self._tables[cls] = gtables.from_pmf(pmf, T_LEN)
        if cls not in self._tables:
            dec = {t + self.path: w for t, w in self.table.pmf_rounds(cls).items()}
            up = {self.collect + t: w for t, w in dec.items()}
            law = composer.compose(0, up, self.c_up, dec, 10 ** 6).p
            pmf = {}
            for t, w in law.items():
                tt = min(int(t), T_LEN - 2)
                if tt != int(t): self.table_clipped += w
                pmf[tt] = pmf.get(tt, 0.0) + w
            self._tables[cls] = gtables.from_pmf(pmf, T_LEN)
        return self._tables[cls]

    def _class_now(self, v):
        """Class of the largest window covering the producer (None while no window exists)."""
        dec = LINK.get("decoder")
        if dec is None:
            return None
        vols = []
        for t in dec._instruction_tasks.get(v.producer_idx, ()):
            task = dec._get_task_or_none(t)
            if task is not None: vols.append(task.window.total_spacetime_volume())
        return self.table.volume_class(max(vols)) if vols else None

    def _solve(self, j: dict) -> FixedPolicy:
        """One build as the RTL's take does it (build_job, hw/golden/engine.py:646-666): the class table conditioned by the
        slot's frozen self_age (j["age"], form_key's min(age_sat(s.e_b, self_age_origin), support), engine.py:642), then
        the fixed-point backward sweep over E_job = j["E_rel"] columns (the slot's frozen e_job).  The table is indexed
        from the enqueue round j["enqueued"] (the installed entry's e_b is the slot's, engine.py:529)."""
        tbl = self._class_table(j["cls"])
        comp = compose_direct(tbl, min(j["age"], tbl.support), j["E_rel"])
        site = j["site"]
        assert self.policy_mode != "threshold", "the threshold is table-free: it never builds"
        if self.policy_mode == "greedy":
            solved = solve_greedy(site, j["omc"], comp.S_q(), comp.Om_q())
        elif self.ns_mode == "boundary" or not self.saturate:
            solved = solve_variant(site, j["omc"], comp.S_q(), comp.Om_q(),
                                   ns_lanes=(j["ns_lanes"] if self.ns_mode == "boundary" else None), saturate=self.saturate)
        else:
            solved = solve_fixed(site, j["omc"], comp.S_q(), comp.Om_q())
        for f in list(comp.flags) + list(solved.flags): self.solver_flags[f] = self.solver_flags.get(f, 0) + 1
        if comp.exhausted: self.exhausted_builds += 1
        return FixedPolicy(j["enqueued"], solved, j["E_rel"], site.F, site.d, mode=self.policy_mode)

    # ------------------------------------------------------------------ the builder
    def _volume_class(self, v) -> tuple:
        """The latency table REGISTER names for this value: the class of its real windows when they exist, else the
        compile-time class of this producer from the training profile, else the table's default (provisional)."""
        if self.client in ("swiper", "strip"):
            return 0, False                                              # the one table the SWIPER and strip clients configure
        cls = self._class_now(v)
        if cls is not None:
            return cls, False
        cls = self.class_profile.get(v.producer_idx)
        return (cls, False) if cls is not None else (self.table.default_class, True)

    def _enqueue(self, dev, v, now: int, kind: str, cls: int, provisional: bool = False):
        """Freeze the job's slot fields as the engine's jq_enq does (hw/golden/engine.py:283-296: s.e_b = this pass's
        label, s.e_job = the remaining horizon) and put the job in the builder's FIFO; the compose + solve runs when the
        builder takes the job (_compose_job), as the engine composes at the take (build_job), not at enqueue.  The DIRECT
        self_age the take will use is the value's age at THIS pass plus the one tick the pass lags the REGISTER's ingress
        (age_sat(s.e_b, self_age_origin), engine.py:642 with the origin of engine.py:504-511; ENGINE_TICK_OFFSET = 2,
        froq/test_route_a.py:391): now - producer_end + 1.  An event sets the entry's policy to NONE (RTL: gen += 1, WAIT
        until the replacement installs): a queued job of the entry is replaced, a running one finishes but its result is
        discarded (its generation is stale), the installed table is dropped."""
        self.gen[v.vid] = self.gen.get(v.vid, 0) + 1
        self._cancel(v.vid)                                              # a queued job of this entry is replaced
        if kind != "register":
            self._installed.pop(v.vid, None)                             # policy := NONE until the rebuild installs
        registered = v.vid in self.E_abs
        if not registered:                                              # REGISTER: the configured horizon (RTL: jq_e_job = e_cfg)
            self.E_abs[v.vid] = now + self.horizon
        E_rel = self.E_abs[v.vid] - now
        if registered and E_rel <= 0:                                   # horizon exhausted: no job, the entry WAITs
            self.expired += 1; return
        if provisional:
            self.provisional += 1
        self.class_of_value[v.vid] = cls
        age = now - self._age_origin(dev, v) + 1                        # the slot's self_age (see the docstring above)
        cycles = self.build_base + 2 * E_rel
        self.queue.append(dict(vid=v.vid, gen=self.gen[v.vid], policy=None, cls=cls, provisional=provisional,
                               cycles=cycles, E_rel=E_rel, enqueued=now, kind=kind, age=age, site=self._site(dev, v),
                               omc=self._omc_value(v),                    # the entry's omc, frozen into the slot like the rest
                               ns_lanes=(self._ns_lanes(dev, v) if self.ns_mode == "boundary" else None)))
        self.builds += 1
        if kind != "register": self.rebuilds += 1

    def _compose_job(self, j: dict):
        """Compose + solve at the builder's take of the job (the engine's build_job at ALLOC), from the slot fields frozen
        at enqueue: j["age"] / j["E_rel"] / j["site"] / j["cls"]."""
        j["policy"] = self._solve(j)
        if os.environ.get("E2E_TRACE_VID") and j["vid"] == int(os.environ["E2E_TRACE_VID"]):
            print("BUILD round %d vid %d kind %s cls %s E_job %d | acts next 30 at k=0: %s" % (j["enqueued"], j["vid"], j["kind"], j["cls"], j["E_rel"], "".join(j["policy"].act(r, 0) for r in range(j["enqueued"], j["enqueued"] + 30))), flush=True)

    def _cancel(self, vid: int):
        before = len(self.queue)
        self.queue = [j for j in self.queue if j["vid"] != vid]
        self.cancelled += before - len(self.queue)
        return self.queue

    def _advance_builder(self, now: int):
        """Spend this round's enabled builder cycles on the FIFO; a job finished this round is installed next round.
        Taking a job composes and solves it (_compose_job) -- the engine composes at the take, not at enqueue."""
        if self.controller_mhz is None:                                 # zero service: everything installs at once
            for j in self.queue:
                if j["policy"] is None: self._compose_job(j)
                self._install(j, now, now)
            self.queue = []; return
        for j, finished in self.staged:                                 # completed last round: install now, after this round's Phase A
            self._install(j, finished, now)
        self.staged = []
        budget = self.builder_cycles_per_round
        while budget > 0:
            if self.current is None:
                if not self.queue: break
                self.current = self.queue.pop(0); self.current_left = self.current["cycles"]; self.current["started"] = now
                self._compose_job(self.current)                         # the take: compose with the slot's frozen age / E_rel
            spent = min(budget, self.current_left); budget -= spent; self.current_left -= spent
            if self.current_left <= 0:
                self.staged.append((self.current, now)); self.current = None          # installs at the next round's pass

    def _install(self, j, finished: int, installed: int):
        """A finished job installs at `installed` unless its entry moved on (superseded by an event or resolved): then the
        completed work is discarded, as the RTL discards a stale generation's line."""
        vid = j["vid"]
        stale = j["gen"] != self.gen.get(vid, -1)
        self.build_log.append(dict(vid=vid, round=j["enqueued"], kind=j["kind"], E_rel=int(j["E_rel"]), cycles=j["cycles"],
                                   queue_wait_rounds=j.get("started", finished) - j["enqueued"], finished=finished, install_round=installed, discarded=stale))
        if stale:
            self.discarded += 1; return
        self.build_rounds_total += installed - j["enqueued"]
        self._installed[vid] = dict(policy=j["policy"], cls=j["cls"], provisional=j["provisional"], installed=installed)

    # ------------------------------------------------------------------ engine messages from the device
    def _engine_visible(self, dev, v) -> bool:
        """The engine can hold an entry for this value: its contract has a site (a zero-only contract, F = 0, is never
        registered, solved, installed or counted as a live entry / policy line -- finding 5) and its site class is one the
        codebook configured (a class miss used to REGISTER all the same and the policy then built an unconfigured class --
        round-2 finding A1).  Either way the value simply WAITs for its definitive value (design Sec. 2.4)."""
        c = dev.contracts.get(v.vid)
        return c is not None and c.has_site and not v.class_miss

    def _site_limits(self, sc, cls: int) -> str | None:
        """Why the client could NOT configure this site class and latency table in the RTL (hw/rtl/se_pkg.sv), or None:
        F edges of 1..64 ticks, prices and n_s (Q8.8) inside their fields, 64 site classes and 64 latency tables."""
        if not 1 <= int(sc.F) <= self.F_MAX:
            return "F_over_F_MAX_%d" % self.F_MAX
        if any(not 1 <= int(x) <= D_MAX for x in sc.d):
            return "duration_outside_1_%d" % D_MAX
        if any(not 0 <= int(x) < (1 << PRICE_W) for x in sc.r):
            return "price_over_%d_bit" % PRICE_W
        if not 0 <= int(sc.ns_q) < (1 << NS_W):
            return "n_s_over_q8_8"
        if not 0 <= int(cls) < N_TABLES:
            return "latency_table_over_%d" % N_TABLES
        if site_key(sc) not in self.site_class_idx and len(self.site_class_idx) >= self.n_sites:
            return "site_classes_over_%d" % self.n_sites
        return None

    def _allocate_instance(self, vid: int, sc, cls: int) -> int | None:
        """The site-instance address REGISTER names: a free address already configured with this (site class, latency
        table), else the next unconfigured one of the N_INST the table holds, else None -- never the value id, whose range
        is the 16-bit VID and would overflow the 10-bit INST_W address (finding A1)."""
        key = site_key(sc)
        idx = self.site_class_idx.get(key)
        tup = (len(self.site_class_idx) if idx is None else idx, int(cls))
        free = self._inst_free.get(tup) or []
        if free:
            addr = free.pop(0)
        elif len(self._inst_cfg) < N_INST:
            addr = len(self._inst_cfg); self._inst_cfg[addr] = tup
        else:
            return None
        if idx is None:
            self.site_class_idx[key] = tup[0]                            # configured now that an instance names it
        self._inst_of[vid] = addr
        self.instances_live_max = max(self.instances_live_max, len(self._inst_of))
        return addr

    def _free_instance(self, vid: int) -> None:
        """RESOLVE / RECOVERY_DONE: the entry is gone, so its instance address returns to the pool of addresses carrying
        its configuration and the next value with that (site class, table) reuses it."""
        addr = self._inst_of.pop(vid, None)
        if addr is not None:
            self._inst_free.setdefault(self._inst_cfg[addr], []).append(addr)

    def register(self, dev, v) -> str | None:
        """REGISTER: the prediction is published, an instance address is allocated and the first solve is enqueued.
        Returns None when the engine took the value, else why it did not -- "no_site" / "class_miss" (the value WAITs for
        its definitive result), "site_limits" (the RTL cannot hold this site class: also a WAIT) or "instance_pool_full"
        (all N_INST addresses are live: nothing is published and the client retries next round)."""
        c = dev.contracts.get(v.vid)
        if c is None or not c.has_site:
            return "no_site"
        if v.class_miss:
            self.class_miss_refusals += 1
            return "class_miss"
        sc = self._site(dev, v)
        cls, provisional = self._volume_class(v)
        why = self._site_limits(sc, cls)
        if why is not None:
            self.site_limit_refusals += 1
            self.site_limit_reasons[why] = self.site_limit_reasons.get(why, 0) + 1
            return "site_limits"
        addr = self._allocate_instance(v.vid, sc, cls)
        if addr is None:
            self.instance_pool_full += 1
            return "instance_pool_full"
        if self.policy_mode == "threshold":
            self._register_table_free(dev, v, cls, provisional)
        else:
            self._enqueue(dev, v, dev.current_round, "register", cls, provisional)
        self._registered[v.vid] = dict(inst=addr, cls=self.class_of_value.get(v.vid),
                                       selfage=dev.current_round - self._age_origin(dev, v), omc=self._omc_value(v),
                                       site=dict(F=int(sc.F), d=[int(x) for x in sc.d], r=[int(x) for x in sc.r],
                                                 settle_dst=[bool(x) for x in sc.settle_dst], ns_q=int(sc.ns_q)))
        return None

    def _register_table_free(self, dev, v, cls: int, provisional: bool):
        """REGISTER in the table-free threshold mode (v9 ab76147's `_register_table_free`): no job and no policy line; the
        entry stores the value's advertised confidence and decides from REGISTER + install_latency on."""
        if provisional:
            self.provisional += 1
        self.class_of_value[v.vid] = cls
        self._tf[v.vid] = [dev.current_round + self.install_latency, float(v.confidence)]
        self._tf_kind[v.vid] = "install"
        self.tf_installs += 1

    def _decide_table_free(self, v, now: int) -> tuple:
        """The table-free ACTION and its WAIT source: BUILDING until the stored confidence is installed, then FORWARD iff
        it is >= theta (at every boundary: the device asks only inside the contract, and no table can run out)."""
        t0, c = self._tf.get(v.vid, (None, None))
        if t0 is None:
            return WAIT, "no_table"                                        # not registered (the device never asks)
        if now < t0:
            return "BUILDING", "no_table"
        self.decisions += 1
        if c >= self.theta:
            self.tf_forward += 1
            return FORWARD, "threshold"
        self.tf_wait += 1
        return WAIT, "threshold"

    def reconfidence(self, dev, v) -> bool:
        """RECONFIDENCE (the engine's RC message, hw/golden/engine.py): the SWIPER client learned that one of the value's own
        windows was reset after T_emit, so the entry's omc becomes 1 - v.confidence and its table is rebuilt (an event job:
        policy NONE -- WAIT -- until the replacement installs, E_rel = the horizon left).  Only a live entry of a registered
        value takes it; returns whether the engine took it."""
        if v.vid not in self._inst_of or not self._entry_live(dev, v) or v.vid not in self.class_of_value:
            return False
        if self.policy_mode == "threshold":                               # table-free: the stored confidence is replaced and
            self._tf[v.vid] = [dev.current_round + self.rc_latency, float(v.confidence)]   # installs like REGISTER's, by its
            self._tf_kind[v.vid] = "rc"                                                    # own deadline (rule 41b')
            self.tf_installs += 1
        else:
            self._enqueue(dev, v, dev.current_round, "reconfidence", self.class_of_value[v.vid])
        self.reconfidences += 1; self._rc_round.append(v.vid)
        self._rc_log.setdefault(v.vid, []).append((dev.current_round, self._omc_value(v)))
        return True

    def resolve(self, v):
        """RESOLVE: the definitive value arrived; the entry's queued job is cancelled and its table released.  A job already in
        the builder runs to completion (the RTL cannot stop a started line) and its result is discarded."""
        self.gen[v.vid] = self.gen.get(v.vid, 0) + 1
        tf = self._tf.pop(v.vid, None)
        kind = self._tf_kind.pop(v.vid, None)
        if tf is not None and v.delivered_round < tf[0]:                  # (rule 41b': resolved before the decision's deadline)
            self.tf_missed[kind] += 1
        self._cancel(v.vid); self._installed.pop(v.vid, None)

    def _entry_live(self, dev, v) -> bool:
        """The engine holds an entry for this value: it REGISTERed (so it holds an instance address) and has not RESOLVEd
        -- or it resolved into a recovery and stays live until RECOVERY_DONE."""
        return v.vid in self._inst_of and (not v.delivered or v.state is VState.RECOVERING)

    def live_entries(self, dev) -> int:
        n = sum(1 for vid in self._inst_of if self._entry_live(dev, dev.values[vid]))   # _entry_live needs vid in _inst_of: the same set
        if dev._scan_check:
            assert n == sum(1 for v in dev.values.values() if self._entry_live(dev, v))
        return n

    # ------------------------------------------------------------------ every round
    def observe(self, dev):
        """Once per round, before dispatch: the builder advances, events replace jobs, and the occupancy record is taken."""
        self._dev = dev
        now = dev.current_round
        if now == self._round_seen:
            return
        self._round_seen = now; self._memo = {}; self.wait_why = {}
        events, self._rc_round = len(self._rc_round), []                  # RC messages this round (Phase A, like the rest)
        for vid in [vid for vid in self._inst_of if not self._entry_live(dev, dev.values[vid])]:
            self._free_instance(vid)                                      # RESOLVE / RECOVERY_DONE happened: the address is reusable
        emitted = 0
        for v in dev._scan(dev._predicted_undelivered, lambda v: v.predicted_round >= 0 and not v.delivered):   # the same values, in vid order
            # the registered table is immutable (RTL: self_table is fixed at REGISTER; no runtime message replaces it).  A real
            # window class larger than the registered one is recorded as a profile mismatch, nothing is rebuilt.  A reset decode
            # attempt is not an event either: the stored table is indexed by absolute round and its composed law already carries
            # the rejected branch, so ageing the same table is what the RTL does (plan Sec. 3.2 / 8).
            real = self._class_now(v) if self.client == "modeled" else None   # the SWIPER client's table is not a volume class
            if real is not None and real > self.class_of_value.get(v.vid, 0) and v.vid not in self._mismatch:
                self._mismatch.add(v.vid); self.profile_mismatches += 1
            # the pass emits one ACTION for every live entry that is not in flight (REST), WAIT outputs included; the engine's
            # notion of in flight is its completion timer, ACCEPT + encoded duration (not the sequencer's instruction stamps).
            # A value the engine REFUSED (class miss, site limits, a full instance pool) holds no entry: it emits nothing
            # (round-4 finding 3 -- `has_site` alone counted seven ACTIONs for an empty codebook with zero registrations)
            c = dev.contracts.get(v.vid)
            if c is not None and c.has_site and self._entry_live(dev, v):
                k = v.admitted
                in_flight = k > 0 and now < v.accept_round.get(k - 1, now) + c.steps[k - 1].duration
                if not in_flight: emitted += 1
        self.actions_emitted_max = max(self.actions_emitted_max, emitted); self.actions_emitted_total += emitted
        self._advance_builder(now)                                        # the builder runs after Phase A's messages
        # records (taken after this round's REGISTERs, RESOLVEs, recoveries and events)
        entries = self.live_entries(dev)
        lines = sum(1 for vid in self._installed if not dev.values[vid].delivered) + (1 if self.current is not None else 0) + len(self.staged)
        pending = len(self.queue) + (1 if self.current is not None else 0) + len(self.staged)
        # Phase-A messages charged to the tick: REGISTER / RESOLVE / RECOVERY_DONE of engine-visible entries only (a
        # no-site value never registers, so its delivery and retirement send the engine nothing -- finding 5)
        # the device lists the values it stamped this round (predicted / delivered / recovery done): the same predicates over
        # those lists give the same counts as a pass over every value, without the pass (profile)
        ev = {k: {v.vid: v for v in vs}.values() for k, vs in dev._round_events.items()}
        registered = sum(1 for v in ev["predicted"] if v.predicted_round == now and self._engine_visible(dev, v))
        resolved = sum(1 for v in ev["delivered"] if v.delivered_round == now and self._engine_visible(dev, v))
        recovered = sum(1 for v in ev["recovered"] if v.recovery and v.state is VState.RETIRED and v.recovery.get("done_round") == now
                        and self._engine_visible(dev, v))
        if dev._scan_check:
            assert registered == sum(1 for v in dev.values.values() if v.predicted_round == now and self._engine_visible(dev, v))
            assert resolved == sum(1 for v in dev.values.values() if v.delivered_round == now and self._engine_visible(dev, v))
            assert recovered == sum(1 for v in dev.values.values() if v.recovery and v.state is VState.RETIRED and v.recovery.get("done_round") == now
                                    and self._engine_visible(dev, v))
        messages = registered + resolved + events + recovered
        self.rounds_sampled += 1; self.max_live_entries = max(self.max_live_entries, entries); self.max_lines = max(self.max_lines, lines)
        self.entries_over_64 += entries > N_ENTRIES; self.lines_over_256 += lines > L_LINES; self.pending_max = max(self.pending_max, pending)
        self.registrations_over_16 += registered > 16
        if self.cycles_per_round and MSG_CYCLES * messages + PASS_CYCLES > self.cycles_per_round: self.tick_overruns += 1
        self.occupancy_hist[entries] = self.occupancy_hist.get(entries, 0) + 1
        self.lines_hist[lines] = self.lines_hist.get(lines, 0) + 1

    # ------------------------------------------------------------------ decisions
    def action(self, dev, v, k: int) -> str:
        """The entry's ACTION at boundary k this round: FORWARD / WAIT, or BUILDING while no table is installed."""
        now = dev.current_round
        key = (v.vid, k)
        if key in self._memo:
            return self._memo[key]
        if any(vid == v.vid and kk != k for vid, kk in self._memo):
            self.multi_boundary_rounds += 1                                # the entry has ONE current boundary per tick
        ver = pending = None
        if self.policy_mode == "threshold":
            out, why = self._decide_table_free(v, now)
        else:
            ver = self._installed.get(v.vid)
            pending = (self.current is not None and self.current["vid"] == v.vid and self.current["gen"] == self.gen.get(v.vid)) \
                      or any(j["vid"] == v.vid for j in self.queue) \
                      or any(j["vid"] == v.vid and j["gen"] == self.gen.get(v.vid) for j, _ in self.staged)   # finished, installs next pass
            if ver is None or ver["installed"] > now:                      # no table yet, or installed by a later pass
                out = "BUILDING" if (pending or ver is not None) else WAIT
                why = "no_table"
            else:
                self.decisions += 1
                a, why = ver["policy"].act_why(now, k)
                out = FORWARD if a == "F" else WAIT                       # "W", and an exhausted table "-", are WAIT
        if out == WAIT:
            self.waits += 1
            if k > 0: self.interior_waits += 1
            self.wait_why[key] = why
        if os.environ.get("E2E_TRACE_VID") and v.vid == int(os.environ["E2E_TRACE_VID"]):
            print("ACTION round %d vid %d k %d -> %s (ver %s, installed %s, pending %s, queue %d, current %s, E_abs %s)"
                  % (now, v.vid, k, out, ver is not None, ver and ver["installed"], pending, len(self.queue), self.current and self.current["vid"], self.E_abs.get(v.vid)), flush=True)
        self._memo[key] = out
        self.actions_per_round_max = max(self.actions_per_round_max, len({vid for vid, _ in self._memo}))
        return out

    def summary(self):
        classes = {}
        for c in self.class_of_value.values(): classes[c] = classes.get(c, 0) + 1
        inst = [b for b in self.build_log if not b["discarded"]]
        waits = [b["queue_wait_rounds"] for b in self.build_log]; delays = [b["install_round"] - b["round"] for b in inst]
        kinds = {}
        for b in self.build_log: kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
        hw = dict(rounds_sampled=self.rounds_sampled, max_live_entries=self.max_live_entries, max_policy_lines=self.max_lines,
                  entries_over_64_rounds=self.entries_over_64, lines_over_256_rounds=self.lines_over_256, pending_jobs_max=self.pending_max,
                  actions_emitted_per_round_max=self.actions_emitted_max, actions_emitted_total=self.actions_emitted_total,
                  admission_queries_per_round_max=self.actions_per_round_max, profile_mismatches=self.profile_mismatches,
                  registrations_over_16_rounds=self.registrations_over_16,
                  tick_overrun_rounds=self.tick_overruns, builder_cycles_per_round=self.builder_cycles_per_round,
                  site_classes_configured=len(self.site_class_idx), instances_configured=len(self._inst_cfg),
                  instances_live_max=self.instances_live_max, instance_pool_full=self.instance_pool_full,
                  class_miss_refusals=self.class_miss_refusals, site_limit_refusals=self.site_limit_refusals,
                  site_limit_reasons=dict(self.site_limit_reasons),
                  builds_finished=len(self.build_log), builds_installed=len(inst), builds_discarded=self.discarded, builds_by_kind=kinds, jobs_cancelled=self.cancelled, jobs_expired=self.expired,
                  multi_boundary_rounds=self.multi_boundary_rounds,
                  queue_wait_rounds=dict(max=max(waits, default=0), mean=(sum(waits) / len(waits)) if waits else 0, nonzero=sum(1 for w in waits if w)),
                  install_delay_rounds=dict(max=max(delays, default=0), mean=(sum(delays) / len(delays)) if delays else 0),
                  E_rel=dict(max=max((b["E_rel"] for b in self.build_log), default=0), mean=(sum(b["E_rel"] for b in self.build_log) / len(self.build_log)) if self.build_log else 0),
                  occupancy_hist={str(k): n for k, n in sorted(self.occupancy_hist.items())},
                  lines_hist={str(k): n for k, n in sorted(self.lines_hist.items())}, build_log=self.build_log[:20000],
                  build_log_full=self.build_log)                     # untruncated (build_log above keeps the 20000-entry cap)
        # the engine-facing trace of the run (froq.test_route_a.controller_trace / replay_through_golden): per registered value
        # its REGISTER round, the site-INSTANCE ADDRESS `inst` that REGISTER named (0..N_INST-1, allocated and reused from
        # the pool; a replay must configure and register this address, never the vid -- INST_W is 10 bits), the volume
        # class, site class, self age, RESOLVE round and verdict, RECOVERY_DONE round and ACCEPT rounds
        values = []
        dev = self._dev
        if dev is not None:
            for v in dev.values.values():
                if v.predicted_round < 0 or v.vid not in self._registered:
                    continue
                r = self._registered[v.vid]
                values.append(dict(vid=v.vid, inst=r["inst"], register_round=v.predicted_round, cls=r["cls"], selfage=r["selfage"], site=r["site"],
                                   **(dict(omc=r["omc"], rc=[list(x) for x in self._rc_log.get(v.vid, [])]) if self.client in ("swiper", "strip") else {}),
                                   resolve_round=v.delivered_round, verdict=int(v.prediction == v.truth),
                                   recovery_done_round=(v.recovery or {}).get("done_round"),
                                   accepts={int(k): r_ for k, r_ in v.accept_round.items()}))
        trace = dict(cycles_per_round=int(self.cycles_per_round), horizon=self.horizon, omc=self.omc,
                     num_rounds=(dev.current_round if dev is not None else -1), values=values,
                     builds=[dict(b) for b in self.build_log])
        return dict(K=self.K, c=self.c, c_up=self.c_up, collect=self.collect, path=self.path, controller_mhz=self.controller_mhz, horizon=self.horizon, policy_mode=self.policy_mode,
                    theta=self.theta, ns_mode=self.ns_mode, saturate=self.saturate, n_sites=self.n_sites,
                    **(dict(threshold=dict(install_latency=self.install_latency, installs=self.tf_installs, forward_decisions=self.tf_forward,
                                           wait_decisions=self.tf_wait),
                            threshold_deadlines=dict(install_latency=self.install_latency, rc_latency=self.rc_latency,
                                                     missed_install=self.tf_missed["install"], missed_rc=self.tf_missed["rc"]))
                       if self.policy_mode == "threshold" else {}),
                    **(dict(client=self.client, reconfidences=self.reconfidences, gap_pmf={str(k): w for k, w in sorted(self.gap_pmf.items())})
                       if self.client in ("swiper", "strip") else {}),       # the SWIPER / strip client's record; the modeled client's summary is unchanged
                    solver=("table-free: c >= theta" if self.policy_mode == "threshold" else "hw/golden fixed-point (compose_direct + solve)"),
                    omc_q20=self.omc, table_mass_clipped=self.table_clipped,
                    solver_flags=self.solver_flags, exhausted_builds=self.exhausted_builds, volume_out_of_range=self.table.volume_out_of_range,
                    table=self.table.describe(), volume_classes=classes, provisional_builds=self.provisional, class_rebuilds=self.class_rebuilds,
                    hardware=hw, builds=self.builds, rebuilds=self.rebuilds, build_rounds_total=self.build_rounds_total,
                    decisions=self.decisions, waits=self.waits, interior_waits=self.interior_waits, controller_trace=trace)


def _selftest_horizon_guard() -> None:
    """Unit test for finding 12 with the reviewer's numbers (run: python -m froq.policy): a stale or
    supplied FORWARD at j = 9 with d_k = 3 and E_job = 10 must read WAIT; j + d_k == E_job (j = 7) remains FORWARD."""
    class _Solved:
        A = [[True]] * 16                        # a supplied all-FORWARD table over 16 columns, 1 boundary
        flags = ()

    p = FixedPolicy(0, _Solved(), 10, 1, [3])
    assert p.act(9, 0) == "W" and p.act(8, 0) == "W"      # 9 + 3 > 10 and 8 + 3 > 10: beyond the horizon
    assert p.act(7, 0) == "F"                             # 7 + 3 == 10: equality allowed
    assert p.act(6, 0) == "F" and p.act(10, 0) == "W"     # inside / past the table
    assert p.act(0, 1) == "W"                             # no such boundary
    q = FixedPolicy(5, _Solved(), 10, 1, [3])
    assert q.act(4, 0) == "W"                             # before the build round: WAIT
    print("policy horizon-guard self-test PASS: j=9 d_k=3 E=10 -> W, j=7 (equality) -> F")


if __name__ == "__main__":
    _selftest_horizon_guard()
