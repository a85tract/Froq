"""Table "programs" (Section 6): qubits, T gates, T depth, feedback and peak decoder demand P, from results/programs/.
Qubits and T gates are recounted from the program circuits shipped with the code; so is the T depth of the adders, whose
circuits have no rotations to synthesize.  The four MQT programs' T depth is the compiler's count on their compiled
schedules (longest chain of T gadgets in the event dependency graph), recorded in programs.tsv."""
import csv
import math
import os
import re

ORDER = ["QPE-5", "QPE-10", "QFT-10", "Grover-5", "Adder-4", "Adder-8", "Adder-16", "Adder-32"]     # the table's row order
QASM = {"QPE-5": "qasm/qpeexact_5.qasm", "QPE-10": "qasm/qpeexact_10.qasm", "QFT-10": "qasm/qft_10.qasm",
        "Grover-5": "qasm/grover_ancilla_5.qasm", "Adder-4": "adder/adder_4.qasm", "Adder-8": "adder/adder_8.qasm",
        "Adder-16": "adder/adder_16.qasm", "Adder-32": "adder/adder_32.qasm"}


def qasm_stats(path):
    """(qubits, T gates, T depth) of an OpenQASM 2 circuit: T depth is the longest chain of T / T-dagger gates when every
    gate orders the qubits it touches."""
    nq, tcount, depth, regs = 0, 0, {}, {}
    for line in open(path):
        line = line.split("//")[0].strip()
        if not line or line.startswith(("OPENQASM", "include", "creg", "barrier", "if")):
            continue
        m = re.match(r"qreg\s+(\w+)\[(\d+)\];", line)
        if m:
            regs[m.group(1)] = nq
            nq += int(m.group(2))
            continue
        m = re.match(r"([a-z_0-9]+)(\([^)]*\))?\s+(.*);", line)
        if not m:
            continue
        qs = [regs[a] + int(b) for a, b in re.findall(r"(\w+)\[(\d+)\]", m.group(3)) if a in regs]
        lvl = max((depth.get(q, 0) for q in qs), default=0)
        if m.group(1) in ("t", "tdg"):
            tcount += 1
            lvl += 1
        for q in qs:
            depth[q] = lvl
    return nq, tcount, max(depth.values())


def run(root, out, sched_dir):
    V = []
    rows = {r["program"]: r for r in csv.DictReader(open(os.path.join(root, "results", "programs", "programs.tsv")), delimiter="\t")}
    for p in ORDER:
        q, t, td = qasm_stats(os.path.join(sched_dir, QASM[p]))
        r = rows[p]
        assert (q, t) == (int(r["qubits"]), int(r["t_gates"])), (p, q, t)
        if p.startswith("Adder"):
            assert td == int(r["t_depth"]), (p, td)
    # every decoder budget of results/main/settings.tsv is ceil(level x P)
    n = 0
    for s in csv.DictReader(open(os.path.join(root, "results", "main", "settings.tsv")), delimiter="\t"):
        P = int(rows[s["program"]]["peak_demand_d%s" % s["d"]])
        assert int(s["cores"]) == math.ceil(int(s["budget"]) * P / 100), s
        n += 1
    fb = lambda r: ", ".join("$%s$" % x.split("_")[1] for x in sorted(r["feedback"].split(","), key=lambda x: x != "CONDITIONAL_S"))
    c = lambda x: "{:,}".format(int(x))
    body = ["%s & %s & %s & %s & %s & %s & %s & %s \\\\" % (p, rows[p]["qubits"], c(rows[p]["t_gates"]), c(rows[p]["t_depth"]), fb(rows[p]),
                                                         rows[p]["peak_demand_d13"], rows[p]["peak_demand_d17"], rows[p]["peak_demand_d21"]) for p in ORDER]
    tex = r"""\begin{table}[t]
\caption{Programs and peak decoder demand in cores.}
\label{tab:programs}
\centering
\setlength{\tabcolsep}{4pt}
\resizebox{\columnwidth}{!}{%
\begin{tabular}{@{}lrrrlrrr@{}}
\toprule
\textbf{Program} & \textbf{Qubits} & \textbf{{\boldmath$T$} gates} & \textbf{{\boldmath$T$} depth} & \textbf{Feedback} & \textbf{{\boldmath$d=13$}} & \textbf{17} & \textbf{21} \\
\midrule
""" + "\n".join(body) + r"""
\bottomrule
\end{tabular}}
\end{table}
"""
    os.makedirs(os.path.join(out, "tables"), exist_ok=True)
    open(os.path.join(out, "tables", "tab_programs.tex"), "w").write(tex)
    lo = min(rows, key=lambda p: int(rows[p]["peak_demand_d13"]))
    hi = max(rows, key=lambda p: int(rows[p]["peak_demand_d21"]))
    V.append(("programs.P_range", "%s %s d=13, %s %s d=21" % (rows[lo]["peak_demand_d13"], lo, rows[hi]["peak_demand_d21"], hi),
              "6: P ranges from 22 cores for QPE-5 at d=13 to 506 for Adder-32 at d=21"))
    ad = [int(rows[p]["t_gates"]) for p in ORDER if p.startswith("Adder")]
    ot = [int(rows[p]["t_gates"]) for p in ORDER if not p.startswith("Adder")]
    V.append(("programs.t_counts", "%s to %s against %s to %s" % (c(min(ad)), c(max(ad)), c(min(ot)), c(max(ot))), "7.1: 12 to 124 against 1,813 to 14,085"))
    V.append(("programs.budgets_checked", "%d" % n, "6: every budget is ceil(level x P) (88 configurations)"))
    # Compile time (7.3): every recovery script the runs used passed the check; the build of scripts and profiles.
    C = list(csv.DictReader(open(os.path.join(root, "results", "main", "compile_checks.tsv")), delimiter="\t"))
    V.append(("compile.scripts_checked", "{:,}".format(sum(int(r["scripts_checked"]) for r in C)), "7.3: all 77,295 scripts that our evaluation used passed this check"))
    Bt = list(csv.DictReader(open(os.path.join(root, "results", "compile", "build_times.tsv")), delimiter="\t"))
    tot = {(r["program"], r["d"]): float(r["script_compile_s"]) + float(r["profile_build_s"]) for r in Bt}
    w = max(tot, key=tot.get)
    V.append(("compile.max_seconds", "%.0f" % tot[w], "7.3: at most 471 s per program and distance on a single core"))
    return V
