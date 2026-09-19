"""
tests/test_multi_asset_os.py: 多資産クラス・インテリジェンスOS (AGENT体系) 単体テスト
================================================================================
テスト対象:
1. 標準データ契約 (MicroSignal, MacroImpact, ExecutionCommand, StrategyDraft, etc.)
2. 第2階層 MACRO Impact AGENT (TDnet, PTS, 世界株価連携)
3. 第3階層 司令塔 (Regime Orchestrator: ガバナンス, リスク予算動的配分, Top-Down Precedence)
4. 第1階層 日本株専属ポッド (JP MICRO, JP ALPHA, JP EXECUTION: 100株単元, 呼値, セッション)
5. 第1階層 暗号資産専属ポッド (BTC MICRO, ALPHA, EXECUTION: 非破壊性アダプタ)
6. 統合協調サイクル (coordinate_cycle)
"""

import unittest
import time
from datetime import datetime, timezone, timedelta

from antigravity.multi_asset.schemas import (
    MicroSignal,
    MacroImpact,
    ExecutionCommand,
    StrategyDraft,
    OrderCommand,
    TradeReport,
    AssetPodState,
)
from antigravity.multi_asset.macro_impact_agent import MacroImpactAgent
from antigravity.multi_asset.regime_orchestrator import RegimeOrchestratorAgent
from antigravity.multi_asset.pods.japan_equity.jp_micro_agent import JpMicroAgent
from antigravity.multi_asset.pods.japan_equity.jp_alpha_agent import JpAlphaAgent
from antigravity.multi_asset.pods.japan_equity.jp_execution_agent import JpExecutionAgent
from antigravity.multi_asset.pods.japan_equity.jp_pod import JapanEquityPod
from antigravity.multi_asset.pods.crypto.crypto_micro_agent import CryptoMicroAgent
from antigravity.multi_asset.pods.crypto.crypto_alpha_agent import CryptoAlphaAgent
from antigravity.multi_asset.pods.crypto.crypto_execution_agent import CryptoExecutionAgent
from antigravity.multi_asset.pods.crypto.crypto_pod import CryptoPod
from antigravity.multi_asset.pods.fx.fx_micro_agent import FxMicroAgent
from antigravity.multi_asset.pods.fx.fx_alpha_agent import FxAlphaAgent
from antigravity.multi_asset.pods.fx.fx_execution_agent import FxExecutionAgent
from antigravity.multi_asset.pods.fx.fx_pod import FxPod
from news_pipeline.market_impact_scorer import MarketEvent
from news_pipeline.pts_causal_engine import PTSFeatureRecord

JST = timezone(timedelta(hours=9))


class TestMultiAssetSchemas(unittest.TestCase):
    """標準データ契約のシリアライズ・検証テスト"""

    def test_microsignal_serialization(self):
        sig = MicroSignal(
            asset_class="JP_STOCK",
            symbol="7203",
            direction="LONG",
            confidence=85.5,
            regime="TREND",
            spread_jpy=1.0,
            imbalance_ratio=2.1,
            timestamp=1726620000.0,
            extra_metrics={"tse_session": "MORNING_SESSION"},
        )
        d = sig.to_dict()
        self.assertEqual(d["asset_class"], "JP_STOCK")
        self.assertEqual(d["confidence"], 85.5)

        sig2 = MicroSignal.from_dict(d)
        self.assertEqual(sig2.symbol, "7203")
        self.assertEqual(sig2.extra_metrics["tse_session"], "MORNING_SESSION")

    def test_macro_impact_serialization(self):
        macro = MacroImpact(
            impact_score=88,
            level="CRITICAL",
            primary_event="東証システム大規模障害",
            global_regime="RISK_OFF",
            asset_impact_map={"JP_STOCK": "BEAR", "BTC": "NEUTRAL"},
            horizon="IMMEDIATE",
        )
        d = macro.to_dict()
        self.assertEqual(d["impact_score"], 88)
        self.assertEqual(d["level"], "CRITICAL")

        macro2 = MacroImpact.from_dict(d)
        self.assertEqual(macro2.primary_event, "東証システム大規模障害")
        self.assertEqual(macro2.asset_impact_map["JP_STOCK"], "BEAR")

    def test_execution_command_and_pod_state(self):
        cmd = ExecutionCommand(
            asset_class="JP_STOCK",
            target_mode="STOP",
            allocated_risk_jpy=0.0,
            max_position_size=0.0,
            is_halted=True,
            reason="マクロショック緊急停止",
        )
        self.assertTrue(cmd.is_halted)
        self.assertEqual(cmd.target_mode, "STOP")

        state = AssetPodState(
            asset_class="JP_STOCK",
            current_mode="STOP",
            active_positions={"7203": 500.0},
            daily_pnl_jpy=-1200.0,
            allocated_budget_jpy=0.0,
            is_halted=True,
            last_command=cmd,
        )
        state_dict = state.to_dict()
        self.assertEqual(state_dict["current_mode"], "STOP")
        self.assertEqual(state_dict["last_command"]["target_mode"], "STOP")

        restored = AssetPodState.from_dict(state_dict)
        self.assertEqual(restored.last_command.reason, "マクロショック緊急停止")


