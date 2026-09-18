"""
tests/test_jp_equity_daemon.py: 日本株ポッド常駐デーモン化 自動テストスイート
================================================================================
日本株ポッド常駐デーモン化 指示書（正式版）セクション6 完全準拠テスト:
1. 東証セッション判定 (前場/昼休み/後場/新引け/PTS)
2. 呼値丸めテスト (1,000〜3,000円: 1円、3,000〜5,000円: 5円、等)
3. 単元株制テスト (100株倍数強制)
4. スプレッドショック防護テスト (スプレッド > 0.8% 新規発注禁止)
5. TDnet高MIS開示 → 戦略モード切替 & アルファ反応
6. PTS急変 → alpha_agent反応
7. Regime Orchestrator STOP → 発注遮断
8. 日次CB → 自動停止 (-20,000円リミット)
9. 状態ファイル永続化 (jp_equity_state.json)
10. watchdog.py による登録・自動再起動設定確認
"""

import os
import sys
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.multi_asset.schemas import MicroSignal, MacroImpact, OrderCommand, ExecutionCommand
from antigravity.multi_asset.pods.japan_equity.jp_micro_agent import JpMicroAgent
from antigravity.multi_asset.pods.japan_equity.jp_alpha_agent import JpAlphaAgent
from antigravity.multi_asset.pods.japan_equity.jp_execution_agent import JpExecutionAgent
from antigravity.multi_asset.pods.japan_equity.jp_pod import JapanEquityPod
from antigravity.multi_asset.regime_orchestrator import RegimeOrchestratorAgent
from antigravity.multi_asset.macro_impact_agent import MacroImpactAgent
from news_pipeline.market_impact_scorer import MarketEvent
from news_pipeline.pts_causal_engine import PTSFeatureRecord
from run_jp_equity_paper import JpEquityPaperDaemon, TSE_TICK_TABLE

JST = timezone(timedelta(hours=9))


