"""
Tests for PortfolioDailyPnLGuard (GAPCORE Multi-Strategy Portfolio Risk Manager)
================================================================================
"""

import os
import json
import shutil
import tempfile
import unittest
from antigravity.risk_guard.portfolio_pnl_guard import PortfolioDailyPnLGuard


class TestPortfolioDailyPnLGuard(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_initialization_and_persistence(self):
        guard = PortfolioDailyPnLGuard(
            portfolio_limit_jpy=3000.0,
            strategy_limits={"EmaTrend": 1500.0, "MeanReversion": 1000.0},
            state_dir=self.test_dir,
        )
        self.assertEqual(guard.portfolio_realized_jpy, 0.0)
        self.assertFalse(guard.portfolio_halted)
        self.assertTrue(os.path.exists(guard.portfolio_state_path))

        # Check entry permissions
        allowed, reason = guard.can_enter("EmaTrend")
        self.assertTrue(allowed)
        self.assertEqual(reason, "ALLOWED")

        allowed, reason = guard.can_enter("MeanReversion")
        self.assertTrue(allowed)

    def test_individual_strategy_halt_does_not_halt_other_strategies(self):
        guard = PortfolioDailyPnLGuard(
            portfolio_limit_jpy=3000.0,
            strategy_limits={"EmaTrend": 1500.0, "MeanReversion": 1000.0},
            state_dir=self.test_dir,
        )

        # EmaTrend incurs 1600 loss -> exceeds its 1500 limit
        res = guard.record_trade_pnl("EmaTrend", -1600.0)
        self.assertEqual(res["portfolio_realized_jpy"], -1600.0)
        self.assertFalse(res["portfolio_halted"])  # 1600 < 3000

        # EmaTrend must be halted
        allowed_ema, reason_ema = guard.can_enter("EmaTrend")
        self.assertFalse(allowed_ema)
        self.assertIn("個別日次リミット到達", reason_ema)

        # MeanReversion must still be allowed!
        allowed_mr, reason_mr = guard.can_enter("MeanReversion")
        self.assertTrue(allowed_mr)
        self.assertEqual(reason_mr, "ALLOWED")

    def test_portfolio_limit_breach_halts_all_strategies(self):
        breach_calls = []

        def on_breach(reason, pnl, limit):
            breach_calls.append((reason, pnl, limit))

        guard = PortfolioDailyPnLGuard(
            portfolio_limit_jpy=2000.0,
            strategy_limits={"EmaTrend": 1500.0, "MeanReversion": 1000.0},
            state_dir=self.test_dir,
            on_portfolio_breach_callback=on_breach,
        )

        # Both strategies take moderate losses, neither alone breaches their limit
        guard.record_trade_pnl("EmaTrend", -1200.0)  # EmaTrend limit 1500 -> OK
        self.assertTrue(guard.can_enter("MeanReversion")[0])

        guard.record_trade_pnl("MeanReversion", -900.0)  # MR limit 1000 -> OK
        # Combined portfolio loss = -2100.0 <= -2000.0 limit -> Portfolio breach!

        self.assertTrue(guard.portfolio_halted)
        self.assertEqual(len(breach_calls), 1)

        # Now BOTH strategies must be rejected at the portfolio level
        allowed_ema, reason_ema = guard.can_enter("EmaTrend")
        self.assertFalse(allowed_ema)
        self.assertIn("ポートフォリオ全体日次リミット到達", reason_ema)

        allowed_mr, reason_mr = guard.can_enter("MeanReversion")
        self.assertFalse(allowed_mr)
        self.assertIn("ポートフォリオ全体日次リミット到達", reason_mr)

    def test_persistence_across_restarts(self):
        # 1st session
        guard1 = PortfolioDailyPnLGuard(
            portfolio_limit_jpy=3000.0,
            strategy_limits={"EmaTrend": 1500.0},
            state_dir=self.test_dir,
        )
        guard1.record_trade_pnl("EmaTrend", -550.0)

        # 2nd session (simulating process restart)
        guard2 = PortfolioDailyPnLGuard(
            portfolio_limit_jpy=3000.0,
            strategy_limits={"EmaTrend": 1500.0},
            state_dir=self.test_dir,
        )
        self.assertEqual(guard2.portfolio_realized_jpy, -550.0)
        self.assertEqual(guard2.get_strategy_guard("EmaTrend").realized_jpy, -550.0)


if __name__ == "__main__":
    unittest.main()
