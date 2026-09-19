"""
tests/test_alpha_history_closed_loop.py: DuckDB/Parquet 閉ループ学習データレイク検証
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.multi_asset.alpha_opportunity_engine import HistoricalAlphaStore
from antigravity.multi_asset.alpha_history_recorder import AlphaTradeHistoryRecorder


class TestAlphaHistoryClosedLoop(unittest.TestCase):
    def test_duckdb_prior_lookup(self):
        """DuckDB 経由で Parquet から事前確率が正しく集計されること"""
        prior = HistoricalAlphaStore.lookup_empirical_prior(tier="TIER1", opportunity_type="DISCLOSURE")
        self.assertGreaterEqual(prior["sample_size"], 100)
        self.assertGreater(prior["win_rate"], 0.60)
        self.assertGreater(prior["avg_bp"], 200.0)
        self.assertLess(prior["avg_holding_days"], 5.0)

    def test_trade_recording_and_loop(self):
        """約定結果を追記し、Parquet が正常に更新されること"""
        initial_prior = HistoricalAlphaStore.lookup_empirical_prior(tier="TIER1", opportunity_type="DISCLOSURE")
        init_samples = initial_prior["sample_size"]

        # 1件追記
        ok = AlphaTradeHistoryRecorder.record_completed_trade(
            symbol="9999",
            event_type="大量保有",
            tier="TIER1",
            entry_price=1000.0,
            exit_price=1050.0,
            holding_days=2.0,
            pnl_bp=500.0,
            evs_at_entry=92.0,
            source="test_verification",
        )
        self.assertTrue(ok)

        # 追記後に DuckDB で再集計
        updated_prior = HistoricalAlphaStore.lookup_empirical_prior(tier="TIER1", opportunity_type="DISCLOSURE")
        self.assertGreater(updated_prior["sample_size"], init_samples)


if __name__ == "__main__":
    unittest.main()
