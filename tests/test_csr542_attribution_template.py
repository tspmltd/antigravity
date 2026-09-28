"""CSR-542: Adverse Actual / Latency Condition — no double count."""
import unittest

from antigravity.quant_pipeline.umm_economic_attribution_template import (
    PRIMARY_2200,
    build_template_v2,
    check_hold_ladder_identity,
    check_identity,
)


class TestCSR542Template(unittest.TestCase):
    def test_hold_ladder_closes(self):
        ident = check_hold_ladder_identity(
            PRIMARY_2200["hold_ladder_1h"], PRIMARY_2200["umm_1h_net_bp"]
        )
        self.assertTrue(ident["identity_ok"])
        self.assertEqual(ident["n_trades"], 58)
        self.assertAlmostEqual(ident["identity_residual_bp"], 0.0, places=2)

    def test_latency_forbidden_in_actual(self):
        rep = build_template_v2(True)
        w1 = rep["A_actual_pnl_attribution"]["windows"]["1h"]
        self.assertEqual(w1["latency_cost_bp"]["status"], "FORBIDDEN_IN_ACTUAL")
        self.assertFalse(w1["latency_cost_bp"]["in_actual_pnl"])
        self.assertFalse(rep["F_dashboard"]["RULE"]["latency_in_actual"])
        self.assertFalse(rep["F_dashboard"]["RULE"]["cf_in_actual"])
        for row in rep["C_latency_condition"]["by_bucket"]:
            self.assertEqual(row["latency_cost_bp"], "FORBIDDEN")

    def test_cf_not_added_to_actual(self):
        rep = build_template_v2(True)
        cf = rep["F_dashboard"]["RESEARCH_CF"]
        self.assertFalse(cf["added_to_actual"])
        self.assertEqual(cf["csr521_delta_30s_bp"], 12.36)
        # Must not invent "improved" = actual + delta
        actual = cf["csr521_actual_1h_bp"]
        delta = cf["csr521_delta_30s_bp"]
        self.assertNotEqual(rep["F_dashboard"]["ACTUAL"]["net_1h_bp"], actual + delta)

    def test_orthogonal_not_invented(self):
        rep = build_template_v2(True)
        w1 = rep["A_actual_pnl_attribution"]["windows"]["1h"]
        self.assertIsNone(w1["gross_spread_capture_bp"]["value"])
        self.assertIsNone(w1["adverse_markout_bp"]["value"])
        self.assertFalse(w1["orthogonal_identity_ok"])

    def test_full_identity_helper(self):
        r = check_identity(10.0, -3.0, 5.0, -2.0, 0.48, 10.48)
        self.assertTrue(r["identity_ok"])


if __name__ == "__main__":
    unittest.main()
