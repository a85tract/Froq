"""Round 8's rule (a), and: future-occupancy certification at every placement authorization
at the unit level.

Each case builds a small device state by hand (`_dev`: instructions on cells, chained in program order as the .lss DAG
chains them, frames with steps, fences and workspaces) and asks the device's own code for every decision: the FORWARD
test (`_r7_refuses`), the evaluator (`_r8_cert`, `_r8_perms`), the candidate guard (`_defer_hold`), the revision's pin
test (`_r8_pins_others`), the settlement (`_r8_settle`), rule 29's knot and pending test, and the status port.

- The hazard cases (`TestHazards`): one placement/release hazard on a certified workspace -- a patch whose release (the
  DISCARD that frees it) waits on held work -- put there by the frame's own member, another frame's member or ordinary
  work, before and after the certificate, in the forms the consult lists. Each is refused at the authorization that
  would let it, and its safe-release control (the same state without the wait) is permitted. Where added the
  check, the case also runs with it taken back and the hazard passes.
   adds a busy cell whose running instruction is its last (busy-cell term), the counter each case expects,
  the commit boundary's certificate (`_r8_rev_refuses`), a cancel's release of the revised cells' guard, an ended
  frame's admitted member, and the registration's what-if reading the frame's own work.
- The interleaving model (`TestModel`): every interleaving of four small programs' registrations (through the device's
  own check, `_r8_reg_refuses`), FORWARDs, starts, verdicts, decisions and splices, the rule deciding, with the
  invariant checked after every transition by an oracle that shares no search with the rule (a greatest fixpoint of
  what can still run), and no reachable state stuck; the state counts are asserted. P3 is, P4
  a safe cross-frame permission. With a check taken back (the own members at FORWARD, the block's permission, the
  candidate guard, record of an ended frame's members), the oracle finds the violation.
- The rest (`TestParts`): the reset of r8_b1 and the witness at a rebase, the knot's cap and its reading of a frame
  waiting in materialize at its witness, the detector's delivery case and its hardware clause (arm D's alone), the
  status port's aging, and the guard service's respect for the reserved room."""
from __future__ import annotations

import bisect
import collections
import unittest
from types import SimpleNamespace

from .device import ScoutDeviceManager as SDM, DeferFrame, R29_KNOT_EVERY, R8Fail
from .hwlimits import HWLimits


def _dev(prog, frames, started=(), busy=(), pend=(), target=None, path=0):
    """prog: [(name, cells, extra preds, duration)], an instruction's index its position; frames: [dict(vid, steps,
    fence=(), ws={j: cells}, admitted=0, live=True, guard_on=True, state="carry", target=-1)]; started: instructions
    started (and ended); busy: cells a running instruction occupies.  A cell carries a patch when its last started
    instruction is not a DISCARD."""
    d = SDM.__new__(SDM)
    pred, succ, last, cells = collections.defaultdict(set), collections.defaultdict(set), {}, collections.defaultdict(list)
    for i, (_nm, cs, ex, _du) in enumerate(prog):
        pred[i] |= set(ex)
        for c_ in cs:
            if c_ in last:
                pred[i].add(last[c_])
            last[c_] = i
            cells[c_].append(i)
    for i, ps in list(pred.items()):
        for p in ps:
            succ[p].add(i)
    started = set(started)
    d._adj = lambda: (pred, succ)
    d.n_original = len(prog)
    d.schedule_instructions = [SimpleNamespace(instruction=SimpleNamespace(name=nm, patches=list(cs)), end_round=(0 if i in started else -1))
                               for i, (nm, cs, _ex, _du) in enumerate(prog)]
    d._instruction_durations = [du for (_n, _c, _e, du) in prog]
    d._is_startup_instruction = [False] * len(prog)
    d._defer_cells, d._defer_started = dict(cells), started
    d._active_patches = {c_ for c_, lst in cells.items() if [i for i in lst if i in started][-1:] and prog[[i for i in lst if i in started][-1]][0] != "DISCARD"}
    d._r7_piu = set(busy)
    d._active_patches -= d._r7_piu
    d._defer_locked, d._defer_cell_ptr, d._r8_disc, d._defer_stats = {}, {}, {}, collections.Counter()
    d._r8_pend = {c_: 1 for c_ in pend}
    d._defer_target_cells = {}
    d._defer_ws_cells, d._defer_hold_of, d._defer_member, d._frames, d._live_frames = {}, {}, {}, {}, set()
    d._r8, d._r7_guard, d._r8_gsvc, d._hw, d.current_round, d.path, d._late_open = True, True, 0, None, 1, path, None
    d._defer_target_need, d._defer_guarded, d._defer_exempt_pins, d._defer_perm_cache = set(), set(), set(), {}
    d._r8_need, d._r8_tg_off, d._active_instructions = frozenset(), False, set()
    d._r8_orph, d._defer_claim, d.arm = {}, {}, "D"
    d._r8_orph_passed, d._r8_orw_vids, d._r8_wn_vids, d._r8_fail = set(), set(), set(), None
    ws_of = {}
    for f in frames:
        F = len(f["steps"])
        fr = DeferFrame(vid=f["vid"], c=None, reg_round=0, members=[set(s) for s in f["steps"]], r=[1.0] * (F + 1), fence=set(f.get("fence", ())))
        fr.kof = {i: k for k, s in enumerate(fr.members) for i in s}
        fr.admitted = fr.r8_wit = f.get("admitted", 0)
        fr.limit = fr.top = F
        fr.state, fr.target = f.get("state", "carry"), f.get("target", -1)
        fr.feas = [True] * (F + 1)
        fr.unadm = {i for k in range(fr.admitted, F) for i in fr.members[k] if i not in started}
        ws_of[fr.vid] = {j: frozenset(c_) for j, c_ in f.get("ws", {}).items()}
        fr.guard = set().union(*ws_of[fr.vid].values()) if ws_of[fr.vid] else set()
        d._frames[fr.vid] = fr
        if not f.get("live", True):
            continue
        d._live_frames.add(fr.vid)
        for i in (fr.fence | fr.unadm) - started:
            d._defer_hold_of.setdefault(i, set()).add(fr.vid)
        for i in fr.kof:
            d._defer_member[i] = fr.vid
        if f.get("guard_on", True):
            for c_ in fr.guard:
                d._defer_ws_cells[c_] = d._defer_ws_cells.get(c_, 0) + 1
        else:
            fr.hw_guard_on, fr.r8_pend = False, frozenset(fr.guard)
            for c_ in fr.guard:
                d._r8_pend[c_] = d._r8_pend.get(c_, 0) + 1
        if fr.state == "materialize" and fr.target >= 0:
            fr.target_ws = ws_of[fr.vid].get(fr.target, frozenset())
            for c_ in fr.target_ws:
                d._defer_target_cells[c_] = d._defer_target_cells.get(c_, 0) + 1
    for c_, n in (target or {}).items():
        d._defer_target_cells[c_] = d._defer_target_cells.get(c_, 0) + n
    d._defer_workspace = lambda f_, j: ws_of.get(f_.vid, {}).get(j, frozenset())
    if d._defer_target_cells:                                          # (the target guard's exemptions, as `_defer_phase` writes them)
        need = set()
        for vid in d._live_frames:
            fr = d._frames[vid]
            for c_ in fr.target_ws:
                if c_ in d._active_patches or c_ in d._r7_piu:
                    d._defer_first_unstarted(c_)
                    rel = d._r8_release(c_, d._defer_cell_ptr.get(c_, 0))[0]
                    if rel is not None:
                        need |= d._defer_cone(rel, cap=SDM.R8_CAP)
        d._defer_target_need = need
    return d


