import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="MicroSpreadMM", version="v1.1", parameters=None):
        default_params = {
            "spread_multiplier": 1.2,
            "atr_period": 14,
            "min_spread_pct": 0.00020,  # 2.0bp確保 (0.00020 = 0.02% ≒ ¥2,500、平均スプレッド2,126円を克服)
            "vol_filter_threshold": 1.3
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "AGENT報告修繕: 2.0bp最低スプレッド確保＋100EMA大局トレンド逆張り遮断型MM。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        atr_p = self.parameters["atr_period"]
        spread_mult = self.parameters["spread_multiplier"]
        min_spread = self.parameters["min_spread_pct"]
        vol_thresh = self.parameters["vol_filter_threshold"]

        tr = np.maximum(df["high"] - df["low"], 
                        np.maximum(abs(df["high"] - df["close"].shift(1)), 
                                   abs(df["low"] - df["close"].shift(1))))
        df["atr"] = tr.rolling(window=atr_p).mean()
        df["atr_baseline"] = df["atr"].rolling(window=atr_p * 3).mean()

        df["mid"] = df["close"].rolling(window=8).mean()
        raw_spread = df["atr"] * spread_mult
        min_half_spread = df["close"] * (min_spread / 2.0)
        df["half_spread"] = np.maximum(raw_spread, min_half_spread)

        df["bid_limit"] = df["mid"] - df["half_spread"]
        df["ask_limit"] = df["mid"] + df["half_spread"]

        # 大局トレンド判定 (100 EMA)
        df["ema_trend"] = df["close"].ewm(span=100, adjust=False).mean()
        is_uptrend = df["close"] > df["ema_trend"]
        is_downtrend = df["close"] < df["ema_trend"]

        df["signal"] = np.nan
        normal_vol = df["atr"] <= (df["atr_baseline"] * vol_thresh)

        # 下降トレンド中の逆張り買い、上昇トレンド中の逆張り売りをブロック
        long_fill = (df["low"] <= df["bid_limit"]) & normal_vol & (~is_downtrend)
        short_fill = (df["high"] >= df["ask_limit"]) & normal_vol & (~is_uptrend)

        df.loc[long_fill, "signal"] = 1
        df.loc[short_fill, "signal"] = -1

        exit_cond = abs(df["close"] - df["mid"]) < (df["half_spread"] * 0.25)
        df.loc[exit_cond, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
