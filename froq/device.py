"""A DeviceManager that lets the sequencer act on a predicted T-gadget result under a certified recovery contract
 (Route A, design Sec. 2 - 5; replaces the coordinate-based adapter of to 09-13).

Configurations (no A, no forced-WAIT / always-forward runs):
  N never acts on a prediction: the conditional S waits for the DELIVERED definitive result (native semantics + path);
  Q Froq: the engine's stored policy answers FORWARD / WAIT at the value's current boundary; a FORWARD is an ACCEPT.
Both run the same instruction-granular scheduler, the same event stream, the same delivery queue (a decoded result is
visible `path` rounds after its decode completes; N and Q alike) and the same causal startup estimate (upstream plans
startup instructions as late as possible; the plan uses the delivered truth where known, the prediction where one exists,
else the static per-site mean -- never the oracle coin), so the two differ only by what Q admits early.

Values are the ZZ-parity results of the T gadgets found in the event stream (`EventStream.t_gadgets`); the device asserts
that each coincides with one of SWIPER's CONDITIONAL_S groups through `lss_map`. Truth and prediction are drawn from keyed
streams named by the producer's event id (identical in N and Q).

Admission (Sec. 2.4). A contract step is an ATOM; it is ACCEPTed as a whole when its external prerequisites hold (every
predecessor outside the atom complete, every decoded result it is conditioned on delivered -- except the value's own,
which the prediction stands in), its cells are free and not reserved by another value, the overlap invariant holds
against every other unresolved value (support, classical dependencies, footprint), the recovery workspace of the NEXT
boundary can be reserved (atomic transfer: reserve the new step's cells, then release the old), and the policy says
FORWARD. From ACCEPT the members run back-to-back under their own edges; the monitor asserts completion at
ACCEPT + encoded duration. Any instruction touching a cell reserved by another value is refused (`held_reserved`), ordinary
allocations included; that is what keeps the corridor free for a recovery that may come many rounds later.

Settlement (Sec. 2.2 / 2.3). A delivered contradiction waits for the in-flight atom to complete, then splices the LOWERED
recovery script of the current boundary (the instructions the builder placed and reserved); the REALISED intrinsic cost is
accumulated from the executed syndrome rounds attributed to the recovery's own instructions and compared with the encoded
P1 of the placed instance (`recovery_price_mismatch`; stalls and settlement delay stay in P2, never in the comparison),
and at completion the step's complete final frame (`Step.frame_after`) is applied as a recorded frame update in the ledger
(frames cost no physical work, and are never dropped). Holds
end only when the script has completed AND its results are decoded and delivered through the same queue; the stall behind
the in-flight atom and the settlement delay are the measured overhead P2, reported next to P1, never encoded.

C6 monitor (Sec. 3). Independently of the builder's fence map, the device keeps for every live value the wires its
admitted work has touched (the residual's light cone) and the result ids those admitted measurements produced; every
instruction that STARTS is checked against every live value: touching a tainted wire or consuming a tainted result
without being an admitted member (or the value's own recovery) is a violation (`c6_violations`, voids the run). Holds
the contract is built at the prediction against the work executed by then -- a contract whose cone work has
already started (a fence instruction, or a member of a later step inside that step's light cone) has no site: the state
has moved past every boundary it describes (`contract.refuse_executed_cone_work`). Program order on the wires of L is
the order of PHYSICAL events; a gadget's frames are bookkeeping of the byproduct its certified channel applies at the gadget
(`contract.atoms_by_wire`). An atom with physical work on a wire of L before that wire's frontier is before the region there:
its frame is stepped over, its physical work after the frontier ends the contract (`wire_has_straddler`); a wire whose
joining member has a physically straddling gadget never joins L (`wire_has_straddler`); an atom's physical work must be
contiguous on every wire of L it touches (`atom_interleaved_on_wire`). From the prediction, a registered, undelivered Q value holds its fence
instructions and the in-cone members of its not-yet-accepted steps (`_other_holds`, the LIVE branches) -- the same predicate
the ACCEPT-time check applies to members that already ran; members outside the light cone may run early. Cone work that
starts between the prediction and the first ACCEPT escaped a hold: a C6 violation. A registration the engine could not take
(instance pool full) drops its contract; the retry rebuilds against that round's executed work. Timers: per ACCEPT, completion == ACCEPT +
encoded duration (<= when the atom contains another value's conditional branch, whose skip idles the timer) while the
value is unresolved; CONFIRMED cancels the value's pending timers (RESOLVE retires the RTL entry, and a later chain may
then legitimately hold a trailing member of the in-flight atom); RECOVERING keeps them. `program_done_round` is stamped
in the round the last ORIGINAL instruction completes."""
from __future__ import annotations

import bisect, collections, hashlib, heapq, itertools, json, math, os, sys, time
from itertools import filterfalse
from dataclasses import dataclass, field
from fractions import Fraction
from types import SimpleNamespace

import networkx as nx
import numpy as np

from swiper.decoder_manager import DecoderManager
from swiper.device_manager import DeviceManager, InstructionTask
from swiper.window_manager import SlidingWindowManager
from swiper.lattice_surgery_schedule import Duration, Instruction

from .contract import Contract, ScheduleView, Semantics, ValueSpec, build_contract, fence_set, refuse_executed_cone_work, values_of
from .events import EventStream
from . import hdepth as _hd
from .r17rec import R17Records
from .diagrec import DiagRecords
from .r9check import ACCESS as R9A, R9, Event as R9Event, Stop as R9Stop, WAIT as R9_WAIT, CoverageError
from .wq import INF as WQ_INF, WQ
from .rng import KeyedRNG
from .values import Value, VState

FORWARD, WAIT = "F", "W"
PROGRESS_STOP_MIN_S = 1200   # the progress stop looks at a run's projection from this many seconds of wall time on
LINK: dict = {}      # filled by the runner: the DecoderManager instance of the current simulation


def script_kind_of(template, wire, wires):
    from .strip import script_kind
    return script_kind(template, wire, wires)


def lowered_program_wires(low, residual) -> list:
    """The program wires a lowered recovery acts: a UR list's members name wire indices into `low.wires`, and those
    it names are its program operands (a synthesized or peephole list can leave a wire of the list idle, which
    then has no cell in the instance; routed list names every one); otherwise the residual's sorted wires."""
    if not low.wires:
        return list(residual.axis.wires)
    return [low.wires[i] for i in sorted({i for m in low.script if m["template"] != "frame" for i in m["wires"]})]


class _WireSlice:
    """The strip client's view of a wire's gadgets before one merge: entries [lo, hi) of the wire's list, lo moved forward
    as the stream's anchor moves (program order on a wire is time order, so what ended by the anchor is a prefix)."""
    __slots__ = ("lst", "lo", "hi")

    def __init__(self, lst, lo, hi):
        self.lst, self.lo, self.hi = lst, lo, hi

    def __iter__(self):
        return iter(self.lst[self.lo:self.hi])

    def __len__(self):
        return self.hi - self.lo
ROLES = ("program", "resource", "mediator", "routing", "recovery", "unknown")
REC56_LIVE = (0, 1, 9, 17, 25, 33, 41, 49, 57, 65)          # (plan v2 5.6, records only), fixed bins' lower edges: live
REC56_CELLS = (0, 1, 65, 129, 193, 257, 321, 385, 449, 513)  # values a round ([0], [1, 8] .. [57, 64], past 64); reserved cells
REC56_ROUNDS = (0, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)   # a round ([0],
                                                             # [1, 64] .. [449, 512], past 512); rounds ([0], [1], [2, 3] ..)
R7_RESV = ("candidate", "exclusive", "reserved")          # round 7's reservation kinds (3(g) item 6; `_r7_resv_round`)
# arm D (v9-deferral): lazy Clifford-frame deferral of every T gadget's S correction
DEFER_K = 10 ** 6                    # no depth limit: a deferral walk ends at its first dependency
WALK_CAP = 16                        # rule 66: the walk cap (ADVANCE is not eligible at boundary 16) under the mode
HW_AGE = 4   # round 8's rule (a), A6: a reservation-manager event that has waited this many rounds is served first
SCRIPT_CLASSES = ("two_wire", "measurement_gadget", "commit_repair", "other")   # (records only): `_script_class`
# (plan v2 3.1, records only): a spliced script's starting event. early_commit: an early script spliced
# on a prediction (`_h_splice`); materialization: a deferred value's script at its decoded outcome; repair: a committed
# (rebased) frame's script, which undoes a contradicted commit; speculation: arm Q's misprediction script
RECOVERY_EVENTS = ("early_commit", "materialization", "repair", "speculation")
W2_ELL = 1                           # the decision: COMMIT's decision latency, the splice in the round after (spec 4.2)
DEFER_SETTLES = ("none", "decode")   # none: a materialization's own results are not waited for (as for the program's own S
                                     # gadgets); decode: holds end when they are decoded and delivered (Froq's repair rule)
# the hybrid mode H: the furthest early boundary, the rebased walk's depth, the
# install of a prediction published after the frame registered (one round, as Q-thr's table-free rule; B.10), and the
# decode-name offset of a repair script
H_JMAX, H_K, H_INSTALL, H_REPAIR_KEY = 16, 16, 1, 10 ** 6
# the guarantee study G (plan v2 A1.3, `---guard`): ch, the charged guard (deployed); zl, the same guard and checks
# with no service time charged (an idealized diagnostic); off, no progress guard and no coverage check (unsafe: failures
# recorded by kind, the run ends with a failure status); ser, at most one live frame at a time under the charged guard
GUARDS = ("ch", "zl", "off", "ser")
# the static reservation `resv` (`--resv`): static is SER-R, serialized speculation that holds
# the whole walk's recovery workspace and its queue entries from registration to the frame's end (one live frame; no
# progress check, bounded walk, window or certificate table); None, off (v16's runtime).  Its charges at the current
# values are model (`` section 1, 556d1abf): `reads`,
# the registration check's accesses, each at c1 + f after its c0 (the footprint row of its walk key and the cells' state:
# 2), with no update part; `test`, a dispatch test's port-1 cycles (0: the dispatcher's guarded-cell test, as v16's,
# with no check after it).  `--resv-cost` replaces them (for example r + 1 reads, a footprint built from r boundary rows)
# A boundary is usable only if the frame's own steps before it leave its workspace clear (`_resv_usable`, review of
# plan 2.2, finding 1): a property of the walk and the layout, kept in the same footprint row (its usable boundaries'
# workspaces), so the registration still reads `reads` words
RESVS = ("static", "incremental")      # (W-E, plan v2 7.2: "incremental" holds only the boundary a frame stands)
RESV_COST = dict(reads=2, test=0)
RESV_FLOOR_STATES = ("carry", "decide", "early_decide", "early_materialize")   # (where a frame may still take steps back, `_resv_floor`)
# U (part C; hardware contract `U-contract.md`): delta per unit of d in Q16.20 (delta =
# this x d, exact; "inf" is U off, "carry" the all-CARRY control: the unit's client off); act_lat, the decision latency
# in rounds from a request's round to its ACT taking effect (rule 41b: the RTL's worst case after the freshness fix,
# 1,846 cycles from ingress to decision with a congested pool, 6.45 rounds, rounded up; sensitivities 4 and 1); the
# engine's entries; the ETA register file's slots; the last boundary with a record;
# the stale limit in units of d; the EPOCH and WIN ingress depths; the replay records' format and lines per gzip member
U_DELTAS = {"0": 0, "d/4": 1 << 18, "d/2": 1 << 19, "inf": None, "carry": 0,
            "noact": 1 << 40}                                     # U v6 (rule 54): U6-noact's delta = inf, the client on
U4_H = ("0", "1", "inf")                                       # U v4's headroom factors (rule 44e's grid)
U_LATENCY = 7
U_ENTRIES, U_SLOTS, U_B_MAX = 64, 256, 16
U_STALE_D = 2
U_FIFO = 128
U_REPLAY_FORMAT, U_REPLAY_FLUSH = "scoutq-u-replay-2", 2000
U_LOG_MAX = 20000                       # rule 41f's per-installation and per-ACT records kept in the summary
U_ACT_FIELDS = ("vid", "epoch", "b", "decided", "effect", "cur", "stop", "splice", "done", "how", "cancel")
H_STATES = ("early_decide", "early_materialize", "confirm_wait")
# round 3 (gap analysis rules 29 and 30; sections 9-11): a run is censored once its
# execution passes CENSOR_FACTOR x R_N rounds (N's at B = 1.5P); the wait-for-cycle detector looks every R29_KNOT_EVERY
# rounds while a frame has waited that long for a materialization boundary
CENSOR_FACTOR, R29_KNOT_EVERY = 4, 256
H_LOG_FIELDS = ("h_trigger_round", "h_choice_round", "h_early_boundary", "h_splice_round", "h_early_done", "h_F_original",
                "h_early_P1", "h_early_stall_rounds", "h_rebased")
DEFER_RULES = ("lazy", "eager", "no_cost_increase")   # lazy: carry to the dependency, materialize at the cheapest boundary from
                                               # the current one on; eager: at the current boundary; no_cost_increase: lazy, but
                                               # while b is unknown the frame is not carried through a step that makes its
                                               # correction costlier (the earlier line's "costaware"; audit finding 2: an
                                               # implemented heuristic, not an optimal deferral policy)


class R8Fail(tuple):
    """`_r8_cert`'s answer when a permission fails: falsy, as False was, and naming the
    permission, (instruction, frame or None for an ended frame's member, cell)."""
    __slots__ = ()

    def __bool__(self):
        return False


@dataclass
class DeferFrame:
    """One deferred correction (arm D): the walk, and where the frame is in it."""
    vid: int
    c: Contract                      # build_contract(prediction=False, deferral=True)
    reg_round: int
    members: list                    # per step: its .lss instructions
    r: list                          # r(j), j = 0..F: the placed script's patch-rounds at boundary j (None: no placement)
    fence: set                       # contract.fence_set (less resources outside the cone): held until the frame finishes
    kof: dict = field(default_factory=dict)       # member -> its step
    state: str = "carry"             # carry -> decide -> materialize -> spliced -> done
    admitted: int = 0                # steps admitted so far (in order; a step is admitted once its outside prerequisites ran)
    limit: int = -1                  # steps that may be admitted: top (lazy, eager), stop (no_cost_increase) while b is unknown, j* after
    unadm: set = field(default_factory=set)       # the unstarted members of the steps not yet admitted: held
    feas: list = field(default_factory=list)      # boundary j can be materialized at: a placed script whose workspace no
                                                  # patch occupies that waits for the frame's own held work
    top: int = -1                    # the furthest feasible boundary the frame can be carried to (F unless cut short)
    top_why: str = ""                # dependency | tangle (a step waits for the frame's own fence or a later step) | tangle_search_cap
                                     # (the bounded search stopped: not a proven tangle) | infeasible | shortened (cut by a later frame)
    top_why_initial: str = ""        # top_why at registration (a later frame may shorten it: audit finding 4 counts the FINAL reason)
    p_verdict: int = -1              # the boundary the frame had reached (admitted steps) when b was delivered
    p_run: int = -1                  # ... when the materialization boundary was chosen
    first_ok: int = -1               # the first boundary from p_run on it could be materialized at, then (eager's choice)
    target: int = -1                 # j*, the boundary the correction is materialized at
    verdict_round: int = -1
    stop: int = -1                   # rule no_cost_increase: the first feasible boundary past which r would rise (top: none)
    guard: set = field(default_factory=set)      # the union of every boundary's workspace (the guard's cells)
    target_ws: frozenset = frozenset()
    locked: frozenset = frozenset()  # the cells the spliced script locks while it runs
    taint_wires: set = field(default_factory=set)
    taint_results: set = field(default_factory=set)
    decide_waits: int = 0
    ws_waits: int = 0
    admit_waits: int = 0
    how: str = ""
    done_round: int = -1
    # the hybrid mode H; unused in arm D
    rebased: bool = False            # the early script is spliced: the frame carries the rebased contract, the verdict's sense inverted
    h_trigger_round: int = -1        # the first round the trigger held
    h_choice_round: int = -1         # the round the early boundary was (last) chosen
    h_je: int = -1                   # the early boundary, on the original contract
    h_frozen: int = -1               # the admitted steps when the trigger froze admission (a retarget re-admits up to them)
    h_next: object = None            # the rebased contract prepared for h_je and the holds it added (choice -> splice or cancel)
    h_bad: set = field(default_factory=set)       # early boundaries whose rebased contract cannot be built (permanent reasons)
    h_splice_round: int = -1
    h_early_rec: dict | None = None  # the early script's record (v.recovery at the splice)
    h_early_done: int = -1           # the round the early script completed and its frame was applied (-1: not yet)
    h_early_locked: frozenset = frozenset()       # the early script's cells, locked until it completes
    h_prefix: list = field(default_factory=list)  # (instrs, events) of the original steps before h_je: released when the frame ends
    h_F0: int = -1                   # the original contract's F
    h_log: list = field(default_factory=list)     # (round, event, detail): the frame's H transitions
    # U; unused otherwise
    u_pending: list = field(default_factory=list) # the ACTs decided for the frame that have not taken effect yet
    u_act: dict | None = None        # the ACT executed (epoch, b, stop, round), kept after its splice; None again if cancelled
    rule: str = ""                   # its carry rule (U v4, rule 44a: chosen at registration; else the arm's)
    u_lazy: int | None = None        # U v4: its lazy reservation, from registration until it materializes or drops
    u6_pred: tuple | None = None     # U v6 (rule 50): the registered prediction (m_hat, c) for p_corr; None: none installed
    u6_noreco: bool = False          # U v6 (rule 52): a stale u_xs vetoed its decision, so it is never reconsidered
    u6_reco: bool = False            # U v6 (rule 50): its one reconsideration has been taken
    # the decision (froq/wager2.py) and rule 66's hardware-limit mode (froq/hwlimits.py); unused otherwise
    w2: dict | None = None           # the decision: the frame's slot, half, base lane, masks and its last decision
    hw_wait: str | None = None       # the mode: the reservation-manager event the frame waits for (its effect pending)
    hw_ok: str | None = None         # the mode: an event that completed, for the step it gates (splice, commit)
    hw_guard_on: bool = True         # the mode: the frame's guard is counted (its registration event completed)
    # round 8's progress rule (a); unused otherwise
    r8_ref: int = -1                 # the last round the guard or the rule refused one of its FORWARDs (counted, not a what-if)
    r8_b1: int = 0                   # the boundary its registration certified: 0 (or the first block's end when 0 had no script)
    r8_cap: int | None = None        # settled under a refusal: it decides among its certified boundaries, up to this one
    r8_wit: int = 0                  # its witness, the boundary of its last certificate (its admitted one), reachable at once
    r8_mat_waits: int = 0            # (records only, and rule 29's knot): rounds in materialize at its witness, the splice waiting on a patch
    r8_pend: frozenset = frozenset()       # the mode: its candidate cells, guarded from the registration request on
    r8_pend_reb: frozenset = frozenset()   # its revised contract's candidate cells, guarded from the commit to the rebase
    r8_fwd: tuple = (-1, 0, 0)             # (records only): (round, a, b) of the last FORWARD rule (a) refused
    r8_last: tuple | None = None           # (records only): the decision's last lookup before the verdict (`_r8_note_decision`)
    resv_fp: frozenset = frozenset()       # (SER-R): its footprint, the cells it holds from registration to its end
    resv_t0: int = -1                      # (SER-R, records): the round its hold began
    resv_unclear: tuple = ()               # (SER-R, records): the boundaries up to its top its own steps may leave unclear
    resv_sb: int = -1                      # (SER-R, records): the last round its own hold kept a prerequisite of its next step
    resv_g1: int = -1                      # (CSR, records): the last round another frame's hold kept its next step (G1)
    resv_js: tuple = ()                    # (SER-R): the walk's static boundaries (a placed script, up to the walk's limit)
    resv_hist: frozenset = frozenset()     # (SER-R): the members of its earlier walks' prefixes (H's rebases), for the rule
    resv_mleft: int = 0                    # (SER-R): the secured hold-mark writes (past the protected 2) it has not spent
    resv_marks: int = 0                    # (SER-R, records): the hold marks it wrote
    resv_set: set = field(default_factory=set)   # (SER-R): the held instructions whose bit is written (each owes a clear)
    resv_unb: set = field(default_factory=set)   # (SER-R): the held instructions past the buffer: no bit until they enter
    resv_scan: int = -1                    # (SER-R): the buffer head at its last entry scan (-1: scan again)


_SET_EMPTY_SIZE = sys.getsizeof(set())


def _set_ascending(s: set, bound: int) -> bool:
    """True when CPython iterates `s`, a set of non-negative ints each below `bound`, in ascending order: its hash table
    has more than `bound` slots, so every element sits in its own home slot (hash(i) == i, slot = i & mask, no two
    elements share one) and slot order is numeric order. The slot count is read from sys.getsizeof (16-byte entries
    beyond the 8-entry inline table). (deploy 2 speed; E2E_SCAN_CHECK=1 checks every use against the set itself.)"""
    return (sys.getsizeof(s) - _SET_EMPTY_SIZE) // 16 > bound


@dataclass
class Ledger:
    rounds: int = 0
    patch_rounds: int = 0                      # generated syndrome rounds = occupied patches summed over rounds
    recovery_patch_rounds: int = 0             # ... of which spliced recovery instructions (also counted under role "recovery")
    recovery_patch_rounds_by_class: dict = field(default_factory=lambda: {c: 0 for c in SCRIPT_CLASSES})   # ... by the
                                               # owning script's class (records only: `_script_class`)
    recovery_patch_rounds_by_event_class: dict = field(default_factory=dict)   # (plan v2 3.1, records only):
                                               # {event: {class: n}}, the same rounds by the script's starting event
                                               # (RECOVERY_EVENTS, `_defer_splice`) and class; its sum is the class counter's
    role_patch_rounds: dict = field(default_factory=lambda: {r: 0 for r in ROLES})
    reserved_cell_rounds: int = 0              # per round: reserved workspace cells not occupied by a patch (union across values)
    reserved_cells_peak: int = 0               # rule 12: the largest such count in any round, reported beside the sum
    defer_reserved_cell_rounds: int = 0        # (records only): per round, D's reserved cells not occupied by a patch: the union of
                                               # the live frames' candidate workspaces, decided materializations' workspaces and the cells
                                               # of spliced scripts still running (`_defer_ws_cells`, `_defer_target_cells`, `_defer_locked`)
    defer_reserved_cells_peak: int = 0         # ... the largest such count in any round
    defer_decided_cell_rounds: int = 0         # ... of which decided workspaces and running scripts' cells only (the candidate
    defer_decided_cells_peak: int = 0          #     workspaces' guard holds a cell only for a patch whose next user waits for a frame)
    defer_steps_ended: int = 0                 # (B12, records only): D's admitted steps whose instructions with a duration
                                               # have all ended (paper 05:56-57: admission fixes a step's end at admission plus
    defer_steps_late: int = 0                  # its encoded duration) ... of which ended after that round (the deferral path
    defer_late_rounds: int = 0                 # has no step timer) ... their lateness in rounds, summed
    defer_late_max: int = 0                    # ... and the largest
    held_native: int = 0                       # instruction-rounds a conditional waited for the delivered definitive result
    held_no_prediction: int = 0
    held_policy: int = 0                       # WAIT at the current boundary (or an earlier step still in flight)
    held_controller: int = 0                   # table still building
    held_fence: int = 0                        # held at a contract's fence or behind a not-yet-admitted step
    held_overlap: int = 0                      # atom refused: another unresolved value's support / D / footprint intersects
    held_reserved: int = 0                     # instruction refused: a cell reserved by another value (ordinary allocations included)
    held_workspace: int = 0                    # atom refused: its recovery workspace is not free
    held_not_ready: int = 0                    # atom refused: external prerequisite (predecessor / delivered result) missing
    held_class_miss: int = 0                   # value's site class not in the codebook: never speculates
    held_feedback: int = 0                     # w10: instruction-rounds a classically controlled CZ waited for its measurement's DELIVERED decode
    feedback_total: int = 0                    # w10: classically controlled CZ groups (measurement-based uncomputations)
    feedback_executed: int = 0                 # w10: ... whose measured bit was 1 (the CZ ran; 0: skipped)
    accepted_steps: int = 0                    # w10 instrumentation: steps ACCEPTed (a verdict pending at every ACCEPT)
    accepted_two_qubit_steps: int = 0          # ... of which a two-qubit gadget (CNOT / CZ): entangling successors run on a prediction
    accepted_two_qubit_residuals: int = 0      # ... of which the residual after the step is a two-qubit quarter-turn
    accepted_by_template: dict = field(default_factory=dict)
    speculative_issues: int = 0
    speculative_skips: int = 0
    confirmations: int = 0
    contradictions: int = 0
    recoveries_spliced: int = 0
    recoveries_frame_only: int = 0
    c6_violations: int = 0                     # must be 0
    timer_violations: int = 0                  # must be 0
    reservation_transfers: int = 0
    class_misses: int = 0
    class_miss_waits: int = 0                  # values the engine never REGISTERed because their site class is not in the codebook
    site_limit_refusals: int = 0               # ... or because the RTL cannot hold their site class (se_pkg.sv widths / 64 classes)
    instance_pool_full: int = 0                # ... or because all 1024 site-instance addresses were live (retried next round)
    zero_only_sites: int = 0                   # contracts without a positive-duration atom: no site, never registered, WAIT
    steps_refused_over_64: int = 0
    no_recovery_workspace_at_build: int = 0
    registrations_deferred: int = 0
    registrations_rejected_full: int = 0
    values_total: int = 0
    program_done_round: int = -1               # every ORIGINAL instruction complete
    settled_round: int = -1                    # ... and every value settled
    fence_reasons: dict = field(default_factory=dict)
    F_hist: dict = field(default_factory=dict)
    P1_total: int = 0                          # encoded script cost of every recovery actually run
    P2_stall_patch_rounds: int = 0             # program-wire patch-rounds idled between contradiction delivery and recovery start
    P2_settle_patch_rounds: int = 0            # ... between recovery completion and settlement (decode + delivery)
    rejected_notifications: int = 0
    recovery_frames_applied: int = 0           # recoveries whose complete final frame (Step.frame_after) was recorded at completion
    recovery_frames_nonidentity: int = 0       # ... of which non-identity
    decoder_key_fallbacks: int = 0             # decoder draws (latency or rejection coin) whose windows could not be identified for keying (must be 0)
    decoder_key_collisions: int = 0            # two DIFFERENT window identities named by one stream key (must be 0)
    decoder_rejection_draws: int = 0           # upstream's speculation-rejection coins answered from a keyed stream
    volume_out_of_range: int = 0               # decode windows beyond the latency table's largest class (the run refuses; must be 0)
    frame_log: list = field(default_factory=list)   # (round, vid, step, pauli key, conditional generators) of every applied recovery frame
    events: list = field(default_factory=list)

    def to_dict(self):
        d = dict(self.__dict__); d["events"] = d["events"][:5000]; d["frame_log"] = d["frame_log"][:5000]; return d


TWO_QUBIT_TEMPLATES = ("CNOT_cf_patched", "CZ", "CZ[pred=asis]")


@dataclass
class Feedback:
    """w10: one classically controlled CZ (a measurement-based uncomputation).  Never speculated: its .lss group waits for
    the DELIVERED decode of its measurement in both arms (the decode anchor, `compile._bind_feedback`), then runs iff the
    measured bit is 1 (a keyed draw per measurement event, identical in N and Q)."""
    fid: int
    gadget_id: str
    result_id: str
    measurement_event: str
    anchor_idx: int
    group: tuple
    truth: bool
    decoded_round: int = -1
    delivered_round: int = -1


def _defer_log_row(dev, fr: DeferFrame, how, end, updates, r) -> tuple:
    """A frame's deferral-log row (`log_fields`): at its end (`_defer_finish`), or for a frame still live at the run's end
    (G, records only: `log_live`, how "live:<state>", no end round).  A function of the device's `_h` and `defer_cfg`
    only, as `_defer_finish`'s own row was (test_r6 runs it on a stand-in device)."""
    row = (fr.vid, fr.c.F, fr.c.fence_reason, fr.p_verdict, fr.p_run, fr.target, how,
           fr.r[fr.p_verdict] if 0 <= fr.p_verdict <= fr.c.F else None,
           fr.r[fr.target] if 0 <= fr.target <= fr.c.F else None, fr.r[fr.c.F],
           fr.reg_round, fr.verdict_round, end, fr.decide_waits, fr.ws_waits,
           r["stall_rounds"] if r else None, (r.get("P2_settle_patch_rounds", 0) if r else None), updates,
           fr.stop, fr.top, fr.top_why, fr.admit_waits, fr.first_ok)
    if dev._h is not None:                                                 # H runs only: the early materialization (H_LOG_FIELDS)
        e = fr.h_early_rec
        row += (fr.h_trigger_round, fr.h_choice_round, fr.h_je, fr.h_splice_round, fr.h_early_done, fr.h_F0,
                e["P1"] if e else None, e["stall_rounds"] if e else None, fr.rebased)
    if dev.defer_cfg.get("settle") == "decode":                            # the hold only (plan-v3): the recovery script's end,
        rend = r.get("recovery_end_round") if r else None                 #  where R2's fit ends W
        row += (None if rend is None else int(rend),)
    return row


class ScoutDeviceManager(DeviceManager):
    ledger = None
    _wq = None                               # the bounded write queue (`froq/wq.py`), off unless `--wq` builds it
    _wq_why = _wq_fwhy = None                # (records): why the port's last event waited; a counted FORWARD's refusal
    _walk_cap = None                         # (X11): rule 66's walk cap, H's J and K tied to it (`--walk-cap`); None: the
                                             #  module's WALK_CAP, H_JMAX and H_K, read when used
    _rec56 = None                            # (plan v2 5.6): arm D's records with --r7-records (`_rec56_summary`)
    _late_open = None                        # (B12): arm D's watch of admitted steps (`_late_admit`); none elsewhere

    def __init__(self, d_t, schedule, lightweight_setting=0, rng=np.random.default_rng(), *, arm="N", seed=0, events: EventStream,
                 semantics: Semantics, client_latency=1, q_value=0.9, confidence=0.9, policy=None, K=4, path=0, keep_events=False,
                 codebook=None, build_contracts=None, force_truth=None, force_prediction=None, wrong_set=None, defer=None,
                 client="modeled", f_flip=1.0, stall_rounds=None, progress_stop_h=None, strip_cores=None, strip_accuracy="lower",
                 reservation=True, workspace_cap=None, t_supply=None, stopping=None, strip_class_rule="corrected",
                 strip_geometry_rule="corrected", pool=None, u_replay=None, planner="client", u_suppress=(), thr_latency=(1, 1),
                 common=None, u6_instr=False, u6_lambda=None, budget_level=None, u6_prior=None, w2_lambda=None,
                 w2_records=False, w2_profiles=None, w2_profile_id=None, hw_limits=None, bench=None, w2_r2=None,
                 w2_xic=None, w2_vq=None, r7_records=False, r7_guard=False, r7_reserve=0, r8_rule=False, r8_gsvc=0, adv_err=None,
                 r9_check=None, w2_hold=None, walk_cap=None, guard=None, cov=None, h_depth=None, r17_records=False,
                 w2_profile_file=None, w2_price_profile_file=None, diag_records=False,
                 client_set=None, ctrl_slow=None):
        self._sidecar_ready = False
        # (plan v2 3.3 and 4.2 C-D): what the strip client supplies through the interface -- availability a (a
        # keyed coin per value; the rest never get a prediction), a publication delay, the wrong-prediction probability m
        # times its own at nominal or matched confidence.  None (the default): nothing changes
        if client_set:
            cs = dict(client_set)
            assert set(cs) <= {"avail", "delay", "err", "conf", "couple"}, cs
            assert cs.get("avail") is None or 0.0 <= float(cs["avail"]) <= 1.0, cs
            assert cs.get("delay") is None or int(cs["delay"]) >= 0, cs
            assert cs.get("err") is None or float(cs["err"]) >= 1.0, cs
            assert cs.get("conf", "matched") in ("nominal", "matched"), cs
            assert cs.get("couple") in (None, "tail"), cs                 # (--pred-couple tail: `_tail_rank`)
            assert client == "strip" or all(cs.get(k) is None for k in ("avail", "delay", "err")), (   # (--pred-couple
                "client settings act on the strip client (its own or the side channel's); --pred-couple tail alone acts"
                "with any client", client)                                 #  tail alone with any client: its rank is the strip
            #                                                                 coin, a keyed uniform by producer event that needs no
            #                                                                 strip client, so N's reference run with its own client
            #                                                                 draws its producers' decodes at the same ranks as the
            #                                                                 SER-R run it is paired with by seed; plan 3.3, D)
            cs.setdefault("conf", "matched")
            self._cset, self._cstat = cs, collections.Counter()
        if ctrl_slow is not None:                                          # (--ctrl-slow s; plan v2 4.2 A, F8):
            self._ctrl_slow = Fraction(str(ctrl_slow))                     #  the controller s times slower, applied once (the ports
            assert self._ctrl_slow > 1, ctrl_slow                          #  and the write queue at `_hw_round`, the registration's
            assert hw_limits, "--ctrl-slow slows the hardware-limit mode's controller: it needs --hw-limits"   # latency below)
        # (plan v2 2.1, `froq/hdepth.py`): the H depth. RHO > 1 rewrites the program's H gadgets before upstream
        # builds its DAG and durations, and the scripts' through the semantics; None (RHO = 1, the default): nothing here
        self._hd = _hd.parse_rho(h_depth)
        self._h_fill = collections.defaultdict(list) if self._hd is not None else None   # patch -> its filler rounds, sorted
        self._hd_stats = None
        assert semantics.h_depth in (None, self._hd), ("the semantics carry another H depth", semantics.h_depth, self._hd)
        if self._hd is not None:
            schedule, self._hd_stats = _hd.stretch_schedule(schedule, events, self._hd, d_t)
            semantics.h_depth = self._hd
        # (plan v2 4.4 and 8, `---records`): records only -- requests by kind, admission stalls, each
        # instruction's ready round, cause and binding predecessor, the completion at the last settlement (`_r17_dump`)
        self._r17 = R17Records() if r17_records else None
        # (`--diag-records`, froq/diagrec.py): records only -- each repair's delivery against its charges, the planner's
        # binding constraint at each held start, each hold's rounds by reason (with the records)
        self._diag = DiagRecords() if diag_records else None
        if self._diag is not None and self._r17 is not None:
            self._r17.held_by = {}
        self._guard = guard or "ch"                                       # (the guarantee study's settings, plan v2 A1.3:
        assert self._guard in GUARDS, guard                                #  `GUARDS`; ch, the charged guard, the default)
        if self._guard == "zl":                                            # (ZL: the same guard and checks, no service time charged:
            r8_gsvc = 0                                                    #  c0, c1 and f, the guard checks' cycles, the events'
            r9_check = None if r9_check is None else dict(r9_check, c1=0)  #  port-1 waits; the queue's admission stays)
        self._g_fail = {}                                                  # (G, OFF: failure kind -> [count, first round])
        self._g_first = {}                                                 # (G, OFF, records only: kind -> its first failure's who)
        self._off_tg, self._g_seen, self._ser_full = set(), set(), 0       # (OFF: starts a decided workspace's guard would have
                                                                           #  held; (kind, key) counted; SER: values refused)
        lf = (defer or {}).get("lf")                                       # (round 11 plan v2 1.1: the live-frame limit k, a
        w2lf = ((defer or {}).get("wager2") or {}).get("lf")               #  setting of the decision; None: no limit; SER is k = 1,
        assert lf is None or w2lf is None or lf == w2lf, ("two live-frame limits", lf, w2lf)   #  `_defer_register`. the decision's
        lf = w2lf if lf is None else lf                                    #  option lf is the same setting: either one sets it,
        lf_inf = lf == "inf" and (defer or {}).get("resv") is not None    # (round 13 plan v2 3.1: CSR's k = infinity, "inf",
        assert lf is None or lf_inf or (type(lf) is int and lf >= 1 and self._guard != "ser"), (lf, self._guard)   #  both must agree;
        self._live_cap = 1 if self._guard == "ser" else (None if lf_inf else lf)   #  code check, MED; only with resv, whose
        self._resv = (defer or {}).get("resv")                             #  absent limit is k = 1) (round 12 plan v2 2.1: the
        if self._resv is not None:                                         #  static reservation, SER-R, under the charged guard; None, off)
            assert self._resv in RESVS and self._guard == "ch", ("resv", self._resv, self._guard, lf)   # (CSR, k > 1)
            assert not workspace_cap or isinstance(workspace_cap, dict), \
                ("SER-R's argument has no wait for workspace cap (--cap-hard only)", workspace_cap)
            self._resv_inc = self._resv == "incremental"                   # (plan 7.2: the footprint of the boundary it stands)
            rc = (defer or {}).get("resv_cost") or {}
            assert set(rc) <= set(RESV_COST) and all(type(x) is int and x >= 0 for x in rc.values()), rc
            if lf is None:
                self._live_cap = 1                                         # (--resv static alone is k = 1; --w2-lf k or inf)
            self._resv_cost = dict(RESV_COST, **rc)                        # (counts, or a file's)
            self._resv_held, self._resv_st = {}, collections.Counter()     # (cell -> the live frame that holds it; records)
            self._resv_tested, self._resv_fph = {}, collections.Counter()  # (idx -> the round its test was charged; footprint sizes)
            self._resv_wsf = {}                                            # (records: a settlement's workspace waits, first by class)
            self._resv_rows, self._resv_mh = {}, collections.Counter()    # (walk key -> its footprint row; marks a frame -> frames)
            self._resv_ok = {}                                             # (vid -> (members, need, h_next) of the floor's last passing check)
        if walk_cap is not None:                                          # (X11's limit sweep: the walk cap a setting,
            assert int(walk_cap) >= 1, walk_cap                            #  H's furthest early boundary and rebased depth tied)
            self._walk_cap = int(walk_cap)
        super().__init__(d_t, schedule, lightweight_setting=lightweight_setting, rng=rng)   # its initial dispatch is a no-op (see below)
        assert arm in ("N", "Q", "D"), arm
        self.arm, self.seed = arm, seed
        self._u_replay_path = u_replay                                     # U's replay records: a file, or None
        self._u6_args = dict(instr=bool(u6_instr), lambda_path=u6_lambda, budget_level=budget_level, prior_path=u6_prior)   # U v6
        # the decision: its data files and records; its Wait-for-Decoder setting disables registration, and with it
        # arm D's three D-only branches take N's path (section 7's prerequisite: D = N)
        self._w2_args = dict(lambda_path=w2_lambda, prior_path=u6_prior, budget_level=budget_level, records=bool(w2_records),
                             profiles_dir=w2_profiles, profile_ident=w2_profile_id, bench=bench,
                             **({"r2_path": w2_r2} if w2_r2 is not None else {}),   # (R2 fit file, if not the default)
                             **({"xic_path": w2_xic} if w2_xic is not None else {}),   # (xi_C fit file, likewise)
                             **({"vq_path": w2_vq} if w2_vq is not None else {}), records7=bool(r7_records),   # (round 7: V2q's fit)
                             **({"hold_path": w2_hold} if w2_hold is not None else {}))   # (the H variant's H, plan v2 5.1)
        if w2_profile_file or w2_price_profile_file or self._diag is not None:   # (explicit profile files, the records)
            self._w2_args.update(profile_file=w2_profile_file, price_profile_file=w2_price_profile_file, diag=self._diag)
        # round 7 (plan v3): V2q at m = inf and QG(never) register nothing, so they take Wait-for-Decoder's path as well
        _w2o = (defer or {}).get("wager2") or {}
        self._noreg = arm == "D" and (_w2o.get("setting") == "wfd" or _w2o.get("vq") == "inf" or (defer or {}).get("qg") == "never")
        self._r7 = bool(r7_records)                                        # (round 7's records-only counters and tracer)
        self._r7_guard, self._r7_reserve = bool(r7_guard), int(r7_reserve)   # (round 7's progress settings, 3(e): default off)
        self._r8, self._r8_gsvc = bool(r8_rule), int(r8_gsvc)            # (round 8's rule (a) and the guard-service time: off)
        if adv_err is not None:                                            # (plan v2 5.8: the advertised error's k; off: None)
            self._adv_k = float(Fraction(str(adv_err)))
        if self._r8:
            self._r7_guard = True                                          # (the rule is the guard at every boundary a frame takes)
        self._r8_pend, self._r8_disc, self._r8_reg_vids, self._r8_need, self._r8_tg_off = {}, {}, set(), set(), False
        self._r8_reg_now: dict = {}                                         # (records only): vid -> the round rule (a) refused it
        self._r8_b0_vids, self._r8_mem_vids = set(), set()                 # the values (a1) kept waiting on boundary 0, on members
        self._r8_b0n_vids = set()                                          # the values the rule sent to N's path (no boundary-0 script)
        self._r8_orph = {}                                                 # an ended frame's admitted, unstarted members {instr: (vid, step)},
                                                                           #  authorized until they start
        self._r8_orw_vids, self._r8_wn_vids, self._r8_orph_passed = set(), set(), set()   # the values
        self._r8_fail = None                                               #  that waited on an ended frame's member, the waiting values
                                                                           #  that took N's path, the members passed; the last failing
                                                                           #  permission (`_r8_cert`; records only)
        self._r9 = None                                                    # (bounded check, off)
        if r9_check is not None:                                           # (froq/r9check.py; plan v2 4.2-4.5: B, W and c1, with
            assert self._r8, ("the bounded check is rule (a)'s", r9_check)   #  c0, --r8-gsvc)
            self._r9 = R9(r9_check.get("B"), r9_check.get("W"), c0=int(r8_gsvc), c1=int(r9_check.get("c1", 0)))
            self._r9.size = self._r9_size                                  # (an instruction beyond C or P is fenced, r9check)
            self._r9_open, self._r9_vseen = set(), set()                   # (the second port: started, not complete; verdicts seen)
            self._cov_assert = True                                        # (plan v2 1.4: a `rel` event fails the run)
        if cov is not None:                                                # (plan v2 1.4: cov=enforce, a setting of the
            assert cov == "enforce" and self._r9 is not None and self._guard != "off", ("cov", cov, self._guard)   #  bounded
            self._r9.cov = True                                            #  check's authorization decision; off: rule)
        self._wq = None                                                    # (bounded write queue, off)
        if r9_check is not None and r9_check.get("wq") is not None:       # (froq/wq.py; plan v2 6.2-6.5: C, w, e, f, the batch cap b, H)
            q = r9_check["wq"]
            self._wq = WQ(q.get("C"), w=q.get("w", 286), e=q.get("e"), f=q.get("f", 0), b=q.get("b"), H=q.get("H"),
                          strict=self._guard != "off")                     # (OFF: writes nothing reserved are counted, not fatal)
            self._wq.r17 = self._r17                                       # (---records: its entries a round)
            if self._guard == "off":
                self._wq.on_over = self._g_over                            # (G, records only: the first overflow's live frames)
            assert self._wq.C == float("inf") or self._wq.H >= 2 * (1 + self._r9.C), ("H must hold a start", self._wq.H)
            self._r9.bmax = self._wq.bmax                                  # (a grant's update part past b stops: the safe answer)
            self._wq_end = {}                                              # (vid -> the clearing writes reserved for its end)
            self._wq_cert, self._wq_resv = {}, {}                          # (vid -> certificate cells installed; the largest reservation)
            self._wq_pend = {}                                             # (vid not yet live -> the last round its registration
                                                                           #  installed or was collected)
            self._wq_ox, self._wq_tch = (), set()                           # (the event in service: its consequences; frames it marks)
            self._wq_hb = {}                                               # (vid -> the instructions whose hold marks changed this round)
            self._wq_req = set()                                           # (F1: instructions whose request the queue refused)
            self._r9.wq_on = True                                          # (records: a cover off its frame's chain is counted)
        assert self._guard == "ch" or self._r9 is not None, ("G's settings are the bounded check's (--r8-event-budget)", self._guard)
        assert self._resv is None or (self.arm == "D" and self._r8 and self._r9 is not None), \
            ("SER-R is charged on the check port and the write path (--r8-rule, --r8-event-budget)", self._resv)
        assert self._resv is None or not getattr(self._r9, "cov", False), \
            ("cov=enforce is the bounded check's walk rule; SER-R has no walk (plan v2 2.1)", self._resv)
        self._r7_piu, self._r7_trace, self._r7_last_ret = (), [], (0, 0)    # (the phase's patches in use; the tracer's rows)
        self._r7_resv = {k: {} for k in R7_RESV}                         # (round 7's records, 3(g): open reservations, cell -> round)
        self._r7_resv_len = {k: [] for k in R7_RESV}                     # (... and the durations of those released)
        self._r7_ts_acq: dict = {}                                       # (... an injection's T state taken at ACCEPT: the round)
        self._hw_cfg, self._hw, self._bench = hw_limits, None, bench
        # rule 41b' (gap analysis Part I): the threshold path's deadlines for H's trigger, the installation's and the
        # RECONFIDENCE's (5 and 8 rounds in the main convention; 1 and 1, the original protocol, keep H's rule of B.10)
        self._thr_install, self._thr_rc = (int(x) for x in thr_latency)
        assert self._thr_install >= 1 and self._thr_rc >= 1, thr_latency
        self._rc_prev_c: dict = {}                                         # vid -> the confidence a revision replaced (records)
        self._u_suppress = {(int(v), int(e)) for v, e in (u_suppress or ())}   # a counterfactual's unasked requests (finding 7)
        self.es, self.sem = events, semantics
        if semantics.tables.library == "sx":                               # (sx library): its two scripts are compared by S + XS, priced
            semantics.price = self._sx_price                               #  at this run's d, path and decoder model (`_sx_price`)
        self.krng = KeyedRNG(seed)
        self.client_latency, self.q_value, self.confidence = client_latency, q_value, confidence
        assert client in ("modeled", "swiper", "strip"), client
        assert 0.0 <= f_flip <= 1.0, f_flip
        self.client, self.f_flip = client, float(f_flip)                   # the prediction source
        # rule 41d: with planner "swiper", D's start-time planner and SWIPER's start-up planner read SWIPER's
        # live, causal predictions in every arm, and the configured client feeds only the decisions of U, H and Q-thr.  In
        # arm D the device's own predictions are then SWIPER's -- exactly D-nci's -- and the decision client (strip,
        # modeled) publishes on a side channel that only U and H read (`_dview`); in arms N and Q the device's own
        # predictions stay the client's (Q's engine registers them) and `_replan` reads SWIPER's, except for a value Q has
        # speculated, which is planned as its S group runs from its ACCEPT on (41d's exception, Part J: found Q-thr's
        # skipped S planned at half its durations, and its steps overran their timers).  "client" (the default): the
        # planner reads the configured client, as before.  Every client draws from its own keyed stream.
        assert planner in ("client", "swiper"), planner
        self.planner, self._dec_client = planner, client
        self._side = client if (planner == "swiper" and arm == "D" and not self._noreg and client != "swiper") else None
        if self._side is not None:
            self.client = "swiper"
        self._plan_swiper = planner == "swiper" and (arm != "D" or self._noreg) and client != "swiper"
        self._side_ready: list = []                                        # (producer end round, vid): the side channel's candidates
        self._side_pub: dict = {}                                          # vid -> (publication round, m, c) on the side channel
        self._sw_plan_due: list = []                                       # heap (round, vid): SWIPER's prediction due for `_replan`
        self._rc_pending: set = set()                                      # SWIPER client: values with a reset notice after T_emit
        self._swc = dict(published=0, reconfidenced=0, rc_messages=0, published_reset=0)
        # the deadlock guard: no instruction started or completed for this many rounds raises "stalled".  Default 50 d + 1000;
        # a decoder-scarce run waits on its decode queue for longer than that without any deadlock (qpeexact_5 d21 at
        # cap 23, 551 windows queued ahead of the one the program needs), so such runs pass a larger value
        self.stall_rounds = int(stall_rounds) if stall_rounds else 50 * d_t + 1000
        # the progress stop (decoder-scarce cells): a run whose share of original instructions completed projects its
        # total wall time beyond this many hours (checked from 20 minutes on) ends like a clock timeout -- success False,
        # the rates recorded in `progress_stop` -- instead of running into the job's walltime; None = off
        self.progress_stop_h = float(progress_stop_h) if progress_stop_h else None
        self.progress_stop = None
        # rule 29 (gap analysis): censoring at CENSOR_FACTOR x R_N execution rounds, and a deadlock only for a
        # static state or a wait-for cycle among frames and patches.  It replaces the stall guard and the wall-time
        # progress stop (wall time stays an infrastructure guard: the job's limit).  None = off: nothing changes
        self._r29 = None
        if stopping is not None:
            rn = int(stopping["R_N"])
            assert rn > 0 and not self.progress_stop_h, "rule 29 needs R_N > 0 and replaces the wall-time progress stop"
            self._r29 = dict(R_N=rn, limit=CENSOR_FACTOR * rn, outcome=None, knot_due=R29_KNOT_EVERY, started=0)
        self.policy, self.K, self.path, self.keep_events = policy, K, path, keep_events
        self.codebook = codebook                                           # {K: [site class tuples]} or None
        # (gap analysis rule 33a as amended at 22:42): the static common eligibility set (value ids; None: off).
        # A value outside it neither speculates (Q: it WAITs, as on a class miss) nor registers with U (no request, no ACT)
        self._common = None if common is None else frozenset(int(x) for x in common)
        self._common_outside: set = set()                                  # values refused for being outside it
        self.build_contracts = (arm == "Q") if build_contracts is None else build_contracts
        self.ledger = Ledger()
        self.n_original = len(self.schedule_instructions)
        # (gap analysis rule 15, Q-nores): no workspace reservation. A recovery allocates its cells on demand
        # and waits while ordinary allocations hold them (`_nores_order`); nothing else changes.  Its record joins the
        # summary only in that mode, so a default run's ledger is unchanged.
        self.reservation = bool(reservation)
        assert self.reservation or arm == "Q", "reservation is a Q setting"
        self._nores = None if self.reservation else dict(recoveries=0, recoveries_waited=0, cells_occupied=0, holder_instructions=0,
                                                          recovery_edges=0, wait_rounds=0, wait_patch_rounds=0, max_wait_rounds=0)
        self._nores_cells_last: dict = {}           # cell -> the last recovery instruction spliced on it (such recoveries run in order)
        self._nores_owner: dict = {}                # ancilla cell -> vid of the recovery whose patch is on it (checked at every start)
        self._nores_pending: dict = {}              # a recovery's first instructions -> (vid, splice round) until one starts
        self._instrs_on_cell = None                 # cell -> original instruction indices touching it, in order (built on first use)
        self._cell_ptr: dict = {}                   # cell -> position of its first unstarted user in that list (only advances)
        self._later_times = {}                                             # proven-later frontier startups of the current round
        # round-2 speed (bit-identical): candidate sets for the per-round scans that used to walk every value
        self._prev_active_snapshot: set = set()      # active instructions at the end of the previous dispatch
        self._zero_completed: list = []              # zero-duration completions made by this dispatch
        # Q-arm round cost (profile of grover d=21: 3000 frontier instructions x their predecessors and all 14085
        # values were scanned every round).  Exact bookkeeping instead: predecessors still running per instruction, the values
        # stamped in THIS round (the policy's message counts) and the predicted-undelivered candidates of its per-round pass.
        self._pending_preds: dict | None = None      # idx -> predecessors with end_round == -1 (built with _adj, kept by the completion paths)
        self._pred_blocked: bytearray = bytearray()  # _pred_blocked[idx] == 1  <=>  _pending_preds[idx] > 0 (C-level filter of the frontier loops)
        self._round_events: dict = dict(predicted=[], delivered=[], recovered=[])
        self._predicted_undelivered: set = set()     # predicted_round >= 0 and not delivered
        self._completed_undecoded: set = set()       # tracked instructions completed but not yet stamped decoded
        self._decoded_undelivered: set = set()       # vids whose producer is decoded, delivery pending
        self._pending_ready: list = []               # (producer end_round, vid): PENDING values whose producer completed
        self._live_undelivered: set = set()          # LIVE, undelivered, not class_miss (admission candidates)
        self._settle_candidates: set = set()         # delivered, not yet in a terminal state
        self._recovering: set = set()                # RECOVERING
        self._speculated_unsettled: set = set()      # speculated and not settled (C6 monitor)
        self._timer_candidates: set = set()          # admitted steps with unchecked timers
        self._vid_of_producer: dict = {}             # producer instruction -> vid
        # deploy 2 (speed, bit-identical): the frontier's entries with no running predecessor, and its startup
        # instructions, kept in step with the frontier (built on first use; `_frontier_sets`)
        self._frontier_ready: set | None = None
        self._frontier_startup: set | None = None
        self._scan_check = bool(os.environ.get("E2E_SCAN_CHECK"))   # assert every candidate scan equals the full scan
        self._progress_file = os.environ.get("E2E_PROGRESS_FILE")     # live progress heartbeat (author), None = off
        self._progress_t0 = self._progress_last = time.time()
        # durations: upstream zeroed the CONDITIONAL_S groups with its coin; here the truth is a keyed draw per producer
        # event (design Sec. 4) and a group keeps its real durations until the branch is legitimately known
        self._true_durations = [Duration.get_true_duration(t.instruction.duration, self.d_t) for t in self.schedule_instructions]
        self._instruction_durations = list(self._true_durations)
        self._plan_durations = list(self._true_durations)                   # the start-time predictor's causal estimate
        self.sched_view = ScheduleView([t.instruction for t in self.schedule_instructions], self.schedule_dag, self.d_t, self._true_durations)
        self._recovery_instrs: set[int] = set()
        self._recovery_owner: dict[int, int] = {}
        self._recovery_class: dict[int, str] = {}                           # spliced instr -> its script's class (records only)
        self._recovery_event: dict[int, str] = {}                           # spliced instr -> its script's starting event (records only)
        self._recovery_local: dict[int, tuple] = {}                         # spliced instr -> (owner vid, boundary, index in the lowered script)
        self._decode_idents: dict[str, tuple] = {}                          # decode stream digest -> the window identity that named it
        self.values: dict[int, Value] = {}
        self._value_of_instr: dict[int, int] = {}
        self.contracts: dict[int, Contract] = {}
        self._chain_of_instr: dict[int, list] = collections.defaultdict(list)      # instr -> [(vid, step k)]
        self._fence_of_instr: dict[int, set] = collections.defaultdict(set)         # instr -> {vid}
        self._fence_by_vid: dict[int, set] = {}                                      # vid -> its fence instrs (speed, `_fence_set_of`)
        self._fence_rank: dict[int, int] = {}                                        # instr -> its key's position in _fence_of_instr
        self._cone_escape_flagged: set[int] = set()                                  # values whose cone work escaped a hold (flagged once)
        self._reserved: dict[tuple, int] = {}                                        # cell -> vid
        self._decoded_round: dict[int, int] = {}                                     # tracked instr -> decode completion round
        self._tracked: set[int] = set()
        self._taint_wires: dict[int, set] = {}                                       # C6: vid -> wires touched by admitted work
        self._taint_results: dict[int, set] = {}                                     # C6: vid -> result ids of admitted measurements
        self._timer_checked: set = set()
        self._role_of_patch: dict[tuple, str] = {}
        self._instr_wires: dict[int, frozenset] = {}
        self._instr_results: dict[int, frozenset] = {}
        self._instr_preds: dict[int, frozenset] = {}
        self._instr_maxpos: dict[int, int] = {}
        for idx, eids in self.es.lss_reverse.items():
            evs = [self.es.events[e] for e in eids]
            self._instr_maxpos[idx] = max(e.pos for e in evs)
            patches = set(self.schedule_instructions[idx].instruction.patches) if idx < len(self.schedule_instructions) else set()
            # an instruction ACTS on a wire only if it occupies the wire's own cell: a merge's routing-cell DISCARD or a
            # rotation's neighbour-cell IDLE (the converter's busy region) touches no logical state and is not a consumption
            acted = set()
            for e in evs:
                for w in e.wires:
                    cells = set(self.es.wire_cell(w, e.event_id))
                    if not cells or cells & patches:
                        acted.add(w)
            self._instr_wires[idx] = frozenset(acted)
            self._instr_results[idx] = frozenset(e.result_id for e in evs if e.result_id)
            self._instr_preds[idx] = frozenset(r for e in evs for r in e.predicate_ids)
        self._seen_missed = 0
        self._rejection_log: list = []                                      # (decision round, poisoned decode task, attempt): the adapter's OWN record
        self._seen_rejected = 0
        self._force_truth, self._force_prediction = force_truth or {}, force_prediction or {}
        # correlated-error client (gap analysis concern 1d): the producer events whose prediction is wrong, fixed in
        # advance by froq.run (`slow_half_wrong_set`); None = the independent client (a keyed coin with P(wrong) = 1 - q)
        self._wrong_set = None if wrong_set is None else frozenset(wrong_set)
        assert self._wrong_set is None or self._dec_client == "modeled", "the correlated-error client is a modeled client"
        # WAIT history: per value the closed WAIT episodes (k, first round, rounds, source, end), and the open ones
        self._wait_log: dict[int, list] = collections.defaultdict(list)
        self._wait_open: dict[int, list] = {}                               # vid -> [k, first round, last round, source]
        self._defer_setup(defer)                                           # arm D only (deferral baseline); N and Q: nothing
        self._sens_setup(workspace_cap, t_supply)                          # resource knobs (off: nothing changes)
        self._sw_setup()                                                   # SWIPER's speculative windows, recorded
        self._discover_values()
        self._discover_feedback()
        if getattr(self, "_u6", None) is not None:
            self._u6.check_lambda()                                        # (U v6, 51b: a frozen Lambda's gap fails at once,
            self._u6.check_prior()                                         #  and 50d's prior's)
        # (part C, rule 41d: U with delta = infinity makes no request and runs no strip job: D-nci with SWIPER's client)
        self._strip_on = client == "strip" and not (self._u is not None and not self._u_client)   # (U6-off: the client is off)
        if self._side == "strip" and not self._strip_on:
            self._side = None                                              # (no strip client: D-nci with SWIPER's, exactly)
        self._strip_splices_on = False
        # rule 42: the shared decoder pool, B cores for the full decodes and the strip
        # client's jobs (idle cores only, non-preemptive); None = the dedicated strip cores of rule 31, or unlimited
        self._pool = None
        if pool is not None and self._strip_on:
            from .strip import SharedPool
            assert not strip_cores, "a shared pool replaces dedicated strip cores"
            self._pool = SharedPool(int(pool["B"]), self._strip_class_max, preemptive=bool(pool.get("preemptive", True)))
        if self._strip_on:
            self._strip_setup(strip_cores, strip_accuracy, strip_class_rule, strip_geometry_rule, pool=self._pool)
        # with E2E_STRIP_TRACE set, a strip run also records its traces (`_strip_trace`, a side file of run.py).
        # Records only: the flag adds two side lists and changes nothing the run reads.
        self._trace_on = self._strip_on and bool(os.environ.get("E2E_STRIP_TRACE"))
        self._trace_scripts: list = []
        # critical-path records (diag, ported for U v4's smoke): with E2E_DIAG_CP set, every round's
        # blocked-and-waiting instructions are tallied by reason (and, for a value's hold, by value), for a critical-path
        # attribution of the run's rounds (`_dg_dump`, a side file of run.py).  Records only: nothing the run reads.
        self._dg = bool(os.environ.get("E2E_DIAG_CP"))
        self._dg_round: dict = {}
        self._dg_hold: dict = collections.defaultdict(collections.Counter)
        self._dg_holdv: dict = collections.defaultdict(collections.Counter)
        self._dg_last: dict = {}                                       # instr -> the reason of its last waiting round
        self._dg_lastblk: dict = {}                                    # instr -> the instruction holding its patch then
        self._dg_reg: dict = {}                                        # (plan v2 6): vid -> [outcome, retries, last wait reason]
        if self._strip_on:
            # part 1d: the spliced gadgets are collected when a corrected rule reads them or a trace records them; a
            # legacy traced run's windows also record the corrected geometry (the audit's shadow), a corrected traced
            # run's windows their gadget lists (the reconstruction check).  Records only in a legacy run.
            self._strip_splices_on = "corrected" in (self._strip_class_rule, self._strip_geometry_rule) or self._trace_on
            self._strip_pool.shadow = self._strip_draw_shadow if self._trace_on and self._strip_geometry_rule == "legacy" else None
            self._strip_pool.trace_gadgets = self._trace_on and self._strip_geometry_rule == "corrected"
        # pilot knobs, off by default (nothing changes). E2E_STRIP_SALT re-keys the strip client's two draws,
        # its correctness coin and its window decode times: a null replay of the independent model.  E2E_STRIP_TSCALE
        # multiplies its window decode times only; the full decoder's draws are untouched.  run.py records them in the result.
        self._strip_salt = (os.environ.get("E2E_STRIP_SALT") or None) if self._strip_on else None
        self._strip_tscale = float(os.environ.get("E2E_STRIP_TSCALE") or 1.0) if self._strip_on else 1.0
        self._strip_knobs = {k: v for k, v, off in (("salt", self._strip_salt, None), ("tscale", self._strip_tscale, 1.0)) if v != off}
        self.ledger.values_total = len(self.values)
        self._last_progress_round, self._last_progress_count = 0, -1
        self._sidecar_ready = True
        self._update_active_instructions(set())                    # the real initial dispatch, with the sidecar in place

    # ------------------------------------------------------------------ values
    def _cell_of_wire(self, wire: str, at_eid: str) -> tuple:
        """The program cell a program wire lives on, from the DOCUMENT's program-role lifetimes (round-9 review,
        finding 1): one chain of lifetimes per wire; when they agree on a cell that is the answer, otherwise the
        lifetime live at `at_eid` decides.  The device binds both the discovered S target and every spliced
        recovery's operands through this one map, so the two are compared like with like.  Fail closed."""
        chain = self._wire_chain.get(wire)
        if not chain:
            raise RuntimeError("wire %s carries no program-role lifetime in the event document" % wire)
        cells = {c for lt in chain for c in self.es.lifetime_cells.get(lt, ())}
        if len(cells) != 1:
            cells = {tuple(c) for c in self.es.wire_cell(wire, at_eid)}      # the lifetime live at this point
        if len(cells) != 1:
            raise RuntimeError("wire %s has no single program cell at %s (lifetimes %s, cells %s)"
                               % (wire, at_eid, sorted(chain), sorted(cells)))
        return tuple(next(iter(cells)))

    def _discover_values(self):
        """The speculated values: every T gadget of the event stream matched to its CONDITIONAL_S group in the .lss,
        with the group's IDLE patch as the S target -- bound (round-9) to the program cell of the
        wire `EventStream.t_gadgets` derives from the T2's S merge, so discovery and splicing share one wire map."""
        self._wire_chain: dict[str, list] = collections.defaultdict(list)
        for lt, w in self.es.lifetime_wire.items():
            if w is not None and self.es.lifetime_role.get(lt) == "program":
                self._wire_chain[w].append(lt)
        groups = {}
        for t in self.schedule_instructions:
            ins = t.instruction
            if ins.conditioned_on_idx and ins.group_instr_indices and ins.group_name != "CONDITIONAL_CZ":   # w10: feedback groups apart
                groups[tuple(sorted(ins.group_instr_indices))] = ins.conditioned_on_idx
        self._specs: dict[int, ValueSpec] = {}
        for spec in values_of(self.es):
            prod = set(self.es.lss_map[spec.producer_event])              # the ZZ merge (+ the DISCARD of its routing cells)
            group = tuple(sorted(self.es.lss_of(spec.t2.events)))
            assert group in groups, "T2 of %s (%s) is not one of SWIPER's CONDITIONAL_S groups" % (spec.gadget_id, group)
            cond = set(groups[group]) & prod
            assert len(cond) == 1, "group %s is conditioned on %s, the producer event maps to %s" % (group, groups[group], prod)
            producer_idx = next(iter(cond))                                # the instruction whose decode is the definitive result
            members = [self.schedule_instructions[i].instruction for i in group]
            idle = [m for m in members if m.name == "IDLE"]
            target = next(iter(idle[0].patches)) if idle else next(iter(members[0].patches))
            # round-9 (discovery side): the S target found in the .lss (the T2 group's IDLE patch)
            # must BE the program cell of the value's corrected wire -- the wire `EventStream.t_gadgets` derives
            # from the T2's S merge.  A document whose wire identity was bent (validators bypassed) otherwise runs
            # the S on one patch and the contract on another; fail closed, the run has no result.
            want = self._cell_of_wire(spec.wire, spec.t2.events[-1])
            if tuple(target) != want:
                raise RuntimeError("value %d (%s): the T2 group's S target patch is %s, the corrected wire %s lives "
                                   "on program cell %s (group %s)" % (spec.vid, spec.gadget_id, tuple(target), spec.wire, want, group))
            truth = self._force_truth.get(spec.producer_event, self._force_truth.get("*"))   # ("*", every value)
            if truth is None:
                truth = self.krng.uniform("truth", spec.producer_event) < 0.5
            v = Value(vid=spec.vid, gadget_id=spec.gadget_id, result_id=spec.result_id, producer_event=spec.producer_event, producer_idx=producer_idx,
                      wire=spec.wire, group=group, target_patch=target, truth=bool(truth), confidence=self.confidence)
            self.values[v.vid] = v; self._specs[v.vid] = spec; self._vid_of_producer[producer_idx] = v.vid
            for i in group:
                self._value_of_instr[i] = v.vid
            self._tracked.add(producer_idx)
            self._replan(v)
        assert len(groups) == len(self.values), "%d CONDITIONAL_S groups in the .lss, %d T gadgets in the events" % (len(groups), len(self.values))
        assert list(self.values) == sorted(self.values), "the candidate scans iterate vids ascending = the values' insertion order"

    def _discover_feedback(self):
        """w10 (classical feedback): every CONDITIONAL_CZ group of the .lss, bound to its record in the event document
        (`classical_feedback`: the CZ gadget, the measurement's event and result, the decode anchor, the group), which must
        agree with the instructions' own conditional fields and with the gadget's events.  Fail closed."""
        self.feedback: dict[int, Feedback] = {}
        self._feedback_of_instr: dict[int, int] = {}
        self._feedback_pending: set = set()
        lss_groups: dict = {}
        for t in self.schedule_instructions:
            ins = t.instruction
            if ins.group_name == "CONDITIONAL_CZ":
                lss_groups.setdefault(tuple(sorted(ins.group_instr_indices)), set()).add(frozenset(ins.conditioned_on_idx))
        recs = self.es.doc.get("classical_feedback") or []
        if len(recs) != len(lss_groups):
            raise RuntimeError("%d CONDITIONAL_CZ groups in the .lss, %d classical_feedback records in the events" % (len(lss_groups), len(recs)))
        for fid, r in enumerate(recs):
            group = tuple(r["lss_group"])
            if lss_groups.get(group) != {frozenset([r["decode_anchor"]])}:
                raise RuntimeError("feedback %s: the .lss group %s is not conditioned on the decode anchor %s" % (r["gadget"], group, r["decode_anchor"]))
            if tuple(self.es.lss_of(r["events"])) != group or self.es.events[r["measurement_event"]].result_id != r["result_id"]:
                raise RuntimeError("feedback %s: the group or the measurement disagrees with the event stream" % r["gadget"])
            truth = self.krng.uniform("feedback", r["measurement_event"]) < 0.5
            fb = Feedback(fid, r["gadget"], r["result_id"], r["measurement_event"], int(r["decode_anchor"]), group, bool(truth))
            self.feedback[fid] = fb
            self._feedback_pending.add(fid)
            for i in group:
                self._feedback_of_instr[i] = fid
            self._tracked.add(fb.anchor_idx)
            self._replan_feedback(fb)
        self.ledger.feedback_total = len(self.feedback)

    def _replan_feedback(self, fb: Feedback):
        """The causal startup estimate of a feedback group: the delivered bit, else the mean (as `_replan`)."""
        for i in fb.group:
            real = self._true_durations[i]
            self._plan_durations[i] = (real if fb.truth else 0) if fb.delivered_round >= 0 else real // 2

    def _deliver_feedback(self):
        """w10: a feedback group's measurement is delivered `path` rounds after its decode anchor's decode completes; the
        group's durations then become the branch (CZ iff the bit is 1).  Called after the decode stamping of the round."""
        now = self.current_round
        for fid in sorted(self._feedback_pending):
            fb = self.feedback[fid]
            if fb.anchor_idx not in self._decoded_round:
                continue
            fb.decoded_round = self._decoded_round[fb.anchor_idx]
            if now >= fb.decoded_round + self.path:
                fb.delivered_round = now
                self._feedback_pending.discard(fid)
                for i in fb.group:
                    self._instruction_durations[i] = self._true_durations[i] if fb.truth else 0
                self._replan_feedback(fb)
                self.ledger.feedback_executed += int(fb.truth)

    def _feedback_hold(self, idx: int) -> str | None:
        fid = self._feedback_of_instr.get(idx)
        if fid is None or self.feedback[fid].delivered_round >= 0:
            return None
        return "feedback"

    def _apply_branch(self, v: Value, apply_s: bool):
        for i in v.group:
            self._instruction_durations[i] = self._true_durations[i] if apply_s else 0

    def _replan(self, v: Value):
        """Causal startup estimate (Sec. 5 MA-9/MB-10): the delivered truth, else the prediction, else the per-site mean.
        A value arm D deferred is planned as its S group runs, and so, by rule 41d's exception, is a value arm Q has
        speculated on: from its ACCEPT, which replans it."""
        if self.arm == "D" and v.speculated:                               # arm D: a deferred value's S group ran with zero duration
            for i in v.group:
                self._plan_durations[i] = 0
            return
        if self.arm == "Q" and v.speculated:                               # arm Q: the branch its ACCEPT applied (rule 41d's exception)
            for i in v.group:
                self._plan_durations[i] = self._instruction_durations[i]
            return
        m = v.prediction
        if self._plan_swiper:                                              # rule 41d: SWIPER's live prediction, once published
            m = (v.truth != v.sw_wrong) if (v.t_emit >= 0 and self.current_round >= v.t_emit + self.path) else None
        for i in v.group:
            real = self._true_durations[i]
            if v.delivered:
                self._plan_durations[i] = real if v.truth else 0
            elif m is not None:
                self._plan_durations[i] = real if m else 0
            else:
                self._plan_durations[i] = real // 2

    # ------------------------------------------------------------------ SWIPER's speculative windows
    SW_FIELDS = ("vid", "producer", "merge_end", "t_emit", "t_own_verified", "t_verified", "own_tasks", "own_resets_before",
                 "pending", "survival", "peek_rejects", "anc_instrs", "anc_tasks", "anc_win_tasks", "anc_redecoding_at_emit",
                 "own_resets_gap", "first_own_reset", "anc_resets_gap", "category", "own_latency",
                 "last_kind", "last_latency", "last_resets", "last_done")

    def _sw_setup(self):
        """The speculative-window record of every T value (ported from the earlier line's commit
        30265f3 and adapted to this base). Per value, from the producing merge's decoding tasks (`_instruction_tasks`):

        - T_emit: the first round at which every task of the merge exists, the tasks' commit regions cover every
          (patch, round) of the merge, and each has completed decoding since its last reset -- the merge's parity
          exists, possibly on speculated boundaries;
        - T_own_verified: the first round at which all of them are verified;
        - T_verified: the device's decode stamp (the producer is not in the simulator's incomplete set: its own tasks
          and the tasks of every schedule ancestor verified), which delivery waits.

        At T_emit it records the pending speculations of the own tasks (parents whose predicted boundary the current
        attempt used) with SWIPER-SIM's acceptance probability of each and whether the keyed coin that will decide it
        rejects (read, never drawn), and what blocks the stamp: the own tasks' unverified window-DAG ancestors (their
        verification needs them; windows of idle rounds carry no instruction, so only the window DAG names them) and
        the producer's schedule ancestors that are still incomplete, with their unverified tasks. From T_emit to the
        stamp it counts the rejections that reset an own task or a blocking task. It reads the decoder's state and the
        adapter's own rejection log and changes nothing."""
        self._sw: dict = {}                        # producer idx -> record, from its first observation to its decode stamp
        self._sw_watch: dict = {}                  # instruction idx -> producers whose emitted, unstamped record it blocks
        self._sw_watch_task: dict = {}             # task idx -> producers whose emitted, unstamped record it blocks (window-DAG ancestors)
        self._sw_rej: dict = {}                    # task idx -> rejections that reset it so far (every window)
        self._sw_seen_rej = 0                      # cursor into _rejection_log
        self._sw_log: list = []                    # closed records, SW_FIELDS
        self._sw_counts = dict(uncovered_at_verify=0, coupling_violations=0)

    def _sw_covered(self, idx: int, tasks, dec) -> bool:
        """Do the commit regions of the producer's decoding tasks cover every (patch, round) of the producer?  A merge's
        windows are built one patch at a time, so its first windows can finish before the last one exists."""
        t = self.schedule_instructions[idx]
        s, e, patches = t.start_round, t.end_round, t.instruction.patches
        got = 0
        for i in tasks:
            for r in dec._tasks_by_idx[i].window.commit_region:
                if r.patch in patches:
                    got += max(0, min(r.round_start + r.duration - 1, e) - max(r.round_start, s) + 1)
        return got >= len(patches) * (e - s + 1)

    def _sw_coin(self, dec, p: int, o: int):
        """The number the keyed rejection coin will draw for the speculation of task o on its parent p (`_RejectionCoin`:
        the stream named by the two windows, the next attempt of that pair), computed without drawing it; None without
        the keyed decoder."""
        coin = dec.rng
        if not isinstance(coin, _RejectionCoin):
            return None
        pk, ph = self._decode_key_of(dec._tasks_by_idx[p], "reject")
        sk, sh = self._decode_key_of(dec._tasks_by_idx[o], "reject")
        key = (pk if pk.startswith("recovery_") else sk, ph, sh)
        return self._decode_sampler.sampler_state._keyed.uniform(*key, coin._attempts.get(key, 0) + 1)

    def _sw_observe(self, nfd):
        """Once per round, before the decode stamps: the rejections the decoder decided in its last step, then the
        emission, own-task verification and decode stamp of every completed, unstamped producer."""
        dec = LINK.get("decoder")
        if dec is None or getattr(dec, "_current_round", None) != self.current_round:   # not this simulation's decoder
            return
        now = self.current_round
        for r, t, _a in self._rejection_log[self._sw_seen_rej:]:          # decided in round r, visible now (= r + 1)
            self._sw_rej[t] = self._sw_rej.get(t, 0) + 1
            task = dec._get_task_or_none(t)
            hit = set(self._sw_watch_task.get(t, ()))
            for x in (task.window.parent_instr_idx if task is not None else ()):
                hit |= self._sw_watch.get(x, set())
            for idx in hit:
                rec = self._sw[idx]
                if t in rec["own"]:
                    rec["own_rej"] += 1
                    if rec["first_own_rej"] is None:
                        rec["first_own_rej"] = r
                else:
                    rec["anc_rej"] += 1; rec["anc"].add(t)
        self._sw_seen_rej = len(self._rejection_log)
        for idx in self._completed_undecoded:
            if idx not in self._vid_of_producer:
                continue
            rec = self._sw.get(idx)
            if rec is None:
                rec = self._sw[idx] = dict(emit=None)
            if rec["emit"] is None:
                tasks = dec._instruction_tasks.get(idx)
                if tasks and all(dec._tasks_by_idx[i].completed_decoding for i in tasks) and self._sw_covered(idx, tasks, dec):
                    self._sw_emit(idx, rec, tasks, dec, now, nfd)
            if rec["emit"] is not None and rec["tv"] is None and all(dec._is_verified_task(i) for i in rec["own"]):
                rec["tv"] = now
            if idx not in nfd:                                             # the device stamps it decoded this round
                self._sw_close(idx, rec, dec, now)

    def _sw_emit(self, idx: int, rec: dict, tasks, dec, now: int, nfd):
        """T_emit: the merge's parity exists.  Records the pending speculations of its own tasks with SWIPER-SIM's
        acceptance probability of each (decoder_manager.py:172: speculation_accuracy, the parent's missed-speculation
        modifier toward the task, the touching faces) and whether the keyed coin that will decide it rejects; the own
        tasks' unverified window-DAG ancestors (a verified task's ancestors are verified, so the walk stops there); and
        the blocking instructions: the producer's schedule ancestors in the incomplete set `nfd`, reached from the
        producer through incomplete predecessors (a blocker X has the producer among its seen descendants, and every
        instruction on that path is incomplete too), with their unverified tasks."""
        own = frozenset(tasks)
        acc, S, pend, peek = dec.speculation_accuracy, 1.0, 0, 0
        for o in own:
            task = dec._tasks_by_idx[o]
            for p, used in task.used_parent_speculations.items():
                if used:
                    par = dec._tasks_by_idx[p]
                    thr = (1 - (1 - acc) * par.speculation_modifiers.get(o, 1.0)) ** task.window.count_touching_faces(par.window)
                    S *= thr; pend += 1
                    x = self._sw_coin(dec, p, o)
                    peek += x is not None and x > thr
        win, stack, dag = set(), list(own), dec._window_idx_dag
        while stack:
            for q in dag.predecessors(stack.pop()):
                if q not in win and q not in own and not dec._is_verified_task(q, treat_none_as_true=True):
                    win.add(q); stack.append(q)
        instrs, stack = set(), [idx]
        while stack:
            for y in dec._preds(stack.pop()):
                if y in nfd and y not in instrs and y != idx:
                    instrs.add(y); stack.append(y)
        anc = set(win)
        for x in instrs:
            for t in dec._instruction_tasks.get(x, ()):
                if t not in own and not dec._is_verified_task(t, treat_none_as_true=True):
                    anc.add(t)
        redec = sum(1 for t in anc if self._sw_rej.get(t, 0))
        v = self.values[self._vid_of_producer[idx]]                     # the SWIPER client's published attempt:
        v.t_emit, v.sw_q = now, S                                        # wrong iff a pending coin rejects and the keyed f_flip coin flips it
        v.sw_wrong = bool(peek) and self.krng.uniform("fflip", v.producer_event) < self.f_flip
        if self._plan_swiper:
            heapq.heappush(self._sw_plan_due, (now + self.path, v.vid))  # rule 41d: the planner reads it from then
        rec.update(emit=now, own=own, instrs=instrs, win=frozenset(win), anc=anc, n_anc=len(anc), n_win=len(win), S=S, pend=pend,
                   peek=peek, redec=redec, own_before=sum(self._sw_rej.get(t, 0) for t in own), own_rej=0, anc_rej=0,
                   first_own_rej=None, tv=None)
        for x in instrs | {idx}:
            self._sw_watch.setdefault(x, set()).add(idx)
        for t in win:
            self._sw_watch_task.setdefault(t, set()).add(idx)

    def _sw_close(self, idx: int, rec: dict, dec, now: int):
        """T_verified = now.  The gap [T_emit, T_verified) is (c) when a rejection reset one of the merge's own tasks
        after T_emit, else (b) when a task of a blocking instruction was reset in the gap or was still unverified after
        an earlier rejection at T_emit, else (a) latency only; "-" without a gap.  Latencies are the final attempts'
        (completion - start + 1 rounds); the last blocker is the own or blocking task whose decode completed last."""
        if rec["emit"] is None:                                            # stamped before its windows covered it: must not happen
            self._sw_counts["uncovered_at_verify"] += 1
            rec.update(emit=now, own=frozenset(dec._instruction_tasks.get(idx, ())), instrs=set(), win=frozenset(), anc=set(), n_anc=0,
                       n_win=0, S=1.0, pend=0, peek=0, redec=0, own_before=0, own_rej=0, anc_rej=0, first_own_rej=None, tv=now)
        if rec["tv"] is None:
            rec["tv"] = now
        if rec["peek"] and not rec["own_rej"]:                             # a rejecting pending coin must reset an own task first
            self._sw_counts["coupling_violations"] += 1
        T = dec._tasks_by_idx
        lat = lambda i: T[i].decoding_completion_time - T[i].decoding_start_time + 1
        blockers = rec["own"] | rec["anc"]
        last = max(blockers, key=lambda i: (T[i].decoding_completion_time, i)) if blockers else None
        gap = now - rec["emit"]
        cat = "-" if gap == 0 else "c" if rec["own_rej"] else "b" if (rec["anc_rej"] or rec["redec"]) else "a"
        self._sw_log.append([self._vid_of_producer[idx], idx, self.schedule_instructions[idx].end_round, rec["emit"], rec["tv"], now,
                             len(rec["own"]), rec["own_before"], rec["pend"], round(rec["S"], 6), rec["peek"], len(rec["instrs"]),
                             rec["n_anc"], rec["n_win"], rec["redec"], rec["own_rej"], rec["first_own_rej"], rec["anc_rej"], cat,
                             sorted(lat(i) for i in rec["own"]), (None if last is None else "own" if last in rec["own"] else "anc"),
                             (None if last is None else lat(last)), (None if last is None else self._sw_rej.get(last, 0)),
                             (None if last is None else T[last].decoding_completion_time)])
        for x in rec["instrs"] | {idx}:
            w = self._sw_watch.get(x)
            if w is not None:
                w.discard(idx)
                if not w:
                    del self._sw_watch[x]
        for t in rec["win"]:
            w = self._sw_watch_task.get(t)
            if w is not None:
                w.discard(idx)
                if not w:
                    del self._sw_watch_task[t]
        del self._sw[idx]

    def _sw_summary(self) -> dict:
        return dict(fields=list(self.SW_FIELDS), path=self.path, open=len(self._sw), **self._sw_counts, log=self._sw_log)

    # ------------------------------------------------------------------ live progress
    # ------------------------------------------------------------------ the strip client
    STRIP_KIND = {"H": "H", "S": "S", "Sdg": "S", "CNOT_cf_patched": None, "CZ": "CZ", "X": "pauli", "Y": "pauli", "Z": "pauli"}

    def _strip_setup(self, cores, accuracy, class_rule="corrected", geometry_rule="corrected", pool=None):
        """Rule 11's client.  Per program wire, its gadgets in program order (a T gadget: its injection-and-merge, with
        the value's merge instruction, then its correction S as a gadget of its own; a CNOT: target or control by the
        template's operand order [c, t]); per T value a stream over the gadgets before its merge on its wire; per data
        cell the full decoder's committed frame, from the commit regions of SWIPER-SIM's verified windows."""
        from .strip import CLASS_RULES, GEOMETRY_RULES, Accuracy, Geometry, Stream, StripPool
        assert class_rule in CLASS_RULES, class_rule
        assert geometry_rule in GEOMETRY_RULES, geometry_rule
        self._strip_geo, self._strip_acc = Geometry(), Accuracy(mode=accuracy)
        # the accuracy class's rule: "corrected", everything that executes on the patch between the anchor
        # and the merge, the gadgets that scripts splice in included (`_strip_splice`, `strip.executed_kinds`); "legacy",
        # v7's rule, the original program's gadgets as the final window saw them
        self._strip_class_rule = class_rule
        self._strip_geometry_rule = geometry_rule                      # part 1d: the windows' sizes (`StripPool`)
        self._strip_spliced = collections.defaultdict(list)            # program wire -> spliced gadgets (kind, instrs, id)
        self._strip_nsplice = 0                                        # spliced scripts so far: the "k" of a gadget's id "k.j"
        self._strip_wire_of_pid = {pid: w for w, pid in self.es.patch_id.items() if pid is not None}
        self._strip_published = 0
        vid_of_gadget = {v.gadget_id: v.vid for v in self.values.values()}
        entries: dict = collections.defaultdict(list)                  # wire -> [(pos, entry)]
        for gid, g in self.es.gadgets.items():
            if g.parent is not None:
                continue
            fl = self.es.flatten(gid)
            if not fl:
                continue
            pos = self.es.events[fl[0]].pos
            instrs = tuple(self.es.lss_of(fl))
            wires = [w for w in dict.fromkeys(list(g.inputs) + list(g.outputs)) if self.es.roles_by_wire.get(w) == "program"]
            for w in wires:
                if g.template_id == "T" and gid in vid_of_gadget:
                    v = self.values[vid_of_gadget[gid]]
                    t1 = tuple(i for i in instrs if i not in set(v.group))
                    entries[w].append((pos, dict(kind="T", instrs=t1, merge=(v.producer_idx,), vid=v.vid)))
                    entries[w].append((pos + 0.5, dict(kind="S", instrs=tuple(v.group), correction_of=v.vid)))
                elif g.template_id == "CNOT_cf_patched":
                    entries[w].append((pos, dict(kind="CNOT_t" if len(g.inputs) > 1 and g.inputs[1] == w else "CNOT_c", instrs=instrs)))
                else:
                    entries[w].append((pos, dict(kind=self.STRIP_KIND.get(g.template_id, "other"), instrs=instrs)))
        self._strip_wire = {w: [e for _, e in sorted(lst, key=lambda x: x[0])] for w, lst in entries.items()}
        for lst in self._strip_wire.values():                          # Paulis and gadgets without instructions do no work
            for e in lst:
                if not e["instrs"]:
                    e["kind"] = "pauli"
        self._strip_cells: dict = {}
        self._strip_next: dict = {}                                    # vid -> the next T value on its wire
        self._strip_first: list = []                                   # the first T value of every wire
        idx_of: dict = {}
        for w, lst in self._strip_wire.items():
            prev = None
            for i, e in enumerate(lst):
                if e["kind"] == "T":
                    idx_of[e["vid"]] = (w, i)
                    if prev is None: self._strip_first.append(e["vid"])
                    else: self._strip_next[prev] = e["vid"]
                    prev = e["vid"]
        self._strip_lo: dict = {w: 0 for w in self._strip_wire}      # first entry of a wire not committed by its frame
        self._strip_pool = StripPool(self.d_t, cores, self._strip_geo, draw=self._strip_draw, frame_fn=self._strip_frame,
                                     span_fn=self._strip_span, spliced_fn=lambda st: self._strip_spliced.get(st.wire, ()),
                                     geometry_rule=geometry_rule, pool=pool)
        if self._hd is not None:                                           # (--h-depth): its windows count syndrome
            fills = self._h_fill                                           #  rounds, an H's fillers left out (hdepth.py)
            self._strip_pool.fill_fn = lambda cell, a, b: _hd.fill_length(fills, cell, a, b)
        for v in self.values.values():
            w, i = idx_of[v.vid]
            self._strip_cells.setdefault(tuple(v.target_patch), dict(regs=[], frame=None, inits=[], maxlen=0))
            st = Stream(vid=v.vid, key=v.producer_event, cell=tuple(v.target_patch), gadgets=_WireSlice(self._strip_wire[w], 0, i))
            st.wire = w
            self._strip_pool.add(st)
        self._strip_uninit = set(self._strip_cells)                   # cells whose patch has not been initialized yet
        self._strip_task_scan, self._strip_task_none = 0, set()
        self._strip_opened_first = False
        self._strip_sampler = None

    def _strip_draw(self, volume, *key):
        """A strip window's decode time from the program's latency table (its keyed sampler's strip draw, the stream named
        by `key`, which rule 57's full-decoder scale leaves as it is); None beyond the table's largest class (the table's
        own class rule, LatencyTable.volume_class, without its refusal: the run refuses a full-decoder window there, the
        strip client only stops the stream)."""
        tab = self._strip_sampler_init()
        if max(tab.floor, math.ceil(volume / tab.d)) > tab.classes[-1]:
            return None
        if self._strip_salt is not None:                              # pilot knobs (off: the same key, times 1.0)
            key = (key[0], self._strip_salt) + tuple(key[1:])
        return self._strip_sampler(volume, *key) * self._strip_tscale

    def _strip_class_max(self, volume) -> float:
        """Rule 42's bound on a strip job's service: the latency table's largest sample of the window's class, in rounds
        (the class rule of `_strip_draw`; a window beyond the table never starts)."""
        tab = self._strip_sampler_init()
        c = min(max(tab.floor, math.ceil(volume / tab.d)), tab.classes[-1])
        return float(np.max(tab.strip_samples[c])) / tab.round_us

    def _strip_sampler_init(self):
        if self._strip_sampler is None:
            base = getattr(self, "_decode_sampler", None) or getattr(LINK.get("decoder"), "decoding_time_function", None)
            if not hasattr(base, "draw_keyed"):
                raise RuntimeError("the strip client needs the keyed latency sampler (froq.latency.KeyedSampler)")
            self._strip_sampler, self._strip_table = base.sampler_state.draw_keyed_strip, base.sampler_state.table
            self._strip_keyed = base.sampler_state
        return self._strip_table

    def _strip_draw_shadow(self, volume, *key):
        """The decode time `_strip_draw` would give this volume and key, for the records of a legacy traced run (part
        1d's audit): the same keyed stream and class rule, without touching the table's or the sampler's counters, so the
        run's results stay v7's."""
        tab = self._strip_sampler_init()
        c = max(tab.floor, math.ceil(volume / tab.d))
        if c > tab.classes[-1]:
            return None
        if self._strip_salt is not None:
            key = (key[0], self._strip_salt) + tuple(key[1:])
        smp = tab.strip_samples[c]
        return float(smp[int(self._strip_keyed._keyed.stream(*key).integers(len(smp)))]) / tab.round_us * self._strip_tscale

    def _strip_regions(self):
        """New SWIPER-SIM windows: their commit regions on the data cells, sorted per cell by round."""
        dec = LINK.get("decoder")
        if dec is None:
            return
        tasks = dec._tasks_by_idx
        new = [i for i in self._strip_task_none if tasks[i] is not None]
        n = len(tasks)
        for i in range(self._strip_task_scan, n):
            (new.append(i) if tasks[i] is not None else self._strip_task_none.add(i))
        self._strip_task_scan = n
        for i in new:
            self._strip_task_none.discard(i)
            for r in tasks[i].window.commit_region:
                c = self._strip_cells.get(tuple(r.patch))
                if c is None:
                    continue
                s0, e0 = int(r.round_start), int(r.round_start + r.duration)
                bisect.insort(c["regs"], (s0, e0, i))
                c["maxlen"] = max(c["maxlen"], e0 - s0)

    def _strip_init(self, cell, rnd: int):
        """A patch initialized on a data cell: its frame starts at its first syndrome round (a fresh patch: nothing before
        it to decode; a re-initialized cell's earlier rounds belong to the patch that left it)."""
        c = self._strip_cells[cell]
        c["inits"].append(rnd)
        c["frame"] = rnd if c["frame"] is None else max(c["frame"], rnd)
        self._strip_uninit.discard(cell)

    def _strip_frame(self, cell):
        """The full decoder's committed frame on a cell: from the patch's first syndrome round (its initialization: nothing
        before it to decode), the end of the longest prefix of its rounds covered by commit regions of verified windows
        (monotone: a verified window stays verified; SWIPER-SIM never removes a constructed window).  None before the patch
        exists."""
        c = self._strip_cells[cell]
        dec = LINK.get("decoder")
        if dec is None or not c["regs"] or c["frame"] is None:
            return c["frame"]
        regs, f = c["regs"], c["frame"]
        moved = True
        while moved:
            moved = False
            j = bisect.bisect_right(regs, (f, 1 << 60, 1 << 60)) - 1
            while j >= 0 and regs[j][0] > f - c["maxlen"] - 1:
                s0, e0, t = regs[j]
                if s0 <= f < e0 and dec._is_verified_task(t, treat_none_as_true=False):
                    f = e0; moved = True
                    break
                j -= 1
        c["frame"] = f
        return f

    def _strip_span(self, instrs):
        """A gadget's executed span: None (not started), (s, None) running, (s, e) done (e exclusive), SKIPPED (every
        instruction ran with duration 0: a correction that was not needed)."""
        from .strip import SKIPPED
        s = e = None
        work = unfinished = False
        for i in instrs:
            if self._instruction_durations[i] == 0:
                continue                                              # no work: a skipped correction (its branch decided)
            work = True
            t = self.schedule_instructions[i]
            if t.end_round == -1:
                unfinished = True
            if t.start_round != -1:
                s = t.start_round if s is None else min(s, t.start_round)
                if t.end_round != -1:
                    e = t.end_round + 1 if e is None else max(e, t.end_round + 1)
        if not work:
            return SKIPPED
        if s is None:
            return None
        return (s, None) if unfinished else (s, e)

    def _strip_round(self, completed_now):
        """Once per round, after the decode stamps and before the publications: new windows' commit regions, openings
        (a wire's first T value in round 0; the next value when a merge ends), merge starts, verdicts that arrived
        first (the stream stops), under the shared pool values whose window has been fully decoded (T_emit: the stream
        stops, rule 42a), the pool's step, and the accuracy draw of every stream that finished its final window."""
        pool, now = self._strip_pool, self.current_round
        self._strip_regions()
        if not self._strip_opened_first:
            self._strip_opened_first = True
            for vid in self._strip_first:
                self._strip_open(vid, now)
        for idx in completed_now:
            vid = self._vid_of_producer.get(idx)
            if vid is not None and vid in self._strip_next:
                self._strip_open(self._strip_next[vid], now)
        for vid in list(pool._active):
            st, v = pool.streams[vid], self.values[vid]
            if st.t_m is None:
                t0 = self.schedule_instructions[v.producer_idx].start_round
                if t0 != -1:
                    pool.merge_started(vid, t0)
            if v.delivered:
                pool.cancel(vid, now)
            elif pool.pool is not None and 0 <= v.t_emit <= now:
                pool.stale(vid, now)                                   # rule 42a: the merge's full decode has completed
        for vid, pub in pool.step(now):
            self._strip_draw_value(vid, pub)

    def _strip_open(self, vid, now):
        st = self._strip_pool.streams[vid]
        f = self._strip_frame(st.cell)
        lst, lo = self._strip_wire[st.wire], self._strip_lo[st.wire]
        while f is not None and lo < st.gadgets.hi and self._strip_ended_by(lst[lo], f):
            lo += 1
        self._strip_lo[st.wire] = lo
        st.gadgets.lo = lo
        self._strip_pool.open(vid, now)

    def _strip_ended_by(self, e, R) -> bool:
        from .strip import SKIPPED
        if e["kind"] == "pauli":
            return True
        sp = self._strip_span(e["merge"] if e["kind"] == "T" else e["instrs"])
        return sp == SKIPPED or (sp is not None and sp[1] is not None and sp[1] <= R)

    def _strip_draw_value(self, vid, pub):
        """The finished stream's value: rule 11's q for (d, class since the anchor, age), its correctness a keyed coin."""
        from .strip import accuracy_class, executed_kinds
        st, v = self._strip_pool.streams[vid], self.values[vid]
        age = st.t_m - st.final_R
        if self._strip_class_rule == "legacy":                        # v7: the program's gadgets as the final window saw them
            kinds = st.final_kinds
        else:                                                          # everything executed between the anchor and the merge
            kinds = executed_kinds(list(st.gadgets), self._strip_spliced.get(st.wire, ()), self._strip_span, st.final_R, st.t_m)
        cls = accuracy_class(kinds)
        q, cons, a = self._strip_acc.value(self.d_t, cls, age)
        v.strip_pub, v.strip_q, v.strip_cons = int(pub), float(q), bool(cons)
        salt = () if self._strip_salt is None else (self._strip_salt,)
        cs = self._cset
        if cs is None or cs.get("err") is None:
            v.strip_wrong = not (self._strip_coin(v) < q)
        else:                                                          # (--pred-err-scale m): the wrong-prediction probability
            e = max(1.0 - q, min(cs["err"] * (1.0 - q), 0.5))          #  m times the predictor's own, at most a coin's, from the
            v.strip_wrong = not (self._strip_coin(v) < 1.0 - e)        #  same keyed uniform
            v.strip_c = q if cs["conf"] == "nominal" else 1.0 - e      # nominal: the calibrated predictor's c; matched: 1 - e
        if cs is not None:
            if cs.get("avail") is not None and not (self.krng.uniform("avail", *salt, v.producer_event) < cs["avail"]):
                v.strip_pub = -1                                       # (--pred-avail a): this value never gets a prediction
                self._cstat["withheld"] += 1
            elif cs.get("delay"):
                v.strip_pub = int(pub) + int(cs["delay"])               # (--pred-delay delta): published delta rounds later
                self._cstat["delayed"] += 1
            self._cstat["wrong"] += bool(v.strip_wrong)
            self._cstat["drawn"] += 1
        st.acc = dict(cls=cls, age=age, age_d=a, q=q, conservative=cons)
        if self._strip_class_rule != "legacy":
            st.acc["kinds"] = list(kinds)

    def _strip_records(self) -> dict:
        """Per value: the stream's timeline and draws (fields), for the tables."""
        fields = ["vid", "open", "t_m", "anchor", "age", "acc_class", "q", "conservative", "published_at", "L", "windows",
                  "reanchors", "final_class", "final_wait", "final_start", "decoded", "delivered", "registered", "state"]
        rows = []
        d = self.d_t
        for vid, st in self._strip_pool.streams.items():
            v = self.values[vid]
            fin = st.windows[-1] if st.windows and st.windows[-1]["kind"] == "final" else None
            acc = getattr(st, "acc", None) or {}
            rows.append([vid, st.open_round, st.t_m, (st.final_R if st.final_R >= 0 else None), acc.get("age"), acc.get("cls"),
                         acc.get("q"), acc.get("conservative"), (st.pub_round if st.pub_round >= 0 else None),
                         ((st.pub_round - (st.t_m + d)) if st.pub_round >= 0 else None), len(st.windows), st.reanchors,
                         (fin["cls"] if fin else None), (round(fin["start"] - fin["ready"], 3) if fin else None),
                         (round(fin["start"] - st.t_m, 3) if fin else None),                        # the final window's start after t_m
                         v.decoded_round, v.delivered_round, self._dview(v)[0], st.state])
        return dict(fields=fields, rows=rows)

    def _strip_summary(self) -> dict:
        pool = self._strip_pool
        dv = self._dview
        pub = [v for v in self.values.values() if dv(v)[0] >= 0]
        spec = [v for v in pub if v.speculated]
        cp = (lambda v: v.c_pub) if self._side is None else (lambda v: dv(v)[2])   # (rule 41d: the side channel's c)
        acc = lambda vs: dict(n=len(vs), c_mean=(sum(cp(v) for v in vs) / len(vs)) if vs else None, right=sum(1 for v in vs if dv(v)[1] == v.truth))
        done = [st for st in pool.streams.values() if st.pub_round >= 0]
        Ls = sorted(st.pub_round - (st.t_m + self.d_t) for st in done)
        pick = lambda p: (Ls[min(len(Ls) - 1, int(p * len(Ls)))] if Ls else None)
        cons = [v for v in self.values.values() if v.strip_pub >= 0 and v.strip_cons]
        tiling = collections.Counter()                                # the frame's premise: a cell's commit regions tile its rounds
        for c in self._strip_cells.values():                           # from each initialization round, without gap or overlap
            regs, inits = c["regs"], set(c["inits"])
            if regs:
                starts = {x[0] for x in regs}
                tiling["first_not_init"] += sum(1 for i in inits if i not in starts) + (regs[0][0] not in inits)
                tiling["gaps"] += sum(1 for a, b in zip(regs, regs[1:]) if b[0] > a[1] and b[0] not in inits)
                tiling["overlaps"] += sum(1 for a, b in zip(regs, regs[1:]) if b[0] < a[1])
        q_hist, cells = {}, collections.Counter()                     # c = q to 6 decimals -> [published, right]; the cells drawn
        for v in pub:
            e = q_hist.setdefault("%.6f" % cp(v), [0, 0]); e[0] += 1; e[1] += int(dv(v)[1] == v.truth)
        for st in done:
            a = getattr(st, "acc", None)
            if a:
                cells["%s@%s" % (a["cls"] or "unmeasured", "%dd" % a["age_d"] if a["age_d"] else ">12d")] += 1
        return dict(pool.summary(self.current_round), accuracy_mode=self._strip_acc.mode, geometry_sha256=self._strip_geo.sha256,
                    q_hist=dict(sorted(q_hist.items())), accuracy_cells=dict(sorted(cells.items())),
                    points_sha256=self._strip_acc.sha256, values=len(self.values), published=self._strip_published,
                    published_share=(round(self._strip_published / len(self.values), 6) if self.values else None),
                    published_acc=acc(pub), admitted_acc=acc(spec), c_hist=_c_hist(pub),
                    finished_streams=len(done), conservative=len(cons), L_median=pick(0.5), L_p90=pick(0.9),
                    cells=len(self._strip_cells), cells_never_initialized=len(self._strip_uninit),
                    region_tiling=dict(first_not_init=tiling["first_not_init"], gaps=tiling["gaps"], overlaps=tiling["overlaps"]),
                    **({"client_set": dict(self._cset, **dict(sorted(self._cstat.items())),
                                           **({"tail": self._tail_summary()} if self._cset.get("couple") == "tail" else {}))}
                       if self._cset is not None else {}))

    def _tail_summary(self) -> dict:
        """(`--pred-couple tail`, plan v2 3.3): the realized relation, records only. The values with a
        published prediction (the decision client's view) and a delivered verdict, ranked by the verdict's lead (delivery
        less the prediction's publication) and cut into tenths: each tenth's lead range, values and wrong predictions. And
        the repair cost given a wrong commit: the values speculated on with a wrong prediction that recovered, their
        number and their recoveries' encoded (P1) and realised patch-rounds summed, by recovery kind."""
        dv = self._dview
        rows = sorted((int(v.delivered_round) - int(dv(v)[0]), v.vid, dv(v)[1] != v.truth) for v in self.values.values()
                      if dv(v)[0] >= 0 and dv(v)[1] is not None and v.delivered_round >= 0)
        n, tenths = len(rows), []
        for k in range(10):
            part = rows[k * n // 10:(k + 1) * n // 10]
            if part:
                tenths.append([part[0][0], part[-1][0], len(part), sum(1 for x in part if x[2])])
        rep = [v.recovery for v in self.values.values()
               if v.speculated and dv(v)[1] is not None and dv(v)[1] != v.truth and getattr(v, "recovery", None)]
        return dict(fields=["lead_lo", "lead_hi", "n", "wrong"], tenths=tenths,
                    repair=dict(n=len(rep), P1=sum(int(r.get("P1") or 0) for r in rep),
                                realised=sum(int(r.get("realised") or 0) for r in rep),
                                kinds=dict(sorted(collections.Counter(str(r.get("kind")) for r in rep).items()))))

    # ------------------------------------------------------------------------------------------------ traces
    @staticmethod
    def _script_gadgets(low, new):
        """A spliced script's gadgets in executed order (`Lowered.instance_gadgets`), each with its absolute instructions
        and its wires (the snippet's names, "q<patch id>")."""
        absof = {x.idx: ins.idx for x, ins in zip(low.instrs, new)}
        lmap = (low.events or {}).get("lss_map", {})
        gad = []
        for g in low.instance_gadgets():
            idx = sorted({absof[i] for m in g.get("events", ()) for i in lmap.get(m, ()) if i in absof})
            ins = g.get("inputs") or ()
            ins = list(ins.values()) if isinstance(ins, dict) else list(ins)
            gad.append(dict(template=g.get("template_id"), wires=[str(w) for w in ins], instrs=idx))
        return gad

    def _strip_splice(self, low, new, kind):
        """part 1c: a spliced script's gadgets on their program wires, for the corrected accuracy class and geometry
        (the client's own gadget lists hold the original program only). Gadget j of the k-th spliced script has the id
        "k.j" (with a trace, scripts[k].gadgets[j] of the trace). In a legacy run, records only."""
        k = self._strip_nsplice
        self._strip_nsplice += 1
        for j, g in enumerate(self._script_gadgets(low, new)):
            wires = [self._strip_wire_of_pid.get(int(x[1:]), x) if x[:1] == "q" and x[1:].isdigit() else x for x in g["wires"]]
            for w in dict.fromkeys(wires):
                self._strip_spliced[w].append(dict(kind=script_kind_of(g["template"], w, wires), instrs=tuple(g["instrs"]), source=kind,
                                                   id="%d.%d" % (k, j)))

    def _trace_splice(self, vid, k, low, new, kind):
        """A spliced script's gadgets (`_script_gadgets`).  Records only; called when E2E_STRIP_TRACE is set."""
        self._trace_scripts.append(dict(vid=int(vid), step=int(k), kind=kind, round=int(self.current_round),
                                        instrs=[int(i.idx) for i in new], gadgets=self._script_gadgets(low, new)))

    def _dg_tally(self):
        """diag: the round's blocks of instructions that are still waiting, by reason and, for a value's hold, by
        value (the instruction's own value, its chain's first unadmitted value, the fence's value, or the frame)."""
        si = self.schedule_instructions
        for idx, why in self._dg_round.items():
            if si[idx].start_round != -1:
                continue
            self._dg_hold[idx][why] += 1
            self._dg_last[idx] = why
            vid = self._dg_vid(idx, why)
            if vid is not None:
                self._dg_holdv[idx][vid] += 1
            if why == "patch":                                         # who holds a patch it needs (the lowest such index)
                pts = set(si[idx].instruction.patches)
                own = [j for j in self._active_instructions if pts.intersection(si[j].instruction.patches)]
                self._dg_lastblk[idx] = min(own) if own else None
        self._dg_round = {}

    def _dg_vid(self, idx, why):
        if why.startswith("defer_"):
            vids = self._defer_hold_of.get(idx) if hasattr(self, "_defer_hold_of") else None
            if vids:
                return next(iter(sorted(vids)))
            m = getattr(self, "_defer_member", {}).get(idx)
            if m is None and why == "defer_rule_a":
                return self._value_of_instr.get(idx)                     # (a value rule (a) kept from registering)
            return m
        if why == "fence":
            for vid in sorted(self._fence_of_instr.get(idx, ())):
                if not self.values[vid].settled:
                    return vid
        own = self._value_of_instr.get(idx)
        if own is not None and why in ("native", "no_prediction", "class_miss", "policy", "not_ready"):
            return own
        for vid, k in self._chain_of_instr.get(idx, ()):
            v = self.values[vid]
            if not v.settled and k >= v.admitted:
                return vid
        return own

    def _dg_dump(self) -> dict:
        """diag: what a critical-path attribution needs, read at the end of the run. Records only.
          instr [idx, name, start, end, duration, predecessors, {reason: rounds blocked while waiting},
                   {value: rounds held by it}, patches, the reason of its last waiting round, the instruction that held
                   its patch then (a patch wait), the value a spliced script belongs to] for every instruction;
          values [vid, producer, the S group, truth, the device's prediction, its confidence, its publication round,
                   decoded, delivered, settled, speculated, {step: accept round}, the decision client's (publication, m, c),
                   its frame (registration, stop, top, the round it reached its stop, end, how) or None]."""
        si = self.schedule_instructions
        preds = self._adj()[0]
        instr = []
        for i, t in enumerate(si):
            instr.append([i, t.instruction.name, int(t.start_round), int(t.end_round), int(self._instruction_durations[i]),
                          sorted(int(p) for p in preds.get(i, ())), dict(self._dg_hold.get(i, {})),
                          {str(k): n for k, n in self._dg_holdv.get(i, {}).items()},
                          [list(map(int, q)) for q in t.instruction.patches], self._dg_last.get(i), self._dg_lastblk.get(i),
                          self._recovery_owner.get(i)])
        frames = {int(row[0]): list(row) for row in getattr(self, "_defer_log", [])}
        fields = None
        if self.arm == "D":
            fields = ["vid", "F", "fence", "p_verdict", "p_run", "target", "how", "r_verdict", "r_target", "r_F", "reg_round",
                      "verdict_round", "done_round", "decide_waits", "ws_waits", "stall_rounds", "settle_patch_rounds", "updates", "stop",
                      "top", "top_why", "admit_waits", "first_ok"] + (list(H_LOG_FIELDS) if self._h is not None else []) + \
                     (["recovery_end_round"] if self.defer_cfg.get("settle") == "decode" else [])   # _defer_summary's
        values = []
        for vid, v in sorted(self.values.items()):
            pub, m, c, _rc = self._dview(v)
            values.append([int(vid), int(v.producer_idx), sorted(int(i) for i in v.group), int(bool(v.truth)),
                           (None if v.prediction is None else int(bool(v.prediction))), v.confidence, int(v.predicted_round),
                           int(v.decoded_round), int(v.delivered_round), int(v.settled_round), bool(v.speculated),
                           {str(k): int(r) for k, r in v.accept_round.items()},
                           [int(pub), (None if m is None else int(bool(m))), c], frames.get(int(vid))])
        return dict(format="scoutq-diag-cp/1", n_original=int(self.n_original), rounds=int(self.current_round),
                    program_done_round=int(self.ledger.program_done_round), frame_log_fields=fields, instr=instr, values=values,
                    registration=dict(fields=["outcome", "retries", "last_wait"],      # (plan v2 6: each value that came to
                                      values={str(int(k)): list(v) for k, v in sorted(self._dg_reg.items())}))   # registration)

    def _strip_trace(self) -> dict:
        """(gap analysis rule 32): what a trace builder needs to rebuild the gadget sequence around every T
        value as a circuit, read at the end of a strip run. Records only.
          wires per program wire, its gadgets in program order: kind (as the strip client classifies it), executed span
                  ([s, e), None not started, "skipped"), and each instruction as [idx, name, patches, start, end, duration];
                  a T gadget also its value and merge instruction, a correction S the value it corrects;
          scripts every spliced script (a repair, a deferral or H early materialization, an H repair after one): owner,
                  boundary, kind, splice round, its gadgets (template, wires as program wires, instructions, span);
          values per T value: wire, data cell, the stream's anchor and windows as the run decoded them, publication,
                  merge, decode and delivery rounds, the strip draw, the next value on the wire, H's early-script rounds;
          cells per data cell, the commit regions of SWIPER-SIM's windows on it ([s0, e0)) and its initializations."""
        from .strip import SKIPPED
        sched = self.schedule_instructions

        def ins_rec(i):
            t = sched[i]; I = t.instruction
            return [int(i), I.name, [list(map(int, p)) for p in I.patches], int(t.start_round), int(t.end_round),
                    int(self._instruction_durations[i])]

        def span(instrs):
            sp = self._strip_span(instrs)
            return "skipped" if sp == SKIPPED else (None if sp is None else [sp[0], sp[1]])
        wires = {}
        for w, lst in self._strip_wire.items():
            out = []
            for e in lst:
                g = dict(kind=e["kind"], span=span(e["instrs"]), instrs=[ins_rec(i) for i in e["instrs"]])
                if e["kind"] == "T":
                    g.update(vid=int(e["vid"]), merge=[ins_rec(i) for i in e["merge"]], merge_span=span(e["merge"]))
                if "correction_of" in e:
                    g["correction_of"] = int(e["correction_of"])
                out.append(g)
            wires[str(w)] = out
        wire_of_pid = {pid: w for w, pid in self.es.patch_id.items() if pid is not None}
        scripts = []
        for sc in self._trace_scripts:
            gs = []
            for g in sc["gadgets"]:
                prog = [wire_of_pid.get(int(x[1:]), x) if x[:1] == "q" and x[1:].isdigit() else x for x in g["wires"]]
                gs.append(dict(g, wires_program=[str(x) for x in prog], span=span(g["instrs"]),
                               instrs=[ins_rec(i) for i in g["instrs"]]))
            scripts.append(dict(sc, gadgets=gs, instrs=[ins_rec(i) for i in sc["instrs"]], span=span(sc["instrs"])))
        early = {}
        for vid, fr in getattr(self, "_frames", {}).items():
            r = getattr(fr, "h_early_rec", None)
            if r is not None:
                early[int(vid)] = dict(trigger=int(getattr(fr, "h_trigger_round", -1)), splice=int(getattr(fr, "h_splice_round", -1)),
                                       done=int(getattr(fr, "h_early_done", -1)), instrs=[int(i) for i in r.get("instrs", [])])
        wkeys = ("lo", "hi", "commit_end", "kind", "R", "ready", "start", "finish", "count", "cls", "abandoned", "refused",
                 "catch", "spl", "gad", "count_c", "cls_c", "tau_c", "catch_c")   # part 1d: the geometry rule's records
        values = {}
        for vid, st in self._strip_pool.streams.items():
            v = self.values[vid]
            values[str(vid)] = dict(
                wire=str(st.wire), cell=[int(c) for c in st.cell], open=int(st.open_round), t_m=st.t_m,
                merge_end=(int(sched[v.producer_idx].end_round) + 1 if sched[v.producer_idx].end_round >= 0 else None),
                anchor=(st.final_R if st.final_R >= 0 else None), final_kinds=list(st.final_kinds), reanchors=int(st.reanchors),
                windows=[{k: w[k] for k in wkeys if k in w} for w in st.windows], pub=int(st.pub_round), state=st.state,
                next=self._strip_next.get(vid), producer=int(v.producer_idx), decoded=int(v.decoded_round),
                delivered=int(v.delivered_round), predicted=int(v.predicted_round), strip_q=getattr(v, "strip_q", None),
                strip_wrong=getattr(v, "strip_wrong", None), speculated=bool(v.speculated),
                acc=getattr(st, "acc", None), early=early.get(int(vid)))
        cells = {",".join(map(str, c)): dict(regs=[[int(a), int(b)] for a, b, _ in x["regs"]], inits=[int(i) for i in x["inits"]])
                 for c, x in self._strip_cells.items()}
        return dict(format="scoutq-strip-trace/1", d=int(self.d_t), rounds=int(self.current_round), wires=wires,
                    scripts=scripts, values=values, cells=cells)

    def _heartbeat(self):
        """Rewrite E2E_PROGRESS_FILE (atomically) at most every 30 s: the round, instructions completed of the program's
        total, active / frontier sizes, the ledger's value counters and the rate -- so a running job can be watched and an
        interrupted one recognised (the file outlives a crash; run.py deletes it on completion).  No effect on the run."""
        now = time.time()
        if now - self._progress_last < 30 and self.current_round > 0:
            return
        self._progress_last = now
        rec = dict(round=self.current_round, completed_instructions=self._completed_instruction_count, n_original=self.n_original,
                   active=len(self._active_instructions), frontier=len(self._instruction_frontier), values=len(self.values),
                   speculated=self.ledger.speculative_issues + self.ledger.speculative_skips, contradictions=self.ledger.contradictions,
                   confirmations=self.ledger.confirmations, elapsed_s=round(now - self._progress_t0, 1),
                   rounds_per_s=round(self.current_round / max(now - self._progress_t0, 1e-9), 1), updated=time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            tmp = self._progress_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rec, f)
            os.replace(tmp, self._progress_file)
        except OSError:
            pass
        elapsed = now - self._progress_t0
        if self.progress_stop_h and self.progress_stop is None and elapsed >= PROGRESS_STOP_MIN_S:
            frac = self._completed_instruction_count / max(self.n_original, 1)
            projected_h = (elapsed / frac / 3600.0) if frac > 0 else float("inf")
            if projected_h > self.progress_stop_h:                          # cannot finish within the limit at this rate
                self.progress_stop = dict(limit_h=self.progress_stop_h, at_s=round(elapsed, 1), round=self.current_round,
                                          completed_instructions=self._completed_instruction_count, n_original=self.n_original,
                                          fraction=round(frac, 6), projected_h=(round(projected_h, 2) if frac > 0 else None),
                                          rounds_per_s=rec["rounds_per_s"])
                sim = LINK.get("simulator")
                if sim is not None:
                    sim.failed = True                                      # upstream's loop ends as on a clock timeout

    # ------------------------------------------------------------------ rule 29: censoring and deadlock
    def _r29_round(self, progressed: bool, held: dict, start_times: dict):
        """Rule 29 (gap analysis; section 10), after each round's dispatch.
        - Censored: the execution has passed CENSOR_FACTOR x R_N rounds. The bounds are recorded: the time, as that limit,
          and the exposure accumulated up to this round.
        - Deadlocked, static: no instruction is running or started this round, and no progress-enabling event is pending
          (`_r29_pending`): nothing can ever change by waiting.
        - Deadlocked, wait-for cycle: frames waiting for a materialization boundary whose every candidate workspace is
          held by patches that wait for those frames' held work (`_r29_knot`). either deadlock's record keeps the knot with its edges (`_r29_edges`); at a static stop the
        knot is read once with no wait (possibly empty). A slow run with a progress-enabling event pending is neither: it runs to the censoring limit. Either stop ends
        the run as a clock timeout does (success False), with the record in `stopping`."""
        r = self._r29
        if r["outcome"] is not None:
            return
        now = self.current_round
        if now > r["limit"]:
            self._r29_stop("censored", time_bound=r["limit"])
            return
        if not progressed and self._instruction_frontier:
            why = self._r29_pending(held, start_times)
            r["pending_last"] = why
            if why is None:
                kn = {}                                                    # (plan 6.1: the knot once, with no wait,
                knot = self._r29_knot(wait=0, out=kn) if self.arm == "D" else set()   #  since nothing can change by waiting)
                self._r29_stop("deadlocked", kind="static", frontier=sorted(self._instruction_frontier)[:16], frames=sorted(knot),
                               **(kn or dict(knot=[], edges=[], cycles=[], never=[])),
                               **({"stalled_under_cap": True} if self._ws_hard and self._sens["cap_hard_escape_refused"] else {}))
                return
        if self.arm == "D" and not self._noreg and now >= r["knot_due"]:   # (Wait-for-Decoder: no frame, N's path)
            r["knot_due"] = now + R29_KNOT_EVERY
            kn = {}
            knot = self._r29_knot(out=kn)
            r["knot_checks"] = r.get("knot_checks", 0) + 1
            if knot:
                self._r29_stop("deadlocked", kind="wait_for_cycle", frames=sorted(knot), **kn)   # (with its edges)

    def _r29_pending(self, held: dict, start_times: dict):
        """The first progress-enabling event pending, by kind, or None.  Conservative: anything that can change by
        waiting counts, so a deadlock is never declared while it might still resolve (an undetected one is censored)."""
        if self._completed_undecoded:
            return "decode"                                                # a completed producer's decode is outstanding
        if self._decoded_undelivered:
            return "verdict"                                               # decoded, its delivery (the path) is due
        if any(v.delivered_round < 0 and v.producer_idx in self._decoded_round for v in self.values.values()):
            return "verdict"
        if self.arm == "D" and (self._defer_waiting or self._defer_arrivals):
            return "registration"
        if self.arm == "D" and getattr(self, "_hw", None) is not None and (self._hw.queue or self._hw.stalled or self._hw.check_q
                                                                    or any(self._frames[vid].hw_wait for vid in self._live_frames)):
            return "hw_service"                                            # (queued reservation-manager work, which ends;
                                                                           #  arm D's alone, as only D has
                                                                           #  frames, so N and Q read)
        if self._recovering or self._rc_pending or getattr(self, "_feedback_pending", None):
            return "recovery_or_revision"
        if self.arm == "D":
            for vid in self._live_frames:
                fr = self._frames[vid]
                rec = self.values[vid].recovery
                if fr.state == "spliced" and rec is not None and rec["kind"] == "frame":
                    return "materialization"
                if fr.state == "spliced" and rec is not None and rec["kind"] == "script" and self.defer_cfg["settle"] != "none" \
                        and any(i in self._decoded_round and not self._delivered_instr(i) for i in rec["instrs"]):
                    return "delivery"                                      # (its decoded script ages
                                                                           #  `path` rounds to delivery; none at path 0)
                if fr.state == "confirm_wait" or (fr.rebased and fr.h_early_done < 0 and fr.h_early_rec is not None
                                                  and fr.h_early_rec["kind"] == "frame"):
                    return "materialization"
        if held.get("magic") or held.get("magic_accept"):                  # the T-supply gate's own keys
            return "t_supply"                                              # the factories keep producing
        now = self.current_round
        for idx in self._instruction_frontier:                             # a startup planned for a later round
            if start_times.get(idx, self._later_times.get(idx, 0)) > now:
                return "planned_start"
        return None

    def _r29_blockers(self, u: int, cap: int = 4000):
        """The live frames whose held work instruction u waits for, through unstarted predecessors (a bounded backward
        search), or None at the cap (unknown: the caller treats the wait as one that may end)."""
        seen, stack, out = set(), [u], set()
        while stack:
            x = stack.pop()
            if x in seen or x in self._defer_started:
                continue
            seen.add(x)
            if len(seen) > cap:
                return None
            hs = self._defer_hold_of.get(x)
            if hs:
                out |= hs
            stack.extend(p for p in self._adj()[0].get(x, ()) if p not in self._defer_started and p not in seen)
        return out

    def _r29_knot(self, wait: int = R29_KNOT_EVERY, out: dict | None = None) -> set:
        """The frames in a wait-for cycle (a knot): each has waited at least R29_KNOT_EVERY rounds in D's decide with no
        usable boundary, and every feasible boundary from its current one to its top has a workspace cell held by a
        patch that waits only for held work of frames of the set (or that no later instruction uses). The greatest such
        set; empty if none. A cell whose wait may end (a running script's lock, a running instruction, a search at its
        cap, a hold of a frame outside the set) frees that boundary, and the frame leaves the set. Under rule (a) a frame
        that has waited R29_KNOT_EVERY rounds in materialize at its witness for a patch to leave counts too, read at that
        boundary alone. The blockers are holds (`_r29_blockers`): a wait through a guard's
        hold frees the boundary here, and such a stall is censored. `wait`, the rounds a frame must have waited (0 at a static stop); `out`, a dict that a
        nonempty knot fills with its record (`_r29_edges`)."""
        cand, running = {}, None
        for vid in sorted(self._live_frames):
            fr = self._frames[vid]
            mat = getattr(self, "_r8", False) and fr.state == "materialize" and fr.target == fr.r8_wit and fr.r8_mat_waits >= wait
            if not mat and (fr.state != "decide" or fr.decide_waits < wait):
                continue                                                   # (a frame waiting in materialize at its witness
            if running is None:                                            #  is read at that boundary alone)
                running = self._active_cells_now()
            p, js = self._defer_current(fr), []
            hi = fr.top if fr.r8_cap is None else min(fr.top, fr.r8_cap)   # (a settled frame's effective cap; check,
            for j in ([fr.target] if mat else range(p, hi + 1)):           #  finding 1)
                if not fr.feas[j]:
                    continue
                cells = []
                for c_ in self._defer_workspace(fr, j):
                    active, busy = c_ in self._active_patches, c_ in running
                    if not active and not busy:
                        continue
                    if c_ in self._defer_locked or busy:
                        cells = None; break                                # it ends: this boundary can free
                    u = self._defer_first_unstarted(c_)
                    if u is None:
                        cells.append((c_, None, frozenset(["never"])))     # a patch no later instruction uses
                        continue
                    b = self._r29_blockers(u)
                    if not b:
                        cells = None; break                                # the search capped, or no frame holds it
                    cells.append((c_, u, frozenset(b)))
                js.append((j, cells))
            cand[vid] = (p, js)
        S = set(cand)
        while True:
            keep = set()
            for vid in S:
                # every boundary blocked by some cell whose blockers all lie in S (or "never")
                if all(cells is not None and cells and any(b <= (S | {"never"}) for _c, _u, b in cells) for _j, cells in cand[vid][1]):
                    keep.add(vid)
            if keep == S:
                if S and out is not None:
                    out.update(self._r29_edges(S, cand))
                return S
            S = keep

    @staticmethod
    def _r29_edges(S: set, cand: dict) -> dict:
        """the knot's record. knot: per frame its value, its current boundary and, per boundary it
        could still use, each blocking cell (one whose patch waits only on held work of frames in the knot, or "never":
        no later instruction uses it) with its next user and the frames that patch waits. edges: [frame, frame]
        from each frame to the frames its blocking patches wait for ("never" gives none). cycles: the edges' strongly
        connected sets that close a directed cycle (two frames or more, or one with an edge to itself), each sorted.
        never: the frames with a blocking patch marked "never". A nonempty knot need not hold a cycle."""
        knot, adj = [], {}
        for vid in sorted(S):
            p, js = cand[vid]
            bnd, to, nev = [], set(), False
            for j, cells in js:
                blk = [(c_, u, b) for c_, u, b in cells if b <= (S | {"never"})]
                bnd.append(dict(j=j, blocking=[dict(cell=list(c_), user=u, waits_for="never" if u is None else sorted(b))
                                                for c_, u, b in blk]))
                for _c, u, b in blk:
                    if u is None:
                        nev = True
                    else:
                        to |= b
            adj[vid] = to
            knot.append(dict(value=vid, boundary=p, boundaries=bnd, waits_for=sorted(to), never=nev))
        reach = {}
        for f in adj:
            seen, st = set(), list(adj[f])
            while st:
                g = st.pop()
                if g not in seen:
                    seen.add(g)
                    st.extend(adj[g])
            reach[f] = seen
        cycles = sorted({tuple(sorted(g for g in adj if g in reach[f] and f in reach[g])) for f in adj if f in reach[f]})
        return dict(knot=knot, edges=[[f, g] for f in sorted(adj) for g in sorted(adj[f])], cycles=[list(c) for c in cycles],
                    never=[x["value"] for x in knot if x["never"]])

    def _active_cells_now(self) -> set:
        """The cells the running instructions occupy (the dispatch's `patches_in_use`)."""
        out = set()
        for i in self._active_instructions:
            out.update(self.schedule_instructions[i].instruction.patches)
        return out

    def _r29_stop(self, outcome: str, **detail):
        if outcome == "deadlocked" and self._guard == "off":
            self._g_note("stall", "run")                                   # (OFF: a deadlock is its stall)
        r = self._r29
        L = self.ledger
        r["outcome"] = outcome
        r["round"] = self.current_round
        r["detail"] = detail
        # the bounds rule 29 records: the time (censored: the limit) and the exposure accumulated so far
        r["bounds"] = dict(time=detail.get("time_bound", self.current_round), all_patch_rounds=L.patch_rounds,
                           program_patch_rounds=L.role_patch_rounds.get("program", 0),
                           completed_instructions=self._completed_instruction_count, n_original=self.n_original)
        sim = LINK.get("simulator")
        if sim is not None:
            sim.failed = True                                              # the loop ends as on a clock timeout

    def r29_record(self, success: bool) -> dict | None:
        """The run's `stopping` record (None when rule 29 is off): completed, censored, deadlocked, or wall_limit (the
        job's clock ended it: an infrastructure guard, rerun once with a longer limit by the runner)."""
        r = self._r29
        if r is None:
            return None
        out = dict(rule=29, R_N=r["R_N"], factor=CENSOR_FACTOR, limit_rounds=r["limit"], knot_checks=r.get("knot_checks", 0))
        if r["outcome"] is not None:
            out.update(outcome=r["outcome"], round=r["round"], bounds=r["bounds"], **{k: v for k, v in r["detail"].items() if k != "time_bound"})
        elif success:
            out.update(outcome="completed")
        else:
            out.update(outcome="wall_limit", round=self.current_round, pending_last=r.get("pending_last"))
        return out

    # ------------------------------------------------------------------ candidate scans (round-2 speed, bit-identical)
    def _scan(self, cands: set, keep) -> list:
        """The values a per-round scan visits, in the order the old full scan visited them (vids ascend with insertion):
        the candidate set filtered by the scan's own condition `keep`; a candidate `keep` rejects has left the scan for
        good (every condition below is monotone: a value never returns to PENDING / LIVE / RECOVERING / unsettled /
        undelivered) and is dropped.  E2E_SCAN_CHECK=1 asserts the survivors equal the full scan over every value."""
        out = []
        for vid in sorted(cands):
            v = self.values[vid]
            if keep(v):
                out.append(v)
            else:
                cands.discard(vid)
        if self._scan_check:
            full = [v.vid for v in self.values.values() if keep(v)]
            assert full == [v.vid for v in out], ("candidate scan differs from the full scan", len(full), len(out))
        return out

    # ------------------------------------------------------------------ delivery queue (path delay, both arms)
    def _stamp_decodes(self, not_fully_decoded):
        now = self.current_round
        self._sw_observe(not_fully_decoded)
        for idx in sorted(self._completed_undecoded):                    # tracked, completed, not yet stamped
            if idx not in not_fully_decoded:
                self._decoded_round[idx] = now
                self._completed_undecoded.discard(idx)
                vid = self._vid_of_producer.get(idx)
                if vid is not None and self.values[vid].delivered_round < 0:
                    self._decoded_undelivered.add(vid)
        if self._scan_check:
            for idx in self._tracked:
                assert not (idx not in self._decoded_round and self.schedule_instructions[idx].end_round != -1 and idx not in not_fully_decoded), idx
        for v in self._scan(self._decoded_undelivered, lambda v: v.delivered_round < 0 and v.producer_idx in self._decoded_round):
            v.decoded_round = self._decoded_round[v.producer_idx]
            if now >= v.decoded_round + self.path:
                v.delivered_round = now
                self._round_events["delivered"].append(v)
                self._decoded_undelivered.discard(v.vid)
                self._settle_candidates.add(v.vid)
                self._replan(v)
        dec = LINK.get("decoder")                                          # rejected decode attempts: notified once, through the path
        if dec is not None:
            if self._r7 and getattr(dec, "_r7_rec", None) is None:
                dec._r7_rec = dict(started=0, marked=0, x=[])              # (round 7's records, 3(b): SWIPER's counters, excerpt)
            self._install_keyed_decoder(dec)
            if getattr(dec, "_scoutq_keyed", False):
                # the adapter's own record (the keyed `_RejectionCoin` decides every rejection and logs it at draw time);
                # upstream's `_missed_speculation_events` history exists only at lightweight_setting == 0, which campaign
                # runs never use, so consuming it left `rejected_notifications` at 0 (round-4 finding 2)
                by_round: dict = collections.defaultdict(set)
                for r, task_idx, _attempt in self._rejection_log[self._seen_rejected:]:
                    by_round[r].add(task_idx)
                self._seen_rejected = len(self._rejection_log)
                for r in sorted(by_round):
                    pw = by_round[r]
                    for v in self._rejection_scan(dec, pw):
                        v.notifications.append((r, r + self.path)); self.ledger.rejected_notifications += 1
                        if self.client == "swiper" and 0 <= v.t_emit <= r and v.rc_round < 0:
                            self._rc_pending.add(v.vid)                      # an own window reset after T_emit (SWIPER client)
            else:                                                            # a foreign (scripted) decoder: upstream's optional history
                events = dec._missed_speculation_events
                for r, poisoned in events[self._seen_missed:]:
                    pw = set(poisoned)
                    for v in self.values.values():
                        if v.delivered_round < 0 and pw & dec._instruction_tasks.get(v.producer_idx, set()):
                            v.notifications.append((r, r + self.path)); self.ledger.rejected_notifications += 1
                self._seen_missed = len(events)

    def _rejection_scan(self, dec, pw: set) -> list:
        """The values the rejected decode tasks `pw` notify -- undelivered, their producer covered by a task in pw -- in vid
        order. They are found from the tasks' own windows (the decoder files a task under every parent instruction of its
        window, decoder_manager.py), not by a scan of every value per rejection round, which was 17-21 % of a run (deploy 2's profile; bit-identical: the same values, and each one's notifications in the same order). A task the
        decoder does not hold falls back to the full scan. E2E_SCAN_CHECK=1 asserts the result equals the full scan."""
        keep = lambda v: v.delivered_round < 0 and bool(pw & dec._instruction_tasks.get(v.producer_idx, set()))
        tasks, cands = dec._tasks_by_idx, set()
        for t in pw:
            task = tasks[t] if 0 <= t < len(tasks) else None
            if task is None:
                cands = None
                break
            for i in task.window.parent_instr_idx:
                vid = self._vid_of_producer.get(i)
                if vid is not None:
                    cands.add(vid)
        if cands is None:
            out = [v for v in self.values.values() if keep(v)]
        else:
            out = [self.values[x] for x in sorted(cands) if keep(self.values[x])]
        if self._scan_check:
            full = [v.vid for v in self.values.values() if keep(v)]
            assert full == [v.vid for v in out], ("rejection scan differs from the full scan", len(full), len(out))
        return out

    def _delivered_instr(self, idx: int) -> bool:
        return idx in self._decoded_round and self.current_round >= self._decoded_round[idx] + self.path

    def _window_ident(self, task) -> tuple:
        """The COMPLETE, stable identity of a decoding window (round-2 findings A3 / B2): the spacetime volume it decodes
        -- every commit and buffer region as (cell, first round, rounds) -- together with the instructions that produced
        it, a spliced recovery instruction named by (owner value, boundary, index in the lowered script) rather than by
        the global index the splice happened to append it at.  No two distinct windows can share it (two windows never
        commit the same region) and the same window carries it in every configuration that produces it, which is what
        makes a latency draw independent of the order and the amount of decoding around it.  The buffers belong to the
        identity: they are part of the volume, hence of the latency class the sample is drawn from, so a window that
        commits the same rounds behind a different buffer is a different decoding problem and draws separately."""
        w = task.window
        def regions(rs):
            return tuple(sorted((tuple(r.patch), int(r.round_start), int(r.duration)) for r in rs))
        program = tuple(sorted(i for i in w.parent_instr_idx if i >= 0 and i not in self._recovery_local))
        recovery = tuple(sorted(self._recovery_local[i] for i in w.parent_instr_idx if i in self._recovery_local))
        return (regions(w.commit_region), regions(w.buffer_regions), program, recovery)

    def _decode_key_of(self, task, stream: str) -> tuple:
        """`_decode_key` without its collision bookkeeping: the same key, and nothing recorded."""
        ident = self._window_ident(task)
        return ("recovery_" + stream if ident[-1] else stream), hashlib.blake2s(repr(ident).encode(), digest_size=8).hexdigest()

    def _decode_key(self, task, stream: str) -> tuple:
        """Name the stream of one window: (namespace, digest of its complete identity).  A window carrying any of a
        value's recovery instructions uses the SEPARATE "recovery_" namespace, so a recovery's draws can never shift the
        program's.  The digest is a function of the WHOLE identity, so two different windows can only meet in one stream
        if that identity is incomplete: `decoder_key_collisions` counts exactly that and must be 0."""
        ident = self._window_ident(task)
        digest = hashlib.blake2s(repr(ident).encode(), digest_size=8).hexdigest()
        seen = self._decode_idents.setdefault(digest, ident)
        if seen != ident:
            self.ledger.decoder_key_collisions += 1
        return ("recovery_" + stream if ident[-1] else stream), digest       # ident[-1]: the recovery instructions in the window

    def _install_keyed_decoder(self, dec) -> None:
        """Key every random decision of the decoder by the identity of the window(s) it belongs to (round-1 finding 8,
        round-2 findings A3 / B2; design Sec. 4), upstream being read-only:

        - the LATENCY draw replaces the one-argument latency function.  Upstream removes the starting task from
          `_pending_decode_tasks` immediately before drawing its latency (decoder_manager.py:420-422), so a recording set
          hands the window to the draw; the stream is (namespace, identity digest, attempt number of THAT window).
        - the speculation-REJECTION coin (decoder_manager.py:162, the one `self.rng` draw of the decoder manager) is
          keyed by the identities of the two windows it decides about, through the `_RejectionCoin` stand-in below.

        Identical windows therefore draw identical numbers in N and Q whatever else the run does, and extra decoder work
        (a recovery's windows) shifts nothing."""
        base = getattr(dec, "decoding_time_function", None)
        if getattr(dec, "_scoutq_keyed", False) or not (hasattr(base, "draw_keyed") and hasattr(base, "draw_coin")):
            return                                                       # a foreign latency function (scripted tests): untouched
        dec._scoutq_keyed = True
        self._decode_sampler = base                          # kept for the summary: the table's refusal counter rides the ledger
        base.sampler_state.clock = lambda: dec._current_round  # (the drift step's clock, plan v2 5.3; inert when off)
        # a decoder cap (rule 7) gets a FIFO queue: upstream pops the pending windows from a set in hash order, which
        # under a cap starves old windows (fermi_hubbard_2_2_Kagome d21, cap 112: a ready window waited 2399 rounds while
        # 3324 younger ones were decoded, and the device stalled); uncapped, the order cannot matter and nothing changes
        pending = (_PendingDecodeTasksFIFO if dec.max_parallel_processes else _PendingDecodeTasks)(dec._pending_decode_tasks)
        if dec.max_parallel_processes:
            pending._dec = dec                                           # (the cap's shortcut reads the busy decoders)
            dec._pending_speculate_tasks = _PendingSpeculations(dec._pending_speculate_tasks)
        dec._pending_decode_tasks = pending
        if self._diag is not None and dec.max_parallel_processes:
            pending._diag_add = {}                                       # (records only: each window's last queue entry)
        if self._pool is not None or self._u_on or self._w2 is not None:  # part C: the pool's and U's decoder hooks
            dec._scout_dev = self                                          # (and the decision's: its busy count and reservations)
        if self._u_on or self._w2 is not None:
            pending.started_log = []                                     # (the windows that start: the pool's UPD events)
        if self._u_on:
            dec._decoded_unverified_tasks = _VerifyLog(dec._decoded_unverified_tasks)   # (the verifications, as events)
        attempts: dict = collections.defaultdict(int)
        fills = self._h_fill                                             # (--h-depth: None when off)
        tail = (self._cset or {}).get("couple") == "tail"                # (--pred-couple tail: `_tail_rank`; off, False)

        def keyed(volume: int) -> float:
            idx = pending.last_removed
            task = dec._get_task_or_none(idx) if idx is not None else None
            if task is None or task.decoding_start_time != dec._current_round:
                self.ledger.decoder_key_fallbacks += 1                   # must not happen; still deterministic
                key = ("decode_unidentified", "%d_%d" % (dec._current_round, volume))
            else:
                key = self._decode_key(task, "decode")
                if self._diag is not None:                               # (records only: the window's decode start)
                    self._diag.win_start(self, task, idx, (getattr(pending, "_diag_add", None) or {}).get(idx))
                if fills:                                               # an H's filler rounds carry no syndrome, so
                    volume -= _hd.window_fills(fills, task.window)       #  the draw reads the window's syndrome rounds
            attempts[key] += 1
            if tail and task is not None and key[0] != "decode_unidentified":
                u = self._tail_rank(task)                                # (a window holding a value's producer draws at
                if u is not None:                                        #  that value's coin's rank, every attempt)
                    return base.sampler_state.draw_at_rank(volume, u)
            return base.draw_keyed(volume, *key, attempts[key])

        dec.decoding_time_function = keyed
        dec.rng = _RejectionCoin(dec, self, base.draw_coin)

    # ------------------------------------------------------------------ contracts
    def _unresolved_others(self, vid: int):
        if self.arm == "Q":    # speed (bit-identical): arm Q's values become speculated only at its ACCEPT, which adds them
            return [w for w in self._scan(self._speculated_unsettled, lambda w: w.speculated and not w.settled) if w.vid != vid]
        return [w for w in self.values.values() if w.vid != vid and w.speculated and not w.settled]

    def _current_L(self, v: Value) -> set:
        c = self.contracts.get(v.vid)
        if c is None or not c.steps:
            return {v.wire}
        k = max(1, v.admitted)
        return set(c.steps[min(k, c.F) - 1].L) | {v.wire}

    def _current_D(self, v: Value) -> set:
        c = self.contracts.get(v.vid)
        if c is None or not c.steps or v.admitted == 0:
            return {v.result_id}
        s = c.steps[min(v.admitted, c.F) - 1]
        return set(s.D_fixed | s.D_cond)

    def _forget_contract(self, vid: int):
        """Drop a contract built for a registration that the engine did not take this round (instance pool full): the
        retry builds a fresh one against the work executed by then, and no membership map keeps the stale one alive."""
        if vid not in self.contracts:
            return
        c = self.contracts.pop(vid); L = self.ledger                       # the abandoned build's per-value statistics are reversed
        L.fence_reasons[c.fence_reason] = L.fence_reasons.get(c.fence_reason, 1) - 1
        L.F_hist[str(c.F)] = L.F_hist.get(str(c.F), 1) - 1
        for i in list(self._chain_of_instr):
            self._chain_of_instr[i] = [(x, k) for x, k in self._chain_of_instr[i] if x != vid]
        for i in list(self._fence_of_instr):
            self._fence_of_instr[i].discard(vid)
        self._fence_by_vid.pop(vid, None)

    def _ensure_contract(self, v: Value) -> Contract:
        if v.vid in self.contracts:
            return self.contracts[v.vid]
        others = self._unresolved_others(v.vid)
        other_res = frozenset(c_ for w in others for c_ in w.reserved)
        other_L = frozenset(w_ for w in others for w_ in self._current_L(w))
        other_D = frozenset(r for w in others for r in self._current_D(w))
        c = build_contract(self.es, self.sem, self.sched_view, self._specs[v.vid], bool(v.prediction), self.K,
                           other_reserved=other_res, other_L=other_L, other_D=other_D,
                           state=lambda i: 2 if self.schedule_instructions[i].end_round != -1 else (1 if self.schedule_instructions[i].start_round != -1 else 0))
        self.contracts[v.vid] = c
        for i, k in c.step_of.items():
            self._chain_of_instr[i].append((v.vid, k))
        for i in fence_set(self.es, c):
            self._fence_of_instr[i].add(v.vid)
            self._fence_rank.setdefault(i, len(self._fence_rank))
            self._fence_by_vid.setdefault(v.vid, set()).add(i)
        L = self.ledger
        L.fence_reasons[c.fence_reason] = L.fence_reasons.get(c.fence_reason, 0) + 1
        L.F_hist[str(c.F)] = L.F_hist.get(str(c.F), 0) + 1
        L.steps_refused_over_64 += c.refusals.get("over_64", 0)
        L.no_recovery_workspace_at_build += c.refusals.get("no_recovery_workspace", 0)
        L.held_overlap += c.refusals.get("held_overlap", 0)
        if not c.has_site:
            L.zero_only_sites += 1
        v.site_prices = list(c.prices)
        if self.codebook is not None and c.has_site:                    # the RTL holds at most 64 site classes (Sec. 2.4)
            cls = codebook_class(self.codebook.get(self.K) or self.codebook.get(str(self.K)) or [], c.site_class())
            if self._common is not None and v.vid not in self._common:  # outside the common set, the value WAITs as
                v.class_miss = True; self._common_outside.add(v.vid)    # on a class miss (counted apart from class misses)
            elif cls is None:
                v.class_miss = True; L.class_misses += 1
            else:
                v.site_prices = [0] + list(cls[2])                      # P3 = the class's prices (>= P1)
        return c

    def _publish_predictions(self):
        """A prediction of a value the ENGINE CAN HOLD is a REGISTER to it (queue depth 32 per round; a full 64-entry table
        rejects).  The engine can hold the value only if its contract has a site (a positive-duration atom, finding 5), its
        site class is in the configured codebook (round-2 finding A1: a class miss used to be REGISTERed all the same, so
        the policy built an unconfigured class), and the RTL can hold that class and a site-instance address for it
        (`ScoutPolicy.register`, hw/rtl/se_pkg.sv: 64 site classes, 1024 instances, the field widths).  Anything else
        consumes no queue slot, table entry, solve or policy line: the value simply WAITs for its definitive value
        (`class_miss_waits` / `site_limit_refusals`, holds counted as `held_class_miss`; design Sec. 2.4).

        A refusal the engine could take later -- all 1024 instance addresses live -- publishes nothing at all and is retried
        next round; the prediction itself is a keyed draw, so the retry uses the same prediction."""
        registered = 0
        live = self.policy.live_entries(self) if self.policy is not None else 0
        engine = self.policy is not None and self.arm == "Q"
        ready = []                                                        # (end_round, vid), ascending -- the old sort order
        for key in self._pending_ready:
            v = self.values[key[1]]
            if v.state is VState.PENDING and not v.delivered:
                ready.append(key)
        self._pending_ready = ready
        if self._scan_check:
            full = [(self.schedule_instructions[v.producer_idx].end_round, v.vid) for v in sorted(self.values.values(), key=lambda v: (self.schedule_instructions[v.producer_idx].end_round, v.vid))
                    if v.state is VState.PENDING and self.schedule_instructions[v.producer_idx].end_round != -1 and not v.delivered]
            assert full == ready, ("publish candidates differ from the full scan", len(full), len(ready))
        for key in ready:
            v = self.values[key[1]]
            if True:
                if self.client == "swiper":                               # SWIPER's decoded, unverified parity: T_emit + path
                    due = v.t_emit >= 0 and self.current_round >= v.t_emit + self.path
                elif self.client == "strip":                              # the strip decoder's parity: its publication + path
                    due = v.strip_pub >= 0 and self.current_round >= v.strip_pub + self.path
                else:
                    due = self.current_round >= self.schedule_instructions[v.producer_idx].end_round + self.client_latency
                if due:
                    if registered >= 32:
                        self.ledger.registrations_deferred += 1; continue
                    if engine and live >= 64:
                        self.ledger.registrations_rejected_full += 1; continue
                    if v.prediction is None:
                        forced = self._force_prediction.get(v.producer_event, self._force_prediction.get("*")) if self._side is None else None   # (41d: SWIPER's own)
                        if forced is None and self.client == "swiper":
                            v.prediction = v.truth != v.sw_wrong
                        elif forced is None and self.client == "strip":
                            v.prediction = v.truth != v.strip_wrong
                        elif forced is None:
                            if self._wrong_set is None:
                                flip = self.krng.uniform("pred", v.producer_event) < (1.0 - self.q_value)
                            else:
                                flip = v.producer_event in self._wrong_set
                            v.prediction = v.truth != flip
                        else:
                            v.prediction = bool(forced)
                    if self.client == "swiper":
                        v.confidence = self._adv_c(v.sw_q + (1.0 - v.sw_q) * (1.0 - self.f_flip), self._side is None)   # c = q + (1 - q)(1 - f_flip)
                        if v.vid in self._rc_pending and self._reset_visible(v):          # the reset notice is already in: REGISTER
                            v.confidence, v.rc_round = self._adv_c(1.0 - self.f_flip, self._side is None), self.current_round   # carries c' (no RC needed)
                            self._rc_pending.discard(v.vid); self._swc["published_reset"] += 1
                        v.c_pub = v.confidence
                    elif self.client == "strip":
                        v.confidence = v.c_pub = self._adv_c(self._strip_cpub(v))   # c = the q the value was drawn with
                    if self.build_contracts:
                        self._ensure_contract(v)
                    taken = True
                    if engine:
                        why = self.policy.register(self, v)               # REGISTER: the first solve is enqueued now
                        if why == "instance_pool_full":                   # nothing published: the client retries next round
                            self.ledger.instance_pool_full += 1
                            self._forget_contract(v.vid); continue        # the retry rebuilds it against that round's executed work
                        taken = why is None
                        if why == "class_miss":
                            self.ledger.class_miss_waits += 1
                        elif why == "site_limits":
                            self.ledger.site_limit_refusals += 1; v.class_miss = True   # the engine holds no entry: never speculate
                    v.predicted_round = self.current_round
                    if self.client == "swiper":
                        self._swc["published"] += 1
                    elif self.client == "strip":
                        self._strip_published += 1
                    self._round_events["predicted"].append(v); self._predicted_undelivered.add(v.vid)
                    v.state = VState.LIVE
                    self._live_undelivered.add(v.vid)
                    self._replan(v)
                    if taken:
                        registered += 1                                  # the N arm keeps the 32-per-round publication throttle
                        if engine:
                            live += 1
        if self._side is not None:
            self._side_publish()                                          # rule 41d: the decision client's side channel
        if self._plan_swiper:
            q = self._sw_plan_due                                         # rule 41d: SWIPER's prediction due for the planner
            while q and q[0][0] <= self.current_round:
                _r, vid = heapq.heappop(q)
                if not self.values[vid].delivered:
                    self._replan(self.values[vid])

    def _side_publish(self):
        """Rule 41d: the decision client's publications -- the strip client's at its publication + path, the modeled
        client's at the producer's end + its latency -- on a side channel that only U and H read (`_dview`); the device's
        own predictions are SWIPER's, as D-nci's.  The draws are the clients' own keyed streams ("strip", "pred")."""
        now, keep = self.current_round, []
        for key in self._side_ready:
            v = self.values[key[1]]
            if v.delivered or v.vid in self._side_pub:
                continue
            if self._side == "strip":
                due = v.strip_pub >= 0 and now >= v.strip_pub + self.path
            else:
                due = now >= self.schedule_instructions[v.producer_idx].end_round + self.client_latency
            if not due:
                keep.append(key)
                continue
            forced = self._force_prediction.get(v.producer_event, self._force_prediction.get("*"))
            if self._side == "strip":                                      # (F4: --force sets the strip client's prediction too,
                m = bool(forced) if forced is not None else v.truth != v.strip_wrong   #  at its round and confidence)
                c = self._adv_c(self._strip_cpub(v))
                self._strip_published += 1
            else:
                if forced is not None:
                    m = bool(forced)
                elif self._wrong_set is None:
                    m = v.truth != (self.krng.uniform("pred", v.producer_event) < (1.0 - self.q_value))
                else:
                    m = v.truth != (v.producer_event in self._wrong_set)
                c = self.confidence
            self._side_pub[v.vid] = (now, bool(m), float(c))
        self._side_ready = keep

    _adv_k = None                                                          # (--adv-err's k; None, off)
    _cset = None                                                           # (the strip client's settings; None, off)
    _ctrl_slow = None                                                      # (--ctrl-slow s: a Fraction; None, off)

    def _strip_cpub(self, v: Value) -> float:
        """the confidence the strip client publishes before --adv-err: the q it drew with, or under
        --pred-err-scale its nominal or matched confidence (`strip_c`)."""
        return v.strip_q if self._cset is None or self._cset.get("err") is None else v.strip_c

    def _strip_coin(self, v: Value) -> float:
        """The strip client's correctness coin of value v: one keyed uniform per producer event (and decoder salt); the
        prediction is wrong when it is at or above q (or 1 - e under --pred-err-scale)."""
        return self.krng.uniform("strip", *(() if self._strip_salt is None else (self._strip_salt,)), v.producer_event)

    def _tail_rank(self, task):
        """(`--pred-couple tail`, plan v2 3.3, F6), the tail-correlated stress: the rank at which a decode
        window that holds a value's producer instruction draws its decode time from the latency table (`draw_at_rank`),
        before any queueing. The rank is that value's correctness coin (`_strip_coin`), so the coin that makes its
        prediction wrong (coin >= q) is also the rank of every window its decoded outcome waits: a wrong prediction's
        verdict decodes come from the slowest (1 - q) of the table's samples, and each class keeps its accuracy exactly
        (the coin, hence which predictions are wrong, is unchanged). A window holding several values' producers takes
        the largest of their coins (counted, `tail_shared`). None for a window holding no producer (a recovery's
        included): its own keyed draw."""
        us = [self._strip_coin(self.values[vid]) for vid in
              sorted({self._vid_of_producer[i] for i in task.window.parent_instr_idx if i in self._vid_of_producer})]
        if not us:
            return None
        self._cstat["tail_windows"] += 1
        self._cstat["tail_shared"] += len(us) > 1
        return max(us)

    def _adv_c(self, c: float, on: bool = True) -> float:
        """(plan v2 5.8): the confidence the strip client (the accuracy-table model, c = the q it draws with) or the
        SWIPER client publishes for its calibrated c -- 1 - min(k (1 - c), 0.5) under --adv-err k, the advertised error k
        times the true one (capped at a coin's); c itself when off. Correctness is drawn as before (`strip_wrong`,
        `sw_wrong`), so only what the controllers read moves: SWIPER's c is left alone (on False) where the decision
        client reads the strip client's side channel (rule 41d, F_10's `--client strip --planner swiper`)."""
        return c if self._adv_k is None or not on else 1.0 - min(self._adv_k * (1.0 - c), 0.5)

    def _dview(self, v: Value) -> tuple:
        """The decision client's view of a value (rule 41d): (publication round or -1, m or None, c, revision round or -1)
        -- the side channel's when there is one, else the device's own."""
        if self._side is None:
            return v.predicted_round, v.prediction, v.confidence, v.rc_round
        x = self._side_pub.get(v.vid)
        return (x[0], x[1], x[2], -1) if x is not None else (-1, None, v.confidence, -1)

    def _reset_visible(self, v) -> bool:
        """A reset of one of the value's own windows decided at or after T_emit has reached the client (decision + path)."""
        return any(r >= v.t_emit and vis <= self.current_round for r, vis in v.notifications)

    def _swiper_reconfidence(self):
        """SWIPER client: the first reset notice of an own window after T_emit that reaches the client
        lowers the published value's confidence to c' = 1 - f_flip and, in Q, sends the engine RECONFIDENCE (RC). Once per
        value; a value delivered first needs none, a value not yet published takes c' with its REGISTER."""
        for vid in sorted(self._rc_pending):
            v = self.values[vid]
            if v.delivered or v.rc_round >= 0:
                self._rc_pending.discard(vid); continue
            if v.predicted_round < 0 or not self._reset_visible(v):
                continue
            self._rc_pending.discard(vid)
            self._rc_prev_c[vid] = v.confidence                            # (H, rule 41b': in use until the revision's deadline)
            v.rc_round, v.confidence = self.current_round, self._adv_c(1.0 - self.f_flip, self._side is None)
            self._swc["reconfidenced"] += 1
            if self.policy is not None and self.arm == "Q" and self.policy.reconfidence(self, v):
                self._swc["rc_messages"] += 1
            if self._u_on and self._side is None:
                self._u_reconfidence(v)                                    # U: RECONFIDENCE to the engine's entry

    # ------------------------------------------------------------------ RESOLVE / settlement
    def _settle_verified(self):
        for v in self._scan(self._settle_candidates, lambda v: v.delivered and v.state not in (VState.CONFIRMED, VState.RECOVERING, VState.RETIRED)):
            if self.arm == "D" and v.vid in self._frames:              # arm D: a deferred value resolves in _defer_phase
                continue
            c = self.contracts.get(v.vid)
            if self.policy is not None and self.arm == "Q" and v.predicted_round >= 0 and c is not None and c.has_site and not v.class_miss:
                self.policy.resolve(v)                                     # RESOLVE: job cancelled, table released, instance freed
            if not v.speculated:
                self._apply_branch(v, v.truth)
                if v.prediction is not None and v.prediction != v.truth:   # registered, never acted on, wrong: the engine's entry
                    v.state = VState.RECOVERING; self._recovering.add(v.vid)   # enters RECOVERING at boundary 0 until RECOVERY_DONE
                    v.recovery = dict(kind="none", round=self.current_round, instrs=[], P1=0, realised=0, step=0, frame=None)
                    v.contradiction_round = self.current_round
                else:
                    self._settle(v, VState.CONFIRMED)
            elif v.prediction == v.truth:
                self.ledger.confirmations += 1
                self._settle(v, VState.CONFIRMED)
                self._event("confirmed", v, "prediction correct at boundary %d" % v.admitted)
            else:
                v.state = VState.RECOVERING; self._recovering.add(v.vid); v.contradiction_round = self.current_round; self.ledger.contradictions += 1
                self._event("contradiction", v, "%s at boundary %d" % ("did S, none needed" if v.prediction else "skipped S, needed", v.admitted))

    def _settle(self, v: Value, state: VState):
        v.state = state; v.settled = True; v.settled_round = self.current_round
        if state is VState.CONFIRMED:
            # C6 fix: RESOLVE retires the chain (job cancelled, instance freed), so its pending atom timers are
            # void -- a settled value is no longer protected by the overlap invariant, and another value's chain may hold
            # a trailing member of its in-flight atom (fermi_hubbard_4_4 d17, value 1873).  RECOVERING keeps its timers.
            self._timer_checked.update((v.vid, k) for k in range(v.admitted))
        self._release(v)

    def _step_complete(self, v: Value, k: int) -> bool:
        """Step k (0-based) of the value's contract has completed: members done and the timer elapsed."""
        c = self.contracts[v.vid]
        s = c.steps[k]
        for i in s.instrs:
            t = self.schedule_instructions[i]
            if t.end_round != -1:
                continue
            # a zero-duration member (a cleanup DISCARD) whose predecessors are complete is stamped in this round's dispatch
            if self._instruction_durations[i] != 0 or any(self.schedule_instructions[p].end_round == -1 and self._instruction_durations[p] != 0
                                                           for p in self._adj()[0].get(i, ())):
                return False
        return self.current_round >= v.accept_round[k] + s.duration

    def _advance_recoveries(self):
        for v in self._scan(self._recovering, lambda v: v.state is VState.RECOVERING):
            if v.recovery is None:                                         # waiting for the in-flight atom
                if v.admitted > 0 and not self._step_complete(v, v.admitted - 1):
                    continue
                self._splice_recovery(v)
            r = v.recovery
            if r["kind"] == "none" or r["kind"] == "frame":
                if r["round"] < self.current_round:                        # RECOVERY_DONE arrives a round later at the earliest
                    self._finish_recovery(v)
            elif all(self.schedule_instructions[i].end_round != -1 for i in r["instrs"]) and all(self._delivered_instr(i) for i in r["instrs"]):
                self._finish_recovery(v)

    def _finish_recovery(self, v: Value):
        r = v.recovery
        if r["kind"] in ("script", "frame"):
            self._apply_recovery_frame(v, r)
        if r["kind"] not in ("none",):
            end = max([self.schedule_instructions[i].end_round + 1 for i in r["instrs"]] or [r["round"]])
            r["recovery_end_round"] = end
            n_prog = self._program_cells(v)
            r["P2_settle_patch_rounds"] = (self.current_round - end) * n_prog
            self.ledger.P2_settle_patch_rounds += r["P2_settle_patch_rounds"]
        r["done_round"] = self.current_round
        self._round_events["recovered"].append(v)
        self._settle(v, VState.RETIRED)
        self._event("recovered", v, "recovery complete (%s), settled" % (
            r["kind"] if r["kind"] != "script" else "script, realised %d vs encoded P1 %d" % (r["realised"], r["P1"])))

    def _apply_recovery_frame(self, v: Value, r: dict):
        """At recovery completion the contract step's complete final frame (`Step.frame_after`: Q^dagger P^dagger Q composed
        with the C3 script's own frame members) is applied as a RECORDED frame update in the ledger for the value: frames
        cost no physical work and are never dropped (finding 1, device side)."""
        check_recovery_frame_record(r)
        fr = r["frame"]
        L = self.ledger
        L.recovery_frames_applied += 1
        L.recovery_frames_nonidentity += 1 if fr["nonempty"] else 0
        L.frame_log.append((self.current_round, v.vid, r["step"], fr["pauli"], [tuple(x) for x in fr["cond"]]))
        self._event("frame", v, "recovery frame applied: %s%s" % (fr["pauli"], "".join(" |%s:%s" % (b, g) for b, g in fr["cond"])))

    def _program_cells(self, v: Value) -> int:
        return len({c for w in self._current_L(v) if self.es.roles_by_wire.get(w) == "program" for c in self.es.wire_cell(w, self.es.events[v.producer_event].event_id)}) or 1

    def _bind_recovery_cells(self, v: Value, k: int, step):
        """Round-8 review (K21, A1a) / round-9, defence in depth at the splice: the instance's
        program operands must be EXACTLY the program cells of the residual this boundary's script was lowered for
        (`Semantics.lower` places the C3 members on `step.residual.axis.wires`' patches), under the same wire ->
        cell map that bound the S target at discovery -- so the two are compared like with like. K21 asked instead
        for the discovered S target to be among them, which Clifford propagation can carry off the T's own wire
        (T q0; CNOT q1->q0; CNOT q0->q1: Z0 -> Z0 Z1 -> Z1 recovers on q1 alone), and valid recoveries died here.
        A frame-only recovery has no instance and is exempt; a backend without a converter carries no document.
        Fail closed with a RuntimeError: the run has no result, rather than a recovery on the wrong wire. (round
        9): a routed UR list (contract.RECOVERY_MODE "ur_routed") was lowered on its own wires (`Lowered.wires`: the
        kept gadgets' and the producer's, a superset of the residual's support), so those are its program operands. of those, the wires its members name (`lowered_program_wires`)."""
        low = step.recovery
        if low.events is None or not low.instrs:
            return
        prog_cells = {tuple(d["cell"]) for d in low.events.get("lifetimes", {}).values()
                      if d.get("role") == "program" and tuple(d.get("cell") or ()) in set(low.cells)}
        support = {self._cell_of_wire(w, step.at_event) for w in lowered_program_wires(low, step.residual)}
        if prog_cells != support:
            raise RuntimeError("recovery of value %d (%s) at boundary %d splices onto program cells %s, the "
                               "residual %s it was lowered for is supported on wires %s at cells %s"
                               % (v.vid, v.gadget_id, k, sorted(prog_cells), step.residual_key,
                                  sorted(step.residual.axis.wires), sorted(support)))

    def _splice_recovery(self, v: Value):
        """Splice the LOWERED recovery script of the current boundary on its reserved cells (design Sec. 2.3): predecessors
        are the admitted members (complete by now), successors every unstarted instruction the value fences or that touches
        the script's cells; then the frame (bookkeeping).  Settlement follows when the script's results are delivered.
        `_bind_recovery_cells` first binds the instance's program operands to this boundary's residual support."""
        c = self.contracts[v.vid]
        k = v.admitted
        assert k >= 1, (v.vid, k)
        step = c.steps[k - 1]
        low = step.recovery
        stall = self.current_round - v.contradiction_round
        n_prog = self._program_cells(v)
        fa = step.frame_after                                   # the complete final frame, recorded at recovery completion
        frame_rec = dict(pauli=fa[0].key(), cond=[[b, g.key()] for b, g in fa[1] if not g.is_identity()],
                         nonempty=(not fa[0].is_identity()) or any(not g.is_identity() for _, g in fa[1]))
        rec = dict(round=self.current_round, step=k, P1=step.price_P1, residual=step.residual_key, frame=frame_rec,
                   stall_rounds=stall, P2_stall_patch_rounds=stall * n_prog, instrs=[], realised=0)
        self.ledger.P2_stall_patch_rounds += rec["P2_stall_patch_rounds"]
        if not low.instrs:
            rec["kind"] = "frame"; v.recovery = rec; self.ledger.recoveries_frame_only += 1
            self._event("recovery", v, "boundary %d: frame-only recovery %s" % (k, step.residual_key)); return
        self._bind_recovery_cells(v, k, step)
        cls_ = self._script_class(c, low, step.residual, k, False)        # (records only)
        dec = LINK.get("decoder")
        base = len(self.schedule_instructions)
        new = []
        prog = {self._cell_of_wire(w, step.at_event) for w in (low.wires or step.residual.axis.wires)}   # (UR: its list's wires)
        anc = frozenset() if self.reservation else frozenset(low.cells) - prog
        for x in low.instrs:
            ins = Instruction(x.name, base + x.idx, x.patches, x.duration, merge_faces=x.merge_faces, group_name="RECOVERY")
            self.schedule_instructions.append(InstructionTask(ins.idx, ins, -1, -1))
            dur = Duration.get_true_duration(ins.duration, self.d_t)
            self._true_durations.append(dur); self._instruction_durations.append(dur); self._plan_durations.append(dur)
            self._is_startup_instruction.append(False)
            # Q-nores: an ancilla cell that holds an ordinary patch now is freed before the recovery runs on it (`_nores_order`)
            self._patches_initialized_by_instr.append(set(ins.patches) - (set(self._active_patches) - anc))
            self.schedule_dag.add_node(ins.idx, duration=dur)
            if dec is not None: dec.instruction_idx_dag.add_node(ins.idx)
            self._recovery_instrs.add(ins.idx); self._recovery_owner[ins.idx] = v.vid; self._tracked.add(ins.idx)
            self._recovery_class[ins.idx] = cls_                         # (records only)
            self._recovery_event[ins.idx] = "speculation"                 # (records only)
            self._recovery_local[ins.idx] = (v.vid, k, x.idx)             # the script-local name a decode window keys by (findings A3 / B2)
            new.append(ins)
        if self._strip_splices_on:
            self._strip_splice(low, new, "recovery")
        if self._trace_on:
            self._trace_splice(v.vid, k, low, new, "recovery")

        def edge(a, b, release=False):
            # Every consumer of the dependency graph reads the neighbour lists (`_adj`): the dispatch, the atom-readiness and
            # step-completion tests, the start-time prediction, the decoder's descendant walk and the end-of-run acyclicity
            # check.  The release -> later-user edges (one per unstarted user of a released cell: ~10^5 per recovery on a
            # 200k-instruction program) are therefore kept in the lists ONLY -- in the two networkx graphs they cost ~100 MB
            # per recovery (grover Q d=21 measured 25 GB after 256 recoveries) -- while the recovery's own edges
            # (predecessors -> firsts, internal order) still go to the graphs, where the per-splice cycle check reads them.
            if not self._add_edge_cached(a, b, fresh=release):            # an existing pair: networkx would change nothing either
                return
            if not release:
                self.schedule_dag.add_edge(a, b, weight=self._instruction_durations[a])
            if dec is not None:
                if not release: dec.instruction_idx_dag.add_edge(a, b)
                dec.note_edge(a, b, in_graph=not release)                    # its neighbour and seen-successor lists stay exact
        for i, x in enumerate(new):                                        # internal order: the .lss rule (shared patch)
            for j in range(i):
                if set(new[j].patches) & set(x.patches):
                    edge(new[j].idx, x.idx)
        firsts = [x.idx for x in new if not any(set(new[j].patches) & set(x.patches) for j in range(new.index(x)))]
        lasts = [x.idx for i, x in enumerate(new) if not any(set(new[j].patches) & set(x.patches) for j in range(i + 1, len(new)))]
        preds = {i for i in c.admitted_instrs(k)}
        for p in preds:                                                    # complete, or a zero-duration cleanup stamped this round
            assert self.schedule_instructions[p].end_round != -1 or self._instruction_durations[p] == 0, p
            for f in firsts:
                edge(p, f)
        succs = set(self._fence_set_of(v)) | {i for s in c.steps[k:] for i in s.instrs}
        succs |= {j for j in range(self.n_original) if self.schedule_instructions[j].start_round == -1 and self.schedule_instructions[j].end_round == -1
                  and set(self.schedule_instructions[j].instruction.patches) & set(low.cells)}
        succs = {j for j in succs if self.schedule_instructions[j].start_round == -1 and self.schedule_instructions[j].end_round == -1} - preds
        if not self.reservation:
            rec["nores_prog"] = sorted(prog)
            succs = self._nores_order(v, rec, new, anc, succs, edge)
        # A release edge li -> s_ can close a cycle only if s_ reaches a spliced instruction, i.e. an admitted member (the
        # only entries into the new nodes are the preds -> firsts edges).  A started instruction has complete predecessors
        # (blocked() requires them), so a path from the unstarted s_ to a member runs through unstarted nodes only: walk the
        # unstarted ancestry of the pending members and refuse the splice exactly where the networkx check refused it when
        # these edges were in the graph (round 3d, reviewer D).
        unstarted_anc = set(); stack = [p for p in preds if self.schedule_instructions[p].start_round == -1]
        while stack:
            x = stack.pop()
            for q in self._adj()[0].get(x, ()):
                if self.schedule_instructions[q].start_round == -1 and q not in unstarted_anc:
                    unstarted_anc.add(q); stack.append(q)
        bad = unstarted_anc & succs
        if bad:
            raise RuntimeError("recovery of value %d created a cycle through its release edges: %s" % (v.vid, sorted(bad)[:8]))
        for li in lasts:
            for s_ in succs:
                edge(li, s_, release=True)
        for f in firsts:
            self._frontier_add(f)
        # realised intrinsic cost is NOT computed here: it accumulates from the executed syndrome rounds attributed to
        # these instructions in `_generate_syndrome_round` (finding 4), so `recovery_price_mismatch` compares the encoded
        # P1 of the placed instance against what was physically run, with stalls / settlement kept in P2
        rec.update(kind="script", instrs=[x.idx for x in new], cells=sorted(low.cells))
        if low.c3_key is not None:                                         # (sx library only): the lowering's binding and form
            rec.update(c3_key=low.c3_key, c3_digest=low.c3_digest, **({"form": low.form} if low.form is not None else {}))
        v.recovery = rec
        self.ledger.recoveries_spliced += 1; self.ledger.P1_total += step.price_P1
        self._event("recovery", v, "boundary %d: %d instructions on %s, encoded P1 %d, stall %d rounds"
                    % (k, len(new), sorted(low.cells), step.price_P1, stall))
        if os.environ.get("E2E_DEBUG"):
            print("RECOVERY round %d value %d boundary %d residual %s instrs %s preds %s succs %s" % (self.current_round, v.vid, k, step.residual_key, rec["instrs"], sorted(preds), sorted(succs)), flush=True)
        if not nx.is_directed_acyclic_graph(self.schedule_dag):
            raise RuntimeError("recovery of value %d created a cycle: %s" % (v.vid, nx.find_cycle(self.schedule_dag)))

    def _fence_set_of(self, v: Value) -> set:
        """speed (bit-identical): the value's fence from its index, built in `_fence_of_instr`'s key order as the old
        scan of every key built it, so the set iterates as before. E2E_SCAN_CHECK=1 asserts it against that scan."""
        out = {i for i in sorted(self._fence_by_vid.get(v.vid, ()), key=self._fence_rank.__getitem__)}
        if self._scan_check:
            full = {i for i, vids in self._fence_of_instr.items() if v.vid in vids}
            assert list(out) == list(full), ("fence index differs from the full scan", v.vid, len(out), len(full))
        return out

    def _nores_order(self, v: Value, rec: dict, new: list, anc: frozenset, succs: set, edge) -> set:
        """Q-nores: no cell was reserved for this recovery, so ordinary patches may sit on its ancilla cells. It
        allocates them on demand: every ordinary patch on one of them is freed first.
        - Blockers: on each occupied ancilla cell, its unstarted users up to the DISCARD that frees it; their unstarted
          ancestors; and, on every ancilla cell a blocker uses, the users up to the DISCARD at or after the last blocker
          there (a patch a blocker leaves would sit under the recovery) -- to a fixpoint. None of them waits for the
          recovery. (A cell's users run in index order, the .lss rule, so its unstarted users are a suffix of its list.)
        - An edge from each such DISCARD to the recovery's first instruction on its cell; an earlier recovery still on a
          cell finishes first. Later users of the cells wait for the recovery, as with a reservation (the release edges).
        - A blocker the value itself holds would deadlock: that fails loudly, so a run keeps its checks clean or has no
          result. Returns the successors."""
        N = self._nores
        if self._instrs_on_cell is None:
            self._instrs_on_cell = collections.defaultdict(list)
            for i in range(self.n_original):
                for p in self.schedule_instructions[i].instruction.patches:
                    self._instrs_on_cell[p].append(i)
        first_on = {}
        for x in new:
            for p in x.patches:
                first_on.setdefault(p, x.idx)
        tasks = self.schedule_instructions
        unstarted = lambda j: tasks[j].start_round == -1 and tasks[j].end_round == -1
        ptr = {}
        for c_ in anc:                                                     # each cell's first unstarted user
            lst, q = self._instrs_on_cell.get(c_, ()), self._cell_ptr.get(c_, 0)
            while q < len(lst) and not unstarted(lst[q]):
                q += 1
            self._cell_ptr[c_] = ptr[c_] = q
        other = set()                                                      # cells another unfinished recovery is on
        for c_ in anc:
            prev = self._nores_cells_last.get(c_)
            if prev is not None and tasks[prev].end_round == -1:
                other.add(c_); edge(prev, first_on[c_]); N["recovery_edges"] += 1
        top = {c_: ptr[c_] for c_ in anc if c_ in self._active_patches and c_ not in other}   # cell -> scan from here
        blockers, cut, todo = set(), {}, []

        def block(j):
            if j not in blockers:
                blockers.add(j); todo.append(j)

        while True:
            for c_, start in list(top.items()):
                lst = self._instrs_on_cell.get(c_, ())
                k = next((n for n in range(start, len(lst)) if tasks[lst[n]].instruction.name == "DISCARD"), None)
                if k is None:
                    raise RuntimeError("Q-nores: value %d's recovery needs cell %s, whose patch no unstarted DISCARD frees" % (v.vid, c_))
                if cut.get(c_, -1) < k:
                    for n in range(ptr[c_], k + 1):
                        block(lst[n])
                    cut[c_] = k
            if not todo:
                break
            while todo:                                                    # the blockers' unstarted ancestry, and their cells
                x = todo.pop()
                for q in self._adj()[0].get(x, ()):
                    if unstarted(q):
                        block(q)
                for p in tasks[x].instruction.patches:
                    if p in anc and p not in other:
                        lst = self._instrs_on_cell.get(p, ())
                        n = bisect.bisect_left(lst, x)
                        if n < len(lst) and lst[n] == x and n > cut.get(p, -1) and n >= top.get(p, -1):
                            top[p] = n                                     # free the cell at the DISCARD at or after x
        for c_, k in cut.items():
            edge(self._instrs_on_cell[c_][k], first_on[c_])
        held = set(self._fence_set_of(v)) | {i for s_ in self.contracts[v.vid].steps[v.admitted:] for i in s_.instrs}
        bad = blockers & held
        if bad:
            raise RuntimeError("Q-nores: value %d's recovery waits for cells whose release needs work the value holds: %s" % (v.vid, sorted(bad)[:8]))
        for x in new:
            for p in x.patches:
                self._nores_cells_last[p] = x.idx
        for i in set(first_on.values()):
            self._nores_pending[i] = (v.vid, self.current_round)
        occupied = [c_ for c_ in cut if c_ in self._active_patches]
        N["recoveries"] += 1; N["holder_instructions"] += len(blockers)
        if cut or other:
            N["recoveries_waited"] += 1; N["cells_occupied"] += len(occupied)
        rec["nores"] = dict(cells_occupied=len(occupied), cells_ordered=len(cut), holders=len(blockers), after_recoveries=len(other))
        return succs - blockers

    def _nores_started(self, idx: int):
        """Q-nores at a recovery instruction's start: the cell invariant (it never lands on an ancilla cell that holds another
        allocation's patch; a violation fails loudly) and the recovery's wait, from the splice to its first start."""
        owner = self._recovery_owner.get(idx)
        v = self.values[owner]
        rec = v.recovery
        prog = set(map(tuple, rec.get("nores_prog", ())))
        for p in self.schedule_instructions[idx].instruction.patches:
            if tuple(p) in prog:
                continue
            o = self._nores_owner.get(p)
            if p in self._active_patches and o != owner:
                raise RuntimeError("Q-nores: recovery instruction %d of value %d starts on cell %s, which holds another patch (owner %s)" % (idx, owner, p, o))
            self._nores_owner[p] = owner
        if self.schedule_instructions[idx].instruction.name == "DISCARD":
            for p in self.schedule_instructions[idx].instruction.patches:
                if self._nores_owner.get(p) == owner:
                    del self._nores_owner[p]
        first = self._nores_pending.pop(idx, None)
        if first is not None and "nores_wait_rounds" not in rec:
            wait = self.current_round - first[1]
            rec["nores_wait_rounds"] = wait
            N = self._nores
            N["wait_rounds"] += wait; N["wait_patch_rounds"] += wait * self._program_cells(v); N["max_wait_rounds"] = max(N["max_wait_rounds"], wait)
            for j in [j for j, x in self._nores_pending.items() if x[0] == owner]:
                del self._nores_pending[j]

    # ------------------------------------------------------------------: resource sensitivities
    # Every knob is off by default and then changes nothing (the occupancy peak is only recorded).  Both are the same for
    # every arm; states the models.
    #   workspace_cap W: at most W non-program cells occupied at a time -- every role but "program" (routing, resource,
    #     mediator, busy regions, recovery and materialization scripts; reserved cells that no patch occupies do not count).
    #     A positive-duration instruction that would newly occupy k such cells starts only if the occupied ones, plus the
    #     part of the accepted steps' claims not yet occupied, plus k stay <= W; else it waits ("workspace_cap").  A Q step
    #     runs on its encoded schedule from ACCEPT (the timer invariant), so its ACCEPT claims the peak its members will
    #     occupy and waits for that ("ws_accept"); its members then start without the check.  Deadlock valve: when nothing
    #     runs and nothing can change by waiting (`_ws_deadlocked`) while instructions wait for the cap, the lowest-numbered
    #     of them starts beyond W (counted: `workspace_cap_overrides`; its cells are the only ones allowed beyond W).
    #     (W-E, plan v2 7.3, F13), --cap-hard ({"W": W, "hard": True}): a hard limit, under either reservation and
    #     without one.  Occupancy is the union of occupied and held non-program cells (a held cell that is occupied counts
    #     once); the valve is off (`_ws_hard_refuse`: a run that can make no progress stops as rule 29's static deadlock,
    #     recorded `stalled_under_cap`); a registration, advance or revision is admitted only if that union, every live
    #     frame's held settlement footprint in it, stays <= W after it (`_ws_hard_exceeds`); an ordinary step still waits.
    #   t_supply {period P, buffer B, start}: the factories deliver one T state every P rounds (exact rational accrual,
    #     usable from the first round the accrued production reaches a whole state) into a buffer of B states; production
    #     pauses while the buffer is full.  The buffer starts full ("full") or empty.  A program INJECT_T starts only with a
    #     state in the buffer and takes it ("magic"); a Q step takes its injections' states at ACCEPT ("magic_accept").
    def _sens_setup(self, workspace_cap, t_supply):
        self._ws_hard = isinstance(workspace_cap, dict)                    # (--cap-hard: {"W": W, "hard": True}; else as)
        if self._ws_hard:
            assert workspace_cap == dict(W=workspace_cap.get("W"), hard=True) and int(workspace_cap["W"]) >= 1, workspace_cap
            workspace_cap = workspace_cap["W"]
        self.workspace_cap = int(workspace_cap) if workspace_cap else None
        assert self.workspace_cap is None or self.workspace_cap >= 1, workspace_cap
        self._np_peak = 0                            # peak occupied non-program cells (recorded in every run)
        self._np_set: set = set()                    # occupied non-program cells: rebuilt every syndrome round, kept in the dispatch
        self._ws_role_cache: dict = {}
        self._ws_claims: dict = {}                   # (vid, k) -> [claim, members, cells they occupied]: accepted Q steps in flight
        self._ws_member: dict = {}                   # member instruction -> (vid, k)
        self._ws_override_cells: set = set()         # cells the deadlock valve let in (occupied beyond W while they live)
        self._ws_overriding = False
        self._sens = collections.Counter()
        self.t_supply = None
        if t_supply:
            P, B, start = Fraction(str(t_supply["period"])), int(t_supply.get("buffer", 1)), str(t_supply.get("start", "full"))
            assert P > 0 and B >= 1 and start in ("full", "empty"), t_supply
            self.t_supply = dict(period=P, buffer=B, start=start)
            self._ts_level = Fraction(B) if start == "full" else Fraction(0)
            self._ts_round = 0
            self._ts_prepaid: set = set()            # injections of accepted Q steps whose state was taken at ACCEPT
            for i in list(self._active_instructions):                     # upstream's constructor started the first one
                if self._ts_is_injection(i):
                    self._ts_take()
        self._sens_on = self.workspace_cap is not None or self.t_supply is not None

    def _ts_is_injection(self, idx: int) -> bool:
        return idx < self.n_original and self.schedule_instructions[idx].instruction.name == "INJECT_T"

    def _ts_take(self):
        self._ts_level -= 1
        self._sens["t_states_taken"] += 1
        if self._ts_level < 0:
            self._sens["t_supply_violations"] += 1                         # impossible by construction: a run check

    def _sens_round(self):
        """Once per dispatch: this round's T-state production; the claims of accepted steps whose members all ended."""
        if self.t_supply is not None and self.current_round > self._ts_round:
            B = Fraction(self.t_supply["buffer"])
            before = self._ts_level
            self._ts_level = min(B, self._ts_level + Fraction(self.current_round - self._ts_round) / self.t_supply["period"])
            if before + Fraction(self.current_round - self._ts_round) / self.t_supply["period"] > B:
                self._sens["t_supply_full_rounds"] += 1                    # production paused: the buffer was full
            self._ts_round = self.current_round
        if self._ws_claims:
            si = self.schedule_instructions
            for key in [k_ for k_, cl in self._ws_claims.items() if all(si[i].end_round != -1 for i in cl[1])]:
                for i in self._ws_claims.pop(key)[1]:
                    self._ws_member.pop(i, None)

    def _ws_role(self, idx: int, p) -> str:
        key = (idx, p)
        r = self._ws_role_cache.get(key)
        if r is None:
            r = self._ws_role_cache[key] = self._role_for(SimpleNamespace(patch=p, instruction_idx=idx))
        return r

    def _ws_new_cells(self, idx: int) -> set:
        """The non-program cells instruction idx would newly occupy if it started now."""
        held = self._ws_held() if self._ws_hard else ()                    # (--cap-hard: a held cell is counted already)
        return {p for p in self.schedule_instructions[idx].instruction.patches
                if p not in self._active_patches and p not in self._np_set and p not in held and self._ws_role(idx, p) != "program"}

    def _ws_occupied(self) -> int:
        """Occupied non-program cells plus the part of the accepted steps' claims their members do not occupy yet.,
        --cap-hard: the occupied cells are the union of the occupied and the held non-program cells."""
        n = len(self._np_set | self._ws_held()) if self._ws_hard else len(self._np_set)
        for claim, _members, cells in self._ws_claims.values():
            n += max(0, claim - len(cells & self._np_set))
        return n

    def _ws_held(self) -> set:
        """(--cap-hard): the non-program cells live frames hold (the reservation's held cells; none without one)."""
        return {c_ for c_ in (self._resv_held or ()) if self._role_of_patch.get(c_) != "program"}

    def _ws_hard_exceeds(self, cells) -> bool:
        """(plan v2 7.3, F13: settlement's capacity reserved): under --cap-hard, True if taking `cells` (a
        registration's, an advance's or a revision's footprint) would put the union of occupied and held non-program cells,
        with the accepted steps' unoccupied claims, above W. Off, or without the cap: False."""
        if not self._ws_hard or self.workspace_cap is None:
            return False
        u = self._np_set | self._ws_held()
        add = {c_ for c_ in cells if c_ not in u and self._role_of_patch.get(c_) != "program"}
        if self._ws_occupied() + len(add) > self.workspace_cap:
            self._sens["cap_hard_admissions_refused"] += 1
            return True
        return False

    def _ws_hard_refuse(self) -> bool:
        """under --cap-hard deadlock valve is off: True, and the round it would have let
        an instruction start beyond W is counted (`cap_hard_escape_refused`, the first such round kept). The run then
        waits; with nothing pending rule 29 stops it as a static deadlock (`stalled_under_cap`). Off: False."""
        if not self._ws_hard:
            return False
        if not self._sens["cap_hard_escape_refused"]:
            self._sens["cap_hard_first_refused_round"] = self.current_round
        self._sens["cap_hard_escape_refused"] += 1
        return True

    def _ws_claim(self, members) -> int:
        """The peak number of non-program cells a step's unstarted members newly occupy together, on the step's own schedule
        from ACCEPT: each member as soon as its predecessors in the step end, a zero-duration DISCARD freeing its cells as it
        runs (before the starts of the same round, as in the dispatch)."""
        si = self.schedule_instructions
        ms = sorted(i for i in members if si[i].start_round == -1 and si[i].end_round == -1)
        mset, t0, end = set(ms), {}, {}
        for i in ms:                                                       # index order is a topological order
            t0[i] = max([end[p] for p in self._adj()[0].get(i, ()) if p in mset] or [0])
            end[i] = t0[i] + self._instruction_durations[i]
        active = set(self._active_patches) | self._np_set
        mine, cur, peak = set(), 0, 0
        for i in sorted(ms, key=lambda i: (t0[i], self._instruction_durations[i] > 0, i)):
            pts = set(si[i].instruction.patches)
            if self._instruction_durations[i] == 0:
                if si[i].instruction.name == "DISCARD":
                    cur -= len(mine & pts); mine -= pts; active -= pts
                continue
            new = {p for p in pts if p not in active and self._ws_role(i, p) != "program"}
            mine |= new; active |= pts; cur += len(new); peak = max(peak, cur)
        return peak

    def _sens_block(self, idx: int, skip_cap: bool = False) -> str | None:
        """The knobs' gate of one instruction that could otherwise start now (None: it may start)."""
        if self._instruction_durations[idx] == 0:
            return None                                                    # occupies nothing, injects nothing
        if self.t_supply is not None and self._ts_is_injection(idx) and idx not in self._ts_prepaid and self._ts_level < 1:
            return "magic"
        if self.workspace_cap is not None and not skip_cap and idx not in self._ws_member:
            k = len(self._ws_new_cells(idx))
            if k and self._ws_occupied() + k > self.workspace_cap:
                return "workspace_cap"
        return None

    def _sens_start(self, idx: int):
        if self._instruction_durations[idx] == 0:
            return
        if self.t_supply is not None and self._ts_is_injection(idx):
            if idx in self._ts_prepaid:
                self._ts_prepaid.discard(idx)
            else:
                self._ts_take()
        if self.workspace_cap is not None:
            new = self._ws_new_cells(idx)
            if new:
                self._np_set |= new
                key = self._ws_member.get(idx)
                if key is not None:
                    self._ws_claims[key][2].update(new)
                if self._ws_overriding:
                    self._ws_override_cells |= new
                if len(new) > self._sens["max_allocation"]:
                    self._sens["max_allocation"] = len(new)             # the largest single allocation (cells)

    def _sens_accept_block(self, step) -> str | None:
        if self.t_supply is not None:
            inj = [i for i in step.instrs if self._ts_is_injection(i) and self.schedule_instructions[i].start_round == -1
                   and self.schedule_instructions[i].end_round == -1 and i not in self._ts_prepaid]
            if inj and self._ts_level < len(inj):
                return "magic_accept"
        if self.workspace_cap is not None:
            need = self._ws_claim(step.instrs)
            if need and self._ws_occupied() + need > self.workspace_cap:
                return "ws_accept"
        return None

    def _sens_accept(self, v: Value, k: int, step):
        if self.t_supply is not None:
            for i in step.instrs:
                if (self._ts_is_injection(i) and self.schedule_instructions[i].start_round == -1
                        and self.schedule_instructions[i].end_round == -1 and i not in self._ts_prepaid):
                    self._ts_prepaid.add(i); self._ts_take()
                    if self._r7:
                        self._r7_ts_acq[i] = self.current_round            # (round 7's records: taken at ACCEPT)
        if self.workspace_cap is not None:                                 # (a claim of 0 too: its members never wait for the cap)
            members = {i for i in step.instrs if self.schedule_instructions[i].end_round == -1}
            self._ws_claims[(v.vid, k)] = [self._ws_claim(step.instrs), members, set()]
            for i in members:
                self._ws_member[i] = (v.vid, k)
            self._sens["ws_claims"] += 1

    def _ws_after_round(self, rounds):
        """After a syndrome round: the occupied non-program cells, and the checks (beyond W only through the valve)."""
        self._np_set = {r.patch for r in rounds if self._role_of_patch.get(r.patch) != "program"}
        self._ws_override_cells &= self._np_set
        n = len(self._np_set)
        if n > self.workspace_cap:
            self._sens["workspace_cap_exceeded_rounds"] += 1
            self._sens["workspace_cap_max_excess"] = max(self._sens["workspace_cap_max_excess"], n - self.workspace_cap)
            if n - len(self._ws_override_cells) > self.workspace_cap:
                self._sens["workspace_cap_violations"] += 1                # beyond W without the valve: a run check

    def _ws_deadlocked(self, held: dict) -> bool:
        """Nothing runs and nothing can change by waiting -- no decode, delivery, publication, controller build, T-state
        production, recovery notice or registration is pending -- while an instruction that could start waits for the
        workspace cap: every other hold then waits, directly or not, on work the cap keeps out. A startup planned for a
        later round (SWIPER plans them as late as possible) does not count: it only adds occupancy, and its plan follows
        successors that wait behind the capped instruction, so it would be planned later round after round (Phase 1,
        QPE-5 at W = 5 stalled so)."""
        if self._active_instructions or any(held.get(w) for w in ("controller", "magic", "magic_accept")):
            return False
        now, si = self.current_round, self.schedule_instructions
        return not (self._completed_undecoded or self._decoded_undelivered or self._pending_ready
                    or any(si[self.feedback[f].anchor_idx].end_round != -1 for f in self._feedback_pending)     # a measurement decoding
                    or any(self.values[x].recovery is not None and self.values[x].recovery["kind"] in ("none", "frame")
                           for x in self._recovering)                                                          # RECOVERY_DONE next round
                    or (self.arm == "D" and any(r > now for r in self._defer_waiting.values())))               # a registration due later

    def _sens_summary(self) -> dict:
        out = dict(workspace_cap=self.workspace_cap)
        if self.t_supply is not None:
            out["t_supply"] = dict(period=str(self.t_supply["period"]), buffer=self.t_supply["buffer"], start=self.t_supply["start"],
                                   level_end=str(self._ts_level))
        out.update({k: int(v) for k, v in sorted(self._sens.items())})
        return out

    # ------------------------------------------------------------------ reservation
    def _reserve(self, v: Value, cells: frozenset):
        """Atomic transfer: the new step's cells are reserved before the old ones are released."""
        if not self.reservation:                                          # Q-nores: nothing is reserved
            return
        for c_ in cells:
            assert self._reserved.get(c_, v.vid) == v.vid, (c_, self._reserved[c_], v.vid)
            self._reserved[c_] = v.vid
        for c_ in v.reserved - cells:
            if self._reserved.get(c_) == v.vid:
                del self._reserved[c_]
        if v.reserved and cells != v.reserved:
            self.ledger.reservation_transfers += 1
        v.reserved = frozenset(cells)

    def _release(self, v: Value):
        for c_ in v.reserved:
            if self._reserved.get(c_) == v.vid:
                del self._reserved[c_]
        v.reserved = frozenset()

    def _reserved_by_other(self, idx: int, exclude: int | None = None) -> bool:
        """A cell reserved by a value may be used only by that value's own contract members and recovery."""
        owner_ok = {exclude} if exclude is not None else set()
        for vid, k in self._chain_of_instr.get(idx, ()):
            owner_ok.add(vid)
        if idx in self._recovery_owner:
            owner_ok.add(self._recovery_owner[idx])
        return any(self._reserved.get(p) not in (None,) and self._reserved[p] not in owner_ok for p in self.schedule_instructions[idx].instruction.patches)

    # ------------------------------------------------------------------ the gates
    def _own_hold(self, idx: int) -> str | None:
        vid = self._value_of_instr.get(idx)
        if vid is None:
            return None
        v = self.values[vid]
        if v.delivered or v.speculated:
            return None
        if self.arm != "Q":                                               # N, and a value arm D did not defer
            if self._r8 and (self._r8_reg_now.get(vid) == self.current_round or vid in self._r8_b0n_vids):
                return "defer_rule_a"                                      # (records only: rule (a) refused its registration;
                                                                           #  or sent it to N's path, until its delivery)
            return "native"
        if v.state is not VState.LIVE:
            return "no_prediction"
        if v.class_miss:
            return "class_miss"
        return v.hold_reason or "policy"

    def _L_before(self, v: Value, k: int) -> set:
        """The light cone at the boundary where step k begins: the corrected wire alone before step 0, else step k-1's L."""
        c = self.contracts.get(v.vid)
        if k <= 0 or c is None or not c.steps:
            return {v.wire}
        return set(c.steps[min(k, c.F) - 1].L) | {v.wire}

    def _live_eligible(self, v: Value) -> bool:
        """A registered, undelivered Q value that can still be accepted: predicted (LIVE), a contract with a site, not
        refused by the engine (class miss / site limits)."""
        c = self.contracts.get(v.vid)
        return self.arm == "Q" and v.state is VState.LIVE and not v.delivered and not v.class_miss and c is not None and c.has_site

    def _other_holds(self, idx: int, exclude: int | None = None) -> str | None:
        holds = "reserved" if self._reserved_by_other(idx, exclude) else None
        for vid, k in self._chain_of_instr.get(idx, ()):
            v = self.values[vid]
            if vid == exclude or v.settled or k < v.admitted:
                continue
            if v.speculated:
                holds = "fence" if (v.state is VState.RECOVERING or v.delivered) else (v.hold_reason or "policy")
            elif self._live_eligible(v) and (self._instr_wires.get(idx, frozenset()) & self._L_before(v, k)):
                # C6 fix: a registered (predicted, undelivered, eligible) Q value holds the members of its
                # not-yet-accepted steps that act inside the light cone of that step's boundary -- exactly what the
                # ACCEPT-time check forbids to have started before ACCEPT; members outside the light cone may run early
                # (design Sec. 2.4).  Not in N (training builds contracts but never admits), not for a value the engine
                # refused (class miss / site limits: it can never be accepted), and released at delivery (the WAIT path).
                holds = v.hold_reason or "policy"
        for vid in self._fence_of_instr.get(idx, ()):
            v = self.values[vid]
            if vid != exclude and not v.settled and (v.speculated or self._live_eligible(v)):
                holds = "fence"                      # the fence, too, is active from registration
        return holds

    def _hold_reason(self, idx: int) -> str | None:
        if self.arm == "D":                  # arm D and the conditional CZ's feedback hold (every arm)
            return self._defer_hold(idx) or self._own_hold(idx) or self._feedback_hold(idx)
        return self._other_holds(idx) or self._own_hold(idx) or self._feedback_hold(idx)

    # ------------------------------------------------------------------ atom admission (ACTION / ACCEPT)
    def _atom_ready(self, v: Value, step, patches_in_use) -> str | None:
        if step.k == 0:
            # the contract was built at the prediction against the work that had started by then (a contract whose cone
            # work had started has no site: `contract.refuse_executed_cone_work`); from the prediction on, the value's
            # LIVE holds cover its fence and its in-cone members.  Cone work that started in between escaped a hold: a C6
            # violation (the run is void), exactly like a member started before its step's ACCEPT.
            c_ = self.contracts[v.vid]
            cone = set(self._fence_set_of(v)) | {i for s_ in c_.steps[1:] for i in s_.instrs if self._instr_wires.get(i, frozenset()) & self._L_before(v, s_.k)}
            ran = sorted(i for i in cone if self.schedule_instructions[i].start_round != -1)
            if ran and v.vid not in self._cone_escape_flagged:                       # once per value
                self._cone_escape_flagged.add(v.vid); self.ledger.c6_violations += 1
                self._event("c6_violation", v, "cone work started between the prediction and the first ACCEPT: %s" % ran[:6])
        members = set(step.instrs)
        for i in members:
            t = self.schedule_instructions[i]
            if t.start_round != -1 or t.end_round != -1:
                continue
            for p in self._adj()[0].get(i, ()):
                if p not in members and self.schedule_instructions[p].end_round == -1:
                    return "not_ready"
            for cidx in t.instruction.conditioned_on_idx:
                if cidx != v.producer_idx and not self._delivered_instr(cidx):
                    return "not_ready"
            for cidx in t.instruction.conditioned_on_completion_idx:
                if self.schedule_instructions[cidx].end_round == -1:
                    return "not_ready"
            if any(p in patches_in_use for p in t.instruction.patches):
                return "not_ready"
            if any(self._reserved.get(p) not in (None, v.vid) for p in t.instruction.patches):
                return "reserved"
            if self._other_holds(i, exclude=v.vid) is not None:
                return "overlap"
        # overlap invariant against every other unresolved value: support, classical dependencies, footprint; and nothing this
        # ACCEPT activates (the value's fence set, its reservation) may cover another value's admitted, incomplete members --
        # their internal schedule is fixed (Sec. 2.4), so a hold or a reserved cell there would break the other atom's timer
        L_cells = {c_ for w in step.L for c_ in self.es.wire_cell(w, step.at_event)}
        foot = set(step.reserved_cells) | L_cells
        pending_instrs, pending_cells = set(), set()
        for w in self._unresolved_others(v.vid):
            if (set(step.L) & self._current_L(w)) or ((set(step.D_fixed) | set(step.D_cond)) & self._current_D(w)) or (foot & set(w.reserved)):
                return "overlap"
            for i in self.contracts[w.vid].admitted_instrs(w.admitted):
                if self.schedule_instructions[i].end_round == -1:
                    pending_instrs.add(i); pending_cells.update(self.schedule_instructions[i].instruction.patches)
        if step.k == 0:                                                    # activation: everything this value will hold from now on
            held_by_me = self._fence_set_of(v) | {i for s_ in self.contracts[v.vid].steps[1:] for i in s_.instrs}
            if held_by_me & pending_instrs:
                return "overlap"
        if not self.reservation:                                          # Q-nores: the workspace is not claimed at ACCEPT
            return None
        if (set(step.reserved_cells) - L_cells) & pending_cells:
            return "overlap"
        # the recovery workspace of this boundary must be free: its ancilla cells carry no live patch and are not reserved by others
        for c_ in set(step.reserved_cells) - L_cells:
            if self._reserved.get(c_) not in (None, v.vid) or c_ in self._active_patches or c_ in patches_in_use:
                return "workspace"
        if self._sens_on:                                                  # an accepted step runs on its encoded schedule, so
            return self._sens_accept_block(step)                           # its cells and T states are claimed at ACCEPT
        return None

    def _admit_steps(self, patches_in_use):
        if self.arm != "Q":
            return
        seen = set()                                                     # WAIT history: the values visited this round
        for v in self._scan(self._live_undelivered, lambda v: v.state is VState.LIVE and not v.delivered and not v.class_miss):
            seen.add(v.vid)
            c = self.contracts.get(v.vid)
            if c is None or not c.has_site or v.admitted >= c.F:
                v.hold_reason = None if c is None or not c.has_site else "fence"
                self._wait_note(v, None, end=v.hold_reason or "no_site"); continue
            k = v.admitted
            if k > 0 and not self._step_complete(v, k - 1):
                v.hold_reason = "inflight"; self._wait_note(v, None, end="inflight"); continue
            step = c.steps[k]
            why = self._atom_ready(v, step, patches_in_use)
            if why is not None:                                          # counted per held member by the dispatch's gate
                v.hold_reason = why; self._wait_note(v, None, end=why); continue
            a = self.policy.action(self, v, k)
            if a == "BUILDING":
                v.hold_reason = "controller"; self._wait_note(v, None, end="controller"); continue
            if a != FORWARD:
                v.hold_reason = "policy"
                self._wait_note(v, k, source=getattr(self.policy, "wait_why", {}).get((v.vid, k), "unknown")); continue
            self._wait_note(v, None, end="forward")
            # ACCEPT.  Members that already ran (on wires outside L: the residual commutes with them) make the encoded
            # duration an upper bound: the boundary still advances at ACCEPT + d_k, as the RTL's timer does (Sec. 2.4)
            started = {i for i in step.instrs if self.schedule_instructions[i].start_round != -1 or self.schedule_instructions[i].end_round != -1}
            Lprev = self._current_L(v) if k > 0 else {v.wire}
            for i in started:
                if self._instr_wires.get(i, frozenset()) & Lprev:
                    self.ledger.c6_violations += 1
                    self._event("c6_violation", v, "member %d of step %d had started on wires %s of L before ACCEPT" % (i, k, sorted(self._instr_wires[i] & Lprev)))
            v.accept_started[k] = started
            self._reserve(v, step.reserved_cells)                          # atomic transfer before anything else moves
            if self._sens_on:
                self._sens_accept(v, k, step)                              # the step's claim on the cap, its T states taken
            v.admitted = k + 1; v.accept_round[k] = self.current_round; v.hold_reason = None; self._timer_candidates.add(v.vid)
            v.issued_by_policy.append((self.current_round, k, "F"))
            if k == 0:
                v.speculated = True; self._speculated_unsettled.add(v.vid)
                self._apply_branch(v, bool(v.prediction))
                self._replan(v)                                            # (rule 41d's exception: planned as the branch taken)
                self._taint_wires[v.vid] = {v.wire}; self._taint_results[v.vid] = {v.result_id}
                if v.prediction: self.ledger.speculative_issues += 1
                else: self.ledger.speculative_skips += 1
                self._event("speculate", v, "S" if v.prediction else "noS")
            self._event("accept", v, "step %d (%s) duration %d" % (k, step.template, step.duration))
            L_ = self.ledger                                                # w10 instrumentation (no effect on the run)
            L_.accepted_steps += 1
            L_.accepted_by_template[step.template] = L_.accepted_by_template.get(step.template, 0) + 1
            if step.template in TWO_QUBIT_TEMPLATES:
                L_.accepted_two_qubit_steps += 1
            if step.residual.has_q and step.residual.axis.weight == 2:
                L_.accepted_two_qubit_residuals += 1
        for vid in [x for x in self._wait_open if x not in seen]:
            w = self.values[vid]
            self._wait_close(vid, "verdict" if w.delivered else w.state.value)

    # ------------------------------------------------------------------ WAIT history
    def _wait_note(self, v: Value, k, source: str | None = None, end: str | None = None):
        """This round's outcome for a visited value: a policy WAIT at boundary k (with its source, `ScoutPolicy.wait_why`)
        extends or opens an episode; anything else -- an ACCEPT, or a hold that kept the policy from being asked --
        closes the open episode, the end naming it: "forward" (ACCEPT), else the hold ("inflight", "not_ready",
        "overlap", "reserved", "workspace", "controller", "fence").  Bookkeeping only: nothing here feeds a decision."""
        now = self.current_round
        op = self._wait_open.get(v.vid)
        if k is not None:
            if op is not None and op[0] == k and op[3] == source and op[2] in (now - 1, now):   # (now: a second pass this round)
                op[2] = now
                return
            if op is not None:
                self._wait_close(v.vid, "source" if op[0] == k and op[2] == now - 1 else "gap")
            self._wait_open[v.vid] = [k, now, now, source]
        elif op is not None:
            self._wait_close(v.vid, end)

    def _wait_close(self, vid: int, end: str):
        k, first, last, source = self._wait_open.pop(vid)
        self._wait_log[vid].append((k, first, last - first + 1, source, end))

    def value_history(self) -> dict:
        """One row per value: the prediction and verdict timeline, the contract, the admitted steps and every WAIT
        episode, with the value's outcome -- "confirmed" (acted, prediction right), "repaired" (acted, prediction
        wrong: contradicted and repaired by the recovery), "waited_right" / "waited_wrong" (registered, never acted),
        "not_registered" (predicted, but the engine held no entry: no site, class miss, site limits, or an N run).
        `measured` is the producing instruction's end round, `resolution` = delivered - measured (the verdict latency).
        A WAIT episode is [k, first round, rounds, source, end]: consecutive rounds in which the policy answered WAIT at
        boundary k with one source ("table", "horizon", "expired", "depth", "no_table"); the end is what followed:
        "forward" (ACCEPT at k), "verdict" (the definitive value arrived while waiting), a hold, "source" (the source
        changed) or "gap". `fence_followed` per episode: the value later reached its fence (all F steps admitted)."""
        for vid in list(self._wait_open):                                   # a run always ends with every value delivered
            w = self.values[vid]
            self._wait_close(vid, "verdict" if w.delivered else "end")
        fields = ["vid", "producer", "measured", "predicted", "delivered", "resolution", "correct", "confidence", "F", "fence",
                  "admitted", "accepts", "outcome", "contradiction", "recovery", "recovery_step", "recovery_start", "recovery_done",
                  "settled", "waits"]
        rows = []
        for v in self.values.values():
            c = self.contracts.get(v.vid)
            end = self.schedule_instructions[v.producer_idx].end_round
            correct = None if v.prediction is None else bool(v.prediction == v.truth)
            if v.speculated:
                outcome = "confirmed" if correct else "repaired"
            elif self.arm == "Q" and v.predicted_round >= 0 and c is not None and c.has_site and not v.class_miss:
                outcome = "waited_right" if correct else "waited_wrong"
            else:
                outcome = "not_registered"
            F = c.F if c is not None else None
            fence_round = v.accept_round.get(F - 1) if F else None
            waits = [[k, first, n, src, e, bool(fence_round is not None and fence_round >= first + n - 1)]
                     for k, first, n, src, e in self._wait_log.get(v.vid, ())]
            r = v.recovery or {}
            rows.append([v.vid, v.producer_idx, end, v.predicted_round, v.delivered_round,
                         (v.delivered_round - end) if (v.delivered_round >= 0 and end >= 0) else None,
                         correct, v.confidence, F, (c.fence_reason if c is not None else None), v.admitted,
                         [v.accept_round[k] for k in sorted(v.accept_round)], outcome, v.contradiction_round,
                         r.get("kind"), r.get("step"), r.get("round"), r.get("done_round"), v.settled_round, waits])
        return dict(fields=fields, wait_fields=["k", "first", "rounds", "source", "end", "fence_followed"], rows=rows)

    def _count_hold(self, why: str, n: int = 1):
        L = self.ledger
        if why == "native": L.held_native += n
        elif why == "no_prediction": L.held_no_prediction += n
        elif why in ("policy", "inflight"): L.held_policy += n
        elif why == "controller": L.held_controller += n
        elif why == "fence": L.held_fence += n
        elif why == "overlap": L.held_overlap += n
        elif why == "reserved": L.held_reserved += n
        elif why == "workspace": L.held_workspace += n
        elif why == "not_ready": L.held_not_ready += n
        elif why == "class_miss": L.held_class_miss += n
        elif why == "feedback": L.held_feedback += n
        elif why.startswith("defer_"): self._defer_stats["held_" + why[6:]] += n    # arm D only
        elif why.startswith("hw_"): self._hw.stats["held_" + why[3:]] += n          # rule 66's mode only
        elif why in ("workspace_cap", "ws_accept", "magic", "magic_accept"): self._sens["held_" + why] += n   # knobs only

    # ------------------------------------------------------------------ C6 monitor
    def _monitor_start(self, idx: int):
        """An instruction started: it must not touch a live value's tainted wires or consume its tainted results unless it
        is that value's admitted member or recovery; admitted members extend the taint (the residual's light cone)."""
        if self.arm == "D":
            self._defer_monitor(idx); return
        if self._nores is not None and idx in self._recovery_instrs:
            self._nores_started(idx)                                      # Q-nores
        ins = self.schedule_instructions[idx].instruction
        wires = self._instr_wires.get(idx, frozenset()); preds = self._instr_preds.get(idx, frozenset())
        for v in self._scan(self._speculated_unsettled, lambda v: v.speculated and not v.settled):
            # only events AFTER the producer can consume the residual: the producer measurement's own bookkeeping (its
            # routing-cell DISCARD maps to the producer event itself) and everything earlier in the stream are causally before it
            if idx < self.n_original and self._instr_maxpos.get(idx, -1) <= self.es.events[v.producer_event].pos:
                continue
            admitted = idx in self.contracts[v.vid].admitted_instrs(v.admitted) or self._recovery_owner.get(idx) == v.vid
            tw, tr = self._taint_wires[v.vid], self._taint_results[v.vid]
            touches = bool(wires & tw) or bool(preds & tr)
            if idx in self._recovery_instrs and self._recovery_owner.get(idx) != v.vid:       # another value's recovery: by cells
                touches = touches or bool(set(ins.patches) & {c_ for w in tw for c_ in self.es.wire_cell(w, v.producer_event)})
            if touches and not admitted:
                self.ledger.c6_violations += 1
                self._event("c6_violation", v, "instruction %d (%s %s) started on tainted wires %s / results %s; events %s; value admitted %d state %s"
                            % (idx, ins.name, sorted(ins.patches), sorted(wires & tw), sorted(preds & tr),
                               [(e, self.es.events[e].kind, self.es.events[e].pos - self.es.events[v.producer_event].pos) for e in self.es.lss_reverse.get(idx, ())], v.admitted, v.state.value))
            if admitted and self._recovery_owner.get(idx) != v.vid:      # an admitted member extends the light cone
                tw.update(wires); tr.update(self._instr_results.get(idx, ()))

    def _monitor_timers(self):
        for v in self._scan(self._timer_candidates, lambda v: v.vid in self.contracts and any((v.vid, k) not in self._timer_checked for k in range(v.admitted))):
            c = self.contracts[v.vid]
            for k in range(v.admitted):
                if (v.vid, k) in self._timer_checked:
                    continue
                s = c.steps[k]; due = v.accept_round[k] + s.duration
                if self.current_round < due:
                    continue
                self._timer_checked.add((v.vid, k))
                ends = [self.schedule_instructions[i].end_round for i in s.instrs]
                exact = not any(self.schedule_instructions[i].instruction.conditioned_on_idx - {v.producer_idx} for i in s.instrs) and not v.accept_started.get(k)
                done = all(e != -1 for e in ends) and max(e + 1 for e in ends) <= due
                completion = max([self.schedule_instructions[i].end_round + 1 for i in s.instrs if self._instruction_durations[i] > 0], default=due)
                if not done or (exact and completion != due):
                    self.ledger.timer_violations += 1
                    self._event("timer_violation", v, "step %d accepted %d duration %d ends %s" % (k, v.accept_round[k], s.duration, ends))

    # ------------------------------------------------------------------ dispatch (upstream logic with the gates)
    def _update_active_instructions(self, not_fully_decoded_instructions: set[int]) -> None:
        if not self._sidecar_ready:
            return
        if self._hw_cfg is not None:
            self._hw_round()                                               # rule 66's mode: the round's servers, the tick first
        patches_in_use = set()
        for i in self._active_instructions:
            patches_in_use.update(self.schedule_instructions[i].instruction.patches)
        # instructions completed since the previous dispatch (upstream pops them from the active dict) + this dispatch's
        # zero-duration completions: they feed the decode stamping and the publication candidates
        preds_c, succs_c = self._adj()
        ready, startup = self._frontier_sets()
        for lst in self._round_events.values():
            lst.clear()                                               # every stamp of this round happens below, before the policy's pass
        finished = self._prev_active_snapshot - set(self._active_instructions)   # completed by upstream since the last dispatch
        for idx in finished:
            if self._scan_check:
                assert self.schedule_instructions[idx].end_round != -1, idx
            for s in succs_c.get(idx, ()):
                self._pending_preds[s] -= 1
                if not self._pending_preds[s]:
                    self._pred_blocked[s] = 0
                    if s in self._instruction_frontier:
                        ready.add(s)
        completed_now = set(finished)
        completed_now.update(self._zero_completed); self._zero_completed = []
        for idx in completed_now:
            if idx in self._tracked:
                self._completed_undecoded.add(idx)
            vid = self._vid_of_producer.get(idx)
            if vid is not None:
                bisect.insort(self._pending_ready, (self.schedule_instructions[idx].end_round, vid))
                if self._side is not None:
                    bisect.insort(self._side_ready, (self.schedule_instructions[idx].end_round, vid))
                if self.arm == "D":
                    self._defer_arrivals.append((self.schedule_instructions[idx].end_round, vid))
        self._stamp_program_done()
        self._stamp_decodes(not_fully_decoded_instructions)
        if self._feedback_pending:
            self._deliver_feedback()                                       # w10: before this round's admissions and dispatch
        if self._strip_on:
            self._strip_round(completed_now)                               # the strip decoder's round: its publications are due
        if self._sens_on:
            self._sens_round()                                             # this round's T-state production; finished steps' claims
        # Phase A in the engine's order: REGISTER, RESOLVE, RECOVERY_DONE; then the builder / record; then ACTION / ACCEPT
        self._publish_predictions()
        self._settle_verified()
        self._advance_recoveries()
        if self.arm == "D":
            self._defer_phase(patches_in_use)                             # register, resolve and materialize frames (v9-deferral)
        if self._rc_pending:
            self._swiper_reconfidence()
        if self.policy is not None and self.arm == "Q":
            self.policy.observe(self)
        self._admit_steps(patches_in_use)
        start_times = self._planned(self._predict_instruction_start_times)
        held: dict[str, set] = collections.defaultdict(set)
        ws_valve = False                                                   # the workspace cap's deadlock valve is starting one
        r17 = self._r17                                                    # (---records: None when off)

        def blocked(idx):
            task = self.schedule_instructions[idx]
            if any(p in patches_in_use for p in task.instruction.patches):
                if self._dg:
                    self._dg_round[idx] = "patch"
                if r17 is not None:
                    r17.note(idx, "patch")                                 # (records only)
                return "patch"
            if any(self.schedule_instructions[p].end_round == -1 for p in self._adj()[0].get(idx, ())):
                if self._scan_check:
                    assert self._pending_preds.get(idx, 0) > 0, idx
                return "pred"
            if self._scan_check:
                assert self._pending_preds.get(idx, 0) == 0, idx
            if any(self.schedule_instructions[c].end_round == -1 for c in task.instruction.conditioned_on_completion_idx):
                if self._dg:
                    self._dg_round[idx] = "completion"
                if r17 is not None:
                    r17.note(idx, "completion")
                return "completion"
            why = self._hold_reason(idx)
            if why is None and self._sens_on:
                why = self._sens_block(idx, skip_cap=ws_valve)            # the T-state supply, the workspace cap
            if why is None and self._wq is not None and self.arm == "D":
                why = self._wq_start(idx)                                  # (the write queue admits the start, or holds it)
            if why:
                held[why].add(idx)
                if self._dg:
                    self._dg_round[idx] = why
                if r17 is not None:
                    r17.note(idx, why)
            return why

        def start(idx):
            task = self.schedule_instructions[idx]
            if self._diag is not None and idx in self._diag.planned_seen:
                self._diag.planned_seen.discard(idx)
                self._diag.plan_start(self, idx, start_times)             # (records only: what bound the planner's start)
            self._monitor_start(idx)
            if self._resv_held:
                self._resv_start(idx)                                     # (SER-R's invariant: records)
            if self._sens_on:
                self._sens_start(idx)                                     # takes a T state, occupies non-program cells
            if self._r9 is not None and self._r9.vrun:
                self._r12_vstart(idx)                                     # (cov=enforce: a run a script's mark covers ends)
            if self._instruction_durations[idx] == 0:
                task.start_round = task.end_round = self.current_round - 1
                for s in succs_c.get(idx, ()):
                    self._pending_preds[s] -= 1
                    if not self._pending_preds[s]:
                        self._pred_blocked[s] = 0
                        if s in self._instruction_frontier:
                            ready.add(s)
                        if (self.arm == "D" and not self._noreg and self._is_startup_instruction[s] and s in self._instruction_frontier
                                and s not in start_times and s not in self._later_times):
                            # arm D: a startup instruction the round's planning skipped while it was blocked is planned now,
                            # as a startup successor entering the frontier is (below)
                            if self._planned(self._surely_later, s, start_times):
                                self._later_times[s] = self.current_round + 1
                            else:
                                self._planned(self._predict_instruction_start_time_fully, s, start_times)
                self._completed_instruction_count += 1
                self._zero_completed.append(idx)
                if self.lightweight_setting < 2:
                    self._completed_instructions.append(idx)
                if task.instruction.name == "DISCARD":
                    self._active_patches -= set(task.instruction.patches)
                    for p in task.instruction.patches:
                        self._role_of_patch.pop(p, None)
                    if self.workspace_cap is not None:
                        self._np_set.difference_update(task.instruction.patches)
                        self._ws_override_cells.difference_update(task.instruction.patches)
                    if self._r9 is not None and idx >= self.n_original:
                        self._r9_vacate(task.instruction.patches, idx)     # (r9-guard-close 2: a script's DISCARD frees cells)
            else:
                task.start_round = self.current_round
                self._active_instructions[idx] = self._instruction_durations[idx]
                patches_in_use.update(task.instruction.patches)
            self._instruction_frontier -= {idx}
            ready.discard(idx); startup.discard(idx)
            new = set(succs_c.get(idx, ())) - self._instruction_frontier   # the lists mirror networkx's insertion order
            if self.arm == "D":                                           # arm D fails closed: a successor never ran before its predecessor
                self._defer_started.add(idx)
                if self._r9 is not None:                                  # (records only: the second port's started bit and
                    self._r9.port2(self.current_round, 1 + len(task.instruction.patches))   #  the cell table's occupant per cell)
                    self._r9_open.add(idx)
                    if self._wq is not None:                              # (on the reservation its admission or its
                        self._wq.issue(("in", idx), 1 + len(task.instruction.patches), "S", self.current_round)   #  owner made)
                        self._wq_adv_start(idx)                            # (records: an admitted step's first start)
                self._defer_check_successors(idx, new)
            for j in new:
                if os.environ.get("E2E_FULL_PREDICTOR"):
                    self._planned(self._predict_instruction_start_time_fully, j, start_times)
                elif self._is_startup_instruction[j]:
                    if self._planned(self._surely_later, j, start_times):
                        self._later_times[j] = self.current_round + 1     # never in start_times: no walk may read it
                    else:
                        self._planned(self._predict_instruction_start_time_fully, j, start_times)
            self._instruction_frontier.update(new)
            ready.update(j for j in new if not self._pred_blocked[j])
            startup.update(j for j in new if self._is_startup_instruction[j])

        progress = True
        while progress:                                           # zero-duration closure, as upstream
            progress = False
            if self._scan_check:
                for idx in self._instruction_frontier:
                    assert (self._pred_blocked[idx] == 1) == (self._pending_preds.get(idx, 0) > 0) == any(self.schedule_instructions[p].end_round == -1 for p in preds_c.get(idx, ())), idx
            # entries with a running predecessor are skipped in C (blocked() would answer "patch" or "pred", neither recorded);
            # the frontier set itself is untouched, so the visiting order of the remaining entries is upstream's
            for idx in self._frontier_scan(self._instruction_frontier, ready):
                if start_times.get(idx, self._later_times.get(idx, 0)) > self.current_round or self._instruction_durations[idx] != 0:
                    continue
                if blocked(idx) is None:
                    start(idx); progress = True; break
        for idx in self._frontier_scan(self._instruction_frontier.copy(), ready):   # upstream iterates a COPY (gate G1 history, README)
            if start_times.get(idx, self._later_times.get(idx, 0)) > self.current_round or self._instruction_durations[idx] == 0:
                if self._dg and self._instruction_durations[idx] != 0:
                    self._dg_round.setdefault(idx, "planned")             # diag: the start-time planner's later start
                if r17 is not None and self._instruction_durations[idx] != 0:
                    r17.round_why.setdefault(idx, "planned")
                if self._diag is not None and self._instruction_durations[idx] != 0:
                    self._diag.planned_seen.add(idx)                      # (records only: its binding at its start)
                continue
            if blocked(idx) is None:
                start(idx)
        if self.workspace_cap is not None and held.get("workspace_cap") and self._ws_deadlocked(held) and not self._ws_hard_refuse():
            # deadlock valve: nothing runs and nothing can change by waiting, while instructions wait for the cap --
            # the lowest-numbered of them starts beyond W (never under --cap-hard, `_ws_hard_refuse`)
            idx = min(held["workspace_cap"])
            held["workspace_cap"].discard(idx)
            ws_valve = True
            if blocked(idx) is None:
                self._sens["workspace_cap_overrides"] += 1
                self._ws_overriding = True
                start(idx)
                self._ws_overriding = False
            ws_valve = False
        progressed = bool(self._active_instructions) or self._completed_instruction_count != self._last_progress_count
        if progressed:
            self._last_progress_count = self._completed_instruction_count; self._last_progress_round = self.current_round
        elif self._guard == "off" and self._instruction_frontier and self.current_round - self._last_progress_round > self.stall_rounds:
            self._g_note("stall", "run")                                   # (OFF: no progress for the stall guard's rounds:
            if self._r29 is not None:                                      #  the run ends, clean, with a failure status)
                if self._r29["outcome"] is None:
                    self._r29_stop("deadlocked", kind="stall_off", since=self._last_progress_round)
            elif LINK.get("simulator") is not None:
                LINK["simulator"].failed = True
        elif self._r29 is None and self._instruction_frontier and self.current_round - self._last_progress_round > self.stall_rounds:
            diag = {idx: (self.schedule_instructions[idx].instruction.name, blocked(idx),
                          [(vid, k, self.values[vid].state.value, self.values[vid].admitted, self.values[vid].hold_reason) for vid, k in self._chain_of_instr.get(idx, ())],
                          [(vid, self.values[vid].state.value, self.values[vid].delivered_round, self.values[vid].recovery and self.values[vid].recovery.get("instrs")) for vid in self._fence_of_instr.get(idx, ())])
                    for idx in sorted(self._instruction_frontier)[:12]}
            raise RuntimeError("stalled: no instruction started or completed since round %d (now %d); frontier %d: %s"
                               % (self._last_progress_round, self.current_round, len(self._instruction_frontier), diag))
        for why, idxs in held.items():
            self._count_hold(why, len(idxs))
            self._conditioned_decode_wait_time_sum += len(idxs)
        if self._dg:
            self._dg_tally()
        if r17 is not None:
            r17.tally(self)                                                # (records only: each waiting instruction's cause)
        if self._r29 is not None:
            self._r29_round(progressed, held, start_times)               # rule 29: censoring and the deadlock detector
        self._monitor_timers()                                    # after this round's dispatch: a cleanup due this round has run
        self.ledger.rounds = self.current_round
        if self._r7:
            self._r7_tick(held)                                            # (round 7's progress tracer, records only)
        self._prev_active_snapshot = set(self._active_instructions)
        if self._progress_file and self.current_round % 500 == 0:
            self._heartbeat()

    def _stamp_program_done(self):
        """The round the last ORIGINAL instruction completed (end_round + 1), checked every round (MA-17 / MB-17)."""
        if self.arm == "D":
            # the same stamp without rescanning every original instruction each round: arm D splices many scripts, so the
            # completed count passes n_original long before the program ends; keep the originals not yet ended instead
            if self.ledger.program_done_round < 0 and self._completed_instruction_count >= self.n_original:
                si = self.schedule_instructions
                if self._completed_instruction_count == getattr(self, "_defer_open_at", -1):
                    # nothing ended since the last scan (every end_round is set together with the count), so the
                    # list is unchanged
                    if self._scan_check:
                        assert all(si[i].end_round == -1 for i in self._defer_open), "an original ended without the count"
                    return
                self._defer_open_at = self._completed_instruction_count
                prev = getattr(self, "_defer_open", None)
                self._defer_open = [i for i in (prev if prev is not None else range(self.n_original)) if si[i].end_round == -1]
                if not self._defer_open:
                    self.ledger.program_done_round = max(t.end_round for t in si[:self.n_original]) + 1
            return
        if self.ledger.program_done_round < 0 and self._completed_instruction_count >= self.n_original:   # necessary condition first
            ends = [t.end_round for t in self.schedule_instructions[:self.n_original]]
            if all(e != -1 for e in ends):
                self.ledger.program_done_round = max(ends) + 1

    def _planned(self, fn, *args):
        """Run a start-time prediction with the causal planning durations in place (never the truth before delivery)."""
        exec_durations = self._instruction_durations
        self._instruction_durations = self._plan_durations
        try:
            return fn(*args)
        finally:
            self._instruction_durations = exec_durations

    def _predict_instruction_start_times(self):
        """Only startup instructions (scheduled as late as possible) need the prediction (upstream spends 92 % here).
        Speed (results bit-identical): a frontier startup instruction whose predicted start is PROVABLY later
        than this round (`_surely_later`, a depth-3 lower bound on upstream's own recurrence) gets no walk at all -- the
        dispatch only compares the prediction with the current round -- and is recorded in `_later_times`, never in the
        shared memo, so no other instruction's value can read the placeholder. The exact walks use upstream's algorithm
        verbatim on cached neighbour lists with an O(1) work-list (`_predict_instruction_start_time[_fully]` below)."""
        self._later_times = {}
        if os.environ.get("E2E_FULL_PREDICTOR"):
            return super()._predict_instruction_start_times()
        first_round = {}
        now = self.current_round
        frontier = self._instruction_frontier
        self._adj()
        ready, startup = self._frontier_sets()
        if _set_ascending(frontier, len(self.schedule_instructions)):   # deploy 2 (speed): the same entries, in the same order
            seq = sorted(startup & ready) if (self.arm == "D" and not self._noreg) else sorted(startup)
        else:
            if self.arm == "D" and not self._noreg:   # arm D: a startup instruction still waiting for a predecessor (a materialization's release
                                             # edge pulls it into the frontier early) cannot start this round and the dispatch never
                frontier = filterfalse(self._pred_blocked.__getitem__, frontier)   # reads its planned start: not planned (N / Q unchanged)
            seq = filter(self._is_startup_instruction.__getitem__, frontier)
        if self._scan_check:
            seq = list(seq)
            full = [i for i in self._instruction_frontier if self._is_startup_instruction[i] and (self.arm != "D" or self._noreg or not self._pred_blocked[i])]
            assert seq == full, ("startup scan differs from the frontier's", len(seq), len(full))
        for idx in seq:
            if self._surely_later(idx, first_round):
                self._later_times[idx] = now + 1
            else:
                first_round = self._predict_instruction_start_time_fully(idx, first_round)
        return first_round

    def _frontier_sets(self):
        """(ready, startup): the frontier's entries with no running predecessor and its startup instructions, kept in step
        with the frontier and the predecessor flags by every path that changes them (deploy 2: a Q recovery's release
        edges put every later user of its cells into the frontier, 10^5 entries on Grover-5, and the per-round scans of the
        frontier then took 80 % of the run). Built on first use, after `_adj`."""
        if self._frontier_ready is None:
            self._adj()
            pb, st = self._pred_blocked, self._is_startup_instruction
            self._frontier_ready = {i for i in self._instruction_frontier if not pb[i]}
            self._frontier_startup = {i for i in self._instruction_frontier if st[i]}
        if self._scan_check:
            pb, st = self._pred_blocked, self._is_startup_instruction
            assert self._frontier_ready == {i for i in self._instruction_frontier if not pb[i]}, "frontier ready set"
            assert self._frontier_startup == {i for i in self._instruction_frontier if st[i]}, "frontier startup set"
        return self._frontier_ready, self._frontier_startup

    def _frontier_add(self, f: int):
        """A splice puts instruction f into the frontier (its predecessor flags are already set)."""
        self._instruction_frontier.add(f)
        if self._frontier_ready is not None:
            if not self._pred_blocked[f]:
                self._frontier_ready.add(f)
            if self._is_startup_instruction[f]:
                self._frontier_startup.add(f)

    def _frontier_scan(self, frontier: set, ready: set):
        """The dispatch's visit of `frontier` (the frontier or its copy): its entries with no running predecessor, in the
        set's iteration order.  When CPython stores the set in ascending order (`_set_ascending`), that is `ready` sorted;
        else the C-level filter of the set itself.  E2E_SCAN_CHECK=1 asserts the two agree."""
        if _set_ascending(frontier, len(self.schedule_instructions)):
            seq = sorted(ready)
            if self._scan_check:
                assert seq == list(filterfalse(self._pred_blocked.__getitem__, frontier)), "frontier scan differs"
            return seq
        return filterfalse(self._pred_blocked.__getitem__, frontier)

    # ------------------------------------------------------------------ prediction internals (exact, faster)
    def _adj(self):
        """Predecessor / successor lists of the schedule DAG in networkx's own iteration order (built once, extended by
        `_add_edge_cached` at every splice) -- the recurrence below iterates them exactly as upstream iterates the graph."""
        if getattr(self, "_pred_list", None) is None:
            self._pred_list = {i: list(self.schedule_dag.predecessors(i)) for i in self.schedule_dag.nodes}
            self._succ_list = {i: list(self.schedule_dag.successors(i)) for i in self.schedule_dag.nodes}
            self._pending_preds = {i: sum(1 for p in ps if self.schedule_instructions[p].end_round == -1) for i, ps in self._pred_list.items()}
            self._pred_blocked = bytearray(max(self._pending_preds, default=0) + 4096)
            for i, n in self._pending_preds.items():
                if n: self._pred_blocked[i] = 1
        return self._pred_list, self._succ_list

    def _add_edge_cached(self, a, b, fresh=False) -> bool:
        """Append the edge a -> b to the neighbour lists (networkx order: a new edge goes to the end of both lists) and keep the
        predecessor counters and flags; False if the pair already exists (networkx's add_edge changes nothing then either).
        fresh=True: the caller knows the pair cannot exist (a release edge leaves a brand-new recovery instruction), so the
        membership test -- O(len(preds[b])), which grows by one per recovery that fenced b -- is skipped (round 3f)."""
        preds, succs = self._adj()
        pb = preds.setdefault(b, [])
        if not fresh and a in pb:
            return False
        pb.append(a); succs.setdefault(a, []).append(b)
        preds.setdefault(a, []); succs.setdefault(b, [])
        self._pending_preds.setdefault(a, 0)
        if max(a, b) >= len(self._pred_blocked):                            # a spliced instruction beyond the flag array
            self._pred_blocked.extend(bytes(max(a, b) + 4096 - len(self._pred_blocked)))
        if self.schedule_instructions[a].end_round == -1:
            self._pending_preds[b] = self._pending_preds.get(b, 0) + 1
            self._pred_blocked[b] = 1
            if self._frontier_ready is not None:
                self._frontier_ready.discard(b)
        else:
            self._pending_preds.setdefault(b, 0)
        return True

    def dag_acyclic(self) -> bool:
        """The end-of-run acyclicity check over the neighbour lists, which hold every edge including the release -> later-user
        edges the networkx graph no longer stores (Kahn's algorithm).  The per-splice networkx check covers the recovery's own
        edges; the release edges are checked at splice time by the unstarted-ancestry walk in `_splice_recovery` (a cycle
        through them needs an unstarted release target that is an ancestor of an admitted member), which raises exactly where
        the networkx check raised when those edges were in the graph.  This final check is the belt to that brace."""
        preds, succs = self._adj()
        indeg = {i: len(ps) for i, ps in preds.items()}
        ready = [i for i, n in indeg.items() if n == 0]; seen = 0
        while ready:
            i = ready.pop(); seen += 1
            for s in succs.get(i, ()):
                indeg[s] -= 1
                if indeg[s] == 0:
                    ready.append(s)
        return seen == len(indeg)

    def _lb_start(self, idx, depth, memo=None, stop=None):
        """Lower bound on upstream's predicted start of the pending standard instruction `idx`, AS THE EXACT WALK WOULD
        COMPUTE IT with the memo `memo` (the round's `first_round` / `start_times`): a predecessor already in the memo
        contributes exactly memo[p] + dur(p) (the walk reuses memo entries, even ones written before this round's timed
        starts); otherwise every prediction is clamped to >= current_round, a pending standard predecessor contributes
        >= bound(p) + dur(p), an active predecessor now + remaining, and completed / pending-startup predecessors add
        nothing (upstream: end + 1 <= now, and 0 / nothing). Durations are the planning durations in force during the
        prediction (`_planned`). speed (results bit-identical): with `stop`, the walk returns as soon as the bound
        exceeds `stop`, with a value > stop that need not be the bound; a bound <= stop is returned exactly (each
        predecessor's walk gets stop - dur, so a cut walk's predecessor already lifts the bound past stop). The only reader,
        `_surely_later`, asks whether the bound is <= its limit."""
        now = self.current_round
        lb = now
        if depth == 0:
            return lb
        preds = self._pred_list                                            # (built: `_predict_instruction_start_times` calls `_adj` first)
        tasks, dur, active = self.schedule_instructions, self._instruction_durations, self._active_instructions
        for p in preds.get(idx, ()):
            if memo is not None and p in memo:
                t = memo[p] + dur[p]
            elif p in active:
                t = now + active[p]
            elif tasks[p].end_round != -1 or self._is_startup_instruction[p]:
                continue
            else:
                t = self._lb_start(p, depth - 1, memo, None if stop is None else stop - dur[p]) + dur[p]
            if t > lb:
                lb = t
                if stop is not None and lb > stop:
                    return lb
        return lb

    def _surely_later(self, idx, memo=None):
        """True when upstream's ALAP prediction of the frontier startup instruction `idx`, evaluated with the memo the
        exact walk would reuse, is provably > current_round: pred(idx) = max(min over successors s of pred(s) - dur(idx),
        now), a memoized successor is taken at its memo value and any other successor is bounded below by
        `_lb_start`(s, memo); so if every successor is a pending standard instruction with value / bound > now + dur(idx),
        the dispatch cannot start `idx` this round whatever the exact value.  Any other shape falls back to the exact
        walk."""
        succs = self._succ_list.get(idx, ())                               # (built, as in `_lb_start`)
        if not succs:
            return False
        limit = self.current_round + self._instruction_durations[idx]
        for s in succs:
            if memo is not None and s in memo:
                if memo[s] <= limit:
                    return False
                continue
            if self.schedule_instructions[s].end_round != -1 or s in self._active_instructions or self._is_startup_instruction[s]:
                return False
            if self._scan_check:                                          # (the cut walk decides as the full one)
                assert (self._lb_start(s, 3, memo, limit) <= limit) == (self._lb_start(s, 3, memo) <= limit), ("lb cut", idx, s)
            if self._lb_start(s, 3, memo, limit) <= limit:
                return False
        return True

    def _predict_instruction_start_time_fully(self, instruction_idx, first_round):
        """Upstream's work-list walk with its OrderedSet replaced by a dict (same visiting order, O(1) move-to-front)."""
        todo = {instruction_idx: None}
        while todo:
            instr, _ = todo.popitem()                                  # LIFO: the most recently (re)inserted key
            first_round, new_instructions = self._predict_instruction_start_time(instr, first_round)
            for x in reversed(new_instructions):
                todo.pop(x, None); todo[x] = None                          # OrderedSet.add(x, push_front=True)
        return first_round

    def _predict_instruction_start_time(self, instruction_idx, first_round):
        """Upstream's recurrence verbatim (swiper/device_manager.py), the graph neighbours read from the cached lists."""
        preds, succs = self._adj()
        instructions_to_process = []
        if instruction_idx in first_round:
            pass
        elif instruction_idx in self._active_instructions:
            first_round[instruction_idx] = self._active_instructions[instruction_idx] + self.current_round - self._instruction_durations[instruction_idx]
        elif self.schedule_instructions[instruction_idx].end_round != -1:
            first_round[instruction_idx] = self.schedule_instructions[instruction_idx].end_round - self._instruction_durations[instruction_idx] + 1
        else:
            valid = True
            expected_start = None
            if self._is_startup_instruction[instruction_idx]:
                for inst_idx in succs.get(instruction_idx, ()):
                    if inst_idx in first_round:
                        if valid:
                            this_start = first_round[inst_idx] - self._instruction_durations[instruction_idx]
                            if not expected_start or this_start < expected_start:
                                expected_start = this_start
                    else:
                        expected_start = None
                        valid = False
                        instructions_to_process.append(inst_idx)
            else:
                for inst_idx in preds.get(instruction_idx, ()):
                    if inst_idx in first_round:
                        if valid:
                            this_start = first_round[inst_idx] + self._instruction_durations[inst_idx]
                            if not expected_start or this_start > expected_start:
                                expected_start = this_start
                    elif self._is_startup_instruction[inst_idx]:
                        if valid:
                            if self.schedule_instructions[inst_idx].end_round != -1:
                                this_start = self.schedule_instructions[inst_idx].end_round
                                if not expected_start or this_start > expected_start:
                                    expected_start = this_start
                            elif inst_idx in self._active_instructions:
                                this_start = self._active_instructions[inst_idx] + self.current_round
                                if not expected_start or this_start > expected_start:
                                    expected_start = this_start
                            else:
                                expected_start = expected_start if expected_start else 0
                    else:
                        expected_start = None
                        valid = False
                        instructions_to_process.append(inst_idx)
            if expected_start is not None:
                first_round[instruction_idx] = max(expected_start, self.current_round)
            else:
                instructions_to_process.append(instruction_idx)
        return first_round, instructions_to_process

    # ------------------------------------------------------------------ occupancy
    def _role_for(self, r) -> str:
        """Role of the lifetime a syndrome round belongs to (design Sec. 1.3: a declaration, never a run length): the value's
        recovery; an `init` of the instruction on this cell; a merge's routing cell; a rotation's busy region; else the
        lifetime table's occupant of the cell at this point of the stream.

        Round-9: an init's cells are read from the ONE canonical representation
        (`EventStream.lifetime_cells`, the event's `cell`) -- the `cells` list this lookup used to prefer is an
        unvalidated alias (now a G-R2 `[schema]` finding) and reclassified a program patch as a resource, moving
        40,595 program patch-rounds of qpeexact_5 out of the headline while every check stayed clean. `cells` is
        read only where the schema defines it: a joint measurement's routing cells, and a `busy_region` event's
        region (a `rotate_boundary`'s region is its own `busy_region` field)."""
        if r.instruction_idx in self._recovery_instrs:
            return "recovery"
        evs = [self.es.events[e] for e in self.es.lss_reverse.get(r.instruction_idx, ())]
        pos = max([e.pos for e in evs], default=None)
        inits = [e for e in evs if e.kind == "init"]
        for e in inits:
            if tuple(r.patch) in set(self.es.lifetime_cells.get(e.raw.get("lifetime"), ())):
                return e.raw.get("role", "unknown")
        if len(inits) == 1 and not self.es.lifetime_cells.get(inits[0].raw.get("lifetime"), ()):
            return inits[0].raw.get("role", "unknown")
        for e in evs:
            if e.kind == "measure_product" and tuple(r.patch) in {tuple(c) for c in e.raw.get("cells", ())}:
                return "routing"
            region = e.raw.get("busy_region") if e.kind == "rotate_boundary" else e.raw.get("cells")
            if e.kind in ("rotate_boundary", "busy_region") and tuple(r.patch) in {tuple(c) for c in (region or ())}:
                lt = e.raw.get("lifetime")
                if lt and tuple(r.patch) in self.es.lifetime_cells.get(lt, ()):
                    return self.es.lifetime_role.get(lt, "unknown")        # the rotating patch's own cell keeps its role
                return "busy_region"
        if pos is not None:
            role = self.es.role_at_cell(r.patch, pos)
            if role:
                return role
        return "unknown"

    def _generate_syndrome_round(self):
        rounds, completed = super()._generate_syndrome_round()
        if self._hd is not None:                                            # (--h-depth): an H member's rounds in which
            si, fills = self.schedule_instructions, self._h_fill           #  no deep round ends are fillers: the decoder gets
            for r in rounds:                                               #  one syndrome round a deep round (hdepth.py); the
                du = r.instruction.duration                                #  ledger below still counts every ordinary round
                if type(du) is _hd.HDur and r.round - si[r.instruction_idx].start_round not in du.ends():
                    r.h_fill = True
                    fills[r.patch].append(r.round)
        if self.ledger is not None:
            L = self.ledger
            L.patch_rounds += len(rounds)
            n_np = 0                                                        # occupied non-program cells this round
            for r in rounds:
                if r.initialized_patch or r.patch not in self._role_of_patch:
                    self._role_of_patch[r.patch] = self._role_for(r)
                    if r.initialized_patch and self._strip_on and r.patch in self._strip_cells:
                        self._strip_init(r.patch, int(r.round))                 # the strip client: a data patch's first round
                role = self._role_of_patch[r.patch]
                L.role_patch_rounds[role] = L.role_patch_rounds.get(role, 0) + 1
                n_np += role != "program"
                if r.instruction_idx in self._recovery_instrs:
                    L.recovery_patch_rounds += 1
                    cls_ = self._recovery_class.get(r.instruction_idx, "other")
                    L.recovery_patch_rounds_by_class[cls_] += 1
                    ec = L.recovery_patch_rounds_by_event_class.setdefault(self._recovery_event[r.instruction_idx], {})
                    ec[cls_] = ec.get(cls_, 0) + 1                              # (records only)
                    owner = self._recovery_owner.get(r.instruction_idx)         # finding 4: the realised intrinsic cost of a
                    rec = self.values[owner].recovery if owner is not None else None   # recovery is its OWN instructions'
                    if rec is not None and rec["kind"] == "script":                    # executed syndrome rounds, accumulated
                        rec["realised"] += 1                                           # per recovery owner (P2 stays separate)
            n_res = len(set(self._reserved) - self._active_patches)
            L.reserved_cell_rounds += n_res
            if n_res > L.reserved_cells_peak:
                L.reserved_cells_peak = n_res
            if self._r7:
                self._r7_resv_round()                                      # (round 7's reservation durations, records only)
                if self._rec56 is not None:                                # (records only: the round's occupancy)
                    self._rec56_bin("live_values", len(self._live_frames), REC56_LIVE)
                    self._rec56_bin("reserved_cells", len(self._defer_ws_cells.keys() | self._defer_target_cells.keys()
                                                          | self._defer_locked.keys() | self._reserved.keys()), REC56_CELLS)
            if self.arm == "D" and (self._defer_ws_cells or self._defer_target_cells or self._defer_locked):   # records only
                hard = (self._defer_target_cells.keys() | self._defer_locked.keys()) - self._active_patches
                n_dec = len(hard)
                n_res = len((self._defer_ws_cells.keys() - self._active_patches) | hard)
                L.defer_reserved_cell_rounds += n_res
                L.defer_decided_cell_rounds += n_dec
                if n_res > L.defer_reserved_cells_peak:
                    L.defer_reserved_cells_peak = n_res
                if n_dec > L.defer_decided_cells_peak:
                    L.defer_decided_cells_peak = n_dec
            if n_np > self._np_peak:
                self._np_peak = n_np
            if self._r17 is not None:                                      # (---records: the union of occupied and held
                self._r17_ws(rounds)                                       #  non-program cells, records only; plan v2 7.3)
            if self.workspace_cap is not None:
                self._ws_after_round(rounds)
        return rounds, completed

    def _r17_ws(self, rounds):
        """(`---records`; plan v2 7.3, cap levels), records only: this round's non-program
        cells, occupied (a patch's syndrome round this round, the cells `peak_nonprogram_cells` counts) and held (the
        cells a live frame's reservation holds, SER-R's and CSR's `_resv_held`; none without a reservation), to the
        records' union peak (`R17Records.ws_round`), which counts a cell in both once. "Non-program" as the hard cap
        reads it (`--cap-hard`, `_ws_held`): a cell whose role is not "program", a held cell no patch has
        occupied yet included. Read once a syndrome round, so the held cells are those the round's dispatch left."""
        role = self._role_of_patch
        self._r17.ws_round(self.current_round, {r.patch for r in rounds if role.get(r.patch) != "program"},
                           {c_ for c_ in (self._resv_held or ()) if role.get(c_) != "program"})

    def is_done(self) -> bool:
        if not super().is_done():
            return False
        if self.ledger is None:
            return True
        self._stamp_program_done()
        if any(not v.settled for v in self.values.values() if v.speculated or v.state is VState.RECOVERING):
            return False
        if any(not v.delivered for v in self.values.values()):
            return False
        if self.ledger.settled_round < 0:
            self.ledger.settled_round = self.current_round
        return True

    def _event(self, kind, v, detail):
        if self.keep_events:
            self.ledger.events.append((self.current_round, kind, v.vid, detail))

    # ------------------------------------------------------------------ arm D: lazy Clifford-frame deferral (v9-deferral)
    # Nothing below runs for N or Q: every entry point is guarded by self.arm == "D".
    def _defer_setup(self, defer):
        self.defer_cfg = None
        self._u, self._u_on = None, False                  # U, the unified policy
        self._u_client, self._u6 = False, None             # U's client on (U v4: as U itself); U v6's state
        self._u6_noframe: dict = {}                        # U v6, rule 56a: vid -> why it got no frame (records on)
        self._u6_noreq: dict = {}                          # U v6, rule 56a: vid -> why its frame was never asked (records on)
        self._u4, self._u4_h, self._u4_lazy_on = False, None, False   # U v4 (rule 44): off unless `unified.h` is set
        self._w2 = None                                    # the decision (froq/wager2.py)
        self._qg, self._qg_q, self._qg_seen = None, 0, {}  # round 7's QG: theta ("always" or an int), the tick's count, verdicts
        if self.arm != "D":
            return
        cfg = dict(latency=1, settle="none", rule="lazy")
        cfg.update(defer or {})
        cfg["latency"] = int(cfg["latency"])
        assert cfg["latency"] >= 0 and cfg["settle"] in DEFER_SETTLES and cfg["rule"] in DEFER_RULES, cfg
        if self._ctrl_slow is not None:                                    # (--ctrl-slow s: the registration's latency,
            self._cs_defer = (cfg["latency"], math.ceil(cfg["latency"] * self._ctrl_slow))   # a fixed controller delay in rounds,
            cfg["latency"] = self._cs_defer[1]                             #  s times, rounded up)
        self.defer_cfg = cfg
        # rule 33's total horizon K_tot: a frame's walk is cut K_tot steps past its
        # registration boundary, and a rebased walk at j_e gets K_tot - j_e steps; None = off (D's walk has no depth limit)
        self._k_tot = None if cfg.get("k_tot") is None else int(cfg["k_tot"])
        assert self._k_tot is None or self._k_tot >= 1, cfg
        # the hybrid mode H: D-lazy plus an early materialization on a confident m = 1 prediction;
        # (rule 30): H-nci carries under D-nci's rule in every carry phase, before and after rebasing, and H-lead
        # triggers only with an estimated verdict lead above lambda (`hybrid.lead`, "d" = lambda = d)
        self._h = h = cfg.get("hybrid")
        self._h_lead = None
        if h is not None:
            from . import contract as _c
            assert cfg["rule"] in ("lazy", "no_cost_increase") and cfg["settle"] == "none", \
                "H carries its frames under D's lazy rule (rule 22) or, H-nci, its no-cost-increase rule (rule 30)"
            assert h.get("source") in ("client", "none") and 0.0 <= float(h.get("theta", -1.0)) <= 1.0, h
            assert int(h.get("install", H_INSTALL)) in (0, 1), h                # (0: a diagnostic variant, 8)
            assert _c.RECOVERY_MODE in ("cancel",) + _c.UR_MODES and _c.CONTRACT_MODE == "full", \
                "H runs the default recovery mode (or routed UR, and variants) and contracts"
            if h.get("lead") is not None:
                self._h_lead = self.d_t if h["lead"] == "d" else int(h["lead"])   # (an int: the tests' extremes; "d" in the arms)
                from .verdict_eta import Reader
                self._eta = Reader(self, LINK)
                self._lead_stats = collections.Counter()
                self._lead_hist = collections.Counter()      # the estimate at each lead check, rounded down to 8 rounds
        self._h_stats = collections.Counter()
        # part C (gap analysis rule 41): U, D-nci's carry with two ACT epochs (E1 at
        # registration, E2 at D-nci's stop), eligibility checks and a fixed-point expected-cost score; the hybrid mode's
        # splice, rebase and races carry an ACT out.  delta = "inf" switches the client off: U is D-nci
        self._u = u = cfg.get("unified")
        qg = cfg.get("qg")                                                 # round 7 (plan v3 3(b)): QG, the unified controller whose
        if qg is not None:                                                 # values register only if the backlog <= theta
            assert u is not None and (qg in ("never", "always") or (type(qg) is int and qg >= 0)), ("QG gates the unified controller", qg)
            if qg == "never":
                self._u = u = None                                         # (never: nothing registers, Wait-for-Decoder's path, no U)
            else:
                self._qg = qg
        if u is not None:
            if u.get("h") is not None:                                     # U v4 (rule 44a): the carry rule per value; the
                assert cfg["settle"] in DEFER_SETTLES and h is None and cfg["rule"] == ("lazy" if str(u["h"]) == "0" else "no_cost_increase"), \
                    ("U v4: h = 0 carries D-lazy's rule, other h D-nci's as the base", cfg)   # arm's rule is h = 0's or h = inf's
            else:
                assert cfg["rule"] == "no_cost_increase" and cfg["settle"] == "none" and h is None, ("U is D-nci's carry with its own epochs", cfg)
            self._u_setup(u)
        w2 = cfg.get("wager2")
        if w2 is not None:                                                 # the decision: D's frames, its
            from .wager2 import W2State                                    # decision once a round, and COMMIT through H's path
            assert u is None and h is None and cfg["settle"] in ("none", "decode"), ("the decision is its own controller", cfg)
            setting = w2.get("setting", "main")
            assert cfg["rule"] == ("no_cost_increase" if setting == "awf" else "lazy"), \
                ("the decision: its cap is D-lazy's limit, or D-nci's stop in the Advance While Flat setting", setting, cfg)
            assert setting == "wfd" or self._hw_cfg is not None, \
                ("the decision runs under rule 66's hardware-limit mode (--hw-limits): its tables hold boundaries 0 to 16 only", setting)
            self._w2 = W2State(self, w2, **self._w2_args)
            if setting == "main" and not self._noreg:                     # (V2q at m = inf: no frame, wfd's path)
                self._h = dict(source="client", wager2=True)               # H's splice, rebase and races carry a commit out (the
                                                                           # exact cases cannot commit: D's records, unchanged)
        self._frames: dict = {}                      # vid -> DeferFrame (live and finished)
        self._live_frames: set = set()               # vids of the frames not finished
        self._defer_arrivals: list = []              # (producer end round, vid) completed since the last phase
        self._defer_waiting: dict = {}               # vid -> first round it may register (retried while its T2 is another frame's)
        self._defer_hold_of: dict = {}               # instr -> {vid}: fence sets and held steps of live frames
        self._defer_target_cells: dict = {}          # cell -> count: workspaces of decided, not yet spliced materializations
        self._defer_locked: dict = {}                # cell -> {vid}: cells of spliced scripts still running (the script-cell lock)
        self._defer_perm_cache: dict = {}            # instr -> it waits for a live frame's lasting hold (this round)
        self._defer_target_need: set = set()         # what the patches on decided workspaces need to leave (exempt from the guard)
        self._defer_exempt_pins: set = set()         # instructions the guard let through although they pin a candidate workspace
        self._defer_ws_cells: dict = {}              # cell -> count: the candidate workspaces (every boundary) of the live frames
        self._defer_guarded: set = set()             # startup instructions the workspace guard held at least once
        self._defer_member: dict = {}                # instr -> vid: step members of live frames
        self._defer_claim: dict = {}                 # event id -> vid: the events of live frames' steps
        self._defer_cells = collections.defaultdict(list)          # cell -> original instructions on it, in program order
        for idx in range(self.n_original):
            for p in self.schedule_instructions[idx].instruction.patches:
                self._defer_cells[p].append(idx)
        self._defer_cell_ptr: dict = {}              # cell -> position of the first possibly unstarted instruction in that list
        self._defer_cell_scripts = collections.defaultdict(list)   # cell -> spliced script instructions on it, in splice order
        self._defer_stats = collections.Counter()
        self._late_open: dict = {}                       # (B12): instruction -> its admitted step's watch (`_late_admit`)
        self._rec56 = dict(fwd_attempts=0, fwd_admitted=0, sites=collections.Counter(), sites_retried=collections.Counter(),
                           live_values=[0] * len(REC56_LIVE), reserved_cells=[0] * len(REC56_CELLS), step_rounds=[0] * len(REC56_ROUNDS),
                           step_exec_rounds=[0] * len(REC56_ROUNDS)) if self._r7 else None   # (records only)
        self._rec56_retry: set = set()
        self._defer_native = collections.Counter()   # values not deferred, by reason
        self._defer_fences = collections.Counter()   # walk end reasons of the deferred frames
        self._defer_depth = collections.Counter()    # walk depth F of the deferred frames
        self._defer_log: list = []                   # one record per finished frame (capped in the summary)
        self._defer_peak_live = 0
        self._defer_live_hist = collections.Counter()   # (G, plan v2 A1.3): live frames -> rounds
        self._defer_violations = 0
        self._defer_edges: dict = {}                 # instr -> [(pred, kind, round)] of the edges the splices added (diagnostics)
        self._defer_open = None                      # original instructions not yet ended (`_stamp_program_done`)
        # instructions that have started, exactly: upstream starts the first instruction in its constructor without a
        # start_round, and a zero-duration instruction completed in the initial dispatch gets start = end = round - 1 = -1,
        # so start_round == -1 does not mean "not started" (the dispatch adds every started instruction here)
        self._defer_started: set = set(self._active_instructions)

    def _defer_phase(self, patches_in_use):
        """Phase A of arm D, after RESOLVE: register the frames whose merge ended `latency` rounds ago (the controller's
        bookkeeping), then advance every live frame: verdict, choice of boundary, splice, completion."""
        now = self.current_round
        self._r7_piu = patches_in_use                                      # (round 7's workspace guard reads them)
        if self._hw is not None:
            self._hw.batching = True                                       # rule 66: the phase's events wait for `_hw_serve`
        if self._noreg:
            self._defer_arrivals.clear()                                   # Wait-for-Decoder: no value is registered (section 7)
        for end, vid in self._defer_arrivals:
            self._defer_waiting.setdefault(vid, end + self.defer_cfg["latency"])
        self._defer_arrivals.clear()
        due = sorted(v_ for v_, r in self._defer_waiting.items() if r <= now)
        if due and self._w2 is not None:
            self._w2.ensure_built()                                        # the decision: its tables, before the first registration
        for vid in due:                                                    # (the keyed decoder they read is installed by then)
            before = dict(self._defer_native) if (self._u6 is not None and self._u6.instr) else None
            b56 = dict(self._defer_native) if self._rec56 is not None else None
            bdg = (dict(self._defer_native), {k: n for k, n in self._defer_stats.items() if k.startswith(("r7:rule_reg", "hw:"))}) \
                if self._dg else None
            got = self._defer_register(self.values[vid], patches_in_use)
            if bdg is not None:
                self._dg_reg_note(vid, got, *bdg)                          # (records only: the value's registration outcome)
            if self._r17 is not None:
                self._r17.registration(vid, got, now)                      # (records only: the admission stall)
            if b56 is not None:
                self._rec56_site(vid, got, b56)                            # (records only: the site's outcome)
            if got is not None:
                del self._defer_waiting[vid]
                if got is False and before is not None:                    # U v6, rule 56a: why this value got no frame
                    self._u6_noframe[vid] = next((k for k, n in self._defer_native.items() if n != before.get(k, 0)), "not_deferred")
        for vid in sorted(self._live_frames):
            self._defer_advance(self._frames[vid], patches_in_use)
        if self._hw is not None:
            self._hw_serve(patches_in_use)                                 # rule 66: the phase's events, in spec 9's order
        if self._u_on:
            self._u_settle_pending()                                       # U: ACTs the verdict or the deadline overtook
        self._defer_peak_live = max(self._defer_peak_live, len(self._live_frames))
        self._defer_live_hist[len(self._live_frames)] += 1
        # a decided materialization waits for its workspace to clear, and the next users of the patches on it clear it:
        # the resource initialisations those need are exempt from the guard that keeps new patches off that workspace
        need, need8, tg_off = set(), set(), False
        if self._defer_target_cells:
            for vid in self._live_frames:
                fr = self._frames[vid]
                if fr.state in ("materialize", "early_materialize"):
                    for c_ in fr.target_ws:
                        if c_ in self._active_patches or c_ in patches_in_use:
                            u = self._defer_first_unstarted(c_)
                            if u is not None:
                                need |= self._defer_cone(u)
                                if self._r8:                               # (round 8: every instruction to the patch's release)
                                    rel = self._r8_release(c_, self._defer_cell_ptr.get(c_, 0))[0]
                                    cone = self._defer_cone(rel, cap=self.R8_CAP)
                                    tg_off |= len(cone) >= self.R8_CAP
                                    need8 |= cone
        self._defer_target_need = need
        if self._r8:
            self._r8_need, self._r8_tg_off = need8 - need, tg_off
            if tg_off:
                self._defer_stats["r7:rule_tg_off_rounds"] += 1            # (a search at the cap: the target guard is off a round)

    _r7 = _r7_guard = False                                                # (round 7's settings: off in a device the tests build
    _r7_reserve, _qg = 0, None                                             #  without __init__)
    _r8, _r8_gsvc, _r8_tg_off, _r8_need = False, 0, False, frozenset()    # (round 8's: likewise)
    _guard = "ch"                                                          # (G's setting: likewise)
    _resv = _resv_held = None                                              # (SER-R's static reservation: off; likewise)
    _resv_inc = _ws_hard = False                                           # (plan v2 7.2 and 7.3: off)
    _live_cap = None                                                       # (the live-frame limit k: none, as `__init__` sets
                                                                           #  it; a device built without `__init__` reads this)
    _r9 = None                                                             # (round 9's bounded check: likewise)
    _cov_assert = False                                                    # (plan v2 1.4: the coverage assertion, on in
                                                                           #  every run with the bounded check (`__init__`); a
                                                                           #  device built by hand, a test's, sets it itself)
    R8_CAP = 4000                                                          # (rule (a)'s searches: at the cap, the safe answer)
    R7_GUARD_CAP = 2000                                                    # (candidate guard's search: at the cap it holds;
                                                                           #  rule (a)'s walk reads it too, `_r8_gcap`)

    def _r7_refuses(self, fr, a: int, b: int, count: bool = True) -> bool:
        """Round 7's progress settings (plan v3 3(e)), both off by default. The workspace guard refuses the block from a to
        b when a cell of boundary b's workspace (the frame's recovery cell there) carries work whose next user waits, itself
        or through unstarted predecessors, for an instruction a live frame holds or can still take back (`_r7_holdable`; test, records-only `_defer_ws_fenced`, saw only the held ones, so a step taken back
        after the FORWARD still fenced the cell: forced state). The T reserve r refuses it when its T injections
        would leave fewer than r T states for verified work (only under a finite T supply)."""
        if self._guard == "off" or self._resv is not None:
            pass                                                           # (OFF: no workspace guard and no rule (a);
                                                                           #  SER-R: no progress check, its footprint held instead)
        elif self._r7_guard and self._r9 is not None:
            if self._r9_fwd(fr, a, b, count):                              # (round 9's bounded check: one event of the port)
                return True
        elif self._r7_guard:
            if self._r8_gsvc and self._hw is not None and not self._hw.gsvc_try():
                self._defer_stats["r7:gsvc_waits"] += 1                    # (round 8's guard service: not served this round)
                if count:
                    fr.r8_ref = self.current_round
                return True
            if self._defer_ws_fenced(fr, b, self._r7_piu, held=self._r7_holdable):
                self._defer_stats["r7:guard_refusals"] += int(count)
                if count and self._r8:
                    fr.r8_ref = self.current_round
                return True
            if self._r8 and self._r8_fwd_refuses(fr, a, b, count):
                self._defer_stats["r7:rule_fwd_refusals" if count else "r7:rule_fwd_whatif"] += 1   # (round 8's rule (a): b's
                if count:                                                  #  certificate and the block's permission, test
                    fr.r8_ref = self.current_round                         #  passed; the decision's what-ifs run the same test)
                    fr.r8_fwd = (self.current_round, a, b)                 # (records only: the hold reason defer_rule_a)
                return True
        if self._r7_reserve and self.t_supply is not None:
            need = sum(1 for k in range(a, b) for i in fr.members[k] if self._ts_is_injection(i) and i not in self._ts_prepaid)
            if need and self._ts_level - need < self._r7_reserve:
                self._defer_stats["r7:reserve_refusals"] += int(count)
                return True
        return False

    def _script_class(self, c, low, res, j: int, repair: bool) -> str:
        """records only (plan v2 5.6; reliability model): the class of a spliced script, for
        `ledger.recovery_patch_rounds_by_class`. In order: commit_repair, the repair of a committed (rebased) contract;
        two_wire, a script on two or more program wires (its lowered script's wires, else its residual's axis);
        measurement_gadget, a one-wire script whose contract crossed a measurement before its boundary j
        (`_r7_measuring_steps`: an atom of a measuring gadget, the value's own excepted, or an elementary measurement);
        other, the rest (one-wire repairs)."""
        if repair:
            return "commit_repair"
        if len(set(low.patches) if low.patches else res.axis.wires) >= 2:
            return "two_wire"
        if any(k < j for k in self._r7_measuring_steps(c)):
            return "measurement_gadget"
        return "other"

    def _r8_note_decision(self, fr):
        """records only: the decision's last lookup before the frame's verdict (the target is reset at the verdict):
        its round, its action (W, C or A), the boundary r it priced (the frame's own, from `base` on half 1), and that
        action's priced terms at r in the realized units -- the placed script's patch-rounds (the contract's price vector;
        None for a COMMIT), its rounds and its hold rounds, as the tables read them: on half 0 the profile's S and XS + W_0
        for WAIT or ADVANCE and the rebased profile's R and XR + W_1 at r for COMMIT; on half 1 the rebased profile's R and XR + W_1 at base + r. XS and XR only when the hold is decoded, and S and
        XS 0 under the decision's zero setting."""
        w2, w = self._w2, fr.w2
        last = w.get("last") if w else None
        if not last or w.get("round") is None or w2 is None:
            return
        r, act = int(last[1]), str(last[2])
        half = int(last[6]) if len(last) > 6 else int(w.get("half", 0))  # (the lookup's half: a COMMIT's rebase may follow it)
        base = int(w.get("base", 0)) if half == 1 else 0
        pr = w2.tab.profiles.get(w["vid"]) if hasattr(w2.tab.profiles, "get") else None
        W0, W1 = ((w2.r2w or {}).get("W0"), (w2.r2w or {}).get("W1")) if getattr(w2, "r2w", None) else (None, None)
        decode, zero = self.defer_cfg.get("settle") == "decode", bool(getattr(w2.tab, "zero", False))
        p1 = sr = hr = None
        if pr is not None and (act == "C" or half == 1):
            rb, p = (pr["reb"].get(str(r)), r) if act == "C" else (pr["reb"].get(str(base)), base + r)
            if rb is not None and p < len(rb["R"]):
                sr = float(rb["R"][p])
                hr = float((rb["XR"][p] if decode and rb.get("XR") else 0) + (W1 or 0))
        elif pr is not None and r < len(pr["S"]):
            sr = 0.0 if zero else float(pr["S"][r])
            hr = float((pr["XS"][r] if decode and not zero and pr.get("XS") else 0) + (W0 or 0))
        if act != "C":
            p1 = int(fr.r[r]) if 0 <= r < len(fr.r) and fr.r[r] is not None else None
        fr.r8_last = (int(w["round"]), act, r, p1, sr, hr, half, base)

    R8_ROWS = 20000                                                        # (the record's row cap; the rows past it are counted)

    def _r8_last_decisions(self, frames) -> dict:
        rows = [[fr.vid] + list(fr.r8_last) for fr in frames if getattr(fr, "r8_last", None) is not None]
        return dict(last_decision=dict(fields=["vid", "round", "act", "r", "script_patch_rounds", "script_rounds", "hold_rounds", "half", "base"],
                                       rows=rows[:self.R8_ROWS], truncated=max(0, len(rows) - self.R8_ROWS))) if rows else {}

    def _r7_measuring_steps(self, c) -> list:
        """3(g) item 5 (records only): the indices of the walked contract's steps that cross a measurement -- a step that
        folds an atom of a measuring gadget (a C1 template with outcome bits: the walk's own `gadget_measures`; the value's
        own gadget excepted) or an elementary measurement event.  With the frame's final `admitted`, a contract crossed a
        measurement going FORWARD if one of these indices is below it."""
        out, cache = [], self.__dict__.setdefault("_r7_meas_cache", {})
        for k, s_ in enumerate(c.steps):
            for a in s_.atoms:
                if a in self.es.events:
                    m = self.es.events[a].kind in ("measure_product", "measure_single")
                elif "#" in a:                                             # (a gadget's atom: "<gadget id>#<segment>")
                    gid = a.rsplit("#", 1)[0]
                    if gid == c.gadget_id:
                        continue
                    if gid not in cache:
                        cache[gid] = any(self.sem.tables.measures(t.template_id) for t in self.es.atoms_of(gid))
                    m = cache[gid]
                else:
                    m = False
                if m:
                    out.append(k)
                    break
        return out

    def _r7_resv_round(self):
        """Round 7's records (plan v3 3(g) item 6), every round: each workspace cell's reservation from acquisition to
        release -- "candidate", a live frame's candidate workspace (the guard keeps patches off it); "exclusive", a decided
        materialization's target or a running script's cell (`_defer_target_cells`, `_defer_locked`); "reserved",
        the per-value path's reserved cells (`_reserved`)."""
        now = self.current_round
        for kind, cur in (("candidate", getattr(self, "_defer_ws_cells", {}).keys()),
                          ("exclusive", getattr(self, "_defer_target_cells", {}).keys() | getattr(self, "_defer_locked", {}).keys()),
                          ("reserved", self._reserved.keys())):
            op, ln = self._r7_resv[kind], self._r7_resv_len[kind]
            for c_ in op.keys() - cur:
                ln.append(now - op.pop(c_))
            for c_ in cur - op.keys():
                op[c_] = now

    def r7_resv_summary(self) -> dict:
        """The reservation durations (rounds, acquisition to release) by kind: count, mean, quantiles, the longest, and
        those still open at the end (their age).  "T": each program T injection's state, from its take (the injection's
        start, or the ACCEPT of its Q step under a finite supply) to the injection's end."""
        si, out = self.schedule_instructions, {}
        ts = [si[i].end_round - self._r7_ts_acq.get(i, si[i].start_round) for i in range(self.n_original)
              if si[i].instruction.name == "INJECT_T" and si[i].end_round != -1 and si[i].start_round != -1]
        now = self.current_round
        for kind, xs in list(self._r7_resv_len.items()) + [("T", ts)]:
            a = np.asarray(xs, dtype=float)
            q = np.quantile(a, (0.5, 0.9, 0.99)).tolist() if len(a) else [None] * 3
            opened = [now - r for r in self._r7_resv.get(kind, {}).values()]
            out[kind] = dict(n=len(a), mean=float(a.mean()) if len(a) else None, p50=q[0], p90=q[1], p99=q[2],
                             max=float(a.max()) if len(a) else None, open=len(opened), open_max=max(opened, default=None))
        return out

    def _r7_tick(self, held):
        """Round 7's progress tracer (plan v3 3(e), E1; records only): every 1,000 rounds the retired operations, the T
        buffer, the T states' holders (started injections not yet ended: speculative, an admitted step of a live frame,
        or verified) and the holds by kind; a snapshot of the waits once nothing has retired for 10,000 rounds."""
        now = self.current_round
        done = self._completed_instruction_count
        if done != self._r7_last_ret[1]:
            self._r7_last_ret = (now, done)
        stall = now - self._r7_last_ret[0]
        if (now % 1000 and stall != 10000) or len(self._r7_trace) >= 20000:
            return
        spec, live = set(), getattr(self, "_live_frames", set())         # (arm D's frames; N and Q have none)
        for vid in live:
            fr = self._frames[vid]
            for k in range(fr.admitted):
                spec |= fr.members[k]
        tl = self.t_supply is not None
        ht = [i for i in self._active_instructions if self._ts_is_injection(i)] if tl else []
        row = [now, done, float(self._ts_level) if tl else None, sum(1 for i in ht if i in spec), sum(1 for i in ht if i not in spec),
               {w: len(x) for w, x in sorted(held.items())}, len(live)]
        if stall == 10000:
            row.append(dict(frames={str(vid): self._frames[vid].state for vid in sorted(live)[:64]},
                            held={w: sorted(x)[:32] for w, x in sorted(held.items())}))
        self._r7_trace.append(row)

    def _qg_admit(self, v) -> bool:
        """Round 7's QG (plan v3 3(b)): FAC-F-Sd-L1's registration only if the raw `_runnable_pending` count, read where V2q
        reads its backlog (the decoder hook, `_after_full_starts`), is at most theta; "always" admits without a read.
        Decided once per value; a rejected value takes N's path for good."""
        vid = v.vid
        if vid not in self._qg_seen:
            ok = self._qg == "always" or self._qg_q <= self._qg
            self._qg_seen[vid] = ok
            if self._hw is not None:
                self._hw.charge_lookup()                                   # (its admission lookup, at V2q's latency, if charged)
            self._defer_stats["qg:admitted" if ok else "qg:rejected"] += 1
        return self._qg_seen[vid]

    def _defer_cone(self, u: int, cap: int = 500) -> set:
        """u and its unstarted predecessors, transitively (bounded)."""
        seen, stack = set(), [u]
        while stack and len(seen) < cap:
            x = stack.pop()
            if x in seen or x in self._defer_started:
                continue
            seen.add(x)
            stack.extend(self._adj()[0].get(x, ()))
        return seen

    def _defer_register(self, v: Value, patches_in_use=()):
        """None: retry next round (v's T2 is a step or a held instruction of another live frame, so it must run as that
        walk recorded it); True: deferred -- the S group is skipped and the frame is live; False: not deferred, v waits
        for its decoded result exactly as in N."""
        st = self._defer_stats
        if v.delivered:
            self._defer_native["decoded_before_registration"] += 1
            if self._r8 and v.vid in self._r8_reg_now and v.vid not in self._r8_wn_vids:
                self._r8_wn_vids.add(v.vid)                                # (a value the rule kept
                st["r7:rule_reg_wait_native"] += 1                         #  waiting took N's path, its decode first; of them, those
                if v.vid in self._r8_mem_vids:                             #  that waited on a member's or an ended frame's member's
                    st["r7:rule_reg_member_native"] += 1                   #  permission)
            return False
        if self._w2 is not None and self._w2.vqf is not None and not self._w2.vqr and not self._w2.admit(v):
            self._defer_native["vq_rejected"] += 1                         # (round 7's V2q: rejected at registration, N's path for good)
            return False
        if self._qg is not None and not self._qg_admit(v):
            self._defer_native["qg_rejected"] += 1                         # (round 7's QG: the backlog above theta, N's path for good)
            return False
        spec = self._specs[v.vid]
        if any(e in self._defer_claim for e in spec.t2.events) or any(i in self._defer_member or i in self._defer_hold_of for i in v.group):
            st["registration_retries"] += 1
            if self._w2 is not None and self._w2.r1:
                self._w2.register_blocked(v, spec.t2.events, v.group)      # (R1: do the bookings' additions stop it?)
            return None
        assert not any(i in self._defer_started for i in v.group), ("T2 group ran before its registration", v.vid)
        if self._live_cap is not None and len(self._live_frames) >= self._live_cap:
            self._ser_full += 1                                            # (SER: one live frame at a time; lf: k live
            self._defer_native["no_step:ser_full"] += 1                    #  frames; the value takes N's path for good, as at a full
            return False                                                   #  frame table)
        if self._hw is not None and len(self._live_frames) >= self._hw.cap_values:
            self._hw.stats["values_full"] += 1                             # rule 66: 64 live values; the value waits as N
            self._defer_native["no_step:hw_values_full"] += 1
            return False
        c = build_contract(self.es, self.sem, self.sched_view, spec, False, (DEFER_K if self._k_tot is None else self._k_tot), deferral=True,
                           claimed=self._defer_claim.keys(),
                           state=lambda i: 2 if self.schedule_instructions[i].end_round != -1 else (1 if i in self._defer_started else 0))
        if not c.has_site:
            self._defer_native["no_step:" + c.fence_reason.split(":")[0]] += 1
            if self._w2 is not None and self._w2.r1 and c.fence_reason.split(":")[0] == "frame_overlap":
                self._w2.register_blocked(v, overlap=c.fence_events)       # (R1: a booking's extra claim?)
            return False
        # the held dependency: the fence set (contract.fence_set, what Froq holds for an unresolved value), except the
        # resource initialisations outside the frame's light cone -- the next T's magic-state injection, a gadget's fresh
        # mediator -- which prepare patches and commute with the frame; they start as SWIPER starts them unless the
        # workspace guard (`_defer_hold`) keeps them off a live frame's workspace.  A gadget of the fence is never started
        # in part: a merge outside the cone that ran would leave its mediator alive, waiting for the held merge after it
        last = c.steps[-1]
        Lf, Df = set(last.L) | {v.wire}, set(last.D_fixed | last.D_cond) | {v.result_id}
        fence = {i for i in fence_set(self.es, c) if i not in self._defer_started
                 and not (self._is_startup_instruction[i] and not (self._instr_wires.get(i, frozenset()) & Lf)
                          and not (self._instr_preds.get(i, frozenset()) & Df))}
        F = c.F
        fr = DeferFrame(vid=v.vid, c=c, reg_round=self.current_round, members=[set(s.instrs) for s in c.steps],
                        r=[(c.entry_recovery.P1 if c.entry_recovery is not None else None)] + [s.price_P1 for s in c.steps],
                        fence=fence)
        fr.kof = {i: k for k, mem in enumerate(fr.members) for i in mem}
        # admission (Q's rule for the steps of a contract, without its policy, K or timer): a step's unstarted members
        # are held until the step is admitted, in order, once every prerequisite outside the frame's admitted steps has
        # run -- so an admitted step always completes, and a materialization never waits for held work
        fr.unadm = {i for k in range(F) for i in fr.members[k] if i not in self._defer_started}
        # another live frame's admitted step runs as that frame carries it: a value whose fence would hold one is not
        # deferred.  A frame whose unadmitted step this fence holds is cut short to a feasible boundary before that step
        cut = {}
        for i in fence:
            owner = self._defer_member.get(i)
            if owner is None:
                continue
            fa = self._frames[owner]; k = fa.kof.get(i)
            verdict = self._defer_fence_verdict(fa, k)
            if verdict == "refuse":
                self._defer_native["no_step:fence_on_admitted_step"] += 1
                return False
            if verdict == "refuse_prepared":                           # H only (a step of a prepared rebased contract)
                self._defer_native["no_step:fence_on_prepared_rebase"] += 1
                if self._w2 is not None and self._w2.r1:
                    self._w2.register_blocked(v, fence=fence)              # (R1: a booking's extra member?)
                return False
            if verdict == "cut":
                cut[owner] = min(cut.get(owner, F + 10 ** 6), k)
        cut_to = {}
        for owner, k in cut.items():
            fa = self._frames[owner]
            js = [j for j in range(fa.admitted, min(fa.top, k) + 1) if fa.feas[j]]
            if not js:
                self._defer_native["no_step:fence_cuts_frame"] += 1
                return False
            cut_to[owner] = js[-1]
        # the boundaries the frame can be materialized at: a placed script, and no workspace patch that waits for the
        # frame's own held work (the fence, the steps from j on) -- the script would wait for a patch that waits for it
        fr.feas = [x is not None for x in fr.r]
        cap = WALK_CAP if self._walk_cap is None else self._walk_cap
        if self._resv is not None:                                         # (SER-R, -account 2.2: the boundaries
            row = self._resv_row(fr, cap)                                  #  and the footprint from the walk key's row, from the
            if row.walk_cap:                                               #  contract and the layout only; no feasibility from the
                self._hw.stats["walk_cap"] += 1                            #  patches now and no tangle search.  A boundary pinned
            if row.completion:                                             #  now fails the free test, and the value asks again)
                self._defer_native["no_step:resv_completion_condition"] += 1   # (a step is admitted once its members' predecessors
                return False                                               #  have started; a completion condition is not one, and
            fr.resv_fp, fr.resv_js, fr.resv_unclear = row.fp, row.js, row.unclear   # the converter emits none)
            fr.feas = [j in row.ok for j in range(F + 1)]                  # (only the boundaries its own
            js, K0, capped = list(row.ok), F, False                        #  steps leave clear are feasible; its blocks end there)
            if self._resv_inc and js:                                      # (plan 7.2: it registers on its first boundary's
                fr.resv_fp = frozenset(self._defer_workspace(fr, 0 if fr.feas[0] else js[0]))   # footprint alone; the rest by advances)
            if row.js and not js:
                self._defer_native["no_step:resv_unclear"] += 1            # (no usable boundary: N's path, as with none feasible)
                return False
        else:
            for j in range(F + 1):
                if fr.feas[j] and self._defer_ws_blocked(fr, j, patches_in_use, splice=False):
                    fr.feas[j] = False
            K0, capped = self._defer_tangle(fr)
            js = [j for j in range(K0 + 1) if fr.feas[j]]
            if self._hw is not None and js and js[-1] > cap:
                self._hw.stats["walk_cap"] += 1                            # rule 66: ADVANCE is not eligible at boundary 16
                js = [j for j in js if j <= cap]
        if not js:
            self._defer_native["no_step:no_feasible_boundary"] += 1
            return False
        if self._r8:
            # round 8's rule (a1): the boundary the frame stands at once registered passes g, with its own held work and
            # candidate cells counted, before any state changes; otherwise the value waits this round, as Wait-for-Decoder
            # does, and registration is retried next round (records only: the hold reason defer_rule_a).
            # That boundary is 0, and no live frame's admitted member that has not started waits
            # there.  Each reason is counted apart; a value whose contract has no script at boundary 0 at all takes N's
            # path at once (the wait would last until its decode)
            if self._guard != "off" and self._resv is None and not self._r8_b0(fr):
                if fr.r[0] is None:                                        # (no script at boundary 0 for good: N's path at once)
                    self._defer_native["no_step:rule_no_boundary0_script"] += 1
                    st["r7:rule_reg_b0_native"] += 1                       # (the rule binds, and the
                    self._r8_b0n_vids.add(v.vid)                           #  value's waits are reported as the rule's)
                    return False
                st["r7:rule_reg_b0_waits"] += 1
                self._r8_reg_now[v.vid] = self.current_round
                self._r8_b0_vids.add(v.vid)
                st["r7:rule_reg_b0_values"] = len(self._r8_b0_vids)
                return None
            s0 = 0 if fr.feas[0] else js[0]                                # (0; and test_r8 with `_r8_b0` taken back:
                                                                           #  or the first block's end)
            if self._guard == "off":
                why = None                                                 # (OFF: no rule (a), no event)
            elif self._resv is not None:
                why = self._resv_reg(fr, js, patches_in_use)               # (SER-R: the footprint free, one event)
            elif self._r9 is not None:                                     # (round 9's bounded check: one event of the port)
                why = self._r9_reg(fr, s0, fence, F, js)
                if why == "g" and self._wq_why == "too_big":
                    self._defer_native["no_step:too_big"] += 1             # (account 4: it needs more than H, so
                    return False                                           #  the value waits for its verdict; never split or asked again)
            else:
                why = "g" if (self._r8_gsvc and self._hw is not None and not self._hw.gsvc_try()) else self._r8_reg_refuses(fr, s0, fence, F)
            if why == "g":
                st["r7:rule_reg_waits"] += 1
                self._r8_reg_now[v.vid] = self.current_round
                self._r8_reg_vids.add(v.vid)
                st["r7:rule_reg_wait_values"] = len(self._r8_reg_vids)
                return None
            if why in ("members", "orphans"):
                st["r7:rule_reg_member_waits"] += 1                        # (the patches live frames' admitted members can still
                self._r8_reg_now[v.vid] = self.current_round
                self._r8_mem_vids.add(v.vid)
                st["r7:rule_reg_member_values"] = len(self._r8_mem_vids)
                if why == "orphans":                                       # (the failing permission is
                    st["r7:rule_reg_orphan_waits"] += 1                    #  an ended frame's member, `_r8_cert`'s answer)
                    self._r8_orw_vids.add(v.vid)
                    st["r7:rule_reg_orphan_values"] = len(self._r8_orw_vids)
                return None
            fr.r8_b1 = fr.r8_wit = s0                                      #  put on s0's workspace; s0 is the frame's witness)
        fr.top = js[-1]
        fr.top_why = "dependency" if fr.top == F else (("tangle_search_cap" if capped else "tangle") if K0 < F else "infeasible")
        fr.top_why_initial = fr.top_why
        fr.stop = fr.top
        if self._u6 is not None:                                           # U v6 (rule 50): the carry rule by time
            s_nci = fr.top
            for a_, b_ in zip(js, js[1:]):
                if fr.r[b_] > fr.r[a_]:
                    s_nci = a_; break
            fr.u6_pred = self._u6.pred_at_registration(v, fr)
            fr.rule = self._u6.decide(v, fr, s_nci)
        else:
            fr.rule = self._u4_rule(v) if self._u4 else self.defer_cfg["rule"]   # U v4 (rule 44a): chosen per value, now
        if fr.rule == "no_cost_increase":                          # carry only through steps that do not make the correction dearer
            for a_, b_ in zip(js, js[1:]):
                if fr.r[b_] > fr.r[a_]:
                    fr.stop = a_; break
            u6 = self._u6                                              # rule 50c's exact control: an arm whose client is off
            native0 = u6 is not None and (u6.cf_stop0 or (u6.stop0_cleanup and not self._u_client))   # (U6-off) cannot act
            if fr.stop == 0 and not (self._u4 and self._u_on and not native0):
                self._defer_native["no_step:no_cost_increase_stop_at_0"] += 1   # nothing to carry: the S waits for its decode,
                return False      # as in N (U v4 keeps the frame at boundary 0 so that E2 can act there, rule 44b; U6-off,
                                  # which cannot act, does not, nor does 's counterfactual (a), cf_stop0)
        fr.limit = fr.stop
        if self._w2 is not None and self._w2.vqf is not None and self._w2.vqr and not self._w2.admit(v, self._w2.reach(fr)):
            self._defer_native["vq_rejected"] += 1                         # ((b): the readiness query's r on the candidate frame,
            return False                                                   #  then the admission; N's path for good)
        if self._w2 is not None:                                           # the decision: the frame's slot, its masks, and the first
            why = self._w2.register(v, fr)                                 # decision, which sets the limit (section 1.3)
            if why is not None:
                self._defer_native["no_step:" + why] += 1
                return False
            self._w2.decide(fr, patches_in_use, registration=True)
        if not fr.feas[0] and self._defer_first_block(fr) is None:        # it could wait for a guarded step: never stand unusable
            self._defer_native["no_step:no_usable_boundary_now"] += 1
            self._u4_refused(v, "no_usable_boundary_now")
            return False
        if self._defer_blocks_others(fr, patches_in_use):
            self._defer_native["no_step:blocks_frame_workspace"] += 1
            self._u4_refused(v, "blocks_frame_workspace")
            return False
        for owner, j in cut_to.items():                                    # (all checks passed: now cut the other frames short)
            fa = self._frames[owner]
            if j < fa.top:
                fa.top, fa.top_why = j, "shortened"; st["frames_shortened"] += 1
            fa.limit, fa.stop = min(fa.limit, j), min(fa.stop, j)
        self._frames[v.vid] = fr
        self._live_frames.add(v.vid)
        if self._resv is not None:
            self._resv_hold(fr)                                            # (SER-R: held from now to the frame's end)
        if self._w2 is not None:
            self._w2.stats["registered"] += 1
        if self._u4_lazy_on and fr.rule == "lazy":
            self._u4_lazy_reserve(fr)                                      # rule 44a: its materialization demand, reserved
        if self._u4:
            self._u_stats["carry:%s%s" % (fr.rule, ":stop0" if fr.stop == 0 else "")] += 1
        for i in fr.fence | fr.unadm:
            self._wq_hadd(i, v.vid)
        for s in c.steps:
            for i in s.instrs:
                self._defer_member[i] = v.vid
            for e in s.events:
                self._defer_claim[e] = v.vid
        # the frame's candidate workspaces (every boundary's script cells): the workspace guard (`_defer_hold`) keeps a
        # resource patch off them while its consumer waits for a live frame -- a fixed placement would otherwise wait
        # for a patch that waits for a frame
        fr.guard = set(fr.resv_fp) if self._resv is not None else \
            set().union(*(self._defer_workspace(fr, j) for j in range(F + 1) if fr.feas[j]))   # (SER-R: the held mask)
        if self._hw is not None:                                           # rule 66: the guard is written by the registration event,
            fr.hw_guard_on, fr.hw_wait = False, "registration"             # and the frame admits nothing until it completes
            if self._r8:
                fr.r8_pend = frozenset(fr.guard)                           # (rule (a4): the candidate guard holds from the request)
                self._r8_pend_add(fr.r8_pend, 1)
            self._hw.submit("registration", lambda fr=fr: self._hw_reg_done(fr), cells=dict(guard=fr.guard),
                            lazy=fr.u_lazy is not None, owner=fr.vid)
        else:
            for c_ in fr.guard:
                self._defer_ws_cells[c_] = self._defer_ws_cells.get(c_, 0) + 1
        v.speculated = True
        self._apply_branch(v, False)                                       # the S group runs with zero duration: deferred
        self._replan(v)
        fr.taint_wires, fr.taint_results = {v.wire}, {v.result_id}
        self._defer_admit(fr)
        st["frames"] += 1; st["steps_walked"] += F
        st["carried_over_64"] += c.refusals.get("carried_over_64", 0)
        st["no_boundary0_script"] += c.entry_recovery is None
        st["top_" + fr.top_why] += 1
        self._defer_fences[c.fence_reason] += 1
        self._defer_depth[F] += 1
        return True

    @staticmethod
    def _defer_fence_verdict(fa: DeferFrame, k) -> str:
        """What a registering frame's fence that holds step k of the live frame `fa` means: "refuse" (k is admitted, or
        fa has chosen a boundary after k: D's materialize, H's early_materialize), "refuse_prepared" (H: k is None, a step
        of a rebased contract prepared but not spliced), "cut" (fa carries, or H's early_decide, whose limit may be
        restored: fa is cut short before k) or "ok" (fa materializes at or before k: the step stays held anyway)."""
        if k is None:
            return "refuse_prepared"
        if k < fa.admitted or (fa.state not in ("carry", "early_decide") and fa.target > k):
            return "refuse"
        return "cut" if fa.state in ("carry", "early_decide") else "ok"

    def _defer_tangle(self, fr: DeferFrame, cap: int = 6000) -> tuple:
        """(K0, capped): K0 is the first step with an unstarted member that waits, through unstarted instructions, for the
        frame's own fence or for a member of a later step -- the frame can never be carried through it (F: none).  One
        bounded backward search shared by the steps; at the cap the answer is the safe one (the frame stops there) and
        `capped` says so: a search-cap stop is not a proven tangle (audit finding 4)."""
        clean = set()
        n = 0
        for k in range(fr.c.F):
            for m in fr.members[k]:
                if m in self._defer_started:
                    continue
                stack = [p for p in self._adj()[0].get(m, ()) if p not in self._defer_started]
                while stack:
                    x = stack.pop()
                    if x in clean:
                        continue
                    kx = fr.kof.get(x)
                    if kx is not None and kx <= k:
                        continue                                           # carried with (or before) step k
                    if x in fr.fence or kx is not None:
                        return k, False
                    clean.add(x); n += 1
                    if n > cap:
                        self._defer_stats["tangle_cap"] += 1
                        return k, True
                    stack.extend(p for p in self._adj()[0].get(x, ()) if p not in self._defer_started and p not in clean)
        return fr.c.F, False

    def _defer_blocks_others(self, fr: DeferFrame, patches_in_use) -> bool:
        """Would the new frame's held work keep a live patch on another live frame's candidate workspace?"""
        for c_ in self._defer_ws_cells:
            if c_ not in self._active_patches or c_ in self._defer_locked:
                continue
            u = self._defer_first_unstarted(c_)
            if u is not None and self._defer_waits_for(fr, u, 0):            # (its fence, or any of its steps)
                return True
        return False

    def _defer_admissible(self, fr: DeferFrame, k: int, virtually=frozenset(), lift=None) -> bool:
        """Step k can be admitted: each unstarted member's predecessors have started (a started instruction completes
        without waiting for anything) or are members of the frame's admitted steps, of step k itself or of `virtually`
        (the steps before k in the block being admitted), and no member is held by another live frame. lift (a what-if,
         R1 validation): {instruction: frames} whose holds on it are ignored.
        CSR (G1's admission half, 2.4): and no unstarted member places a patch on a cell another live frame
        holds (`_resv_other`); such a cell is an outside prerequisite that has not started, so the step waits unadmitted
        and nothing waits on it. A patch on the cell does not let the member pass, as it lets ordinary work at dispatch:
        that patch is the holder's own member's, and the frame's own unstarted DISCARD before the member on the cell
        could remove it after admission."""
        mem = fr.members[k]
        for i in mem:
            if i in self._defer_started:
                continue
            other = self._defer_hold_of.get(i)
            if other is not None and len(other) > (fr.vid in other):
                if lift is None or any(o != fr.vid and o not in lift.get(i, ()) for o in other):
                    return False
            if self._resv_held and self._resv_other(i, fr.vid):
                return False
            for p in self._adj()[0].get(i, ()):
                if p in self._defer_started or p in mem or p in virtually:
                    continue
                kp = fr.kof.get(p)
                if kp is not None and kp < fr.admitted:
                    continue
                return False
        return True

    def _defer_admit(self, fr: DeferFrame) -> None:
        """Admit the frame's next steps, in order and within its limit, a block at a time: the steps up to the next
        feasible boundary, once they are all admissible -- the frame always stands at a boundary it can be materialized
        at (or at 0 until its first block is admitted). Rule 66: nothing before the registration's event completed.
        the decision: a carrying frame admits only what this round's decision admitted (section 1.3's invariant)."""
        if self._wq is not None:                                           # (records: the steps its policy admits)
            self._wq.adv_decide(fr.vid, fr.admitted, min(fr.limit, len(fr.members)), self.current_round)
        if not fr.hw_guard_on:
            return                                                         # (rule 66: its registration event is pending)
        while fr.admitted < fr.limit:
            a, virt, b = fr.admitted, set(), fr.admitted
            while b < fr.limit and self._defer_admissible(fr, b, virt):
                virt |= fr.members[b]; b += 1
                if fr.feas[b]:
                    break
            if b == a or not fr.feas[b]:
                if self._w2 is not None and self._w2.r1:
                    self._w2.admit_blocked(fr)                             # (R1: did the bookings' holds stop it?)
                if self._resv_held and b < fr.limit:
                    self._resv_selfblock(fr, b)                            # (SER-R, records: step b waits on its own hold?)
                return
            if self._rec56 is not None:
                self._rec56["fwd_attempts"] += 1                           # (records only: the refusals' denominator)
            self._wq_fwhy = None
            if (self._r7_guard or self._r7_reserve) and self._r7_refuses(fr, a, b):
                if self._wq is not None:                                   # (records: the block's own request held it,
                    why = self._wq_fwhy                                    #  but for the steps that wait anyway, F5)
                    self._wq.adv_held(fr.vid, a, b, self.current_round, why,
                                      self._wq_adv_apart(fr, a, b) if why in ("port", "wq") else ())
                return                                                     # (round 7's guard or T reserve: FORWARD at +inf)
            if self._resv_inc and not self._resv_inc_adv(fr, a, b):
                return                                                     # (plan 7.2: the advance to b refused or in service)
            if fr.w2 is not None and fr.state == "carry" and not self.values[fr.vid].delivered:
                assert fr.w2["round"] == self.current_round and b <= fr.w2["lim"], \
                    ("the decision: an admission beyond this round's decision", fr.vid, fr.w2["round"], self.current_round, b, fr.w2["lim"])
            for k in range(a, b):
                for i in fr.members[k]:
                    if i in fr.unadm:
                        fr.unadm.discard(i)
                        s_ = self._defer_hold_of.get(i)
                        if s_ is not None:
                            if self._wq is not None and fr.vid in s_:
                                self._wq_h(fr.vid, i)                                         # (a hold mark cleared)
                            s_.discard(fr.vid)
                            if not s_:
                                del self._defer_hold_of[i]
            fr.admitted = fr.r8_wit = b                                    # (the FORWARD made b's certificate: its witness)
            if self._wq is not None:
                self._wq.adv_answer(fr.vid, a, b, self.current_round)      # (records: the grant collected)
                self._wq.g_admit(fr.vid, a, b, self.current_round)         # (G, records: the steps admitted)
            self._defer_stats["steps_admitted"] += b - a
            if self._rec56 is not None:
                self._rec56["fwd_admitted"] += 1
            if self._late_open is not None:
                for k in range(a, b):
                    self._late_admit(fr, k)

    def _wq_adv_apart(self, fr: DeferFrame, a: int, b: int) -> set:
        """(records only, the decision's price; the code check's F5): the steps of the block a to b - 1 whose start this
        round's port or queue hold cannot delay, because each unstarted member waits for something outside the request
        anyway, as dispatch's `blocked` would find it: a predecessor or a completion it is conditioned on that has not
        ended and is not a member of the block (or is one that waits so itself, or has started), or a cell in use
        (`_r7_piu`). `_defer_admissible` lets a started predecessor pass, since admission needs no more; the member's
        start still waits for its end. Reads only; admission is unchanged."""
        si, preds, piu, started = self.schedule_instructions, self._adj()[0], self._r7_piu, self._defer_started
        block = set().union(*fr.members[a:b])
        waits = {}

        def deps(x):
            return tuple(preds.get(x, ())) + tuple(getattr(si[x].instruction, "conditioned_on_completion_idx", ()))

        for i in sorted(block - started):
            stack = [i]                                                    # (depth first over the block's own unstarted
            while stack:                                                   #  predecessors: a chain within the block waits on
                x = stack[-1]                                              #  its first link's wait)
                if x in waits:
                    stack.pop()
                    continue
                if any(c_ in piu for c_ in si[x].instruction.patches):
                    waits[x] = True
                    stack.pop()
                    continue
                inner = [p for p in deps(x) if p in block and p not in started and p not in waits and si[p].end_round == -1]
                if inner:
                    stack.extend(inner)
                    continue
                waits[x] = any(si[p].end_round == -1 and (p not in block or p in started or waits[p]) for p in deps(x))
                stack.pop()
        return {k for k in range(a, b) if all(waits.get(i, False) for i in fr.members[k] if i not in started)}

    def _late_admit(self, fr: DeferFrame, k: int):
        """(B12, records only): step k of the frame, admitted this round, is due to end at this round plus its encoded
        duration; its instructions with a duration are watched until they have all ended (`_clean_completed_instructions`).
        A step taken back before it began is dropped, and timed again from its next admission."""
        s = fr.c.steps[k]
        rec = [0, self.current_round + s.duration, 0, self.current_round, s]   # [instructions not ended, due, latest end + 1,
                                                                           #  records: the admission round and the step]
        for i in s.instrs:
            if self._instruction_durations[i] > 0:
                e = self.schedule_instructions[i].end_round
                if e == -1:
                    rec[0] += 1
                    self._late_open[i] = rec
                elif e + 1 > rec[2]:
                    rec[2] = e + 1
        if rec[0] == 0 and rec[2]:
            self._late_end(rec)

    def _late_end(self, rec):
        L = self.ledger
        if self._rec56 is not None:                                        # (records only: the step's rounds from its
            st_ = [self.schedule_instructions[i].start_round for i in rec[4].instrs if self._instruction_durations[i] > 0]
            self._rec56_bin("step_rounds", rec[2] - rec[3], REC56_ROUNDS)  #  admission, and from its first start, to its end)
            if st_ and min(st_) >= 0:
                self._rec56_bin("step_exec_rounds", rec[2] - min(st_), REC56_ROUNDS)
        L.defer_steps_ended += 1
        late = rec[2] - rec[1]
        if late > 0:
            L.defer_steps_late += 1
            L.defer_late_rounds += late
            if late > L.defer_late_max:
                L.defer_late_max = late

    def _rec56_bin(self, key: str, x: int, edges: tuple):
        self._rec56[key][max(0, bisect.bisect_right(edges, x) - 1)] += 1

    def _dg_reg_note(self, vid: int, got, before: dict, before_st: dict):
        """(plan v2 6, E12; diag only): a value's registration outcome -- "registered", or the reason it took N's path
        (the `_defer_native` key its attempt counted, "not_deferred" if none) -- with the rounds it waited before it (an
        attempt that returned None: a retry) and the last wait's reason (the rule (a) or hardware counter it moved)."""
        row = self._dg_reg.setdefault(vid, [None, 0, None])
        if got is None:
            row[1] += 1
            moved = [k for k, n in self._defer_stats.items() if k.startswith(("r7:rule_reg", "hw:")) and n != before_st.get(k, 0)]
            row[2] = ",".join(sorted(moved)) or "wait"
            return
        row[0] = "registered" if got else next((k for k, n in self._defer_native.items() if n != before.get(k, 0)), "not_deferred")

    def _rec56_site(self, vid: int, got, before: dict):
        """(plan v2 5.6, records only): an eligible site's registration outcome, once -- "registered", or the reason it
        took N's path (the `_defer_native` key its attempt counted); a site that waited a round or more (None: a retry)
        is counted again under `sites_retried` by its outcome."""
        if got is None:
            self._rec56_retry.add(vid)
            return
        why = "registered" if got else next((k for k, n in self._defer_native.items() if n != before.get(k, 0)), "not_deferred")
        self._rec56["sites"][why] += 1
        if vid in self._rec56_retry:
            self._rec56_retry.discard(vid)
            self._rec56["sites_retried"][why] += 1

    def _rec56_summary(self) -> dict:
        """(plan v2 5.6, records only): FORWARD attempts (the guard's and the rule's refusals' denominator) and those
        admitted; the eligible sites by outcome; fixed-bin histograms (lower edges in `bins`) of the live values and the
        reserved cells a round (candidate, exclusive and per-value cells together), of each admitted step's rounds from its
        admission and from its first start to its end, and of the reservation durations by kind (released;
        those open at the end apart); the overflow counts (rule 66's limits) and the decision's profile slots per branch, which
        are allocated once a run (sites without a walk slot take N's path; a boundary without a rebased slot no COMMIT)."""
        r, hist = self._rec56, (lambda xs: [sum(1 for x in xs if lo <= x < hi) for lo, hi in zip(REC56_ROUNDS, REC56_ROUNDS[1:] + (math.inf,))])
        out = dict(r, sites=dict(sorted(r["sites"].items())), sites_retried=dict(sorted(r["sites_retried"].items())),
                   resv_rounds={k: hist(xs) for k, xs in self._r7_resv_len.items()}, resv_open_at_end={k: len(op) for k, op in self._r7_resv.items()},
                   bins=dict(live_values=list(REC56_LIVE), reserved_cells=list(REC56_CELLS), rounds=list(REC56_ROUNDS)))
        if self._hw is not None:
            out["overflow"] = dict(values_full=int(self._hw.stats["values_full"]), resv_slots_full=int(self._hw.stats["resv_full"]),
                                   cells_over=int(self._hw.stats["cells_over"]))
        tab = getattr(self._w2, "tab", None) if self._w2 is not None else None
        if tab is not None:
            out["profiles"] = dict(cap=int(tab.S0), walk=int(tab.K0), rebased=int(tab.K1), walk_overflow=int(tab.stats["sites_no_slot"]),
                                   rebased_overflow=int(tab.stats["commit_no_slot"]),
                                   **({"cap_rebased": int(tab.S1)} if tab.S1 != tab.S0 else {}))   # (the limits file's counts)
        return out

    def _clean_completed_instructions(self, completed_instructions: set[int] = set()):
        super()._clean_completed_instructions(completed_instructions)
        if self._late_open:                                                # (B12): watched steps' instructions that ended
            for i in completed_instructions:
                rec = self._late_open.pop(i, None)
                if rec is not None:
                    e = self.schedule_instructions[i].end_round + 1
                    if e > rec[2]:
                        rec[2] = e
                    rec[0] -= 1
                    if rec[0] == 0:
                        self._late_end(rec)

    def _defer_current(self, fr: DeferFrame) -> int:
        """The boundary the frame stands at: after the last admitted step an instruction of which has started (the
        admitted steps after it have not begun and can still be held back)."""
        k = fr.admitted
        while k > 0 and not any(i in self._defer_started for i in fr.members[k - 1]):
            k -= 1
        return k

    def _defer_unadmit(self, fr: DeferFrame, j: int) -> None:
        """Hold back the admitted steps from j on (none has begun): the correction is materialized before them."""
        if self._resv is not None and self._wq is not None:
            self._resv_secure(fr, self._resv_inbuf(self._resv_taken(fr, j)))   # (item 2: asked of every path; the decision and the
        for k in range(j, fr.admitted):                                    #  revision check it first and refuse or wait, so this is no more)
            for i in fr.members[k]:
                assert i not in self._defer_started, ("a step taken back had begun", fr.vid, k, i)
                fr.unadm.add(i)
                if self._late_open is not None:
                    self._late_open.pop(i, None)                           # (B12: re-timed if admitted again)
                self._wq_hadd(i, fr.vid)
        self._defer_stats["steps_taken_back"] += fr.admitted - j
        if self._wq is not None:
            self._wq.g_back(fr.vid, j, fr.admitted)                        # (G, records: the steps taken back)
        fr.admitted = j
        fr.r8_wit = min(fr.r8_wit, j)                                      # (j's certificate, made when the frame reached it)

    def _defer_first_block(self, fr: DeferFrame):
        """The first feasible boundary the frame reaches by admitting steps now (before it registers), or None."""
        j, virt = fr.admitted, set()
        while j < fr.limit and self._defer_admissible(fr, j, virt):
            virt |= fr.members[j]; j += 1
            if fr.feas[j]:
                return j
        return None

    def _defer_reachable(self, fr: DeferFrame) -> int:
        """The furthest boundary up to `top` the frame reaches by admitting, now, every step it can (a what-if)."""
        j, virt = fr.admitted, set()
        while j < fr.top and self._defer_admissible(fr, j, virt):
            virt |= fr.members[j]; j += 1
        return j

    def _defer_boundary(self, fr: DeferFrame, j: int):
        """(script, residual, placing event, final frame) of boundary j: 0 = the skipped S itself, j >= 1 = after step j - 1."""
        c = fr.c
        if j == 0:                                                     # (H's rebased contract: placed at its early boundary's event)
            return c.entry_recovery, c.entry, (c.entry_at or self._specs[fr.vid].t2.events[-1]), c.entry_frame_after
        s = c.steps[j - 1]
        return s.recovery, s.residual, s.at_event, s.frame_after

    def _defer_workspace(self, fr: DeferFrame, j: int) -> frozenset:
        """The cells of boundary j's script other than the program cells of the residual it corrects (a routed UR list:
        of its own wires, all on the frame's L --)."""
        low, res, at, _ = self._defer_boundary(fr, j)
        if low is None or not low.instrs:
            return frozenset()
        prog = {tuple(c_) for w in (low.wires or res.axis.wires) for c_ in self.es.wire_cell(w, at)}
        return frozenset(c_ for c_ in low.cells if tuple(c_) not in prog)

    def _defer_first_unstarted(self, cell):
        """The first original instruction on `cell` that has not started (they start in program order: the .lss DAG
        chains every instruction to the previous one on each of its patches), or None."""
        lst = self._defer_cells.get(cell, ())
        p = self._defer_cell_ptr.get(cell, 0)
        while p < len(lst) and lst[p] in self._defer_started:
            p += 1
        self._defer_cell_ptr[cell] = p
        return lst[p] if p < len(lst) else None

    def _defer_last_started(self, cell):
        """The original instruction that started last on `cell`, or None."""
        self._defer_first_unstarted(cell)
        p = self._defer_cell_ptr.get(cell, 0)
        return self._defer_cells[cell][p - 1] if p else None

    def _defer_waits_for(self, fr: DeferFrame, u: int, bound: int, cap: int = 4000) -> bool:
        """Does instruction u wait, through unstarted predecessors, for the frame's own work that stays held while it is
        materialized at boundary `bound` -- its fence, or an unstarted member of a step from `bound` on (held, or taken
        back if admitted)?  A bounded backward search; at the cap the answer is the safe one (yes)."""
        seen, stack = set(), [u]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            if x in fr.fence or fr.kof.get(x, -1) >= bound:                # (x is unstarted: reached through unstarted preds)
                return True
            if len(seen) > cap:
                self._defer_stats["waits_search_cap"] += 1
                return True
            for p in self._adj()[0].get(x, ()):
                if p not in self._defer_started and p not in seen:
                    stack.append(p)
        return False

    def _defer_ws_blocked(self, fr: DeferFrame, j: int, patches_in_use, splice: bool) -> bool:
        """splice False (can j be chosen?): a workspace cell of boundary j holds a patch, or runs an instruction, whose
        patch would not leave while the frame is materialized at j -- no later original instruction uses the cell, or the
        next one waits for the frame's own held work.  splice True: any workspace cell still carries a live patch or runs
        an instruction; the script waits a round."""
        for c_ in self._defer_workspace(fr, j):
            active, busy = c_ in self._active_patches, c_ in patches_in_use
            if not active and not busy:
                continue
            if splice:
                return True
            if c_ in self._defer_locked:
                continue                                                   # a running script's cell: it ends
            u = self._defer_first_unstarted(c_)
            if u is None:
                if active and not busy:
                    return True                                            # a patch no later instruction uses
                continue
            if self._defer_waits_for(fr, u, j):                            # (a running instruction's patch too: its next user)
                return True
        return False

    def _defer_is_perm(self, x: int) -> bool:
        """x is held by a live frame: its fence, or a step not admitted yet -- which stays held until the frame finishes
        if the correction is materialized before it, so the guard treats both as lasting."""
        return x in self._defer_hold_of

    def _defer_pinned(self, u: int, cap: int = 2000) -> bool:
        """Does instruction u wait, itself or through unstarted predecessors, for an instruction a live frame holds?
        Cached for the round; at the cap the answer is no (counted)."""
        if self._r9 is not None and self._r9.ev is not None:
            return self._r9_pinned_walk(u, None)                           # (round 9: charged, uncached, inside an event)
        cache = self._defer_perm_cache
        if cache.get(-1) != self.current_round:
            cache.clear(); cache[-1] = self.current_round
        if u in cache:
            return cache[u]
        res = cache[u] = self._defer_pinned_walk(u, cap, count=True)
        return res

    def _defer_pinned_walk(self, u: int, cap: int = 2000, count: bool = False, held=None) -> bool:
        """`_defer_pinned`'s search, uncached; at the cap the answer is no (counted if `count`).  `held` (round 7's guard,
        `_r7_holdable`) replaces `_defer_is_perm`, and at the cap the answer is then yes (the guard's safe side)."""
        if self._r9 is not None and self._r9.ev is not None:
            return self._r9_pinned_walk(u, held)                           # (round 9: inside a bounded-check event)
        seen, stack, res, is_held = set(), [u], False, held or self._defer_is_perm
        while stack:
            x = stack.pop()
            if x in seen or x in self._defer_started:
                continue
            seen.add(x)
            if is_held(x):
                res = True; break
            if len(seen) > cap:
                if count:
                    self._defer_stats["guard_search_cap"] += 1
                if held is not None:
                    self._defer_stats["r7:guard_search_cap"] += 1
                    res = True
                break
            stack.extend(p for p in self._adj()[0].get(x, ()) if p not in self._defer_started and p not in seen)
        return res

    def _r9_pinned_walk(self, u: int, held) -> bool:
        """`_defer_pinned_walk` inside a bounded-check event: charged, with no cap of its own."""
        r9, n0 = self._r9, self.n_original
        seen, stack, is_held = set(), [u], held or self._defer_is_perm
        pred, started = self._adj()[0], self._defer_started
        while stack:
            x = stack.pop()
            if x in seen or x in started:
                continue
            seen.add(x)
            r9.node(x, n0)
            if is_held(x):
                return True
            ps = pred.get(x, ())
            r9.charge(R9A["edge"] * len(ps))
            stack.extend(p for p in ps if p not in started and p not in seen)
        return False

    def _defer_ws_fenced(self, fr: DeferFrame, j: int, patches_in_use, held=None) -> bool:
        """(F_8 gate 1, records only): a workspace cell of boundary j carries a patch, or runs an instruction,
        whose next user waits, itself or through unstarted predecessors, for an instruction a live frame holds -- the
        candidate guard keeps such a patch off a free cell only while that wait exists when it arrives, and a step taken
        back (`_defer_unadmit`) or a frame's guard written over an occupied cell starts one later. Uncached, so that the
        guard's own answers in the round are the deploy's."""
        ev = self._r9.ev if self._r9 is not None else None
        for c_ in self._defer_workspace(fr, j):
            if ev is not None:
                self._r9.charge(R9A["cell"])                               # (the cell table's occupant and first user)
            if (c_ in self._active_patches or c_ in patches_in_use) and c_ not in self._defer_locked:
                u = self._defer_first_unstarted(c_)
                if u is not None and self._defer_pinned_walk(u, held=held):
                    return True
        return False

    def _r8_hold(self, idx: int, p) -> bool:
        """Round 8's candidate guard (rule (a)): the patch idx would put on the free candidate cell p is released only after
        work that can be held, or a guard's hold, or never (the patch's whole release, not only its next user)."""
        rel, freed = self._r8_release(p, bisect.bisect_left(self._defer_cells.get(p, ()), idx) + 1)
        if self._r9 is not None and self._r9.ev is not None:
            self._r9.charge(R9A["release"] * (2 if freed else 1))         # (the release lookup)
        return not freed or self._r8_walk([rel], self._r7_holdable)

    def _r7_keeps_off(self, u: int) -> bool:
        """Round 7's guard on the candidate workspaces: a patch whose next user u waits for work a live frame can
        still take back stays off a free candidate cell too, so that no take-back fences a live frame's recovery cell
        after its FORWARD (the candidate guard alone admits it: an admitted step that has not begun is not held)."""
        if self._defer_pinned_walk(u, cap=self.R7_GUARD_CAP, held=self._r7_holdable):
            self._defer_stats["r7:guard_kept_off"] += 1
            return True
        return False

    def _r7_holdable(self, x: int) -> bool:
        """Round 7's guard (plan v3 3(e): work that can be held): x is held by a live frame (`_defer_is_perm`), or is a
        member of an admitted step its frame can still take back -- from the frame's current boundary on, where no step
        has begun (`_defer_current`, `_defer_unadmit`)."""
        if x in self._defer_hold_of:
            return True
        vid = self._defer_member.get(x)
        if vid is None or vid not in self._live_frames:
            return False
        fr = self._frames[vid]
        return self._defer_current(fr) <= fr.kof.get(x, -1) < fr.admitted

    # ------------------------------------------------------------------ round 8's progress rule (a)
    def _r8_release(self, cell, k: int):
        """(the patch's release from position k of `cell`'s instructions on: the first DISCARD there, True), or (the
        cell's last instruction, False) when no DISCARD frees it, or (None, False) past the end.  Only a DISCARD takes a
        patch off a cell, so a workspace cell clears when its release has run."""
        lst = self._defer_cells.get(cell, ())
        if k >= len(lst):
            return None, False
        d = self._r8_disc.get(cell)
        if d is None:
            d = self._r8_disc[cell] = [n for n, i in enumerate(lst) if self.schedule_instructions[i].instruction.name == "DISCARD"]
        n = bisect.bisect_left(d, k)
        return (lst[d[n]], True) if n < len(d) else (lst[-1], False)

    def _r8_places(self, x: int, q) -> bool:
        """Does x put a new patch on cell q: no instruction before it there, or a DISCARD just before it, or q is free and
        x is its next one?"""
        lst = self._defer_cells.get(q, ())
        k = bisect.bisect_left(lst, x)
        if k == 0 or self.schedule_instructions[lst[k - 1]].instruction.name == "DISCARD":
            return True
        return q not in self._active_patches and q not in self._r7_piu and self._defer_first_unstarted(q) == x

    def _r8_guardable(self, x: int) -> bool:
        """x is an instruction the candidate guard can hold (`_defer_hold`): original, not a frame's, not an ended frame's
        admitted member (it keeps its permission), and not a zero-duration cleanup."""
        return x < self.n_original and x not in self._defer_member and x not in self._r8_orph \
            and (self._is_startup_instruction[x] or self._instruction_durations[x] > 0)

    def _r8_walk(self, starts, held, guard=()) -> bool:
        """Rule (a)'s wait search: from `starts` back through unstarted predecessors, and from an unstarted placement onto a
        guarded cell (a live frame's candidate or target cell, a pending one, or `guard`) on to the release of the patch it
        places there, since the candidate guard holds it while that release waits. True when the search reaches work
        `held` names, a patch nothing releases, or the cap (the safe side, counted). It follows the guard as the guard
        acts: only what `_defer_hold` can hold is followed (`_r8_guardable`: an original instruction that is no
        frame's member and places a patch; a member, admitted, is never held, and an unadmitted one is held work), and
        only its placements that put a new patch on a guarded cell (`_r8_places`), which the guard checks when the cell
        is free; the target guard's hold of a placement a materializing frame needs is lifted (`_r8_need`), so there the
        candidate guard's rule is the one that holds."""
        seen = set()
        try:
            if self._r9 is not None and self._r9.ev is not None:
                return self._r9_search(starts, held, guard, seen)          # (round 9: inside a bounded-check event)
            return self._r8_search(starts, held, guard, seen)
        finally:                                                           # (records only: the searches' cost)
            st, n = self._defer_stats, len(seen)
            st["r7:rule_rec_walks"] += 1
            st["r7:rule_rec_nodes"] += n
            if n > st["r7:rule_rec_cone_max"]:
                st["r7:rule_rec_cone_max"] = n

    def _r8_search(self, starts, held, guard, seen) -> bool:
        """`_r8_walk`'s search, filling `seen`."""
        stack = list(starts)
        pred, si, started = self._adj()[0], self.schedule_instructions, self._defer_started
        ws, tg, pd = self._defer_ws_cells, self._defer_target_cells, self._r8_pend
        while stack:
            x = stack.pop()
            if x in seen or x in started:
                continue
            seen.add(x)
            if held(x):
                return True
            if len(seen) > self.R8_CAP:
                self._defer_stats["r7:rule_search_cap"] += 1
                return True
            if self._r8_guardable(x):
                for q in si[x].instruction.patches:
                    if (q in ws or q in tg or q in pd or q in guard) and self._r8_places(x, q):
                        if (q in ws or q in guard) and self._r8_gcap(x, q):
                            self._defer_stats["r7:rule_guard_cap"] += 1    # (the candidate guard's
                            return True                                    #  own search stops at its cap and holds it)
                        rel, freed = self._r8_release(q, bisect.bisect_left(self._defer_cells.get(q, ()), x) + 1)
                        if not freed:
                            return True                                    # (its patch stays: the guard would hold it for good)
                        stack.append(rel)
            stack.extend(p for p in pred.get(x, ()) if p not in started and p not in seen)
        return False

    def _r9_search(self, starts, held, guard, seen) -> bool:
        """`_r8_search` inside a bounded-check event: every access charged to the event (froq/r9check.py's ACCESS),
        no node cap (the event's budget and the window stop it), and the candidate guard's own capped search not replayed
        (`_r8_gcap`): a guarded placement on an installed certificate's chains is release work, admitted without a walk
        (`_r9_dispatch`), so no cap of its own can hold it. The guarded placements it follows are kept for the event's
        covered placements (`_r9_cover`)."""
        r9, n0 = self._r9, self.n_original
        ev = r9.ev
        stack = list(starts)
        pred, si, started = self._adj()[0], self.schedule_instructions, self._defer_started
        ws, tg, pd = self._defer_ws_cells, self._defer_target_cells, self._r8_pend
        while stack:
            x = stack.pop()
            if x in seen or x in started:
                continue
            seen.add(x)
            r9.node(x, n0)
            if held(x):
                return True
            if self._r8_guardable(x):
                pts = si[x].instruction.patches
                r9.charge(R9A["cell"] * len(pts))
                for q in pts:
                    if (q in ws or q in tg or q in pd or q in guard) and self._r8_places(x, q):
                        rel, freed = self._r8_release(q, bisect.bisect_left(self._defer_cells.get(q, ()), x) + 1)
                        r9.charge(R9A["release"] * (2 if freed else 1))
                        if not freed:
                            return True                                    # (its patch stays: the guard would hold it for good)
                        ev.followed.add((x, q))
                        stack.append(rel)
            ps = pred.get(x, ())
            r9.charge(R9A["edge"] * len(ps))
            stack.extend(p for p in ps if p not in started and p not in seen)
        return False

    def _r8_gcap(self, x: int, q) -> bool:
        """would the candidate guard's r7 test on x's placement on candidate cell q
        (`_r7_keeps_off`, `_defer_pinned_walk` from the patch's next user) stop at its cap (`R7_GUARD_CAP`), where it answers
        hold? The walk then counts the placement as held, as the guard does; held work within the cap the walk finds
        itself, since the patch's release waits on that next user. Cached per state."""
        u = self._defer_next_on_cell(x, q)
        if u is None:
            return False
        key, c = (getattr(self, "current_round", None), len(self._defer_started)), self.__dict__.get("_r8_gcap_c")
        if c is None or c[0] != key:
            c = self._r8_gcap_c = (key, {})
        if u not in c[1]:
            seen, stack, started, pred = set(), [u], self._defer_started, self._adj()[0]
            while stack and len(seen) <= self.R7_GUARD_CAP:
                y = stack.pop()
                if y in seen or y in started:
                    continue
                seen.add(y)
                stack.extend(p for p in pred.get(y, ()) if p not in started and p not in seen)
            c[1][u] = len(seen) > self.R7_GUARD_CAP
        return c[1][u]

    def _r8_refuses_at(self, fr, j: int, own=None, guard=(), held=None) -> bool:
        """g(frame, j), rule (a)'s one decision term: a workspace cell of boundary j carries a patch, or runs an instruction,
        whose release waits, through unstarted predecessors and guarded placements, on work a live frame holds or can still
        take back (`_r7_holdable`, test, which looks only at the next user) or on `own` (the frame's own work before
        it registers or revises); or nothing releases it. A locked cell's script ends (A4). `held` replaces both (a
        FORWARD's: `_r8_fwd_held`). the certificate's half for the patches there now; `_r8_cert` checks the patches
        authorized instructions can still put there."""
        self._defer_stats["r7:rule_rec_checks"] += 1                       # (records only)
        if held is None:
            held = self._r7_holdable if own is None else (lambda x: self._r7_holdable(x) or own(x))
        r9 = self._r9 if (self._r9 is not None and self._r9.ev is not None) else None
        for c_ in self._defer_workspace(fr, j):
            if r9 is not None:
                r9.charge(R9A["cell"])                                     # (the cell table's entry)
            active, busy = c_ in self._active_patches, c_ in self._r7_piu
            if not (active or busy) or c_ in self._defer_locked:
                continue
            self._defer_first_unstarted(c_)
            rel, freed = self._r8_release(c_, self._defer_cell_ptr.get(c_, 0))
            if r9 is not None:
                r9.charge(R9A["release"] * (2 if freed else 1))
            if rel is None:
                lst = self._defer_cells.get(c_, ())
                if (active and not busy) or (busy and lst and self.schedule_instructions[lst[-1]].instruction.name != "DISCARD"):
                    return True                                            # (a patch no later instruction uses: it never leaves;
                continue                                                   #  nor does the one a running last instruction leaves)
            if not freed or self._r8_walk([rel], held, guard):
                return True
        return False

    def _r8_reg_refuses(self, fr, s0: int, fence, F: int):
        """Rule (a1)'s two halves at a registration, as one method (test_r8_cert's model registers through it):
        boundary s0's present patches (g, with the frame's own held work, its fence and its steps from s0, counted and
        its candidate cells guarded), then the permissions on ws(s0) (`_r8_cert`). None when both pass, else "g" or
        "members"."""
        own = lambda x: x in fence or fr.kof.get(x, -1) >= s0
        gc = set().union(*(self._defer_workspace(fr, j) for j in range(F + 1) if fr.feas[j]))
        if self._r8_refuses_at(fr, s0, own=own, guard=gc):
            return "g"
        got = self._r8_cert([(self._defer_workspace(fr, s0), lambda x: self._r7_holdable(x) or own(x), gc, own)], self._r8_perms())
        if not got:
            return "orphans" if got[1] is None else "members"              # (an ended frame's member's permission failed)
        return None

    def _r8_rev_refuses(self, fr, tmp, j: int, new_holds, F2: int) -> bool:
        """Rule (a2) at a revision (the decision's commit, H's choice, U's ACT), as one method (test_r8_cert's commit-boundary
        case calls it). With the holds the revision adds: the revised boundary 0 (tmp's, the undo at j) and the commit
        boundary j pass g (the frame's own work counted as its FORWARD to j counts it, since its prefix completes
        before the splice); the new holds pin no other frame's cell (`_r8_pins_others`); and certificates of both
        boundaries hold against the permissions, the reviser's own from j on taken back. True: refuse (counted)."""
        if self._guard == "off":
            return False                                                   # (OFF: no rule (a), no event)
        if self._resv is not None:
            return self._resv_rev(fr, tmp, j, F2, new_holds)               # (SER-R: within its held footprint, or refused)
        if self._r9 is not None:
            return self._r9_rev(fr, tmp, j, new_holds, F2)                 # (round 9's bounded check: one event of the port)
        own = lambda x: x in new_holds or x in tmp.fence or x in tmp.kof
        ownj = lambda x: x in new_holds or x in fr.fence or fr.kof.get(x, -1) >= j   # (the post-decision holds at j)
        gc = set().union(*(self._defer_workspace(tmp, jj) for jj in range(F2 + 1) if tmp.feas[jj]))
        h = self._r8_fwd_held(fr, j)
        h0, hj = (lambda x: own(x) or h(x)), (lambda x: x in new_holds or h(x))
        refuse = (self._r8_gsvc and self._hw is not None and not self._hw.gsvc_try()) \
            or self._r8_refuses_at(tmp, 0, guard=gc, held=h0) \
            or self._r8_refuses_at(fr, j, guard=gc, held=hj) or self._r8_pins_others(fr, new_holds, j)
        if not refuse and not self._r8_cert([(self._defer_workspace(tmp, 0), h0, gc, own), (self._defer_workspace(fr, j), hj, gc, ownj)],
                                            self._r8_perms(top={fr.vid: j})):
            self._defer_stats["r7:rule_reb_member_refusals"] += 1
            refuse = True
        if refuse:
            self._defer_stats["r7:rule_reb_refusals"] += 1
        return bool(refuse)

    def _r8_fwd_held(self, fr, b: int):
        """The held work of the frame's FORWARD to b, counted as (a1) counts it: its own work
        is its fence and its steps from b, so the block being admitted no longer refuses itself; its other holds (a
        prepared revision's new ones) and the other frames' work as `_r7_holdable` reads them.
        the frame's steps and fence are read from the frame itself, so a what-if at the registration (the decision's first
        decision, V2q's readiness query), which runs before its members and holds are written, counts them too."""
        def held(x):
            if x in fr.kof:
                return fr.kof[x] >= b or bool(self._defer_hold_of.get(x, set()) - {fr.vid})   # (or another frame's fence holds it)
            if x in fr.fence:
                return True
            return self._r7_holdable(x)
        return held

    def _r8_b0(self, fr) -> bool:
        """(a1)'s premise: the frame registers only where boundary 0 has a usable script, so
        the boundary it stands at needs no FORWARD and (a3) can always settle it there."""
        return bool(fr.feas[0])

    def _r8_perms(self, top=None, block=None) -> dict:
        """Rule (a)'s placement permissions: {instruction: (frame, step, the frame's steps)}, every live frame's admitted
        member that has not started -- no guard holds a member once its FORWARD admitted it (`_defer_hold`), so its
        placements are authorized -- those of a frame named in `top` only below top[vid] (a revision takes back the
        reviser's steps from j), and with `block` = (frame, a, b) the frame's members from its admitted step up to b, a
        FORWARD's block before it is admitted (checked before any of it starts; the decision what-if's blocks before a, which it
        counts as admitted, too). The frame comes as itself: a what-if can run before the registration stores it. Script instructions and zero-duration cleanups are left out, as the guard leaves them: a script
        runs on locked cells and ends (A4), and a cleanup places no patch. an ended frame's admitted members that have
        not started (`_r8_orph`) too, with no member exception, since that frame holds nothing any more."""
        started, top = self._defer_started, top or {}
        dur, boot, n0 = self._instruction_durations, self._is_startup_instruction, self.n_original
        if self._r8_orph and self.__dict__.get("_r8_orph_pruned") != len(started):
            for m in [m for m in self._r8_orph if m in started]:       # (a started one is no permission; pruned so the
                del self._r8_orph[m]                                   #  scan stays short)
            self._r8_orph_pruned = len(started)
        live = sorted(self._live_frames)
        key = (getattr(self, "current_round", None), len(started), len(self._r8_orph),
               tuple((y, self._frames[y].admitted, id(self._frames[y].members)) for y in live))
        cache = self.__dict__.get("_r8_pcache")
        if cache is None or cache[0] != key:                           # (every admitted member not started, per state: the
            base = {}                                                  #  started set only grows, a FORWARD or a take-back moves
            for y in live:                                             #  `admitted`, a rebase replaces the members)
                fy = self._frames[y]
                for k in range(fy.admitted):
                    for m in fy.members[k]:
                        if m not in started and m < n0 and (boot[m] or dur[m] > 0):
                            base[m] = (y, k, fy.kof)
                for instrs_, _ev in fy.h_prefix:                       # (a rebased frame's prefix,
                    for m in instrs_:                                  #  still its members, before its rebased steps: no
                        if m not in started and m < n0 and (boot[m] or dur[m] > 0) and m not in base:   # exception)
                            base[m] = (y, -1, fy.kof)
            for m, (_y, k) in self._r8_orph.items():                   # (an ended frame's admitted members, with no member
                if m not in started and m not in base:                 #  exception: the frame holds nothing any more.  One a
                    base[m] = (None, k, {})                            #  later frame took into its steps stays listed: when that
                                                                       #  frame ends it is authorized again, so every certificate
                                                                       #  made meanwhile counts it too., check,
                                                                       #  finding 1: one a live frame has admitted keeps that
                                                                       #  frame's entry and exception)
            cache = self._r8_pcache = (key, base)
        out = {m: yk for m, yk in cache[1].items() if yk[1] < top.get(yk[0], yk[1] + 1)}
        if block is not None:
            fb, _a, b = block
            for k in range(fb.admitted, b):
                for m in fb.members[k]:
                    if m not in started and m < n0 and (boot[m] or dur[m] > 0):
                        out[m] = (fb.vid, k, fb.kof)
        if self._r9 is not None and (self._r9.cover or self._r9.vac):      # (the live certificates' covered release
            for m, yk in self._r9.perms(self._live_frames, started).items():   #  placements, which every later certificate
                out.setdefault(m, yk)                                      #  counts as permissions)
        if self._r9 is not None and self._r9.vrun:                         # (cov=enforce: a run's placement, by the cell's
            cur = {}                                                       #  first unstarted user, as `_r9_vacate` marked one
            for q in sorted(self._r9.vrun, key=repr):                      #  before: rule (a) reads a permission only where it
                m = self._defer_first_unstarted(q)                         #  places now, `_r8_places`)
                if m is not None and self.schedule_instructions[m].instruction.name != "DISCARD" \
                        and (self._r8_guardable(m) or self._r9_mo(m, cur)):
                    out.setdefault(m, (None, -1, {}))
        return out

    def _r8_cert(self, certs, perms) -> bool:
        """Rule (a)'s one evaluator of future occupancy: do the certificates
        `certs` hold against the placement permissions `perms` (`_r8_perms`)? A certificate (cells, held, guard) is a
        boundary's workspace, the work its frame's settlement there leaves held (post-decision: its fence and its steps
        from the boundary, a revision's new holds, and the work live frames hold or can still take back), and cells the
        walk counts as guarded besides the live ones (a registering or revising frame's). Every permitted placement on
        its cells must be released (a DISCARD frees the patch), through a release that waits on nothing held, through
        unstarted predecessors and the guarded placements the candidate guard would hold (`_r8_walk`). A placement after
        a DISCARD is a permission of its own, so a cell freed and occupied again is checked again. For a member of frame
        y at step k, y's steps up to k are not held unless another frame holds them: taking one back takes the member back
        too, and it then places nothing. The patches on the cells now are the certificate's other half
        (`_r8_refuses_at`). A failure is falsy and names the failing permission, (instruction, frame or None, cell)
        (`R8Fail`, kept in `_r8_fail`); a search that stops at its cap fails, counted `rule_member_search_cap` as well as
        `rule_search_cap`. an ended frame's member (frame None) is passed over for a
        certificate whose own post-decision holds (its fourth part, `own`) contain it, since it places nothing before
        that settlement ends; and its own hold is not counted in the walk from its release, since the hold only decides
        whether the patch exists."""
        if not perms or not certs:
            return True
        self._defer_stats["r7:rule_rec_checks"] += 1                       # (records only)
        si, cells_of, hold_of = self.schedule_instructions, self._defer_cells, self._defer_hold_of
        allc = set().union(*(c_[0] for c_ in certs))
        r9 = self._r9 if (self._r9 is not None and self._r9.ev is not None) else None
        for m, (y, km, ky) in perms.items():
            if r9 is not None:                                             # (the permission's read and its cells' entries)
                r9.charge(R9A["perm"] + R9A["cell"] * len(si[m].instruction.patches))
            qs = [q for q in si[m].instruction.patches if q in allc and self._r8_places(m, q)]
            if not qs:
                continue
            for cert in certs:
                cells, held, guard = cert[:3]
                if y is None:
                    own = cert[3] if len(cert) > 3 else None
                    if own is not None and own(m):
                        continue                                           # (the certified frame holds it until it has settled)
                    h = lambda x, held=held, m=m: x != m and held(x)       # (its own hold only delays the patch)
                else:
                    h = lambda x, held=held: bool(hold_of.get(x, set()) - {y}) if ky.get(x, km + 1) <= km else held(x)
                for q in qs:
                    if q not in cells:
                        continue
                    rel, freed = self._r8_release(q, bisect.bisect_left(cells_of.get(q, ()), m) + 1)
                    if r9 is not None:
                        r9.charge(R9A["release"] * (2 if freed else 1))
                    cap0 = self._defer_stats["r7:rule_search_cap"]
                    if not freed or self._r8_walk([rel], h, guard):
                        if self._defer_stats["r7:rule_search_cap"] > cap0:
                            self._defer_stats["r7:rule_member_search_cap"] += 1
                        self._r8_fail = R8Fail((m, y, q))
                        return self._r8_fail
        return True

    def _r8_fwd_refuses(self, fr, a: int, b: int, count: bool = True) -> bool:
        """Rule (a) at a FORWARD from a to b, after test. It makes b's certificate: b's workspace clears with
        the work the frame's settlement at b leaves held (`_r8_fwd_held`), for the patches on it now (g) and for every
        patch an authorized, unstarted instruction can still put there -- every live frame's admitted members that have
        not started, the frame's own below b among them, and the block itself, checked before any
        of it starts. It grants the block's permission: the members put no patch on another live frame's workspace whose
        release waits (`_r8_places_ok`). True: refuse."""
        held = self._r8_fwd_held(fr, b)
        if self._r8_refuses_at(fr, b, held=held):
            self._defer_stats["r7:rule_rec_fwd_g"] += int(count)           # (records only: which half refused a FORWARD)
            return True
        own = lambda x: fr.kof.get(x, -1) >= b or x in fr.fence           # (the post-decision holds at b, the frame's own)
        if not self._r8_cert([(self._defer_workspace(fr, b), held, (), own)], self._r8_perms(block=(fr, a, b))):
            self._defer_stats["r7:rule_fwd_member_refusals" if count else "r7:rule_fwd_member_whatif"] += 1
            return True
        if not self._r8_places_ok(fr, a, b):
            self._defer_stats["r7:rule_rec_fwd_places"] += int(count)
            return True
        return False

    def _r8_places_ok(self, fr, a: int, b: int) -> bool:
        """The block's permission against the other live frames' certificates (`_r8_cert`): its members put no patch on
        another live frame's candidate, pending or target cell whose release waits on work that can be held
        (`_r7_holdable`, which every certificate's post-decision holds are part of) -- the block's later steps included,
        since the frame can still take them back once it is admitted.  The frame's own workspace is its certificate's,
        checked in `_r8_fwd_refuses`."""
        perms = {m: (fr.vid, k, fr.kof) for k in range(a, b) for m in fr.members[k] if m not in self._defer_started and m < self.n_original
                 and (self._is_startup_instruction[m] or self._instruction_durations[m] > 0)}
        mine, tw, ws, tg, pd = getattr(fr, "guard", ()), getattr(fr, "target_ws", ()), self._defer_ws_cells, self._defer_target_cells, self._r8_pend
        cells = {q for m in perms for q in self.schedule_instructions[m].instruction.patches
                 if ws.get(q, 0) - (q in mine) > 0 or q in pd or tg.get(q, 0) - (q in tw) > 0}
        return self._r8_cert([(cells, self._r7_holdable, ())], perms)

    def _r8_pins_others(self, fr, new_holds, j=None) -> bool:
        """A revision's new holds pin the release of a patch on another live frame's candidate cell, pending or not (rule
        (a2)): one there now, or one an authorized, unstarted instruction can still put there (`_r8_perms`; the
        reviser's steps from j on go with the revision). No new holds, no search (the search's safe answers at its
        cap or at a guarded placement nothing frees would refuse a revision that adds nothing)."""
        if not new_holds:
            return False
        nh, cells, pd = (lambda x: x in new_holds), set(), self._r8_pend
        mine = set(fr.guard) | set(fr.r8_pend) | set(fr.r8_pend_reb)
        r9 = self._r9 if (self._r9 is not None and self._r9.ev is not None) else None
        for c_ in sorted(set(self._defer_ws_cells) | set(pd), key=repr):   # (a pending registration's or
            if r9 is not None:
                r9.charge(R9A["cell"])                                     # (a guarded cell's entry)
            if self._defer_ws_cells.get(c_, 0) - (c_ in fr.guard) <= 0 and pd.get(c_, 0) - (c_ in fr.r8_pend) - (c_ in fr.r8_pend_reb) <= 0:
                continue                                                   #  revision's cells, which the guard holds, are certified too)
            cells.add(c_)
            if c_ in self._defer_locked or not (c_ in self._active_patches or c_ in self._r7_piu):
                continue
            self._defer_first_unstarted(c_)
            rel, freed = self._r8_release(c_, self._defer_cell_ptr.get(c_, 0))
            if r9 is not None:
                r9.charge(R9A["release"] * (2 if freed else 1))
            if rel is not None and self._r8_walk([rel], nh):
                return True
        for m in self._r8_perms(top=None if j is None else {fr.vid: j}):
            if r9 is not None:
                r9.charge(R9A["perm"] + R9A["cell"] * len(self.schedule_instructions[m].instruction.patches))
            for q in self.schedule_instructions[m].instruction.patches:
                if q in cells and self._r8_places(m, q):
                    rel, freed = self._r8_release(q, bisect.bisect_left(self._defer_cells.get(q, ()), m) + 1)
                    if r9 is not None:
                        r9.charge(R9A["release"] * (2 if freed else 1))
                    if freed and self._r8_walk([rel], nh):
                        return True
        return False

    def _r8_settle(self, fr: DeferFrame):
        """Rule (a3): a contradicted frame whose FORWARD to its target the guard or the rule refused decides again among its
        certified boundaries, from its current one up to its witness (`r8_wit`): the boundary of its last
        certificate, made at its registration (0), at its last FORWARD or at a commit, or at its rebase (0), which is its
        admitted boundary, so reachable without a FORWARD and at or past its begun work. That certificate covers the
        patches on the workspace and those authorized, unstarted instructions can still put there, so the workspace
        clears and the decide finds the witness usable. A witness that is not reachable (an invariant failure, counted
        `rule_wit_missing`) falls back to the admitted boundary. It does not wait on the guard."""
        if self._hw is not None:
            self._hw_reset(fr)                                             # (the old target's queued events go)
        for c_ in fr.target_ws:
            n = self._defer_target_cells.get(c_, 0) - 1
            if n > 0:
                self._defer_target_cells[c_] = n
            else:
                self._defer_target_cells.pop(c_, None)
        fr.target_ws = frozenset()
        w = fr.r8_wit
        if not (self._defer_current(fr) <= w <= fr.admitted and fr.feas[w]):
            self._defer_stats["r7:rule_wit_missing"] += 1
            w = max(fr.admitted, fr.r8_b1)
        fr.r8_cap = w
        fr.limit, fr.state = fr.r8_cap, "decide"
        self._defer_stats["r7:rule_settle_retargets"] += 1

    def _r8_pend_add(self, cells, d: int):
        for c_ in cells:
            n = self._r8_pend.get(c_, 0) + d
            if n > 0:
                self._r8_pend[c_] = n
            else:
                self._r8_pend.pop(c_, None)

    def _r8_orphan_add(self, fr: DeferFrame):
        """a frame that ends (dropped, confirmed early, or materialized) leaves its admitted
        members that have not started authorized until they start. Their FORWARD granted the permission, every certificate
        made since counted it (`_r8_perms`), and no guard holds them (`_defer_hold`, `_r8_guardable`), so the frame's end
        turns none of them into work a guard can hold. The authorization lasts until the instruction starts: a later
        frame that takes it into its steps holds it while that frame lives, and it stays among the permissions
        meanwhile. Records only: `rule_rec_orphans`."""
        started, n0, dur, boot, n = self._defer_started, self.n_original, self._instruction_durations, self._is_startup_instruction, 0
        for k in range(fr.admitted):
            for i in fr.members[k]:
                if i not in started and i < n0 and (boot[i] or dur[i] > 0):
                    self._r8_orph[i] = (fr.vid, k); n += 1
        for instrs_, _ev in fr.h_prefix:                                   # (a rebased frame's prefix,
            for i in instrs_:                                              #  its members up to its end)
                if i not in started and i < n0 and (boot[i] or dur[i] > 0):
                    self._r8_orph[i] = (fr.vid, -1); n += 1
        self._defer_stats["r7:rule_rec_orphans"] += n

    def _r8_pend_reb(self, fr: DeferFrame, nxt):
        tmp = nxt.tmp
        fr.r8_pend_reb = frozenset(set().union(*(self._defer_workspace(tmp, jj) for jj in range(tmp.c.F + 1) if tmp.feas[jj])))
        self._r8_pend_add(fr.r8_pend_reb, 1)

    # ------------------------------------------------------------------ round 9's bounded check (froq/r9check.py)
    def _r9_event(self, kind, key, fn, safe, vid=None, accept=None, sig=None, wq_o=None, batch=None):
        """One event of the check port (plan v2 4.2-4.3): the stored answer of an event whose service has ended (its
        requester asks again each round), WAIT while one is in service or the port has no room, else the event now --
        `fn` under the event's budget and the window, the safe answer at a stop, `accept(ev, answer)` adding the
        update part (it may stop too: a reservation that does not fit) -- served at c0 + c1 x accesses cycles
        (`HWLimits.r9_take`). A grant (reg, fwd, rev) first takes c0 as guard service did (`gsvc_try`); a
        placement's check (plc, rel) has paid 1 + c0 in the dispatcher's guard check. r9-guard-close 3: a stored answer
        applies only to the request it answered (`sig`, the request's parts, `R9.covers`); one whose request has changed
        since is dropped and the request checked anew. A grant waits while an answer whose service has ended is not
        taken (`R9.unapplied`). r9-guard-close 4: otherwise the round's order serves it, as evaluated, unless an aged
        request is pending (`R9.turn`, K-aging): then only the oldest aged one is served. Every port refusal ages its
        request (`R9.refused`: a grant's turn is its requester's, a counted FORWARD's apart from a what-if's; a
        placement's check takes its turn at its guard check). r9-guard-close 5: only collecting a valid stored answer is
        exempt; a placement's check whose stored answer is stale takes its turn before its walk (its guard check drops
        that answer first, `_r9_gc_turn`; here for a direct call)."""
        r9, hw, now, wq = self._r9, self._hw, self.current_round, getattr(self, "_wq", None)   # (a stand-in device
        if wq is not None and self._wq_hb:                                 #  without the attribute: off)
            self._wq_hflush()                                              # (F2: pending holds first, their debt before the
        if r9.p0 is None:                                                  #  arbitration)
            preds = self._adj()[0]
            r9.p0 = max((len(preds.get(i, ())) for i in range(self.n_original)), default=0)
        if r9.tickets and r9._pruned != now:
            r9.prune(now)                                                  # (answers no requester came back for)
        grant = kind in ("reg", "fwd", "rev")
        if grant and vid is not None and len(r9.tickets) > (key in r9.tickets):
            for k in [k for k in r9.tickets if k[:2] == key[:2] and k != key and k[4:] == key[4:]]:
                del r9.tickets[k]                                          # (r9-guard-close 3: its requester asks for another
                r9.stats["tickets_stale:" + kind] += 1                     #  request of the kind: that answer is not taken)
                if wq is not None and kind == "reg" and vid not in self._live_frames:
                    self._wq_drop(vid)                                     # (what it reserved goes back at once)
        t, stale = r9.tickets.get(key), False
        if t is not None and sig is not None and not r9.covers(t[2], sig()):
            del r9.tickets[key]                                            # (r9-guard-close 3: the request changed after its
            r9.stats["tickets_stale:" + kind] += 1                         #  check; the answer is not its answer)
            if wq is not None and kind == "reg" and vid not in self._live_frames:
                self._wq_drop(vid)                                         # (a stale registration's reservation goes
            t, stale = None, True                                          #  back at collection, before it is asked again)
        if wq is not None:
            self._wq_why = None                                            # (records: why it waits or stops, below)
        if t is not None:
            if now < t[1]:
                r9.stats["ticket_waits:" + kind] += 1
                if wq is not None:
                    self._wq_why = "ticket"
                return R9_WAIT
            del r9.tickets[key]
            r9.stats["ticket_applied:" + kind] += 1
            if kind == "reg" and vid is not None and vid not in self._live_frames:
                r9.collected(vid, now)                                     # (a deliberate change: its window
                                                                           #  reservation stays through this round)
            if wq is not None and kind == "reg":
                self._wq_pend[vid] = now                                   # (its frame goes live this round, or not)
            return t[0]
        if not grant:
            aq = ("gc", key[1])                                            # (a placement's check: its guard check's turn)
        elif vid is None:
            aq = key
        else:
            aq = ("fwd", vid, bool(key[4])) if kind == "fwd" else (kind, vid)   # (r9-guard-close 4: the request's identity)
        ox, need = (), 0
        if wq is not None:                                                 # (the write queue's admission, plan v2 6.2: its
            ox = tuple(wq_o()) if wq_o is not None else ()                 #  consequences from the tables, (owner, writes, long);
            oxn = sum(n for _o, n, _l in ox)                               #  its batch, its installs' clearing writes (a what-if
            need = oxn + (batch if batch is not None else   # (SER-R's registration knows its writes and their clearing)
                          0 if wq.bmax == WQ_INF else wq.bmax if kind == "fwd" and not key[4] else 2 * wq.bmax + 1)   #  installs
            wq.ox_max[kind] = max(wq.ox_max[kind], oxn)                    #  nothing; unbounded: reserved as they are made)
            if wq.C != WQ_INF and need > wq.H:                             #  nothing).  One larger than H never fits beside the
                wq.stats["too_big:" + kind] += 1                           #  long-lived reservations: refused outright, as
                self._wq_why = "too_big"
                r9.qserve(key, now)                                        #  served, so it never ages ahead of the others
                r9.served(aq)
                return safe
        why = None
        wq_no = wq is not None and (not wq.fits(need) or (not grant and not stale and wq.aged_wait not in (None, aq)
                                                           and wq.aged_wait in r9.aged))   # (no room, or an aged
        if hw is not None:                                                 #  request waits for it: a placement's check took
            if hw.r9_debt:                                                 #  its turn at its guard check)
                why = "debt"
            elif grant or stale:                                           # (r9-guard-close 5: a placement's fresh check
                if r9.unapplied(now, key):                                 #  after its stale answer takes its turn too; its
                    why = "kept"                                           #  guard check paid its c0.  An untaken answer
                elif not r9.turn(aq, now):                                 #  keeps the port: validity)
                    why = "aged"                                           # (an aged request goes first)
                elif wq_no:
                    why = "wq"                                             # (secured before its service starts, c0 too)
                elif grant and not hw.gsvc_try(aged=r9.aged_head(aq)):
                    why = "room"
            elif wq_no:
                why = "wq"
        elif wq_no:
            why = "wq"
        if why is not None:
            r9.stats["port_waits:" + kind] += 1
            if wq is not None:
                self._wq_why = why
            if why in ("kept", "aged"):
                r9.stats["%s_waits:%s" % (why, kind)] += 1
            if not grant and wq is not None and wq.holds:
                r9.resume(aq, now)                                         # (F1: a placement's check keeps its guard check's age)
            if why == "wq":                                                # (it waits for capacity; an aged request that
                wq.wait(kind, aq, now)                                     #  does gets freed capacity first: no other grant or
                if r9.aged_head(aq):                                       #  guarded start is admitted meanwhile)
                    wq.aged_wait = aq
            r9.qwait(kind, key, now)                                       # (records only: queued waiting, r9-guard-close)
            r9.refused(aq, now)                                            # (r9-guard-close 4: its age runs)
            if wq is not None:
                self._wq_keep(aq)                                          # (and a queue wait it had, uncounted)
            return R9_WAIT
        if wq is not None:
            assert wq.admit(("ev", key), need, now), ("an admission that does not fit", kind, need)
            if grant:
                wq.served(aq, now)
            self._wq_ox, self._wq_tch = ox, set()
        r9.qserve(key, now)
        if wq is not None and wq.holds and not grant:
            r9.suspend(aq, now)                                            # (F1: a placement's check is served; its
            self._wq_keep(aq)                                              #  instruction's request runs on to its start's
        else:                                                              #  admission, its queue wait too)
            r9.served(aq)
        r9.advance_head(self._defer_started, self.n_original)
        r9.sweep(self._live_frames, now)                                   # (the queue's own, `_wq_sweep`: r9's may drop a
                                                                           #  frame whose registration answer it collected this
                                                                           #  round, before the frame is live)
        ev = r9.ev = R9Event(kind, r9.b_rel() if kind == "rel" else r9.B, vid)
        try:
            ans = fn()
            if accept is not None:
                accept(ev, ans)
        except R9Stop as e:
            ans = safe
            r9.stats["%s:%s" % (e.why, kind)] += 1
            if wq is not None:
                self._wq_why = e.why
        finally:
            r9.ev = None
        if wq is not None:
            self._wq_settle(ev, key, kind, ox)
        r9.record(ev, now)
        r9.note_peak(now)
        if hw is not None:
            fw = wq.f if wq is not None and self._guard != "zl" else 0     # (f a dependent read, forwarding; ZL: none)
            cyc = (r9.c1 + fw) * (ev.acc + ev.upd)
            room = max(0, hw.R - hw.checks)
            k = hw.r9_take(cyc) if cyc else 0
            if wq is not None:                                             # (port 1 writes its batch during its service,
                wq.p1(ev.upd, r9.c1 + fw, ev.acc, room, hw.R)              #  F3: after the walk's accesses)
            r9.service(kind, r9.c0 + cyc, k)
            if k:
                r9.stats["multi_round:" + kind] += 1
                r9.tickets[key] = (ans, now + k, sig() if sig is not None else None)
                return R9_WAIT
        return ans

    def _r9_fwd(self, fr, a: int, b: int, count: bool) -> bool:
        """test and rule (a)'s at a FORWARD from a to b (`_r7_refuses`), as one event; the decision's what-ifs (count
        False) are events too and install nothing. True: refused this round (the guard, the rule, the safe answer at a
        stop, or the port)."""
        st = self._defer_stats

        def fn():
            if self._defer_ws_fenced(fr, b, self._r7_piu, held=self._r7_holdable):
                st["r7:guard_refusals"] += int(count)
                return "guard"
            if self._r8_fwd_refuses(fr, a, b, count):
                st["r7:rule_fwd_refusals" if count else "r7:rule_fwd_whatif"] += 1
                return "rule"
            return None
        own = lambda x: fr.kof.get(x, -1) >= b or x in fr.fence
        acc = (lambda ev, ans: ans is None and self._r9_accept(ev, fr.vid, self._defer_workspace(fr, b), own)) if count else None
        sig = (lambda: ((frozenset(self._defer_workspace(fr, b)),),        # (r9-guard-close 3: the request a stored answer is for)
                        (frozenset(fr.fence) | frozenset(x for x, k in fr.kof.items() if k >= b),))) if count else None
        wq_o = (lambda: self._wq_fwd(fr, a, b)) if count and self._wq is not None else None   # (its members, take-backs)
        ans = self._r9_event("fwd", ("fwd", fr.vid, a, b, bool(count)), fn, "stop", vid=fr.vid, accept=acc, sig=sig, wq_o=wq_o)
        if count and self._wq is not None:                                 # (records: the write queue held it; the port
            self._wq_fwhy = (None if ans is None else "wq" if self._wq_why in ("wq", "wq_long", "too_big")   #  did (a wait, or
                             else "port" if ans is R9_WAIT or ans == "stop" else "check")   #  a budget or window stop); or
                                                                           #  the check's rules refused it (guard, rule)
        if ans is None:
            return False
        if ans is R9_WAIT:
            st["r7:gsvc_waits"] += 1                                       # (as: the port did not serve it this round)
        if count:
            fr.r8_ref = self.current_round
            if ans in ("rule", "stop"):
                fr.r8_fwd = (self.current_round, a, b)
        return True

    def _r9_reg(self, fr, s0: int, fence, F: int, js) -> str | None:
        """Rule (a1) at a registration (`_r8_reg_refuses`) as one event, with the frame's reservation: the longest
        settlement script over the boundaries it may reach (`js`).  "g" (wait, as Wait-for-Decoder) at a stop, at the
        reservation's capacity hit or while the port is busy."""
        own = lambda x: x in fence or fr.kof.get(x, -1) >= s0
        n = self._r9_script_len(fr, js)
        gc = set().union(*(self._defer_workspace(fr, j) for j in range(F + 1) if fr.feas[j]))

        def fn():
            self._r9.ev.gc = frozenset(gc)                                 # (r9-guard-close 4: its member coverage's guard)
            why = self._r8_reg_refuses(fr, s0, fence, F)
            if why is None and not self._r9_guard_new(fr.vid, gc, own):
                self._r9.stats["guard_new_refusals:reg"] += 1
                return "g"
            return why
        acc = lambda ev, ans: ans is None and self._r9_accept(ev, fr.vid, self._defer_workspace(fr, s0), own, resv=n)
        sig = lambda: ((s0, frozenset(self._defer_workspace(fr, s0)), n), (frozenset(gc), frozenset(fence) | frozenset(fr.kof)))
        wq_o = (lambda: [(("fr", fr.vid), 1 + 2 * min(len(set(fence) | set(fr.fence) | set(fr.unadm)), self._r9.W), True),   # (the registration's
                         (("sc", fr.vid), self._wq_script(fr, js), True)]) if self._wq is not None else None   #  its verdict, its
        ans = self._r9_event("reg", ("reg", fr.vid), fn, "g", vid=fr.vid, accept=acc, sig=sig, wq_o=wq_o)   #  hold marks set and
        return "g" if ans is R9_WAIT else ans                              #  cleared in the window's W rows at most, its scripts)

    def _resv_reg(self, fr, js, piu) -> str | None:
        """SER-R: the static
        reservation's registration check, one `reg` event of the check port in place of rule (a1)'s walk. The footprint
        is the union of the workspace cells of every boundary the frame may reach (`js`: each with a placed settlement
        script and no workspace patch that waits for the frame's own held work, up to the walk's end, its top), a
        boundary's workspace being its script's cells less the program cells of the residual it corrects
        (`_defer_workspace`). The check reads the footprint row and the cells' state (`RESV_COST["reads"]` accesses at
        c1 + f, after c0) and stands only if every footprint cell is free (`_resv_free`); it has no update part. With it
        the queue admits the frame's queue entries: its frame writes as counts them (the verdict, the end's row, the
        hold marks set and cleared, and again for a take-back of each) and its settlement
        scripts' writes over js (`_wq_script`), with no batch. "g": the
        value asks again next round, and it takes waiting's path if its decode arrives first, as a SER value does. the footprint and the boundaries are the walk key's static row (`_resv_row`):
        every boundary with a placed script up to the walk's limit, whatever the patches now, and the scripts' writes over
        all of them (`fr.resv_js`; the argument `js`, the usable ones, is not used)."""
        fp, st = fr.resv_fp, self._resv_st
        js = fr.resv_js

        def fn():
            self._r9.ev.acc += self._resv_cost["reads"]
            adm = self._resv_adm_cells()                                   # (CSR: G1's registration half)
            if self._resv_inc:
                adm = adm | self._resv_inc_booked(fr.vid)                  # (plan 7.2: every booking, not current emptiness)
            if any(not self._resv_free(c_, piu, adm) for c_ in fp):
                st["refused"] += 1                                         # (records: the footprint was not free; of them,
                st["g1_reg"] += not adm.isdisjoint(fp)                     #  with a cell a live frame's admitted member places on)
                return "g"
            if self._ws_hard_exceeds(fp):
                st["refused_cap_hard"] += 1                                # (--cap-hard: its footprint would pass W)
                return "g"
            return None
        acc = lambda ev, ans: ans is None and self._resv_accept(ev, fr.vid)
        sig = lambda: ((fp,), ())
        held = fr.fence | {i for m in fr.members for i in m if i not in self._defer_started}   # (the set the floor counts, so n <= h by construction)
        hm = 4 * min(len(held), self._r9.W)                                # (the hold marks: a set and a clear, and a take-back's set and
                                                                           #  clear, an instruction); a
                                                                           #  tight queue refuses the registration, `wq_long`.  A later change
                                                                           #  that spends the slack (an entry, a revision) tops it up to the
                                                                           #  floor of a take-back of every unstarted step member, at most W,
                                                                           #  first, or waits or is refused (finding 1: `_resv_live`))
        fr.resv_mleft = hm                                                 # (written from it, `_wq_h`)
        wq_o = (lambda: [(("fr", fr.vid), 2 + hm, True), (("sc", fr.vid), self._wq_script(fr, js), True)]) if self._wq is not None else None
        stored = ("reg", fr.vid) in self._r9.tickets                       # (a multi-round event's answer, stored for collection)
        ans = self._r9_event("reg", ("reg", fr.vid), fn, "g", vid=fr.vid, accept=acc, sig=sig, wq_o=wq_o, batch=0)
        if ans is R9_WAIT:
            return "g"
        if ans is None and stored and self._hw is not None:
            st["collect_reads"] += 1                                       # (the collection-time occupancy
            self._hw.ev_take(self._r9.c1 + (self._wq.f if self._wq is not None and self._guard != "zl" else 0))   # read below is an
        adm = self._resv_adm_cells() if ans is None else None              #  access of the check port's: one c1 + f, charged;
        if adm is not None and self._resv_inc:
            adm |= self._resv_inc_booked(fr.vid)                           # (the bookings at collection too; and the cap below)
        if ans is None and (any(not self._resv_free(c_, piu, adm) for c_ in fp) or self._ws_hard_exceeds(fp)):   # an immediate answer's cells were read in its
            st["stale"] += 1                                               # 2 reads. G1's registration half is tested again.
            return "g"                                                     # (A stored answer whose cells were taken after its
        return ans                                                         #  service: asked again; `_wq_sweep` returns its queue)

    def _resv_accept(self, ev, vid) -> bool:
        """SER-R's free answer: no update part; the frame's queue entries (the event's consequences) go to their owners,
        within the long-lived room C - H (`_wq_fit`), else the safe answer and nothing is reserved."""
        wq = self._wq
        if wq is not None:
            why = self._wq_fit(ev, 0, [], None)
            if why is not None:
                raise R9Stop(why)
            if vid not in self._live_frames:
                self._wq_drop(vid)                                         # (as `_r9_accept`: an earlier event's, never applied)
            ev.inst = True                                                 # (`_wq_settle` moves the consequences; the end's row
        return True                                                        #  is in `("fr", vid)`'s own, `_wq_need`)

    def _resv_rev(self, fr, tmp, j: int, F2: int, new_holds=()) -> bool:
        """SER-R's revision (a commit's early script at j and the rebased walk after it, the decision's or H's): it stands only if
        the early script's workspace and every workspace of the rebased walk (each boundary of the revised contract with a
        placed script) lie within the cells the frame holds; then nothing more is reserved or checked.
        Otherwise it is refused, as rule (a2) refuses one, and the frame keeps its walk. True: refuse (counted). and both of its new settlements must find their workspaces clear (`_resv_usable`): the early
        script's at j after the frame's own steps before j, and the rebased boundary 0's (the undo at j) after those steps
        too; the rebase keeps only the rebased walk's usable boundaries (`_h_rebase`). both tests are static, from
        the walks' contracts and the layout (the rows of the frame's walk key and of the rebased key), folded from the free
        cells of the registration over every member of the frame's walks before them (`resv_hist`, then the steps before
        j); they read no cell state. The same tests on the cells' state now (form) are records only."""
        need = self._defer_workspace(fr, j).union(*(self._defer_workspace(tmp, jj) for jj in range(F2 + 1) if tmp.feas[jj]))
        self._resv_st["rev_checks"] += 1
        if self._resv_inc:                                                 # (plan 7.2: the early script's footprint and the
            js0 = [jj for jj in range(F2 + 1) if tmp.feas[jj]][:1]         #  boundary the frame stands at after the splice, taken
            need = frozenset(self._defer_workspace(fr, j)).union(*(self._defer_workspace(tmp, jj) for jj in js0))   # by the same
            if not self._resv_inc_check(fr, need, "rev"):                  #  check, charged; refused: it keeps its walk and may ask
                self._resv_st["rev_refused"] += 1                          #  again at a later decision)
                return True
        elif not need <= fr.resv_fp:
            self._resv_st["rev_refused"] += 1                              # (records: a revision needing cells not held)
            return True
        js2 = [jj for jj in range(F2 + 1) if tmp.feas[jj]]
        si = self.schedule_instructions
        if js2 and any(si[i].instruction.conditioned_on_completion_idx for k in range(js2[-1]) for i in tmp.members[k]):
            self._resv_st["rev_refused"] += 1                              # (registration's exclusion,
            self._resv_st["rev_refused_completion"] += 1                   #  applied to the revised contract: a member before its
            return True                                                    #  last boundary carries a completion condition)
        pre = {i for k in range(min(j, len(fr.members))) for i in fr.members[k]}
        ok = bool(self._resv_usable(fr, [j], pre=fr.resv_hist, static=True)) and \
            bool(self._resv_usable(tmp, [0], pre=fr.resv_hist | pre, static=True))
        dyn = bool(self._resv_usable(fr, [j], self._r7_piu)) and bool(self._resv_usable(tmp, [0], self._r7_piu, pre))
        if ok != dyn:                                                      # (records: the static test and the cells' state now
            self._resv_st["rev_static_only" if ok else "rev_dyn_only"] += 1   #  disagree; static_only: the cells will clear)
        if not ok:
            self._resv_st["rev_refused"] += 1
            self._resv_st["rev_refused_unclear"] += 1                      # (records: one of them could find a patch left there)
            return True
        if self._wq is not None:                                           # (items 1 and 2: the revision's writes are secured
            taken = {i for k in range(j, min(fr.admitted, len(fr.members))) for i in fr.members[k] if i not in self._defer_started}
            sc = self._wq_script(fr, [j]) + self._wq_script(tmp, js2)      #  before it is accepted: the marks of the steps it takes
            if not self._resv_secure(fr, self._resv_live(fr, tmp) + self._resv_inbuf(taken | set(new_holds)), sc):   # back and holds anew, with
                self._resv_st["rev_refused"] += 1                          #  their clears, on top of the floor of a take-back of every unstarted
                self._resv_st["rev_refused_room"] += 1                     #  step member of the walk and of the rebased contract (the
                return True                                                #  the repair's D never asks the queue
        if self._resv_inc:
            self._resv_inc_move(fr, fr.resv_fp | need)                     # (taken, none released: the old go at its next advance)
        return False                                                       #  for room); the early script's writes and the repair after it.  Short:
                                                                           #  refused, the frame keeps its walk. After the usability test:
                                                                           #  a refused one keeps nothing)

    def _resv_row(self, fr, cap: int):
        """SER-R: the footprint row of the frame's walk key, from its contract and the
        layout only, computed once per key and read at each registration attempt in the check's 2 reads. `js`: every
        boundary j with a placed script up to the walk's limit (F, and the walk cap under rule 66); `fp`: the union of
        their workspaces W_j (the footprint); `ok`: those its own steps leave clear (`_resv_usable`, static: from free
        cells, every member counted; at a registration none has started); `completion`: a member before the last boundary
        carries a completion condition. Nothing in it depends on the patches, the started instructions or a search: a
        boundary pinned now has a footprint cell that fails the free test, and the value asks again. The key is the
        value and its walk's length; a walk with other steps under the same key is counted and computed anew, never read
        (none expected: another live frame's claim ends a walk, `frame_overlap`, so it gives a shorter walk and its own key; CSR at k > 1, where other frames claim events when a value registers)."""
        key = (fr.vid, fr.c.F)
        row = self._resv_rows.get(key)
        if row is not None and row.members == fr.members:
            self._resv_st["row_reads"] += 1
            return row
        self._resv_st["row_variants" if row is not None else "rows"] += 1
        F = fr.c.F
        top = min(F, cap) if self._hw is not None else F
        js = tuple(j for j in range(top + 1) if fr.r[j] is not None)
        si = self.schedule_instructions
        row = SimpleNamespace(members=[set(m) for m in fr.members], js=js,
                              fp=frozenset().union(*(self._defer_workspace(fr, j) for j in js)),
                              ok=tuple(self._resv_usable(fr, list(js), static=True)) if js else (),
                              completion=bool(js) and any(si[i].instruction.conditioned_on_completion_idx
                                                          for k in range(js[-1]) for i in fr.members[k]),
                              walk_cap=top < F and any(x is not None for x in fr.r[top + 1:]))
        row.unclear = tuple(j for j in js if j not in row.ok)
        self._resv_rows[key] = row
        return row

    def _resv_usable(self, fr, js, piu=None, pre=(), static: bool = False) -> list:
        """SER-R: the boundaries of `js` that the frame's own steps leave clear. The
        hold is a wait: an ordinary placement onto a held cell with no patch waits for the frame's end. While the frame is
        live nothing but its own members and scripts puts a patch on a held cell (an ordinary placement waits, a
        zero-duration instruction places nothing, and a script releases every routing and borrowed cell it uses:
        validate_events' recovery-instance rule, `cleanup_binding`), so its settlement at j can find a patch on
        W_j only if one of its members below j leaves it there, and that patch's release could follow ordinary work the
        hold keeps waiting: a cycle through the hold. Boundary j is usable when, on every cell of W_j, the last of the
        frame's members below j (with `pre`, its earlier walks' members) that puts a patch there (positive duration) or
        removes it (DISCARD), in program order, removes it. A step is admitted only once its members' outside
        prerequisites have started, so when that DISCARD runs every instruction before it on the cell has run, and no
        ordinary placement follows it while the frame is live: the settlement at j finds W_j clear. Program order on a
        cell is index order (the .lss DAG chains each instruction to the previous one on each of its patches).
        static (the registration, the revision and the rebase): every member and `pre` counted, from free cells
        (the registration's free test checks them), so the answer is the walk's and the layout's alone. Else (records
        only, form): members that started are left out, and with `piu` the fold starts from the cells' state now
        (a live patch, or an instruction running there)."""
        si = self.schedule_instructions
        dur = self._true_durations if static else self._instruction_durations   # (item 5: static reads the program's own
        started = frozenset() if static else self._defer_started                #  durations, which feedback and branch decisions
                                                                                #  never change, so a run-time zero is treated as a placement)
        if static:
            piu = None
        cells = frozenset().union(*(self._defer_workspace(fr, j) for j in js))
        on = collections.defaultdict(list)                                 # (cell -> [(member, its step)]; -1: `pre`)
        for k, mem in [(-1, pre)] + list(enumerate(fr.members[:max(js, default=0)])):
            for i in mem:
                if i not in started:
                    for c_ in si[i].instruction.patches:
                        if c_ in cells:
                            on[c_].append((i, k))

        def put(placed, i):
            return True if dur[i] > 0 else (False if si[i].instruction.name == "DISCARD" else placed)
        ok = []
        for j in js:
            for c_ in self._defer_workspace(fr, j):
                placed = piu is not None and (c_ in self._active_patches or c_ in piu)
                for i, k in sorted(on.get(c_, ())):
                    if k < j:
                        placed = put(placed, i)
                if placed:
                    break
            else:
                ok.append(j)
        return ok

    def _resv_selfblock(self, fr, k: int):
        """SER-R (records only; 's own case): the frame's step k is not admissible because an
        outside prerequisite of one of its members has not started and is an ordinary placement onto a cell the frame holds
        with no patch, so the frame's own hold keeps it waiting. No cycle: a step not admitted is not waited for (the
        frame settles at a boundary it has reached), but the frame cannot pass k while it is live. Counted once a round.
        CSR: the hold counted is the frame's own; a step k that waits because a member places a patch on another live
        frame's held cell (G1's admission half, `_defer_admissible`) is counted apart (`g1_adm`, once a round)."""
        if fr.resv_g1 != self.current_round and any(i not in self._defer_started and self._resv_other(i, fr.vid) for i in fr.members[k]):
            fr.resv_g1 = self.current_round
            self._resv_st["g1_adm"] += 1
        if fr.resv_sb == self.current_round:
            return
        held, act, si, started, preds = self._resv_held, self._active_patches, self.schedule_instructions, self._defer_started, self._adj()[0]
        for i in fr.members[k]:
            if i in started:
                continue
            for p in preds.get(i, ()):
                if (p in started or p in fr.kof or p >= self.n_original or p in self._defer_member
                        or not (self._is_startup_instruction[p] or self._instruction_durations[p] > 0)):
                    continue
                if any(held.get(c_) == fr.vid and c_ not in act for c_ in si[p].instruction.patches):
                    if fr.resv_sb < 0:
                        self._resv_st["self_block_frames"] += 1
                    fr.resv_sb = self.current_round
                    self._resv_st["self_block_rounds"] += 1
                    return

    def _resv_free(self, c_, piu, adm=None) -> bool:
        """SER-R: a cell is free when no patch is on it and none is pending: no live patch, no instruction running on it, no
        running script's lock, no reservation-manager write pending (pending guard) and no live frame's hold.
        CSR (G1's registration half, 2.4): nor will a live frame's admitted or bound, unstarted member place a
        patch on it (`adm`, `_resv_adm_cells`, found here when not given); else that member, admitted or bound before this
        frame held the cell, would place on it. At k = 1 a value registers with no frame live, and there is none."""
        if adm is None:
            adm = self._resv_adm_cells()
        return (c_ not in self._active_patches and c_ not in piu and c_ not in self._resv_held and not self._defer_locked.get(c_)
                and not self._r8_pend.get(c_) and c_ not in adm)

    def _resv_places(self, i: int) -> bool:
        """CSR (G1): program instruction i places a patch: a resource initialisation or a positive duration, the
        program's own, as `_resv_usable`'s static test reads it. A run-time duration is the program's own or 0 (feedback
        and branch decisions), so dispatch never finds a placement this misses."""
        return i < self.n_original and (self._is_startup_instruction[i] or self._true_durations[i] > 0)

    def _resv_other(self, i: int, vid) -> bool:
        """CSR (G1's admission half): instruction i places a patch on a cell a live frame other than vid holds."""
        return self._resv_places(i) and any(self._resv_held.get(c_, vid) != vid for c_ in self.schedule_instructions[i].instruction.patches)

    def _resv_adm_cells(self, skip=None) -> set:
        """CSR (G1's registration half): the cells the live frames' admitted, unstarted members place patches on (the
        admitted steps and a rebased frame's prefix, as `_r8_orphan_add` finds them), and those of the steps a live frame is
        bound to admit: the first block of a frame at an unusable boundary 0, which
        `_defer_first_block` found admissible at its registration and rule 66 holds back until the registration event
        completes, and the steps up to a target chosen beyond the admitted ones (`fr.target`, -1 when there is none; no
        retarget under resv). The frame cannot settle short of them, so another frame's hold on such a cell would leave
        it waiting for that frame's end."""
        si, started, out = self.schedule_instructions, self._defer_started, set()
        for v in self._live_frames:
            if v == skip:
                continue                                                   # (an advance's own frame, `_resv_inc_booked`)
            fr = self._frames[v]
            a = fr.admitted
            if a == 0 and not fr.feas[0]:
                a = next((j for j in range(1, len(fr.feas)) if fr.feas[j]), 0)
            for mem in fr.members[:max(a, fr.target)] + [s for s, _e in fr.h_prefix]:
                out.update(c_ for i in mem if i not in started and self._resv_places(i) for c_ in si[i].instruction.patches)
        return out

    def _resv_hold(self, fr):
        """SER-R: the frame is live; it holds its footprint until it ends (`_resv_release`)."""
        for c_ in fr.resv_fp:
            assert c_ not in self._resv_held, ("a cell two live frames hold", c_, self._resv_held[c_], fr.vid)
            self._resv_held[c_] = fr.vid
        fr.resv_t0 = self.current_round
        self._resv_st["frames"] += 1
        self._resv_fph[len(fr.resv_fp)] += 1
        if fr.resv_unclear:                                                # (records: boundaries up to its top not usable)
            self._resv_st["unclear_frames"] += 1
            self._resv_st["unclear_boundaries"] += len(fr.resv_unclear)
        self._resv_st["held_peak"] = max(self._resv_st["held_peak"], len(self._resv_held))

    def _resv_release(self, fr):
        """SER-R: the frame has ended; its cells are free for ordinary work (records: the held cell-rounds)."""
        for c_ in fr.resv_fp:
            if self._resv_held.get(c_) == fr.vid:
                del self._resv_held[c_]
        self._resv_st["held_cell_rounds"] += len(fr.resv_fp) * (self.current_round - fr.resv_t0)
        self._resv_mh[fr.resv_marks] += 1                                  # (records: the hold marks it wrote, its end's clears too)

    # ---- (W-E, plan v2 7.2, F11): incremental reservation, SER-I and CSR-I (--resv incremental). A live frame
    # holds, from registration to settlement, the footprint of the boundary it stands at (its workspace cells: the script's
    # placed cells and the routing and seam cells its surgery uses, which `_defer_workspace` gives), never less; every
    # other mechanism is static reservation's (the held mask, the dispatch test, the queue entries, the release at its end)
    def _resv_inc_booked(self, skip) -> set:
        """The cells an admitted instruction has booked for a later round, so that a currently empty one is not free (plan
        7.2's check: current emptiness alone is not enough): the live frames' admitted or bound, unstarted members other
        than `skip`'s (`_resv_adm_cells`), the granted script instructions (splices and rebases) not yet started, other
        than `skip`'s, and the ordinary instructions the start-time planner placed for a later round (`_later_times`)."""
        si = self.schedule_instructions
        out = self._resv_adm_cells(skip=skip)
        for c_, scr in (getattr(self, "_defer_cell_scripts", None) or {}).items():
            if any(si[i].start_round < 0 and self._recovery_owner.get(i) != skip for i in scr):
                out.add(c_)
        for i in (getattr(self, "_later_times", None) or {}):
            if i < self.n_original and si[i].start_round < 0 and self._resv_places(i):
                out.update(si[i].instruction.patches)
        return out

    def _resv_inc_free(self, fr, cells, piu=None) -> bool:
        """Every cell of `cells` the frame does not hold is free over the interval to its settlement: `_resv_free` (no
        patch, nothing running or placed this round, no lock, no pending write, no other frame's hold) with every booking
        (`_resv_inc_booked`); and under --cap-hard the union of occupied and held cells stays <= W after it."""
        new = frozenset(cells) - fr.resv_fp
        if not new:
            return True
        piu = (getattr(self, "_r7_piu", None) or set()) if piu is None else piu
        adm = self._resv_inc_booked(fr.vid)
        if any(not self._resv_free(c_, piu, adm) for c_ in new):
            self._resv_st["inc_not_free"] += 1
            self._resv_st["inc_not_free_booked"] += not adm.isdisjoint(new)   # (records: a booking among the reasons)
            return False
        return not self._ws_hard_exceeds(new)

    def _resv_inc_check(self, fr, need, kind):
        """One request of the check port for an advance ("adv") or a revision ("rev"), charged as the registration's for
        the same footprint (`_r9_event`: c0 as a grant, then `RESV_COST["reads"]` accesses at c1): can the frame take
        `need`?  True: it stands; False: refused; None: no answer this round (in service, no room, or a stored answer
        whose cells changed): the requester asks again."""
        st = self._resv_st
        st["inc_checks_" + kind] += 1
        new = frozenset(need) - fr.resv_fp
        if not new:
            return True                                                    # (nothing to take: no request)
        st["inc_port_" + kind] += 1                                        # (F2: a request of the port, by kind;
        #                                                                     inc_checks_ counts every call, one with nothing to take too)

        def fn():
            self._r9.ev.acc += self._resv_cost["reads"]
            return None if self._resv_inc_free(fr, new) else "g"
        ek = "fwd" if kind == "adv" else "rev"                             # (the port's grant kinds; a stop is its hold, not a refusal)
        n_app = self._r9.stats["ticket_applied:" + ek]
        ans = self._r9_event(ek, (ek, fr.vid, "inc", tuple(sorted(new)), True), fn, "stop", vid=fr.vid,   # (the fifth
                             accept=lambda ev, a_: a_ is None, sig=lambda: ((new,), ()), wq_o=None, batch=0)   #  element is
        # `_r9_fwd`'s count, which r9-guard-close 4 reads for a FORWARD's identity (a counted FORWARD's turn apart from a
        # what-if's): this check is the advance or revision itself and takes the cells it is granted, so it is never a what-if
        if self._r9.stats["ticket_applied:" + ek] != n_app:
            st["inc_collected_" + kind] += 1                               # (its stored answer collected, after it waited)
        if ans is R9_WAIT or ans == "stop":
            st["inc_wait_" + kind] += 1
            return None
        if ans is not None:
            st["inc_refused_" + kind] += 1
            return False
        if not self._resv_inc_free(fr, new):                               # (collection reads the cells again, as `_resv_reg`)
            st["inc_stale_" + kind] += 1
            return None
        st["inc_granted_" + kind] += 1                                     # (port = wait + refused + stale + granted)
        return True

    def _resv_inc_adv(self, fr, a: int, b: int) -> bool:
        """The advance (plan 7.2): before the block a..b-1 is admitted, the frame takes boundary b's footprint by the check,
        then releases what it held outside it, in one controller operation (`_resv_inc_move`), so at every round it holds
        a footprint at which it can recover.  Refused: the frame stays at its boundary for good (`resv_stuck`), and the
        members that needed the advance stay unadmitted, so they wait for the value's decoded outcome, as under waiting.
        No answer yet: it stays and asks again at its next admission.  True: admit the block."""
        if getattr(fr, "resv_stuck", False):
            return False
        need = frozenset(self._defer_workspace(fr, b))
        ok = self._resv_inc_check(fr, need, "adv")
        if not ok:
            if ok is False:
                fr.resv_stuck = True
                self._resv_st["inc_adv_refused"] += 1
            return False
        self._resv_inc_move(fr, need)
        self._resv_st["inc_advances"] += 1
        return True

    def _resv_inc_move(self, fr, need):
        """Acquire before release, one controller operation (plan 7.2): the frame takes every cell of `need` it does not
        hold, and only then releases the cells it held outside `need`; its held mask follows.  Records: the held
        cell-rounds of the footprint it leaves, and the peak cells one frame held at once (old and new together)."""
        need, old, held, st = frozenset(need), fr.resv_fp, self._resv_held, self._resv_st
        for c_ in sorted(need - old):
            assert held.get(c_, fr.vid) == fr.vid, ("a cell another live frame holds", c_, held.get(c_), fr.vid)
            held[c_] = fr.vid
        st["held_peak"] = max(st["held_peak"], len(held))
        st["inc_peak_frame"] = max(st["inc_peak_frame"], len(old | need))
        for c_ in sorted(old - need):
            if held.get(c_) == fr.vid:
                del held[c_]
        st["held_cell_rounds"] += len(old) * (self.current_round - fr.resv_t0)
        fr.resv_t0, fr.resv_fp, fr.guard = self.current_round, need, set(need)
        st["inc_moves"] += 1

    def _resv_inc_settle(self, fr, ok) -> list:
        """Settlement (plan 7.2): of the boundaries `ok` the decision may take, those whose footprint the frame holds; it
        settles only there, with nothing it does not hold.  Boundaries left out are counted; none held among them (an
        invariant failure, expected 0: it holds its standing boundary's footprint) is counted apart, and the frame waits
        as a decision waits."""
        out = [j for j in ok if self._defer_workspace(fr, j) <= fr.resv_fp]
        if len(out) < len(ok):
            self._resv_st["inc_settle_not_held"] += len(ok) - len(out)
        if ok and not out:
            self._resv_st["inc_settle_none_held"] += 1
        return out

    def _resv_test(self, idx: int, pts) -> str | None:
        """SER-R's dispatch test: an ordinary instruction whose placement targets a held cell (one with no
        patch, which a live frame holds) waits until that frame ends: the dispatcher's guarded-cell test, as v16's, with no
        check after it (`RESV_COST["test"]` port-1 cycles once a round, 0). This is a wait on the frame; the frame never
        waits on such an instruction (`_resv_usable`; the splice orders a script after no unstarted instruction)."""
        held = self._resv_held
        if not held:
            return None
        hw = self._hw
        if hw is not None and not self._resv_lookup(idx, pts):
            return "hw_guard_check"                                        # (the held-bit lookup is not
        if self._resv_tested.get(idx) != self.current_round:               #  served this round; the instruction waits, as rule 66's)
            self._resv_tested[idx] = self.current_round
            self._resv_st["lookups"] += 1                                  # (every test performed, passing or blocking, is charged)
            if hw is not None and self._resv_cost["test"]:
                hw.ev_take(self._resv_cost["test"])
            if any(p in held and p not in self._active_patches for p in pts):
                self._resv_st["tests"] += 1
        return "defer_resv" if any(p in held and p not in self._active_patches for p in pts) else None

    def _resv_lookup(self, idx: int, pts) -> bool:
        """SER-R: the dispatcher reads the held bits of an ordinary instruction's target
        cells through the dispatch port, the status port's guard check (1 + c0 + f cycles of the check port, once a round per
        instruction, the port's room, aging and turn as every dispatch check), and nothing after it: no `plc` or `rel` walk
        (`_r9_dispatch`). False: not served this round. Run only while a frame holds cells: with none held the held mask is
        empty and there is nothing to read. The bookkeeping of a refused or served check is `_defer_hold`'s."""
        hw, r9, cells = self._hw, self._r9, list(pts)[:4]
        if not cells:
            return True
        if not self._r9_gc_turn(idx) or not hw.guard_check(idx, cells, aged=r9.aged_head(("gc", idx)) or ("disp", idx) in r9.tickets):
            r9.qwait(lambda: "gc", ("gc", idx), self.current_round)
            held = self._wq is not None and self._wq.holds
            if ("disp", idx) not in r9.tickets:
                if held:
                    r9.resume(("gc", idx), self.current_round)
                r9.refused(("gc", idx), self.current_round)
            elif held:
                r9.carry(("gc", idx), self.current_round)
            self._wq_keep(("gc", idx))
            return False
        r9.qserve(("gc", idx), self.current_round)
        if self._wq is not None and self._wq.holds:
            r9.suspend(("gc", idx), self.current_round)
            self._wq_keep(("gc", idx))
        else:
            r9.served(("gc", idx))
        return True

    def _resv_start(self, idx: int):
        """SER-R's invariant (records): no ordinary placement starts on a held cell; one that did is counted. A
        zero-duration instruction that is not a resource initialisation passes the dispatcher before any guard: it starts
        and ends in this dispatch, never runs a round, and so puts no patch on any cell (only a running instruction adds
        its cells to `_active_patches`, SWIPER's round step; a DISCARD only removes them). Those on a held cell are
        counted (`zero_on_held`)., CSR: with two frames live a frame's member is counted alike on a cell
        another live frame holds; G1's two halves keep it at 0 (`_defer_admissible`, `_resv_free`). A placing member is
        counted whether or not the cell carries a patch, as the halves ignore it."""
        mine = self._defer_member.get(idx)
        if idx >= self.n_original or (mine is not None and len(self._live_frames) < 2):
            return
        other = [p for p in self.schedule_instructions[idx].instruction.patches if self._resv_held.get(p, mine) != mine]
        on_held = any(p not in self._active_patches for p in other)
        if not (self._is_startup_instruction[idx] or self._instruction_durations[idx] > 0):
            self._resv_st["zero_on_held"] += on_held
            return
        if on_held or (mine is not None and other):
            self._resv_st["placed_on_held"] += 1

    def _resv_ws(self, fr, piu):
        """SER-R (records only): a settlement round that waits because a workspace cell of its target carries a patch or
        runs an instruction.  Each such cell's occupant -- the instruction running on it, else the last that started on
        it (a script's, else the program's) -- is classed: the frame's own member, the frame's own script (an early
        script or the settlement's), another frame's script, or ordinary work; the round counts once per class seen,
        and the first case of each class is kept."""
        st, seen = self._resv_st, set()
        st["ws_rounds"] += 1
        for c_ in self._defer_workspace(fr, fr.target):
            if c_ not in self._active_patches and c_ not in piu:
                continue
            run = [i for i in self._active_instructions if c_ in self.schedule_instructions[i].instruction.patches]
            scr = self._defer_cell_scripts.get(c_) or []
            ls = scr[-1] if scr and self.schedule_instructions[scr[-1]].start_round >= 0 else None
            lo = self._defer_last_started(c_)
            sr = lambda i: -1 if i is None else self.schedule_instructions[i].start_round
            occ = run[0] if run else (ls if sr(ls) > sr(lo) else lo)       # (the later start: a script's or the program's)
            if occ is None:
                cls = "none"
            elif occ >= self.n_original:
                cls = "own_script" if self._recovery_owner.get(occ) == fr.vid else "other_script"
            elif self._defer_member.get(occ) == fr.vid or occ in fr.kof:
                cls = "own_member"
            else:
                cls = "ordinary" if self._instruction_durations[occ] > 0 or self._is_startup_instruction[occ] else "ordinary_zero"
            if cls not in seen:
                seen.add(cls)
                st["ws:" + cls] += 1
                d = lambda i: None if i is None else dict(idx=i, name=self.schedule_instructions[i].instruction.name, start=sr(i),
                                                         dur=self._instruction_durations[i] if i < self.n_original else None,
                                                         owner=self._recovery_owner.get(i))
                self._resv_wsf.setdefault(cls, dict(round=self.current_round, vid=fr.vid, reg=fr.resv_t0, target=fr.target,
                                                    cell=list(c_), running=bool(run), occ=d(occ), script=d(ls), program=d(lo),
                                                    rebased=bool(fr.rebased)))

    def _resv_summary(self) -> dict:
        """SER-R's records (plan 2.6): registrations refused because the footprint was not free, the held cell-rounds,
        ordinary work's waits on held cells (instruction-rounds), the dispatch tests charged, and the safety counts: an
        ordinary placement started on a held cell (0), a settlement's rounds waiting on its workspace (`ws_by`: its own
        member or script only). review: the frames registered with boundaries up to their top not usable
        (`unclear`), revisions refused for an unclear workspace, the rounds a frame's own hold kept a prerequisite of its
        next step (`self_block`), and zero-duration instructions started on a held cell (`zero_on_held`, no patch)."""
        st, fph, mh = self._resv_st, self._resv_fph, self._resv_mh
        hc = self._wq.h_ev.get("H", {}) if self._wq is not None else {}    # (the hold-change events' cycles -> events)
        n = sum(fph.values())
        return dict(setting=self._resv, cost=dict(self._resv_cost), frames=st["frames"], refused=st["refused"], stale=st["stale"],
                    rev_checks=st["rev_checks"], rev_refused=st["rev_refused"], rev_refused_unclear=st["rev_refused_unclear"],
                    unclear=dict(frames=st["unclear_frames"], boundaries=st["unclear_boundaries"], rebase=st["rebase_unclear"],
                                 native=self._defer_native.get("no_step:resv_unclear", 0),
                                 completion=self._defer_native.get("no_step:resv_completion_condition", 0)),
                    self_block=dict(frames=st["self_block_frames"], rounds=st["self_block_rounds"]), zero_on_held=st["zero_on_held"],
                    t025f=dict(lookups=st["lookups"], collect_reads=st["collect_reads"], rev_refused_completion=st["rev_refused_completion"],
                               rev_refused_room=st["rev_refused_room"], decide_room_waits=st["decide_room_waits"],
                               marks_secured=st["marks_secured"], marks_entered=st["marks_entered"],
                               marks_enter_waits=st["marks_enter_waits"], script_secured=st["script_secured"]),
                    t025j=dict(takeback_refused=st["takeback_refused"], floor_checks=st["floor_checks"], floor_counts=st["floor_counts"]),
                    tests=st["tests"], waits=self._defer_stats.get("held_resv", 0), held_cell_rounds=st["held_cell_rounds"],
                    held_peak=st["held_peak"], held_at_end=len(self._resv_held), placed_on_held=st["placed_on_held"],
                    ws_wait_rounds=st["ws_rounds"], ws_by={k[3:]: n for k, n in sorted(st.items()) if k.startswith("ws:")},
                    ws_first=dict(sorted(self._resv_wsf.items())),
                    footprint=dict(n=n, max=max(fph, default=0), mean=round(sum(k * c for k, c in fph.items()) / n, 3) if n else None),
                    rows=dict(keys=len(self._resv_rows), computed=st["rows"], reads=st["row_reads"], variants=st["row_variants"]),
                    marks=dict(written=st["marks"], unbuffered=st["marks_unbuffered"], admitted=st["marks_admitted"],
                               no_room=st["marks_no_room"], frames=sum(mh.values()), max=max(mh, default=0),
                               mean=round(sum(k * c for k, c in mh.items()) / sum(mh.values()), 3) if mh else None,
                               h_events=sum(hc.values()), h_cycles=sum(k * c for k, c in hc.items()), h_cycles_max=max(hc, default=0)),
                    static=dict(rev_static_only=st["rev_static_only"], rev_dyn_only=st["rev_dyn_only"],
                                rebase_static_only=st["rebase_static_only"], rebase_dyn_only=st["rebase_dyn_only"]),
                    **({"csr": dict(k="inf" if self._live_cap is None else self._live_cap, g1_adm=st["g1_adm"], g1_reg=st["g1_reg"])}
                       if self._live_cap != 1 else {}),
                    **({"incremental": {k[4:]: n for k, n in sorted(st.items()) if k.startswith("inc_")}} if self._resv_inc else {}),
                    **({"refused_cap_hard": st["refused_cap_hard"]} if self._ws_hard else {}))   # (plan 7.2-7.3: records only)
                                                                           # (CSR, records: its k; the frame-rounds a frame's next
                                                                           #  step waited on another frame's hold, and the registration checks
                                                                           #  refused with a cell an admitted or bound member places on: G1)

    def _r9_rev(self, fr, tmp, j: int, new_holds, F2: int) -> bool:
        """Rule (a2) at a revision (`_r8_rev_refuses`) as one event, with the revised contract's reservation.  True:
        refused (this round)."""
        st = self._defer_stats

        def fn():
            own = lambda x: x in new_holds or x in tmp.fence or x in tmp.kof
            ownj = lambda x: x in new_holds or x in fr.fence or fr.kof.get(x, -1) >= j
            gc = set().union(*(self._defer_workspace(tmp, jj) for jj in range(F2 + 1) if tmp.feas[jj]))
            ev = self._r9.ev                                               # (r9-guard-close 4: the cells it guards, and its own
            ev.gc = frozenset(gc)                                          #  members the rebase drops from j on: ordinary then)
            ev.rm = frozenset(x for x, k in fr.kof.items() if k >= j and x not in tmp.kof and x not in self._defer_started)
            h = self._r8_fwd_held(fr, j)
            h0, hj = (lambda x: own(x) or h(x)), (lambda x: x in new_holds or h(x))
            refuse = self._r8_refuses_at(tmp, 0, guard=gc, held=h0) or self._r8_refuses_at(fr, j, guard=gc, held=hj) \
                or self._r8_pins_others(fr, new_holds, j)
            if not refuse and not self._r8_cert([(self._defer_workspace(tmp, 0), h0, gc, own), (self._defer_workspace(fr, j), hj, gc, ownj)],
                                                self._r8_perms(top={fr.vid: j})):
                st["r7:rule_reb_member_refusals"] += 1
                refuse = True
            if not refuse and not self._r9_guard_new(fr.vid, gc, own_all):
                self._r9.stats["guard_new_refusals:rev"] += 1
                refuse = True
            if refuse:
                st["r7:rule_reb_refusals"] += 1
            return bool(refuse)
        own_all = lambda x: x in new_holds or x in tmp.fence or x in tmp.kof or x in fr.fence or fr.kof.get(x, -1) >= j
        n = self._r9_script_len(tmp, [jj for jj in range(F2 + 1) if tmp.feas[jj]])
        cells = self._defer_workspace(tmp, 0) | self._defer_workspace(fr, j)
        acc = lambda ev, ans: (not ans) and self._r9_accept(ev, fr.vid, cells, own_all, resv=max(n, self._r9.resv.get(fr.vid, 0)))
        sig = lambda: ((j, frozenset(cells), n),
                       (frozenset().union(*(self._defer_workspace(tmp, jj) for jj in range(F2 + 1) if tmp.feas[jj])),
                        frozenset(new_holds) | frozenset(tmp.fence) | frozenset(tmp.kof) | frozenset(fr.fence)
                        | frozenset(x for x, k in fr.kof.items() if k >= j)))
        wq_o = (lambda: [(("fr", fr.vid), 2 * min(len(new_holds), self._r9.W), True),   # (its new hold marks, set and
                         (("sc", fr.vid), self._wq_script(tmp, [jj for jj in range(F2 + 1) if tmp.feas[jj]]), True)]) \
            if self._wq is not None else None                              #  cleared in the window's W rows at most, and the
                                                                           #  revised contract's scripts)
        ans = self._r9_event("rev", ("rev", fr.vid, j), fn, True, vid=fr.vid, accept=acc, sig=sig, wq_o=wq_o)
        if ans is R9_WAIT:
            st["r7:rule_reb_refusals"] += 1
            return True
        return bool(ans)

    def _r9_guard_new(self, vid, cells, own) -> bool:
        """A grant that guards cells no live frame guards yet (a registration's candidate workspaces, a revision's) checks
        the release work on them: every unstarted ordinary placement onto such a cell that lies on a live frame's chains
        (its entry's chain mask; r9-guard-close 3: a revising frame's own earlier chains included, which its revision's
        walks need not read again) must have its patch's release wait on nothing held -- every live frame's holdable
        work and the grant's own holds -- and is then covered for those frames; otherwise the grant waits. So a guard
        written later never meets an uncovered release placement of an earlier certificate (plan v2 4.2: later grants are
        checked against the release work in turn). r9-guard-close 4: so is a member that can later become an ordinary
        instruction (`_r9_mo`) on a live frame's chains other than its own frame's (or on its own frame's, when this
        revision drops it), walked from its patch's release with the member itself not counted as held (it is the
        placement). Charged inside the grant's event: a cell's entry, each user's entry, the release lookup and the walk.
        cov=enforce (proof-check items 1 and 3): the scan runs from the cell's first unstarted user to the window's
        tail, each next user one index read, and a placement is one the user can still make (`_r12_pp`, read along it)."""
        r9, started, cells_of = self._r9, self._defer_started, self._defer_cells
        owners_of = [y for y in sorted(self._live_frames) if r9.chain.get(y)]    # (a frame not yet live has none: `_r9_accept`)
        if not owners_of:
            return True
        ev, cur = r9.ev, {}
        held = lambda z: self._r7_holdable(z) or own(z)
        new = [q for q in cells if q not in self._defer_ws_cells and q not in self._r8_pend
               and (r9.cov or q not in self._defer_target_cells)]          # (cov=enforce: E2 reads a cell as guarded only
        si, tail = self.schedule_instructions, r9.tail                     #  when the dispatcher does, a candidate or pending cell)
        for q in sorted(new, key=repr):
            r9.charge(R9A["cell"])
            lst, k0, pp, rn = cells_of.get(q, ()), 0, False, False
            if r9.cov:                                                     # (from the cell's first
                self._defer_first_unstarted(q)                             #  unstarted user, its entry's field, to the window's
                k0 = self._defer_cell_ptr.get(q, 0)                        #  tail: every chain entry lies there)
            for k in range(k0, len(lst)):
                x = lst[k]
                if x in started:
                    continue
                if r9.cov:
                    if k > k0:
                        r9.charge(R9A["next_user"])                        # (the next user: the entry's per-cell index)
                    if x > tail:
                        break
                    if k == k0:
                        pp = self._r8_places(x, q)
                        rn = pp and q in r9.vrun                           # (the re-check's HIGH: the run a script's mark
                    else:                                                  #  covers, read with the cell's entry)
                        dsc = si[lst[k - 1]].instruction.name == "DISCARD"
                        tr = pp and not dsc and self._r12_tr(lst[k - 1])
                        pp, rn = dsc or tr, rn and tr
                r9.charge(R9A["perm"])                                     # (the user's entry: its chain mask; item 1: its
                owners = [y for y in owners_of if x in r9.chain[y]]        #  zero-duration bit, `_r12_pp` read along the scan)
                if not owners:
                    continue
                g = self._r8_guardable(x)
                if not g and (self._r9_own(x, owners, ev.rm) or not self._r9_mo(x, cur)):
                    continue                                               # (r9-guard-close 4: not a potential placement)
                if not (pp if r9.cov else self._r8_places(x, q)) or rn or r9.covered(x, q, owners) \
                        or any((x, q) in c_ and not set(o_).isdisjoint(owners) for o_, _v, c_ in ev.gn):
                    continue
                rel, freed = self._r8_release(q, k + 1)
                r9.charge(R9A["release"] * (2 if freed else 1))
                before = set(ev.followed)
                try:
                    bad = not freed or self._r8_walk([rel], held if g else (lambda z, x=x: z != x and held(z)))
                except R9Stop:
                    if not g:
                        r9.stats["guard_new_member:" + ev.kind] += 1      # (r9-guard-close 4: a member's, records)
                    raise
                if bad:
                    if not g:
                        r9.stats["guard_new_member:" + ev.kind] += 1
                    return False
                ev.gn.append((owners, set(ev.visited), {(x, q)} | (ev.followed - before)))   # (r9-guard-close 3: installed
                r9.stats["guard_new_covered"] += 1                         #  if the grant stands, `_r9_accept`)
                if not g:
                    r9.stats["guard_new_member_covered"] += 1
            else:
                if r9.cov and k0 < len(lst):
                    r9.charge(R9A["next_user"])                            # (the read that finds no next user)
        return True

    def _r9_vacate(self, cells, src=None):
        """r9-guard-close 2: a script's DISCARD has freed `cells`. On such a cell the next original instruction puts a new
        patch when the one before it there is not a DISCARD (`_r8_places`, its third clause): a placement no certificate's
        walk could follow, since the cell was occupied then. Its patch's release is the release of the patch the script
        discarded (no DISCARD lies between them), so the cell is held until the same release as before the script freed
        it, and every certificate made meanwhile counted it so. The placement is release work: admitted without a walk
        (`R9.covered`), and a placement permission of every later certificate (`R9.perms`) until it starts. The second
        port writes one mark each (records only). r9-guard-close 5: x may still be a member when the script frees the
        cell; one that can later become an ordinary instruction before it starts (`_r9_mo`) is marked too, since the
        guard holds it then and no walk followed its placement either., cov=enforce: so is
        each user after x there that becomes the cell's first unstarted user when x and the users between them put no
        patch (`_r12_tr`), up to the first that puts one or a DISCARD: its patch's release is the same release. The
        re-check's HIGH: one mark on the cell covers that whole run (`R9.vrun`, `_r12_run`), the one placement mark a
        cell the DISCARD reserved (`_wq_script`, `_wq_splice`, `_wq_start`), and no window entry is read or written; a
        start that puts a patch there or discards it ends the run and clears the mark with its occupant write
        (`_r12_vstart`). The third re-check's MED: the mark reads q's entry only (its next user and whether its last
        start was a DISCARD's), not x's, which can lie past the window's tail; when x is a DISCARD the mark covers no
        placement, and x's start clears it."""
        r9, n, cur, si = self._r9, 0, {}, self.schedule_instructions
        for q in cells:
            x = self._defer_first_unstarted(q)
            if x is not None and r9.cov:
                lst, k = self._defer_cells.get(q, ()), self._defer_cell_ptr.get(q, 0)
                if k == 0 or si[lst[k - 1]].instruction.name == "DISCARD":
                    continue                                               # (static: E1 reads it and the run after it, `_r12_pp`.
                if q not in r9.vrun:                                       #  x's entry is not read (the third re-check's MED: it
                    r9.vrun.add(q)                                         #  can lie past the tail); a mark before a DISCARD
                    n += 1                                                 #  covers nothing, and that DISCARD's start clears it)
                continue
            if x is None or self.schedule_instructions[x].instruction.name == "DISCARD" \
                    or not (self._r8_guardable(x) or self._r9_mo(x, cur)):
                continue                                                   # (a DISCARD places nothing; nor is a member that
                                                                           #  never becomes ordinary held: r9-guard-close 5)
            lst = self._defer_cells.get(q, ())
            k = bisect.bisect_left(lst, x)
            if k == 0 or self.schedule_instructions[lst[k - 1]].instruction.name == "DISCARD":
                continue                                                   # (a placement in program order: walks follow it)
            if (x, q) not in r9.vac:
                r9.vac.add((x, q))
                n += 1
        if n:
            r9.stats["vacated"] += n
            r9.port2(self.current_round, n)
            if self._wq is not None and src is not None:
                self._wq.issue(("in", src), n, "P", self.current_round)   # (on the DISCARD's reservation, made at its splice)

    def _r9_script_len(self, fr, js) -> int:
        """The window entries a frame reserves: its longest settlement script over the boundaries it may reach."""
        n = 0
        for j in js:
            low = self._defer_boundary(fr, j)[0]
            if low is not None and low.instrs:
                n = max(n, len(low.instrs))
        return n

    def _r9_accept(self, ev, vid, cells, own, resv=None) -> bool:
        """An authorizing grant's update part: its reservation (a capacity hit stops the event: the safe answer), the
        certificate's cells in the cell table, its chains marked (one access an entry), and the guarded placements on
        them covered (`_r9_cover`).  r9-guard-close: the authorization stands only if every placement its walks followed
        is covered.  One left uncovered (its release waits on work a live frame can hold or on the grant's own holds, or
        its cover walk stopped) stops the event with the safe answer (`cover:<kind>`): nothing is installed and the
        reservation made here is given back, so no release placement of a live frame ever needs a walk of its own.
        r9-guard-close 4: so does a potential placement of a member its walks read that can later become an ordinary
        instruction (`_r9_mplc`), uncovered (`member:<kind>`): the chains go to the frame and to the frames of its later
        guard's covers (`ev.gn`).  r9-guard-close 5: and so does a coverage scan a stop cut short (`ev.cut`, counted as
        `member:<kind>` when no followed placement is left uncovered): the members past the stop were never enumerated."""
        r9, wq = self._r9, self._wq
        if resv is not None:
            old = r9.resv.get(vid)
            if wq is not None and ev.upd + int(resv) > r9.bmax:
                raise R9Stop("wq")                                         # (past the batch cap, before it reserves)
            if not r9.reserve(vid, int(resv), self.current_round):
                raise R9Stop("window")
            r9.hold_now(vid, self.current_round)
            ev.upd += int(resv)
        cov = self._r9_cover(ev, own, recv={vid}.union(*(o_ for o_, _v, _c in ev.gn)))
        unc = any(x not in self._defer_started for (x, _q) in self._r9_left(ev.followed, cov))
        why = ("cover" if unc else "member") if unc or ev.cut or any(x not in self._defer_started for (x, _q) in self._r9_left(ev.mpl, cov)) \
            else None
        recv, e1, nw = {vid}.union(*(o_ for o_, _v, _c in ev.gn)), set(), 0
        if why is None and r9.cov:                                         # (cov=enforce, E1: every guarded placement its
            try:                                                           #  entries can still make is covered, its own cells
                e1, nw = self._r12_e1(ev, own, recv, cov.union(*(g_ for _o, _v, g_ in ev.gn)))   #  counted; else the safe answer)
            except R9Stop as e:
                why = e.why
        if why is None and wq is not None:                                 # (its batch, its installs' clearing writes and
            why = self._wq_fit(ev, len(cells) + len(ev.visited) + nw,      #  its consequences fit, or the safe answer)
                               [(y, ev.visited) for y in sorted(recv)] if r9.cov else
                               [(vid, ev.visited)] + [(y, vis) for owners, vis, _g in ev.gn for y in owners], vid, cells, resv)
        if why is not None:
            if resv is not None:
                if old is None:
                    r9.resv.pop(vid, None)
                else:
                    r9.resv[vid] = old
            raise R9Stop(why)
        ev.upd += len(cells) + len(ev.visited) + nw
        if vid not in self._live_frames:                                   # (r9-guard-close 3: a frame not yet live has no chain
            if wq is not None:                                             #  or cover of its own: an earlier event's, whose
                self._wq_drop(vid)                                         #  answer never applied, goes; cleared)
            r9.chain.pop(vid, None)
            if r9.cover.pop(vid, None):
                r9.cover_ver += 1
        if wq is not None:
            self._wq_mark(ev, vid, cells, resv, [vid] + [y for owners, _v, _g in ev.gn for y in owners])
        if r9.cov:                                                         # (cov=enforce: every frame that gets the entries
            allc = cov.union(*(g_ for _o, _v, g_ in ev.gn)) | e1           #  gets their covers, so a cover lasts with its entry:
            for y in sorted(recv):                                         #  the registering frame may never go live, G2)
                r9.install(y, ev.visited, allc)
            return True
        r9.install(vid, ev.visited, cov)
        for owners, vis, gcov in ev.gn:                                    # (a later guard's covers for other frames' chains,
            for y in owners:                                               #  now that the grant stands: a refused grant
                r9.install(y, vis, gcov)                                   #  installs nothing)
        return True

    def _r12_cond(self, u: int) -> bool:
        """can u's duration still change? A value's group gets its durations once, at its
        ACCEPT's speculation (arm Q), its deferral (arm D) or its verdict when neither came first (`_apply_branch`; each
        sets `speculated` or settles the value), a feedback group's at its delivery (`_deliver_feedback`); until then u
        has its true duration, and after it never changes again (so a zero duration stays zero)."""
        vid = (getattr(self, "_value_of_instr", None) or {}).get(u)
        if vid is not None:
            v = self.values[vid]
            if not v.speculated and v.state not in (VState.CONFIRMED, VState.RECOVERING, VState.RETIRED):
                return True
        fid = (getattr(self, "_feedback_of_instr", None) or {}).get(u)
        return fid is not None and self.feedback[fid].delivered_round < 0

    def _r12_tr(self, u: int) -> bool:
        """may u, an original instruction that has not started, start without putting a patch
        (zero duration: it ends at once and adds none, device.py 3090), now or once its group is decided? The window's
        entry holds this bit; it changes only with the duration write the decision makes anyway."""
        return u not in self._defer_started and (self._instruction_durations[u] == 0 or self._r12_cond(u))

    def _r12_pp(self, x: int, q) -> bool:
        """cov=enforce: may x still put a new patch on cell q? `_r8_places`, with the users
        before x there that may put no patch (`_r12_tr`) passed over, since each can start, add nothing, and leave x the
        first unstarted user of a free cell: a DISCARD before them, or none, places; a started user (x then follows the
        cell's first unstarted users) places when the cell is free now, as `_r8_places` reads it; a user that will put a
        patch does not. A cell occupied now is freed only by a DISCARD: an original one is read here, a script's marks
        the placements it makes (`_r9_vacate`). Inside an event, each user passed over is one per-cell index read."""
        return self._r12_pk(x, q) is not None

    def _r12_pk(self, x: int, q):
        """`_r12_pp` with the way it holds: "order" (a DISCARD before the users passed over, or none: a placement in program
        order) or "run" (the cell is free now and the scan reached its last started user, which is not a DISCARD: the
        placement is the run's a script's mark on the cell covers, `_r12_run`); None when x cannot place there."""
        lst, si, r9 = self._defer_cells.get(q, ()), self.schedule_instructions, self._r9
        k = bisect.bisect_left(lst, x)
        while k > 0:
            w = lst[k - 1]
            if si[w].instruction.name == "DISCARD":
                return "order"
            if w in self._defer_started:
                return "run" if q not in self._active_patches and q not in self._r7_piu else None
            if not self._r12_tr(w):
                return None
            if r9 is not None and r9.ev is not None:
                r9.charge(R9A["next_user"])
            k -= 1
        return "order"

    def _r12_run(self, x: int, q) -> bool:
        """cov=enforce (the re-check's HIGH): does q's run mark (`_r9_vacate`) cover x's placement there, x the cell's
        first unstarted user (its entry's field)? The mark is set only while q is free and its last started user is not
        a DISCARD (`_r12_vstart`), so x then puts the run's patch, whose release is the one the script discarded (a DISCARD
        x puts none, and its start clears the mark). A user further on is in the run when `_r12_pk` reads "run" for it
        (E1, E2)."""
        r9 = self._r9
        return r9 is not None and q in r9.vrun and self._defer_first_unstarted(q) == x

    def _r12_vstart(self, idx: int):
        """cov=enforce (the re-check's HIGH): a start that puts a patch on a cell, or a DISCARD's, ends the run a
        script's mark covers there; the start's write of the cell's occupant (1 + k, device.py's port 2) clears the mark,
        so no write is added. A start that puts no patch keeps it: the cell stays free and the run goes on with its next
        user."""
        ins = self.schedule_instructions[idx].instruction
        if self._instruction_durations[idx] > 0 or ins.name == "DISCARD":
            self._r9.vrun.difference_update(ins.patches)

    def _r9_left(self, xqs, cov) -> set:
        """The placements in `xqs` that neither `cov` nor a script's marks cover (`vac`; cov=enforce: a run's,
        `_r12_run`)."""
        r9 = self._r9
        out = xqs - cov - r9.vac
        return {xq for xq in out if not self._r12_run(*xq)} if r9.vrun else out

    def _r12_e1(self, ev, own, recv, base):
        """cov=enforce (plan v2 1.4, E1): before an event installs, every guarded placement that an entry it installs
        can still make is covered. The entries: everything its walks read (`ev.visited`: rule (a)'s, its later guard's and
        its cover walks' alike), each going on the chains of every frame in `recv`, and a revision's members it drops that
        lie on those frames' earlier chains (`ev.rm`: ordinary once the rebase applies). The guarded cells: those
        guarded once the grant stands, a live frame's candidate, target or pending cell and the grant's own (`ev.gc`). A
        placement it
        can still make (`_r12_pp`: the users before it there that may put no patch passed over) of an
        entry the candidate guard can hold, or of a member that can later become one (`_r9_mo`, read as
        `_r9_mplc` reads it), is covered by a script (`vac`), by the event's own covers (`base`), by a live cover (its bit is
        already set; installed for `recv` too, so it lasts with the entry), or by a cover walk of its own that counts the
        grant's cells as guarded: the release waits on nothing held (`_r7_holdable`, the grant's own holds), and every
        placement the walk follows is covered with it. A walk's new entries are tested in turn. Charged inside the event
        as today's accesses are: an entry's cells (1 each), a cover bit's read (1), each walk's release lookup and nodes;
        each cover bit a walk sets is one write of the update part (returned, the caller adds it). Returns (the covers for
        `recv`, the bits set). R9Stop("cov") when a placement's release waits on held work or nothing frees it; the walks'
        own stops (budget, window, beyond) as they come: the safe answer, and nothing is installed."""
        r9, si, started, n0 = self._r9, self.schedule_instructions, self._defer_started, self.n_original
        ws, tg, pd, gc, live = self._defer_ws_cells, self._defer_target_cells, self._r8_pend, ev.gc, self._live_frames
        held = lambda z: self._r7_holdable(z) or own(z)
        out, done, cur, nw = set(), set(), {}, 0
        drop = set().union(*(ev.rm & r9.chain.get(y, set()) for y in recv)) if ev.rm else set()
        while True:                                                        # (and a revision's members it
            todo = sorted(x for x in ev.visited | drop if x not in done)   #  drops, on its frame's earlier chains: ordinary
            if not todo:                                                   #  once the rebase applies, `_r9_cover`'s scan)
                break
            for x in todo:
                done.add(x)
                if x in started or x >= n0:
                    continue
                g = self._r8_guardable(x)
                if not g and (self._r9_own(x, recv, ev.rm) or not self._r9_mo(x, cur)):
                    continue                                               # (not a placement a guard can ever hold)
                pts = si[x].instruction.patches
                r9.charge(R9A["cell"] * len(pts))
                for q in pts:
                    xq = (x, q)
                    if xq in base or xq in out or xq in r9.vac or not (q in ws or q in tg or q in pd or q in gc):
                        continue
                    pk = self._r12_pk(x, q)
                    if pk is None or (pk == "run" and q in r9.vrun):
                        continue                                           # (the re-check's HIGH: a script's run mark, read with
                    r9.charge(R9A["perm"])                                 #  the cell's entry) (the placement's cover bit)
                    if r9.covered(x, q, live):
                        out.add(xq)
                        r9.stats["cov_kept:" + ev.kind] += 1
                        continue
                    rel, freed = self._r8_release(q, bisect.bisect_left(self._defer_cells.get(q, ()), x) + 1)
                    r9.charge(R9A["release"] * (2 if freed else 1))
                    before = set(ev.followed)
                    if not freed or self._r8_walk([rel], held if g else (lambda z, x=x: z != x and held(z)), gc):
                        r9.stats["cov_refusals:" + ev.kind] += 1
                        raise R9Stop("cov")
                    new = ({xq} | (ev.followed - before)) - base - out
                    out |= new
                    nw += len(new)
                    r9.stats["cov_walks:" + ev.kind] += 1
        r9.stats["cov_bits:" + ev.kind] += nw
        return out, nw

    def _r9_cover(self, ev, own, recv=None, members=False) -> set:
        """The guarded placements an authorizing event's walks followed that are covered: the release of the patch each
        puts on its guarded cell waits, through unstarted predecessors and the guarded placements the walk follows on,
        on nothing any live frame can hold (`_r7_holdable`) nor on the grant's own post-decision holds (`own`).  Each is
        one more walk of the event, inside its budget; one the budget or the window stops is left uncovered, and so is
        the rest.  Every placement a successful walk followed is covered.  (r9-guard-close: an uncovered one refuses the
        authorization, `_r9_accept`.)  r9-guard-close 4: with `recv` (the frames whose chains get the event's entries),
        the members the walks read (all of them, a cover walk's included) that can later become ordinary instructions
        give their potential placements (`_r9_mplc`, kept in `ev.mpl`), each walked the same way with the member itself
        not counted as held: when it is dispatched as an ordinary instruction it is the placement.  `members`: only
        those (and what their walks follow), for a placement's check, whose own walks read every holdable.
        r9-guard-close 5: a stop anywhere in the scan marks the event cut (`ev.cut`): the members it had not enumerated
        (a scan's partial result is lost with it) and their placements are unknown, so its caller refuses the event."""
        r9, out, done = self._r9, set(), set()
        held = lambda x: self._r7_holdable(x) or own(x)
        cells_of, started = self._defer_cells, self._defer_started
        base, scanned, cur = (set(ev.followed) if members else set()), set(), {}
        try:
            if recv is not None and ev.rm:                                 # (a revision's own members it drops, on its frame's
                for y in recv:                                             #  earlier chains: ordinary from the rebase on)
                    ev.mpl |= self._r9_mplc(ev.rm & r9.chain.get(y, set()), recv, ev.rm, ev.gc, cur)
            while True:
                if recv is not None and len(ev.visited) > len(scanned):
                    new = ev.visited - scanned
                    scanned |= new
                    ev.mpl |= self._r9_mplc(new, recv, ev.rm, ev.gc, cur)
                todo = sorted(xq for xq in ev.followed | ev.mpl if xq not in done and xq not in out and xq not in base)
                if not todo:
                    break
                for x, q in todo:
                    done.add((x, q))
                    if x in started or (x, q) in r9.vac or self._r12_run(x, q):
                        continue                                           # (r9-guard-close 2: release work already)
                    before = set(ev.followed)
                    rel, freed = self._r8_release(q, bisect.bisect_left(cells_of.get(q, ()), x) + 1)
                    r9.charge(R9A["release"] * (2 if freed else 1))
                    if freed and not self._r8_walk([rel], held if (x, q) not in ev.mpl else (lambda z, x=x: z != x and held(z))):
                        out.add((x, q))
                        out |= ev.followed - before
        except R9Stop:
            r9.stats["cover_stopped:" + ev.kind] += 1
            ev.cut = True                                                  # (r9-guard-close 5: the scan is incomplete)
        r9.stats["covered:" + ev.kind] += len(out - base)
        r9.stats["uncovered:" + ev.kind] += len(self._r9_left(ev.followed, out) - base)
        if ev.mpl:
            r9.stats["member_plc:" + ev.kind] += len(ev.mpl)
            r9.stats["member_covered:" + ev.kind] += len(ev.mpl & out)
        return out

    def _r9_mo(self, x: int, cur: dict) -> bool:
        """r9-guard-close 4: can x, a member that has not started, later become an ordinary instruction (one a guard can
        hold) before it starts? Its frame's end returns its unadmitted members, those of the steps it can still take
        back included (from its current boundary, `_defer_current`), a rebase drops old steps' members, and a
        prepared rebase that is released returns its extra members (`_h_release_next`). Not: an orphan (authorized
        until it starts), a rebased frame's prefix member and an admitted member of a begun step (orphans at the frame's
        end), an instruction a guard never holds (a script's, a zero-duration cleanup). `cur`: the current boundaries."""
        vid = self._defer_member.get(x)
        if vid is None or x >= self.n_original or x in self._defer_started or x in self._r8_orph \
                or not (self._is_startup_instruction[x] or self._instruction_durations[x] > 0):
            return False
        fr = self._frames.get(vid)
        if fr is None or vid not in self._live_frames:
            return True
        k = fr.kof.get(x)
        if k is None:
            return not any(x in s_ for s_, _e in fr.h_prefix)
        c = cur.get(vid)
        if c is None:
            c = cur[vid] = self._defer_current(fr)
        return k >= c

    def _r9_own(self, x: int, recv, rm) -> bool:
        """r9-guard-close 4: member x matters only to its own frame's chains (`recv` is that frame alone), and only that
        frame can make it ordinary while those chains last: by a revision that drops it (checked then, `rm`), by the
        rebase it prepared, which drops it when it applies (`h_next`), or by releasing a prepared rebase's extra members
        (not its steps', so never exempt).  Its frame's end drops the chains."""
        f = self._defer_member.get(x)
        if f is None or len(recv) != 1 or f not in recv or x in rm:
            return False
        fr = self._frames.get(f)
        if fr is None or x not in fr.kof:
            return False
        nxt = getattr(fr, "h_next", None)
        return nxt is None or fr.kof[x] < nxt.j or x in nxt.tmp.kof

    def _r9_mplc(self, nodes, recv, rm, gc, cur) -> set:
        """r9-guard-close 4: the potential placements of the members among `nodes`, entries an event puts on the chains
        of the frames `recv`: a member that can later become an ordinary instruction (`_r9_mo`) -- one of a frame in
        `recv` only for the other frames there, or when the grant's revision drops it (`rm`) -- and its placements onto
        cells guarded once the grant stands (a live frame's candidate, target or pending cell, or the grant's `gc`).
        Charged: the member's frame and step (1), and its cells' guard masks (1 each)."""
        r9, si, mem, out = self._r9, self.schedule_instructions, self._defer_member, set()
        ws, tg, pd = self._defer_ws_cells, self._defer_target_cells, self._r8_pend
        for x in sorted(nodes):
            f = mem.get(x)
            if f is None:
                continue
            r9.charge(R9A["perm"])
            if self._r9_own(x, recv, rm) or not self._r9_mo(x, cur):
                continue
            pts = si[x].instruction.patches
            r9.charge(R9A["cell"] * len(pts))
            for q in pts:
                if (q in ws or q in tg or q in pd or q in gc) and self._r8_places(x, q):
                    out.add((x, q))
        return out

    def _r9_summary(self) -> dict:
        """records (froq/r9check.py `summary`), with the program's own cells and predecessors per instruction (the
        representation's C and P are the maxima over the nine programs.md) and the port's cycles."""
        si, preds, n0 = self.schedule_instructions, self._adj()[0], self.n_original
        out = self._r9.summary((self._hw.R if self._hw.slow_s is None else float(self._hw.R)) if self._hw is not None else 286)
        if self._guard != "ch" or self._live_cap is not None or self._resv is not None:
            out["guard"] = self._g_summary()                               # (G's records; with a live-frame limit; CSR at k = inf)
        if self._r9.cov:
            out["cov"] = "enforce"                                         # (plan v2 1.4: the setting, when it is)
        out["program_C"] = max((len(si[i].instruction.patches) for i in range(n0)), default=0)
        out["program_P"] = max((len(preds.get(i, ())) for i in range(n0)), default=0)
        out["script_C"] = max((len(si[i].instruction.patches) for i in range(n0, len(si))), default=0)
        out["script_P"] = max((len(preds.get(i, ())) for i in range(n0, len(si))), default=0)
        out["program_cells"] = len({tuple(p) for t in si[:n0] for p in t.instruction.patches})
        out["program_instrs"] = n0
        if self._hw is not None:
            out["port"] = {k: int(self._hw.stats.get(k, 0)) for k in ("r9_cycles", "r9_multi_round", "r9_busy_rounds", "gsvc_cycles",
                                                                     "gsvc_spill", "hold_check_spill", "guard_checks")}
        if self._wq is not None:
            pend = [(len(r), r) for _v, r in sorted(self._wq_hb.items())]   # (F3: hold changes not yet charged, in the open round)
            out["wq"] = dict(self._wq.summary(pend), D_max=int(max(self._instruction_durations, default=0)))   # (the
                                                                           #  write queue's records; D_max for its bound G)
        return out

    def _r9_size(self, x: int):
        """Instruction x's cells and predecessors, against the representation's C and P (r9check `node`)."""
        return len(self.schedule_instructions[x].instruction.patches), len(self._adj()[0].get(x, ()))

    def _r9_port2_round(self):
        """The second port's other changes (records only), written in the round after they happen: an instruction that
        completed (its entry, and each cell's occupant or release: 1 + its cells) and a live frame's verdict (1). each enters the write queue on the reservation its start or its frame's registration made."""
        si, n, wq, now = self.schedule_instructions, 0, self._wq, self.current_round
        for i in [i for i in self._r9_open if si[i].end_round != -1]:
            self._r9_open.discard(i)
            n += 1 + len(si[i].instruction.patches)
            if wq is not None:
                wq.issue(("in", i), 1 + len(si[i].instruction.patches), "D", now)
                wq.release(("in", i))                                      # (a DISCARD's unused placement marks)
        for vid in self._live_frames:
            if vid not in self._r9_vseen and self.values[vid].delivered:
                self._r9_vseen.add(vid)
                n += 1
                if wq is not None:
                    wq.issue(("fr", vid), 1, "V", now)
        if n:
            self._r9.port2(self.current_round, n)

    # --: the bounded write queue (froq/wq.py; plan v2 6.2-6.5)
    def _wq_fit(self, ev, add: int, inst, vid=None, cells=(), resv=None) -> str | None:
        """Before a grant installs (its acceptance, or an admitted placement's): its batch within the cap with `add` more
        writes; the clearing writes its installs will owe (a frame's row the first time, and per frame each new chain
        entry, certificate cell and reserved entry: at most what its admission reserved for them, b + 1); and those with
        its consequences within the long-lived room, C - H.  Else "wq" or "wq_long": the event stops with the safe
        answer and installs nothing.  `inst`: [(frame, the entries its chains get)]."""
        wq, r9, live = self._wq, self._r9, self._live_frames
        if ev.upd + add > wq.bmax:
            return "wq"
        n = 0 if vid is None else 1 + len(cells) + int(resv or 0)
        for y, ents in inst:
            ch = r9.chain.get(y, ()) if (y in live or y != vid) else ()
            n += sum(1 for x in ents if x not in ch)
        if n > wq.bmax + 1:
            return "wq"
        if not wq.long_fits(n + sum(k for _o, k, _l in self._wq_ox)):
            return "wq_long"
        return None

    def _wq_mark(self, ev, vid, cells, resv, ys):
        """A grant stands: its frame's certificate cells and reservation, and the frames whose chains it marks."""
        if vid is not None:
            self._wq_cert.setdefault(vid, set()).update(cells)
            if resv is not None:
                self._wq_resv[vid] = max(self._wq_resv.get(vid, 0), int(resv))
        self._wq_tch.update(ys)
        ev.inst = True

    def _wq_need(self, y) -> int:
        """Frame y's end: its row, its certificate bits, its chain marks and its reserved entries, each cleared once."""
        return 1 + len(self._wq_cert.get(y, ())) + len(self._r9.chain.get(y, ())) + self._wq_resv.get(y, 0)

    def _wq_settle(self, ev, key, kind, ox):
        """The event's batch (update part: the window's inserts, then its installs) enters the queue whether it stood
        or stopped. If it stood, the clearing writes of what it installed go to the frames that hold them, and its
        consequences to their owners; the rest of its reservation goes back (a refusal returns all of it)."""
        wq, now, own = self._wq, self.current_round, ("ev", key)
        clr = [(y, self._wq_need(y) - self._wq_end.get(y, 0)) for y in sorted(self._wq_tch)] if ev.inst else []
        short = ev.upd + sum(n for _y, n in clr if n > 0) + (sum(n for _o, n, _l in ox) if ev.inst else 0) - wq.has(own)
        if short > 0:                                                      # (b unbounded, so C is: reserved as made)
            assert wq.bmax == WQ_INF and wq.admit(own, short, now), ("a grant's writes past its admission", kind, short)
        wq.issue(own, ev.ins, "I", now)
        wq.issue(own, ev.upd - ev.ins, "G", now)
        wq.batch_max[kind] = max(wq.batch_max[kind], ev.upd)
        for y, n in clr:
            if n > 0:
                wq.move(own, ("fr", y), n, long=True)
                self._wq_end[y] = self._wq_end.get(y, 0) + n
        if ev.inst:
            for o, n, lg in ox:
                wq.move(own, o, n, lg)
        wq.release(own)
        self._wq_ox, self._wq_tch = (), set()
        if kind == "reg" and ev.inst and ev.vid not in self._live_frames:
            self._wq_pend[ev.vid] = now                                    # (its frame is not live yet: `_wq_sweep`)

    def _wq_drop(self, vid):
        """A frame that is not live loses what its earlier events installed (its registration did not go on, or a new one
        replaces it): those marks are cleared, and its other reservations go back."""
        wq = self._wq
        n = self._wq_end.pop(vid, 0)
        if n:
            wq.issue(("fr", vid), n, "E", self.current_round)
            wq.event("E", n, self._r9.chain.get(vid, ()))
        self._wq_cert.pop(vid, None)
        self._wq_resv.pop(vid, None)
        wq.release(("fr", vid))
        wq.release(("sc", vid))
        wq.adv_drop(vid)
        self._wq_pend.pop(vid, None)

    def _wq_sweep(self):
        """Once a round, at its start: a frame whose registration installed and reserved, which is not live, has no
        registration answer stored and was not registering last round, did not go on: what it reserved goes back
        (`_wq_drop`).  (Not r9's `R9.sweep`, which can drop a frame whose stored registration answer was collected this
        round, before `_defer_register` makes it live: its hold marks would then find nothing reserved.)"""
        if not self._wq_pend:
            return
        now, tk = self.current_round, {k[1] for k in self._r9.tickets if k[0] == "reg"}
        for v in [v for v, t in self._wq_pend.items() if t < now]:
            if v in self._live_frames:
                del self._wq_pend[v]
            elif v not in tk:
                self._wq_drop(v)

    def _wq_finish(self, vid):
        """A frame's end: its row, certificate bits, chain marks and reserved entries cleared (covers kept for other frames
        need no write: a cover lasts while its entry's chain mask is not empty), from what its grants reserved; its other
        reservations go back (its script's instructions keep theirs)."""
        wq = self._wq
        n = self._wq_need(vid)
        wq.issue(("fr", vid), n, "E", self.current_round)
        wq.event("E", n, self._r9.chain.get(vid, ()))                      # (its rows: the chain's entries)
        self._wq_end.pop(vid, None)
        self._wq_cert.pop(vid, None)
        self._wq_resv.pop(vid, None)
        self._wq_pend.pop(vid, None)
        wq.release(("fr", vid))
        wq.release(("sc", vid))
        keep = {}                                                          # (records: a collected step whose members its end
        for i, (y, k) in (getattr(self, "_r8_orph", None) or {}).items():   #  leaves authorized keeps its record, tied to
            if y == vid and k >= 0:                                        #  each of them, F6; `_r8_orphan_add` ran first)
                keep.setdefault(k, []).append(i)
        wq.adv_drop(vid, keep)

    def _wq_adv_start(self, idx: int):
        """(records only): instruction idx starts, its step's first start ends the step's record: a live frame's
        member, and (the code check's F6) every record an ended frame's end kept for a step of idx's, which that grant
        left authorized (`_wq_finish` ties them to idx in the queue). `_r8_orph` names only idx's latest ended owner,
        so the queue keeps the ties itself (F6's re-check: two successive owners that both end before idx starts)."""
        wq, now = self._wq, self.current_round
        m_ = self._defer_member.get(idx)
        if m_ is not None and m_ in self._frames:
            wq.adv_start(m_, self._frames[m_].kof.get(idx), now)
            wq.g_start(m_, self._frames[m_].kof.get(idx), now)             # (G, records: a step's first start)
        o = self._r8_orph.get(idx)
        if o is not None and o[1] >= 0:
            wq.g_start(o[0], o[1], now)                                    # (G: an ended frame's member that keeps its grant)
        wq.adv_orph_start(idx, now)

    def _wq_h(self, vid, i: int):
        """A hold mark: instruction i's held or take-back mask changes for frame vid (one write), on what the frame's
        registration, FORWARDs and revisions reserved for it. The marks live in the window's rows:
        an instruction past the window's tail has no row yet and takes its frames' marks in its insert's own write, and
        one before its head has started, so a change there writes nothing. The frame's hold changes in a round are one
        event, charged at the next round's start (`_wq_hflush`).
        SER-R: no window, so the hold bits index the instruction buffer, the W instructions
        in program order from the oldest unstarted one; a change in that range is written as above, one past it is taken
        in the instruction's own insert write, and one before it has started. The registration reserved one set and one
        clear a held instruction (`_resv_reg`, as counts them); a mark past that count (a step taken back and held
        again, a revision's new hold: v16 reserves those in its FORWARD and revision grants, which SER-R does not have) is
        admitted to the queue when it is written (counted)."""
        if self._wq is None:
            return
        r9 = self._r9
        if self._resv is not None:
            fr = self._frames[vid]
            r9.advance_head(self._defer_started, self.n_original)
            if i in fr.resv_set:                                           # (its bit is written: this change clears it)
                fr.resv_set.discard(i)
            elif i in fr.resv_unb:                                         # (held past the buffer, never written: nothing to clear)
                fr.resv_unb.discard(i)
                return
            elif r9.head <= i < r9.head + r9.W:                            # (a hold set in the buffer)
                fr.resv_set.add(i)
            else:
                fr.resv_unb.add(i)                                         # (no row, so no write until it enters: `_resv_enter`)
                self._resv_st["marks_unbuffered"] += 1
                return
            self._resv_write(fr, i)
            return
        elif not r9.head <= i <= r9.tail:
            return
        self._wq.issue(("fr", vid), 1, "H", self.current_round)
        self._wq_hb.setdefault(vid, []).append(i)

    def _resv_write(self, fr, i: int):
        """SER-R: one hold-mark write of the frame's queue entry, drawn from the writes
        the frame secured for it (`resv_mleft`: its registration's, then `_resv_secure`'s before a change that holds more),
        never from the 2 it keeps for its verdict and its end's row. A change is made only after its marks and their clears
        are secured, so none is short; one that is (a path that did not secure) is admitted when written (counted), and if the
        queue cannot take it the write is an overdraw, fatal under the strict queue, not a draw on the protected writes."""
        wq, st, now = self._wq, self._resv_st, self.current_round
        if fr.resv_mleft > 0:
            fr.resv_mleft -= 1
        else:
            st["marks_admitted"] += 1
            if not wq.admit(("fr", fr.vid), 1, now, long=True):
                st["marks_no_room"] += 1
                wq._overdraw("H", ("fr", fr.vid), 1, 0)
        st["marks"] += 1
        fr.resv_marks += 1
        wq.issue(("fr", fr.vid), 1, "H", now)
        self._wq_hb.setdefault(fr.vid, []).append(i)
        if fr.state == "carry":
            self._resv_floor(fr, "write")                                  # (every write is an event that can lower the slack)

    def _resv_floor(self, fr, where: str):
        """SER-R: the floor as a runtime assertion. A registered frame that may still take steps back by choice (its state
        carry, with rule 66's commit wait; decide and early_decide, checked at the round's start, before the take-back and its
        marks) has a slack, `resv_mleft` less the clears owed (`resv_set`), of at least twice `_resv_live`: the unstarted step
        members of its walk, at most W, a set and a clear each. Registration funds it, every voluntary take-back is gated at the
        floor and its own marks (`_resv_back_ok`) and every entry and revision at the floor and its own (`_resv_secure`), and a
        mandatory one (`_defer_advance`'s decide, at the verdict) is covered by the floor itself. A path that spends the slack
        without that fails the run here, recorded as an overdraw is (`overdraw:floor`, `overdraw_writes`), never as a silent
        wait. Incremental: the unstarted members only fall and the slack is checked where it can fall (the writes, the round's
        start), so a check whose slack is at least the need of the last one that passed (same members) passes without a count."""
        if fr.resv_t0 < 0 or self._wq is None:
            return
        st, slack, c = self._resv_st, fr.resv_mleft - len(fr.resv_set), self._resv_ok.get(fr.vid)
        st["floor_checks"] += 1
        if c is not None and c[0] is fr.members and c[2] is fr.h_next and slack >= c[1]:
            return
        st["floor_counts"] += 1
        need = 2 * self._resv_live(fr)
        if slack < need:
            wq = self._wq
            wq.stats["overdraw:floor"] += 1
            wq.stats["overdraw_writes"] += need - slack
            raise AssertionError(("SER-R: the floor broke (the slack is below twice min(W, the unstarted step members))", where, fr.vid,
                                  fr.state, "round", self.current_round, "slack", slack, "need", need, "unentered", len(fr.resv_unb)))
        self._resv_ok[fr.vid] = (fr.members, need, fr.h_next)          # (a prepared revision raises the need, so the cache keys on it too)

    def _resv_back_ok(self, fr, j: int) -> bool:
        """SER-R: a take-back its owner chooses (the decision's decision's back path, a commit's, H's choice, U's ACT; not the
        settlement at the verdict, which the floor covers) is made only after the floor and its own marks are secured: the slack
        raised to twice the floor and the members it holds again, in the buffer. False, and nothing taken back: the frame keeps
        carrying, which asks the queue for nothing (the verdict settles it on the floor). True if no step is taken back."""
        if self._resv is None or self._wq is None or j >= fr.admitted:
            return True
        if self._resv_secure(fr, self._resv_live(fr) + self._resv_inbuf(self._resv_taken(fr, j))):
            return True
        self._resv_st["takeback_refused"] += 1
        return False

    def _resv_taken(self, fr, j: int) -> set:
        """SER-R: the unstarted members of the frame's admitted steps from j, which a take-back at j holds again."""
        return {i for k in range(j, min(fr.admitted, len(fr.members))) for i in fr.members[k] if i not in self._defer_started}

    def _resv_inbuf(self, idxs) -> int:
        """SER-R: how many of the instructions lie in the instruction buffer now (the W in program order from the
        oldest unstarted one), where a hold change writes a mark."""
        r9 = self._r9
        r9.advance_head(self._defer_started, self.n_original)
        return sum(r9.head <= i < r9.head + r9.W for i in idxs)

    def _resv_live(self, fr, *more) -> int:
        """SER-R: the most a take-back can hold again: the unstarted step members of the
        walk and of `more` (a revision's rebased contract), at most W. The slack (`resv_mleft` less the clears owed) is kept at
        twice this, a set and a clear each, before any change that spends it (`_resv_enter`, `_resv_rev`). Not the ones in the
        buffer now: an admitted one past the head enters with no event, and a held one whose entry waits for room is admitted
        with no write, so the count in the buffer grows between events and the floor must not follow it."""
        if fr.h_next is not None:                                          # (a prepared revision, from
            more += (fr.h_next.tmp,)                                       #  the commit, H's choice or U's ACT to `_h_rebase`: its members are the
        s = {i for f in (fr, *more) for m in f.members for i in m if i not in self._defer_started}   # walk's from the rebase on)
        return min(self._r9.W, len(s))

    def _resv_secure(self, fr, n_new: int = 0, sc_need: int = 0) -> bool:
        """SER-R: secure the queue writes a change needs before it is made, from the
        queue's room within the long-lived share (C - H). `n_new`: the slack the change needs, in instructions: the frame's
        slack (`resv_mleft` less the clears it owes, `resv_set`) is raised to a floor of twice `n_new`, a set and a clear each,
        and a change that spends slack passes the count of its own writes on top of the floor the frame keeps (`_resv_live`: the slack is one pool, not a reserve for each instruction, so what a change
        spends must be above the floor of every take-back still to come); `sc_need`: the writes its settlement scripts
        need from now (a revision's early script and the repair after it), against what `("sc", v)` retains. False, and
        nothing reserved, if they do not fit: the change is refused or deferred. Idempotent: what is secured stays with the
        frame to its end (the same need again asks for nothing)."""
        wq = self._wq
        if wq is None:
            return True
        s_mk = max(0, len(fr.resv_set) + 2 * n_new - fr.resv_mleft) if n_new > 0 else 0
        s_sc = max(0, sc_need - wq.has(("sc", fr.vid)))
        n = s_mk + s_sc
        if n and not (wq.fits(n) and wq.long_fits(n)):
            return False
        now = self.current_round
        if s_mk:
            wq.admit(("fr", fr.vid), s_mk, now, long=True)
            fr.resv_mleft += s_mk
            self._resv_st["marks_secured"] += s_mk
        if s_sc:
            wq.admit(("sc", fr.vid), s_sc, now, long=True)
            self._resv_st["script_secured"] += s_sc
        return True

    def _resv_enter(self, fr):
        """SER-R: a held instruction past the buffer when it was held has no bit; SER-R
        makes no window insertion that would carry its mark. When the buffer's head moves and it enters, its set is written
        (and its clear secured with it), one write from the frame's queue entry; if the queue cannot take it yet it waits
        (the instruction is held by the frame either way, so it starts nowhere meanwhile). (the second re-check, finding
        1): the entry's own set and clear come out of the slack, which must stay at twice the most a take-back can hold again
        (`_resv_live`: the unstarted step members, at most W), so each entry asks for the
        live count plus its own."""
        r9 = self._r9
        r9.advance_head(self._defer_started, self.n_original)
        if not fr.resv_unb or fr.resv_scan == r9.head:
            return
        lo, hi, stalled = r9.head, r9.head + r9.W, False
        live = self._resv_live(fr)
        for i in sorted(x for x in fr.resv_unb if lo <= x < hi):
            if not self._resv_secure(fr, live + 1):                        # (the floor of every take-back, and this entry's set and clear)
                stalled = True
                self._resv_st["marks_enter_waits"] += 1
                break
            fr.resv_unb.discard(i)
            fr.resv_set.add(i)
            self._resv_st["marks_entered"] += 1
            self._resv_write(fr, i)
        fr.resv_scan = -1 if stalled else r9.head

    def _wq_hflush(self):
        """account 3.2: each frame's hold changes, one event (its rows: the instructions), in the round's records
        and port 1's cycles. F2: those made since the last check-port read or admission are charged before the next one
        (`_r9_gc_turn`, `_r9_event`, `_wq_start`), so no reader sees holds whose event has not been served, and their
        cycles past the round's room are owed (`r9_debt`) before any arbitration; a round's last ones before the next
        round's `round_start`."""
        for vid in sorted(self._wq_hb):
            rows = self._wq_hb[vid]
            self._wq.event("H", len(rows), rows)
        self._wq_hb = {}

    def _wq_fwd(self, fr, a: int, b: int) -> list:
        """A counted FORWARD's consequences, from the frame's steps a to b: each unstarted member's start and completion
        (2 (1 + k), less what it holds already: a member taken back and admitted again keeps its first reservation), and
        the block's hold marks if it is taken back and admitted again (2 a member)."""
        si, wq, out, nb = self.schedule_instructions, self._wq, [], 0
        for k in range(a, b):
            for i in fr.members[k]:
                nb += 1
                if i not in self._defer_started:
                    need = 2 * (1 + len(si[i].instruction.patches)) - wq.has(("in", i))
                    if need > 0:
                        out.append((("in", i), need, True))
        out.append((("fr", fr.vid), 2 * nb, True))
        return out

    def _wq_hadd(self, i: int, vid):
        """Frame vid holds instruction i (`_defer_hold_of`); one hold-mark write when the hold is new."""
        s = self._defer_hold_of.setdefault(i, set())
        if self._wq is not None and vid not in s:
            self._wq_h(vid, i)
        s.add(vid)

    def _wq_script(self, fr, js) -> int:
        """The writes a frame's settlement script can make, over the boundaries it may reach (as `_r9_script_len`): per
        instruction its window entry (1 + 2k), its start (1 + k), its completion (1 + k) and a DISCARD's marks (k)."""
        n = 0
        for j in js:
            low = self._defer_boundary(fr, j)[0]
            if low is not None and low.instrs:
                n = max(n, sum(3 + 4 * len(x.patches) + (len(x.patches) if x.name == "DISCARD" else 0) for x in low.instrs))
        return n

    def _wq_splice(self, vid, new):
        """A settlement script is spliced: each instruction's window entry is written (1 + 2k), and its start, completion
        and a DISCARD's placement marks are reserved for it, from what its frame's admissions reserved for scripts."""
        wq, now = self._wq, self.current_round
        for x in new:
            k = len(x.patches)
            wq.issue(("sc", vid), 1 + 2 * k, "I", now)
            wq.move(("sc", vid), ("in", x.idx), 2 * (1 + k) + (k if x.name == "DISCARD" else 0), long=True)
        wq.event("splice", sum(1 + 2 * len(x.patches) for x in new), [x.idx for x in new])   # (one event: its rows, the new entries)

    def _g_note(self, kind: str, key=None, fr=None, piu=None):
        """OFF: a failure of `kind`, counted once per key, with the round of the first. G (records only): the first
        of each kind also keeps who failed (`_g_who`); `fr` is the failing frame, `piu` the cells in use there."""
        if key is not None:
            if (kind, key) in self._g_seen:
                return
            self._g_seen.add((kind, key))
        f = self._g_fail.setdefault(kind, [0, self.current_round])
        f[0] += 1
        if f[0] == 1:
            self._g_first[kind] = self._g_who(kind, key, fr, piu)

    def _g_who(self, kind, key, fr, piu) -> dict:
        """G's necessity example (plan v2 A1.3 item 5; F1.md 9.6), records only: a kind's first failure -- its round, every
        live frame (`live`, `_g_frame`; the frame a refused release waits on is one of them), and, but for a stall, the
        failing frame (`frame`): for script_ws and release_refused the frame whose script waits for its workspace; for
        queue_refused the refused instruction (`instr`, `_g_instr`) and its owner's frame."""
        busy = self._active_cells_now() if piu is None else piu
        out = dict(round=self.current_round)
        if kind == "queue_refused":
            out["instr"] = self._g_instr(key)
            fr = self._frames.get(out["instr"]["vid"])
        if kind != "stall":
            out["frame"] = None if fr is None else self._g_frame(fr, busy)
        out["live"] = [self._g_frame(self._frames[v], busy) for v in sorted(self._live_frames)]
        return out

    def _g_instr(self, idx) -> dict:
        """G (records only): instruction idx -- its index, name and patches -- and its owner (`owner`, `vid`): a script's
        frame (`_recovery_owner`), a member's (`_defer_member`), an ended frame's orphan (`_r8_orph`), or none."""
        ins = self.schedule_instructions[idx].instruction
        if idx >= self.n_original:
            how, vid = "script", self._recovery_owner.get(idx)
        elif idx in self._defer_member:
            how, vid = "member", self._defer_member[idx]
        elif idx in self._r8_orph:
            how, vid = "orphan", self._r8_orph[idx][0]
        else:
            how, vid = "none", None
        return dict(idx=int(idx), name=ins.name, patches=[list(map(int, p)) for p in ins.patches], owner=how,
                    vid=None if vid is None else int(vid))

    def _g_frame(self, fr, busy) -> dict:
        """One frame as G reads it (records only): vid, boundary target, p_verdict, state, taint_wires, and whether it
        carries a rebased walk (H's early splice; target and p_verdict are then the rebased walk's); its workspace
        (`_defer_workspace`) at its target, or, with no target yet or back in `decide` (`_defer_retarget` and `_r8_settle`
        leave the old target), at the boundary it stands at (`_defer_current`, `boundary`), each cell as [cell, its
        occupant, its next instruction, a patch on it or a running instruction using it] (`_g_occupant`).  An ended frame
        keeps no scripts and no taint wires: its workspace and wires are None."""
        done = fr.state == "done"
        d = dict(vid=int(fr.vid), target=int(fr.target), p_verdict=int(fr.p_verdict), state=fr.state, rebased=bool(fr.rebased),
                 taint_wires=None if done else sorted(fr.taint_wires, key=str), boundary=None, ws=None)
        if not done:
            j = fr.target if fr.target >= 0 and fr.state != "decide" else self._defer_current(fr)
            d.update(boundary=int(j), ws=[[list(map(int, c_)), *self._g_occupant(c_), c_ in self._active_patches or c_ in busy]
                                          for c_ in sorted(self._defer_workspace(fr, j))])
        return d

    def _g_occupant(self, cell) -> tuple:
        """`_defer_last_started` and `_defer_first_unstarted` without the pointer's move: the original instruction that
        started last on the cell and the next one, the first not started (a refused release waits on it).  The pointer is
        read by rule (a)'s `_r8_release`, so a record must leave it where the run put it."""
        lst = self._defer_cells.get(cell, ())
        p = self._defer_cell_ptr.get(cell, 0)
        while p < len(lst) and lst[p] in self._defer_started:
            p += 1
        return (int(lst[p - 1]) if p else None), (int(lst[p]) if p < len(lst) else None)

    def _g_over(self, now, who=None):
        """G, OFF (records only): the queue's first overflow (`queue_overflow`, O + R past C, `WQ.on_over`): its round,
        every live frame, and the write that passed C (`who`, `WQ.issue`'s): `owner` [kind, id] and `write` its kind,
        and the owner's frame (`frame`) -- a frame's own writes or its script's (`fr`, `sc`), or an instruction's (`in`,
        with `instr` as a refused one has).  A frame's end writes come after it left the live set: `frame` names it."""
        busy = self._active_cells_now()
        (o, x), w = who or ((None, None), None)
        out = dict(round=now, owner=None if o is None else [o, int(x) if o in ("in", "fr", "sc") else str(x)], write=w)
        vid = x if o in ("fr", "sc") else None
        if o == "in":
            out["instr"] = self._g_instr(x)
            vid = out["instr"]["vid"]
        fr = self._frames.get(vid)
        out.update(frame=None if fr is None else self._g_frame(fr, busy),
                   live=[self._g_frame(self._frames[v], busy) for v in sorted(self._live_frames)])
        self._g_first["queue_overflow"] = out

    def _off_ws(self, fr, patches_in_use):
        """OFF: a settlement script waits for its workspace. `script_ws` when a cell holds the patch of a start a
        decided workspace's guard would have held (`_off_tg`); `release_refused` when a cell's patch waits to leave on work
        a live frame holds (`_defer_ws_fenced`, the state rule (a) and the workspace guard exclude). Once per frame."""
        ws = self._defer_workspace(fr, fr.target)
        if any(self._defer_last_started(c) in self._off_tg for c in ws if c in self._active_patches or c in patches_in_use):
            self._g_note("script_ws", fr.vid, fr, patches_in_use)
        if self._defer_ws_fenced(fr, fr.target, patches_in_use):
            self._g_note("release_refused", fr.vid, fr, patches_in_use)

    def _g_summary(self) -> dict:
        """G's records (plan v2 A1.3): the setting; under OFF the failures by kind (count, first round) and the status
        (none, or the first failure's kind and round); under SER, or a live-frame limit k, its progress check (k live
        frames at most, SER's k = 1, every frame ended) and the values it sent to N's path."""
        out = dict(setting=self._guard)
        if self._guard == "off":
            f = {k: list(v) for k, v in self._g_fail.items()}
            wq = self._wq
            if wq is not None and wq.first_over is not None:
                f["queue_overflow"] = [1, wq.first_over]                   # (owed writes with no room: O + R past C)
            first = min(((v[1], k) for k, v in f.items()), default=None)
            out.update(failures={k: dict(count=v[0], first_round=v[1]) for k, v in sorted(f.items())},
                       status="none" if first is None else first[1], first_round=None if first is None else first[0],
                       tg_passed=len(self._off_tg), overdraw_writes=0 if wq is None else int(wq.stats.get("overdraw_writes", 0)),
                       first=dict(sorted(self._g_first.items())))   # (G, records only: each kind's first failure, `_g_who`)
        if self._live_cap is not None or self._resv is not None:          # (CSR at k = inf: the hardware's live-value limit)
            cap = self._live_cap if self._live_cap is not None else (self._hw.cap_values if self._hw is not None else None)
            out.update(refused_values=self._ser_full, peak_live=self._defer_peak_live, live_at_end=len(self._live_frames),
                       progress_ok=(cap is None or self._defer_peak_live <= cap) and not self._live_frames)
        if self._resv is not None:
            out["resv"] = self._resv_summary()                             # (SER-R's records)
        return out

    def _wq_start(self, idx: int) -> str | None:
        """The dispatcher's start of idx, after every other hold.  One whose start and completion an owner reserved (an
        admitted member or an orphan: its group's FORWARD; a script instruction: its frame) starts on it and never waits.
        Any other start is admitted here, its start and completion reserved, if they fit and no aged request waits for
        capacity; else it is held (`wq_hold`, `wq_aged`, `wq_e`) and its age runs under K-aging, as a port request's.
        F1: the request is the instruction's, `("gc", idx)`, the one its guard check and its placement's check make (r9's
        identity for both).  A check served set its age aside (`R9.suspend`; kept aside while its own check is in
        service, `R9.carry`) and a refusal here takes it back (`R9.resume`), so its age runs from its first refusal, at
        either port, until it is admitted; an aged one keeps its turn at its own checks meanwhile.  The queue's record of
        its wait runs as long (`WQ.wait`, `_wq_keep`, `WQ.served`).  A queue that holds nothing (`WQ.holds`) sets nothing
        aside."""
        if self._wq_hb:
            self._wq_hflush()                                              # (F2: pending hold events before an admission)
        wq, r9, now = self._wq, self._r9, self.current_round
        ins = self.schedule_instructions[idx].instruction
        k, have = len(ins.patches), wq.has(("in", idx))
        full = 2 * (1 + k) + (k if idx >= self.n_original and ins.name == "DISCARD" else 0)   # (a script DISCARD's marks)
        key = ("gc", idx)
        if have >= full:
            wq.served(key, now)                                            # (an owner reserved it: a wait the queue recorded
            if idx in self._wq_req:                                        #  ends, and a request it refused)
                self._wq_req.discard(idx)
                r9.served(key)
            return None
        need = full - have
        if idx >= self.n_original or idx in self._defer_member or idx in self._r8_orph:
            wq.stats["unreserved_release"] += 1                            # (records: release work no owner reserved)
        back = r9.resume(key, now)                                         # (F1: its checks' age, set aside or carried)
        aw = wq.aged_wait
        if aw is not None and aw not in r9.aged:
            wq.aged_wait = aw = None
        if aw is not None and aw != key:
            why = "wq_aged"                                                # (an aged request waits for capacity: it goes first)
        elif wq.C != WQ_INF and key in r9.aged and not r9.aged_head(key):
            why = "wq_aged"                                                # (aged itself: in the order the aged were aged; C
        elif not wq.fits(need):                                            #  unbounded holds nothing, as r9 would not)
            why = "wq_hold"
            if r9.aged_head(key):
                wq.aged_wait = key
        elif not wq.e_take(1 + k):
            why = "wq_e"                                                   # (the round's enqueue room is spent)
        else:
            wq.admit(("in", idx), need, now)
            wq.served(key, now)
            if back or idx in self._wq_req:                                # (the request ends: its age, if the queue held it or
                self._wq_req.discard(idx)                                  #  its checks set it aside; an age r9's port alone
                r9.served(key)                                             #  made, its checks skipped, stays as in r9)
            return None
        if self._guard == "off" and (idx >= self.n_original or idx in self._defer_member or idx in self._r8_orph):
            self._g_note("queue_refused", idx)                             # (OFF: release work nothing reserved, held)
        wq.wait("start", key, now)
        r9.refused(key, now)
        self._wq_req.add(idx)
        return why

    def _wq_keep(self, key):
        """The re-check's MED: a request the queue held goes on through its own checks, its age kept (`R9.alive`: refused
        at a port, set aside or carried): the queue's record of its wait goes on with it (`WQ.keep`), so it neither leaves
        (`WQ.prune`) nor counts a refusal.  A queue that holds nothing has no such record."""
        wq, now = self._wq, self.current_round
        if wq is not None and wq.holds and self._r9.alive(key, now):
            wq.keep(key, now)

    def _r9_gc_turn(self, idx: int, req=None) -> bool:
        """r9-guard-close 3: the dispatcher's guard check of `idx` takes its turn at the port: at once if it was served this
        round or its own check's answer is stored (it takes it); otherwise not while another answer is untaken.
        r9-guard-close 4: nor while an aged request other than its own is pending (K-aging).  r9-guard-close 5: a stored
        answer is its own only for the request it answered (`req()`, the request now, as `_r9_dispatch` makes it): a
        stale one is dropped here, and the fresh check takes its turn as any request does, before its walk or the port's
        cycles -- served this round or not (the exemption is for collecting a valid answer only)."""
        if getattr(self, "_wq", None) is not None and self._wq_hb:
            self._wq_hflush()                                              # (F2: pending hold events first, before the port)
        r9, now, key = self._r9, self.current_round, ("disp", idx)
        t = r9.tickets.get(key)
        if t is not None and req is not None and not r9.covers(t[2], req()):
            del r9.tickets[key]
            r9.stats["tickets_stale:" + ("rel" if r9.on_chain(idx, self._live_frames) else "plc")] += 1
            r9.stats["tickets_stale_gc"] += 1                              # (records: dropped at the guard check)
        elif t is not None or self._hw.checked.get(idx) == self._hw.now:
            return True
        why = "kept" if r9.unapplied(now) else ("aged" if not r9.turn(("gc", idx), now) else None)
        if why is not None:
            r9.stats["%s_waits:gc" % why] += 1
            return False
        return True

    def _r9_gkind(self, idx: int, gc) -> str:
        """(Records only, r9-guard-close) a guard check's kind for the queued-waiting records: "gc_rel" when every
        candidate cell it checks is covered for idx (release work, admitted without a walk once the check is served),
        else "gc"."""
        r9, live = self._r9, self._live_frames
        cs = [p for p in gc if p in self._defer_ws_cells or p in self._r8_pend]
        return "gc_rel" if cs and all(r9.covered(idx, p, live) or self._r12_run(idx, p) for p in cs) else "gc"

    def _r9_req(self, idx: int, pts, exempt: bool, pend) -> tuple:
        """r9-guard-close 5: the request `_r9_dispatch` would check now for `idx` (its `sig`): the exemption and the
        guarded cells that are not covered."""
        r9, live = self._r9, self._live_frames
        gp = [p for p in pts if (p in self._defer_ws_cells or (pend and p in self._r8_pend)) and p not in self._active_patches]
        return ((exempt,), (frozenset(p for p in gp if not (r9.covered(idx, p, live) or self._r12_run(idx, p))),))

    def _r9_holders(self, cells) -> list:
        """The live frames whose certificate's workspace (at its witness) holds one of the cells (the cell table's mask)."""
        out = []
        for vid in sorted(self._live_frames):
            fr = self._frames[vid]
            w = getattr(fr, "r8_wit", None)
            if w is not None and w >= 0 and any(c_ in self._defer_workspace(fr, w) for c_ in cells):
                out.append(vid)
        return out

    def _r9_dispatch(self, idx: int, gp, exempt: bool):
        """The candidate guard on an ordinary instruction that puts a patch on guarded cells `gp` (`_defer_hold`'s r8
        part).  A placement a live certificate covers is release work, admitted without a walk.  The others are one
        event: `rel` for an instruction on a live certificate's chains (B_rel, so no budget hit refuses it), `plc`
        otherwise (B; the safe answer, a hold, at a stop).  An admitted placement onto a certified cell adds the walk's
        chains to the certificates holding the cell, covered (its walks read every holdable).  r9-guard-close 4: and the
        potential placements of the members on them that can later become ordinary instructions are covered too, or the
        placement is held (`member:<kind>`); a stored answer applies only to the cells it checked (`sig`)."""
        r9, live, st = self._r9, self._live_frames, self._defer_stats
        todo = [p for p in gp if not (r9.covered(idx, p, live) or self._r12_run(idx, p))]   # (a run's mark too)
        if r9.vac or r9.vrun:
            r9.stats["vac_admitted_cells"] += sum((idx, p) in r9.vac or self._r12_run(idx, p) for p in gp)   # (r9-guard-close 2,
                                                                           #  records only)
        if len(todo) < len(gp):
            r9.stats["rel_admitted_cells"] += len(gp) - len(todo)
        if not todo:
            if r9.tickets.pop(("disp", idx), None) is not None:            # (r9-guard-close 4: its own check's stored answer,
                r9.stats["tickets_taken_covered"] += 1                     #  whose walk covered it, is taken: it keeps no port)
            r9.stats["rel_admitted"] += 1                                  # (release work: admitted without a walk)
            return None
        kind = "rel" if r9.on_chain(idx, live) else "plc"
        if kind == "rel" and self._cov_assert:                             # (plan v2 1.4: the coverage assertion. An
            r9.stats["cov_violation"] += 1                                 #  uncovered placement on a live chain fails the run
            raise CoverageError(("an uncovered guarded placement on a live chain (a rel event)", idx, sorted(todo, key=repr),
                                 sorted(y for y in live if idx in r9.chain.get(y, ())), self.current_round))   #  as an overdraw does)

        def fn():
            for p in todo:
                r9.charge(R9A["cell"] + R9A["next_user"])
                u = self._defer_next_on_cell(idx, p)
                hold = p in self._defer_ws_cells and u is not None and (self._defer_pinned(u) or (self._r7_guard and self._r7_keeps_off(u)))
                by8 = False
                if self._r8 and not hold and self._r8_hold(idx, p):
                    st["r7:rule_kept_off"] += 1
                    hold = by8 = True
                if hold:
                    if exempt:
                        st["r7:rule_exempt_held"] += 1
                        by8 = True
                    return "defer_rule_a" if by8 else "defer_guard"
            return None

        def acc(ev, ans):
            if ans is None:
                hs = self._r9_holders(todo)
                if hs:
                    cov = self._r9_cover(ev, lambda z: False, recv=set(hs), members=True)
                    if ev.cut or any(x not in self._defer_started for (x, _q) in self._r9_left(ev.mpl, cov)):
                        raise R9Stop("member")                             # (r9-guard-close 4; 5: a scan cut short)
                    e1, nw = self._r12_e1(ev, lambda z: False, set(hs), set(ev.followed) | cov) if r9.cov else ((), 0)   # (with
                    if self._wq is not None:                               #  cov=enforce, E1; a stop holds it. Its batch
                        why = self._wq_fit(ev, len(ev.visited) + nw, [(y, ev.visited) for y in hs])   #  and the holders'
                        if why is not None:                                #  clearing writes fit, or it is held)
                            raise R9Stop(why)
                        self._wq_mark(ev, None, (), None, hs)
                    ev.upd += len(ev.visited) + nw
                    for y in hs:
                        r9.install(y, ev.visited, set(ev.followed) | cov | set(e1))
        sig = lambda: ((exempt,), (frozenset(todo),))                      # (r9-guard-close 4: the cells it checked)
        ans = self._r9_event(kind, ("disp", idx), fn, "defer_rule_a", accept=acc, sig=sig)
        if ans is R9_WAIT:
            if self._wq is not None and self._wq.holds and ("disp", idx) in r9.tickets:
                r9.carry(("gc", idx), self.current_round)                  # (F1: its own check in service: its age stays aside,
                self._wq_keep(("gc", idx))                                 #  and its queue wait goes on)
            return "hw_guard_check"                                        # (the port has not answered: as rule 66's spill)
        if ans is not None:
            r9.stats["holds:" + kind] += 1
            self._defer_guarded.add(idx)
        return ans

    def _defer_complete(self, i: int) -> bool:
        """Complete, or a zero-duration member whose positive-duration predecessors are complete (stamped in this round's
        dispatch, as `_step_complete` treats a cleanup DISCARD)."""
        if self.schedule_instructions[i].end_round != -1 or (self._instruction_durations[i] == 0 and i in self._defer_started):
            return True                                                    # (a zero-duration one completed in round 0 keeps end -1)
        return self._instruction_durations[i] == 0 and not any(self.schedule_instructions[p].end_round == -1 and self._instruction_durations[p] != 0
                                                                for p in self._adj()[0].get(i, ()))

    def _defer_advance(self, fr: DeferFrame, patches_in_use):
        v = self.values[fr.vid]
        if self._resv is not None and self._wq is not None:
            if fr.resv_unb:
                self._resv_enter(fr)                                       # (SER-R, item 7: held instructions entering the buffer)
            if fr.state in RESV_FLOOR_STATES:
                self._resv_floor(fr, "advance")                            # (the floor, at the round's start)
        if fr.rebased and fr.h_early_done < 0:
            self._h_early_progress(fr)                                     # H: the early script's completion and frame
        if self._u_on and v.delivered:
            self._u_resolve(v)                                             # U: RESOLVE when the scheduler first sees the verdict
        if fr.state == "carry" and self._w2 is not None and not v.delivered and fr.hw_wait is None:
            self._w2_round(fr, patches_in_use)                             # the decision: the round's decision (section 1.3)
        elif fr.state == "carry" and self._w2 is not None and self._w2.r1 and not v.delivered and fr.hw_wait == "commit":
            self._w2_r1_pending(fr, patches_in_use)                        # R1: the round's lookup while the check waits
        if fr.state == "carry":
            self._defer_admit(fr)
            if not v.delivered:
                if fr.admitted < fr.limit:
                    fr.admit_waits += 1
                if self._u_on:
                    self._u_round(fr, patches_in_use)                      # U: the ACTs due now, REGISTER, the epoch requests
                elif self._w2 is None and self._h is not None and not fr.rebased and self._h_trigger(v, fr) and self._h_lead_ok(v, fr):
                    self._h_start(fr)                                      # H: a confident m = 1 prediction
                    self._h_advance(fr, patches_in_use)
                return
            fr.verdict_round, fr.p_verdict = self.current_round, self._defer_current(fr)
            if fr.w2 is not None:
                self._r8_note_decision(fr); self._w2.verdict(fr)                                       # (records only: a wrong bet that resolves)
            if fr.rebased:                                                 # H, after the early script: b = 1 confirms it,
                if v.truth:                                                # b = 0 repairs it (D's decide on the rebased contract)
                    self._h_confirm(fr); return
            elif not v.truth:                                              # b = 0: no S was due, the frame is void
                self._defer_finish(fr, "dropped"); return
            fr.state = "decide"
        if fr.state in H_STATES:
            if not self._h_advance(fr, patches_in_use):
                return                                                     # (True: b = 1 before the splice, D's decide now)
        if fr.state == "decide":
            p = self._defer_current(fr)                                    # admitted steps not yet begun can still be held back
            reach = self._defer_reachable(fr)                              # ... and the steps admissible now can be carried
            if self._r8 and fr.r8_cap is not None:
                reach = min(reach, fr.r8_cap)                              # (round 8's rule (a3): its certified boundaries only)
            ok = [j for j in range(p, reach + 1) if fr.feas[j] and not self._defer_ws_blocked(fr, j, patches_in_use, splice=False)]
            if self._resv_inc:                                             # (plan 7.2: it settles only where it holds the
                ok = self._resv_inc_settle(fr, ok)                         #  footprint, never waiting on a cell)
            if self._r8 and fr.r8_wit not in ok:
                self._defer_stats["r7:rule_wit_blocked_rounds"] += 1       # (the witness unusable, an invariant failure; 0)
            if self.defer_cfg["rule"] == "eager":
                ok = ok[:1]                                                # the first boundary it can be materialized at
            if not ok:
                fr.decide_waits += 1; self._defer_stats["decide_wait_rounds"] += 1; return
            j = min(ok, key=lambda j_: (fr.r[j_], j_))                     # cheapest placed script; ties: the earliest boundary
            if self._resv is not None and self._wq is not None:            # (a take-back holds its steps
                while j < fr.admitted and not self._resv_secure(fr, self._resv_inbuf(self._resv_taken(fr, j))):   # anew; their
                    ok.remove(j)                                           #  marks and clears are secured first, else the
                    self._resv_st["decide_room_waits"] += 1                #  boundary is not taken this round)
                    if not ok:
                        break
                    j = min(ok, key=lambda j_: (fr.r[j_], j_))
                if not ok:
                    fr.decide_waits += 1; self._defer_stats["decide_wait_rounds"] += 1; return
            if j < fr.admitted:
                self._defer_unadmit(fr, j)
            fr.p_run, fr.first_ok, fr.target, fr.limit = p, ok[0], j, j
            self._defer_admit(fr)
            ws_ = self._defer_workspace(fr, j)
            if self._hw is not None:                                       # rule 66: the decide event claims the workspace
                fr.hw_wait = "decide"
                self._hw.submit("decide", lambda fr=fr, ws_=ws_: self._hw_target_on(fr, ws_), cells=dict(workspace=ws_), owner=fr.vid)
            else:
                fr.target_ws = ws_
                for c_ in fr.target_ws:
                    self._defer_target_cells[c_] = self._defer_target_cells.get(c_, 0) + 1
            fr.state = "materialize"
        if fr.state == "materialize":
            if fr.admitted < fr.target:
                self._defer_admit(fr)
                if fr.admitted < fr.target:
                    if self._r8 and fr.r8_ref == self.current_round and fr.target > max(fr.admitted, fr.r8_b1):
                        self._r8_settle(fr); return                        # (round 8's rule (a3): no wait on the guard)
                    fr.admit_waits += 1; return
            if not all(self._defer_complete(i) for k in range(fr.target) for i in fr.members[k]):
                return
            if fr.rebased and fr.h_early_done < 0:                         # H: a repair follows the early script's completion
                self._h_stats["repair_wait_rounds"] += 1; return
            if fr.hw_wait == "decide":
                return                                                     # rule 66: the decide event has not completed
            if self._defer_ws_blocked(fr, fr.target, patches_in_use, splice=True):
                fr.ws_waits += 1; self._defer_stats["workspace_wait_rounds"] += 1
                if self._guard == "off":
                    self._off_ws(fr, patches_in_use)                       # (OFF: a failure's kind, if any)
                if self._resv is not None:
                    self._resv_ws(fr, patches_in_use)                      # (SER-R, records: what occupies its workspace)
                if self._r8 and fr.target == fr.r8_wit:                    # (records only, and read by rule 29's knot: the
                    fr.r8_mat_waits += 1                                   #  settlement waits at its witness for a patch to leave)
                    self._defer_stats["r7:rule_rec_wit_mat_rounds"] += 1
                if self._defer_ws_blocked(fr, fr.target, patches_in_use, splice=False):
                    self._defer_retarget(fr)                               # pinned by a patch that waits for the frame: choose again
                elif self._defer_ws_fenced(fr, fr.target, patches_in_use):
                    self._defer_stats["workspace_wait_fenced_rounds"] += 1   # records only: it waits on held work
                return
            if self._hw is not None and not self._hw_splice_ready(fr, rebase=False):
                return                                                     # rule 66: the splice event has not completed
            for c_ in fr.target_ws:
                n = self._defer_target_cells.get(c_, 0) - 1
                if n > 0:
                    self._defer_target_cells[c_] = n
                else:
                    self._defer_target_cells.pop(c_, None)
            fr.target_ws = frozenset()
            if fr.w2 is not None and self._w2.records:                     # the decision (records only): the repair's decoding event
                self._w2_repair_event(fr)
            self._defer_splice(fr)
            if self._u_on and fr.rebased and v.recovery.get("kind") == "script":
                self._u_repair_instrs.update(v.recovery["instrs"])          # U: a repair after an early script (traffic tag)
            return
        if fr.state == "spliced":
            r = v.recovery
            if fr.locked and all(self.schedule_instructions[i].end_round != -1 for i in r["instrs"]):
                self._defer_unlock(fr)                                     # the script is done: its cells are free again
            how = "early_repaired" if fr.rebased else "materialized"       # (H: the repair of an early script)
            if r["kind"] == "frame":
                if r["round"] < self.current_round:                        # RECOVERY_DONE a round later at the earliest, as in Q
                    self._defer_finish(fr, how)
            elif all(self.schedule_instructions[i].end_round != -1 for i in r["instrs"]) and (
                    self.defer_cfg["settle"] == "none" or all(self._delivered_instr(i) for i in r["instrs"])):
                self._defer_finish(fr, how)

    def _defer_retarget(self, fr: DeferFrame):
        assert self._resv is None, ("SER-R: a retarget re-opens decide after a take-back", fr.vid)
        if self._hw is not None:
            self._hw_reset(fr)                                             # rule 66: a queued splice event of the old target goes
        for c_ in fr.target_ws:
            n = self._defer_target_cells.get(c_, 0) - 1
            if n > 0:
                self._defer_target_cells[c_] = n
            else:
                self._defer_target_cells.pop(c_, None)
        fr.target_ws = frozenset()
        if self._r8 and fr.target == fr.r8_wit:
            self._defer_stats["r7:rule_wit_retargets"] += 1                # (the witness retargeted, an invariant failure; 0)
        fr.feas[fr.target] = False
        fr.limit, fr.state = fr.top, "decide"
        self._defer_stats["redecisions"] += 1

    # ------------------------------------------------------------------ the decision (froq/wager2.py)
    def _w2_round(self, fr: DeferFrame, patches_in_use):
        """A carrying frame's round under the decision: a commit whose reservation-manager
        check completed acts now, revalidated at this round's state; otherwise the round's decision."""
        w2 = self._w2
        if fr.hw_ok == "commit":
            fr.hw_ok = None
            if w2.r1:
                if self._w2_r1_final(fr, patches_in_use):
                    return
            else:
                k = fr.w2.pop("pending", None)
                if k is not None and k == fr.limit and w2._commit_ok(fr, k, patches_in_use):
                    fr.w2["round"] = self.current_round
                    self._w2_act(fr, k, patches_in_use)
                    return
            w2.stats["commit:dropped_after_check"] += 1
        if fr.w2["round"] == self.current_round:
            return                                                         # (decided this round already: at its registration)
        kind, k = w2.decide(fr, patches_in_use)
        if w2.records:
            w2.blocked_tick(fr)                                            # (spec 14: J_v's blocked rounds)
        if kind == "commit":
            self._w2_commit(fr, k, patches_in_use)

    def _w2_commit(self, fr: DeferFrame, k: int, patches_in_use):
        """COMMIT at the decision boundary k: through rule 66's commit check and reserve when the mode is on (the frame
        holds at k until it completes), then the act."""
        if self._hw is not None:
            if k < fr.admitted:
                if not self._resv_back_ok(fr, k):
                    return                                                 # (SER-R: no floor for the take-back; the frame keeps carrying)
                self._defer_unadmit(fr, k)
            fr.limit = k
            fr.w2["pending"] = k
            fr.hw_wait = "commit"
            low = self._defer_boundary(fr, k)[0]
            self._hw.submit("commit_check_and_reserve", lambda fr=fr: self._hw_done(fr, "commit"),
                            cells=dict(script_cells=(low.cells if low is not None else ()), own_cells=(),
                                       workspace=self._defer_workspace(fr, k)), owner=fr.vid)
            if fr.hw_wait is not None:
                return
            fr.hw_ok = None
            fr.w2.pop("pending", None)
        self._w2_act(fr, k, patches_in_use)

    def _w2_act(self, fr: DeferFrame, k: int, patches_in_use):
        """The commit's bookkeeping, as the decision's ACT (`_u_act`): the steps from k taken back, the target and its
        workspace, the prepared rebased contract's holds, claims and members, the decoder-core reservation; then H's
        early materialization."""
        w2 = self._w2
        if k < fr.admitted and not self._resv_back_ok(fr, k):
            return                                                         # (SER-R: no floor for the take-back; the frame keeps carrying)
        r1 = (dict(a_hat=fr.w2.get("a_hat"), f5=fr.w2.get("f5")) if w2.r1 else None)   # (R1: the booking's bound, 16.1)
        nxt, demand, end, start = w2.commit_args(fr, k)
        now = self.current_round
        if k < fr.admitted:
            self._defer_unadmit(fr, k)
        fr.target = fr.limit = fr.h_je = k
        fr.h_trigger_round = fr.h_choice_round = now
        fr.target_ws = self._defer_workspace(fr, k)
        for c_ in fr.target_ws:
            self._defer_target_cells[c_] = self._defer_target_cells.get(c_, 0) + 1
        for i in nxt.new_holds:
            self._wq_hadd(i, fr.vid)
        for e in nxt.extra_claims:
            self._defer_claim[e] = fr.vid
        for i in nxt.extra_members:
            self._defer_member[i] = fr.vid
        fr.h_next = nxt
        if self._r8:
            self._r8_pend_reb(fr, nxt)                                     # (rule (a2): its candidate guard holds from the commit)
        rid = w2.reserve(fr.vid, demand, now, end, start)
        if self._hw is not None:
            self._hw.take_slot(("w2", rid))                                # (checked free by `_commit_ok`)
        fetch = self._hw is not None and k > self._hw.b_res
        rec = w2.commit_rec(fr, k, w2.cstate(self.values[fr.vid], fr), fetch)
        ev_ = ("commit", now, int(demand), w2.forecast())
        fr.w2["commit"] = dict(b=k, round=now, stop=fr.stop, rid=rid, ev=ev_)
        fr.w2["commit_rec"] = rec
        fr.w2["events"].append(ev_)
        fr.state = "early_materialize"
        w2.stats["commits"] += 1
        if r1 is not None:                                                 # R1: a booking (16.1), and its records
            a_hat = r1["a_hat"] if r1["a_hat"] is not None else now
            w2.stats["r1:bookings"] += 1
            w2.stats["r1:bookings_f5_ready"] += int(bool(r1["f5"]))
            w2.stats["r1:predicted_excess_rounds"] += max(0, a_hat - now - W2_ELL)
            w2.stats["r1:rebookings"] += int(bool(fr.w2.pop("r1_cancelled", False)))
            rec.update(a_hat=a_hat, f5=bool(r1["f5"]), complete=(now if all(self._defer_complete(i) for q in range(k)
                                                                           for i in fr.members[q]) else None))
        self._h_log_event(fr, "w2_commit", k)

    def _w2_uncommit(self, fr: DeferFrame, why: str, detail: str | None = None):
        c = fr.w2.get("commit") if fr.w2 is not None else None
        if c is None:
            return
        self._w2.release(c["rid"], why)
        if self._hw is not None:
            self._hw.free_slot(("w2", c["rid"]))
        fr.w2["commit"] = None
        rec = fr.w2.get("commit_rec")
        if rec is not None and rec["splice"] is None:                      # spec 14: a commit never spliced has no decoding
            fr.w2["events"] = [e for e in fr.w2["events"] if e is not c["ev"]]   # event, so no load charge
        if rec is not None and rec["outcome"] is None:
            rec["outcome"] = why + (":" + detail if detail else "")        # (spec 14: done, verdict_first, or cancel:<why>)
        self._w2.stats["uncommit:" + why] += 1

    def _w2_cancel(self, fr: DeferFrame, why: str):
        """A commit before its splice that no longer holds (as `_u_cancel`): back to carrying on beta = 0, admitting
        nothing more this round; the next round's decision decides again (spec 1.3)."""
        self._h_release_next(fr); self._h_release_target(fr)
        if self._hw is not None:
            self._hw_reset(fr)
        self._w2_uncommit(fr, "cancel", why)
        self._w2.stats["cancel:" + why] += 1
        fr.state, fr.target, fr.h_je = "carry", -1, -1
        fr.limit = fr.w2["lim"] = fr.admitted
        fr.w2["round"] = self.current_round                                # (this round's admission: none)
        if self._w2.r1:
            fr.w2["r1_cancelled"] = True                                   # (R1: a later booking is a rebooking)

    def _w2_r1_round(self, fr: DeferFrame, patches_in_use) -> bool:
        """R1: a booked frame's round, after the verdict check and before the splice
        attempt. The round's one lookup (none in the booking's own round, whose decision was the booking; none again when
        rule 66's server re-enters the frame); True: the booking stands; False: it is cancelled (`redecided`), and the
        frame takes the lookup's admission limit and admits in this same round, as F_5's rule would."""
        w = fr.w2
        if w["round"] == self.current_round:
            return True
        _kind, stands, r0 = self._w2.decide(fr, patches_in_use, booked=True)
        if stands:
            return True
        self._w2_cancel(fr, "redecided")
        fr.limit = w["lim"] = r0
        self._defer_admit(fr)
        return False

    def _w2_r1_pending(self, fr: DeferFrame, patches_in_use):
        """R1 (16.1): a frame whose COMMIT waits for rule 66's commit check holds p as a booked frame does, so its
        round's one lookup is a booked frame's (none again when the server re-enters it). If the booking would not stand,
        the check is dropped, and the frame takes the lookup's admission limit and admits in this same round."""
        w = fr.w2
        if w["round"] == self.current_round:
            return
        _kind, stands, r0 = self._w2.decide(fr, patches_in_use, pending=True)
        if stands:
            return
        self._hw_reset(fr)
        w.pop("pending", None)
        fr.limit = w["lim"] = r0

    def _w2_r1_final(self, fr: DeferFrame, patches_in_use) -> bool:
        """R1 (16.1): the commit check completed. The frame books only if its round's lookup (the one the round
        made, else one now) gives COMMIT at p with R1's eligibility checked now; otherwise it takes that lookup's
        admission limit, as a cancelled booking does. True: it booked."""
        w = fr.w2
        if w["round"] != self.current_round:
            self._w2.decide(fr, patches_in_use, pending=True)
        k = w.pop("pending", None)
        if k is not None and k == fr.limit and self._w2.r1_commit_now(fr, k, patches_in_use):
            self._w2_act(fr, k, patches_in_use)
            return True
        fr.limit = w["lim"] = w["r1_row"]["r0"]
        return False

    def _w2_repair_event(self, fr: DeferFrame):
        """J_v's Lc_v (records only): the materialization or repair as a decoding event, with its script's static demand
        (the walk's N_0 on beta = 0, the rebased walk's N_1 on beta = 1) and the free-core forecast now."""
        w = fr.w2
        pr = self._w2.tab.profiles[w["vid"]]
        N = pr["reb"][str(w["base"])]["N"][w["base"] + fr.target] if w["half"] == 1 else pr["N0"][fr.target]
        w["events"].append(("repair", self.current_round, int(N), self._w2.forecast()))

    def _w2_splice_check(self, fr: DeferFrame, patches_in_use):
        """The splice's revalidation (`_u_splice_check`'s): D's stop unchanged, the cells free, the readiness known."""
        if fr.stop != fr.w2["commit"]["stop"]:
            return "stop_changed"
        if not self._u_cells_free(fr, fr.target):
            return "cells_reserved"
        if self._u_ready(fr, fr.target, patches_in_use)[0] is None:
            return "readiness"
        return None

    # ------------------------------------------------------------------ rule 66's hardware-limit mode (froq/hwlimits.py)
    def _hw_round(self):
        if self._hw is None:
            from .hwlimits import HWLimits, load
            params, sha = load(self._hw_cfg.get("file"))                  # (round 6, F(iii): --hw-limits-file)
            self._hw = HWLimits(self, self._hw_cfg["setting"], params, sha, bench=self._bench)
            self._hw.r17 = self._r17                                       # (---records: None when off)
            self._hw.setup_cells({tuple(p) for t in self.schedule_instructions[:self.n_original] for p in t.instruction.patches})
            self._hw.gsvc = self._r8_gsvc                                  # (round 8's guard-service time, default 0)
            if self._wq is not None and self._guard != "zl":               # (a guard check's f; port 1 waits an event's
                self._hw.ck_f, self._wq.port1 = self._wq.f, self._hw.ev_take   #  cycles past its first, account 3.2)
            self._hw.zl = self._guard == "zl"                              # (ZL: a guard check takes no cycles)
            if self._ctrl_slow is not None:                                # (--ctrl-slow s, applied once: every port and
                self._hw.set_slow(self._ctrl_slow, defer_latency=getattr(self, "_cs_defer", None))   # the write queue's port 2 get
                if self._wq is not None:                                   #  286/s cycles a round, with carry; refused where the
                    self._wq.set_slow(self._ctrl_slow)                     #  model is undefined)
            assert self._r9 is None or self._hw.setting == "improved", "the bounded check's port is the improved setting's"
            if self._r8:
                self._hw.age = HW_AGE                                      # (rule (a)'s A6: an event waiting HW_AGE rounds goes first)
                self._hw.assert_fits()                                     # (and every operation fits a port after the tick)
        if self._wq is not None and self._wq_hb:
            self._wq_hflush()                                              # (last round's hold changes, before its port closes)
        self._hw.round_start(self.current_round)
        if self._r9 is not None:
            if self._wq is not None:
                self._wq.round(self.current_round)                         # (port 2 retired up to w entries last round)
                self._wq.prune(self.current_round)
                if self._wq.C != WQ_INF:                                   # (K-aging's clock each round: who left, who aged.
                    self._r9.age_round(self.current_round)                 #  r9 runs it at a port turn, but the queue holds
                self._wq_sweep()                                           #  starts that take none, so an aged request that
            self._r9_port2_round()                                         #  left held every admission.  C unbounded: r9's)

    def _hw_serve(self, patches_in_use):
        """The deferral phase's end under rule 66: the reservation manager serves the phase's events in spec 9's order
        (`HWLimits.serve`), and each frame whose event completed carries on in the same phase -- a registration admits,
        a commit acts, a decide continues to its splice, a splice issues -- as it would have had its event completed
        when it was submitted.  What that submits is served in turn; the rest waits for the next round."""
        for _ in range(8):
            todo = [vid for vid in sorted(self._hw.serve()) if vid in self._live_frames]
            if not todo:
                break
            for vid in todo:
                self._defer_advance(self._frames[vid], patches_in_use)
        self._hw.batching = False

    def _hw_done(self, fr: DeferFrame, what: str):
        if fr.hw_wait == what:
            fr.hw_wait, fr.hw_ok = None, what

    def _hw_reset(self, fr: DeferFrame):
        """A cancel or retarget: the frame's queued splice, commit and decide events go."""
        for k in ("splice_and_rebase", "commit_check_and_reserve", "decide"):
            self._hw.cancel(fr.vid, k)
        if fr.hw_wait in ("splice", "commit", "decide"):
            fr.hw_wait = None
        if fr.hw_ok in ("splice", "commit"):
            fr.hw_ok = None

    def _hw_reg_done(self, fr: DeferFrame):
        """The registration event completed: the frame's guard is written, and it may admit."""
        if fr.state == "done" or fr.hw_guard_on:
            return
        if self._r8 and fr.r8_pend:
            self._r8_pend_add(fr.r8_pend, -1); fr.r8_pend = frozenset()
        for c_ in fr.guard:
            self._defer_ws_cells[c_] = self._defer_ws_cells.get(c_, 0) + 1
        fr.hw_guard_on = True
        if fr.hw_wait == "registration":
            fr.hw_wait = None

    def _hw_target_on(self, fr: DeferFrame, ws_):
        """D's decide event completed: the materialization's workspace is claimed."""
        if fr.state == "done" or fr.hw_wait != "decide":
            return
        fr.target_ws = frozenset(ws_)
        for c_ in fr.target_ws:
            self._defer_target_cells[c_] = self._defer_target_cells.get(c_, 0) + 1
        fr.hw_wait = None

    def _hw_splice_ready(self, fr: DeferFrame, rebase: bool) -> bool:
        """A splice (D's, or H's with its rebase) proceeds once its event, submitted the first round the frame is ready to
        splice, has completed: the reservation manager's operations, then the splice engine (and the HBM fetch of a
        commit past b_res)."""
        if fr.hw_ok == "splice":
            fr.hw_ok = None
            return True
        if fr.hw_wait == "splice":
            return False
        low = self._defer_boundary(fr, fr.target)[0]
        cells = dict(workspace=fr.target_ws, script_cells=(low.cells if low is not None else ()), old_guard=fr.guard)
        if rebase and fr.h_next is not None:
            tmp = fr.h_next.tmp
            cells["new_guard"] = set().union(*(self._defer_workspace(tmp, jj) for jj in range(tmp.c.F + 1) if tmp.feas[jj]))
        fr.hw_wait = "splice"
        cyc, bound = self._hw.splice_engine(low, rebase)
        self._hw.submit("splice_and_rebase", lambda fr=fr: self._hw_done(fr, "splice"), cells=cells, engine=cyc, store_bound=bound,
                        fetch=bool(rebase and fr.target > self._hw.b_res), owner=fr.vid, plain=not rebase)
        if fr.hw_ok == "splice":
            fr.hw_ok = None
            return True
        return False

    def _hw_unlock(self, vid: int, cells):
        for p in cells:
            s_ = self._defer_locked.get(p)
            if s_ is not None:
                s_.discard(vid)
                if not s_:
                    del self._defer_locked[p]

    def _hw_guard_off(self, cells):
        for c_ in cells:
            n = self._defer_ws_cells.get(c_, 0) - 1
            if n > 0:
                self._defer_ws_cells[c_] = n
            else:
                self._defer_ws_cells.pop(c_, None)

    def _recovery_finished(self, r: dict) -> bool:
        """A recovery's script has ended (a frame-only recovery always has): the price-mismatch check counts these only,
        so a recovery in flight at a censoring cut is not a mismatch (hygiene, decision log)."""
        return r.get("kind") != "script" or all(self.schedule_instructions[i].end_round != -1 for i in r.get("instrs", ()))

    # ------------------------------------------------------------------ the hybrid mode H
    def _h_trigger(self, v: Value, fr: DeferFrame) -> bool:
        """The threshold rule on the client interface (section 1; gap analysis B.10): a prediction published by the
        frame's registration round is used at registration, a later one from one round after its publication, a
        revision from one round after it arrives (the deferral phase runs before this round's revisions); the trigger
        holds iff the prediction in use is m = 1 with confidence >= theta. `hybrid.install` (the smoke's diagnostic
        arms) replaces the rule by a fixed install of 0 or 1 rounds after publication."""
        h = self._h
        pr, m, c, rc = self._dview(v)                                      # (rule 41d: the decision client)
        if h["source"] != "client" or pr < 0:
            return False
        now = self.current_round
        assert rc < now or rc == pr, ("H: a revision visible in its own round's deferral phase", v.vid)
        if rc > pr and now < rc + self._thr_rc:                            # rule 41b': a revision takes effect thr_rc rounds
            c = self._rc_prev_c.get(v.vid, c)                              # after it arrives (never at 1: rc < now)
        return now >= self._h_install_round(fr, pr) and bool(m) and float(c) >= float(h["theta"])

    def _h_install_round(self, fr: DeferFrame, pr: int) -> int:
        """The first round H's trigger may use the frame's prediction, published in round pr. The original protocol
        (threshold latency 1; B.10): at the frame's registration if published by then, else one round after its
        publication. Rule 41b' at a latency L >= 2: the installation takes effect L rounds after the prediction's
        REGISTER, at its publication, and the frame uses it from then on or from its registration. `hybrid.install`
        (the smoke's diagnostic arms) fixes it at 0 or 1 rounds after publication."""
        if "install" in self._h:
            return pr + int(self._h["install"])
        if self._thr_install == 1:
            return fr.reg_round if pr <= fr.reg_round else pr + H_INSTALL
        return max(fr.reg_round, pr + self._thr_install)

    @property
    def _eta_table(self):
        """The run's latency table (H-lead's estimator reads its class means), from the keyed decoder the device installed."""
        st = getattr(getattr(self, "_decode_sampler", None), "sampler_state", None)
        if st is None:
            raise RuntimeError("H-lead's estimator needs the run's keyed decoder (a run through froq.run.run_one)")
        return st.table

    def _h_lead_ok(self, v: Value, fr: DeferFrame) -> bool:
        """H-lead: in a round where the trigger would hold, a frame in carry starts
        its early materialization only if the causal estimate of the rounds left until the value's verdict exceeds
        lambda. It filters the start only: a trigger that has started is cancelled by the trigger rule alone (R7 / R8), not
        by a lead that shrinks as the verdict approaches. Without the option every trigger passes."""
        if self._h_lead is None:
            return True
        est = self._eta.estimate(v)
        st = self._lead_stats
        st["checks"] += 1
        self._lead_hist[str(int(min(est, 4096)) // 8 * 8)] += 1
        if est > self._h_lead:
            st["passed"] += 1
            return True
        st["filtered"] += 1
        return False

    def _h_start(self, fr: DeferFrame):
        """The trigger held in carry (section 2.1): admission is frozen at the admitted steps."""
        now = self.current_round
        if fr.h_trigger_round < 0:
            fr.h_trigger_round = now
        fr.state, fr.limit, fr.target, fr.h_frozen = "early_decide", fr.admitted, -1, fr.admitted
        self._h_stats["triggers"] += 1
        fr.h_log.append((now, "trigger", fr.admitted))

    def _h_advance(self, fr: DeferFrame, patches_in_use) -> bool:
        """One round of an H state.  True: the verdict b = 1 arrived before the splice and the frame is D's decide now."""
        v = self.values[fr.vid]; now = self.current_round; st = self._h_stats
        st["rounds_" + fr.state] += 1
        if fr.state == "confirm_wait":                                     # R4: the early script is still running
            if fr.h_early_done >= 0:
                self._h_log_event(fr, "confirmed", fr.h_je)
                self._defer_finish(fr, "early_confirmed")
            return False
        if v.delivered:                                                    # R2 / R3: the verdict before the splice
            self._h_release_next(fr); self._h_release_target(fr)
            if self._hw is not None:
                self._hw_reset(fr)                                         # rule 66: its queued splice or commit event goes
            fr.verdict_round, fr.p_verdict = now, self._defer_current(fr)
            if fr.w2 is not None:
                self._r8_note_decision(fr); self._w2.verdict(fr)
            st["verdict%d_before_splice" % int(bool(v.truth))] += 1
            self._h_log_event(fr, "verdict_before_splice", int(bool(v.truth)))
            fr.limit, fr.target = fr.stop, -1
            if self._u_on:                                                 # U: the verdict first cancels the ACT, as in H
                self._u_unact(fr, "verdict_first", "verdict%d" % int(bool(v.truth)))
            if self._w2 is not None:                                       # the decision: so does it its commit (1.2's simplification)
                self._w2_uncommit(fr, "verdict_first")
            if not v.truth:
                fr.state = "carry"
                self._defer_finish(fr, "dropped"); return False
            fr.state = "decide"
            return True
        if self._u_on or self._w2 is not None:
            pass                                                           # U and the decision have no threshold (unified-policy.md 7)
        elif not self._h_trigger(v, fr):                                   # R7 / R8: a revision took c below theta
            st["cancels_" + ("before_choice" if fr.state == "early_decide" else "before_splice")] += 1
            self._h_release_next(fr); self._h_release_target(fr)
            fr.state, fr.limit, fr.target = "carry", fr.stop, -1
            self._h_log_event(fr, "cancel", fr.admitted)
            fr.h_je = -1
            self._defer_admit(fr)
            return False
        if fr.state == "early_decide":
            if not self._h_choose(fr, patches_in_use):
                st["no_candidate_rounds"] += 1; return False              # R12: wait (the verdict or a revision ends it)
            fr.state = "early_materialize"
        if self._w2 is not None and self._w2.r1 and fr.state == "early_materialize" and not self._w2_r1_round(fr, patches_in_use):
            return False                                                   # R1: the booked round's lookup cancelled it
        # early_materialize: D's materialize logic with the early target (section 2.2)
        if not all(self._defer_complete(i) for k in range(fr.target) for i in fr.members[k]):
            st["materialize_wait_rounds"] += 1; return False
        if self._defer_ws_blocked(fr, fr.target, patches_in_use, splice=True):
            st["workspace_wait_rounds"] += 1
            if self._guard == "off":
                self._off_ws(fr, patches_in_use)                           # (OFF: a failure's kind, if any)
            if self._resv is not None:
                self._resv_ws(fr, patches_in_use)                          # (SER-R, records: what occupies its workspace)
            if self._w2 is not None and self._defer_ws_blocked(fr, fr.target, patches_in_use, splice=False):
                self._w2_cancel(fr, "target_pinned")       # the decision never retargets, as U
            elif self._u_on and self._defer_ws_blocked(fr, fr.target, patches_in_use, splice=False):
                self._u_cancel(fr, "revalidation", "target_pinned")        # U never retargets (section 7)
            elif self._defer_ws_blocked(fr, fr.target, patches_in_use, splice=False):
                self._h_release_next(fr); self._h_release_target(fr)     # R13: pinned by a patch that waits for the frame
                if self._r8 and fr.target == fr.r8_wit:
                    self._defer_stats["r7:rule_wit_retargets"] += 1        # (the witness retargeted, an invariant failure; 0)
                fr.feas[fr.target] = False                                 # (as D's retarget); the steps taken back for it
                fr.state, fr.limit, fr.target, fr.h_je = "early_decide", fr.h_frozen, -1, -1   # are admitted again
                st["retargets"] += 1; self._h_log_event(fr, "retarget", fr.admitted)
                self._defer_admit(fr)
            return False
        if self._u_on:
            why = self._u_splice_check(fr, patches_in_use)                 # U: duty 3, the splice revalidation
            if why is not None:
                self._u_cancel(fr, "revalidation", why)
                return False
            self._u_acts[fr.u_act["log"]]["splice"] = now
        if self._w2 is not None:                                           # the decision: the splice in the round after the decision
            if now < fr.w2["commit"]["round"] + W2_ELL:                    # (ell = 1, section 4.2), then revalidated
                st["w2:ell_wait_rounds"] += 1
                return False
            why = self._w2_splice_check(fr, patches_in_use)
            if why is not None:
                self._w2_cancel(fr, why)
                return False
        if self._hw is not None and not self._hw_splice_ready(fr, rebase=True):
            return False                                                   # rule 66: the splice and rebase event
        if self._u_on and fr.u_act is not None and self._hw is not None:
            self._u_spliced(fr.u_act["rid"])                               # rule 66: the ACT's reservation from its splice
        self._h_release_target(fr)
        self._h_splice(fr, patches_in_use)
        return False

    def _h_log_event(self, fr: DeferFrame, what: str, detail=None):
        fr.h_log.append((self.current_round, what, detail))

    def _h_choose(self, fr: DeferFrame, patches_in_use) -> bool:
        """Section 2.1, steps 2-4: the nearest certified feasible boundary from the current one to the admitted steps."""
        st = self._h_stats
        p = self._defer_current(fr)
        for j in range(p, fr.admitted + 1):
            if j > (H_JMAX if self._walk_cap is None else self._walk_cap):
                st["beyond_JH_rounds"] += 1; break
            if j in fr.h_bad or not fr.feas[j] or self._defer_ws_blocked(fr, j, patches_in_use, splice=False):
                continue
            nxt = self._h_prepare(fr, j, patches_in_use)
            if nxt is None:
                continue
            if j < fr.admitted:
                if not self._resv_back_ok(fr, j):
                    continue                                               # (SER-R: no floor for this take-back; the next boundary)
                self._defer_unadmit(fr, j)
            fr.target = fr.limit = fr.h_je = j
            fr.h_choice_round = self.current_round
            fr.target_ws = self._defer_workspace(fr, j)
            for c_ in fr.target_ws:
                self._defer_target_cells[c_] = self._defer_target_cells.get(c_, 0) + 1
            for i in nxt.new_holds:                                        # the rebased contract's holds, from the choice on
                self._wq_hadd(i, fr.vid)
            for e in nxt.extra_claims:
                self._defer_claim[e] = fr.vid
            for i in nxt.extra_members:
                self._defer_member[i] = fr.vid
            fr.h_next = nxt
            if self._r8:
                self._r8_pend_reb(fr, nxt)
            st["choices"] += 1; self._h_log_event(fr, "choose", j)
            return True
        return False

    def _h_prepare(self, fr: DeferFrame, j: int, patches_in_use):
        """The rebased contract of an early materialization at j (section 3), with the frame data it would carry and the
        holds, claims and members it adds; None if it cannot be built or used now (counted by reason; a permanent reason
        marks j bad for the frame)."""
        nxt, why, permanent = self._h_prepare_core(fr, j, patches_in_use)
        if nxt is None:
            self._h_stats["prepare_fail:" + why] += 1
            if permanent:
                fr.h_bad.add(j)
        return nxt

    def _h_prepare_core(self, fr: DeferFrame, j: int, patches_in_use):
        """`_h_prepare` without its records: (the prepared contract or None, why not, whether that is permanent). It
        changes no state of the frame or the run (U's preview, section 8, uses it directly)."""
        v = self.values[fr.vid]
        others = frozenset(e for e, x in self._defer_claim.items() if x != fr.vid)
        hk = H_K if self._walk_cap is None else self._walk_cap
        k_reb = hk if self._k_tot is None else min(hk, self._k_tot - j)   # K_tot: the prefix and the rebased steps together
        c2 = build_contract(self.es, self.sem, self.sched_view, self._specs[fr.vid], False, k_reb, deferral=True, claimed=others, rebase_at=j)
        if c2.rebase is None:
            return None, c2.fence_reason.split(":")[0], True
        if [(s.instrs, s.residual_key) for s in c2.rebase["prefix"]] != [(s.instrs, s.residual_key) for s in fr.c.steps[:j]]:
            return None, "prefix", True
        last = c2.steps[-1] if c2.steps else None
        Lf = (set(last.L) if last else set(c2.entry_L)) | {v.wire}
        Df = (set(last.D_fixed | last.D_cond) if last else set(c2.entry_D)) | {v.result_id} | set(c2.held_bits)

        def exempt(i):                                                     # D's fence rule (`_defer_register`): resource
            return (self._is_startup_instruction[i] and not (self._instr_wires.get(i, frozenset()) & Lf)   # initialisations
                    and not (self._instr_preds.get(i, frozenset()) & Df))                                    # outside the cone
        # executed cone work refuses the rebased contract, except those initialisations: D lets them start, they commute
        refuse_executed_cone_work(self.es, c2, lambda i: 0 if exempt(i) else (2 if self.schedule_instructions[i].end_round != -1
                                                                             else (1 if i in self._defer_started else 0)))
        if c2.fence_reason == "executed_cone_work":
            return None, "executed_cone_work", True
        tmp = DeferFrame(vid=fr.vid, c=c2, reg_round=fr.reg_round, members=[set(s.instrs) for s in c2.steps],
                         r=[c2.entry_recovery.P1] + [s.price_P1 for s in c2.steps], fence=set())
        tmp.kof = {i: k for k, mem in enumerate(tmp.members) for i in mem}
        tmp.fence = {i for i in fence_set(self.es, c2) if i not in self._defer_started and not exempt(i)}
        tmp.unadm = {i for k in range(c2.F) for i in tmp.members[k] if i not in self._defer_started}
        tmp.feas = [x is not None for x in tmp.r]
        if self._resv is None and self._defer_ws_blocked(tmp, 0, patches_in_use, splice=False):   # rebased boundary 0, the
            return None, "boundary0_pinned", False                         # undo at j, must be usable (SER-R, the static
                                                                           # rule in `_resv_rev`, no state read or search)
        new_holds = (tmp.fence | tmp.unadm) - (fr.fence | fr.unadm)
        if any(self._defer_member.get(i, fr.vid) != fr.vid for i in new_holds):
            return None, "holds_other_frame", False
        for c_ in self._defer_ws_cells:                                    # nor pin another live frame's candidate workspace
            if self._defer_ws_cells[c_] - (c_ in fr.guard) <= 0 or c_ not in self._active_patches or c_ in self._defer_locked:
                continue
            u = self._defer_first_unstarted(c_)
            if u is not None and u not in fr.kof and u not in tmp.kof and self._defer_waits_for(tmp, u, 0):
                return None, "pins_other_workspace", False
        if self._r8 and self._r8_rev_refuses(fr, tmp, j, new_holds, c2.F):
            return None, "rule_revision", False                            # (round 8's rule (a2); the old contract stays)
        own_events = {e for s in fr.c.steps for e in s.events}
        extra_claims = {e for s in c2.steps for e in s.events} - own_events
        assert not any(e in self._defer_claim for e in extra_claims), ("H: a rebased walk took another frame's event", fr.vid)
        extra_members = {i for s in c2.steps for i in s.instrs} - set(fr.kof)
        return SimpleNamespace(c=c2, tmp=tmp, new_holds=new_holds, extra_claims=extra_claims, extra_members=extra_members, j=j), None, False

    def _h_release_next(self, fr: DeferFrame):
        """Undo `_h_choose`'s additions (a cancel, a verdict before the splice, a retarget)."""
        nxt = fr.h_next
        if nxt is None:
            return
        if self._r8 and fr.r8_pend_reb:
            self._r8_pend_add(fr.r8_pend_reb, -1); fr.r8_pend_reb = frozenset()
        keep = fr.fence | fr.unadm
        for i in nxt.new_holds:
            if i in keep:
                continue
            s_ = self._defer_hold_of.get(i)
            if s_ is not None:
                if self._wq is not None and fr.vid in s_:
                    self._wq_h(fr.vid, i)                                         # (a hold mark cleared)
                s_.discard(fr.vid)
                if not s_:
                    del self._defer_hold_of[i]
        for e in nxt.extra_claims:
            if self._defer_claim.get(e) == fr.vid:
                del self._defer_claim[e]
        for i in nxt.extra_members:
            if self._defer_member.get(i) == fr.vid:
                del self._defer_member[i]
        fr.h_next = None

    def _h_release_target(self, fr: DeferFrame):
        for c_ in fr.target_ws:
            n = self._defer_target_cells.get(c_, 0) - 1
            if n > 0:
                self._defer_target_cells[c_] = n
            else:
                self._defer_target_cells.pop(c_, None)
        fr.target_ws = frozenset()

    def _h_splice(self, fr: DeferFrame, patches_in_use):
        """Section 2.2: D's splice of the early script at j_e, then the rebase in the same round."""
        self._defer_splice(fr, since=fr.h_trigger_round)
        rec = self.values[fr.vid].recovery
        rec["early"] = True
        fr.h_early_rec, fr.h_splice_round = rec, self.current_round
        fr.h_early_locked, fr.locked = fr.locked, frozenset()             # the early lock apart (a repair's goes in fr.locked)
        if self._u_on and rec["kind"] == "script":                         # U's decoder-traffic tags
            self._u_script_instrs.update(rec["instrs"])
            for c_ in fr.h_early_locked:
                u = self._defer_first_unstarted(c_)
                if u is not None:
                    self._u_delayed_instrs.add(u)
        self._h_stats["splices"] += 1; self._h_stats["early_P1"] += rec["P1"]
        self._h_log_event(fr, "splice", fr.target)
        self._h_rebase(fr, patches_in_use)

    def _h_rebase(self, fr: DeferFrame, patches_in_use):
        """Section 3 / 4: the frame carries the rebased contract prepared at the choice.  The original holds, members and
        claims from j_e on and its guard are replaced by the rebased ones in this one step; the prefix (the steps before
        j_e, complete) stays claimed until the frame ends, as in D."""
        nxt, j = fr.h_next, fr.target
        c2, tmp = nxt.c, nxt.tmp
        if self._r8 and fr.r8_pend_reb:
            self._r8_pend_add(fr.r8_pend_reb, -1); fr.r8_pend_reb = frozenset()   # (round 8: the guard is the frame's now)
        assert not any(i in self._defer_started for i in tmp.unadm), ("H: a rebased step began before the rebase", fr.vid)
        old = fr.c.steps
        fr.h_prefix = [(set(s.instrs), set(s.events)) for s in old[:j]]
        fr.h_F0 = fr.c.F
        keep = tmp.fence | tmp.unadm
        for i in (fr.fence | fr.unadm) - keep:
            s_ = self._defer_hold_of.get(i)
            if s_ is not None:
                if self._wq is not None and fr.vid in s_:
                    self._wq_h(fr.vid, i)                                         # (a hold mark cleared)
                s_.discard(fr.vid)
                if not s_:
                    del self._defer_hold_of[i]
        new_instrs = {i for s in c2.steps for i in s.instrs}; new_events = {e for s in c2.steps for e in s.events}
        for s in old[j:]:
            for i in s.instrs:
                if i not in new_instrs and self._defer_member.get(i) == fr.vid:
                    del self._defer_member[i]
            for e in s.events:
                if e not in new_events and self._defer_claim.get(e) == fr.vid:
                    del self._defer_claim[e]
        for c_ in (fr.guard if fr.hw_guard_on else ()):                    # (rule 66: a guard not yet registered is not
            n = self._defer_ws_cells.get(c_, 0) - 1                        # counted; its registration counts the new one)
            if n > 0:
                self._defer_ws_cells[c_] = n
            else:
                self._defer_ws_cells.pop(c_, None)
        fr.c, fr.members, fr.kof, fr.r, fr.fence = c2, tmp.members, tmp.kof, tmp.r, tmp.fence
        fr.unadm = set(tmp.unadm)
        fr.admitted, fr.rebased, fr.h_next = 0, True, None
        if self._wq is not None:
            self._wq.g_rebase(fr.vid)                                      # (G, records: the rebased walk's steps, numbered from 0)
        fr.r8_b1 = fr.r8_wit = 0                                           # ((a2) certified the rebased boundary 0)
        fr.target, fr.p_run, fr.first_ok = -1, -1, -1
        fr.feas = [x is not None for x in fr.r]
        cap = WALK_CAP if self._walk_cap is None else self._walk_cap
        if self._resv is not None:                                         # (SER-R, the rebased
            pre = {i for instrs_, _e in fr.h_prefix for i in instrs_}      #  walk's usable boundaries only; `_resv_rev` checked
            js = [jj for jj in range(c2.F + 1) if fr.feas[jj]]              #  boundary 0 at the choice. static, as at the
            if self._hw is not None and js and j + js[-1] > cap:           #  registration -- the rebased key's boundaries up to
                self._hw.stats["walk_cap"] += 1                            #  the walk cap, the rule folded over every member of
                js = [jj for jj in js if j + jj <= cap]                    #  the frame's walks so far from free cells; no
            ok = self._resv_usable(fr, js, pre=fr.resv_hist | pre, static=True)   # feasibility from the patches now and no
            dyn = set(self._resv_usable(fr, js, patches_in_use, pre))      #  tangle search. dyn: form, from the cells'
            self._resv_st["rebase_static_only"] += len(set(ok) - dyn)      #  state now, records only)
            self._resv_st["rebase_dyn_only"] += len(dyn - set(ok))
            for jj in js:
                if jj not in ok:
                    fr.feas[jj] = False
            self._resv_st["rebase_unclear"] += len(js) - len(ok)
            fr.resv_hist = fr.resv_hist | frozenset(pre)
            js, K0, capped = ok, c2.F, False
        else:
            for jj in range(c2.F + 1):
                if fr.feas[jj] and self._defer_ws_blocked(fr, jj, patches_in_use, splice=False):
                    fr.feas[jj] = False
            K0, capped = self._defer_tangle(fr)
            js = [jj for jj in range(K0 + 1) if fr.feas[jj]]
            if self._hw is not None and js and j + js[-1] > cap:
                self._hw.stats["walk_cap"] += 1                            # rule 66: the walk cap counts program boundaries
                js = [jj for jj in js if j + jj <= cap]
        assert js and js[0] == 0, ("H: the rebased boundary 0 (the undo at the early boundary) is not usable", fr.vid, fr.feas[:3])
        fr.top = js[-1]
        fr.top_why = "dependency" if fr.top == c2.F else (("tangle_search_cap" if capped else "tangle") if K0 < c2.F else "infeasible")
        if c2.fence_reason == "K" and fr.top == c2.F:
            self._h_stats["rebased_K_frames"] += 1
        fr.stop = fr.top
        if (fr.rule or self.defer_cfg["rule"]) == "no_cost_increase":   # H-nci (rule 30), U: the frame's rule after the rebase too
            for a_, b_ in zip(js, js[1:]):
                if fr.r[b_] > fr.r[a_]:
                    fr.stop = a_; break
            self._h_stats["rebased_nci_stops"] += fr.stop < fr.top
        fr.limit = fr.stop
        fr.guard = set(fr.resv_fp) if self._resv is not None else \
            set().union(*(self._defer_workspace(fr, jj) for jj in range(c2.F + 1) if fr.feas[jj]))   # (SER-R: the held mask)
        for c_ in (fr.guard if fr.hw_guard_on else ()):
            self._defer_ws_cells[c_] = self._defer_ws_cells.get(c_, 0) + 1
        fr.state = "carry"
        if self._w2 is not None:                                           # the decision: the rebased key's slot; its decisions read
            self._w2.rebased(fr, j)                                        # it from the next round's start (spec 1.3 and 8), so
            fr.limit = fr.w2["lim"] = fr.admitted                          # the splice round admits nothing on beta = 1
            fr.w2["round"] = self.current_round
            rec = fr.w2.get("commit_rec")
            if rec is not None:
                rec["splice"] = self.current_round
            if fr.w2.get("commit") is not None:
                self._w2.spliced(fr.w2["commit"]["rid"])                   # (its reservation runs from the splice)
        self._defer_admit(fr)

    def _h_early_progress(self, fr: DeferFrame):
        """Section 2.2: when the early script's instructions have all completed (a frame-only script: the next round),
        its cells are unlocked and its frame is applied, in both worlds."""
        r = fr.h_early_rec; now = self.current_round
        if r["kind"] == "frame":
            if r["round"] >= now:
                return
        elif not all(self.schedule_instructions[i].end_round != -1 for i in r["instrs"]):
            return
        if self._hw is not None and fr.h_early_locked:
            cells, vid = fr.h_early_locked, fr.vid                         # rule 66: the script end event unlocks its cells
            self._hw.submit("script_end", lambda vid=vid, cells=cells: self._hw_unlock(vid, cells), cells=dict(script_cells=cells))
        else:
            for p in fr.h_early_locked:
                s_ = self._defer_locked.get(p)
                if s_ is not None:
                    s_.discard(fr.vid)
                    if not s_:
                        del self._defer_locked[p]
        fr.h_early_locked = frozenset()
        self._apply_recovery_frame(self.values[fr.vid], r)
        if r["kind"] == "script":
            r["recovery_end_round"] = max(self.schedule_instructions[i].end_round + 1 for i in r["instrs"])
        r["done_round"] = fr.h_early_done = now
        self._h_log_event(fr, "early_done", fr.h_je)

    def _h_confirm(self, fr: DeferFrame):
        """Section 2.3, b = 1 after the splice: confirmed once the early script is complete."""
        self._h_stats["verdict1_after_splice"] += 1
        self._h_log_event(fr, "verdict", 1)
        if fr.h_early_done >= 0:
            self._h_log_event(fr, "confirmed", fr.h_je)
            self._defer_finish(fr, "early_confirmed")
        else:
            fr.state = "confirm_wait"

    def _h_summary(self) -> dict:
        frames = [fr for fr in self._frames.values()]
        how = collections.Counter(fr.how for fr in frames)
        st = self._h_stats
        dl = {}
        if self._h.get("source") == "client" and not self._h.get("unified"):   # rule 41b': H's deadlines and their misses
            n = mi = nr = mr = 0
            for fr in frames:
                pr, _m, _c, rc = self._dview(self.values[fr.vid])
                if pr < 0:
                    continue
                n += 1
                if fr.verdict_round >= 0 and self._h_install_round(fr, pr) > fr.verdict_round:
                    mi += 1                                                # (the verdict came before the installation)
                if rc > pr:
                    nr += 1
                    mr += fr.verdict_round >= 0 and rc + self._thr_rc > fr.verdict_round
            dl = dict(deadlines=dict(install_latency=self._thr_install, rc_latency=self._thr_rc, predictions=n, missed_install=mi,
                                     revisions=nr, missed_rc=mr))
        return dict(config=dict(self._h), J_H=(H_JMAX if self._walk_cap is None else self._walk_cap),
                    K_H=(H_K if self._walk_cap is None else self._walk_cap), install_latency=H_INSTALL, counters=dict(sorted(st.items())), **dl,
                    outcomes=dict(how), early_confirmed=how.get("early_confirmed", 0), early_repaired=how.get("early_repaired", 0),
                    repairs_at_undo=sum(1 for fr in frames if fr.how == "early_repaired" and fr.target == 0),
                    early_boundary_hist={str(k): n for k, n in sorted(collections.Counter(fr.h_je for fr in frames if fr.rebased).items())},
                    early_incomplete=sum(1 for fr in frames if fr.h_early_rec is not None and fr.h_early_rec["kind"] == "script"
                                         and any(self.schedule_instructions[i].end_round == -1 for i in fr.h_early_rec["instrs"])),
                    log_fields=list(H_LOG_FIELDS),
                    **({"lead": dict(lambda_rounds=self._h_lead, checks=self._lead_stats["checks"], passed=self._lead_stats["passed"],
                                     filtered=self._lead_stats["filtered"],
                                     estimate_hist=dict(sorted(self._lead_hist.items(), key=lambda kv: int(kv[0]))))}
                       if self._h_lead is not None else {}))

    # ------------------------------------------------------------------ U, the unified policy
    def _u_setup(self, u):
        """U's settings and state. delta = "inf" switches U off: no REGISTER, epoch
        request or window event is made, so the scheduler runs D-nci; the confidence client runs as configured."""
        delta = str(u.get("delta"))
        assert delta in U_DELTAS, u
        self._u_L = int(u.get("latency", U_LATENCY))                     # act_lat: from a request's round to its effect
        assert 1 <= self._u_L <= 16, u                                   # (the golden's ACT history: act_lat <= H_MAX)
        self._u_elig = int(u.get("elig", self._u_L))                     # round 6 (B): the eligibility horizon -- 51a's queue
        assert 1 <= self._u_elig <= 16, u                                # check and the stale scan -- act_lat unless held apart
        self._u_on = U_DELTAS[delta] is not None
        self._u_carry = delta == "carry"                                 # rule 41d's all-CARRY control
        self._u_client, self._u6 = self._u_on, None                      # (U v4: U's client is on exactly when U is)
        self._u_noact = delta == "noact"                                 # U v6, U6-noact: no EPOCH request at all
        self._u_nocom = bool(u.get("nocom"))                             # SER-NC: every ACT the unit
        assert not self._u_nocom or (self._u_on and not self._u_carry and not self._u_noact), u   # answers is discarded at once
                                                                         # (cause nocom): requests, passes and u_xs stand, no commit
        self._u_newr: dict = {}                                          # U v6: window -> the round its NEW was sent
        # rule 66: each window slot's generation, one up at each allocation; the window's
        # own; the unit's (the generation of the last NEW it served there); a request carries its roots' generations
        self._u_gen: dict = {}
        self._u_wgen: dict = {}
        self._u_ugen: dict = {}
        if u.get("v6") is not None:                                      # U v6 (round rules 50-52, 56; froq/u6.py)
            from .u6 import U6State
            assert u.get("h") is not None and str(u["h"]) in ("0", "1", "inf") and self._u_on and not self._u_carry, \
                ("U v6 runs h = 0 (FAC-A), 1 or inf (FAC-F) with U's unit on (its ETA file feeds the carry rule)", u)
            self._u6 = U6State(self, u["v6"], **self._u6_args)
            self._u_client = self._u6.client                            # (U6-off: the unit runs, the client is off)
        # ablations of U v4 (rule 33a): E1 off, E2 off (E1 then has no E2 gain to compare with), the stall term off
        self._u_epochs = {1: bool(u.get("e1", True)), 2: bool(u.get("e2", True))}
        self._u_nostall = bool(u.get("nostall", False))
        assert (all(self._u_epochs.values()) and not self._u_nostall) or (u.get("h") is not None and delta not in ("inf", "carry")), \
            ("ablations are of U v4 with its client", u)
        self._u_stats = collections.Counter()
        # U v4 (rule 44a): the carry rule per value at registration, by headroom h (None: h = inf, D-nci's rule for every
        # frame; 0: D-lazy's for every frame); lazy frames' materialization demand is reserved (`_u4_lazy_reserve`)
        self._u4 = u.get("h") is not None
        assert not self._u4 or str(u["h"]) in U4_H, u
        self._u4_h = None if not self._u4 or str(u["h"]) == "inf" else float(u["h"])
        self._u4_lazy_on = self._u4 and self._u4_h is not None and (self._u4_h > 0 or self._u_on)   # (h = 0, delta = inf: D-lazy)
        self._u_resv: dict = {}                                          # rule 41c: reservation -> its record (`_u_reserve`)
        self._u_resv_n, self._u_resv_total = 0, 0                        # (the total: the reservations the pool counts now)
        self._u_round_resv: list = []                                    # this round's reservation events (replay)
        self._u_peaks = collections.Counter()                            # entries, slots, requests and window events per pass
        self._u_mu_cache: dict = {}                                      # (`_u_mu`: the tables' and demand_lazy's mu, U off too)
        rep = self._u6 is not None and self._u6.repair                   # U v6: Eq. (1)'s XS and XR in the tables
        if self._u4_lazy_on:
            from .u_tables import UTables
            self._u_tab = UTables(self, self._k_tot, repair=rep)
        if not self._u_on:
            return
        from .u_tables import UTables
        gu = _golden_u()
        self._h = dict(source="client", unified=True)                   # the hybrid mode's splice, rebase and races
        assert self._u_L <= gu.H_MAX, u
        uu = self._uu = gu.UUnit(n_w=U_SLOTS)
        uu.delta, uu.client_on = U_DELTAS[delta] * self.d_t, not self._u_carry and self._u_client
        uu.no_stall = self._u_nostall                                    # (rule 33a, 6: W_b = 0 in the gains)
        if self._u6 is not None:                                         # U v6: 51a, Eq. (1)'s repair terms, T6's threshold,
            uu.q_wait, uu.repair_terms = self._u6.q_wait, self._u6.repair    # rule 56a's check traces
            if self._u6.xi0:                                               # round 6 (amendment 1): the score without XS, XR
                assert not self._u_replay_path, "the replay records carry no xi0 (round 6)"
                uu.xi0 = True
            uu.theta = None if self._u6.theta is None else min((1 << gu.FRAC) - 1, int(round(self._u6.theta * (1 << gu.FRAC))))
            uu.trace_checks = self._u6.instr
        uu.path, uu.stale_max, uu.act_lat = int(self.path), U_STALE_D * self.d_t, self._u_L
        if self._u_elig != self._u_L:
            assert not self._u_replay_path, "the replay records carry no eligibility horizon apart from act_lat (round 6)"
            uu.elig_lat = self._u_elig
        if not self._u4_lazy_on:
            self._u_tab = UTables(self, self._k_tot, repair=rep)
        self._u_loaded = 0                                               # classes configured in the unit (CONFIG sel 6)
        self._u_ent = [None] * U_ENTRIES                                 # the engine's entry table: [vid, live] (False: recovering)
        self._u_ent_of: dict = {}                                        # vid -> its entry (live or recovering)
        self._u_reg: dict = {}                                           # vid -> the scheduler's REGISTER (None: not registered)
        self._u_msgs = dict(REG=[], RES=[], RC=[], RD=[])                # this round's engine messages, in arrival order
        self._u_wins: list = []                                          # this round's window events (op, slot, own, preds, window)
        self._u_reqs: list = []                                          # this round's epoch requests
        self._u_pending_vids: set = set()                                # frames with ACTs that have not taken effect
        self._u_out = collections.deque()                                # outstanding requests: (answer round, potential excess)
        self._u_out_total = 0
        self._u_round_disc: list = []                                    # this round's discards, and reservation events (replay)
        self._u_acts: list = []                                          # rule 41f: every executed ACT
        self._u_install: list = []                                       # rule 41f: every installation's b*
        self._u_stale_q: list = []                                       # heap (stamp, slot): windows that may go stale
        self._u_slot: dict = {}                                          # window -> its slot in the ETA register file
        self._u_owner = [None] * U_SLOTS                                 # slot -> its window (None: never used)
        self._u_svf = [False] * U_SLOTS                                  # slot -> its window is verified
        self._u_pin = [0] * U_SLOTS                                      # slot -> unverified windows naming it among their first 4
        self._u_free = list(range(U_SLOTS))                              # heap of the slots that may be (re)used
        self._u_named: dict = {}                                         # window -> the slots it names (first 4, distinct)
        self._u_wait_cnt: dict = {}                                      # window outside the file -> its predecessors not in it
        self._u_waits_on: dict = {}                                      # ... and which
        self._u_blocked_by: dict = {}                                    # window -> the windows waiting for it to enter
        self._u_enter_q: list = []                                       # heap: windows outside waiting only for a slot
        self._u_slot_waiting: set = set()                                # (counted once)
        self._u_last: dict = {}                                          # window -> the round of its last NEW or UPD
        self._u_refresh = collections.deque()                            # (due round, window, the update it refreshes)
        self._u_queued: dict = {}                                        # window -> its mu (Q24.8), while queued for a full decoder
        self._u_qbit = _Fenwick()                                        # ... by window index: the queue term's sums
        self._u_mu_w: dict = {}                                          # window -> its mu (Q24.8)
        self._u_script_instrs, self._u_repair_instrs, self._u_delayed_instrs = set(), set(), set()
        self._u_tag: dict = {}                                           # decoder task -> script | repair | delayed
        self._u_traffic = dict(script=0, repair=0, delayed=0, rounds=0)
        self._u_unbuilt_round, self._u_unbuilt = None, {}
        self._u_rep = None                                               # the replay records (section 11), when asked for
        if self._u_replay_path:
            self._u_rep = dict(path=self._u_replay_path, bytes=0, buf=[], passes=0, done=False,
                               header=dict(format=U_REPLAY_FORMAT, d=int(self.d_t), delta=uu.delta, delta_name=delta, client_on=uu.client_on,
                                           path=uu.path, stale_max=uu.stale_max, act_lat=self._u_L, h_max=gu.H_MAX, entries=U_ENTRIES, slots=U_SLOTS,
                                           refresh=int(self.d_t), b_max=gu.B_MAX, budget=gu.BUDGET, frac=gu.FRAC, eta_f=gu.ETA_F,
                                           causes=list(gu.CAUSE)))
            v6 = self._u6.unit_inputs() if self._u6 is not None else None   # U v6: its inputs to the unit (none: U v4's format)
            if v6 is not None:
                self._u_rep["header"]["v6"] = dict(v6, theta_q20=uu.theta)

    def _sx_price(self, low) -> int:
        """(sx library): S + XS of a lowered script in rounds, by the profile builder's own functions (`wager2.site_profiles`: the
        script's duration, and its settlement from `settle_rounds` over `script_windows`) at this run's d, path and decoder model."""
        from .u_tables import script_windows, settle_rounds
        ws, end = script_windows(low, int(self.d_t), 0, self._u_mu)
        return int(low.duration) + int(settle_rounds(ws, end, int(self.path)))

    def _u_mu(self, volume) -> float:
        """The latency table's mean service of a volume's class, in rounds (the mean of max(1, ceil(sample)); the pool's
        and the tables' mu).  The class is the table's rule, clamped to its largest class."""
        t = self._eta_table
        cls = min(max(t.floor, math.ceil(volume / t.d)), t.classes[-1])
        mu = self._u_mu_cache.get(cls)
        if mu is None:
            mu = self._u_mu_cache[cls] = sum(r * p for r, p in t.pmf_rounds(cls).items())
        return mu

    def _u_installed(self, v: Value, fr: DeferFrame) -> bool:
        """A prediction m = 1 of the decision client in use under H's install rule."""
        pr, m, _c, _rc = self._dview(v)
        if pr < 0 or not m:
            return False
        t0 = fr.reg_round if pr <= fr.reg_round else pr + H_INSTALL
        return self.current_round >= t0

    def _runnable_pending(self, dec, limit: int) -> int:
        """Pending full-decode windows whose predecessors have all decoded or speculated (upstream's start test), counted
        oldest first up to `limit`: min(limit, their number). a capped decoder keeps them as a set
        (`_RunnableSet`); otherwise the scan."""
        if limit <= 0:
            return 0
        rq = _RunnableSet.of(dec)
        if rq is None:
            return _runnable_scan(dec, limit)
        return min(limit, len(rq.run))

    def _u_unbuilt_parts(self, p: int) -> list:
        """The producer's rounds not yet in a decoder window, as rule 30's waiting-for-data windows (froq.verdict_eta's
        reader): [(end, mu, predecessor tasks)], gathered once per round."""
        now = self.current_round
        if self._u_unbuilt_round != now:
            self._u_unbuilt_round, self._u_unbuilt = now, {}
            sim = LINK.get("simulator"); dec = LINK.get("decoder")
            wm = getattr(sim, "_window_manager", None) if sim is not None else None
            if wm is not None:
                d = self.d_t
                for wdw, idx in wm._unconstructed_window_indices.items():
                    end, vol = 0, sum(b.duration for b in wdw.buffer_regions)
                    for cr in wdw.commit_region:
                        e = cr.round_start + cr.duration
                        vol += cr.duration
                        if not cr.discard_after:
                            e += d
                            if not any(b.patch == cr.patch and b.round_start == cr.round_start + cr.duration for b in wdw.buffer_regions):
                                vol += d
                        end = max(end, e)
                    preds = tuple(x for x in wm.window_dag.predecessors(idx) if dec is not None and dec._get_task_or_none(x) is not None)
                    for i in wdw.parent_instr_idx:
                        if i >= 0:
                            self._u_unbuilt.setdefault(i, []).append((end, self._u_mu(vol), preds))
                b = wm.window_builder
                groups: dict = {}
                for ri in b._waiting_rounds:
                    r = b._all_rounds[ri]
                    if r is not None and r.instruction_idx >= 0:
                        g = groups.setdefault((r.instruction_idx, r.patch), [r.round, r.round])
                        g[0], g[1] = min(g[0], r.round), max(g[1], r.round)
                for (i, _patch), (r0, r1) in sorted(groups.items()):
                    e = max(r1 + 1, r0 + d)
                    self._u_unbuilt.setdefault(i, []).append((e + d, self._u_mu(e - r0 + d), ()))
        return self._u_unbuilt.get(p, [])

    def _u_dyn_clean(self, fr: DeferFrame, nxt) -> bool:
        """Section 3's subset rule on the live sets: what the rebased frame would hold while it waits, D-nci's frame holds."""
        from .u_tables import dnci_stop
        tmp = nxt.tmp
        stop_r, top_r = dnci_stop(tmp.r)
        if fr.rule == "lazy":
            stop_r = top_r                                                 # (U v4: a lazy frame carries the rebased walk lazily)
        held_d = fr.fence | {i for k in range(fr.stop, fr.c.F) for i in fr.members[k] if i not in self._defer_started}
        held_r = tmp.fence | {i for k in range(stop_r or 0, tmp.c.F) for i in tmp.members[k] if i not in self._defer_started}
        return held_r <= held_d

    def _u_ready(self, fr: DeferFrame, b: int, patches_in_use, zero_start: bool = False):
        """The readiness registers (section 4): (a_b, the workspace's release, None), or (None, None, the part that is
        unknown). a_b is the round in which b's prefix is complete and its script's cells are released; a prefix member
        not yet started counts with its causal lower bound (`_u_est`) plus its duration, and one that waits for a step
        from b on (which the ACT would hold) makes a_b unknown. zero_start: `_u_est`'s."""
        now = self.current_round
        pre, est = now, None
        for k in range(b):                                                 # prefix completion
            for i in fr.members[k]:
                t = self.schedule_instructions[i]; dur = self._instruction_durations[i]
                if t.end_round != -1:
                    pre = max(pre, t.end_round + 1)
                elif i in self._defer_started:
                    pre = max(pre, t.start_round + dur)
                elif any(x not in self._defer_started and fr.kof.get(x, -1) >= b for x in self._adj()[0].get(i, ())):
                    return None, None, "prefix"                            # (it waits for a step the ACT would hold)
                else:
                    if est is None:
                        est = self._u_est(fr, now, zero_start)             # (not started: its causal lower bound)
                    if est[i] == math.inf:
                        return None, None, "prefix"                        # (held: it cannot begin before the verdict)
                    pre = max(pre, est[i] + dur)
        ws = self._defer_workspace(fr, b)
        a_ws = now
        for c_ in ws:                                                      # script-cell release: the workspace
            if c_ not in self._active_patches and c_ not in patches_in_use:
                continue
            u = self._defer_first_unstarted(c_)
            if u is None or self.schedule_instructions[u].instruction.name != "DISCARD":
                return None, None, "workspace"
            a_ws = max(a_ws, now + 1)                                      # (the DISCARD runs in a dispatch, after this phase)
            for x in self._adj()[0].get(u, ()):
                if x not in self._defer_started:
                    return None, None, "workspace"
                tx = self.schedule_instructions[x]
                a_ws = max(a_ws, (tx.end_round + 1) if tx.end_round != -1 else tx.start_round + self._instruction_durations[x])
        low = self._defer_boundary(fr, b)[0]
        a_prog = now
        for c_ in set(low.cells) - set(ws):                                # ... and the program cells: their running instructions
            last = self._defer_last_started(c_)
            if last is not None and self.schedule_instructions[last].end_round == -1 and self._instruction_durations[last] > 0:
                a_prog = max(a_prog, self.schedule_instructions[last].start_round + self._instruction_durations[last])
        return max(pre, a_ws, a_prog), a_ws, None

    def _u_cells_free(self, fr: DeferFrame, b: int) -> bool:
        """The cell-ownership check (section 4): the script's cells are disjoint from other frames' reserved and pending cells."""
        low = self._defer_boundary(fr, b)[0]
        own = fr.target_ws if fr.state == "early_materialize" else frozenset()
        for c_ in low.cells:
            n = self._defer_target_cells.get(c_, 0) - (c_ in own)
            if n > 0 or (self._defer_locked.get(c_, set()) - {fr.vid}):
                return False
        return True

    # -- the scheduler's side of the contract: REGISTER, the epoch requests, the decisions that take effect
    def _u_round(self, fr: DeferFrame, patches_in_use):
        """U in a carrying frame's round, after D-nci's admission, while the verdict is unknown (U-contract.md section 8):
        first the ACTs pending for it -- an E1 whose b* the frame has entered is abandoned, an ACT whose effect round is
        now is executed or discarded at once; then, once, the REGISTER of an m = 1 prediction now in use, with E1 at b*
        if b* is before D-nci's stop s; then, once, E2 at s in the first round in which the frame has admitted every step
        up to s (D-nci's stop: from then on the frame only waits for the verdict) and s's readiness is known (rule 44b as
        amended after U v4's code gate: asking at admission keeps the ACT's lead over act_lat, and an unknown readiness,
        as for admitted steps that have not started when their start cannot be bounded, does not consume the only E2)."""
        now = self.current_round
        if fr.u_pending:
            keep = []
            for item in fr.u_pending:
                if item["effect"] <= now:
                    if not fr.hw_guard_on:                                 # rule 66: U acts once the frame's registration
                        keep.append(item)                                  # has completed (the decision decides only then)
                    elif not self._u_exec(fr, item, patches_in_use) and item.get("hw") == "wait":
                        keep.append(item)                                  # rule 66: its commit check has not completed
                elif item["epoch"] == 1 and self._defer_current(fr) > item["b"]:
                    self._u_discard(item, "abandonment", "boundary_passed")    # rule 41e: a step from b* on began
                else:
                    keep.append(item)
            fr.u_pending = keep
        if fr.state != "carry" or fr.rebased or fr.u_act is not None:
            return
        v = self.values[fr.vid]
        if v.vid not in self._u_reg:
            if self._u6 is not None:                                       # U v6: prediction registration (m_hat = 0 too
                if not self._u6.on_install(v, fr, patches_in_use):        # under 51c), the one reconsideration, REGISTER, E1
                    return
            else:
                if not self._u_installed(v, fr):
                    return
                self._u_register(v, fr)
                if self._u_reg[v.vid] is not None:
                    self._u_epoch1(fr, patches_in_use)
        r = self._u_reg[v.vid]
        if r is not None and not r["e2"] and fr.admitted == fr.stop:      # D-nci's stop: it admits no further step
            if fr.stop > U_B_MAX:
                r["e2"] = True
                self._u_not_asked(fr, 2, "beyond_16")
            elif self._u_ready(fr, fr.stop, patches_in_use)[0] is None:   # rule 44b as amended (U v4's code gate, 1): an
                self._u_stats["E2:wait_rounds:readiness"] += 1             # unknown readiness does not consume the only E2
            else:
                r["e2"] = True                                             # exactly once (U-contract.md section 8, duty 1)
                self._u_request(fr, 2, fr.stop, patches_in_use)

    def _u_epoch1(self, fr: DeferFrame, patches_in_use):
        """E1 at the installation (rule 41e): at b* if b* is before D-nci's stop s and within B_MAX, else counted."""
        b, cur = self._u_bstar(fr), self._defer_current(fr)
        self._u_install_note(fr, cur, b)
        if b < fr.stop and b <= U_B_MAX:
            self._u_request(fr, 1, b, patches_in_use)
        else:
            self._u_not_asked(fr, 1, "at_or_past_stop" if b >= fr.stop else "beyond_16")

    def _u_not_asked(self, fr: DeferFrame, epoch: int, why: str):
        """An epoch the scheduler does not ask, counted by reason; U v6 records it per value (rule 56a's "no request")."""
        self._u_stats["E%d:not_asked:%s" % (epoch, why)] += 1
        if self._u6 is not None and self._u6.instr:
            self._u6_noreq[fr.vid] = "E%d:%s" % (epoch, why)

    def _u6_register_mhat0(self, v: Value, fr: DeferFrame):
        """U v6, 51c: an m_hat = 0 prediction's REGISTER (its 1 - c, m_hat = 0), for the carry decision only: an entry in
        the engine's table, freed by its RESOLVE (or RECOVERY_DONE), and never a request."""
        self._u_reg[v.vid] = dict(cls=None, round=self.current_round, verdict=None, e2=True, mhat=0)
        self._u_msgs["REG"].append((v.vid, 0, _omc_of(self._dview(v)[2]), 0))
        self._u_stats["register:sent:mhat0"] += 1

    def _u_install_note(self, fr: DeferFrame, cur: int, b: int):
        """Rule 41f: the installation's boundary b* against the frame's current boundary and its stop."""
        st = self._u_stats
        st["install:b*-cur:%d" % min(b - cur, 9)] += 1
        st["install:stop-b*:%d" % max(-9, min(fr.stop - b, 9))] += 1
        if len(self._u_install) < U_LOG_MAX:
            self._u_install.append((fr.vid, self.current_round, cur, b, fr.stop, fr.c.F))

    def _u_est(self, fr: DeferFrame, r: int, zero_start: bool = False) -> dict:
        """A causal lower bound on the start round of each member of the frame's steps that has not started, from the
        scheduler's state in round r: no earlier than r (r + 1 if its step is not admitted: admission comes in a later
        deferral phase), nor before any predecessor can end -- an ended one at its end + 1, a running one at start +
        duration, one not started at its own bound + duration (r + duration outside the frame). A member of a step from
        the frame's admission limit on (D-nci's stop, while it carries) cannot begin before an ACT or the verdict: no
        bound (infinity), nor for what waits for it. Members in index order, the program's order, so a predecessor's
        bound comes first. zero_start: a zero-duration member (a DISCARD) starts in the round its
        predecessor ends, and a predecessor's end can be start + duration - 1, so its bound is a round earlier; the decision
        v1's own calls keep the bound as it was."""
        est, preds, si, dur = {}, self._adj()[0], self.schedule_instructions, self._instruction_durations
        for i in sorted(i for mem in fr.members for i in mem if i not in self._defer_started):
            if fr.kof.get(i, -1) >= fr.limit:
                est[i] = math.inf                                          # (held at the stop: `_defer_hold`'s defer_stop)
                continue
            e = r + (1 if i in fr.unadm else 0)
            for p in preds.get(i, ()):
                tp = si[p]
                if tp.end_round != -1:
                    q = tp.end_round + 1
                elif p in self._defer_started:
                    q = tp.start_round + dur[p]
                elif p in est:
                    q = est[p] + dur[p]
                else:
                    q = r + dur[p]
                if zero_start and dur[i] == 0:
                    q -= 1
                e = max(e, q)
            est[i] = e
        return est

    def _u_bstar(self, fr: DeferFrame) -> int:
        """Rule 41e's b*: the smallest boundary b >= the frame's current boundary whose steps cannot begin before round
        now + act_lat -- every member of the steps from b on has `_u_est` >= now + act_lat (a member that has started,
        before the frame or since, counts as begun).  At most the frame's limit, D-nci's stop s: the frame never passes
        it while it carries (the steps from s on wait for an ACT or the verdict), so b* <= s, and E1 is asked iff b* < s."""
        now, F = self.current_round, fr.c.F
        cur = self._defer_current(fr)
        est = self._u_est(fr, now)
        lb, LB = math.inf, [math.inf] * (F + 1)
        for j in range(min(F, fr.limit) - 1, cur - 1, -1):               # (from the limit on: never passed while it carries,
            lb = min([lb] + [est.get(i, -math.inf) for i in fr.members[j]])   # whatever started before the frame)
            LB[j] = lb
        return next(b for b in range(cur, F + 1) if LB[b] >= now + self._u_L)

    def _u_register(self, v: Value, fr: DeferFrame):
        """REGISTER (section 2): the site's class (its 17 records), 1 - c in Q0.20 and m_hat = 1. Only a frame whose walk
        is its site's static walk, at a site with a class, is registered: the records describe no other. (F11, paper
        07:80-81, "a value that finds no capacity stays out of the decision unit and runs as Wait-for-Decoder"): a site
        with no class (the 65th, or records wider than their fields) stops its frame where it stands -- it admits no
        further step, so the rest of its steps wait for its decoded verdict -- where let it carry on under its carry
        rule with no commit to come. The steps its carry rule admitted before REGISTER, as every frame's, stand."""
        st = self._u_stats
        if self._common is not None and v.vid not in self._common:      # outside the common set, no REGISTER (so no
            st["register:skipped:not_common"] += 1                       # request and no ACT); the carry rule stays
            self._common_outside.add(v.vid)
            self._u_reg[v.vid] = None
            return
        ok = self._u_tab.matches(v.vid, fr.c)
        cls = self._u_tab.site_class(v.vid, fr.rule or "no_cost_increase") if ok else None   # (rule 44a: the walk's class)
        if cls is None:
            st["register:skipped:" + ("no_class" if ok else "walk_differs")] += 1
            self._u_reg[v.vid] = None
            if ok:                                                         # (F11): no capacity, Wait-for-Decoder from here
                fr.limit = fr.stop = min(fr.limit, fr.stop, fr.admitted)  # (counted above: one REGISTER per value)
            return
        while self._u_loaded <= cls:                                     # CONFIG sel 6, before the class's first use
            for b in range(U_B_MAX + 1):
                self._uu.set_table(self._u_loaded, b, **self._u_tab.record(self._u_loaded, b))
            self._u_loaded += 1
        self._u_reg[v.vid] = dict(cls=cls, round=self.current_round, verdict=None, e2=False)
        self._u_msgs["REG"].append((v.vid, cls, _omc_of(self._dview(v)[2]), 1))
        st["register:sent"] += 1

    def _u_reconfidence(self, v: Value):
        """RECONFIDENCE (the SWIPER client's revision of c) for a registered value whose verdict is not yet known."""
        r = self._u_reg.get(v.vid)
        if r is not None and r["verdict"] is None:
            self._u_msgs["RC"].append((v.vid, _omc_of(v.confidence)))

    def _u_resolve(self, v: Value):
        """RESOLVE, in the first round the scheduler sees the verdict: 1 if the prediction was right."""
        r = self._u_reg.get(v.vid)
        if r is not None and r["verdict"] is None:
            r["verdict"] = int(bool(self._dview(v)[1]) == bool(v.truth))
            self._u_msgs["RES"].append((v.vid, r["verdict"]))

    def _u_request(self, fr: DeferFrame, epoch: int, b: int, patches_in_use):
        """One epoch request with the contract's fields (U-contract.md section 3, EPOCH). ready is a_b, and no
        earlier than now + act_lat, the round the decision takes effect; at E1 the stop's readiness is the stop's own a_s
        (no earlier than now + act_lat), so E1 is compared with E2's gain at s as E2 would score it (rule 44c)."""
        now, L = self.current_round, self._u_L
        if self._u_noact:                                                  # U6-noact: delta = inf asks nothing, so its u_xs
            self._u_not_asked(fr, epoch, "noact")                          # holds no request that could never act
            return
        if self._defer_boundary(fr, b)[0] is None:
            self._u_not_asked(fr, epoch, "no_script")                      # (a boundary without a script: nothing to ask)
            return
        if (fr.vid, epoch) in self._u_suppress:                            # a counterfactual: the request
            self._u_not_asked(fr, epoch, "suppressed")                     # is not made, as if U answered CARRY to it
            return
        if not self._u_epochs[epoch]:                                      # ablation (rule 33a, 1 and 2): the epoch is off
            self._u_not_asked(fr, epoch, "off")
            return
        a, _ws, _why = self._u_ready(fr, b, patches_in_use)
        a_s = (self._u_ready(fr, fr.stop, patches_in_use)[0]               # E1: the stop's readiness (rule 44c); with E2 off
               if epoch == 1 and self._u_epochs[2] else None)               # none, so E1 has no E2 gain to compare with (33a, 2)
        q = dict(vid=fr.vid, epoch=epoch, b=b, cur=min(self._defer_current(fr), 31), reach=min(self._defer_reachable(fr), 31),
                 ready_v=a is not None, ready=(max(a, now + L) if a is not None else 0),
                 cells_free=self._u_cells_free(fr, b), s=(min(fr.stop, 31) if epoch == 1 else b),
                 s_ready_v=a_s is not None, s_ready=(max(a_s, now + L) if a_s is not None else 0),
                 roots=None, held=self._u_held(fr))
        if self._u6 is not None and self._u6.eta_floor:                     # U v6, 51b: the built roots, and the floor while unbuilt
            q.update(self._u6.request_fields(self.values[fr.vid]))
        else:
            q["roots"] = self._u_roots(self.values[fr.vid])
        if self._u6 is not None and self._u6.instr:
            q["_ready_why"] = _why                                          # (rule 56a's refusal causes: records only; without
            if "_roots_why" not in q:                                       #  51b, U v4's roots, classified as U v6 classifies
                q["_roots_why"] = self._u6.roots(self.values[fr.vid])[2]    #  them: a capacity limit is logged as such)
        self._u_send(q)
        self._u_stats["E%d:requests" % epoch] += 1

    def _u_send(self, q: dict):
        """An EPOCH request sent this round. Until its answer arrives (its effect round, act_lat rounds) it is
        outstanding, and its potential excess max(0, demand_b - held) enters the carry rule's idle count: a pending ACT and a new lazy reservation cannot overcommit."""
        if self._hw is not None and q.get("roots"):
            q["_gens"] = [self._u_gen.get(s, 0) for s in q["roots"]]      # rule 66: the roots' generations when formed
        self._u_reqs.append(q)
        t = self._uu.tab.get((self._u_reg[q["vid"]]["cls"], q["b"]))
        x = max(0, (int(t.demand) if t is not None else 0) - int(q.get("held", 0)))
        if x:
            self._u_out.append((self.current_round + self._u_L, x))
            self._u_out_total += x

    def _u4_outstanding(self) -> int:
        """The outstanding requests' potential excess this round (`_u_send`): those whose answers have not arrived."""
        now, q = self.current_round, self._u_out
        while q and q[0][0] <= now:                                        # (arrival rounds never decrease)
            self._u_out_total -= q.popleft()[1]
        return self._u_out_total

    def _u_roots(self, v: Value) -> list:
        """The slots of the windows the value's verdict waits on last: among the producer's decode windows not yet
        verified, those with no successor among them (each of the others is an unverified ancestor of one, so its ETA is
        in theirs).  None, so not estimable, while some of the producer's rounds are in no pool window yet, while one of
        its windows is outside the register file, or once all are verified.  Each request's roots are classified in the
        counters (`roots:*`, a diagnostic)."""
        dec = LINK.get("decoder")
        p = v.producer_idx
        st = self._u_stats
        if dec is None or self._u_unbuilt_parts(p):
            st["roots:unbuilt"] += 1
            return []
        T = [t for t in sorted(dec._instruction_tasks.get(p, ())) if not dec._is_verified_task(t, treat_none_as_true=True)]
        if not T:
            st["roots:none_unverified"] += 1
            return []
        if not all(self._u_in_file(t) for t in T):
            st["roots:outside_file"] += 1
            return []
        Ts, g = set(T), dec._window_idx_dag
        out = [self._u_slot[t] for t in T if not any(y in Ts for y in g.successors(t))]
        uu = self._uu
        st["roots:" + ("over_4" if len(out) > 4 else "invalid" if not all(uu.valid[s] for s in out) else "valid")] += 1
        return out

    def _u_exec(self, fr: DeferFrame, item: dict, patches_in_use) -> bool:
        """An ACT at its effect round (U-contract.md section 8, duties 2-5): executed only if the value is still undecided
        and b still at hand -- not passed (E1: else abandoned), D's stop unchanged, b's readiness still known, b usable,
        its cells free, its rebased contract buildable and clean on the live sets; else discarded at once, never waited
        for.  Discards are counted by cause (rule 41b: verdict first, deadline, revalidation; 41f: and abandonment)."""
        epoch, b = item["epoch"], item["b"]
        why, cat, nxt = None, "revalidation", None
        if fr.state != "carry" or fr.rebased or fr.u_act is not None:
            why, cat = "acting", "deadline"                                # (the frame took another ACT before this one's effect)
        elif b < self._defer_current(fr):
            why, cat = "boundary_passed", ("abandonment" if epoch == 1 else "revalidation")
        elif b > fr.stop or (epoch == 2 and b != fr.stop):
            why = "stop_changed"
        elif self._u_ready(fr, b, patches_in_use)[0] is None:
            why = "readiness"
        elif not fr.feas[b] or self._defer_ws_blocked(fr, b, patches_in_use, splice=False):
            why = "pinned"
        elif not self._u_cells_free(fr, b):
            why = "cells_reserved"
        else:
            nxt, w2, _perm = self._h_prepare_core(fr, b, patches_in_use)
            if nxt is None:
                why = "rebase:" + w2
            elif not self._u_dyn_clean(fr, nxt):
                why = "rebase:not_clean"
            elif b < fr.admitted and not self._resv_back_ok(fr, b):
                why = "resv_floor"                                         # (SER-R: no floor for the take-back; the ACT is dropped)
        if why is not None:
            self._u_discard(item, cat, why)
            return False
        if self._hw is not None and item.get("hw") != "done":              # rule 66: the ACT takes effect when the reservation
            if item.get("hw") != "wait":                                   # manager's commit check and reserve completes
                item["hw"] = "wait"
                low = self._defer_boundary(fr, b)[0]
                item["ev"] = self._hw.submit("commit_check_and_reserve", lambda item=item: item.__setitem__("hw", "done"),
                                             cells=dict(script_cells=(low.cells if low is not None else ()), own_cells=(),
                                                        workspace=self._defer_workspace(fr, b)), owner=fr.vid)
            if item.get("hw") != "done":
                return False                                               # (kept pending: delayed, never discarded for it)
        self._u_act(fr, item, nxt)
        self._h_advance(fr, patches_in_use)
        return True

    def _u_spliced(self, rid: int):
        """Rule 66: an executed ACT keeps its reservation, slot and demand pending until
        its splice executes, however late, and the reservation then runs its duration (S_b + R_b) from the splice."""
        r_ = self._u_resv.get(rid)
        if r_ is None or not r_.get("pending"):
            return
        now = self.current_round
        r_["pending"], r_["end"] = False, max(r_["end"], now + r_["dur"])
        if now > r_["act_from"]:
            self._hw.stats["act_splices_delayed"] += 1
            self._hw.stats["act_splice_delay_rounds"] += now - r_["act_from"]

    def _u_discard(self, item: dict, cat: str, why: str):
        assert item.get("hw") != "discarded", ("U: an ACT discarded twice", item["vid"], item["epoch"], item["b"])
        if self._hw is not None and item.get("hw") == "wait":
            if item.get("ev") is not None:
                self._hw.cancel_event(item["ev"])                          # rule 66: its queued commit check goes with it
            self._hw.stats["act_checks_cancelled"] += 1
        item["hw"], item["ev"] = "discarded", None                         # (no wait state outlives it: settled once)
        st = self._u_stats
        st["E%d:discard:%s" % (item["epoch"], cat)] += 1
        st["E%d:discard:%s:%s" % (item["epoch"], cat, why)] += 1
        self._u_release(item["rid"], "discard")
        self._u_round_disc.append([item["vid"], item["epoch"], item["b"], cat, why])
        if self._u6 is not None and self._u6.instr:                        # U v6, rule 56a: an accepted ACT abandoned
            self._u6._log("discards", (item["vid"], item["epoch"], item["b"], cat, why))

    def _u_act(self, fr: DeferFrame, item: dict, nxt):
        """ACT(b) (section 7): H's choice bookkeeping with target b and the prepared rebased contract; no admission freeze."""
        now, b, epoch = self.current_round, item["b"], item["epoch"]
        if b < fr.admitted:
            self._defer_unadmit(fr, b)                                     # (admitted steps that have not begun)
        fr.target = fr.limit = fr.h_je = b
        fr.h_trigger_round = fr.h_choice_round = now
        fr.target_ws = self._defer_workspace(fr, b)
        for c_ in fr.target_ws:
            self._defer_target_cells[c_] = self._defer_target_cells.get(c_, 0) + 1
        for i in nxt.new_holds:
            self._wq_hadd(i, fr.vid)
        for e in nxt.extra_claims:
            self._defer_claim[e] = fr.vid
        for i in nxt.extra_members:
            self._defer_member[i] = fr.vid
        fr.h_next = nxt
        if self._r8:
            self._r8_pend_reb(fr, nxt)                                     # (rule (a2)'s guard on the revised cells from the ACT, as at a
        log = len(self._u_acts)                                            #  the decision commit or H's choice)
        self._u_acts.append(dict(vid=fr.vid, epoch=epoch, b=b, decided=item["decided"], effect=now, cur=self._defer_current(fr),
                                 stop=fr.stop, splice=None, done=None, how=None, cancel=None))
        fr.u_act = dict(epoch=epoch, b=b, stop=fr.stop, round=now, rid=item["rid"], log=log)
        r_ = self._u_resv.get(item["rid"])
        if r_ is not None and r_.get("pending") and self._hw is None:      # (the mode off: round 4's duration from the ACT; under
            r_["pending"], r_["end"] = False, max(r_["end"], now + r_["dur"])  # rule 66 it runs from the splice, `_u_spliced`)
        fr.state = "early_materialize"
        self._u_stats["E%d:acted" % epoch] += 1
        self._h_log_event(fr, "u_act", (epoch, b))

    def _u_cancel(self, fr: DeferFrame, cat: str, why: str):
        """An executed ACT before its splice (duties 3-4), back to D-nci's carry: the target, the holds, the prepared
        contract and the reservation released."""
        self._h_release_next(fr); self._h_release_target(fr)
        if self._hw is not None:
            self._hw_reset(fr)                                             # rule 66: its queued splice event goes
        self._u_unact(fr, cat, why)
        fr.state, fr.limit, fr.target, fr.h_je = "carry", fr.stop, -1, -1
        self._defer_admit(fr)

    def _u_unact(self, fr: DeferFrame, cat: str, why: str):
        a = fr.u_act
        st = self._u_stats
        st["E%d:cancel:%s" % (a["epoch"], cat)] += 1                       # (an executed ACT, before its splice)
        st["E%d:cancel:%s:%s" % (a["epoch"], cat, why)] += 1
        self._u_release(a["rid"], "cancel")
        self._u_acts[a["log"]]["cancel"] = why
        self._u_round_disc.append([fr.vid, a["epoch"], a["b"], cat, why])
        self._h_log_event(fr, "u_cancel", why)
        fr.u_act = None

    def _u_splice_check(self, fr: DeferFrame, patches_in_use):
        """Duty 3, the splice revalidation: what the scheduler supplied still holds -- D's stop, the cells free, the
        readiness known -- or why not."""
        if fr.stop != fr.u_act["stop"]:
            return "stop_changed"
        if not self._u_cells_free(fr, fr.target):
            return "cells_reserved"
        if self._u_ready(fr, fr.target, patches_in_use)[0] is None:
            return "readiness"
        return None

    def _u_settle_pending(self):
        """The deferral phase's end: ACTs whose frame did not take them -- the verdict came first (or the frame ended),
        or their effect round passed while the frame was not carrying (the deadline)."""
        now = self.current_round
        for vid in sorted(self._u_pending_vids):
            fr = self._frames[vid]
            if fr.u_pending:
                if vid not in self._live_frames or self.values[vid].delivered:
                    for item in fr.u_pending:
                        self._u_discard(item, "verdict_first", "verdict")
                    fr.u_pending = []
                else:
                    keep = []
                    for item in fr.u_pending:
                        if item["effect"] <= now and item.get("hw") != "wait" and fr.hw_guard_on:
                            self._u_discard(item, "deadline", "not_carrying")
                        else:
                            keep.append(item)                              # (rule 66: a commit check still queued, or the
                                                                           # frame's registration, is delayed)
                    fr.u_pending = keep
            if not fr.u_pending:
                self._u_pending_vids.discard(vid)

    # -- rule 41c: duration-wide headroom as a reservation, split between U and the pool (amended 22:20); U v4's lazy
    #    frames' materialization demand, counted once
    def _u_reserve(self, vid: int, b: int, held: int = 0) -> int:
        """An ACT decided now (pass k, the end of round r) reserves demand_b cores until its early script and repair can
        have ended, at most act_lat + S_b + R_b rounds after the decision.  The pool counts it from the ACT's effect round
        r + act_lat on (in the strip starts of that round and the status latched for pass k + act_lat); before that, U's
        own history of its ACTs (golden `act_hist`) debits it, so the pool does not count it twice.  U v4 (rule 44a,
        option B): an ACT on a lazy frame keeps the frame's reservation (`_u4_lazy_reserve`) and reserves only its excess
        over it, max(0, demand_b - held), with the `held` its request carried, as the unit debits it."""
        t = self._uu.tab[(self._u_reg[vid]["cls"], b)]
        now, L = self.current_round, self._u_L
        dem = max(0, int(t.demand) - held)
        end = now + L + int(t.S) + int(t.R)
        rid = self._u_resv_n
        self._u_resv_n += 1
        if self._hw is not None:
            self._hw.take_slot(("u", rid))                                 # rule 66: its slot (checked free at the ACT)
        self._u_resv[rid] = dict(vid=vid, kind="act", lazy=0, excess=dem, act_from=now + L, start=now + L, end=end, pool=0, counted=False,
                                 pending=True, dur=int(t.S) + int(t.R))   # (pending: kept until the ACT executes or goes)
        self._u_round_resv.append(["new", vid, dem, now + L, end])
        if held:
            self._u_stats["reservation:act_on_lazy"] += 1
        return rid

    def _u4_lazy_reserve(self, fr: DeferFrame):
        """Rule 44a (option B): a lazy frame's materialization demand (demand_lazy of its site) is reserved from its
        registration until it materializes (D's splice or an ACT's early one) or drops.  The scheduler chooses the carry
        rule itself, so no decision latency applies: the pool counts it at once (and U sees it in the pool's status)."""
        lam = self._u4_demand_lazy(fr.vid)
        if lam <= 0:
            return
        if self._hw is not None and not self._hw.take_slot(("u", self._u_resv_n)):
            return                                                         # rule 66: no slot, the lazy reservation is not made
        now = self.current_round
        rid = self._u_resv_n
        self._u_resv_n += 1
        self._u_resv[rid] = dict(vid=fr.vid, kind="lazy", lazy=lam, excess=0, act_from=None, start=now, end=None, pool=lam, counted=True)
        self._u_resv_total += lam
        fr.u_lazy = rid
        if self._u_on:                                                    # (the replay's reservation events: U on only)
            self._u_round_resv.append(["lazy", fr.vid, lam, now, -1])
        self._u_stats["reservation:lazy"] += 1
        self._u_peaks["reserved"] = max(self._u_peaks["reserved"], self._u_resv_total)

    def _u4_demand_lazy(self, vid: int) -> int:
        """demand_lazy of the value's site (rule 44a; `UTables.demand_lazy`, kept per site).  The static walk it needs is
        kept only for U's REGISTER (U on), which the frame's end, or its refusal, drops."""
        d = int(self._u_tab.demand_lazy(vid))
        if not self._u_on:
            self._u_tab.forget(vid)
        return d

    def _u_held(self, fr: DeferFrame) -> int:
        """The reservation a frame holds, for its EPOCH requests (rule 44a, 8 bits): its lazy reservation while it lasts;
        0 otherwise."""
        r = self._u_resv.get(fr.u_lazy) if fr.u_lazy is not None else None
        return min(255, r["lazy"]) if r is not None else 0

    def _u_release(self, rid: int, why: str):
        r = self._u_resv.pop(rid, None)
        if r is None:
            return
        if self._hw is not None:
            self._hw.free_slot(("u", rid))
        if r["pool"]:
            self._u_resv_total -= r["pool"]
        if not r["counted"]:
            self._u_stats["reservation:never_counted"] += 1               # (ended within act_lat: U's history alone debited it)
        self._u_stats["reservation:end:" + ("lazy:" if r["kind"] == "lazy" else "") + why] += 1
        if self._u_on:
            self._u_round_resv.append(["end", r["vid"], r["lazy"] + r["excess"], why])

    @staticmethod
    def _u_pool_count(r: dict, now: int) -> int:
        """What the pool counts of a reservation in round `now`: a lazy frame's from its registration, an ACT's demand
        (on a lazy frame, its excess) from the ACT's effect round."""
        return (r["lazy"] if now >= r["start"] else 0) + (r["excess"] if (r["act_from"] is not None and now >= r["act_from"]) else 0)

    def _u_resv_tick(self):
        """The pool's side in round `current_round`, before its strip starts and the pass's latch: each reservation's
        count now (`_u_pool_count`); those past their last round end."""
        now = self.current_round
        for rid in [rid for rid, r in self._u_resv.items() if r["end"] is not None and r["end"] < now and not r.get("pending")]:
            self._u_release(rid, "duration")                               # (an ACT still pending keeps its reservation)
        tot = 0
        for r in self._u_resv.values():
            r["pool"] = self._u_pool_count(r, now)
            r["counted"] = r["counted"] or now >= r["start"]
            tot += r["pool"]
        self._u_resv_total = tot
        self._u_peaks["reserved"] = max(self._u_peaks["reserved"], tot)

    def _u4_committed(self) -> int:
        """The reservations the pool counts now (rule 44a): the lazy frames' from registration, the ACTs' from their effect
        rounds, which is also what the scheduler knows."""
        now = self.current_round
        return sum(self._u_pool_count(r, now) for r in self._u_resv.values())

    def _u_unstarted(self) -> int:
        """Rule 50c's accounting: the reservations the pool counts now, less their decode work that has started (the busy
        count holds it, so nothing is counted twice) -- an ACT's demand less its windows started so far; a lazy frame's
        whole demand, which it holds until it materializes, before its script's decodes start."""
        now = self.current_round
        return sum(max(0, self._u_pool_count(r, now) - r.get("started", 0)) for r in self._u_resv.values())

    def _u6_started(self, dec, started):
        """Rule 50c's accounting: a window that started decoding this round and belongs to the script or repair of a value
        with an ACT reservation (its parent instructions' owner) moves one core of that reservation into the busy count."""
        acts = {}
        for rid, r in self._u_resv.items():
            if r["kind"] == "act":
                acts[r["vid"]] = r                                     # (a value's latest ACT reservation)
        if not acts:
            return
        for t in started:
            task = dec._get_task_or_none(t)
            if task is None:
                continue
            for vid in {self._recovery_owner.get(i) for i in task.window.parent_instr_idx}:
                r = acts.get(vid)
                if r is not None:
                    r["started"] = r.get("started", 0) + 1
                    self._u_stats["reservation:act_window_started"] += 1

    def _u4_refused(self, v: Value, why: str = ""):
        """A value refused a frame after its carry rule was chosen: the static walk kept for its REGISTER is dropped; U v6
        records the decision apart."""
        if self._u4 and self._u_on:
            self._u_tab.forget(v.vid)
        if getattr(self, "_u6", None) is not None:
            self._u6.decision_refused(v.vid, why)

    def _u4_rule(self, v: Value) -> str:
        """Rule 44a's carry rule at v's registration: lazy if the pool's idle cores -- B less the busy full decodes (a strip
        job's core counts as available under rule 42a), less the reservations the pool counts, less every outstanding
        EPOCH request's potential excess (44a as amended at 04:58) -- are at least h x demand_lazy of v's site, D-nci's
        rule otherwise.  h = 0: always lazy; h = inf: always D-nci's; unlimited decoders: lazy."""
        h = self._u4_h
        if h is None:
            return "no_cost_increase"
        if h == 0:
            return "lazy"
        dec = LINK.get("decoder")
        c = self._u_cores(dec) if dec is not None else None
        if not c:
            return "lazy"
        busy = len(dec._active_window_progress)
        if self._pool is not None and not self._pool.preemptive:
            busy += self._pool.busy(dec._current_round)
        idle = c - busy - self._u4_committed() - (self._u4_outstanding() if self._u_on else 0)
        return "lazy" if idle >= h * self._u4_demand_lazy(v.vid) else "no_cost_increase"

    # -- the decoder pool's side: slots, window events and status
    def _u_task_mu(self, dec, t) -> int:
        mu = self._u_mu_w.get(t)
        if mu is None:
            w_ = dec._get_task(t).window                                   # (--h-depth: the window's syndrome rounds)
            mu = self._u_mu_w[t] = int(math.floor(self._u_mu(w_.total_spacetime_volume() - _hd.window_fills(self._h_fill, w_)) * 256 + 0.5))
        return mu

    def _u_cores(self, dec):
        """The full decoders (the pool's B; None: unlimited)."""
        return self._pool.B if self._pool is not None else (dec.max_parallel_processes or None)

    def _u_busy(self, dec) -> int:
        """The busy full decoders' remaining mean service, Q24.8: the sum of max(0, mu - elapsed)."""
        now = dec._current_round
        return sum(max(0, self._u_task_mu(dec, t) - ((now - dec._get_task(t).decoding_start_time) << 8)) for t in dec._active_window_progress)

    def _u_wait(self, dec, ahead: int, busy=None) -> int:
        """Rule 30's queue term in Q24.8: (the mu of the windows ahead + the busy decoders' remaining mean service) / c,
        rounded down; 0 with unlimited decoders."""
        c = self._u_cores(dec)
        if not c:
            return 0
        return (ahead + (self._u_busy(dec) if busy is None else busy)) // c

    def _u_alloc(self, t):
        """The lowest slot that may be used (never used, or its window verified and named by no unverified window)."""
        if not self._u_free:
            return None
        s = heapq.heappop(self._u_free)
        old = self._u_owner[s]
        if old is not None:
            self._u_slot.pop(old, None)
        self._u_owner[s], self._u_svf[s], self._u_slot[t] = t, False, s
        self._u_gen[s] = self._u_wgen[t] = self._u_gen.get(s, 0) + 1
        return s

    def _u_in_file(self, t) -> bool:
        s = self._u_slot.get(t)
        return s is not None and self._u_owner[s] == t

    def _u_unverified_preds(self, dec, t) -> list:
        """The window's predecessors whose commit it waits on: those not verified (a window not yet constructed is not)."""
        out = []
        for p in sorted(dec._window_idx_dag.predecessors(t)):
            if dec._get_task_or_none(p) is None or not dec._is_verified_task(p):
                out.append(p)
        return out

    def _u_own_now(self, dec, t, now: int, ahead=None) -> int:
        """Rule 30's estimate of the window's own decode, now, as an absolute round in Q24.8: decoded, the round its end
        was seen; in service, max(now, start + mu); queued, now + Wq(the queued windows ahead) + mu."""
        task = dec._get_task(t)
        mu = self._u_task_mu(dec, t)
        if task.decoding_completion_time != -1:
            return (task.decoding_completion_time + 1) << 8
        if task.decoding_start_time != -1:
            return max(now << 8, (task.decoding_start_time << 8) + mu)
        if ahead is None:
            ahead = self._u_qbit.prefix(t)                                 # (the queued windows of a smaller index)
        return (now << 8) + self._u_wait(dec, ahead) + mu

    def _u_emit(self, op, t, s, own, preds, now):
        """NEW or UPD: the window's stamp is the pass now + 1, from which it may go stale (the stale scan's candidates)."""
        self._u_wins.append((op, s, own, preds, t))
        self._u_last[t] = now
        self._u_refresh.append((now + self.d_t, t, now))
        heapq.heappush(self._u_stale_q, (now + 1, s))

    def _u_win_done(self, t, own: int):
        """DONE: the window is decoded; its estimate is final and never goes stale."""
        if self._u_in_file(t):
            self._u_wins.append(("DONE", self._u_slot[t], own, (), t))

    def _u_win_new(self, dec, t, own: int, now: int):
        """NEW: the window enters the register file with its unverified predecessors, all already in it."""
        s = self._u_slot[t]
        preds = [self._u_slot[p] for p in self._u_unverified_preds(dec, t)]
        named = set(preds[:4])
        for q in named:
            self._u_pin[q] += 1
        self._u_named[t] = named
        if self._u6 is not None:                                         # (U v6: which pass first holds it, MED 3)
            self._u_newr[t] = now
        self._u_emit("NEW", t, s, own, tuple(preds), now)

    def _u_win_upd(self, t, own: int, now: int):
        if self._u_in_file(t):
            self._u_emit("UPD", t, self._u_slot[t], own, (), now)

    def _u_win_commit(self, dec, t, now: int):
        """COMMIT, or for a window still outside the file: it leaves the waiting list, and so stops holding back the
        windows that wait for it."""
        if not self._u_in_file(t):
            if t in self._u_waits_on:
                for p in self._u_waits_on.pop(t):
                    self._u_blocked_by.get(p, set()).discard(t)
                self._u_wait_cnt.pop(t, None)
                self._u_unblock(t)
            return
        s = self._u_slot[t]
        self._u_wins.append(("COMMIT", s, 0, (), t))
        self._u_svf[s] = True
        for q in self._u_named.pop(t, ()):
            self._u_pin[q] -= 1
            if self._u_pin[q] == 0 and self._u_svf[q]:
                heapq.heappush(self._u_free, q)
        if self._u_pin[s] == 0:
            heapq.heappush(self._u_free, s)

    def _u_unblock(self, p):
        """The window p is in the file, or verified: the windows waiting for it need one predecessor fewer."""
        for y in self._u_blocked_by.pop(p, ()):
            self._u_waits_on[y].discard(p)
            self._u_wait_cnt[y] -= 1
            if not self._u_wait_cnt[y]:
                heapq.heappush(self._u_enter_q, y)

    def _u_decoder_step(self, dec, completed, verified, resets):
        """The pool's events of the decoder's step, in round k (the decoder's round after it): a decode's end (DONE, own =
        k), a verification (COMMIT), a reset by a failed speculation (UPD, queued again)."""
        now = dec._current_round
        for t in sorted(completed):
            self._u_win_done(t, now << 8)
        for t in sorted(verified):
            self._u_win_commit(dec, t, now)
        for t in sorted(set(resets)):
            if dec._get_task_or_none(t) is None or t not in dec._pending_decode_tasks or t in self._u_queued:
                continue
            mu = self._u_task_mu(dec, t)
            self._u_win_upd(t, self._u_own_now(dec, t, now), now)
            self._u_queued[t] = mu; self._u_qbit.add(t, mu)

    def _u_pool_events(self, dec, new_windows):
        """After the round's starts (section 5): UPD for the windows queued before that started (own = start + mu); the
        refresh (a re-estimate) of windows in service whose last update is d rounds old; then the round's new windows join the
        waiting list, and every window waiting only for a slot enters the register file (NEW), oldest first, as long as
        slots are free.  A window waits until each of its unverified predecessors is in the file (or verified), so the
        NEWs follow the window DAG and a reused slot is never named before its new window's NEW."""
        now = dec._current_round
        log = dec._pending_decode_tasks.started_log
        started = set(log)
        log.clear()
        if started and self._u6 is not None and self._u6.slack_mode == "ewma":
            self._u6_started(dec, started)
        new = sorted(w.window_idx for w in new_windows)
        for t in sorted(started.difference(new)):
            mu = self._u_queued.pop(t, None)
            if mu is not None:
                self._u_qbit.add(t, -mu)
            self._u_win_upd(t, (now << 8) + self._u_task_mu(dec, t), now)
        rq = self._u_refresh
        while rq and rq[0][0] <= now:
            _due, t, last = rq.popleft()
            task = dec._get_task_or_none(t)
            if (self._u_last.get(t) == last and self._u_in_file(t) and not dec._is_verified_task(t)
                    and task.decoding_start_time != -1 and task.decoding_completion_time == -1):
                self._u_win_upd(t, self._u_own_now(dec, t, now), now)
                self._u_stats["pool:refresh"] += 1
        busy, own = None, {}
        for t in new:                                                      # rule 30: the queued windows ahead are those of a
            mu = self._u_task_mu(dec, t)                                   # smaller index (windows arrive out of index order)
            if t in started:
                own[t] = (now << 8) + mu
                continue
            if busy is None:
                busy = self._u_busy(dec)                                   # (once a round)
            own[t] = (now << 8) + self._u_wait(dec, self._u_qbit.prefix(t), busy) + mu
            self._u_queued[t] = mu; self._u_qbit.add(t, mu)
        for t in new:
            wait = {p for p in self._u_unverified_preds(dec, t) if not self._u_in_file(p)}
            if wait:
                self._u_wait_cnt[t], self._u_waits_on[t] = len(wait), wait
                for p in wait:
                    self._u_blocked_by.setdefault(p, set()).add(t)
                self._u_stats["pool:waited_for_predecessor"] += 1
            else:
                self._u_wait_cnt[t], self._u_waits_on[t] = 0, set()
                heapq.heappush(self._u_enter_q, t)
        ready = self._u_enter_q
        while ready:
            t = ready[0]
            if t not in self._u_waits_on:                                  # (verified while waiting)
                heapq.heappop(ready); continue
            if self._u_alloc(t) is None:
                if t not in self._u_slot_waiting:
                    self._u_slot_waiting.add(t); self._u_stats["pool:waited_for_slot"] += 1
                break
            heapq.heappop(ready)
            del self._u_waits_on[t], self._u_wait_cnt[t]
            self._u_slot_waiting.discard(t)
            if t not in own:
                self._u_stats["pool:entered_late"] += 1
            self._u_win_new(dec, t, own[t] if t in own else self._u_own_now(dec, t, now), now)
            if dec._get_task(t).decoding_completion_time != -1:
                self._u_win_done(t, (dec._get_task(t).decoding_completion_time + 1) << 8)   # (decoded before it entered)
            self._u_unblock(t)
        pk = self._u_peaks
        pk["slots"] = max(pk["slots"], len(self._u_slot))
        pk["outside"] = max(pk["outside"], len(self._u_waits_on))
        pk["unverified"] = max(pk["unverified"], len(dec._pending_decode_tasks) + len(dec._active_window_progress)
                               + len(dec._decoded_unverified_tasks))

    def _u_pool_status(self, dec) -> dict:
        """The pool's status latched at the strobe (pl_fe, pl_idle): no full-decode window waits, ready or blocked; the cores
        not running a full decode, less the reserved cores (rule 41c) -- a strip job's core counts as available under
        rule 42a's preemption, and as busy in the non-preemptive ablation (8 bits; 255 with unlimited decoders)."""
        c = self._u_cores(dec)
        if not c:
            st = dict(fe=not self._pending_any(dec), idle=255)          # (uncapped too: a blocked window is backlog; re-check, 3)
            if self._u6 is not None and self._u6.q_wait:
                st["wq"] = 0                                             # (U v6, 51a: no queue with unlimited decoders)
            return st
        busy = len(dec._active_window_progress)
        if self._pool is not None and not self._pool.preemptive:
            busy += self._pool.busy(dec._current_round)
        st = dict(fe=not self._pending_any(dec), idle=max(0, min(255, c - busy - self._u_resv_total)))
        if self._u6 is not None and self._u6.q_wait:                      # U v6, 51a: the expected wait, Q24.8, of a window
            st["wq"] = self._u_wait(dec, sum(self._u_queued.values()))   # queued now behind every queued one (rule 30)
        return st

    @staticmethod
    def _pending_any(dec) -> bool:
        """A full-decode window waits for a core, ready or blocked by a dependency (a blocked window
        is backlog that will need a core)."""
        return any(dec._get_task_or_none(t) is not None for t in dec._pending_decode_tasks)

    # -- the U unit's pass
    def _u_phase_a(self, k) -> list:
        """The engine's Phase A of pass k on this round's messages, as golden/engine.py takes them (by type in the order
        REGISTER, RESOLVE, RECONFIDENCE, RECOVERY_DONE, each in arrival order), on its 64-entry table: a REGISTER for a
        value that holds an entry, or into a full table, is refused, else it takes the lowest free entry; RESOLVE 1 frees
        a live entry, 0 leaves it recovering until RECOVERY_DONE.  Returns the taps, in order, that U's mirror takes."""
        m, ent, of, st, taps = self._u_msgs, self._u_ent, self._u_ent_of, self._u_stats, []
        for vid, cls, omc, mhat in m["REG"]:
            if vid in of:
                st["engine:refused:in_use"] += 1; continue
            i = next((j for j, e in enumerate(ent) if e is None), None)
            if i is None:
                st["engine:refused:full"] += 1; continue
            ent[i] = [vid, True]; of[vid] = i
            taps.append(("REG", i, vid, cls, omc, mhat))
        for vid, verdict in m["RES"]:
            i = of.get(vid)
            if i is None or not ent[i][1]:
                st["engine:unmatched"] += 1; continue
            taps.append(("END", i))
            if verdict:
                ent[i] = None; del of[vid]
            else:
                ent[i][1] = False
        for vid, omc in m["RC"]:
            i = of.get(vid)
            if i is None or not ent[i][1]:
                st["engine:unmatched"] += 1; continue
            taps.append(("RC", i, omc))
        for vid in m["RD"]:
            i = of.get(vid)
            if i is None or ent[i][1]:
                st["engine:unmatched"] += 1; continue
            ent[i] = None; del of[vid]
        for t in taps:
            self._uu.tap(k, *t)
        self._u_peaks["entries"] = max(self._u_peaks["entries"], len(of))
        return taps

    def _u_stale_due(self, k: int) -> bool:
        """Some window goes stale in pass k (U-contract.md section 4: a live, unverified, unmarked window that is not DONE,
        stamped before k + act_lat - stale_max), so the pass must run for its stale scan even without a message."""
        uu, q = self._uu, self._u_stale_q
        kms = uu.kms(k)                                                  # (the golden's: its eligibility horizon)
        while q:
            stamp, s_ = q[0]
            if uu.alive[s_] and not uu.verified[s_] and not uu.exact[s_] and not uu.sm[s_] and uu.stamp[s_] == stamp:
                return stamp < kms
            heapq.heappop(q)
        return False

    def _u_pass(self, dec):
        """The end of round k: U's pass k + 1 (U-contract.md section 2) on this round's messages -- the engine's Phase A,
        the window events, the stale scan, then the epoch requests with the pool's status latched now (net of the
        reservations, rule 41c).  Each ACT reserves its demand at once and takes effect in round k + act_lat.  Every pass
        that bears a message runs and is recorded; a round without one still runs its pass when a window goes stale in it
        (otherwise the pass would change nothing)."""
        uu, now, st, gu = self._uu, self.current_round, self._u_stats, _golden_u()
        k = now + 1
        self._u_resv_tick()
        msgs = any(self._u_msgs[t] for t in ("REG", "RES", "RC", "RD"))   # (a RECOVERY_DONE alone too)
        taps = self._u_phase_a(k)
        wins, reqs = self._u_wins, self._u_reqs
        wcarry, rcarry = [], []
        if self._hw is not None:                                           # rule 66: U's EPOCH and window ingress, 128 a pass;
            wins, wcarry = wins[:self._hw.q_win], wins[self._hw.q_win:]    # the rest waits for the next pass, in order
            reqs, rcarry = reqs[:self._hw.q_epoch], reqs[self._hw.q_epoch:]
            for q, x in (("win_ingress", wcarry), ("epoch", rcarry)):
                if x:
                    self._hw.stats["full_passes:" + q] += 1
                    self._hw.stats["delayed:" + q] += len(x)
            for w in wins:                                                 # the unit's slots take the generations of the NEWs it
                if w[0] == "NEW":                                          # serves this pass (before the pass's requests)
                    self._u_ugen[w[1]] = self._u_wgen.get(w[4], 0)
            late = {w[1] for w in wcarry if w[0] == "NEW"}
            for w in wcarry:                                               # U v6's own read waits for the pass that takes a
                if w[0] == "NEW" and w[4] in self._u_newr:                 # NEW held behind the cutoff
                    self._u_newr[w[4]] = now + 1
            for q in reqs:                                                 # a root whose slot the unit holds at another
                g = q.get("_gens")                                         # generation (its NEW not yet served, or the slot
                if g is not None and q.get("roots") and any(self._u_ugen.get(s_, 0) != x for s_, x in zip(q["roots"], g)):
                    self._hw.stats["epoch_roots_generation"] += 1          # reused since the request was formed) leaves the
                    if late.intersection(q["roots"]):                      # ETA unavailable, as for a window outside the file
                        self._hw.stats["epoch_roots_new_not_served"] += 1
                    q["roots"] = []
                    q.pop("floor", None)
                    q["_roots_why"] = "generation"
        u6c = self._u6 is not None and self._u6.needs_clock              # U v6, rule 52: the pass's work for its timing
        if u6c:
            work0 = [uu.stats.get(x, 0) for x in ("recomputes", "invalidated", "budget_exhausted", "stale")]
        if msgs or taps or wins or reqs or self._u_round_disc or self._u_round_resv or self._u_stale_due(k):
            pool = self._u_pool_status(dec) if reqs else None
            reserved = self._u_resv_total
            n0, m0 = len(uu.out), len(uu.nets)
            uu.run_pass(k, [w[:4] for w in wins], reqs, pool or dict(fe=True, idle=0))
            outs, nets = uu.out[n0:], iter(uu.nets[m0:])
            u6i = self._u6 is not None and self._u6.instr
            chk = iter(uu.chk) if u6i else None
            rows = []
            for req, (_k, vid, epoch, b, act, cause) in zip(reqs, outs):
                scored = cause in (gu.C_ACT, gu.C_SCORE, gu.C_NET2)
                n = next(nets) if scored else None
                st["E%d:%s" % (epoch, gu.CAUSE[cause])] += 1
                if u6i:                                                   # U v6, rule 56a: the request's record
                    self._u6.request_rec(k, req, next(chk)[3], gu.CAUSE[cause])
                if act and self._u_nocom:                                  # SER-NC: no commit on a prediction; the request,
                    self._u_discard(dict(vid=vid, epoch=epoch, b=b, rid=None), "nocom", "act")   # its answer and the pass stand
                elif act and self._hw is not None and not self._hw.resv_slot_free():
                    self._hw.stats["resv_full"] += 1                       # rule 66: no reservation slot, the ACT is refused
                    self._u_discard(dict(vid=vid, epoch=epoch, b=b, rid=None), "resv", "full")
                elif act:
                    fr = self._frames[vid]
                    fr.u_pending.append(dict(vid=vid, epoch=epoch, b=b, req=req, decided=now, effect=now + self._u_L,
                                             rid=self._u_reserve(vid, b, req.get("held", 0))))
                    self._u_pending_vids.add(vid)
                    if u6i:
                        self._u6_calib(k, req, n)                         # U v6, rule 56c: the score's inputs at the ACT
                rows.append([act, cause] + ([n[2], n[3], n[4], n[5]] if n is not None else []))
            if u6i:
                uu.chk.clear()
            assert len(outs) == len(reqs) and next(nets, None) is None, "U: one answer per request"
            del uu.out[n0:], uu.nets[m0:]
            pk = self._u_peaks
            pk["requests"], pk["window_events"] = max(pk["requests"], len(reqs)), max(pk["window_events"], len(wins))
            st["passes"] += 1
            st["passes_over_128_requests"] += len(reqs) > U_FIFO
            st["passes_over_128_window_events"] += len(wins) > U_FIFO
            if self._u_rep is not None:
                self._u_rep_add(k, taps, wins, pool, reserved, reqs, rows)
        if u6c:                                                           # U v6, rule 52: the pass's timing (model)
            ops = collections.Counter(w[0] for w in wins)                 # and its u_xs export
            w1 = [uu.stats.get(x, 0) - w0 for x, w0 in zip(("recomputes", "invalidated", "budget_exhausted", "stale"), work0)]
            m = self._u_msgs
            self._u6.pass_end(k, dict(new=ops["NEW"], upd=ops["UPD"], commit=ops["COMMIT"], done=ops["DONE"], req=len(reqs),
                                      taps=len(taps) if taps else 0, recomputes=w1[0], invalidated=w1[1], exhausted=w1[2],
                                      stale=w1[3]),
                              dict(reg=len(m["REG"]), res=len(m["RES"]), rc=len(m["RC"]), rd=len(m["RD"])))
        self._u_msgs = dict(REG=[], RES=[], RC=[], RD=[])
        self._u_wins, self._u_reqs = wcarry, rcarry
        self._u_round_disc, self._u_round_resv = [], []

    def _u6_calib(self, k: int, req: dict, n):
        """U v6, rule 56c (records only): the score's inputs at an ACT -- eta, the early script's start a, W_b, S_b, R_b and
        its XR, S_D and its XS, and the gain -- to set against the realized verdict and repair."""
        uu, gu = self._uu, _golden_u()
        cls = self._u_reg[req["vid"]]["cls"]
        t = uu.tab.get((cls, req["b"]))
        sdx = uu._sd_x(cls, req["cur"], req["reach"])
        eta = n[5] if n is not None else None
        a = max(k + uu.act_lat, req["ready"])
        W = None if eta is None or t is None else max(0.0, eta / 256.0 - a - t.S)
        self._u6._log("calib", dict(vid=req["vid"], k=k, epoch=req["epoch"], b=req["b"], c=1.0 - (next((e[2] for e in uu.ent.values() if e[0] == req["vid"]), 0) / float(1 << gu.FRAC)),
                                          eta=(None if eta is None else round(eta / 256.0, 2)), a=a, W=W,
                                          S=(t.S if t else None), R=(t.R if t else None), XR=(t.XR if t else None),
                                          S_D=(sdx[0] if sdx else None), XS_D=(sdx[1] if sdx else None),
                                          gain=(None if n is None else (n[2] if req["epoch"] == 1 else n[3]))))

    # -- the replay records (section 11): what cycle-level replay reads
    def _u_rep_add(self, k, taps, wins, pool, reserved, reqs, rows):
        rec = dict(k=k)
        m = {t: [list(x) if isinstance(x, tuple) else x for x in self._u_msgs[t]] for t in ("REG", "RES", "RC", "RD") if self._u_msgs[t]}
        if m:
            rec["msg"] = m
        if taps:
            rec["tap"] = [list(t) for t in taps]
        if wins:
            rec["win"] = [[op, s, own, list(preds), w] for op, s, own, preds, w in wins]
        if self._u_round_resv:
            rec["resv"] = [list(x) for x in self._u_round_resv]             # rules 41c and 44a: ["new", value, cores (an ACT's
                                                                            # demand, on a lazy frame its excess), first round,
                                                                            # last round], ["lazy", value, cores, round, -1],
                                                                            # ["end", value, cores, why]
        if self._u_round_disc:
            rec["disc"] = [list(x) for x in self._u_round_disc]             # discards: [value, epoch, b, cause, reason]
        if reqs:
            rec["pool"] = [int(pool["fe"]), pool["idle"], reserved] + ([pool["wq"]] if "wq" in pool else [])   # (U v6, 51a: pl_wq)
            fl = self._u6 is not None and self._u6.eta_floor                # (U v6, 51b: the floor, -1 for none)
            rec["req"] = [[q["vid"], int(q["epoch"] == 1), q["b"], q["cur"], q["reach"], int(q["ready_v"]), q["ready"], int(q["cells_free"]),
                           q["s"], int(q["s_ready_v"]), q["s_ready"], list(q["roots"]), q.get("held", 0)] + ([q.get("floor", -1)] if fl else [])
                          for q in reqs]
            rec["out"] = rows
        rep = self._u_rep
        rep["buf"].append(json.dumps(rec, separators=(",", ":")))
        rep["passes"] += 1
        if len(rep["buf"]) >= U_REPLAY_FLUSH:
            self._u_rep_flush()

    def _u_rep_flush(self, tail=None):
        """Append the buffered lines as one gzip member.  The file is first cut to the length the last flush left, so a
        run resumed from a checkpoint rewrites what it wrote after that checkpoint."""
        import gzip
        rep = self._u_rep
        lines = ([json.dumps(dict(header=rep["header"]), separators=(",", ":"))] if rep["bytes"] == 0 else []) + rep["buf"] + ([tail] if tail else [])
        data = gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0)
        with open(rep["path"], "r+b" if os.path.exists(rep["path"]) else "wb") as f:
            f.truncate(rep["bytes"]); f.seek(rep["bytes"]); f.write(data)
        rep["bytes"] += len(data)
        rep["buf"] = []

    def _after_full_starts(self, dec, new_windows):
        """After each round's full decodes have started (`ScoutDecoderManager.update_decoding`): rule 42a's preemption of
        the strip jobs started last, as many as the full decodes need; the head-of-line count; the strip starts on the
        cores still idle, less U's reservations (rule 41c); then U's window events, its decoder-traffic accounting and
        its pass."""
        now = dec._current_round
        pool = self._pool
        if type(getattr(self, "_qg", None)) is int:                     # (round 7's QG: the count its next registrations read)
            self._qg_q = self._runnable_pending(dec, self._qg + 1)
        if self._u_on:
            self._u_resv_tick()                                         # (rule 41c: the reservations the pool counts now)
        if pool is not None:
            held = pool.busy(now)
            if pool.preemptive:                                         # rule 42a: full decodes took cores strip jobs held
                excess = len(dec._active_window_progress) + held - pool.B
                if excess > 0:
                    for vid, rec in pool.preempt(excess, now):
                        self._strip_pool.preempt(vid, rec, now)
                    held = pool.busy(now)
            if held:
                pool.note_hol(self._runnable_pending(dec, held))       # min(runnable windows still waiting, cores strip jobs hold)
            idle = (pool.B - len(dec._active_window_progress) - held - (self._u_resv_total if self._u_on else 0)   # (41c)
                    - (self._w2.counted(now) if self._w2 is not None else 0))          # (the decision: its commits' cores)
            if self._w2 is not None and self._w2.r1:
                self._w2.strip_blocked(now, idle, self._strip_pool)        # (R1: the starts its bookings' cores block)
            if idle > 0:
                self._strip_pool.pool_start(now, idle)
        if self._u6 is not None:                                          # rule 50c: busy_ewma, from the round's busy count
            self._u6.tick_busy(now, len(dec._active_window_progress) + (pool.busy(now) if pool is not None and not pool.preemptive else 0))
        if self._w2 is not None:                                          # the decision: busy_ewma, its reservations, the load bin
            log = getattr(dec._pending_decode_tasks, "started_log", None)
            started = list(log) if log else []
            if log is not None and not self._u_on:
                log.clear()
            self._w2.tick(dec, started)
        if not self._u_on:
            return
        self._u_pool_events(dec, new_windows)
        tags = (("script", self._u_script_instrs), ("repair", self._u_repair_instrs), ("delayed", self._u_delayed_instrs))
        if self._u_script_instrs:
            for w in new_windows:
                par = w.parent_instr_idx
                for cat, instrs in tags:
                    if any(i in instrs for i in par):
                        self._u_tag[w.window_idx] = cat; break
        if self._u_tag:
            busy = dict(script=0, repair=0, delayed=0)
            for t in dec._active_window_progress:
                cat = self._u_tag.get(t)
                if cat is not None:
                    busy[cat] += 1
            total = sum(busy.values())
            if total:
                wait = self._runnable_untagged(dec, total)
                for cat in ("script", "repair", "delayed"):
                    k = min(wait, busy[cat])
                    self._u_traffic[cat] += k; wait -= k
                self._u_traffic["rounds"] += 1
        self._u_pass(dec)

    def _runnable_untagged(self, dec, limit: int) -> int:
        """`_runnable_pending`, counting only the windows no ACT tag names."""
        pend = dec._pending_decode_tasks
        n = 0
        for t in (pend._asc if hasattr(pend, "_asc") else sorted(pend)):
            if t in self._u_tag or dec._get_task_or_none(t) is None:
                continue
            if all(dec._completed_decoding(p) or dec._completed_speculation(p) for p in dec._window_idx_dag.predecessors(t)):
                n += 1
                if n >= limit:
                    break
        return n

    def _u_summary(self) -> dict:
        frames = list(self._frames.values())
        how = collections.Counter(fr.how for fr in frames if fr.rebased)
        out = dict(act_lat=self._u_L, **({"elig": self._u_elig} if self._u_elig != self._u_L else {}), carry=bool(getattr(self, "_u_carry", False)), counters=dict(sorted(self._u_stats.items())),
                   early_confirmed=how.get("early_confirmed", 0), early_repaired=how.get("early_repaired", 0))
        if self._u4:                                                       # U v4 (rule 44): the carry rule's choices
            rules = collections.Counter(fr.rule for fr in frames)
            out["u4"] = dict(h=("inf" if self._u4_h is None else self._u4_h), frames_lazy=rules.get("lazy", 0),
                             frames_nci=rules.get("no_cost_increase", 0), frames_stop0=sum(1 for fr in frames if fr.stop == 0),
                             lazy_reservations=self._u_stats.get("reservation:lazy", 0))
        if self._u_on or self._u4_lazy_on:                                 # the classes, per walk and together: at most
            tab = self._u_tab                                              # 64 (the engine's 6-bit site class); a site that would
            byw = collections.Counter(tab.rule_of(c) for c in range(len(tab.classes)))   # need a 65th gets none
            out["classes"] = dict(total=len(tab.classes), lazy=byw.get("lazy", 0), nci=byw.get("no_cost_increase", 0),
                                  sites_without_class_full=tab.stats["no_class_full"], sites_without_class_width=tab.stats["no_class_width"],
                                  sites=tab.stats["sites"])
        if self._u_on:
            uu, cn = self._uu, self._u_stats
            pend = sum(len(self._frames[v].u_pending) for v in self._u_pending_vids)
            out.update(delta=uu.delta, path=uu.path, stale_max=uu.stale_max, tables=self._u_tab.summary(), unit=dict(sorted(uu.stats.items())),
                       peaks=dict(self._u_peaks), decoder_traffic=dict(self._u_traffic), pending_at_end=pend,
                       discards={c: sum(v for k, v in cn.items() if k.endswith((":discard:" + c, ":cancel:" + c)))   # 41b, 41f
                                 for c in ("verdict_first", "deadline", "revalidation", "abandonment")},
                       acts_fields=list(U_ACT_FIELDS), acts=[[a[f] for f in U_ACT_FIELDS] for a in self._u_acts[:U_LOG_MAX]],
                       install_fields=["vid", "round", "cur", "b*", "stop", "F"], installs=[list(x) for x in self._u_install])
            rep = self._u_rep
            if rep is not None and not rep["done"]:
                tail = json.dumps(dict(tables=[[c, [list(r) for r in recs[1:]], recs[0][0]] for c, recs in enumerate(self._u_tab.classes)],
                                       counters=out["counters"], unit=out["unit"]), separators=(",", ":"))
                self._u_rep_flush(tail)
                rep["done"] = True
                out["replay"] = dict(path=os.path.basename(rep["path"]), passes=rep["passes"], bytes=rep["bytes"])
        if self._u6 is not None:
            out["u6"] = self._u6.summary()                                 # U v6: its options, counters, records
        return out

    def _defer_check_successors(self, idx: int, new):
        """Invariant of the dispatch (upstream's too): the successors an instruction adds to the frontier when it starts
        have not started.  A splice edge that broke it would make the dispatch run an instruction twice; fail closed."""
        for j in new:
            t = self.schedule_instructions[j]
            if j in self._defer_started:
                ins, jj = self.schedule_instructions[idx].instruction, t.instruction
                raise RuntimeError("arm D: instruction %d (%s %s) started in round %d, but its successor %d (%s %s) had started in round %d "
                                   "(end %d); edges into %d added by splices: %s; preds of %d: %s"
                                   % (idx, ins.name, sorted(ins.patches), self.current_round, j, jj.name, sorted(jj.patches), t.start_round,
                                      t.end_round, j, self._defer_edges.get(j, [])[:6], idx, self._adj()[0].get(idx, [])[:12]))

    def _defer_unlock(self, fr: DeferFrame):
        if self._hw is not None and fr.locked:
            cells, vid = fr.locked, fr.vid                                 # rule 66: the script end event unlocks its cells
            fr.locked = frozenset()
            self._hw.submit("script_end", lambda vid=vid, cells=cells: self._hw_unlock(vid, cells), cells=dict(script_cells=cells))
            return
        for p in fr.locked:
            s_ = self._defer_locked.get(p)
            if s_ is not None:
                s_.discard(fr.vid)
                if not s_:
                    del self._defer_locked[p]
        fr.locked = frozenset()

    def _defer_edge(self, a: int, b: int, why: str = ""):
        """One dependency edge a -> b, kept exactly as `_splice_recovery`'s edges are: the neighbour lists and both
        networkx graphs."""
        if not self._add_edge_cached(a, b):
            return
        self._defer_edges.setdefault(b, []).append((a, why, self.current_round))
        self.schedule_dag.add_edge(a, b, weight=self._instruction_durations[a])
        dec = LINK.get("decoder")
        if dec is not None:
            dec.instruction_idx_dag.add_edge(a, b)
            dec.note_edge(a, b, in_graph=True)

    def _defer_splice(self, fr: DeferFrame, since: int | None = None):
        """Materialize the frame at boundary j*: splice the placed script of E_j* (instructions as `_splice_recovery`
        creates them).  Each first instruction on a cell follows the latest instruction still running there and an
        earlier script's last one there; the admitted steps before j* are complete.  While the script runs its cells
        are locked (`_defer_hold`: no original instruction of positive duration, and no resource initialisation, starts
        there -- the cell's later users follow the script, a zero-duration cleanup of earlier work still completes), and
        the frame's held work (fence, the steps from j* on) waits until the frame finishes; the decoder is told the
        release edges `_splice_recovery` would add.  The final frame is recorded when the frame finishes.  `since`: the
        round the stall is measured from (the verdict's; H's early script: its trigger's)."""
        v = self.values[fr.vid]; j = fr.target; now = self.current_round; L = self.ledger; st = self._defer_stats
        ref = fr.verdict_round if since is None else since
        low, res, at, fa = self._defer_boundary(fr, j)
        frame_rec = dict(pauli=fa[0].key(), cond=[[b, g.key()] for b, g in fa[1] if not g.is_identity()],
                         nonempty=(not fa[0].is_identity()) or any(not g.is_identity() for _, g in fa[1]))
        n_prog = len({w for w in res.axis.wires | res.P.wires if self.es.roles_by_wire.get(w) == "program"}) or 1
        rec = dict(round=now, step=j, P1=low.P1, residual=res.full_key(), frame=frame_rec, stall_rounds=now - ref,
                   P2_stall_patch_rounds=(now - ref) * n_prog, instrs=[], realised=0, n_prog=n_prog)
        if fr.u_lazy is not None:
            self._u_release(fr.u_lazy, "materialize")                     # U v4 (rule 44a): its demand is now the script's
                                                                           # (D's splice or an ACT's early one; an ACT's own
                                                                           # excess runs on to its end)
        v.recovery = rec
        fr.state = "spliced"
        if self._u6 is not None and self._u6.instr:                        # U v6, rule 56b: the executed path, the residual's
            self._u6._log("paths", dict(vid=fr.vid, F=fr.c.F, j=j, top=fr.top, rule=fr.rule, rebased=bool(fr.rebased),
                                              weight=len(res.axis.wires | res.P.wires), P1=int(low.P1),   # weight, and the
                                              contract_P1=[(None if x is None else int(x)) for x in fr.r]))   # per-step prices
        st["P1_total"] += low.P1; st["stall_rounds"] += rec["stall_rounds"]
        L.P1_total += low.P1; L.P2_stall_patch_rounds += rec["P2_stall_patch_rounds"]
        if not low.instrs:
            rec["kind"] = "frame"; L.recoveries_frame_only += 1; st["frame_only"] += 1
            return
        rec["kind"] = "script"
        self._bind_recovery_cells(v, j, SimpleNamespace(recovery=low, residual=res, at_event=at, residual_key=res.full_key()))
        cls_ = self._script_class(fr.c, low, res, j, fr.rebased and since is None)   # (records only: a rebased frame's
                                                                           #  materialization repairs its commit)
        ev_ = "early_commit" if since is not None else ("repair" if fr.rebased else "materialization")   # (records only:
                                                                           #  `_h_splice` alone passes since)
        dec = LINK.get("decoder")
        base = len(self.schedule_instructions)
        new = []
        for pos, x in enumerate(low.instrs):
            # numbered by position, not base + x.idx (a defensive choice: every lookup relies on list position == idx)
            ins = Instruction(x.name, base + pos, x.patches, x.duration, merge_faces=x.merge_faces, group_name="RECOVERY")
            self.schedule_instructions.append(InstructionTask(ins.idx, ins, -1, -1))
            assert self.schedule_instructions[ins.idx].instruction is ins
            dur = Duration.get_true_duration(ins.duration, self.d_t)
            self._true_durations.append(dur); self._instruction_durations.append(dur); self._plan_durations.append(dur)
            self._is_startup_instruction.append(False)
            self._patches_initialized_by_instr.append(set(ins.patches) - set(self._active_patches))
            self.schedule_dag.add_node(ins.idx, duration=dur)
            if dec is not None: dec.instruction_idx_dag.add_node(ins.idx)
            self._recovery_instrs.add(ins.idx); self._recovery_owner[ins.idx] = v.vid; self._tracked.add(ins.idx)
            self._recovery_class[ins.idx] = cls_                         # (records only)
            self._recovery_event[ins.idx] = ev_                          # (records only)
            self._recovery_local[ins.idx] = (v.vid, j + (H_REPAIR_KEY if fr.rebased else 0), pos)   # (H: a repair's own names)
            new.append(ins)
        if self._wq is not None:
            self._wq_splice(v.vid, new)                                    # (on its frame's script reservation)
        if self._strip_splices_on:
            self._strip_splice(low, new, "defer_repair" if fr.rebased else "defer")
        if self._trace_on:
            self._trace_splice(v.vid, j, low, new, "defer_repair" if fr.rebased else "defer")
        firsts, lasts = {}, {}                                             # cell -> the script's first / last instruction on it
        for i, x in enumerate(new):                                        # internal order: the .lss rule (shared patch)
            for jj in range(i):
                if set(new[jj].patches) & set(x.patches):
                    self._defer_edge(new[jj].idx, x.idx, why="internal")
            for p in x.patches:
                firsts.setdefault(p, x.idx); lasts[p] = x.idx
        members_before = {i for k in range(j) for i in fr.members[k]}
        for p, f in firsts.items():
            prev = self._defer_cell_scripts[p][-1] if self._defer_cell_scripts[p] else None
            if prev is not None and self.schedule_instructions[prev].end_round == -1:
                self._defer_edge(prev, f, why="script_chain")               # behind an earlier script on the cell
            last = self._defer_last_started(p)
            if last is not None and self.schedule_instructions[last].end_round == -1 and self._instruction_durations[last] > 0:
                self._defer_edge(last, f, why="last_started")              # behind the latest instruction on it, still running
        for i in members_before:                                           # the frame's own steps before j* (zero-duration ones
            if i not in self._defer_started:                               # still pending complete in this round's dispatch)
                for f in set(firsts.values()):
                    self._defer_edge(i, f, why="pending_member")
        if dec is not None:                                                # the decoder's view of the release edges (Q's splice)
            held = {i for i in fr.fence | fr.unadm if i not in self._defer_started}
            for p, l in lasts.items():
                u = self._defer_first_unstarted(p)
                if u is not None and u not in members_before:
                    dec.note_edge(l, u, in_graph=False)
            for l in set(lasts.values()):
                for u in held:
                    dec.note_edge(l, u, in_graph=False)
        for x in new:
            for p in x.patches:
                self._defer_cell_scripts[p].append(x.idx)
        fr.locked = frozenset(p for x in new for p in x.patches)
        for p in fr.locked:
            self._defer_locked.setdefault(p, set()).add(fr.vid)
        for f in set(firsts.values()):
            self._frontier_add(f)
        rec.update(instrs=[x.idx for x in new], cells=sorted(low.cells))
        L.recoveries_spliced += 1; st["scripts"] += 1

    def _defer_finish(self, fr: DeferFrame, how: str):
        """The frame ends: dropped (b = 0) or materialized (script or frame done).  Its holds, guard, members and claims
        are released and the value settles (CONFIRMED / RETIRED)."""
        v = self.values[fr.vid]; now = self.current_round; st = self._defer_stats
        if how in ("materialized", "early_repaired"):                     # (H: early_confirmed / early_repaired, section 2.3)
            r = v.recovery
            self._apply_recovery_frame(v, r)                               # the complete final frame, recorded
            if r["kind"] == "script":
                end = max(self.schedule_instructions[i].end_round + 1 for i in r["instrs"])
                r["recovery_end_round"] = end
                r["P2_settle_patch_rounds"] = max(0, now - end) * r["n_prog"]
                self.ledger.P2_settle_patch_rounds += r["P2_settle_patch_rounds"]; st["settle_patch_rounds"] += r["P2_settle_patch_rounds"]
                st["settle_rounds"] += max(0, now - end)
            elif r["kind"] == "frame":                                     # frame-only (no instructions): it ends here, one round
                r["recovery_end_round"] = now                              #  after its splice in both settlements
            r["done_round"] = now
            state = VState.RETIRED
        else:
            state = VState.CONFIRMED
        if fr.locked:
            self._defer_unlock(fr)
        assert not fr.h_early_locked and fr.h_next is None, ("H: a frame finishes with its early script or prepared rebase open", fr.vid)
        for instrs_, events_ in fr.h_prefix:                               # H: the original steps before the early boundary
            for i in instrs_:
                if self._defer_member.get(i) == fr.vid:
                    del self._defer_member[i]
            for e in events_:
                if self._defer_claim.get(e) == fr.vid:
                    del self._defer_claim[e]
        for i in fr.fence | fr.unadm:
            s = self._defer_hold_of.get(i)
            if s is not None:
                if self._wq is not None and fr.vid in s:
                    self._wq_h(fr.vid, i)                                         # (a hold mark cleared)
                s.discard(fr.vid)
                if not s:
                    del self._defer_hold_of[i]
        if getattr(self, "_r8", False) and (fr.r8_pend or fr.r8_pend_reb):   # (round 8: guards still pending go with the frame)
            self._r8_pend_add(fr.r8_pend | fr.r8_pend_reb, -1); fr.r8_pend = fr.r8_pend_reb = frozenset()
        if self._hw is not None:                                           # rule 66: the frame end event removes the guard
            self._hw.cancel(fr.vid)                                        # (its own events still queued go with the frame)
            if fr.hw_guard_on:
                cells = frozenset(fr.guard)
                held = (fr.u_lazy is not None or fr.u_act is not None or (fr.w2 is not None and fr.w2.get("commit") is not None))
                self._hw.submit("frame_end", lambda cells=cells: self._hw_guard_off(cells), cells=dict(guard=cells), rel=held)
            fr.hw_wait = fr.hw_ok = None
        else:
            for c_ in fr.guard:
                n = self._defer_ws_cells.get(c_, 0) - 1
                if n > 0:
                    self._defer_ws_cells[c_] = n
                else:
                    self._defer_ws_cells.pop(c_, None)
        if fr.w2 is not None:
            self._w2_uncommit(fr, "done")                                  # the decision: its commit's script and repair have ended
            self._w2.age_check(fr)                                         # (the hardware's 16-bit age)
            self._w2.value_end(fr, how)                                    # (records only: spec 14's per-value record)
        for c_ in fr.target_ws:                                            # (empty: a frame finishes after its splice)
            n = self._defer_target_cells.get(c_, 0) - 1
            if n > 0:
                self._defer_target_cells[c_] = n
            else:
                self._defer_target_cells.pop(c_, None)
        if getattr(self, "_r8", False):
            self._r8_orphan_add(fr)                                        # (its admitted members that have not started keep
        updates = 0                                                        #  their permissions)
        for s_, mem in zip(fr.c.steps, fr.members):
            updates += all(self.schedule_instructions[i].end_round != -1 for i in mem)
            for i in s_.instrs:
                if self._defer_member.get(i) == fr.vid:
                    del self._defer_member[i]
            for e in s_.events:
                if self._defer_claim.get(e) == fr.vid:
                    del self._defer_claim[e]
        self._live_frames.discard(fr.vid)
        if fr.resv_fp and self._resv_held is not None:
            self._resv_release(fr)                                         # (SER-R: its hold ends with it)
        if self._r9 is not None:
            if self._wq is not None:
                self._wq_finish(fr.vid)                                    # (before its chains go: their marks are cleared)
            kept = self._r9.frame_end(fr.vid, self._live_frames, self._defer_started)   # (its reservation, chains and
            self._r9.port2(self.current_round, 1 + len(fr.guard) + kept)   #  covered placements go, but for the covers another
                                                                           #  live frame's chains still hold, r9-guard-close;
                                                                           #  the second port clears its masks)
        if self._u_on:
            self._u_tab.forget(fr.vid)
            r = self._u_reg.get(fr.vid)
            if r is not None and r["verdict"] == 0:
                self._u_msgs["RD"].append(fr.vid)                         # U: RECOVERY_DONE, the frame's correction is done
            for item in fr.u_pending:
                self._u_discard(item, "verdict_first", "verdict")
            fr.u_pending = []
            if fr.u_act is not None:                                       # the executed ACT's script and repair have ended
                self._u_release(fr.u_act["rid"], "done")
                self._u_acts[fr.u_act["log"]].update(done=now, how=how)
        if fr.u_lazy is not None:
            self._u_release(fr.u_lazy, "frame_end")                        # U v4: a lazy frame that drops (or ends) releases it
        fr.how, fr.done_round, fr.state = how, now, "done"
        # a finished frame keeps only what the summary reads (the walk's scripts and member sets are released)
        fr.c = SimpleNamespace(F=fr.c.F, fence_reason=fr.c.fence_reason,
                               **({"measuring_steps": self._r7_measuring_steps(fr.c)} if self._r7 else {}))
        fr.members, fr.fence, fr.unadm, fr.kof, fr.guard, fr.taint_wires, fr.taint_results = [], set(), set(), {}, set(), set(), set()
        fr.target_ws, fr.h_prefix = frozenset(), []
        st[how] += 1; st["frame_updates"] += updates; st["live_rounds"] += now - fr.reg_round; st["admit_waits"] += fr.admit_waits
        self._settle(v, state)
        r = v.recovery if how in ("materialized", "early_repaired") else None
        self._defer_log.append(_defer_log_row(self, fr, how, now, updates, r))

    def _defer_log_live(self) -> list:
        """G (records only): the log rows of the frames still live at the run's end (a stall leaves them), as
        `_defer_finish` would write them now: how "live:<state>", end None, `updates` the steps whose members all ended,
        and the script's record when one was spliced."""
        rows = []
        for vid in sorted(self._live_frames):
            fr, r = self._frames[vid], self.values[vid].recovery
            updates = sum(all(self.schedule_instructions[i].end_round != -1 for i in mem) for mem in fr.members)
            r = r if isinstance(r, dict) and "stall_rounds" in r else None
            rows.append(_defer_log_row(self, fr, "live:" + fr.state, None, updates, r))
        return rows

    def _defer_hold(self, idx: int) -> str | None:
        vids = self._defer_hold_of.get(idx)
        if vids:
            for x in vids:
                fr = self._frames[x]
                if idx in fr.unadm:
                    if fr.state != "carry":
                        return "defer_materialize"                         # from j* on: waits for the correction
                    if self._r8 and fr.r8_fwd[0] == self.current_round and fr.r8_fwd[1] <= fr.kof[idx] < fr.r8_fwd[2]:
                        return "defer_rule_a"                              # (records only: a block rule (a) refused now)
                    return "defer_stop" if fr.kof[idx] >= fr.limit else "defer_step"
            return "defer_dependency"
        if idx >= self.n_original:
            return None
        if not (self._is_startup_instruction[idx] or self._instruction_durations[idx] > 0):
            return None                                                    # a zero-duration cleanup of earlier work
        pts = self.schedule_instructions[idx].instruction.patches
        mine = self._defer_member.get(idx)
        if self._defer_locked:
            for p in pts:
                lk = self._defer_locked.get(p)
                if lk and (mine is None or mine not in lk or self._frames[mine].rebased):
                    return "defer_script_cell"                             # a materialization runs on this cell (H: its own
                                                                           # early script too, section 4)
        if mine is not None:
            return None                                                    # an admitted step (the others are held above; CSR:
                                                                           #  none places on another live frame's held cell, G1's
                                                                           #  two halves, so no test here; `_resv_start` counts one)
        if self._resv is not None:
            return self._resv_test(idx, pts)                               # (SER-R: a placement onto a held cell waits; no
                                                                           #  other guard, an ended frame's members included)
        if self._r8 and idx in self._r8_orph:                              # (an ended frame's admitted member keeps its
            if idx not in self._r8_orph_passed and any(p in self._defer_ws_cells or p in self._defer_target_cells or p in self._r8_pend
                                                       for p in pts):      #  permission until it starts; counted where path
                self._r8_orph_passed.add(idx)                              #  would have taken it to a guard, once per
                self._defer_stats["r7:rule_orphan_passes"] += 1            #  instruction)
            return None
        if self._guard == "off":                                           # (OFF: no workspace guard and no rule (a); records
            if self._defer_target_cells and idx not in self._defer_target_need and any(   #  the starts a decided workspace's
                    p in self._defer_target_cells and p not in self._active_patches for p in pts):   #  guard would have held)
                self._off_tg.add(idx)
            return None
        # the workspace guard: placements are fixed, so an instruction does not bring a new patch onto a free cell of a
        # decided materialization's workspace (unless the patches already there need it to leave), nor onto a free cell
        # of any live frame's candidate workspaces if that patch's next user waits for a frame's lasting hold
        # (a frame whose next step needs a guarded instruction is not carried further while the guard holds it: decisions
        # choose among the boundaries the frame can reach at once, so a guard never stops a materialization)
        exempt = idx in self._defer_target_need
        pend = self._r8 and self._r8_pend
        if self._hw is not None and (self._defer_target_cells or self._defer_ws_cells or pend):
            gc = [p for p in pts if (p in self._defer_target_cells or p in self._defer_ws_cells) and p not in self._active_patches]
            if pend and len(gc) < 4:                                       # (round 8: a pending registration's or revision's cells
                g8 = [p for p in pts if p in self._r8_pend and p not in self._active_patches and p not in gc]   # are checked too)
                if g8:
                    self._defer_stats["r7:rule_pend_checks"] += 1
                    gc += g8
            r9 = self._r9
            req = lambda: self._r9_req(idx, pts, exempt, pend)             # (r9-guard-close 5: the request its check answers)
            if gc and ((r9 is not None and not self._r9_gc_turn(idx, req))
                       or not self._hw.guard_check(idx, gc[:4], aged=r9 is not None and (r9.aged_head(("gc", idx)) or ("disp", idx) in r9.tickets))):
                if r9 is not None:                                         # (records only: queued waiting, r9-guard-close)
                    r9.qwait(lambda: self._r9_gkind(idx, gc), ("gc", idx), self.current_round)
                    held = self._wq is not None and self._wq.holds
                    if ("disp", idx) not in r9.tickets:
                        if held:
                            r9.resume(("gc", idx), self.current_round)     # (F1: the request goes on, its age with it)
                        r9.refused(("gc", idx), self.current_round)        # (r9-guard-close 4: its age runs; not while its own
                    elif held:                                             #  check is in service: F1, an age set aside stays
                        r9.carry(("gc", idx), self.current_round)          #  aside)
                    self._wq_keep(("gc", idx))                             # (its queue wait goes on as its age does)
                return "hw_guard_check"                                    # rule 66: not served this round
            if gc and r9 is not None:
                r9.qserve(("gc", idx), self.current_round)
                if self._wq is not None and self._wq.holds:                # (F1: its start's admission is still to come,
                    r9.suspend(("gc", idx), self.current_round)            #  one request with it: its age is set aside, not
                    self._wq_keep(("gc", idx))                             #  ended, and its queue wait goes on.  A queue that
                else:                                                      #  holds nothing: r9's)
                    r9.served(("gc", idx))
        if self._defer_target_cells and not self._r8_tg_off:
            ex8 = self._r8 and not exempt and idx in self._r8_need         # (round 8: needed by a settling frame's whole release)
            if not exempt and not ex8:
                for p in pts:
                    if p in self._defer_target_cells and p not in self._active_patches:
                        self._defer_guarded.add(idx)
                        return "defer_guard"
            elif ex8 and any(p in self._defer_target_cells and p not in self._active_patches for p in pts):
                self._defer_stats["r7:rule_need_exempt"] += 1
        elif self._defer_target_cells and not exempt and any(p in self._defer_target_cells and p not in self._active_patches for p in pts):
            self._defer_stats["r7:rule_tg_off_passes"] += 1                # (round 8: the target guard is off this round)
        if (self._defer_ws_cells or pend) and self._r9 is not None:
            gp = [p for p in pts if (p in self._defer_ws_cells or (pend and p in self._r8_pend)) and p not in self._active_patches]
            if gp:
                return self._r9_dispatch(idx, gp, exempt)                  # (round 9's bounded check: release work, or one event)
        elif self._defer_ws_cells or pend:
            for p in pts:
                if (p in self._defer_ws_cells or (pend and p in self._r8_pend)) and p not in self._active_patches:
                    u = self._defer_next_on_cell(idx, p)
                    hold = p in self._defer_ws_cells and u is not None and (self._defer_pinned(u) or (self._r7_guard and self._r7_keeps_off(u)))
                    by8 = False
                    if self._r8 and not hold and self._r8_hold(idx, p):
                        self._defer_stats["r7:rule_kept_off"] += 1         # (round 8: the patch's release waits on work that can be
                        hold = by8 = True                                  #  held, or on a guard's hold, or nothing releases it)
                    if hold:
                        if exempt and not self._r8:
                            self._defer_exempt_pins.add(idx)               # needed to clear a decided workspace: runs anyway
                            break
                        if exempt:
                            self._defer_stats["r7:rule_exempt_held"] += 1  # (round 8: the exemption is gone; it waits)
                            by8 = True
                        self._defer_guarded.add(idx)
                        return "defer_rule_a" if by8 else "defer_guard"     # (records only: rule (a)'s holds by name)
        return None

    def _defer_next_on_cell(self, idx: int, cell):
        """The original instruction after idx on `cell`, in program order, or None."""
        lst = self._defer_cells.get(cell, ())
        k = bisect.bisect_right(lst, idx)
        return lst[k] if k < len(lst) else None

    def _defer_monitor(self, idx: int):
        """The deferral monitor (C6's rule for frames): an instruction that starts must not act on a wire of a live
        frame's light cone -- the wires its started steps touched, from the corrected wire on -- or consume a result
        those steps produced, unless it is one of the frame's steps or its materialization; steps extend the cone."""
        if not self._live_frames:
            return
        wires = self._instr_wires.get(idx, frozenset()); preds = self._instr_preds.get(idx, frozenset())
        member, owner = self._defer_member.get(idx), self._recovery_owner.get(idx)
        cells = set(self.schedule_instructions[idx].instruction.patches) if (owner is not None and idx >= self.n_original) else None
        for vid in self._live_frames:
            fr = self._frames[vid]
            if idx < self.n_original and self._instr_maxpos.get(idx, -1) <= self.es.events[self.values[vid].producer_event].pos:
                continue
            mine = member == vid or owner == vid
            touches = bool(wires & fr.taint_wires) or bool(preds & fr.taint_results)
            if cells is not None and owner != vid:                        # another frame's script: by cells
                touches = touches or bool(cells & {c_ for w in fr.taint_wires for c_ in self.es.wire_cell(w, self.values[vid].producer_event)})
            if touches and not mine:
                self._defer_violations += 1
                self._event("defer_violation", self.values[vid], "instruction %d (%s) started on the frame's wires %s / results %s"
                            % (idx, self.schedule_instructions[idx].instruction.name, sorted(wires & fr.taint_wires), sorted(preds & fr.taint_results)))
            if member == vid:
                fr.taint_wires.update(wires); fr.taint_results.update(self._instr_results.get(idx, ()))

    def _defer_summary(self) -> dict:
        st = self._defer_stats
        frames = list(self._frames.values())
        mats = [fr for fr in frames if fr.how == "materialized"]
        place = collections.Counter()
        for fr in mats:
            F = fr.c.F
            place["reached_dependency" if fr.p_verdict >= F else "before_dependency"] += 1
            place["at_dependency_boundary" if fr.target == F else "before_dependency_boundary"] += 1
            if fr.target > fr.first_ok:
                place["lazy_later_than_current"] += 1                      # later than the first boundary it could have used
            if fr.first_ok > fr.p_run:
                place["current_boundary_unusable"] += 1                    # (no script, or its workspace held by the frame's work)
            if fr.target < fr.c.F and fr.top < fr.c.F:
                place["carry_cut_short_" + fr.top_why] += 1
            if fr.target == 0:
                place["boundary_0"] += 1
            if fr.r[fr.target] == 0:
                place["pauli_frame_only"] += 1
        dropped = [fr for fr in frames if fr.how == "dropped"]
        saved = sum(fr.r[fr.first_ok] - fr.r[fr.target] for fr in mats)
        return dict(config=dict(self.defer_cfg), values=len(self.values), deferred=len(frames), not_deferred=dict(self._defer_native),
                    dropped=len(dropped), dropped_at_dependency=sum(1 for fr in dropped if fr.p_verdict >= fr.c.F),
                    materialized=len(mats), materialized_where=dict(place), scripts=st["scripts"], frame_only=st["frame_only"],
                    unfinished=len(self._live_frames), fence_reasons=dict(self._defer_fences),
                    depth_hist={str(k): n for k, n in sorted(self._defer_depth.items())}, steps_walked=st["steps_walked"],
                    frame_updates=st["frame_updates"], peak_live_frames=self._defer_peak_live, live_frame_rounds=st["live_rounds"],
                    live_frames_hist={str(k): n for k, n in sorted(self._defer_live_hist.items())},
                    registration_latency_rounds=self.defer_cfg["latency"] * len(frames), registration_retries=st["registration_retries"],
                    carried_over_64=st["carried_over_64"], no_boundary0_script=st["no_boundary0_script"],
                    cache_miss_fences=self.sem.cache_miss_fences, P1_total=st["P1_total"], lazy_P1_saved=saved,
                    stall_rounds=st["stall_rounds"], settle_rounds=st["settle_rounds"], settle_patch_rounds=st["settle_patch_rounds"],
                    held_dependency=st["held_dependency"], held_materialize=st["held_materialize"], held_guard=st["held_guard"],
                    held_stop=st["held_stop"], held_step=st["held_step"], held_script_cell=st["held_script_cell"], **({"held_rule_a": st["held_rule_a"]} if st["held_rule_a"] else {}),   # (rule (a)'s waits)
                    stopped_before_dependency=sum(1 for fr in frames if 0 <= fr.stop < fr.c.F),
                    carried_to_initial={k[4:]: n for k, n in st.items() if k.startswith("top_")}, frames_shortened=st["frames_shortened"],
                    # audit finding 4: the FINAL reason each frame was carried no further than it was (a later frame can
                    # shorten one after registration), frames cut short = top < F, search-cap stops apart from tangles
                    carried_to_final=dict(collections.Counter(fr.top_why for fr in frames)),
                    frames_cut_short_final=sum(1 for fr in frames if fr.top < fr.c.F),
                    frames_cut_short_by=dict(collections.Counter(fr.top_why for fr in frames if fr.top < fr.c.F)),
                    shortened_from=dict(collections.Counter(fr.top_why_initial for fr in frames if fr.top_why == "shortened")),
                    no_cost_increase_stopped=sum(1 for fr in frames if 0 <= fr.stop < fr.top), steps_admitted=st["steps_admitted"],
                    admit_wait_rounds=st["admit_waits"], steps_taken_back=st["steps_taken_back"], search_caps=dict(tangle=st["tangle_cap"], waits=st["waits_search_cap"],
                                                                          guard=st["guard_search_cap"]),
                    guarded_resources=len(self._defer_guarded), exempt_pins=len(self._defer_exempt_pins), decide_wait_rounds=st["decide_wait_rounds"],
                    workspace_wait_rounds=st["workspace_wait_rounds"], workspace_wait_fenced_rounds=st["workspace_wait_fenced_rounds"], redecisions=st["redecisions"], monitor_violations=self._defer_violations,
                    log_fields=["vid", "F", "fence", "p_verdict", "p_run", "target", "how", "r_verdict", "r_target", "r_F", "reg_round",
                                "verdict_round", "done_round", "decide_waits", "ws_waits", "stall_rounds", "settle_patch_rounds", "updates", "stop",
                                "top", "top_why", "admit_waits", "first_ok"] + (list(H_LOG_FIELDS) if self._h is not None else []) +
                               (["recovery_end_round"] if self.defer_cfg.get("settle") == "decode" else []),
                    log=self._defer_log[:20000], log_live=self._defer_log_live(),   # (G, records only: frames live at the end)
                    **({"hybrid": self._h_summary()} if self._h is not None else {}),
                    **({"unified": self._u_summary()} if self._u is not None else {}),
                    **({"wager2": dict(self._w2.summary(), **self._r8_last_decisions(frames))} if self._w2 is not None else {}),   # (last_decision)
                    **({"r7": r7} if (r7 := {k[3:]: n for k, n in sorted(st.items()) if k.startswith("r7:")}) else {}),   # (the
                    # progress settings' counts -- refusals, kept-off instruction-rounds, search caps; absent with both off)
                    **({"r7_rec56": self._rec56_summary()} if self._rec56 is not None else {}),   # (plan v2 5.6's records)
                    **({"r9": self._r9_summary()} if self._r9 is not None else {}),   # (the bounded check's records)
                    **({"r7_contracts": dict(fields=["vid", "F", "admitted", "how", "measuring_steps"],     # (3(g) item 5,
                        rows=[[fr.vid, fr.c.F, fr.admitted, fr.how, fr.c.measuring_steps                  # records
                               if hasattr(fr.c, "measuring_steps") else self._r7_measuring_steps(fr.c)]       # only;
                              for fr in frames])}                                                  # an ended frame's
                       if self._r7 else {}))                                                       # from its end)

    def summary(self) -> dict:
        st = getattr(getattr(self, "_decode_sampler", None), "sampler_state", None)
        if st is not None:                                               # the latency table's refusal counter rides the ledger
            self.ledger.volume_out_of_range = st.table.volume_out_of_range
        s = self.ledger.to_dict()
        if self._nores is not None:                                        # Q-nores: the on-demand allocation's record
            s["nores"] = dict(self._nores)
        roles = s.pop("role_patch_rounds")                                 # by the lifetime's declared role (design Sec. 1.3)
        s.update({"role_%s_patch_rounds" % r: n for r, n in roles.items()})
        s["program_patch_rounds"] = roles.get("program", 0)                # the headline basis
        if self.client == "swiper":                                        # the SWIPER client's record
            pub = [v for v in self.values.values() if v.predicted_round >= 0]
            spec = [v for v in pub if v.speculated]
            acc = lambda vs: dict(n=len(vs), c_mean=(sum(v.c_pub for v in vs) / len(vs)) if vs else None,   # advertised at publication
                                  right=sum(1 for v in vs if v.prediction == v.truth))
            s["swiper_client"] = dict(f_flip=self.f_flip, **self._swc, never_published=len(self.values) - len(pub),
                                      published_acc=acc(pub), admitted_acc=acc(spec),
                                      c_hist=_c_hist(pub),
                                      wrong_without_reset=sum(1 for v in pub if v.prediction != v.truth and v.rc_round < 0))   # must be 0
        if self._strip_on:
            s["strip_client"] = self._strip_summary()
        elif self._cset is not None:                                       # (--pred-couple tail with another client, N's
            s["client_set"] = dict(self._cset, **dict(sorted(self._cstat.items())))   # reference: the coupling's counts; no
        #                                                                     prediction of the strip client exists to summarize)
        vd_ = [v for v in self.values.values() if v.delivered_round >= 0]   # (plan v2 8.2: the share of values whose
        nb_ = sum(1 for v in vd_ if 0 <= v.predicted_round < v.delivered_round)   #  prediction came before the verdict)
        s["pred_before_verdict"] = dict(values=len(vd_), predicted=sum(1 for v in vd_ if v.predicted_round >= 0), before=nb_,
                                        share=(nb_ / len(vd_)) if vd_ else None)
        if self._pool is not None:
            s["pool"] = self._pool.summary()
        s["program_qubits"] = len(self.es.program_wires)
        s["peak_nonprogram_cells"] = self._np_peak                         # W = ceil(peak / 2) of N's seed-1 run
        if self._sens_on:
            s["sensitivity"] = self._sens_summary()
        if self._common is not None:                                       # the common set's size and its refusals
            s["common"] = dict(values=len(self._common), outside=len(self._common_outside))
        if self.feedback:                                                  # w10: every classically controlled CZ
            s["feedback"] = [dict(fid=f.fid, gadget=f.gadget_id, bit=int(f.truth), anchor_end=self.schedule_instructions[f.anchor_idx].end_round,
                                  decoded=f.decoded_round, delivered=f.delivered_round,
                                  cz_start=min(self.schedule_instructions[i].start_round for i in f.group)) for f in self.feedback.values()]
        recs = [v.recovery for v in self.values.values() if v.recovery and v.recovery["kind"] == "script"]
        s.update(arm=self.arm, num_rounds=self.current_round, K=self.K, path=self.path, semantics=self.sem.describe(),
                 values_speculated=sum(1 for v in self.values.values() if v.speculated),
                 values_unverified=sum(1 for v in self.values.values() if not v.delivered),
                 values_unsettled=sum(1 for v in self.values.values() if (v.speculated or v.state is VState.RECOVERING) and not v.settled),
                 contracts=len(self.contracts), sites=sum(1 for c in self.contracts.values() if c.has_site),
                 mean_contract_depth=(sum(c.F for c in self.contracts.values()) / len(self.contracts)) if self.contracts else 0,
                 recovery_price_mismatch=sum(1 for r in recs if r["realised"] != r["P1"] and self._recovery_finished(r)),
                 recovery_realised_total=sum(r["realised"] for r in recs),
                 recovery_stall_rounds=[r["stall_rounds"] for r in recs][:1000],
                 recoveries_by_boundary={str(k): sum(1 for r in recs if r["step"] == k) for k in sorted({r["step"] for r in recs})},
                 delivery=dict(path=self.path, values=[(v.vid, v.decoded_round, v.delivered_round) for v in self.values.values()][:2000],
                               notifications=sum(len(v.notifications) for v in self.values.values())),
                 contracts_summary=[dict(vid=c.vid, F=c.F, fence=c.fence_reason, site=list(map(str, c.site_class()[:3])), n_s=c.n_s, refusals=c.refusals)
                                    for c in list(self.contracts.values())[:2000]])
        infl = sum(1 for r in recs if not self._recovery_finished(r))
        if infl:
            s["recovery_in_flight"] = infl                                 # (a censored run's recoveries still running at its cut)
        if self.arm == "D":
            s["deferral"] = self._defer_summary()
        if self._hw is not None:
            s["hw_limits"] = self._hw.summary()                            # rule 66's counters and peaks
        return s


def _golden_u():
    """golden model of U (`hw/golden/u_unit.py`, part B), imported on use: a run's state holds no module
    (it is pickled into checkpoints)."""
    from . import policy  # noqa: F401  (puts hw/ on the path)
    from golden import u_unit
    return u_unit


def _omc_of(c) -> int:
    """1 - c in Q0.20 as REGISTER and RECONFIDENCE carry it (the controller trace's conversion, `ScoutPolicy.omc_of`)."""
    from .policy import ScoutPolicy
    return ScoutPolicy.omc_of(c)


def check_recovery_frame_record(rec: dict) -> None:
    """Unit-level invariant (finding 1, device side): when the contract step's `frame_after` is non-identity, the
    recovery's ledger record must carry a non-empty frame (a non-identity Pauli key or at least one conditional
    generator).  Runs at every recovery completion (`_finish_recovery`)."""
    fr = rec.get("frame")
    assert fr is not None, "recovery record carries no frame"
    if fr["nonempty"]:
        assert fr["pauli"] not in ("+I", "I", "") or fr["cond"], "non-identity frame_after recorded as an empty frame: %s" % (fr,)


class _RejectionCoin:
    """The decoder manager's `rng`, replaced so that its ONE draw is keyed (round-2 findings A3 / B2).

    Upstream decides whether a speculation was rejected with `self.rng.random()` at decoder_manager.py:162 -- the only
    draw it takes from that generator -- once per (completed window, successor that speculated on it).  Sequentially: any
    extra decoder activity, a recovery's windows above all, shifted every later coin of the program.  The decision belongs
    to the two windows, so the coin is drawn from the stream named by their identities and the attempt number of that
    pair.  The two windows are the `task_idx` / `successor_idx` of the caller's loop; a call from anywhere else (upstream
    adding a second draw) falls back to the original generator and is counted in `decoder_key_fallbacks`, which must be 0.

    The coin is also where the adapter learns the DECISION (round-4 finding 2): a draw above the caller's own threshold
    (decoder_manager.py:162) is a rejected attempt, so (decision round, poisoned task, attempt) is logged on the device
    here, and the device routes one notification per affected undelivered value through the delivery queue at decision
    round + path -- independently of upstream's `_missed_speculation_events`, which exists only at lightweight_setting
    == 0.  The threshold mirrors the caller exactly: `spec_acc_modifier` of the parent for this successor and the number
    of touching faces between the two windows."""

    def __init__(self, dec, dev, draw_coin):
        self._dec, self._dev, self._draw = dec, dev, draw_coin
        self._base = dec.rng
        self._attempts: dict = collections.defaultdict(int)

    def random(self) -> float:
        f = sys._getframe(1)
        loc = f.f_locals if f.f_code.co_name == "step" else {}
        parent = self._dec._get_task_or_none(loc["task_idx"]) if isinstance(loc.get("task_idx"), int) else None
        successor = self._dec._get_task_or_none(loc["successor_idx"]) if isinstance(loc.get("successor_idx"), int) else None
        if parent is None or successor is None:
            self._dev.ledger.decoder_key_fallbacks += 1
            return float(self._base.random())
        pk, ph = self._dev._decode_key(parent, "reject")
        sk, sh = self._dev._decode_key(successor, "reject")
        key = (pk if pk.startswith("recovery_") else sk, ph, sh)
        self._attempts[key] += 1
        self._dev.ledger.decoder_rejection_draws += 1
        x = float(self._draw(*key, self._attempts[key]))
        faces = successor.window.count_touching_faces(parent.window)
        if x > (1 - (1 - self._dec.speculation_accuracy) * parent.speculation_modifiers.get(loc["successor_idx"], 1.0)) ** faces:
            self._dev._rejection_log.append((self._dec._current_round, loc["successor_idx"], self._attempts[key]))
        return x


class _PendingDecodeTasks(set):
    """The decoder's pending-decode set that remembers the last task removed: upstream removes the starting task
    immediately before drawing its decode latency (decoder_manager.py:420-422), which is how the keyed sampler learns
    which window attempt a draw belongs to (finding 8)."""

    def __init__(self, *a):
        super().__init__(*a)
        self.last_removed = None
        self.started_log = None                                        # U: a list of the windows that started

    def remove(self, x):
        self.last_removed = x
        if getattr(self, "started_log", None) is not None:
            self.started_log.append(x)
        super().remove(x)


class _AscMirror:
    """(A mixin for the capped decoder's pending sets; exact speed-up.) A sorted list of the set's elements,
    `_asc`, kept in step by every mutator upstream uses on those sets (add, remove, `|=`; decoder_manager.py:349, 362,
    409-411, 442, 458, 472); the others refuse, so the list can never go stale unnoticed."""

    def add(self, x):
        if x not in self:
            super().add(x)
            _asc_insert(self._asc, x)

    def remove(self, x):
        super().remove(x)
        del self._asc[bisect.bisect_left(self._asc, x)]

    def discard(self, x):
        if x in self:
            super().discard(x)
            del self._asc[bisect.bisect_left(self._asc, x)]

    def __ior__(self, other):
        new = sorted(x for x in other if x not in self)
        super().__ior__(other)
        for x in new:
            _asc_insert(self._asc, x)
        return self

    def _refuse(self, *a, **kw):
        raise NotImplementedError("a pending set keeps its sorted list in step through add, remove, discard and |= only")
    update = difference_update = intersection_update = symmetric_difference_update = pop = clear = _refuse
    __isub__ = __iand__ = __ixor__ = _refuse


def _asc_insert(a: list, x) -> None:
    if not a or x > a[-1]:
        a.append(x)                                                    # (new windows: indices grow with creation)
    else:
        bisect.insort(a, x)


class _PendingDecodeTasksFIFO(_AscMirror, _PendingDecodeTasks):
    """The pending-decode set of a CAPPED decoder: the union upstream's `update_decoding` walks each round (pending decodes |
    pending speculations, decoder_manager.py:427) comes back as `_OldestFirst`, so the cap's `break` (decoder_manager.py:436)
    hands the free decoders to the oldest ready windows (window indices grow with creation). Without a cap every pending
    task is visited each round and the visiting order has no effect, which is why only capped runs install this.

     (exact speed-up; in a run whose decoding falls behind, the union and its heap cost O(queue) every round): with the
    speculations' set a `_PendingSpeculations`, both keep sorted lists, and the union is read from them -- the oldest
    window alone when every decoder is busy (`_CapReached`), else a cursor over the two lists (`_OldestFirstView`).
    E2E_SCAN_CHECK=1 checks both against the `_OldestFirst` they replace. with a `_RunnableSet` attached (`_rq`), what it adds is re-tested at the next count and what it removes leaves
    the runnable set."""

    _rq = None
    _diag_add = None                    # (--diag-records: task -> the round it last entered the queue; records only)

    def __init__(self, *a):
        super().__init__(*a)
        self._asc = sorted(self)

    def add(self, x):
        super().add(x)
        if self._rq is not None:
            self._rq.dirty.add(x)
        if self._diag_add is not None:
            self._diag_add[x] = self._dec._current_round

    def remove(self, x):
        super().remove(x)
        if self._rq is not None:
            self._rq.run.discard(x)

    def discard(self, x):
        super().discard(x)
        if self._rq is not None:
            self._rq.run.discard(x)

    def __ior__(self, other):
        super().__ior__(other)
        if self._rq is not None:
            self._rq.dirty.update(other)
        if self._diag_add is not None:
            for x in other:
                self._diag_add[x] = self._dec._current_round
        return self

    def __or__(self, other):
        dec = getattr(self, "_dec", None)
        if dec is None or not isinstance(other, _PendingSpeculations):
            return _OldestFirst(set.__or__(self, other))
        a, b = self._asc, other._asc
        check = getattr(dec, "_check_descendants", False)
        if check:
            assert a == sorted(self) and b == sorted(other), "a pending set's sorted list is stale"
        if len(dec._active_window_progress) >= dec.max_parallel_processes and (a or b):
            # every decoder is busy, so upstream's loop pops the oldest window, checks it and stops at the cap
            # (decoder_manager.py:430-436): nothing else of the union is read
            lo = a[0] if a and (not b or a[0] < b[0]) else b[0]
            if check:
                assert lo == min(set.__or__(self, other))
            return _CapReached(lo)
        view = _OldestFirstView(a, b)
        return _CheckedOldest(view, _OldestFirst(set.__or__(self, other))) if check else view


class _PendingSpeculations(_AscMirror, set):
    """The pending-speculation set of a capped decoder, with its sorted list (see `_PendingDecodeTasksFIFO`)."""

    def __init__(self, *a):
        super().__init__(*a)
        self._asc = sorted(self)


def _runnable(dec, t) -> bool:
    """Upstream's start test of a pending full decode (decoder_manager.py:451-455): every predecessor has decoded or
    speculated.  A window without a task is not counted."""
    return dec._get_task_or_none(t) is not None and all(
        dec._completed_decoding(p) or dec._completed_speculation(p) for p in dec._window_idx_dag.predecessors(t))


def _runnable_scan(dec, limit: int) -> int:
    """`_runnable_pending` by a scan of the pending windows, oldest first, up to `limit`."""
    pend = dec._pending_decode_tasks
    n = 0
    for t in (pend._asc if hasattr(pend, "_asc") else sorted(pend)):
        if _runnable(dec, t):
            n += 1
            if n >= limit:
                break
    return n


class _RunnableSet:
    """(exact speed-up; profile of main:21:B05: the scan of every pending decode and its predecessors
    took 76 % of a V2*-Sd run under the round-7 records, and V2q's tick and QG count at every round): the pending full
    decodes that pass upstream's start test (`_runnable`), kept as a set, `run`. A window's test reads its membership
    of the pending set, its task, its predecessors and their two completion flags. Whatever changes one of those
    reports the windows whose test it may change (`dirty`), and only those are tested again at the next count:
      - the capped decoder's pending set (`_PendingDecodeTasksFIFO`): what it adds is re-tested, what it removes leaves
        `run` (a new window's task is set in the same `update_decoding`, before any count);
      - the decoder (`ScoutDecoderManager`): the successors of the windows whose decode or speculation completed in a
        step and of every reset window (decode or speculation), upstream's only writes of the two flags
        (decoder_manager.py:162, 207, 346, 360; 447 and 477 complete a speculation at once only at a speculation time
        of 0, where the scan stays), and of each new window (its task is new);
      - the window DAG (`_OrderedDAG`): the head of each edge it adds or removes, a removed node's successors (upstream's
        sliding manager adds edges only between unconstructed windows, which hold no task, so a pending window's
        predecessors never change; kept so that the set stays exact if they do).
    Built by one scan at the decoder's first count; a count is then min(limit, |run|), as the scan's. E2E_SCAN_CHECK=1
    (the decoder's `_check_descendants`) asserts `run` against the scan at every count."""

    DIRTY_CAP = 65536       # (between counts far apart: `update_decoding` re-tests once this many windows wait)

    def __init__(self, dec):
        self.dag = dec._window_idx_dag
        self.run = {t for t in dec._pending_decode_tasks if _runnable(dec, t)}
        self.dirty = set()
        dec._rq = dec._pending_decode_tasks._rq = self.dag._rq = self

    @staticmethod
    def of(dec):
        """The decoder's set, up to date; None where the scan stays (an uncapped decoder, a plain window DAG, or a
        speculation time of 0)."""
        rq = getattr(dec, "_rq", None)
        if rq is None:
            if not (isinstance(dec, ScoutDecoderManager) and isinstance(dec._pending_decode_tasks, _PendingDecodeTasksFIFO)
                    and isinstance(dec._window_idx_dag, _OrderedDAG) and not (dec.speculation_mode and dec.speculation_time <= 0)):
                return None
            rq = _RunnableSet(dec)
        else:
            rq.refresh(dec)
        if dec._check_descendants:
            assert rq.run == {t for t in dec._pending_decode_tasks if _runnable(dec, t)}, "the runnable set is stale"
        return rq

    def refresh(self, dec):
        assert dec._pending_decode_tasks._rq is self and dec._window_idx_dag is self.dag, "the runnable set lost a hook"
        d = self.dirty
        if d:
            pend, run = dec._pending_decode_tasks, self.run
            for t in d:
                if t in pend and _runnable(dec, t):
                    run.add(t)
                else:
                    run.discard(t)
            d.clear()

    def touch(self, windows):
        """Windows whose completion flags may have changed: their successors are re-tested."""
        succ = self.dag._succ
        for p in windows:
            s = succ.get(p)
            if s:
                self.dirty.update(s)


class _CapReached:
    """The pending windows when the decoder cap is reached: upstream's loop checks its length, pops one window (the
    oldest), checks it and stops at the cap; nothing else is ever asked of it."""
    __slots__ = ("_oldest",)

    def __init__(self, oldest):
        self._oldest = oldest

    def __len__(self):
        return 1 if self._oldest is not None else 0

    def pop(self):
        x, self._oldest = self._oldest, None
        if x is None:
            raise KeyError("pop from an empty set")
        return x


class _OldestFirstView:
    """The union upstream's loop walks when a decoder is free, read in ascending order from the two sorted lists with a
    cursor.  The loop removes from the two sets only windows it has popped and adds none, so the elements above the
    cursor are exactly the union's that remain.  `|=` (upstream's, only with a zero speculation time) turns it into
    the `_OldestFirst` of what remains."""
    __slots__ = ("_a", "_b", "_last", "_rest")

    def __init__(self, a, b):
        self._a, self._b, self._last, self._rest = a, b, None, None

    def _next(self):
        a, b, last = self._a, self._b, self._last
        i = 0 if last is None else bisect.bisect_right(a, last)
        j = 0 if last is None else bisect.bisect_right(b, last)
        x = a[i] if i < len(a) else None
        y = b[j] if j < len(b) else None
        if x is None or (y is not None and y < x):
            return y
        return x

    def __len__(self):
        if self._rest is not None:
            return len(self._rest)
        return 0 if self._next() is None else 1                        # (upstream only asks whether any remain)

    def pop(self):
        if self._rest is not None:
            return self._rest.pop()
        x = self._next()
        if x is None:
            raise KeyError("pop from an empty set")
        self._last = x
        return x

    def __ior__(self, other):
        if self._rest is None:
            last = self._last
            self._rest = _OldestFirst({x for x in itertools.chain(self._a, self._b) if last is None or x > last})
        self._rest |= other
        return self


class _CheckedOldest:
    """E2E_SCAN_CHECK=1: the view, and the `_OldestFirst` it replaces, popped together."""
    __slots__ = ("_v", "_o")

    def __init__(self, v, o):
        self._v, self._o = v, o

    def __len__(self):
        n, m = len(self._v), len(self._o)
        assert (n > 0) == (m > 0), "the union view's length differs"
        return m

    def pop(self):
        x, y = self._v.pop(), self._o.pop()
        assert x == y, ("the union view pops another window", x, y)
        return y

    def __ior__(self, other):
        self._v |= other
        self._o |= other
        return self


class _OldestFirst(set):
    """A set whose pop() returns its smallest element (a heap beside the set); `|=` keeps both in step."""

    def __init__(self, it=()):
        super().__init__(it)
        self._heap = list(self); heapq.heapify(self._heap)

    def pop(self):
        while self._heap:
            x = heapq.heappop(self._heap)
            if x in self:
                self.remove(x)
                return x
        raise KeyError("pop from an empty set")

    def __ior__(self, other):
        for x in other:
            if x not in self:
                heapq.heappush(self._heap, x)
        set.__ior__(self, other)
        return self


NX_TESTED = "3.4.2"   # the networkx whose subgraph views and topological_generations `_topo_order` reproduces (the venv's)
CHAIN_MIN, CHAINS_MAX = 32, 32   # `_OrderedDAG._chain` caches a walked chain this long, and keeps at most this many


class _OrderedDAG(nx.DiGraph):
    """The window DAG with a topological order kept under every edge insertion (Pearce and Kelly, "A dynamic
    topological sort algorithm for directed acyclic graphs", 2006), so that `_topo_order` knows the whole graph is
    acyclic. A new node takes the next order value. An edge u -> v that the order already
    agrees with costs nothing. Otherwise the nodes reachable from v below u's order, and those reaching u above v's,
    swap order values; reaching u from v closes a cycle, and `cyclic` is set for good (the decoder then uses
    upstream's sort for the rest of the run, which raises where a sorted subgraph has a cycle). Every networkx
    structure, and so every iteration order, is the plain DiGraph's: the order lives beside it. it also keeps `_chain`'s cache, chains of windows each of which, but the last, has exactly one successor, the
    next window of the chain (a patch's windows in time, where nothing merges): a breadth-first walk from a window of a
    chain meets the rest of the chain one window a level, in its order, before anything else. A chain is in increasing
    window order (`_chain` finds a window by bisection). An edge out of a chain's last window extends it (when the new
    window is later); an edge out of any other of its windows splits it there; a removed node or edge drops the cache. with a `_RunnableSet` attached (`_rq`), a window whose predecessors change is re-tested at its next count."""

    _rq = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._ord, self._next, self.cyclic = {}, 0, False
        self._chains, self._chain_end = [], {}                           # the chains, and the chain each last window ends
        for n in self._succ:
            self._ord[n] = self._next; self._next += 1
        for u, v in list(self.edges):
            self._check(u, v)

    def _new(self, n):
        if n not in self._succ:
            self._ord[n] = self._next; self._next += 1

    def add_node(self, n, **attr):
        self._new(n)
        super().add_node(n, **attr)

    def add_nodes_from(self, nodes, **attr):
        for n in nodes:                                                  # (networkx's order; (node, attributes) as its own)
            if isinstance(n, tuple) and len(n) == 2 and isinstance(n[1], dict):
                self.add_node(n[0], **dict(attr, **n[1]))
            else:
                self.add_node(n, **attr)

    def add_edge(self, u, v, **attr):
        self._new(u); self._new(v)
        new = not (u in self._succ and v in self._succ[u])
        had = len(self._succ[u]) if u in self._succ else 0
        super().add_edge(u, v, **attr)
        if new:
            self._check(u, v)
            if self._chains:
                self._chain_edge(u, v, had)
            if self._rq is not None:
                self._rq.dirty.add(v)

    def add_edges_from(self, ebunch, **attr):
        for e in ebunch:
            self.add_edge(e[0], e[1], **(dict(attr, **e[2]) if len(e) == 3 else attr))

    def remove_node(self, n):
        if self._rq is not None and n in self._succ:
            self._rq.dirty.update(self._succ[n])
        super().remove_node(n)
        self._ord.pop(n, None)
        self._chains, self._chain_end = [], {}

    def remove_edge(self, u, v):
        super().remove_edge(u, v)
        self._chains, self._chain_end = [], {}
        if self._rq is not None:
            self._rq.dirty.add(v)

    def remove_edges_from(self, ebunch):
        ebunch = list(ebunch)
        super().remove_edges_from(ebunch)
        self._chains, self._chain_end = [], {}
        if self._rq is not None:
            self._rq.dirty.update(e[1] for e in ebunch)

    def _chain_edge(self, u, v, had):
        """a new edge u -> v; u had `had` successors before it."""
        if had == 0:
            c = self._chain_end.get(u)
            if c is not None and v > u:                                  # (else the chain ends at u, now with one successor)
                c.append(v)
                del self._chain_end[u]
                self._chain_end.setdefault(v, c)
            return
        for c in list(self._chains):
            i = bisect.bisect_left(c, u)
            if i < len(c) - 1 and c[i] == u:                             # u had one successor, the chain's next window
                rest = c[i + 1:]
                del c[i + 1:]
                keep = len(rest) >= CHAIN_MIN
                if self._chain_end.get(rest[-1]) is c:
                    del self._chain_end[rest[-1]]
                    if keep:
                        self._chain_end[rest[-1]] = rest
                if keep:
                    self._chain_add(rest)
                self._chain_end.setdefault(u, c)

    def _chain_add(self, c):
        self._chains.append(c)
        if len(self._chains) > CHAINS_MAX:
            drop = self._chains.pop(0)
            if self._chain_end.get(drop[-1]) is drop:
                del self._chain_end[drop[-1]]

    def _chain(self, src):
        """the windows below src down single successors, in order, while the window order increases ([] if src has
        no single later successor): a slice of a cached chain, or walked (and cached from CHAIN_MIN windows)."""
        for c in self._chains:
            i = bisect.bisect_left(c, src)
            if i < len(c) and c[i] == src:
                return c[i + 1:]
        succ, path, x = self._succ, [], src
        while len(succ[x]) == 1:
            (y,) = succ[x]
            if y <= x:
                break
            path.append(y); x = y
        if len(path) >= CHAIN_MIN:
            c = [src] + path
            old = self._chain_end.get(x)
            if old is not None and c[bisect.bisect_left(c, old[0])] == old[0]:
                self._chains = [d for d in self._chains if d is not old]  # (old is the new chain's tail: both go down single
                old = None                                                #  successors to x)
            if old is None:
                self._chain_end[x] = c
            self._chain_add(c)
        return path

    def remove_nodes_from(self, nodes):
        for n in list(nodes):
            if n in self._succ:
                self.remove_node(n)

    def _check(self, u, v):
        if self.cyclic:
            return
        o = self._ord
        lb, ub = o[v], o[u]
        if lb > ub:
            return                                                       # the order already agrees
        if u == v:
            self.cyclic = True
            return
        fwd, seen, stack = [], {v}, [v]                                  # reachable from v, below u's order
        while stack:
            x = stack.pop()
            fwd.append(x)
            for w in self._succ[x]:
                if w == u:
                    self.cyclic = True                                   # v reaches u: the edge closes a cycle
                    return
                if w not in seen and o[w] < ub:
                    seen.add(w); stack.append(w)
        back, seen, stack = [], {u}, [u]                                 # reaching u, above v's order
        while stack:
            x = stack.pop()
            back.append(x)
            for w in self._pred[x]:
                if w not in seen and o[w] > lb:
                    seen.add(w); stack.append(w)
        back.sort(key=o.__getitem__); fwd.sort(key=o.__getitem__)
        pool = sorted(o[x] for x in back + fwd)
        for x, k in zip(back + fwd, pool):                               # those reaching u first, then those v reaches
            o[x] = k


class ScoutSlidingWindowManager(SlidingWindowManager):
    """Upstream's sliding window manager with its window DAG an `_OrderedDAG` (built empty, before any window).
    with `h_deep` set (run.py, --h-depth above 1) its builder counts an H's syndrome rounds, not its fillers
    (`hdepth.DeepWindowBuilder`); unset, upstream's builder."""

    h_deep = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        assert not self.window_dag, "the window DAG is not empty at construction"
        self.window_dag = _OrderedDAG()
        if self.h_deep:
            assert type(self.window_builder) is _hd.WindowBuilder and not self.window_builder._all_rounds, "a used builder"
            self.window_builder.__class__ = _hd.DeepWindowBuilder


class _VerifyLog(set):
    """The decoder's decoded-unverified set (upstream's `_decoded_unverified_tasks`), logging the windows each step's
    verification removes (upstream's `-=`), so U's pool events see the verified windows without copying the set every
    round. A reset leaves through `remove` and is not logged (it is not a verification)."""

    def __init__(self, *a):
        super().__init__(*a)
        self.removed: list = []

    def __isub__(self, other):
        self.removed.extend(x for x in other if x in self)
        return super().__isub__(other)


class _Fenwick:
    """Prefix sums over non-negative integer keys (window indices), growing as keys arrive: `prefix(i)` is the sum of
    the values at keys < i.  Integer values (Q24.8 mean services), so the sums are exact."""
    __slots__ = ("t", "v")

    def __init__(self, n: int = 1024):
        self.t, self.v = [0] * (n + 1), {}

    def add(self, i: int, x: int):
        self.v[i] = self.v.get(i, 0) + x
        if i + 1 >= len(self.t):
            n = len(self.t) - 1
            while i + 1 > n:
                n *= 2
            self.t = [0] * (n + 1)
            for k, y in self.v.items():
                self._add(k, y)
            return
        self._add(i, x)

    def _add(self, i: int, x: int):
        i += 1
        n = len(self.t)
        while i < n:
            self.t[i] += x
            i += i & -i

    def prefix(self, i: int) -> int:
        i = min(i, len(self.t) - 1)
        s = 0
        while i > 0:
            s += self.t[i]
            i -= i & -i
        return s


class ScoutDecoderManager(DecoderManager):
    """Upstream's decoder manager with one exact speed-up (the profile of smoke, COMMON_RULES rule 8): in a
    capped run whose decoding falls behind, upstream's `_topologically_sort` (decoder_manager.py:324) took 60-73 % of the
    time, and its share grew with the backlog. It sorts the subgraph induced by the round's completed windows and all
    their descendants through networkx's filtered graph views. `_topo_order` computes the same list on the graph's own
    dicts. E2E_SCAN_CHECK=1 (the decoder's `_check_descendants`) asserts every call against upstream's."""

    _r7_rec = None          # (round 7's records, 3(b), set by the device: speculations started, children marked, the excerpt)
    _rq = None              # (the runnable pending decodes, `_RunnableSet`, from the first count)

    def step(self):
        """Upstream's step; with U, its completions, verifications and resets become register events. the
        windows whose decode or speculation completed are reported to the runnable set (resets report themselves)."""
        dev, now = getattr(self, "_scout_dev", None), self._current_round
        rq = self._rq
        spec = list(self._active_speculation_progress) if rq is not None else None
        if dev is None or not dev._u_on:
            completed = super().step()
            if rq is not None:
                self._rq_stepped(rq, spec, completed)
        else:
            log = self._decoded_unverified_tasks.removed                 # (`_VerifyLog`: this step's verifications)
            del log[:]
            self._u_resets = []
            completed = super().step()
            if rq is not None:
                self._rq_stepped(rq, spec, completed)
            verified = {t for t in log if self._is_verified_task(t)}     # (a decoded window reset by a failed speculation is not)
            dev._u_decoder_step(self, completed, sorted(verified), self._u_resets)
            self._u_resets = None
        if self._r7_rec is not None and self._r7_rec["x"]:
            self._r7_follow(now)
        return completed

    def _rq_stepped(self, rq, spec, completed):
        """the step's completed decodes, and the speculations that left the active set (completed, or reset)."""
        rq.touch(completed)
        act = self._active_speculation_progress
        rq.touch([t for t in spec if t not in act])

    def _reset_decode_task(self, task_idx):
        log = getattr(self, "_u_resets", None)
        if log is not None:
            log.append(task_idx)
        rec = self._r7_rec
        if rec is not None and len(rec["x"]) < 2:                        # (round 7's excerpt: the first two poisoned windows)
            t, nf = self._get_task(task_idx), self._not_fully_decoded_instructions
            rec["x"].append(dict(window=task_idx, poisoned_round=self._current_round, decode_started=t.decoding_start_time,
                                 decoded=t.decoding_completion_time if t.completed_decoding else None,
                                 parents=sorted(self._window_idx_dag.predecessors(task_idx)),
                                 children=sorted(self._window_idx_dag.successors(task_idx)),
                                 instructions={str(i): None for i in sorted(i for i, ts in self._instruction_tasks.items()
                                                                            if task_idx in ts and i in nf)[:16]}))
        out = super()._reset_decode_task(task_idx)
        if self._rq is not None:                                         # (its decode flag may have dropped)
            self._rq.touch((task_idx,))
        return out

    def _reset_speculate_task(self, task_idx):
        out = super()._reset_speculate_task(task_idx)
        if self._rq is not None:                                         # (its speculation flag may have dropped)
            self._rq.touch((task_idx,))
        return out

    def _poisoned_task_reset_children_that_used_decoding(self, task_idx, only_mark_speculated=False):
        rec = self._r7_rec
        if rec is not None and only_mark_speculated:                     # (round 7's records: the children upstream marks)
            for c in self._window_idx_dag.successors(task_idx):
                ch = self._get_task_or_none(c)
                if ch and ch.decoding_start_time != -1 and not ch.used_parent_speculations[task_idx]:
                    rec["marked"] += 1
        return super()._poisoned_task_reset_children_that_used_decoding(task_idx, only_mark_speculated)

    def _r7_follow(self, now: int) -> None:
        """Round 7's excerpt (records only): each poisoned window's re-decode, its verification and its instructions'."""
        nf = self._not_fully_decoded_instructions
        for x in self._r7_rec["x"]:
            if x.get("_done"):
                continue
            t = self._get_task_or_none(x["window"])
            if t is None:
                x["verified"], x["_done"] = None, True                      # (purged)
                continue
            if "redecode_started" not in x and t.decoding_start_time != -1:
                x["redecode_started"] = t.decoding_start_time
            if "redecoded" not in x and t.completed_decoding:
                x["redecoded"] = t.decoding_completion_time
            for i, r in x["instructions"].items():
                if r is None and int(i) not in nf:
                    x["instructions"][i] = now                             # (the step in which its last window verified)
            if "verified" not in x and self._is_verified_task(x["window"]):
                x["verified"] = now
            x["_done"] = "verified" in x and None not in x["instructions"].values()

    def update_decoding(self, new_windows, purged_indices, window_idx_dag):
        """Upstream's; the device's hook runs after it (`ScoutDeviceManager._after_full_starts`).  Under the shared pool
        the full decodes start on all B cores and the hook preempts the strip jobs they need (rule 42a), or, in the
        non-preemptive ablation, on B less the cores strip jobs hold."""
        s0 = None if self._r7_rec is None else set(self._active_speculation_progress)   # (round 7's records)
        dev = getattr(self, "_scout_dev", None)
        pool = getattr(dev, "_pool", None) if dev is not None else None
        if pool is None or pool.preemptive:                              # (rule 42a: B cores; the hook preempts strip jobs)
            super().update_decoding(new_windows, purged_indices, window_idx_dag)
        else:
            cap = pool.B - pool.busy(self._current_round)
            self.max_parallel_processes = cap if cap > 0 else -1       # (-1: upstream's cap test stops at once; 0 means none)
            try:
                super().update_decoding(new_windows, purged_indices, window_idx_dag)
            finally:
                self.max_parallel_processes = pool.B
        rq = self._rq
        if rq is not None:                                               # (a new window's task, should one replace another,
            rq.touch([w.window_idx for w in new_windows])                # re-tests its successors; counts far apart keep
            if len(rq.dirty) > rq.DIRTY_CAP:                             # the set small)
                rq.refresh(self)
        if dev is not None:
            dev._after_full_starts(self, new_windows)
        if s0 is not None:
            self._r7_rec["started"] += len(self._active_speculation_progress.keys() - s0)   # (the speculations started)

    def _topologically_sort(self, task_indices):
        out = _topo_order(self._window_idx_dag, task_indices)
        if out is None or self._check_descendants:
            if out is None and len(task_indices):
                self.topo_fallbacks = getattr(self, "topo_fallbacks", 0) + 1     # upstream's sort, by precondition
            ref = DecoderManager._topologically_sort(self, task_indices)
            assert out is None or out == ref, ("topological order differs from upstream's", list(task_indices)[:8])
            return ref
        return out


def _topo_order(G, task_indices):
    """Upstream's `_topologically_sort` result, under networkx 3.4.2 (the deploy's venv):
        sub = nx.subgraph(G, set(chain.from_iterable(nx.descendants(G, i) for i in task_indices)) | set(task_indices))
        [i for i in nx.topological_sort(sub) if i in task_indices]
    computed on G's dicts. Every set is built by the same operations in the same order as networkx builds it, so CPython
    lays it out, and iterates it, the same way: each descendant set in bfs_edges' discovery order (generic_bfs_edges,
    depth limit len(G)); the induced node set as show_nodes builds it from nbunch_iter; the subgraph's nodes in the
    order FilterAdjacency iterates them (that set's own order when it holds under half of G's nodes, else G's order);
    the in-degrees as InDegreeView counts them; and Kahn's generations as topological_generations runs them, with each
    node's successors in G's order. None when G is a view, a multigraph or undirected, a window is not in G, or the
    subgraph has a cycle: the caller then runs upstream's, which raises as it did.

     (profile: 61 % of a runaway FAC-A-Sd unit's time was here, walking each completed window's descendants,
    by then a patch's windows in time one after another): a walk's first part, down single successors, is read from
    `_OrderedDAG._chain`'s cache -- the same windows in the same order, so the same set -- and goes on from the chain's
    last window as before; the filters run in C; the subgraph's node order is read until it holds all of A.

    Preconditions, enforced: G is an `_OrderedDAG` that has never had a cycle, so no subgraph
    has one (the single-window shortcut and the restriction to the completed windows' ancestors rely on it), and the
    installed networkx is the tested one (`NX_TESTED`). Otherwise None: upstream's sort."""
    if len(task_indices) == 0:
        return []
    if type(G) is not _OrderedDAG or G.cyclic or nx.__version__ != NX_TESTED:
        return None
    if hasattr(G, "_NODE_OK") or G.is_multigraph() or not G.is_directed():
        return None
    adj, pred = G._adj, G._pred
    n = len(G._node)
    if any(i not in adj for i in task_indices):
        return None
    if len(task_indices) == 1:
        return list(task_indices)                                    # a single completed window comes back alone

    def descendants(src):
        head = G._chain(src)                                         # src's chain below it, as the walk meets it
        if head:
            src = head[-1]
        seen, out, level, depth = {src}, set(head), [src], len(head)
        while level and depth < n:
            nxt = []
            for parent in level:
                for child in adj[parent]:
                    if child not in seen:
                        seen.add(child); out.add(child); nxt.append(child)
                if len(seen) == n:
                    return out
            level, depth = nxt, depth + 1
        return out
    nodes = set(itertools.chain.from_iterable(descendants(i) for i in task_indices)) | set(task_indices)
    show = set(filter(adj.__contains__, nodes))
    # Kahn's generations need only A, the nodes of the subgraph with a path to a completed window: A holds every
    # predecessor its nodes have in the subgraph, so their in-degrees, generations and relative order (each generation's
    # list keeps the order in which its nodes' last predecessors were visited, and each node's successors in G's order)
    # are the full subgraph's; the other nodes only interleave with them.  (A cycle outside A, which a DAG never has,
    # would go unnoticed here; upstream would raise.)
    A, stack = set(task_indices), list(task_indices)
    while stack:
        for q in pred[stack.pop()]:
            if q in show and q not in A:
                A.add(q); stack.append(q)
    order = list(itertools.islice(filter(A.__contains__, show if 2 * len(show) < len(adj) else adj), len(A)))   # (A is in both)
    indeg, zero = {}, []
    for v in order:
        k = sum(1 for q in pred[v] if q in A)
        if k > 0:
            indeg[v] = k
        else:
            zero.append(v)
    out = []
    while zero:
        gen, zero = zero, []
        for node in gen:
            for child in adj[node]:
                if child in A:
                    indeg[child] -= 1
                    if indeg[child] == 0:
                        zero.append(child)
                        del indeg[child]
        out.extend(gen)
    if indeg:
        return None
    return [i for i in out if i in task_indices]


def _c_hist(values) -> dict:
    """Advertised confidence at publication (to 3 decimals) -> [published, right]: the SWIPER client's calibration record."""
    h: dict = {}
    for v in values:
        e = h.setdefault("%.3f" % v.c_pub, [0, 0]); e[0] += 1; e[1] += int(v.prediction == v.truth)
    return dict(sorted(h.items()))


def codebook_class(classes, site):
    """The codebook class covering a site: same F, durations and n_s, class prices >= the site's P1 elementwise (P3 is an
    upper bound of P1, Sec. 2.4); the tightest such class, or None (a class miss -> WAIT)."""
    F, durs, prices, n_s = site
    best = None
    for c in classes:
        cF, cd, cp, cn = c[0], tuple(c[1]), tuple(c[2]), c[3]
        if cF == F and cd == tuple(durs) and cn == n_s and all(a <= b for a, b in zip(prices, cp)):
            if best is None or sum(cp) < sum(best[2]):
                best = (cF, cd, cp, cn)
    return best


def sx_price(d: int, path: int, table):
    """(sx library): the price the device sets as `Semantics.price` (`ScoutDeviceManager._sx_price`, S + XS in rounds by the
    profile builder's own functions) at distance d, path delay `path` and latency table `table`, for a caller that builds its
    own `Semantics` with no device (the offline C4 gates, `check_contracts` and `sweep.c4_gate`). The device's two methods run
    on a namespace holding those three inputs, so the price is the device's code, not a copy."""
    ns = SimpleNamespace(d_t=int(d), path=int(path), _eta_table=table, _u_mu_cache={})
    ns._u_mu = lambda volume: ScoutDeviceManager._u_mu(ns, volume)
    return lambda low: ScoutDeviceManager._sx_price(ns, low)
