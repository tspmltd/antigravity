"""
Live Order Executor (本番執行エンジン & リスク防護)
===================================================
SafetyGate を通過したシグナルに基づき、bitFlyer Lightning FX への実発注および建玉管理を行う。
- 実建玉の同期 (API /v1/me/getpositions)
- 利確 (+15円) / 損切 (-12円) / 最大保有時間 (30分) ガード
- 連敗ガード (4回) / 日次最大損失ガード (300円) による自動緊急停止
- Discord LIVE 取引サーバーへのノイズゼロ通知
"""
import os
import sys
import time
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta

from core.bitflyer_client import BitFlyerClient
from .safety_gate import LiveExecutionState, DryRunStats
from .quant_discord_notifier import QuantDiscordNotifier

JST = timezone(timedelta(hours=9))


class LiveOrderExecutor:
    def __init__(
        self,
        notifier: Optional[QuantDiscordNotifier] = None,
        symbol: str = "FX_BTC_JPY",
        order_size_btc: float = 0.001,
        enable_real_trading: bool = False,
        take_profit_jpy: float = 35.0,
        stop_loss_jpy: float = 25.0,
        max_hold_sec: float = 1800.0,
        daily_loss_limit_jpy: float = 300.0,
        max_consecutive_losses: int = 4,
    ):
        self.notifier = notifier
        self.symbol = symbol
        self.order_size_btc = order_size_btc
        self.enable_real_trading = enable_real_trading
        self.take_profit_jpy = take_profit_jpy
        self.stop_loss_jpy = stop_loss_jpy
        self.max_hold_sec = max_hold_sec
        self.daily_loss_limit_jpy = daily_loss_limit_jpy
        self.max_consecutive_losses = max_consecutive_losses

        # bitFlyer Private API クライアント初期化
        self.client = BitFlyerClient(enable_real_trading=enable_real_trading)

        # 口座・建玉状態
        self.state = LiveExecutionState()
        self.entry_price: float = 0.0
        self.entry_time: float = 0.0
        self.trades_history: List[Dict[str, Any]] = []

        # 起動時に取引所から実建玉を同期復元
        self.sync_positions_from_exchange()

    def sync_positions_from_exchange(self) -> bool:
        """bitFlyer取引所から現在の実建玉を取得し状態を同期"""
        if not self.enable_real_trading or not self.client.api_key:
            return False
        try:
            positions = self.client.get_positions(product_code=self.symbol)
            if positions and isinstance(positions, list) and len(positions) > 0:
                # 複数建玉がある場合は合算
                total_size = sum(float(p["size"]) for p in positions)
                primary_side = positions[0]["side"].lower()
                avg_price = sum(float(p["price"]) * float(p["size"]) for p in positions) / total_size

                self.state.has_open_position = True
                self.state.open_side = primary_side
                self.state.open_size = total_size
                self.entry_price = avg_price
                self.entry_time = time.time()
                print(f"[LiveOrderExecutor] 🔄 実建玉を復元同期: {primary_side.upper()} {total_size} BTC @ ¥{avg_price:,.0f}")
                return True
            else:
                self.state.has_open_position = False
                self.state.open_side = None
                self.state.open_size = 0.0
                return True
        except Exception as e:
            print(f"[LiveOrderExecutor] ⚠️ 建玉同期エラー: {e}")
            return False

    def execute_entry(
        self,
        action: str,
        market_price: float,
        signal: Dict[str, Any],
        stats: DryRunStats,
    ) -> bool:
        """安全ゲート通過後の本番エントリー発注"""
        if self.state.is_halted:
            print("[LiveOrderExecutor] 🛑 LIVE停止中のためエントリーは見送られました。")
            return False

        if self.state.has_open_position:
            print("[LiveOrderExecutor] ⚠️ 既存建玉があるためエントリーは見送られました。")
            return False

        action_upper = action.upper()
        print(f"\n[LiveOrderExecutor] ⚡ 【本番エントリー執行】 {action_upper} {self.order_size_btc} BTC @ ¥{market_price:,.0f}")

        exec_price = market_price
        acceptance_id = None

        if self.enable_real_trading:
            try:
                res = self.client.send_order(
                    product_code=self.symbol,
                    side=action_upper,
                    size=self.order_size_btc,
                    order_type="MARKET",
                )
                acceptance_id = res.get("child_order_acceptance_id")
                # 実約定価格の取得を試行
                actual_p = self.client.get_execution_price_by_acceptance_id(
                    product_code=self.symbol,
                    acceptance_id=acceptance_id,
                    max_retries=3,
                    retry_interval_sec=0.3,
                )
                if actual_p:
                    exec_price = actual_p
            except Exception as e:
                print(f"[LiveOrderExecutor] ❌ 本番発注APIエラー: {e}")
                if self.notifier:
                    self.notifier.notify_live_risk_alert(
                        alert_title="発注APIエラー発生",
                        reason=str(e),
                        action_taken="エントリー中断・安全維持",
                        daily_pnl=self.state.daily_pnl,
                        consecutive_losses=self.state.consecutive_losses,
                    )
                return False

        # 建玉状態更新
        self.state.has_open_position = True
        self.state.open_side = action.lower()
        self.state.open_size = self.order_size_btc
        self.state.last_trade_ts = time.time()
        self.entry_price = exec_price
        self.entry_time = time.time()

        # Discord LIVE 取引サーバーへ通知
        if self.notifier:
            mode_note = "【実資金】bitFlyer API執行" if self.enable_real_trading else "【安全検証】LIVEシミュレーション執行"
            self.notifier.notify_live_trade(
                action=action.lower(),
                symbol=self.symbol,
                price=exec_price,
                size=self.order_size_btc,
                trade_type="ENTRY",
                daily_pnl=self.state.daily_pnl,
                consecutive_losses=self.state.consecutive_losses,
                extra_note=f"{mode_note} | 影武者Sharpe: {stats.sharpe_ratio:.2f} | 確信度: {signal.get('final_confidence', 0):.2f}",
            )
        return True

    def check_position_guards(
        self,
        current_price: float,
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """建玉の防護チェック（利確 / 損切 / タイムアウト）"""
        if not self.state.has_open_position or self.entry_price <= 0:
            return None

        elapsed = time.time() - self.entry_time
        # 実効評価価格: BUY決済時は売るためbest_bid、SELL決済時は買うためbest_askを使用（気配値があれば）
        if self.state.open_side == "buy":
            eval_price = best_bid if best_bid and best_bid > 0 else current_price
            diff = eval_price - self.entry_price
        else:
            eval_price = best_ask if best_ask and best_ask > 0 else current_price
            diff = self.entry_price - eval_price

        # 実損益 (円) = 価格変動幅 (BTC単価差) * 建玉サイズ (BTC)
        pnl = diff * self.state.open_size

        exit_reason = None
        trade_type = "TAKE_PROFIT"

        # 1. 最低利幅目標 (TAKE PROFIT: 実現円損益で判定)
        if pnl >= self.take_profit_jpy:
            exit_reason = f"TAKE_PROFIT (+¥{pnl:.1f} [BTC幅:{diff:+,.0f}円])"
            trade_type = "TAKE_PROFIT"

        # 2. ハードストップロス (STOP LOSS: 実現円損益で判定)
        elif pnl <= -self.stop_loss_jpy:
            exit_reason = f"STOP_LOSS (-¥{abs(pnl):.1f} [BTC幅:{diff:+,.0f}円])"
            trade_type = "STOP_LOSS"

        # 3. 最大保有時間 (TIMEOUT)
        elif elapsed >= self.max_hold_sec:
            exit_reason = f"TIMEOUT ({elapsed:.0f}s経過, pnl:{pnl:+.1f}円)"
            trade_type = "TAKE_PROFIT" if pnl > 0 else "STOP_LOSS"

        if exit_reason:
            return self._close_position(eval_price, exit_reason, trade_type)

        return None

    def execute_exit(self, current_price: float, reason: str = "SIGNAL_EXIT") -> Optional[Dict[str, Any]]:
        """外部シグナルによるポジション手仕舞い"""
        if not self.state.has_open_position:
            return None
        return self._close_position(current_price, reason, "TAKE_PROFIT")

    def _close_position(self, current_price: float, reason: str, trade_type: str) -> Dict[str, Any]:
        """ポジション決済執行"""
        close_side = "SELL" if self.state.open_side == "buy" else "BUY"
        exec_price = current_price

        print(f"\n[LiveOrderExecutor] 🛡️ 【本番ポジション決済】 {close_side} {self.state.open_size} BTC @ ¥{current_price:,.0f} ({reason})")

        if self.enable_real_trading:
            try:
                res = self.client.send_order(
                    product_code=self.symbol,
                    side=close_side,
                    size=self.state.open_size,
                    order_type="MARKET",
                )
                acceptance_id = res.get("child_order_acceptance_id")
                actual_p = self.client.get_execution_price_by_acceptance_id(
                    product_code=self.symbol,
                    acceptance_id=acceptance_id,
                    max_retries=3,
                    retry_interval_sec=0.3,
                )
                if actual_p:
                    exec_price = actual_p
            except Exception as e:
                print(f"[LiveOrderExecutor] ❌ 決済発注エラー: {e}")

        diff = (exec_price - self.entry_price) if self.state.open_side == "buy" else (self.entry_price - exec_price)
        pnl = diff * self.state.open_size
        self.state.daily_pnl += pnl

        if pnl > 0:
            self.state.consecutive_losses = 0
        else:
            self.state.consecutive_losses += 1

        prev_side = self.state.open_side
        self.state.has_open_position = False
        self.state.open_side = None
        self.state.open_size = 0.0
        self.entry_price = 0.0
        self.entry_time = 0.0

        trade_record = {
            "side": prev_side,
            "pnl": pnl,
            "reason": reason,
            "daily_pnl": self.state.daily_pnl,
            "consecutive_losses": self.state.consecutive_losses,
        }
        self.trades_history.append(trade_record)

        # Discord LIVE サーバーへ約定通知
        if self.notifier:
            self.notifier.notify_live_trade(
                action="exit",
                symbol=self.symbol,
                price=exec_price,
                size=self.order_size_btc,
                pnl=pnl,
                trade_type=trade_type,
                daily_pnl=self.state.daily_pnl,
                consecutive_losses=self.state.consecutive_losses,
                extra_note=f"理由: {reason} | 本番実現損益確定",
            )

        # リスク制限到達チェック
        if self.state.daily_pnl <= -self.daily_loss_limit_jpy:
            self._trigger_emergency_halt(f"日次最大損失超過 (-¥{abs(self.state.daily_pnl):.1f} <= -¥{self.daily_loss_limit_jpy})")
        elif self.state.consecutive_losses >= self.max_consecutive_losses:
            self._trigger_emergency_halt(f"最大連敗数到達 ({self.state.consecutive_losses} >= {self.max_consecutive_losses}回)")

        return trade_record

    def _trigger_emergency_halt(self, reason: str):
        """緊急停止 (キルスイッチ作動)"""
        self.state.is_halted = True
        print(f"\n[LiveOrderExecutor] 🚨 【緊急停止発動】 {reason}")
        if self.notifier:
            self.notifier.notify_live_risk_alert(
                alert_title="安全装置発動による自動取引停止",
                reason=reason,
                action_taken="当日全エントリー遮断 & 本番資金完全保護",
                daily_pnl=self.state.daily_pnl,
                consecutive_losses=self.state.consecutive_losses,
            )
