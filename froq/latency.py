"""The decoder-latency table and its lookup rule, shared by the simulator (which samples a decode time per window) and the
policy (which needs the decode-time law per volume class), so both see the same decoder.

Tables (samples in microseconds, 10,000 per (d, class); one round = 1 us):
  swiper SWIPER's shipped `benchmarks/data/decoder_dists.json` (d = 13..31, classes r = 2..7, measured on their machine) with
          SWIPER's rule: class = max(2, ceil(volume / d)), volume in units of rounds x d^2 (the artifact's `_simulate.py`).
  local `froq/data/decoder_dists_local.json`, measured on this machine by `froq/measure_latency.py` with SWIPER's script for
          d = 3..21 and classes r = 1..7; rule: class = max(1, ceil(volume / d)) -- class 1 exists because 10-15 % of sliding
          windows are <= 1 d^3 (the last window of a patch has no forward buffer), which SWIPER's floor rounds up to 2.
          d = 23: MODELED, the same script and setup in a later session whose d = 21
          missed this table's medians by 5-13 %, rescaled to this table's basis by d = 21's pooled ratio (its meta file);
          in its own file `froq/data/decoder_dists_local_d23.json` (EXTRA), so that the main file and its hash, part of
          every run's fingerprint, stay as they are; a run at d = 23 reads and hashes that file.
A volume above the largest class is REFUSED (`VolumeOutOfRange`, counted in `volume_out_of_range`): the design's E0 table
ends at class 7 and an out-of-domain volume fails the run rather than silently substituting (design Sec. 5, MA-10/MB-12).

Randomness (design Sec. 4, findings A3 / B2): decoder-latency draws are keyed by the COMPLETE identity of
the window attempt -- the spacetime region it decodes (commit and buffer) and the instructions that produced it, digested by
`froq.device.ScoutDeviceManager._decode_key`, plus the attempt number of THAT window -- so identical windows draw identical
latencies in the N and Q configurations whatever else the run does, and no two distinct windows share a stream. Windows of
a value's recovery draw from the SEPARATE "recovery_decode" namespace, so a recovery's draws can never shift the program's.
Upstream's speculation-REJECTION coin (swiper/decoder_manager.py:162) was the remaining sequential decoder draw; it is
keyed here too (`KeyedSampler.coin`, by the identities of the two windows the coin decides about and its attempt) and
installed from the adapter, upstream being read-only. The keyed path is installed by `froq.device.ScoutDeviceManager` (it
alone knows which window each draw belongs to); `KeyedSampler.__call__` keeps the upstream one-argument signature as a
sequential fallback for foreign callers."""
from __future__ import annotations

import json, math, os

import numpy as np

from . import SWIPER_ROOT
from .rng import KeyedRNG

PATHS = {"swiper": os.path.join(SWIPER_ROOT, "benchmarks", "data", "decoder_dists.json"),
         "local": os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "decoder_dists_local.json")}
# distances measured after a table, each in its own file beside it (a run at that d reads and hashes that file)
EXTRA = {"local": {23: os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "decoder_dists_local_d23.json")}}
FLOOR = {"swiper": 2, "local": 1}
DECODER_SEED_OFFSET = 1_000_003       # the decoder streams never share a seed with the truth / prediction streams


class VolumeOutOfRange(ValueError):
    """A decode window whose volume needs a latency class beyond the table's largest one (design Sec. 5, MA-10/MB-12:
    extend the E0 table or refuse the run; never silently substitute the largest class)."""