OP, DIS = "OP", "DISCARD"


def _cert_r8e(dev, certs, perms):
    """`_r8_cert`, for `_undo("orph_cert")`: an ended frame's member checked against every certificate, its own hold
    counted in the walk from its release (parts 2 and 3 taken back)."""
    if not perms or not certs:
        return True
    si, cells_of, hold_of = dev.schedule_instructions, dev._defer_cells, dev._defer_hold_of
    allc = set().union(*(c_[0] for c_ in certs))
    for m, (y, km, ky) in perms.items():
        qs = [q for q in si[m].instruction.patches if q in allc and dev._r8_places(m, q)]
        for cert in certs:
            cells, held, guard = cert[:3]
            h = lambda x, held=held, y=y, km=km, ky=ky: bool(hold_of.get(x, set()) - {y}) if ky.get(x, km + 1) <= km else held(x)
            for q in qs:
                if q in cells:
                    rel, freed = dev._r8_release(q, bisect.bisect_left(cells_of.get(q, ()), m) + 1)
                    if not freed or dev._r8_walk([rel], h, guard):
                        dev._r8_fail = R8Fail((m, y, q))
                        return dev._r8_fail
    return True


def _perms_r8e(orig):
    """`_r8_perms`, for `_undo("orph_live")`: an ended frame's member's entry written over a live frame's (part 1 taken back)."""
    def perms(dev, top=None, block=None):
        out = orig(dev, top, block)
        bm = {m for k in range(block[0].admitted, block[2]) for m in block[0].members[k]} if block is not None else set()
        out.update({m: (None, k, {}) for m, (_y, k) in dev._r8_orph.items() if m in out and m not in bm})
        return out
    return perms


def _undo(name):
    """A check, or added, taken back (the earlier code path there)."""
    return {"orph_live": ("_r8_perms", _perms_r8e),                                   # (three parts of check,
            "orph_cert": ("_r8_cert", lambda orig: _cert_r8e),                          #  finding 1, and the guard's cap in the
            "gcap": ("_r8_gcap", lambda orig: lambda dev, x, q: False),                 #  walk, finding 7)
            "own": ("_r8_perms", lambda orig: lambda dev, top=None, block=None:
                    {m: yk for m, yk in orig(dev, top, None).items() if block is None or yk[0] != block[0].vid}),
            "members": ("_r8_perms", lambda orig: lambda dev, top=None, block=None: {}),
            "places": ("_r8_places_ok", lambda orig: lambda dev, fr, a, b: True),
            "guard": ("_r8_hold", lambda orig: lambda dev, idx, p: False),
            "orphans": ("_r8_orphan_add", lambda orig: lambda dev, fr: None)}[name]       # (record at a frame's end)


class _Undone:
    def __init__(self, *names):
        self.names, self.saved = names, []

    def __enter__(self):
        for n in self.names:
            meth, mk = _undo(n)
            self.saved.append((meth, getattr(SDM, meth)))
            setattr(SDM, meth, mk(getattr(SDM, meth)))

    def __exit__(self, *a):
        for meth, f in reversed(self.saved):
            setattr(SDM, meth, f)


# ---------------------------------------------------------------- the hazard cases
# Instruction 0 is X's fence (held while X lives); the release of the hazard's patch waits on it when `wait` is set, and
# on nothing held otherwise (the safe-release control).  X is frame 1, Y frame 2, Z frame 3 (registration order).

