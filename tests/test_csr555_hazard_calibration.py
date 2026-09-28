"""CSR-555 hazard calibration + prior 554 smoke."""
import unittest

from antigravity.quant_pipeline.csr555_hazard_threshold_calibration import (
    build_lock,
    SNAPSHOT_AGES_SEC,
    THRESHOLDS,
)
from antigravity.quant_pipeline.csr554_csr521_cf_duckdb import build_lock as cf_lock
from antigravity.quant_pipeline.csr553_umm_ablation_plan_v1 import build_lock as abl
from antigravity.quant_pipeline.csr521_conditional_unwind_v1_spec import compute_hazard_v1


class TestCSR555(unittest.TestCase):
    def test_no_accuracy_objective(self):
        lock = build_lock()
        self.assertIn("MAXIMIZE_CLASSIFICATION_ACCURACY_FOR_THRESHOLD", lock["forbidden"])
        self.assertIn("INFER_HARD_STOP_FROM_PNL_LE_MINUS_4_8", lock["forbidden"])
        self.assertFalse(lock["gov"]["Economic_PASS"])

    def test_snapshot_ages(self):
        self.assertIn(30, SNAPSHOT_AGES_SEC)
        self.assertIn(0.60, THRESHOLDS)

    def test_prior_locks(self):
        self.assertEqual(abl()["phase1_p0"], ["A0", "A1", "A3"])
        self.assertFalse(cf_lock()["gov"]["economic_pass"])
        h = compute_hazard_v1(hold_ladder="L0", current_pnl_bp=1.0)
        self.assertFalse(h.eligible)


if __name__ == "__main__":
    unittest.main()
