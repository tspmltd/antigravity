"""
Dry-run Virtual Simulator (影武者シミュレータ)
==============================================
Fusion Engine のシグナルを先取りしてリアルタイムに仮想売買をシミュレーション。
仮想ポジション管理、仮想PnL計算、勝率・Sharpe比を算出して
SafetyGate に提供するとともに、Discord Dry-run サーバーへ観測ログを配信。
"""
import time
import math
from typing import Dict, Any, Optional, List
from .safety_gate import DryRunStats
from .quant_discord_notifier import QuantDiscordNotifier


class DryRunSimulator:
    def __init__(
        self,
        notifier: Optional[QuantDiscordNotifier] = None,
        trade_size_btc: float = 0.001,
        take_profit_jpy: float = 25.0,
        stop_loss_jpy: float = 20.0,
        max_hold_sec: float = 1800.0,
    ):
        self.notifier = notifier
        self.trade_size_btc = trade_size_btc
        self.take_profit_jpy = take_profit_jpy
        self.stop_loss_jpy = stop_loss_jpy
        self.max_hold_sec = max_hold_sec

        # ポジション状態
        self.position_side: Optional[str] = None  # "buy" or "sell"
        self.entry_price: float = 0.0
        self.entry_ts: float = 0.0
        self.entry_confidence: float = 0.0

        # トレード統計
        self.trades_history: List[float] = []  # 各トレードの損益
        self.win_count: int = 0
        self.loss_count: int = 0
        self.consecutive_losses: int = 0
        self.total_pnl: float = 0.0
        self.peak_pnl: float = 0.0
        self.max_dd: float = 0.0

    def feed_market_price(
        self,
        current_price: float,
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """最新価格を投入し、利確・損切・タイムアウトを判定 (スプレッド考慮)"""
        if not self.position_side:
            return None

        elapsed = time.time() - self.entry_ts
        # 実効評価価格: BUY建玉決済はbest_bid、SELL建玉決済はbest_ask
        if self.position_side == "buy":
            eval_price = best_bid if best_bid and best_bid > 0 else current_price
            diff = eval_price - self.entry_price
        else:
            eval_price = best_ask if best_ask and best_ask > 0 else current_price
            diff = self.entry_price - eval_price

        pnl = diff * self.trade_size_btc

        exit_reason = None
        # 1. 利確判定 (円損益で判定)
        if pnl >= self.take_profit_jpy:
            exit_reason = f"TAKE_PROFIT (+¥{pnl:.1f} [BTC幅:{diff:+,.0f}円])"

        # 2. 損切判定 (円損益で判定)
        elif pnl <= -self.stop_loss_jpy:
            exit_reason = f"STOP_LOSS (-¥{abs(pnl):.1f} [BTC幅:{diff:+,.0f}円])"

        # 3. タイムアウト判定
        elif elapsed >= self.max_hold_sec:
            exit_reason = f"TIMEOUT ({elapsed:.0f}s経過, pnl:{pnl:+.1f}円)"

        if exit_reason:
            return self._close_position(eval_price, exit_reason)

        return None

    def feed_signal(
        self,
        signal: Dict[str, Any],
        current_price: float,
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fusion Engine のシグナルを処理 (スプレッド・気配値を反映)"""
        action = signal.get("action", "hold").lower()
        confidence = float(signal.get("final_confidence", 0.0))

        # エグジットシグナル
        if action == "exit" and self.position_side:
            eval_price = best_bid if self.position_side == "buy" and best_bid else (best_ask if self.position_side == "sell" and best_ask else current_price)
            return self._close_position(eval_price, "FUSION_EXIT_SIGNAL")

        # 新規エントリー (BUYは約定Ask、SELLは約定Bidでスプレッドを正確に反映)
        if action in ("buy", "sell") and not self.position_side:
            self.position_side = action
            if action == "buy":
                self.entry_price = best_ask if best_ask and best_ask > 0 else current_price
            else:
                self.entry_price = best_bid if best_bid and best_bid > 0 else current_price

            self.entry_ts = time.time()
            self.entry_confidence = confidence

            # Discord Dry-run へ新規仮想エントリー通知
            if self.notifier:
                self.notifier.notify_dryrun_fusion_decision(
                    decision=signal,
                    mid_price=self.entry_price,
                    virtual_pnl=self.total_pnl,
                    virtual_win_rate=self.get_stats().win_rate,
                    force=True,
                )

        return None

    def _close_position(self, exit_price: float, reason: str) -> Dict[str, Any]:
        """仮想ポジション決済処理"""
        diff = exit_price - self.entry_price
        pnl = (diff if self.position_side == "buy" else -diff) * self.trade_size_btc

        self.total_pnl += pnl
        self.trades_history.append(pnl)

        if pnl > 0:
            self.win_count += 1
            self.consecutive_losses = 0
        else:
            self.loss_count += 1
            self.consecutive_losses += 1

        # ドローダウン計算
        if self.total_pnl > self.peak_pnl:
            self.peak_pnl = self.total_pnl
        current_dd = self.peak_pnl - self.total_pnl
        if current_dd > self.max_dd:
            self.max_dd = current_dd

        side = self.position_side
        entry_p = self.entry_price
        self.position_side = None

        trade_info = {
            "side": side,
            "entry_price": entry_p,
            "exit_price": exit_price,
            "pnl": pnl,
            "reason": reason,
            "total_pnl": self.total_pnl,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
        }

        # Discord Dry-run サーバーへ決済通知
        if self.notifier:
            self.notifier.notify_dryrun_virtual_trade(
                side=side,
                entry_price=entry_p,
                exit_price=exit_price,
                pnl=pnl,
                reason=reason,
                total_pnl=self.total_pnl,
                win_count=self.win_count,
                loss_count=self.loss_count,
            )

        return trade_info

    def get_stats(self) -> DryRunStats:
        """現在の DryRunStats を計算して返却"""
        total = self.win_count + self.loss_count
        win_rate = (self.win_count / total) if total > 0 else 0.0
        avg_pnl = (self.total_pnl / total) if total > 0 else 0.0

        # Sharpe 比計算 (直近トレードの平均/標準偏差)
        sharpe = 0.0
        if total >= 3:
            mean = sum(self.trades_history) / total
            variance = sum((x - mean) ** 2 for x in self.trades_history) / total
            std_dev = math.sqrt(variance) if variance > 1e-6 else 1e-6
            sharpe = (mean / std_dev) * math.sqrt(252 * 24)  # 年率概算

        return DryRunStats(
            total_trades=total,
            win_count=self.win_count,
            loss_count=self.loss_count,
            win_rate=round(win_rate, 3),
            total_pnl=round(self.total_pnl, 2),
            avg_pnl=round(avg_pnl, 2),
            sharpe_ratio=round(sharpe, 2),
            max_drawdown=round(self.max_dd, 2),
            consecutive_losses=self.consecutive_losses,
            recent_trend_positive=(avg_pnl > 0),
        )
