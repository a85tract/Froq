"""Rule 66's hardware-limit mode, from `froq/data/rule66_limits.json` (final file, 66b7975). Task, and the code
gate's fixes.

One object per run (`ScoutDeviceManager._hw`), made when the run sets `--hw-limits improved` (the evaluation's setting)
or `--hw-limits as_built` (the sensitivity). Off, nothing below runs and no result changes.

Every resource is a server with a budget of `round_cycles` (286) cycles a round. The reservation manager's events
(registration, commit check and reserve, splice and rebase, decide, frame end, script end, the dispatcher's guard
checks, the tick) are the file's operation lists: within an event the operations run in order, each on its port once
the previous one has completed; different events overlap across ports. A MASK operation writes mask register A or B
and costs 2 cycles for each 64-bit word in which the new content differs from the register's previous content (the
changed-word writes; a cell's id is its rank in row-major order among the layout's cells).

The service (spec 9). The tick runs first in every round. Events enter the operation FIFO in the file's queue order:
the waiting events (the deferral phase's, which wait for `serve`, and a stalled producer's) enter by priority (each
port's order: ends, then splices and rebases, decides, COMMIT checks and reserves, and registrations last) and, within a
priority, in the order submitted, while the FIFO has room, so registrations cannot keep a frame end out; an event whose
own queue (the splices, the HBM tags) is full waits without holding back the others, and room that frees during the
round lets the next ones in at once (`_admit`). The FIFO is served in the same order. A port is head-of-line blocked
for the rest of the round once an operation on it does not fit: no later event overtakes it there. Work that misses
the round carries over and is served first in the next round, in the same order. An event takes effect when its last
operation completes; the device then carries on with the frame in the same deferral phase
(`ScoutDeviceManager._hw_serve`), so a registration admits, a commit acts and a splice issues in the round its event
completes. The dispatcher's guard check of an instruction about to start: on the status port in the same round
(improved); as built, on the operation port in spec 9's place, so the dispatch queues it and the next round's service
serves it after the tick and the ends and before the splices, decides, commits and registrations, and the instruction
starts in the round its check is served (the deferral phase's events still take effect before the round's dispatch).
The finite queues: the operation FIFO (32 events), the splice queue (8), the HBM tags (64), and U's EPOCH and window
ingress (128 a pass, the device's `_u_pass`). A full queue stalls its producer: the event waits outside it and enters
it, in spec 9's order, when it has room (counted per queue: the events delayed, their rounds, the rounds the queue was full).
The splice engine is a server of its own: a splice costs 10 cycles to its first instruction plus one a dependency
edge, or the script store's words at 4 a cycle if that is longer (`store_words_bound`; a store-bound splice that
streams into the next round counts `store_words_spill`), and a rebase 50 more; a commit at a boundary past the
program's resident bound b_res waits one round for its HBM fetch. Capacities: 64 live values, the 16-boundary walk
cap, 64 reservation slots (held per reservation), 512 cells (checked at load). Overflow and backpressure are counted
(`summary`)."""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os

LIMITS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "rule66_limits.json")
SETTINGS = ("improved", "as_built")
WORDS_PER_INSTR = 5        # the script store's 16-bit words per spliced instruction: ~225 words for 46 instructions
B_RES = {"grover_ancilla_5": 4, "qft_10": 5, "qpeexact_10": 5,  # 4.2; every other program: every b;
         "qpeexact_20": -1}                                        # QPE-20: no level resident, every rebased table fetched
WALK_CAP = 16
SPLICE = "splice_and_rebase"


def load(path=None):
    path = path or LIMITS_FILE
    raw = open(path, "rb").read()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


