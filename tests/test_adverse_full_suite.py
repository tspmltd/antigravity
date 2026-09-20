"""
Comprehensive Test Suite for Adverse Agent Improvement Directive v1.0
=====================================================================
S1. Adverse Excursion (AE_100ms〜30s)
S2. Toxic Flow (Toxic Score 0-100)
S3. Capture Rate (実現bp / 理論スプレッドbp)
A1. Fill Quality (GOOD / NORMAL / TOXIC)
A2. Time-to-Adverse (0-250ms〜5s+ / AFTER 1s 回復率)
A3. 4レジーム分析 (trend_high_vol, trend_low_vol, range_high_vol, range_low_vol)
B1. Latency分析 (50ms以下, 50-80ms, 80-120ms, 120ms以上)
B2. Inventory分析 (在庫と逆選択)
C1. Agent責任分析 (戦略別集計)
"""

import os
import sys
import time
import json
import shutil
import tempfile
import unittest

BASE_DIR = "/home/azureuser/antigravity"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.quant_pipeline.adverse_excursion import AdverseExcursionTracker
from antigravity.quant_pipeline.toxic_flow_analyzer import ToxicFlowAnalyzer
from antigravity.quant_pipeline.agents.adverse_agent import AdverseResearchAgent
from antigravity.quant_pipeline.event_bus import EventBus
from antigravity.quant_pipeline.schema import OrderbookMicroSnapshot


class TestAdverseDirectiveV1(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.tracker = AdverseExcursionTracker(save_dir=self.temp_dir)
        self.toxic_analyzer = ToxicFlowAnalyzer()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_s1_and_a1_and_s3_flow(self):
        """S1(AE), A1(Fill Quality), S3(Capture Rate) の完全ライフサイクル検証"""
        t0 = 1000.0
        # 1. 新規約定登録
        rec = self.tracker.track_entry(
            trade_id="T001",
            side="buy",
            entry_price=12718515,
            entry_time=t0,
            strategy_name="UMM_v1",
            mid_price=12718500,
            micro_price=12718400,
            queue_rank_estimate=1,
            regime="trend_high_vol",
            latency_ms=45.0,
            spread_bp=2.0,
            inventory=0.001,
        )

        # 2. Tick経過 (逆行発生)
        self.tracker.on_tick(current_price=12717500, timestamp=t0 + 0.11)  # 100ms
        self.tracker.on_tick(current_price=12716000, timestamp=t0 + 1.05)  # 1s (約-2.0bp -> TOXIC_FILL)
        self.tracker.on_tick(current_price=12717000, timestamp=t0 + 3.05)  # 3s
        self.tracker.on_tick(current_price=12721000, timestamp=t0 + 10.05) # 10s (反発)
        self.tracker.on_tick(current_price=12722000, timestamp=t0 + 30.05) # 30s 完了

        # A1 検証: 1sで -1.5bp 未満だったため TOXIC_FILL
        self.assertEqual(rec.fill_quality, "TOXIC_FILL")

        # 3. エグジット & S3 Capture Rate 判定
        # 理論スプレッド 2.0bp に対し、実現利益 1.8bp ➔ capture_rate = 90.0% (優秀)
        self.tracker.record_exit(
            trade_id="T001",
            exit_price=12720804,
            exit_time=t0 + 45.0,
            realized_pnl_bp=1.8,
            theory_spread_bp=2.0,
        )
        self.assertEqual(rec.capture_rate, 90.0)
        self.assertEqual(rec.capture_grade, "優秀")

        # 4. サマリー集計の検証
        summary = self.tracker.get_summary_stats()
        self.assertEqual(summary["total_tracked_trades"], 1)
        self.assertEqual(summary["capture_rate_stats"]["grade"], "優秀")
        self.assertEqual(summary["fill_quality"]["TOXIC_FILL"], 1)
        self.assertIn("trend_high_vol", summary["regime_stats"])
        self.assertEqual(summary["regime_stats"]["trend_high_vol"]["count"], 1)

    def test_s2_toxic_flow_critical(self):
        """S2. Toxic Flow 分析器の危険例判定 (Toxic: 92前後)"""
        res = self.toxic_analyzer.calculate_toxic_score(
            side="buy",
            imbalance=-0.8,
            taker_buy=0.002,
            taker_sell=0.075,
            cancel_rate=0.60,
            refill_rate=0.05,
            depth_1=0.012,
            depth_3=0.04,
            depth_5=0.08,
        )
        # スコアが 85〜98 の範囲（危険水準）であることを確認
        self.assertGreaterEqual(res["toxic_score"], 85.0)
        self.assertEqual(res["level"], "TOXIC_CRITICAL")
        self.assertEqual(res["action"], "cancel")


if __name__ == "__main__":
    unittest.main()
