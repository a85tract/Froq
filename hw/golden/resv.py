"""The reservation manager's golden model: the scheduler's two reservation stores of
F_4's simulator (`froq/device.py`), in the form `hw/rtl/reservation_manager.sv` implements. It is the hardware contract
the RTL is checked against, cycle-free: every operation acts in the order given.

RM-D, the decoder-core reservations (rules 41c, 44a and 50c), N_R slots, each addressed by the slot id its reserve
returned (F_4's reservation id `rid`):
- LAZY(lam): a lazy frame's materialization demand, counted from now (`_u4_lazy_reserve`).
- ACT(demand, held, S, R): an ACT decided now reserves max(0, demand - held) cores, counted from now + act_lat (its
  effect round) to its end, now + act_lat + S + R; past its end it expires at a tick (`_u_reserve`).
- REL(slot): the reservation ends (`_u_release`): it leaves the totals.
- STARTED(slot): a decode window of the ACT's script started; one core of its demand is busy from now on (`_u6_started`).
- HELD(slot): a lazy reservation's cores, at most 255 (`_u_held`, the EPOCH request's 8-bit field).
- TICK: the next round (`_u_resv_tick`): reservations past their end expire, ACTs whose effect round has come are
  counted.
- the totals, kept current: `total`, the cores the pool counts now (`_u_resv_total`); `unstarted`, rule 50c's cores
  counted and not yet started, the sum of max(0, cores - started) (`_u_unstarted`).
A full RM-D refuses a reserve (ok = 0); the scheduler then reserves nothing, as for a frame it does not defer.

RM-W, the workspace reservations, N_C cells, each with three saturating counters:
- ws, the live frames whose candidate workspace (guard) holds the cell (`_defer_ws_cells`);
- tgt, the decided, not yet spliced materializations whose workspace holds it (`_defer_target_cells`);
- lock, the spliced scripts still running on it (`_defer_locked`, kept as a count: a query names its own cells).
Operands are two cell masks, A (the cells operated) and B (the asking frame's own cells, excluded from a query):
- MASKW(word, sel, bits): 64 bits of mask A (sel 0) or B (sel 1);
- WS_ADD / WS_SUB, TGT_ADD / TGT_SUB, LOCK_ADD / LOCK_SUB: every cell of A one up or down (a count saturates at its
  maximum and stops at 0, as F_4's counts drop to nothing);
- QUERY: whether any cell of A is held by another frame -- ws, tgt or lock greater than the cell's own share in B
  (`_defer_hold`'s guards, `_u_cells_free`, `_h_prepare_core`'s pin check).

Round 5's changes:
- Two ports. The slot operations (RM-D, LAZY to TOTALS: SLOT_OPS) and the cell operations (RM-W, MASKW to GUARD_SUB)
  each have their own port. The two stores share no state, so each port's stream is checked on its own, in its order.
- The frames' guards by frame number, N_G of them (`_defer_ws_cells`, kept so that a frame's end and its rebase need no
  mask load):
  - GUARD_ADD(frame): mask A becomes the frame's guard, and every cell of A moves one up in ws (saturating);
  - GUARD_SUB(frame): every cell of the frame's guard moves one down in ws (stopping at 0), and the guard is cleared.
- CHECK(cells), the status port: for each of up to four cells (None: not asked), bit 0 ws > 0, bit 1 tgt > 0 and bit 2
  lock > 0 (`_defer_hold`'s per-cell tests). A check sees every cell operation before it."""
N_R, N_C, N_G = 64, 512, 64
CW, RW, TW = 10, 16, 16          # cores, rounds, totals (bits)
WS_MAX, TGT_MAX, LOCK_MAX = 127, 15, 7
OPS = ("LAZY", "ACT", "REL", "STARTED", "HELD", "TICK", "TOTALS", "MASKW", "WS_ADD", "WS_SUB", "TGT_ADD", "TGT_SUB",
       "LOCK_ADD", "LOCK_SUB", "QUERY", "GUARD_ADD", "GUARD_SUB")
OP = {n: i for i, n in enumerate(OPS)}
SLOT_OPS = frozenset(OPS[:7])                      # the slot port's; the others are the cell port's