class TestMacroImpactAgent(unittest.TestCase):
    """第2階層 MACRO Impact AGENT のテスト"""

    def setUp(self):
        self.agent = MacroImpactAgent()

    def test_evaluate_tdnet_event(self):
        event = MarketEvent(
            event_type="決算短信",
            name="トヨタ自動車",
            symbol="7203",
            headline_metric="営利+35%大幅増益",
            direction="up",
            op_surprise=35.0,
        )
        macro = self.agent.evaluate_tdnet_event(event)
        self.assertGreaterEqual(macro.impact_score, 40)
        self.assertEqual(macro.asset_impact_map["JP_STOCK"], "BULL")
        self.assertIn("7203", macro.primary_event)

    def test_evaluate_pts_anomaly(self):
        record = PTSFeatureRecord(
            symbol="6857",
            name="アドバンテスト",
            pts_change_pct=+8.5,
            pts_volume_ratio=4.5,
            disclosure_type="決算短信",
        )
        cis2_result = {"cis2_score": 150.0, "label": "DIRECT"}
        macro = self.agent.evaluate_pts_anomaly(record, cis2_result)
        self.assertGreaterEqual(macro.impact_score, 70)
        self.assertEqual(macro.level, "WARNING")
        self.assertEqual(macro.asset_impact_map["JP_STOCK"], "BULL")

    def test_evaluate_world_market_snapshot_crash(self):
        indices = {
            "nikkei_fut": -3.5,
            "sp500": -2.8,
            "nasdaq": -3.2,
            "usdjpy_change": -1.2,
        }
        macro = self.agent.evaluate_world_market_snapshot(indices)
        self.assertEqual(macro.impact_score, 88)
        self.assertEqual(macro.level, "CRITICAL")
        self.assertEqual(macro.global_regime, "RISK_OFF")
        self.assertEqual(macro.asset_impact_map["JP_STOCK"], "BEAR")


class TestRegimeOrchestrator(unittest.TestCase):
    """第3階層 司令塔 (Regime Orchestrator) のテスト"""

    def setUp(self):
        self.orch = RegimeOrchestratorAgent(total_daily_risk_budget_jpy=100000.0)
        self.jp_pod = JapanEquityPod()
        self.btc_pod = CryptoPod()
        self.orch.register_pod(self.jp_pod)
        self.orch.register_pod(self.btc_pod)

    def test_risk_budget_allocation_across_regimes(self):
        # RISK_ON: BTC 30%, JP 40%
        alloc_on = self.orch.compute_risk_allocation("RISK_ON")
        self.assertEqual(alloc_on["BTC"], 30000.0)
        self.assertEqual(alloc_on["JP_STOCK"], 40000.0)

        # RISK_OFF: BTC 10%, JP 20%
        alloc_off = self.orch.compute_risk_allocation("RISK_OFF")
        self.assertEqual(alloc_off["BTC"], 10000.0)
        self.assertEqual(alloc_off["JP_STOCK"], 20000.0)

        # SHOCK: BTC 5%, JP 15%
        alloc_shock = self.orch.compute_risk_allocation("SHOCK")
        self.assertEqual(alloc_shock["BTC"], 5000.0)
        self.assertEqual(alloc_shock["JP_STOCK"], 15000.0)

    def test_top_down_precedence_critical_shock(self):
        """上位命令の絶対優先（Top-Down Precedence）検証"""
        macro = MacroImpact(
            impact_score=92,
            level="CRITICAL",
            primary_event="地政学危機ショック",
            global_regime="RISK_OFF",
        )
        commands = self.orch.formulate_governance_commands(macro)
        self.orch.broadcast_commands(commands)

        # 全ポッドが即座にSTOPかつis_halted=Trueになっていること
        self.assertTrue(self.jp_pod.execution.is_halted)
        self.assertEqual(self.jp_pod.execution.target_mode, "STOP")
        self.assertTrue(self.btc_pod.execution.is_halted)
        self.assertEqual(self.btc_pod.execution.target_mode, "STOP")

        # STOP発令時は新規発注が物理的に拒否されること
        order = OrderCommand(
            order_id="test_ord_1",
            asset_class="JP_STOCK",
            symbol="7203",
            side="BUY",
            size=100.0,
            price=2500.0,
        )
        report = self.jp_pod.execution.execute_order(order)
        self.assertIsNone(report)


