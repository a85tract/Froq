"""Arms: the named configurations a campaign compares.

An arm is a BASE configuration of the device plus keyword options for `froq.run.run_one`:
  base "N" SWIPER's sliding windows; a conditional S waits for its delivered decode (v5's N);
  base "Q" the same plus Froq: the engine may act on a prediction under the certified recovery contract (v5's Q).
`run_one` resolves an arm name here (`resolve`); the result file name and the fingerprint follow from the registry, so a
new arm needs no change to the runner or the analysis.

Adding an arm (the SWIPER client; policies and baselines):
  1. give `run_one` -- and the device or policy below it -- the keyword option the arm needs, defaulting to today's
     behaviour so that N and Q stay bit-identical;
  2. register the arm below, e.g. register("Qthr", "Q", "fixed confidence threshold", policy_mode="threshold");
  3. nothing else. Every option of a registered arm lands in the fingerprint's policy part (with the arm's name), and
     the arm's name replaces the base in the result file name.
Arm options apply to that arm's runs only. Settings that change what BOTH arms of a pair see -- the client's actual
accuracy q, its publication delay, its error model, the path delay, the round duration -- are unit settings, not arm
options: the campaign runner applies them to the N and the Q unit of a pair alike (froq/tools/campaign.py).
A Q-based arm needs the training profile of its (program, d) (the seed-1 N run); an N-based arm does not."""

ARMS: dict = {}


def register(name: str, base: str, doc: str, **options) -> None:
    """Add an arm.  `options` are `run_one` keyword arguments (JSON-serialisable values)."""
    assert base in ("N", "Q", "D"), "an arm's base configuration is N, Q or D (the deferral baseline)"
    assert name not in ARMS, "arm %s is registered already" % name
    assert name.isidentifier() and "_" not in name, "arm names are letters and digits (they sit between underscores in file names)"
    assert base == name or options, "a registered arm other than N and Q must set at least one option"
    ARMS[name] = dict(base=base, doc=doc, options=dict(options))


def resolve(name: str) -> tuple:
    """(base configuration, run_one options) of an arm name."""
    if name not in ARMS:
        raise KeyError("unknown arm %r (registered: %s)" % (name, ", ".join(sorted(ARMS))))
    a = ARMS[name]
    return a["base"], dict(a["options"])


register("N", "N", "SWIPER sliding windows; the conditional S waits for its delivered decode (v5's N)")
register("Q", "Q", "Froq with the priced DP policy (v5's Q)")
register("Qfwd", "Q", "Froq, FORWARD at every cell the contract admits (always-forward; froq.run --policy-mode forward)",
         policy_mode="forward")

# the policies on the same contracts: the threshold grid of gap-analysis
# rule 4 (the tuned theta is one of them, recorded) and the greedy rule.
for _t in (50, 60, 70, 75, 80, 85, 90, 95):
    register("Qthr%d" % _t, "Q", "Froq, FORWARD iff the advertised confidence >= %.2f (froq.run --policy-mode threshold)" % (_t / 100),
             policy_mode="threshold", theta=_t / 100)
register("Qgrd", "Q", "Froq, the greedy one-step rule (policy.solve_greedy; froq.run --policy-mode greedy)", policy_mode="greedy")
# the baselines: undo-and-redo (its Q runs need a profile trained with --recovery undo_redo) and deferral (arm D)
register("Qur", "Q", "Froq's DP with the undo-and-redo recovery of ARTERY / Stalls and Spequlation (E2E_RECOVERY=undo_redo)",
         recovery="undo_redo")
register("D", "D", "Clifford-frame deferral, lazy rule")
register("Dnci", "D", "Clifford-frame deferral, the no-cost-increase rule", defer=dict(rule="no_cost_increase"))
register("Deager", "D", "Clifford-frame deferral, eager rule (ablation)", defer=dict(rule="eager"))
register("Dsettle", "D", "Clifford-frame deferral, lazy rule with Froq's decode hold (ablation)", defer=dict(settle="decode"))
# controller settings (simulated; the RTL's are horizon 256, 64 site classes, one n_s per site, saturating Vhat)
register("Qh128", "Q", "Froq, policy horizon 128 rounds", horizon=128)
register("Qh512", "Q", "Froq, policy horizon 512 rounds", horizon=512)
register("Qpc32", "Q", "Froq, 32 site (price) classes", codebook_classes=32)
register("Qpc128", "Q", "Froq, 128 site (price) classes (training with --codebook-record 128)", codebook_classes=128)
register("Qnsb", "Q", "Froq, waiting cost n(p) per boundary (policy.solve_variant)", ns_mode="boundary")
register("Qunsat", "Q", "Froq, the solve without the Q22.24 saturation (policy.solve_variant)", saturate=False)
# gap analysis rule 15: Froq's primary controller (the table-free
# threshold at the modeled client's tuned 0.9) with one mechanism removed each. Q-nomeas and Q-v5scripts change
# the contracts' shapes or prices, so their Q runs need profiles trained under the same setting (`froq.run --contracts
# nomeas` / `--library v5` on the seed-1 N run); Q-K1, Q-K2 and Q-nores use the standard profile.
_THR = dict(policy_mode="threshold", theta=0.9)
register("Qthr90nomeas", "Q", "Froq (threshold 0.9) whose contracts end at any gadget that contains a measurement", contracts="nomeas", **_THR)
register("Qthr90v5", "Q", "Froq (threshold 0.9) with v5's compensation scripts instead of the shortened ones", library="v5", **_THR)
register("Qthr90K1", "Q", "Froq (threshold 0.9) with contract depth K = 1", K=1, **_THR)
register("Qthr90K2", "Q", "Froq (threshold 0.9) with contract depth K = 2", K=2, **_THR)
register("Qthr90nores", "Q", "Froq (threshold 0.9) without workspace reservation: a recovery allocates on demand", reservation=False, **_THR)
# gap analysis rule 22: the hybrid mode H, arm D-lazy plus an early materialization when a
# prediction arrives with m = 1 and confidence >= theta, theta being the client's rule-4 value (0.9 for the modeled
# client, 0.85 for the SWIPER client; the client is a unit setting).  Hsilent takes no prediction: validation gate (a).
register("Hthr90", "D", "hybrid mode H: deferral, early materialization on a confident m = 1 prediction, theta 0.9 (modeled client)",
         defer=dict(hybrid=dict(theta=0.9, source="client")))
