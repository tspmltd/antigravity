"""
Adverse Selection Research & Defense Agent (ADVERSE 専門研究・防御エージェント)
========================================================================
CSR-098c / CSR-113 / CSR-408 / CSR-446 準拠

【核心設計思想】
「予測器ではなく、観測計器」
単発の静的スコア（Rsize/Spread/OFI）で未来を当てるのではなく、
取引所の板崩壊プロセス（時系列エピソード）をミリ秒単位で追跡し、
トキシック・テイカーに喰われる直前に指値を退避（Cancel / 遮断）させる。

【状態機械 (State Machine)】
  NORMAL       : 正常な板状態
    ↓
  PRE_ADVERSE  : 最良気配 (tip) の厚みをマーク (baseline)
    ↓
  DEPLETING    : 最良気配が急減・蒸発 (Rising edge で episode 開始)
    ↓
  NO_REFILL    : 板の補充 (Refill) が失敗・不在
    ↓
  OPP_TAKER    : 反対側の大口成行テイカーが連続着弾 (Taker Acceleration)
    ↓
  MAKER_VICTIM : 被害約定 (mk100/250/500, non-recovery, MAE拡大)
"""

import time
from typing import Dict, Any, Optional, List
from ..schema import OrderbookMicroSnapshot
from ..event_bus import EventBus


