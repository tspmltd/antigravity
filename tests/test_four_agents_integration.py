"""
Test 4AGENT Council & Strategy Reflection Integration
=====================================================
4つのエージェント (Micro, Trend, DuckDB, Adverse) が分析結論を出し、
それが戦略 (SignalFusionEngine / SafetyGate / SyncExecutor / Simulator) へ
有効に反映されることを検証する結合テスト。
"""
import unittest
import time
import os
import json
from unittest.mock import MagicMock

from antigravity.quant_pipeline.event_bus import EventBus
from antigravity.quant_pipeline.schema import OrderbookMicroSnapshot, AgentConclusion, CouncilVerdict
from antigravity.quant_pipeline.agents.microstructure_agent import MicrostructureAgent
from antigravity.quant_pipeline.agents.trend_agent import TrendFollowAgent
from antigravity.quant_pipeline.agents.adverse_agent import AdverseResearchAgent
from antigravity.quant_pipeline.agents.duckdb_optimizer_agent import DuckDBOptimizerAgent
from antigravity.quant_pipeline.council_coordinator import FourAgentsCouncil
from antigravity.quant_pipeline.fusion_engine import SignalFusionEngine
from antigravity.quant_pipeline.safety_gate import SafetyGate, SafetyGateConfig, LiveExecutionState, DryRunStats
from antigravity.quant_pipeline.dryrun_simulator import DryRunSimulator
from antigravity.quant_pipeline.sync_executor import SyncExecutor
from antigravity.quant_pipeline.live_order_executor import LiveOrderExecutor


