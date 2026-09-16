import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="GridMM", version="v1.0", parameters=None):
        default_params = {
            "grid_spacing_atr": 0.8,
            "bb_period": 20,
            "max_bandwidth_pct": 0.015, # 1.5%以内のレンジ相場に限定
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "ボリンジャーバンド幅によるレンジ判定＋ATRグリッドMM。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        bb_p = self.parameters["bb_period"]
        max_bw = self.parameters["max_bandwidth_pct"]
        grid_s = self.parameters["grid_spacing_atr"]

        df["ma"] = df["close"].rolling(window=bb_p).mean()
        df["std"] = df["close"].rolling(window=bb_p).std()
        df["upper"] = df["ma"] + (1.8 * df["std"])
        df["lower"] = df["ma"] - (1.8 * df["std"])
        df["bandwidth"] = (df["upper"] - df["lower"]) / df["ma"]

        # レンジ相場判定
        is_range = df["bandwidth"] <= max_bw

        df["signal"] = np.nan
        long_grid = (df["low"] <= df["lower"]) & is_range
        short_grid = (df["high"] >= df["upper"]) & is_range

        df.loc[long_grid, "signal"] = 1
        df.loc[short_grid, "signal"] = -1

        # 中心回帰利確またはレンジブレイク時のエグジット
        exit_cond = (abs(df["close"] - df["ma"]) / df["ma"] < 0.001) | (~is_range)
        df.loc[exit_cond, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