class Resv:
    def __init__(self, act_lat=7):
        self.L = act_lat
        self.now = 0                                   # 16-bit round counter (wrapping); lifetimes stay below 2^15
        self.slots = [None] * N_R
        self.total = self.unstarted = 0
        self.ws, self.tgt, self.lock = [0] * N_C, [0] * N_C, [0] * N_C
        self.mask = [[0] * N_C, [0] * N_C]
        self.guard = [0] * N_G                        # each frame's guard, a bit per cell

    # -- RM-D
    @staticmethod
    def _ge(a, b):
        """a >= b on the wrapping 16-bit round counter."""
        return ((a - b) & 0xFFFF) < 0x8000

    def _count(self, s):
        return s["cores"] if s["counting"] else 0

    def _alloc(self, s):
        for i in range(N_R):
            if self.slots[i] is None:
                self.slots[i] = s
                return 1, i
        return 0, 0

    def lazy(self, lam):
        assert 0 <= lam < (1 << CW)
        ok, i = self._alloc(dict(kind="lazy", cores=lam, frm=self.now, end=None, started=0, counting=True))
        if ok:
            self.total += lam
            self.unstarted += lam
        return dict(ok=ok, slot=i)

    def act(self, demand, held, S, R):
        assert 0 <= demand < (1 << CW) and 0 <= held < (1 << CW) and S + R + self.L < 0x8000
        dem = max(0, demand - held)
        return dict(zip(("ok", "slot"), self._alloc(dict(kind="act", cores=dem, frm=(self.now + self.L) & 0xFFFF,
                                                           end=(self.now + self.L + S + R) & 0xFFFF, started=0, counting=False))))

    def rel(self, i):
        s = self.slots[i]
        if s is not None:
            if s["counting"]:
                self.total -= s["cores"]
                self.unstarted -= max(0, s["cores"] - s["started"])
            self.slots[i] = None
        return {}

    def started(self, i):
        s = self.slots[i]
        if s is not None and s["kind"] == "act" and s["started"] < (1 << CW) - 1:
            if s["counting"] and s["cores"] > s["started"]:
                self.unstarted -= 1
            s["started"] += 1
        return {}

    def held(self, i):
        s = self.slots[i]
        return dict(held=min(255, s["cores"]) if (s is not None and s["kind"] == "lazy") else 0)

    def tick(self):
        self.now = (self.now + 1) & 0xFFFF
        for i, s in enumerate(self.slots):
            if s is None:
                continue
            if s["end"] is not None and not self._ge(s["end"], self.now):           # end < now: expired
                self.rel(i)
            elif not s["counting"] and self._ge(self.now, s["frm"]):              # its effect round has come
                s["counting"] = True
                self.total += s["cores"]
                self.unstarted += max(0, s["cores"] - s["started"])
        return self.totals()

    def totals(self):
        return dict(total=self.total, unstarted=self.unstarted)

    # -- RM-W
    def maskw(self, word, sel, bits):
        for b in range(64):
            c = word * 64 + b
            if c < N_C:
                self.mask[sel][c] = (bits >> b) & 1
        return {}

    def _bump(self, arr, mx, up):
        a = self.mask[0]
        for c in range(N_C):
            if a[c]:
                arr[c] = min(mx, arr[c] + 1) if up else max(0, arr[c] - 1)
        return {}

    def query(self):
        a, b = self.mask
        other = lambda arr: int(any(a[c] and arr[c] > b[c] for c in range(N_C)))
        return dict(q_ws=other(self.ws), q_tgt=other(self.tgt), q_lock=other(self.lock))

    def guard_add(self, f):
        a = self.mask[0]
        self.guard[f] = sum(1 << c for c in range(N_C) if a[c])
        return self._bump(self.ws, WS_MAX, True)

    def guard_sub(self, f):
        g = self.guard[f]
        for c in range(N_C):
            if (g >> c) & 1:
                self.ws[c] = max(0, self.ws[c] - 1)
        self.guard[f] = 0
        return {}

    def check(self, cells):
        """The status port: per cell asked, bit 0 ws > 0, bit 1 tgt > 0, bit 2 lock > 0; 0 for a cell not asked."""
        return dict(held=[0 if c is None else int(self.ws[c] > 0) | int(self.tgt[c] > 0) << 1 | int(self.lock[c] > 0) << 2
                          for c in cells])

    def do(self, op, **kw):
        """One operation by name; returns its results (a dict)."""
        f = dict(LAZY=lambda: self.lazy(kw["cores"]), ACT=lambda: self.act(kw["cores"], kw["held"], kw["s"], kw["r"]),
                 REL=lambda: self.rel(kw["slot"]), STARTED=lambda: self.started(kw["slot"]), HELD=lambda: self.held(kw["slot"]),
                 TICK=self.tick, TOTALS=self.totals, MASKW=lambda: self.maskw(kw["word"], kw["sel"], kw["data"]),
                 WS_ADD=lambda: self._bump(self.ws, WS_MAX, True), WS_SUB=lambda: self._bump(self.ws, WS_MAX, False),
                 TGT_ADD=lambda: self._bump(self.tgt, TGT_MAX, True), TGT_SUB=lambda: self._bump(self.tgt, TGT_MAX, False),
                 LOCK_ADD=lambda: self._bump(self.lock, LOCK_MAX, True), LOCK_SUB=lambda: self._bump(self.lock, LOCK_MAX, False),
                 QUERY=self.query, GUARD_ADD=lambda: self.guard_add(kw["frame"]), GUARD_SUB=lambda: self.guard_sub(kw["frame"]),
                 CHECK=lambda: self.check(kw["cells"]))[op]
        return f()

    def state(self):
        """Every slot, cell and guard, for the testbench's dumps: slots as (valid, kind, cores, frm, end or -1, started,
        counting); guards as {frame: bits} for the frames whose guard is not empty."""
        sl = [(0, 0, 0, 0, -1, 0, 0) if s is None else
              (1, int(s["kind"] == "act"), s["cores"], s["frm"], -1 if s["end"] is None else s["end"], s["started"], int(s["counting"]))
              for s in self.slots]
        return dict(slots=sl, ws=list(self.ws), tgt=list(self.tgt), lock=list(self.lock), total=self.total, unstarted=self.unstarted,
                    now=self.now, guards={f: g for f, g in enumerate(self.guard) if g})