register("Hthr85", "D", "hybrid mode H: deferral, early materialization on a confident m = 1 prediction, theta 0.85 (SWIPER client)",
         defer=dict(hybrid=dict(theta=0.85, source="client")))
register("Hsilent", "D", "hybrid mode H with a client that never publishes to it (gate (a): must reproduce D-lazy)",
         defer=dict(hybrid=dict(theta=0.9, source="none")))
# H-thr+strip(M): theta 0.95, the strip client's rule-4 value (phase 1: every advertised c >= 0.998639,
# so the eight thresholds tie and the tie rule picks 0.95, Qthr95).
register("Hthr95", "D", "hybrid mode H: deferral, early materialization on a confident m = 1 prediction, theta 0.95 (strip client)",
         defer=dict(hybrid=dict(theta=0.95, source="client")))
# A diagnostic of the design, not an arm of rule 22: the prediction used in the round it
# is published (no one-round install), so the trigger can hold at registration, before step 0 begins (boundary 0).
register("Hthr90i0", "D", "diagnostic: H at theta 0.9 with the prediction used in its publication round (no install round)",
         defer=dict(hybrid=dict(theta=0.9, source="client", install=0)))
register("Hthr85i0", "D", "diagnostic: H at theta 0.85 with the prediction used in its publication round (no install round)",
         defer=dict(hybrid=dict(theta=0.85, source="client", install=0)))
# (gap analysis rule 30): round 3's four H3 candidates, at each client's round-2 theta (0.9 modeled, 0.85 SWIPER,
# 0.95 strip).  H-nci carries its frames under D-nci's rule in every carry phase, before and after rebasing; H-lead
# triggers only if the causal estimate of the rounds to the verdict exceeds lambda = d (froq/verdict_eta.py).  The silent
# variants never take a prediction: each must reproduce its deferral rule (D-lazy for H-lead, D-nci for H-nci and
# H-nci-lead), as Hsilent reproduces D-lazy for H.
_NCI = dict(rule="no_cost_increase")
for _t in (85, 90, 95):
    _h = dict(theta=_t / 100, source="client")
    register("Hnci%d" % _t, "D", "H-nci: H under D-nci's admission rule, before and after rebasing, theta %.2f" % (_t / 100),
             defer=dict(_NCI, hybrid=dict(_h)))
    register("Hlead%d" % _t, "D", "H-lead: H triggering only with an estimated verdict lead above lambda = d, theta %.2f" % (_t / 100),
             defer=dict(hybrid=dict(_h, lead="d")))
    register("Hncilead%d" % _t, "D", "H-nci-lead: H-nci with H-lead's trigger filter (lambda = d), theta %.2f" % (_t / 100),
             defer=dict(_NCI, hybrid=dict(_h, lead="d")))
register("Hncisilent", "D", "H-nci with a client that never publishes to it (must reproduce D-nci)",
         defer=dict(_NCI, hybrid=dict(theta=0.9, source="none")))
register("Hleadsilent", "D", "H-lead with a client that never publishes to it (must reproduce D-lazy)",
         defer=dict(hybrid=dict(theta=0.9, source="none", lead="d")))
register("Hncileadsilent", "D", "H-nci-lead with a client that never publishes to it (must reproduce D-nci)",
         defer=dict(_NCI, hybrid=dict(theta=0.9, source="none", lead="d")))
# part C (gap analysis rules 41-43, Parts E-G; hardware contract U-contract.md): U,
# the unified policy -- D-nci's carry, with two epochs (E1 at installation at b*, E2 at D-nci's stop) at which U
# unit answers ACT or CARRY by its eligibility checks and fixed-point expected-cost score with margin delta in rounds; a
# decision takes effect act_lat = 7 rounds after its request.  Uinf makes no request and runs no strip job: D-nci exactly
# (the selection's fallback).  Ucarry is rule 41d's all-CARRY control (strip jobs on, every request answered CARRY).  The
# L4 and L1 arms are rule 41b's latency sensitivities.
for _n, _delta in (("U0", "0"), ("Ud4", "d/4"), ("Ud2", "d/2"), ("Uinf", "inf")):
    register(_n, "D", "U: D-nci with expected-cost early materialization at installation or at D-nci's stop, delta = %s" % _delta,
             defer=dict(_NCI, unified=dict(delta=_delta)))
register("Ucarry", "D", "U's all-CARRY control: the strip client and U's requests on, every request answered CARRY (rule 41d)",
         defer=dict(_NCI, unified=dict(delta="carry")))
for _lat in (4, 1):
    for _n, _delta in (("U0", "0"), ("Ud4", "d/4"), ("Ud2", "d/2")):
        register("%sL%d" % (_n, _lat), "D", "U at delta = %s with act_lat = %d rounds (rule 41b's sensitivity)" % (_delta, _lat),
                 defer=dict(_NCI, unified=dict(delta=_delta, latency=_lat)))