def _case(kind, wait):
    w = [0] if wait else []
    if kind == "own_block":            # X's FORWARD 0 -> 1 admits m, which puts a patch on A of ws(1); its release waits on X's fence
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], w, 1), (OP, ["G"], [], 1)]
        return prog, [dict(vid=1, steps=[[1], [4]], fence=[0], ws={0: ["Z"], 1: ["A"], 2: ["A"]})], (), ("fwd", 1, 0, 1)
    if kind == "own_earlier":          # X admitted 1 with m (step 0) not started; its FORWARD 1 -> 2 certifies ws(2) = {A}
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], w, 1), (OP, ["G"], [], 1), (OP, ["H"], [], 1)]
        return prog, [dict(vid=1, steps=[[1], [4], [5]], fence=[0], ws={0: ["Z"], 1: ["Q"], 2: ["A"]}, admitted=1)], (), ("fwd", 1, 1, 2)
    if kind == "other_before":         # Y's admitted member m (not started) puts a patch on A; X's FORWARD 0 -> 1 certifies ws(1) = {A}
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], w, 1), (OP, ["G"], [], 1), (OP, ["H"], [], 1)]
        return prog, [dict(vid=1, steps=[[4], [6]], fence=[0], ws={1: ["A"]}), dict(vid=2, steps=[[1], [5]], ws={0: ["Q"]}, admitted=1)] \
            , (), ("fwd", 1, 0, 1)
    if kind == "other_after":          # X certified at 1 (admitted); Y's FORWARD 0 -> 1 admits m onto A, its release on X's suffix
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], [6] if wait else [], 1), (OP, ["G"], [], 1),
                (OP, ["H"], [], 1), (OP, ["K"], [], 1)]
        return prog, [dict(vid=1, steps=[[4], [6]], fence=[0], ws={1: ["A"]}, admitted=1), dict(vid=2, steps=[[1], [5]], ws={0: ["Q"], 1: ["Q"]})] \
            , (), ("fwd", 2, 0, 1)
    if kind == "ordinary_before":      # ordinary o already put its patch on A; next user harmless, the DISCARD waits (g at the FORWARD)
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], w, 1), (OP, ["G"], [], 1)]
        return prog, [dict(vid=1, steps=[[4], []], fence=[0], ws={1: ["A"]})], {1}, ("fwd", 1, 0, 1)
    if kind == "ordinary_after":       # X certified; ordinary o would put a patch on free A: the candidate guard holds it
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], w, 1), (OP, ["G"], [], 1)]
        return prog, [dict(vid=1, steps=[[4], []], fence=[0], ws={1: ["A"]}, admitted=1)], (), ("hold", 1)
    if kind == "reoccupied":           # free -> occupied -> free -> occupied: o1's patch is released safely by d1, then Y's member m2
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], w, 1), (OP, ["G"], [], 1),
                (OP, ["H"], [], 1)]                                    # puts one there again, released by d2, which waits
        return prog, [dict(vid=1, steps=[[5], []], fence=[0], ws={1: ["A"]}), dict(vid=2, steps=[[3], [6]], ws={0: ["Q"]}, admitted=1)] \
            , {1}, ("fwd", 1, 0, 1)
    if kind == "busy":                 # an instruction runs on A (busy, no patch yet); the release after it waits
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], w, 1), (OP, ["G"], [], 1)]   # (the next-user test refuses it first)
        return prog, [dict(vid=1, steps=[[3], []], fence=[0], ws={1: ["A"]})], {1}, ("fwd_busy", 1, 0, 1)
    if kind == "busy_last":            # busy-cell term: the instruction running on A is A's last and no DISCARD, so its
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (OP if wait else DIS, ["A"], [], 1), (OP, ["G"], [], 1)]   # patch
        return prog, [dict(vid=1, steps=[[3], []], fence=[0], ws={1: ["A"]})], {1, 2}, ("fwd_busy", 1, 0, 1)   # never leaves
    if kind == "pending_member":       # X's registration event pending (its cells in `_r8_pend`); Y's FORWARD admits m onto A
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], w, 1), (OP, ["G"], [], 1), (OP, ["H"], [], 1)]
        return prog, [dict(vid=1, steps=[[4]], fence=[0], ws={0: ["A"]}, guard_on=False), dict(vid=2, steps=[[1], [5]], ws={0: ["Q"], 1: ["Q"]})] \
            , (), ("fwd", 2, 0, 1)
    if kind == "pending_ordinary":     # the same pending cells; ordinary o onto A: the guard holds it from the request on
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], w, 1), (OP, ["G"], [], 1)]
        return prog, [dict(vid=1, steps=[[4]], fence=[0], ws={0: ["A"]}, guard_on=False)], (), ("hold", 1)
    if kind == "indirect":             # X's block member m onto A; A's release needs ordinary p onto Z's free cell B, which the guard
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (OP, ["B"], [], 1), (DIS, ["B"], w, 1), (DIS, ["A"], [2], 1), (OP, ["G"], [], 1)]
        return prog, [dict(vid=1, steps=[[1], [5]], fence=[0], ws={1: ["A"]}), dict(vid=3, steps=[[]], ws={0: ["B"]})], (), ("fwd", 1, 0, 1)
    raise KeyError(kind)                                               # (holds while B's release waits)


def _ask(kind, wait, undo=()):
    prog, frames, started, (what, *arg) = _case(kind, wait)
    d = _dev(prog, frames, started=started, busy=({"A"} if what == "fwd_busy" else ()))
    with _Undone(*undo):
        if what.startswith("fwd"):
            vid, a, b = arg
            return d._r7_refuses(d._frames[vid], a, b), d
        return d._defer_hold(arg[0]) is not None, d


