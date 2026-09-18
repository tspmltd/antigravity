"""
Tests for GAPCORE Target/Actual Position Management Integration
================================================================
Validates:
1. TargetQty / ActualQty / PendingQty / RequiredQty invariants
2. Single-order doten flip (e.g. -0.001 -> +0.001 in one 0.002 BTC order)
3. Partial fills and multi-leg executions
4. Order rejection / failure rollback
5. External desync reconciliation
"""

import unittest
import time
from antigravity.execution.position_manager import PositionManager


class TestPositionManagerIntegration(unittest.TestCase):
    def test_position_manager_basic_lifecycle(self):
        pm = PositionManager(strategy_id="EmaTrend", product_code="FX_BTC_JPY", lot_size=0.001)

        self.assertEqual(pm.target_qty, 0.0)
        self.assertEqual(pm.actual_qty, 0.0)
        self.assertEqual(pm.pending_qty, 0.0)
        self.assertEqual(pm.required_qty, 0.0)

        # 1. Strategy sets BUY target
        req = pm.set_target(0.001, reason="EMA Golden Cross")
        self.assertEqual(req, 0.001)
        self.assertEqual(pm.required_qty, 0.001)

        # 2. Dispatch order: pending increases, required becomes 0 (preventing duplicate dispatch)
        pm.on_order_sent("BUY", 0.001)
        self.assertEqual(pm.pending_qty, 0.001)
        self.assertEqual(pm.required_qty, 0.0)

        # 3. Order filled
        trade_pnl = pm.on_fill("BUY", 0.001, price=11_500_000.0)
        self.assertEqual(trade_pnl, 0.0)
        self.assertEqual(pm.pending_qty, 0.0)
        self.assertEqual(pm.actual_qty, 0.001)
        self.assertEqual(pm.avg_price, 11_500_000.0)
        self.assertEqual(pm.required_qty, 0.0)

        # 4. Mid price unrealized pnl
        unrealized = pm.get_unrealized_pnl(11_510_000.0)
        self.assertEqual(round(unrealized, 2), 10.0)  # +10,000 * 0.001 = +10 JPY

        # 5. Exit target
        req = pm.set_target(0.0, reason="EMA Dead Cross")
        self.assertEqual(req, -0.001)
        pm.on_order_sent("SELL", 0.001)
        self.assertEqual(pm.pending_qty, -0.001)
        self.assertEqual(pm.required_qty, 0.0)

        # Fill exit
        trade_pnl = pm.on_fill("SELL", 0.001, price=11_510_000.0)
        self.assertEqual(round(trade_pnl, 2), 10.0)
        self.assertEqual(pm.pending_qty, 0.0)
        self.assertEqual(pm.actual_qty, 0.0)
        self.assertEqual(pm.avg_price, 0.0)
        self.assertEqual(pm.realized_pnl, 10.0)

    def test_position_manager_doten_flip_single_order(self):
        """GAPCORE hallmark: -0.001 -> +0.001 executed in a single 0.002 BTC order"""
        pm = PositionManager(
            strategy_id="EmaTrend",
            product_code="FX_BTC_JPY",
            lot_size=0.001,
            initial_actual_qty=-0.001,
            initial_avg_price=11_500_000.0,
        )

        self.assertEqual(pm.actual_qty, -0.001)
        self.assertEqual(pm.target_qty, -0.001)
        self.assertEqual(pm.pending_qty, 0.0)
        self.assertEqual(pm.required_qty, 0.0)

        # Strategy flips to BUY: Target = +0.001
        req = pm.set_target(+0.001, reason="Bullish Reversal")
        # Invariant: Required = Target - Actual - Pending = +0.001 - (-0.001) - 0 = +0.002
        self.assertEqual(req, 0.002)
        self.assertEqual(pm.required_qty, 0.002)

        # Dispatch single 0.002 BUY order
        pm.on_order_sent("BUY", 0.002)
        self.assertEqual(pm.pending_qty, 0.002)
        self.assertEqual(pm.required_qty, 0.0)

        # Fill 0.002 BUY @ 11,490,000 (Short profit: (11,500,000 - 11,490,000) * 0.001 = +10 JPY)
        trade_pnl = pm.on_fill("BUY", 0.002, price=11_490_000.0)
        self.assertEqual(round(trade_pnl, 2), 10.0)
        self.assertEqual(pm.pending_qty, 0.0)
        self.assertEqual(pm.actual_qty, +0.001)
        self.assertEqual(pm.avg_price, 11_490_000.0)
        self.assertEqual(pm.realized_pnl, 10.0)
        self.assertEqual(pm.required_qty, 0.0)

    def test_position_manager_partial_fills(self):
        """Partial fills: 0.001 order filled in two 0.0005 slices"""
        pm = PositionManager(lot_size=0.001)
        pm.set_target(0.001)
        self.assertEqual(pm.required_qty, 0.001)

        pm.on_order_sent("BUY", 0.001)
        self.assertEqual(pm.pending_qty, 0.001)
        self.assertEqual(pm.required_qty, 0.0)

        # 1st slice fill
        pnl1 = pm.on_fill("BUY", 0.0005, price=11_500_000.0)
        self.assertEqual(pnl1, 0.0)
        self.assertEqual(pm.pending_qty, 0.0005)
        self.assertEqual(pm.actual_qty, 0.0005)
        self.assertEqual(pm.required_qty, 0.0)

        # 2nd slice fill
        pnl2 = pm.on_fill("BUY", 0.0005, price=11_502_000.0)
        self.assertEqual(pnl2, 0.0)
        self.assertEqual(pm.pending_qty, 0.0)
        self.assertEqual(pm.actual_qty, 0.001)
        # Weighted avg price: (0.0005 * 11500000 + 0.0005 * 11502000) / 0.001 = 11501000
        self.assertEqual(pm.avg_price, 11_501_000.0)
        self.assertEqual(pm.required_qty, 0.0)

    def test_position_manager_order_failure_rollback(self):
        """Reverts pending on failure"""
        pm = PositionManager(lot_size=0.001)
        pm.set_target(0.001)
        self.assertEqual(pm.required_qty, 0.001)

        pm.on_order_sent("BUY", 0.001)
        self.assertEqual(pm.pending_qty, 0.001)
        self.assertEqual(pm.required_qty, 0.0)

        # Order rejected by exchange
        pm.on_order_failed("BUY", 0.001)
        self.assertEqual(pm.pending_qty, 0.0)
        self.assertEqual(pm.actual_qty, 0.0)
        # Required returns to 0.001 so engine can retry if desired
        self.assertEqual(pm.required_qty, 0.001)

    def test_position_manager_reconciliation(self):
        """Handles external exchange desync gracefully"""
        pm = PositionManager(initial_actual_qty=0.0)
        diff = pm.reconcile_actual(0.001, avg_price=11_500_000.0)
        self.assertEqual(diff, 0.001)
        self.assertEqual(pm.actual_qty, 0.001)
        self.assertEqual(pm.avg_price, 11_500_000.0)

        # When target was 0, now required is -0.001
        self.assertEqual(pm.required_qty, -0.001)


if __name__ == "__main__":
    unittest.main()
