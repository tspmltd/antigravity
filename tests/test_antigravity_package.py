import unittest
import time
from antigravity.config.settings import Settings
from antigravity.ws_engine.flow_analyzer import FlowAnalyzer
from antigravity.risk_guard.circuit_breaker import PeakDrawdownCircuitBreaker
from antigravity.risk_guard.performance import PerformanceTracker
from antigravity.strategies.micro_trend import MicroTrendTickStrategy
from antigravity.strategies.inventory_mm import InventorySkewTickMMStrategy
from antigravity.strategies.order_flow_scalp import OrderFlowScalpTickStrategy
from antigravity.runtime.runner import AntigravityRunner


class TestAntigravityPackage(unittest.TestCase):
    def test_settings(self):
        self.assertEqual(Settings.PRODUCT_CODE, "FX_BTC_JPY")
        self.assertEqual(Settings.MAKER_FEE_PCT, 0.0)
        self.assertEqual(Settings.TAKER_FEE_PCT, 0.0)

    def test_flow_analyzer(self):
        analyzer = FlowAnalyzer(window_seconds=15.0)
        now = time.time()
        ticks = [
            {"id": 1, "price": 10000000.0, "size": 0.5, "side": "BUY", "timestamp": now - 5.0},
            {"id": 2, "price": 10002000.0, "size": 1.0, "side": "BUY", "timestamp": now - 1.0},
        ]
        stats = analyzer.compute_flow_stats(ticks, current_price=10002000.0)
        self.assertEqual(stats["tick_count"], 2)
        self.assertGreater(stats["price_change_bp"], 0)
        self.assertEqual(stats["net_delta"], 1.5)
        self.assertEqual(stats["delta_ratio"], 1.0)
        self.assertTrue(stats["is_2bp_momentum_up"])

    def test_circuit_breaker(self):
        cb = PeakDrawdownCircuitBreaker(max_drawdown_limit_jpy=1000.0, cooldown_seconds=60.0)
        res1 = cb.update(500.0)
        self.assertEqual(res1["peak_pnl"], 500.0)
        self.assertFalse(res1["is_halted"])

        # 下落 600円 (DD = 600 < 1000)
        res2 = cb.update(-100.0)
        self.assertFalse(res2["is_halted"])

        # 下落 1100円 (DD = 1100 >= 1000) -> トリップ
        res3 = cb.update(-600.0)
        self.assertTrue(res3["is_halted"])
        self.assertEqual(cb.trip_count, 1)

    def test_performance_tracker(self):
        tracker = PerformanceTracker()
        now = time.time()
        trades_map = {
            "strat_a": [
                {"timestamp": now - 100.0, "pnl": 50.0},
                {"timestamp": now - 50.0, "pnl": -20.0},
            ]
        }
        snapshots = tracker.compute_snapshots(trades_map=trades_map, now_ts=now)
        overall = snapshots["overall"]
        self.assertEqual(overall["hourly"]["trades_count"], 2)
        self.assertEqual(overall["hourly"]["realized_pnl"], 30.0)
        self.assertEqual(overall["hourly"]["win_rate_pct"], 50.0)

    def test_strategies(self):
        s_micro = MicroTrendTickStrategy()
        s_mm = InventorySkewTickMMStrategy()
        s_scalp = OrderFlowScalpTickStrategy()

        # 上昇初動時のシグナル判定
        flow_up = {
            "price_change_bp": 2.0,
            "delta_ratio": 0.5,
            "is_2bp_momentum_up": True,
            "should_cancel_bid": False,
            "should_cancel_ask": True,
        }
        tick = {"price": 10000000.0}

        sig_micro = s_micro.on_tick(tick, flow_up, current_pos=0.0, entry_price=0.0)
        self.assertEqual(sig_micro["action"], "BUY")

        sig_scalp = s_scalp.on_tick(tick, flow_up, current_pos=0.0, entry_price=0.0)
        self.assertEqual(sig_scalp["action"], "BUY")


if __name__ == "__main__":
    unittest.main()
