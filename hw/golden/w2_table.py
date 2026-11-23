"""the decision's policy-table block, its golden model: the hardware contract of `hw/rtl/decision_unit.sv`,
written from the design spec and
 section 1.

The table: N_K slots x C c-states x L load bins x E age bins = 9,216 rows of P lanes (boundaries 0 to 16), 4 bits a
lane, row r = 8(18k + 3c + l) + j. A lane's bits [2:0] are the slot's β = 0 half (its walk key) and bit 3 its β = 1 half
(its rebased key):
- the β = 0 codes, the preference order of ADVANCE (A), WAIT (W) and COMMIT (C): 0 AWC, 1 ACW, 2 WAC, 3 WCA, 4 CAW,
  5 CWA, 6 AWC, 7 ACW. When the block is a jump, ADVANCE stays eligible under 0 and 1 only (spec 6.3);
- the β = 1 bit: 0 AW, 1 WA. A jump is prohibited.
Each half of a slot has its own 7 age-bin edges (ascending; the last is H) and its fence F (spec 6.2).

A value (N_V of them) holds its slot k, its c-state c (a prediction's c applies from the next round's start), its age e
(the rounds since its producer ended, set at registration and one up at each round's start, saturating at 1,023, so it
never wraps however long the value lives), β, its mask (the boundaries it can stand) and its jump vector: lane a's block is a jump if a lane of the gap mask (the statically feasible boundaries the
mask lacks) lies after a and before the next lane of the mask. Both are written at registration and at the rebase. A
global load bin is set at each round's start.

A decision (spec 1.3 and 8) is one lookup, at the value's row (k, c, l, the age bin of e under β's edges). The scan
starts at the cut and passes the lanes outside the mask. At each lane a of the mask, nxt(a) the next lane of the mask:
- ADVANCE is eligible if nxt(a) <= min(adm, cap, F) (D's admissible run, the frame's cap, the fence); if the block is a
  jump, only under codes 0 and 1 on β = 0, and never on β = 1;
- COMMIT only at p, the first lane of the mask at or after the cut, on β = 0, if the scheduler's checks pass (cok);
- WAIT always.
The lane's action is the first eligible one in its order. The scan stops at the first lane whose action is not ADVANCE:
r, and its action, WAIT or COMMIT. r0 is the lane it stops at with adm taken as 31 (D's admission conditions held
everywhere). jr: the action at r would be ADVANCE if the jump were allowed (a removed or prohibited jump). err: the value
is not live, or no lane of its mask lies at or after the cut.

Operations act in the order given; a lookup sees every control operation before it."""
N_V, N_K, C, L, E, P = 64, 64, 6, 3, 8, 17
ROWS = N_K * C * L * E
ORDERS = {0: "AWC", 1: "ACW", 2: "WAC", 3: "WCA", 4: "CAW", 5: "CWA", 6: "AWC", 7: "ACW"}
ACT = {"W": 0, "C": 1}                               # result codes; 3: error
CTL = ("ROW", "SLOT", "REG", "REB", "PRED", "END", "RND")


def jumps(mask, gap):
    """Lane a's block is a jump: a gap lane g > a with no lane of the mask in (a, g]."""
    out = 0
    for a in range(P):
        for g in range(a + 1, P):
            if (mask >> g) & 1:
                break
            if (gap >> g) & 1:
                out |= 1 << a
                break
    return out


def lane_act(f, beta, x, jm, cm):
    """The first eligible action of lane field f: x ADVANCE's limit test, jm a jump, cm COMMIT's checks."""
    if beta == 0:
        order, a_ok = ORDERS[f & 7], x and (not jm or (f & 7) in (0, 1))
    else:
        order, a_ok, cm = ("AW" if not (f >> 3) & 1 else "WA"), x and not jm, False
    for ch in order:
        if ch == "A" and a_ok:
            return "A"
        if ch == "W":
            return "W"
        if ch == "C" and cm:
            return "C"
    return "W"


class Table:
    def __init__(self):
        self.rows = [0] * ROWS
        self.slot = [[([0] * 7, 0) for _ in range(2)] for _ in range(N_K)]     # (edges, fence) per half
        self.v = [None] * N_V
        self.lb = 0

    # -- control
    def row(self, r, data):
        self.rows[r] = data & ((1 << (4 * P)) - 1)

    def slotw(self, k, half, edges, fence):
        self.slot[k][half] = (list(edges), fence)

    def reg(self, v, k, c, e0, mask, gap):
        self.v[v] = dict(k=k, c=c, cp=None, e=min(e0, 1023), beta=0, mask=mask, jmp=jumps(mask, gap))

    def reb(self, v, k, mask, gap):
        s = self.v[v]
        if s is not None:
            s.update(k=k, beta=1, mask=mask, jmp=jumps(mask, gap))

    def pred(self, v, c):
        if self.v[v] is not None:
            self.v[v]["cp"] = c

    def end(self, v):
        self.v[v] = None

    def rnd(self, lb):
        self.lb = lb
        for s in self.v:
            if s is not None:
                s["e"] = min(s["e"] + 1, 1023)
                if s["cp"] is not None:
                    s["c"], s["cp"] = s["cp"], None

    def do(self, op, **kw):
        return dict(ROW=lambda: self.row(kw["row"], kw["data"]), SLOT=lambda: self.slotw(kw["k"], kw["half"], kw["edges"], kw["fence"]),
                    REG=lambda: self.reg(kw["v"], kw["k"], kw["c"], kw["e0"], kw["mask"], kw["gap"]),
                    REB=lambda: self.reb(kw["v"], kw["k"], kw["mask"], kw["gap"]), PRED=lambda: self.pred(kw["v"], kw["c"]),
                    END=lambda: self.end(kw["v"]), RND=lambda: self.rnd(kw["lb"]))[op]()

    # -- a decision
    def row_of(self, s):
        edges, _ = self.slot[s["k"]][s["beta"]]
        j = sum(1 for x in edges if x <= s["e"])
        return 8 * (18 * s["k"] + 3 * s["c"] + self.lb) + j

    def lookup(self, v, cut, adm, cap, cok):
        s = self.v[v]
        lanes = [] if s is None else [a for a in range(P) if (s["mask"] >> a) & 1 and a >= cut]
        if not lanes:
            return dict(r=0, act=3, ro=0, jr=0)
        beta, row = s["beta"], self.rows[self.row_of(s)]
        fence = self.slot[s["k"]][beta][1]
        nxt = {a: next((b for b in range(a + 1, P) if (s["mask"] >> b) & 1), None) for a in lanes}
        p = lanes[0]

        def scan(lim):
            for a in lanes:
                f, x, jm = (row >> (4 * a)) & 15, nxt[a] is not None and nxt[a] <= lim, bool((s["jmp"] >> a) & 1)
                cm = bool(cok) and beta == 0 and a == p
                ac = lane_act(f, beta, x, jm, cm)
                if ac != "A":
                    return a, ac, lane_act(f, beta, x, False, cm) == "A"
            return lanes[-1], "W", False                       # (not reached: the last lane cannot advance)

        r, ac, jr = scan(min(adm, cap, fence))
        ro = scan(min(cap, fence))[0]
        return dict(r=r, act=ACT[ac], ro=ro, jr=int(jr))

    def state(self):
        """The live values, for the testbench's dumps: (v, k, c, pending c or -1, e, β, mask, jumps), and the load bin."""
        vals = [(i, s["k"], s["c"], -1 if s["cp"] is None else s["cp"], s["e"], s["beta"], s["mask"], s["jmp"])
                for i, s in enumerate(self.v) if s is not None]
        return vals, self.lb