# U v4 (gap analysis Part H, rule 44; section 13): the carry rule chosen per value at its
# registration -- lazy if the pool's idle cores, net of reservations and of earlier lazy frames' outstanding
# materialization demand, are at least h x demand_lazy(class), D-nci's rule otherwise -- every m_hat = 1 value
# registered (frames at their stop included, boundary 0 among them), and the gains with the stall term (rule 44c).
# The grid of rule 44e: h in {0, 1, inf} x delta in {0, d/2, inf}.  U4h0dinf is D-lazy and U4hinfdinf is D-nci (no ACT,
# no strip job), each bit for bit; h = 0 carries every frame lazily, h = inf under D-nci's rule.
for _hn, _h in (("0", "0"), ("1", "1"), ("inf", "inf")):
    for _dn, _delta in (("0", "0"), ("2", "d/2"), ("inf", "inf")):
        register("U4h%sd%s" % (_hn, _dn), "D", "U v4: the carry rule by headroom h = %s, gains with the stall term, delta = %s" % (_h, _delta),
                 defer=dict(rule=("lazy" if _h == "0" else "no_cost_increase"), unified=dict(delta=_delta, h=_h)))
# (gap analysis Part I, rule 41b'): one latency convention for both decision paths of the controller. The main
# convention charges the threshold path's installation (REGISTER) decisions 5 rounds, ceil((896 + 286) / 286), and its
# RECONFIDENCE decisions 8; the original protocol (Hthr95, Qthr95: 1 and 1, H's rule of B.10) is the audit
# comparison.  Rule 44e' runs the references at both.
register("Hthr95T5", "D", "hybrid mode H at theta 0.95 (strip client), the threshold path's main convention: installation 5 rounds, RECONFIDENCE 8",
         defer=dict(hybrid=dict(theta=0.95, source="client")), thr_install_lat=5, thr_rc_lat=8)
register("Qthr95T5", "Q", "Froq at threshold 0.95, the threshold path's main convention: installation 5 rounds, RECONFIDENCE 8",
         policy_mode="threshold", theta=0.95, thr_install_lat=5, thr_rc_lat=8)
# (gap analysis rules 43 and 43b): Q-thr at frozen threshold 0.9, with the threshold path's 5 rounds.
register("Qthr90T5", "Q", "Froq at threshold 0.9 (comparator, rule 43), the threshold path's main convention: installation 5 rounds, RECONFIDENCE 8",
         policy_mode="threshold", theta=0.9, thr_install_lat=5, thr_rc_lat=8)
# (gap analysis Part M, rule 33a): the ablations of the headline U4(1, d/2)+strip(B), each removing one
# mechanism at equal resources -- the runner gives them the headline's unit settings (the strip client on the shared
# preemptive pool, SWIPER routing, act_lat 7); always lazy and always nci are U4h0d2 and U4hinfd2 above.  E1 off and E2
# off drop one epoch (E2 off also drops E1's comparison with E2's gain); U4h1carry answers every request CARRY with the
# strip jobs and the requests on (not delta = inf, which stops the strip jobs); noW sets W_b = 0 in 44c's gain, keeping
# the ETA requirement and the tail term; np runs on 42a's non-preemptive pool.
_U4H = dict(rule="no_cost_increase", unified=dict(delta="d/2", h="1"))
register("U4h1d2noE1", "D", "U4(1, d/2) without E1 (rule 33a's ablation 1)",
         defer=dict(_U4H, unified=dict(_U4H["unified"], e1=False)))
register("U4h1d2noE2", "D", "U4(1, d/2) without E2, and E1 without the comparison with E2's gain (rule 33a's ablation 2)",
         defer=dict(_U4H, unified=dict(_U4H["unified"], e2=False)))
register("U4h1carry", "D", "U4(1)'s all-CARRY control: strip jobs and requests on, every request answered CARRY (rule 33a's ablation 3)",
         defer=dict(_U4H, unified=dict(delta="carry", h="1")))
register("U4h1d2noW", "D", "U4(1, d/2) without the stall term: W_b = 0 at both epochs (rule 33a's ablation 6)",
         defer=dict(_U4H, unified=dict(_U4H["unified"], nostall=True)))
register("U4h1d2np", "D", "U4(1, d/2) on 42a's non-preemptive pool, shared-np (rule 33a's ablation 7)",
         defer=dict(_U4H), pool="shared-np")
# controlled comparison (rule 33a as amended at 22:42): U4(1, d/2)+strip and Qthr95T5+strip, each with the total
# horizon K_tot = 4 steps per value (rule 33's fallback; U: `defer.k_tot`, the prefix before a splice and the rebased
# steps together; Q: the contract depth K, Q's contracts not rebasing; section 11), and each limited
# to the static common eligibility set "c028" of its (program, d) (`common`: run.common_set_of's file in the run's
# output directory, beside Q's training profile).  Q-thr runs with its native routing (rule 48), and with SWIPER's as
# the sensitivity (the runner's planner setting, which brings f_flip).
register("U4h1d2kt4", "D", "U4(1, d/2) with K_tot = 4 steps per value, on common set (rule 33a's controlled comparison)",
         defer=dict(_U4H, k_tot=4), common="c028")
register("Qthr95T5K4", "Q", "Q-thr at threshold 0.95, the main latency convention, contract depth K = 4, on common set (rule 33a's controlled comparison)",
         policy_mode="threshold", theta=0.95, thr_install_lat=5, thr_rc_lat=8, K=4, common="c028")
