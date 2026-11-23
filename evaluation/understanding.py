"""Section 7.2 (Figure "flow", the waiting removed, the recovery price) and Section 5.5's table counts, from results/main/."""
import collections
import csv
import math
import os
import statistics


def read(path):
    with open(path) as f:
        return list(csv.DictReader(f, delimiter="\t"))


def gm(xs):
    return math.exp(statistics.fmean(math.log(x) for x in xs))


def run(root, out):
    V = []
    add = lambda k, v, w: V.append((k, v, w))
    R = read(os.path.join(root, "results", "main", "corrections.tsv"))
    n = len(R)
    tot = collections.Counter()
    for r in R:
        for k, v in r.items():
            if k not in ("program", "d", "budget", "seed"):
                tot[k] += int(v)
    a = {k: v / n for k, v in tot.items()}                       # per run, over the 600 Froq runs
    unreg = a["pending"] - a["registered"]
    apply_ = a["apply_right"] + a["apply_decoded_first"] + a["apply_decoded_first_dropped"] + a["apply_wrong"]
    defer = a["defer_s_not_needed"] + a["defer_s_needed"]
    wait = a["wait_s_not_needed"] + a["wait_s_needed"]
    pct = lambda x, t: "%.1f" % (100 * x / t)
    f1 = lambda x: "{:,.1f}".format(x) if x < 100 else "{:,.0f}".format(x)
    add("flow.pending", "{:,.0f}".format(a["pending"]), "7.2 and Figure flow: 7,440 pending corrections a run")
    add("flow.registered", pct(a["registered"], a["pending"]), "7.2, Figure flow: 99.5 % registered")
    add("flow.not_registered", pct(unreg, a["pending"]), "Figure flow: not registered 0.5 %")
    add("flow.apply", pct(apply_, a["registered"]), "7.2, Figure flow: applying 12.5 %")
    add("flow.defer", pct(defer, a["registered"]), "7.2, Figure flow: deferring 85.8 %")
    add("flow.wait", pct(wait, a["registered"]), "7.2, Figure flow: waiting on 1.7 %")
    add("flow.unreg_decoded_first", pct(a["unreg_decoded_first"], unreg), "7.2: 72.0 % decoded before they could be registered")
    add("flow.unreg_cone", pct(a["unreg_cone_work_started"], unreg), "7.2: 25.6 % rejected, light-cone work already started")
    add("flow.unreg_one", pct(a["unreg_one_at_a_time"], unreg), "7.2: 2.3 % rejected by the limit of one prediction at a time")
    other = a["pending"] - a["registered"] - sum(a[k] for k in ("unreg_decoded_first", "unreg_cone_work_started", "unreg_one_at_a_time", "unreg_overlap"))
    add("flow.unreg_capacity", "%d" % round(other * n), "7.2: the capacity of 64 live corrections rejected none")
    add("flow.apply_right", f1(a["apply_right"]), "Figure flow: prediction right 907")
    add("flow.apply_first", f1(a["apply_decoded_first"]), "Figure flow: decoded first 15.0")
    add("flow.apply_wrong", f1(a["apply_wrong"]), "7.2, Figure flow: 0.2 mispredictions a run (wrong: recovery 0.2)")
    add("flow.apply_total", "%.1f" % apply_, "7.2: 0.2 mispredictions per run out of about 922")
    add("flow.defer_total", "{:,.0f}".format(defer), "7.2: 6,354 deferred corrections a run")
    add("flow.defer_s_not_needed", f1(a["defer_s_not_needed"]), "Figure flow: S not needed 3,615")
    add("flow.defer_s_needed", f1(a["defer_s_needed"]), "7.2, Figure flow: 2,739 later needed S and ran a recovery")
    add("flow.defer_s_needed_share", "%.0f" % (100 * a["defer_s_needed"] / defer), "7.2: (43 %)")
    add("flow.wait_s_not_needed", f1(a["wait_s_not_needed"]), "Figure flow: S not needed 87.8")
    add("flow.wait_s_needed", f1(a["wait_s_needed"]), "Figure flow: S needed 37.5")
    # Figure flow's bar heights: 4 cm for the registered corrections (the figure's band widths are proportional to counts)
    h = lambda x: "%.3f" % (4.0 * x / a["registered"])
    add("flow.bars", "%s %s %s" % (h(apply_), h(defer), h(wait)), "Figure flow: decision bars 4.160-3.661, 3.501-0.068, -0.092-(-0.160) (heights 0.499 3.433 0.068)")

    # Waiting removed (7.2): instruction-rounds delayed by pending corrections, per run, Froq against SWIPER alone.
    D = read(os.path.join(root, "results", "main", "delays.tsv"))
    reasons = [k for k in D[0] if k not in ("program", "d", "budget", "system")]
    held = {s: {k: statistics.fmean(float(r[k]) for r in D if r["system"] == s) for k in reasons} for s in ("froq", "swiper")}
    hf, hs = sum(held["froq"].values()), sum(held["swiper"].values())
    sh = {k: 100 * v / hf for k, v in held["froq"].items()}
    add("delay.removed", "%.0f" % (100 * (1 - hf / hs)), "7.2: Froq removed 61 % of the waiting")
    add("delay.remaining", "%.0f" % (100 * hf / hs), "7.2: 39 % of those under SWIPER alone")
    add("delay.contract_end", "%.1f" % sh["defer_dependency"], "7.2: 56.5 % delayed at the end of a contract")
    add("delay.script", "%.1f" % sh["defer_materialize"], "7.2: 31.9 % waited for a recovery script")
    add("delay.not_registered", "%.1f" % sh["native"], "7.2: 0.3 % waited for corrections that were not registered")
    other = sh["defer_stop"] + sh["defer_step"] + sh["defer_rule_a"] + sh["defer_script_cell"] + sh["feedback"]
    add("delay.other", "%.1f" % other, "7.2: the other 11.3 %")
    add("delay.decision_waits", "%.1f" % sh["defer_stop"], "7.2: the decision chose to wait before the contract's end (6.1 %)")
    add("delay.admission", "%.1f" % (sh["defer_step"] + sh["defer_rule_a"]), "7.2: admission of a step that the decision allowed (4.8 %)")
    add("delay.cell_or_cz", "%.1f" % (sh["defer_script_cell"] + sh["feedback"]), "7.2: a cell a recovery script held or a conditioned CZ (0.3 %)")
    assert all(v == 0 for k, v in held["froq"].items() if k not in ("defer_dependency", "defer_materialize", "native", "defer_stop",
                                                                  "defer_step", "defer_rule_a", "defer_script_cell", "feedback"))

    # Recovery price against the measured recovery time (7.2, C3).
    P = [r for r in read(os.path.join(root, "results", "main", "recovery_price.tsv")) if r["ratio"] != "-"]
    x = [float(r["ratio"]) for r in P]
    add("price.gm", "%.2f" % gm(x), "7.2: ratio 1.07")
    add("price.n", "%d" % len(P), "7.2: over the 48 configurations with such recoveries")
    add("price.range", "%.2f-%.2f" % (min(x), max(x)), "7.2: per-configuration ratios from 0.87 to 1.47")
    g = {d: gm([float(r["ratio"]) for r in P if r["d"] == d]) for d in ("13", "17", "21")}
    add("price.d13", "%.1f" % (100 * (1 / g["13"] - 1)), "7.2: underestimated by 8.8 % at d=13")
    add("price.d17", "%.1f" % (100 * (g["17"] - 1)), "7.2: overestimated by 16.0 % at d=17")
    add("price.d21", "%.1f" % (100 * (g["21"] - 1)), "7.2: 13.9 % at d=21")

    # Section 5.5: decision-table profiles.
    T = read(os.path.join(root, "results", "main", "profiles.tsv"))
    add("tables.max_uncorrected", "%d" % max(int(r["profiles_uncorrected"]) for r in T), "5.5: the uncorrected branch needed at most 59 profiles")
    add("tables.applied_over_64", "%d of %d" % (sum(int(r["profiles_applied"]) > 64 for r in T), len(T)), "5.5: more than 64 at 37 of the 50")
    add("tables.no_slot", "%.1f" % (100 * sum(int(r["apply_pairs_without_table"]) for r in T) / sum(int(r["apply_pairs"]) for r in T)),
        "5.5: the other 1.5 % of these pairs cannot apply")
    return V