class TestJapanEquityPod(unittest.TestCase):
    """日本株専属ポッド (JP Pod) の単体テスト"""

    def setUp(self):
        self.pod = JapanEquityPod(symbols=["7203", "9984"])

    def test_tse_session_regimes(self):
        # 前場 (09:30 JST)
        dt_morning = datetime(2026, 9, 18, 9, 30, tzinfo=JST) # 金曜日
        self.assertEqual(JpMicroAgent.get_tse_session(dt_morning), "MORNING_SESSION")

        # 昼休み (12:00 JST)
        dt_lunch = datetime(2026, 9, 18, 12, 0, tzinfo=JST)
        self.assertEqual(JpMicroAgent.get_tse_session(dt_lunch), "LUNCH_BREAK")

        # 後場 (14:30 JST)
        dt_afternoon = datetime(2026, 9, 18, 14, 30, tzinfo=JST)
        self.assertEqual(JpMicroAgent.get_tse_session(dt_afternoon), "AFTERNOON_SESSION")

        # 夜間PTS (18:30 JST)
        dt_pts = datetime(2026, 9, 18, 18, 30, tzinfo=JST)
        self.assertEqual(JpMicroAgent.get_tse_session(dt_pts), "PTS_NIGHT_SESSION")

    def test_micro_imbalance_evaluation(self):
        market_data = {
            "symbol": "7203",
            "bid_price": 2800.0,
            "ask_price": 2801.0,
            "bid_vol": 60000,
            "ask_vol": 20000, # 買い厚み 3.0倍
            "last_price": 2800.0,
            "price_change_pct": +1.2,
            "session": "MORNING_SESSION",
        }
        sig = self.pod.micro.evaluate_micro(market_data)
        self.assertEqual(sig.direction, "LONG")
        self.assertGreaterEqual(sig.confidence, 70.0)
        self.assertEqual(sig.spread_jpy, 1.0)
        self.assertEqual(sig.imbalance_ratio, 3.0)

    def test_execution_100_share_unit_and_tick(self):
        # 100株単元丸めチェック
        shares_150 = self.pod.execution.enforce_unit_shares(150.0)
        self.assertEqual(shares_150, 200)

        shares_40 = self.pod.execution.enforce_unit_shares(40.0)
        self.assertEqual(shares_40, 100) # 最小1単元

        # 呼値丸めチェック (3000円以下は1円、3000超〜5000は5円)
        self.assertEqual(JpExecutionAgent.round_to_tse_tick(2850.4), 2850.0)
        self.assertEqual(JpExecutionAgent.round_to_tse_tick(4123.0), 4125.0)

    def test_execution_order_flow_and_pnl(self):
        exec_agent = self.pod.execution

        # 1. 新規買い 100株 @ 2500円
        buy_order = OrderCommand(
            order_id="jp_ord_1",
            asset_class="JP_STOCK",
            symbol="7203",
            side="BUY",
            size=100.0,
            price=2500.0,
        )
        rep1 = exec_agent.execute_order(buy_order)
        self.assertIsNone(rep1) # 新規約定はポジション保有、レポートは決済時に生成
        self.assertEqual(exec_agent.active_positions["7203"], 100.0)

        # 2. 返済売り 100株 @ 2550円 (+50円利益)
        sell_order = OrderCommand(
            order_id="jp_ord_2",
            asset_class="JP_STOCK",
            symbol="7203",
            side="SELL",
            size=100.0,
            price=2550.0,
        )
        rep2 = exec_agent.execute_order(sell_order)
        self.assertIsNotNone(rep2)
        self.assertGreater(rep2.pnl_jpy, 0.0)
        self.assertNotIn("7203", exec_agent.active_positions)
        self.assertGreater(exec_agent.daily_pnl_jpy, 4500.0) # 50円 x 100株 - スリッページ

    def test_disclosure_injection_alpha_trigger(self):
        # 高MIS開示を注入
        event = MarketEvent(
            event_type="決算短信",
            name="トヨタ",
            symbol="7203",
            headline_metric="大幅増益",
            direction="up",
            mis=80,
        )
        self.pod.inject_tdnet_event(event)

        sig = MicroSignal(
            asset_class="JP_STOCK",
            symbol="7203",
            direction="NEUTRAL",
            confidence=50.0,
            regime="MEAN_REVERT",
            spread_jpy=1.0,
            imbalance_ratio=1.0,
        )
        alpha_res = self.pod.alpha.evaluate_alpha(sig)
        self.assertEqual(alpha_res["action"], "BUY")
        self.assertGreaterEqual(alpha_res["confidence"], 80.0)


