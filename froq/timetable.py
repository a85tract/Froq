"""The Froq engine, generalized from prototypes/rr_release.py.

A *site* is a chain of decision boundaries p_0..p_F with forward-edge
durations, a source-settlement recovery price per boundary, a wait cost
n_s per epoch, and a fence at p_F (wait loop only). A *timetable* is a
pmf over absolute resolution rounds plus an overflow bin (mass beyond the
horizon E and its mean lateness). solve runs the backward sweep of
paper eqs. (3)-(5) and returns a Policy: one action per reachable
(round, boundary) cell, walked deterministically between events.

W0 gate (tests/test_engine_w0.py): bit-exact agreement with
prototypes/rr_release.py on the worked site and the corridor variant."""
from __future__ import annotations
from dataclasses import dataclass, field
import json


# ---------------------------------------------------------------- site
@dataclass
class Site:
    name: str
    durations: list[int]          # forward-edge rounds, p_k -> p_{k+1}
    r: list[float]                # recovery price per boundary, len = F+1
    n_s: float                    # wait cost per epoch (patch-rounds/round)
    epoch: int = 1                # decision epoch in rounds
    settle_mode: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def fence(self) -> int:
        return len(self.durations)

    def r_fwd(self, k: int) -> float:
        """Recovery price charged for a disagreement that lands WHILE
        edge k is in flight.  SRC settlement (the default, and what
        every merge edge uses) charges the source boundary r_k; DST
        settlement charges the destination r_{k+1} -- the compiled
        rule for edges whose commit point is the measurement itself
        (msd_fold's Y_MEAS; see scout_sim/sites/recovery_library.md).
        WAIT and the fence always charge r_k."""
        if k < len(self.settle_mode) and self.settle_mode[k] == "DST":
            return self.r[k + 1]
        return self.r[k]

    def births(self, e_r: int) -> list[int]:
        b = [e_r]
        for d in self.durations:
            b.append(b[-1] + d)
        return b

    def to_json(self) -> dict:
        return dict(name=self.name, durations=self.durations, r=self.r,
                    n_s=self.n_s, epoch=self.epoch,
                    settle_mode=self.settle_mode, meta=self.meta)

    @classmethod
    def from_json(cls, d: dict) -> "Site":
        return cls(name=d["name"], durations=list(d["durations"]),
                   r=list(d["r"]), n_s=d["n_s"], epoch=d.get("epoch", 1),
                   settle_mode=list(d.get("settle_mode", [])),
                   meta=d.get("meta", {}))

    def save(self, path):
        json.dump(self.to_json(), open(path, "w", encoding="utf-8"), indent=1)

    @staticmethod
    def load(path) -> "Site":
        return Site.from_json(json.load(open(path, encoding="utf-8")))


# ----------------------------------------------------------- timetable
class Timetable:
    """pmf over absolute rounds; overflow bin keeps (mass, mean lateness
    past E).  Immutable once built; events build a new one."""

    def __init__(self, probs: dict[int, float], E: int,
                 overflow_mass: float = 0.0, overflow_lateness: float = 0.0):
        self.E = E
        self.p = {int(t): float(w) for t, w in probs.items()
                  if w > 0 and t <= E}
        spill = sum(float(w) for t, w in probs.items() if t > E and w > 0)
        if spill > 0:
            late = sum(float(w) * (t - E) for t, w in probs.items()
                       if t > E and w > 0)
            overflow_lateness = ((overflow_mass * overflow_lateness + late)
                                 / (overflow_mass + spill))
            overflow_mass += spill
        self.of_mass = overflow_mass
        self.of_late = overflow_lateness
        # suffix survival S(e) = P(tau > e), including overflow
        self._smin = min(self.p) if self.p else E
        self._S = {}
        acc = self.of_mass
        for t in range(E, self._smin - 2, -1):
            self._S[t] = acc
            acc += self.p.get(t, 0.0)
        self._Sfloor = acc  # S(e) for e < smin-1

    def S(self, e: int) -> float:
        if e >= self.E:
            return self.of_mass
        if e < self._smin - 1:
            return self._Sfloor
        return self._S[e]

    def mass_at(self, t: int) -> float:
        return self.p.get(t, 0.0)

    def mass_between(self, a: int, b: int) -> float:
        """P(a < tau <= b), table part only (b <= E enforced by caller)."""
        return sum(self.p.get(t, 0.0) for t in range(a + 1, b + 1))

    def stall_sum(self, e: int, n_s: float) -> float:
        """n_s * E[(tau - e) * 1(tau > e)], overflow bin exact."""
        s = sum(w * (t - e) for t, w in self.p.items() if t > e)
        s += self.of_mass * (self.E - e + self.of_late)
        return n_s * s

    def total(self) -> float:
        return sum(self.p.values()) + self.of_mass


