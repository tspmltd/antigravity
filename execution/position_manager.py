"""
PositionManager: GAPCORE-compliant Target/Actual Position & Execution Engine
============================================================================
Decouples trading strategy (the brain) from order execution (the hands).

GAPCore Invariant:
    RequiredQty = TargetQty - ActualQty - PendingQty

Strategies only express intent by setting TargetQty (e.g. +0.001, 0.0, -0.001).
The execution engine computes RequiredQty and safely executes the difference,
handling partial fills, pyramid adds, doten flips, and cancellations autonomously.
"""

import time
import threading
from typing import Optional, Dict, Any, Tuple


class PositionManager:
    """
    GAPCORE-compliant Position Manager.
    Tracks TargetQty, ActualQty, and PendingQty with thread-safe lock.
    """

    def __init__(
        self,
        strategy_id: str = "EmaTrend",
        product_code: str = "FX_BTC_JPY",
        lot_size: float = 0.001,
        initial_actual_qty: float = 0.0,
        initial_avg_price: float = 0.0,
        initial_realized_pnl: float = 0.0,
    ):
        self.strategy_id = strategy_id
        self.product_code = product_code
        self.lot_size = lot_size

        self.lock = threading.RLock()

        # Core GAPCORE position state
        self.target_qty: float = initial_actual_qty
        self.actual_qty: float = initial_actual_qty
        self.pending_qty: float = 0.0

        self.target_reason: str = "Initial"
        self.target_revision: int = 0

        # Accounting state
        self.avg_price: float = initial_avg_price
        self.realized_pnl: float = initial_realized_pnl
        self.fees_jpy: float = 0.0
        self.oldest_fill_time: Optional[float] = None

    @property
    def required_qty(self) -> float:
        """
        RequiredQty is the only position adjustment formula in GAPCore.
        Positive -> Need to BUY.
        Negative -> Need to SELL.
        Zero     -> Target matches Actual + Pending (no order needed).
        """
        with self.lock:
            val = self.target_qty - self.actual_qty - self.pending_qty
            # Round to 6 decimal places to prevent floating point dust
            return round(val, 6)

    def set_target(self, target_qty: float, reason: str = "") -> float:
        """
        Strategy interface: sets the intended target position.
        Returns the resulting required_qty.
        """
        with self.lock:
            target_qty = round(target_qty, 6)
            if abs(self.target_qty - target_qty) > 1e-9:
                self.target_qty = target_qty
                self.target_reason = reason
                self.target_revision += 1
            return self.required_qty

    def on_order_sent(self, side: str, size: float) -> None:
        """
        Called immediately after an order is dispatched to exchange.
        Updates PendingQty to prevent double orders.
        """
        with self.lock:
            delta = size if side.upper() == "BUY" else -size
            self.pending_qty = round(self.pending_qty + delta, 6)

    def on_order_failed(self, side: str, size: float) -> None:
        """
        Called if an order fails or is rejected, reverting PendingQty.
        """
        with self.lock:
            delta = size if side.upper() == "BUY" else -size
            self.pending_qty = round(self.pending_qty - delta, 6)

    def on_fill(
        self,
        side: str,
        size: float,
        price: float,
        timestamp: Optional[float] = None,
    ) -> float:
        """
        Applies a verified fill.
        - Deducts filled quantity from PendingQty.
        - Adds to ActualQty.
        - Computes Realized PnL and updates avg_price and oldest_fill_time.
        Returns trade_pnl from this fill.
        """
        now = timestamp if timestamp is not None else time.time()
        trade_pnl = 0.0

        with self.lock:
            fill_delta = size if side.upper() == "BUY" else -size

            # Deduct from pending (bring closer to 0)
            if self.pending_qty > 0 and fill_delta > 0:
                self.pending_qty = max(0.0, round(self.pending_qty - fill_delta, 6))
            elif self.pending_qty < 0 and fill_delta < 0:
                self.pending_qty = min(0.0, round(self.pending_qty - fill_delta, 6))
            else:
                self.pending_qty = round(self.pending_qty - fill_delta, 6)

            prev_actual = self.actual_qty
            next_actual = round(prev_actual + fill_delta, 6)

            # 1. Opening from flat
            if abs(prev_actual) < 1e-9:
                self.actual_qty = next_actual
                self.avg_price = price
                self.oldest_fill_time = now if abs(next_actual) > 1e-9 else None
                return 0.0

            # 2. Same direction (pyramid / increase)
            if (prev_actual > 0 and fill_delta > 0) or (prev_actual < 0 and fill_delta < 0):
                total_size = abs(prev_actual) + size
                if total_size > 1e-9:
                    self.avg_price = (abs(prev_actual) * self.avg_price + size * price) / total_size
                self.actual_qty = next_actual
                return 0.0

            # 3. Opposite direction (closing or doten flip)
            close_qty = min(abs(prev_actual), size)
            if prev_actual > 0:
                trade_pnl = (price - self.avg_price) * close_qty
            else:
                trade_pnl = (self.avg_price - price) * close_qty

            self.realized_pnl += trade_pnl
            self.actual_qty = next_actual

            if abs(next_actual) < 1e-9:
                # Fully closed to FLAT
                self.actual_qty = 0.0
                self.avg_price = 0.0
                self.oldest_fill_time = None
            elif (prev_actual > 0 and next_actual < 0) or (prev_actual < 0 and next_actual > 0):
                # Doten flip: remaining quantity forms new position at current fill price
                self.avg_price = price
                self.oldest_fill_time = now

            return trade_pnl

    def get_unrealized_pnl(self, mid_price: float) -> float:
        """GAPCORE-compliant Mid-price valuation."""
        with self.lock:
            if abs(self.actual_qty) < 1e-9 or self.avg_price <= 0.0 or mid_price <= 0.0:
                return 0.0
            if self.actual_qty > 0:
                return (mid_price - self.avg_price) * self.actual_qty
            else:
                return (self.avg_price - mid_price) * abs(self.actual_qty)

    def reconcile_actual(self, exchange_position: float, avg_price: Optional[float] = None) -> float:
        """
        Reconcile actual position with exchange account report.
        Returns position difference.
        """
        with self.lock:
            diff = exchange_position - self.actual_qty
            if abs(diff) > 1e-9:
                print(
                    f"[PositionManager] ℹ️ Reconciling position mismatch: local={self.actual_qty:+.4f} "
                    f"-> exchange={exchange_position:+.4f} (diff={diff:+.4f})",
                    flush=True
                )
                self.actual_qty = exchange_position
                if avg_price and avg_price > 0:
                    self.avg_price = avg_price
                if abs(self.actual_qty) < 1e-9:
                    self.oldest_fill_time = None
                elif self.oldest_fill_time is None:
                    self.oldest_fill_time = time.time()
            return diff

    def get_snapshot(self, mid_price: float = 0.0) -> Dict[str, Any]:
        """Returns thread-safe snapshot dictionary."""
        with self.lock:
            unrealized = self.get_unrealized_pnl(mid_price)
            req = self.required_qty
            return {
                "target_qty": self.target_qty,
                "actual_qty": self.actual_qty,
                "pending_qty": self.pending_qty,
                "required_qty": req,
                "needs_order": abs(req) >= self.lot_size,
                "target_reason": self.target_reason,
                "target_revision": self.target_revision,
                "avg_price": self.avg_price,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": unrealized,
                "total_pnl": self.realized_pnl + unrealized,
                "oldest_fill_time": self.oldest_fill_time,
            }