class TestCryptoPod(unittest.TestCase):
    """暗号資産専属ポッド (BTC Pod) の単体テスト"""

    def setUp(self):
        self.pod = CryptoPod()

    def test_crypto_micro_evaluation(self):
        m_data = {
            "symbol": "FX_BTC_JPY",
            "bid_price": 10002000.0,
            "ask_price": 10002500.0,
            "bid_vol": 5.0,
            "ask_vol": 1.5,
            "taker_delta": +2.0,
        }
        sig = self.pod.micro.evaluate_micro(m_data)
        self.assertEqual(sig.direction, "LONG")
        self.assertGreaterEqual(sig.confidence, 70.0)

    def test_crypto_execution_lifecycle(self):
        exec_agent = self.pod.execution

        # 新規BUY
        buy_ord = OrderCommand(
            order_id="btc_ord_1",
            asset_class="BTC",
            symbol="FX_BTC_JPY",
            side="BUY",
            size=0.001,
            price=10000000.0,
        )
        exec_agent.execute_order(buy_ord)
        self.assertEqual(exec_agent.active_positions["FX_BTC_JPY"], 0.001)

        # 決済SELL (+20円利益)
        sell_ord = OrderCommand(
            order_id="btc_ord_2",
            asset_class="BTC",
            symbol="FX_BTC_JPY",
            side="SELL",
            size=0.001,
            price=10020000.0,
        )
        rep = exec_agent.execute_order(sell_ord)
        self.assertIsNotNone(rep)
        self.assertGreater(rep.pnl_jpy, 0.0)
        self.assertNotIn("FX_BTC_JPY", exec_agent.active_positions)


class TestFxPod(unittest.TestCase):
    """為替専属ポッド (FX Pod) の単体テスト"""

    def setUp(self):
        self.pod = FxPod(symbols=["USDJPY"])

    def test_fx_sessions(self):
        # 仲値 (09:50 JST)
        dt_fix = datetime(2026, 9, 18, 9, 50, tzinfo=JST)
        self.assertEqual(FxMicroAgent.get_fx_session(dt_fix), "TOKYO_FIX")

        # 東京セッション (13:00 JST)
        dt_tokyo = datetime(2026, 9, 18, 13, 0, tzinfo=JST)
        self.assertEqual(FxMicroAgent.get_fx_session(dt_tokyo), "TOKYO_SESSION")

        # ロンドン/NY 重複 (22:30 JST)
        dt_overlap = datetime(2026, 9, 18, 22, 30, tzinfo=JST)
        self.assertEqual(FxMicroAgent.get_fx_session(dt_overlap), "NY_LONDON_OVERLAP")

    def test_fx_micro_evaluation(self):
        m_data = {
            "symbol": "USDJPY",
            "bid_price": 155.250,
            "ask_price": 155.253, # 0.3 pips
            "bid_depth": 10.0,
            "ask_depth": 4.0,     # 2.5倍買い
            "tick_direction": +1,
            "session": "TOKYO_SESSION",
        }
        sig = self.pod.micro.evaluate_micro(m_data)
        self.assertEqual(sig.direction, "LONG")
        self.assertGreaterEqual(sig.confidence, 70.0)
        self.assertAlmostEqual(sig.extra_metrics["spread_pips"], 0.3, places=1)

    def test_fx_execution_lifecycle_and_pip_pnl(self):
        exec_agent = self.pod.execution

        # 1. 新規BUY 1.0 lot (1万通貨) @ 155.000円
        buy_ord = OrderCommand(
            order_id="fx_ord_1",
            asset_class="FX",
            symbol="USDJPY",
            side="BUY",
            size=1.0,
            price=155.000,
        )
        rep1 = exec_agent.execute_order(buy_ord)
        self.assertIsNone(rep1)
        self.assertEqual(exec_agent.active_positions["USDJPY"], 1.0)

        # 2. 決済SELL 1.0 lot @ 155.200円 (+20 pips = +0.20円/ドル x 10,000ドル = +2,000円 - スリッページ)
        sell_ord = OrderCommand(
            order_id="fx_ord_2",
            asset_class="FX",
            symbol="USDJPY",
            side="SELL",
            size=1.0,
            price=155.200,
        )
        rep2 = exec_agent.execute_order(sell_ord)
        self.assertIsNotNone(rep2)
        self.assertGreater(rep2.pnl_jpy, 1900.0)
        self.assertNotIn("USDJPY", exec_agent.active_positions)
        self.assertGreater(exec_agent.daily_pnl_jpy, 1900.0)


