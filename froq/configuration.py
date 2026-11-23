"""Run one configuration of the paper's evaluation (Section 6) end to end, in the paper's terms.

A configuration is a program, a code distance d and a decoder budget (50, 75, 100 or 150 % of the program's peak decoder
demand P).  Every run is one seed of one system: SWIPER alone, or Froq with one setting of the decision (Section 5.4):

    wait            Froq always waits (its runs are SWIPER alone's)
    concurrent/M    concurrent predictions, margin M in {0, d, 4d}
    one/M           one prediction at a time, margin M in {0, d, 4d}

Commands (from the repository root):

    python -m froq.configuration run    --program Adder-4 --distance 17 --budget 100 --system froq --setting concurrent/0 --seed 7200
    python -m froq.configuration select --program Adder-4 --distance 13 --budget 100 --jobs 8
    python -m froq.configuration test   --program Adder-4 --distance 17 --budget 100 --jobs 8
    python -m froq.configuration all    --part 1/8 --jobs 8
    python -m froq.configuration unpack

`unpack` decompresses the four MQT programs' schedules (froq/schedules/mqt/*.xz) and the adders' recovery-script caches
(froq/schedules/snippets_sx/*.xz), and checks each file's SHA-256 against the program's manifest or the caches'
SHA256SUMS; the other commands do it first when it is needed.  A run whose output directory already holds
a finished record is not run again, so an interrupted command can be started again.

`select` runs every setting on the ten training seeds and picks one (fewest censored runs; waiting wins when it is within
0.5 % of the best; otherwise the lowest mean log makespan).  `test` runs the picked setting (by default the paper's, from
results/main/settings.tsv) and SWIPER alone on the twelve test seeds.  Both print each number next to the recorded one in
results/.  `all` does `select` then `test` for every configuration in part i of n.
"""
from __future__ import annotations

import argparse, concurrent.futures, csv, glob, hashlib, json, lzma, math, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")
PROGRAMS = {"Grover-5": "grover_ancilla_5", "QFT-10": "qft_10", "QPE-10": "qpeexact_10", "QPE-5": "qpeexact_5",
            "Adder-4": "adder_4", "Adder-8": "adder_8", "Adder-16": "adder_16", "Adder-32": "adder_32"}
LEVELS = {50: "0.5P", 75: "0.75P", 100: "P", 150: "1.5P"}
F_FLIP = {13: "0.127096", 17: "0.130592", 21: "0.127511"}        # SWIPER's model: probability a T gadget needs its S
SETTINGS = ("wait", "concurrent/4d", "concurrent/d", "concurrent/0", "one/4d", "one/d", "one/0")   # the selector's tie order
TIE = math.log(1.005)                                              # waiting wins within 0.5 % of the best setting
TRAINING_SEEDS = tuple(range(6600, 6610))
TEST_SEEDS = tuple(range(7200, 7212))
DATA = os.path.join(ROOT, "froq", "data")
PRICES = ["--w2-r2", os.path.join(DATA, "prices", "waits.json"),
          "--w2-xic", os.path.join(DATA, "prices", "decode_delay_price.json"),
          "--w2-lambda", os.path.join(DATA, "prices", "arrival.json"),
          "--w2-co", os.path.join(DATA, "prices", "overhead_price.json")]


def read_tsv(path):
    with open(path) as f:
        return list(csv.DictReader(f, delimiter="\t"))


def peak_demand(program, d):
    """P, the program's peak decoder demand at distance d (cores)."""
    for r in read_tsv(os.path.join(RESULTS, "programs", "programs.tsv")):
        if r["program"] == program:
            return int(r["peak_demand_d%d" % d])
    raise SystemExit("unknown program %s" % program)


def schedules(bench):
    return os.path.join("froq", "schedules", "adder" if bench.startswith("adder") else "mqt")