class AdverseResearchAgent:
    """
    Adverse Selection 専門防御・研究エージェント
    """

    def __init__(
        self,
        bus: EventBus,
        min_lead_ms_threshold: float = 85.0,  # 取引所RTT(85ms)以上の先回りリードタイム
        depletion_ratio_threshold: float = 0.40,  # 最良気配が40%以下に急減
        toxic_taker_threshold: float = 0.50,  # Taker成行の偏り閾値
    ):
        self.bus = bus
        self.min_lead_ms_threshold = min_lead_ms_threshold
        self.depletion_ratio_threshold = depletion_ratio_threshold
        self.toxic_taker_threshold = toxic_taker_threshold

        # 状態機械トラッキング (BUY側・SELL側を完全分離: CSR-113)
        self.state_buy = "NORMAL"
        self.state_sell = "NORMAL"

        self.tip_baseline_buy = 0.0
        self.tip_baseline_sell = 0.0

        self.episode_start_ts_buy = 0.0
        self.episode_start_ts_sell = 0.0

        self.episode_id_buy = 0
        self.episode_id_sell = 0

        # 統計カウンタ
        self.total_episodes = 0
        self.intercepted_kills = 0
        self.estimated_lead_ms_history: List[float] = []

        # EventBus購読
        self.bus.subscribe("orderbook_micro", self.on_orderbook)

    def on_orderbook(self, snap: OrderbookMicroSnapshot):
        now_ts = snap.timestamp / 1000.0 if snap.timestamp > 1e6 else float(snap.timestamp)

        # -------------------------------------------------------------
        # 1. BUY側 (買い指値に対する逆選択 = 急落・下落貫通リスク)
        # -------------------------------------------------------------
        buy_res = self._evaluate_side(
            side="buy",
            tip_depth=snap.bid_depth_1,
            opp_taker_vol=snap.taker_volume_bid,  # bidを叩く成行売り
            micro_dev=snap.micro_dev,
            imbalance=snap.imbalance,
            now_ts=now_ts,
        )

        # -------------------------------------------------------------
        # 2. SELL側 (売り指値に対する逆選択 = 急騰・踏み上げ貫通リスク)
        # -------------------------------------------------------------
        sell_res = self._evaluate_side(
            side="sell",
            tip_depth=snap.ask_depth_1,
            opp_taker_vol=snap.taker_volume_ask,  # askを叩く成行買い
            micro_dev=-snap.micro_dev,
            imbalance=-snap.imbalance,
            now_ts=now_ts,
        )

        # 判定結果の合成
        adverse_side = "none"
        adverse_score = 0.0
        cancel_recommendation = False
        lead_ms_est = 0.0
        active_episode_id = 0

        if buy_res["score"] >= sell_res["score"] and buy_res["score"] >= 0.50:
            adverse_side = "buy"
            adverse_score = buy_res["score"]
            cancel_recommendation = buy_res["cancel_recommended"]
            lead_ms_est = buy_res["lead_ms"]
            active_episode_id = self.episode_id_buy
        elif sell_res["score"] > buy_res["score"] and sell_res["score"] >= 0.50:
            adverse_side = "sell"
            adverse_score = sell_res["score"]
            cancel_recommendation = sell_res["cancel_recommended"]
            lead_ms_est = sell_res["lead_ms"]
            active_episode_id = self.episode_id_sell

        adverse_state = {
            "timestamp": snap.timestamp,
            "adverse_side": adverse_side,
            "adverse_score": round(adverse_score, 3),
            "cancel_recommendation": cancel_recommendation,
            "lead_ms_estimated": round(lead_ms_est, 1),
            "episode_id": active_episode_id,
            "buy_state": self.state_buy,
            "sell_state": self.state_sell,
            "total_episodes": self.total_episodes,
            "intercepted_kills": self.intercepted_kills,
        }

        self.latest_state = adverse_state
        self.bus.publish("adverse_research_state", adverse_state)

        # 結論の策定 (4AGENT 統一)
        hard_veto = (adverse_score >= 0.60)
        emergency_cancel = cancel_recommendation

        verdict = "SAFE"
        primary_action = "hold"
        explanation = f"逆選択スコア: {adverse_score:.2f}, 状態: (B:{self.state_buy}/S:{self.state_sell})"

        if emergency_cancel:
            verdict = "ADVERSE_CRITICAL_CANCEL"
            primary_action = "cancel"
            explanation += f" [🚨 緊急退避: 被害約定先回りCancel推奨 (Lead: {lead_ms_est:.0f}ms)]"
        elif hard_veto:
            verdict = "ADVERSE_WARNING_VETO"
            primary_action = "veto"
            explanation += f" [🛡️ 逆選択警戒: {adverse_side.upper()}側エントリー遮断]"

        from ..schema import AgentConclusion
        from dataclasses import asdict

        conclusion = AgentConclusion(
            agent_name="AdverseResearchAgent",
            timestamp=snap.timestamp,
            verdict=verdict,
            confidence=round(adverse_score, 3),
            primary_action=primary_action,
            metrics={
                "adverse_side": adverse_side,
                "adverse_score": adverse_score,
                "lead_ms_estimated": lead_ms_est,
                "buy_state": self.state_buy,
                "sell_state": self.state_sell,
                "episode_id": active_episode_id,
                "total_episodes": self.total_episodes,
                "intercepted_kills": self.intercepted_kills,
            },
            parameters={"min_lead_ms_threshold": self.min_lead_ms_threshold},
            hard_veto=hard_veto,
            emergency_cancel=emergency_cancel,
            explanation=explanation,
        )
        self.latest_conclusion = conclusion
        self.bus.publish("adverse_conclusion", asdict(conclusion))
        return adverse_state

    def get_latest_conclusion(self) -> Optional[Any]:
        return getattr(self, "latest_conclusion", None)


    def _evaluate_side(
        self,
        side: str,
        tip_depth: float,
        opp_taker_vol: float,
        micro_dev: float,
        imbalance: float,
        now_ts: float,
    ) -> Dict[str, Any]:
        """
        片側の板崩壊エピソード判定 (CSR-408 / CSR-113 準拠)
        """
        is_buy = (side == "buy")
        cur_state = self.state_buy if is_buy else self.state_sell
        baseline = self.tip_baseline_buy if is_buy else self.tip_baseline_sell
        ep_start = self.episode_start_ts_buy if is_buy else self.episode_start_ts_sell

        score = 0.0
        cancel_recommended = False
        lead_ms = 0.0

        # 1. NORMAL -> PRE_ADVERSE (tipのベースライン記録)
        if cur_state == "NORMAL":
            if tip_depth > 0.05:
                if is_buy:
                    self.tip_baseline_buy = tip_depth
                    self.state_buy = "PRE_ADVERSE"
                else:
                    self.tip_baseline_sell = tip_depth
                    self.state_sell = "PRE_ADVERSE"

        # 2. PRE_ADVERSE -> DEPLETING (板の急激な枯渇・Rising edge でエピソード開始)
        elif cur_state == "PRE_ADVERSE":
            if baseline > 0 and tip_depth <= baseline * self.depletion_ratio_threshold:
                # 枯渇開始
                if is_buy:
                    self.state_buy = "DEPLETING"
                    self.episode_start_ts_buy = now_ts
                    self.episode_id_buy += 1
                else:
                    self.state_sell = "DEPLETING"
                    self.episode_start_ts_sell = now_ts
                    self.episode_id_sell += 1
                self.total_episodes += 1
                score = 0.60
            elif tip_depth > baseline * 1.2:
                # 板が厚くなった場合はベースライン更新
                if is_buy:
                    self.tip_baseline_buy = tip_depth
                else:
                    self.tip_baseline_sell = tip_depth

        # 3. DEPLETING / NO_REFILL / OPP_TAKER
        elif cur_state in ("DEPLETING", "NO_REFILL", "OPP_TAKER"):
            age_ms = (now_ts - ep_start) * 1000.0

            # 安全弁: エピソード最大存続時間 (2.5秒超えで自動リセット: CSR-113)
            if age_ms > 2500.0:
                if is_buy:
                    self.state_buy = "NORMAL"
                else:
                    self.state_sell = "NORMAL"
                return {"score": 0.0, "cancel_recommended": False, "lead_ms": 0.0}

            # トキシック成行の加速判定
            toxic_surge = opp_taker_vol >= self.toxic_taker_threshold
            dev_adverse = micro_dev <= -100.0  # 不利方向への乖離
            imb_adverse = imbalance <= -0.20

            raw_score = 0.50
            if toxic_surge:
                raw_score += 0.25
            if dev_adverse:
                raw_score += 0.15
            if imb_adverse:
                raw_score += 0.10

            score = min(1.0, raw_score)

            # RTT (85ms) 以上の先回り可能時間がある場合、即時キャンセル推奨
            lead_ms = max(0.0, 150.0 - (age_ms % 150.0))
            if score >= 0.70 and lead_ms >= self.min_lead_ms_threshold:
                cancel_recommended = True
                self.intercepted_kills += 1

            # 状態遷移
            if toxic_surge:
                if is_buy:
                    self.state_buy = "OPP_TAKER"
                else:
                    self.state_sell = "OPP_TAKER"
            elif tip_depth <= 0.01:
                if is_buy:
                    self.state_buy = "NO_REFILL"
                else:
                    self.state_sell = "NO_REFILL"

            # 板が完全に回復（baselineの90%以上）した場合は解除
            if tip_depth >= baseline * 0.90:
                if is_buy:
                    self.state_buy = "NORMAL"
                else:
                    self.state_sell = "NORMAL"

        return {
            "score": score,
            "cancel_recommended": cancel_recommended,
            "lead_ms": lead_ms,
        }
