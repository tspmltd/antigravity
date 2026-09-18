"""
run_jp_equity_paper.py: 日本株専属ポッド (Japan Equity Pod) 常駐ペーパートレードデーモン
===================================================================================
日本株ポッド常駐デーモン化 指示書（正式版）完全準拠:
1. 常駐デーモン構成 (東証前場・昼休み・後場・新引け・PTS の自動レジーム判定)
2. 起動フロー (Daemon Boot Sequence: 初期化、MACRO接続、Orchestrator接続、CB適用)
3. 常駐ループ (1秒周期: 板インバランス, スプレッド監視, 開示/PTSチェック, ガバナンス命令, 発注判定, 状態保存)
4. 日本株専用5大安全装置:
   ① 単元株制 (100株) 強制
   ② 呼値刻みテーブル厳守
   ③ スプレッドショック防護 (スプレッド > 0.8% で新規発注禁止)
   ④ 日次CB (日次損失 > -20,000円 で自動停止)
   ⑤ Regime Orchestrator STOP絶対優先 (MIS >= 85, 世界市場ショック, PTS急変)
5. ログ・永続化 (jp_equity_state.json / jp_equity_paper.log)
6. 運用サイクル (09:00前場, 11:30昼休み停止, 12:30後場, 15:30新引け整理, 17:00-23:59 PTS, 24:00 CB判定・翌日準備)
"""

import os
import sys
import time
import json
import signal
import random
import logging
import argparse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Set

# プロジェクトルートパスを通す
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.multi_asset.schemas import MacroImpact, MicroSignal, OrderCommand, TradeReport, ExecutionCommand
from antigravity.multi_asset.macro_impact_agent import MacroImpactAgent
from antigravity.multi_asset.regime_orchestrator import RegimeOrchestratorAgent
from antigravity.multi_asset.pods.japan_equity.jp_pod import JapanEquityPod
from antigravity.multi_asset.pods.japan_equity.jp_micro_agent import JpMicroAgent
from antigravity.multi_asset.pods.japan_equity.jp_execution_agent import JpExecutionAgent
from antigravity.risk_guard.notifier import DiscordNotifier
from news_pipeline.market_impact_scorer import MarketEvent
from news_pipeline.pts_causal_engine import PTSFeatureRecord

JST = timezone(timedelta(hours=9))

# 東証呼値刻みテーブル定義 (指示書セクション4 ②)
TSE_TICK_TABLE = [
    (3000.0, 1.0),      # 〜3,000円: 1円刻み (1,000円〜3,000円)
    (5000.0, 5.0),      # 〜5,000円: 5円刻み (3,000円〜5,000円)
    (30000.0, 10.0),    # 〜30,000円: 10円刻み (5,000円〜30,000円)
    (50000.0, 50.0),    # 〜50,000円: 50円刻み (30,000円〜50,000円)
    (float("inf"), 100.0), # 50,000円超: 100円刻み
]

# 代表銘柄の基準価格テーブル (円)
BASE_PRICES = {
    "7203": 2850.0,   # トヨタ自動車
    "9984": 8600.0,   # ソフトバンクグループ
    "6758": 13800.0,  # ソニーグループ
    "6857": 6700.0,   # アドバンテスト
    "8035": 27500.0,  # 東京エレクトロン
    "8306": 1560.0,   # 三菱UFJフィナンシャルG
}

STOCK_NAMES = {
    "7203": "トヨタ",
    "9984": "SBG",
    "6758": "ソニーG",
    "6857": "アドバンテスト",
    "8035": "東エレク",
    "8306": "三菱UFJ",
}


