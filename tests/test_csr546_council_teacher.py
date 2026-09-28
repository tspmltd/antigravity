"""CSR-546 council teacher / adverse NULL."""
import unittest

from antigravity.quant_pipeline.council_teacher_schema import (
    direction_label_ok,
    normalize_adverse_for_db,
    build_lock,
)


class TestCSR546(unittest.TestCase):
    def test_shadow_off_null(self):
        r = normalize_adverse_for_db(
            adverse_score=0.0, adverse_score_valid=False, adverse_risk_level="SHADOW_OFF"
        )
        self.assertIsNone(r["adverse_score"])
        self.assertFalse(r["adverse_score_valid"])

    def test_teacher_forbids_final(self):
        self.assertTrue(direction_label_ok("candidate_action"))
        self.assertTrue(direction_label_ok("decision_action"))
        self.assertFalse(direction_label_ok("final_action"))

    def test_lock_status(self):
        lock = build_lock()
        self.assertEqual(lock["status"]["Economic_PASS"], "NO")
        self.assertEqual(lock["status"]["Adverse"], "INVALID_WHILE_SHADOW_OFF")
        self.assertEqual(len(lock["fake_bo_veto_cf_table"]), 3)


if __name__ == "__main__":
    unittest.main()
