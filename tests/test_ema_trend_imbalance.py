"""
Tests for EmaTrendTickStrategy with Mid valuation and Order Book Imbalance filter.
"""

import unittest
from antigravity.strategies.ema_trend import EmaTrendTickStrategy


class TestEmaTrendImbalance(unittest.TestCase):
    def setUp(self):
        self.strategy = EmaTrendTickStrategy(
            parameters={
                "fast_span_sec": 10.0,
                "slow_span_sec": 50.0,
                "min_delta_ratio": 0.20,
                "min_imbalance": 0.05,
                "max_spread_bp": 8.0,
                "enable_imbalance_filter": True,
                "use_mid_price": True,
            }
        )

    def test_mid_price_ema_update(self):
        # Tick with different LTP vs Mid
        # Even if price bounces around LTP (10000 -> 10020), mid stays smooth
        t0 = 1000.0
        tick1 = {"price": 10020.0, "mid": 10000.0, "timestamp": t0}
        stats = {"delta_ratio": 0.0, "book_imbalance": 0.0, "spread_bp": 2.0}
        self.strategy.on_tick(tick1, stats, current_pos=0.0, entry_price=0.0)

        # EMA should be initialized to Mid (10000.0), NOT LTP (10020.0)
        self.assertEqual(self.strategy.fast_ema, 10000.0)
        self.assertEqual(self.strategy.slow_ema, 10000.0)

    def test_buy_blocked_by_heavy_ask_imbalance(self):
        # Set up an upward trending EMA
        self.strategy.fast_ema = 10050.0
        self.strategy.slow_ema = 10000.0
        self.strategy.last_tick_time = 1000.0

        tick = {"price": 10050.0, "mid": 10050.0, "timestamp": 1005.0}

        # Case A: Taker delta is positive (0.50), but Orderbook has heavy ASK wall (imb = -0.40)
        # Adverse selection risk -> Entry MUST be blocked by Imbalance filter!
        stats_adverse = {
            "delta_ratio": 0.50,
            "book_imbalance": -0.40,
            "spread_bp": 1.5,
        }
        res_blocked = self.strategy.on_tick(tick, stats_adverse, current_pos=0.0, entry_price=0.0)
        self.assertEqual(res_blocked["action"], "HOLD")
        self.assertIn("板厚不均衡フィルタ見送り", res_blocked["reason"])

        # Case B: Taker delta is positive AND Orderbook is supportive (imb = +0.25)
        # Entry allowed!
        stats_supportive = {
            "delta_ratio": 0.50,
            "book_imbalance": 0.25,
            "spread_bp": 1.5,
        }
        res_allowed = self.strategy.on_tick(tick, stats_supportive, current_pos=0.0, entry_price=0.0)
        self.assertEqual(res_allowed["action"], "BUY")
        self.assertIn("EMA上昇トレンド順張り", res_allowed["reason"])

    def test_sell_blocked_by_heavy_bid_imbalance(self):
        # Set up a downward trending EMA
        self.strategy.fast_ema = 9950.0
        self.strategy.slow_ema = 10000.0
        self.strategy.last_tick_time = 1000.0

        tick = {"price": 9950.0, "mid": 9950.0, "timestamp": 1005.0}

        # Case A: Taker delta is negative (-0.50), but Orderbook has heavy BID wall (imb = +0.30)
        # Entry MUST be blocked!
        stats_adverse = {
            "delta_ratio": -0.50,
            "book_imbalance": 0.30,
            "spread_bp": 1.5,
        }
        res_blocked = self.strategy.on_tick(tick, stats_adverse, current_pos=0.0, entry_price=0.0)
        self.assertEqual(res_blocked["action"], "HOLD")
        self.assertIn("板厚不均衡フィルタ見送り", res_blocked["reason"])

        # Case B: Taker delta negative AND supportive book (imb = -0.20)
        # Entry allowed!
        stats_supportive = {
            "delta_ratio": -0.50,
            "book_imbalance": -0.20,
            "spread_bp": 1.5,
        }
        res_allowed = self.strategy.on_tick(tick, stats_supportive, current_pos=0.0, entry_price=0.0)
        self.assertEqual(res_allowed["action"], "SELL")
        self.assertIn("EMA下降トレンド順張り", res_allowed["reason"])

    def test_spread_filter_blocks_entry(self):
        self.strategy.fast_ema = 10050.0
        self.strategy.slow_ema = 10000.0
        self.strategy.last_tick_time = 1000.0

        tick = {"price": 10050.0, "mid": 10050.0, "timestamp": 1005.0}

        # Spread is excessively wide (12 bp > 8 bp limit)
        stats_wide_spread = {
            "delta_ratio": 0.50,
            "book_imbalance": 0.30,
            "spread_bp": 12.0,
        }
        res = self.strategy.on_tick(tick, stats_wide_spread, current_pos=0.0, entry_price=0.0)
        self.assertEqual(res["action"], "HOLD")
        self.assertIn("スプレッド過大見送り", res["reason"])


if __name__ == "__main__":
    unittest.main()
