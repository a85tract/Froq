"""The policy-table block's tests: directed scenarios and random streams, the RTL
(`hw/rtl/decision_unit.sv` under `hw/tb/tb_decision_unit.sv`, Verilator) against the golden model (`hw/golden/w2_table.py`).

Each case checks every lookup's result (r, its action, r0, the jump flag), each port's in its order, and dumps of the
live values; it records the cycles from a lookup's presentation to its result, and that a burst's lookups were taken
one a cycle on each port.

   python3 -m tests.run_decision_unit [directed | random | all] [n_random] [items_each] [seed] [OUT.json] (from hw/)"""
import collections, json, os, random, sys
HW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HW)
from golden import w2_table as G
from tests import vlt

OPC = {n: i for i, n in enumerate(G.CTL)}
A_KEYS = ("port", "val", "r", "act", "ro", "jr", "cycles", "taken")


def edges_hex(edges):
    return sum((x & 1023) << (10 * i) for i, x in enumerate(edges))


def lanes_word(codes):
    """A row from 17 lane fields."""
    return sum((f & 15) << (4 * a) for a, f in enumerate(codes))


class Case:
    def __init__(self, name):
        self.name, self.items = name, []

    def ctl(self, op, **kw):
        self.items.append(("C", op, kw))

    def look(self, port, v, cut, adm, cap, cok):
        self.items.append(("Q", port, v, cut, adm, cap, cok))

    def burst(self, qs):
        self.items.append(("B", len(qs)))
        for q in qs:
            self.look(*q)

    def same(self, op, kw, q):
        """A control operation and a lookup presented in one cycle."""
        self.items.append(("S",))
        self.ctl(op, **kw)
        self.look(*q)

    def dump(self):
        self.items.append(("D",))

    def vec(self):
        L = []
        for it in self.items:
            if it[0] == "C":
                _, op, k = it
                L.append("C %d %d %x %d %d %x %d %d %d %d %d %d %d" % (OPC[op], k.get("row", 0), k.get("data", 0), k.get("k", 0), k.get("half", 0),
                                                                     edges_hex(k.get("edges", [0] * 7)), k.get("fence", 0), k.get("v", 0),
                                                                     k.get("c", 0), k.get("e0", 0), k.get("mask", 0), k.get("gap", 0),
                                                                     k.get("lb", 0)))
            elif it[0] == "Q":
                L.append("Q %d %d %d %d %d %d" % it[1:])
            elif it[0] == "B":
                L.append("B %d" % it[1])
            else:
                L.append(it[0])
        return "\n".join(L + ["X"]) + "\n"

    def golden(self):
        t = G.Table()
        res, dumps = [], []
        for it in self.items:
            if it[0] == "C":
                t.do(it[1], **it[2])
            elif it[0] == "Q":
                _, port, v, cut, adm, cap, cok = it
                res.append((port, v, t.lookup(v, cut, adm, cap, cok)))
            elif it[0] == "D":
                vals, lb = t.state()
                dumps.append(["V %d %d %d %d %d %d %d %d" % x for x in vals] + ["T %d" % lb])
        return res, dumps


def parse(path):
    res, dumps, cur, end = [], [], [], None
    for l in open(path):
        p = l.split()
        if not p:
            continue
        if p[0] == "A":
            res.append(dict(zip(A_KEYS, map(int, p[1:]))))
        elif p[0] == "V":
            cur.append(l.strip())
        elif p[0] == "T":
            dumps.append(cur + [l.strip()]); cur = []
        elif p[0] == "E":
            end = tuple(map(int, p[1:]))
    return res, dumps, end


