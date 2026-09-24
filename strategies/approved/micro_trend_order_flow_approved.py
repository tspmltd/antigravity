import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    """
    AGENT報告修繕: 4bp初動モメンタム ＋ Takerフロー継続 ＋ 100EMA大局整合マイクロトレンド。
    """
    def __init__(self, name="MicroTrendOrderFlow", version="v1.1", parameters=None):
        default_params = {
            "micro_mom_window": 4,         # 4期間のモメンタム
            "flow_persistence_bars": 3,    # 同方向フロー継続本数
            "reverse_noise_max": 0.30,     # 逆方向ノイズ許容
            "min_mom_pct": 0.0004,         # 4bp以上の明確な初動 (スプレッド2bp超を確実に克服)
            "target_bp": 15.0,             # ターゲットトレンド幅
            "trail_stop_bp": 4.0           # トレーリングストップ幅
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.strategy_type = "micro_trend"
        self.hypothesis = "AGENT報告修繕: 4bp初動＋Takerフロー継続＋100EMA整合でスプレッド耐性を獲得。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        win = self.parameters["micro_mom_window"]
        p_bars = self.parameters["flow_persistence_bars"]
        noise_max = self.parameters["reverse_noise_max"]
        min_mom = self.parameters["min_mom_pct"]

        # 1. 微小リターン計算
        df["ret_1"] = df["close"].pct_change()
        df["ret_win"] = df["close"].pct_change(win)

        # 2. Approximate Volume Delta
        # 形成中足・同時刻 doji は range=0。旧実装は replace(0, 1e-9) で
        # close_pos=0 → delta_ratio=-1（偽の最大売り）になり、最終バーの ±1 を reverse_exit で消していた。
        candle_range = (df["high"] - df["low"]).astype(float)
        denom = candle_range.replace(0, np.nan)
        close_pos = ((df["close"].astype(float) - df["low"].astype(float)) / denom).fillna(0.5).to_numpy()
        df["delta_ratio"] = (close_pos - 0.5) * 2.0

        # 3. 大局トレンド判定 (100 EMA)
        df["ema_macro"] = df["close"].ewm(span=100, adjust=False).mean()
        is_uptrend = df["close"] > df["ema_macro"]
        is_downtrend = df["close"] < df["ema_macro"]

        # 4. フロー継続性 (過去N本のDeltaが同方向か)
        df["positive_flow_run"] = (df["delta_ratio"] > 0.15).rolling(window=p_bars).sum()
        df["negative_flow_run"] = (df["delta_ratio"] < -0.15).rolling(window=p_bars).sum()

        # 5. 逆方向ノイズの少なさ
        df["noise_ratio_buy"] = (df["ret_1"] < 0).rolling(window=win).mean()
        df["noise_ratio_sell"] = (df["ret_1"] > 0).rolling(window=win).mean()

        # 6. エントリーシグナル: 4bp初動 ＋ フロー継続 ＋ ノイズ極小 ＋ 大局トレンド一致
        long_trigger = (
            (df["ret_win"] >= min_mom) &
            (df["positive_flow_run"] >= (p_bars - 1)) &
            (df["noise_ratio_buy"] <= noise_max) &
            is_uptrend
        )

        short_trigger = (
            (df["ret_win"] <= -min_mom) &
            (df["negative_flow_run"] >= (p_bars - 1)) &
            (df["noise_ratio_sell"] <= noise_max) &
            is_downtrend
        )

        df["signal"] = np.nan
        df.loc[long_trigger, "signal"] = 1
        df.loc[short_trigger, "signal"] = -1

        # 反対方向の強烈なフロー流入時のみエグジット
        reverse_exit = (
            (df["delta_ratio"].abs() > 0.8) &
            ((df["delta_ratio"] * df["signal"].ffill()) < -0.5)
        )
        df.loc[reverse_exit, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
