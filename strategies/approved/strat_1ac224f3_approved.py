import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="GridMM", version="v1.1", parameters=None):
        default_params = {
            "grid_spacing_atr": 1.0,
            "bb_period": 20,
            "bb_std": 2.0,  # 2.0σへ安全化
            "max_bandwidth_pct": 0.018,
            "min_spread_pct": 0.00020,  # 最低2.0bpスプレッド確保 (0.00020 = 0.02% ≒ ¥2,500)
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "AGENT報告修繕: 100EMA大局トレンド遮断＋2.0σ外側グリッドMM。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        bb_p = self.parameters["bb_period"]
        bb_std = self.parameters["bb_std"]
        max_bw = self.parameters["max_bandwidth_pct"]

        df["ma"] = df["close"].rolling(window=bb_p).mean()
        df["std"] = df["close"].rolling(window=bb_p).std()
        df["upper"] = df["ma"] + (bb_std * df["std"])
        df["lower"] = df["ma"] - (bb_std * df["std"])
        df["bandwidth"] = (df["upper"] - df["lower"]) / (df["ma"] + 1e-9)

        # 大局トレンド判定 (100 EMA)
        df["ema_trend"] = df["close"].ewm(span=100, adjust=False).mean()
        is_uptrend = df["close"] > df["ema_trend"]
        is_downtrend = df["close"] < df["ema_trend"]

        # レンジ相場判定
        is_range = df["bandwidth"] <= max_bw

        df["signal"] = np.nan
        # 下降トレンド中の逆張り買い、上昇トレンド中の逆張り売りをブロック
        long_grid = (df["low"] <= df["lower"]) & is_range & (~is_downtrend)
        short_grid = (df["high"] >= df["upper"]) & is_range & (~is_uptrend)

        df.loc[long_grid, "signal"] = 1
        df.loc[short_grid, "signal"] = -1

        # 中心回帰利確またはレンジ崩壊時の安全エグジット
        exit_cond = (abs(df["close"] - df["ma"]) / (df["ma"] + 1e-9) < 0.0005) | (df["bandwidth"] > max_bw * 1.5)
        df.loc[exit_cond, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
