"""
Approved Strategy Arena Runner (承認済み12戦略 統合Dry-runアリーナ)
=====================================================================
過去に承認（Approved）された12個のアルゴリズムを一堂に会し、
同一のリアルタイム相場データ（FX_BTC_JPY）で並行して仮想売買シミュレーションを実行。
リアルタイムな成績比較・ランキング（PnL・勝率・MaxDD）を可視化する。

⚠️ 【運用規則】
  - 自動調整・再学習は完全禁止 (auto_tune_allowed = False, frozen_mode = True)。
  - パラメータは各戦略ファイルに定義された承認時固定値を使用。
  - 1プロセス内で全12戦略を高速ループ評価し、CPU・メモリ負荷を極小化 (CPU < 2%, RAM < 100MB)。
"""
import os
import sys
import time
import json
import glob
import signal
import argparse
import traceback
import importlib.util
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

import pandas as pd
import numpy as np

# プロジェクトルートパス
BASE_DIR = "/home/azureuser/antigravity"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier
from antigravity.quant_pipeline.adverse_excursion import AdverseExcursionTracker
from antigravity.quant_pipeline.toxic_flow_analyzer import ToxicFlowAnalyzer
from antigravity.quant_pipeline.spread_gate_tracker import SpreadGateTracker
from core.dataloader import DataLoader
from core.bitflyer_client import BitFlyerClient
from antigravity.quant_pipeline.arena_signal import (
    bars_for_signal_eval,
    missing_ohlcv_columns,
    read_last_signal,
    to_utc_ts,
    upsert_minute_bar,
)