# deploy 3: the
# headline U4(1, d/2) at act_lat 4 and 1 rounds, rule 41b''s latency sensitivity; U4h1d2's options with `unified.latency`.
register("U4h1d2L4", "D", "U v4 at h = 1, delta = d/2, with act_lat = 4 rounds (rule 41b''s latency sensitivity, deploy 3)",
         defer=dict(rule="no_cost_increase", unified=dict(delta="d/2", h="1", latency=4)))
register("U4h1d2L1", "D", "U v4 at h = 1, delta = d/2, with act_lat = 1 round (rule 41b''s latency sensitivity, deploy 3)",
         defer=dict(rule="no_cost_increase", unified=dict(delta="d/2", h="1", latency=1)))
# U v6 (round: gap analysis `` on `scoutq`, rules 50-52, 54 and 56;
# froq/u6.py). Every U v6 arm is U v4's controller (h = 1, D-nci's walk as the base) with `v6`
# options; each is a configuration of the one controller.  U6_PARAMS are U6's own parameters, set by the seed-1
# selection (rule 50s with 50b-50d, re-selection under 50d, -38): U6c0k1w0's -- 51c off, c = 1, w_s = 0, H =
# 0 -- pending the selection's results check before F_4; U6-noact, U6-off and T6 take them too.  kappa_c is rule 50a's
# c: kappa = c x the mean full-decode service time of the value's class (froq/u6.py); ws is rule 50d's share of C_s.
U6_PARAMS = dict(mhat0=False, kappa_c=1.0, H="0", ws=0.0)
_U6 = lambda delta="d/2", **v6: dict(defer=dict(rule="no_cost_increase", unified=dict(delta=delta, h="1", v6=dict(U6_PARAMS, **v6))))
register("U6", "D", "U v6: the carry rule by time (rule 50), 51a-51c, Eq. (1)'s repair terms, the closed loop (rule 52); "
         "its parameters U6_PARAMS (set by the selection)", **_U6())
register("U6noact", "D", "U6-noact: U6 with its actions off (delta = inf: no EPOCH request), the client: strip jobs,"
         "registrations and p_corr as U6's", **_U6("noact"))
register("U6off", "D", "U6-off: adaptive deferral alone -- the client off (no strip job, prediction or request; p_corr = the base rate), "
         "U's ETA file on for the carry rule", **_U6(client=False))
register("T6", "D", "T6: U6 with the threshold c >= theta in place of U's score (rule 54), its decision opportunities U6's; "
         "theta = 0.5, rule 4's first grid value: the strip client's c (0.9986 to 0.9998) never meets the threshold, so T6 "
         "acts on every eligible value (rule 50b)", **_U6(theta=0.5))
# rule 50b's diagnosis: U6-off with (a) the frames D-nci would not make (its walk stopping at boundary 0) left
# undeferred, (b) the lazy walk refused, and both -- counterfactuals for the diagnosis only (froq/u6.py, U6_CF)
register("U6offCFa", "D", "U6-off with its D-nci-walk frames that stop at boundary 0 left undeferred ('s counterfactual (a))",
         **_U6(client=False, cf_stop0=True))
register("U6offCFb", "D", "U6-off with the lazy walk refused ('s counterfactual (b))", **_U6(client=False, cf_nolazy=True))
register("U6offCFab", "D", "U6-off with both of 's counterfactuals, (a) and (b)", **_U6(client=False, cf_stop0=True, cf_nolazy=True))
register("T6t095", "D", "T6 at theta = 0.95, the grid's last value: rule 50b's check that theta never binds at the outcome",
         **_U6(theta=0.95))
# rule 50s's candidates by name (rule 50a's grid): 51c off (c0) or on (c1) x c = 0.25, 1, 4 service times per core
# (k025, k1, k4), H = 0, and each with H = d/4 (Hd4) or d/2 (Hd2) for the H stage (two of these twelve run, at the best
# of the six)
U6_C_GRID = (("025", 0.25), ("1", 1.0), ("4", 4.0))
for _c in (0, 1):
    for _kn, _k in U6_C_GRID:
        for _hn, _h in (("", "0"), ("Hd4", "d/4"), ("Hd2", "d/2")):
            register("U6c%dk%s%s" % (_c, _kn, _hn), "D", "rule 50s candidate: 51c %s, kappa = %s x mu, H = %s" % (("off", "on")[_c], _k, _h),
                     defer=dict(rule="no_cost_increase", unified=dict(delta="d/2", h="1",
                                                                      v6=dict(mhat0=bool(_c), kappa_c=_k, H=_h))))
# rule 50d's stage-1 candidates: 51c off (c0) or on (c1) x c = 0.25, 1, 4 (k025, k1, k4) x C_s's critical-path share w_s
# = 0, 0.5, 1 (w0, w05, w1), H = 0, and each with H = d/4 (Hd4) or d/2 (Hd2) for the H stage; all with 50d's prior
U6_WS_GRID = (("0", 0.0), ("05", 0.5), ("1", 1.0))
for _c in (0, 1):
    for _kn, _k in U6_C_GRID:
        for _wn, _w in U6_WS_GRID:
            for _hn, _h in (("", "0"), ("Hd4", "d/4"), ("Hd2", "d/2")):
                register("U6c%dk%sw%s%s" % (_c, _kn, _wn, _hn), "D", "rule 50d candidate: 51c %s, kappa = %s x mu, w_s = %s, H = %s"
                         % (("off", "on")[_c], _k, _w, _h), defer=dict(rule="no_cost_increase", unified=dict(
                             delta="d/2", h="1", v6=dict(mhat0=bool(_c), kappa_c=_k, H=_h, ws=_w))))