def run_case(case, exe):
    wdir = vlt.workdir("w2t")
    vec, out = os.path.join(wdir, "vec_%s.txt" % case.name), os.path.join(wdir, "out_%s.txt" % case.name)
    open(vec, "w").write(case.vec())
    vlt.run(exe, {"VEC": vec, "OUT": out}, timeout=3600)
    got, gdumps, end = parse(out)
    want, wdumps = case.golden()
    fails, cyc = [], collections.Counter()
    for port in (0, 1):
        g_ = [r for r in got if r["port"] == port]
        w_ = [(v, w) for p, v, w in want if p == port]
        if len(g_) != len(w_):
            fails.append("port %d: %d results, %d expected" % (port, len(g_), len(w_)))
        for i, (r, (v, w)) in enumerate(zip(g_, w_)):
            cyc[r["cycles"]] += 1
            bad = [f for f in ("r", "act", "ro", "jr") if r[f] != w[f]] + (["val"] if r["val"] != v else [])
            if bad:
                fails.append("port %d lookup %d (value %d): %s" % (port, i, v, ", ".join(
                    "%s rtl %d golden %d" % (f, r[f], w[f] if f != "val" else v) for f in bad)))
                if len(fails) > 6:
                    break
    if len(gdumps) != len(wdumps):
        fails.append("%d dumps, %d expected" % (len(gdumps), len(wdumps)))
    for i, (a, b) in enumerate(zip(gdumps, wdumps)):
        if a != b:
            fails.append("dump %d differs: %s" % (i, sorted(set(a) ^ set(b))[:6]))
            break
    # a burst's lookups: taken one a cycle on each port
    gaps = collections.Counter()
    for port in (0, 1):
        t = [r["taken"] for r in got if r["port"] == port]
        gaps.update(b - a for a, b in zip(t, t[1:]) if b - a <= 2)
    stat = dict(case=case.name, lookups=len(want), dumps=len(wdumps), cycles=end[1] if end else None,
                lookup_cycles=dict(sorted(cyc.items())), back_to_back=gaps.get(1, 0), fails=fails)
    print("%s: %d lookups, %d dumps, %d taken one cycle after the last -> %s" % (
        case.name, len(want), len(wdumps), gaps.get(1, 0), "PASS" if not fails else "FAIL: " + "; ".join(fails[:3])), flush=True)
    return stat


# ------------------------------------------------------------------------------------------------- helpers
ALL = (1 << G.P) - 1


def slot_all(c, k, edges=(4, 8, 16, 32, 64, 128, 256), fence=16, half=None):
    for h in ((0, 1) if half is None else (half,)):
        c.ctl("SLOT", k=k, half=h, edges=list(edges), fence=fence)


def row_index(k, cc, lb, j):
    return 8 * (18 * k + 3 * cc + lb) + j


