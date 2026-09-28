"""CSR-547 hazard v3 / latency provenance smoke."""
import unittest

from antigravity.quant_pipeline.csr521_hazard_score_v3 import (
    compute_hazard_score_v3,
    build_lock,
)
from antigravity.quant_pipeline.latency_provenance_schema import normalize_latency_row


class TestCSR547(unittest.TestCase):
    def test_10s_detection_starts(self):
        s = compute_hazard_score_v3(hold_sec=12.0, current_pnl_bp=0.0)
        self.assertEqual(s.age_pts, 1)

    def test_l1_can_score_without_l2(self):
        s = compute_hazard_score_v3(
            hold_sec=15.0,
            current_pnl_bp=-3.0,
            pnl_deteriorating=True,
            mae_deepening=True,
            hold_ladder="L1",
            taker_same_side=True,
        )
        self.assertEqual(s.ladder_pts, 1)
        self.assertGreaterEqual(s.total, 5)

    def test_imb_alone_zero(self):
        s = compute_hazard_score_v3(
            hold_sec=5.0,
            current_pnl_bp=0.0,
            imb_extreme_alone=True,
            cancel_alone=True,
        )
        self.assertEqual(s.total, 0)

    def test_adverse_invalid_no_points(self):
        s = compute_hazard_score_v3(
            hold_sec=15.0,
            current_pnl_bp=0.0,
            adverse_high=True,
            adverse_score_valid=False,
        )
        self.assertEqual(s.adverse_pts, 0)

    def test_30s_counterexample_in_lock(self):
        lock = build_lock()
        self.assertLess(lock["negative_controls"]["15:00"]["cf"]["30s"], 0)
        self.assertEqual(lock["status"]["CSR521_30s_blanket"], "NOT_ROBUST")

    def test_latency_1ms_invalid(self):
        r = normalize_latency_row(
            latency_ms=1.0,
            latency_source="synthetic_stub",
            latency_measurement_version="unknown",
            latency_valid=False,
        )
        self.assertFalse(r["latency_valid"])


if __name__ == "__main__":
    unittest.main()
