"""
MeanReversionStrategy: GAPCORE Clean Architecture Strategy Brain
=================================================================
Statistical Mean-Reversion tick strategy that fades extreme price deviations
from the dynamic Micro-EMA baseline back towards equilibrium.

Characteristics:
- Strategy type: Mean Reversion / Range Bound
- Compliant with StrategyBrain (decide_target_qty)
- Fades overextended price spikes when Order Book Imbalance supports reversal
- Closes position when price converges back within target band
"""

import time
from typing import Dict, Any, Optional
from .base import StrategyBrain


class MeanReversionStrategy(StrategyBrain):
    def __init__(self, parameters: Optional[Dict[str, Any]] = None, **kwargs):
        default_params = {
            "baseline_span_sec": 120.0,   # 基準EMA (2分足相当)
            "reversion_trigger_bp": 8.0,  # 逆張りエントリー乖離閾値 (8bp)
            "target_exit_bp": 2.0,        # 平均回帰利確収束閾値 (2bp以内)
            "stop_loss_bp": 15.0,         # 逆行防衛ストップ (-15bp)
            "min_imbalance": 0.05,        # 逆張り方向の板厚支持 (5%以上の板厚)
            "order_size": 0.001,          # 発注ロット
        }
        if parameters:
            default_params.update(parameters)
        if kwargs:
            default_params.update(kwargs)

        super().__init__(name="MeanReversion", version="v1.0", parameters=default_params)
        self.strategy_type = "mean_reversion"

        self.baseline_ema: Optional[float] = None
        self.last_tick_time: float = 0.0

    def _update_baseline(self, mid_price: float, current_ts: float):
        if self.baseline_ema is None or self.baseline_ema <= 0:
            self.baseline_ema = mid_price
            self.last_tick_time = current_ts
            return

        dt = max(0.001, current_ts - self.last_tick_time)
        self.last_tick_time = current_ts
        alpha = min(1.0, dt / self.parameters["baseline_span_sec"])
        self.baseline_ema = alpha * mid_price + (1.0 - alpha) * self.baseline_ema

    def on_tick(
        self,
        tick: Dict[str, Any],
        flow_stats: Dict[str, Any],
        current_pos: float,
        entry_price: float,
    ) -> Dict[str, Any]:
        ts = tick.get("timestamp", time.time())
        ltp = tick.get("price", 0.0)
        mid = flow_stats.get("mid_price", tick.get("mid", ltp))
        eval_p = mid if mid > 0 else ltp

        if eval_p <= 0:
            return {"action": "HOLD", "target_qty": current_pos, "reason": "無効価格"}

        self._update_baseline(eval_p, ts)
        if self.baseline_ema is None or self.baseline_ema <= 0:
            return {"action": "HOLD", "target_qty": current_pos, "reason": "基準線初期化中"}

        # 基準線からの乖離率 (bp)
        deviation_bp = (eval_p - self.baseline_ema) / self.baseline_ema * 10000.0
        book_imb = flow_stats.get("book_imbalance", 0.0)
        lot = float(self.parameters["order_size"])

        # ----------------------------------------------------
        # 1. 保有建玉のエグジット判定 (平均回帰達成 or 損切)
        # ----------------------------------------------------
        if current_pos != 0 and entry_price > 0:
            ret_bp = ((eval_p - entry_price) / entry_price * 10000.0) if current_pos > 0 else ((entry_price - eval_p) / entry_price * 10000.0)

            # 防衛ストップ
            if ret_bp <= -self.parameters["stop_loss_bp"]:
                return {
                    "action": "EXIT",
                    "target_qty": 0.0,
                    "reason": f"平均回帰防衛ストップ損切 ({ret_bp:+.1f}bp)",
                }

            # 平均回帰達成による利確 (基準線近傍へ収束)
            if abs(deviation_bp) <= self.parameters["target_exit_bp"]:
                return {
                    "action": "EXIT",
                    "target_qty": 0.0,
                    "reason": f"平均回帰完了利確 (乖離収束:{deviation_bp:+.1f}bp, 損益:{ret_bp:+.1f}bp)",
                }

            return {
                "action": "HOLD",
                "target_qty": current_pos,
                "reason": f"平均回帰追随中 (乖離:{deviation_bp:+.1f}bp, 損益:{ret_bp:+.1f}bp)",
            }

        # ----------------------------------------------------
        # 2. ノーポジション時の逆張りエントリー判定
        # ----------------------------------------------------
        trigger_bp = self.parameters["reversion_trigger_bp"]
        min_imb = self.parameters["min_imbalance"]

        # 売られすぎ ➔ 買い逆張り (乖離 <= -trigger_bp かつ 買い板支持)
        if deviation_bp <= -trigger_bp and book_imb >= min_imb:
            return {
                "action": "BUY",
                "target_qty": lot,
                "reason": f"売られすぎ逆張りロング (乖離:{deviation_bp:+.1f}bp, 買い板支持:Imb={book_imb:+.2f})",
            }

        # 買われすぎ ➔ 売り逆張り (乖離 >= +trigger_bp かつ 売り板支持)
        if deviation_bp >= trigger_bp and book_imb <= -min_imb:
            return {
                "action": "SELL",
                "target_qty": -lot,
                "reason": f"買われすぎ逆張りショート (乖離:{deviation_bp:+.1f}bp, 売り板支持:Imb={book_imb:+.2f})",
            }

        return {"action": "HOLD", "target_qty": 0.0, "reason": "レンジ乖離待機"}
