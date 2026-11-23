"""The strip client (gap analysis rule 11; design note).

 strip decoder in its pipelined form, simulated inside the program run. For every T value a stream decodes
the value's data patch -- the plaquettes within floor(d/2) lines of the merge seam plus the regions of the gadgets that ran
on the patch since the anchor -- in chained windows from the full decoder's committed frame, then the final window
[t_m - d, t_m + floor(d/2)), on one shared pool of strip cores, and the merge parity is published the round after the
final window's decode.

This module holds what does not need the device:
  - `Geometry`: a window's detector count (its latency class), geometry as per-round densities calibrated on
     528 measured windows (froq/data/strip_geometry.json);
  - `Accuracy`: measured q (froq/data/strip_points.csv = results/points.csv) with rule 11's mapping;
  - `accuracy_class`: class of what ran on the patch since the anchor; `executed_kinds`, what that is under the
    corrected class rule (part 1c: the program's gadgets and the gadgets scripts spliced onto the patch);
  - the geometry rule (part 1d, `StripPool`): "corrected" sizes each window from the program's gadgets and the
    spliced gadgets that have begun, plus a catch-up for history decoded before a splice widened it; "legacy", v7's,
    from the program's gadgets alone;
  - `StripPool`: the streams and the strip-core pool, driven by the device through events (`open`, `merge_started`,
    `cancel`) and one `step(now)` per round, with two callbacks: the patch's committed frame and a gadget's executed span;
  - `isolated_merge`: rule 11's validation, one merge on an isolated patch at frame age d, unlimited cores.

Times inside the strip decoder are continuous rounds: a window started at s with a latency sample of tau rounds finishes
at s + tau (SWIPER-SIM's decoders count a fractional sample down the same way; timing rule sums samples). The
device sees integer rounds: a value is published in round max(ceil(finish) + 1, merge end + 1), merge end = t_m + d."""
from __future__ import annotations

import csv, hashlib, heapq, json, math, os
from dataclasses import dataclass, field

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
GEOMETRY_PATH = os.path.join(DATA, "strip_geometry.json")
POINTS_PATH = os.path.join(DATA, "strip_points.csv")


def set_calibration(calib_dir: str | None = None):
    """(plan v2, section 3.1; `froq.run --strip-calib DIR`): the strip client's two tables for the
    whole run from DIR (its `strip_geometry.json` and `strip_points.csv`, e.g. E3's tables extended to d = 15, 19 and
    23), read by the device's Geometry and Accuracy and hashed into the run's settings as
    froq/data's are; None: froq/data's. DIR's tables extend froq/data's: every distance froq/data holds keeps its densities
    and its rows unchanged, and the points' columns are the same, or this refuses."""
    global GEOMETRY_PATH, POINTS_PATH
    base_g, base_p = os.path.join(DATA, "strip_geometry.json"), os.path.join(DATA, "strip_points.csv")
    if not calib_dir:
        GEOMETRY_PATH, POINTS_PATH = base_g, base_p
        return
    new_g, new_p = os.path.join(calib_dir, "strip_geometry.json"), os.path.join(calib_dir, "strip_points.csv")
    old_dens, dens = Geometry(base_g).by_d, Geometry(new_g).by_d
    bad = sorted(d for d in old_dens if dens.get(d) != old_dens[d])
    assert not bad, "--strip-calib: %s changes the measured densities at d = %s" % (new_g, bad)

    def rows(path):
        rd = csv.DictReader(open(path))
        by_d = {}
        for r in rd:
            by_d.setdefault(int(r["d"]), []).append(tuple(r[k] for k in rd.fieldnames))
        return rd.fieldnames, by_d
    (old_cols, old_rows), (cols, new_rows) = rows(base_p), rows(new_p)
    assert cols == old_cols, "--strip-calib: %s has other columns than %s" % (new_p, base_p)
    bad = sorted(d for d in old_rows if new_rows.get(d) != old_rows[d])
    assert not bad, "--strip-calib: %s changes the measured rows at d = %s" % (new_p, bad)
    GEOMETRY_PATH, POINTS_PATH = new_g, new_p
AGES = (1, 3, 6, 12)                                                  # frame ages, in units of d
# classes: what ran on the patch since the frame
MEASURED_SEQUENCES = {(): "none", ("H",): "H", ("S",): "S", ("S", "H"): "SH", ("H", "S"): "HS", ("CNOT_t",): "CNOTt",
                      ("CNOT_c",): "CNOTc", ("T",): "T"}
