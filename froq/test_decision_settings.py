"""Deploy: V2*-Sd's removal settings noA and noC (plan v2 5.1), the
advertised-error setting `--adv-err` (5.8) and the records of 5.6, at the unit level; seed 4's smoke runs are.md.

- The arms: W2YBl1spSdnoA and W2YBl1spSdnoC are V2*-Sd's options with ADVANCE or COMMIT priced at +inf and nothing else;
  the decision's option check takes them without V2q's margin, and still refuses D0 without one and any removal at m = inf.
- The mapping: off, the published c is the client's c itself; at k it is 1 - min(k (1 - c), 0.5).
- The name and the fingerprint: k as a reduced fraction, in the name's `_ae` tag and in the policy part only."""
import unittest

from . import arms, run, wager2
from .device import REC56_CELLS, REC56_LIVE, REC56_ROUNDS, ScoutDeviceManager


class TestR8g(unittest.TestCase):
    def test_removal_arms(self):
        base = arms.resolve("W2YBl1spSd")
        for name, key in (("W2YBl1spSdnoA", "noadv"), ("W2YBl1spSdnoC", "nocom")):
            b, o = arms.resolve(name)
            self.assertEqual(b, base[0])
            w2 = dict(o["defer"]["wager2"])
            self.assertIs(w2.pop(key), True)
            self.assertEqual(dict(o, defer=dict(o["defer"], wager2=w2)), base[1])     # (V2*-Sd's options and the one removal)
            self.assertIsNone(o["defer"]["wager2"].get("vq"))

    @staticmethod
    def _refused(**opts) -> bool:
        """the decision's option check on `opts` (the constructor, which reaches the device only after it): True if refused."""
        try:
            wager2.W2State(None, dict(opts))
        except AssertionError as e:
            return "removal settings" in str(e) or "V2q-D0" in str(e)
        except Exception:
            pass
        return False

    def test_option_check(self):
        self.assertFalse(self._refused(noadv=True))
        self.assertFalse(self._refused(nocom=True))
        self.assertFalse(self._refused(vq="m1", vqd0=True))
        self.assertTrue(self._refused(vqd0=True))
        self.assertTrue(self._refused(vq="inf", noadv=True))
        self.assertTrue(self._refused(vq="inf", nocom=True))

    def test_mapping(self):
        d = ScoutDeviceManager.__new__(ScoutDeviceManager)
        for c in (0.5, 0.9, 0.999, 1.0):
            self.assertEqual(d._adv_c(c), c)                                       # (off: the same float)
        d._adv_k = 1 / 30
        self.assertAlmostEqual(d._adv_c(0.97), 1 - 0.001, places=12)
        d._adv_k = 30.0
        self.assertAlmostEqual(d._adv_c(0.999), 0.97, places=12)
        self.assertEqual(d._adv_c(0.9), 0.5)                                       # (capped at a coin's error)

    def test_name_and_fingerprint(self):
        kw = dict(arm_name="W2YBl1spSd", client="strip", budget=39)
        n0 = run.out_name("adder_4", 13, "D", 4, **kw)
        self.assertEqual(run.out_name("adder_4", 13, "D", 4, adv_err="1/30", **kw).replace("_ae1o30", ""), n0)
        self.assertIn("_ae30_", run.out_name("adder_4", 13, "D", 4, adv_err="30", **kw))
        f0 = run.fingerprint({}, None, 4, None, 0.9, 0, arm_name="W2YBl1spSd")
        f1 = run.fingerprint({}, None, 4, None, 0.9, 0, arm_name="W2YBl1spSd", adv_err="1/30")
        self.assertEqual(f0["shared"], f1["shared"])                               # (N pairs on the shared part)
        self.assertEqual(f1["policy"]["adv_err"], "1/30")
        self.assertNotIn("adv_err", f0["policy"])

    def test_bins(self):
        for e in (REC56_LIVE, REC56_CELLS, REC56_ROUNDS):
            self.assertEqual(list(e), sorted(set(e)))
            self.assertEqual(e[0], 0)
        self.assertEqual((REC56_LIVE[-1], REC56_CELLS[-1]), (65, 513))             # (past the limits: 64 values, 512 cells)


if __name__ == "__main__":
    unittest.main()
