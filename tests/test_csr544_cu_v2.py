"""CSR-544: Risk-score CU must not fire on hold-alone; HS not from PnL."""
import unittest

from antigravity.quant_pipeline.csr521_conditional_unwind_v2 import (
    build_observe_event,
    compute_risk_score,
    recovery_vs_deteriorating,
)
from antigravity.quant_pipeline.umm_3axis_attribution_sql import (
    FORBIDDEN_SQL_PATTERNS,
    SQL_TRADE_ENRICH,
    build_lock,
)


class TestCSR544(unittest.TestCase):
    def test_hold_alone_does_not_force_unwind(self):
        br = compute_risk_score(
            hold_ladder="L1",
            current_pnl_bp=-0.2,
            hold_sec=120.0,
            adverse_score=10,
            hs_distance_bp=5.0,
        )
        self.assertLess(br.total, 4)
        ev = build_observe_event(
            trade_id="t1",
            hold_ladder="L1",
            current_pnl_bp=-0.2,
            mae_bp=-0.3,
            mfe_bp=1.0,
            pnl_slope_5s=0.1,
            adverse_score=10,
            hs_distance_bp=5.0,
            hold_sec=120,
            threshold=5,
        )
        self.assertFalse(ev["virtual_unwind_candidate"])

    def test_toxic_scores_high(self):
        br = compute_risk_score(
            hold_ladder="L3",
            current_pnl_bp=-3.2,
            adverse_score=85,
            hs_distance_bp=1.0,
        )
        # L3=2 + pnl<=-3=3 + adv>=80=3 + hs=2 = 10
        self.assertGreaterEqual(br.total, 7)

    def test_mae_path_separated(self):
        self.assertEqual(
            recovery_vs_deteriorating(
                current_pnl_bp=-1.6, mae_bp=-4.7, pnl_slope_5s=0.2
            ),
            "RECOVERY",
        )
        self.assertEqual(
            recovery_vs_deteriorating(
                current_pnl_bp=-1.6, mae_bp=-1.7, pnl_slope_5s=-0.3
            ),
            "DETERIORATING",
        )

    def test_hard_stop_from_exit_reason_sql(self):
        self.assertIn("exit_reason = 'HARD_STOP'", SQL_TRADE_ENRICH)
        self.assertNotIn("realized_pnl_bp <= -4.8", SQL_TRADE_ENRICH)
        lock = build_lock()
        for pat in FORBIDDEN_SQL_PATTERNS:
            self.assertIn(pat, lock["forbidden_sql_patterns"])
        self.assertIn("MID", lock["adverse_bands"])


if __name__ == "__main__":
    unittest.main()
