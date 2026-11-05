"""TF1A_WOBI_HONEST paper exit: keep +3 after +5, HARD_STOP hits bid."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from antigravity.quant_pipeline.run_dryrun_tf1a_wobi_honest import TF1AWobiHonestDryRun


def _stub(*, entry: float = 10_000_000.0, peak: float = 0.0, ts: float = 1.0):
    obj = SimpleNamespace(
        TARGET_MIN_PROFIT_BP=TF1AWobiHonestDryRun.TARGET_MIN_PROFIT_BP,
        TRAILING_ACTIVATION_BP=TF1AWobiHonestDryRun.TRAILING_ACTIVATION_BP,
        TRAILING_STEP_BP=TF1AWobiHonestDryRun.TRAILING_STEP_BP,
        HARD_STOP_BP=TF1AWobiHonestDryRun.HARD_STOP_BP,
        PROFIT_LOCK_BP=TF1AWobiHonestDryRun.PROFIT_LOCK_BP,
        peak_pnl_bp=peak,
        entry_ts=ts,
        breakeven_locked=False,
        wobi3=SimpleNamespace(entry_price=entry),
    )
    obj._update_trend_context = lambda mid, now: (0.0, 0.0)
    return obj


def _eval(stub, mid: float, now: float = 10.0):
    return TF1AWobiHonestDryRun._eval_tf_exit(stub, mid, {}, now)


class TestTf1aProfitLock(unittest.TestCase):
    def test_lock_floor_is_3_not_half(self):
        self.assertEqual(TF1AWobiHonestDryRun.PROFIT_LOCK_BP, 3.0)
        self.assertEqual(TF1AWobiHonestDryRun.HARD_STOP_BP, -4.0)

    def test_after_plus_5_hold_above_3(self):
        stub = _stub(peak=5.2)
        # +3.5bp: still above lock
        mid = 10_000_000.0 * (1.0 + 3.5e-4)
        ok, tag, _, _ = _eval(stub, mid)
        self.assertFalse(ok)
        self.assertEqual(tag, "HOLD")

    def test_after_plus_5_cut_at_3(self):
        stub = _stub(peak=5.2)
        mid = 10_000_000.0 * (1.0 + 2.9e-4)
        ok, tag, _, why = _eval(stub, mid)
        self.assertTrue(ok)
        self.assertEqual(tag, "BREAKEVEN_LOCK")
        self.assertIn("floor=+3.0bp", why)

    def test_retrace_to_1bp_cuts_now(self):
        """Old floor +0.5 would still hold here. New floor +3 cuts."""
        stub = _stub(peak=5.2)
        mid = 10_000_000.0 * (1.0 + 1.0e-4)
        ok, tag, _, _ = _eval(stub, mid)
        self.assertTrue(ok)
        self.assertEqual(tag, "BREAKEVEN_LOCK")

    def test_hard_stop_still_minus_4(self):
        stub = _stub(peak=0.0)
        mid = 10_000_000.0 * (1.0 - 4.05e-4)
        ok, tag, _, _ = _eval(stub, mid)
        self.assertTrue(ok)
        self.assertEqual(tag, "HARD_STOP")


if __name__ == "__main__":
    unittest.main()
