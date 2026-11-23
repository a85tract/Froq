"""`contract.critical_path`, a lowered script's rounds kept per patch (each
instruction starts at the latest finish on its patches), against the pairwise rule it replaced in `_place` (each
instruction scans every earlier one for a shared patch): fixed cases with their rounds (ties, zero durations, repeated
patches, an empty list, instructions on no patch), random lists, and a negative duration, which raises.

    python -m unittest -v froq.test_script_placement"""
from __future__ import annotations

import random
import unittest
from types import SimpleNamespace

from .contract import critical_path


def pairwise(instrs, durs) -> int:
    """The rule `critical_path` replaced in `_place`."""
    finish = []
    for i, x in enumerate(instrs):
        start = max([finish[j] for j in range(i) if set(instrs[j].patches) & set(x.patches)] or [0])
        finish.append(start + durs[i])
    return max(finish or [0])


def lst(*pd):
    """(patches, duration) pairs -> (instructions, durations)."""
    return [SimpleNamespace(patches=tuple(p)) for p, _ in pd], [d for _, d in pd]


class TestCriticalPath(unittest.TestCase):
    CASES = {                                                       # name: ((patches, duration), ...), rounds
        "empty": ((), 0),
        "no patch": ((((), 3), ((), 5)), 5),
        "no patch among others": ((((0,), 4), ((), 9), ((0,), 1)), 9),
        "zero durations": ((((0,), 0), ((0,), 4), ((0,), 0), ((1,), 0)), 4),
        "all zero": ((((0,), 0), ((0, 1), 0), ((1,), 0)), 0),
        "tie": ((((0,), 3), ((1,), 3), ((0, 1), 2)), 5),
        "patch repeated in one instruction": ((((0, 0), 2), ((0,), 3)), 5),
        "patch repeated later": ((((0,), 2), ((1,), 7), ((0,), 1)), 7),
        "chain through a shared instruction": ((((0,), 2), ((1,), 7), ((0, 1), 1), ((0,), 1)), 9),
    }

    def test_cases(self):
        for name, (pd, rounds) in self.CASES.items():
            instrs, durs = lst(*pd)
            self.assertEqual((critical_path(instrs, durs), pairwise(instrs, durs)), (rounds, rounds), name)

    def test_random_lists(self):
        rng = random.Random(10)
        for _ in range(5000):
            n = rng.randint(1, 6)
            instrs = [SimpleNamespace(patches=tuple(rng.choices(range(n), k=rng.randint(0, 4)))) for _ in range(rng.randint(0, 30))]
            durs = [rng.choice((0, 0, 1, 2, 3, 5, 8, 13)) for _ in instrs]
            self.assertEqual(critical_path(instrs, durs), pairwise(instrs, durs))

    def test_negative_duration_raises(self):
        for pd in ((((0,), -1),), (((0,), 5), ((0,), -10), ((0,), 6))):      # (the second: 11 pairwise, 5 per patch)
            with self.assertRaises(AssertionError):
                critical_path(*lst(*pd))


if __name__ == "__main__":
    unittest.main()