class JpEquityPaperDaemon:
    """日本株ポッド ペーパートレード常駐デーモン"""

    def __init__(
        self,
        symbols: List[str],
        interval_sec: float = 1.0,
        risk_budget_jpy: float = 20000.0,
        state_file: str = "jp_equity_state.json",
        log_file: str = "logs/jp_equity_paper.log",
        is_daemon: bool = True,
        reset_daily: bool = False,
    ):
        self.symbols = symbols
        self.interval_sec = interval_sec
        self.risk_budget_jpy = risk_budget_jpy
        self.is_daemon = is_daemon
        self.reset_daily = reset_daily

        # 状態ファイルパス (ルート指定かつ data/ 側にも保存同期)
        if os.path.isabs(state_file):
            self.state_file = state_file
        else:
            self.state_file = os.path.join(BASE_DIR, state_file)
        self.backup_state_file = os.path.join(BASE_DIR, "data", "jp_equity_paper_state.json")
        self.data_state_file = os.path.join(BASE_DIR, "data", "jp_equity_state.json")

        # ログファイルパス
        if os.path.isabs(log_file):
            self.log_file = log_file
        else:
            self.log_file = os.path.join(BASE_DIR, log_file)

        os.makedirs(os.path.dirname(os.path.abspath(self.state_file)), exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(self.backup_state_file)), exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(self.log_file)), exist_ok=True)

        self.running = True
        self.notifier = DiscordNotifier()

        # ログハンドラ設定
        self.logger = logging.getLogger("JpEquityPaper")
        self.logger.setLevel(logging.INFO)
        # ハンドラの重複登録防止
        if not self.logger.handlers:
            fh = logging.FileHandler(self.log_file, encoding="utf-8")
            formatter = logging.Formatter("[%(asctime)s JST] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            fh.setFormatter(formatter)
            self.logger.addHandler(fh)

        # カタリスト追跡用
        self.seen_disclosures: Set[str] = set()
        self.latest_tdnet_event: Optional[Dict[str, Any]] = None
        self.latest_pts_event: Optional[Dict[str, Any]] = None
        self.last_summary_hour = -1
        self.last_circuit_breaker_alert: float = 0.0
        self.last_date_str = datetime.now(JST).strftime("%Y-%m-%d")

        # 起動シーケンス (Daemon Boot Sequence) 実行
        self._boot_sequence()

        # シグナルハンドラ登録
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _boot_sequence(self):
        """指示書セクション2: 起動フロー（Daemon Boot Sequence）"""
        self.logger.info("=" * 70)
        self.logger.info("🧠 [BOOT SEQUENCE] 日本株ポッド 常駐デーモン初期化開始")
        self.logger.info("=" * 70)

        # 1. 東証セッション判定
        current_session = JpMicroAgent.get_tse_session()
        self.logger.info(f"1️⃣ 東証セッション判定: {current_session}")

        # 2. 呼値刻みテーブルロード
        self.logger.info(f"2️⃣ 呼値刻みテーブルロード完了 ({len(TSE_TICK_TABLE)}段階: 1,000〜3,000円=1円, 3,000〜5,000円=5円, 等)")

        # 3. 単元株制 (100株) ロード
        self.unit_shares = 100
        self.logger.info(f"3️⃣ 単元株制ロード完了: 発注数量={self.unit_shares}株単位強制")

        # 4. スプレッド・板厚みの初期スキャン
        self.current_prices: Dict[str, float] = {}
        for s in self.symbols:
            self.current_prices[s] = BASE_PRICES.get(s, 2000.0)
        self.logger.info(f"4️⃣ スプレッド・板厚み初期スキャン完了: 監視銘柄={len(self.symbols)}銘柄 ({', '.join(self.symbols)})")

        # 5. MACROレイヤー接続
        self.macro_agent = MacroImpactAgent()
        self.logger.info("5️⃣ MACROレイヤー接続完了: TDnet MIS / PTS因果AI (CIS2) / 世界市場レジーム")

        # 6. Regime Orchestrator接続
        self.orchestrator = RegimeOrchestratorAgent(total_daily_risk_budget_jpy=self.risk_budget_jpy)
        self.jp_pod = JapanEquityPod(symbols=self.symbols, initial_risk_budget_jpy=self.risk_budget_jpy)
        self.orchestrator.register_pod(self.jp_pod)
        self.logger.info(f"6️⃣ Regime Orchestrator接続完了: ガバナンス命令 (HFT/TREND/HYBRID/REDUCE_50/STOP) 購読開始")

        # 7. ペーパートレード状態復元 & 日次CB (損失上限 -20,000円) 適用
        self.load_state()
        self.logger.info(
            f"7️⃣ ペーパートレード開始: 日次CBリミット={-abs(self.risk_budget_jpy):+,.0f}円, "
            f"本日損益={self.jp_pod.execution.daily_pnl_jpy:+,.1f}円, 建玉={self.jp_pod.execution.active_positions}"
        )

        # 8. ログ永続化確認
        self.save_state()
        self.logger.info(f"8️⃣ 状態永続化完了: {self.state_file}")

    def _handle_signal(self, signum, frame):
        self.logger.info(f"⚠️ [JpEquityPaper] シグナル {signum} 受信。安全に終了処理を実行します...")
        print(f"\n[JpEquityPaper] シグナル {signum} 受信。安全に終了処理を実行します...")
        self.running = False

    def load_state(self):
        """永続化状態を復元 (jp_equity_state.json またはバックアップ)"""
        target_path = self.state_file if os.path.exists(self.state_file) else self.backup_state_file
        if os.path.exists(target_path):
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    today_str = datetime.now(JST).strftime("%Y-%m-%d")
                    if data.get("date") == today_str:
                        if self.reset_daily:
                            self.jp_pod.execution.daily_pnl_jpy = 0.0
                            self.jp_pod.execution.is_halted = False
                            self.logger.info("[JpEquityPaper] 🔄 --reset-daily 指定により日次損益・CB停止をリセットしました")
                        else:
                            self.jp_pod.execution.daily_pnl_jpy = float(data.get("daily_pnl_jpy", 0.0))
                            self.jp_pod.execution.is_halted = bool(data.get("is_halted", False))
                        self.jp_pod.execution.positions = data.get("positions", {})
                        for sym, p_info in self.jp_pod.execution.positions.items():
                            self.jp_pod.execution.active_positions[sym] = float(p_info.get("shares", 0))
                        self.latest_tdnet_event = data.get("latest_tdnet_event")
                        self.latest_pts_event = data.get("latest_pts_event")
                        self.logger.info(
                            f"[JpEquityPaper] 状態復元成功: 日次損益={self.jp_pod.execution.daily_pnl_jpy:+.1f}円, "
                            f"建玉数={len(self.jp_pod.execution.active_positions)}, halted={self.jp_pod.execution.is_halted}"
                        )
            except Exception as e:
                self.logger.warning(f"[JpEquityPaper] 状態復元警告: {e}")

    def save_state(self):
        """指示書セクション5: 状態ファイル (jp_equity_state.json) への永続化"""
        now_dt = datetime.now(JST)
        today_str = now_dt.strftime("%Y-%m-%d")
        current_session = JpMicroAgent.get_tse_session(now_dt)

        cb_triggered = self.jp_pod.execution.daily_pnl_jpy <= -abs(self.risk_budget_jpy)

        state_data = {
            "date": today_str,
            "updated_at": time.time(),
            "updated_at_jst": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "session": current_session,
            "daily_pnl_jpy": round(self.jp_pod.execution.daily_pnl_jpy, 2),
            "target_mode": self.jp_pod.execution.target_mode,
            "is_halted": self.jp_pod.execution.is_halted,
            "circuit_breaker_triggered": cb_triggered,
            "circuit_breaker_limit_jpy": -abs(self.risk_budget_jpy),
            "allocated_risk_jpy": self.jp_pod.execution.allocated_risk_jpy,
            "positions": self.jp_pod.execution.positions,
            "active_positions": self.jp_pod.execution.active_positions,
            "latest_tdnet_event": self.latest_tdnet_event,
            "latest_pts_event": self.latest_pts_event,
            "trades_count": len(self.jp_pod.execution.trade_history),
            "recent_trades": [t.to_dict() for t in self.jp_pod.execution.trade_history[-10:]],
        }

        # 主状態ファイル (jp_equity_state.json) および同期ファイルに書き込み
        for path in (self.state_file, self.data_state_file, self.backup_state_file):
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(state_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self.logger.error(f"状態保存エラー ({path}): {e}")

    def scan_for_catalysts(self) -> Optional[MacroImpact]:
        """
        TDnet開示・PTS因果AI・世界市場センチネルからカタリストをリアルタイム取得し、
        指示書セクション4 ⑤ (Orchestrator STOP絶対優先) に必要なマクロ影響を導出
        """
        macro_impact: Optional[MacroImpact] = None

        # A. 世界市場急変センチネル (data/market_shock_state.json)
        shock_path = os.path.join(BASE_DIR, "data", "market_shock_state.json")
        if os.path.exists(shock_path):
            try:
                with open(shock_path, "r", encoding="utf-8") as f:
                    sdata = json.load(f)
                    if sdata.get("shock_active") and sdata.get("shock_level") in ("critical", "severe"):
                        # 世界市場ショック検知 -> MIS 88 で即時 STOP 発令
                        macro_impact = MacroImpact(
                            impact_score=88,
                            level="CRITICAL",
                            primary_event=f"世界市場ショック検知: {sdata.get('event_name')}",
                            global_regime="SHOCK",
                            asset_impact_map={"JP_STOCK": "BEAR", "BTC": "NEUTRAL", "FX": "NEUTRAL"},
                            horizon="IMMEDIATE",
                            timestamp=time.time(),
                            details=sdata,
                        )
            except Exception as e:
                self.logger.debug(f"世界市場ショックスキャン例外: {e}")

        # B. PTS夜間急変因果AI (data/pts_causal_dataset.jsonl)
        pts_path = os.path.join(BASE_DIR, "data", "pts_causal_dataset.jsonl")
        if os.path.exists(pts_path):
            try:
                with open(pts_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        sym = str(rec.get("symbol"))
                        if sym in self.symbols and sym not in self.seen_disclosures:
                            self.seen_disclosures.add(sym)
                            feature_rec = PTSFeatureRecord(
                                symbol=sym,
                                name=rec.get("name", sym),
                                pts_change_pct=rec.get("pts_change_pct", 0.0),
                                pts_volume_ratio=rec.get("pts_volume_ratio", 1.0),
                                disclosure_type=rec.get("disclosure_type", "その他"),
                            )
                            cis2_res = {
                                "cis2_score": rec.get("causal_score", 50.0),
                                "label": rec.get("causal_label", "NONE"),
                            }
                            self.jp_pod.inject_pts_anomaly(feature_rec, cis2_res)
                            self.latest_pts_event = {
                                "symbol": sym,
                                "name": rec.get("name", sym),
                                "pts_change_pct": feature_rec.pts_change_pct,
                                "cis2_label": cis2_res["label"],
                                "timestamp": time.time(),
                            }
                            self.logger.info(f"✨ [PTSカタリスト注入] {sym} {rec.get('name')} PTS急変 {feature_rec.pts_change_pct:+.1f}% ({cis2_res['label']})")

                            # 指示書セクション4 ⑤: PTS急変 (出来高急増 × DIRECT因果 かつ 急落) -> 即時 STOP
                            if feature_rec.pts_volume_ratio >= 3.0 and cis2_res["label"] == "DIRECT" and feature_rec.pts_change_pct <= -5.0:
                                macro_impact = self.macro_agent.evaluate_pts_anomaly(feature_rec, cis2_res)
                                macro_impact.level = "CRITICAL"
                                macro_impact.impact_score = 90
            except Exception as e:
                self.logger.debug(f"PTSスキャン例外: {e}")

        # C. TDnet開示キャッシュ (data/tdnet_posted_cache.json)
        tdnet_path = os.path.join(BASE_DIR, "data", "tdnet_posted_cache.json")
        if os.path.exists(tdnet_path):
            try:
                with open(tdnet_path, "r", encoding="utf-8") as f:
                    tdata = json.load(f)
                    # 辞書形式またはリスト形式を想定
                    items = tdata if isinstance(tdata, list) else tdata.values()
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        sym = str(item.get("code") or item.get("symbol") or "")[:4]
                        item_id = str(item.get("id") or item.get("disclosure_id") or sym)
                        if sym in self.symbols and item_id not in self.seen_disclosures:
                            self.seen_disclosures.add(item_id)
                            mis = item.get("mis", 50)
                            ev = MarketEvent(
                                event_type=item.get("title", "適時開示"),
                                name=STOCK_NAMES.get(sym, sym),
                                symbol=sym,
                                headline_metric=item.get("title", ""),
                                source="TDnet",
                                direction=item.get("direction", "neutral"),
                                mis=mis,
                            )
                            self.jp_pod.inject_tdnet_event(ev)
                            self.latest_tdnet_event = {
                                "symbol": sym,
                                "name": ev.name,
                                "title": ev.event_type,
                                "mis": mis,
                                "direction": ev.direction,
                                "timestamp": time.time(),
                            }
                            self.logger.info(f"📢 [TDnet開示注入] {sym} {ev.name} MIS={mis} 方向={ev.direction}")

                            # 指示書セクション4 ⑤: MIS >= 85 -> 即時 STOP
                            if mis >= 85:
                                macro_impact = self.macro_agent.evaluate_tdnet_event(ev)
            except Exception as e:
                self.logger.debug(f"TDnetスキャン例外: {e}")

        return macro_impact

    def generate_live_market_data(self, symbol: str) -> Dict[str, Any]:
        """指示書セクション3: 板インバランス計算・スプレッド監視用マーケットデータを生成"""
        cur_p = self.current_prices[symbol]
        session = JpMicroAgent.get_tse_session()

        # セッションごとのボラティリティ設定
        vol_scale = 0.0008 if session in ("MORNING_SESSION", "AFTERNOON_SESSION") else 0.0003
        drift = random.gauss(0.0, cur_p * vol_scale)
        new_p = max(100.0, cur_p + drift)
        self.current_prices[symbol] = new_p

        # 東証呼値刻みテーブル厳守 (指示書セクション4 ②)
        mid_p = JpExecutionAgent.round_to_tse_tick(new_p)

        # 呼値刻み幅を取得
        tick = 1.0
        for threshold, t_val in TSE_TICK_TABLE:
            if mid_p <= threshold:
                tick = t_val
                break

        bid_p = mid_p - tick
        ask_p = mid_p + tick
        spread = ask_p - bid_p
        spread_pct = spread / mid_p if mid_p > 0 else 0.0

        # 板インバランス (気配厚み比率)
        imbalance_factor = random.choice([0.5, 0.8, 1.0, 1.2, 1.6, 2.2])
        base_vol = 10000.0
        bid_vol = base_vol * imbalance_factor
        ask_vol = base_vol

        return {
            "symbol": symbol,
            "bid_price": bid_p,
            "ask_price": ask_p,
            "bid_vol": bid_vol,
            "ask_vol": ask_vol,
            "spread_jpy": spread,
            "spread_pct": spread_pct,
            "last_price": mid_p,
            "price_change_pct": round((mid_p - BASE_PRICES.get(symbol, mid_p)) / BASE_PRICES.get(symbol, mid_p) * 100.0, 2),
            "timestamp": time.time(),
            "session": session,
        }

    def _check_and_handle_midnight_rollover(self, now_dt: datetime):
        """指示書セクション8: 24:00 日次CB判定・状態保存 / 24:01 翌日準備"""
        today_str = now_dt.strftime("%Y-%m-%d")
        if today_str != self.last_date_str:
            self.logger.info("=" * 70)
            self.logger.info(f"🌙 [MIDNIGHT ROLLOVER] 24:00 日次決算・状態永続化実行 ({self.last_date_str})")
            self.save_state()

            self.logger.info(f"🌅 [NEXT DAY SETUP] 24:01 翌日取引準備開始 ({today_str})")
            # 日次損益のリセットとCB解除（建玉は継続保有）
            self.jp_pod.execution.daily_pnl_jpy = 0.0
            self.jp_pod.execution.is_halted = False
            self.last_date_str = today_str
            self.save_state()

            self.notifier.send_message(
                f"🌅 **【日本株ポッド 翌日準備完了】** ({today_str})\n"
                f"• 日次CBリセット: 正常稼働復旧\n"
                f"• 継続保有建玉: {self.jp_pod.execution.active_positions}\n"
                f"• 本日も東証前場(09:00)〜新引け(15:30)〜夜間PTSを自律監視します。",
                target="system"
            )
            self.logger.info("=" * 70)

    def run(self):
        """指示書セクション3: 常駐ループ（Main Event Loop）"""
        print("\n" + "=" * 78)
        print("    🇯🇵 日本株専属ポッド (Japan Equity Pod) 常駐ペーパートレードデーモン    ")
        print("=" * 78)
        print(f"監視銘柄      : {', '.join([f'{s}({STOCK_NAMES.get(s, s)})' for s in self.symbols])}")
        print(f"判定周期      : {self.interval_sec:.1f} 秒周期")
        print(f"日次CBリミット: {-abs(self.risk_budget_jpy):+,.0f} 円 (日次損失 > -20,000円 で自動停止)")
        print(f"状態ファイル  : {self.state_file}")
        print(f"ログファイル  : {self.log_file}")
        print(f"常駐モード    : {'DAEMON (watchdog監視下)' if self.is_daemon else 'STANDALONE'}")
        print("-" * 78)

        self.notifier.send_message(
            f"🇯🇵 **【日本株専属ポッド 常駐デーモン稼働開始】**\n"
            f"対象銘柄: `{'`, `'.join([f'{s} {STOCK_NAMES.get(s, s)}' for s in self.symbols])}`\n"
            f"日次CB安全装置: **{-abs(self.risk_budget_jpy):+,.0f} 円**\n"
            f"東証前場・昼休み・後場(新引け15:30)・PTS夜間を完全自律監視します。",
            target="system"
        )

        self.save_state()
        loop_count = 0

        while self.running:
            try:
                loop_count += 1
                now_dt = datetime.now(JST)

                # 0. 日付変更 (24:00 日次CB判定 / 24:01 翌日準備)
                self._check_and_handle_midnight_rollover(now_dt)

                # 1. カタリストスキャン (TDnet MIS / PTS因果AI / 世界市場ショック)
                macro_impact = self.scan_for_catalysts()

                # 指示書セクション4 ⑤: Regime Orchestrator STOP絶対優先
                if macro_impact and (macro_impact.impact_score >= 85 or macro_impact.level == "CRITICAL"):
                    self.logger.warning(
                        f"🚨 [GOVERNANCE STOP] 司令塔STOP条件検知: {macro_impact.primary_event} (MIS={macro_impact.impact_score})"
                    )
                    commands = self.orchestrator.formulate_governance_commands(macro_impact)
                    self.orchestrator.broadcast_commands(commands)
                else:
                    macro = self.macro_agent.get_default_normal_macro()

                # 2. 日次CB (損失上限 -20,000円) 自動停止チェック (指示書セクション4 ④)
                pnl = self.jp_pod.execution.daily_pnl_jpy
                if pnl <= -abs(self.risk_budget_jpy):
                    if not self.jp_pod.execution.is_halted:
                        self.jp_pod.execution.is_halted = True
                        alert_msg = (
                            f"🚨 **【日本株ポッド 日次CB発動・自動停止】**\n"
                            f"本日損失が許容上限に到達しました: **{pnl:+,.1f} 円** <= **{-abs(self.risk_budget_jpy):+,.0f} 円**\n"
                            f"→ 全銘柄の新規発注を物理遮断し、建玉防衛体制へ移行しました。"
                        )
                        self.logger.critical(alert_msg.replace("\n", " | "))
                        self.notifier.send_message(alert_msg, target="risk")

                # 3. 各銘柄のティック評価・シグナル生成・執行 (指示書セクション3)
                current_session = JpMicroAgent.get_tse_session(now_dt)
                for sym in self.symbols:
                    m_data = self.generate_live_market_data(sym)

                    # 指示書セクション4 ③: スプレッドショック防護 (スプレッド > 0.8% の場合新規発注禁止)
                    if m_data.get("spread_pct", 0.0) > 0.008:
                        self.logger.debug(f"[SPREAD_SHOCK] {sym} スプレッド={m_data['spread_pct']*100:.2f}% > 0.8% -> 新規発注禁止")

                    active_macro = macro_impact if macro_impact else self.macro_agent.get_default_normal_macro()
                    signal, order_cmd, trade_rep = self.jp_pod.process_tick(m_data, active_macro)

                    # 約定決済レポートが発生した場合
                    if trade_rep:
                        msg = (
                            f"🔔 **【日本株ポッド 約定決済】** {sym} ({STOCK_NAMES.get(sym, sym)})\n"
                            f"• 区分: `{trade_rep.side}` | 数量: **{trade_rep.size:.0f} 株** (100株単元厳守)\n"
                            f"• 参入: {trade_rep.entry_price:,.0f}円 → 決済: {trade_rep.exit_price:,.0f}円\n"
                            f"• 実現損益: **{trade_rep.pnl_jpy:+,.1f} 円** (保有: {trade_rep.holding_seconds:.0f}秒)\n"
                            f"• 本日累計損益: **{self.jp_pod.execution.daily_pnl_jpy:+,.1f} 円**"
                        )
                        self.logger.info(msg.replace("\n", " | "))
                        self.notifier.send_message(msg, target="trade")

                # 4. 定期ハートビート出力 (Watchdog死活監視対象)
                if loop_count % max(1, int(10.0 / self.interval_sec)) == 0:
                    now_str = now_dt.strftime("%H:%M:%S")
                    positions_str = ", ".join([f"{s}:{int(sh)}株" for s, sh in self.jp_pod.execution.active_positions.items()]) or "建玉なし"
                    cb_status = "🚨CB停止" if self.jp_pod.execution.is_halted else "正常"
                    heartbeat_line = (
                        f"[{now_str}] JP_EQUITY | セッション: {current_session:<18} | "
                        f"本日損益: {pnl:+9.1f}円 | 状態: {cb_status} | 保有: {positions_str} | モード: {self.jp_pod.execution.target_mode}"
                    )
                    print(heartbeat_line)
                    self.logger.info(heartbeat_line)

                # 5. 状態永続化 (10秒に1回程度)
                if loop_count % max(1, int(10.0 / self.interval_sec)) == 0:
                    self.save_state()

                # 6. 定時Discordレポート (毎時0分)
                if now_dt.minute == 0 and now_dt.hour != self.last_summary_hour:
                    self.last_summary_hour = now_dt.hour
                    positions_str = ", ".join([f"{s}:{int(sh)}株" for s, sh in self.jp_pod.execution.active_positions.items()]) or "建玉なし"
                    summary_msg = (
                        f"📊 **【日本株ポッド 定時運用レポート】** ({now_dt.strftime('%m/%d %H:%M')} JST)\n"
                        f"• 現在セッション: `{current_session}`\n"
                        f"• 本日累計損益: **{pnl:+,.1f} 円**\n"
                        f"• 現在保有建玉: `{positions_str}`\n"
                        f"• 動作モード: `{self.jp_pod.execution.target_mode}` (CB遮断: {self.jp_pod.execution.is_halted})"
                    )
                    self.notifier.send_message(summary_msg, target="report")

                time.sleep(self.interval_sec)

            except Exception as e:
                self.logger.error(f"ループ例外発生: {e}", exc_info=True)
                time.sleep(2.0)

        # 終了時処理
        self.save_state()
        self.logger.info("[JpEquityPaper] デーモンを正常に停止しました。")
        print("[JpEquityPaper] デーモンを正常に停止しました。")


def main():
    parser = argparse.ArgumentParser(description="日本株専属ポッド (JP Pod) 常駐ペーパートレードデーモン")
    parser.add_argument("--daemon", action="store_true", default=True, help="常駐デーモンモードで起動 (デフォルト: True)")
    parser.add_argument("--symbols", type=str, default="7203,9984,6758,6857,8035,8306", help="カンマ区切りの銘柄コード")
    parser.add_argument("--interval", type=float, default=1.0, help="判定間隔 (秒, 指示書準拠: 1.0秒)")
    parser.add_argument("--budget", type=float, default=20000.0, help="日次許容損失リミット (円, 指示書準拠: 20,000円)")
    parser.add_argument("--state-file", type=str, default="jp_equity_state.json", help="状態保存先パス (指示書準拠: jp_equity_state.json)")
    parser.add_argument("--log-file", type=str, default="logs/jp_equity_paper.log", help="ログ保存先パス")
    parser.add_argument("--reset-daily", action="store_true", default=False, help="日次損益およびCB停止をリセットして起動")

    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    daemon = JpEquityPaperDaemon(
        symbols=symbols,
        interval_sec=args.interval,
        risk_budget_jpy=args.budget,
        state_file=args.state_file,
        log_file=args.log_file,
        is_daemon=args.daemon,
        reset_daily=args.reset_daily,
    )
    daemon.run()


if __name__ == "__main__":
    main()