def unpack():
    """Decompress each compressed input that is not unpacked yet; its SHA-256 must equal the recorded one (the MQT
    schedules' manifests, the recovery-script caches' SHA256SUMS)."""
    want = {}
    for man in glob.glob(os.path.join(ROOT, "froq", "schedules", "mqt", "*.manifest.json")):
        f = json.load(open(man))["fingerprints"]
        want[man[:-len(".manifest.json")] + ".lss"] = f["lss"]
        want[man[:-len(".manifest.json")] + ".events.json"] = f["events"]
    sx = os.path.join(ROOT, "froq", "schedules", "snippets_sx")
    for line in open(os.path.join(sx, "SHA256SUMS")):
        digest, name = line.split()
        want[os.path.join(sx, name)] = digest
    for path, digest in sorted(want.items()):
        if os.path.exists(path) or not os.path.exists(path + ".xz"):
            continue
        h = hashlib.sha256()
        with lzma.open(path + ".xz") as src, open(path + ".part", "wb") as dst:
            for chunk in iter(lambda: src.read(1 << 22), b""):
                h.update(chunk)
                dst.write(chunk)
        if h.hexdigest() != digest:
            os.remove(path + ".part")
            raise SystemExit("%s.xz does not decompress to the recorded SHA-256" % path)
        os.replace(path + ".part", path)
        print("unpacked %s (SHA-256 checked)" % os.path.relpath(path, ROOT))


def missing(program, d, froq=True):
    """The first input a run of this program needs that is not in this release (README, 'What is not included'), or None."""
    bench = PROGRAMS[program]
    need = [os.path.join(schedules(bench), bench + ".lss")]
    if froq:                                                       # Froq's recovery scripts, by the program's qubit count
        n = next(int(r["qubits"]) for r in read_tsv(os.path.join(RESULTS, "programs", "programs.tsv")) if r["program"] == program)
        need.append(os.path.join("froq", "schedules", "snippets_sx", "%d.json" % n))
    return next((f for f in need if not os.path.exists(os.path.join(ROOT, f))), None)


