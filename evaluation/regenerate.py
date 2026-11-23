"""Regenerate the numbers, tables and figure data of the paper's evaluation from the result files in results/.

    python evaluation/regenerate.py [--out DIR] [--arxiv DIR]

Writes DIR/data/cutbars.dat (Figure "cuts"), DIR/data/stress.dat (the table inside Figure "stress"),
DIR/tables/tab_programs.tex, DIR/tables/tab_cost.tex, DIR/main_comparison.txt (every comparison and configuration) and
DIR/numbers.tsv (every number, regenerated beside the paper's).  It then compares: each number with the value the paper
prints (evaluation/paper_values.tsv), and each file with the paper's own copy (evaluation/paper/, or the files of an
unpacked arXiv source given with --arxiv).  The exit status is 0 only if everything matches.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import hardware          # noqa: E402
import introduction      # noqa: E402
import main_comparison   # noqa: E402
import programs          # noqa: E402
import supporting        # noqa: E402
import understanding     # noqa: E402

NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def numbers(s):
    return [float(x.replace(",", "")) for x in NUM.findall(s)]


def same(got, want, tol):
    if not tol:
        return got == want
    a, b = numbers(got), numbers(want)
    return len(a) == len(b) and all(abs(x - y) <= float(tol) + 1e-9 for x, y in zip(a, b))


def stress_table(tex):
    """The rows of the table inside figures/fig_stress.tex."""
    rows = re.findall(r"^\s*(\d+ [\d.]+ [\d.]+ [\d.]+) \\\\", tex, re.M)
    return "y r lo hi\n" + "\n".join(rows) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--arxiv", help="an unpacked arXiv source of the paper; its files replace evaluation/paper/ in the comparison")
    a = ap.parse_args()
    out = a.out
    os.makedirs(out, exist_ok=True)
    V = []
    V += main_comparison.run(ROOT, out)
    V += understanding.run(ROOT, out)
    V += supporting.run(ROOT, out)
    V += programs.run(ROOT, out, os.path.join(ROOT, "froq", "schedules"))
    V += hardware.run(ROOT, out)
    V += introduction.run(ROOT, out)

    want = {}
    for line in open(os.path.join(HERE, "paper_values.tsv")):
        if line.startswith("#") or not line.strip():
            continue
        k, v, tol, where = (line.rstrip("\n").split("\t") + ["", ""])[:4]
        want[k] = (v, tol, where)
    bad, rows = 0, ["key\tregenerated\tpaper\tmatch\twhere"]
    for k, got, where in V:
        if k not in want:
            rows.append("%s\t%s\t-\tno paper value\t%s" % (k, got, where))
            continue
        v, tol, _ = want.pop(k)
        ok = same(got, v, tol)
        bad += not ok
        rows.append("%s\t%s\t%s\t%s\t%s" % (k, got, v, ("yes" if not tol else "within %s" % tol) if ok else "NO", where))
    for k, (v, tol, where) in want.items():
        bad += 1
        rows.append("%s\t-\t%s\tNOT REGENERATED\t%s" % (k, v, where))
    open(os.path.join(out, "numbers.tsv"), "w").write("\n".join(rows) + "\n")

    src = a.arxiv or os.path.join(HERE, "paper")
    files = [("data/cutbars.dat", "data/cutbars.dat", None), ("tables/tab_programs.tex", "tables/tab_programs.tex", None),
             ("tables/tab_cost.tex", "tables/tab_cost.tex", None), ("data/stress.dat", "figures/fig_stress.tex", stress_table)]
    print("%-28s %s" % ("file", "against " + src))
    for mine, theirs, conv in files:
        p = os.path.join(src, theirs)
        if not os.path.exists(p):
            print("%-28s paper file %s missing" % (mine, theirs)); bad += 1; continue
        ref = open(p).read()
        ref = conv(ref) if conv else ref
        ok = open(os.path.join(out, mine)).read() == ref
        bad += not ok
        print("%-28s %s" % (mine, "identical" if ok else "DIFFERENT"))
    n = sum(1 for r in rows[1:] if r.split("\t")[3] != "no paper value")
    print("numbers: %d compared, %d differ or are missing (details: %s)" % (n, bad, os.path.join(out, "numbers.tsv")))
    for r in rows[1:]:
        f = r.split("\t")
        if f[3] in ("NO", "NOT REGENERATED"):
            print("  %s: regenerated %s, paper %s (%s)" % (f[0], f[1], f[2], f[4]))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
