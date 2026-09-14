from typing import Dict, Any, Optional
from .base import BaseTickStrategy


class InventorySkewTickMMStrategy(BaseTickStrategy):
    """
    【ティック駆動型 在庫スキューMM戦略 (高回転スプレッド回収版)】
    - タイトスプレッドで指値を提示しMaker手数料利得＆微小サヤを獲得
    - 2.5bp反発で高回転利確
    - 逆流Taker殺到時に即時CANCELで貫通・逆選択を防止
    """

    def __init__(self, parameters: Optional[Dict[str, Any]] = None, **kwargs):
        default_params = {
            "half_spread_bp": 1.2,
            "micro_tp_bp": 2.5,
            "stop_loss_bp": 5.0,
            "skew_factor": 1.5,
            "reverse_flow_exit_ratio": 0.55,
        }
        if parameters:
            default_params.update(parameters)
        if kwargs:
            if "spread_bp" in kwargs:
                default_params["half_spread_bp"] = kwargs["spread_bp"] / 2.0
            if "cancel_reverse_threshold" in kwargs:
                default_params["reverse_flow_exit_ratio"] = abs(kwargs["cancel_reverse_threshold"])
            default_params.update(kwargs)
        super().__init__(name="InventorySkewTickMM", version="v2.0", parameters=default_params)
        self.strategy_type = "market_making"

    def on_tick(
        self,
        tick: Dict[str, Any],
        flow_stats: Dict[str, Any],
        current_pos: float,
        entry_price: float,
    ) -> Dict[str, Any]:
        curr_p = tick.get("price", 0.0)
        delta_ratio = flow_stats.get("delta_ratio", 0.0)

        # ポジション保有中の決済
        if current_pos != 0 and entry_price > 0:
            is_long = current_pos > 0
            ret_bp = ((curr_p - entry_price) / entry_price * 10000.0) * (1.0 if is_long else -1.0)

            # 逆方向Taker殺到時は即時CANCEL脱出
            rev_exit = self.parameters.get("reverse_flow_exit_ratio", 0.55)
            if is_long and (flow_stats.get("should_cancel_bid") or delta_ratio <= -rev_exit):
                return {"action": "CANCEL", "reason": f"MM逆流Cancel脱出 (Delta:{delta_ratio:.2f})", "target_price": None}
            if not is_long and (flow_stats.get("should_cancel_ask") or delta_ratio >= rev_exit):
                return {"action": "CANCEL", "reason": f"MM逆流Cancel脱出 (Delta:{delta_ratio:.2f})", "target_price": None}

            # 微小反発利確 (2.5bp)
            if ret_bp >= self.parameters["micro_tp_bp"]:
                return {"action": "EXIT", "reason": f"MM高回転利確 (+{ret_bp:.1f}bp)", "target_price": None}

            # タイト損切
            if ret_bp <= -self.parameters["stop_loss_bp"]:
                return {"action": "EXIT", "reason": f"MMタイト損切 ({ret_bp:.1f}bp)", "target_price": None}

            return {"action": "HOLD", "reason": f"MMスプレッド獲得待機 ({ret_bp:+.1f}bp)", "target_price": None}

        # ノーポジション時
        if current_pos == 0:
            if not flow_stats.get("should_cancel_bid") and delta_ratio > -0.25:
                return {
                    "action": "BUY",
                    "reason": "MM買い指値REFILL (スプレッド確保)",
                    "target_price": curr_p * (1 - self.parameters["half_spread_bp"] / 10000.0),
                }
            elif not flow_stats.get("should_cancel_ask") and delta_ratio < 0.25:
                return {
                    "action": "SELL",
                    "reason": "MM売り指値REFILL (スプレッド確保)",
                    "target_price": curr_p * (1 + self.parameters["half_spread_bp"] / 10000.0),
                }

        return {"action": "HOLD", "reason": "MM待機", "target_price": None}
