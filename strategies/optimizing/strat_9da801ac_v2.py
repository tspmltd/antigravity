import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="InventorySkewMM", version="v1.0", parameters=None):
        default_params = {
            "trend_period": 20,
            "skew_factor": 0.5,
            "spread_multiplier": 1.0,
            "min_spread_pct": 0.0020
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "Avellaneda-Stoikov理論着想: トレンド偏向スキューMM戦略。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        tp = self.parameters["trend_period"]
        skew_f = self.parameters["skew_factor"]
        spread_m = self.parameters["spread_multiplier"]
        min_spread = self.parameters["min_spread_pct"]

        df["ema_trend"] = df["close"].ewm(span=tp, adjust=False).mean()
        df["trend_slope"] = (df["ema_trend"] - df["ema_trend"].shift(3)) / (df["close"] + 1e-9)

        # ATR
        tr = np.maximum(df["high"] - df["low"], 
                        np.maximum(abs(df["high"] - df["close"].shift(1)), 
                                   abs(df["low"] - df["close"].shift(1))))
        df["atr"] = tr.rolling(window=14).mean()

        df["mid"] = df["close"].rolling(window=8).mean()
        half_spread = np.maximum(df["atr"] * spread_m, df["close"] * (min_spread / 2.0))

        # スキュー調整: 上昇トレンドではBidを浅く(約定しやすく)、Askを深く(利確を大きく)
        skew_offset = df["trend_slope"] * df["close"] * skew_f * 50
        df["bid_limit"] = df["mid"] - half_spread + skew_offset
        df["ask_limit"] = df["mid"] + half_spread + skew_offset

        df["signal"] = np.nan
        long_fill = df["low"] <= df["bid_limit"]
        short_fill = df["high"] >= df["ask_limit"]

        df.loc[long_fill, "signal"] = 1
        df.loc[short_fill, "signal"] = -1

        # 逆トレンド加速時の手仕舞いガード
        exit_guard = (df["trend_slope"] < -0.001) | (df["trend_slope"] > 0.001)
        df.loc[exit_guard, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        
        # Optimizer追加: レジームフィルター (200期間SMA立脚判定)
        df["ma_regime"] = df["close"].rolling(window=100).mean()
        trend_up = df["close"] > df["ma_regime"]
        trend_down = df["close"] < df["ma_regime"]
        df.loc[(df["signal"] == 1) & (~trend_up), "signal"] = 0
        df.loc[(df["signal"] == -1) & (~trend_down), "signal"] = 0
        df["signal"] = df["signal"].replace(0, np.nan).ffill().fillna(0).astype(int)
        return df
