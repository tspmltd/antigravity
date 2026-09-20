"""
24-Hour UMM & TF2BP Dual Strategy Dry-Run Observation Runner
============================================================
GIT正本（FIX.me CSR-504 / CSR-499）から導入した:
  1. UMM (Unified Market Making v1)
  2. TF2BP (2bp Micro Trend Order Flow v1)
の2戦略を新規にDry-runへ配備し、24時間連続で仮想約定・損益・逆選択耐性を観測する。

⚠️ 【重要運用規則】
  - 自動調整・再学習は完全禁止 (auto_tune_allowed = False)。
  - パラメータ調整はユーザーからの明示的な指示のみ受け付ける。
"""
import os
import sys
import time
import json
import argparse
import signal
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

from .event_bus import EventBus
from .parquet_logger import ParquetBatchLogger
from .schema import OrderbookMicroSnapshot
from .ingestion import MarketDataIngestion
from .quant_discord_notifier import QuantDiscordNotifier
from .agents.adverse_agent import AdverseResearchAgent
from ..strategies.umm_strategy import UMMStrategy
from ..strategies.tf2bp_strategy import TF2BPStrategy
from ..strategies.tf2bp_peg_v2_strategy import TF2BP_PEG_v2_Strategy

JST = timezone(timedelta(hours=9))
BASE_DIR = "/home/azureuser/antigravity"
CONFIG_PATH = os.path.join(BASE_DIR, "configs", "umm_tf2bp_config.json")
STATE_PATH = os.path.join(BASE_DIR, "data", "dryrun_umm_tf2bp_state.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "dryrun_umm_tf2bp_24h.log")


class DryRunObservation24h:
    def __init__(
        self,
        symbol: str = "FX_BTC_JPY",
        config_path: str = CONFIG_PATH,
        duration_hours: float = 24.0,
        interval_sec: float = 2.0,
        discord_report_sec: float = 3600.0,  # 1時間ごとにDiscordレポート
    ):
        self.symbol = symbol
        self.config_path = config_path
        self.duration_sec = duration_hours * 3600.0
        self.interval_sec = interval_sec
        self.discord_report_sec = discord_report_sec

        self.start_time = time.time()
        self.last_report_time = self.start_time
        self.step_count = 0

        # 設定のロード
        self.config_data = self._load_config()

        # 戦略インスタンス初期化 (固定パラメータ)
        self.umm = UMMStrategy(self.config_data.get("umm_config", {}))
        self.tf2bp = TF2BPStrategy(self.config_data.get("tf2bp_config", {}))
        self.tf2bp_v2 = TF2BP_PEG_v2_Strategy(self.config_data.get("tf2bp_config", {}))

        # インフラ
        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=30.0, batch_size=100)
        self.notifier = QuantDiscordNotifier()
        self.adverse_agent = AdverseResearchAgent(self.bus)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code=self.symbol)

        # 仮想口座
        self.umm_account = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        self.tf2bp_account = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        self.tf2bp_v2_account = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}

        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("policy", {}).get("auto_tune_allowed", True):
                    print("[WARNING] auto_tune_allowed is True in config. Enforcing False per user instruction.")
                    data["policy"]["auto_tune_allowed"] = False
                return data
            except Exception as e:
                print(f"[ERROR] Config load failed: {e}")
        return {}

    def run(self):
        print("=" * 80)
        print("  ⏳ UMM & TF2BP 24時間 連続Dry-run 観測ランナー (24h Observation Runner)")
        print("=" * 80)
        print(f"対象銘柄          : {self.symbol}")
        print(f"観測期間          : {self.duration_sec / 3600:.1f} 時間 (86,400 秒)")
        print(f"サンプリング間隔  : {self.interval_sec} 秒")
        print(f"Discord レポート  : {self.discord_report_sec / 60:.0f} 分ごと")
        print(f"パラメータ制御    : 🔒 自動調整禁止 (完全手動指示・固定パラメータ)")
        print(f"UMM パラメータ    : {self.umm.params}")
        print(f"TF2BP パラメータ  : {self.tf2bp.params}")
        print("-" * 80)
        print(" [経過時間] | Mid価格 (円) | スプレッド | Adverse | UMM状態 (PnL)      | TF2BP状態 (PnL)")
        print("-" * 80)

        # 開始通知をDiscordへ
        self._send_start_discord_notification()

        def _sig_handler(sig, frame):
            print(f"\n[24hRunner] シグナル {sig} を受信しました。安全に終了処理を行います。")
            self._log_to_file(f"[SIGNAL_RECEIVED] Signal {sig}")
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # SIGHUP は無視して常駐継続

        try:
            while True:
                now = time.time()
                elapsed_sec = now - self.start_time

                if elapsed_sec >= self.duration_sec:
                    print(f"\n[24hRunner] 🎉 指定の24時間観察が完了しました。")
                    self._send_discord_summary_report(final=True)
                    break

                self.step_count += 1
                snap = self.ingestion.poll_once()

                if snap:
                    # 1. Adverse 判定の取得
                    adv_state = self.adverse_agent.latest_state
                    adv_score = adv_state.get("adverse_score", 0.0)
                    cancel_rec = adv_state.get("cancel_recommendation", False)

                    # 2. UMM 戦略評価
                    umm_res = self.umm.on_tick(
                        mid_price=snap.mid_price,
                        best_bid=snap.best_bid,
                        best_ask=snap.best_ask,
                        imbalance=snap.imbalance,
                        adverse_score=adv_score,
                        cancel_recommendation=cancel_rec,
                    )
                    self._process_strategy_action("UMM", self.umm, umm_res, snap)

                    # 3. TF2BP 戦略評価 (Baseline)
                    tf2bp_res = self.tf2bp.on_tick(
                        mid_price=snap.mid_price,
                        best_bid=snap.best_bid,
                        best_ask=snap.best_ask,
                        taker_vol_bid=snap.taker_volume_bid,
                        taker_vol_ask=snap.taker_volume_ask,
                        adverse_score=adv_score,
                        cancel_recommendation=cancel_rec,
                    )
                    self._process_strategy_action("TF2BP", self.tf2bp, tf2bp_res, snap)

                    # 4. TF2BP_PEG_v2 戦略評価 (OBSERVATION: Model 3+1 検証レーン)
                    tf2bp_v2_res = self.tf2bp_v2.on_tick(
                        mid_price=snap.mid_price,
                        best_bid=snap.best_bid,
                        best_ask=snap.best_ask,
                        taker_vol_bid=snap.taker_volume_bid,
                        taker_vol_ask=snap.taker_volume_ask,
                        ask_depth_1=snap.ask_depth_1,
                        bid_depth_1=snap.bid_depth_1,
                        taker_aggressiveness=snap.taker_aggressiveness,
                        cancel_rate=snap.cancel_rate,
                        refill_rate=snap.refill_rate,
                        adverse_score=adv_score,
                        cancel_recommendation=cancel_rec,
                    )
                    self._process_strategy_action("TF2BP_PEG_v2", self.tf2bp_v2, tf2bp_v2_res, snap)

                    # 5. コンソール表示
                    elapsed_h = int(elapsed_sec // 3600)
                    elapsed_m = int((elapsed_sec % 3600) // 60)
                    elapsed_s = int(elapsed_sec % 60)
                    time_str = f"{elapsed_h:02d}:{elapsed_m:02d}:{elapsed_s:02d}"

                    spread_val = snap.best_ask - snap.best_bid
                    adv_str = f"{adv_state.get('adverse_side', 'none')[:1].upper()}:{adv_score:.2f}"

                    umm_pos_str = f"{self.umm.position_side.upper() if self.umm.position_side else 'FLAT'} ({self.umm.total_pnl_bp:+.1f}bp)"
                    tf_pos_str = f"{self.tf2bp.position_side.upper() if self.tf2bp.position_side else 'FLAT'} ({self.tf2bp.total_pnl_bp:+.1f}bp)"
                    tf_v2_pos_str = f"{self.tf2bp_v2.position_side.upper() if self.tf2bp_v2.position_side else 'FLAT'} ({self.tf2bp_v2.total_pnl_bp:+.1f}bp)"

                    print(
                        f" [{time_str}] | {snap.mid_price:12,.0f} | ¥{spread_val:5,.0f} | {adv_str:7} | "
                        f"{umm_pos_str:15} | {tf_pos_str:15} | PEG_v2:{tf_v2_pos_str}",
                        flush=True
                    )

                    # 状態ファイル保存 (アトミック)
                    self._persist_state(snap, elapsed_sec)

                    # 定期 Discord レポート
                    if now - self.last_report_time >= self.discord_report_sec:
                        self._send_discord_summary_report(final=False)
                        self.last_report_time = now

                time.sleep(self.interval_sec)

        except KeyboardInterrupt:
            print("\n[24hRunner] ユーザー中断を受信しました。")
            self._send_discord_summary_report(final=True, interrupted=True)
        except Exception as e:
            print(f"\n[24hRunner] 予期せぬエラーが発生しました: {e}")
            traceback.print_exc()
            self._log_to_file(f"[FATAL_ERROR] {e}\n{traceback.format_exc()}")
            self._send_discord_summary_report(final=True, interrupted=True)
        finally:
            self.logger.stop()
            print("✅ 24時間観察ログおよび Parquet 保存を完了しました。")

    def _process_strategy_action(self, name: str, strat: Any, res: Dict[str, Any], snap: OrderbookMicroSnapshot):
        action = res.get("action", "hold")
        if name == "UMM":
            acc = self.umm_account
        elif name == "TF2BP":
            acc = self.tf2bp_account
        elif name == "TF2BP_PEG_v2":
            acc = self.tf2bp_v2_account
        else:
            acc = self.tf2bp_account

        if action in ("buy", "sell"):
            if not strat.position_side:
                fill_price = snap.best_ask if action == "buy" else snap.best_bid
                strat.record_trade(action, fill_price, 0.0)
                acc["trades"] += 1
                log_line = f"[{name}] 📥 新規エントリー: {action.upper()} @ ¥{fill_price:,.0f} ({res.get('reason')})"
                self._log_to_file(log_line)
                # S1. Adverse Excursion (AE) 追跡開始
                self.adverse_agent.track_entry(
                    trade_id=f"{name}_{acc['trades']}",
                    side=action,
                    entry_price=fill_price,
                    entry_time=time.time(),
                    strategy_name=name,
                    meta={"reason": res.get("reason"), "spread": snap.best_ask - snap.best_bid},
                )

        elif action == "post_peg":
            p_side = res.get("side", "")
            p_price = res.get("price", 0.0)
            log_line = f"[{name}] 📝 PEG_v2 指値提示: {p_side.upper()} @ ¥{p_price:,.0f} ({res.get('reason')})"
            self._log_to_file(log_line)

        elif action == "fill":
            fill_price = res.get("fill_price", snap.mid_price)
            p_side = res.get("side", strat.position_side or "UNKNOWN")
            acc["trades"] += 1
            log_line = f"[{name}] 📥 PEG_v2 指値約定: {p_side.upper()} @ ¥{fill_price:,.0f} ({res.get('reason')})"
            self._log_to_file(log_line)
            # S1. Adverse Excursion (AE) 追跡開始
            self.adverse_agent.track_entry(
                trade_id=f"{name}_{acc['trades']}",
                side=p_side,
                entry_price=fill_price,
                entry_time=time.time(),
                strategy_name=name,
                meta={"reason": res.get("reason"), "fill_type": "maker_peg"},
            )

        elif action == "cancel_pending":
            log_line = f"[{name}] 🚫 PEG_v2 待機取消: ({res.get('reason')})"
            self._log_to_file(log_line)

        elif action in ("exit", "cancel"):
            if strat.position_side:
                fill_price = snap.best_bid if strat.position_side == "buy" else snap.best_ask
                pnl = res.get("expected_pnl", res.get("pnl", 0.0))
                strat.record_trade(action, fill_price, pnl, mid_price=snap.mid_price)
                acc["pnl"] += pnl
                if pnl > 0:
                    acc["wins"] += 1
                else:
                    acc["losses"] += 1
                order_val = strat.params["order_size_btc"] * snap.mid_price if snap.mid_price > 0 else 12500.0
                pnl_bp = (pnl / order_val) * 10000.0 if order_val > 0 else 0.0
                log_line = f"[{name}] 📤 エグジット/キャンセル ({action}): PnL: {pnl:+.1f}円 ({pnl_bp:+.2f}bp) @ ¥{fill_price:,.0f} ({res.get('reason')})"
                self._log_to_file(log_line)

    def _persist_state(self, snap: OrderbookMicroSnapshot, elapsed_sec: float):
        try:
            umm_1h = self.umm.get_window_stats(1.0)
            umm_24h = self.umm.get_window_stats(24.0)
            tf_1h = self.tf2bp.get_window_stats(1.0)
            tf_24h = self.tf2bp.get_window_stats(24.0)
            tf_v2_1h = self.tf2bp_v2.get_window_stats(1.0)
            tf_v2_24h = self.tf2bp_v2.get_window_stats(24.0)

            state = {
                "timestamp": int(time.time() * 1000),
                "elapsed_hours": round(elapsed_sec / 3600.0, 2),
                "mid_price": snap.mid_price,
                "best_bid": snap.best_bid,
                "best_ask": snap.best_ask,
                "spread_jpy": snap.best_ask - snap.best_bid,
                "umm": {
                    "position": self.umm.position_side or "FLAT",
                    "inventory_btc": round(self.umm.inventory_btc, 4),
                    "total_trades": self.umm.total_trades,
                    "win_trades": self.umm.win_trades,
                    "total_pnl": round(self.umm.total_pnl, 1),
                    "total_pnl_bp": round(self.umm.total_pnl_bp, 2),
                    "stats_1h": umm_1h,
                    "stats_24h": umm_24h,
                    "params": self.umm.params,
                    "frozen": self.umm.frozen_mode,
                    "user_directive": self.umm.last_user_directive,
                },
                "tf2bp": {
                    "position": self.tf2bp.position_side or "FLAT",
                    "total_trades": self.tf2bp.total_trades,
                    "win_trades": self.tf2bp.win_trades,
                    "total_pnl": round(self.tf2bp.total_pnl, 1),
                    "total_pnl_bp": round(self.tf2bp.total_pnl_bp, 2),
                    "stats_1h": tf_1h,
                    "stats_24h": tf_24h,
                    "params": self.tf2bp.params,
                    "frozen": self.tf2bp.frozen_mode,
                    "user_directive": self.tf2bp.last_user_directive,
                },
                "tf2bp_peg_v2": {
                    "position": self.tf2bp_v2.position_side or ("PENDING" if self.tf2bp_v2.pending_order else "FLAT"),
                    "pending_order": self.tf2bp_v2.pending_order,
                    "total_trades": self.tf2bp_v2.total_trades,
                    "win_trades": self.tf2bp_v2.win_trades,
                    "total_pnl": round(self.tf2bp_v2.total_pnl, 1),
                    "total_pnl_bp": round(self.tf2bp_v2.total_pnl_bp, 2),
                    "stats_1h": tf_v2_1h,
                    "stats_24h": tf_v2_24h,
                    "params": self.tf2bp_v2.params,
                    "frozen": self.tf2bp_v2.frozen_mode,
                    "user_directive": self.tf2bp_v2.last_user_directive,
                },
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

    def _send_start_discord_notification(self):
        embed = {
            "title": "🚀 【UMM ＆ TF2BP ＋ TF2BP_PEG_v2 24時間 連続Dry-run 観察開始】",
            "description": (
                f"GIT正本（CSR-504 / CSR-499）から UMM および TF2BP を導入し、\n"
                f"さらにバックテスト最高改善 (+54bp) の **PEG_v2 (Model 3+1)** を搭載した\n"
                f"**`TF2BP_PEG_v2` を OBSERVATION (並行観測) レーン** として稼働開始しました。\n\n"
                f"🔒 **パラメータ運用方針**: **自動調整禁止 (FROZEN / OBSERVATION)**\n"
                f"（DuckDB / Evolver による自動変更は遮断、完全手動・固定パラメータ）"
            ),
            "color": 0x3498DB,
            "fields": [
                {
                    "name": "① UMM (Unified Market Making v1 - CSR-504)",
                    "value": f"• スプレッド下限: `{self.umm.params['spread_min_bp']} bp` | 在庫スキュー感度: `{self.umm.params['gamma_high']}`\n• 利確: `+¥{self.umm.params['take_profit_jpy']}` / 損切: `-¥{self.umm.params['stop_loss_jpy']}` / ロット: `{self.umm.params['order_size_btc']} BTC`",
                    "inline": False,
                },
                {
                    "name": "② TF2BP (2bp Micro Trend - CSR-499 Baseline: FROZEN)",
                    "value": f"• 初動閾値: `{self.tf2bp.params['micro_mom_bp']} bp` | 目標: `{self.tf2bp.params['target_bp']} bp` | トレール: `{self.tf2bp.params['trail_stop_bp']} bp`\n• 逆ノイズ上限: `{self.tf2bp.params['reverse_noise_max']*100:.0f}%` | ロット: `{self.tf2bp.params['order_size_btc']} BTC`",
                    "inline": False,
                },
                {
                    "name": "🔬 ③ TF2BP_PEG_v2 (Model 3+1 観測レーン: OBSERVATION)",
                    "value": (
                        f"• ロジック: **Model 3 (EffectiveReach) + Model 1 (DynamicRatio)**\n"
                        f"• 指値比率: 動的 `0.910 〜 0.985` (板厚連動 ＋ テイカー攻撃性ブースト)\n"
                        f"• エグジット: **BE5 建値防衛** (MFE $\\ge$ 5bp到達で利益ゼロ反落時即座に微小利確撤退)\n"
                        f"• 運用方針: `🔒 OBSERVATION (Baseline と同一板並行 A/B テスト)`"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "🏛️ Antigravity 24h Multi-Strategy Dry-run Sentinel"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.notifier.post_dryrun_multicast({"embeds": [embed]})

    def _send_discord_summary_report(self, final: bool = False, interrupted: bool = False):
        elapsed_sec = time.time() - self.start_time
        elapsed_hours = elapsed_sec / 3600.0

        umm_1h = self.umm.get_window_stats(1.0)
        umm_24h = self.umm.get_window_stats(24.0)
        tf_1h = self.tf2bp.get_window_stats(1.0)
        tf_24h = self.tf2bp.get_window_stats(24.0)
        v2_1h = self.tf2bp_v2.get_window_stats(1.0)
        v2_24h = self.tf2bp_v2.get_window_stats(24.0)

        diff_1h_bp = v2_1h["pnl_bp"] - tf_1h["pnl_bp"]
        diff_24h_bp = v2_24h["pnl_bp"] - tf_24h["pnl_bp"]

        status_title = "🏁 【UMM ＆ TF2BP 24時間観察 完了総括レポート】" if final else "📊 【UMM ＆ TF2BP 1時間定期レポート】"
        if interrupted:
            status_title = "⚠️ 【UMM ＆ TF2BP 24時間観察 中断レポート】"

        embed = {
            "title": status_title,
            "description": f"観察経過時間: **{elapsed_hours:.2f} / 24.0 時間** | 対象銘柄: `{self.symbol}`\n🔒 **運用方針**: **自動調整完全禁止 (FROZEN / OBSERVATION)**",
            "color": 0x2ECC71 if (self.umm.total_pnl + self.tf2bp.total_pnl) >= 0 else 0xE67E22,
            "fields": [
                {
                    "name": "① UMM (Unified Market Making CSR-504)",
                    "value": (
                        f"• 1h: **`{umm_1h['pnl_bp']:+.2f} bp`** ({umm_1h['total_trades']}戦/{umm_1h['win_rate_pct']:.0f}% / ¥{umm_1h['pnl_jpy']:+,.0f})\n"
                        f"• 24h: **`{umm_24h['pnl_bp']:+.2f} bp`** ({umm_24h['total_trades']}戦/{umm_24h['win_rate_pct']:.0f}% / ¥{umm_24h['pnl_jpy']:+,.0f})\n"
                        f"• 建玉: `{self.umm.position_side or 'FLAT'}` (在庫: `{self.umm.inventory_btc:+.4f} BTC`)"
                    ),
                    "inline": False,
                },
                {
                    "name": "② TF2BP (2bp Micro Trend CSR-499 Baseline)",
                    "value": (
                        f"• 1h: **`{tf_1h['pnl_bp']:+.2f} bp`** ({tf_1h['total_trades']}戦/{tf_1h['win_rate_pct']:.0f}% / ¥{tf_1h['pnl_jpy']:+,.0f})\n"
                        f"• 24h: **`{tf_24h['pnl_bp']:+.2f} bp`** ({tf_24h['total_trades']}戦/{tf_24h['win_rate_pct']:.0f}% / ¥{tf_24h['pnl_jpy']:+,.0f})\n"
                        f"• 建玉: `{self.tf2bp.position_side or 'FLAT'}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "🔬 ③ TF2BP_PEG_v2 (Model 3+1 観測レーン)",
                    "value": (
                        f"• 1h: **`{v2_1h['pnl_bp']:+.2f} bp`** ({v2_1h['total_trades']}戦/{v2_1h['win_rate_pct']:.0f}% / ¥{v2_1h['pnl_jpy']:+,.0f})\n"
                        f"• 24h: **`{v2_24h['pnl_bp']:+.2f} bp`** ({v2_24h['total_trades']}戦/{v2_24h['win_rate_pct']:.0f}% / ¥{v2_24h['pnl_jpy']:+,.0f})\n"
                        f"• 建玉: `{self.tf2bp_v2.position_side or ('PENDING' if self.tf2bp_v2.pending_order else 'FLAT')}`\n"
                        f"• **Baseline比較差分**: 1h: **`{diff_1h_bp:+.2f} bp`** | 24h: **`{diff_24h_bp:+.2f} bp`**"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "🏛️ Antigravity 24h Multi-Strategy Dry-run Sentinel"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.notifier.post_dryrun_multicast({"embeds": [embed]})


def main():
    parser = argparse.ArgumentParser(description="24-Hour UMM & TF2BP Dual Strategy Dry-Run Observation Runner")
    parser.add_argument("--symbol", default="FX_BTC_JPY", help="対象銘柄")
    parser.add_argument("--hours", type=float, default=24.0, help="観察時間 (デフォルト: 24時間)")
    parser.add_argument("--interval", type=float, default=2.0, help="観測間隔 (秒)")
    parser.add_argument("--report-interval", type=float, default=3600.0, help="Discord進捗レポート間隔 (秒, デフォルト1時間)")
    args = parser.parse_args()

    runner = DryRunObservation24h(
        symbol=args.symbol,
        duration_hours=args.hours,
        interval_sec=args.interval,
        discord_report_sec=args.report_interval,
    )
    runner.run()


if __name__ == "__main__":
    main()
