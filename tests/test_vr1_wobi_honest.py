"""VR1 Vacuum Reversal — arm / decel / exit gates (no venue)."""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock

from antigravity.quant_pipeline import run_dryrun_vr1_wobi_honest as vr1
from antigravity.quant_pipeline.run_dryrun_vr1_wobi_honest import VR1WobiHonestDryRun


class TestVR1Gates(unittest.TestCase):
    def _runner(self) -> VR1WobiHonestDryRun:
        r = object.__new__(VR1WobiHonestDryRun)
        r._tape = vr1.deque(maxlen=5000)
        r.vr_phase = "IDLE"
        r.arm_ts = 0.0
        r.arm_sell_btc = 0.0
        r.arm_spread_bp = 0.0
        r.entry_ts = 0.0
        r.vr_tp = 0
        r.vr_time_cuts = 0
        r.wobi3 = MagicMock()
        r.wobi3.position_side = "buy"
        r.wobi3.entry_price = 10_000_000.0
        return r

    def test_constants(self):
        self.assertEqual(VR1WobiHonestDryRun.LANE_NAME, "VR1_WOBI_HONEST")
        self.assertGreaterEqual(vr1.TAKER_BURST_BTC, 0.5)
        self.assertGreaterEqual(vr1.MIN_VACUUM_SPREAD_BP, 3.0)
        self.assertEqual(vr1.TIME_CUT_SEC, 2.5)
        self.assertGreaterEqual(vr1.CHASE_WINDOW_SEC, 0.5)

    def test_vacuum_signal_requires_burst_and_wide_spread(self):
        r = self._runner()
        now = time.time()
        r._tape.append((now, 0.6, 0.0))
        ok, sell, _ = VR1WobiHonestDryRun._vacuum_signal(r, now, 3.5)
        self.assertTrue(ok)
        self.assertGreaterEqual(sell, 0.5)
        ok2, _, why = VR1WobiHonestDryRun._vacuum_signal(r, now, 1.5)
        self.assertFalse(ok2)
        self.assertIn("spread", why)

    def test_chase_blocked_when_sell_continues(self):
        r = self._runner()
        now = time.time()
        r._tape.append((now, 0.20, 0.0))  # > CHASE_SELL_MAX_BTC
        blocked, why = VR1WobiHonestDryRun._chase_blocked(r, now)
        self.assertTrue(blocked)
        self.assertIn("chase_sell", why)
        r2 = self._runner()
        r2._tape.append((now, 0.05, 0.0))
        blocked2, _ = VR1WobiHonestDryRun._chase_blocked(r2, now)
        self.assertFalse(blocked2)

    def test_time_cut_and_tp(self):
        r = self._runner()
        # 1.5s < TIME_CUT 2.5s → still hold
        r.entry_ts = time.time() - 1.5
        cut0, tag0, _, _ = VR1WobiHonestDryRun._eval_wobi3_dynamic_exit(
            r, 10_000_000.0, [], [], {"best_bid": 9_999_000.0}, time.time()
        )
        self.assertFalse(cut0)
        self.assertEqual(tag0, "VR_HOLD")

        r.entry_ts = time.time() - 2.6
        cut, tag, _, why = VR1WobiHonestDryRun._eval_wobi3_dynamic_exit(
            r, 10_000_000.0, [], [], {"best_bid": 9_999_000.0}, time.time()
        )
        self.assertTrue(cut)
        self.assertEqual(tag, "VR_TIME_CUT")
        self.assertIn("vr_time_cut", why)

        r2 = self._runner()
        r2.entry_ts = time.time()
        cut2, tag2, _, why2 = VR1WobiHonestDryRun._eval_wobi3_dynamic_exit(
            r2, 10_003_000.0, [], [], {"best_bid": 10_003_000.0}, time.time()
        )
        self.assertTrue(cut2)
        self.assertEqual(tag2, "VR_TP")
        self.assertIn("vr_tp", why2)


if __name__ == "__main__":
    unittest.main()