register("U6c0k1cs0", "D", "U6c0k1 with C_s off (cs = False): rule 50d's equivalence pairs, w_s = 0 against C_s off",
         defer=dict(rule="no_cost_increase", unified=dict(delta="d/2", h="1", v6=dict(mhat0=False, kappa_c=1.0, H="0", cs=False))))
# U v4 through U v6's code, with rule 50's U v4 conditions (W = 0, H = 0, p_corr = 1, no reconsideration; rule 50c's
# instantaneous slack, C_s off and no stop-0 cleanup, so 50c does not touch them): R, the guard's
# reference (round 4's closed loop and Eq. (1)'s repair terms), the fallback chain's first step (R with 51a and 51b), and
# the equivalence arm of rule 59 (51a-51c off, no repair terms, round 3's instantaneous u_xs: U4h1d2 bit for bit)
_V4 = dict(w="zero", pcorr="one", H="0", kappa_c=1.0, mhat0=False, reconsider=False, slack="instant", cs=False, stop0_cleanup=False)
register("U4R", "D", "R: U v4 under round 4's conventions (closed loop) with Eq. (1)'s repair terms (rule 50s's guard)",
         defer=dict(rule="no_cost_increase", unified=dict(delta="d/2", h="1", v6=dict(_V4, q_wait=False, eta_floor=False, repair=True, loop="closed"))))
register("U4F", "D", "the fallback: U v4 with 51a, 51b and Eq. (1)'s repair terms, closed loop (rule 50s)",
         defer=dict(rule="no_cost_increase", unified=dict(delta="d/2", h="1", v6=dict(_V4, q_wait=True, eta_floor=True, repair=True, loop="closed"))))
register("U6v4", "D", "rule 59's equivalence arm: U v4 through U v6's code (W = 0, H = 0, p_corr = 1, 51a-51c and the repair "
         "terms off, round 3's instantaneous u_xs); must equal U4h1d2 bit for bit",
         defer=dict(rule="no_cost_increase", unified=dict(delta="d/2", h="1", v6=dict(_V4, q_wait=False, eta_floor=False, repair=False, loop="instant"))))
# rule 52's sensitivity: round 3's instantaneous u_xs against the closed loop, U6 and U6-noact
register("U6inst", "D", "U6 with round 3's instantaneous u_xs (rule 52's sensitivity)", **_U6(loop="instant"))
register("U6noactinst", "D", "U6-noact with round 3's instantaneous u_xs (rule 52's sensitivity)", **_U6("noact", loop="instant"))
# T6's theta training (rule 50s, by round 1's rule 4: theta in {0.5, ..., 0.95} on seed 1) at the selection's outcome.  The
# selection fell back to R (none of the eight candidates and not U4F passes the guard), so T6 is R with the
# threshold c >= theta in place of U's score (rule 54): T6Rt050.. T6Rt095.
_R6 = dict(_V4, q_wait=False, eta_floor=False, repair=True, loop="closed")
for _tn, _t in (("050", 0.5), ("060", 0.6), ("070", 0.7), ("075", 0.75), ("080", 0.8), ("085", 0.85), ("090", 0.9), ("095", 0.95)):
    register("T6Rt" + _tn, "D", "T6 on R: R with the threshold c >= %s in place of U's score (rule 54; T6's theta grid after "
             "the selection's fallback to R)" % _t, defer=dict(rule="no_cost_increase", unified=dict(delta="d/2", h="1", v6=dict(_R6, theta=_t))))
# Rules 61-66: the decision (froq/wager2.py), D's frames
# with v5's recurrence in critical-path rounds; its selection grid (section 11): the age bins uniform (u) or equal-mass
# (q) x c_kappa 0.25, 1, 4 (k025, k1, k4) x the horizon H_a (a, q99) or H_b (b, q99.9); greedy (section 10) on the same
# grid; the exact cases' complete settings (section 7); and the decisive baselines FAC-A and FAC-F (section 10):
# U6c0k1w0 with the demand weight h = 0 (D-lazy's walk for every frame) and h = inf (D-nci's walk).
_W2_K = (("025", 0.25), ("1", 1.0), ("4", 4.0))
for _b in ("u", "q"):
    for _kn, _k in _W2_K:
        for _hz in ("a", "b"):
            register("W2%sk%s%s" % (_b, _kn, _hz), "D", "the decision: %s age bins, c_kappa = %s, the horizon H_%s (section 11's grid)"
                     % ({"u": "uniform", "q": "equal-mass"}[_b], _k, _hz), defer=dict(rule="lazy", wager2=dict(bins=_b, ckappa=_k, horizon=_hz)))
            register("W2G%sk%s%s" % (_b, _kn, _hz), "D", "the decision's greedy baseline (one action, then wait): %s bins, c_kappa = %s, H_%s"
                     % ({"u": "uniform", "q": "equal-mass"}[_b], _k, _hz),
                     defer=dict(rule="lazy", wager2=dict(bins=_b, ckappa=_k, horizon=_hz, greedy=True)))
# the spec's amendment, after the grid's selection failed: the re-selection's 3
# candidates, each with the c0 COMMIT ban, kappa per missing core = c_kappa x mu / B, uniform bins and H_a (W2P...), and
# section 10's greedy setting of each (W2GP...)
for _kn, _k in _W2_K:
    _amend = dict(bins="u", ckappa=_k, horizon="a", c0ban=True, kscale="mu/B")
    register("W2Puk%sa" % _kn, "D", "the decision as amended (no COMMIT at c0, kappa = c_kappa mu / B): uniform bins, c_kappa = %s,"
             "H_a (the re-selection's candidates)" % _k, defer=dict(rule="lazy", wager2=_amend))
    register("W2GPuk%sa" % _kn, "D", "the decision's greedy baseline as amended (no COMMIT at c0, kappa = c_kappa mu / B): uniform"
             "bins, c_kappa = %s, H_a" % _k, defer=dict(rule="lazy", wager2=dict(_amend, greedy=True)))
