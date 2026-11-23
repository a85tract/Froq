"""Section 7.1 (Figure "cuts"), Section 6's counts, and the workspace cost of Section 7.3, from results/main/."""
import collections
import csv
import math
import os
import statistics
import sys
from decimal import ROUND_HALF_UP, Decimal

from stats import compare, cut, holm, per_seed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from froq.configuration import SETTINGS as MENU, pick                               # noqa: E402  the selector

PROGRAMS = ["Grover-5", "QFT-10", "QPE-10", "QPE-5", "Adder-4", "Adder-8", "Adder-16", "Adder-32"]
BUDGETS = {13: [50, 75, 100, 150], 17: [50, 75, 100, 150], 21: [75, 100, 150]}


def r1(x):
    """One decimal, halves rounded up (as the paper prints 4.25 as 4.3)."""
    return str(Decimal(repr(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def read(path):
    with open(path) as f:
        return list(csv.DictReader(f, delimiter="\t"))


def load(root):
    runs = read(os.path.join(root, "results", "main", "runs.tsv"))
    settings = {(r["program"], int(r["d"]), int(r["budget"])): r for r in read(os.path.join(root, "results", "main", "settings.tsv"))}
    T = {}
    for r in runs:
        assert r["censored"] == "0"
        T[(r["program"], int(r["d"]), int(r["budget"])), r["system"], int(r["seed"])] = r
    return runs, settings, T


def reselect(root, settings):
    """Re-apply the training-time selector (Section 5.4) to the recorded training means; it must give every setting."""
    n = 0
    for r in read(os.path.join(root, "results", "main", "selection.tsv")):
        got = pick({c: (int(r[c + ":censored"]), float(r[c + ":mean_log"])) for c in MENU})
        s = settings[(r["program"], int(r["d"]), int(r["budget"]))]
        assert got == ("wait" if s["setting"] == "wait" else s["setting"] + "/" + s["margin"]), (r, got)
        n += 1
    return n


def run(root, out):
    runs, settings, T = load(root)
    nsel = reselect(root, settings)
    configs = sorted(settings, key=lambda k: (PROGRAMS.index(k[0]), k[1], k[2]))
    assert len(configs) == 88 and nsel == 88
    S = [c for c in configs if settings[c]["setting"] != "wait"]
    M = [c for c in S if settings[c]["setting"] == "concurrent"]
    W = [c for c in configs if c not in S]
    seeds = sorted({k[2] for k in T})
    assert len(seeds) == 12 and set((k[0] for k in T)) == set(S)
    LR = {(c, s): math.log(T[c, "froq", s]["makespan"] and int(T[c, "froq", s]["makespan"]) / int(T[c, "swiper", s]["makespan"]))
          for c in S for s in seeds}
    f88, f1, f3 = (compare(per_seed(LR, X, seeds)) for X in (configs, S, M))
    H = holm({"F1": f1["p"], "F3": f3["p"]})
    per_cfg = {c: compare([LR[c, s] for s in seeds]) for c in S}
    peak = min(S, key=lambda c: per_cfg[c]["ratio"])
    margins = collections.Counter(settings[c]["margin"] for c in S)

    # Figure "cuts": data/cutbars.dat (the same rounding and layout as the paper's file).
    lines = ["x prog " + " ".join("c%d ep%d em%d lo%d hi%d w%d" % ((d,) * 6) for d in BUDGETS)]
    bars = {}
    for x, p in enumerate(PROGRAMS, 1):
        cells = []
        for d in BUDGETS:
            X = [(p, d, b) for b in BUDGETS[d]]
            r = compare(per_seed(LR, X, seeds))
            c, lo, hi = cut(r["ratio"]), cut(r["hi"]), cut(r["lo"])
            waits = int(not any(cf in S for cf in X))
            bars[(p, d)] = (c, lo, hi, waits)
            cells.append("%.1f %.2f %.2f %.2f %.2f %d" % (c, hi - c, c - lo, lo, hi, waits))
        lines.append("%d %s %s" % (x, p, " ".join(cells)))
    os.makedirs(os.path.join(out, "data"), exist_ok=True)
    with open(os.path.join(out, "data", "cutbars.dat"), "w") as f:
        f.write("\n".join(lines) + "\n")

    # Workspace (Section 7.3): the most cells occupied or held at once, per configuration the mean over the seeds.
    ws = {sysname: {c: statistics.fmean(int(T[c, sysname, s]["peak_cells"]) for s in seeds) for c in S} for sysname in ("froq", "swiper")}
    extra = {c: ws["froq"][c] - ws["swiper"][c] for c in S}
    wmax = max(S, key=lambda c: extra[c])

    spec_bars = [bars[k] for k in bars if not bars[k][3]]
    four = [p for p in PROGRAMS if all(not bars[(p, d)][3] for d in BUDGETS) and p.split("-")[0] != "Adder"]
    a8 = [c for c in S if c[0] == "Adder-8"]
    sel = {(r["program"], int(r["d"]), int(r["budget"])): r for r in read(os.path.join(root, "results", "main", "selection.tsv"))}
    a8gap = []
    for c in a8:
        r = sel[c]
        best = min(float(r[m + ":mean_log"]) for m in MENU)
        a8gap.append(100 * (float(r["wait:mean_log"]) - best))
    a8cfg = {c: per_cfg[c] for c in a8}
    a4 = [c for c in S if c[0] == "Adder-4"]
    q5 = [c for c in S if c[0] == "QPE-5" and c[1] == 21]

    V = []   # (key, value as printed, where)
    add = lambda k, v, w: V.append((k, v, w))
    add("main.cut88", "%.2f" % cut(f88["ratio"]), "abstract, 1, 7.1: over all 88 configurations")
    add("main.cut50", "%.2f" % cut(f1["ratio"]), "7.1, 8: over the 50 speculating configurations")
    add("main.cut45", "%.2f" % cut(f3["ratio"]), "abstract, 1, 7.1: over the 45 concurrently speculating configurations")
    add("main.peak", "%.1f" % cut(per_cfg[peak]["ratio"]), "abstract, 1, 7.1: peak reduction")
    add("main.peak_at", "%s d=%d budgets %s" % (peak[0], peak[1], "/".join(str(c[2]) for c in q5 if round(cut(per_cfg[c]["ratio"]), 1) == round(cut(per_cfg[peak]["ratio"]), 1))),
        "7.1: QPE-5 at d=21, at budgets of 100 and 150 %")
    add("main.n_configs", "%d" % len(configs), "6: 88 configurations")
    add("main.n_wait", "%d" % len(W), "1, 6, 7.1: 38 configurations where Froq waits")
    add("main.n_spec", "%d" % len(S), "6, 7: 50 speculating configurations")
    add("main.n_concurrent", "%d" % len(M), "6: concurrent predictions at 45")
    add("main.n_one", "%d" % (len(S) - len(M)), "6: one prediction at a time at 5")
    add("main.margins", "%d/%d/%d" % (margins["0"], margins["d"], margins["4d"]), "6: margin 0 at 33, d at 10, 4d at 7")
    add("main.n_runs", "{:,}".format(2 * len(S) * len(seeds)), "6: all 1,200 runs completed")
    add("main.adder16_32_wait", "%s" % all(c in W for c in configs if c[0] in ("Adder-16", "Adder-32")), "7.1: every configuration of Adder-16 and Adder-32 waits")
    add("main.p_max", "%s" % (max(f1["p"], f3["p"]) < 1e-23), "7.1: each comparison p < 10^-23")
    add("main.holm", "both rejected: %s" % (H["F1"][1] and H["F3"][1]), "7.1: the comparisons are significant under Holm's procedure")
    add("main.four_range", "%.1f-%.1f" % (min(bars[(p, d)][0] for p in four for d in BUDGETS), max(bars[(p, d)][0] for p in four for d in BUDGETS)), "7.1: 10.6 to 30.2 % each")
    add("main.d13_range", "%.1f-%.1f" % (min(bars[(p, 13)][0] for p in four), max(bars[(p, 13)][0] for p in four)), "7.1: 10.6-14.4 % at d=13")
    add("main.d21_range", "%.1f-%.1f" % (min(bars[(p, 21)][0] for p in four), max(bars[(p, 21)][0] for p in four)), "7.1: 24.8-30.2 % at d=21")
    add("main.qpe5_d21_bar", "%.1f" % bars[("QPE-5", 21)][0], "7.1: 30.2 % of QPE-5 at d=21 (geometric mean over three budgets)")
    add("main.adder4", "d=%s: %.1f, interval includes zero: %s" % (",".join(sorted({str(c[1]) for c in a4})), bars[("Adder-4", 17)][0], bars[("Adder-4", 17)][1] < 0 < bars[("Adder-4", 17)][2]),
        "7.1: Adder-4 speculates only at d=17, 0.4 %, interval includes zero")
    add("main.adder8_train", "%.2f-%.2f" % (min(a8gap), max(a8gap)), "7.1: waiting was 0.52-0.73 % slower on the training seeds")
    add("main.adder8_d", "%s" % ",".join(sorted({str(c[1]) for c in a8})) + " " + ",".join(sorted({settings[c]["setting"] for c in a8})), "7.1: Adder-8 at d=13 and 17, one prediction at a time")
    add("main.adder8_test", "d=13 interval includes zero: %s; d=17 %.1f, interval includes zero: %s" % (
        bars[("Adder-8", 13)][1] < 0 < bars[("Adder-8", 13)][2], bars[("Adder-8", 17)][0], bars[("Adder-8", 17)][1] < 0 < bars[("Adder-8", 17)][2]),
        "7.1: no significant change at d=13, 0.8 % slowdown at d=17")
    add("ws.froq", r1(statistics.fmean(ws["froq"].values())), "7.3: 21.5 cells under Froq")
    add("ws.swiper", r1(statistics.fmean(ws["swiper"].values())), "7.3: 19.9 under SWIPER alone")
    add("ws.extra", r1(statistics.fmean(extra.values())), "7.3: 1.6 more cells on average")
    add("ws.extra_max", "%s at %s d=%d" % (r1(extra[wmax]), wmax[0], wmax[1]), "7.3: at most 4.3 more, for Grover-5 at d=21")
    detail = dict(F88=f88, F1=f1, F3=f3, holm=H, peak=(peak, per_cfg[peak]))
    with open(os.path.join(out, "main_comparison.txt"), "w") as f:
        for k in ("F88", "F1", "F3"):
            r = detail[k]
            f.write("%s: ratio %.4f [%.4f, %.4f], cut %.2f %% [%.2f, %.2f], t %.2f, df %d, one-sided p %.3e, bound %.4f\n" % (
                k, r["ratio"], r["lo"], r["hi"], cut(r["ratio"]), cut(r["hi"]), cut(r["lo"]), r["t"], r["df"], r["p"], r["upper"]))
        f.write("Holm: %s\n" % H)
        for c in S:
            r = per_cfg[c]
            f.write("%-9s d=%d budget %3d%%  %-10s margin %-2s  ratio %.5f [%.5f, %.5f]  cut %.2f %%\n" % (
                c[0], c[1], c[2], settings[c]["setting"], settings[c]["margin"], r["ratio"], r["lo"], r["hi"], cut(r["ratio"])))
    return V
