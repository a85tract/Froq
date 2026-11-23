"""Speculated values (design Sec. 2.1): one per T gadget, discovered from the event stream (the ZZ-parity result whose
predicate drives the conditional S), with the client's prediction, the definitive truth, the delivery timestamps of the
decoded result (path delay, Sec. 4), the contract's admission state (steps admitted, ACCEPT rounds, reservation) and the
recovery record used by the price accounting (P1 encoded, P2 measured)."""
from dataclasses import dataclass, field
from enum import Enum


class VState(Enum):
    PENDING = "pending"        # producer not finished: no prediction yet
    LIVE = "live"              # prediction published, definitive value not yet delivered
    CONFIRMED = "confirmed"    # definitive == prediction (or no speculation was used)
    RECOVERING = "recovering"  # contradiction: waiting for the in-flight atom, running the recovery, or settling
    RETIRED = "retired"        # RECOVERY_DONE


@dataclass
class Value:
    vid: int
    gadget_id: str
    result_id: str                          # the predicted result id (D starts here)
    producer_event: str
    producer_idx: int                       # the .lss instruction whose decode is the definitive result
    wire: str                               # the corrected program wire
    group: tuple                            # .lss indices of the T2 atom (SWIPER's CONDITIONAL_S group)
    target_patch: tuple
    truth: bool                             # is the S needed (u = 1)?
    prediction: bool | None = None
    confidence: float = 0.9
    predicted_round: int = -1
    decoded_round: int = -1                 # decode completion of the producer (before the path)
    delivered_round: int = -1               # decoded_round + path: the definitive result is visible to the sequencer
    notifications: list = field(default_factory=list)   # (decode reset round, delivery round) of rejected attempts
    state: VState = VState.PENDING
    speculated: bool = False                # did the sequencer act on the prediction (ACCEPT of step 0)?
    admitted: int = 0                       # steps admitted so far (the current boundary)
    accept_round: dict = field(default_factory=dict)      # step k -> round of ACCEPT
    accept_started: dict = field(default_factory=dict)    # step k -> members that had already run at ACCEPT (timer = upper bound)
    reserved: frozenset = frozenset()       # recovery workspace currently reserved
    hold_reason: str | None = None          # why the current boundary was not admitted this round (ledger)
    class_miss: bool = False                # site class not in the codebook: the value only WAITs
    site_prices: list | None = None         # codebook (P3) prices the policy sees, r_0..r_F
    settled: bool = False                   # holds ended: confirmed, or recovery complete and delivered
    settled_round: int = -1
    contradiction_round: int = -1
    recovery: dict | None = None
    issued_by_policy: list = field(default_factory=list)
    # the SWIPER client: T_emit, the first-order survival q of the pending boundaries there, whether
    # the published parity is wrong (a pending coin rejects and the f_flip coin flips it), and the reset notice's RC round
    t_emit: int = -1
    sw_q: float = 1.0
    sw_wrong: bool = False
    rc_round: int = -1
    c_pub: float = -1.0                     # the confidence the publication (REGISTER) carried
    # the strip client: its publication round, the q it was drawn with (= its confidence),
    # whether its parity is wrong, and whether q is rule 11's conservative value
    strip_pub: int = -1
    strip_q: float = -1.0
    strip_wrong: bool = False
    strip_cons: bool = False

    @property
    def delivered(self) -> bool:
        return self.delivered_round >= 0
