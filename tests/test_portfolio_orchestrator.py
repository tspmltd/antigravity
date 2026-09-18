"""
tests/test_portfolio_orchestrator.py: Unit tests for PortfolioOrchestrator
"""

import unittest
import time
from antigravity.execution.portfolio_orchestrator import PortfolioOrchestrator
from antigravity.risk_guard.order_rate_guard import OrderRateGuard
from antigravity.risk_guard.portfolio_pnl_guard import PortfolioDailyPnLGuard


class TestPortfolioOrchestrator(unittest.TestCase):
    def setUp(self):
        self.rate_guard = OrderRateGuard(max_orders_per_min=60, min_order_interval_sec=0.1, global_min_interval_sec=0.0)
        self.pf_guard = PortfolioDailyPnLGuard(
            portfolio_limit_jpy=3000.0,
            strategy_limits={"EmaTrend": 1500.0, "MeanReversion": 1000.0, "OrderBookImbalance": 500.0},
        )
        self.orchestrator = PortfolioOrchestrator(
            symbol="FX_BTC_JPY",
            order_size=0.001,
            portfolio_guard=self.pf_guard,
            order_rate_guard=self.rate_guard,
            enable_internal_netting=True,
            enable_regime_switch=False,  # Keep normal regime for deterministic test
        )

    def test_internal_netting_offsets_opposing_orders(self):
        """When strategies produce offsetting targets, physical required qty is 0 and netting events count increments."""
        # Manually force opposing signals
        # EmaTrend wants +0.001 (BUY), MeanReversion wants -0.001 (SELL)
        # We can directly test on_tick by passing mock stats
        self.orchestrator.pos_managers["EmaTrend"].set_target(0.001)
        self.orchestrator.pos_managers["MeanReversion"].set_target(-0.001)
        self.orchestrator.pos_managers["OrderBookImbalance"].set_target(0.0)

        # Target PF is +0.001 + -0.001 + 0 = 0.0
        self.assertEqual(self.orchestrator.portfolio_target_qty, 0.0)
        self.assertEqual(self.orchestrator.required_exchange_qty, 0.0)

    def test_physical_order_dispatched_for_unbalanced_targets(self):
        """When net portfolio target differs from exchange actual, order is dispatched."""
        # Force net positive target
        self.orchestrator.pos_managers["EmaTrend"].set_target(0.001)
        self.orchestrator.pos_managers["MeanReversion"].set_target(0.0)
        self.orchestrator.pos_managers["OrderBookImbalance"].set_target(0.0)

        self.assertEqual(self.orchestrator.portfolio_target_qty, 0.001)
        self.assertEqual(self.orchestrator.required_exchange_qty, 0.001)

        # Simulate sending order
        self.orchestrator.on_order_sent("BUY", 0.001)
        self.assertEqual(self.orchestrator.exchange_pending_qty, 0.001)
        self.assertEqual(self.orchestrator.required_exchange_qty, 0.0)

        # Simulate fill
        pnl = self.orchestrator.on_exchange_fill("BUY", 0.001, 10000000.0)
        self.assertEqual(self.orchestrator.exchange_actual_qty, 0.001)
        self.assertEqual(self.orchestrator.exchange_pending_qty, 0.0)
        self.assertEqual(self.orchestrator.pos_managers["EmaTrend"].actual_qty, 0.001)


if __name__ == "__main__":
    unittest.main()
