"""CSR-550/551 smoke."""
import unittest

from antigravity.quant_pipeline.csr550_research_priority_lock import build_lock as p550
from antigravity.quant_pipeline.csr551_council_cf_scorecard import (
    build_lock as p551,
    training_eligible,
)


class TestCSR550551(unittest.TestCase):
    def test_521_delta_matches_l2l3(self):
        lock = p550()
        c = lock["primary"]["csr521_2200"]
        self.assertEqual(c["delta_30_bp"], c["l2_l3_delta_bp"])
        self.assertFalse(lock["primary"]["adverse_research_2200"]["economic_pass"])

    def test_eligibility(self):
        self.assertFalse(
            training_eligible(
                schema_version="LEGACY_PRE_CSR531",
                candidate_action="HOLD",
                final_action="HOLD",
            )["training_eligible"]
        )
        self.assertFalse(
            training_eligible(
                schema_version="csr531_v1",
                candidate_action="HOLD",
                final_action="BUY",
            )["training_eligible"]
        )
        self.assertTrue(
            training_eligible(
                schema_version="csr538_v1",
                candidate_action="SELL",
                final_action="HOLD",
            )["training_eligible"]
        )

    def test_scorecard_final_teacher_zero(self):
        lock = p551()
        self.assertEqual(lock["scorecard_template"]["E_DATA_QUALITY"]["final_as_teacher"], 0)
        self.assertEqual(lock["status"]["CSR538"], "CANONICAL")


if __name__ == "__main__":
    unittest.main()
