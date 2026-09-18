"""
antigravity/multi_asset/pods/fx/fx_micro_agent.py: 為替 (FX) 専属 MICRO担当 AGENT
=============================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 & 5 に準拠。
- グローバル為替市場セッション判定 (東京・ロンドン・NY・仲値・NYカット)
- USD/JPY ピップス (0.01円 = 1 pip) & スプレッド拡大/縮小判定
- 短期方向性 & フローインバランス推定
- 出力: MicroSignal (asset_class="FX")
"""

import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

from ...schemas import MicroSignal
from ...base_agent import BaseMicroAgent

JST = timezone(timedelta(hours=9))


class FxMicroAgent(BaseMicroAgent):
    """
    為替 (FX: USD/JPY, EUR/JPY等) 専属 MICRO担当 AGENT
    """

    def __init__(self, symbols: Optional[List[str]] = None):
        super().__init__(asset_class="FX", symbols=symbols or ["USDJPY", "EURJPY"])

    @staticmethod
    def get_fx_session(now_dt: Optional[datetime] = None) -> str:
        """
        現在時刻 (JST) からFX主要市場セッションを判定
        """
        if now_dt is None:
            now_dt = datetime.now(JST)

        # 週末判定: 土曜06:00〜月曜07:00 JSTはクローズ
        weekday = now_dt.weekday() # 0: Mon, 5: Sat, 6: Sun
        minutes = now_dt.hour * 60 + now_dt.minute

        if (weekday == 5 and minutes >= 360) or weekday == 6 or (weekday == 0 and minutes < 420):
            return "WEEKEND_CLOSED"

        # 仲値セッション (09:40 - 10:00 JST, 09:55公示)
        if 580 <= minutes <= 600:
            return "TOKYO_FIX"
        # 東京セッション (09:00 - 15:00)
        elif 540 <= minutes < 900:
            return "TOKYO_SESSION"
        # ロンドン/NY 重複セッション (21:30 - 01:00 JST 最も流動性大)
        elif (1290 <= minutes <= 1439) or (0 <= minutes < 60):
            return "NY_LONDON_OVERLAP"
        # ロンドン単独 (16:00 - 21:30)
        elif 960 <= minutes < 1290:
            return "LONDON_SESSION"
        # NY単独・終盤 (01:00 - 06:00)
        elif 60 <= minutes < 360:
            return "NY_LATE_SESSION"
        # オセアニア早朝 (06:00 - 09:00 スプレッド拡大帯)
        else:
            return "OCEANIA_EARLY"

    def evaluate_micro(self, market_data: Dict[str, Any]) -> MicroSignal:
        """
        FXレート・スプレッド・気配から MicroSignal を算出
        market_data 例:
        {
            "symbol": "USDJPY",
            "bid_price": 155.250,
            "ask_price": 155.253, # スプレッド 0.3 pips (0.003円)
            "tick_direction": +1,
            "bid_depth": 5.0,     # 万通貨
            "ask_depth": 3.0,
            "timestamp": 1726620000.0,
        }
        """
        symbol = market_data.get("symbol", self.symbols[0])
        bid_p = float(market_data.get("bid_price", 150.000))
        ask_p = float(market_data.get("ask_price", 150.003))
        spread_jpy = max(0.0, ask_p - bid_p)
        spread_pips = spread_jpy * 100.0  # 1 pip = 0.01 JPY

        # 1. セッション判定
        if "session" in market_data:
            session = str(market_data["session"])
        elif "dt" in market_data and isinstance(market_data["dt"], datetime):
            session = self.get_fx_session(market_data["dt"])
        elif "timestamp" in market_data and market_data["timestamp"]:
            try:
                dt = datetime.fromtimestamp(float(market_data["timestamp"]), tz=JST)
                session = self.get_fx_session(dt)
            except Exception:
                session = self.get_fx_session()
        else:
            session = self.get_fx_session()

        # 2. 板気配厚み・フローインバランス
        bid_depth = float(market_data.get("bid_depth", 1.0))
        ask_depth = float(market_data.get("ask_depth", 1.0))
        imbalance_ratio = bid_depth / max(0.01, ask_depth)

        tick_dir = int(market_data.get("tick_direction", 0))

        # 3. 方向性 & 確信度
        if imbalance_ratio >= 1.5 or tick_dir > 0:
            direction = "LONG"
            confidence = min(95.0, 50.0 + (imbalance_ratio - 1.0) * 20.0 + (10.0 if tick_dir > 0 else 0.0))
        elif imbalance_ratio <= 0.67 or tick_dir < 0:
            direction = "SHORT"
            confidence = min(95.0, 50.0 + (1.0 / max(0.01, imbalance_ratio) - 1.0) * 20.0 + (10.0 if tick_dir < 0 else 0.0))
        else:
            direction = "NEUTRAL"
            confidence = 50.0

        # 4. レジーム判定
        if spread_pips > 1.2 or session == "OCEANIA_EARLY":
            regime = "ILLIQUID"
        elif session in ("NY_LONDON_OVERLAP", "TOKYO_FIX") and confidence >= 68.0:
            regime = "TREND"
        elif session == "WEEKEND_CLOSED":
            regime = "ILLIQUID"
        else:
            regime = "MEAN_REVERT"

        # 週末休場中は確信度ゼロ
        if session == "WEEKEND_CLOSED":
            confidence = 0.0

        sig = MicroSignal(
            asset_class=self.asset_class,
            symbol=symbol,
            direction=direction,
            confidence=round(confidence, 1),
            regime=regime,
            spread_jpy=round(spread_jpy, 4),
            imbalance_ratio=round(imbalance_ratio, 3),
            timestamp=float(market_data.get("timestamp", time.time())),
            extra_metrics={
                "fx_session": session,
                "spread_pips": round(spread_pips, 2),
                "mid_price": round((bid_p + ask_p) / 2.0, 4),
            },
        )
        self.last_signal = sig
        return sig
