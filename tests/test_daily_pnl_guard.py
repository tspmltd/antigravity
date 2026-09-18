"""
Tests for DailyPnLGuard (GAPCORE-compliant persistent daily loss guard)
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from antigravity.risk_guard.daily_pnl_guard import DailyPnLGuard, get_jst_day_str, JST


class TestDailyPnLGuard(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.strategy_id = "test_strategy"
        self.limit_jpy = 1000.0

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_initial_state(self):
        guard = DailyPnLGuard(
            strategy_id=self.strategy_id,
            limit_jpy=self.limit_jpy,
            state_dir=self.test_dir,
        )
        self.assertEqual(guard.realized_jpy, 0.0)
        self.assertFalse(guard.is_halted)
        self.assertTrue(guard.can_enter())
        self.assertTrue(os.path.exists(guard.state_path))

    def test_record_trade_and_breach(self):
        breached_calls = []

        def on_breach(reason, daily_jpy, limit):
            breached_calls.append((reason, daily_jpy, limit))

        guard = DailyPnLGuard(
            strategy_id=self.strategy_id,
            limit_jpy=self.limit_jpy,
            state_dir=self.test_dir,
            on_breach_callback=on_breach,
        )

        # Small loss: -400 JPY
        res1 = guard.record_trade_pnl(-400.0)
        self.assertEqual(res1["realized_jpy"], -400.0)
        self.assertFalse(res1["halted"])
        self.assertTrue(res1["can_enter"])
        self.assertEqual(len(breached_calls), 0)

        # Gain: +100 JPY -> -300 JPY
        res2 = guard.record_trade_pnl(100.0)
        self.assertEqual(res2["realized_jpy"], -300.0)
        self.assertFalse(res2["halted"])

        # Heavy loss: -800 JPY -> -1100 JPY (breaches limit 1000)
        res3 = guard.record_trade_pnl(-800.0)
        self.assertEqual(res3["realized_jpy"], -1100.0)
        self.assertTrue(res3["halted"])
        self.assertTrue(res3["breached"])
        self.assertFalse(res3["can_enter"])
        self.assertTrue(guard.is_halted)
        self.assertEqual(len(breached_calls), 1)

    def test_persistence_across_restarts(self):
        # 1st run: incurs loss and halts
        guard1 = DailyPnLGuard(
            strategy_id=self.strategy_id,
            limit_jpy=self.limit_jpy,
            state_dir=self.test_dir,
        )
        guard1.record_trade_pnl(-1200.0)
        self.assertTrue(guard1.is_halted)

        # Simulated process restart / crash / watchdog reboot
        guard2 = DailyPnLGuard(
            strategy_id=self.strategy_id,
            limit_jpy=self.limit_jpy,
            state_dir=self.test_dir,
        )
        self.assertEqual(guard2.realized_jpy, -1200.0)
        self.assertTrue(guard2.is_halted)
        self.assertFalse(guard2.can_enter())

    def test_manual_resume(self):
        guard = DailyPnLGuard(
            strategy_id=self.strategy_id,
            limit_jpy=self.limit_jpy,
            state_dir=self.test_dir,
        )
        guard.record_trade_pnl(-1500.0)
        self.assertTrue(guard.is_halted)

        guard.manual_resume(reason="Operator override test")
        self.assertFalse(guard.is_halted)
        self.assertTrue(guard.can_enter())

        # Check that resumed state was saved
        guard_reloaded = DailyPnLGuard(
            strategy_id=self.strategy_id,
            limit_jpy=self.limit_jpy,
            state_dir=self.test_dir,
        )
        self.assertFalse(guard_reloaded.is_halted)

    def test_day_rollover(self):
        guard = DailyPnLGuard(
            strategy_id=self.strategy_id,
            limit_jpy=self.limit_jpy,
            state_dir=self.test_dir,
        )
        guard.record_trade_pnl(-1200.0)
        self.assertTrue(guard.is_halted)

        # Artificially set stored jst_day to yesterday
        guard.jst_day = "2020-01-01"
        guard._save()

        # Check that rollover resets state
        guard._check_day_rollover()
        self.assertEqual(guard.jst_day, get_jst_day_str())
        self.assertEqual(guard.realized_jpy, 0.0)
        self.assertFalse(guard.is_halted)
        self.assertTrue(guard.can_enter())


if __name__ == "__main__":
    unittest.main()