class TestHazards(unittest.TestCase):
    CASES = ("own_block", "own_earlier", "other_before", "other_after", "ordinary_before", "ordinary_after", "reoccupied",
             "busy", "busy_last", "pending_member", "pending_ordinary", "indirect")
    ADDED = {"own_block": ("own",), "own_earlier": ("own",), "other_before": ("members",), "reoccupied": ("members",)}
    EXPECT = {"own_block": "rule_fwd_member_refusals", "own_earlier": "rule_fwd_member_refusals",   # (the check that
              "other_before": "rule_fwd_member_refusals", "other_after": "rule_rec_fwd_places",      #  refuses each case,
              "ordinary_before": "rule_rec_fwd_g", "ordinary_after": "rule_kept_off",                #  by its counter)
              "reoccupied": "rule_fwd_member_refusals", "busy": "guard_refusals", "busy_last": "rule_rec_fwd_g",
              "pending_member": "rule_rec_fwd_places", "pending_ordinary": "rule_kept_off", "indirect": "rule_fwd_member_refusals"}

    def test_hazards_refused_controls_permitted(self):
        for kind in self.CASES:
            with self.subTest(kind=kind):
                refused, d = _ask(kind, True)
                got = {k[3:]: v for k, v in d._defer_stats.items() if v and k.startswith("r7:") and not k.startswith("r7:rule_rec_")
                       or k in ("r7:rule_rec_fwd_g", "r7:rule_rec_fwd_places")}
                self.assertTrue(refused, (kind, got))
                self.assertGreater(got.get(self.EXPECT[kind], 0), 0, (kind, got))   # refused by the check the case is about
                self.assertFalse(_ask(kind, False)[0], kind)               # the safe-release control stays permitted
                for u in [self.ADDED.get(kind)] if kind in self.ADDED else []:
                    self.assertFalse(_ask(kind, True, undo=u)[0], (kind, "taken back", u))   # with check taken back the
                                                                           # hazard passes (for other_before and reoccupied
                                                                           # own checks made the state unreachable: this is the
                                                                           # check alone, taken back)

    def test_commit_boundary_certificate(self):
        """(a2)'s certificate of the commit boundary j: Y's admitted member m can still put a patch
        on A, the reviser X's cell at j = 1, released by a DISCARD that waits on n, a hold the revision adds; the revised
        boundary 0 is clean. `_r8_rev_refuses` refuses, by the members check; with only boundary 0's certificate kept,
        and in the control (no wait), it passes."""
        for wait in (True, False):
            prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], [5] if wait else [], 1), (OP, ["G"], [], 1),
                    (OP, ["H"], [], 1), (OP, ["N"], [], 1)]
            d = _dev(prog, [dict(vid=1, steps=[[3], [4]], fence=[0], ws={0: ["Z"], 1: ["A"], 2: ["B"]}, admitted=1),
                            dict(vid=2, steps=[[1], []], ws={0: ["Q"]}, admitted=1)])
            fr = d._frames[1]
            tmp = DeferFrame(vid=1, c=None, reg_round=0, members=[{5}], r=[1.0, 1.0], fence=set())
            tmp.kof, tmp.feas, tmp.unadm = {5: 0}, [True, True], {5}
            w0 = d._defer_workspace
            d._defer_workspace = lambda f_, j: ({0: frozenset({"R"}), 1: frozenset({"S"})}.get(j, frozenset()) if f_ is tmp else w0(f_, j))
            self.assertEqual(d._r8_rev_refuses(fr, tmp, 1, {5}, 1), wait)
            self.assertEqual(d._defer_stats["r7:rule_reb_member_refusals"], int(wait))
            if wait:
                orig = SDM._r8_cert
                SDM._r8_cert = lambda dev, certs, perms: orig(dev, certs[:1], perms)   # (the revised boundary 0's alone)
                try:
                    self.assertFalse(d._r8_rev_refuses(fr, tmp, 1, {5}, 1))
                finally:
                    SDM._r8_cert = orig

    def test_cancel_releases_the_revision_guard(self):
        """A revision guards its revised cells from the revision on (`_r8_pend_reb`, which `_w2_act`, `_h_choose` and
        `_u_act` call), and a cancel (`_h_release_next`, which `_w2_cancel` and `_u_cancel` call) releases them with the
        revision's new holds and members.  Ordinary o's placement on the revised cell A, whose release waits on X's fence,
        is held while the revision stands and free after the cancel."""
        prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], [0], 1), (OP, ["G"], [], 1), (OP, ["H"], [], 1)]
        d = _dev(prog, [dict(vid=1, steps=[[3]], fence=[0], ws={0: ["Z"]})])
        fr = d._frames[1]
        tmp = DeferFrame(vid=1, c=SimpleNamespace(F=1), reg_round=0, members=[{4}], r=[1.0, 1.0], fence=set())
        tmp.feas = [True, True]
        w0 = d._defer_workspace
        d._defer_workspace = lambda f_, j: ((frozenset({"A"}) if j == 0 else frozenset()) if f_ is tmp else w0(f_, j))
        nxt = SimpleNamespace(tmp=tmp, new_holds={4}, extra_claims=set(), extra_members={4})
        self.assertIsNone(d._defer_hold(1))                                # (A is no frame's cell yet)
        fr.h_next = nxt
        d._r8_pend_reb(fr, nxt)
        d._defer_hold_of.setdefault(4, set()).add(1); d._defer_member[4] = 1   # (the revision's new hold and member)
        self.assertEqual(d._defer_hold(1), "defer_rule_a")                 # guarded from the revision on
        d._h_release_next(fr)
        self.assertIsNone(d._defer_hold(1))
        self.assertEqual((fr.r8_pend_reb, d._r8_pend.get("A"), fr.h_next, d._defer_hold_of.get(4), d._defer_member.get(4)),
                         (frozenset(), None, None, None, None))

    def test_ended_frame_member_stays_authorized(self):
        """at the unit level: Y's admitted member x (not started) puts the first patch on P, X's free
        candidate cell, released by a DISCARD that waits on X's fence. Y has ended. With `_defer_finish`'s record
        (`_r8_orphan_add`) x keeps its permission: it passes the dispatch, stays among the permissions with no member
        exception, and the search does not treat it as guardable. Without the record the candidate guard holds it
        for as long as X lives."""
        prog = [(OP, ["FX"], [], 1), (OP, ["FY"], [], 1), (OP, ["P"], [], 1), (OP, ["P"], [], 1), (DIS, ["P"], [0], 1), (OP, ["XS"], [], 1)]
        for keep in (True, False):
            d = _dev(prog, [dict(vid=1, steps=[[5]], fence=[0], ws={0: ["Q"], 1: ["P"]}),
                            dict(vid=2, steps=[[2], []], fence=[1], ws={0: ["W"]}, admitted=1, live=False)])
            if keep:
                d._r8_orphan_add(d._frames[2])
            self.assertEqual(d._defer_hold(2), None if keep else "defer_rule_a", keep)
            self.assertEqual(d._r8_perms().get(2), (None, 0, {}) if keep else None, keep)
            self.assertEqual(d._r8_guardable(2), not keep, keep)
            self.assertEqual(d._defer_stats["r7:rule_orphan_passes"], int(keep), keep)

    def test_ended_member_adopted(self):
        """the three parts of fix. Y has ended and its admitted member m (not started) puts
        the first patch on P, released by dP. (1) A live frame Z that has admitted m keeps its entry and exception:
        m is listed as Z's. (2) X registers with P in ws(0) and m in its own step 0: X holds m until it has settled, so m's
        permission is passed over. (3) X registers with P in ws(0) while another live frame Z holds m in a step it has
        not admitted: m's own hold is not counted in the walk from its release, and X registers. With the part taken
        back X waits, the failing permission an ended frame's member's (`R8Fail`)."""
        prog = [(OP, ["FX"], [], 1), (OP, ["FY"], [], 1), (OP, ["P"], [], 1), (DIS, ["P"], [], 1), (OP, ["XS"], [], 1),
                (OP, ["FZ"], [], 1), (OP, ["ZS"], [], 1)]
        y = dict(vid=2, steps=[[2]], fence=[1], ws={0: ["W"]}, admitted=1, live=False)
        for undo in ((), ("orph_live",)):                                  # (1)
            with _Undone(*undo):
                d = _dev(prog, [dict(y), dict(vid=3, steps=[[2], [6]], fence=[5], ws={0: ["V"]}, admitted=1)])
                d._r8_orphan_add(d._frames[2])
                self.assertEqual(d._r8_perms().get(2), (None, 0, {}) if undo else (3, 0, d._frames[3].kof), undo)
        for part, z in (("own", None), ("held", dict(vid=3, steps=[[6], [2]], fence=[5], ws={0: ["V"]}))):   # (2), (3)
            xs = [[2, 4]] if part == "own" else [[4]]
            for undo in ((), ("orph_cert",)):
                with _Undone(*undo):
                    d = _dev(prog, [dict(vid=1, steps=xs, fence=[0], ws={0: ["P"]}, live=False), dict(y)] + ([z] if z else []))
                    d._r8_orphan_add(d._frames[2])
                    if z:
                        self.assertIn(3, d._defer_hold_of.get(2, set()), "Z holds m")
                    fx = d._frames[1]
                    got = d._r8_reg_refuses(fx, 0, fx.fence, 0)
                    self.assertEqual(got, "orphans" if undo else None, (part, undo))
                    if undo:
                        self.assertEqual(tuple(d._r8_fail), (2, None, "P"), part)

    def test_walk_counts_the_guard_cap(self):
        """the candidate guard's r7 test answers hold at its search cap (`R7_GUARD_CAP`), and rule
        (a)'s walk counts such a placement as held too (`_r8_gcap`, `rule_guard_cap`). Ordinary x puts the first patch on
        X's candidate cell Q; its next user u waits on a chain of three. With the cap at 2 the guard holds x and the walk
        says held; at 2,000 neither; with check taken back the walk disagrees with the guard."""
        prog = [(OP, ["F"], [], 1), (OP, ["A1"], [], 1), (OP, ["A2"], [1], 1), (OP, ["A3"], [2], 1), (OP, ["Q"], [], 1),
                (OP, ["Q", "A3"], [], 1), (DIS, ["Q"], [], 1), (OP, ["XS"], [], 1)]
        for cap, undo, walk in ((2, (), True), (2000, (), False), (2, ("gcap",), False)):
            with _Undone(*undo):
                d = _dev(prog, [dict(vid=1, steps=[[7]], fence=[0], ws={0: ["Q"], 1: ["Q"]})])
                d.R7_GUARD_CAP = cap
                self.assertEqual(d._r7_keeps_off(5), cap == 2, cap)
                self.assertEqual(d._r8_walk([4], lambda z: False), walk, (cap, undo))
                self.assertEqual(d._defer_stats["r7:rule_guard_cap"], int(walk), (cap, undo))

    def test_rebased_prefix_listed_and_recorded(self):
        """a rebased frame's prefix (`h_prefix`) holds its members up to its end; one that has not
        started (a startup instruction here) is a permission of the live frame, with no exception, and the record
        takes it at the frame's end."""
        prog = [(OP, ["F"], [], 1), (OP, ["P"], [], 1), (DIS, ["P"], [], 1), (OP, ["S"], [], 1)]
        d = _dev(prog, [dict(vid=1, steps=[[3]], fence=[0], ws={0: ["W"]})])
        fr = d._frames[1]
        fr.h_prefix = [({1}, set())]
        d._defer_member[1] = 1
        d._is_startup_instruction[1] = True
        d._r8_pcache = None
        self.assertEqual(d._r8_perms().get(1), (1, -1, fr.kof))
        d._r8_orphan_add(fr)
        self.assertEqual(d._r8_orph.get(1), (1, -1))

    def test_registration_whatif_counts_own_work(self):
        """a what-if at the registration (the decision's first decision, V2q's readiness query) runs the
        FORWARD test before the frame's members and holds are written. `_r8_fwd_held` reads the frame's steps and fence
        from the frame itself, so there too its fence and its steps from b on count as held."""
        d = _dev([(OP, ["F"], [], 1), (OP, ["G"], [], 1), (OP, ["H"], [], 1)],
                 [dict(vid=1, steps=[[1], [2]], fence=[0], ws={}, live=False)])     # (not registered: nothing written)
        h = d._r8_fwd_held(d._frames[1], 1)
        self.assertEqual((h(0), h(1), h(2)), (True, False, True))

    def test_next_user_harmless_release_blocked(self):
        """next-user test passes and only the whole release (rule (a)'s g) refuses."""
        prog, frames, started, _ = _case("ordinary_before", True)
        d = _dev(prog, frames, started=started)
        fr = d._frames[1]
        self.assertFalse(d._defer_ws_fenced(fr, 1, d._r7_piu, held=d._r7_holdable))
        self.assertTrue(d._r8_refuses_at(fr, 1, held=d._r8_fwd_held(fr, 1)))

    def test_revision_pins_future_placement(self):
        """A revision's new holds pin the release of a patch Y's admitted member can still put on X's free candidate cell
        (`_r8_pins_others`); no new holds, no search; the control's release waits on none of them."""
        for wait in (True, False):
            prog = [(OP, ["F"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], [5] if wait else [], 1), (OP, ["G"], [], 1), (OP, ["H"], [], 1),
                    (OP, ["N"], [], 1)]
            d = _dev(prog, [dict(vid=1, steps=[[3]], fence=[0], ws={0: ["A"]}), dict(vid=2, steps=[[1], [4]], ws={0: ["Q"]}, admitted=1),
                            dict(vid=4, steps=[[]], ws={})])
            self.assertEqual(d._r8_pins_others(d._frames[4], {5}, 0), wait)
            self.assertFalse(d._r8_pins_others(d._frames[4], set(), 0))

    def test_take_back_moves_the_witness(self):
        """A take-back to j makes j the witness, j's certificate made when the frame reached it; the settlement caps there."""
        prog = [(OP, ["F"], [], 1), (OP, ["G"], [], 1), (OP, ["H"], [], 1), (OP, ["K"], [], 1)]
        d = _dev(prog, [dict(vid=1, steps=[[1], [2], [3]], fence=[0], ws={0: ["A"], 1: ["B"], 2: ["C"]}, admitted=2)])
        fr = d._frames[1]
        d._defer_unadmit(fr, 1)
        self.assertEqual((fr.admitted, fr.r8_wit), (1, 1))
        self.assertEqual(d._defer_hold_of.get(2), {1})                     # (step 1 held again)
        d._r8_settle(fr)
        self.assertEqual((fr.r8_cap, fr.limit, fr.state), (1, 1, "decide"))
        self.assertEqual(d._defer_stats["r7:rule_wit_missing"], 0)


