"""PegResearchAgent — CSR-aligned research tasks (no execution)."""
import time
import unittest

from antigravity.quant_pipeline.event_bus import EventBus
from antigravity.quant_pipeline.agents.peg_research_agent import PegResearchAgent
from antigravity.quant_pipeline.peg_research_store import PegResearchStore
from antigravity.quant_pipeline.schema import OrderbookMicroSnapshot


def _snap(ts: float, mid: float, bid_d=0.1, ask_d=0.1, tb=0.0, ta=0.0, cancel=0.0, refill=0.2):
    return OrderbookMicroSnapshot(
        timestamp=int(ts * 1000),
        latency_ms=5.0,
        best_bid=mid - 500,
        best_ask=mid + 500,
        mid_price=mid,
        micro_price=mid,
        micro_dev=0.0,
        bid_depth_1=bid_d,
        ask_depth_1=ask_d,
        total_bid_depth=bid_d * 2,
        total_ask_depth=ask_d * 2,
        imbalance=0.0,
        taker_volume_bid=tb,
        taker_volume_ask=ta,
        taker_aggressiveness=0.3,
        cancel_rate=cancel,
        refill_rate=refill,
    )


class TestPegResearchAgent(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.store = PegResearchStore(root="/tmp/peg_research_test")
        self.agent = PegResearchAgent(bus=self.bus, store=self.store, pred_cooldown_sec=0.0)

    def test_research_only_flags(self):
        t0 = time.time()
        mid = 13_500_000.0
        for i in range(25):
            # climb ~2bp over window
            m = mid * (1.0 + 0.00015 * (i / 25.0))
            self.agent.on_orderbook(_snap(t0 + i * 0.4, m, ta=0.02))
        c = self.agent.get_latest_conclusion()
        self.assertIsNotNone(c)
        self.assertFalse(c.hard_veto)
        self.assertFalse(c.emergency_cancel)
        self.assertEqual(c.primary_action, "hold")
        self.assertEqual(c.metrics.get("wire"), "NO")
        self.assertIn("CSR-022", c.metrics.get("csr_refs", []))

    def test_multi_horizon_direction_emits(self):
        t0 = time.time()
        mid = 13_500_000.0
        for i in range(30):
            m = mid * (1.0 + 0.00025 * (i / 30.0))  # ~2.5bp
            self.agent.on_orderbook(_snap(t0 + i * 0.3, m, ta=0.02))
        tasks = [p["task"] for p in self.agent.pending]
        horizons = sorted({p["horizon_sec"] for p in self.agent.pending if p["task"] == "direction_1_5bp"})
        self.assertIn("direction_1_5bp", tasks)
        self.assertEqual(horizons, [10.0, 30.0, 60.0])


if __name__ == "__main__":
    unittest.main()
