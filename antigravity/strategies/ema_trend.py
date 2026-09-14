import time
from typing import Dict, Any, Optional
from .base import BaseTickStrategy


class EmaTrendTickStrategy(BaseTickStrategy):
    """
    【大波EMAトレンドフォロー・ティック戦略】
    - リアルタイムティックから逐次EMA（短期Fast & 長期Slow）を動的算出
    - ゴールデンクロス/デッドクロスとTaker Deltaの方向一致で大波順張りエントリー
    - 30bp〜60bp（約4万〜8万円幅）の大きなトレンドを追随し、トレーリングで利益を最大化
    - トレンド反転クロスまたは防衛ストップで安全に手仕舞い
    """

    def __init__(self, parameters: Optional[Dict[str, Any]] = None, **kwargs):
        default_params = {
            "fast_span_sec": 60.0,        # 短期EMA (約1分足相当: 60秒)
            "slow_span_sec": 300.0,       # 長期EMA (約5分足相当: 300秒)
            "min_delta_ratio": 0.20,       # エントリー時の最小Taker優勢度 (20%)
            "target_trend_bp": 35.0,      # 目標大波利確幅 (+35bp = 0.35%)
            "trail_trigger_bp": 20.0,     # トレーリング発動閾値 (+20bp)
            "trail_distance_bp": 10.0,    # 最高値からのトレーリング許容幅 (10bp)
            "stop_loss_bp": 12.0,         # 逆行防衛ストップ (12bp)
        }
        if parameters:
            default_params.update(parameters)
        if kwargs:
            default_params.update(kwargs)

        super().__init__(name="EmaTrendTick", version="v1.0", parameters=default_params)
        self.strategy_type = "trend_following"

        # 逐次EMA計算用内部ステート
        self.fast_ema: Optional[float] = None
        self.slow_ema: Optional[float] = None
        self.last_tick_time: float = 0.0
        self.highest_return_bp: float = 0.0

    def _update_emas(self, price: float, current_ts: float):
        """時間加重による逐次EMA更新"""
        if self.fast_ema is None or self.slow_ema is None:
            self.fast_ema = price
            self.slow_ema = price
            self.last_tick_time = current_ts
            return

        dt = max(0.1, current_ts - self.last_tick_time)
        self.last_tick_time = current_ts

        # 連続時間EMA減衰率: alpha = 1 - exp(-dt / span)
        # 近似式: alpha = dt / span
        alpha_fast = min(1.0, dt / self.parameters["fast_span_sec"])
        alpha_slow = min(1.0, dt / self.parameters["slow_span_sec"])

        self.fast_ema = (1.0 - alpha_fast) * self.fast_ema + alpha_fast * price
        self.slow_ema = (1.0 - alpha_slow) * self.slow_ema + alpha_slow * price

    def on_tick(
        self,
        tick: Dict[str, Any],
        flow_stats: Dict[str, Any],
        current_pos: float,
        entry_price: float,
    ) -> Dict[str, Any]:
        curr_p = tick.get("price", 0.0)
        now_ts = tick.get("timestamp", time.time())
        if curr_p <= 0:
            return {"action": "HOLD", "reason": "無効価格"}

        # EMAの逐次更新
        self._update_emas(curr_p, now_ts)
        delta_ratio = flow_stats.get("delta_ratio", 0.0)

        # --------------------------------------------------------
        # 1. ポジション保有中の管理（大波追随・トレーリング・反転EXIT）
        # --------------------------------------------------------
        if current_pos != 0 and entry_price > 0:
            is_long = current_pos > 0
            ret_bp = ((curr_p - entry_price) / entry_price * 10000.0) * (1.0 if is_long else -1.0)

            # 最高益（ピーク）の更新
            if ret_bp > self.highest_return_bp:
                self.highest_return_bp = ret_bp

            # ① 目標大波到達利確 (+35bp以上)
            if ret_bp >= self.parameters["target_trend_bp"]:
                self.highest_return_bp = 0.0
                return {
                    "action": "EXIT",
                    "reason": f"大波EMAトレンド目標到達利確 (+{ret_bp:.1f}bp)",
                    "target_price": None,
                }

            # ② トレーリングストップ (+20bp到達後、ピークから10bp落ちたら利確保護)
            if self.highest_return_bp >= self.parameters["trail_trigger_bp"]:
                trail_drawdown = self.highest_return_bp - ret_bp
                if trail_drawdown >= self.parameters["trail_distance_bp"]:
                    self.highest_return_bp = 0.0
                    return {
                        "action": "EXIT",
                        "reason": f"EMAトレーリング利確保護 (ピーク:+{self.highest_return_bp:.1f}bp -> 現在:+{ret_bp:.1f}bp)",
                        "target_price": None,
                    }

            # ③ トレンド反転クロスによるEXIT
            if is_long and self.fast_ema < self.slow_ema:
                self.highest_return_bp = 0.0
                return {
                    "action": "EXIT",
                    "reason": f"EMAデッドクロス反転手仕舞い (損益:{ret_bp:+.1f}bp)",
                    "target_price": None,
                }
            elif not is_long and self.fast_ema > self.slow_ema:
                self.highest_return_bp = 0.0
                return {
                    "action": "EXIT",
                    "reason": f"EMAゴールデンクロス反転手仕舞い (損益:{ret_bp:+.1f}bp)",
                    "target_price": None,
                }

            # ④ 防衛ストップ
            if ret_bp <= -self.parameters["stop_loss_bp"]:
                self.highest_return_bp = 0.0
                return {
                    "action": "EXIT",
                    "reason": f"大波防衛ストップ損切 ({ret_bp:.1f}bp)",
                    "target_price": None,
                }

            return {"action": "HOLD", "reason": f"大波トレンド追随中 ({ret_bp:+.1f}bp)"}

        # --------------------------------------------------------
        # 2. ノーポジション時のエントリー（EMAクロス ＆ Taker Delta一致）
        # --------------------------------------------------------
        if current_pos == 0:
            self.highest_return_bp = 0.0
            min_delta = self.parameters["min_delta_ratio"]

            # 買い条件: 短期EMA > 長期EMA かつ Taker買い優勢
            if self.fast_ema > self.slow_ema and delta_ratio >= min_delta:
                spread_bp = (self.fast_ema - self.slow_ema) / self.slow_ema * 10000.0
                if spread_bp >= 0.5:  # 明確な乖離 (0.5bp以上)
                    return {
                        "action": "BUY",
                        "reason": f"EMA上昇トレンド順張り (Fast-Slow:{spread_bp:+.1f}bp, Delta:{delta_ratio:+.2f})",
                        "target_price": None,
                    }

            # 売り条件: 短期EMA < 長期EMA かつ Taker売り優勢
            elif self.fast_ema < self.slow_ema and delta_ratio <= -min_delta:
                spread_bp = (self.fast_ema - self.slow_ema) / self.slow_ema * 10000.0
                if spread_bp <= -0.5:
                    return {
                        "action": "SELL",
                        "reason": f"EMA下降トレンド順張り (Fast-Slow:{spread_bp:+.1f}bp, Delta:{delta_ratio:+.2f})",
                        "target_price": None,
                    }

        return {"action": "HOLD", "reason": "EMAトレンド待機"}