# ---------------------------------------------------------------- the interleaving model

def _oracle(d, X, w, rank):
    """Is frame X's boundary w reachable, by a computation that shares no search with rule (a)? X's fence and steps
    from w, and every earlier-ranked live frame's fence, unadmitted steps and admitted steps it can still take back,
    stay held for good; later-ranked frames end (their holds and guards go), but their admitted members may run at any
    time. S, the instructions that can still run, is the greatest set closed: not held for good, every unstarted
    predecessor in S, and, for an instruction the candidate guard can hold (no live frame's admitted member, nor an ended
    frame's admitted member that has not started) that puts a
    patch on a guarded cell of X or an earlier frame, the release of that patch in S. w is reachable when X's prefix
    runs and every patch on ws(w) -- there now, or put there by an instruction of S, or by an earlier frame's admitted
    member that can still be taken back (with its own steps up to it then not held) -- is released by an instruction
    of S."""
    fx, started, pred = d._frames[X], d._defer_started, d._adj()[0]
    earlier = [y for y in d._live_frames if rank[y] < rank[X]]
    G = set().union(*(d._frames[y].guard for y in earlier + [X]))
    free_m = {i for y in d._live_frames for i, k in d._frames[y].kof.items() if k < d._frames[y].admitted} \
        | {i for i in d._r8_orph if i not in started}                     # (an ended frame's admitted members too)

    def held_for_good(free=()):
        H = set(fx.fence) | {i for i, k in fx.kof.items() if k >= w}
        for y in earlier:
            fy = d._frames[y]
            H |= set(fy.fence) | {i for i, k in fy.kof.items() if k >= d._defer_current(fy)}
        return H - started - set(free)

    def runs(H):
        S = {i for i in range(d.n_original) if i not in started and i not in H}
        while True:
            out = set()
            for i in S:
                if any(p not in started and p not in S for p in pred.get(i, ())):
                    out.add(i); continue
                if i not in free_m and d._instruction_durations[i] > 0:
                    for q in d.schedule_instructions[i].instruction.patches:
                        if q in G and d._r8_places(i, q):
                            r, freed = d._r8_release(q, bisect.bisect_left(d._defer_cells[q], i) + 1)
                            if not freed or (r not in S and r not in started):
                                out.add(i); break
            if not out:
                return S
            S -= out

    S = runs(held_for_good())
    if any(i not in started and i not in S for k in range(w) for i in fx.members[k]):
        return False
    for q in d._defer_workspace(fx, w):
        if q in d._active_patches or q in d._r7_piu:
            d._defer_first_unstarted(q)
            r, freed = d._r8_release(q, d._defer_cell_ptr.get(q, 0))
            if (r is None and q in d._active_patches) or (r is not None and (not freed or r not in S)):
                return False
        for i in d._defer_cells.get(q, ()):
            if i in started or not d._r8_places(i, q):
                continue
            S_i = S
            if i not in S:
                y = d._defer_member.get(i)
                if y not in earlier or i not in free_m:
                    continue
                ky = d._frames[y].kof[i]
                S_i = runs(held_for_good(free={x for x, k in d._frames[y].kof.items() if k <= ky}))
                if i not in S_i:
                    continue
            r, freed = d._r8_release(q, bisect.bisect_left(d._defer_cells[q], i) + 1)
            if not freed or r not in S_i:
                return False
    return True


