from typing import Dict, Any, Optional
from .base import BaseTickStrategy


class OrderFlowScalpTickStrategy(BaseTickStrategy):
    """
    【オーダーフロー・インバランス 高頻度スキャルピング戦略】
    - 直近のTaker差分量（Delta）の瞬間的不均衡に即座に飛び乗る
    - +1.8bp〜+2.0bpの微小利益を高回転に刈り取る超高頻度戦略
    - 逆行時はミリ秒で即座に脱出
    """

    def __init__(self, parameters: Optional[Dict[str, Any]] = None, **kwargs):
        default_params = {
            "imbalance_threshold": 0.45,
            "target_tp_bp": 2.0,
            "stop_loss_bp": 3.5,
            "cancel_reverse_ratio": 0.35,
        }
        if parameters:
            default_params.update(parameters)
        if kwargs:
            default_params.update(kwargs)
        super().__init__(name="OrderFlowScalpTick", version="v1.0", parameters=default_params)
        self.strategy_type = "scalping"

    def on_tick(
        self,
        tick: Dict[str, Any],
        flow_stats: Dict[str, Any],
        current_pos: float,
        entry_price: float,
    ) -> Dict[str, Any]:
        curr_p = tick.get("price", 0.0)
        delta_ratio = flow_stats.get("delta_ratio", 0.0)

        # ポジション保有中
        if current_pos != 0 and entry_price > 0:
            is_long = current_pos > 0
            ret_bp = ((curr_p - entry_price) / entry_price * 10000.0) * (1.0 if is_long else -1.0)

            # 瞬時スキャルプ利確
            if ret_bp >= self.parameters["target_tp_bp"]:
                return {"action": "EXIT", "reason": f"スキャルプ瞬時利確 (+{ret_bp:.1f}bp)", "target_price": None}

            # 逆流脱出
            rev_exit = self.parameters["cancel_reverse_ratio"]
            if is_long and delta_ratio <= -rev_exit:
                return {"action": "CANCEL", "reason": f"スキャルプ逆流脱出 (Delta:{delta_ratio:+.2f})", "target_price": None}
            if not is_long and delta_ratio >= rev_exit:
                return {"action": "CANCEL", "reason": f"スキャルプ逆流脱出 (Delta:{delta_ratio:+.2f})", "target_price": None}

            # タイトストップ
            if ret_bp <= -self.parameters["stop_loss_bp"]:
                return {"action": "EXIT", "reason": f"スキャルプ防衛損切 ({ret_bp:.1f}bp)", "target_price": None}

            return {"action": "HOLD", "reason": f"スキャルプ保有中 ({ret_bp:+.1f}bp)", "target_price": None}

        # ノーポジション時
        if current_pos == 0:
            imb = self.parameters["imbalance_threshold"]
            if delta_ratio >= imb:
                return {
                    "action": "BUY",
                    "reason": f"Taker買いインバランス検知 (Delta:{delta_ratio:+.2f})",
                    "target_price": None,
                }
            elif delta_ratio <= -imb:
                return {
                    "action": "SELL",
                    "reason": f"Taker売りインバランス検知 (Delta:{delta_ratio:+.2f})",
                    "target_price": None,
                }

        return {"action": "HOLD", "reason": "スキャルプ待機", "target_price": None}
