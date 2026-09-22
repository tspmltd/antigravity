"""ToxicFlowAnalyzer v2 — opp_taker is primary; cancel alone cannot alert."""
import unittest

from antigravity.quant_pipeline.toxic_flow_analyzer import ToxicFlowAnalyzer


class TestToxicFlowV2(unittest.TestCase):
    def setUp(self):
        self.a = ToxicFlowAnalyzer()

    def test_cancel_refill_only_capped_below_warning(self):
        r = self.a.calculate_toxic_score(
            side="buy",
            imbalance=-0.5,
            taker_buy=0.0,
            taker_sell=0.0,
            cancel_rate=1.0,
            refill_rate=0.0,
            depth_1=0.001,
            depth_3=0.05,
        )
        self.assertLessEqual(r["toxic_score"], 35.0)
        self.assertTrue(r["taker_capped"])
        self.assertNotIn(r["level"], ("TOXIC_WARNING", "TOXIC_CRITICAL"))

    def test_strong_opp_taker_can_warn(self):
        r = self.a.calculate_toxic_score(
            side="buy",
            imbalance=-0.7,
            taker_buy=0.0,
            taker_sell=0.10,
            cancel_rate=0.4,
            refill_rate=0.1,
            depth_1=0.02,
            depth_3=0.08,
        )
        self.assertGreaterEqual(r["toxic_score"], 40.0)
        self.assertFalse(r["taker_capped"])
        self.assertEqual(r["breakdown"]["score_taker_flow"], max(
            r["breakdown"]["score_taker_flow"],
            r["breakdown"]["score_cancel"],
        ))


if __name__ == "__main__":
    unittest.main()