def _explore(prog, frames, undo=(), cap=200000):
    """Every interleaving of the program's transitions: a registration (a frame with reg=False starts unregistered: it
    registers through the device's own check, `_r8_reg_refuses`, or its verdict comes first and it takes N's path,
    holding nothing; until then its fence waits for the decode), a FORWARD (one block, as rule (a) and test
    decide), the start of an instruction (as `_defer_hold` decides; an admitted member runs once its predecessors have),
    a verdict (the frame drops, or decides), a decision (any usable boundary from its current one to its witness, as the
    settlement allows: a take-back to it), and a splice (prefix done, workspace clear: the frame ends). A frame that
    ends leaves its admitted members that have not started to the device's record (`_r8_orphan_add`). Frames rank
    by registration. After every transition the oracle checks every live frame's witness. Returns (states, the first
    violation or stuck state or None, the registrations that passed with another frame's permission on the new
    frame's ws(0))."""
    F = {f["vid"]: len(f["steps"]) for f in frames}
    fence_of = {f["vid"]: set(f.get("fence", ())) for f in frames}

    def build(st):
        started, fs = st[0], dict(st[1])
        spec, ended = [], []
        for f in frames:
            adm, state, tgt = fs[f["vid"]]
            if state in ("unreg", "native"):
                spec.append(dict(f, admitted=0, live=False))
            elif state == "done":
                spec.append(dict(f, admitted=adm, live=False))
                ended.append(f["vid"])
            else:
                spec.append(dict(f, admitted=adm, state=state, target=tgt))
        d = _dev(prog, spec, started=started)
        for vid in ended:
            d._r8_orphan_add(d._frames[vid])
        return d

    start = (frozenset(), tuple(sorted((f["vid"], (0, "carry" if f.get("reg", True) else "unreg", -1)) for f in frames)),
             tuple(f["vid"] for f in frames if f.get("reg", True)))
    seen, todo, n, cross = {start}, [start], 0, 0
    with _Undone(*undo):
        while todo:
            st = todo.pop()
            n += 1
            if n > cap:
                return n, ("cap", st), cross
            started, fs0, order = st
            fs, rank = dict(fs0), {v: k for k, v in enumerate(order)}
            d = build(st)
            for x in d._live_frames:
                fx = d._frames[x]
                if not _oracle(d, x, fx.r8_wit, rank):
                    return n, ("violation", x, fx.r8_wit, st), cross
            waiting = set().union(*(fence_of[v] for v, (_a, s_, _t) in fs.items() if s_ == "unreg"))
            nxt = []

            def put(started_, fs_, order_=order):
                nxt.append((started_, tuple(sorted(fs_.items())), order_))
            for i in range(len(prog)):
                if i in started or i in waiting or any(p not in started for p in d._adj()[0].get(i, ())):
                    continue
                if d._defer_hold(i) is None:
                    put(started | {i}, fs)
            for x in sorted(fs):
                fx, (adm, state, tgt) = d._frames[x], fs[x]
                if state == "unreg":
                    mine_ = fx.fence | set().union(*fx.members)
                    if any(d._defer_member.get(i) in d._live_frames or i in d._defer_hold_of for i in mine_):
                        pass                                               # (the device's registration retries while another live
                                                                           #  frame has one of its instructions: P5)
                    elif d._r8_reg_refuses(fx, 0, fx.fence, F[x]) is None:
                        put(started, {**fs, x: (0, "carry", -1)}, order + (x,))
                        ws0 = d._defer_workspace(fx, 0)
                        cross += any(d._r8_places(m, q) for m in d._r8_perms() for q in d.schedule_instructions[m].instruction.patches
                                     if q in ws0)
                    put(started, {**fs, x: (0, "native", -1)})                                   # (its verdict came first)
                elif state == "carry":
                    if adm < F[x] and d._defer_admissible(fx, adm) and not d._r7_refuses(fx, adm, adm + 1):
                        put(started, {**fs, x: (adm + 1, "carry", -1)})
                    put(started, {**fs, x: (adm, "done", -1)})                                   # (the verdict: dropped)
                    put(started, {**fs, x: (adm, "decide", -1)})                                 # (the verdict: decide)
                elif state == "decide":
                    p = d._defer_current(fx)
                    for j in range(p, fx.r8_wit + 1):
                        if fx.feas[j] and not d._defer_ws_blocked(fx, j, d._r7_piu, splice=False):
                            put(started, {**fs, x: (j, "materialize", j)})
                elif state == "materialize":
                    if all(i in started for k in range(tgt) for i in fx.members[k]) and \
                            not any(c_ in d._active_patches or c_ in d._r7_piu for c_ in d._defer_workspace(fx, tgt)):
                        put(started, {**fs, x: (adm, "done", -1)})
            if not nxt and (len(started) < len(prog) or d._live_frames):
                return n, ("stuck", st), cross
            for s2 in nxt:
                if s2 not in seen:
                    seen.add(s2); todo.append(s2)
    return n, None, cross


