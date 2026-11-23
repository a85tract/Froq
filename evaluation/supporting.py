"""Sections 7.2 (fixed rules), 7.3 (caps, simpler admission) and 7.4 (Figure "stress", prediction sources) from
results/sensitivity/ and results/supporting/.  These runs use the settings selected for one prediction at a time
(Section 7.4's note), except where a row says concurrent."""
import collections
import csv
import math
import os
import statistics

from stats import compare, cut

N_POINTS = {"f0.125", "f0.25", "f0.5", "tail"}      # points whose SWIPER-alone runs change too (decode time, decode draws)


def read(path):
    with open(path) as f:
        return list(csv.DictReader(f, delimiter="\t"))


def pooled(num, den, configs, seeds, all_configs=None):
    """Per seed mean over configs of ln(num/den); configs outside `configs` but in all_configs add 0."""
    X = all_configs or configs
    xs = [sum(math.log(num[c, s] / den[c, s]) for c in configs) / len(X) for s in seeds]
    return compare(xs)


def run(root, out):
    V = []
    add = lambda k, v, w: V.append((k, v, w))
    pc = lambda r: "%.1f" % cut(r["ratio"])
    R = read(os.path.join(root, "results", "sensitivity", "runs.tsv"))
    T, setting = {}, {}
    for r in R:
        c = (r["program"], int(r["d"]), int(r["budget"]))
        T[(r["point"], r["system"], c, int(r["seed"]))] = int(r["makespan"])
        if r["system"] == "froq":
            setting[c] = r["setting"]
    seeds = sorted({k[3] for k in T})
    C50 = sorted(setting)
    dec = [c for c in C50 if setting[c].startswith("decision")]
    assert len(C50) == 50 and len(dec) == 39 and seeds == [7126, 7127, 7128]

    def point(p, configs=C50):
        num = {(c, s): T[(p, "froq", c, s)] for c in configs for s in seeds}
        den = {(c, s): T[(p if p in N_POINTS else "ref", "swiper", c, s)] for c in configs for s in seeds}
        return pooled(num, den, configs, seeds)
    P = {p: point(p) for p in ("ref", "s2", "f0.5", "f0.25", "f0.125", "a0.5", "a0", "d16", "d64", "d128", "k1/10", "k10",
                               "mn10", "mn100", "mn1000", "mc10", "mc100", "mc1000", "tail")}
    s4c = sorted({k[2] for k in T if k[0] == "s4"})
    P["s4"], ref45 = point("s4", s4c), point("ref", s4c)
    # Figure "stress": reduction and 95 % interval at the most extreme setting of each stress (the figure's own table)
    rows = [(8, "ref"), (7, "s4"), (6, "f0.125"), (5, "a0"), (4, "d128"), (3, "k10"), (2, "mn1000"), (1, "tail")]
    fig = ["y r lo hi"] + ["%d %.2f %.2f %.2f" % (y, cut(P[p]["ratio"]), cut(P[p]["hi"]), cut(P[p]["lo"])) for y, p in rows]
    os.makedirs(os.path.join(out, "data"), exist_ok=True)
    with open(os.path.join(out, "data", "stress.dat"), "w") as f:
        f.write("\n".join(fig) + "\n")
    add("stress.ref", pc(P["ref"]), "7.2, 7.4: the reference reduction of 11.1 %")
    add("stress.s2", pc(P["s2"]), "7.4: controller two times slower, 10.9 %")
    add("stress.s4", "%s over %d, reference %s" % (pc(P["s4"]), len(s4c), pc(ref45)), "7.4: four times slower, 11.6 % over the other 45, against 12.4 %")
    add("stress.decode", "%s %s %s" % (pc(P["f0.5"]), pc(P["f0.25"]), pc(P["f0.125"])), "7.4: decodes a half, a quarter, an eighth: 7.3, 4.9, 3.5 %")
    add("stress.avail", "%s %s" % (pc(P["a0.5"]), pc(P["a0"])), "7.2, 7.4: predictions for half 9.1 %, none 7.0 %")
    add("stress.later", "%s %s %s" % (pc(P["d16"]), pc(P["d64"]), pc(P["d128"])), "7.4: published 16, 64, 128 rounds later: 10.6, 8.6, 7.2 %")
    add("stress.calibration", "%s %s" % (pc(P["k1/10"]), pc(P["k10"])), "7.4: error probabilities ten times larger or smaller: 11.1 %")
    add("stress.mispred_10_100", "%s %s / %s %s" % (pc(P["mn10"]), pc(P["mn100"]), pc(P["mc10"]), pc(P["mc100"])), "7.4: 10 or 100 times as frequent: 11.1 and 10.8 % (both confidences)")
    add("stress.mispred_1000", "%s %s" % (pc(P["mn1000"]), pc(P["mc1000"])), "7.4: 1,000 times: 9.2 % (original confidence), 9.1 % (corrected)")
    M = {r["inflation"]: r for r in read(os.path.join(root, "results", "sensitivity", "mispredictions.tsv"))}
    add("stress.mispred_rate", "%.0f" % (100 * int(M["1000"]["wrong_original_confidence"]) / int(M["1000"]["values_both"])), "7.4: a misprediction rate of about 31 %")

    # Fixed rules for the choice to defer (7.2): at the 39 decision configurations; the 11 fixed-policy ones keep their runs.
    def rule(p):
        return {(c, s): T[(p if c in dec else "ref", "froq", c, s)] for c in C50 for s in seeds}
    ref = {(c, s): T[("ref", "froq", c, s)] for c in C50 for s in seeds}
    nref = {(c, s): T[("ref", "swiper", c, s)] for c in C50 for s in seeds}
    aa, awf = rule("rule:always-defer"), rule("rule:defer-while-flat")
    r_aa, r_awf = pooled(aa, nref, C50, seeds), pooled(awf, nref, C50, seeds)
    p_aa, p_awf = pooled(aa, ref, C50, seeds), pooled(awf, ref, C50, seeds)
    add("rules.n", "%d of %d" % (len(dec), len(C50)), "7.2: at the 39 of the 50 speculating configurations")
    add("rules.decision", pc(P["ref"]), "7.2: the decision reduced quantum-side time by 11.1 %")
    add("rules.always", pc(r_aa), "7.2: always deferring reduced it by only 10.0 %")
    add("rules.always_paired", "%.2f (%.2f to %.2f)" % (100 * (p_aa["ratio"] - 1), 100 * (p_aa["lo"] - 1), 100 * (p_aa["hi"] - 1)), "7.2: took 1.25 % longer (interval 0.64 to 1.87 %)")
    add("rules.flat", pc(r_awf), "7.2: deferring while the script's volume stays flat reduced it by 10.9 %")
    add("rules.flat_paired", "%.2f (%.2f to %.2f)" % (100 * (p_awf["ratio"] - 1), 100 * (p_awf["lo"] - 1), 100 * (p_awf["hi"] - 1)), "7.2: paired difference 0.22 % (interval -0.03 to 0.46 %)")

    # Supporting runs on seeds 7100-7111.
    S = read(os.path.join(root, "results", "supporting", "runs.tsv"))
    U = {}
    for r in S:
        U[(r["experiment"], r["system"], r["setting"].split("/")[2] if r["setting"].startswith("decision") else r["setting"],
           (r["program"], int(r["d"]), int(r["budget"])), r["cap"], int(r["seed"]))] = int(r["makespan"])
    main = collections.defaultdict(dict)
    for r in S:
        if r["experiment"] == "main":
            frame = "concurrent" if "concurrent" in r["setting"] else "one"
            main[(r["system"], frame)][((r["program"], int(r["d"]), int(r["budget"])), int(r["seed"]))] = int(r["makespan"])
    sd12 = sorted({k[1] for k in main[("froq", "one")]})
    one = sorted({k[0] for k in main[("froq", "one")]})
    conc = sorted({k[0] for k in main[("froq", "concurrent")]})
    assert len(sd12) == 12 and len(one) == 50 and len(conc) == 45
    x2 = pooled(main[("froq-simple-admission", "one")], main[("froq", "one")], one, sd12, all_configs=range(88))
    x3 = pooled(main[("froq-simple-admission", "concurrent")], main[("froq", "concurrent")], conc, sd12)
    add("admission.concurrent", "%.2f (%.2f to %.2f) over %d" % (cut(x3["ratio"]), cut(x3["hi"]), cut(x3["lo"]), len(conc)), "7.3: 1.19 % over the 45 (interval 1.11 to 1.27 %)")
    add("admission.one", "%.2f (%.2f to %.2f) over 88" % (cut(x2["ratio"]), cut(x2["hi"]), cut(x2["lo"])), "7.3: 0.10 % over all 88 (interval 0.06 to 0.15 %)")
    sw = main[("froq-swiper-predictions", "one")]
    x1b = pooled(sw, main[("froq", "one")], one, sd12)
    add("source.vs_local", "%.2f (%.2f to %.2f) over %d" % (100 * (x1b["ratio"] - 1), 100 * (x1b["lo"] - 1), 100 * (x1b["hi"] - 1), len(one)),
        "7.4: 3.92 % longer than with the local-decoding predictor (interval 3.80 to 4.04 %)")
    nsw = main[("swiper", "one")]
    have = [c for c in one if all((c, s) in nsw for s in sd12)]
    if len(have) == len(one):
        x1 = pooled(sw, nsw, one, sd12, all_configs=range(88))
        add("source.vs_swiper", "%.2f" % cut(x1["ratio"]), "7.4: still reduced quantum-side time by 4.43 % over all 88")
    else:
        add("source.vs_swiper", "not in release (%d of %d configurations have every SWIPER-alone run)" % (len(have), len(one)),
            "7.4: still reduced quantum-side time by 4.43 % over all 88")

    # Caps (7.3): d = 13, seeds 7100-7105, both systems capped alike at SWIPER alone's median peak or midway.
    caps = read(os.path.join(root, "results", "supporting", "caps.tsv"))
    Cr = collections.defaultdict(dict)
    for r in S:
        if r["experiment"] == "caps":
            k = (r["system"], int(r["cap"])) if r["system"] == "swiper" else (r["system"], int(r["cap"]), r["setting"])
            Cr[k][((r["program"], int(r["d"]), int(r["budget"])), int(r["seed"]))] = int(r["makespan"])
    sd6 = sorted({k[1] for v in Cr.values() for k in v})
    for frame in ("one", "concurrent"):
        cells = [r for r in caps if r["set"] == frame]
        for lev in ("cap_swiper_peak", "cap_mid"):
            num = {(i, s): Cr[("froq", int(r[lev]), r["setting"])][((r["program"], int(r["d"]), int(r["budget"])), s)] for i, r in enumerate(cells) for s in sd6}
            den = {(i, s): Cr[("swiper", int(r[lev]))][((r["program"], int(r["d"]), int(r["budget"])), s)] for i, r in enumerate(cells) for s in sd6}
            x = pooled(num, den, list(range(len(cells))), sd6)
            add("caps.%s.%s" % (frame, lev), "%.2f" % cut(x["ratio"]), "7.3: under the caps 7.88 / 7.90 % (one at a time), 12.85 / 12.86 % (concurrent)")

    # Leads (7.4): per run the median lead; the median over a configuration's seeds, the mean over a program's
    # configurations, the mean over programs; Froq's runs.
    Ld = read(os.path.join(root, "results", "supporting", "leads.tsv"))
    for col, key, w in (("swiper_lead_median", "source.lead_swiper", "7.4: arrives only 2.5 rounds before SWIPER confirms it"),
                        ("predictor_lead_median", "source.lead_local", "7.4: against 32.9 rounds for the local-decoding predictor")):
        byc = collections.defaultdict(list)
        for r in Ld:
            if r["system"] == "froq":
                byc[(r["program"], r["d"], r["budget"])].append(float(r[col]))
        byp = collections.defaultdict(list)
        for c, v in byc.items():
            byp[c[0]].append(statistics.median(v))
        add(key, "%.1f" % statistics.fmean(statistics.fmean(v) for v in byp.values()), w)
    return V
