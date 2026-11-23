"""(plan v2 section 2.1; the plan gate's disposition 1, F1): the H depth setting, `--h-depth RHO`.

What is wrong at RHO = 1 (AFB-2's README 33.3). The runtime's H gadget is a transversal H, then the patch merged with its
side tile for 3d rounds, then the split: as the converter emits it, three IDLE instructions of d rounds on the patch and
its side cell and a DISCARD of the side cell (`h_gadgets`). Its sampled circuit (`g_Hr2`) measures the X checks and then
the Z checks in each round, eight layers of two-qubit gates where an ordinary round has four, while the runtime charges
the time of an ordinary round.

The setting (RHO > 1). Every H gadget the runtime schedules -- the program's, and those inside recovery, commit and
rebase scripts, in every arm alike (`stretch_schedule`, `stretch_instrs`) -- keeps its 3d deep syndrome rounds, and deep
round k ends ceil(RHO k) ordinary-round times after the gadget starts. A member covering the gadget's deep rounds
c0 + 1.. c0 + n takes ceil(RHO (c0 + n)) - ceil(RHO c0) ordinary rounds (`HDur`), so the gadget takes ceil(RHO 3d): 6d
at RHO = 2. Its duration is an int, so every reader that sums durations (the schedule's DAG, the start-time planner,
the contracts' steps, the recovery scripts' critical paths and prices P1, the decision's static profiles) sees the ordinary
rounds without a change of its own.
- The decoder receives one syndrome round at the end of each deep round. The member's other ordinary rounds are
  fillers (`HDur.ends`): the device marks their syndrome rounds (`h_fill`), the window builder does not count them
  toward a window's d rounds (`DeepWindowBuilder`), and the decode-time draw's volume leaves them out (`fill_count`, the
  device's keyed draw). So the H's decode jobs keep their 3d rounds of syndrome and their draws; only the arrival
  times of its rounds move.
- Every other patch, idle or working, is charged the ordinary rounds that pass: the ledger's patch-rounds count every
  ordinary round (the H's own cells' fillers included), so a budget that charges memory per runtime round needs no
  separate term.

RHO = 1, or the setting absent, changes nothing: no member is rewritten, no round is a filler, the upstream window
builder runs and nothing new enters the name, the fingerprint or the decision's profile key. RHO < 1 is refused."""
from __future__ import annotations

import bisect
import dataclasses
import functools
from fractions import Fraction

from swiper.lattice_surgery_schedule import Duration, LatticeSurgerySchedule
from swiper.window_builder import DecodingWindow, SpacetimeRegion, WindowBuilder

H_TEMPLATE = "H"


def parse_rho(x) -> Fraction | None:
    """`--h-depth`'s value as an exact fraction ("2", "1.5", "3/2"); None when it is 1 (the setting is off)."""
    if x is None:
        return None
    r = Fraction(str(x).strip())
    assert r >= 1, ("--h-depth: RHO must be at least 1", x)
    return None if r == 1 else r


def tag(rho: Fraction) -> str:
    """The file-name and fingerprint form of RHO: "2", "1.5" (a terminating decimal) or "p/q"."""
    r = Fraction(rho)
    if r.denominator == 1:
        return str(r.numerator)
    s = "%.12g" % float(r)
    return s if Fraction(s) == r else "%d/%d" % (r.numerator, r.denominator)


