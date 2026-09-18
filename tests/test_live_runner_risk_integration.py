"""
Integration test verifying DailyPnLGuard and AgingGuard integration in run_live_production.
"""

import os
import shutil
import tempfile
import unittest
from run_live_production import LiveRunner


class TestLiveRunnerRiskIntegration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_live_runner_risk_guards_initialized(self):
        runner = LiveRunner(
            symbol="FX_BTC_JPY",
            order_size=0.001,
            max_drawdown_limit=1500.0,
            daily_limit_jpy=1200.0,
            max_hold_seconds=200.0,
            enable_real_order=False,
            initial_collateral=7000.0,
            state_dir=self.test_dir,
        )

        # Verify DailyPnLGuard is initialized and wired
        self.assertIsNotNone(runner.daily_pnl_guard)
        self.assertEqual(runner.daily_pnl_guard.limit_jpy, 1200.0)
        self.assertTrue(runner.daily_pnl_guard.can_enter())

        # Verify AgingGuard is initialized and wired
        self.assertIsNotNone(runner.aging_guard)
        self.assertEqual(runner.aging_guard.max_hold_seconds, 200.0)
        self.assertFalse(runner.aging_guard.has_position)

    def test_aging_timeout_order_dispatch(self):
        runner = LiveRunner(
            symbol="FX_BTC_JPY",
            order_size=0.001,
            max_drawdown_limit=1500.0,
            daily_limit_jpy=1200.0,
            max_hold_seconds=5.0,  # Short 5s timeout
            enable_real_order=False,
            initial_collateral=7000.0,
            state_dir=self.test_dir,
        )
        runner.current_price = 10000000.0
        runner.is_running = True

        # Simulate entry
        runner._dispatch_order("BUY", 0.001, 10000000.0, "Test entry")
        self.assertEqual(runner.current_position_btc, 0.001)
        self.assertTrue(runner.aging_guard.has_position)

        # Allow rate limiter to pass for testing
        runner.rate_limiter.last_order_ts = 0.0

        # Dispatch ticks 6 seconds later -> Aging timeout must trigger an exit order
        t0 = runner.aging_guard.oldest_fill_time
        ticks = [
            {"price": 10005000.0, "timestamp": t0 + 6.0}
        ]

        stats = {"flow": "neutral"}
        runner._on_ticks(ticks, stats)

        # After timeout exit, position must be FLAT
        self.assertEqual(runner.current_position_btc, 0.0)
        self.assertFalse(runner.aging_guard.has_position)
        self.assertEqual(len(runner.trades_history), 1)
        self.assertIn("在庫滞留タイムアウト", runner.trades_history[0]["reason"])

    def test_daily_loss_limit_blocks_new_orders(self):
        runner = LiveRunner(
            symbol="FX_BTC_JPY",
            order_size=0.001,
            max_drawdown_limit=1500.0,
            daily_limit_jpy=50.0,  # Tight 50 JPY limit
            max_hold_seconds=300.0,
            enable_real_order=False,
            initial_collateral=7000.0,
            state_dir=self.test_dir,
        )
        runner.current_price = 10000000.0
        runner.is_running = True

        # Incur a loss of -100 JPY (exceeding daily limit 50 JPY)
        runner.daily_pnl_guard.record_trade_pnl(-100.0)
        self.assertTrue(runner.daily_pnl_guard.is_halted)
        self.assertFalse(runner.daily_pnl_guard.can_enter())

        # Attempt to trigger a BUY tick signal
        ticks = [{"price": 10000000.0, "timestamp": 1000.0}]
        stats = {"flow": "buy"}
        runner._on_ticks(ticks, stats)

        # Position must remain FLAT (orders blocked)
        self.assertEqual(runner.current_position_btc, 0.0)


if __name__ == "__main__":
    unittest.main()