class LatencyTable:
    def __init__(self, kind: str, distance: int, program: str | None = None, round_us: float = 1.0, decode_scale: float = 1.0,
                 full_decode_scale: float = 1.0):
        if kind not in PATHS:
            raise ValueError(kind)
        if not (round_us > 0):
            raise ValueError("round_us must be positive, got %r" % (round_us,))
        if not (decode_scale > 0):
            raise ValueError("decode_scale must be positive, got %r" % (decode_scale,))
        if not (full_decode_scale > 0):
            raise ValueError("full_decode_scale must be positive, got %r" % (full_decode_scale,))
        self.kind, self.d, self.path = kind, distance, EXTRA.get(kind, {}).get(int(distance), PATHS[kind])
        self.round_us = float(round_us)              # syndrome-round duration in microseconds (E5 round-duration ablation); samples are in us
        self.decode_scale = float(decode_scale)      # (rule 25): every decode time multiplied by this (2 = doubled); 1 = the table as measured
        self.step = None                             # (plan v2 5.3): (shock round, scale), the drift step on top; None = off
        self.full_decode_scale = float(full_decode_scale)   # rule 57: the full decoder's decode times multiplied by this too
                                                            # (0.5 = halved), the strip pool's not; 1 = off
        self.program = program                       # the bench being run, named in the VolumeOutOfRange error
        if not os.path.exists(self.path):
            raise FileNotFoundError("%s latency table missing: %s%s" % (kind, self.path, " (run froq/measure_latency.py)" if kind == "local" else ""))
        all_d = json.load(open(self.path))
        if str(distance) not in all_d:
            raise KeyError("no latency samples for d = %d in the %s table (%s)" % (distance, kind, sorted(all_d, key=int)))
        # the strip pool's samples (`KeyedSampler.draw_keyed_strip`); the full decoder's (`samples`: its draws and every model
        # of its service, `pmf_rounds`) are the same arrays unless rule 57's scale is on
        self.strip_samples = {int(k): np.asarray(v, dtype=float) * self.decode_scale for k, v in all_d[str(distance)].items()}
        self.samples = ({c: s * self.full_decode_scale for c, s in self.strip_samples.items()} if self.full_decode_scale != 1.0
                        else self.strip_samples)
        self.classes = sorted(self.samples)
        self.floor = FLOOR[kind]
        self.default_class = max(self.floor, self.classes[0])
        self.volume_out_of_range = 0
        self.max_volume_seen = 0                     # largest window volume ever classified (E0-table coverage record)
        self.max_class_seen = 0                      # ... and the largest class it required
        self._pmf: dict[int, dict[int, float]] = {}

    def volume_class(self, volume: int) -> int:
        c = max(self.floor, math.ceil(volume / self.d))
        self.max_volume_seen = max(self.max_volume_seen, volume)
        self.max_class_seen = max(self.max_class_seen, c)
        if c > self.classes[-1]:
            self.volume_out_of_range += 1
            raise VolumeOutOfRange(
                "window volume %d (rounds x d^2) needs latency class %d, beyond the %s table's largest class %d at d = %d "
                "(program %s): extend the E0 table or refuse the run (design Sec. 5, MA-10/MB-12)"
                % (volume, c, self.kind, self.classes[-1], self.d, self.program or "<unnamed>"))
        return c

    def sampler(self, seed: int, salt: int | None = None):
        """The simulator's decoding_latency_fn: draws keyed by window-attempt identity (installed by the device), so the
        same program window draws the same latency in every configuration that produces it. Returned as a one-argument
        closure (upstream introspects its source) with the KeyedSampler's keyed entry points attached as `draw_keyed`
        (a latency sample) and `draw_coin` (the decoder's speculation-rejection coin, same seed, separate namespaces).
        `salt` (--decoder-salt k, k >= 1; None = none): an independent decoder stream of the same seed."""
        ks = KeyedSampler(self, seed, salt)

        def fn(volume: int) -> float:
            return ks(volume)

        fn.draw_keyed = ks.draw_keyed
        fn.draw_coin = ks.coin
        fn.sampler_state = ks
        return fn

    def pmf_rounds(self, cls: int) -> dict[int, float]:
        """Decode time in whole rounds as a probability mass function: ceil of the microsecond sample, at least 1 -- the same
        service the simulator realises when it counts a fractional sample down one round at a time."""
        if cls not in self._pmf:
            rounds = np.maximum(1, np.ceil(self.samples[cls] / self.round_us).astype(int))
            v, n = np.unique(rounds, return_counts=True)
            self._pmf[cls] = {int(a): float(b) / float(n.sum()) for a, b in zip(v, n)}
        return self._pmf[cls]

    def describe(self) -> dict:
        d = dict(kind=self.kind, path=os.path.relpath(self.path, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                 rule="class = max(%d, ceil(volume / d)); a volume above class %d is refused, never clamped" % (self.floor, self.classes[-1]),
                 classes=self.classes, samples_per_class=int(len(self.samples[self.classes[0]])), round_us=self.round_us,
                 median_us={c: float(np.median(self.samples[c])) for c in self.classes},
                 volume_out_of_range=self.volume_out_of_range,
                 max_volume_seen=self.max_volume_seen, max_class_seen=self.max_class_seen)
        if self.decode_scale != 1.0:                 # decoder-latency sensitivity (absent = the table as measured)
            d["decode_scale"] = self.decode_scale
        if self.step is not None:                    # drift step (absent = off)
            d["decode_step"] = dict(round=int(self.step[0]), scale=float(self.step[1]))
        if self.full_decode_scale != 1.0:            # rule 57's decoding speed (absent = off); median_us is then the full decoder's
            d["full_decode_scale"] = self.full_decode_scale
        return d


class KeyedSampler:
    """Every decoder draw of one simulation seed (round-1 finding 8, round-2 findings A3 / B2; design Sec. 4).

    `draw_keyed(volume, *key)` draws a latency sample from the KeyedRNG stream named by `key`; the device names a program
    window ("decode", digest of the window's complete identity, attempt) and a window of a value's recovery
    ("recovery_decode", digest, attempt), so a recovery's draws can never shift the program's. `coin(*key)` is the
    decoder's speculation-rejection draw under the same regime ("reject" / "recovery_reject", the two window digests, the
    attempt). All of them are streams of the DECODER seed, disjoint from the truth / prediction streams and from each
    other. `__call__(volume)` keeps the upstream one-argument signature (one sequential stream, the pre-fix behaviour)
    for a caller that never installs the keyed path. A salt k (replicates) puts ("salt", k) before every key and
    seeds the sequential stream with (seed, k): the same windows then draw from a second, independent set of streams,
    and the truths and predictions, which are not decoder draws, stay the seed's. Without a salt nothing changes."""

    def __init__(self, table: LatencyTable, seed: int, salt: int | None = None):
        self.table, self.seed = table, int(seed)
        self.salt = None if salt is None else int(salt)
        assert self.salt is None or self.salt >= 1, ("the decoder salt is an integer >= 1", salt)
        if self.salt is None:
            self._sequential = np.random.default_rng(self.seed + DECODER_SEED_OFFSET)
            self._keyed = KeyedRNG(self.seed + DECODER_SEED_OFFSET)
        else:                                # ("salt", k) before every key, so salts draw independent streams
            self._sequential = np.random.default_rng([self.seed + DECODER_SEED_OFFSET, self.salt])
            self._keyed = KeyedRNG(self.seed + DECODER_SEED_OFFSET, prefix=("salt", self.salt))
        self.keyed_draws = 0
        self.sequential_draws = 0
        self.coin_draws = 0
        self.clock = None                    # (plan v2 5.3): the decoder's current round, installed by the device
        self.step_draws = 0

    def _stepped(self, x: float) -> float:
        """(plan v2 5.3, E10): the drift step -- a decode that starts at or after the shock round takes `scale` times
        its drawn time (the draw itself unchanged, so every other draw is the same)."""
        st = getattr(self.table, "step", None)
        if st is not None and self.clock is not None and self.clock() >= st[0]:
            self.step_draws += 1
            return x * st[1]
        return x

    def __call__(self, volume: int) -> float:
        self.sequential_draws += 1
        return self._stepped(float(self._sequential.choice(self.table.samples[self.table.volume_class(volume)])) / self.table.round_us)   # fractional, in rounds: the decoder counts it down one round at a time, so the service is ceil(sample_us / round_us)

    def draw_keyed(self, volume: int, *key) -> float:
        """One sample of the window's volume class from the stream named by `key` (a fresh stream per key:
        order-independent, and disjoint from every other semantic stream)."""
        return self._draw(self.table.samples, volume, key)

    def draw_keyed_strip(self, volume: int, *key) -> float:
        """`draw_keyed` for the strip pool: the same stream and class rule on the strip pool's samples, which rule 57's
        full-decoder scale leaves as they are."""
        return self._draw(self.table.strip_samples, volume, key)

    def _draw(self, by_class: dict, volume: int, key: tuple) -> float:
        self.keyed_draws += 1
        samples = by_class[self.table.volume_class(volume)]
        return self._stepped(float(samples[int(self._keyed.stream(*key).integers(len(samples)))]) / self.table.round_us)

    def draw_at_rank(self, volume: int, u: float) -> float:
        """(`--pred-couple tail`; plan v2 3.3, F6): the window's volume class's sample at rank u in [0, 1): its
        samples in ascending order, index floor(u n). For a uniform u it has the keyed draw's law (each sample with
        probability 1/n), and it is monotone in u, so a larger u never gives a faster decode."""
        self.keyed_draws += 1
        cls = self.table.volume_class(volume)
        srt = self.__dict__.setdefault("_rank_sorted", {})
        if cls not in srt:
            srt[cls] = np.sort(np.asarray(self.table.samples[cls], dtype=float))
        s = srt[cls]
        return self._stepped(float(s[min(len(s) - 1, int(float(u) * len(s)))]) / self.table.round_us)

    def coin(self, *key) -> float:
        """The uniform number of one speculation-rejection decision, from the stream named by `key`: upstream draws it
        sequentially (decoder_manager.py:162), so any extra decoder activity shifted every later coin (findings A3 / B2)."""
        self.coin_draws += 1
        return self._keyed.uniform(*key)


def _selftest() -> None:
    """Unit checks for findings 8 and 10 (run: python -m froq.latency)."""
    t = LatencyTable("local", 5, program="selftest_bench")
    # finding 10: a volume beyond class 7 (d = 5: volume > 35) is refused, naming volume, class limit and program
    try:
        t.volume_class(10_000)
        raise AssertionError("volume 10000 was not refused")
    except VolumeOutOfRange as e:
        msg = str(e)
        assert "10000" in msg and "class 7" in msg and "selftest_bench" in msg, msg
    assert t.volume_out_of_range == 1 and t.max_class_seen == 2000
    assert t.volume_class(35) == 7 and t.volume_class(1) == 1            # the in-range rule is unchanged
    # findings 8 / A3 / B2: a draw depends on its key alone -- not on the order of the draws, not on how many other
    # windows drew before it, and not on how often the key itself was used
    vols = [12, 7, 30, 22, 17, 9]
    windows = ["w%d" % i for i in range(len(vols))]                      # stand-ins for the device's window digests

    def program_draws(s: KeyedSampler, order=None, inject: bool = False):
        out = {}
        for i in (order if order is not None else range(len(vols))):
            if inject and i == 3:
                s.draw_keyed(vols[i], "recovery_decode", "r0", 1)        # an injected recovery-window draw
            out[windows[i]] = s.draw_keyed(vols[i], "decode", windows[i], 1)
        return out

    plain = program_draws(t.sampler(0))
    assert program_draws(t.sampler(0), inject=True) == plain             # (ii) an inserted recovery window changes nothing
    assert program_draws(t.sampler(0), order=[3, 0, 5, 1, 4, 2]) == plain  # (i) reordering the windows changes nothing
    assert program_draws(t.sampler(0)) == plain                          # a replayed run reproduces every sample
    s = t.sampler(0)                                                     # a repeated identical key repeats its value ...
    assert s.draw_keyed(20, "decode", "w0", 1) == s.draw_keyed(20, "decode", "w0", 1) == t.sampler(0).draw_keyed(20, "decode", "w0", 1)
    attempts = [s.draw_keyed(20, "decode", "w0", a) for a in range(1, 4)]  # ... and only the attempt number advances it
    assert len(set(attempts)) == 3 and [t.sampler(0).draw_keyed(20, "decode", "w0", a) for a in range(1, 4)] == attempts
    # the decoder's speculation-rejection coin obeys the same rule (its key names both windows and the attempt)
    c = t.sampler(0)
    coins = {(a, b): c.draw_coin("reject", a, b, 1) for a in windows[:3] for b in windows[3:]}
    assert len(set(coins.values())) == len(coins) and all(0.0 <= x < 1.0 for x in coins.values())
    c2 = t.sampler(0)
    for k in reversed(list(coins)):                                      # drawn in the opposite order: same numbers
        assert c2.draw_coin("reject", k[0], k[1], 1) == coins[k]
    assert c2.draw_coin("recovery_reject", windows[0], windows[3], 1) != coins[(windows[0], windows[3])]   # separate stream
    print("latency self-test PASS: volume 10000 refused (%s); %d program draws and %d rejection coins unchanged by "
          "reordering, by an injected recovery draw and by repetition" % (msg.strip(), len(plain), len(coins)))


if __name__ == "__main__":
    _selftest()