SKIPPED = "skipped"                                                    # span of a gadget whose instructions all ran with duration 0


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------------------ geometry
class Geometry:
    """Detector count of a strip window: geometry in per-round densities (strip_geometry.json, per d).

    count([lo, hi)) = the strip's density summed over the window's rounds -- `hist` before [t_m - d, t_m), `pre` in it
    (the magic patch's injection beside the seam), `merge` in the merge rounds -- plus, for every gadget since the anchor,
    its region's density over the rounds of each of its phases:
      H `hist` over [s, e + 1) (the extension continues the strip across a tile)
      T `T_pre` over [m_s - d, m_s), `T_near` over [m_s, m_e) (another T merge on the patch: injection, merge)
      CNOT_t `CNOTt_far` over [R, s), `CNOTt_near` over [s, e), `CNOTt_post` over [e, e + floor(d/2)) (the patch is the
                CNOT's target: the partner's history since the frame can flip the parity)
      S then H `SH_far` over [R, S_s), `SH_nearS` over the S, `SH_nearH` over the H, `SH_post` over [H_e, H_e + floor(d/2))
                (an S followed later by an H; that H takes no H term of its own)
      S alone, CNOT_c, Paulis: nothing
      unmeasured (CZ, anything else): the largest far, near and post densities at that d, over [R, s), [s, e), post.
    A gadget that has not started contributes only its far phase (every round of the window precedes it: the program
    fixes the order of the gadgets on a wire, not their times); a running one its phases up to now. The latency class is
    max(1, ceil(count / (d (d^2 - 1)))): the local table's rule with the volume count / (d^2 - 1) patch-rounds."""

    def __init__(self, path: str | None = None, densities: dict | None = None):
        if densities is not None:                                      # the fit's own evaluations (fit_geometry.py)
            self.path, self.sha256, self.doc = None, None, None
            self.by_d = {int(d): v for d, v in densities.items()}
            return
        path = path or GEOMETRY_PATH                                   # (set_calibration's, read when made)
        doc = json.load(open(path))
        self.path, self.sha256, self.doc = path, sha256_of(path), doc
        self.by_d = {int(d): v for d, v in doc["densities"].items()}

    def dens(self, d: int) -> dict:
        if d not in self.by_d:
            raise KeyError("the strip geometry is calibrated at d = %s, not at d = %d" % (sorted(self.by_d), d))
        return self.by_d[d]

    @staticmethod
    def _ov(a: float, b: float, s: float, e: float) -> float:
        return max(0.0, min(b, e) - max(a, s))

    def count(self, d: int, lo: float, hi: float, t_m, R: float, gadgets: list, now: float, fill=None) -> float:
        """Detectors of the window [lo, hi) of a stream anchored at R, merge start t_m (None: not started, every round of
        the window is a history round), the gadgets since the anchor in program order: dicts with `kind`, `span`
        ((s, e) done, (s, None) running, None not started, SKIPPED) and, for kind T, `merge_span`. (--h-depth):
        `fill(a, b)`, the stream cell's filler rounds in [a, b) (an H's ordinary rounds in which no deep round ends), is
        left out of every interval, so the window counts syndrome rounds as the decoder does; None (off): unchanged."""
        D, ov, half, big = self.dens(d), self._ov, d // 2, 1e18
        if fill is not None:
            ov0 = ov
            ov = lambda a, b, s, e: max(0.0, ov0(a, b, s, e) - fill(max(a, s), min(b, e)))
        if t_m is None:
            n = D["hist"] * ((hi - lo) if fill is None else ov(lo, hi, -big, big))
        else:
            n = D["hist"] * ov(lo, hi, -big, t_m - d) + D["pre"] * ov(lo, hi, t_m - d, t_m) + D["merge"] * ov(lo, hi, t_m, big)
        live = [g for g in gadgets if g.get("span") != SKIPPED]
        paired = set()
        for i, g in enumerate(live):
            k, sp = g["kind"], g.get("span")
            s = sp[0] if sp else None
            e = (sp[1] if sp[1] is not None else now) if sp else None
            if k == "H":
                if i not in paired and sp is not None:
                    n += D["hist"] * ov(lo, hi, s, e + 1)
            elif k == "T":
                ms = g.get("merge_span")
                if ms not in (None, SKIPPED):
                    m_s, m_e = ms[0], (ms[1] if ms[1] is not None else now)
                    n += D["T_pre"] * ov(lo, hi, m_s - d, m_s) + D["T_near"] * ov(lo, hi, m_s, m_e)
            elif k == "CNOT_t":
                n += D["CNOTt_far"] * ov(lo, hi, R, s if s is not None else big)
                if s is not None:
                    n += D["CNOTt_near"] * ov(lo, hi, s, e)
                    if sp[1] is not None:
                        n += D["CNOTt_post"] * ov(lo, hi, e, e + half)
            elif k == "S":
                j = next((j for j in range(i + 1, len(live)) if live[j]["kind"] == "H" and j not in paired), None)
                if j is None:
                    continue                                           # an S alone (or H then S's S): nothing
                paired.add(j)
                h = live[j].get("span")
                n += D["SH_far"] * ov(lo, hi, R, s if s is not None else big)
                if s is not None:
                    n += D["SH_nearS"] * ov(lo, hi, s, e)
                if h is not None:
                    h_s, h_e = h[0], (h[1] if h[1] is not None else now)
                    n += D["SH_nearH"] * ov(lo, hi, h_s, h_e)
                    if h[1] is not None:
                        n += D["SH_post"] * ov(lo, hi, h_e, h_e + half)
            elif k in ("CNOT_c", "pauli"):
                continue
            else:                                                      # unmeasured (CZ, anything else): conservative
                n += D["max_far"] * ov(lo, hi, R, s if s is not None else big)
                if s is not None:
                    n += D["max_near"] * ov(lo, hi, s, e)
                    if sp[1] is not None:
                        n += D["max_post"] * ov(lo, hi, e, e + half)
        return n

    @staticmethod
    def latency_class(d: int, count: float) -> int:
        return max(1, math.ceil(count / (d * (d * d - 1)) - 1e-12))