class TestJpEquityDaemonSuite(unittest.TestCase):
    """日本株ポッド常駐デーモン化 指示書（正式版）自動テストスイート"""

    # --------------------------------------------------------------------------
    # 1. 東証セッション判定テスト
    # --------------------------------------------------------------------------
    def test_01_tse_sessions(self):
        """東証新取引時間 (15:30引け) & 昼休み・PTSセッションの判定検証"""
        weekday = datetime(2026, 9, 18, 0, 0, tzinfo=JST)  # 金曜日

        # 08:30 JST -> PRE_MARKET
        dt_pre = weekday.replace(hour=8, minute=30)
        assert JpMicroAgent.get_tse_session(dt_pre) == "PRE_MARKET"

        # 09:30 JST -> MORNING_SESSION (前場)
        dt_morn = weekday.replace(hour=9, minute=30)
        assert JpMicroAgent.get_tse_session(dt_morn) == "MORNING_SESSION"

        # 12:00 JST -> LUNCH_BREAK (昼休み)
        dt_lunch = weekday.replace(hour=12, minute=0)
        assert JpMicroAgent.get_tse_session(dt_lunch) == "LUNCH_BREAK"

        # 13:30 JST -> AFTERNOON_SESSION (後場)
        dt_after = weekday.replace(hour=13, minute=30)
        assert JpMicroAgent.get_tse_session(dt_after) == "AFTERNOON_SESSION"

        # 15:29 JST -> AFTERNOON_SESSION (新引け 15:30 直前)
        dt_close_pre = weekday.replace(hour=15, minute=29)
        assert JpMicroAgent.get_tse_session(dt_close_pre) == "AFTERNOON_SESSION"

        # 16:00 JST -> POST_MARKET (引け後)
        dt_post = weekday.replace(hour=16, minute=0)
        assert JpMicroAgent.get_tse_session(dt_post) == "POST_MARKET"

        # 18:30 JST -> PTS_NIGHT_SESSION (夜間PTS)
        dt_pts = weekday.replace(hour=18, minute=30)
        assert JpMicroAgent.get_tse_session(dt_pts) == "PTS_NIGHT_SESSION"

        # 土日判定
        weekend = datetime(2026, 9, 20, 10, 0, tzinfo=JST)  # 日曜日
        assert JpMicroAgent.get_tse_session(weekend) == "CLOSED_WEEKEND"

    # --------------------------------------------------------------------------
    # 2. 呼値刻みテーブル厳守テスト
    # --------------------------------------------------------------------------
    def test_02_tick_rounding(self):
        """東証呼値刻みテーブルに従った価格丸めの検証"""
        exec_agent = JpExecutionAgent()

        # 1,000円〜3,000円: 1円刻み
        assert exec_agent.round_to_tse_tick(2850.4) == 2850.0
        assert exec_agent.round_to_tse_tick(2850.6) == 2851.0
        assert exec_agent.round_to_tse_tick(1560.2) == 1560.0

        # 3,000円〜5,000円: 5円刻み
        assert exec_agent.round_to_tse_tick(3452.0) == 3450.0
        assert exec_agent.round_to_tse_tick(3454.0) == 3455.0
        assert exec_agent.round_to_tse_tick(4998.0) == 5000.0

        # 5,000円〜30,000円: 10円刻み
        assert exec_agent.round_to_tse_tick(8603.0) == 8600.0
        assert exec_agent.round_to_tse_tick(8607.0) == 8610.0
        assert exec_agent.round_to_tse_tick(27504.0) == 27500.0
        assert exec_agent.round_to_tse_tick(27506.0) == 27510.0

        # 30,000円〜50,000円: 50円刻み
        assert exec_agent.round_to_tse_tick(45022.0) == 45000.0
        assert exec_agent.round_to_tse_tick(45030.0) == 45050.0

        # 50,000円超: 100円刻み
        assert exec_agent.round_to_tse_tick(65040.0) == 65000.0
        assert exec_agent.round_to_tse_tick(65060.0) == 65100.0

    # --------------------------------------------------------------------------
    # 3. 単元株制 (100株) 強制テスト
    # --------------------------------------------------------------------------
    def test_03_unit_shares_enforcement(self):
        """発注数量が必ず100株の倍数に丸められるかの検証"""
        exec_agent = JpExecutionAgent()

        assert exec_agent.enforce_unit_shares(1.0) == 100
        assert exec_agent.enforce_unit_shares(50.0) == 100
        assert exec_agent.enforce_unit_shares(99.0) == 100
        assert exec_agent.enforce_unit_shares(100.0) == 100
        assert exec_agent.enforce_unit_shares(149.0) == 100
        assert exec_agent.enforce_unit_shares(151.0) == 200
        assert exec_agent.enforce_unit_shares(250.0) == 300
        assert exec_agent.enforce_unit_shares(1000.0) == 1000

        # 実際のexecute_orderで端数発注が100株単位に強制されるか
        order = OrderCommand(
            order_id="test_unit",
            asset_class="JP_STOCK",
            symbol="7203",
            side="BUY",
            size=149.0,
            price=2850.0,
        )
        report = exec_agent.execute_order(order)
        # 建玉が100株になっていることを確認
        assert exec_agent.positions["7203"]["shares"] == 100

    # --------------------------------------------------------------------------
    # 4. スプレッドショック防護テスト (スプレッド > 0.8% 新規発注禁止)
    # --------------------------------------------------------------------------
    def test_04_spread_shock_guard(self):
        """スプレッド > 0.8% の場合、新規発注が物理遮断されることの検証"""
        exec_agent = JpExecutionAgent()

        # スプレッド 1.2% (> 0.8%) の新規注文
        order_shock = OrderCommand(
            order_id="test_shock",
            asset_class="JP_STOCK",
            symbol="9984",
            side="BUY",
            size=100.0,
            price=8600.0,
            extra={"spread_pct": 0.012},  # 1.2%
        )
        report = exec_agent.execute_order(order_shock)
        assert report is None  # 新規発注が物理遮断されていること
        assert "9984" not in exec_agent.positions

        # スプレッド 0.3% (<= 0.8%) の正常注文
        order_normal = OrderCommand(
            order_id="test_normal",
            asset_class="JP_STOCK",
            symbol="9984",
            side="BUY",
            size=100.0,
            price=8600.0,
            extra={"spread_pct": 0.003},  # 0.3%
        )
        report_normal = exec_agent.execute_order(order_normal)
        assert report_normal is None  # 新規は約定（返済レポートは出ないが建玉登録される）
        assert exec_agent.positions["9984"]["shares"] == 100

        # 返済注文の場合はスプレッドが高くても決済が通ることの検証
        close_order = OrderCommand(
            order_id="test_close",
            asset_class="JP_STOCK",
            symbol="9984",
            side="SELL",
            size=100.0,
            price=8650.0,
            extra={"spread_pct": 0.015},  # 1.5%
        )
        close_rep = exec_agent.execute_order(close_order)
        assert close_rep is not None
        assert close_rep.pnl_jpy > 0
        assert "9984" not in exec_agent.positions

    # --------------------------------------------------------------------------
    # 5. TDnet高MIS開示 → 戦略モード切替 & アルファ反応テスト
    # --------------------------------------------------------------------------
    def test_05_tdnet_high_mis(self):
        """高MIS開示 (MIS >= 70 / 85) 受信時にアルファとマクロが即応することの検証"""
        pod = JapanEquityPod(symbols=["7203"])
        macro_agent = MacroImpactAgent()

        # MIS=90 の上方修正開示
        event = MarketEvent(
            event_type="業績予想の修正",
            name="トヨタ",
            symbol="7203",
            headline_metric="通期営業利益+35%上方修正",
            source="TDnet",
            direction="up",
            mis=90,
        )
        pod.inject_tdnet_event(event)

        # Alphaエージェントの判定検証
        sig = MicroSignal(
            asset_class="JP_STOCK",
            symbol="7203",
            direction="LONG",
            confidence=60.0,
            regime="TREND",
            spread_jpy=1.0,
            imbalance_ratio=1.5,
            timestamp=1726620000.0,
            extra_metrics={"tse_session": "MORNING_SESSION", "mid_price": 2850.0, "spread_pct": 0.00035},
        )
        alpha_res = pod.alpha.evaluate_alpha(sig)
        assert alpha_res["action"] == "BUY"
        assert alpha_res["confidence"] >= 80.0
        assert "高MIS開示" in alpha_res["reason"]

        # MacroImpactAgent によるレジーム格上げ検証
        macro = macro_agent.evaluate_tdnet_event(event)
        assert macro.impact_score == 90
        assert macro.level == "CRITICAL"
        assert macro.asset_impact_map["JP_STOCK"] == "BULL"

    # --------------------------------------------------------------------------
    # 6. PTS急変 → alpha_agent反応テスト
    # --------------------------------------------------------------------------
    def test_06_pts_anomaly_alpha(self):
        """夜間PTSでの急変 (出来高急増 & CIS2 DIRECT因果) に対するアルファ反応検証"""
        pod = JapanEquityPod(symbols=["6758"])

        feature_rec = PTSFeatureRecord(
            symbol="6758",
            name="ソニーG",
            pts_change_pct=+8.5,
            pts_volume_ratio=4.2,
            disclosure_type="決算",
        )
        cis2_res = {"cis2_score": 160.0, "label": "DIRECT"}
        pod.inject_pts_anomaly(feature_rec, cis2_res)

        sig = MicroSignal(
            asset_class="JP_STOCK",
            symbol="6758",
            direction="LONG",
            confidence=50.0,
            regime="MEAN_REVERT",
            spread_jpy=5.0,
            imbalance_ratio=1.8,
            timestamp=1726620000.0,
            extra_metrics={"tse_session": "PTS_NIGHT_SESSION", "mid_price": 13800.0, "spread_pct": 0.00036},
        )
        alpha_res = pod.alpha.evaluate_alpha(sig)
        assert alpha_res["action"] == "BUY"
        assert alpha_res["confidence"] >= 80.0

    # --------------------------------------------------------------------------
    # 7. Regime Orchestrator STOP → 発注遮断テスト
    # --------------------------------------------------------------------------
    def test_07_orchestrator_stop_enforcement(self):
        """司令塔 (Regime Orchestrator) が STOP を発令した際、JP Podの新規発注が遮断されることの検証"""
        pod = JapanEquityPod(symbols=["7203"])
        orchestrator = RegimeOrchestratorAgent()
        orchestrator.register_pod(pod)

        # 致命的ショックマクロ (MIS >= 85)
        shock_macro = MacroImpact(
            impact_score=92,
            level="CRITICAL",
            primary_event="世界市場大暴落・原油ショック",
            global_regime="SHOCK",
            asset_impact_map={"JP_STOCK": "BEAR", "BTC": "NEUTRAL", "FX": "NEUTRAL"},
            horizon="IMMEDIATE",
        )

        commands = orchestrator.formulate_governance_commands(shock_macro)
        assert commands["JP_STOCK"].target_mode == "STOP"
        assert commands["JP_STOCK"].is_halted is True

        orchestrator.broadcast_commands(commands)
        assert pod.execution.target_mode == "STOP"
        assert pod.execution.is_halted is True

        # 発注試行 -> 遮断されること
        m_data = {
            "symbol": "7203",
            "bid_price": 2850.0,
            "ask_price": 2851.0,
            "bid_vol": 50000.0,
            "ask_vol": 10000.0,
            "last_price": 2850.5,
            "timestamp": 1726620000.0,
            "session": "MORNING_SESSION",
        }
        sig, order, rep = pod.process_tick(m_data, shock_macro)
        assert order is None  # STOP発令中は注文が一切生成されない

    # --------------------------------------------------------------------------
    # 8. 日次CB (損失上限 -20,000円) 自動停止テスト
    # --------------------------------------------------------------------------
    def test_08_daily_circuit_breaker(self):
        """日次損失 > -20,000円 到達で自動緊急停止 (is_halted = True) となることの検証"""
        exec_agent = JpExecutionAgent(initial_risk_budget_jpy=20000.0)

        # 100株買いエントリー
        order_buy = OrderCommand(
            order_id="cb_01",
            asset_class="JP_STOCK",
            symbol="8035",
            side="BUY",
            size=100.0,
            price=27500.0,
        )
        exec_agent.execute_order(order_buy)

        # -25,000円 の大損決済をシミュレート
        order_sell = OrderCommand(
            order_id="cb_02",
            asset_class="JP_STOCK",
            symbol="8035",
            side="SELL",
            size=100.0,
            price=27250.0,  # 250円幅下落 x 100株 = -25,000円
        )
        rep = exec_agent.execute_order(order_sell)
        assert rep is not None
        assert rep.pnl_jpy <= -20000.0
        assert exec_agent.daily_pnl_jpy <= -20000.0

        # 次の注文を試行 -> CB発動で拒否されること
        order_next = OrderCommand(
            order_id="cb_03",
            asset_class="JP_STOCK",
            symbol="7203",
            side="BUY",
            size=100.0,
            price=2850.0,
        )
        rep_next = exec_agent.execute_order(order_next)
        assert rep_next is None
        assert exec_agent.is_halted is True  # CBにより自動停止

    # --------------------------------------------------------------------------
    # 9. 状態ファイル永続化テスト (jp_equity_state.json)
    # --------------------------------------------------------------------------
    def test_09_state_persistence(self):
        """状態ファイル (jp_equity_state.json) への正常な永続化と読み込みの検証"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "jp_equity_state.json")
            log_path = os.path.join(tmpdir, "jp_equity_paper.log")

            daemon = JpEquityPaperDaemon(
                symbols=["7203", "9984"],
                interval_sec=1.0,
                risk_budget_jpy=20000.0,
                state_file=state_path,
                log_file=log_path,
                is_daemon=False,
            )

            # ポジションと損益をセット
            daemon.jp_pod.execution.daily_pnl_jpy = 1250.0
            daemon.jp_pod.execution.positions = {"7203": {"shares": 100, "avg_price": 2850.0}}
            daemon.jp_pod.execution.active_positions = {"7203": 100.0}
            daemon.save_state()

            assert os.path.exists(state_path)
            with open(state_path, "r", encoding="utf-8") as f:
                saved = json.load(f)

            assert saved["daily_pnl_jpy"] == 1250.0
            assert saved["active_positions"]["7203"] == 100.0
            assert saved["circuit_breaker_limit_jpy"] == -20000.0
            assert "session" in saved
            assert "updated_at_jst" in saved

    # --------------------------------------------------------------------------
    # 10. watchdog.py による登録設定確認
    # --------------------------------------------------------------------------
    def test_10_watchdog_registration(self):
        """watchdog.py に run_jp_equity_paper.py が監視対象として登録されていることの検証"""
        from antigravity.risk_guard.watchdog import WatchdogSentinel

        watchdog = WatchdogSentinel(base_dir=BASE_DIR)
        assert "jp_equity_paper" in watchdog.services

        jp_service = watchdog.services["jp_equity_paper"]
        assert "run_jp_equity_paper.py" in jp_service["keywords"][0]
        assert "--daemon" in jp_service["start_cmd"]
        assert "--budget" in jp_service["start_cmd"]
        assert jp_service["check_heartbeat"] is True


if __name__ == "__main__":
    unittest.main()
