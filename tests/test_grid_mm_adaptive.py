"""
tests/test_grid_mm_adaptive.py: Unit tests for Regime-Adaptive GridMM Strategy
"""

import unittest
import time
from antigravity.strategies.grid_mm import GridMmStrategy
from antigravity.execution.portfolio_orchestrator import PortfolioOrchestrator
from antigravity.risk_guard.order_rate_guard import OrderRateGuard
from antigravity.risk_guard.portfolio_pnl_guard import PortfolioDailyPnLGuard


class TestGridMmAdaptive(unittest.TestCase):
    def setUp(self):
        self.strat = GridMmStrategy(
            order_size=0.001,
            max_spread_jpy=1500.0,
            max_atr_jpy=20000.0,
            base_tp_jpy=4.0,
            min_tp_jpy=3.0,
            max_tp_jpy=7.0,
            sl_tp_ratio=1.3,
            grid_reversion_bp=5.0,
        )

    def test_spread_filter_blocks_entry(self):
        """When spread exceeds 1500 JPY, new entry must be blocked (HOLD)."""
        now = time.time()
        # Initialize baseline
        self.strat.on_tick({"price": 12000000.0, "timestamp": now}, {"mid_price": 12000000.0, "spread": 500.0}, 0.0, 0.0)

        # Price deviates far below baseline (-10bp), but spread is 2000 JPY (> 1500 JPY limit)
        tick = {"price": 11985000.0, "timestamp": now + 1.0}
        flow = {"mid_price": 11985000.0, "spread": 2000.0, "book_imbalance": 0.5}
        res = self.strat.on_tick(tick, flow, 0.0, 0.0)

        self.assertEqual(res["action"], "HOLD")
        self.assertEqual(res["target_qty"], 0.0)
        self.assertIn("スプレッドフィルター遮断", res["reason"])

    def test_volatility_filter_blocks_entry(self):
        """When ATR spike exceeds 20000 JPY, new entry must be blocked."""
        now = time.time()
        # Initialize
        self.strat.on_tick({"price": 12000000.0, "timestamp": now}, {"mid_price": 12000000.0, "spread": 500.0}, 0.0, 0.0)
        # Inject artificial massive spike in ATR
        self.strat.micro_atr = 25000.0

        tick = {"price": 11985000.0, "timestamp": now + 1.0}
        flow = {"mid_price": 11985000.0, "spread": 800.0, "book_imbalance": 0.5}
        res = self.strat.on_tick(tick, flow, 0.0, 0.0)

        self.assertEqual(res["action"], "HOLD")
        self.assertEqual(res["target_qty"], 0.0)
        self.assertIn("ボラティリティフィルター遮断", res["reason"])

    def test_dynamic_tp_and_sl_execution(self):
        """Verify dynamic TP and dynamic SL calculations and exits."""
        now = time.time()
        self.strat.on_tick({"price": 12000000.0, "timestamp": now}, {"mid_price": 12000000.0, "spread": 500.0}, 0.0, 0.0)

        # Dynamic TP test for LONG (entry @ 12,000,000, current @ 12,005,000 -> gain = +5.0 JPY)
        tick_tp = {"price": 12005000.0, "timestamp": now + 10.0}
        flow = {"mid_price": 12005000.0, "spread": 500.0}
        res_tp = self.strat.on_tick(tick_tp, flow, current_pos=0.001, entry_price=12000000.0)
        self.assertEqual(res_tp["action"], "EXIT")
        self.assertEqual(res_tp["target_qty"], 0.0)
        self.assertIn("動的TP利確", res_tp["reason"])

        # Dynamic SL test for LONG (entry @ 12,000,000, current @ 11,993,000 -> loss = -7.0 JPY)
        # SL is approx 4.0 * 1.3 = 5.2 JPY. -7.0 <= -5.2 -> triggers EXIT
        tick_sl = {"price": 11993000.0, "timestamp": now + 15.0}
        flow = {"mid_price": 11993000.0, "spread": 500.0}
        res_sl = self.strat.on_tick(tick_sl, flow, current_pos=0.001, entry_price=12000000.0)
        self.assertEqual(res_sl["action"], "EXIT")
        self.assertEqual(res_sl["target_qty"], 0.0)
        self.assertIn("動的SL損切", res_sl["reason"])

    def test_regime_switch_in_portfolio_orchestrator(self):
        """Verify that in TREND regime, GridMM allocation is automatically 0.0."""
        rate_guard = OrderRateGuard(max_orders_per_min=60, min_order_interval_sec=0.1, global_min_interval_sec=0.0)
        pf_guard = PortfolioDailyPnLGuard(portfolio_limit_jpy=3000.0)
        orch = PortfolioOrchestrator(
            symbol="FX_BTC_JPY",
            order_size=0.001,
            portfolio_guard=pf_guard,
            order_rate_guard=rate_guard,
            enable_internal_netting=True,
            enable_regime_switch=True,
        )

        now = time.time()
        # Feed high trend data: Fast EMA far above Slow EMA (1.5bp+ divergence)
        # mid = 12,000,000, fast_ema = 12,010,000, slow_ema = 11,990,000 -> diff = 20,000 JPY = 16.6bp
        orch.strategies["EmaTrend"].fast_ema = 12010000.0
        orch.strategies["EmaTrend"].slow_ema = 11990000.0

        tick = {"price": 12000000.0, "timestamp": now}
        flow = {"mid_price": 12000000.0, "spread_bp": 0.8, "spread": 960.0}

        orch.on_tick(tick, flow, now_ts=now)

        self.assertEqual(orch.current_regime, "TREND")
        # Under TREND, GridMM weight must be 0.0
        self.assertEqual(orch.current_weights.get("GridMM"), 0.0)
        # Under TREND, EmaTrend weight must be 1.0
        self.assertEqual(orch.current_weights.get("EmaTrend"), 1.0)


if __name__ == "__main__":
    unittest.main()
