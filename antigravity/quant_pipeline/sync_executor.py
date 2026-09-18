"""
Sync Executor (Dry-run & LIVE 同期執行オーケストレーター)
==========================================================
共通の意思決定ロジック (Signal Fusion Engine) から発せられるシグナルを受け取り、
1. Dry-run 側 (影武者) で常時仮想約定・PnL計算・Discord観測配信
2. LIVE 側で SafetyGate を評価し、合格時のみ実発注 & Discord LIVE通知
"""
import time
from typing import Dict, Any, Optional
from dataclasses import asdict

from .safety_gate import SafetyGate, LiveExecutionState, SafetyGateConfig, DryRunStats
from .dryrun_simulator import DryRunSimulator
from .quant_discord_notifier import QuantDiscordNotifier
from .live_order_executor import LiveOrderExecutor


class SyncExecutor:
    def __init__(
        self,
        notifier: QuantDiscordNotifier,
        safety_gate: Optional[SafetyGate] = None,
        dryrun_sim: Optional[DryRunSimulator] = None,
        live_executor: Optional[LiveOrderExecutor] = None,
        symbol: str = "FX_BTC_JPY",
        live_order_size: float = 0.001,
        enable_real_live: bool = False,
    ):
        self.notifier = notifier
        self.safety_gate = safety_gate or SafetyGate()
        self.dryrun_sim = dryrun_sim or DryRunSimulator(notifier=notifier)
        self.symbol = symbol
        self.live_order_size = live_order_size
        self.enable_real_live = enable_real_live

        # 本番執行エンジン
        self.live_executor = live_executor or LiveOrderExecutor(
            notifier=notifier,
            symbol=symbol,
            order_size_btc=live_order_size,
            enable_real_trading=enable_real_live,
        )

    @property
    def live_state(self) -> LiveExecutionState:
        return self.live_executor.state

    def on_market_tick(self, mid_price: float, best_bid: Optional[float] = None, best_ask: Optional[float] = None):
        """価格更新をDry-runとLIVEの両方へ反映（利確・損切・タイムアウト判定）"""
        # 1. 影武者 (Dry-run) の仮想ポジション監視
        self.dryrun_sim.feed_market_price(mid_price, best_bid=best_bid, best_ask=best_ask)

        # 2. LIVE (本番) の建玉防護チェック (利確/損切/タイムアウト)
        self.live_executor.check_position_guards(mid_price, best_bid=best_bid, best_ask=best_ask)

    def process_signal(
        self,
        signal: Dict[str, Any],
        current_price: float,
        spread_jpy: Optional[float] = None,
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
    ):
        """
        共通シグナルを受け取り、Dry-runとLIVEを同期処理
        """
        action = signal.get("action", "hold").lower()

        # ---------------------------------------------------------
        # ステップ 1: Dry-run 側 (影武者) は常に全シグナルを受け取って検証
        # ---------------------------------------------------------
        self.dryrun_sim.feed_signal(signal, current_price, best_bid=best_bid, best_ask=best_ask)
        stats = self.dryrun_sim.get_stats()

        # ---------------------------------------------------------
        # ステップ 2: LIVE 側 (本番) は Safety Gate で厳格に判定
        # ---------------------------------------------------------
        allowed, reason = self.safety_gate.allow(signal, self.live_state, stats, spread_jpy=spread_jpy)

        if allowed:
            if action in ("buy", "sell"):
                entry_market_price = best_ask if action == "buy" and best_ask else (best_bid if action == "sell" and best_bid else current_price)
                self.live_executor.execute_entry(
                    action=action,
                    market_price=entry_market_price,
                    signal=signal,
                    stats=stats,
                )
            elif action == "exit" and self.live_state.has_open_position:
                exit_price = best_bid if self.live_state.open_side == "buy" and best_bid else (best_ask if self.live_state.open_side == "sell" and best_ask else current_price)
                self.live_executor.execute_exit(
                    current_price=exit_price,
                    reason="FUSION_EXIT_SIGNAL",
                )
        else:
            # 却下ログ (ノイズ防止のためコンソールのみ、またはデバッグ用)
            if action in ("buy", "sell"):
                print(f"[SyncExecutor:LIVE-GATE] 🛡️ シグナル拒絶: {action.upper()} - {reason} (確信度: {signal.get('final_confidence', 0):.2f})")
