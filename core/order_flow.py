import time
import json
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np


class OrderFlowAnalyzer:
    """
    市場微小構造（Market Microstructure）におけるTaker約定フロー（Order Flow Delta）アナライザー。
    
    【主な機能】
    1. bitFlyer Lightning FX の約定履歴 (Executions) から Taker Buy と Taker Sell を分離集計
    2. Net Volume Delta (Taker差分量) および Delta Ratio (-1.0 〜 +1.0) の算出
    3. フローの同方向継続性 (Directional Persistence) の判定 (2bpの小さな波の検出)
    4. 逆方向ノイズ比率 (Opposite Flow Ratio) の監視 (逆の動きが少ないか)
    5. 逆流検知による即時Cancel推奨シグナルの発行 (Adverse Selection防止)
    """

    EXECUTIONS_URL = "https://api.bitflyer.com/v1/executions?product_code="

    def __init__(
        self,
        product_code: str = "FX_BTC_JPY",
        window_seconds: float = 30.0,
        persistence_threshold: float = 0.65,
        noise_ceiling: float = 0.25,
    ):
        self.product_code = product_code
        self.window_seconds = window_seconds
        self.persistence_threshold = persistence_threshold
        self.noise_ceiling = noise_ceiling

        # 履歴バッファ
        self.raw_executions: List[Dict[str, Any]] = []
        self.delta_history: List[float] = []
        self.last_fetch_time = 0.0

    def add_executions(self, executions: List[Dict[str, Any]]):
        """外部から渡された約定リストを追加"""
        for item in executions:
            self.raw_executions.append(item)
        # メモリ制限 (直近 1000 件)
        if len(self.raw_executions) > 1000:
            self.raw_executions = self.raw_executions[-1000:]

    def fetch_recent_executions(self, count: int = 100) -> List[Dict[str, Any]]:
        """bitFlyer APIから最新約定データを取得"""
        url = f"{self.EXECUTIONS_URL}{self.product_code}&count={count}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (GapcorePJ OrderFlowAnalyzer)"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    self.add_executions(data)
                    self.last_fetch_time = time.time()
                    return data
        except Exception as e:
            # エラー時は空リスト
            pass
        return []

    def analyze(self, window_sec: Optional[float] = None) -> Dict[str, Any]:
        """
        直近 window_sec 秒間のTaker約定フローを解析。
        
        Returns:
            Dict containing:
                - taker_buy_vol: 成行買い量 (BTC)
                - taker_sell_vol: 成行売り量 (BTC)
                - net_delta: 正味差分量 (Buy - Sell)
                - delta_ratio: 差分比率 (-1.0 〜 +1.0)
                - flow_direction: 1 (買い継続), -1 (売り継続), 0 (拮抗/中立)
                - persistence_score: 同方向継続度 (0.0 〜 1.0)
                - opposite_noise_ratio: 逆方向比率 (0.0 〜 1.0)
                - should_cancel_bid: 売りフロー急増により買い指値を即Cancelすべきか
                - should_cancel_ask: 買いフロー急増により売り指値を即Cancelすべきか
                - micro_trend_bp: 推定マイクロトレンド期待値 (bp)
        """
        win = window_sec or self.window_seconds
        if not self.raw_executions:
            return self._empty_result()

        # 最新約定時刻を基準に win 秒以内の約定を抽出
        df = pd.DataFrame(self.raw_executions)
        if "exec_date" not in df.columns or "side" not in df.columns or "size" not in df.columns:
            return self._empty_result()

        # 日時変換
        df["dt"] = pd.to_datetime(df["exec_date"], format="mixed")
        latest_dt = df["dt"].max()
        cutoff_dt = latest_dt - pd.Timedelta(seconds=win)

        df_window = df[df["dt"] >= cutoff_dt].copy()
        if df_window.empty:
            return self._empty_result()

        # Taker Buy / Taker Sell 集計
        buy_mask = df_window["side"].str.upper() == "BUY"
        sell_mask = df_window["side"].str.upper() == "SELL"

        buy_vol = float(df_window.loc[buy_mask, "size"].sum())
        sell_vol = float(df_window.loc[sell_mask, "size"].sum())
        total_vol = buy_vol + sell_vol

        if total_vol <= 1e-6:
            return self._empty_result()

        net_delta = buy_vol - sell_vol
        delta_ratio = net_delta / total_vol

        # 履歴記録
        self.delta_history.append(delta_ratio)
        if len(self.delta_history) > 20:
            self.delta_history = self.delta_history[-20:]

        # 同方向の継続性 (直近5回の符号の一致度)
        recent_deltas = self.delta_history[-5:]
        if net_delta > 0:
            same_sign_count = sum(1 for d in recent_deltas if d > 0.1)
            opposite_noise_ratio = sell_vol / total_vol
        elif net_delta < 0:
            same_sign_count = sum(1 for d in recent_deltas if d < -0.1)
            opposite_noise_ratio = buy_vol / total_vol
        else:
            same_sign_count = 0
            opposite_noise_ratio = 0.5

        persistence_score = same_sign_count / len(recent_deltas) if recent_deltas else 0.0

        # マイクロトレンド判定 (2bpの初動判定)
        # 1. DeltaRatioが十分偏っている
        # 2. 継続性スコアが高い
        # 3. 逆方向の動きが少ない (noise_ceiling以下)
        flow_direction = 0
        micro_trend_bp = 0.0

        if (
            delta_ratio >= self.persistence_threshold
            and persistence_score >= 0.6
            and opposite_noise_ratio <= self.noise_ceiling
        ):
            flow_direction = 1  # 強い買いフロー継続
            micro_trend_bp = round(delta_ratio * 15.0, 1)  # 2bp〜15bp期待
        elif (
            delta_ratio <= -self.persistence_threshold
            and persistence_score >= 0.6
            and opposite_noise_ratio <= self.noise_ceiling
        ):
            flow_direction = -1  # 強い売りフロー継続
            micro_trend_bp = round(delta_ratio * 15.0, 1)

        # 動的Cancel判定:
        # 逆方向Takerが急増した瞬間、既存のMaker指値が貫通されて解消コスト割れするのを防ぐ
        should_cancel_bid = (delta_ratio <= -0.5)  # 売り圧殺到 -> 買い指値即キャンセル
        should_cancel_ask = (delta_ratio >= 0.5)   # 買い圧殺到 -> 売り指値即キャンセル

        return {
            "taker_buy_vol": round(buy_vol, 4),
            "taker_sell_vol": round(sell_vol, 4),
            "net_delta": round(net_delta, 4),
            "delta_ratio": round(delta_ratio, 3),
            "flow_direction": flow_direction,
            "persistence_score": round(persistence_score, 2),
            "opposite_noise_ratio": round(opposite_noise_ratio, 3),
            "should_cancel_bid": should_cancel_bid,
            "should_cancel_ask": should_cancel_ask,
            "micro_trend_bp": micro_trend_bp,
        }

    def _empty_result(self) -> Dict[str, Any]:
        return {
            "taker_buy_vol": 0.0,
            "taker_sell_vol": 0.0,
            "net_delta": 0.0,
            "delta_ratio": 0.0,
            "flow_direction": 0,
            "persistence_score": 0.0,
            "opposite_noise_ratio": 0.0,
            "should_cancel_bid": False,
            "should_cancel_ask": False,
            "micro_trend_bp": 0.0,
        }
