"""
Trend Follow Agent (方向性 ＆ レジーム判定)
- 価格の中期トレンド・ボラティリティ・市場レジーム(trend/range)を判定
- 周期: 1s〜30s
"""
from typing import List, Optional
from dataclasses import asdict
from ..event_bus import EventBus
from ..schema import OrderbookMicroSnapshot, AgentConclusion


class TrendFollowAgent:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.mid_history: List[float] = []
        self.latest_state: dict = {}
        self.latest_conclusion: Optional[AgentConclusion] = None
        self.bus.subscribe("orderbook_micro", self.on_micro_update)

    def on_micro_update(self, snap: OrderbookMicroSnapshot):
        self.mid_history.append(snap.mid_price)
        if len(self.mid_history) > 60:
            self.mid_history.pop(0)

        # 30サンプル以上で方向判定。未満は WARMUP（偽のトレンドを出さない）
        min_samples = 20
        if len(self.mid_history) >= min_samples:
            price_delta = self.mid_history[-1] - self.mid_history[0]
            p_range = max(self.mid_history) - min(self.mid_history)
            strength = min(1.0, abs(price_delta) / 2500.0)
            ready = True

            if price_delta >= 400.0:
                direction = "up"
            elif price_delta <= -400.0:
                direction = "down"
            else:
                direction = "neutral"

            if strength >= 0.35:
                regime = "trend"
            elif p_range >= 3500.0:
                regime = "high_vol"
            elif p_range <= 800.0:
                regime = "low_vol"
            else:
                regime = "range"
        else:
            direction, strength, regime = "neutral", 0.0, "warmup"
            price_delta = 0.0
            p_range = 0.0
            ready = False

        trend_state = {
            "timestamp": snap.timestamp,
            "trend_direction": direction,
            "trend_strength": round(strength, 3),
            "regime_tag": regime,
            "sample_ready": ready,
            "n_samples": len(self.mid_history),
        }
        self.latest_state = trend_state
        self.bus.publish("trend_state", trend_state)

        if not ready:
            verdict = "WARMUP"
            primary_action = "hold"
            explanation = f"サンプル不足 ({len(self.mid_history)}/{min_samples}) — トレンド未確定"
            conf = 0.0
        else:
            verdict = f"{regime.upper()}_{direction.upper()}"
            primary_action = "buy" if direction == "up" else ("sell" if direction == "down" else "hold")
            explanation = f"レジーム: {regime}, トレンド: {direction.upper()} (強度: {strength:.2f}, 変動幅: ¥{price_delta:+.0f})"
            conf = round(strength, 3)

        conclusion = AgentConclusion(
            agent_name="TrendFollowAgent",
            timestamp=snap.timestamp,
            verdict=verdict,
            confidence=conf,
            primary_action=primary_action,
            metrics={
                "trend_direction": direction,
                "trend_strength": strength,
                "regime_tag": regime,
                "price_delta": price_delta,
                "price_range": p_range,
                "sample_ready": ready,
                "n_samples": len(self.mid_history),
            },
            parameters={},
            hard_veto=False,
            emergency_cancel=False,
            explanation=explanation,
        )
        self.latest_conclusion = conclusion
        self.bus.publish("trend_conclusion", asdict(conclusion))
        return conclusion

    def get_latest_conclusion(self) -> Optional[AgentConclusion]:
        return self.latest_conclusion

