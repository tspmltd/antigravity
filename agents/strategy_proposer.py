import os
import uuid
from typing import Dict, Any, Optional
from agents.base_agent import BaseAgent


SYSTEM_PROMPT = """あなたは定量クオンツ・自動売買戦略の設計を専門とするエキスパート【Strategy Proposer】です。
市場仮説に基づき、BaseStrategyを継承した実行可能なPythonコードを生成してください。

【制約事項】
1. 必ず `from core.base_strategy import BaseStrategy` をインポートし、`BaseStrategy` のサブクラスを作成すること。
2. クラス名は `CustomStrategy` とすること。
3. `generate_signals(self, df: pd.DataFrame) -> pd.DataFrame` を実装すること。
4. 返却するDataFrameには必ず 1 (買い), -1 (売り), 0 (中立/手仕舞い) の整数値を取る 'signal' 列を含めること。
5. ルックアヘッドバイアス（未来データの参照）を絶対に排除すること（例: shift(1) やローリングウィンドウを適切に使用）。
6. コードは ```python ... ``` ブロックで出力すること。
"""

SAMPLE_STRATEGIES = [
    {
        "theme": "Trend Following (EMA Cross + ATR Filter)",
        "name": "EmaTrendStrategy",
        "hypothesis": "短期EMAが長期EMAを上抜いた際に順張りエントリー。高ボラティリティ時のダマシをATRフィルターで除外する。",
        "code": """import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="EmaTrend", version="v1.0", parameters=None):
        default_params = {
            "fast_period": 12,
            "slow_period": 26,
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
        return df
"""
    },
    {
        "theme": "Mean Reversion (RSI + Bollinger Bands)",
        "name": "RsiMeanReversionStrategy",
        "hypothesis": "価格がボリンジャーバンド下限を下回り、かつRSIが売られすぎ水準に達した際の反発を狙う。",
        "code": """import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="RsiMeanReversion", version="v1.0", parameters=None):
        default_params = {
            "rsi_period": 14,
            "rsi_oversold": 35,
            "rsi_overbought": 65,
            "bb_period": 20,
            "bb_std": 1.6
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "ボリンジャーバンド逆張り＋RSIの売られすぎ・買われすぎ反転を検知。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        rsi_p = self.parameters["rsi_period"]
        bb_p = self.parameters["bb_period"]
        bb_std = self.parameters["bb_std"]

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

        df["signal"] = np.nan
        long_entry = (df["close"] < df["bb_lower"]) & (df["rsi"] < self.parameters["rsi_oversold"])
        short_entry = (df["close"] > df["bb_upper"]) & (df["rsi"] > self.parameters["rsi_overbought"])
        exit_cond = abs(df["close"] - df["bb_mid"]) / df["bb_mid"] < 0.002

        df.loc[long_entry, "signal"] = 1
        df.loc[short_entry, "signal"] = -1
        df.loc[exit_cond, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
"""
    },
    {
        "theme": "Volatility Breakout (Donchian Channel / Turtle)",
        "name": "DonchianBreakoutStrategy",
        "hypothesis": "過去N期間の高値ブレイクで順張り買い、安値ブレイクで順張り売り。強いトレンドを捕獲する。",
        "code": """import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="DonchianBreakout", version="v1.0", parameters=None):
        default_params = {
            "channel_period": 20,
            "exit_period": 10
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "ドンチャンチャネル高値・安値ブレイクアウト順張り。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        p = self.parameters["channel_period"]
        exit_p = self.parameters["exit_period"]

        df["upper"] = df["high"].rolling(window=p).max().shift(1)
        df["lower"] = df["low"].rolling(window=p).min().shift(1)
        df["exit_high"] = df["high"].rolling(window=exit_p).max().shift(1)
        df["exit_low"] = df["low"].rolling(window=exit_p).min().shift(1)

        df["signal"] = np.nan
        long_entry = df["close"] > df["upper"]
        short_entry = df["close"] < df["lower"]
        long_exit = df["close"] < df["exit_low"]
        short_exit = df["close"] > df["exit_high"]

        df.loc[long_entry, "signal"] = 1
        df.loc[short_entry, "signal"] = -1
        df.loc[long_exit | short_exit, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
"""
    },
    {
        "theme": "Momentum (MACD + Volume Spike)",
        "name": "MacdVolumeMomentumStrategy",
        "hypothesis": "MACDのゴールデンクロスと同時に出来高急増（平均の1.5倍）が発生した強力なモメンタムを狙う。",
        "code": """import pandas as pd
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
        return df
"""
    },
    {
        "theme": "Spread Capture MM (Cost-Aware Micro Spread)",
        "name": "SpreadCaptureMMStrategy",
        "hypothesis": "往復取引コスト(0.14%)を超えるダイナミックスプレッドを計算し、低ボラレンジでの指値約定とミッド回帰利確を狙う。",
        "code": """import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="SpreadCaptureMM", version="v1.0", parameters=None):
        default_params = {
            "spread_multiplier": 1.2,
            "atr_period": 14,
            "min_spread_pct": 0.0025,  # 0.25% (コスト0.14%をカバー)
            "vol_filter_threshold": 1.3
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "手数料カバー型スプレッドキャプチャMM＋ボラ急増回避。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        atr_p = self.parameters["atr_period"]
        spread_mult = self.parameters["spread_multiplier"]
        min_spread = self.parameters["min_spread_pct"]
        vol_thresh = self.parameters["vol_filter_threshold"]

        tr = np.maximum(df["high"] - df["low"], 
                        np.maximum(abs(df["high"] - df["close"].shift(1)), 
                                   abs(df["low"] - df["close"].shift(1))))
        df["atr"] = tr.rolling(window=atr_p).mean()
        df["atr_baseline"] = df["atr"].rolling(window=atr_p * 3).mean()

        df["mid"] = df["close"].rolling(window=10).mean()
        raw_spread = df["atr"] * spread_mult
        min_half_spread = df["close"] * (min_spread / 2.0)
        df["half_spread"] = np.maximum(raw_spread, min_half_spread)

        df["bid_limit"] = df["mid"] - df["half_spread"]
        df["ask_limit"] = df["mid"] + df["half_spread"]

        df["signal"] = np.nan
        normal_vol = df["atr"] <= (df["atr_baseline"] * vol_thresh)

        long_fill = (df["low"] <= df["bid_limit"]) & normal_vol
        short_fill = (df["high"] >= df["ask_limit"]) & normal_vol

        df.loc[long_fill, "signal"] = 1
        df.loc[short_fill, "signal"] = -1

        exit_cond = abs(df["close"] - df["mid"]) < (df["half_spread"] * 0.25)
        df.loc[exit_cond, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
"""
    },
    {
        "theme": "Inventory Skew MM (Avellaneda-Stoikov Style)",
        "name": "InventorySkewMMStrategy",
        "hypothesis": "短期トレンド方向と相場モメンタムに応じて指値価格を非対称にスキュー（偏向）させ、在庫の逆行リスクを低減するMM戦略。",
        "code": """import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="InventorySkewMM", version="v1.0", parameters=None):
        default_params = {
            "trend_period": 20,
            "skew_factor": 0.5,
            "spread_multiplier": 1.0,
            "min_spread_pct": 0.0020
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "Avellaneda-Stoikov理論着想: トレンド偏向スキューMM戦略。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        tp = self.parameters["trend_period"]
        skew_f = self.parameters["skew_factor"]
        spread_m = self.parameters["spread_multiplier"]
        min_spread = self.parameters["min_spread_pct"]

        df["ema_trend"] = df["close"].ewm(span=tp, adjust=False).mean()
        df["trend_slope"] = (df["ema_trend"] - df["ema_trend"].shift(3)) / (df["close"] + 1e-9)

        # ATR
        tr = np.maximum(df["high"] - df["low"], 
                        np.maximum(abs(df["high"] - df["close"].shift(1)), 
                                   abs(df["low"] - df["close"].shift(1))))
        df["atr"] = tr.rolling(window=14).mean()

        df["mid"] = df["close"].rolling(window=8).mean()
        half_spread = np.maximum(df["atr"] * spread_m, df["close"] * (min_spread / 2.0))

        # スキュー調整: 上昇トレンドではBidを浅く(約定しやすく)、Askを深く(利確を大きく)
        skew_offset = df["trend_slope"] * df["close"] * skew_f * 50
        df["bid_limit"] = df["mid"] - half_spread + skew_offset
        df["ask_limit"] = df["mid"] + half_spread + skew_offset

        df["signal"] = np.nan
        long_fill = df["low"] <= df["bid_limit"]
        short_fill = df["high"] >= df["ask_limit"]

        df.loc[long_fill, "signal"] = 1
        df.loc[short_fill, "signal"] = -1

        # 逆トレンド加速時の手仕舞いガード
        exit_guard = (df["trend_slope"] < -0.001) | (df["trend_slope"] > 0.001)
        df.loc[exit_guard, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
"""
    },
    {
        "theme": "Grid Market Making (Range Bound Orders)",
        "name": "GridMarketMakingStrategy",
        "hypothesis": "低ボラティリティのボックス圏相場を特定し、一定ATR幅のグリッド多段指値で細かく反転利益を積み上げるMM戦略。",
        "code": """import pandas as pd
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
"""
    },
    {
        "theme": "Micro Spread MM (Market Making & Spread Skew)",
        "name": "MicroSpreadMMStrategy",
        "hypothesis": "微小スプレッド(0.5 ATR)での高頻度指値約定と在庫偏りスキューによる超短期マーケットメイク戦略。",
        "code": """import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self, name="MicroSpreadMM", version="v1.0", parameters=None):
        default_params = {
            "spread_multiplier": 0.5,
            "atr_period": 10,
            "vol_filter_threshold": 1.4
        }
        if parameters:
            default_params.update(parameters)
        super().__init__(name=name, version=version, parameters=default_params)
        self.hypothesis = "微小スプレッド高頻度キャプチャ＋ボラ急増回避フィルター。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        atr_p = self.parameters["atr_period"]
        spread_mult = self.parameters["spread_multiplier"]
        vol_thresh = self.parameters["vol_filter_threshold"]

        tr = np.maximum(df["high"] - df["low"], 
                        np.maximum(abs(df["high"] - df["close"].shift(1)), 
                                   abs(df["low"] - df["close"].shift(1))))
        df["atr"] = tr.rolling(window=atr_p).mean()
        df["atr_baseline"] = df["atr"].rolling(window=atr_p * 3).mean()

        df["mid"] = df["close"].rolling(window=5).mean()
        df["half_spread"] = df["atr"] * spread_mult
        df["bid_limit"] = df["mid"] - df["half_spread"]
        df["ask_limit"] = df["mid"] + df["half_spread"]

        df["signal"] = np.nan
        normal_vol = df["atr"] <= (df["atr_baseline"] * vol_thresh)

        long_fill = (df["low"] <= df["bid_limit"]) & normal_vol
        short_fill = (df["high"] >= df["ask_limit"]) & normal_vol

        df.loc[long_fill, "signal"] = 1
        df.loc[short_fill, "signal"] = -1

        exit_cond = abs(df["close"] - df["mid"]) < (df["half_spread"] * 0.3)
        df.loc[exit_cond, "signal"] = 0

        df["signal"] = df["signal"].ffill().fillna(0).astype(int)
        return df
"""
    },
    {
        "theme": "Micro Trend Following (Order Flow Delta & Low Reverse Flow)",
        "name": "MicroTrendOrderFlowStrategy",
        "hypothesis": "Taker約定差分量(Delta)が同方向に継続し逆方向の動きが少ない局面で2bpの順張りを仕掛け、10〜20bpの波を追随するマイクロトレンド戦略。",
        "code": """import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
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
        self.strategy_type = "trend_following"
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
"""
    }
]