class TestIntegratedCoordinatedCycle(unittest.TestCase):
    """統合協調サイクル (Regime Orchestrator x JP Pod x BTC Pod x FX Pod)"""

    def test_coordinated_cycle_execution(self):
        orch = RegimeOrchestratorAgent(total_daily_risk_budget_jpy=100000.0)
        jp_pod = JapanEquityPod(symbols=["7203"])
        btc_pod = CryptoPod(symbols=["FX_BTC_JPY"])
        fx_pod = FxPod(symbols=["USDJPY"])
        orch.register_pod(jp_pod)
        orch.register_pod(btc_pod)
        orch.register_pod(fx_pod)

        macro = MacroImpact(
            impact_score=30,
            level="NORMAL",
            primary_event="通常相場レジーム",
            global_regime="RISK_ON",
            asset_impact_map={"JP_STOCK": "BULL", "BTC": "BULL", "FX": "BULL_USD"},
        )

        market_data = {
            "JP_STOCK": {
                "symbol": "7203",
                "bid_price": 2800.0,
                "ask_price": 2801.0,
                "bid_vol": 80000,
                "ask_vol": 20000,
                "last_price": 2800.0,
                "price_change_pct": +1.5,
                "session": "MORNING_SESSION",
            },
            "BTC": {
                "symbol": "FX_BTC_JPY",
                "bid_price": 10000000.0,
                "ask_price": 10000500.0,
                "bid_vol": 4.0,
                "ask_vol": 1.0,
                "taker_delta": +1.5,
            },
            "FX": {
                "symbol": "USDJPY",
                "bid_price": 155.100,
                "ask_price": 155.103,
                "bid_depth": 8.0,
                "ask_depth": 2.0,
                "tick_direction": +1,
                "session": "TOKYO_SESSION",
            },
        }

        res = orch.coordinate_cycle(macro, market_data)
        self.assertEqual(res["regime"], "RISK_ON")
        self.assertIn("JP_STOCK", res["pods"])
        self.assertIn("BTC", res["pods"])
        self.assertIn("FX", res["pods"])

        jp_signal = res["pods"]["JP_STOCK"]["signal"]
        self.assertIsNotNone(jp_signal)
        self.assertEqual(jp_signal["direction"], "LONG")

        btc_signal = res["pods"]["BTC"]["signal"]
        self.assertIsNotNone(btc_signal)
        self.assertEqual(btc_signal["direction"], "LONG")

        fx_signal = res["pods"]["FX"]["signal"]
        self.assertIsNotNone(fx_signal)
        self.assertEqual(fx_signal["direction"], "LONG")
        self.assertIn("playbook", res)


