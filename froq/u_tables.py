"""U's compiler tables (part C; gap analysis rule 41; hardware contract `U-contract.md` sections 3 and 5,
CONFIG sel 6; section 3).

Per site (a value's T gadget with its static deferral walk) the compiler emits 17 records, one per boundary b = 0..16
(golden.u_unit.B_MAX), each (P1, R, S, demand, clean, placed) in the contract's widths: P1, R and S 16 bits (P1 in
patch-rounds, R and S in rounds), demand 8 bits. Sites with identical records share a site class. At most 64 classes
(the 6-bit class field) are configured, in order of first use; a site that would need a 65th, or a field that does not
fit its width, has no class, and the scheduler never registers its values. Everything is computed from the static
program alone -- the walk of `build_contract(deferral=True)` with no other frame's claims and no instruction started --
on first use, and cached. Per boundary b:
- placed: the static walk reaches b and places a script there; P1 is its patch-rounds (D's price), S its duration
  (`Lowered.duration`), the early script's if b is acted;
- on the static rebased contract at b (`rebase_at = b`, at most H_K = 16 rebased steps): k_b is the placed rebased
  boundary of least P1 in [0, top'_b] (ties: the earliest), with top'_b the static rebased walk's last placed boundary,
  and R is that script's duration;
- in the hardware-limit mode (rule 66) both walks stop where the live frames stop: no rebased
  boundary past absolute boundary WALK_CAP = 16 (device.py, the rebase's `j + jj <= WALK_CAP`) and no boundary of the
  static walk past it (the frame's top), so top'_b <= 16 - b and the stops below are those of the capped walks;
- clean: H_R(b) <= H_D(b), what the rebased frame holds while it waits for the verdict is held by D-nci's frame too;
- demand: the peak number of decoder cores the early script at b, then the repair at k_b, keep busy at once.
A placed boundary whose rebased contract cannot be built gets clean = 0 (U never acts there) and R = demand = 0. A
boundary that is not placed is all zeros."""
from __future__ import annotations

from swiper.lattice_surgery_schedule import Duration

from .contract import build_contract, fence_set
from .hdepth import HDur, deep_chunks
from .hwlimits import WALK_CAP

H_K = 16                     # the rebased walk's depth
B_MAX = 16                   # the last boundary with a record (golden.u_unit.B_MAX)
N_CLASSES = 64               # site classes the table holds (6 bits)
FIELDS = ("P1", "R", "S", "demand", "clean", "placed")
WIDTH = dict(P1=16, R=16, S=16, demand=8)
EMPTY = (0, 0, 0, 0, False, False)
# U v6 (round rule 51, Eq. (1)'s repair terms): two more fields per boundary, when the run asks for them -- XS, the
# rounds from the end of the script at b until its results are decoded and delivered (its last window's data complete
# d rounds after its chunk, then its class's mean service, rounded up, plus the delivery path), and XR, the same for its
# repair at k_b.  A run without them keeps U v4's records and classes exactly.
FIELDS_X = FIELDS + ("XS", "XR")
WIDTH_X = dict(WIDTH, XS=16, XR=16)
EMPTY_X = EMPTY + (0, 0)


def dnci_stop(r: list) -> tuple:
    """(stop, top) of D-nci's rule on per-boundary prices r (None: no placement): top is the last placed boundary, stop
    the first placed boundary past which the next placed one costs more (top if none)."""
    js = [j for j, x in enumerate(r) if x is not None]
    if not js:
        return None, None
    stop = js[-1]
    for a, b in zip(js, js[1:]):
        if r[b] > r[a]:
            stop = a
            break
    return stop, js[-1]


