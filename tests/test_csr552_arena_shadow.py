"""CSR-552 Arena Shadow Execution."""
import unittest

from antigravity.quant_pipeline.arena_shadow_execution import (
    build_lock,
    is_shadow_strategy,
    on_arena_entry,
    empty_ablation,
    SHADOW_LOG,
)


class TestCSR552(unittest.TestCase):
    def test_shadow_names(self):
        self.assertTrue(is_shadow_strategy("SpreadCaptureMM"))
        self.assertTrue(is_shadow_strategy("InventorySkewMM"))
        self.assertTrue(is_shadow_strategy("MicroSpreadMM"))
        self.assertFalse(is_shadow_strategy("UMM"))

    def test_on_entry_writes(self):
        row = on_arena_entry(
            strategy="SpreadCaptureMM",
            strat_id="test_shadow",
            side="buy",
            quote_price=13000000.0,
            mid=13000100.0,
            spread=2500.0,
            reason="UNIT_TEST",
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["ae_status"], "NULL_PENDING")
        self.assertEqual(row["enforce"], 0)
        self.assertTrue(SHADOW_LOG.exists())

    def test_ablation_arms(self):
        a = empty_ablation()
        self.assertIn("Spread_plus_InventorySkew_plus_CurrentExit", a["arms"])
        lock = build_lock()
        self.assertEqual(lock["gov"]["ENFORCE"], 0)
        self.assertIn("Inventory Hazard", lock["priorities"]["P0"])


if __name__ == "__main__":
    unittest.main()
