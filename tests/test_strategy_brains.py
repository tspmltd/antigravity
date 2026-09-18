"""
Tests for GAPCORE StrategyBrain Implementations
================================================
Validates:
1. MeanReversionStrategy (decide_target_qty, reversion trigger, exit)
2. OrderBookImbalanceStrategy (decide_target_qty, OBI trigger, reverse escape)
"""

import unittest
from antigravity.strategies.mean_reversion import MeanReversionStrategy
from antigravity.strategies.orderbook_imbalance import OrderBookImbalanceStrategy


class TestStrategyBrains(unittest.TestCase):
    def test_mean_reversion_strategy_lifecycle(self):
        strat = MeanReversionStrategy(
            parameters={
                "baseline_span_sec": 60.0,
                "reversion_trigger_bp": 10.0,
                "target_exit_bp": 2.0,
                "min_imbalance": 0.05,
                "order_size": 0.001,
            }
        )

        # 1. Warm up baseline at 10,000,000
        for i in range(10):
            strat.on_tick(
                tick={"price": 10_000_000.0, "timestamp": 1000.0 + i * 5},
                flow_stats={"mid_price": 10_000_000.0, "book_imbalance": 0.0},
                current_pos=0.0,
                entry_price=0.0,
            )

        self.assertAlmostEqual(strat.baseline_ema, 10_000_000.0, delta=10.0)

        # 2. Extreme downward deviation: mid drops to 9,985,000 (-15bp), with bid imbalance (+0.10)
        # Should trigger BUY target +0.001
        res = strat.on_tick(
            tick={"price": 9_985_000.0, "timestamp": 1060.0},
            flow_stats={"mid_price": 9_985_000.0, "book_imbalance": 0.10},
            current_pos=0.0,
            entry_price=0.0,
        )
        self.assertEqual(res["action"], "BUY")
        self.assertEqual(res["target_qty"], 0.001)

        # Using decide_target_qty interface
        tgt = strat.decide_target_qty(
            tick={"price": 9_985_000.0, "timestamp": 1060.0},
            flow_stats={"mid_price": 9_985_000.0, "book_imbalance": 0.10},
            current_pos=0.0,
            entry_price=0.0,
        )
        self.assertEqual(tgt, 0.001)

        # 3. Holding long, price mean-reverts back to 9,997,000 (deviation converges within 2bp target_exit)
        res_exit = strat.on_tick(
            tick={"price": 9_997_000.0, "timestamp": 1070.0},
            flow_stats={"mid_price": 9_997_000.0, "book_imbalance": 0.0},
            current_pos=0.001,
            entry_price=9_985_000.0,
        )
        self.assertEqual(res_exit["action"], "EXIT")
        self.assertEqual(res_exit["target_qty"], 0.0)

    def test_orderbook_imbalance_strategy_lifecycle(self):
        strat = OrderBookImbalanceStrategy(
            parameters={
                "imbalance_threshold": 0.25,
                "min_delta_ratio": 0.20,
                "take_profit_bp": 5.0,
                "stop_loss_bp": 8.0,
                "order_size": 0.001,
            }
        )

        # 1. Neutral book -> HOLD
        res = strat.on_tick(
            tick={"price": 10_000_000.0, "timestamp": 1000.0},
            flow_stats={"mid_price": 10_000_000.0, "book_imbalance": 0.05, "delta_ratio": 0.0, "spread_bp": 1.0},
            current_pos=0.0,
            entry_price=0.0,
        )
        self.assertEqual(res["action"], "HOLD")
        self.assertEqual(res["target_qty"], 0.0)

        # 2. Strong buy imbalance (+0.35) and taker delta (+0.30) -> BUY +0.001
        res_entry = strat.on_tick(
            tick={"price": 10_000_000.0, "timestamp": 1001.0},
            flow_stats={"mid_price": 10_000_000.0, "book_imbalance": 0.35, "delta_ratio": 0.30, "spread_bp": 1.0},
            current_pos=0.0,
            entry_price=0.0,
        )
        self.assertEqual(res_entry["action"], "BUY")
        self.assertEqual(res_entry["target_qty"], 0.001)

        # 3. Holding long, book flips to heavy sell imbalance (-0.20) -> Instant Exit
        res_flip = strat.on_tick(
            tick={"price": 10_000_500.0, "timestamp": 1003.0},
            flow_stats={"mid_price": 10_000_500.0, "book_imbalance": -0.20, "delta_ratio": -0.10, "spread_bp": 1.0},
            current_pos=0.001,
            entry_price=10_000_000.0,
        )
        self.assertEqual(res_flip["action"], "EXIT")
        self.assertEqual(res_flip["target_qty"], 0.0)


if __name__ == "__main__":
    unittest.main()
