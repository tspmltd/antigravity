import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="SpreadCaptureMM", version="v1.0", parameters=None):
        default_params = {
            "spread_multiplier": 1.2,
            "atr_period": 14,
            "min_spread_pct": 0.00025,  # 2.5bp確保 (0.00025 = 0.025% ≒ ¥3,100、Lightning FX平均スプレッド2,126円をカバー)
            "vol_filter_threshold": 1.3
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "2.5bp確保型スプレッドキャプチャMM＋ボラ急増回避。"

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

        df["mid"] = df["close"].rolling(window=10).mean()
        raw_spread = df["atr"] * spread_mult
        min_half_spread = df["close"] * (min_spread / 2.0)
        df["half_spread"] = np.maximum(raw_spread, min_half_spread)

        df["bid_limit"] = df["mid"] - df["half_spread"]
        df["ask_limit"] = df["mid"] + df["half_spread"]

        df["signal"] = np.nan
        normal_vol = df["atr"] <= (df["atr_baseline"] * vol_thresh)

        long_fill = (df["low"] <= df["bid_limit"]) & normal_vol
        short_fill = (df["high"] >= df["ask_limit"]) & normal_vol

        df.loc[long_fill, "signal"] = 1
        df.loc[short_fill, "signal"] = -1

        exit_cond = abs(df["close"] - df["mid"]) < (df["half_spread"] * 0.25)
        df.loc[exit_cond, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
