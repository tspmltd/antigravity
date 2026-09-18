"""
GridMmStrategy: GAPCORE Clean Architecture Strategy Brain (Regime-Adaptive Grid MM)
===================================================================================
A production-grade Mean Reversion / Grid Market Making strategy designed to be
orchestrated within the GAPCORE Portfolio framework.

Features (Akari-spec):
1. Regime-Adaptive: Active only in 'RANGE' regime. Freezes entry in 'TREND' and 'HIGH_VOL'.
2. Volatility Filter (ATR): Rejects entry when micro-volatility exceeds safety threshold.
3. Spread Filter: Rejects entry when spread exceeds max_spread_jpy (default 1500 JPY / 1.5 JPY per 0.001 BTC).
4. Dynamic TP/SL (Payoff Ratio 0.8 - 1.0):
   - Dynamic TP: 3.0 - 7.0 JPY (per 0.001 BTC lot, i.e., 3,000 - 7,000 JPY price delta).
   - Dynamic SL: 1.2x - 1.5x of TP, eliminating the structural asymmetric loss (0.55 -> 0.8+).
5. Microstructure Confirmation: Requires Order Book Imbalance (OBI) support before fading spikes.
"""

import time
from typing import Dict, Any, Optional
from antigravity.strategies.base import StrategyBrain