# ------------------------------------------------------------------------------------------------------------ accuracy
def accuracy_class(kinds: list) -> str | None:
    """class of what ran on the patch since the anchor (Paulis and skipped gadgets are not work), or None for a
    sequence did not measure (rule 11's conservative value)."""
    return MEASURED_SEQUENCES.get(tuple(k for k in kinds if k != "pauli"))


CLASS_RULES = ("corrected", "legacy")
GEOMETRY_RULES = ("corrected", "legacy")
SCRIPT_KIND = {"H": "H", "S": "S", "Sdg": "S", "T": "T", "CZ": "CZ", "X": "pauli", "Y": "pauli", "Z": "pauli"}


def script_kind(template, wire, wires) -> str:
    """The client's kind of a spliced script's gadget on one of its program wires: as the program's gadgets are
    classified (device `STRIP_KIND`), a CNOT by the template's operand order [c, t]."""
    if template == "CNOT_cf_patched":
        return "CNOT_t" if len(wires) > 1 and wires[1] == wire else "CNOT_c"
    return SCRIPT_KIND.get(template, "other")


def executed_kinds(program: list, spliced: list, span_fn, R: int, t_m: int) -> list:
    """The corrected class rule (part 1c; audit, ``):
    the kinds of everything that executed on the patch between the anchor R and the merge t_m, by start round.
      program the stream's own gadgets on its wire (the original program's, before the value's T), with the client's
               filter: Paulis and skipped gadgets do no work; a gadget that ended by R, a T whose merge ended by R, is
               committed (as `StripPool._gadgets_since_anchor`);
      spliced the gadgets that deferral, early-materialization and repair scripts spliced onto the wire: each that did
               work and ran in (R, t_m) -- started before the merge, not ended by the anchor.
    v7's rule (`legacy`) saw only the first list, as it stood when the final window started."""
    seq = []
    for g in program:
        if g["kind"] == "pauli":
            continue
        sp = span_fn(g["instrs"])
        if sp == SKIPPED:
            continue
        if g["kind"] == "T":
            ms = span_fn(g["merge"])
            if ms not in (None, SKIPPED) and ms[1] is not None and ms[1] <= R:
                continue
            start = ms[0] if ms not in (None, SKIPPED) else None
        else:
            if sp is not None and sp[1] is not None and sp[1] <= R:
                continue
            start = sp[0] if sp is not None else None
        seq.append((t_m if start is None else start, 0, g["kind"]))
    for g in spliced:
        if g["kind"] == "pauli":
            continue
        sp = span_fn(g["instrs"])
        if sp in (None, SKIPPED) or sp[0] >= t_m:
            continue
        if (sp[1] if sp[1] is not None else t_m) > R:
            seq.append((sp[0], 1, g["kind"]))
    seq.sort(key=lambda x: (x[0], x[1]))
    return [k for _t, _o, k in seq]


class Accuracy:
    """q per (d, class since the frame, age). Rule 11's main value: the cell's lower 95 % bound, the age rounded
    up to the next measured age (d, 3d, 6d, 12d); an age above 12d or a sequence did not measure takes the lowest
    lower bound measured at that d over every class and age. mode "point": the point estimates (rule 11's sensitivity),
    with the same rounding and the lowest point estimate as the conservative value."""

    def __init__(self, path: str | None = None, mode: str = "lower"):
        assert mode in ("lower", "point"), mode
        path = path or POINTS_PATH                                     # (set_calibration's, read when made)
        self.mode, self.path, self.sha256 = mode, path, sha256_of(path)
        col = "q_lo" if mode == "lower" else "q"
        self.q, self.low = {}, {}
        for r in csv.DictReader(open(path)):
            d = int(r["d"])
            self.q[(d, r["cls"], int(r["age_d"]))] = float(r[col])
            self.low[d] = min(self.low.get(d, 1.0), float(r[col]))

    def value(self, d: int, cls: str | None, age: int) -> tuple:
        """(q, conservative?, the measured age it was rounded up to, in units of d, or None above 12d)."""
        a = next((m for m in AGES if age <= m * d), None)
        if cls is None or a is None or (d, cls, a) not in self.q:
            return self.low[d], True, a
        return self.q[(d, cls, a)], False, a


# ------------------------------------------------------------------------------------------------------------ streams
@dataclass
class Stream:
    vid: int
    key: str                                   # the value's producer event: names every keyed draw of the stream
    cell: tuple
    gadgets: list                              # the wire's gadgets before this merge, program order (see StripPool)
    open_round: int = -1
    R: float | None = None                     # the anchor: the committed frame the chain decodes from
    pos: float | None = None                   # the next commit region's first round (None: the patch has no frame yet)
    ready_floor: float = -1.0                  # the chain's hand-over: the last window's finish, or the re-anchoring round
    t_m: int | None = None
    state: str = "closed"                      # closed -> history -> done | cancelled | stale | beyond_table
    busy: dict | None = None                   # the window in decode
    cand: tuple | None = None                  # the next window, once defined
    windows: list = field(default_factory=list)
    reanchors: int = 0
    pub_round: int = -1
    final_R: float = -1
    final_kinds: list = field(default_factory=list)   # what ran since the anchor, as the final window saw it
    seen: set = field(default_factory=set)             # corrected geometry: the spliced gadgets an earlier window sized for
    seen_shadow: set = field(default_factory=set)      # the same, for the corrected count a legacy traced run records
    busy_spl: tuple = ()                               # the busy window's spliced gadgets (`seen`, `seen_shadow`), committed
    busy_spl_shadow: tuple = ()                        # when it completes


