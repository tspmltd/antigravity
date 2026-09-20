import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="InventorySkewMM", version="v1.1", parameters=None):
        default_params = {
            "trend_period": 20,
            "skew_factor": 0.3,
            "spread_multiplier": 1.2,
            "min_spread_pct": 0.0025,  # 最低25bpスプレッド確保
            "vol_filter_threshold": 1.3
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "AGENT報告修繕: Avellaneda-Stoikovスキュー＋25bp確保＋100EMA逆張り遮断。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        tp = self.parameters["trend_period"]
        skew_f = self.parameters["skew_factor"]
        spread_m = self.parameters["spread_multiplier"]
        min_spread = self.parameters["min_spread_pct"]

        df["ema_trend"] = df["close"].ewm(span=tp, adjust=False).mean()
        df["trend_slope"] = (df["ema_trend"] - df["ema_trend"].shift(3)) / (df["close"] + 1e-9)

        # 大局トレンド判定 (100 EMA)
        df["ema_macro"] = df["close"].ewm(span=100, adjust=False).mean()
        is_uptrend = df["close"] > df["ema_macro"]
        is_downtrend = df["close"] < df["ema_macro"]

        # ATR
        tr = np.maximum(df["high"] - df["low"], 
                        np.maximum(abs(df["high"] - df["close"].shift(1)), 
                                   abs(df["low"] - df["close"].shift(1))))
        df["atr"] = tr.rolling(window=14).mean()

        df["mid"] = df["close"].rolling(window=8).mean()
        half_spread = np.maximum(df["atr"] * spread_m, df["close"] * (min_spread / 2.0))

        # スキュー調整: 上昇トレンドではBidを浅く(約定しやすく)、Askを深く(利確を大きく)
        skew_offset = df["trend_slope"] * df["close"] * skew_f * 30
        df["bid_limit"] = df["mid"] - half_spread + skew_offset
        df["ask_limit"] = df["mid"] + half_spread + skew_offset

        df["signal"] = np.nan
        # 下降トレンド中の逆張り買い、上昇トレンド中の逆張り売りをブロック
        long_fill = (df["low"] <= df["bid_limit"]) & (~is_downtrend)
        short_fill = (df["high"] >= df["ask_limit"]) & (~is_uptrend)

        df.loc[long_fill, "signal"] = 1
        df.loc[short_fill, "signal"] = -1

        # 中心回帰利確 (即死ガードを廃止し、正規利確へ)
        exit_cond = abs(df["close"] - df["mid"]) < (half_spread * 0.25)
        df.loc[exit_cond, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