class StrategyProposer(BaseAgent):
    """
    【1. 提案 (Strategy Proposer)】
    新規戦略ロジックやパラメータ仮説を立案し、BaseStrategyを継承したPythonコードを生成する。
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="StrategyProposer", role="Strategy Idea Generator & Code Writer", config=config)
        self.strategy_counter = 0

    def propose_strategy(self, theme: Optional[str] = None, output_dir: str = "strategies/proposed") -> Dict[str, Any]:
        """
        仮説を立案し、戦略コードファイルを生成する。
        """
        os.makedirs(output_dir, exist_ok=True)
        self.strategy_counter += 1
        strat_id = f"strat_{uuid.uuid4().hex[:8]}"

        user_prompt = f"テーマ: {theme or 'Trend Following & Volatility Breakout'} に基づく自動売買戦略を立案してください。"
        
        # モックまたはLLMで戦略を取得
        if self.provider != "mock":
            response = self.call_llm(SYSTEM_PROMPT, user_prompt)
            code = self.extract_python_code(response)
            hypothesis = f"LLM生成戦略: {theme}"
            name = f"LlmGenerated_{strat_id}"
        else:
            # テーマに応じたサンプル戦略を選択
            sample = None
            if theme:
                t_lower = theme.lower()
                if any(k in t_lower for k in ["micro", "微小", "classic mm"]):
                    sample = next((s for s in SAMPLE_STRATEGIES if s["name"] == "MicroSpreadMMStrategy"), None)
                elif any(k in t_lower for k in ["skew", "stoikov", "在庫", "skew mm"]):
                    sample = next((s for s in SAMPLE_STRATEGIES if s["name"] == "InventorySkewMMStrategy"), None)
                elif any(k in t_lower for k in ["grid", "グリッド", "range mm", "box"]):
                    sample = next((s for s in SAMPLE_STRATEGIES if s["name"] == "GridMarketMakingStrategy"), None)
                elif any(k in t_lower for k in ["spread capture", "cost-aware", "スプレッドキャプチャ"]):
                    sample = next((s for s in SAMPLE_STRATEGIES if s["name"] == "SpreadCaptureMMStrategy"), None)
                elif any(k in t_lower for k in ["mm戦略", "market making", "mm"]):
                    sample = next((s for s in SAMPLE_STRATEGIES if s["name"] == "MicroSpreadMMStrategy"), None)
                elif any(k in t_lower for k in ["frend", "trend", "ema", "トレンド"]):
                    sample = next((s for s in SAMPLE_STRATEGIES if s["name"] == "EmaTrendStrategy"), None)
                elif any(k in t_lower for k in ["mean", "reversion", "rsi", "平均回帰", "オシレーター"]):
                    sample = next((s for s in SAMPLE_STRATEGIES if s["name"] == "RsiMeanReversionStrategy"), None)
                elif any(k in t_lower for k in ["breakout", "donchian", "turtle", "ブレイク"]):
                    sample = next((s for s in SAMPLE_STRATEGIES if s["name"] == "DonchianBreakoutStrategy"), None)
                elif any(k in t_lower for k in ["momentum", "macd", "モメンタム"]):
                    sample = next((s for s in SAMPLE_STRATEGIES if s["name"] == "MacdVolumeMomentumStrategy"), None)

            if sample is None:
                sample = SAMPLE_STRATEGIES[(self.strategy_counter - 1) % len(SAMPLE_STRATEGIES)]

            code = sample["code"]
            hypothesis = sample["hypothesis"]
            name = f"{sample['name']}_{strat_id}"

        file_name = f"{strat_id}_v1.py"
        file_path = os.path.join(output_dir, file_name)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        return {
            "strategy_id": strat_id,
            "name": name,
            "version": "v1.0",
            "file_path": file_path,
            "code": code,
            "hypothesis": hypothesis,
            "iteration": 1,
        }
