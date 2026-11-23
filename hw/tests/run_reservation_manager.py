"""The reservation manager's tests (rule 59 after F_4; ports): directed scenarios and
random operation streams, the RTL (`hw/rtl/reservation_manager.sv` under `hw/tb/tb_reservation_manager.sv`, Verilator) against the golden model
(`hw/golden/resv.py`).

Each case checks every operation's result (the fields its operation defines), each port's in its order, every status
check's held bits, and full-state dumps (every valid slot, every cell with a count, every frame's guard, the round and
the totals), and records the cycles each kind of operation and each check took. A case may also require that cell
operations ran while the slot port's TICK scanned (the port split).
- directed: the six first cases, then d_ports, d_guards and d_status;
- random: the 20 first streams, unchanged;
- random2: round 5's streams, with batches across the two ports, the frames' guards and the status port.

   python3 -m tests.run_reservation_manager [directed | random | random2 | all] [n_random] [ops_each] [seed] [OUT.json]
                             [n_random2] [ops_each2] [seed2] (from hw/)"""
import collections, json, os, random, sys
HW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HW)
from golden import resv as G
from tests import vlt

FIELDS = dict(LAZY=("ok", "slot"), ACT=("ok", "slot"), HELD=("held",), TICK=("total", "unstarted"), TOTALS=("total", "unstarted"),
              QUERY=("q_ws", "q_tgt", "q_lock"))
R_KEYS = ("port", "op", "ok", "slot", "held", "total", "unstarted", "q_ws", "q_tgt", "q_lock", "cycles", "taken")
Q_KEYS = ("h0", "h1", "h2", "h3", "cycles", "taken")


