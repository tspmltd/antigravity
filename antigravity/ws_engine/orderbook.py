"""
OrderBook & MidPrice Tracker (GAPCORE-compliant Microstructure Engine)
=====================================================================
Calculates Mid price, Microprice, and Order Book Imbalance (OBI) in real time.
Eliminates LTP (last traded price) tick-bounce noise and filters out
adverse selection in taker entry strategies.
"""

import math
import threading
from typing import Dict, Any, Optional, Tuple


def calculate_imbalance(bid_size: float, ask_size: float) -> float:
    """
    GAPCORE BookImbalance: (B - A) / (B + A) in [-1.0, 1.0].
    Returns 0.0 if both sides empty or invalid.
    """
    total = bid_size + ask_size
    if total <= 0.0 or math.isnan(total) or math.isinf(total):
        return 0.0
    imb = (bid_size - ask_size) / total
    if math.isnan(imb) or math.isinf(imb):
        return 0.0
    return max(-1.0, min(1.0, imb))


def apply_deadzone(imbalance: float, theta: float = 0.10) -> float:
    """
    GAPCORE ApplyDeadzone: Zeroes weak imbalance noise below theta.
    """
    if theta < 0.0:
        theta = 0.0
    if abs(imbalance) < theta:
        return 0.0
    return imbalance


def calculate_microprice(
    best_bid: float,
    best_ask: float,
    bid_size: float,
    ask_size: float,
) -> float:
    """
    Volume-weighted Mid (Microprice):
    Microprice = (best_bid * ask_size + best_ask * bid_size) / (bid_size + ask_size)
    Leans towards the ask when bid size is thick (buying pressure).
    """
    denom = bid_size + ask_size
    if denom <= 0.0 or best_bid <= 0.0 or best_ask <= 0.0:
        return (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else max(best_bid, best_ask)
    return (best_bid * ask_size + best_ask * bid_size) / denom


class OrderBookTracker:
    """
    Real-time Order Book and Mid-Price Tracker.
    Maintains best quotes, book depths, and imbalance metrics.
    """

    def __init__(self, deadzone_theta: float = 0.10):
        self.deadzone_theta = deadzone_theta
        self.lock = threading.RLock()

        # Best quotes
        self.best_bid: float = 0.0
        self.best_ask: float = 0.0
        self.best_bid_size: float = 0.0
        self.best_ask_size: float = 0.0

        # Aggregate depth (total board or near N levels)
        self.total_bid_depth: float = 0.0
        self.total_ask_depth: float = 0.0

        # Derived metrics
        self.mid_price: float = 0.0
        self.microprice: float = 0.0
        self.spread: float = 0.0
        self.spread_bp: float = 0.0
        self.top_imbalance: float = 0.0
        self.total_imbalance: float = 0.0
        self.last_update_ts: float = 0.0

    def update_from_ticker(self, ticker_data: Dict[str, Any], timestamp: Optional[float] = None) -> Dict[str, Any]:
        """
        Update state from bitFlyer lightning_ticker message.
        Expected fields:
            best_bid, best_ask, best_bid_size, best_ask_size,
            total_bid_depth, total_ask_depth
        """
        with self.lock:
            bb = float(ticker_data.get("best_bid", 0.0))
            ba = float(ticker_data.get("best_ask", 0.0))
            bbs = float(ticker_data.get("best_bid_size", 0.0))
            bas = float(ticker_data.get("best_ask_size", 0.0))
            tbd = float(ticker_data.get("total_bid_depth", 0.0))
            tad = float(ticker_data.get("total_ask_depth", 0.0))

            if bb > 0:
                self.best_bid = bb
            if ba > 0:
                self.best_ask = ba
            if bbs > 0:
                self.best_bid_size = bbs
            if bas > 0:
                self.best_ask_size = bas
            if tbd > 0:
                self.total_bid_depth = tbd
            if tad > 0:
                self.total_ask_depth = tad

            # Recalculate metrics
            if self.best_bid > 0 and self.best_ask > 0 and self.best_ask >= self.best_bid:
                self.mid_price = (self.best_bid + self.best_ask) / 2.0
                self.spread = self.best_ask - self.best_bid
                self.spread_bp = (self.spread / self.mid_price) * 10000.0 if self.mid_price > 0 else 0.0
                self.microprice = calculate_microprice(
                    self.best_bid, self.best_ask, self.best_bid_size, self.best_ask_size
                )
            elif self.best_bid > 0:
                self.mid_price = self.best_bid
            elif self.best_ask > 0:
                self.mid_price = self.best_ask

            # Imbalance metrics
            self.top_imbalance = calculate_imbalance(self.best_bid_size, self.best_ask_size)
            if self.total_bid_depth > 0 or self.total_ask_depth > 0:
                self.total_imbalance = calculate_imbalance(self.total_bid_depth, self.total_ask_depth)
            else:
                self.total_imbalance = self.top_imbalance

            self.last_update_ts = timestamp if timestamp is not None else 0.0

            return self.get_snapshot()

    def get_snapshot(self) -> Dict[str, Any]:
        """Returns thread-safe immutable snapshot of the book state."""
        with self.lock:
            top_imb_clean = apply_deadzone(self.top_imbalance, self.deadzone_theta)
            tot_imb_clean = apply_deadzone(self.total_imbalance, self.deadzone_theta)
            return {
                "mid_price": self.mid_price,
                "best_bid": self.best_bid,
                "best_ask": self.best_ask,
                "best_bid_size": self.best_bid_size,
                "best_ask_size": self.best_ask_size,
                "microprice": self.microprice,
                "spread": self.spread,
                "spread_bp": self.spread_bp,
                "top_imbalance": self.top_imbalance,
                "top_imbalance_clean": top_imb_clean,
                "total_imbalance": self.total_imbalance,
                "total_imbalance_clean": tot_imb_clean,
                "book_imbalance": self.total_imbalance,  # Standard reference
                "is_valid": self.mid_price > 0 and self.spread >= 0,
            }