def command(program, d, budget, setting, seed, out, censor_at=0):
    """The simulator's command line for one run; `setting` "wait" is SWIPER alone."""
    bench = PROGRAMS[program]
    gap = missing(program, d, froq=setting != "wait")
    if gap:
        raise SystemExit("%s is not in this release (README, 'What is not included')" % gap)
    cores = -(-budget * peak_demand(program, d) // 100)            # ceil(level x P)
    c = [sys.executable, "-m", "froq.run", "--bench", bench, "--d", str(d), "--seed", str(seed), "--table", "local",
         "--out", out, "--mhz", "0", "--sched", schedules(bench), "--budget", str(cores), "--f-flip", F_FLIP[d]]
    if setting == "wait":
        return c + ["--arm", "N", "--client", "swiper", "--hw-limits", "improved", "--r7-records", "--h-depth", "2",
                    "--r17-records", "--stall-rounds", "100000"]
    share, margin = setting.split("/")
    profile = os.path.join(DATA, "profiles", "%s_d%d.json.gz" % (bench, d))
    c += ["--arm", "W2YBl1spSd", "--client", "strip", "--pool", "shared", "--planner", "swiper", "--censor-rn", str(censor_at),
          "--budget-level", LEVELS[budget], "--hw-limits", "improved", "--w2-profiles", os.path.join(out, "profiles"),
          "--w2-records", "--r7-records", "--r7-guard", "--r8-rule", "--r8-event-budget", "131072", "--r8-window", "8192",
          "--r8-gsvc", "8", "--r8-gsvc-acc", "4", "--wq", "C=32768,H=16384,e=32,f=1,b=4096", "--w2-margin", margin,
          "--resv", "static", "--w2-lf", "inf" if share == "concurrent" else "1", "--h-depth", "2", "--r17-records",
          "--library", "sx", "--diag-records"] + PRICES
    if os.path.exists(profile):                                    # profiles built ahead of the campaign (as it ran)
        c += ["--w2-profile-file", profile]
    return c


def finished(out):
    """(makespan in rounds, censored) from the run's record in `out`, or None if there is no complete record."""
    res = [p for p in glob.glob(os.path.join(out, "*.json")) if not p.endswith((".diag.json", ".trace.json"))]
    try:
        r = json.load(open(res[0])) if len(res) == 1 else None
        return (int(r["num_rounds"]), r.get("stopping", {}).get("outcome") == "censored") if r else None
    except (ValueError, KeyError, TypeError):
        return None


def run_one(program, d, budget, setting, seed, out, censor_at=0):
    """Run once (or read the finished record already in `out`); return (makespan in rounds, censored)."""
    got = finished(out)
    if got:
        return got
    os.makedirs(out, exist_ok=True)
    log = os.path.join(out, "run.log")
    with open(log, "w") as f:
        rc = subprocess.run(command(program, d, budget, setting, seed, out, censor_at), cwd=ROOT, stdout=f,
                            stderr=subprocess.STDOUT).returncode
    got = finished(out)
    if rc != 0 or not got:
        raise RuntimeError("run failed (rc %d), see %s" % (rc, log))
    return got


def run_many(jobs, tasks):
    """tasks: {key: (args...)} for run_one; returns {key: (makespan, censored)}."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        fut = {k: ex.submit(run_one, *a) for k, a in tasks.items()}
        return {k: f.result() for k, f in fut.items()}


def pick(stats):
    """The selector (Section 5.4): stats {setting: (censored runs, mean log makespan)} -> setting."""
    few = min(c for c, _ in stats.values())
    eligible = [s for s in SETTINGS if stats[s][0] == few]
    best = min(stats[s][1] for s in eligible)
    if "wait" in eligible and stats["wait"][1] - best <= TIE:
        return "wait"
    return min(eligible, key=lambda s: (stats[s][1], SETTINGS.index(s)))


def where(out, program, d, budget):
    return os.path.join(out, "%s_d%d_b%d" % (program, d, budget))


def select(program, d, budget, out, jobs):
    base = where(out, program, d, budget)
    wait = run_many(jobs, {s: (program, d, budget, "wait", s, os.path.join(base, "train", "wait_s%d" % s))
                           for s in TRAINING_SEEDS})
    tasks = {(st, s): (program, d, budget, st, s, os.path.join(base, "train", "%s_s%d" % (st.replace("/", "_"), s)),
                       wait[s][0]) for st in SETTINGS[1:] for s in TRAINING_SEEDS}
    got = run_many(jobs, tasks)
    got.update({("wait", s): v for s, v in wait.items()})
    stats = {st: (sum(got[st, s][1] for s in TRAINING_SEEDS),
                  sum(math.log(got[st, s][0]) for s in TRAINING_SEEDS) / len(TRAINING_SEEDS)) for st in SETTINGS}
    rec = {(r["program"], int(r["d"]), int(r["budget"])): r for r in read_tsv(os.path.join(RESULTS, "main", "selection.tsv"))}
    r = rec.get((program, d, budget))
    print("selection, %s d=%d budget %d%%, training seeds %d-%d" % (program, d, budget, TRAINING_SEEDS[0], TRAINING_SEEDS[-1]))
    print("  %-14s %9s %20s %20s %11s" % ("setting", "censored", "mean log makespan", "recorded", "difference"))
    for st in SETTINGS:
        was = float(r[st + ":mean_log"]) if r else float("nan")
        print("  %-14s %9d %20.15f %20.15f %11.1e" % (st, stats[st][0], stats[st][1], was, stats[st][1] - was))
    chosen = pick(stats)
    paper = paper_setting(program, d, budget)
    print("  picked: %s (paper: %s)" % (chosen, paper))
    return chosen


def paper_setting(program, d, budget):
    for r in read_tsv(os.path.join(RESULTS, "main", "settings.tsv")):
        if (r["program"], int(r["d"]), int(r["budget"])) == (program, d, budget):
            return "wait" if r["setting"] == "wait" else r["setting"] + "/" + r["margin"]
    return None


def test(program, d, budget, out, jobs, setting=None):
    setting = setting or paper_setting(program, d, budget)
    base = where(out, program, d, budget)
    swiper = run_many(jobs, {s: (program, d, budget, "wait", s, os.path.join(base, "test", "swiper_s%d" % s))
                             for s in TEST_SEEDS})
    if setting == "wait":
        froq = dict(swiper)                                        # Froq waits: its runs are SWIPER alone's
    else:
        froq = run_many(jobs, {s: (program, d, budget, setting, s, os.path.join(base, "test", "froq_s%d" % s), swiper[s][0])
                               for s in TEST_SEEDS})
    rec = {(r["system"], int(r["seed"])): r for r in read_tsv(os.path.join(RESULTS, "main", "runs.tsv"))
           if (r["program"], int(r["d"]), int(r["budget"])) == (program, d, budget)}
    print("test, %s d=%d budget %d%%, Froq setting %s, test seeds %d-%d" % (program, d, budget, setting, TEST_SEEDS[0],
                                                                         TEST_SEEDS[-1]))
    print("  %5s %14s %10s %12s %10s" % ("seed", "SWIPER alone", "recorded", "Froq", "recorded"))
    for s in TEST_SEEDS:
        print("  %5d %14d %10s %12d %10s" % (s, swiper[s][0], rec.get(("swiper", s), {}).get("makespan", "-"), froq[s][0],
                                             rec.get(("froq", s), {}).get("makespan", "-")))
    logs = [math.log(froq[s][0] / swiper[s][0]) for s in TEST_SEEDS]
    ratio = math.exp(sum(logs) / len(logs))
    print("  quantum-side time, Froq over SWIPER alone (geometric mean): %.6f, reduction %.2f %%" % (ratio, 100 * (1 - ratio)))
    return ratio


def configurations():
    return [(r["program"], int(r["d"]), int(r["budget"])) for r in read_tsv(os.path.join(RESULTS, "main", "settings.tsv"))]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=("run", "select", "test", "all", "unpack"))
    ap.add_argument("--program", choices=sorted(PROGRAMS))
    ap.add_argument("--distance", type=int, choices=(13, 17, 21))
    ap.add_argument("--budget", type=int, choices=sorted(LEVELS), help="percent of the peak decoder demand P")
    ap.add_argument("--system", choices=("swiper", "froq"), default="froq")
    ap.add_argument("--setting", choices=SETTINGS, help="Froq's setting (run, test); default for test: the paper's")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--censor-at", type=int, default=0, help="run: SWIPER alone's makespan for this seed; a Froq run "
                    "is censored past 4 times it (0: no bound)")
    ap.add_argument("--part", default="1/1", help="all: part i of n of the 88 configurations, e.g. 3/8")
    ap.add_argument("--jobs", type=int, default=1, help="runs at once")
    ap.add_argument("--out", default="runs", help="directory for the runs' records")
    a = ap.parse_args(argv)
    out = os.path.abspath(a.out)
    unpack()
    if a.what == "unpack":
        return
    if a.what == "all":
        i, n = (int(x) for x in a.part.split("/"))
        for k, (p, d, b) in enumerate(configurations()):
            if k % n != i - 1:
                continue
            gap = missing(p, d)
            if gap:
                print("skipped %s d=%d budget %d%%: %s is not in this release" % (p, d, b, gap))
                continue
            test(p, d, b, out, a.jobs, select(p, d, b, out, a.jobs))
        return
    if None in (a.program, a.distance, a.budget):
        ap.error("--program, --distance and --budget are required")
    if a.what == "run":
        setting = "wait" if a.system == "swiper" else a.setting
        if setting is None or a.seed is None:
            ap.error("run needs --seed, and --setting for Froq")
        name = "%s_d%d_b%d_%s_s%d" % (a.program, a.distance, a.budget, setting.replace("/", "_"), a.seed)
        m, cens = run_one(a.program, a.distance, a.budget, setting, a.seed, os.path.join(out, name), a.censor_at)
        print("%s d=%d budget %d%% %s seed %d: makespan %d rounds%s" % (a.program, a.distance, a.budget,
              "SWIPER alone" if setting == "wait" else "Froq " + setting, a.seed, m, " (censored)" if cens else ""))
    elif a.what == "select":
        select(a.program, a.distance, a.budget, out, a.jobs)
    else:
        test(a.program, a.distance, a.budget, out, a.jobs, a.setting)


if __name__ == "__main__":
    main()
