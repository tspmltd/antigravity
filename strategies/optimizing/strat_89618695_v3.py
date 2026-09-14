import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="MarketMaking", version="v1.0", parameters=None):
        default_params = {
            "spread_multiplier": 0.7,
            "atr_period": 10,
            "max_inventory": 3,
            "vol_filter_threshold": 1.2
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "微小スプレッドキャプチャ＋在庫偏りスキュー＋ボラティリティ急変フィルター。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        atr_p = self.parameters["atr_period"]
        spread_mult = self.parameters["spread_multiplier"]
        vol_thresh = self.parameters["vol_filter_threshold"]

        # ATRと短期ボラティリティ
        tr = np.maximum(df["high"] - df["low"], 
                        np.maximum(abs(df["high"] - df["close"].shift(1)), 
                                   abs(df["low"] - df["close"].shift(1))))
        df["atr"] = tr.rolling(window=atr_p).mean()
        df["atr_baseline"] = df["atr"].rolling(window=atr_p * 3).mean()

        # 微小チャネル
        df["mid"] = df["close"].rolling(window=5).mean()
        df["half_spread"] = df["atr"] * spread_mult
        df["bid_limit"] = df["mid"] - df["half_spread"]
        df["ask_limit"] = df["mid"] + df["half_spread"]

        df["signal"] = np.nan

        # 急激なボラティリティ急増時はMMを一時停止 (ダウントレンド/アップトレンド被弾防止)
        normal_vol = df["atr"] <= (df["atr_baseline"] * vol_thresh)

        # 指値タッチ判定
        long_fill = (df["low"] <= df["bid_limit"]) & normal_vol
        short_fill = (df["high"] >= df["ask_limit"]) & normal_vol

        # 反転決済ロジック
        df.loc[long_fill, "signal"] = 1
        df.loc[short_fill, "signal"] = -1

        # 中心値近辺で早めの利確
        exit_cond = abs(df["close"] - df["mid"]) < (df["half_spread"] * 0.3)
        df.loc[exit_cond, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        
        # Optimizer追加: レジームフィルター (200期間SMA立脚判定)
        df["ma_regime"] = df["close"].rolling(window=100).mean()
        trend_up = df["close"] > df["ma_regime"]
        trend_down = df["close"] < df["ma_regime"]
        df.loc[(df["signal"] == 1) & (~trend_up), "signal"] = 0
        df.loc[(df["signal"] == -1) & (~trend_down), "signal"] = 0
        df["signal"] = df["signal"].replace(0, np.nan).ffill().fillna(0).astype(int)
        
        # Optimizer追加: レジームフィルター (200期間SMA立脚判定)
        df["ma_regime"] = df["close"].rolling(window=100).mean()
        trend_up = df["close"] > df["ma_regime"]
        trend_down = df["close"] < df["ma_regime"]
        df.loc[(df["signal"] == 1) & (~trend_up), "signal"] = 0
        df.loc[(df["signal"] == -1) & (~trend_down), "signal"] = 0
        df["signal"] = df["signal"].replace(0, np.nan).ffill().fillna(0).astype(int)
        return df