def mask_words(cells):
    w = [0] * ((G.N_C + 63) // 64)
    for c in cells:
        w[c // 64] |= 1 << (c % 64)
    return w


class Case:
    def __init__(self, name, act_lat=7):
        self.name, self.act_lat, self.items, self.expect_overlap = name, act_lat, [], 0

    def op(self, name, **kw):
        self.items.append(("O", name, kw))

    def dump(self):
        self.items.append(("D",))

    def mask(self, sel, cells):
        for i, w in enumerate(mask_words(cells)):
            self.op("MASKW", word=i, sel=sel, data=w)

    def batch(self, ops):
        """[(name, kw)]: run with the two ports at once, each port's in this order."""
        self.items.append(("B", len(ops)))
        for n, kw in ops:
            self.op(n, **kw)

    def check(self, cells, overlap=False):
        """A status check of four cells (None: not asked); overlap: presented with the next operation."""
        self.items.append(("KO" if overlap else "K", list(cells)))

    def burst(self, checks):
        self.items.append(("KB", len(checks)))
        for cells in checks:
            self.check(cells)

    def vec(self):
        L = ["L %d" % self.act_lat]
        for it in self.items:
            if it[0] == "D":
                L.append("D")
            elif it[0] in ("B", "KB"):
                L.append("%s %d" % it)
            elif it[0] in ("K", "KO"):
                m = sum(1 << i for i, x in enumerate(it[1]) if x is not None)
                L.append("%s %d %s" % (it[0], m, " ".join(str(x or 0) for x in it[1])))
            else:
                _, n, k = it
                L.append("O %d %d %d %d %d %d %d %d %x %d" % (G.OP[n], k.get("slot", 0), k.get("cores", 0), k.get("held", 0), k.get("s", 0),
                                                              k.get("r", 0), k.get("word", 0), k.get("sel", 0), k.get("data", 0),
                                                              k.get("frame", 0)))
        return "\n".join(L + ["X"]) + "\n"

    def golden(self):
        """The expected results [(port, op, {field: value})], checks [held bits] and dumps [lines]."""
        g = G.Resv(self.act_lat)
        res, chk, dumps = [], [], []
        for it in self.items:
            if it[0] == "D":
                st = g.state()
                lines = ["S %d %d %d %d %d %d %d" % ((i,) + tuple(s[1:])) for i, s in enumerate(st["slots"]) if s[0]]
                lines += ["C %d %d %d %d" % (c, st["ws"][c], st["tgt"][c], st["lock"][c]) for c in range(G.N_C)
                          if st["ws"][c] or st["tgt"][c] or st["lock"][c]]
                lines += ["G %d %0*x" % (f, G.N_C // 4, gb) for f, gb in sorted(st["guards"].items())]
                lines.append("N %d %d %d" % (st["now"], st["total"], st["unstarted"]))
                dumps.append(lines)
            elif it[0] in ("K", "KO"):
                chk.append(g.check(it[1])["held"])
            elif it[0] == "O":
                _, n, k = it
                res.append((0 if n in G.SLOT_OPS else 1, n, g.do(n, **k)))
        return res, chk, dumps


def parse(path):
    res, chk, dumps, cur, end = [], [], [], None, None
    for l in open(path):
        p = l.split()
        if not p:
            continue
        if p[0] == "R":
            res.append(dict(zip(R_KEYS, map(int, p[1:]))))
        elif p[0] == "Q":
            chk.append(dict(zip(Q_KEYS, map(int, p[1:]))))
        elif p[0] in ("S", "C", "G"):
            if cur is None:
                cur = []
            cur.append(l.strip())
        elif p[0] == "N":
            (cur if cur is not None else []).append(l.strip())
            dumps.append(cur if cur is not None else [l.strip()])
            cur = None
        elif p[0] == "E":
            end = tuple(map(int, p[1:]))
    return res, chk, dumps, end


def run_case(case, exe):
    wdir = vlt.workdir("resv")
    vec, out = os.path.join(wdir, "vec_%s.txt" % case.name), os.path.join(wdir, "out_%s.txt" % case.name)
    open(vec, "w").write(case.vec())
    vlt.run(exe, {"VEC": vec, "OUT": out}, timeout=3600)
    got, gchk, gdumps, end = parse(out)
    want, wchk, wdumps = case.golden()
    fails = []
    cyc = collections.defaultdict(list)
    for port in (0, 1):
        g_ = [r for r in got if r["port"] == port]
        w_ = [(n, w) for p, n, w in want if p == port]
        if len(g_) != len(w_):
            fails.append("port %d: %d results, %d expected" % (port, len(g_), len(w_)))
        for i, (r, (n, w)) in enumerate(zip(g_, w_)):
            cyc[n].append(r["cycles"])
            if r["op"] != G.OP[n]:
                fails.append("port %d result %d: op %d, expected %s" % (port, i, r["op"], n)); break
            bad = [f for f in FIELDS.get(n, ()) if r[f] != w[f]]
            if bad:
                fails.append("port %d result %d (%s): %s" % (port, i, n, ", ".join("%s rtl %d golden %d" % (f, r[f], w[f]) for f in bad)))
                if len(fails) > 5:
                    break
    if len(gchk) != len(wchk):
        fails.append("%d checks, %d expected" % (len(gchk), len(wchk)))
    for i, (r, w) in enumerate(zip(gchk, wchk)):
        cyc["CHECK"].append(r["cycles"])
        h = [r["h0"], r["h1"], r["h2"], r["h3"]]
        if h != w:
            fails.append("check %d: rtl %s golden %s" % (i, h, w))
            if len(fails) > 5:
                break
    if len(gdumps) != len(wdumps):
        fails.append("%d dumps, %d expected" % (len(gdumps), len(wdumps)))
    for i, (a, b) in enumerate(zip(gdumps, wdumps)):
        if a != b:
            diff = sorted(set(a) ^ set(b))[:6]
            fails.append("dump %d differs: %s" % (i, diff))
            break
    ticks = [(r["taken"], r["taken"] + r["cycles"]) for r in got if r["port"] == 0 and r["op"] == G.OP["TICK"]]
    overlap = sum(1 for r in got if r["port"] == 1 and any(a <= r["taken"] < b for a, b in ticks))
    if overlap < case.expect_overlap:
        fails.append("%d cell operations ran during a tick, %d expected" % (overlap, case.expect_overlap))
    stat = dict(case=case.name, ops=len(want), checks=len(wchk), dumps=len(wdumps), cycles=end[2] if end else None,
                cell_ops_during_ticks=overlap,
                op_cycles={n: dict(min=min(v), max=max(v), mean=round(sum(v) / len(v), 2), n=len(v)) for n, v in sorted(cyc.items())},
                fails=fails)
    print("%s: %d operations, %d checks, %d dumps, %d cell operations during ticks -> %s" % (
        case.name, len(want), len(wchk), len(wdumps), overlap, "PASS" if not fails else "FAIL: " + "; ".join(fails[:3])), flush=True)
    return stat


# ------------------------------------------------------------------------------------------------- directed
def d_lazy():
    c = Case("d_lazy")
    c.op("LAZY", cores=12); c.op("TOTALS"); c.op("HELD", slot=0); c.op("LAZY", cores=300); c.op("HELD", slot=1)
    c.op("TICK"); c.dump(); c.op("REL", slot=0); c.op("TOTALS"); c.op("REL", slot=0); c.op("REL", slot=1); c.op("TOTALS"); c.dump()
    return c


def d_act():
    """ACT: no count until its effect round (act_lat ticks), counted to its end (S + R after), then expired; demand
    below held gives 0 cores."""
    c = Case("d_act", act_lat=3)
    c.op("ACT", cores=20, held=5, s=4, r=2)                   # 15 cores, counted at rounds 3..9, expired at 10
    c.op("ACT", cores=4, held=9, s=1, r=1)                    # 0 cores
    for _ in range(12):
        c.op("TICK"); c.dump()
    return c


def d_started():
    c = Case("d_started", act_lat=1)
    c.op("ACT", cores=3, held=0, s=10, r=0)
    c.op("STARTED", slot=0); c.op("TOTALS")                   # started before it counts
    c.op("TICK"); c.op("TOTALS"); c.dump()                     # counted: unstarted 3 - 1
    for _ in range(4):
        c.op("STARTED", slot=0); c.op("TOTALS")                # down to 0, never below
    c.op("LAZY", cores=7); c.op("STARTED", slot=1); c.op("TOTALS")   # a lazy reservation takes no started core
    c.op("REL", slot=0); c.op("TOTALS"); c.dump()
    return c


def d_full():
    c = Case("d_full")
    for i in range(G.N_R):
        c.op("LAZY", cores=i % 7)
    c.op("LAZY", cores=5); c.op("ACT", cores=5, held=0, s=1, r=1)    # refused: no free slot
    c.op("REL", slot=17); c.op("REL", slot=3); c.op("ACT", cores=9, held=1, s=2, r=3); c.op("LAZY", cores=1)
    c.op("TOTALS"); c.dump()
    return c


def d_cells():
    c = Case("d_cells")
    c.mask(0, [0, 1, 63, 64, 511]); c.mask(1, [])
    c.op("QUERY")
    c.op("WS_ADD"); c.op("QUERY"); c.dump()
    c.mask(1, [0, 1, 63, 64, 511]); c.op("QUERY")              # its own share only: no other frame
    c.op("WS_ADD"); c.op("QUERY")                               # a second frame
    c.op("TGT_ADD"); c.op("LOCK_ADD"); c.op("QUERY"); c.dump()
    c.mask(1, []); c.op("QUERY")
    for _ in range(130):
        c.op("WS_ADD")                                         # saturates at 127
    for _ in range(20):
        c.op("TGT_ADD"); c.op("LOCK_ADD")                     # 15 and 7
    c.dump()
    for _ in range(140):
        c.op("WS_SUB")
    for _ in range(20):
        c.op("TGT_SUB"); c.op("LOCK_SUB")                     # down to 0, never below
    c.op("QUERY"); c.dump()
    c.mask(0, list(range(G.N_C))); c.op("LOCK_ADD"); c.mask(0, [300]); c.mask(1, [300]); c.op("QUERY")
    c.mask(1, [301]); c.op("QUERY"); c.dump()
    return c


def d_wrap():
    """The 16-bit round counter wraps with an ACT counted across it."""
    c = Case("d_wrap", act_lat=7)
    for _ in range(65530):
        c.op("TICK")
    c.op("ACT", cores=33, held=0, s=10, r=5); c.op("LAZY", cores=2)
    for _ in range(30):
        c.op("TICK"); c.dump()
    return c


def d_ports():
    """The port split: RM-W's operations run while RM-D's TICK scans its slots (as built they waited for it), and
    slot and cell operations run at once."""
    c = Case("d_ports", act_lat=2)
    c.op("LAZY", cores=5); c.op("ACT", cores=9, held=2, s=3, r=1)
    ops = [("TICK", {})] + [("MASKW", dict(word=i, sel=0, data=w)) for i, w in enumerate(mask_words([0, 7, 64, 200, 511]))]
    ops += [("WS_ADD", {}), ("TGT_ADD", {}), ("LOCK_ADD", {}), ("QUERY", {}), ("GUARD_ADD", dict(frame=5))]
    c.batch(ops); c.dump()                                     # 13 cell operations, 27 cycles, inside the 67-cycle tick
    c.expect_overlap += 13
    ops = [("LAZY", dict(cores=3)), ("STARTED", dict(slot=1)), ("HELD", dict(slot=0)), ("REL", dict(slot=0)), ("TOTALS", {}),
           ("ACT", dict(cores=4, held=0, s=2, r=2))]
    ops += [("MASKW", dict(word=i, sel=1, data=w)) for i, w in enumerate(mask_words([7, 200]))]
    ops += [("QUERY", {}), ("WS_SUB", {}), ("GUARD_SUB", dict(frame=5)), ("QUERY", {})]
    c.batch(ops); c.dump()
    ops = [("TICK", {})] * 3 + [("TOTALS", {})]
    for k in range(6):                                         # 6 x 11 cell operations: they outrun one tick, not three
        ops += [("MASKW", dict(word=i, sel=0, data=w)) for i, w in enumerate(mask_words([k, 64 + k, 300 + k]))]
        ops += [("WS_ADD", {}), ("GUARD_ADD", dict(frame=10 + k)), ("QUERY", {})]
    c.batch(ops); c.dump()
    c.expect_overlap += 6 * 11
    return c


def d_guards():
    """Guards by frame number: GUARD_ADD stores mask A and counts it in ws; GUARD_SUB uncounts the stored guard and
    clears it; overlaps, an empty guard, the last frame, an overwrite, saturation and a rebase."""
    c = Case("d_guards")
    c.mask(0, [0, 5, 63, 64, 300, 511]); c.op("GUARD_ADD", frame=0); c.dump()
    c.mask(0, [5, 6, 64]); c.op("GUARD_ADD", frame=1); c.dump()              # 5 and 64 at 2
    c.mask(0, [100]); c.op("WS_ADD")                                          # a plain update beside them
    c.op("GUARD_SUB", frame=0); c.dump()                                      # its stored guard, not mask A
    c.op("GUARD_SUB", frame=0); c.dump()                                      # an empty guard: nothing
    c.op("GUARD_SUB", frame=1); c.op("WS_SUB"); c.dump()                      # back to 0
    c.mask(0, list(range(G.N_C))); c.op("GUARD_ADD", frame=G.N_G - 1); c.dump()   # the last frame, every cell
    c.mask(0, [9]); c.op("GUARD_ADD", frame=G.N_G - 1)                        # overwritten: the guard is [9] now
    c.op("GUARD_SUB", frame=G.N_G - 1); c.dump()                              # only [9] goes: every cell stays at 1
    c.mask(0, list(range(G.N_C)))
    for f in range(G.N_G):
        c.op("GUARD_ADD", frame=f)                                            # 1 + 64 = 65
    for _ in range(70):
        c.op("WS_ADD")                                                        # saturates at 127
    c.dump()
    for f in range(G.N_G):
        c.op("GUARD_SUB", frame=f)                                            # 127 - 64 = 63
    c.dump()
    for _ in range(70):
        c.op("WS_SUB")                                                        # down to 0, never below
    c.mask(0, [1, 2, 3]); c.op("GUARD_ADD", frame=7)                          # a rebase: the old guard out by number,
    c.op("GUARD_SUB", frame=7); c.mask(0, [3, 4]); c.op("GUARD_ADD", frame=7) # the new one in
    c.mask(1, []); c.mask(0, [1, 2, 3, 4]); c.op("QUERY"); c.mask(1, [3]); c.op("QUERY"); c.dump()
    return c


def d_status():
    """The status port: the held bits of up to four cells, one check a cycle; a check presented with a cell operation
    does not see it."""
    c = Case("d_status")
    edge = [0, 63, 64, 127, 128, 255, 256, 511]
    c.check([0, 63, 64, 511])                                                 # all clear
    c.mask(0, [0, 64]); c.op("WS_ADD"); c.mask(0, [63]); c.op("TGT_ADD"); c.mask(0, [511]); c.op("LOCK_ADD")
    c.check([0, 63, 64, 511]); c.check([0, None, 64, None]); c.check([None, None, None, None]); c.check([511, 511, 0, 0])
    c.mask(0, [128]); c.check([128, 255, 256, 127], overlap=True); c.op("WS_ADD")    # presented with it: not seen
    c.check([128, 255, 256, 127])                                             # seen
    c.mask(0, [0, 64, 63, 511, 128]); c.op("GUARD_ADD", frame=9)
    c.burst([[e, edge[(i + 1) % 8], edge[(i + 3) % 8], edge[(i + 5) % 8]] for i, e in enumerate(edge)] * 2)
    c.check([63, 0, 0, 0], overlap=True); c.op("GUARD_SUB", frame=9)          # the guard's cells are still held
    c.check([63, 0, 64, 128])
    c.mask(0, [0, 64, 128]); c.op("WS_SUB")
    c.check([63, 0, 64, 128]); c.check([511, None, 63, None])
    c.mask(0, list(range(G.N_C)))
    for _ in range(20):
        c.op("TGT_ADD"); c.op("LOCK_ADD")                                     # saturated: still held
    c.burst([[x, x + 1, x + 2, x + 3] for x in range(0, G.N_C, 32)])
    for _ in range(20):
        c.op("TGT_SUB"); c.op("LOCK_SUB")
    c.burst([[x, x + 9, x + 17, x + 31] for x in range(0, G.N_C, 32)])
    c.dump()
    return c


DIRECTED = (d_lazy, d_act, d_started, d_full, d_cells, d_wrap, d_ports, d_guards, d_status)


# ------------------------------------------------------------------------------------------------- random
def random_case(seed, n_ops):
    rng = random.Random(seed)
    c = Case("r_%d" % seed, act_lat=rng.choice((1, 3, 7, 12)))
    g = G.Resv(c.act_lat)                                      # a scheduler's view, to aim its operations
    W = dict(LAZY=10, ACT=10, REL=10, STARTED=10, HELD=5, TICK=15, TOTALS=3, MASK=8, WS_ADD=5, WS_SUB=4, TGT_ADD=4, TGT_SUB=3,
             LOCK_ADD=4, LOCK_SUB=3, QUERY=10)
    names, weights = zip(*W.items())
    hot = list(range(G.N_C))
    for k in range(n_ops):
        n = rng.choices(names, weights)[0]
        live = [i for i, s in enumerate(g.slots) if s is not None]
        slot = rng.choice(live) if (live and rng.random() < 0.85) else rng.randrange(G.N_R)
        cores = rng.randrange(41) if rng.random() < 0.7 else rng.randrange(1 << G.CW)
        if n == "MASK":
            dens = rng.choice((0.002, 0.02, 0.1, 0.5))
            cells = [x for x in hot if rng.random() < dens]
            sel = rng.randrange(2)
            for i, w in enumerate(mask_words(cells)):
                c.op("MASKW", word=i, sel=sel, data=w); g.do("MASKW", word=i, sel=sel, data=w)
            continue
        kw = dict(LAZY=dict(cores=cores), ACT=dict(cores=cores, held=rng.randrange(cores + 1) if rng.random() < 0.5 else rng.randrange(1 << G.CW),
                                                      s=rng.randrange(1500), r=rng.randrange(600)),
                  REL=dict(slot=slot), STARTED=dict(slot=slot), HELD=dict(slot=slot)).get(n, {})
        c.op(n, **kw); g.do(n, **kw)
        if k % 50 == 49:
            c.dump()
    c.dump()
    return c


def random2_case(seed, n_ops):
    """Round 5's streams: every operation of the first streams, the frames' guards, status checks (alone, presented with
    a cell operation, and in bursts), and batches across the two ports."""
    rng = random.Random(seed)
    c = Case("q_%d" % seed, act_lat=rng.choice((1, 3, 7, 12)))
    g = G.Resv(c.act_lat)                                      # a scheduler's view, to aim its operations
    W = dict(LAZY=6, ACT=6, REL=6, STARTED=5, HELD=3, TICK=8, TOTALS=2, MASK=6, WS_ADD=3, WS_SUB=3, TGT_ADD=3, TGT_SUB=3,
             LOCK_ADD=3, LOCK_SUB=3, QUERY=6, GUARD_ADD=6, GUARD_SUB=6)
    X = dict(CHECK=8, OVERLAP=2, BURST=2, BATCH=4)
    names, weights = zip(*W.items())
    cell_names = [n for n in names if n not in G.SLOT_OPS]

    def cells4():
        held = [x for x in range(G.N_C) if g.ws[x] or g.tgt[x] or g.lock[x]]
        return [None if rng.random() < 0.15 else (rng.choice(held) if held and rng.random() < 0.6 else rng.randrange(G.N_C))
                for _ in range(4)]

    def one(n):
        """The operations [(name, kw)] of one draw, aimed by the view and applied to it."""
        live = [i for i, s_ in enumerate(g.slots) if s_ is not None]
        slot = rng.choice(live) if (live and rng.random() < 0.85) else rng.randrange(G.N_R)
        cores = rng.randrange(41) if rng.random() < 0.7 else rng.randrange(1 << G.CW)
        if n == "MASK":
            dens = rng.choice((0.002, 0.02, 0.1, 0.5))
            cells = [x for x in range(G.N_C) if rng.random() < dens]
            sel = rng.randrange(2)
            ops = [("MASKW", dict(word=i, sel=sel, data=w)) for i, w in enumerate(mask_words(cells))]
        elif n in ("GUARD_ADD", "GUARD_SUB"):
            used = [f for f in range(G.N_G) if g.guard[f]]
            free = [f for f in range(G.N_G) if not g.guard[f]]
            pool = (used if n == "GUARD_SUB" else free) if rng.random() < 0.85 else list(range(G.N_G))
            ops = [(n, dict(frame=rng.choice(pool or list(range(G.N_G)))))]
        else:
            kw = dict(LAZY=dict(cores=cores), ACT=dict(cores=cores, held=rng.randrange(cores + 1) if rng.random() < 0.5 else rng.randrange(1 << G.CW),
                                                          s=rng.randrange(1500), r=rng.randrange(600)),
                      REL=dict(slot=slot), STARTED=dict(slot=slot), HELD=dict(slot=slot)).get(n, {})
            ops = [(n, kw)]
        for m, kw in ops:
            g.do(m, **kw)
        return ops

    for k in range(n_ops):
        n = rng.choices(names + tuple(X), weights + tuple(X.values()))[0]
        if n == "CHECK":
            c.check(cells4())
        elif n == "OVERLAP":
            c.check(cells4(), overlap=True)
            for m, kw in one(rng.choice(cell_names)):
                c.op(m, **kw)
        elif n == "BURST":
            c.burst([cells4() for _ in range(rng.randrange(2, 17))])
        elif n == "BATCH":
            ops = []
            for _ in range(rng.randrange(2, 13)):
                ops += one(rng.choices(names, weights)[0])
            c.batch(ops)
        else:
            for m, kw in one(n):
                c.op(m, **kw)
        if k % 50 == 49:
            c.dump()
    c.dump()
    return c


def main(argv):
    what = argv[0] if argv else "all"
    n_rand, n_ops, seed = (int(argv[1]) if len(argv) > 1 else 20), (int(argv[2]) if len(argv) > 2 else 3000), (int(argv[3]) if len(argv) > 3 else 1)
    out = argv[4] if len(argv) > 4 else None
    n_rand2, n_ops2, seed2 = (int(argv[5]) if len(argv) > 5 else 10), (int(argv[6]) if len(argv) > 6 else 3000), (int(argv[7]) if len(argv) > 7 else 101)
    exe = vlt.build("resv", "tb_resv", "tb_reservation_manager.sv")
    cases = ([f() for f in DIRECTED] if what in ("directed", "all") else []) + \
            ([random_case(seed + i, n_ops) for i in range(n_rand)] if what in ("random", "all") else []) + \
            ([random2_case(seed2 + i, n_ops2) for i in range(n_rand2)] if what in ("random2", "all") else [])
    stats = [run_case(c, exe) for c in cases]
    bad = sum(1 for s in stats if s["fails"])
    agg = collections.defaultdict(lambda: dict(min=10 ** 9, max=0, n=0, tot=0.0))
    for s in stats:
        for n, v in s["op_cycles"].items():
            a = agg[n]; a["min"] = min(a["min"], v["min"]); a["max"] = max(a["max"], v["max"]); a["n"] += v["n"]; a["tot"] += v["mean"] * v["n"]
    cyc = {n: dict(min=a["min"], max=a["max"], mean=round(a["tot"] / a["n"], 2), n=a["n"]) for n, a in sorted(agg.items())}
    during = sum(s["cell_ops_during_ticks"] for s in stats)
    print("reservation manager: %d of %d cases failed; %d cell operations ran during ticks; cycles per operation (presented to result): %s"
          % (bad, len(stats), during, json.dumps(cyc)))
    if out:
        json.dump(dict(cases=stats, cycles=cyc, failed=bad, cell_ops_during_ticks=during), open(out, "w"), indent=1)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
