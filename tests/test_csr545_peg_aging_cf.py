"""CSR-545: PEG Aging CF schema guards."""
import unittest

from antigravity.quant_pipeline.peg_aging_exit_cf_schema import (
    AGING_CF_HORIZONS_SEC,
    AGING_EXIT_LOG_SCHEMA,
    build_lock,
    capture_rate_audit_checklist,
)


class TestCSR545(unittest.TestCase):
    def test_no_live_aging_param_change(self):
        lock = build_lock()
        self.assertEqual(lock["status"]["parameter_change"], "NO")
        self.assertEqual(lock["status"]["promotion"], "REJECT")
        self.assertIn("FORBIDDEN", lock["status"]["aging_3_to_5_live_change"])
        self.assertFalse(lock["status"]["economic_pass"])

    def test_cf_horizons_include_5_to_30(self):
        self.assertEqual(AGING_CF_HORIZONS_SEC, (5.0, 10.0, 20.0, 30.0))
        names = [r["counterfactual"] for r in build_lock()["cf_table_template"]]
        self.assertIn("Actual Aging 3s", names)
        self.assertIn("CF Aging 5s", names)
        self.assertIn("CF Best-Maker Exit", names)
        self.assertIn("CF Baseline Exit", names)

    def test_required_log_fields(self):
        for f in ("trade_id", "pnl_3s", "pnl_30s", "cf_5s_bp", "cf_bestmaker_bp", "exit_type"):
            self.assertIn(f, AGING_EXIT_LOG_SCHEMA)

    def test_capture_invalid(self):
        a = capture_rate_audit_checklist()
        self.assertEqual(a["status"], "INVALID_UNTIL_AUDIT")
        self.assertFalse(a["promotion_gate"])

    def test_claims_not_overreached(self):
        c = build_lock()["claims_allowed"]
        self.assertTrue(c["ae_not_converted_to_pnl"])
        self.assertFalse(c["entry_edge_fully_proven"])
        self.assertFalse(c["agingguard_friction_convicted"])


if __name__ == "__main__":
    unittest.main()