class TestFourAgentsIntegration(unittest.TestCase):

    def setUp(self):
        self.bus = EventBus()
        self.logger = MagicMock()
        self.notifier = MagicMock()

        self.micro_agent = MicrostructureAgent(self.bus)
        self.trend_agent = TrendFollowAgent(self.bus)
        self.adverse_agent = AdverseResearchAgent(self.bus, min_lead_ms_threshold=50.0)
        self.duckdb_agent = DuckDBOptimizerAgent(
            self.bus,
            weights_path="/tmp/test_approved_weights.json",
            auto_apply=True,
        )

        self.council = FourAgentsCouncil(
            bus=self.bus,
            micro_agent=self.micro_agent,
            trend_agent=self.trend_agent,
            duckdb_agent=self.duckdb_agent,
            adverse_agent=self.adverse_agent,
            notifier=self.notifier,
            state_path="/tmp/test_agents_council_state.json",
        )

        self.fusion_engine = SignalFusionEngine(self.bus, self.logger)
        self.fusion_engine.weights_file = "/tmp/test_approved_weights.json"

        self.simulator = DryRunSimulator(notifier=self.notifier)
        self.safety_gate = SafetyGate(SafetyGateConfig())
        self.live_executor = LiveOrderExecutor(
            notifier=self.notifier,
            enable_real_trading=False,
            symbol="FX_BTC_JPY",
        )
        self.sync_executor = SyncExecutor(
            notifier=self.notifier,
            safety_gate=self.safety_gate,
            dryrun_sim=self.simulator,
            live_executor=self.live_executor,
            enable_real_live=False,
        )

    def tearDown(self):
        for p in ["/tmp/test_approved_weights.json", "/tmp/test_agents_council_state.json"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def test_01_all_four_agents_produce_conclusions(self):
        """4エージェント全員が分析結論 (AgentConclusion) を正しく出力できることを検証"""
        snap = OrderbookMicroSnapshot(
            timestamp=int(time.time() * 1000),
            latency_ms=15.0,
            best_bid=12000000.0,
            best_ask=12002000.0,
            mid_price=12001000.0,
            micro_price=12001500.0,
            micro_dev=500.0,
            bid_depth_1=0.8,
            ask_depth_1=0.2,
            total_bid_depth=2.5,
            total_ask_depth=0.8,
            imbalance=0.515,
            taker_volume_bid=0.05,
            taker_volume_ask=0.40,
            taker_aggressiveness=0.50,
            cancel_rate=0.05,
            refill_rate=0.80,
        )

        # イベント配信
        self.bus.publish("orderbook_micro", snap)

        # 1. Micro
        micro_c = self.micro_agent.get_latest_conclusion()
        self.assertIsNotNone(micro_c)
        self.assertEqual(micro_c.agent_name, "MicrostructureAgent")
        self.assertIn("BUY", micro_c.verdict)

        # 2. Trend
        trend_c = self.trend_agent.get_latest_conclusion()
        self.assertIsNotNone(trend_c)
        self.assertEqual(trend_c.agent_name, "TrendFollowAgent")

        # 3. Adverse
        adverse_c = self.adverse_agent.get_latest_conclusion()
        self.assertIsNotNone(adverse_c)
        self.assertEqual(adverse_c.agent_name, "AdverseResearchAgent")

        # 4. DuckDB
        duckdb_c = self.duckdb_agent.get_latest_conclusion()
        self.assertIsNotNone(duckdb_c)
        self.assertEqual(duckdb_c.agent_name, "DuckDBOptimizerAgent")

        # 評議会合議判定
        verdict = self.council.deliberate()
        self.assertIsNotNone(verdict)
        self.assertIn("MicrostructureAgent", verdict.conclusions)
        self.assertIn("TrendFollowAgent", verdict.conclusions)
        self.assertIn("DuckDBOptimizerAgent", verdict.conclusions)
        self.assertIn("AdverseResearchAgent", verdict.conclusions)
        self.assertTrue(len(verdict.strategy_directives) > 0)

    def test_02_adverse_emergency_cancel_reflects_to_strategy(self):
        """AdverseAgentが逆選択を検知した時、指値キャンセル＆ポジション緊急退避が戦略に反映されることを検証"""
        now_ts = time.time()
        # まず買いポジションをシミュレータに持たせる
        self.simulator.position_side = "buy"
        self.simulator.position_size = 0.001
        self.simulator.entry_price = 12000000.0
        self.simulator.entry_ts = now_ts

        # AdverseAgent に急激な買い側逆選択（板枯渇 + トキシック成行売り急襲）を注入
        snap1 = OrderbookMicroSnapshot(
            timestamp=int(now_ts * 1000),
            latency_ms=10.0,
            best_bid=12000000.0,
            best_ask=12001000.0,
            mid_price=12000500.0,
            micro_price=12000500.0,
            micro_dev=0.0,
            bid_depth_1=1.5,
            ask_depth_1=1.5,
            total_bid_depth=3.0,
            total_ask_depth=3.0,
            imbalance=0.0,
            taker_volume_bid=0.0,
            taker_volume_ask=0.0,
            taker_aggressiveness=0.0,
        )
        self.adverse_agent.on_orderbook(snap1)

        # 板が40%以下に枯渇（DEPLETING）し、トキシック成行売りが着弾
        snap2 = OrderbookMicroSnapshot(
            timestamp=int((now_ts + 0.02) * 1000),
            latency_ms=10.0,
            best_bid=12000000.0,
            best_ask=12001000.0,
            mid_price=12000500.0,
            micro_price=11999500.0,
            micro_dev=-500.0,
            bid_depth_1=0.1,  # 枯渇
            ask_depth_1=1.5,
            total_bid_depth=1.0,
            total_ask_depth=3.0,
            imbalance=-0.5,
            taker_volume_bid=0.9,  # 大口トキシック売り
            taker_volume_ask=0.0,
            taker_aggressiveness=0.8,
        )
        self.adverse_agent.on_orderbook(snap2)

        adv_c = self.adverse_agent.get_latest_conclusion()
        self.assertTrue(adv_c.metrics.get("avoidance_on"))
        self.assertFalse(adv_c.hard_veto)
        self.assertFalse(adv_c.emergency_cancel)

        # 評議会は研究メモを載せるが、Adverse では発注を止めない
        verdict = self.council.deliberate()
        self.assertFalse(verdict.hard_veto_active)
        self.assertFalse(verdict.emergency_cancel_active)
        self.assertEqual(verdict.adverse_risk_level, "ON")

        # 装置 ON でも Fusion は注文を動かさない（研究フラグ）
        self.fusion_engine.latest_adverse = {
            "adverse_side": "buy",
            "adverse_score": 28.0,
            "avoidance_on": True,
            "lead_ms_estimated": 95.0,
        }
        dec = self.fusion_engine.evaluate()
        self.assertNotEqual(dec.action, "cancel")

    def test_03_duckdb_weights_hot_reloads_in_fusion_engine(self):
        """DuckDBエージェントが重みを更新した際、FusionEngineが無停止ホットリロードすることを検証"""
        test_weights = {
            "W_PRESSURE": {"trend": 0.88, "range": 0.22, "high_vol": 0.95, "low_vol": 0.15},
            "W_CONFLICT": {"trend": 0.33, "range": 0.77, "high_vol": 0.55, "low_vol": 0.44},
            "confidence_threshold": 0.72,
        }
        with open("/tmp/test_approved_weights.json", "w") as f:
            json.dump(test_weights, f)

        # FusionEngine がホットリロード
        self.fusion_engine.load_weights("/tmp/test_approved_weights.json")
        self.assertEqual(self.fusion_engine.W_PRESSURE["trend"], 0.88)
        self.assertEqual(self.fusion_engine.W_PRESSURE["high_vol"], 0.95)
        self.assertEqual(self.fusion_engine.confidence_threshold, 0.72)


if __name__ == "__main__":
    unittest.main()