# ------------------------------------------------------------------------------------------------- directed
def d_banks():
    """Every bank edge, both ports at once on the same row and on different banks: lane (row mod 17) is WAIT first,
    the others ADVANCE first, so r names the row read."""
    c = Case("d_banks")
    rows = sorted({b * 1024 + o for b in range(9) for o in (0, 1, 511, 1023)})
    for r in rows:
        c.ctl("ROW", row=r, data=lanes_word([2 if a == r % 17 else 0 for a in range(G.P)]))
    ks = sorted({r // 144 for r in rows})
    for k in ks:
        slot_all(c, k, edges=(1, 2, 3, 4, 5, 6, 7))             # age e gives bin min(e, 7)
    for i, r in enumerate(rows):
        k, rest = divmod(r, 144); cc, rest = divmod(rest, 24); lb, j = divmod(rest, 8)
        v = i % 64
        c.ctl("RND", lb=lb)
        c.ctl("REG", v=v, k=k, c=cc, e0=j, mask=ALL, gap=0)
        c.look(i % 2, v, 0, 16, 16, 0)
    vs = list(range(len(rows)))
    c.burst([(i % 2, i, 0, 16, 16, 0) for i in vs])                  # every bank, both ports at once
    c.burst([(p, i, 0, 16, 16, 0) for i in vs for p in (0, 1)])      # both ports on one row in one cycle
    c.dump()
    return c


def d_codes():
    """Every β = 0 code with ADVANCE in and out of its limit, with and without a jump, with and without COMMIT's checks,
    at the first lane and after it; both β = 1 orders with and without a jump."""
    c = Case("d_codes")
    slot_all(c, 1)
    # mask 0, 2, 4, 6, 8; gap 5 (so 4 -> 6 is a jump)
    mask, gap = 0b101010101, 0b000100000
    for code in range(8):
        for b3 in (0, 1):
            for lane in (0, 2, 4):
                k_row = [0] * G.P
                k_row[lane] = code | (b3 << 3)
                v = (code * 6 + b3 * 3 + lane // 2) % 64
                c.ctl("ROW", row=row_index(1, 0, 0, 0), data=lanes_word(k_row))
                c.ctl("REG", v=v, k=1, c=0, e0=0, mask=mask, gap=gap)
                for cut in (0, lane):
                    for adm in (16, lane, lane + 2):
                        for cok in (0, 1):
                            c.look(cok, v, cut, adm, 16, cok)
                c.ctl("REB", v=v, k=1, mask=mask, gap=gap)            # β = 1: bit 3 decides
                for cut in (0, lane):
                    for adm in (16, lane + 2):
                        c.look(0, v, cut, adm, 16, 1)
                c.ctl("END", v=v)
    c.dump()
    return c


def d_scan():
    """The scan's limits: cuts inside a block, at a lane and past the last; adm below cap (r below r0); the fence below
    cap; cap 0; a single-lane mask; a mask with several gaps."""
    c = Case("d_scan")
    slot_all(c, 2, fence=12)
    slot_all(c, 3, fence=16)
    c.ctl("ROW", row=row_index(2, 0, 0, 0), data=lanes_word([0] * G.P))
    c.ctl("ROW", row=row_index(3, 0, 0, 0), data=lanes_word([0 if a % 3 else 1 for a in range(G.P)]))
    cases = [(0b10010010010010011, 0b00001000000100000), (0b1, 0), (1 << 16, 0), (0b11111111111111111, 0),
             (0b10000000000000001, 0b01111111111111110), (0b00100100100100100, 0b01001001001001001)]
    for i, (mask, gap) in enumerate(cases):
        for k in (2, 3):
            v = 2 * i + k - 2
            c.ctl("REG", v=v, k=k, c=0, e0=0, mask=mask, gap=gap)
            for cut in (0, 1, 3, 7, 15, 16, 17, 31):
                for adm, cap in ((16, 16), (5, 16), (16, 9), (0, 0), (31, 31), (9, 5)):
                    c.look((cut + adm) % 2, v, cut, adm, cap, 1)
    c.dump()
    return c


def d_age():
    """Ages: edges with repeats, an age at each edge, saturation at 1,023; a prediction pending until the next round;
    the load bin; a rebase switching the slot and the half (its own edges and fence); and a value that lives 65,600
    rounds, whose age stays saturated (a 16-bit difference would have wrapped)."""
    c = Case("d_age")
    c.ctl("SLOT", k=5, half=0, edges=[3, 3, 10, 200, 500, 1000, 1020], fence=16)
    c.ctl("SLOT", k=6, half=1, edges=[1, 2, 4, 8, 16, 32, 64], fence=7)
    for cc in range(6):
        for lb in range(3):
            for j in range(8):
                c.ctl("ROW", row=row_index(5, cc, lb, j), data=lanes_word([2 if a == (cc * 3 + lb + j) % 17 else 0 for a in range(G.P)]))
                c.ctl("ROW", row=row_index(6, cc, lb, j), data=lanes_word([8 if a == (cc + 2 * lb + j) % 8 else 0 for a in range(G.P)]))
    for e in (0, 2, 3, 4, 9, 10, 199, 200, 999, 1000, 1019, 1020, 1022, 1023):
        c.ctl("REG", v=1, k=5, c=e % 6, e0=e, mask=ALL, gap=0)
        c.look(0, 1, 0, 16, 16, 0); c.look(1, 1, 3, 16, 16, 0)
    c.ctl("REG", v=2, k=5, c=0, e0=0, mask=ALL, gap=0)
    for n in range(40):
        if n % 5 == 0:
            c.ctl("PRED", v=2, c=(n // 5) % 6)
        c.look(0, 2, 0, 16, 16, 0)                                    # the prediction waits for the round
        c.ctl("RND", lb=n % 3)
        c.look(1, 2, 0, 16, 16, 0)
    c.ctl("REB", v=2, k=6, mask=ALL, gap=0)                           # the β = 1 half: its edges, fence 7, bit 3
    for n in range(70):
        c.look(n % 2, 2, n % 9, 16, 16, 1)
        c.ctl("RND", lb=(n * 7) % 3)
    c.dump()
    c.ctl("REG", v=3, k=5, c=1, e0=1018, mask=ALL, gap=0)               # a value that lives 65,600 rounds: its age
    for n in range(65600):                                            # saturates at 1,023 and stays there
        c.ctl("RND", lb=1)
        if n < 8 or n in (1022, 65534, 65535, 65536, 65537) or n % 8192 == 0:
            c.look(n % 2, 3, 0, 16, 16, 0)
    c.dump()
    return c


def d_same():
    """Control operations in the cycle of a lookup: the lookup sees them."""
    c = Case("d_same")
    slot_all(c, 7, edges=(2, 4, 6, 8, 10, 12, 14))
    for cc in range(6):
        for lb in range(3):
            for j in range(8):
                c.ctl("ROW", row=row_index(7, cc, lb, j), data=lanes_word([10 if a == (cc + lb + j) % 17 else 0 for a in range(G.P)]))
    c.same("REG", dict(v=4, k=7, c=2, e0=0, mask=ALL, gap=0), (0, 4, 0, 16, 16, 0))
    c.same("PRED", dict(v=4, c=5), (1, 4, 0, 16, 16, 0))
    c.same("RND", dict(lb=2), (0, 4, 0, 16, 16, 0))
    c.same("REB", dict(v=4, k=7, mask=0b10101, gap=0b01010), (1, 4, 0, 16, 16, 1))
    c.same("END", dict(v=4), (0, 4, 0, 16, 16, 0))
    c.same("REG", dict(v=4, k=7, c=0, e0=3, mask=ALL ^ 0b100, gap=0b100), (1, 4, 0, 16, 16, 1))
    c.dump()
    return c


def d_exact():
    """Always Advance's setting: every lane code 0, so a decision stops only where D's admission stops (r) or at the
    cap (r0), passes every gap, and never commits."""
    c = Case("d_exact")
    slot_all(c, 8)
    for cc in range(6):
        for lb in range(3):
            for j in range(8):
                c.ctl("ROW", row=row_index(8, cc, lb, j), data=lanes_word([0] * G.P))
    rng = random.Random(8)
    for v in range(64):
        static = rng.getrandbits(G.P) | 1
        mask = static & ~rng.getrandbits(G.P) | 1
        c.ctl("REG", v=v, k=8, c=rng.randrange(6), e0=rng.randrange(300), mask=mask, gap=static & ~mask)
    c.burst([(v % 2, v, rng.randrange(17), rng.randrange(17), rng.randrange(17), 1) for v in range(64)])
    c.dump()
    return c


DIRECTED = (d_banks, d_codes, d_scan, d_age, d_same, d_exact)


# ------------------------------------------------------------------------------------------------- random
def random_case(seed, n_items):
    """A random table, slot halves and values; registrations, rebases, predictions, ends and rounds; lookups alone, in
    bursts on both ports and with a control operation in their cycle."""
    rng = random.Random(seed)
    c = Case("r_%d" % seed)
    for r in range(G.ROWS):
        c.ctl("ROW", row=r, data=rng.getrandbits(4 * G.P))
    for k in range(G.N_K):
        for h in (0, 1):
            ed = sorted(rng.randrange(1024) for _ in range(7))
            c.ctl("SLOT", k=k, half=h, edges=ed, fence=rng.choice((16, 16, rng.randrange(17))))
    live = set()

    def reg_kw(v):
        static = rng.getrandbits(G.P)
        mask = static & ~(rng.getrandbits(G.P) if rng.random() < 0.6 else 0)
        if mask == 0 and rng.random() < 0.9:
            mask = 1 << rng.randrange(G.P)
        return dict(v=v, k=rng.randrange(G.N_K), c=rng.randrange(6), e0=rng.randrange(1024),
                    mask=mask, gap=static & ~mask)

    def look():
        v = rng.choice(sorted(live)) if live and rng.random() < 0.9 else rng.randrange(G.N_V)
        return (rng.randrange(2), v, rng.choice((0, rng.randrange(17), rng.randrange(32))), rng.randrange(17) if rng.random() < 0.7 else rng.randrange(32),
                rng.randrange(17) if rng.random() < 0.7 else rng.randrange(32), rng.randrange(2))

    def ctl_draw():
        n = rng.choices(("REG", "REB", "PRED", "END", "RND"), (5, 2, 3, 2, 4))[0]
        if n == "REG":
            v = rng.randrange(G.N_V); live.add(v); return n, reg_kw(v)
        v = rng.choice(sorted(live)) if live and rng.random() < 0.9 else rng.randrange(G.N_V)
        if n == "REB":
            kw = reg_kw(v); return n, dict(v=v, k=kw["k"], mask=kw["mask"], gap=kw["gap"])
        if n == "PRED":
            return n, dict(v=v, c=rng.randrange(6))
        if n == "END":
            live.discard(v); return n, dict(v=v)
        return n, dict(lb=rng.randrange(3))

    for v in range(G.N_V):
        if rng.random() < 0.7:
            live.add(v); c.ctl("REG", **reg_kw(v))
    for i in range(n_items):
        x = rng.random()
        if x < 0.35:
            n, kw = ctl_draw(); c.ctl(n, **kw)
        elif x < 0.6:
            c.look(*look())
        elif x < 0.9:
            c.burst([look() for _ in range(rng.randrange(2, 40))])
        else:
            n, kw = ctl_draw(); c.same(n, kw, look())
        if i % 100 == 99:
            c.dump()
    c.dump()
    return c


def main(argv):
    what = argv[0] if argv else "all"
    n_rand, n_items, seed = (int(argv[1]) if len(argv) > 1 else 10), (int(argv[2]) if len(argv) > 2 else 3000), (int(argv[3]) if len(argv) > 3 else 1)
    out = argv[4] if len(argv) > 4 else None
    exe = vlt.build("w2t", "tb_w2_table", "tb_decision_unit.sv")
    cases = ([f() for f in DIRECTED] if what in ("directed", "all") else []) + \
            ([random_case(seed + i, n_items) for i in range(n_rand)] if what in ("random", "all") else [])
    stats = [run_case(c, exe) for c in cases]
    bad = sum(1 for s in stats if s["fails"])
    cyc = collections.Counter()
    for s in stats:
        cyc.update(s["lookup_cycles"])
    b2b = sum(s["back_to_back"] for s in stats)
    print("decision unit: %d of %d cases failed; %d lookups; cycles from presentation to result: %s; %d lookups taken "
          "one cycle after the previous on their port" % (bad, len(stats), sum(s["lookups"] for s in stats), json.dumps(dict(sorted(cyc.items()))), b2b))
    if out:
        json.dump(dict(cases=stats, lookup_cycles=dict(sorted(cyc.items())), back_to_back=b2b, failed=bad), open(out, "w"), indent=1)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