class TestOandaAdapterAndPlaybooks(unittest.TestCase):
    """OANDAアダプター安全装置 ＆ 5大クロスアセット・プレイブック検証テスト"""

    def test_oanda_risk_layer_protections(self):
        """OandaRisk 4大安全装置の単体検証"""
        from antigravity.multi_asset.pods.fx.oanda_adapter import OandaRisk

        risk = OandaRisk(
            max_spread_pips=3.0,
            max_lot_map={"USD_JPY": 3.0, "EUR_JPY": 2.0},
            daily_cb_limit_jpy=-15000.0,
            event_max_lot_ratio=0.30,
        )

        # 1. 正常時: 合格
        ok, reason = risk.pre_order_check("USD_JPY", lots=1.0, current_spread_pips=0.3)
        self.assertTrue(ok, f"正常時は合格するべき: {reason}")

        # 2. スプレッド急拡大ショック: 遮断
        ok, reason = risk.pre_order_check("USD_JPY", lots=1.0, current_spread_pips=4.5)
        self.assertFalse(ok)
        self.assertIn("スプレッド急拡大遮断", reason)

        # 3. 建玉上限超過: 遮断
        ok, reason = risk.pre_order_check("USD_JPY", lots=4.0, current_spread_pips=0.3)
        self.assertFalse(ok)
        self.assertIn("最大保有ロット上限超過", reason)

        # 4. イベント窓 (CPI/FOMC): ロット30%縮小 (通常3lot -> 0.9lot)
        risk.set_event_window(True)
        ok, reason = risk.pre_order_check("USD_JPY", lots=1.0, current_spread_pips=0.3)
        self.assertFalse(ok)
        self.assertIn("マクロイベント窓 ロット上限超過", reason)
        # 0.5lotならイベント窓でも合格
        ok, reason = risk.pre_order_check("USD_JPY", lots=0.5, current_spread_pips=0.3)
        self.assertTrue(ok)
        risk.set_event_window(False)

        # 5. 日次CBリミット到達: 遮断
        risk.daily_pnl_fx = -16000.0
        ok, reason = risk.pre_order_check("USD_JPY", lots=0.5, current_spread_pips=0.3)
        self.assertFalse(ok)
        self.assertIn("日次損失リミット到達", reason)

    def test_five_cross_asset_playbooks(self):
        """Regime Orchestrator 5大プレイブック判定とリスクバジェット配分の検証"""
        orch = RegimeOrchestratorAgent(total_daily_risk_budget_jpy=100_000.0)

        # ① FX-Macro-Dominant (米CPI / FOMC)
        macro_cpi = MacroImpact(
            impact_score=65,
            level="WARNING",
            primary_event="米8月CPI消費者物価指数 予想上振れ",
            global_regime="NEUTRAL",
            asset_impact_map={"FX": "BULL_USD", "BTC": "BEAR", "JP_STOCK": "NEUTRAL"},
        )
        pb_fx = orch.determine_playbook(macro_cpi)
        self.assertEqual(pb_fx, "FX_MACRO_DOMINANT")
        alloc_fx = orch.compute_risk_allocation(pb_fx)
        self.assertEqual(alloc_fx["FX"], 50_000.0)  # FX 50% 配分
        self.assertEqual(alloc_fx["BTC"], 20_000.0)

        # ② JP-Equity-Catalyst (東証適時開示 / PTS)
        macro_tdnet = MacroImpact(
            impact_score=40,
            level="NORMAL",
            primary_event="東証適時開示 TDnet TOB大量保有発表",
            global_regime="NEUTRAL",
        )
        pb_jp = orch.determine_playbook(macro_tdnet)
        self.assertEqual(pb_jp, "JP_EQUITY_CATALYST")
        alloc_jp = orch.compute_risk_allocation(pb_jp)
        self.assertEqual(alloc_jp["JP_STOCK"], 50_000.0)  # 日本株 50% 配分

        # ③ Crypto-Dominant (暗号資産モメンタム・リスクオン)
        macro_crypto = MacroImpact(
            impact_score=30,
            level="RISK_ON",
            primary_event="グローバルリスク選好 米株高・暗号資産急伸",
            global_regime="RISK_ON",
        )
        pb_crypto = orch.determine_playbook(macro_crypto)
        self.assertEqual(pb_crypto, "CRYPTO_DOMINANT")
        alloc_crypto = orch.compute_risk_allocation(pb_crypto)
        self.assertEqual(alloc_crypto["BTC"], 50_000.0)  # BTC 50% 配分

        # ④ Balanced Tri-Asset (平常中立)
        macro_neutral = MacroImpact(
            impact_score=20,
            level="NORMAL",
            primary_event="市場平常・中立レジーム",
            global_regime="NEUTRAL",
        )
        pb_bal = orch.determine_playbook(macro_neutral)
        self.assertEqual(pb_bal, "BALANCED_TRI_ASSET")
        alloc_bal = orch.compute_risk_allocation(pb_bal)
        self.assertEqual(alloc_bal["BTC"], 30_000.0)
        self.assertEqual(alloc_bal["JP_STOCK"], 40_000.0)
        self.assertEqual(alloc_bal["FX"], 30_000.0)

        # ⑤ Defensive FX-Anchor (クリティカルショック・急落防衛)
        macro_shock = MacroImpact(
            impact_score=92,
            level="CRITICAL",
            primary_event="世界同時株安・地政学ショック",
            global_regime="SHOCK",
        )
        pb_def = orch.determine_playbook(macro_shock)
        self.assertEqual(pb_def, "DEFENSIVE_FX_ANCHOR")
        alloc_def = orch.compute_risk_allocation(pb_def)
        self.assertEqual(alloc_def["BTC"], 0.0)  # BTC完全遮断
        self.assertEqual(alloc_def["FX"], 40_000.0)  # 為替防衛アンカー 40%

    def test_two_dimensional_arbitration_stop_vs_special_event(self):
        """
        MIS (ブレーキ) × OAS (アクセル) の二次元調停テスト
        ====================================================
        ユーザー設計要件:
        1. MIS >= 85 and OAS <= 30 -> STOP (東証上場廃止・破滅リスク)
        2. MIS >= 85 (or >= 70) and OAS >= 80 -> SPECIAL_EVENT_MODE (TOB・確定鞘取り起動)
        3. MIS < 70 and OAS >= 70 -> ALPHA_ACCUMULATE (大量保有・自社株買い)
        """
        orch = RegimeOrchestratorAgent(total_daily_risk_budget_jpy=100_000.0)
        jp_pod = JapanEquityPod(symbols=["6335", "9999"])
        btc_pod = CryptoPod()
        orch.register_pod(jp_pod)
        orch.register_pod(btc_pod)

        # ケースA: 上場廃止 (MIS 90, OAS 10) -> 全停止 STOP
        macro_delist = MacroImpact(
            impact_score=90,
            level="CRITICAL",
            primary_event="東証上場廃止決定",
            opportunity_score=10,
            opportunity_type="NONE",
            is_special_event=False,
        )
        regime_delist = orch.evaluate_macro_regime(macro_delist)
        self.assertEqual(regime_delist, "SHOCK")
        cmds_delist = orch.formulate_governance_commands(macro_delist)
        self.assertTrue(cmds_delist["JP_STOCK"].is_halted)
        self.assertEqual(cmds_delist["JP_STOCK"].target_mode, "STOP")

        # ケースB: TOB公開買付 (MIS 75, OAS 95) -> 停止ではなく SPECIAL_EVENT_MODE 起動！
        macro_tob = MacroImpact(
            impact_score=75,
            level="WARNING",
            primary_event="TOB 公開買付発表 (買付プレミアム+25%)",
            opportunity_score=95,
            opportunity_type="TOB_ARBITRAGE",
            is_special_event=True,
        )
        regime_tob = orch.evaluate_macro_regime(macro_tob)
        self.assertEqual(regime_tob, "SPECIAL_EVENT")
        self.assertEqual(orch.determine_playbook(macro_tob), "SPECIAL_EVENT")

        cmds_tob = orch.formulate_governance_commands(macro_tob)
        self.assertFalse(cmds_tob["JP_STOCK"].is_halted)
        self.assertEqual(cmds_tob["JP_STOCK"].target_mode, "SPECIAL_EVENT")
        self.assertEqual(cmds_tob["JP_STOCK"].allocated_risk_jpy, 70_000.0)  # 日本株に70%集中配分
        self.assertIn("SPECIAL EVENT ARBITRAGE", cmds_tob["JP_STOCK"].reason)

        # ケースC: 大量保有報告書 (MIS 45, OAS 85) -> ALPHA_ACCUMULATE 起動！
        macro_activist = MacroImpact(
            impact_score=45,
            level="NORMAL",
            primary_event="エフィッシモ 大量保有報告書 買い増し",
            opportunity_score=85,
            opportunity_type="ACTIVIST_FOLLOW",
        )
        regime_act = orch.evaluate_macro_regime(macro_activist)
        self.assertEqual(regime_act, "ALPHA_ACCUMULATE")
        cmds_act = orch.formulate_governance_commands(macro_activist)
        self.assertFalse(cmds_act["JP_STOCK"].is_halted)
        self.assertEqual(cmds_act["JP_STOCK"].target_mode, "ALPHA_ACCUMULATE")
        self.assertEqual(cmds_act["JP_STOCK"].allocated_risk_jpy, 60_000.0)  # 60%配分


if __name__ == "__main__":
    unittest.main()

