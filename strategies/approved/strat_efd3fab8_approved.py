import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="EmaTrend", version="v1.1", parameters=None):
        default_params = {
            "fast_period": 12,
            "slow_period": 26,
            "atr_period": 14,
            "atr_threshold": 1.0,
            "min_divergence_pct": 0.0006,  # 最低6bpの明確な乖離
            "min_bandwidth_pct": 0.005,    # 狭小レンジでの騙しブレイクを防止
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "AGENT報告修繕: 100EMA大局トレンド整合＋レンジダマシ遮断型EMAトレンド。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        fast = self.parameters["fast_period"]
        slow = self.parameters["slow_period"]
        atr_p = self.parameters["atr_period"]
        min_div = self.parameters["min_divergence_pct"]

        df["ema_fast"] = df["close"].ewm(span=fast, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=slow, adjust=False).mean()
        df["ema_macro"] = df["close"].ewm(span=100, adjust=False).mean()

        # ATR簡易計算
        tr = np.maximum(df["high"] - df["low"], 
                        np.maximum(abs(df["high"] - df["close"].shift(1)), 
                                   abs(df["low"] - df["close"].shift(1))))
        df["atr"] = tr.rolling(window=atr_p).mean()
        df["atr_ma"] = df["atr"].rolling(window=atr_p * 2).mean()

        # ボリンジャーバンド幅によるレンジ検出
        ma20 = df["close"].rolling(window=20).mean()
        std20 = df["close"].rolling(window=20).std()
        bandwidth = (std20 * 4.0) / (ma20 + 1e-9)
        not_tight_range = bandwidth >= self.parameters["min_bandwidth_pct"]

        # EMA乖離幅
        ema_div = abs(df["ema_fast"] - df["ema_slow"]) / (df["close"] + 1e-9)
        has_momentum = ema_div >= min_div

        df["signal"] = 0

        # シグナル判定: 大局EMAとも一致し、レンジ騙しを排除
        long_cond = (
            (df["ema_fast"] > df["ema_slow"])
            & (df["close"] > df["ema_macro"])
            & has_momentum
            & not_tight_range
        )
        short_cond = (
            (df["ema_fast"] < df["ema_slow"])
            & (df["close"] < df["ema_macro"])
            & has_momentum
            & not_tight_range
        )

        vol_filter = df["atr"] >= (df["atr_ma"] * 0.8)

        df.loc[long_cond & vol_filter, "signal"] = 1
        df.loc[short_cond & vol_filter, "signal"] = -1

        # 乖離縮小またはマクロ反転で手仕舞い
        exit_cond = ema_div < (min_div * 0.4)
        df.loc[exit_cond, "signal"] = 0

        # シグナルをフォワードフィルしてポジション維持
        df["signal"] = df["signal"].replace(0, np.nan).ffill().fillna(0).astype(int)
        return df