# the repair R1-R3' on the amendment's settings (uniform bins, H_a,
# the c0 ban, kappa = c_kappa mu / B). R1's admission horizon is the decision's act_lat, 7 rounds (froq.device.U_LATENCY). The
# diagnostics W2X1 (R1) and W2X12 (R1 + R2); the candidates A (R3', c_kappa 0.25 / 1 / 4), B (lambda 0.5 / 1 / 2) and C
# (lambda 1 and R3'), each with R1 and R2 (W2X...); and each candidate without R1 (W2Y...), R1's fallback (16.1).
_R1H = 7
_W2X = dict(bins="u", horizon="a", c0ban=True, kscale="mu/B")
register("W2X1", "D", "the decision repaired, diagnostic: R1 (the booked COMMIT, horizon 7 rounds) on W2Puk1a's settings",
         defer=dict(rule="lazy", wager2=dict(_W2X, ckappa=1.0, r1=_R1H)))
register("W2X12", "D", "the decision repaired, diagnostic: R1 and R2 (prices from froq/data/wager2_r2.json) on W2Puk1a's settings",
         defer=dict(rule="lazy", wager2=dict(_W2X, ckappa=1.0, r1=_R1H, r2=True)))
for _p, _r1 in (("W2X", _R1H), ("W2Y", 0)):
    _with = "R1, R2" if _r1 else "R2 (R1's fallback: no R1)"
    for _kn, _k in _W2_K:
        register(_p + "Ak" + _kn, "D", "the decision repaired, candidate A: %s and R3' (beta = 0 within D-nci's stop), c_kappa = %s"
                 % (_with, _k), defer=dict(rule="lazy", wager2=dict(_W2X, ckappa=_k, r1=_r1, r2=True, scope0="nci")))
    for _ln, _l in (("05", 0.5), ("1", 1.0), ("2", 2.0)):
        register(_p + "Bl" + _ln, "D", "the decision repaired, candidate B: %s and R3 (lambda = %s), c_kappa = 1" % (_with, _l),
                 defer=dict(rule="lazy", wager2=dict(_W2X, ckappa=1.0, r1=_r1, r2=True, lam=_l)))
    register(_p + "C", "D", "the decision repaired, candidate C: %s, R3 (lambda = 1) and R3', c_kappa = 1" % _with,
             defer=dict(rule="lazy", wager2=dict(_W2X, ckappa=1.0, r1=_r1, r2=True, lam=1.0, scope0="nci")))
for _ln, _l in (("1", 1.0), ("2", 2.0)):                          # iteration 1 (spec 16.7): R4 on W2YBl1 and W2YBl2
    register("W2YBl" + _ln + "e", "D", "the decision repaired, iteration 1: W2YBl%s (R2 and R3, lambda = %s, c_kappa = 1) with R4,"
             "the eta-indexed table" % (_ln, _l), defer=dict(rule="lazy", wager2=dict(_W2X, ckappa=1.0, r1=0, r2=True, lam=_l, r4=True)))
for _ln, _l in (("1", 1.0), ("2", 2.0)):                          # iteration 1 as redefined (spec 16.8): R5
    register("W2YBl" + _ln + "s", "D", "the decision repaired, iteration 1 (redefined): W2YBl%s (R2 and R3, lambda = %s, c_kappa ="
             "1) with R5, saturation pricing" % (_ln, _l), defer=dict(rule="lazy", wager2=dict(_W2X, ckappa=1.0, r1=0, r2=True, lam=_l, r5=True)))
for _xm in ("cell", "program"):                                   # iteration 2 (spec 16.9): xi_C on W2YBl1
    register("W2YBl1" + _xm[0], "D", "the decision repaired, iteration 2: W2YBl1 (R2 and R3, lambda = 1, c_kappa = 1) with xi_C,"
             "the settlement price of an early COMMIT, per %s" % ("(d, budget level)" if _xm == "cell" else "(program, d, budget level)"),
             defer=dict(rule="lazy", wager2=dict(_W2X, ckappa=1.0, r1=0, r2=True, lam=1.0, xic=_xm)))
for _xm in ("cell", "program"):                                   # iteration 2, combined (plan 18; spec 16.8-16.10)
    for _sx, _p1 in (("s", False), ("b", True)):                  # (s: R5 and xi_C; b: the same with P1)
        register("W2YBl1" + _sx + _xm[0], "D", "the decision repaired, iteration 2 combined: W2YBl1 (R2 and R3, lambda = 1,"
                 "c_kappa = 1) with R5 and xi_C per %s%s" % ("(d, budget level)" if _xm == "cell" else "(program, d, budget level)",
                                                              ", and P1, the backlog load level" if _p1 else ""),
                 defer=dict(rule="lazy", wager2=dict(_W2X, ckappa=1.0, r1=0, r2=True, lam=1.0, r5=True, xic=_xm,
                                                     **({"p1": True} if _p1 else {}))))
register("W2AA", "D", "the decision's Always Advance setting (rho = 0, kappa = 0, COMMIT not authorised, c0): must equal D-lazy",
         defer=dict(rule="lazy", wager2=dict(setting="aa")))
register("W2AWF", "D", "the decision's Advance While Flat setting (the same, capped at D-nci's stop): must equal D-nci",
         defer=dict(rule="no_cost_increase", wager2=dict(setting="awf")))
