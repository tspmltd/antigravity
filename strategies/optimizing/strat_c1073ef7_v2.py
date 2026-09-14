import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="MacdMomentum", version="v1.0", parameters=None):
        default_params = {
            "fast": 12,
            "slow": 26,
            "signal_period": 9,
            "vol_factor": 1.3
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "MACDクロス＋出来高急増フィルタによるモメンタム順張り。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        fast = self.parameters["fast"]
        slow = self.parameters["slow"]
        sig_p = self.parameters["signal_period"]
        vol_f = self.parameters["vol_factor"]

        ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
        df["macd"] = ema_fast - ema_slow
        df["macd_sig"] = df["macd"].ewm(span=sig_p, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_sig"]

        df["vol_ma"] = df["volume"].rolling(window=20).mean()
        vol_spike = df["volume"] > (df["vol_ma"] * vol_f)

        df["signal"] = 0
        long_cond = (df["macd"] > df["macd_sig"]) & (df["macd"].shift(1) <= df["macd_sig"].shift(1)) & vol_spike
        short_cond = (df["macd"] < df["macd_sig"]) & (df["macd"].shift(1) >= df["macd_sig"].shift(1)) & vol_spike

        df.loc[long_cond, "signal"] = 1
        df.loc[short_cond, "signal"] = -1

        df["signal"] = df["signal"].replace(0, np.nan).ffill().fillna(0).astype(int)
        
        # Optimizer追加: レジームフィルター (200期間SMA立脚判定)
        df["ma_regime"] = df["close"].rolling(window=100).mean()
        trend_up = df["close"] > df["ma_regime"]
        trend_down = df["close"] < df["ma_regime"]
        df.loc[(df["signal"] == 1) & (~trend_up), "signal"] = 0
        df.loc[(df["signal"] == -1) & (~trend_down), "signal"] = 0
        df["signal"] = df["signal"].replace(0, np.nan).ffill().fillna(0).astype(int)
        return df
