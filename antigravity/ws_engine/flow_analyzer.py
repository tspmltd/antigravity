from typing import Dict, Any, List, Optional
from datetime import datetime


class FlowAnalyzer:
    """
    約定ストリーム（Tick Data）からミリ秒単位でオーダーフローおよび価格モメンタム指標を分析・算出するアナライザー。
    """

    def __init__(self, window_seconds: float = 15.0):
        self.window_seconds = window_seconds

    @staticmethod
    def empty_stats(current_price: float = 0.0) -> Dict[str, Any]:
        return {
            "current_price": current_price,
            "window_seconds": 0.0,
            "tick_count": 0,
            "price_change": 0.0,
            "price_change_bp": 0.0,
            "buy_volume": 0.0,
            "sell_volume": 0.0,
            "total_volume": 0.0,
            "net_delta": 0.0,
            "delta_ratio": 0.0,
            "is_2bp_momentum_up": False,
            "is_2bp_momentum_down": False,
            "persistence_score": 0.0,
            "opposite_noise_ratio": 0.0,
        }

    def compute_flow_stats(self, ticks: List[Dict[str, Any]], current_price: float = 0.0, window_sec: Optional[float] = None) -> Dict[str, Any]:
        win = window_sec or self.window_seconds
        if not ticks:
            return self.empty_stats(current_price)

        latest_tick = ticks[-1]
        latest_ts = latest_tick.get("timestamp", 0.0)
        cutoff_ts = latest_ts - win

        window_ticks = [tk for tk in ticks if tk.get("timestamp", 0.0) >= cutoff_ts]
        if not window_ticks:
            return self.empty_stats(current_price or latest_tick.get("price", 0.0))

        curr_p = latest_tick.get("price", current_price)
        start_p = window_ticks[0].get("price", curr_p)

        price_diff = curr_p - start_p
        price_change_bp = (price_diff / start_p * 10000.0) if start_p > 0 else 0.0

        buy_vol = sum(tk.get("size", 0.0) for tk in window_ticks if str(tk.get("side", "")).upper() == "BUY")
        sell_vol = sum(tk.get("size", 0.0) for tk in window_ticks if str(tk.get("side", "")).upper() == "SELL")
        total_vol = buy_vol + sell_vol

        if total_vol <= 1e-9:
            stats = self.empty_stats(curr_p)
            stats["price_change"] = price_diff
            stats["price_change_bp"] = price_change_bp
            stats["tick_count"] = len(window_ticks)
            return stats

        net_delta = buy_vol - sell_vol
        delta_ratio = net_delta / total_vol

        is_2bp_momentum_up = (price_change_bp >= 1.5) and (delta_ratio >= 0.25)
        is_2bp_momentum_down = (price_change_bp <= -1.5) and (delta_ratio <= -0.25)

        dominant_side = "BUY" if net_delta >= 0 else "SELL"
        opposite_side = "SELL" if dominant_side == "BUY" else "BUY"

        opposite_vol = sell_vol if dominant_side == "BUY" else buy_vol
        opposite_noise_ratio = (opposite_vol / total_vol) if total_vol > 0 else 0.0

        dominant_ticks_count = sum(1 for tk in window_ticks if str(tk.get("side", "")).upper() == dominant_side)
        persistence_score = (dominant_ticks_count / len(window_ticks)) if window_ticks else 0.0

        return {
            "current_price": curr_p,
            "window_seconds": win,
            "tick_count": len(window_ticks),
            "price_change": price_diff,
            "price_change_bp": price_change_bp,
            "buy_volume": buy_vol,
            "sell_volume": sell_vol,
            "total_volume": total_vol,
            "net_delta": net_delta,
            "delta_ratio": delta_ratio,
            "is_2bp_momentum_up": is_2bp_momentum_up,
            "is_2bp_momentum_down": is_2bp_momentum_down,
            "persistence_score": persistence_score,
            "opposite_noise_ratio": opposite_noise_ratio,
        }