register("W2WFD", "D", "the decision's Wait-for-Decoder setting (registration disabled, N's planner branches): must equal N",
         defer=dict(rule="lazy", wager2=dict(setting="wfd")))
register("FACA", "D", "FAC-A: U6c0k1w0 with h = 0 -- D-lazy's walk for every frame, the decision's commit score (section 10)",
         defer=dict(rule="lazy", unified=dict(delta="d/2", h="0", v6=dict(U6_PARAMS))))
register("FACF", "D", "FAC-F: U6c0k1w0 with h = inf -- D-nci's walk for every frame, the decision's commit score (section 10)",
         defer=dict(rule="no_cost_increase", unified=dict(delta="d/2", h="inf", v6=dict(U6_PARAMS))))
# round 6 (with amendment 1; deploy r6), exploratory: each arm is a registered arm's options
# with one change. B: FAC-A, FAC-F and W1 (U6c0k1w0) at the decision's latency, act_lat = 1 round from request to effect,
# with the eligibility horizon (51a's queue check and the stale scan) held at U_LATENCY = 7 (`unified.elig`).
# Amendment 1.1: FAC-A and FAC-F with Eq. (1)'s XS and XR (xi_mat, xi_rep) priced at 0 in the commit score alone (v6
# option `xi0`; the tables, classes and records keep them).  Amendment 1.2: V2* (W2YBl1sp), FAC-A and FAC-F with arm D's
# decode hold (Dsettle's settle = "decode").
def _r6(src, unified=None, v6=None, **defer):
    import copy
    o = copy.deepcopy(ARMS[src]["options"])
    o["defer"].update(defer)
    if unified:
        o["defer"]["unified"].update(unified)
    if v6:
        o["defer"]["unified"]["v6"].update(v6)
    return o
for _src in ("FACA", "FACF", "U6c0k1w0"):
    register(_src + "L1", "D", "%s at equal latency (round 6, B): act_lat = 1 round, eligibility held at 7" % _src,
             **_r6(_src, unified=dict(latency=1, elig=7)))
for _src in ("FACA", "FACF"):
    register(_src + "X0", "D", "%s with xi_mat = xi_rep = 0 in the commit score (round 6, amendment 1.1)" % _src, **_r6(_src, v6=dict(xi0=True)))
for _src in ("W2YBl1sp", "FACA", "FACF", "U6c0k1w0"):
    register(_src + "Sd", "D", "%s with arm D's decode hold, Dsettle's settle = decode (round 6, amendment 1.2)" % _src,
             **_r6(_src, settle="decode"))
# round 6 plan v2: a registered arm ignores --defer-settle, so each combination of the
# hold with B's equal latency is its own registration: settle = decode, act_lat = 1 round, eligibility held at 7.
for _src in ("FACA", "FACF", "U6c0k1w0"):
    register(_src + "SdL1", "D", "%s with arm D's decode hold at equal latency (round 6 plan v2): settle = decode, act_lat = 1 round, "
             "eligibility held at 7" % _src, **_r6(_src, settle="decode", unified=dict(latency=1, elig=7)))
# round 6 plan v3: the fit sources' hold versions, for the refit under the hold
# Lambda's D-nci, R2's W2Puk1a and xi_C's W2YBl1, each with
# settle = decode.  N, xi_C's other source, has no hold.
for _src in ("Dnci", "W2Puk1a", "W2YBl1"):
    register(_src + "Sd", "D", "%s with arm D's decode hold, Dsettle's settle = decode: a fit source under the hold (round 6 "
             "plan v3)" % _src, **_r6(_src, settle="decode"))
# round 7. V2q-Sd (3(a)): V2*-Sd (W2YBl1spSd) with the backlog level b as a table
# index, the delivery priced XS_k + Delta(l, b, k) and the admission V(0 | c0, l, b) + m < J_N; m = inf registers nothing
# (Wait-for-Decoder's path), m0-m3 are the fit file's margins (`--w2-vq`).  Its removal settings (3(d)), each with tables and
# admission bits rebuilt from the same fit: noM (the contracts end at any measurement, X1), noA (ADVANCE at +inf), noC
# (COMMIT at +inf), D0 (Delta = 0).  QG (3(b)): FAC-F-Sd-L1 whose values register only if the decoder backlog is at most
# theta; never registers nothing (N's path), always everything.  N-nospec (3(b)): SWIPER's sliding windows, speculation off.
def _r7(src, w2=None, **defer):
    import copy
    o = copy.deepcopy(ARMS[src]["options"])
    o["defer"].update(defer)
    if w2:
        o["defer"]["wager2"].update(w2)
    return o
register("V2qSdMinf", "D", "V2q-Sd at m = inf (round 7, 3(a)): nothing registers, Wait-for-Decoder's path; must equal N",
         **_r7("W2YBl1spSd", w2=dict(vq="inf")))
for _m in ("m0", "m1", "m2", "m3"):
    _M = _m.upper()
    register("V2qSd" + _M, "D", "V2q-Sd at the fit file's margin %s (round 7, 3(a))" % _m, **_r7("W2YBl1spSd", w2=dict(vq=_m)))
    register("V2qSd" + _M + "noM", "D", "V2q-noM at %s (round 7, 3(d)): contracts end at any gadget with a measurement" % _m,
             contracts="nomeas_all", **_r7("W2YBl1spSd", w2=dict(vq=_m)))
    register("V2qSd" + _M + "noA", "D", "V2q-noA at %s (round 7, 3(d)): ADVANCE priced at +inf, tables rebuilt" % _m,
             **_r7("W2YBl1spSd", w2=dict(vq=_m, noadv=True)))
    register("V2qSd" + _M + "noC", "D", "V2q-noC at %s (round 7, 3(d)): COMMIT priced at +inf, tables rebuilt" % _m,
             **_r7("W2YBl1spSd", w2=dict(vq=_m, nocom=True)))
    register("V2qSd" + _M + "D0", "D", "V2q-D0 at %s (round 7, 3(d)): Delta = 0, the admission kept, tables rebuilt" % _m,
             **_r7("W2YBl1spSd", w2=dict(vq=_m, vqd0=True)))