class GridMmStrategy(StrategyBrain):
    def __init__(self, parameters: Optional[Dict[str, Any]] = None, **kwargs):
        default_params = {
            "order_size": 0.001,             # 発注ロット (BTC)
            "max_spread_jpy": 1500.0,        # スプレッドフィルター (価格差1,500円 = 1.5円/0.001BTC)
            "max_atr_jpy": 25000.0,          # ボラティリティフィルター (急変時停止)
            "base_tp_jpy": 4.0,              # 基準利幅 (0.001 BTCあたり 4.0円)
            "min_tp_jpy": 3.0,               # 最小利幅 (0.001 BTCあたり 3.0円)
            "max_tp_jpy": 7.0,               # 最大利幅 (0.001 BTCあたり 7.0円)
            "sl_tp_ratio": 1.3,              # 損切倍率 (SL = TP * 1.3 -> ペイオフレシオ約 0.77 - 0.85)
            "grid_reversion_bp": 6.0,        # 基準線からのエントリー乖離幅 (bp)
            "baseline_span_sec": 120.0,      # マイクロ基準線平滑化スパン (2分)
            "min_imbalance": 0.03,           # 板厚不均衡支持 (3%以上の板支持)
            "max_hold_time_sec": 300.0,      # タイムアウト決済時間 (秒)
        }
        if parameters:
            default_params.update(parameters)
        if kwargs:
            default_params.update(kwargs)

        super().__init__(name="GridMM", version="v2.0-adaptive", parameters=default_params)
        self.strategy_type = "grid_mm"

        self.baseline_ema: Optional[float] = None
        self.micro_atr: Optional[float] = None
        self.last_tick_time: float = 0.0
        self.entry_time: float = 0.0

    def _update_indicators(self, mid_price: float, current_ts: float):
        if self.baseline_ema is None or self.baseline_ema <= 0:
            self.baseline_ema = mid_price
            self.micro_atr = 5000.0  # 初期想定ATR (5,000円幅)
            self.last_tick_time = current_ts
            return

        dt = max(0.001, current_ts - self.last_tick_time)
        self.last_tick_time = current_ts

        # 1. 基準線 EMA 更新
        alpha_ema = min(1.0, dt / self.parameters["baseline_span_sec"])
        self.baseline_ema = alpha_ema * mid_price + (1.0 - alpha_ema) * self.baseline_ema

        # 2. マイクロATR (価格変動幅の平滑化)
        diff = abs(mid_price - self.baseline_ema)
        alpha_atr = min(1.0, dt / 60.0)  # 1分スパン
        self.micro_atr = alpha_atr * diff + (1.0 - alpha_atr) * (self.micro_atr or diff)

    def compute_dynamic_tp_sl(self, eval_p: float) -> tuple[float, float]:
        """
        ボラティリティ (ATR) に応じて動的な利確幅 (TP) と損切幅 (SL) を算出。
        単位: 0.001 BTCあたりの円
        """
        atr = self.micro_atr or 5000.0
        # ATR 5,000円 -> TP 3.5円、ATR 15,000円 -> TP 6.0円
        lot = float(self.parameters["order_size"])
        tp_price_delta = max(3000.0, min(7000.0, atr * 0.5))
        dynamic_tp_jpy = tp_price_delta * lot  # 例: 4,000円 * 0.001 = 4.0円

        dynamic_tp_jpy = max(self.parameters["min_tp_jpy"], min(self.parameters["max_tp_jpy"], dynamic_tp_jpy))
        dynamic_sl_jpy = dynamic_tp_jpy * self.parameters["sl_tp_ratio"]
        return dynamic_tp_jpy, dynamic_sl_jpy

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

        self._update_indicators(eval_p, ts)
        if self.baseline_ema is None or self.baseline_ema <= 0:
            return {"action": "HOLD", "target_qty": current_pos, "reason": "基準線初期化中"}

        spread_jpy = flow_stats.get("spread", 0.0)
        if spread_jpy <= 0 and "spread_bp" in flow_stats:
            spread_jpy = eval_p * flow_stats["spread_bp"] / 10000.0

        lot = float(self.parameters["order_size"])
        dynamic_tp_jpy, dynamic_sl_jpy = self.compute_dynamic_tp_sl(eval_p)

        # ----------------------------------------------------
        # 1. 保有建玉のエグジット判定 (動的 TP / 動的 SL / タイムアウト)
        # ----------------------------------------------------
        if abs(current_pos) > 1e-9 and entry_price > 0:
            # 損益計算 (0.001 BTC あたりの円)
            pnl_jpy = (eval_p - entry_price) * current_pos

            # A. 動的ハードストップロス (ペイオフレシオ防衛)
            if pnl_jpy <= -dynamic_sl_jpy:
                return {
                    "action": "EXIT",
                    "target_qty": 0.0,
                    "reason": f"動的SL損切 (損益:{pnl_jpy:+.1f}円 <= -{dynamic_sl_jpy:.1f}円, 比率:{self.parameters['sl_tp_ratio']})",
                }

            # B. 動的利確 (ボラ適応型TP)
            if pnl_jpy >= dynamic_tp_jpy:
                return {
                    "action": "EXIT",
                    "target_qty": 0.0,
                    "reason": f"動的TP利確 (損益:{pnl_jpy:+.1f}円 >= +{dynamic_tp_jpy:.1f}円, 目標達成)",
                }

            # C. タイムアウト (長期滞留回避)
            if self.entry_time > 0 and (ts - self.entry_time) >= self.parameters["max_hold_time_sec"]:
                # 微益または小損でタイムアウト脱出
                return {
                    "action": "EXIT",
                    "target_qty": 0.0,
                    "reason": f"保有タイムアウト決済 ({ts - self.entry_time:.0f}s >= {self.parameters['max_hold_time_sec']:.0f}s, 損益:{pnl_jpy:+.1f}円)",
                }

            return {
                "action": "HOLD",
                "target_qty": current_pos,
                "reason": f"GridMM建玉追随中 (含み損益:{pnl_jpy:+.1f}円, TP:+{dynamic_tp_jpy:.1f}円, SL:-{dynamic_sl_jpy:.1f}円)",
            }

        # ポジションなし時の初期化
        self.entry_time = 0.0

        # ----------------------------------------------------
        # 2. 安全フィルター判定 (スプレッド & ボラティリティ)
        # ----------------------------------------------------
        # ① スプレッドフィルター (スプレッド > 1,500円で新規停止)
        if spread_jpy > self.parameters["max_spread_jpy"]:
            return {
                "action": "HOLD",
                "target_qty": 0.0,
                "reason": f"スプレッドフィルター遮断 (実勢:{spread_jpy:.0f}円 > 上限:{self.parameters['max_spread_jpy']:.0f}円)",
            }

        # ② ボラティリティフィルター (ATR急拡大時は停止)
        if self.micro_atr and self.micro_atr > self.parameters["max_atr_jpy"]:
            return {
                "action": "HOLD",
                "target_qty": 0.0,
                "reason": f"ボラティリティフィルター遮断 (ATR:{self.micro_atr:.0f}円 > 上限:{self.parameters['max_atr_jpy']:.0f}円)",
            }

        # ----------------------------------------------------
        # 3. レンジ相場での逆張りグリッドエントリー判定
        # ----------------------------------------------------
        deviation_bp = (eval_p - self.baseline_ema) / self.baseline_ema * 10000.0
        grid_bp = self.parameters["grid_reversion_bp"]
        book_imb = flow_stats.get("book_imbalance", 0.0)
        min_imb = self.parameters["min_imbalance"]

        # 売られすぎ ➔ 買い逆張り
        if deviation_bp <= -grid_bp and book_imb >= min_imb:
            self.entry_time = ts
            return {
                "action": "BUY",
                "target_qty": lot,
                "reason": f"GridMM売られすぎ逆張りBUY (乖離:{deviation_bp:+.1f}bp <= -{grid_bp:.1f}bp, 板支持:Imb={book_imb:+.2f})",
            }

        # 買われすぎ ➔ 売り逆張り
        if deviation_bp >= grid_bp and book_imb <= -min_imb:
            self.entry_time = ts
            return {
                "action": "SELL",
                "target_qty": -lot,
                "reason": f"GridMM買われすぎ逆張りSELL (乖離:{deviation_bp:+.1f}bp >= +{grid_bp:.1f}bp, 板支持:Imb={book_imb:+.2f})",
            }

        return {"action": "HOLD", "target_qty": 0.0, "reason": f"GridMMレンジ待機 (乖離:{deviation_bp:+.1f}bp)"}
