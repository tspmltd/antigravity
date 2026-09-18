"""
Trend Follow Agent (方向性 ＆ レジーム判定)
- 価格の中期トレンド・ボラティリティ・市場レジーム(trend/range)を判定
- 周期: 1s〜30s
"""
from typing import List
from ..event_bus import EventBus
from ..schema import OrderbookMicroSnapshot, SignalMetrics


class TrendFollowAgent:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.mid_history: List[float] = []
        self.bus.subscribe("orderbook_micro", self.on_micro_update)

    def on_micro_update(self, snap: OrderbookMicroSnapshot):
        self.mid_history.append(snap.mid_price)
        if len(self.mid_history) > 60:
            self.mid_history.pop(0)

        # 30サンプル(約30秒)での価格傾き & ボラティリティ計算
        if len(self.mid_history) >= 10:
            price_delta = self.mid_history[-1] - self.mid_history[0]
            # 直近の価格レンジ
            p_range = max(self.mid_history) - min(self.mid_history)
            
            # トレンド強度: 価格変動幅 / 2500円
            strength = min(1.0, abs(price_delta) / 2500.0)
            
            if price_delta >= 400.0:
                direction = "up"
            elif price_delta <= -400.0:
                direction = "down"
            else:
                direction = "neutral"

            # レジーム判定
            if strength >= 0.35:
                regime = "trend"
            elif p_range >= 3500.0:
                regime = "high_vol"
            elif p_range <= 800.0:
                regime = "low_vol"
            else:
                regime = "range"
        else:
            direction, strength, regime = "neutral", 0.0, "range"

        trend_state = {
            "timestamp": snap.timestamp,
            "trend_direction": direction,
            "trend_strength": round(strength, 3),
            "regime_tag": regime,
        }
        self.bus.publish("trend_state", trend_state)