# round 7's amendment: V2qd-Sd is V2q-Sd with (a) J_N at N's own cost (N's
# correction and its decoding load, without the recovery's settlement XS or R2's W_0) and (b) the feasibility-aware
# admission (V(0 | c0, l, b, r), the walk capped at the r certified boundaries admissible at registration, r = 0
# rejected); its removal settings as V2q's.  V2qa-Sd and V2qb-Sd carry (a) or (b) alone, for the screen's matched
# comparisons; m = inf registers nothing, as V2q-Sd's.
_R7D = dict(vqjn=True, vqr=True)
register("V2qdSdMinf", "D", "V2qd-Sd at m = inf: nothing registers, Wait-for-Decoder's path; must equal N",
         **_r7("W2YBl1spSd", w2=dict(vq="inf", **_R7D)))
for _m in ("m0", "m1", "m2", "m3"):
    _M = _m.upper()
    register("V2qdSd" + _M, "D", "V2qd-Sd at the fit file's margin %s ((a) and (b))" % _m, **_r7("W2YBl1spSd", w2=dict(vq=_m, **_R7D)))
    register("V2qaSd" + _M, "D", "V2q-Sd with (a) alone at %s: J_N at N's own cost" % _m, **_r7("W2YBl1spSd", w2=dict(vq=_m, vqjn=True)))
    register("V2qbSd" + _M, "D", "V2q-Sd with (b) alone at %s: the feasibility-aware admission" % _m,
             **_r7("W2YBl1spSd", w2=dict(vq=_m, vqr=True)))
    register("V2qdSd" + _M + "noM", "D", "V2qd-noM at %s: contracts end at any gadget with a measurement" % _m,
             contracts="nomeas_all", **_r7("W2YBl1spSd", w2=dict(vq=_m, **_R7D)))
    register("V2qdSd" + _M + "noA", "D", "V2qd-noA at %s: ADVANCE priced at +inf, tables and admission bits rebuilt" % _m,
             **_r7("W2YBl1spSd", w2=dict(vq=_m, noadv=True, **_R7D)))
    register("V2qdSd" + _M + "noC", "D", "V2qd-noC at %s: COMMIT priced at +inf, tables and admission bits rebuilt" % _m,
             **_r7("W2YBl1spSd", w2=dict(vq=_m, nocom=True, **_R7D)))
    register("V2qdSd" + _M + "D0", "D", "V2qd-D0 at %s: Delta = 0, the admission kept, tables rebuilt" % _m,
             **_r7("W2YBl1spSd", w2=dict(vq=_m, vqd0=True, **_R7D)))
# round 8: V2*-Sd's own removal settings for F_10, V2q's 3(d) builder options on V2*-Sd (no backlog
# index and no admission test: every site with a walk slot registers, as in V2*-Sd).  noA: ADVANCE priced at +inf (commit
# only); noC: COMMIT priced at +inf, no rebased key (advance only; its ADVANCE and WAIT prices still read the predicted
# probabilities).  Tables rebuilt from the same frozen inputs; --r7-guard and --r8-rule as on every arm D registration.
register("W2YBl1spSdnoA", "D", "V2*-Sd-noA (round 8, plan v2 5.1): ADVANCE priced at +inf in the builder, tables rebuilt; commit only",
         **_r7("W2YBl1spSd", w2=dict(noadv=True)))
register("W2YBl1spSdnoC", "D", "V2*-Sd-noC (round 8, plan v2 5.1): COMMIT priced at +inf in the builder, tables rebuilt; advance only",
         **_r7("W2YBl1spSd", w2=dict(nocom=True)))
for _t in ("never", 0, 1, 2, 4, 8, "always"):
    register("QG" + ("t%d" % _t if type(_t) is int else _t), "D", "QG (round 7, 3(b)): FAC-F-Sd-L1, a value registers only if the "
             "decoder backlog at registration <= %s" % _t, **_r7("FACFSdL1", qg=_t))
register("Nnospec", "N", "N-nospec (round 7, 3(b)): SWIPER's sliding windows with speculation off (speculation_mode None)",
         swiper_nospec=True)
# SER-NC, static reservation at one frame with prediction-driven commits disabled. Its
# the decision entries are V2*-Sd-noC (W2YBl1spSdnoC, above: COMMIT priced at +inf, no rebased key). Its TS entries are FAC-F,
# FAC-A and W1 (-Sd-L1) with U's option nocom: the unit is asked and answers as before, and every ACT it answers is
# discarded at once (`_u_discard`, cause nocom), so no early script is spliced.  U6-noact is not used: it makes no
# request at all, which also removes the requests' potential excess from u_xs and their work from the unit's passes,
# both read by the carry rule.
for _src in ("FACA", "FACF", "U6c0k1w0"):
    register(_src + "SdL1noC", "D", "%sSdL1 with prediction-driven commits disabled (SER-NC): every ACT discarded at once,"
             "requests and the unit's passes as %sSdL1's" % (_src, _src), **_r6(_src + "SdL1", unified=dict(nocom=True)))
