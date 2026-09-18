"""
tests/test_portfolio_backtester.py: Unit and Integration Test for PortfolioBacktester
"""

import unittest
import pandas as pd
from antigravity.backtest.portfolio_backtester import PortfolioBacktester
from antigravity.backtest.regime_detector import RegimeDetector


class TestPortfolioBacktester(unittest.TestCase):
    def setUp(self):
        # Create minimal synthetic OHLCV dataframe (5 bars)
        data = {
            "timestamp": [
                "2026-09-16 00:00:00",
                "2026-09-16 00:01:00",
                "2026-09-16 00:02:00",
                "2026-09-16 00:03:00",
                "2026-09-16 00:04:00",
            ],
            "open": [10000000.0, 10010000.0, 10020000.0, 10015000.0, 10010000.0],
            "high": [10015000.0, 10025000.0, 10030000.0, 10020000.0, 10015000.0],
            "low": [9995000.0, 10005000.0, 10010000.0, 10005000.0, 10005000.0],
            "close": [10010000.0, 10020000.0, 10015000.0, 10010000.0, 10012000.0],
            "volume": [1.0, 2.0, 1.5, 1.2, 0.8],
        }
        self.df = pd.DataFrame(data)

    def test_regime_detector_classification(self):
        detector = RegimeDetector(trend_ema_diff_bp=1.5, range_ema_diff_bp=0.5, high_vol_spread_bp=4.0)

        # 1. High Vol
        regime, weights = detector.detect(mid_price=10000000.0, fast_ema=10000000.0, slow_ema=10000000.0, spread_bp=5.0)
        self.assertEqual(regime, "HIGH_VOL")
        self.assertEqual(weights["EmaTrend"], 0.0)

        # 2. Strong Trend
        regime, weights = detector.detect(mid_price=10000000.0, fast_ema=10005000.0, slow_ema=10000000.0, spread_bp=1.0)
        self.assertEqual(regime, "TREND")
        self.assertEqual(weights["EmaTrend"], 1.0)
        self.assertEqual(weights["MeanReversion"], 0.0)

        # 3. Tight Range
        regime, weights = detector.detect(mid_price=10000000.0, fast_ema=10000100.0, slow_ema=10000000.0, spread_bp=0.5)
        self.assertEqual(regime, "RANGE")
        self.assertEqual(weights["MeanReversion"], 1.0)

    def test_portfolio_backtester_runs_and_returns_valid_metrics(self):
        bt = PortfolioBacktester(
            enable_internal_netting=True,
            enable_rate_limit=True,
            enable_regime_switch=True,
            enable_daily_cap=True,
            order_size=0.001,
        )
        res = bt.run(self.df, mode="portfolio")

        self.assertIn("net_profit_jpy", res)
        self.assertIn("max_drawdown_jpy", res)
        self.assertIn("sharpe_ratio", res)
        self.assertIn("netting_events_count", res)
        self.assertIn("spread_savings_jpy", res)
        self.assertIn("strategy_breakdown", res)
        self.assertIn("regime_counts", res)
        self.assertGreaterEqual(len(res["strategy_breakdown"]), 3)


if __name__ == "__main__":
    unittest.main()
