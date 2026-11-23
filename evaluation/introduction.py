"""Section 1's median waits: SWIPER alone's mean wait per T gate (rounds) for each program, and the median over the
eight programs, at d = 13 and d = 21 (results/introduction/waits.tsv: one row per run, seeds 6940 to 6951, at decoder
budgets of 100 % and 150 %, which give the same printed values)."""
import collections
import csv
import os
import statistics


def run(root, out=None):
    per = collections.defaultdict(list)
    with open(os.path.join(root, "results", "introduction", "waits.tsv")) as f:
        for r in csv.DictReader(f, delimiter="\t"):
            per[int(r["d"]), int(r["budget"]), r["program"]].append(float(r["wait_per_t_gate"]))
    V = []
    for d in (13, 21):
        med = {b: statistics.median([statistics.mean(w) for (dd, bb, _), w in per.items() if (dd, bb) == (d, b)])
               for b in (100, 150)}
        assert "%.1f" % med[100] == "%.1f" % med[150], med
        V.append(("intro.wait_d%d" % d, "%.1f" % med[100],
                  "1: median over the eight programs of SWIPER alone's mean wait per T gate at d=%d (rounds)" % d))
    return V