def script_windows(lowered, d: int, t0: int, mu_of_volume) -> tuple:
    """The decode windows of one lowered script laid out from round t0 (each instruction after the previous one on a
    shared patch): [(ready round, mean service)], and the script's end round."""
    free, end, out = {}, t0, []
    for x in (lowered.instrs if lowered is not None else ()):
        dur = Duration.get_true_duration(x.duration, d)
        s = max([t0] + [free.get(p, t0) for p in x.patches])
        e = s + dur
        for p in x.patches:
            free[p] = e
        end = max(end, e)
        if dur <= 0:
            continue
        groups = [tuple(sorted(x.patches))] if x.name == "MERGE" else [(p,) for p in sorted(x.patches)]
        if isinstance(x.duration, HDur):                   # (--h-depth): an H member's windows hold its deep rounds, d
            for g in groups:                               #  a window, each ready when its last deep round ends (hdepth.py)
                out.extend((a, mu_of_volume((n + d) * len(g))) for a, n in deep_chunks(x, s, d))
            continue
        for g in groups:
            r = s
            while r < e:
                n = min(d, e - r)
                out.append((r + n + d, mu_of_volume((n + d) * len(g))))   # data complete d rounds after the chunk (buffer)
                r += n
    return out, end


def settle_rounds(windows, end: int, path: int) -> int:
    """U v6 (Eq. (1)): the rounds from a script's end until its results are decoded and delivered: the last window's
    ready round plus its mean service, rounded up, less the end, plus the delivery path; the path alone without a
    window."""
    if not windows:
        return int(path)
    import math
    return max(0, math.ceil(max(a + m for a, m in windows)) - end) + int(path)


def peak_concurrency(windows) -> int:
    """The largest number of windows [ready, ready + service) that overlap."""
    ev = sorted([(a, 1) for a, m in windows] + [(a + m, -1) for a, m in windows], key=lambda z: (z[0], z[1]))
    cur = best = 0
    for _, k in ev:
        cur += k
        best = max(best, cur)
    return best