class HWLimits:
    ck_f = 0          # a guard check's dependent read pays f more cycles, 1 + c0 + f (the queue's f)
    zl = False        # (plan v2 A1.3, ZL): a guard check takes no cycles
    r17 = None        # (---records, `froq/r17rec.py`): each request's submission, grant and completion, and
                      #  the queues' occupancy a round (`_r17_occ`); records only
    slow_s = None     # (--ctrl-slow s): the controller's clock s times slower (a Fraction); None, off
    slow_rec = None

    def set_slow(self, s, defer_latency=None):
        """(`--ctrl-slow s`; plan v2 4.2 A, F8), applied once. Every port gets R / s cycles a round (R =
        round_cycles, 286): `R` becomes that Fraction, so the carry is exact; the operations' and the requests' cycles
        stay as they are (each cycle is s times longer). The store's fetch, a fixed delay in rounds, takes
        ceil(s fetch_rounds). The lookup latency is in cycles, so R / s covers it. A service longer than what its port
        has streams on into the next rounds where the model has that: the splice engine (`_run`), the bounded check's and
        an event's cycles on the check port (`r9_take`, `ev_take`). The rest of the model serves an operation, a guard
        check and a guard service whole within one round. So it is refused where it would be undefined:
        - rule (a)'s A6 premise at R / s: the round's tick (TICK cycles on the tick port) and every single operation
          fit what their port has after the tick (`assert_fits`). The tick costs 67 cycles, so s <= 286 / 67 (4.27);
        - improved: a guard check (CHECK + the guard service + f) and a guard service fit a whole round."""
        from fractions import Fraction
        s = Fraction(s)
        assert s > 1 and self.slow_s is None, (s, self.slow_s)
        R0, f0 = int(self.p["round_cycles"]), self.fetch_rounds
        self.slow_s, self.R = s, Fraction(R0) / s
        self.fetch_rounds = math.ceil(f0 * s)
        tick = self.cyc["TICK"]
        assert tick <= self.R, ("--ctrl-slow %s: the round's tick (%d cycles) does not fit %s cycles a round; the model is "
                                "defined up to s = %s" % (s, tick, self.R, Fraction(R0, tick)))
        if self.setting == "improved" and not self.zl:
            chk = self.cyc["CHECK"] + self.gsvc + self.ck_f
            assert chk <= self.R and self.gsvc <= self.R, ("--ctrl-slow %s: a guard check (%d cycles) or a guard service "
                                                           "(%d) does not fit a round of %s cycles" % (s, chk, self.gsvc, self.R))
        if self.ids:
            self.assert_fits()                                             # (A6 at R / s: every operation after the tick)
        self.slow_rec = dict(s=str(s), round_cycles=str(self.R), fetch_rounds=[f0, self.fetch_rounds],
                             **({"defer_latency": list(defer_latency)} if defer_latency else {}))

    def __init__(self, dev, setting: str, params: dict, sha: str, bench: str | None = None):
        assert setting in SETTINGS, setting
        self.dev, self.setting, self.p, self.sha, self.bench = dev, setting, params, sha, bench
        self.R = int(params["round_cycles"])
        rm = params["resv"][setting]
        self.cyc = {k: int(v) for k, v in rm["cycles"].items()}
        self.events = rm["events"]
        self.order = rm["queue_order"]
        self.ports = list(rm["ports"])
        self.tick_port = "slot" if setting == "improved" else "op"
        self.check_port = "status" if setting == "improved" else "op"
        cap = params["capacities"]
        self.cap_values, self.cap_slots, self.cap_cells = int(cap["live_values"]), int(cap["resv_slots"]), int(cap["resv_cells"])
        self.cap_walk_keys, self.cap_rebased_keys = int(cap["walk_keys"]), int(cap["rebased_keys"])
        sp = params["splice"]
        self.splice_first, self.rebase_cycles = int(sp["first_instruction_cycles"]), int(sp["rebase_cycles"])
        st = params["store"]
        self.words_per_cycle, self.fetch_rounds = int(st["words_per_cycle"]), int(st["fetch_rounds"])
        q = params["queues"]
        self.q_resv, self.q_splice, self.q_hbm = int(q["resv_ops"]), int(q["splices"]), int(q["hbm_tags"])
        self.q_epoch, self.q_win = int(q["epoch"]), int(q["win_ingress"])
        self.lookup_latency = int(params["lookup"]["latency"])
        self.lookup_charged = bool(params["lookup"].get("charged", False))   # (round 7, 3(f): off unless the file sets it)
        # as built, the dispatcher's guard checks' place in the operation port's order, and the checks the dispatch queued
        self.guard_prio = self.order["op"].index("dispatcher_guard_check") if setting == "as_built" else None
        self.check_q: list = []
        self.check_pending: set = set()
        self.b_res = B_RES.get(bench, math.inf)
        self.masks = {"A": {}, "B": {}}                  # register -> {word: bits}
        self.queue: list = []                            # the operation FIFO: events not complete, in submission order
        self.stalled: list = []                          # events whose producer stalls (a full FIFO, splice queue or tag pool)
        self.batching = False                            # the deferral phase: events wait for `serve`
        self.now = None
        self.free = {}
        self.blocked: set = set()                        # ports head-of-line blocked for the rest of this round
        self.block_prio: dict = {}                       # port -> the priority of the operation that blocked it
        self.checked: dict = {}                          # instruction -> the round its guard check was served
        self.checks = 0
        self.age, self.gsvc = None, 0                    # round 8: rule (a)'s aging (A6) and the guard-service time (cycles)
        self.r9_debt = 0                                 # round 9: the check port's cycles owed into the next rounds
        self.ck_spill, self.ck_resv = {}, set()          # improved: spilled guard checks {idx: [first, last round]}; the reserved
        self.stats = collections.Counter()
        self.peak = collections.Counter()
        self.n_ev = 0
        self.ids: dict = {}                              # cell -> its id (`setup_cells`)
        self.slots: set = set()                          # the reservations that hold a slot

    # -- the layout
    def setup_cells(self, patches):
        """The cell ids: the layout's cells (every cell the program's instructions use) numbered in row-major order, and
        the layout check (512 cells; Adder-32, the largest, has 499 by this count).  A cell first named later (a
        script's routing cell outside them) takes the next id, counted (`cells_added`, and `cells_over` past 512)."""
        self.ids = {c: i for i, c in enumerate(sorted({(int(p[0]), int(p[1])) for p in patches}))}
        n = len(self.ids)
        self.layout_cells = n
        if n > self.cap_cells:
            self.stats["cells_over"] += 1
            raise RuntimeError("rule 66: the layout has %d cells, more than the reservation manager's %d (hw:cells_over)" % (n, self.cap_cells))

    def _cid(self, cell) -> int:
        key = (int(cell[0]), int(cell[1]))
        i = self.ids.get(key)
        if i is None:
            i = self.ids[key] = len(self.ids)
            self.stats["cells_added"] += 1
            if i >= self.cap_cells:
                self.stats["cells_over"] += 1
        return i

    def _words(self, cells) -> dict:
        out = {}
        for c in cells:
            i = self._cid(c)
            out[i >> 6] = out.get(i >> 6, 0) | (1 << (i & 63))
        return out

    # -- the round
    def round_start(self, now: int):
        """The tick first; the stalled events that have room enter the FIFO; then the carried work, in the queue order."""
        if self.now == now:
            return
        self.now = now
        self.free = {p: 0 for p in self.ports}
        self.free["splice"] = 0
        self.blocked, self.block_prio = set(), {}
        self.checks = 0
        if self.r9_debt:                                   # (an event in service keeps the check port first)
            self.r9_prefill()
        if self.age is not None and (self.ck_spill or self.ck_resv):
            self._ck_age(now)                              # (rule (a)'s fairness on the status port)
        self.free[self.tick_port] = self.cyc["TICK"]
        self.stats["ticks"] += 1
        if self.r17 is not None:
            self._r17_occ()                                        # (records only: what the round carries)
        self._admit()
        for q in {ev["stalled"] for ev in self.stalled}:
            self.stats["full_rounds:" + q] += 1
        if self.check_q:
            self.serve(self.guard_prio)                    # (as built: the carried ends, then the queued guard checks)
            self._serve_checks()
        if self.queue:
            self.serve()

    def charge_lookup(self):
        """Round 7's lookup-charging setting (plan v3 3(f)): a decision-table or admission lookup takes lookup.latency
        cycles of the controller's round on the tick port, ahead of the events served there; off by default."""
        if self.lookup_charged:
            self.free[self.tick_port] += self.lookup_latency
            self.stats["lookup_cycles_charged"] += self.lookup_latency

    def _prio(self, ev) -> int:
        port = ev["ops"][ev["i"]][0] if ev["i"] < len(ev["ops"]) else self.tick_port
        lst = self.order.get(port) or self.order.get("op") or []
        k = ev["kind"]
        if self.age is not None and self.now - ev["round"] >= self.age:
            if not ev.get("aged"):
                ev["aged"] = True
                self.stats["r8_aged_events"] += 1          # (round 8's A6: served first on its port and into a full queue)
            return -1
        return lst.index(k) if k in lst else len(lst)

    def _op_cycles(self, op: str, arg) -> int:
        if op.startswith("MASK"):
            reg = op.split()[1]
            new = self._words(arg or ())
            old = self.masks[reg]
            return 2 * sum(1 for w in set(new) | set(old) if new.get(w, 0) != old.get(w, 0))
        if op == "ENGINE":
            return int(arg) if self.slow_s is None else arg               # (--ctrl-slow: a streamed remainder may be a Fraction)
        if op == "GSVC":
            return self.gsvc
        return self.cyc[op]

    def _run(self, ev) -> bool:
        """Serve ev's remaining operations within this round's budget; True when its last one completes.  A port that
        cannot serve its operation is head-of-line blocked for the rest of the round."""
        if ev.get("not_before", -1) > self.now:
            return False
        t = ev["t"]
        while ev["i"] < len(ev["ops"]):
            port, op, arg = ev["ops"][ev["i"]]
            if port in self.blocked:
                self.stats["hol_blocked"] += 1
                return False
            c = self._op_cycles(op, arg)
            start = max(t, self.free.get(port, 0))
            end = start + c
            if end > self.R:
                if op == "ENGINE" and start < self.R:              # the splice engine streams on into the next round
                    done = self.R - start
                    ev["ops"][ev["i"]] = (port, op, (int(arg) if self.slow_s is None else arg) - done)
                    self.free[port] = self.R
                    self.stats["splice_spill_rounds"] += 1
                    if ev.get("store_bound"):
                        self.stats["store_words_spill"] += 1        # (its instructions' words stream into the next round)
                else:
                    self.stats["rm_spill_cycles"] += c
                self.blocked.add(port)
                self.block_prio.setdefault(port, self._prio(ev))
                return False
            self.free[port] = end
            t = end
            if op.startswith("MASK"):
                self.masks[op.split()[1]] = self._words(arg or ())
            ev["i"] += 1
        ev["t"] = t
        return True

    def _full(self, ev) -> str | None:
        """The queue a new event would overflow: the FIFO, the splice queue or the HBM tags; None if it has room."""
        if len(self.queue) >= self.q_resv:
            return "resv_ops"
        if ev["kind"] == SPLICE and sum(1 for x in self.queue if x["kind"] == SPLICE) >= self.q_splice:
            return "splices"
        if ev.get("not_before") is not None and sum(1 for x in self.queue if x.get("not_before", -1) > self.now) >= self.q_hbm:
            return "hbm_tags"
        return None

    def _admit(self):
        """The waiting events enter the FIFO in spec 9's queue order (priority, then submission) while it has room; one
        whose own queue (the splices, the HBM tags) is full waits without holding back the others.  An event that cannot
        enter when first offered stalls its producer (counted once, per queue)."""
        if not self.stalled:
            return
        keep = []
        for ev in sorted(self.stalled, key=lambda e: (self._prio(e), e["n"])):
            q = self._full(ev)
            if q is None:
                self.queue.append(ev)
                if self.r17 is not None:
                    ev["granted"] = self.now                       # (records only: the round it entered the FIFO)
                if ev.get("stalled"):
                    self.stats["delay_rounds:" + ev["stalled"]] += self.now - ev["round"]
                continue
            if not ev.get("stalled"):
                ev["stalled"] = q
                self.stats["delayed:" + q] += 1
                self.stats["stalled:" + ev["kind"]] += 1
            keep.append(ev)
        self.stalled = sorted(keep, key=lambda e: e["n"])
        self.peak["queue"] = max(self.peak["queue"], len(self.queue))
        self.peak["stalled"] = max(self.peak["stalled"], len(self.stalled))
        if self.r17 is not None:
            self._r17_occ()                                        # (records only: the FIFO after its entries)

    def _r17_occ(self):
        """(---records): the queues at this point of the round, for each one's most a round
        (`R17Records.occ`); reads only. Events enter the FIFO only in `_admit` and wait outside it only from `submit`
        (the deferral phase's) or `_admit` (a stalled producer's), and the dispatch queues a guard check only in
        `_guard_check`, so these points and the round's start see every round's most."""
        q, now = self.queue, self.now
        if now is None:
            return                                         # (before the first round: nothing queued yet)
        self.r17.occ(now, dict(event_queue=len(q), event_waiting=len(self.stalled),
                               splice_queue=sum(1 for e in q if e["kind"] == SPLICE),
                               hbm_tags=sum(1 for e in q if e.get("not_before", -1) > now),
                               guard_check_queue=len(self.check_q)))

    def submit(self, kind: str, cb, cells: dict | None = None, lazy=False, rel=False, engine=0, fetch=False, owner=None,
               plain=False, store_bound=False):
        """One reservation-manager event of `kind`, built from the file's operation list: MASK operations take their
        cell set from `cells` by name (guard, script_cells, own_cells, workspace, old_guard, new_guard, patch_cells,
        none); LAZY and REL run only where the frame has such a reservation; `engine` adds the splice engine's cycles
        after the operations; `fetch` makes it wait a round for an HBM fetch.  cb() runs when it completes.  During the
        deferral phase (`batching`) the event waits for `serve`; otherwise it runs now if it fits.  Returns the event
        (for `cancel_event`), or None when it completed at once."""
        cells = cells or {}
        ops = []
        for port, spec in self.events[kind]:
            parts = spec.split()
            op = parts[0]
            if op == "LAZY" and not lazy:
                continue
            if op == "REL" and not rel:
                continue
            if op == "MASK":
                ops.append((port, "MASK " + parts[1], tuple(cells.get(parts[2], ())) if parts[2] != "none" else ()))
            else:
                ops.append((port, op, None))
        if plain and kind == SPLICE:
            ops = ops[:4]                                  # a materialization's splice: no rebase, so the guard stays
        if engine:
            ops.append(("splice", "ENGINE", int(engine)))
        self.n_ev += 1
        ev = dict(kind=kind, ops=ops, i=0, t=0, cb=cb, round=self.now, n=self.n_ev, owner=owner, store_bound=bool(store_bound))
        if fetch:
            ev["not_before"] = self.now + self.fetch_rounds
            self.stats["store_fetches"] += 1
            self.stats["store_wait_rounds"] += self.fetch_rounds
        self.stats["events:" + kind] += 1
        self.stalled.append(ev)                            # (it enters the FIFO in spec 9's order: `_admit`)
        if self.batching:
            if self.r17 is not None:
                self._r17_occ()                            # (records only: it waits for `serve`)
            return ev
        self._admit()
        if ev.get("stalled"):
            return ev                                      # (the producer stalls)
        if self._run(ev):
            self.queue.remove(ev)
            if self.r17 is not None:
                self.r17.ev_done(ev, self.now)                     # (records only)
            cb()
            return None
        self.stats["spilled:" + kind] += 1
        ev["spilled"] = True
        return ev

    def serve(self, upto=None) -> set:
        """Admit the waiting events and serve the FIFO in the queue order (each port's priorities, then submission), with
        head-of-line blocking; run the callbacks of the events that complete, then admit and serve what they submit and
        what their room lets in, in turn.  upto: only the events before that priority (as built, before the guard
        checks).  Returns the owners whose events completed."""
        owners = set()
        while True:
            self._admit()
            done = []
            for ev in sorted(self.queue, key=lambda e: (self._prio(e), e["n"])):
                if upto is not None and self._prio(ev) >= upto:
                    break
                if ev.get("tried") != self.now:
                    ev["tried"], ev["t"] = self.now, 0         # (a round's first attempt starts at cycle 0)
                if self._run(ev):
                    done.append(ev)
                elif ev["round"] == self.now and not ev.get("spilled"):
                    ev["spilled"] = True
                    self.stats["spilled:" + ev["kind"]] += 1
            if not done:
                break
            for ev in done:
                self.queue.remove(ev)
                if ev["round"] != self.now:
                    self.stats["delayed_events:" + ev["kind"]] += 1
                    self.stats["delayed_rounds:" + ev["kind"]] += self.now - ev["round"]
                if ev["owner"] is not None:
                    owners.add(ev["owner"])
                if self.r17 is not None:
                    self.r17.ev_done(ev, self.now)                 # (records only)
                ev["cb"]()
        return owners

    def cancel(self, owner, kind=None):
        """Drop the queued (and stalled) events of an owner, of one kind or all: a frame that ended or moved on."""
        for lst in (self.queue, self.stalled):
            keep = [ev for ev in lst if not (ev.get("owner") == owner and (kind is None or ev["kind"] == kind))]
            self.stats["cancelled"] += len(lst) - len(keep)
            if self.r17 is not None:
                for ev in lst:
                    if ev.get("owner") == owner and (kind is None or ev["kind"] == kind):
                        self.r17.ev_cancel(ev)                     # (records only)
            lst[:] = keep

    def cancel_event(self, ev):
        for lst in (self.queue, self.stalled):
            if ev in lst:
                lst.remove(ev)
                self.stats["cancelled"] += 1
                if self.r17 is not None:
                    self.r17.ev_cancel(ev)                         # (records only)

    def splice_engine(self, low, rebase: bool) -> tuple:
        """(the splice engine's cycles for one script, whether the store's words bound them): 10 to its first instruction,
        then one a dependency edge (each instruction's patches: one edge each, to the previous user of the patch), or the
        store's words at 4 a cycle if longer; a rebase adds 50."""
        if low is None or not getattr(low, "instrs", None):
            return self.splice_first + (self.rebase_cycles if rebase else 0), False
        edges = sum(len(x.patches) for x in low.instrs)
        words = WORDS_PER_INSTR * len(low.instrs)
        w = int(math.ceil(words / float(self.words_per_cycle)))
        if w > edges:
            self.stats["store_words_bound"] += 1
        return self.splice_first + max(edges, w) + (self.rebase_cycles if rebase else 0), w > edges

    def guard_check(self, idx: int, cells, aged=False) -> bool:
        """`_guard_check`; (---records): its first ask and its service recorded, nothing else."""
        ok = self._guard_check(idx, cells, aged)
        if self.r17 is not None:
            self.r17.check(idx, self.now, ok)
            if self.check_q:
                self._r17_occ()                            # (as built, the guard checks queued for the next round)
        return ok

    def _guard_check(self, idx: int, cells, aged=False) -> bool:
        """The dispatcher's guard check of an instruction about to start on a guarded cell (once a round each): the
        status port (improved: one check a cycle, CHECK cycles each, in the same round), or the operation port (as
        built: MASK A, MASK B none, QUERY), where the dispatch queues it for the next round's service (`_serve_checks`).
        False: not served this round, the instruction waits. `aged` (r9-guard-close 4): an aged request's turn, or its own
        stored answer's collection, which reservation for aged guard checks (`ck_resv`) does not hold back."""
        if self.checked.get(idx) == self.now:
            return True
        if self.setting == "as_built":
            if idx not in self.check_pending and self._check_now(cells):
                self.checked[idx] = self.now
                self.stats["guard_checks"] += 1
                return True
            if idx not in self.check_pending:
                self.check_pending.add(idx)
                self.check_q.append((idx, tuple(cells), self.now))
                self.stats["guard_checks_queued"] += 1
            return False
        if self.setting == "improved":
            if self.zl:
                self.checked[idx] = self.now
                self.stats["guard_checks"] += 1
                return True
            room = self.checks + self.cyc["CHECK"] + self.gsvc + self.ck_f
            if room <= self.R and self.ck_resv and idx not in self.ck_resv and room + len(self.ck_resv) * (1 + self.gsvc + self.ck_f) > self.R:
                if aged:
                    self.stats["r9_aged_resv_override"] += 1
                else:
                    self.stats["r8_resv_spill"] += 1       # (the room is the aged checks'; this younger one waits)
                    room = self.R + 1
            if room > self.R:
                self.stats["hold_check_spill"] += 1
                if self.age is not None:
                    self.ck_spill.setdefault(idx, [self.now, self.now])[1] = self.now
                return False
            self.checks += 1 + self.gsvc + self.ck_f
            self.checked[idx] = self.now
            self.stats["guard_checks"] += 1
            if self.ck_spill.pop(idx, None) is not None and idx in self.ck_resv:
                self.ck_resv.discard(idx)
                self.stats["r8_aged_checks"] += 1          # (an aged check served in its reserved room)
            return True
        raise AssertionError(self.setting)

    def _ck_age(self, now: int):
        """Rule (a)'s fairness on the status port (improved setting): a guard check that first spilled HW_AGE or more
        rounds ago and spilled again within the last HW_AGE rounds is aged. The oldest aged checks, as many as one round
        can serve, have the port's room reserved for the round: a younger check that would take that room waits
        (`r8_resv_spill`), and a reserved instruction that reaches its check is served (`r8_aged_checks`). So an
        instruction whose check keeps spilling while it comes to its check at least once every HW_AGE rounds is served in
        finitely many rounds; one that stops coming for HW_AGE rounds drops out and ages again from its next spill."""
        for idx in [i for i, (f, l) in self.ck_spill.items() if now - l > self.age]:
            del self.ck_spill[idx]
        aged = sorted((f, i) for i, (f, l) in self.ck_spill.items() if now - f >= self.age)
        k = max(0, (self.R - self.cyc["CHECK"] - self.gsvc - self.ck_f) // (1 + self.gsvc + self.ck_f) + 1)
        self.ck_resv = {i for _f, i in aged[:k]}

    def gsvc_try(self, aged=False) -> bool:
        """Round 8's guard-service setting: a frame's guard check (FORWARD, and rule (a)'s registration and revision checks)
        takes `gsvc` cycles of the check port -- improved, the status port's cycles; as built, the operation port's, ahead
        of the events served there. False when the port has no room left this round: the check, and the step it guards,
        wait for the next round (a registration waits as Wait-for-Decoder, a FORWARD or a revision is refused this round;
        a settlement needs no check). Always True at 0, the default. improved, it leaves
        the room reserved for the aged guard checks (`_ck_age`), so their service does not depend on gsvc. `aged`
        (r9-guard-close 4): an aged request's turn, which that reservation does not hold back."""
        s = self.gsvc
        if not s:
            return True
        if self.setting == "improved":
            resv = ((len(self.ck_resv) - 1) * (1 + self.gsvc + self.ck_f) + self.cyc["CHECK"] + self.gsvc + self.ck_f
                    if self.ck_resv else 0)
            if aged and resv:
                if self.checks + s + resv > self.R and self.checks + s <= self.R:
                    self.stats["r9_aged_resv_override"] += 1
                resv = 0
            if self.checks + s + resv > self.R:                            # (the room the last reserved check needs, as
                                                                           #  `guard_check` keeps it from a younger check)
                self.stats["gsvc_spill"] += 1
                if resv and self.checks + s <= self.R:
                    self.stats["r8_gsvc_resv"] += 1                        # (the room was the aged checks')
                return False
            self.checks += s
        else:
            if "op" in self.blocked or self.free.get("op", 0) + s > self.R:
                self.stats["gsvc_spill"] += 1
                return False
            self.free["op"] = self.free.get("op", 0) + s
        self.stats["gsvc_cycles"] += s
        return True

    def r9_prefill(self):
        """at a round's start the event in service takes the check port first, up to the round's cycles."""
        take = min(self.R, self.r9_debt)
        self.checks, self.r9_debt = take, self.r9_debt - take
        self.stats["r9_busy_rounds"] += 1

    def r9_take(self, cyc: int) -> int:
        """Round 9's bounded check (froq/r9check.py): an event's c1 x accesses cycles on the check port, after its c0
        (`gsvc_try`, or the dispatcher's `guard_check`). 0 when they fit the round's room; else the port stays busy into
        the next rounds (`r9_debt`, served first at each round's start, so every other check waits behind it) and the
        event's answer applies in the round its service ends, the number of rounds returned. Improved only (the
        status port)."""
        assert self.setting == "improved", ("the bounded check's port model is the improved setting's", self.setting)
        self.stats["r9_cycles"] += int(cyc)
        room = max(0, self.R - self.checks)
        if cyc <= room:
            self.checks += cyc
            return 0
        self.checks = self.R
        self.r9_debt = int(cyc) - room
        self.stats["r9_multi_round"] += 1
        return -(-self.r9_debt // self.R)

    def ev_take(self, cyc: int):
        """an event of more than one cycle. Port 1 waits its cycles past the first, from the
        round's room, the rest owed into the next rounds (`r9_debt`, served first at each round's start; a grant waits
        while any is owed). Improved only."""
        assert self.setting == "improved", ("port 1's model is the improved setting's", self.setting)
        self.stats["ev_wait_cycles"] += int(cyc)
        room = max(0, self.R - self.checks)
        if cyc <= room:
            self.checks += cyc
            return
        self.checks = self.R
        self.r9_debt += int(cyc) - room
        self.stats["ev_multi_round"] += 1

    def assert_fits(self):
        """Rule (a)'s A6 premise: every single operation fits in what its port has after the tick (a MASK at its largest:
        2 cycles a word of the layout's cell ids), so the event first in its port's order always progresses in a round."""
        words = -(-max(1, len(self.ids)) // 64)
        for port, ops in self.ports_ops().items():
            room = self.R - (self.cyc["TICK"] if port == self.tick_port else 0)
            for op in ops:
                if op == "TICK":
                    continue
                c = 2 * words if op == "MASKW" else self.cyc.get(op, 0)
                assert c + (self.gsvc if port == self.check_port else 0) <= room, ("A6: an operation exceeds its port's room", port, op, c, room)
            assert 2 * words <= room, ("A6: a MASK exceeds its port's room", port, words, room)

    def ports_ops(self) -> dict:
        return {p: list(ops) for p, ops in self.p["resv"][self.setting]["ports"].items()}

    def _check_now(self, cells) -> bool:
        """As built, at the dispatch: the check in its own round if it fits in the operation port's remaining cycles by
        spec 9's costs.  Work of a lower priority that could not be served (a splice, decide, commit or registration)
        does not hold it back, as it comes first in the queue order; a tick, an end or an earlier check that did not fit
        does (and then every later check of the round queues, in order)."""
        if self.check_q:
            return False                                   # (an earlier check waits: none overtakes it)
        bp = self.block_prio.get("op")
        if "op" in self.blocked and (bp is None or bp <= self.guard_prio):
            return False
        ev = dict(kind="dispatcher_guard_check", ops=[("op", "MASK A", tuple(cells)), ("op", "MASK B", ()), ("op", "QUERY", None)]
                  + ([("op", "GSVC", None)] if self.gsvc else []), i=0, t=0, cb=None, round=self.now, n=0)
        snap = (dict(self.free), {k: dict(v) for k, v in self.masks.items()}, set(self.blocked), dict(self.block_prio))
        self.blocked.discard("op")
        if self._run(ev):
            self.blocked, self.block_prio = snap[2], snap[3]
            return True
        self.free, self.masks, self.blocked, self.block_prio = snap
        self.stats["hold_check_spill"] += 1
        return False

    def _serve_checks(self):
        """As built, at a round's start after the tick and the carried ends: the guard checks the dispatch queued, in
        order, each MASK A (its cells), MASK B none and QUERY on the operation port; one that does not fit blocks the
        port for the round (the rest wait, in order).  A served check holds for the instruction in this round."""
        keep = []
        for idx, cells, r0 in self.check_q:
            if keep or "op" in self.blocked:
                keep.append((idx, cells, r0))
                continue
            ev = dict(kind="dispatcher_guard_check", ops=[("op", "MASK A", cells), ("op", "MASK B", ()), ("op", "QUERY", None)]
                      + ([("op", "GSVC", None)] if self.gsvc else []), i=0, t=0, cb=None, round=r0, n=0)
            snap = (dict(self.free), {k: dict(v) for k, v in self.masks.items()})
            if self._run(ev):
                self.checked[idx] = self.now
                self.check_pending.discard(idx)
                self.stats["guard_checks"] += 1
                self.stats["guard_check_wait_rounds"] += self.now - r0
            else:
                self.free, self.masks = snap
                self.blocked.add("op")
                self.stats["hold_check_spill"] += 1
                keep.append((idx, cells, r0))
        self.check_q = keep

    # -- the reservation slots (RM-D), held per reservation
    def resv_slot_free(self) -> bool:
        return len(self.slots) < self.cap_slots

    def take_slot(self, key) -> bool:
        if key in self.slots:
            return True
        if len(self.slots) >= self.cap_slots:
            self.stats["resv_full"] += 1
            return False
        self.slots.add(key)
        self.peak["resv_slots"] = max(self.peak["resv_slots"], len(self.slots))
        return True

    def free_slot(self, key):
        self.slots.discard(key)

    def summary(self) -> dict:
        return dict(setting=self.setting, sha256=self.sha, round_cycles=(self.R if self.slow_s is None else int(self.p["round_cycles"])),
                    b_res=(None if self.b_res == math.inf else self.b_res),
                    layout_cells=getattr(self, "layout_cells", None), queued_at_end=len(self.queue), stalled_at_end=len(self.stalled),
                    slots_at_end=len(self.slots), lookup_latency_cycles=self.lookup_latency,
                    counters={k: int(v) for k, v in sorted(self.stats.items()) if v},
                    peaks={k: int(v) for k, v in sorted(self.peak.items())},
                    **({"ctrl_slow": self.slow_rec} if self.slow_s is not None else {}))   # (s and what it scaled)
