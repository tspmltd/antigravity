import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="RsiMeanReversion", version="v1.0", parameters=None):
        default_params = {
            "rsi_period": 14,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "bb_period": 20,
            "bb_std": 2.0
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "ボリンジャーバンド逆張り＋RSIの売られすぎ・買われすぎ反転を検知。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        rsi_p = self.parameters["rsi_period"]
        bb_p = self.parameters["bb_period"]
        bb_std = self.parameters["bb_std"]

        # RSI計算
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=rsi_p).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_p).mean()
        rs = gain / (loss + 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))

        # Bollinger Bands
        df["bb_mid"] = df["close"].rolling(window=bb_p).mean()
        df["bb_dev"] = df["close"].rolling(window=bb_p).std()
        df["bb_upper"] = df["bb_mid"] + (bb_std * df["bb_dev"])
        df["bb_lower"] = df["bb_mid"] - (bb_std * df["bb_dev"])

        df["signal"] = np.nan
        long_entry = (df["close"] < df["bb_lower"]) & (df["rsi"] < self.parameters["rsi_oversold"])
        short_entry = (df["close"] > df["bb_upper"]) & (df["rsi"] > self.parameters["rsi_overbought"])
        exit_cond = abs(df["close"] - df["bb_mid"]) / df["bb_mid"] < 0.002

        df.loc[long_entry, "signal"] = 1
        df.loc[short_entry, "signal"] = -1
        df.loc[exit_cond, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