class UTables:
    """The cache of static walks, site tables and classes of one run.  `dev` supplies the program (es, sem, sched_view,
    specs, d_t), the resource-initialisation test of D's fence rule and the latency table's mean service (`_u_mu`)."""

    def __init__(self, dev, k_tot=None, repair=False):
        self.dev, self.k_tot = dev, k_tot
        self.repair = bool(repair)              # U v6: XS and XR per boundary (Eq. (1)'s repair terms)
        self.fields = FIELDS_X if self.repair else FIELDS
        self.width = WIDTH_X if self.repair else WIDTH
        self._walks: dict = {}                 # vid -> the static deferral walk (dropped when the frame ends)
        self._site: dict = {}                  # (vid, the walk's rule) -> class index, or None (no class)
        self.classes: list = []                # class index -> (its walk: (rule, demand_lazy or None), then its 17 records:
                                               # tuples in FIELDS order)
        self._class_of: dict = {}              # (the walk, the 17 records) -> class index
        self._demand_lazy: dict = {}           # vid -> demand_lazy (rule 44a)
        self._script_cost: dict = {}           # (vid, b) -> S + XS of the site's script at b (rule 50c's C_s)
        self.stats = dict(sites=0, walks=0, rebased_walks=0, no_class_full=0, no_class_width=0, rebase_failed=0, demand_lazy=0)

    @staticmethod
    def _state0(i):
        return 0

    def _capped(self) -> bool:
        """the run is in the hardware-limit mode, whose walk cap stops the live frames at absolute
        boundary WALK_CAP (the tables are built on first use, after the device's setup)."""
        return getattr(self.dev, "_hw", None) is not None

    def walk(self, vid):
        c = self._walks.get(vid)
        if c is None:
            dev = self.dev
            K = 10 ** 6 if self.k_tot is None else self.k_tot
            c = build_contract(dev.es, dev.sem, dev.sched_view, dev._specs[vid], False, K, deferral=True, claimed=(), state=self._state0)
            self._walks[vid] = c
            self.stats["walks"] += 1
        return c

    def forget(self, vid):
        self._walks.pop(vid, None)

    @staticmethod
    def _boundary(c, j):
        if j == 0:
            return c.entry_recovery
        return c.steps[j - 1].recovery

    @staticmethod
    def _prices(c):
        return [(c.entry_recovery.P1 if c.entry_recovery is not None else None)] + [s.price_P1 for s in c.steps]

    def _held(self, c, vid, stop, extra_bits=()):
        """What a frame of contract c holds while it waits at its stop: the fence, less D's exempt resource
        initialisations, and the members of the steps from its stop on."""
        dev = self.dev
        v = dev.values[vid]
        last = c.steps[-1] if c.steps else None
        Lf = (set(last.L) if last else set(c.entry_L or ())) | {v.wire}
        Df = (set(last.D_fixed | last.D_cond) if last else set(c.entry_D or ())) | {v.result_id} | set(extra_bits)
        fence = {i for i in fence_set(dev.es, c)
                 if not (dev._is_startup_instruction[i] and not (dev._instr_wires.get(i, frozenset()) & Lf)
                         and not (dev._instr_preds.get(i, frozenset()) & Df))}
        return fence | {i for s in c.steps[stop:] for i in s.instrs}

    def _record(self, vid, c, r, b, rule="no_cost_increase"):
        """Boundary b's record (FIELDS order), for a frame carried under `rule` (U v4, rule 44a: clean compares the
        rebased frame's holds with those of the frame under its own rule -- D-nci's stop, or lazily to top)."""
        low = self._boundary(c, b) if b <= c.F else None
        if low is None or r[b] is None:
            return EMPTY_X if self.repair else EMPTY
        dev = self.dev
        P1, S = int(r[b]), int(low.duration)
        hk = getattr(dev, "_walk_cap", None) or H_K               # (H's K tied to the walk cap, `--walk-cap`)
        K = hk if self.k_tot is None else min(hk, self.k_tot - b)
        c2 = build_contract(dev.es, dev.sem, dev.sched_view, dev._specs[vid], False, K, deferral=True, claimed=(), rebase_at=b,
                            state=self._state0)
        self.stats["rebased_walks"] += 1
        if c2.rebase is None or c2.entry_recovery is None:
            self.stats["rebase_failed"] += 1
            if self.repair:
                ws, end = script_windows(low, dev.d_t, 0, dev._u_mu)
                return (P1, 0, S, 0, False, True, settle_rounds(ws, end, dev.path), 0)
            return (P1, 0, S, 0, False, True)
        r2 = self._prices(c2)
        if self._capped():                                         # (the live frames' horizon, rule 66)
            cap = getattr(dev, "_walk_cap", None) or WALK_CAP
            r2 = [x if b + k <= cap else None for k, x in enumerate(r2)]
            r = [x if j <= cap else None for j, x in enumerate(r)]
        stop2, top2 = dnci_stop(r2)
        kb = min((k for k in range(top2 + 1) if r2[k] is not None), key=lambda k: (r2[k], k))
        stop, top = dnci_stop(r)
        if rule == "lazy":
            stop, stop2 = top, top2                                    # (a lazy frame, and its rebased walk, carry to top)
        rep = self._boundary(c2, kb)
        clean = self._held(c2, vid, stop2, extra_bits=c2.held_bits) <= self._held(c, vid, stop)
        ws, end = script_windows(low, dev.d_t, 0, dev._u_mu)
        ws2, end2 = script_windows(rep, dev.d_t, end, dev._u_mu)
        R = int(rep.duration if rep is not None else 0)
        if self.repair:
            return (P1, R, S, peak_concurrency(ws + ws2), bool(clean), True, settle_rounds(ws, end, dev.path),
                    settle_rounds(ws2, end2, dev.path))
        return (P1, R, S, peak_concurrency(ws + ws2), bool(clean), True)

    def site_class(self, vid, rule="no_cost_increase"):
        """The class of site `vid` for a frame carried under `rule` (computing its 17 records on first use), or None.  A
        class's id names its walk (rule 44a): identical records under the two rules are two classes, and a lazy-walk
        class also fixes its demand_lazy, the compiler number rule 44a reads per class."""
        if (vid, rule) in self._site:
            return self._site[(vid, rule)]
        c = self.walk(vid)
        r = self._prices(c)
        recs = ((rule, self.demand_lazy(vid) if rule == "lazy" else None),) + tuple(self._record(vid, c, r, b, rule) for b in range(B_MAX + 1))
        self.stats["sites"] += 1
        cls = self._class_of.get(recs)
        if cls is None:
            if any(int(x[self.fields.index(f)]) >= (1 << w) for x in recs[1:] for f, w in self.width.items()):
                self.stats["no_class_width"] += 1
            elif len(self.classes) >= N_CLASSES:
                self.stats["no_class_full"] += 1
            else:
                cls = self._class_of[recs] = len(self.classes)
                self.classes.append(recs)
        self._site[(vid, rule)] = cls
        return cls

    def fits(self, vid) -> bool:
        """(gap analysis rule 33a as amended at 22:42): the site's 17 records, on its static walk, fit their
        widths -- the part of having a class that the program alone decides (the 64 classes in use are a run's). The
        widths do not depend on the walk's rule: only `clean` does."""
        c = self.walk(vid)
        r = self._prices(c)
        recs = [self._record(vid, c, r, b) for b in range(B_MAX + 1)]
        return not any(int(x[FIELDS.index(f)]) >= (1 << w) for x in recs for f, w in WIDTH.items())

    def record(self, cls, b) -> dict:
        return dict(zip(self.fields, self.classes[cls][1 + b]))

    def rule_of(self, cls) -> str:
        """The walk a class names (rule 44a)."""
        return self.classes[cls][0][0]

    def lazy_boundary(self, vid):
        """The boundary D's decide would choose on the site's lazy walk: the placed boundary of least P1 up to top (the
        static walk's last placed boundary), the earliest on ties; None without a placed boundary."""
        c = self.walk(vid)
        r = self._prices(c)
        cap = getattr(self.dev, "_walk_cap", None) or WALK_CAP
        js = [j for j, x in enumerate(r) if x is not None and (j <= cap or not self._capped())]   # (the frame's top)
        return min(js, key=lambda j: (r[j], j)) if js else None

    def demand_lazy(self, vid) -> int:
        """Rule 44a's demand_lazy of the site, a compiler number: the peak decoder concurrency of the materialization at
        the lazy walk's boundary (`lazy_boundary`); 0 without a placed boundary."""
        d = self._demand_lazy.get(vid)
        if d is None:
            j = self.lazy_boundary(vid)
            if j is None:
                d = 0
            else:
                ws, _end = script_windows(self._boundary(self.walk(vid), j), self.dev.d_t, 0, self.dev._u_mu)
                d = peak_concurrency(ws)
            self._demand_lazy[vid] = d
            self.stats["demand_lazy"] += 1
        return d

    def script_cost(self, vid, b):
        """Rule 50c's C_s input at boundary b of the site's static walk: S + XS, its script's execution rounds and the
        settlement of its results (decoded and delivered), the boundary's record's S and XS (the same under either walk;
        `_record` computes them this way), or None without one (b unplaced, past the walk or past B_MAX, or no repair
        terms).  Computed for the site alone, so no class is allocated in the 64-entry table U's unit loads."""
        key = (vid, b)
        if key in self._script_cost:
            return self._script_cost[key]
        x = None
        if self.repair and b is not None and 0 <= b <= B_MAX:
            c = self.walk(vid)
            r = self._prices(c)
            low = self._boundary(c, b) if b <= c.F else None
            if low is not None and r[b] is not None:
                ws, end = script_windows(low, self.dev.d_t, 0, self.dev._u_mu)
                x = int(low.duration) + settle_rounds(ws, end, self.dev.path)
        self._script_cost[key] = x
        return x

    def matches(self, vid, fr_c) -> bool:
        """The frame's walk is the static walk as far as it goes (the same steps: instructions and residual keys), so the
        site's records describe every boundary the frame can stand at."""
        c = self.walk(vid)
        if fr_c.F > c.F:
            return False
        return [(tuple(s.instrs), s.residual_key) for s in fr_c.steps] == [(tuple(s.instrs), s.residual_key) for s in c.steps[:fr_c.F]]

    def summary(self) -> dict:
        return dict(classes=len(self.classes), **self.stats)
