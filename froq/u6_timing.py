"""Rule 52's pass timing: how long each pass of U's unit lasts and when its u_xs export reaches the
scheduler, by model of the RTL, `hw/golden/pass_timing.py` (ported unchanged from `` 84c12d9; its doc
is `` there).

The closed loop (froq/u6.py, `U6State.pass_end`) feeds the model, per pass k, with the full form's inputs: the pass's
window events by kind (NEW, UPD, COMMIT, DONE), its requests and the engine's taps for U, the golden unit's ETA-file work
in the pass (recomputes, invalidated slots, spent budgets, stale-scan roots: the increase of its counters), and the
round's engine messages (REGISTER, RESOLVE, RECONFIDENCE, RECOVERY_DONE). `PassClock` chains the passes (phase A,
then the pass, one pass at a time) and gives the first round in which the scheduler, reading at the strobe (t_dec = 0,
when the round's frames open), can use pass k's u_xs, tagged with its source round k - 1 (u_xs_src, the round of the
pass-start snapshot, as the U v6 RTL latches it; the age is measured from it). The ETA file a pass leaves is readable
from the first read at or after the pass's end: round k + rounds_after(end), never the round in which the pass ends
unless it ends by that round's read. A read sees the file of the last pass completed by it. When a pass runs past a
round's read, the RTL's carry-rule read port waits (er_busy) and returns the newer file, so the simulator is one pass
older there: conservative, never a look ahead (the U v6 code gate's re-check, LOW 2).

The model is U v4's RTL (6ea9fc7); is extending it to U v6's. That update is a data change: `hw/golden/
pass_timing.py` is replaced, with the same functions and arguments, and MODEL below is renamed. Every U v6 result
records `describe` (`fingerprint.policy.u6.timing` and the U v6 summary), so no run is mistaken for another model's."""
from . import policy  # noqa: F401  (puts hw/ on the path)
from golden import pass_timing as PT

MODEL = "T019 hw/golden/pass_timing.py at 84c12d9: U v4's RTL 6ea9fc7, the full form"
STUB = False                                # (a stub model must never time a selection or evaluation run: froq/run.py)
CYCLES_PER_ROUND = PT.P                     # one syndrome round at 3.5 ns (rule 41b')
T_DEC = 0                                   # the scheduler reads u_xs and the ETA file at the strobe, when the round's
                                            # frames open


def rounds_after(cycles: int) -> int:
    """The rounds after strobe k from which a read at cycle T_DEC of a round sees an event at `cycles` after strobe k."""
    return -((T_DEC - int(cycles)) // CYCLES_PER_ROUND)


def pass_cycles(**work) -> int:
    """Pass k's cycles from its occupancy and the ETA file's work (full form)."""
    return PT.pass_cycles(**work)


def engine_cycles(**msgs) -> int:
    """The engine's phase A of round k from its messages (model)."""
    return PT.engine_cycles(**msgs)


def clock():
    """One per run: PassClock(t_dec).step(k, engine_cycles, pass_cycles) -> (start, end, export, first round of use)."""
    return PT.PassClock(t_dec=T_DEC)


def describe() -> dict:
    return dict(model=MODEL, stub=STUB, cycles_per_round=CYCLES_PER_ROUND, t_dec=T_DEC, engine_cost=dict(PT.ENGINE_COST))
