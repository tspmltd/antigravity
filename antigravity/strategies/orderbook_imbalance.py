"""
OrderBookImbalanceStrategy: GAPCORE Clean Architecture Strategy Brain
=====================================================================
High-frequency Order Book Imbalance (OBI) scalp strategy that trades
alongside strong order book depth skew and taker momentum bursts.

Characteristics:
- Strategy type: Order Flow / Market Microstructure Scalp
- Compliant with StrategyBrain (decide_target_qty)
- Enters when BookImbalance exceeds threshold (e.g. +0.30 / -0.30)
- Exits quickly when book returns to equilibrium or on trailing profit
"""

import time
from typing import Dict, Any, Optional
from .base import StrategyBrain


class OrderBookImbalanceStrategy(StrategyBrain):
    def __init__(self, parameters: Optional[Dict[str, Any]] = None, **kwargs):
        default_params = {
            "imbalance_threshold": 0.25,     # エントリー板厚不均衡閾値 (25%偏り)
            "min_delta_ratio": 0.20,         # Taker Delta方向一致閾値 (20%)
            "take_profit_bp": 6.0,           # スキャルプ利確目標 (+6bp)
            "stop_loss_bp": 8.0,             # スキャルプ防衛ストップ (-8bp)
            "max_spread_bp": 4.0,            # スプレッド拡大抑止 (4bp)
            "order_size": 0.001,             # 発注ロット
        }
        if parameters:
            default_params.update(parameters)
        if kwargs:
            default_params.update(kwargs)

        super().__init__(name="OrderBookImbalance", version="v1.0", parameters=default_params)
        self.strategy_type = "microstructure_scalp"

    def on_tick(
        self,
        tick: Dict[str, Any],
        flow_stats: Dict[str, Any],
        current_pos: float,
        entry_price: float,
    ) -> Dict[str, Any]:
        ltp = tick.get("price", 0.0)
        mid = flow_stats.get("mid_price", tick.get("mid", ltp))
        eval_p = mid if mid > 0 else ltp

        if eval_p <= 0:
            return {"action": "HOLD", "target_qty": current_pos, "reason": "無効価格"}

        book_imb = flow_stats.get("book_imbalance", 0.0)
        spread_bp = flow_stats.get("spread_bp", 0.0)
        delta = flow_stats.get("delta_ratio", 0.0)
        lot = float(self.parameters["order_size"])

        # ----------------------------------------------------
        # 1. 保有建玉のエグジット判定
        # ----------------------------------------------------
        if current_pos != 0 and entry_price > 0:
            ret_bp = ((eval_p - entry_price) / entry_price * 10000.0) if current_pos > 0 else ((entry_price - eval_p) / entry_price * 10000.0)

            # 防衛ストップ
            if ret_bp <= -self.parameters["stop_loss_bp"]:
                return {
                    "action": "EXIT",
                    "target_qty": 0.0,
                    "reason": f"OBIスキャルプ防衛ストップ損切 ({ret_bp:+.1f}bp)",
                }

            # 目標利確達成
            if ret_bp >= self.parameters["take_profit_bp"]:
                return {
                    "action": "EXIT",
                    "target_qty": 0.0,
                    "reason": f"OBI目標利確完了 ({ret_bp:+.1f}bp)",
                }

            # 板厚逆転による即時脱出
            if current_pos > 0 and book_imb < -0.15:
                return {
                    "action": "EXIT",
                    "target_qty": 0.0,
                    "reason": f"板厚反転脱出 (売り板偏重: Imb={book_imb:+.2f}, 損益:{ret_bp:+.1f}bp)",
                }
            elif current_pos < 0 and book_imb > 0.15:
                return {
                    "action": "EXIT",
                    "target_qty": 0.0,
                    "reason": f"板厚反転脱出 (買い板偏重: Imb={book_imb:+.2f}, 損益:{ret_bp:+.1f}bp)",
                }

            return {
                "action": "HOLD",
                "target_qty": current_pos,
                "reason": f"OBIスキャルプ追随中 (Imb:{book_imb:+.2f}, 損益:{ret_bp:+.1f}bp)",
            }

        # ----------------------------------------------------
        # 2. ノーポジション時のエントリー判定
        # ----------------------------------------------------
        if spread_bp > self.parameters["max_spread_bp"]:
            return {"action": "HOLD", "target_qty": 0.0, "reason": "スプレッド拡大見送り"}

        imb_th = self.parameters["imbalance_threshold"]
        min_delta = self.parameters["min_delta_ratio"]

        # 買いエントリー: 買い板が圧倒的 ＆ Taker買い追随
        if book_imb >= imb_th and delta >= min_delta:
            return {
                "action": "BUY",
                "target_qty": lot,
                "reason": f"買い板厚不均衡エントリー (Imb={book_imb:+.2f} >= +{imb_th:.2f}, Delta={delta:+.2f})",
            }

        # 売りエントリー: 売り板が圧倒的 ＆ Taker売り追随
        if book_imb <= -imb_th and delta <= -min_delta:
            return {
                "action": "SELL",
                "target_qty": -lot,
                "reason": f"売り板厚不均衡エントリー (Imb={book_imb:+.2f} <= -{imb_th:.2f}, Delta={delta:+.2f})",
            }

        return {"action": "HOLD", "target_qty": 0.0, "reason": "板厚不均衡待機"}
