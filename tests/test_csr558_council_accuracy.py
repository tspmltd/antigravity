"""CSR-558 council accuracy / veto_effective."""
import unittest

from antigravity.quant_pipeline.csr558_council_accuracy_veto_effective import (
    build_lock,
    veto_effective,
)


class TestCSR558(unittest.TestCase):
    def test_effective_vs_redundant(self):
        self.assertTrue(veto_effective(veto=True, decision_action="BUY"))
        self.assertTrue(veto_effective(veto=True, decision_action="sell"))
        self.assertFalse(veto_effective(veto=True, decision_action="HOLD"))
        self.assertFalse(veto_effective(veto=False, decision_action="BUY"))

    def test_lock_cases(self):
        lock = build_lock()
        self.assertEqual([x["ts"] for x in lock["classification"]["effective_veto"]],
                         ["02:00", "07:00"])
        self.assertEqual([x["ts"] for x in lock["classification"]["redundant_veto"]],
                         ["04:00"])
        self.assertEqual(lock["adverse"]["status"], "HEALTHY_NULL_SEMANTICS")


if __name__ == "__main__":
    unittest.main()
