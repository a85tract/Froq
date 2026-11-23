"""Table "cost" and the controller's numbers of Section 5.6, from results/hardware/.

blocks.tsv lists each block of a controller design with how its LUTs are counted: routed (the RTL placed and routed out
of context at 3.5 ns on the V80's FPGA, hw/), estimated with a band, or by unit cost; RAMB36 counts carry their own band.
"""
import csv
import os


def read(path):
    with open(path) as f:
        return [r for r in csv.DictReader((l for l in f if not l.startswith("#")), delimiter="\t")]


def num(x):
    return 0.0 if x in ("-", "") else float(x)


def k(x):
    return "%.1f" % (x / 1000.0)


def run(root, out):
    H = os.path.join(root, "results", "hardware")
    B = read(os.path.join(H, "blocks.tsv"))
    ser = [b for b in B if "static_reservation" in b["designs"].split(",")]
    routed = sum(num(b["luts"]) for b in ser if b["status"] == "routed")
    est = [b for b in ser if b["status"] == "estimated"]
    est_p, est_lo, est_hi = (sum(num(b[c]) for b in est) for c in ("luts", "luts_lo", "luts_hi"))
    unit = sum(num(b["luts"]) for b in ser if b["status"] == "unit-cost")
    ramb = sum(num(b["ramb36"]) for b in ser)
    assert all(num(b["ramb36"]) == 0 for b in ser if b["status"] != "routed")      # all of it in the routed decision unit
    S = {r["part"]: r for r in read(os.path.join(H, "splice_engine.tsv"))}
    st = read(os.path.join(H, "script_store.tsv"))
    store_luts = [l for l in open(os.path.join(H, "script_store.tsv")) if l.startswith("# store logic")][0]
    store_luts = float(store_luts.split(":")[1].split()[0])
    lr, tags = S["lut_ram"], S["hold_tags"]
    sp_unit = num(S["logic_unit_cost"]["luts"]) + num(S["rebase_unit_cost"]["luts"])
    ures = [int(r["uram288_resident"]) for r in st]
    g = lambda x: "%g" % x
    rows = [r"static reservation & \textbf{%d} RAMB36 & \textbf{%s} + %s (%s--{}%s) + \textit{%s} \\" % (ramb, k(routed), k(est_p), k(est_lo), k(est_hi), k(unit)),
            r"script store & %d to %d URAM288 & \textit{%s} \\" % (min(ures), max(ures), k(store_luts)),
            r"splice engine & %s (%s--{}%s) RAMB36 & %s (%s--{}%s) + \textit{%s} \\" % (g(num(tags["ramb36"])), g(num(tags["ramb36_lo"])), g(num(tags["ramb36_hi"])),
                                                                                    k(num(lr["luts"])), k(num(lr["luts_lo"])), k(num(lr["luts_hi"])), k(sp_unit))]
    tex = r"""\begin{table}[t]
\caption{Controller's cost on the V80 sums the routed part, the estimated part and the part counted by unit cost.}
\label{tab:cost}
\centering
\setlength{\tabcolsep}{4pt}
\resizebox{\columnwidth}{!}{%
\begin{tabular}{@{}lll@{}}
\toprule
\textbf{Block} & \textbf{Memory blocks} & \textbf{LUTs (k)} \\
\midrule
""" + "\n".join(rows) + r"""
\bottomrule
\end{tabular}}
\end{table}
"""
    os.makedirs(os.path.join(out, "tables"), exist_ok=True)
    open(os.path.join(out, "tables", "tab_cost.tex"), "w").write(tex)
    V = []
    add = lambda a, b, c: V.append((a, b, c))
    du = [b for b in B if b["block"] == "decision_unit"][0]
    guarded = sum(num(b["luts"]) for b in B if "guarded_runtime" in b["designs"].split(",") and b["status"] == "routed")
    add("hw.decision_unit_luts", "{:,.0f}".format(num(du["luts"])), "5.6: the decision unit takes 3,146 LUTs")
    add("hw.guarded_vs_static", "%s k against %s k (%.0f times)" % (k(guarded), k(routed), guarded / routed), "7.3: about 13 times the logic, 241.1 k against 18.7 k LUTs")
    L = {r["path"]: r for r in read(os.path.join(H, "latency.tsv"))}
    rc = int(L["registration"]["round_cycles"])
    add("hw.round_cycles", "%d" % rc, "5.6: a syndrome round of 1 us is 286 cycles at 3.5 ns")
    add("hw.read", L["dependent_read"]["cycles"], "5.6: a dependent access of 5 cycles")
    add("hw.registration", "%s cycles, %.1f %%" % (L["registration"]["cycles"], 100 * int(L["registration"]["cycles"]) / rc), "5.6: a registration takes 18 derived cycles, 6.3 % of a round")
    add("hw.settlement", "%s cycles, %.1f %%, %s routed, %s estimated" % (L["settlement"]["cycles"], 100 * int(L["settlement"]["cycles"]) / rc,
                                                                         L["settlement"]["routed_cycles"], L["settlement"]["estimated_cycles"]),
        "5.6: settlement 28 cycles, 9.8 % of a round, 18 routed and 10 estimated")
    add("hw.store_blocks", "%d to %d" % (min(ures), max(ures)), "5.6: the sizing method estimates 21 to 60 blocks by program")
    return V
