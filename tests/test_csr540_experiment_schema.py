"""CSR-540: Discovery↔Evolution schema + novelty + theme mapping (light)."""
import unittest

from pipeline.experiment_schema import (
    build_experiment_record,
    extract_metrics,
    format_evolution_line,
    ExperimentLedger,
)
from agents.strategy_proposer import StrategyProposer


class TestCSR540Schema(unittest.TestCase):
    def test_extract_metrics_win_rate_pct(self):
        m = extract_metrics({
            "total_trades": 34,
            "winning_trades": 20,
            "win_rate_pct": 58.8,
            "total_pnl_jpy": -1234.5,
            "profit_factor": 0.95,
            "sharpe_ratio": -1.01,
            "max_drawdown_pct": 3.2,
        })
        self.assertEqual(m["trades"], 34)
        self.assertEqual(m["win_rate"], 58.8)
        self.assertEqual(m["net_pnl"], -1234.5)
        self.assertEqual(m["sharpe"], -1.01)

    def test_evolution_line_never_unknown(self):
        rec = build_experiment_record(
            strategy_id="strat_abc",
            strategy_name="Unknown",  # must be replaced
            theme="Mean Reversion",
            cycle=125,
            final_status="REJECTED",
            metrics_raw={"total_trades": 34, "win_rate_pct": 58.8, "sharpe_ratio": -1.01, "total_pnl_jpy": -100},
            code="print(1)",
        )
        self.assertNotEqual(rec["strategy_name"].upper(), "UNKNOWN")
        line = format_evolution_line(rec)
        self.assertNotIn("Unknown", line)
        self.assertIn("58.8%", line)

    def test_theme_mapping_mm_vs_micro_trend(self):
        p = StrategyProposer(config={"llm": {"provider": "mock"}})
        mm = p.propose_strategy(theme="Market Making (Inventory Skew & Micro Spread)")
        mt = p.propose_strategy(theme="Micro Trend Following (Order Flow Delta & Low Reverse Flow)")
        self.assertIn("InventorySkew", mm["name"])
        self.assertIn("MicroTrend", mt["name"])
        self.assertNotEqual(mm["code"], mt["code"])

    def test_novelty_same_strategy_same_data_skips(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            led = ExperimentLedger(Path(td) / "ledger.jsonl")
            rec = build_experiment_record(
                strategy_id="a",
                strategy_name="S",
                theme="T",
                cycle=1,
                final_status="REJECTED",
                metrics_raw={"total_trades": 1, "win_rate_pct": 0},
                code="CODE_A",
                parameters={"x": 1},
                dataset_fp="ds1",
            )
            led.append(rec)
            decision = led.should_skip_novelty(
                rec["strategy_fingerprint"],
                None,
                dataset_hash_now=rec["dataset_hash"],
            )
            self.assertTrue(decision["skip"])
            decision2 = led.should_skip_novelty(
                rec["strategy_fingerprint"],
                None,
                dataset_hash_now="ds_changed_hash_xxxxx",
            )
            self.assertFalse(decision2["skip"])
            self.assertEqual(decision2["auto_retry_reason"], "NEW_DATA")


if __name__ == "__main__":
    unittest.main()
