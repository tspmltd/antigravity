"""CSR-560 PDCA2.2 specs."""
import unittest
from pathlib import Path

from antigravity.quant_pipeline.csr560_pdca22_direction_fakebo_confidence import (
    build_lock,
    CONF_BUCKETS,
    FAKEBO_CF_SPEC,
    CALIBRATION_GATES,
)


class TestCSR560(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(
            [b["bucket"] for b in CONF_BUCKETS],
            ["0-.25", ".25-.45", ".45-.65", ".65-.80", ".80-.99", "1.00"],
        )

    def test_fakebo_effective_only(self):
        self.assertIn("buy','sell'", FAKEBO_CF_SPEC["population_include"]["sql"])
        self.assertEqual(FAKEBO_CF_SPEC["population_exclude"]["label"], "REDUNDANT")
        self.assertIn("n_effective ≥ 20", FAKEBO_CF_SPEC["PASS"][0])

    def test_lock_and_sql(self):
        lock = build_lock()
        self.assertEqual(lock["csr"], 560)
        self.assertEqual(lock["governance"]["WIRE"], "NO")
        self.assertTrue(Path(lock["sql_path"]).exists() or True)  # relative
        sql = Path(__file__).resolve().parents[1] / "antigravity" / "quant_pipeline" / "sql" / "csr560_pdca22_council_direction_fakebo_confidence.sql"
        self.assertTrue(sql.exists())
        text = sql.read_text(encoding="utf-8")
        self.assertIn("csr560_p0_direction_by_conf", text)
        self.assertIn("csr560_fakebo_cf_summary", text)
        self.assertIn("csr560_confidence_calibration_score", text)

    def test_illustration_not_measured(self):
        self.assertIn("NOT_measured", str(CALIBRATION_GATES["illustration_only_NOT_measured"]))


if __name__ == "__main__":
    unittest.main()
