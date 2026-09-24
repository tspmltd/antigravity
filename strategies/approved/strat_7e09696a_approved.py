import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="RsiMeanReversion", version="v1.1", parameters=None):
        default_params = {
            "rsi_period": 14,
            "rsi_oversold": 32,
            "rsi_overbought": 68,
            "bb_period": 20,
            "bb_std": 1.8,
            "max_slope": 0.0004,  # 急騰・急落時の逆張りエントリーを完全ブロック
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "AGENT報告修繕: ボリバン逆張り＋RSI過熱反転＋100EMAトレンド逆行完全ブロック。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        rsi_p = self.parameters["rsi_period"]
        bb_p = self.parameters["bb_period"]
        bb_std = self.parameters["bb_std"]
        max_slope = self.parameters["max_slope"]

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

        # MA傾き (直近3本の変化率)
        df["ma_slope"] = (df["bb_mid"] - df["bb_mid"].shift(3)) / (df["bb_mid"].shift(3) + 1e-9)

        # 大局トレンド判定 (100 EMA)
        df["ema_trend"] = df["close"].ewm(span=100, adjust=False).mean()
        is_uptrend = df["close"] > df["ema_trend"]
        is_downtrend = df["close"] < df["ema_trend"]

        df["signal"] = np.nan
        # 急落中の買い、急騰中の売りをブロック。さらに大局下降トレンド中のロング、大局上昇トレンド中のショートを完全遮断
        long_entry = (
            (df["close"] < df["bb_lower"])
            & (df["rsi"] < self.parameters["rsi_oversold"])
            & (df["ma_slope"] >= -max_slope)
            & (~is_downtrend)
        )
        short_entry = (
            (df["close"] > df["bb_upper"])
            & (df["rsi"] > self.parameters["rsi_overbought"])
            & (df["ma_slope"] <= max_slope)
            & (~is_uptrend)
        )
        exit_cond = abs(df["close"] - df["bb_mid"]) / (df["bb_mid"] + 1e-9) < 0.001

        df.loc[long_entry, "signal"] = 1
        df.loc[short_entry, "signal"] = -1
        # エントリー足を中心回帰で即 0 にすると ffill 後の最終バーが消える
        df.loc[exit_cond & ~long_entry & ~short_entry, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
