"""CSR-554 DuckDB CF schema smoke + prior 521/553 locks."""
import unittest
from pathlib import Path

from antigravity.quant_pipeline.csr554_csr521_cf_duckdb import (
    build_lock,
    MM_TRADE_CF_LOG_SCHEMA,
    SQL_PATH,
)
from antigravity.quant_pipeline.csr553_umm_ablation_plan_v1 import build_lock as abl
from antigravity.quant_pipeline.csr521_conditional_unwind_v1_spec import (
    compute_hazard_v1,
)


class TestCSR554(unittest.TestCase):
    def test_schema_has_cf_and_actual(self):
        self.assertIn("actual_pnl_bp", MM_TRADE_CF_LOG_SCHEMA)
        self.assertIn("cf_conditional_bp", MM_TRADE_CF_LOG_SCHEMA)
        self.assertIn("conditional_trigger", MM_TRADE_CF_LOG_SCHEMA)
        self.assertTrue(SQL_PATH.exists())

    def test_lock_forbids_actual_plus_cf(self):
        lock = build_lock()
        self.assertIn("REPORT_ACTUAL_PLUS_CF_AS_REALIZED", lock["forbidden"])
        self.assertFalse(lock["gov"]["economic_pass"])

    def test_ablation_phase1(self):
        self.assertEqual(abl()["phase1_p0"], ["A0", "A1", "A3"])

    def test_hazard_adverse_null(self):
        h = compute_hazard_v1(
            hold_ladder="L2", current_pnl_bp=-1.0,
            adverse_valid=False, adverse_score=0.0,
        )
        self.assertIsNone(h.adverse)


if __name__ == "__main__":
    unittest.main()