class SharedPool:
    """Rule 42: the B cores that the full decoder and the strip client
    share. Full decodes come first. Rule 42a (`preemptive`, the default): a full decode that finds no idle core
    preempts the strip job started last (`preempt`), at no cost in time; the aborted job publishes nothing. The
    ablation (`preemptive` False, rule 42 as first written): a strip job holds its core from its start round to its
    finish round (exclusive), abandoned or not, and the full decoder starts its decodes on B less those cores.
    class_max(volume) is the latency table's largest sample of the window's class, in rounds: the bound on a job's
    service."""

    def __init__(self, B: int, class_max, preemptive: bool = True):
        self.B, self.class_max, self.preemptive = int(B), class_max, bool(preemptive)
        assert self.B >= 1, B
        self.jobs: list = []                   # heap of (finish, seq, vid): the strip jobs on cores (lazily cleaned)
        self._live: dict = {}                  # seq -> (start, vid, window record): the jobs holding a core now
        self._seq = 0
        self.stats = dict(jobs=0, core_rounds=0.0, peak=0, abandoned_kept=0, abandoned_kept_core_rounds=0.0, service_clamps=0,
                          hol_core_rounds=0, hol_rounds=0, preempted=0, preempted_core_rounds=0.0)

    def busy(self, now) -> int:
        """Strip jobs on cores in round `now` (a job whose finish is `now` has released its core)."""
        while self.jobs and self.jobs[0][0] <= now:
            _f, seq, _v = heapq.heappop(self.jobs)
            self._live.pop(seq, None)
        return len(self._live)

    def occupy(self, vid: int, rec: dict) -> None:
        self._seq += 1
        heapq.heappush(self.jobs, (rec["finish"], self._seq, vid))
        self._live[self._seq] = (rec["start"], vid, rec)
        st = self.stats
        st["jobs"] += 1; st["core_rounds"] += int(rec["service"]); st["peak"] = max(st["peak"], len(self._live))

    def preempt(self, n: int, now) -> list:
        """Rule 42a: the n jobs started last (ties: the later started first) leave their cores in round `now`; their
        windows' records.  Their unserved rounds are not charged as core-rounds."""
        out = sorted(self._live.items(), key=lambda kv: (kv[1][0], kv[0]), reverse=True)[:max(0, int(n))]
        for seq, (_start, _vid, rec) in out:
            del self._live[seq]
            left = max(0.0, rec["finish"] - now)
            self.stats["preempted"] += 1; self.stats["preempted_core_rounds"] += left; self.stats["core_rounds"] -= left
        return [(vid, rec) for _seq, (_start, vid, rec) in out]

    def note_abandon(self, rec: dict, now) -> None:
        self.stats["abandoned_kept"] += 1
        self.stats["abandoned_kept_core_rounds"] += max(0.0, rec["finish"] - now)

    def note_hol(self, blocked: int) -> None:
        if blocked > 0:
            self.stats["hol_core_rounds"] += blocked; self.stats["hol_rounds"] += 1

    def summary(self) -> dict:
        return dict(B=self.B, mode="shared" if self.preemptive else "shared_nonpreemptive",
                    **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.stats.items()})