# Five programs.  Instructions 0 and 1 are X's and Y's fences.  X is frame 1, Y frame 2; a frame with reg=False registers
# by a transition, through the device's registration check.
#  P1: X's step-0 member x0 puts a patch on A (X's ws(1)) whose release dA waits on X's fence (the own-member hazard);
#      Y's step-0 member y0 puts one on B (X's ws(0)) released by dB, which waits on X's fence (another frame's member);
#      ordinary o puts one on C (Y's workspace), its next user harmless, released by dC, which waits on X's fence
#      (ordinary work: Y would wait on the earlier X; only the whole release shows it).  Y registers by a transition.
_P1 = [(OP, ["FX"], [], 1), (OP, ["FY"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], [0], 1), (OP, ["GX"], [], 1),
       (OP, ["B"], [], 1), (DIS, ["B"], [0], 1), (OP, ["C"], [], 1), (OP, ["C"], [], 1), (DIS, ["C"], [0], 1)]
_F1 = [dict(vid=1, steps=[[2], [4]], fence=[0], ws={0: ["B"], 1: ["A"], 2: ["A"]}),
       dict(vid=2, steps=[[5], []], fence=[1], ws={0: ["C"], 1: ["C"]}, reg=False)]
#  P2: free -> occupied -> free -> occupied on A (X's ws(1)): ordinary o1 then d1 (safe), then Y's member y0 and d2,
#      which waits on X's fence; and an indirect guarded placement: the release of X's step-1 member's patch on E (X's
#      ws(2)) needs ordinary p onto Y's cell D, whose release dD waits on X's fence.  Y registers by a transition.
_P2 = [(OP, ["FX"], [], 1), (OP, ["FY"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], [], 1), (OP, ["A"], [], 1), (DIS, ["A"], [0], 1),
       (OP, ["GX"], [], 1), (OP, ["D"], [], 1), (DIS, ["D"], [0], 1), (OP, ["E"], [], 1), (DIS, ["E"], [7], 1)]
_F2 = [dict(vid=1, steps=[[6], [9]], fence=[0], ws={0: ["Q"], 1: ["A"], 2: ["E"]}),
       dict(vid=2, steps=[[4], []], fence=[1], ws={0: ["D"], 1: ["D"]}, reg=False)]
#  P3, Y's step-0 members m (first on Q) and x (first on P, after ordinary z); u uses Q and P
#      after them; dQ frees Q after u, and dP frees P after u and waits on X's fence.  X registers by a transition with
#      Q in ws(0) and P in ws(1).  Once X stands at 0 (its FORWARD is refused by x's permission on P), is contradicted and
#      waits for Q, and Y drops before x starts, x must keep its permission: held by the candidate guard (P is X's free
#      cell, dP waits on X's fence) it would keep Q occupied for good.
_P3 = [(OP, ["FX"], [], 1), (OP, ["FY"], [], 1), (OP, ["Z"], [], 1), (OP, ["Q"], [], 1), (OP, ["P"], [2], 1), (OP, ["Q", "P"], [], 1),
       (DIS, ["Q"], [], 1), (DIS, ["P"], [0], 1), (OP, ["XS"], [], 1), (OP, ["YS"], [], 1)]
_F3 = [dict(vid=2, steps=[[3, 4], [9]], fence=[1], ws={0: ["W"], 1: ["W"]}),
       dict(vid=1, steps=[[8]], fence=[0], ws={0: ["Q"], 1: ["P"]}, reg=False)]
#  P4, a safe cross-frame permission: Y's step-0 member m puts the first patch on Q (X's ws(0)), released by dQ, which waits
#      on y1, a member of the same step (the member exception: no hold); X registers by a transition while m's
#      permission stands, and every frame and instruction completes.
_P4 = [(OP, ["FX"], [], 1), (OP, ["FY"], [], 1), (OP, ["Q"], [], 1), (OP, ["R"], [], 1), (DIS, ["Q"], [3], 1), (OP, ["XS"], [], 1),
       (OP, ["YS"], [], 1)]
_F4 = [dict(vid=2, steps=[[2, 3], [6]], fence=[1], ws={0: ["W"], 1: ["W"]}),
       dict(vid=1, steps=[[5]], fence=[0], ws={0: ["Q"], 1: ["XW"]}, reg=False)]
#  P5, adoption: Y's step-0 member m puts the first patch on P, released by dP, and m is in X's
#      fence too, so it waits while X has not registered.  X registers by a transition with P in ws(0) and ws(1), and
#      only once Y has ended (the device's registration retries while another live frame has one of its instructions);
#      m is then an ended frame's member that X's fence holds until X has settled, so its permission is passed over
#      or X's verdict comes first and m runs.
_P5 = [(OP, ["FX"], [], 1), (OP, ["FY"], [], 1), (OP, ["P"], [], 1), (DIS, ["P"], [], 1), (OP, ["XS"], [], 1), (OP, ["YS"], [], 1)]
_F5 = [dict(vid=1, steps=[[4]], fence=[0, 2], ws={0: ["P"], 1: ["P"]}, reg=False),
       dict(vid=2, steps=[[2], [5]], fence=[1], ws={0: ["C"], 1: ["C"], 2: ["C"]})]
PROGS = {"P1": (_P1, _F1), "P2": (_P2, _F2), "P3": (_P3, _F3), "P4": (_P4, _F4), "P5": (_P5, _F5)}


class TestModel(unittest.TestCase):
    """The state counts are asserted, so a change of the rule or of the model shows here."""
    STATES = {"P1": 1782, "P2": 5829, "P3": 3088, "P4": 2808, "P5": 744}
    CROSS = {"P1": 0, "P2": 0, "P3": 68, "P4": 68, "P5": 16}                         # (registrations passed with another frame's
    TAKEN = {("P1", "own"): (207, "violation", 1, 1), ("P1", "places"): (501, "violation", 1, 0),   #  permission on ws(0))
             ("P1", "guard"): (433, "violation", 2, 0), ("P2", "places"): (3592, "violation", 1, 1),
             ("P3", "orphans"): (2619, "violation", 1, 0)}

    def test_invariant_every_interleaving(self):
        for name, (prog, frames) in PROGS.items():
            with self.subTest(prog=name):
                n, bad, cross = _explore(prog, frames)
                print("model %s: %d states, %s, registrations with another frame's permission on ws(0): %d" % (name, n, bad, cross))
                self.assertIsNone(bad, (name, n, bad))
                self.assertEqual((n, cross), (self.STATES[name], self.CROSS[name]), name)

    def test_violation_found_with_a_check_taken_back(self):
        for (name, undo), want in self.TAKEN.items():
            with self.subTest(prog=name, undo=undo):
                n, bad, _cross = _explore(*PROGS[name], undo=(undo,))
                print("model %s without %s: %d states, %s" % (name, undo, n, bad and bad[:3]))
                self.assertIsNotNone(bad, (name, undo))
                self.assertIn(bad[0], ("violation", "stuck"), bad)
                self.assertEqual((n, bad[0]) + (tuple(bad[1:3]) if bad[0] == "violation" else ()), want, (name, undo))


