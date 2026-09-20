"""
Unit and Integration Tests for Adverse Excursion (AE) Tracker (Priority S1)
==========================================================================
"""
import os
import sys
import time
import json
import shutil
import tempfile
import unittest

# プロジェクトルート
BASE_DIR = "/home/azureuser/antigravity"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.quant_pipeline.adverse_excursion import AdverseExcursionTracker, CHECKPOINTS_SEC
from antigravity.quant_pipeline.agents.adverse_agent import AdverseResearchAgent
from antigravity.quant_pipeline.event_bus import EventBus
from antigravity.quant_pipeline.schema import OrderbookMicroSnapshot


class TestAdverseExcursion(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.tracker = AdverseExcursionTracker(save_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_single_entry_tracking_buy(self):
        """BUY約定後の逆行・順行の計算テスト"""
        entry_px = 12718515
        t0 = 1000.0
        record = self.tracker.track_entry(
            trade_id="trade_buy_01",
            side="buy",
            entry_price=entry_px,
            entry_time=t0,
            strategy_name="TF2BP_PEG_v2",
        )

        # 100ms 後: 12,717,497 (-1018円 = -0.8bp)
        self.tracker.on_tick(current_price=12717497, timestamp=t0 + 0.101)
        # 500ms 後: 12,716,734 (-1781円 = -1.4bp)
        self.tracker.on_tick(current_price=12716734, timestamp=t0 + 0.501)
        # 1s 後: 12,715,844 (-2671円 = -2.1bp)
        self.tracker.on_tick(current_price=12715844, timestamp=t0 + 1.001)
        # 3s 後: 12,717,625 (-890円 = -0.7bp)
        self.tracker.on_tick(current_price=12717625, timestamp=t0 + 3.001)
        # 10s 後: 12,722,585 (+4070円 = +3.2bp)
        self.tracker.on_tick(current_price=12722585, timestamp=t0 + 10.001)
        # 30s 後: 12,724,240 (+5725円 = +4.5bp)
        completed = self.tracker.on_tick(current_price=12724240, timestamp=t0 + 30.001)

        self.assertEqual(len(completed), 1)
        d = completed[0]

        # 指示書 v1.0 のフォーマット検証
        self.assertEqual(d["entry_price"], 12718515)
        self.assertEqual(d["ae_100ms"], -0.8)
        self.assertEqual(d["ae_500ms"], -1.4)
        self.assertEqual(d["ae_1s"], -2.1)
        self.assertEqual(d["ae_3s"], -0.7)
        self.assertEqual(d["ae_10s"], 3.2)
        self.assertEqual(d["ae_30s"], 4.5)

        # 保存ファイルの存在確認
        latest_file = os.path.join(self.temp_dir, "adverse_excursion_latest.json")
        jsonl_file = os.path.join(self.temp_dir, "adverse_excursion_records.jsonl")
        summary_file = os.path.join(self.temp_dir, "adverse_excursion_summary.json")

        self.assertTrue(os.path.exists(latest_file))
        self.assertTrue(os.path.exists(jsonl_file))
        self.assertTrue(os.path.exists(summary_file))

        with open(latest_file, "r") as f:
            saved_latest = json.load(f)
        self.assertEqual(saved_latest["entry_price"], 12718515)
        self.assertEqual(saved_latest["ae_10s"], 3.2)

    def test_adverse_agent_integration(self):
        """AdverseResearchAgent 経由での自動AE追跡テスト"""
        bus = EventBus()
        agent = AdverseResearchAgent(bus=bus, save_dir=self.temp_dir)

        t0 = time.time()
        # 約定イベント
        agent.track_entry(
            trade_id="agent_trade_01",
            side="buy",
            entry_price=12700000,
            entry_time=t0,
            strategy_name="UMM_PEG_v2",
        )

        # OrderbookSnapshot 到達
        snap_100ms = OrderbookMicroSnapshot(
            timestamp=int((t0 + 0.12) * 1000),
            latency_ms=1.5,
            best_bid=12698730.0,
            best_ask=12700730.0,
            mid_price=12699730.0,
            micro_price=12699730.0,
            micro_dev=0.0,
            bid_depth_1=0.2,
            ask_depth_1=0.2,
            total_bid_depth=1.0,
            total_ask_depth=1.0,
            imbalance=0.0,
            taker_volume_bid=0.0,
            taker_volume_ask=0.0,
            taker_aggressiveness=0.0,
            cancel_rate=0.0,
            refill_rate=0.0,
        )
        agent.on_orderbook(snap_100ms)

        # 最新結論の確認
        conc = agent.get_latest_conclusion()
        self.assertIsNotNone(conc)
        self.assertEqual(conc.metrics.get("agent_rank"), "CHIEF_RESEARCH_AGENT")


if __name__ == "__main__":
    unittest.main()
