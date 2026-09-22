"""Adverse episode state machine v2 — fire / confirm / false gates."""
import time
import unittest

from antigravity.quant_pipeline.event_bus import EventBus
from antigravity.quant_pipeline.agents.adverse_agent import AdverseResearchAgent
from antigravity.quant_pipeline.schema import OrderbookMicroSnapshot


def _snap(ts: float, bid_d: float, ask_d: float = 1.0, taker_bid: float = 0.0, taker_ask: float = 0.0):
    mid = 12_000_000.0
    return OrderbookMicroSnapshot(
        timestamp=int(ts * 1000),
        latency_ms=10.0,
        best_bid=mid,
        best_ask=mid + 1000.0,
        mid_price=mid + 500.0,
        micro_price=mid + 500.0,
        micro_dev=0.0,
        bid_depth_1=bid_d,
        ask_depth_1=ask_d,
        total_bid_depth=bid_d * 2,
        total_ask_depth=ask_d * 2,
        imbalance=0.0,
        taker_volume_bid=taker_bid,
        taker_volume_ask=taker_ask,
        taker_aggressiveness=0.0,
    )


class TestAdverseEpisodeSMv2(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.agent = AdverseResearchAgent(bus=self.bus, save_dir="/tmp/adverse_sm_v2_test")

    def test_thin_tip_does_not_arm(self):
        t0 = time.time()
        self.agent.on_orderbook(_snap(t0, bid_d=0.03))
        self.assertEqual(self.agent.state_buy, "NORMAL")
        self.assertEqual(self.agent.total_episodes, 0)

    def test_ratio_only_without_abs_drop_does_not_fire(self):
        t0 = time.time()
        self.agent.on_orderbook(_snap(t0, bid_d=0.08))
        self.assertEqual(self.agent.state_buy, "PRE_ADVERSE")
        # ratio 0.038/0.08=0.475 <= 0.50 but drop=0.042 < min_abs 0.05
        self.agent.on_orderbook(_snap(t0 + 0.05, bid_d=0.038))
        self.assertEqual(self.agent.total_episodes, 0)
        self.assertFalse(self.agent.latest_state.get("avoidance_on"))

    def test_fire_without_taker_is_false_episode(self):
        t0 = time.time()
        self.agent.on_orderbook(_snap(t0, bid_d=0.20))
        self.agent.on_orderbook(_snap(t0 + 0.02, bid_d=0.05))  # drop 0.15, ratio 0.25
        self.assertEqual(self.agent.total_episodes, 1)
        self.assertTrue(self.agent.latest_state.get("avoidance_on"))
        self.assertEqual(self.agent.confirmed_episodes, 0)
        self.agent.on_orderbook(_snap(t0 + 2.6, bid_d=0.05))
        self.assertEqual(self.agent.false_episodes, 1)
        self.assertEqual(self.agent.state_buy, "NORMAL")

    def test_fire_with_cum_taker_confirms(self):
        t0 = time.time()
        self.agent.on_orderbook(_snap(t0, bid_d=0.20))
        self.agent.on_orderbook(_snap(t0 + 0.02, bid_d=0.05, taker_bid=0.0))
        self.assertEqual(self.agent.total_episodes, 1)
        # thr = max(0.015, min(0.08, 0.04)) = 0.04
        self.agent.on_orderbook(_snap(t0 + 0.05, bid_d=0.04, taker_bid=0.02))
        self.agent.on_orderbook(_snap(t0 + 0.08, bid_d=0.03, taker_bid=0.025))
        self.assertEqual(self.agent.confirmed_episodes, 1)
        self.assertEqual(self.agent.state_buy, "OPP_TAKER")
        self.assertGreater(self.agent.lead_ms_buy, 0.0)

    def test_cancel_recommendation_always_false(self):
        t0 = time.time()
        self.agent.on_orderbook(_snap(t0, bid_d=0.20))
        st = self.agent.on_orderbook(_snap(t0 + 0.02, bid_d=0.05, taker_bid=0.10))
        self.assertTrue(st["avoidance_on"])
        self.assertFalse(st["cancel_recommendation"])

if __name__ == "__main__":
    unittest.main()
