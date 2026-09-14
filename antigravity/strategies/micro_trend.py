from typing import Dict, Any, Optional
from .base import BaseTickStrategy


class MicroTrendTickStrategy(BaseTickStrategy):
    """
    【マイクロトレンド・ティック戦略 (高回転・早期初動対応版)】
    - 1.2bp〜1.5bpの微小初動および先行大口Takerフローを敏感に検知して早期順張り
    - 初動失速時は+3.5bpで即利確して高回転化
    - 強いトレンド時は10〜20bpの大波を追随利確
    - 逆方向Taker急増時は即座にCANCEL/脱出
    """

    def __init__(self, parameters: Optional[Dict[str, Any]] = None, **kwargs):
        default_params = {
            "initial_momentum_bp": 1.2,
            "min_delta_ratio": 0.35,
            "target_trend_bp": 12.0,
            "micro_tp_bp": 3.5,
            "trail_step_bp": 3.0,
            "stop_loss_bp": 4.5,
            "reverse_flow_exit_ratio": 0.35,
        }
        if parameters:
            default_params.update(parameters)
        if kwargs:
            if "trigger_bp" in kwargs:
                default_params["initial_momentum_bp"] = kwargs["trigger_bp"]
            if "cancel_reverse_threshold" in kwargs:
                default_params["reverse_flow_exit_ratio"] = abs(kwargs["cancel_reverse_threshold"])
            default_params.update(kwargs)
        super().__init__(name="MicroTrendTick", version="v2.0", parameters=default_params)
        self.strategy_type = "micro_trend"

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

            # 大波利確目標到達
            if ret_bp >= self.parameters["target_trend_bp"]:
                return {
                    "action": "EXIT",
                    "reason": f"大波利確達成 (+{ret_bp:.1f}bp)",
                    "target_price": None,
                }

            # 初動失速時の早期マイクロ利確 (+3.5bp以上 & フロー沈静化)
            if ret_bp >= self.parameters["micro_tp_bp"]:
                is_stalled = (is_long and delta_ratio <= 0.10) or (not is_long and delta_ratio >= -0.10)
                if is_stalled:
                    return {
                        "action": "EXIT",
                        "reason": f"初動モメンタム失速・早期利確 (+{ret_bp:.1f}bp, Delta:{delta_ratio:+.2f})",
                        "target_price": None,
                    }

            # 逆流Taker急増による即時脱出 (Cancel)
            rev_exit = self.parameters["reverse_flow_exit_ratio"]
            if is_long:
                if flow_stats.get("should_cancel_bid") or delta_ratio <= -rev_exit:
                    return {
                        "action": "CANCEL",
                        "reason": f"売りTaker急増・即時Cancel脱出 (Delta:{delta_ratio:.2f}, PnL:{ret_bp:+.1f}bp)",
                        "target_price": None,
                    }
            else:
                if flow_stats.get("should_cancel_ask") or delta_ratio >= rev_exit:
                    return {
                        "action": "CANCEL",
                        "reason": f"買いTaker急増・即時Cancel脱出 (Delta:{delta_ratio:.2f}, PnL:{ret_bp:+.1f}bp)",
                        "target_price": None,
                    }

            # 防衛ストップ
            if ret_bp <= -self.parameters["stop_loss_bp"]:
                return {
                    "action": "EXIT",
                    "reason": f"防衛ストップ損切 ({ret_bp:.1f}bp)",
                    "target_price": None,
                }

            return {"action": "HOLD", "reason": f"トレンド追随中 ({ret_bp:+.1f}bp)", "target_price": None}

        # ノーポジション時
        if current_pos == 0:
            min_bp = self.parameters["initial_momentum_bp"]
            min_delta = self.parameters["min_delta_ratio"]
            p_change = flow_stats.get("price_change_bp", 0.0)

            is_up = (
                flow_stats.get("is_micro_momentum_up")
                or flow_stats.get("is_2bp_momentum_up")
                or (p_change >= min_bp and delta_ratio >= min_delta)
                or (p_change >= 0.8 and delta_ratio >= 0.60)
            )

            is_down = (
                flow_stats.get("is_micro_momentum_down")
                or flow_stats.get("is_2bp_momentum_down")
                or (p_change <= -min_bp and delta_ratio <= -min_delta)
                or (p_change <= -0.8 and delta_ratio <= -0.60)
            )

            if is_up:
                return {
                    "action": "BUY",
                    "reason": f"初動順張り (Change:{p_change:+.1f}bp, Delta:{delta_ratio:+.2f})",
                    "target_price": None,
                }
            elif is_down:
                return {
                    "action": "SELL",
                    "reason": f"初動順張り (Change:{p_change:+.1f}bp, Delta:{delta_ratio:+.2f})",
                    "target_price": None,
                }

        return {"action": "HOLD", "reason": "待機中 (初動シグナル待ち)", "target_price": None}
