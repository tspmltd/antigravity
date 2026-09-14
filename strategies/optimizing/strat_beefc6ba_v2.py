import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="EmaTrend", version="v1.0", parameters=None):
        default_params = {
            "fast_period": 10,
            "slow_period": 30,
            "atr_period": 14,
            "atr_threshold": 1.2
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "短期EMAが長期EMAを上抜いた際に順張りエントリー。ATRフィルターでボラティリティを考慮。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        fast = self.parameters["fast_period"]
        slow = self.parameters["slow_period"]
        atr_p = self.parameters["atr_period"]

        df["ema_fast"] = df["close"].ewm(span=fast, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=slow, adjust=False).mean()

        # ATR簡易計算
        tr = np.maximum(df["high"] - df["low"], 
                        np.maximum(abs(df["high"] - df["close"].shift(1)), 
                                   abs(df["low"] - df["close"].shift(1))))
        df["atr"] = tr.rolling(window=atr_p).mean()
        df["atr_ma"] = df["atr"].rolling(window=atr_p * 2).mean()

        df["signal"] = 0

        # シグナル判定 (前足の値で判断しルックアヘッドを防ぐ)
        long_cond = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
        short_cond = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
        
        # ATRによるボラティリティ十分条件
        vol_filter = df["atr"] >= (df["atr_ma"] * 0.8)

        df.loc[long_cond & vol_filter, "signal"] = 1
        df.loc[short_cond & vol_filter, "signal"] = -1

        # シグナルをフォワードフィルしてポジション維持
        df["signal"] = df["signal"].replace(0, np.nan).ffill().fillna(0).astype(int)
        
        # Optimizer追加: レジームフィルター (200期間SMA立脚判定)
        df["ma_regime"] = df["close"].rolling(window=100).mean()
        trend_up = df["close"] > df["ma_regime"]
        trend_down = df["close"] < df["ma_regime"]
        df.loc[(df["signal"] == 1) & (~trend_up), "signal"] = 0
        df.loc[(df["signal"] == -1) & (~trend_down), "signal"] = 0
        df["signal"] = df["signal"].replace(0, np.nan).ffill().fillna(0).astype(int)
        return df