JST = timezone(timedelta(hours=9))
CONFIG_PATH = os.path.join(BASE_DIR, "configs", "approved_arena_config.json")
STATE_PATH = os.path.join(BASE_DIR, "data", "dryrun_approved_arena_state.json")
ADVERSE_SCORE_STATE_PATH = os.path.join(BASE_DIR, "data", "adverse_score_state.json")
TOXIC_STATE_PATH = os.path.join(BASE_DIR, "data", "adverse_toxic_state.json")
COUNCIL_STATE_PATH = os.path.join(BASE_DIR, "configs", "agents_council_state.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "dryrun_approved_arena.log")


class FailedStrategyStub:
    """Keeps a strat_id in the arena ranking when the module fails to load."""

    def __init__(self, name: str, error: str):
        self.name = name
        self.parameters: Dict[str, Any] = {}
        self._error = error

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        raise RuntimeError(self._error)


class SingleStrategyState:
    """各戦略の仮想口座・ポジション・成績管理"""

    def __init__(self, strat_id: str, name: str, file_path: str, instance: Any, params: Dict[str, Any]):
        self.strat_id = strat_id
        self.name = name
        self.file_path = file_path
        self.instance = instance
        self.params = params

        # 仮想ポジション
        self.position: Optional[str] = None  # "buy", "sell", None
        self.position_size: float = 0.001
        self.entry_price: float = 0.0
        self.entry_time: float = 0.0
        self.be_armed: bool = False  # BE5 建値防衛アーム
        self.mfe_bp: float = 0.0
        self.current_trade_id: Optional[str] = None  # Adverse 追跡用トレードID
        # CSR-515: 連続クォート / 止血フラグ
        self.entry_halted: bool = False
        self.observe_only: bool = False
        self.pending_bid: Optional[float] = None
        self.pending_ask: Optional[float] = None
        self.quote_side: Optional[str] = None  # intended inventory direction from signal

        # 成績
        self.total_trades: int = 0
        self.win_trades: int = 0
        self.loss_trades: int = 0
        self.total_pnl: float = 0.0
        self.total_pnl_bp: float = 0.0
        self.peak_pnl: float = 0.0
        self.max_dd: float = 0.0
        self.consecutive_losses: int = 0
        self.is_halted: bool = False
        self.halt_reason: str = ""
        self.trades_history: List[Dict[str, Any]] = []

        # 直近の行動記録
        self.last_action: str = "INIT"
        self.last_reason: str = "Initial state"
        self.signal_fail_count: int = 0

    def get_window_stats(self, hours: float = 1.0) -> Dict[str, Any]:
        """指定ウィンドウ (1h または 24h) の成績を集計"""
        now = time.time()
        cutoff = now - (hours * 3600.0)
        recent = [t for t in self.trades_history if t["ts"] >= cutoff]

        total_t = len(recent)
        win_t = sum(1 for t in recent if t["is_win"])
        loss_t = total_t - win_t
        pnl_jpy = sum(t["pnl_jpy"] for t in recent)
        pnl_bp = sum(t["pnl_bp"] for t in recent)
        wr = (win_t / total_t * 100.0) if total_t > 0 else 0.0

        return {
            "window_hours": hours,
            "total_trades": total_t,
            "win_trades": win_t,
            "loss_trades": loss_t,
            "win_rate_pct": round(wr, 1),
            "pnl_jpy": round(pnl_jpy, 1),
            "pnl_bp": round(pnl_bp, 2),
        }

    def to_dict(self) -> Dict[str, Any]:
        win_rate = (self.win_trades / self.total_trades * 100) if self.total_trades > 0 else 0.0
        return {
            "strat_id": self.strat_id,
            "name": self.name,
            "file": os.path.basename(self.file_path),
            "position": self.position or "FLAT",
            "entry_price": self.entry_price,
            "hold_sec": round(time.time() - self.entry_time, 0) if self.position else 0,
            "total_trades": self.total_trades,
            "win_trades": self.win_trades,
            "loss_trades": self.loss_trades,
            "win_rate_pct": round(win_rate, 1),
            "total_pnl_jpy": round(self.total_pnl, 1),
            "total_pnl_bp": round(self.total_pnl_bp, 2),
            "stats_1h": self.get_window_stats(1.0),
            "stats_24h": self.get_window_stats(24.0),
            "max_dd_jpy": round(self.max_dd, 1),
            "is_halted": self.is_halted,
            "entry_halted": self.entry_halted,
            "observe_only": self.observe_only,
            "be_armed": self.be_armed,
            "mfe_bp": round(self.mfe_bp, 2),
            "last_action": self.last_action,
            "last_reason": self.last_reason,
            "signal_fail_count": self.signal_fail_count,
            "quote_side": self.quote_side,
        }


class ApprovedStrategyArena:
    """承認済み12戦略 統合Dry-runアリーナランナー"""

    def __init__(
        self,
        symbol: str = "FX_BTC_JPY",
        poll_interval_sec: float = 5.0,
        report_interval_sec: float = 900.0,
    ):
        self.symbol = symbol
        self.poll_interval_sec = poll_interval_sec
        self.report_interval_sec = report_interval_sec

        self.start_time = time.time()
        self.last_report_time = self.start_time
        self.step_count = 0

        # 設定ロード
        self.config = self._load_config()

        # インフラ
        self.client = BitFlyerClient(enable_real_trading=False)
        self.notifier = QuantDiscordNotifier()
        self.adverse_tracker = AdverseExcursionTracker(save_dir=os.path.join(BASE_DIR, "data"))
        self.toxic_analyzer = ToxicFlowAnalyzer()
        self.spread_gate_tracker = SpreadGateTracker(save_path=os.path.join(BASE_DIR, "data", "spread_gate_stats.json"))

        # 戦略ローダー
        self.strategies: Dict[str, SingleStrategyState] = self._load_all_approved_strategies()

        # 履歴データの初期化 (1分足ローリングDataFrame)
        print("[Arena] 📊 直近ヒストリカルデータを準備中...")
        self.df_history = DataLoader.load_or_generate_data(
            source="bitflyer",
            symbol=self.symbol,
            timeframe="1m",
            limit=300,
        )
        if len(self.df_history) > 300:
            self.df_history = self.df_history.iloc[-300:].reset_index(drop=True)
        print(f"[Arena] ✅ ヒストリカルデータ準備完了: {len(self.df_history)} 本")

        if "timestamp" in self.df_history.columns:
            self.df_history["timestamp"] = self.df_history["timestamp"].map(to_utc_ts)

        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _avoidance_on(self) -> bool:
        path = os.path.join(BASE_DIR, "data", "adverse_device_state.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if time.time() - float(data.get("timestamp", 0)) < 5.0:
                    return bool(data.get("avoidance_on", False))
            except Exception:
                pass
        return False

    def _get_adverse_score(self) -> float:
        """Adverse Score (0-100) を adverse_score_state.json から取得"""
        if os.path.exists(ADVERSE_SCORE_STATE_PATH):
            try:
                with open(ADVERSE_SCORE_STATE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    ts = data.get("timestamp", 0)
                    if time.time() - ts < 60.0:
                        return float(data.get("adverse_score", 30.0))
            except Exception:
                pass
        return 30.0

    def _get_toxic_state(self) -> Dict[str, Any]:
        """Toxic Flow 状態を adverse_toxic_state.json から取得"""
        if os.path.exists(TOXIC_STATE_PATH):
            try:
                with open(TOXIC_STATE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _get_council_state(self) -> Dict[str, Any]:
        """4AGENT評議会合議ステートを configs/agents_council_state.json から取得"""
        if os.path.exists(COUNCIL_STATE_PATH):
            try:
                with open(COUNCIL_STATE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    ts = data.get("timestamp", 0) / 1000.0
                    if time.time() - ts < 60.0:
                        return data
            except Exception:
                pass
        return {}

    def _load_all_approved_strategies(self) -> Dict[str, SingleStrategyState]:
        """strategies/approved/*.py を動的ロード"""
        strat_dict = {}
        files = sorted(glob.glob(os.path.join(BASE_DIR, "strategies", "approved", "*.py")))
        for f in files:
            fname = os.path.basename(f)
            if fname.startswith("test_") or fname == "__init__.py":
                continue

            strat_id = fname.replace("_approved.py", "").replace(".py", "")
            try:
                spec = importlib.util.spec_from_file_location(f"approved_{strat_id}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "CustomStrategy"):
                    inst = mod.CustomStrategy()
                    state = SingleStrategyState(
                        strat_id=strat_id,
                        name=inst.name,
                        file_path=f,
                        instance=inst,
                        params=getattr(inst, "parameters", {}),
                    )
                    strat_dict[strat_id] = state
                else:
                    raise AttributeError("CustomStrategy が定義されていません")
            except Exception as e:
                print(f"[Arena] ⚠️ 戦略ロード失敗: {fname} ({e}) — strat_id は残し SIGNAL_ERROR で可視化します")
                stub = FailedStrategyStub(name=f"LOAD_FAILED:{strat_id}", error=f"{fname}: {e}")
                state = SingleStrategyState(
                    strat_id=strat_id,
                    name=stub.name,
                    file_path=f,
                    instance=stub,
                    params={},
                )
                state.last_action = "LOAD_ERROR"
                state.last_reason = f"{type(e).__name__}: {e}"
                strat_dict[strat_id] = state

        # CSR-515: 止血・クローン縮退フラグ適用
        imp = self.config.get("improvement_csr515") or {}
        halt_ids = set(imp.get("entry_halt") or [])
        obs_ids = set(imp.get("observe_only") or [])
        for sid, st in strat_dict.items():
            if sid in halt_ids:
                st.entry_halted = True
                st.last_reason = "CSR515_ENTRY_HALT"
            if sid in obs_ids:
                st.observe_only = True
                st.last_reason = "CSR515_OBSERVE_ONLY"

        print(f"[Arena] 🏛️ 合計 {len(strat_dict)} 個の承認済み戦略をロード完了しました。")
        print(f"[Arena] CSR-515 entry_halt={sorted(halt_ids)} observe_only={sorted(obs_ids)}")
        return strat_dict

    def run(self):
        print("=" * 80)
        print("  🏆 承認済み戦略 統合Dry-runアリーナ (Approved Strategy Arena)")
        print("=" * 80)
        print(f"対象銘柄          : {self.symbol}")
        print(f"参戦戦略数        : {len(self.strategies)} 戦略")
        print(f"サンプリング間隔  : {self.poll_interval_sec} 秒")
        print(f"Discord レポート  : {self.report_interval_sec / 60:.0f} 分ごと")
        print(f"運用ポリシー      : 🔒 自動調整完全禁止 (FROZEN: 各承認時パラメータ固定)")
        print("CSR-515           : 出血停止 + クローン縮退 + MM連続クォート + HardStop mid-bp")
        print("-" * 80)
        for s in self.strategies.values():
            tags = []
            if s.entry_halted:
                tags.append("HALT")
            if s.observe_only:
                tags.append("OBS")
            tag = f" [{','.join(tags)}]" if tags else ""
            print(f"  • [{s.strat_id:25}] {s.name:18}{tag} ({os.path.basename(s.file_path)})")
        print("-" * 80)

        # Discord 開始通知
        self._send_discord_start_report()

        # シグナルハンドラ
        def _sig_handler(sig, frame):
            print(f"\n[Arena] シグナル {sig} を受信しました。安全に終了します。")
            self._send_discord_summary_report(interrupted=True)
            sys.exit(0)

        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

        try:
            while True:
                self.step_count += 1
                self._execute_cycle()
                time.sleep(self.poll_interval_sec)

        except KeyboardInterrupt:
            print("\n[Arena] ユーザー中断を受信しました。")
            self._send_discord_summary_report(interrupted=True)
        except Exception as e:
            print(f"\n[Arena] 予期せぬエラーが発生しました: {e}")
            traceback.print_exc()
            self._log_to_file(f"[FATAL_ERROR] {e}\n{traceback.format_exc()}")
            self._send_discord_summary_report(interrupted=True)

    def _execute_cycle(self):
        """1サイクルの価格取得、バー更新、12戦略一括評価"""
        try:
            # 1. 最新 Ticker 取得
            ticker = self.client.get_ticker(self.symbol)
            if not ticker or "ltp" not in ticker:
                return

            ltp = float(ticker["ltp"])
            best_bid = float(ticker.get("best_bid", ltp - 1000))
            best_ask = float(ticker.get("best_ask", ltp + 1000))
            spread = best_ask - best_bid
            now = datetime.now(timezone.utc)

            # S1. Adverse Excursion のリアルタイム Tick 更新 (100ms, 500ms, 1s, 3s, 10s, 30s)
            self.adverse_tracker.on_tick(
                current_price=ltp,
                timestamp=time.time(),
                best_bid=best_bid,
                best_ask=best_ask,
            )

            # #spread-gate-validation 検証用 スプレッド記録
            self.spread_gate_tracker.record_tick_spread(spread, ltp)

            # 2. ローリング1分足の更新 (UTC 分で揃える。naive vs aware の分ズレで 5 秒足 doji を量産しない)
            self.df_history = upsert_minute_bar(
                self.df_history,
                ltp=ltp,
                now=now,
                volume=float(ticker.get("volume", 0.01)),
            )

            # 3. 各戦略のシグナル評価 & 仮想約定管理
            # 形成中の1分足は捨て、確定足の最終バーだけを見る（iloc[-1] が doji で ±1 が消えるのを防ぐ）
            cur_df = bars_for_signal_eval(self.df_history, now=now)
            exec_cfg = self.config.get("execution", {})
            take_profit = exec_cfg.get("take_profit_jpy", 25.0)
            stop_loss = exec_cfg.get("stop_loss_jpy", 25.0)
            hard_stop_bp = float(exec_cfg.get("hard_stop_bp", 5.0))
            take_profit_bp = float(exec_cfg.get("take_profit_bp", 2.5))
            max_hold = exec_cfg.get("max_hold_sec", 1800.0)
            order_size = exec_cfg.get("order_size_btc", 0.001)
            mm_cont = bool(exec_cfg.get("mm_continuous_quote", True))

            # Adverse Score & Toxic State & 4AGENT評議会合議ステート取得
            adverse_score = self._get_adverse_score()
            toxic_state = self._get_toxic_state()
            toxic_score = float(toxic_state.get("toxic_score", 0.0))
            toxic_side = toxic_state.get("side", "none")
            council_state = self._get_council_state()
            active_regime = council_state.get("active_regime", "normal")
            adverse_risk_level = council_state.get("adverse_risk_level", "SAFE")
            final_confidence = council_state.get("final_confidence", 1.0)
            max_spread_allowed = float(council_state.get("applied_weights", {}).get("max_spread_jpy", 2900.0))

            for s in self.strategies.values():
                if s.is_halted:
                    continue

                # 戦略タイプ分類
                target_str = (s.strat_id + " " + s.name).lower()
                is_mm = any(k in target_str for k in ["mm", "spread", "maker", "grid"])
                is_trend = any(k in target_str for k in ["trend", "ema", "order_flow"])
                is_reversion = any(k in target_str for k in ["rsi", "reversion", "mean"])

                # (A) 既存建玉のエグジット・利確・損切判定
                if s.position:
                    hold_time = time.time() - s.entry_time
                    # 決済価格 (成行決済: BUYはBid、SELLはAsk)
                    tip_exit = best_bid if s.position == "buy" else best_ask
                    exit_price = tip_exit
                    pnl_per_unit = (exit_price - s.entry_price) if s.position == "buy" else (s.entry_price - exit_price)
                    current_pnl = pnl_per_unit * s.position_size
                    entry_val = s.entry_price * s.position_size if s.entry_price > 0 else 12500.0
                    current_pnl_bp = (current_pnl / entry_val) * 10000.0 if entry_val > 0 else 0.0
                    # mid-bp（HardStop 正本 · Go unified_mm）
                    mid_pnl_unit = (ltp - s.entry_price) if s.position == "buy" else (s.entry_price - ltp)
                    mid_pnl_bp = (mid_pnl_unit / s.entry_price * 10000.0) if s.entry_price > 0 else 0.0

                    # MFE (Maximum Favorable Excursion) 更新
                    if current_pnl_bp > s.mfe_bp:
                        s.mfe_bp = current_pnl_bp

                    # BE5 建値防衛アーム判定: 含み益が +5.0bp 以上に到達したら発動準備
                    if current_pnl_bp >= 5.0 and not s.be_armed:
                        s.be_armed = True
                        self._log_to_file(f"[{s.strat_id}] 🛡️ BE5 Armed: 含み益 +{current_pnl_bp:.2f}bp 到達 (建値撤退防衛起動)")

                    close_reason = None
                    # 0. HardStop mid-bp failsafe（CSR-515 / Go HardStopBp=5）
                    if mid_pnl_bp <= -hard_stop_bp:
                        close_reason = f"HARD_STOP ({mid_pnl_bp:.2f}bp mid≤-{hard_stop_bp:.1f})"
                    # 1. BE5 建値防衛
                    elif s.be_armed and current_pnl_bp <= 0.2:
                        close_reason = f"BE5_PROFIT_DEFENSE (MFE: +{s.mfe_bp:.1f}bp -> {current_pnl_bp:+.2f}bp, 利確防衛)"
                    # 2. tip TP (bp 正本; jpy は後方互換)
                    elif current_pnl_bp >= take_profit_bp or current_pnl >= take_profit:
                        if is_mm:
                            exit_price = best_ask if s.position == "buy" else best_bid
                            current_pnl = (exit_price - s.entry_price if s.position == "buy" else s.entry_price - exit_price) * s.position_size
                        close_reason = f"TAKE_PROFIT (+{current_pnl_bp:.2f}bp tip)"
                    # 3. 旧 jpy stop（HardStop より浅い場合のみ補助）
                    elif current_pnl <= -stop_loss and mid_pnl_bp > -hard_stop_bp:
                        close_reason = f"STOP_LOSS (-¥{abs(current_pnl):.1f})"
                    # 4. タイムアウト
                    elif hold_time >= max_hold:
                        close_reason = f"TIMEOUT ({hold_time:.0f}s経過, {mid_pnl_bp:+.2f}bp mid)"
                    # 5. MM 連続クォート: LTPが反対側クォートに到達したら maker 解消
                    elif mm_cont and is_mm:
                        if s.position == "buy" and s.pending_ask and ltp >= s.pending_ask:
                            exit_price = float(s.pending_ask)
                            current_pnl = (exit_price - s.entry_price) * s.position_size
                            close_reason = f"MM_MAKER_CLOSE (ask@{exit_price:.0f})"
                        elif s.position == "sell" and s.pending_bid and ltp <= s.pending_bid:
                            exit_price = float(s.pending_bid)
                            current_pnl = (s.entry_price - exit_price) * s.position_size
                            close_reason = f"MM_MAKER_CLOSE (bid@{exit_price:.0f})"

                    if close_reason:
                        self._close_position(s, exit_price, current_pnl, close_reason)
                        s.pending_bid = s.pending_ask = s.quote_side = None
                        continue

                    # MM 連続クォート: 建玉中も両面を更新（fill-and-hold しない）
                    if mm_cont and is_mm:
                        s.pending_bid = best_bid
                        s.pending_ask = best_ask

                # (B) シグナル算出
                # 例外や列無しを sig=0 に黙殺すると、故障本が FLAT 0戦に見える。
                # 失敗時はエントリーも例外起因の SIGNAL_EXIT もせず、strat_id を残してログする。
                missing = missing_ohlcv_columns(cur_df)
                if missing:
                    self._record_signal_failure(s, f"missing_ohlcv:{missing}")
                    continue
                try:
                    df_sig = s.instance.generate_signals(cur_df)
                    sig, sig_err = read_last_signal(df_sig)
                    if sig_err:
                        self._record_signal_failure(s, sig_err)
                        continue
                except Exception as ex:
                    self._record_signal_failure(s, ex)
                    continue

                # (C) 新規エントリーまたはシグナル決済
                if s.position is None:
                    if sig in (1, -1):
                        # CSR-515: 出血停止 / クローン観測のみ — 新規禁止
                        if s.entry_halted or s.observe_only:
                            self.spread_gate_tracker.record_signal_decision(
                                s.strat_id, passed=False,
                                block_reason="entry_halt" if s.entry_halted else "observe_only",
                            )
                            # MM 連続クォート観測: クォートは出すが約定させない
                            if mm_cont and is_mm and s.observe_only:
                                s.pending_bid = best_bid
                                s.pending_ask = best_ask
                                s.quote_side = "buy" if sig == 1 else "sell"
                            continue

                        # 2. S2. Toxic Flow 危険例遮断 (Toxic Score >= 75 または方向別Toxic急変)
                        if toxic_score >= 75.0:
                            self.spread_gate_tracker.record_signal_decision(s.strat_id, passed=False, block_reason="toxic_flow_gate")
                            continue
                        if sig == 1 and (toxic_side == "buy" or adverse_risk_level == "CAUTION_BUY"):
                            self.spread_gate_tracker.record_signal_decision(s.strat_id, passed=False, block_reason="toxic_flow_gate")
                            continue
                        if sig == -1 and (toxic_side == "sell" or adverse_risk_level == "CAUTION_SELL"):
                            self.spread_gate_tracker.record_signal_decision(s.strat_id, passed=False, block_reason="toxic_flow_gate")
                            continue

                        # 3. DuckDB 最適スプレッドフィルター (超過時は見送り)
                        if spread > min(2500.0, max_spread_allowed):
                            self.spread_gate_tracker.record_signal_decision(s.strat_id, passed=False, block_reason="spread_gate")
                            continue

                        # 4. レジーム・戦略タイプ整合性フィルター (AGENT合議知見)
                        if active_regime == "trend" and is_reversion:
                            self.spread_gate_tracker.record_signal_decision(s.strat_id, passed=False, block_reason="regime_gate")
                            continue
                        if active_regime == "range" and is_trend:
                            self.spread_gate_tracker.record_signal_decision(s.strat_id, passed=False, block_reason="regime_gate")
                            continue

                        # 5. 確信度フィルター
                        if final_confidence < 0.45:
                            self.spread_gate_tracker.record_signal_decision(s.strat_id, passed=False, block_reason="confidence_gate")
                            continue

                        # MM 連続クォート: 即約定せず pending → LTP交差で maker fill
                        if mm_cont and is_mm:
                            s.pending_bid = best_bid
                            s.pending_ask = best_ask
                            s.quote_side = "buy" if sig == 1 else "sell"
                            self.spread_gate_tracker.record_signal_decision(s.strat_id, passed=True)
                            # LTP がクォートに到達したら約定
                            if sig == 1 and ltp <= best_bid:
                                self._open_position(
                                    s, "buy", best_bid, order_size,
                                    "SIGNAL_BUY(MAKER_CONT)", spread=spread, adverse_score=adverse_score,
                                )
                            elif sig == -1 and ltp >= best_ask:
                                self._open_position(
                                    s, "sell", best_ask, order_size,
                                    "SIGNAL_SELL(MAKER_CONT)", spread=spread, adverse_score=adverse_score,
                                )
                            continue

                        # 非MM: 従来の即時エントリー
                        self.spread_gate_tracker.record_signal_decision(s.strat_id, passed=True)
                        if sig == 1:
                            entry_p = best_ask
                            self._open_position(s, "buy", entry_p, order_size, "SIGNAL_BUY(TAKER)", spread=spread, adverse_score=adverse_score)
                        elif sig == -1:
                            entry_p = best_bid
                            self._open_position(s, "sell", entry_p, order_size, "SIGNAL_SELL(TAKER)", spread=spread, adverse_score=adverse_score)
                    elif mm_cont and is_mm and s.quote_side and s.pending_bid and s.pending_ask:
                        # シグナル消灯後も pending が残っていれば LTP 交差で fill
                        if s.quote_side == "buy" and ltp <= s.pending_bid:
                            self._open_position(
                                s, "buy", float(s.pending_bid), order_size,
                                "QUOTE_FILL(MAKER_CONT)", spread=spread, adverse_score=adverse_score,
                            )
                        elif s.quote_side == "sell" and ltp >= s.pending_ask:
                            self._open_position(
                                s, "sell", float(s.pending_ask), order_size,
                                "QUOTE_FILL(MAKER_CONT)", spread=spread, adverse_score=adverse_score,
                            )
                else:
                    # ドテンまたは手仕舞いシグナル（非MM / MMでもシグナル反転）
                    hold_time = time.time() - s.entry_time
                    exit_price = best_bid if s.position == "buy" else best_ask
                    pnl = ((exit_price - s.entry_price) if s.position == "buy" else (s.entry_price - exit_price)) * s.position_size

                    if sig == 0 and not (mm_cont and is_mm):
                        # ノイズ即時損切り防止ガード（非MMのみ）
                        if hold_time >= 15.0 or pnl > 0:
                            self._close_position(s, exit_price, pnl, "SIGNAL_EXIT")
                    elif (s.position == "buy" and sig == -1) or (s.position == "sell" and sig == 1):
                        self._close_position(s, exit_price, pnl, "SIGNAL_REVERSE")
                        s.pending_bid = s.pending_ask = s.quote_side = None

            # 4. コンソール進捗表示 (上位3戦略の表示)
            sorted_strats = sorted(self.strategies.values(), key=lambda x: x.total_pnl, reverse=True)
            top1 = sorted_strats[0]
            top2 = sorted_strats[1] if len(sorted_strats) > 1 else top1
            top3 = sorted_strats[2] if len(sorted_strats) > 2 else top1

            gate_stats = self.spread_gate_tracker.get_window_stats(3600.0)
            gate_rate = gate_stats["pass_rate_pct"]
            now_str = now.strftime("%H:%M:%S")
            print(
                f" [{now_str}] | LTP: ¥{ltp:10,.0f} | Spr: ¥{spread:5,.0f} | Adv: {adverse_score:4.1f} | Tox: {toxic_score:4.1f} | Gate: {gate_rate:4.1f}% | "
                f"🥇 {top1.strat_id[:10]}: {top1.total_pnl_bp:+.1f}bp (¥{top1.total_pnl:+,.0f}) | "
                f"🥈 {top2.strat_id[:10]}: {top2.total_pnl_bp:+.1f}bp (¥{top2.total_pnl:+,.0f}) | "
                f"🥉 {top3.strat_id[:10]}: {top3.total_pnl_bp:+.1f}bp (¥{top3.total_pnl:+,.0f})",
                flush=True
            )

            # 5. 状態永続化 (アトミック)
            self._persist_state(ltp, spread, sorted_strats)

            # 6. 定期 Discord レポート (15分ごと)
            if time.time() - self.last_report_time >= self.report_interval_sec:
                self._send_discord_summary_report()
                self.last_report_time = time.time()

        except Exception as e:
            self._log_to_file(f"[CYCLE_ERROR] {e}\n{traceback.format_exc()}")
            print(f"[Arena] CYCLE_ERROR: {e}", flush=True)

    def _record_signal_failure(self, s: SingleStrategyState, err: Any):
        """generate_signals 失敗をログし、ランキング上に strat_id を残す。黙って sig=0 にはしない。"""
        if isinstance(err, BaseException):
            msg = f"{type(err).__name__}: {err}"
            tb = traceback.format_exc()
        else:
            msg = str(err)
            tb = ""
        s.signal_fail_count += 1
        s.last_action = "SIGNAL_ERROR"
        s.last_reason = msg[:240]
        # 5秒周期なので初回と以降おおよそ1分ごとに残す
        if s.signal_fail_count == 1 or s.signal_fail_count % 12 == 0:
            line = f"[{s.strat_id}] SIGNAL_ERROR x{s.signal_fail_count}: {msg}"
            print(f"[Arena] {line}", flush=True)
            self._log_to_file(f"{line}\n{tb}".rstrip())

    def _open_position(self, s: SingleStrategyState, side: str, price: float, size: float, reason: str, spread: float = 2000.0, adverse_score: float = 30.0):
        s.position = side
        s.entry_price = price
        s.position_size = size
        s.entry_time = time.time()
        s.be_armed = False
        s.mfe_bp = 0.0
        s.total_trades += 1
        s.last_action = f"OPEN_{side.upper()}"
        s.last_reason = reason

        # S1. Adverse Excursion 追跡開始 (100ms, 500ms, 1s, 3s, 10s, 30s)
        trade_id = f"{s.strat_id}_{int(s.entry_time * 1000)}"
        s.current_trade_id = trade_id
        theoretical_spread_bp = (spread / price) * 10000.0 if price > 0 else 2.0
        self.adverse_tracker.track_entry(
            trade_id=trade_id,
            side=side,
            entry_price=price,
            entry_time=s.entry_time,
            strategy_name=s.name,  # UMM, SpreadCaptureMM, MicroTrend, GridMM 等
            meta={
                "strat_id": s.strat_id,
                "strategy": s.name,
                "theoretical_spread_bp": theoretical_spread_bp,
                "adverse_score": adverse_score,
                "reason": reason,
            }
        )
        self._log_to_file(f"[{s.strat_id}] 📥 新規約定: {side.upper()} @ ¥{price:,.0f} ({reason}) | Adv:{adverse_score:.1f}")

    def _close_position(self, s: SingleStrategyState, price: float, pnl: float, reason: str):
        s.total_pnl += pnl
        order_val = s.position_size * price if price > 0 else 12500.0
        pnl_bp = (pnl / order_val) * 10000.0 if order_val > 0 else 0.0
        s.total_pnl_bp += pnl_bp

        now_ts = time.time()
        is_win = pnl > 0
        s.trades_history.append({
            "ts": now_ts,
            "pnl_jpy": pnl,
            "pnl_bp": pnl_bp,
            "is_win": is_win,
        })

        if pnl > 0:
            s.win_trades += 1
            s.consecutive_losses = 0
        else:
            s.loss_trades += 1
            s.consecutive_losses += 1

        # Max Drawdown
        if s.total_pnl > s.peak_pnl:
            s.peak_pnl = s.total_pnl
        dd = s.peak_pnl - s.total_pnl
        if dd > s.max_dd:
            s.max_dd = dd

        # S3 & C1. Adverse Excursion 完了 & Capture Rate 計算 & 永続化
        if s.current_trade_id:
            self.adverse_tracker.on_close(
                trade_id=s.current_trade_id,
                exit_price=price,
                pnl_bp=pnl_bp,
                exit_reason=reason,
            )
            s.current_trade_id = None

        s.last_action = f"CLOSE_{s.position.upper()}"
        s.last_reason = reason
        self._log_to_file(f"[{s.strat_id}] 📤 決済: {s.position.upper()} @ ¥{price:,.0f} | PnL: {pnl_bp:+.2f}bp (¥{pnl:+,.1f}) ({reason})")

        s.position = None
        s.entry_price = 0.0
        s.entry_time = 0.0
        s.be_armed = False
        s.mfe_bp = 0.0

    def _persist_state(self, ltp: float, spread: float, sorted_strats: List[SingleStrategyState]):
        try:
            state = {
                "timestamp": int(time.time() * 1000),
                "ltp": ltp,
                "spread_jpy": spread,
                "total_strategies": len(self.strategies),
                "ranking": [s.to_dict() for s in sorted_strats],
            }
            tmp = f"{STATE_PATH}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(tmp, STATE_PATH)
        except Exception:
            pass

    def _log_to_file(self, message: str):
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{now_str}] {message}\n")
        except Exception:
            pass

    def _send_discord_start_report(self):
        desc = (
            f"過去に検証・承認された **12個のアルゴリズム** を一堂に会し、\n"
            f"**「統合Dry-runアリーナ（並行ベンチマーク）」** を開始しました。\n\n"
            f"🔒 **運用方針**: **自動調整完全禁止 (FROZEN)**\n"
            f"（各戦略承認時の固定パラメータで稼働し、現環境での優劣をリアルタイム観測）"
        )
        embed = {
            "title": "🏆 【承認済み12戦略 統合Dry-runアリーナ 開幕】",
            "description": desc,
            "color": 0x9B59B6,
            "fields": [
                {
                    "name": "🏟️ 参戦アルゴリズム一覧",
                    "value": "\n".join([f"• `{s.strat_id}` ({s.name})" for s in list(self.strategies.values())[:8]]) + "\n• ...他 4戦略",
                    "inline": False,
                },
                {
                    "name": "⚙️ 観測レギュレーション",
                    "value": "• 取引サイズ: `0.001 BTC` | 気配値・スプレッド摩擦完全適用\n• 利確: `+¥25` / 損切: `-¥25` / タイムアウト: `30分`",
                    "inline": False,
                },
            ],
            "footer": {"text": "🏛️ Antigravity Approved Strategy Arena Sentinel"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.notifier.post_dryrun_multicast({"embeds": [embed]})

    def _send_discord_summary_report(self, interrupted: bool = False):
        sorted_strats = sorted(self.strategies.values(), key=lambda x: x.total_pnl_bp, reverse=True)
        total_arena_pnl = sum(s.total_pnl for s in sorted_strats)
        total_arena_bp = sum(s.total_pnl_bp for s in sorted_strats)
        total_arena_trades = sum(s.total_trades for s in sorted_strats)

        elapsed_h = (time.time() - self.start_time) / 3600.0
        title = "⚠️ 【承認済み12戦略 アリーナ 中断レポート】" if interrupted else "🏆 【承認済み12戦略 アリーナ 定期成績ランキング (bp表示)】"

        # ランキングテキスト作成 (1h & 24h 累積 bp 表示)
        rank_lines = []
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟", "1️⃣1️⃣", "1️⃣2️⃣"]
        for i, s in enumerate(sorted_strats):
            medal = medals[i] if i < len(medals) else f"{i+1}."
            s_1h = s.get_window_stats(1.0)
            s_24h = s.get_window_stats(24.0)
            pos = f"[{s.position.upper()}]" if s.position else "[FLAT]"
            rank_lines.append(
                f"{medal} **`{s.strat_id[:14]}`** ({s.name}) `{pos}`\n"
                f"   • 1h: **`{s_1h['pnl_bp']:+.2f} bp`** ({s_1h['total_trades']}戦/{s_1h['win_rate_pct']:.0f}% / ¥{s_1h['pnl_jpy']:+,.0f})\n"
                f"   • 24h: **`{s_24h['pnl_bp']:+.2f} bp`** ({s_24h['total_trades']}戦/{s_24h['win_rate_pct']:.0f}% / ¥{s_24h['pnl_jpy']:+,.0f})"
            )

        embed = {
            "title": title,
            "description": (
                f"観測経過時間: **{elapsed_h:.2f} 時間** | 参戦アルゴ: **{len(sorted_strats)} 戦略**\n"
                f"合算損益: **`{total_arena_bp:+.2f} bp`** (`¥{total_arena_pnl:+,.1f}`, 総取引数: {total_arena_trades}回)\n"
                f"🔒 運用方針: **自動調整完全禁止 (FROZEN)**"
            ),
            "color": 0x2ECC71 if total_arena_bp >= 0 else 0xE74C3C,
            "fields": [
                {
                    "name": "📊 リアルタイム成績順位表 (TOP 6 / bp順)",
                    "value": "\n".join(rank_lines[:6]),
                    "inline": False,
                },
                {
                    "name": "📊 下位グループ (7〜12位)",
                    "value": "\n".join(rank_lines[6:]),
                    "inline": False,
                },
            ],
            "footer": {"text": "🏛️ Antigravity Approved Strategy Arena Sentinel"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.notifier.post_dryrun_multicast({"embeds": [embed]})


def main():
    parser = argparse.ArgumentParser(description="Approved Strategy Arena Runner")
    parser.add_argument("--symbol", default="FX_BTC_JPY", help="対象銘柄")
    parser.add_argument("--interval", type=float, default=5.0, help="観測間隔 (秒)")
    parser.add_argument("--report-interval", type=float, default=3600.0, help="Discordレポート間隔 (秒, デフォルト1時間)")
    args = parser.parse_args()

    arena = ApprovedStrategyArena(
        symbol=args.symbol,
        poll_interval_sec=args.interval,
        report_interval_sec=args.report_interval,
    )
    arena.run()


if __name__ == "__main__":
    main()
