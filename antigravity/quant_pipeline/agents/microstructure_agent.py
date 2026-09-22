"""
Microstructure Agent (板の癖 ＆ 瞬間圧力解析)
- 板の厚み不均衡 (Imbalance), Micro-price, Taker攻撃性, フェイクブレイク, レイテンシを監視
- 1時間集計: tip/成行/cancel 同時発生を蓄積し、人間が読めない板癖を毎時報告
- 周期: イベント駆動 / WIRE=NO（hard_veto 常時 False）
"""
from typing import Optional, Any, Dict
from dataclasses import asdict
from ..event_bus import EventBus
from ..schema import OrderbookMicroSnapshot, AgentConclusion
from ..microstructure_hourly_store import MicrostructureHourlyStore


class MicrostructureAgent:
    def __init__(self, bus: EventBus, latency_threshold_ms: float = 80.0):
        self.bus = bus
        self.latency_threshold_ms = latency_threshold_ms
        self.latest_state: dict = {}
        self.latest_conclusion: Optional[AgentConclusion] = None
        self.hourly = MicrostructureHourlyStore()
        self.latest_hourly_report: Optional[Dict[str, Any]] = None
        self.bus.subscribe("orderbook_micro", self.on_micro_update)

    def on_micro_update(self, snap: OrderbookMicroSnapshot):
        # 1. 板圧力スコア (pressure_side, pressure_score)
        # 条件: Imbalanceの偏り ＋ Taker成行約定の方向性の一致
        imb = snap.imbalance
        taker_buy = snap.taker_volume_ask   # askを叩く成行買い
        taker_sell = snap.taker_volume_bid  # bidを叩く成行売り

        pressure_side = "none"
        pressure_score = 0.0

        if imb >= 0.20 and taker_buy > taker_sell and taker_buy >= 0.01:
            pressure_side = "buy"
            # Imbalanceの強さとTaker攻撃性を合成
            raw_score = imb * 1.2 + (snap.taker_aggressiveness * 0.4)
            pressure_score = min(1.0, max(0.0, raw_score))
        elif imb <= -0.20 and taker_sell > taker_buy and taker_sell >= 0.01:
            pressure_side = "sell"
            raw_score = abs(imb) * 1.2 + (snap.taker_aggressiveness * 0.4)
            pressure_score = min(1.0, max(0.0, raw_score))
        # imb 単独では pressure を立てない（成行確認なしは観測のみ）

        # 2. フェイクブレイク判定 (cancel率が高く、Taker継続性がないだまし)
        fake_breakout = (snap.cancel_rate >= 0.55 and snap.taker_aggressiveness < 0.15)

        # 3. レイテンシーリスク判定 (API遅延が閾値超え)
        latency_risk = (snap.latency_ms >= self.latency_threshold_ms)

        # 4. Adverse Selection 予兆 — 研究フラグ。執行 veto には使わない
        adverse_side = "none"
        adverse_score = 0.0
        adverse_warning = False

        # 買いへの逆選択リスク (下落崩落の予兆) — tip+成行の両方が必要
        adverse_buy_factors = []
        if snap.micro_dev <= -150.0:
            adverse_buy_factors.append(min(1.0, abs(snap.micro_dev) / 800.0) * 0.4)
        if snap.bid_depth_1 < max(0.001, snap.ask_depth_1 * 0.6):
            adverse_buy_factors.append(0.25)
        if imb <= -0.15:
            adverse_buy_factors.append(min(1.0, abs(imb)) * 0.2)
        if taker_sell > taker_buy and taker_sell >= 0.01:
            adverse_buy_factors.append(0.35)

        raw_adverse_buy = sum(adverse_buy_factors)

        # 売りへの逆選択リスク (上昇踏み上げの予兆)
        adverse_sell_factors = []
        if snap.micro_dev >= 150.0:
            adverse_sell_factors.append(min(1.0, abs(snap.micro_dev) / 800.0) * 0.4)
        if snap.ask_depth_1 < max(0.001, snap.bid_depth_1 * 0.6):
            adverse_sell_factors.append(0.25)
        if imb >= 0.15:
            adverse_sell_factors.append(min(1.0, abs(imb)) * 0.2)
        if taker_buy > taker_sell and taker_buy >= 0.01:
            adverse_sell_factors.append(0.35)

        raw_adverse_sell = sum(adverse_sell_factors)

        # 成行確認が無い adverse は warning 不可
        buy_confirmed = taker_sell >= 0.01
        sell_confirmed = taker_buy >= 0.01

        if raw_adverse_buy >= 0.50 and raw_adverse_buy > raw_adverse_sell and buy_confirmed:
            adverse_side = "buy"
            adverse_score = min(1.0, round(raw_adverse_buy, 3))
            adverse_warning = (raw_adverse_buy >= 0.65)
        elif raw_adverse_sell >= 0.50 and raw_adverse_sell > raw_adverse_buy and sell_confirmed:
            adverse_side = "sell"
            adverse_score = min(1.0, round(raw_adverse_sell, 3))
            adverse_warning = (raw_adverse_sell >= 0.65)
        # 結論の策定 (研究表示。hard_veto は常に False — cancel_rate 誤警報で執行遮断しない)
        verdict = "NEUTRAL"
        primary_action = "hold"
        explanation = f"Imbalance: {snap.imbalance:+.2f}, MicroDev: {snap.micro_dev:+.0f}円"

        if fake_breakout:
            verdict = "FAKE_BREAKOUT_OBSERVE"
            primary_action = "hold"
            explanation += " [観測: cancel高・taker弱]"
        elif pressure_score >= 0.30 and pressure_side != "none":
            verdict = f"{pressure_side.upper()}_PRESSURE"
            primary_action = pressure_side
            explanation += f" [板圧力: {pressure_side.upper()} 強度{pressure_score:.2f}]"

        micro_state = {
            "timestamp": snap.timestamp,
            "pressure_side": pressure_side,
            "pressure_score": round(pressure_score, 3),
            "fake_breakout_flag": fake_breakout,
            "latency_risk_flag": latency_risk,
            "micro_deviation": snap.micro_dev,
            "imbalance": snap.imbalance,
            "adverse_risk_side": adverse_side,
            "adverse_risk_score": adverse_score,
            "adverse_warning_flag": adverse_warning,
            "verdict": verdict,
        }
        self.latest_state = micro_state
        self.bus.publish("micro_state", micro_state)

        # 1時間板癖集計（新シグナル判断材料・執行非接続）
        try:
            rolled = self.hourly.ingest(snap, micro_state)
            if rolled is not None:
                self.latest_hourly_report = rolled
                self.bus.publish("micro_hourly_report", rolled)
        except Exception:
            pass

        conclusion = AgentConclusion(
            agent_name="MicrostructureAgent",
            timestamp=snap.timestamp,
            verdict=verdict,
            confidence=round(pressure_score, 3),
            primary_action=primary_action,
            metrics={
                "imbalance": snap.imbalance,
                "micro_dev": snap.micro_dev,
                "pressure_side": pressure_side,
                "pressure_score": pressure_score,
                "fake_breakout": fake_breakout,
                "latency_risk": latency_risk,
                "adverse_risk_side": adverse_side,
                "adverse_risk_score": adverse_score,
                "adverse_warning": adverse_warning,
            },
            parameters={},
            hard_veto=False,
            emergency_cancel=False,
            explanation=explanation,
        )
        self.latest_conclusion = conclusion
        self.bus.publish("micro_conclusion", asdict(conclusion))
        return conclusion

    def get_latest_conclusion(self) -> Optional[AgentConclusion]:
        return self.latest_conclusion

    def get_hourly_snapshot(self) -> Dict[str, Any]:
        """定期報告用: 確定時間次があればそれ、なければ部分集計。"""
        if self.latest_hourly_report and not self.latest_hourly_report.get("partial"):
            return self.latest_hourly_report
        try:
            return self.hourly.flush_partial()
        except Exception:
            return {}

