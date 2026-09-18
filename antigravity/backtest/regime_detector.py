"""
RegimeDetector: GAPCORE Market Regime Classification
=====================================================
Classifies real-time market micro-conditions into discrete regimes:
- TREND: Directional momentum dominant (favor trend-following, disable mean-reversion)
- RANGE: Mean-reverting, tight spread, low directional drift (favor mean-reversion & OBI)
- HIGH_VOL: Extreme volatility spike / wide spread (halt new entries, protect capital)
"""

from typing import Dict, Any, Tuple


class RegimeDetector:
    def __init__(
        self,
        trend_spread_threshold_bp: float = 1.0,
        trend_ema_diff_bp: float = 1.5,
        range_ema_diff_bp: float = 0.8,
        high_vol_spread_bp: float = 4.0,
        high_vol_bar_range_bp: float = 15.0,
    ):
        self.trend_spread_threshold_bp = trend_spread_threshold_bp
        self.trend_ema_diff_bp = trend_ema_diff_bp
        self.range_ema_diff_bp = range_ema_diff_bp
        self.high_vol_spread_bp = high_vol_spread_bp
        self.high_vol_bar_range_bp = high_vol_bar_range_bp

    def detect(
        self,
        mid_price: float,
        fast_ema: float,
        slow_ema: float,
        spread_bp: float,
        bar_range_bp: float = 0.0,
    ) -> Tuple[str, Dict[str, float]]:
        """
        Detects market regime.
        Returns:
            (regime_name: 'TREND' | 'RANGE' | 'HIGH_VOL' | 'NORMAL',
             weights: Dict[strategy_id, weight_float])
        """
        if mid_price <= 0:
            return "NORMAL", {"EmaTrend": 1.0, "MeanReversion": 1.0, "OrderBookImbalance": 1.0, "GridMM": 1.0}

        # 1. High-Volatility Safety Check
        if spread_bp >= self.high_vol_spread_bp or bar_range_bp >= self.high_vol_bar_range_bp:
            return "HIGH_VOL", {
                "EmaTrend": 0.0,
                "MeanReversion": 0.0,
                "OrderBookImbalance": 0.0,
                "GridMM": 0.0,
            }

        # EMA divergence in basis points
        ema_diff_bp = abs(fast_ema - slow_ema) / mid_price * 10000.0 if mid_price > 0 else 0.0

        # 2. Strong Trend Regime
        if ema_diff_bp >= self.trend_ema_diff_bp:
            return "TREND", {
                "EmaTrend": 1.0,           # Full allocation to Trend
                "MeanReversion": 0.0,      # Disable MeanReversion (prevent getting trampled)
                "OrderBookImbalance": 0.5, # Reduced scalp allocation
                "GridMM": 0.0,             # Disable GridMM (prevent adverse trend drift)
            }

        # 3. Tight Range Regime
        if ema_diff_bp <= self.range_ema_diff_bp and spread_bp <= self.trend_spread_threshold_bp:
            return "RANGE", {
                "EmaTrend": 0.0,           # Disable Trend (avoid whipsaws)
                "MeanReversion": 1.0,      # Full allocation to MeanReversion
                "OrderBookImbalance": 1.0, # Full allocation to OBI scalp
                "GridMM": 1.0,             # Full allocation to GridMM
            }

        # 4. Normal / Transition Regime
        return "NORMAL", {
            "EmaTrend": 0.5,
            "MeanReversion": 0.5,
            "OrderBookImbalance": 1.0,
            "GridMM": 0.5,
        }
