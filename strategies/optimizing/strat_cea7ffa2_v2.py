import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="DonchianBreakout", version="v1.0", parameters=None):
        default_params = {
            "channel_period": 20,
            "exit_period": 10
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "ドンチャンチャネル高値・安値ブレイクアウト順張り。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        p = self.parameters["channel_period"]
        exit_p = self.parameters["exit_period"]

        df["upper"] = df["high"].rolling(window=p).max().shift(1)
        df["lower"] = df["low"].rolling(window=p).min().shift(1)
        df["exit_high"] = df["high"].rolling(window=exit_p).max().shift(1)
        df["exit_low"] = df["low"].rolling(window=exit_p).min().shift(1)

        df["signal"] = 0
        long_entry = df["close"] > df["upper"]
        short_entry = df["close"] < df["lower"]
        long_exit = df["close"] < df["exit_low"]
        short_exit = df["close"] > df["exit_high"]

        df.loc[long_entry, "signal"] = 1
        df.loc[short_entry, "signal"] = -1
        df.loc[long_exit | short_exit, "signal"] = 0

        df["signal"] = df["signal"].replace(0, np.nan).ffill().fillna(0).astype(int)
        
        # Optimizer追加: レジームフィルター (200期間SMA立脚判定)
        df["ma_regime"] = df["close"].rolling(window=100).mean()
        trend_up = df["close"] > df["ma_regime"]
        trend_down = df["close"] < df["ma_regime"]
        df.loc[(df["signal"] == 1) & (~trend_up), "signal"] = 0
        df.loc[(df["signal"] == -1) & (~trend_down), "signal"] = 0
        df["signal"] = df["signal"].replace(0, np.nan).ffill().fillna(0).astype(int)
        return df
