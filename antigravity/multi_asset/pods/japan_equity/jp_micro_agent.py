"""
antigravity/multi_asset/pods/japan_equity/jp_micro_agent.py: 日本株専属 MICRO担当 AGENT
==================================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 に準拠。
- 東証取引時間帯レジーム (前場 09:00-11:30 / 昼休み 11:30-12:30 / 後場 12:30-15:30 / PTS 17:00-23:59)
- 板インバランス (買い気配 vs 売り気配厚み比率)
- スプレッド拡大/縮小 & ボラティリティによるレジーム判定
- 出力: MicroSignal (asset_class="JP_STOCK")
"""

import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

from ...schemas import MicroSignal
from ...base_agent import BaseMicroAgent

JST = timezone(timedelta(hours=9))


class JpMicroAgent(BaseMicroAgent):
    """
    日本株専属 MICRO担当 AGENT
    """

    def __init__(self, symbols: Optional[List[str]] = None):
        super().__init__(asset_class="JP_STOCK", symbols=symbols or ["7203", "9984", "6758", "6857", "8035"])

    @staticmethod
    def get_tse_session(now_dt: Optional[datetime] = None) -> str:
        """
        現在時刻 (JST) から東証/PTSセッションレジームを判定
        """
        if now_dt is None:
            now_dt = datetime.now(JST)

        time_minutes = now_dt.hour * 60 + now_dt.minute

        # 土日は休場 (平日のみ東証開場)
        if now_dt.weekday() >= 5:
            return "CLOSED_WEEKEND"

        if 480 <= time_minutes < 540:    # 08:00 - 09:00
            return "PRE_MARKET"
        elif 540 <= time_minutes < 690:  # 09:00 - 11:30
            return "MORNING_SESSION"
        elif 690 <= time_minutes < 750:  # 11:30 - 12:30
            return "LUNCH_BREAK"
        elif 750 <= time_minutes <= 930: # 12:30 - 15:30 (東証新取引時間)
            return "AFTERNOON_SESSION"
        elif 930 < time_minutes < 1020:  # 15:30 - 17:00
            return "POST_MARKET"
        elif 1020 <= time_minutes < 1440:# 17:00 - 23:59
            return "PTS_NIGHT_SESSION"
        else:                            # 00:00 - 08:00
            return "CLOSED_NIGHT"

    def evaluate_micro(self, market_data: Dict[str, Any]) -> MicroSignal:
        """
        日本株の板・歩み値・スプレッドから MicroSignal を算出
        market_data 例:
        {
            "symbol": "7203",
            "bid_price": 2850.0,
            "ask_price": 2851.0,
            "bid_vol": 50000,
            "ask_vol": 25000,
            "last_price": 2850.5,
            "price_change_pct": +0.85,
            "timestamp": 1726620000.0,
        }
        """
        symbol = market_data.get("symbol", self.symbols[0])
        bid_p = float(market_data.get("bid_price", 0.0))
        ask_p = float(market_data.get("ask_price", 0.0))
        bid_v = float(market_data.get("bid_vol", 1.0))
        ask_v = float(market_data.get("ask_vol", 1.0))
        mid_p = (bid_p + ask_p) / 2.0 if (bid_p + ask_p) > 0 else float(market_data.get("last_price", 1000.0))
        spread = max(0.0, ask_p - bid_p)

        # 1. セッション判定
        if "session" in market_data:
            session = str(market_data["session"])
        elif "dt" in market_data and isinstance(market_data["dt"], datetime):
            session = self.get_tse_session(market_data["dt"])
        elif "timestamp" in market_data and market_data["timestamp"]:
            try:
                dt = datetime.fromtimestamp(float(market_data["timestamp"]), tz=JST)
                session = self.get_tse_session(dt)
            except Exception:
                session = self.get_tse_session()
        else:
            session = self.get_tse_session()

        # 2. 板インバランス比率 (買い量 / 売り量)
        imbalance_ratio = bid_v / max(1.0, ask_v)

        # 3. スプレッド比率 (スプレッド / 仲値)
        spread_pct = (spread / mid_p) if mid_p > 0 else 0.0

        # 4. 方向性 & 確信度
        if imbalance_ratio >= 1.5:
            direction = "LONG"
            confidence = min(95.0, 50.0 + (imbalance_ratio - 1.0) * 25.0)
        elif imbalance_ratio <= 0.67:
            direction = "SHORT"
            confidence = min(95.0, 50.0 + (1.0 / max(0.01, imbalance_ratio) - 1.0) * 25.0)
        else:
            direction = "NEUTRAL"
            confidence = 50.0

        # 5. レジーム判定
        is_spread_shock = spread_pct > 0.008  # スプレッド 0.8%超 (スプレッドショック)
        if is_spread_shock:
            regime = "SPREAD_SHOCK"
            direction = "NEUTRAL"
            confidence = 0.0
        elif spread_pct > 0.0025:  # スプレッド 0.25%以上
            regime = "ILLIQUID"
        elif abs(float(market_data.get("price_change_pct", 0.0))) >= 3.0:
            regime = "HIGH_VOL"
        elif confidence >= 70.0:
            regime = "TREND"
        else:
            regime = "MEAN_REVERT"

        # 昼休みや引け後・休場中は新規戦略停止 (確信度ゼロ化)
        if session in ("LUNCH_BREAK", "POST_MARKET", "CLOSED_NIGHT", "CLOSED_WEEKEND"):
            direction = "NEUTRAL"
            confidence = 0.0

        sig = MicroSignal(
            asset_class=self.asset_class,
            symbol=symbol,
            direction=direction,
            confidence=round(confidence, 1),
            regime=regime,
            spread_jpy=round(spread, 2),
            imbalance_ratio=round(imbalance_ratio, 3),
            timestamp=float(market_data.get("timestamp", time.time())),
            extra_metrics={
                "tse_session": session,
                "mid_price": round(mid_p, 2),
                "spread_pct": round(spread_pct, 5),
                "is_spread_shock": is_spread_shock,
            },
        )
        self.last_signal = sig
        return sig
