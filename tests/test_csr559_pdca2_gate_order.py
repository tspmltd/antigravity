"""CSR-559 PDCA2 architecture · Do NOT STARTED."""
import unittest

from antigravity.quant_pipeline.csr559_pdca2_gate_order_lock import build_lock, PNL_IDENTITY


class TestCSR559(unittest.TestCase):
    def test_do_not_started(self):
        lock = build_lock()
        self.assertEqual(lock["pdca2"]["Do"], "NOT_STARTED")
        self.assertEqual(lock["pdca2"]["Plan"], "LOCKED")

    def test_gate_order(self):
        lock = build_lock()
        self.assertEqual(lock["gate_order"], ["P0-a", "P0-b", "P1-a", "P1-b", "P2"])
        self.assertEqual(lock["gates"][0]["name"], "Simulator parity harness")
        self.assertIn("CSR-553", lock["gates"][1]["name"])
        self.assertIn("Attribution", lock["gates"][2]["name"])
        self.assertIn("latency", lock["gates"][3]["name"])
        self.assertIn("CSR-521", lock["gates"][4]["name"])

    def test_identity_and_ledgers(self):
        lock = build_lock()
        self.assertEqual(lock["architecture"]["identity"], PNL_IDENTITY)
        names = [x["name"] for x in lock["do"]["ledgers"]]
        self.assertEqual(names, ["orderbook_micro", "fusion_log", "execution_log"])

    def test_governance_freeze(self):
        g = build_lock()["governance"]
        self.assertTrue(g["NO_CODE_CHANGE"])
        self.assertTrue(g["NO_PARAM_CHANGE"])
        self.assertEqual(g["WIRE"], "NO")
        self.assertEqual(g["ENFORCE"], 0)
        self.assertEqual(g["AUTO_APPLY"], "OFF")


if __name__ == "__main__":
    unittest.main()