# --------------------------------------------------------------- policy
@dataclass
class Policy:
    site: Site
    c: float
    e_r: int
    E: int
    b: list[int]
    A: dict            # (e, k) -> 'F' | 'W'
    V: dict            # (e, k) -> expected remaining cost

    def act(self, e: int, k: int) -> str:
        if k == self.site.fence:
            return "W"
        if e >= self.E:
            return "W"
        return self.A[(e, k)]

    def stored_cells(self) -> int:
        """Decision cells only: e in [b_k, E), fence and seed rows excluded."""
        return sum(1 for (e, k) in self.A
                   if k < self.site.fence and self.b[k] <= e < self.E)


def solve_split(site: Site, c: float, lam_c: Timetable, lam_w: Timetable,
                e_r: int) -> Policy:
    """The factorization-limit reference policy (C4c): the client hands
    over conditional timetables Lambda_correct / Lambda_wrong instead
    of one marginal Lambda and one constant c.  Same recurrence as
    solve(), but recovery is weighted by the *wrong* arrival mass
    (1-c) * dLambda_w while stall and survival use the mixture — so
    the policy prices in that late arrivals are more likely wrong.
    With lam_c == lam_w this reduces exactly to solve() on the mixture
    (asserted in campaigns/c4_adversarial.py part_c at delta=0)."""
    E, F, ns = lam_c.E, site.fence, site.n_s
    assert lam_w.E == E
    b = site.births(e_r)
    V, A = {}, {}

    def S(e):        # mixture survival
        return c * lam_c.S(e) + (1 - c) * lam_w.S(e)

    def wmass(a, bb):    # wrong arrival mass in (a, bb]
        return (1 - c) * lam_w.mass_between(a, bb)

    def stall(e):    # n_s * E[(tau-e) 1(tau>e)] under the mixture
        return c * lam_c.stall_sum(e, ns) + (1 - c) * lam_w.stall_sum(e, ns)

    for k in range(F + 1):
        SE = S(E)
        if SE > 0:
            late = (c * lam_c.of_mass * lam_c.of_late
                    + (1 - c) * lam_w.of_mass * lam_w.of_late) / SE
            pw = (1 - c) * lam_w.S(E) / SE
            V[(E, k)] = ns * late + pw * site.r[k]
        else:
            V[(E, k)] = 0.0
        A[(E, k)] = "W"
    for e in range(E - 1, e_r - 1, -1):
        Se = S(e)
        for k in range(F, -1, -1):
            if Se <= 1e-15:
                V[(e, k)], A[(e, k)] = 0.0, "-"
                continue
            if k == F:
                V[(e, k)] = (stall(e) + site.r[F] * (1 - c) * lam_w.S(e)) / Se
                A[(e, k)] = "W"
                continue
            d = site.durations[k]
            e2 = min(e + d, E)
            qF = (site.r_fwd(k) * wmass(e, e2) + S(e2) * V[(e2, k + 1)]) / Se
            e1 = min(e + site.epoch, E)
            qW = ns * site.epoch + (site.r[k] * wmass(e, e1)
                                    + S(e1) * V[(e1, k)]) / Se
            V[(e, k)] = min(qF, qW)
            A[(e, k)] = "F" if qF <= qW else "W"
    return Policy(site=site, c=c, e_r=e_r, E=E, b=b, A=A, V=V)


def solve(site: Site, c: float, lam: Timetable, e_r: int) -> Policy:
    """One backward sweep (paper eqs. 3-5), O(E * |P|)."""
    E, F, ns = lam.E, site.fence, site.n_s
    b = site.births(e_r)
    V, A = {}, {}
    # seed at the horizon: wait until resolution
    for k in range(F + 1):
        Se = lam.S(E)
        V[(E, k)] = (ns * lam.of_late + (1 - c) * site.r[k]) if Se > 0 else 0.0
        A[(E, k)] = "W"
    for e in range(E - 1, e_r - 1, -1):
        Se = lam.S(e)
        for k in range(F, -1, -1):
            if Se <= 1e-15:
                V[(e, k)], A[(e, k)] = 0.0, "-"
                continue
            if k == F:  # the fence: closed form, eq. (5)
                V[(e, k)] = (lam.stall_sum(e, ns)
                             + (1 - c) * site.r[F] * Se) / Se
                A[(e, k)] = "W"
                continue
            d = site.durations[k]
            e2 = min(e + d, E)
            win = (1 - c) * site.r_fwd(k) * (lam.mass_between(e, e2))
            qF = (win + lam.S(e2) * V[(e2, k + 1)]) / Se
            e1 = min(e + site.epoch, E)
            w1 = (1 - c) * site.r[k] * lam.mass_between(e, e1)
            qW = ns * site.epoch + (w1 + lam.S(e1) * V[(e1, k)]) / Se
            V[(e, k)] = min(qF, qW)
            A[(e, k)] = "F" if qF <= qW else "W"
    return Policy(site=site, c=c, e_r=e_r, E=E, b=b, A=A, V=V)