# ---------------------------------------------------------------- the rest

class TestParts(unittest.TestCase):

    def test_rebase_resets_the_witness(self):
        """`_h_rebase` resets r8_b1 and the witness to the rebased boundary 0, which (a2)
        certified (observed in real runs by test_r8's `test_revision_lifecycle`). Without the reset, a frame settling
        after its rebase would read the old contract's boundary: the witness check catches it (`rule_wit_missing`) and the
        cap falls back to a boundary nothing certified."""
        for stale, cap, missing in ((0, 0, 0), (2, 2, 1)):
            d = _dev([(OP, ["F"], [], 1), (OP, ["G"], [], 1), (OP, ["H"], [], 1)],
                     [dict(vid=1, steps=[[1], [2]], fence=[0], ws={0: ["A"], 1: ["B"], 2: ["C"]})])
            fr = d._frames[1]
            fr.r8_b1 = fr.r8_wit = stale                                   # (as `_h_rebase` leaves the frame, or without its reset)
            d._r8_settle(fr)
            self.assertEqual((fr.r8_cap, d._defer_stats["r7:rule_wit_missing"]), (cap, missing))

    def test_knot_reads_the_cap(self):
        """(detection): a settled frame's knot is read up to its cap, so a free boundary past the cap
        does not hide it."""
        prog = [(OP, ["F"], [], 1), (OP, ["G"], [], 1), (OP, ["A"], [], 1), (OP, ["A"], [4], 1), (OP, ["K"], [], 1)]
        for cap, want in ((None, set()), (1, {1})):                    # (A's next user waits on X's step-2 member; ws(2) free)
            d = _dev(prog, [dict(vid=1, steps=[[1], [], [4]], fence=[0], ws={1: ["A"], 2: []}, admitted=1, state="decide")], started={1, 2})
            fr = d._frames[1]
            fr.decide_waits, fr.top, fr.r8_cap = R29_KNOT_EVERY, 2, cap
            self.assertEqual(d._r29_knot(), want, cap)

    def test_detector_counts_delivery(self):
        """a spliced script whose decoded instructions are not delivered yet (path > 0) is pending;
        at path 0 the case never arises."""
        for path, want in ((60, "delivery"), (0, None)):
            d = _dev([(OP, ["F"], [], 1)], [dict(vid=1, steps=[[]], fence=[], ws={}, state="spliced")], path=path)
            d.arm, d.defer_cfg, d.current_round = "D", {"settle": "decode"}, 10
            d._completed_undecoded, d._decoded_undelivered, d._decoded_round = set(), set(), {7: 9, 8: 10}
            d._defer_waiting, d._defer_arrivals, d._recovering, d._rc_pending, d._instruction_frontier = {}, [], set(), set(), set()
            d.values = {1: SimpleNamespace(delivered_round=3, producer_idx=0, recovery=dict(kind="script", instrs={7, 8}))}
            self.assertEqual(d._r29_pending({}, {}), want, path)

    def test_knot_reads_materialize_at_witness(self):
        """a frame waiting in materialize at its witness for a patch to leave (`r8_mat_waits`) is
        read by rule 29's knot at that boundary, so a stall through held work shows as a deadlock (read frames in
        decide only). A wait through a guard's hold is not read (`_r29_blockers` reads holds), and such a stall is still
        censored."""
        prog = [(OP, ["F"], [], 1), (OP, ["G"], [], 1), (OP, ["A"], [], 1), (OP, ["A"], [0], 1), (DIS, ["A"], [], 1)]
        for rule, waits, want in ((True, R29_KNOT_EVERY, {1}), (True, 0, set()), (False, R29_KNOT_EVERY, set())):
            d = _dev(prog, [dict(vid=1, steps=[[1]], fence=[0], ws={0: ["A"]}, state="materialize", target=0)], started={2})
            d._r8 = rule
            fr = d._frames[1]
            fr.r8_mat_waits, fr.top = waits, 1                             # (A's next user waits on X's fence)
            self.assertEqual(d._r29_knot(), want, (rule, waits))

    def test_hw_service_reads_arm_D_alone(self):
        """an N or Q run has no frames; with the hardware mode and rule 29 its pending test must
        not read them (raised AttributeError there)."""
        d = SDM.__new__(SDM)
        d.arm, d._hw, d.values, d.current_round = "N", SimpleNamespace(queue=[], stalled=False, check_q=[]), {}, 5
        d._completed_undecoded, d._decoded_undelivered, d._recovering, d._rc_pending = set(), set(), set(), set()
        d._instruction_frontier, d._later_times = set(), {}
        self.assertIsNone(d._r29_pending({}, {}))
        self.assertFalse(hasattr(d, "_live_frames"))

    def test_guard_service_respects_reserved_room(self):
        """with gsvc > 0 a frame's own guard checks (`gsvc_try`) leave the status port's room
        reserved for an aged check, which is then served in the same round; without the reservation it spills."""
        for keep in (True, False):
            h = HWLimits.__new__(HWLimits)
            h.setting, h.R, h.cyc, h.gsvc, h.age = "improved", 10, {"CHECK": 2}, 1, 4
            h.checked, h.stats, h.ck_spill, h.ck_resv, h.now, h.checks = {}, collections.Counter(), {7: [1, 4]}, {7}, 5, 0
            if not keep:
                h.ck_resv = set()                                          # (gsvc_try reads no reservation)
            while h.gsvc_try():
                pass
            h.ck_resv = {7}
            self.assertEqual(h.guard_check(7, ["A"]), keep, keep)
            self.assertEqual(h.stats["r8_gsvc_resv"], int(keep), keep)

    def test_status_port_ages_spilled_checks(self):
        """Rule (a)'s fairness on the status port: three new instructions reach their guard checks before a fourth
        one every round, and the port serves three; without aging the fourth spills for good, with it it is served
        within HW_AGE + 1 rounds."""
        for age, served in ((None, None), (4, 5)):
            h = HWLimits.__new__(HWLimits)
            h.setting, h.R, h.cyc, h.gsvc, h.age = "improved", 4, {"CHECK": 2}, 0, age
            h.checked, h.stats, h.ck_spill, h.ck_resv, h.now = {}, collections.Counter(), {}, set(), None
            got = None
            for t in range(1, 40):
                h.now, h.checks = t, 0
                if h.age is not None and (h.ck_spill or h.ck_resv):
                    h._ck_age(t)
                for i in (3 * t, 3 * t + 1, 3 * t + 2):
                    h.guard_check(i, ("A",))
                if h.guard_check(10 ** 6, ("A",)):
                    got = t; break
            self.assertEqual(got, served, (age, dict(h.stats)))


if __name__ == "__main__":
    unittest.main()
