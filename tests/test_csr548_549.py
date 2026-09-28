"""CSR-548 PEG reject + CSR-549 council teacher timeline."""
import unittest

from antigravity.quant_pipeline.csr548_peg_v3_hard_reject import (
    build_lock as peg_lock,
    ev_per_trade,
    CF_MATRIX_POLICIES,
)
from antigravity.quant_pipeline.csr549_council_day_teacher import (
    build_lock as council_lock,
    HOURLY,
)


class TestCSR548(unittest.TestCase):
    def test_hard_reject_and_ev(self):
        lock = peg_lock()
        self.assertEqual(lock["status"]["Promotion"], "HARD REJECT")
        self.assertAlmostEqual(ev_per_trade(-252.50, 138), -1.8297, places=3)
        self.assertFalse(lock["claims_allowed"]["aging_convicted"])
        self.assertEqual(lock["capture_rate"]["economic_gate"], "EXCLUDED")
        self.assertIn("CF 3s Best-Maker", CF_MATRIX_POLICIES)
        self.assertIn("PEG_v4", lock["forbidden"])


class TestCSR549(unittest.TestCase):
    def test_stale_13_excluded(self):
        lock = council_lock()
        self.assertEqual(lock["summary"]["stale_excluded"], ["13:00"])
        self.assertEqual(lock["summary"]["teacher_ok_n"], 14)

    def test_veto_priority_not_hold_candidate(self):
        lock = council_lock()
        ts = [x["ts"] for x in lock["summary"]["veto_cf_priority"]]
        self.assertEqual(ts, ["12:00", "18:00", "20:00"])
        low = [x["ts"] for x in lock["summary"]["veto_cf_low_value"]]
        self.assertEqual(low, ["19:00"])

    def test_no_final_as_teacher(self):
        lock = council_lock()
        self.assertIn("final_action", lock["teacher_rules"]["forbidden"])
        for r in HOURLY:
            if r["teacher_ok"]:
                self.assertIn(r["schema"], ("csr538_v1",))


if __name__ == "__main__":
    unittest.main()
