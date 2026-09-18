"""
Microstructure Agent (板の癖 ＆ 瞬間圧力解析)
- 板の厚み不均衡 (Imbalance), Micro-price, Taker攻撃性, フェイクブレイク, レイテンシを監視
- 周期: 10ms〜100ms (イベント駆動)
"""
from ..event_bus import EventBus
from ..schema import OrderbookMicroSnapshot


class MicrostructureAgent:
    def __init__(self, bus: EventBus, latency_threshold_ms: float = 80.0):
        self.bus = bus
        self.latency_threshold_ms = latency_threshold_ms
        self.bus.subscribe("orderbook_micro", self.on_micro_update)

    def on_micro_update(self, snap: OrderbookMicroSnapshot):
        # 1. 板圧力スコア (pressure_side, pressure_score)
        # 条件: Imbalanceの偏り ＋ Taker成行約定の方向性の一致
        imb = snap.imbalance
        taker_buy = snap.taker_volume_ask   # askを叩く成行買い
        taker_sell = snap.taker_volume_bid  # bidを叩く成行売り

        pressure_side = "none"
        pressure_score = 0.0

        if imb >= 0.20 and taker_buy >= taker_sell:
            pressure_side = "buy"
            # Imbalanceの強さとTaker攻撃性を合成
            raw_score = imb * 1.2 + (snap.taker_aggressiveness * 0.4)
            pressure_score = min(1.0, max(0.0, raw_score))
        elif imb <= -0.20 and taker_sell >= taker_buy:
            pressure_side = "sell"
            raw_score = abs(imb) * 1.2 + (snap.taker_aggressiveness * 0.4)
            pressure_score = min(1.0, max(0.0, raw_score))
        elif abs(imb) >= 0.40:
            # Taker約定がなくとも板厚が極端に偏っている場合
            pressure_side = "buy" if imb > 0 else "sell"
            pressure_score = min(1.0, abs(imb))

        # 2. フェイクブレイク判定 (cancel率が高く、Taker継続性がないだまし)
        fake_breakout = (snap.cancel_rate >= 0.55 and snap.taker_aggressiveness < 0.15)

        # 3. レイテンシーリスク判定 (API遅延が閾値超え)
        latency_risk = (snap.latency_ms >= self.latency_threshold_ms)

        # 4. Adverse Selection (逆選択・急激な逆行リスク) の先回り予兆判定
        # - Micro-PriceがMid価格から先行乖離しているか
        # - 最良気配(Depth 1)の板が薄く蒸発(Book Depletion)しているか
        # - Taker成行フローが逆方向に偏っているか
        adverse_side = "none"
        adverse_score = 0.0
        adverse_warning = False

        # 買いへの逆選択リスク (下落崩落の予兆)
        adverse_buy_factors = []
        if snap.micro_dev <= -150.0:
            adverse_buy_factors.append(min(1.0, abs(snap.micro_dev) / 800.0) * 0.4)
        if snap.bid_depth_1 < max(0.001, snap.ask_depth_1 * 0.6):
            adverse_buy_factors.append(0.3)
        if imb <= -0.15:
            adverse_buy_factors.append(min(1.0, abs(imb)) * 0.3)
        if taker_sell > taker_buy:
            adverse_buy_factors.append(0.2)

        raw_adverse_buy = sum(adverse_buy_factors)

        # 売りへの逆選択リスク (上昇踏み上げの予兆)
        adverse_sell_factors = []
        if snap.micro_dev >= 150.0:
            adverse_sell_factors.append(min(1.0, abs(snap.micro_dev) / 800.0) * 0.4)
        if snap.ask_depth_1 < max(0.001, snap.bid_depth_1 * 0.6):
            adverse_sell_factors.append(0.3)
        if imb >= 0.15:
            adverse_sell_factors.append(min(1.0, abs(imb)) * 0.3)
        if taker_buy > taker_sell:
            adverse_sell_factors.append(0.2)

        raw_adverse_sell = sum(adverse_sell_factors)

        if raw_adverse_buy >= 0.50 and raw_adverse_buy > raw_adverse_sell:
            adverse_side = "buy"  # 買い手にとっての逆選択 (直後に急落)
            adverse_score = min(1.0, round(raw_adverse_buy, 3))
            adverse_warning = (raw_adverse_buy >= 0.65)
        elif raw_adverse_sell >= 0.50 and raw_adverse_sell > raw_adverse_buy:
            adverse_side = "sell"  # 売り手にとっての逆選択 (直後に踏み上げ急騰)
            adverse_score = min(1.0, round(raw_adverse_sell, 3))
            adverse_warning = (raw_adverse_sell >= 0.65)

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
        }
        self.bus.publish("micro_state", micro_state)
