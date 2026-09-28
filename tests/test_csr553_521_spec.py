"""CSR-553 Ablation + CSR-521 CU v1 spec."""
import unittest

from antigravity.quant_pipeline.csr553_umm_ablation_plan_v1 import build_lock as abl
from antigravity.quant_pipeline.csr521_conditional_unwind_v1_spec import (
    build_lock as cu,
    compute_hazard_v1,
    eligible_inventory,
)


class TestCSR553521(unittest.TestCase):
    def test_phase1_lanes(self):
        lock = abl()
        self.assertEqual(lock["phase1_p0"], ["A0", "A1", "A3"])
        self.assertTrue(lock["lanes"]["A3"]["frozen_control"])
        self.assertEqual(lock["lanes"]["A5"]["enforce"], 0)

    def test_l0_protected(self):
        self.assertFalse(eligible_inventory("L0"))
        h = compute_hazard_v1(hold_ladder="L0", current_pnl_bp=0.5)
        self.assertFalse(h.eligible)

    def test_adverse_null_not_zero(self):
        h = compute_hazard_v1(
            hold_ladder="L2", current_pnl_bp=-1.0,
            adverse_score=0.0, adverse_valid=False,
        )
        self.assertIsNone(h.adverse)

    def test_cu_spec_gov(self):
        lock = cu()
        self.assertFalse(lock["final_spec"]["ECONOMIC_PASS"])
        self.assertEqual(lock["final_spec"]["WIRE"], "NO")
        self.assertIn("BLANKET_30S_UNWIND", lock["reject"])


if __name__ == "__main__":
    unittest.main()
