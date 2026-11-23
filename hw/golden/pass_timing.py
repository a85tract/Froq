"""U's pass timing on 6ea9fc7's RTL at 3.5 ns, for rule 52's closed loop.

Derived from the pass sequencer and the ETA event FSM of hw/rtl/se_u_unit.sv and checked, pass by pass, against the
RTL's own cycles in every replay record. A round is P = 286 cycles.

In round k, with its strobe at cycle s_k:
- the engine's phase A starts at s_k + 2 and lasts engine_cycles(...) cycles;
- U's pass k starts one cycle after phase A ends, or one cycle after pass k - 1 ends, whichever is later;
- it lasts pass_cycles(...) cycles;
- u_xs and its tag u_xs_k = k are valid 4 cycles after the pass ends (5 after its last state, S_FIN).
PassClock chains these over the passes and gives the round from which the scheduler can use each pass's u_xs.

The inputs are per pass. The window events (NEW, UPD, COMMIT, DONE) and the requests are the pass's WIN and EPOCH
traffic. The taps are the engine's entry events for U. The recomputes, invalidated slots, exhausted budgets and
stale-scan roots are the ETA register file's work, which the golden model (golden.u_unit.UUnit) counts in
stats["recomputes"], ["invalidated"], ["budget_exhausted"] and ["stale"]: their increase over the pass."""
import math

P = 286                      # cycles a round at 3.5 ns


def pass_cycles(new=0, upd=0, commit=0, done=0, req=0, taps=0, recomputes=0, invalidated=0, exhausted=0, stale=0):
    """Cycles from the pass's start to its end (the testbench's UPS to UPE lines).
    - S_RUN: 3 cycles, or the ETA FSM's work plus 3, or the taps (one a cycle, in parallel) plus 1, whichever is
      longest.  A NEW, UPD or DONE takes 4 cycles, a COMMIT 2, each recompute of a pending successor 5; a budget spent
      marks its closure invalid at about 3 cycles a slot, plus 2; each stale window the scan finds takes 6 (a pick, then
      an event).
    - S_EP: 1 cycle without a request; otherwise one request a cycle, then the 11-stage pipeline drains (R + 11).
    - S_FIN: 1 cycle.
    The invalidation term is exact when the closure's frontier never empties; otherwise the RTL takes up to one cycle
    less per slot, so the formula then runs long.  Over every pass of the replays it is never short: it is exact in all
    but 417 of 34,610,409 passes, all with a spent budget, which it overestimates by 1 to 94 cycles."""
    busy = 4 * (new + upd + done) + 2 * commit + 5 * recomputes + 3 * invalidated + 2 * exhausted + 6 * stale
    return max(3 + busy, taps + 1) + (2 if req == 0 else req + 12)


def pass_cycles_occupancy(win, req, taps=0):
    """The same from the pass's occupancy alone (its window events and requests): every window event at 4 cycles, and no
    recompute, invalidation or stale scan.  Exact for a pass without ETA propagation; otherwise short by the propagation
    the golden model counts (pass_cycles), which is why the closed loop should use pass_cycles."""
    return pass_cycles(new=win, req=req, taps=taps)


def engine_cycles(reg=0, res=0, rc=0, rd=0):
    """The engine's phase A in round k (se_entry_table.sv): 3 cycles, plus each REGISTER, RESOLVE, RECONFIDENCE and
    release (RD) message of the round.  Exact in every pass of the replays (pass-timing.md), whose engine traffic has no
    upstream links; a RESOLVE or RECONFIDENCE that starts a worklist cascade adds 3 cycles per entry it walks, plus 1."""
    return 3 + ENGINE_COST["reg"] * reg + ENGINE_COST["res"] * res + ENGINE_COST["rc"] * rc + ENGINE_COST["rd"] * rd


# cycles per message: A_REG_L, A_REG_X, A_NEXT; a matched RESOLVE A_RES_L, A_RES_M, A_RES_X, A_NEXT (3 if unmatched);
# a RECONFIDENCE A_RC_L, A_RC_X, A_NEXT when it misses (none occurs in the replays: the RTL's count); a release
# A_RD_L, A_RD_X, A_NEXT
ENGINE_COST = dict(reg=3, res=4, rc=3, rd=3)


class PassClock:
    """The passes in order: pass k's start, end and u_xs export, in cycles from its own strobe, and the first round in
    which the scheduler can use that u_xs, assuming it reads at cycle t_dec of the round (0: at the strobe, when the
    round's frames open).  Freshness is then (the round of use) - k, the pass's own latency included (rule 52)."""

    def __init__(self, t_dec=0):
        self.t_dec, self.k, self.end = t_dec, None, None        # end: the last pass's end, in cycles from its strobe

    def step(self, k, eng, length):
        """eng: engine_cycles(...) of round k; length: pass_cycles(...) of pass k.  Returns (start, end, export,
        visible round), the first three in cycles after strobe k."""
        start = 2 + eng + 1
        if self.end is not None:
            start = max(start, self.end - P * (k - self.k) + 1)    # (passes run one at a time)
        end = start + length
        export = end + 4
        self.k, self.end = k, end
        return start, end, export, k + math.ceil((export - self.t_dec) / P)