def _ceil(x: Fraction) -> int:
    return -((-x.numerator) // x.denominator)


@functools.lru_cache(maxsize=4096)
def _ends(c0: int, n: int, rho: Fraction) -> frozenset:
    s0 = _ceil(rho * c0)
    return frozenset(_ceil(rho * k) - s0 - 1 for k in range(c0 + 1, c0 + n + 1))


class HDur(int):
    """The ordinary rounds of one member of an H gadget under RHO, carrying the member's place in its gadget: deep rounds
    c0 + 1 .. c0 + n.  An int for every reader; pickled, copied and printed as such."""

    def __new__(cls, c0: int, n: int, rho, tot: int | None = None):
        rho = Fraction(rho)
        o = super().__new__(cls, _ceil(rho * (int(c0) + int(n))) - _ceil(rho * int(c0)))
        o.c0, o.n, o.rho = int(c0), int(n), rho
        o.tot = int(c0) + int(n) if tot is None else int(tot)          # the gadget's deep rounds (3d for the runtime's H)
        return o

    def __reduce__(self):
        return (HDur, (self.c0, self.n, self.rho, self.tot))

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def ends(self) -> frozenset:
        """The member's ordinary rounds (0-based from its start) in which one of its deep rounds ends: its n syndrome
        rounds; every other round of the member is a filler."""
        return _ends(self.c0, self.n, self.rho)


def _true(x, d: int) -> int:
    return int(Duration.get_true_duration(x.duration, d))


def _gadget_lss(doc) -> list:
    """[(gadget id, [lss indices of its member events, nested gadgets flattened])] of every gadget of template H, from an
    EventStream (a program's) or a raw event document (a snippet's: the same schema)."""
    if hasattr(doc, "gadgets") and hasattr(doc, "lss_map"):
        gadgets = {gid: (g.template_id, list(g.members)) for gid, g in doc.gadgets.items()}
        lss_map = doc.lss_map
    else:
        raw = dict((doc or {}).get("gadgets") or {})
        for e in (doc or {}).get("events", ()):
            if e.get("kind") == "gadget":
                g = e.get("gadget", e) if isinstance(e.get("gadget"), dict) else e
                raw[g["gadget_id"]] = g
        gadgets = {gid: (g.get("template_id"), list(g.get("events", g.get("members", ())))) for gid, g in raw.items()}
        lss_map = (doc or {}).get("lss_map") or {}

    def flat(gid, seen):
        out = []
        for m in gadgets[gid][1]:
            if m in gadgets:
                if m not in seen:
                    seen.add(m)
                    out.extend(flat(m, seen))
            else:
                out.extend(int(i) for i in lss_map.get(m, ()))
        return out
    return [(gid, sorted(set(flat(gid, {gid})))) for gid, (tid, _m) in sorted(gadgets.items()) if tid == H_TEMPLATE]


def h_gadgets(instrs, doc, d: int) -> list:
    """The H gadgets of an instruction list, each as the positions of its positive-duration members in order.  From the
    event document when there is one.  Without one (the fixture compiler, `tests_fixtures/compile_stub.py`, whose H is a
    single IDLE of d rounds and whose S's IDLE is d/2 + 2), each IDLE of duration D is one H gadget.  A gadget whose
    positive members do not all act on the same cells is refused: the deep rounds are counted along one sequence."""
    if doc is None:
        return [[i] for i, x in enumerate(instrs) if x.name == "IDLE" and x.duration == Duration.D]
    out = []
    for gid, idxs in _gadget_lss(doc):
        pos = [i for i in idxs if 0 <= i < len(instrs) and _true(instrs[i], d) > 0]
        if not pos:
            continue
        cells = {frozenset(instrs[i].patches) for i in pos}
        assert len(cells) == 1, ("--h-depth: an H gadget whose members act on different cells", gid,
                                 [(i, instrs[i].name, sorted(instrs[i].patches)) for i in pos])
        out.append(pos)
    return out


def _stretch(instrs: list, groups, rho: Fraction, d: int) -> dict:
    """Rewrites each group's members in place (a list); returns the counts."""
    n_g = n_m = 0
    for grp in groups:
        c, tot = 0, sum(_true(instrs[i], d) for i in grp)
        for i in grp:
            x = instrs[i]
            n = _true(x, d)
            assert not isinstance(x.duration, HDur), ("--h-depth applied twice", i)
            instrs[i] = dataclasses.replace(x, duration=HDur(c, n, rho, tot))
            c += n
            n_m += 1
        n_g += 1
    return dict(gadgets=n_g, members=n_m)


def stretch_schedule(schedule: LatticeSurgerySchedule, events, rho: Fraction, d: int):
    """(a new schedule with the program's H gadgets at RHO, the counts).  The input is not modified (run.py's parser is
    memoized); the new schedule shares its dependency structure, which durations do not change."""
    instrs = list(schedule.instructions)
    stats = _stretch(instrs, h_gadgets(instrs, events, d), rho, d)
    out = LatticeSurgerySchedule(generate_dag_incrementally=schedule.generate_dag_incrementally)
    out.instructions = instrs
    out._instructions_by_patch = schedule._instructions_by_patch
    out._generated_dag = schedule._generated_dag
    return out, stats


def stretch_instrs(instrs, doc, rho: Fraction, d: int) -> tuple:
    """A lowered script's instructions with its H gadgets at RHO (`doc`: the snippet's event document, or None for the
    fixture compiler)."""
    lst = list(instrs)
    _stretch(lst, h_gadgets(lst, doc, d), rho, d)
    return tuple(lst)


# -------------------------------------------------------------------------------------------- the decoder's arrivals
def is_fill(r) -> bool:
    return getattr(r, "h_fill", False)


def fill_count(fills: dict, region) -> int:
    """Filler rounds of region.patch inside [round_start, round_start + duration) (`fills`: patch -> sorted rounds)."""
    lst = fills.get(region.patch)
    if not lst:
        return 0
    a, b = int(region.round_start), int(region.round_start) + int(region.duration)
    return bisect.bisect_left(lst, b) - bisect.bisect_left(lst, a)


def fill_length(fills: dict, cell, a: float, b: float) -> float:
    """The length of [a, b) (bounds may be fractional) that the cell's filler rounds [r, r + 1) cover: the strip
    predictor's windows count syndrome rounds (strip.Geometry.count's `fill`)."""
    lst = fills.get(tuple(cell))
    if not lst or b <= a:
        return 0.0
    import math
    i0, i1 = bisect.bisect_left(lst, math.floor(a)), bisect.bisect_left(lst, math.ceil(b))
    n = i1 - i0
    if n <= 0:
        return 0.0
    tot = float(n)
    if lst[i0] < a:
        tot -= a - lst[i0]
    if lst[i1 - 1] + 1 > b:
        tot -= lst[i1 - 1] + 1 - b
    return max(0.0, tot)


def window_fills(fills: dict, window) -> int:
    """The filler rounds a decode window's regions hold: its volume less this is its syndrome rounds."""
    if not fills:
        return 0
    return sum(fill_count(fills, r) for r in tuple(window.commit_region) + tuple(window.buffer_regions))


class DeepWindowBuilder(WindowBuilder):
    """Upstream's builder (swiper/window_builder.py `build_windows`, verbatim but for two lines), with an H's filler
    rounds not counted: a window's commit region closes when its patch has d syndrome rounds, not d ordinary rounds, and
    its region keeps the ordinary span (so windows still chain in time by their end and start rounds).  Installed only
    when RHO > 1 (`ScoutSlidingWindowManager`); without fillers it builds upstream's windows."""

    def build_windows(self, new_rounds):
        if not new_rounds or len(new_rounds) == 0:
            curr_round = -1
        else:
            curr_round = new_rounds[0].round
            assert all([round.round == curr_round for round in new_rounds])
            new_round_start = len(self._all_rounds)
            self._all_rounds.extend(new_rounds)
            self._waiting_rounds.update([i + new_round_start for i, round in enumerate(new_rounds) if round.instruction.name != 'INJECT_T'])
            for i, round in enumerate(new_rounds):
                if round.instruction.name != 'INJECT_T':
                    self._patch_groups.setdefault(round.patch, []).append(i + new_round_start)
                else:
                    self._inject_t_rounds_dict.setdefault(round.patch, []).append(i + new_round_start)
        new_windows = []
        for patch, round_indices in list(self._patch_groups.items()):
            rounds = [self._all_rounds[round_idx] for round_idx in round_indices]
            min_round = min(rounds, key=lambda x: x.round)
            max_round = max(rounds, key=lambda x: x.round)
            duration = self.d
            if max_round.round != curr_round or max_round.discard_after:
                duration = max_round.round - min_round.round + 1
            elif min_round.instruction.name != 'MERGE' and max_round.instruction.name == 'MERGE':
                junk_round_end = max(rounds, key=lambda x: x.round * (0 if x.instruction.name == 'MERGE' else 1))
                round_indices = [round_idx for round_idx in round_indices if self._all_rounds[round_idx].round <= junk_round_end.round]
                rounds = [self._all_rounds[round_idx] for round_idx in round_indices]
                duration = junk_round_end.round - min_round.round + 1
            elif min_round.instruction.name == 'MERGE' and max_round.instruction.name != 'MERGE':
                junk_round_end = max(rounds, key=lambda x: x.round * (1 if x.instruction.name == 'MERGE' else 0))
                round_indices = [round_idx for round_idx in round_indices if self._all_rounds[round_idx].round <= junk_round_end.round]
                rounds = [self._all_rounds[round_idx] for round_idx in round_indices]
                duration = junk_round_end.round - min_round.round + 1
            elif sum(1 for x in rounds if not is_fill(x)) < duration:          # d syndrome rounds (upstream: the span)
                continue
            else:
                duration = max_round.round - min_round.round + 1                  # the ordinary span (upstream: d)
            max_round = max(rounds, key=lambda x: x.round)
            num_spatial_boundaries = 4 - sum(patch in face for face in min_round.instruction.merge_faces)
            prior_t = False
            if not min_round.initialized_patch and patch in self._inject_t_rounds_dict:
                for idx in reversed(self._inject_t_rounds_dict[patch]):
                    t_round = self._all_rounds[idx]
                    if t_round.round == min_round.round - 1:
                        prior_t = True
                        break
                    elif t_round.round < min_round.round - 1:
                        break
            parent_instr_idx = frozenset([round.instruction_idx for round in rounds])
            commit_region = SpacetimeRegion(
                patch=patch, round_start=min_round.round, duration=duration, num_spatial_boundaries=num_spatial_boundaries,
                initialized_patch=min_round.initialized_patch, discard_after=max_round.discard_after, prior_t=prior_t,
                merge_instr=min_round.instruction if min_round.instruction.name == 'MERGE' else None)
            new_windows.append(DecodingWindow(
                commit_region=(commit_region,), buffer_regions=frozenset(),
                merge_instr=frozenset() if min_round.instruction.name != 'MERGE' else frozenset([min_round.instruction]),
                parent_instr_idx=parent_instr_idx, window_idx=self._created_window_count, constructed=False))
            self._created_window_count += 1
            self._patch_groups[patch] = [round_idx for round_idx in self._patch_groups[patch] if round_idx not in round_indices]
            if not self._patch_groups[patch]:
                self._patch_groups.pop(patch)
            self._waiting_rounds -= set(round_indices)
            if self.lightweight_setting > 0:
                for round_idx in round_indices:
                    self._all_rounds[round_idx] = None
        self._total_rounds_processed += len(new_rounds)
        return new_windows


def deep_chunks(x, s: int, d: int):
    """U's static layout of an H member's decode windows (u_tables.script_windows): chunks of at most d of its deep
    rounds, [(ready, the chunk's deep rounds)] from the member's start s. Ready is when the chunk's buffer, the next d
    syndrome rounds, is complete: deep time while the gadget lasts (deep round K ends ceil(RHO K) after the gadget's
    start), ordinary rounds after it (the; upstream's rule is the chunk's end plus d)."""
    hd = x.duration
    g0 = s - _ceil(hd.rho * hd.c0)                                     # the gadget's start
    out, k = [], 0
    while k < hd.n:
        n = min(d, hd.n - k)
        K = hd.c0 + k + n + d                                          # the buffer's last deep round, in the gadget
        ready = g0 + (_ceil(hd.rho * K) if K <= hd.tot else _ceil(hd.rho * hd.tot) + (K - hd.tot))
        out.append((ready, n))
        k += n
    return out
