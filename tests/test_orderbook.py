"""
Tests for OrderBookTracker and GAPCORE microstructure functions.
"""

import unittest
from antigravity.ws_engine.orderbook import (
    OrderBookTracker,
    calculate_imbalance,
    calculate_microprice,
    apply_deadzone,
)


class TestOrderBook(unittest.TestCase):
    def test_calculate_imbalance(self):
        # Equal sizes
        self.assertAlmostEqual(calculate_imbalance(10.0, 10.0), 0.0)
        # Bid dominant
        self.assertAlmostEqual(calculate_imbalance(10.0, 0.0), 1.0)
        # Ask dominant
        self.assertAlmostEqual(calculate_imbalance(0.0, 10.0), -1.0)
        # Empty book
        self.assertAlmostEqual(calculate_imbalance(0.0, 0.0), 0.0)
        # Ratio 3:1 -> (3 - 1) / (3 + 1) = 0.5
        self.assertAlmostEqual(calculate_imbalance(7.5, 2.5), 0.5)

    def test_apply_deadzone(self):
        # Within deadzone
        self.assertEqual(apply_deadzone(0.08, theta=0.10), 0.0)
        self.assertEqual(apply_deadzone(-0.05, theta=0.10), 0.0)
        # Outside deadzone
        self.assertEqual(apply_deadzone(0.15, theta=0.10), 0.15)
        self.assertEqual(apply_deadzone(-0.25, theta=0.10), -0.25)

    def test_calculate_microprice(self):
        # Symmetric book -> Microprice = Mid
        bb, ba = 10000.0, 10010.0
        mid = 10005.0
        self.assertAlmostEqual(calculate_microprice(bb, ba, 1.0, 1.0), mid)

        # Thick bid (3.0) vs thin ask (1.0) -> Microprice leans toward ask
        # (10000 * 1.0 + 10010 * 3.0) / 4.0 = 40030 / 4 = 10007.5
        mp = calculate_microprice(bb, ba, 3.0, 1.0)
        self.assertAlmostEqual(mp, 10007.5)
        self.assertGreater(mp, mid)

    def test_orderbook_tracker_update(self):
        tracker = OrderBookTracker(deadzone_theta=0.10)
        ticker_msg = {
            "product_code": "FX_BTC_JPY",
            "best_bid": 11770000.0,
            "best_ask": 11772000.0,
            "best_bid_size": 2.0,
            "best_ask_size": 1.0,
            "total_bid_depth": 150.0,
            "total_ask_depth": 100.0,
        }

        snap = tracker.update_from_ticker(ticker_msg)

        # Mid = (11770000 + 11772000) / 2 = 11771000
        self.assertEqual(snap["mid_price"], 11771000.0)
        self.assertEqual(snap["spread"], 2000.0)
        # Spread BP = (2000 / 11771000) * 10000 =~ 1.699 bp
        self.assertAlmostEqual(snap["spread_bp"], (2000.0 / 11771000.0) * 10000.0, places=2)

        # Top Imb = (2 - 1) / 3 =~ +0.333
        self.assertAlmostEqual(snap["top_imbalance"], 1.0 / 3.0, places=3)
        # Total Imb = (150 - 100) / 250 = 50 / 250 = +0.20
        self.assertAlmostEqual(snap["total_imbalance"], 0.20, places=3)
        self.assertTrue(snap["is_valid"])


if __name__ == "__main__":
    unittest.main()
