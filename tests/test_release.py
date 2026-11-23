"""End-to-end checks of the release: the evaluation regenerates the paper's numbers and files, and the simulator
reproduces recorded runs exactly (same seeds give the same makespans).

    python -m unittest discover -s tests          (about 30 s)
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from froq import configuration as C                                   # noqa: E402


class Release(unittest.TestCase):
    def test_regenerate(self):
        with tempfile.TemporaryDirectory() as out:
            p = subprocess.run([sys.executable, os.path.join(ROOT, "evaluation", "regenerate.py"), "--out", out],
                               capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(" 0 differ or are missing", p.stdout)

    def test_recorded_runs(self):
        """Adder-4, d=17, budget 100 %, test seed 7200: SWIPER alone and Froq (concurrent predictions, margin 0)."""
        rec = {r["system"]: int(r["makespan"]) for r in C.read_tsv(os.path.join(ROOT, "results", "main", "runs.tsv"))
               if (r["program"], r["d"], r["budget"], r["seed"]) == ("Adder-4", "17", "100", "7200")}
        with tempfile.TemporaryDirectory() as out:
            swiper, _ = C.run_one("Adder-4", 17, 100, "wait", 7200, os.path.join(out, "swiper"))
            froq, censored = C.run_one("Adder-4", 17, 100, "concurrent/0", 7200, os.path.join(out, "froq"), swiper)
        self.assertEqual((swiper, froq, censored), (rec["swiper"], rec["froq"], False))


if __name__ == "__main__":
    unittest.main()
