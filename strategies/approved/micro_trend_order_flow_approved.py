import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    """
    Taker約定差分量 (Order Flow Delta) ＆ 2bp初動→10〜20bp追随マイクロトレンド戦略。
    - 2bp微小リターンの初動を捉える
    - Taker約定差分量(Delta)が同方向に継続し、逆方向の動きが少ない局面で順張り
    - 10〜20bpの波まで利益を伸ばし、反対フローが入ったら素早く撤退
    """
    def __init__(self, name="MicroTrendOrderFlow", version="v1.0", parameters=None):
        default_params = {
            "micro_mom_window": 3,         # 3期間の微小モメンタム (2bp初動)
            "flow_persistence_bars": 3,    # 同方向フロー継続本数
            "reverse_noise_max": 0.25,     # 逆方向ノイズ上限 (25%以下)
            "target_bp": 15.0,             # ターゲットトレンド幅 (15bp = 0.15%)
            "trail_stop_bp": 4.0           # トレーリングストップ幅 (4bp = 0.04%)
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.strategy_type = "micro_trend"
        self.hypothesis = "Taker約定差分継続＆逆方向僅少時の2bp順張り→10-20bp追随マイクロトレンド。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        win = self.parameters["micro_mom_window"]
        p_bars = self.parameters["flow_persistence_bars"]
        noise_max = self.parameters["reverse_noise_max"]

        # 1. 2bp微小リターン計算 (0.02% = 0.0002)
        df["ret_1"] = df["close"].pct_change()
        df["ret_win"] = df["close"].pct_change(win)

        # 2. Approximate Volume Delta (ローソク足構造からTakerフローを推定)
        candle_range = (df["high"] - df["low"]).replace(0, 1e-9)
        # 終値がレンジのどこに位置するか (0.0: 最安値引け, 1.0: 最高値引け)
        close_pos = (df["close"] - df["low"]) / candle_range
        # 0.5を中心に -1.0 〜 +1.0 に正規化 (大陽線なら+1.0、大陰線なら-1.0)
        df["delta_ratio"] = (close_pos - 0.5) * 2.0

        # 3. フロー継続性 (過去N本のDeltaが同方向か)
        df["positive_flow_run"] = (df["delta_ratio"] > 0.2).rolling(window=p_bars).sum()
        df["negative_flow_run"] = (df["delta_ratio"] < -0.2).rolling(window=p_bars).sum()

        # 4. 逆方向ノイズの少なさ (逆方向の足の割合)
        df["noise_ratio_buy"] = (df["ret_1"] < 0).rolling(window=win).mean()
        df["noise_ratio_sell"] = (df["ret_1"] > 0).rolling(window=win).mean()

        # 5. エントリーシグナル:
        # 2bp以上の初動モメンタム ＋ Takerフロー継続 ＋ 逆方向が少ない
        long_trigger = (
            (df["ret_win"] >= 0.0002) &               # 2bp以上の初動
            (df["positive_flow_run"] >= (p_bars - 1)) & # フロー継続
            (df["noise_ratio_buy"] <= noise_max)       # 逆方向ノイズ極小
        )

        short_trigger = (
            (df["ret_win"] <= -0.0002) &              # -2bp以上の初動
            (df["negative_flow_run"] >= (p_bars - 1)) &
            (df["noise_ratio_sell"] <= noise_max)
        )

        df["signal"] = np.nan
        df.loc[long_trigger, "signal"] = 1
        df.loc[short_trigger, "signal"] = -1

        # 反対方向の強いフロー流入時は即座にエグジット (Cancel/Exit)
        reverse_exit = (
            (df["delta_ratio"].abs() > 0.6) &
            ((df["delta_ratio"] * df["signal"].ffill()) < -0.3)
        )
        df.loc[reverse_exit, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