class StripPool:
    """Every stream of a run and the shared pool of strip cores (cores None = unlimited), first come, first served.

    The device calls `open(vid, now)` when a stream opens (its anchor: the committed frame of its patch then; a patch
    not yet initialized has none, frame_fn returns None and the stream anchors at the patch's first frame),
    `merge_started(vid, t_m)`, `cancel(vid, now)` when the value's verdict arrives first (the window in decode is
    abandoned and its core freed), and `step(now)` once per round. Re-anchoring: before each window the stream moves to
    the newest committed frame if that frame has passed the window's first round (after the merge's start at most to
    t_m - d); a history window in decode whose whole commit region the frame has passed is abandoned in that round (its
    core freed) and the stream moves at once. A window starts when its rounds are measured (data), its predecessor has
    finished (hand-over) and a core is free, in the order the windows became ready (ties by vid). History windows commit d rounds with d rounds of
    buffer; the last one commits up to t_m - d (its buffer [t_m - d, t_m)); the final window is [t_m - d, t_m + floor(d/2)).
    A history window starts before the merge has started only if its data are complete, so its commit region ends by
    now - d < t_m - d: no history window ever overlaps the final window.

    gadgets: dicts kind in {H, S, T, CNOT_t, CNOT_c, CZ, other, pauli}, `instrs` (the lss instructions, for the span) and,
    for kind T, `merge` (the merge instruction). span_fn(instrs) -> None (not started), (s, None) running, (s, e) done
    (e exclusive), or SKIPPED. draw(volume, *key) -> the decode time in rounds, or None for a window beyond the latency
    table's largest class: no latency is known for it, so the stream stops there and its value waits for the full
    decoder (state beyond_table; conservative: no prediction).

    The geometry rule. "legacy" (v7) sizes a window from the stream's program gadgets alone.
    "corrected" adds the gadgets that scripts spliced onto the stream's wire (spliced_fn(stream) -> dicts kind, instrs,
    id) once they have begun (`_spliced_since_anchor`), in executed order (`_merge`). A splice is known only once it
    exists, so earlier windows may have committed rounds since the anchor without it; when a spliced gadget is first
    seen and it widens those rounds (the far phase of a CNOT target or of an S-then-H pair, an S that a spliced H now
    pairs with), the window also decodes the missing detectors of [R, lo): the catch-up (`_corrected_count`).
    shadow (legacy runs, records only): every window also records the corrected count, class and decode time."""

    def __init__(self, d: int, cores, geometry: Geometry, draw, frame_fn, span_fn, spliced_fn=None, geometry_rule="legacy",
                 pool=None):
        self.d, self.cores = d, (None if cores is None else int(cores))
        assert self.cores is None or self.cores >= 1, cores
        # part C: with `pool`, a `SharedPool`, the strip client has no cores
        # of its own: its windows start on the cores the full decoder leaves idle (`pool_start`, after each round's full
        # decodes), non-preemptively, for max(1, ceil(tau)) rounds.  None: the dedicated cores above, or unlimited (legacy)
        self.pool = pool
        assert pool is None or cores is None, "a shared pool replaces dedicated strip cores"
        self._ready: list = []                 # pool: the windows ready this round, (ready, vid), started by `pool_start`
        assert geometry_rule in GEOMETRY_RULES, geometry_rule
        self.geo, self.draw, self.frame_fn, self.span_fn = geometry, draw, frame_fn, span_fn
        self.spliced_fn, self.geometry_rule = spliced_fn, geometry_rule
        self.shadow = None                     # legacy runs: a draw function, records the corrected geometry per window
        self.trace_gadgets = False             # corrected runs: record each window's gadget list (the trace's check)
        self.streams: dict[int, Stream] = {}
        self._active: set = set()
        self._core_free = [] if cores is None else [0.0] * self.cores  # min-heap of the cores' free times
        self.causality = dict(before_data=0, before_handover=0, before_final=0, before_merge_end=0)
        self.beyond_table = 0                  # streams stopped at a window beyond the latency table
        self.abandoned = 0                     # windows abandoned in decode: superseded by the frame, or the verdict came
        self.preempted = 0                     # rule 42a: jobs that lost their core to a full decode (retried while useful)
        self.stale_streams = 0                 # rule 42a: streams stopped once their value's window was fully decoded
        self.intervals: list = []              # (start, finish) of every started window
        self.waits: list = []                  # start - ready of every started window

    # -- events
    def add(self, s: Stream):
        self.streams[s.vid] = s

    def open(self, vid: int, now: int):
        s = self.streams[vid]
        if s.state != "closed":
            return
        s.state, s.open_round = "history", now
        s.R = s.pos = self.frame_fn(s.cell)
        s.ready_floor = float(now)
        self._active.add(vid)

    def merge_started(self, vid: int, t_m: int):
        s = self.streams[vid]
        if s.t_m is None:
            s.t_m = int(t_m)
            s.cand = None
            if s.pos is not None and s.pos > s.t_m - self.d:             # re-anchored past t_m - d just before the merge: the
                s.pos = s.R = s.t_m - self.d                           # final window starts there, so does its anchor

    def cancel(self, vid: int, now: int):
        s = self.streams.get(vid)
        if s is None or s.state not in ("closed", "history"):
            return
        if s.busy is not None:
            self._abandon(s, now)                                      # the verdict is known: the decode in progress is dropped
        s.state = "cancelled"; self._active.discard(vid)

    def stale(self, vid: int, now: int):
        """Rule 42a's stale completions (the shared pool): the value's window has been fully decoded (the merge's decode
        tasks have all completed: SWIPER's T_emit), so the stream publishes nothing more.  A job in progress, final or
        history, is dropped as stale (it keeps its core as an abandoned job does, unless preempted), and no window starts
        again: neither the next one nor a retry.  The device calls it before the round's step, as it calls `cancel`."""
        s = self.streams.get(vid)
        if s is None or s.state not in ("closed", "history"):
            return
        if s.busy is not None:
            self._abandon(s, now)
        s.state = "stale"; self._active.discard(vid)
        self.stale_streams += 1

    def preempt(self, vid: int, rec: dict, now: int):
        """Rule 42a: the job of window `rec` lost its core in round `now` to a full decode.  It publishes nothing and the
        stream does not advance: a stream still on that window takes it again once a core is idle (the same window, so the
        same keyed decode time), while it is useful -- the full decoder's frame has not passed it and the verdict is
        unknown, as for any window.  A job already abandoned only gives its core back."""
        s = self.streams[vid]
        rec.update(preempted=True, preempted_at=float(now))
        a, _ = self.intervals[rec["interval"]]
        self.intervals[rec["interval"]] = (a, float(now))
        if s.busy is rec:
            s.busy = None
            s.busy_spl = s.busy_spl_shadow = ()                        # (aborted: the retry does the catch-up)
            s.ready_floor = max(s.ready_floor, float(now))
        self.preempted += 1

    def _abandon(self, s: Stream, now: int):
        """Drop the window in decode at round `now`: its core is free from now on, its interval ends now.  Under the
        shared pool the job keeps its core until its service ends (or, rule 42a, until a full decode preempts it), and
        only the stream moves on: a stale job publishes nothing."""
        rec, s.busy = s.busy, None
        s.busy_spl = s.busy_spl_shadow = ()                            # (it never completes: the history stays as it was)
        if self.pool is not None:
            rec.update(abandoned=True, abandoned_at=float(now))
            self.pool.note_abandon(rec, now)
            s.ready_floor = max(s.ready_floor, float(now))
            self.abandoned += 1
            return
        if self.cores is not None:
            i = self._core_free.index(rec["finish"])                   # a core that frees at this window's exact finish
            self._core_free[i] = float(now)
            heapq.heapify(self._core_free)
        a, _ = self.intervals[rec["interval"]]
        self.intervals[rec["interval"]] = (a, float(now))
        rec.update(finish=float(now), abandoned=True)
        s.ready_floor = max(s.ready_floor, float(now))
        self.abandoned += 1

    # -- one round
    def step(self, now: int) -> list:
        """Advance to round `now`; returns [(vid, publication round)] of the streams whose final window finished."""
        done = []
        for v in sorted(self._active):                                # history windows the full decoder's frame has overtaken
            s = self.streams[v]
            if s.busy is not None and s.busy["kind"] == "history" and s.busy["finish"] > now:
                f = self.frame_fn(s.cell)
                if f is not None and f >= s.busy["commit_end"]:
                    self._abandon(s, now)
        fin = sorted((self.streams[v].busy["finish"], v) for v in self._active if self.streams[v].busy is not None)
        for f, v in fin:
            if f <= now:
                self._finish(self.streams[v], done)
        heap = []
        for v in sorted(self._active):
            self._push(heap, self.streams[v], now)
        if self.pool is not None:                                      # started after the round's full decodes (`pool_start`)
            self._ready = sorted(heap)
            return done
        while heap:
            ready, v = heapq.heappop(heap)
            s = self.streams[v]
            if s.busy is not None or s.state != "history" or s.cand is None:
                continue
            start = max(ready, self._core_free[0] if self.cores is not None else 0.0)
            if start > now:
                break                                                  # every core busy past this round: nothing starts
            if not self._start(s, start):
                continue                                               # beyond the table: the stream stopped
            if s.busy["finish"] <= now:
                self._finish(s, done)
                self._push(heap, s, now)
        return done

    def ready_count(self) -> int:
        """The windows `pool_start` would consider this round (R1 validation counts the starts a booking blocks)."""
        return sum(1 for _ready, v in self._ready if self.streams[v].busy is None and self.streams[v].state == "history"
                   and self.streams[v].cand is not None)

    def pool_start(self, now: int, idle: int) -> int:
        """The shared pool, after round `now`'s full decodes have started: start this round's ready windows, in the order
        they became ready (ties by vid), on at most `idle` cores.  Returns the number started."""
        n = 0
        for ready, v in self._ready:
            if n >= idle:
                break
            s = self.streams[v]
            if s.busy is not None or s.state != "history" or s.cand is None:
                continue
            if self._start(s, float(now)):
                n += 1
        self._ready = []
        return n

    def _next_window(self, s: Stream, now: int):
        """(lo, hi, commit end, data ready, kind) of the stream's next window, or None while it cannot be defined."""
        d = self.d
        if s.pos is None:
            return None
        if s.t_m is None:
            lo, hi = s.pos, s.pos + 2 * d
            return (lo, hi, lo + d, float(hi), "history") if hi <= now else None
        last = s.t_m - d
        if s.pos < last:
            if s.pos + d <= last:
                return (s.pos, s.pos + 2 * d, s.pos + d, float(s.pos + 2 * d), "history")
            return (s.pos, s.t_m, last, float(s.t_m), "history")
        return (last, s.t_m + d // 2, s.t_m + d // 2, float(s.t_m + d // 2), "final")

    def _reanchor(self, s: Stream, now: int):
        f = self.frame_fn(s.cell)
        if f is not None and (s.pos is None or f > s.pos):
            s.reanchors += s.pos is not None                           # the first anchor of a stream opened early is none
            s.pos = f if s.t_m is None else min(f, s.t_m - self.d)
            s.R = s.pos
            s.ready_floor = max(s.ready_floor, float(now))             # the full decoder's frame, known in this round

    def _push(self, heap, s: Stream, now: int):
        if s.state != "history" or s.busy is not None:
            return
        self._reanchor(s, now)
        w = self._next_window(s, now)
        s.cand = w
        if w is not None:
            ready = max(w[3], s.ready_floor)
            if ready <= now:
                heapq.heappush(heap, (ready, s.vid))

    def _start(self, s: Stream, start: float) -> bool:
        d = self.d
        lo, hi, c_end, data_ready, kind = s.cand
        gad = self._gadgets_since_anchor(s)
        extra = {}
        spl, spl_c = (), ()
        if self.geometry_rule == "corrected":
            cnt, catch, G, ids = self._corrected_count(s, lo, hi, start, s.seen, prog=gad)
            spl = tuple(ids)
            extra.update(({"catch": round(catch, 1)} if catch else {}), **({"spl": ids} if ids else {}))
            if self.trace_gadgets:
                extra["gad"] = [[g["kind"], *self._span_rec(g), g.get("id", -1)] for g in G]
        else:
            cnt = self.geo.count(d, lo, hi, s.t_m, s.R, gad, start, fill=self._fill(s))
            if self.shadow is not None:                                # records only: what the corrected rule would size
                cc, catch, _G, ids = self._corrected_count(s, lo, hi, start, s.seen_shadow, prog=gad)
                spl_c = tuple(ids)
                extra.update(count_c=round(cc, 1), cls_c=self.geo.latency_class(d, cc),
                             tau_c=self.shadow(cc / (d * d - 1), "strip_decode", s.key, lo, hi, s.R), **({"catch_c": round(catch, 1)} if catch else {}))
        tau = self.draw(cnt / (d * d - 1), "strip_decode", s.key, lo, hi, s.R)
        if tau is None:
            s.windows.append(dict(lo=lo, hi=hi, kind=kind, count=round(cnt, 1), cls=self.geo.latency_class(d, cnt), R=s.R, refused=True, **extra))
            s.state, s.cand = "beyond_table", None
            self._active.discard(s.vid); self.beyond_table += 1
            return False
        if self.pool is not None:                                      # rule 42: whole rounds, at most the class's largest sample
            cap = self.pool.class_max(cnt / (d * d - 1))
            if tau > cap:
                tau = cap; self.pool.stats["service_clamps"] += 1
            extra["service"] = max(1, math.ceil(tau - 1e-9))
            finish = start + extra["service"]
        else:
            finish = start + tau
        if start < data_ready - 1e-9: self.causality["before_data"] += 1
        if start < s.ready_floor - 1e-9: self.causality["before_handover"] += 1
        if self.cores is not None:
            heapq.heapreplace(self._core_free, finish)
        rec = dict(lo=lo, hi=hi, commit_end=c_end, kind=kind, count=round(cnt, 1), cls=self.geo.latency_class(d, cnt),
                   R=s.R, ready=max(data_ready, s.ready_floor), start=start, finish=finish, interval=len(self.intervals), **extra)
        if self.pool is not None:
            self.pool.occupy(s.vid, rec)
        s.windows.append(rec)
        self.intervals.append((start, finish)); self.waits.append(start - max(data_ready, s.ready_floor))
        s.busy, s.cand = rec, None
        s.busy_spl, s.busy_spl_shadow = spl, spl_c                     # (the history: committed in `_finish` only)
        if kind == "final":
            s.final_R, s.final_kinds = s.R, [g["kind"] for g in gad]
        return True

    def _finish(self, s: Stream, done: list):
        """A job that completed: the gadgets it sized for join the stream's history now (a preempted or abandoned job
        never completes, so its catch-up is left for the window that next decodes those rounds)."""
        rec, s.busy = s.busy, None
        s.seen.update(s.busy_spl); s.seen_shadow.update(s.busy_spl_shadow)
        s.busy_spl = s.busy_spl_shadow = ()
        s.ready_floor = max(s.ready_floor, rec["finish"])
        if rec["kind"] == "final":
            merge_end = s.t_m + self.d
            s.pub_round = max(math.ceil(rec["finish"] - 1e-9) + 1, merge_end + 1)
            if s.pub_round < math.ceil(rec["finish"] - 1e-9) + 1: self.causality["before_final"] += 1
            if s.pub_round < merge_end + 1: self.causality["before_merge_end"] += 1
            s.state = "done"; self._active.discard(s.vid)
            done.append((s.vid, s.pub_round))
        else:
            s.pos = rec["commit_end"]

    @staticmethod
    def _start_of(g):
        """The round a program gadget began on the patch (a T: its merge), None if it has not."""
        sp = g.get("merge_span") if g["kind"] == "T" else g.get("span")
        return sp[0] if sp not in (None, SKIPPED) else None

    @staticmethod
    def _span_rec(g):
        sp = g.get("merge_span") if g["kind"] == "T" else g.get("span")
        return [None, None] if sp in (None, SKIPPED) else [sp[0], sp[1]]

    def _spliced_since_anchor(self, s: Stream) -> list:
        """The gadgets scripts spliced onto the stream's wire that have begun, did work, did not end by the anchor, and
        (once the merge has started) began before it; by start round, splice order on a tie.  The wire's list is in splice
        order and a wire runs its gadgets one after another, so the scan goes back from the newest and stops at the first
        that was done by the anchor (`audit_geometry.py --expect-corrected` checks the result against every splice)."""
        out = []
        for g in reversed(self.spliced_fn(s) if self.spliced_fn is not None else ()):
            sp = self.span_fn(g["instrs"])
            if sp == SKIPPED:
                continue
            if sp is not None and sp[1] is not None and sp[1] <= s.R:
                break
            if sp is None or (s.t_m is not None and sp[0] >= s.t_m):
                continue
            out.append(dict(kind=g["kind"], span=sp, id=g["id"]))
        out.reverse()
        out.sort(key=lambda h: h["span"][0])
        return out

    @classmethod
    def _merge(cls, prog: list, spl: list) -> list:
        """The program's gadgets in program order, each spliced gadget placed before the first program gadget that began
        after it or has not begun (the program's first on a tie)."""
        out, j = [], 0
        for g in prog:
            st = cls._start_of(g)
            while j < len(spl) and (st is None or spl[j]["span"][0] < st):
                out.append(spl[j]); j += 1
            out.append(g)
        return out + spl[j:]

    def _corrected_count(self, s: Stream, lo, hi, start, seen: set, prog=None):
        """The corrected geometry: the window's detectors with the spliced gadgets that have begun, plus the catch-up, the
        detectors that the spliced gadgets first seen now add to the rounds [R, lo) that earlier windows committed (never
        negative: decoded rounds stay decoded).  Returns (count, catch-up, merged gadgets, spliced ids)."""
        d = self.d
        prog = self._gadgets_since_anchor(s) if prog is None else prog   # (the caller's, computed once: code gate, 13)
        spl = self._spliced_since_anchor(s)
        G = self._merge(prog, spl)
        fl = self._fill(s)
        n = self.geo.count(d, lo, hi, s.t_m, s.R, G, start, fill=fl)
        catch = 0.0
        if lo > s.R and any(g["id"] not in seen for g in spl):
            G0 = self._merge(prog, [g for g in spl if g["id"] in seen])
            catch = max(0.0, self.geo.count(d, s.R, lo, s.t_m, s.R, G, start, fill=fl) - self.geo.count(d, s.R, lo, s.t_m, s.R, G0, start, fill=fl))
        return n + catch, catch, G, [g["id"] for g in spl]

    fill_fn = None          # (--h-depth): cell, a, b -> the cell's filler rounds in [a, b) (the device's); None: off

    def _fill(self, s: Stream):
        f = self.fill_fn
        return None if f is None else (lambda a, b: f(s.cell, a, b))

    def _gadgets_since_anchor(self, s: Stream) -> list:
        """The stream's gadgets with their executed spans, the ones that ended by the anchor dropped (committed)."""
        out = []
        for g in s.gadgets:
            sp = self.span_fn(g["instrs"])
            if sp == SKIPPED:
                continue
            h = dict(kind=g["kind"], span=sp)
            if g["kind"] == "T":
                ms = self.span_fn(g["merge"])
                h["merge_span"] = ms
                if ms not in (None, SKIPPED) and ms[1] is not None and ms[1] <= s.R:
                    continue
            elif sp is not None and sp[1] is not None and sp[1] <= s.R:
                continue
            out.append(h)
        return out

    # -- records
    def peak(self) -> int:
        ev = sorted([(a, 1) for a, _ in self.intervals] + [(b, -1) for _, b in self.intervals], key=lambda x: (x[0], x[1]))
        cur = best = 0
        for _, k in ev:
            cur += k; best = max(best, cur)
        return best

    def summary(self, rounds: int) -> dict:
        busy = sum(b - a for a, b in self.intervals)
        ws = sorted(self.waits)
        pick = lambda p: (round(ws[min(len(ws) - 1, int(p * len(ws)))], 3) if ws else None)
        return dict(cores=("pool" if self.pool is not None else self.cores), windows=len(self.intervals), peak_concurrent=self.peak(),
                    busy_core_rounds=round(busy, 1),
                    utilization=(round(busy / (self.cores * rounds), 6) if (self.cores and rounds) else None),
                    wait_mean=(round(sum(ws) / len(ws), 4) if ws else None), wait_p50=pick(0.5), wait_p90=pick(0.9),
                    wait_max=(round(ws[-1], 3) if ws else None), windows_waited=sum(1 for w in ws if w > 1e-9),
                    causality=dict(self.causality), streams=len(self.streams), beyond_table=self.beyond_table, abandoned=self.abandoned,
                    opened=sum(1 for s in self.streams.values() if s.open_round >= 0),
                    reanchors=sum(s.reanchors for s in self.streams.values()),
                    cancelled=sum(1 for s in self.streams.values() if s.state == "cancelled"),
                    finished=sum(1 for s in self.streams.values() if s.state == "done"),
                    **({"preempted": self.preempted, "stale": self.stale_streams} if self.pool is not None else {}))


# ------------------------------------------------------------------------------------------------------------ validation
def isolated_merge(d: int, shots: int, table, seed: int, age_mult: int = 1, cores=None, geometry: Geometry | None = None,
                   accuracy: "Accuracy | None" = None) -> dict:
    """Rule 11's validation: one merge on an isolated patch, the full decoder's committed frame at t_m - age (age =
    age_mult d, fixed), nothing else on the patch, unlimited strip cores by default. Per shot: the publication delay
    L = publication round - (t_m + d), and the value's correctness drawn as the device draws it (a keyed coin
    against the q of (d, class "none", age)). Each shot is one stream with its own keyed draws (its number in the key)."""
    from .latency import KeyedSampler
    from .rng import KeyedRNG
    geo, acc = geometry or Geometry(), accuracy or Accuracy()
    ks, krng = KeyedSampler(table, seed), KeyedRNG(seed)
    t_m = (age_mult + 2) * d
    R0 = t_m - age_mult * d
    L, right, windows = [], 0, []
    for shot in range(shots):
        pool = StripPool(d, cores, geo, ks.draw_keyed_strip, frame_fn=lambda cell: R0, span_fn=lambda instrs: None)
        pool.add(Stream(vid=0, key="shot%d" % shot, cell=(0, 0), gadgets=[]))
        pub = None
        for now in range(0, t_m + 60 * d):
            if now == R0:
                pool.open(0, now)
            if now == t_m:
                pool.merge_started(0, t_m)
            got = pool.step(now)
            if got:
                pub = got[0][1]
                break
        st = pool.streams[0]
        q, cons, _ = acc.value(d, accuracy_class(st.final_kinds), t_m - st.final_R)
        assert not cons and pool.causality == dict(before_data=0, before_handover=0, before_final=0, before_merge_end=0)
        right += krng.uniform("strip", st.key) < q
        L.append(pub - (t_m + d)); windows.append(len(st.windows))
    return dict(L=L, q=q, right=right, shots=shots, windows=windows)
