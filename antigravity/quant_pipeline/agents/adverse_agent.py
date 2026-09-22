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

import os
import json
import time
from typing import Dict, Any, Optional, List
from ..schema import OrderbookMicroSnapshot
from ..event_bus import EventBus
from ..adverse_excursion import AdverseExcursionTracker
from ..toxic_flow_analyzer import ToxicFlowAnalyzer
from ..adverse_score_engine import AdverseScoreEngine
from ..adverse_victim_anatomy import AdverseVictimAnatomy, BoardFeatureBuffer


class AdverseResearchAgent:
    """
    【研究員】Adverse Selection の感知と分析。発注の門番ではない。
    出力はエピソードごとの ON/OFF。スコア帯では注文を止めない。
    - S1. Adverse Excursion (AE_100ms〜30s) のリアルタイム測定・分析
    - S2. Toxic Flow (Toxic Score 0-100) のリアルタイム採点
    - S3. Capture Rate 分析・管理
    - 【最終成果物】統合 Adverse Score (0〜100: 安全/注意/危険/発注禁止) 算出
    - トキシック・テイカー直撃時の緊急指値退避 (Emergency Cancel)
    """

    def __init__(
        self,
        bus: EventBus,
        min_lead_ms_threshold: float = 85.0,  # 研究用: 確認リードが RTT 以上か
        depletion_ratio_threshold: float = 0.50,  # tip ≤ baseline×この比 かつ絶対減少で枯渇候補
        toxic_taker_threshold: float = 0.015,  # 旧0.50は窓成行量と単位不一致で confirm=0 だった
        min_arm_tip: float = 0.08,  # これ未満の tip では PRE に入らない
        min_abs_drop: float = 0.05,  # fire に必要な絶対減少量 (BTC); 比だけだと薄い板で誤爆
        max_episode_age_ms: float = 2500.0,
        save_dir: str = "/home/azureuser/antigravity/data",
    ):
        self.bus = bus
        self.save_dir = save_dir
        self.min_lead_ms_threshold = min_lead_ms_threshold
        self.depletion_ratio_threshold = depletion_ratio_threshold
        self.toxic_taker_threshold = toxic_taker_threshold
        self.min_arm_tip = min_arm_tip
        self.min_abs_drop = min_abs_drop
        self.max_episode_age_ms = max_episode_age_ms
        self.device_version = "episode_sm_v2"
        self._device_sig = None
        self._last_device_write = 0.0

        # S1. Adverse Excursion 分析器の統合
        self.excursion_tracker = AdverseExcursionTracker(save_dir=save_dir)

        # S2. Toxic Flow 分析器の統合
        self.toxic_analyzer = ToxicFlowAnalyzer()

        # 【最終成果物】Adverse Score エンジン (30% AE, 25% Toxic, 20% Capture, 15% Latency, 10% Inventory)
        self.score_engine = AdverseScoreEngine(save_dir=save_dir)

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
        self.confirmed_episodes = 0
        self.false_episodes = 0
        self.cooldown_until_buy = 0.0
        self.cooldown_until_sell = 0.0
        self.taker_seen_buy = False
        self.taker_seen_sell = False
        self.cum_opp_taker_buy = 0.0
        self.cum_opp_taker_sell = 0.0
        self.lead_ms_buy = 0.0
        self.lead_ms_sell = 0.0
        self.estimated_lead_ms_history: List[float] = []
        self.latest_state = {
            "avoidance_on": False,
            "adverse_score": 0.0,
            "cancel_recommendation": False,
            "adverse_side": "none",
        }
        # 研究モード: victim_anatomy = 常時スコア縮退・UMM toxic 直前特徴解剖
        # full_legacy = 旧 episode/toxic/score 毎tick（非推奨）
        # advance_from_umm | victim_anatomy（互換）— いずれも legacy episode/score 停止
        self.research_mode = os.environ.get("ADVERSE_RESEARCH_MODE", "advance_from_umm")
        self.board_buf = BoardFeatureBuffer(maxlen=180)
        self.anatomy = AdverseVictimAnatomy()
        self._hist_len_before_tick = 0
        self._last_legacy_eval = 0.0
        self._legacy_interval_sec = float(os.environ.get("ADVERSE_LEGACY_INTERVAL_SEC", "300"))

        # EventBus購読
        self.bus.subscribe("orderbook_micro", self.on_orderbook)
        self.bus.subscribe("order_filled", self._on_order_filled_event)
        self.bus.subscribe("trade_entry", self._on_order_filled_event)
        self.bus.subscribe("order_closed", self._on_order_closed_event)
        self.bus.subscribe("trade_close", self._on_order_closed_event)

    def track_entry(
        self,
        trade_id: str,
        side: str,
        entry_price: float,
        entry_time: Optional[float] = None,
        strategy_name: str = "default",
        meta: Optional[Dict[str, Any]] = None,
    ):
        """約定時の Adverse Excursion (AE) 追跡を開始。UMM 教師用に直前〜数秒前板を付与。"""
        et = float(entry_time) if entry_time is not None else time.time()
        meta = dict(meta or {})
        asof = self.board_buf.asof(et) or self.board_buf.latest()
        if asof is not None:
            meta["asof_board"] = asof
            side_l = str(side or "").lower()
            meta["pred_adverse_score"] = self.anatomy.simple_adverse_score(asof, side_l or "buy")
        pre5 = self.board_buf.asof(et - 5.0)
        pre10 = self.board_buf.asof(et - 10.0)
        if pre5 is not None:
            meta["asof_board_pre5s"] = pre5
        if pre10 is not None:
            meta["asof_board_pre10s"] = pre10
        meta["adverse_research_mode"] = self.research_mode
        meta["data_source"] = strategy_name
        return self.excursion_tracker.track_entry(
            trade_id=trade_id,
            side=side,
            entry_price=entry_price,
            entry_time=et,
            strategy_name=strategy_name,
            meta=meta,
        )

    def on_close(
        self,
        trade_id: str,
        exit_price: float,
        pnl_bp: float = 0.0,
        exit_reason: str = "",
        theory_spread_bp: Optional[float] = None,
    ):
        """トレード決済時のフック (Capture Rate 計算とサマリー永続化)"""
        return self.excursion_tracker.on_close(
            trade_id=trade_id,
            exit_price=exit_price,
            pnl_bp=pnl_bp,
            exit_reason=exit_reason,
            theory_spread_bp=theory_spread_bp,
        )

    def _on_order_filled_event(self, event_data: Dict[str, Any]):
        """EventBus からの約定通知ハンドラ"""
        if not isinstance(event_data, dict):
            return
        trade_id = str(event_data.get("trade_id", f"trade_{int(time.time()*1000)}"))
        side = str(event_data.get("side", "buy"))
        entry_price = float(event_data.get("entry_price", event_data.get("price", 0.0)))
        entry_time = event_data.get("timestamp", time.time())
        strategy_name = event_data.get("strategy_name", event_data.get("strategy", "pipeline"))
        if entry_price > 0:
            self.track_entry(
                trade_id=trade_id,
                side=side,
                entry_price=entry_price,
                entry_time=entry_time,
                strategy_name=strategy_name,
                meta=event_data,
            )

    def _on_order_closed_event(self, event_data: Dict[str, Any]):
        """EventBus からの決済通知ハンドラ"""
        if not isinstance(event_data, dict):
            return
        trade_id = str(event_data.get("trade_id", ""))
        exit_price = float(event_data.get("exit_price", event_data.get("price", 0.0)))
        pnl_bp = float(event_data.get("pnl_bp", 0.0))
        exit_reason = str(event_data.get("reason", ""))
        theory_spread_bp = event_data.get("theory_spread_bp")
        if trade_id:
            self.on_close(
                trade_id=trade_id,
                exit_price=exit_price,
                pnl_bp=pnl_bp,
                exit_reason=exit_reason,
                theory_spread_bp=theory_spread_bp,
            )

    def _board_feat(self, snap: OrderbookMicroSnapshot, now_ts: float) -> Dict[str, Any]:
        mid = float(snap.mid_price)
        spread = max(0.0, float(snap.best_ask) - float(snap.best_bid))
        tb = float(snap.taker_volume_bid or 0.0)
        ta = float(snap.taker_volume_ask or 0.0)
        cancel_rate = float(getattr(snap, "cancel_rate", 0.0) or 0.0)
        refill_rate = float(getattr(snap, "refill_rate", 0.0) or 0.0)
        return {
            "_ts": now_ts,
            "mid": mid,
            "best_bid": float(snap.best_bid),
            "best_ask": float(snap.best_ask),
            "spread": spread,
            "spread_bp": (spread / mid * 10000.0) if mid > 0 else 0.0,
            "bid_depth_1": float(snap.bid_depth_1),
            "ask_depth_1": float(snap.ask_depth_1),
            "imbalance": float(snap.imbalance),
            "micro_dev": float(snap.micro_dev),
            "taker_volume_bid": tb,
            "taker_volume_ask": ta,
            "taker_total": tb + ta,
            "taker_aggressiveness": float(getattr(snap, "taker_aggressiveness", 0.0) or 0.0),
            "cancel_rate": cancel_rate,
            "refill_rate": refill_rate,
            "cancel_minus_refill": cancel_rate - refill_rate,
            "latency_ms": float(getattr(snap, "latency_ms", 0.0) or 0.0),
        }

    def on_orderbook(self, snap: OrderbookMicroSnapshot):
        ts_raw = float(snap.timestamp)
        if ts_raw > 1e12:
            now_ts = ts_raw / 1000.0
        elif ts_raw > 1e9:
            now_ts = ts_raw
        else:
            now_ts = time.time()

        # 直前板バッファ（victim anatomy の本体）
        feat = self._board_feat(snap, now_ts)
        self.board_buf.push(now_ts, feat)

        # AE tick（TOXIC/NORMAL ラベル確定に必要）
        hist_before = len(self.excursion_tracker.history_records)
        self.excursion_tracker.on_tick(
            current_price=snap.mid_price,
            timestamp=now_ts,
            best_bid=snap.best_bid,
            best_ask=snap.best_ask,
        )
        for rec in self.excursion_tracker.history_records[hist_before:]:
            try:
                self.anatomy.ingest_umm_completed(rec.to_full_dict())
            except Exception:
                pass

        # --- advance / anatomy モード: 常時 episode/toxic/score を止める ---
        if self.research_mode in ("advance_from_umm", "victim_anatomy"):
            if now_ts - self._last_legacy_eval >= self._legacy_interval_sec:
                self._last_legacy_eval = now_ts
                self.latest_state = {
                    "timestamp": snap.timestamp,
                    "agent_rank": "RESEARCH_AGENT",
                    "research_mode": "advance_from_umm",
                    "avoidance_on": False,
                    "adverse_side": "none",
                    "adverse_score": 0.0,
                    "research_score": 0.0,
                    "cancel_recommendation": False,
                    "wire": "NO",
                    "enforce": 0,
                    "advance_counts": dict(self.anatomy._counts),
                    "anatomy_counts": dict(self.anatomy._counts),
                    "note": "UMM-supervised advance research only; legacy episode/score OFF",
                }
                self._persist_device_state({
                    **self.latest_state,
                    "total_episodes": self.total_episodes,
                    "confirmed_episodes": self.confirmed_episodes,
                    "false_episodes": self.false_episodes,
                    "confirm_rate": None,
                    "episode_id": 0,
                    "lead_ms_estimated": 0.0,
                })
            return self.latest_state

        # -------------------------------------------------------------
        # full_legacy: 旧 episode / toxic / score（非推奨・明示時のみ）
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

        # -------------------------------------------------------------
        # S2. Toxic Flow (トキシック・フロー) リアルタイム採点
        # -------------------------------------------------------------
        toxic_buy = self.toxic_analyzer.calculate_toxic_score(
            side="buy",
            imbalance=snap.imbalance,
            taker_buy=snap.taker_volume_ask,  # ask買い
            taker_sell=snap.taker_volume_bid, # bid売り
            cancel_rate=snap.cancel_rate,
            refill_rate=snap.refill_rate,
            depth_1=snap.bid_depth_1,
            depth_3=snap.total_bid_depth * 0.6,
            depth_5=snap.total_bid_depth,
        )
        toxic_sell = self.toxic_analyzer.calculate_toxic_score(
            side="sell",
            imbalance=snap.imbalance,
            taker_buy=snap.taker_volume_ask,
            taker_sell=snap.taker_volume_bid,
            cancel_rate=snap.cancel_rate,
            refill_rate=snap.refill_rate,
            depth_1=snap.ask_depth_1,
            depth_3=snap.total_ask_depth * 0.6,
            depth_5=snap.total_ask_depth,
        )

        toxic_dominant = toxic_buy if toxic_buy["toxic_score"] >= toxic_sell["toxic_score"] else toxic_sell
        toxic_score = toxic_dominant["toxic_score"]
        self._persist_toxic_state(toxic_dominant)

        ae_latest = self.excursion_tracker.get_latest_standard_record()
        ae_summary = self.excursion_tracker.get_summary_stats()

        # -------------------------------------------------------------
        # 【最終成果物】5大要素統合 Adverse Score (0〜100) 算出
        # 30% AE + 25% Toxic + 20% Capture + 15% Latency + 10% Inventory
        # -------------------------------------------------------------
        unified_score_res = self.score_engine.calculate_adverse_score(
            ae_1s=ae_latest.get("ae_1s"),
            ae_3s=ae_latest.get("ae_3s"),
            mae_bp=ae_latest.get("mae_bp"),
            toxic_score=toxic_score,
            capture_rate_pct=ae_summary.get("capture_rate_stats", {}).get("avg_capture_rate_pct"),
            latency_ms=getattr(snap, "latency_ms", 1.5),
            inventory_btc=0.0,
            holding_time_sec=0.0,
            micro_dev=snap.micro_dev,
        )
        total_adverse_score = unified_score_res["adverse_score"]
        tier = unified_score_res["tier"]
        avoidance_on = bool(buy_res.get("avoidance_on") or sell_res.get("avoidance_on"))
        if buy_res.get("avoidance_on") and not sell_res.get("avoidance_on"):
            adverse_side = "buy"
            lead_ms_est = buy_res.get("lead_ms", 0.0)
            active_episode_id = self.episode_id_buy
        elif sell_res.get("avoidance_on"):
            adverse_side = "sell"
            lead_ms_est = sell_res.get("lead_ms", 0.0)
            active_episode_id = self.episode_id_sell

        adverse_state = {
            "timestamp": snap.timestamp,
            "agent_rank": "RESEARCH_AGENT",
            "adverse_side": adverse_side,
            "adverse_score": total_adverse_score,
            "research_score": total_adverse_score,
            "avoidance_on": avoidance_on,
            "tier": tier,
            "tier_code": unified_score_res["tier_code"],
            "toxic_score": toxic_score,
            "toxic_level": toxic_dominant["level"],
            "cancel_recommendation": False,
            "lead_ms_estimated": round(lead_ms_est, 1),
            "episode_id": active_episode_id,
            "buy_state": self.state_buy,
            "sell_state": self.state_sell,
            "total_episodes": self.total_episodes,
            "confirmed_episodes": self.confirmed_episodes,
            "false_episodes": self.false_episodes,
            "confirm_rate": (
                round(self.confirmed_episodes / self.total_episodes, 4)
                if self.total_episodes > 0
                else None
            ),
            "intercepted_kills": self.intercepted_kills,
            "device_version": self.device_version,
            "ae_latest": ae_latest,
            "ae_summary": ae_summary,
            "toxic_dominant": toxic_dominant,
            "unified_score_details": unified_score_res,
        }

        self.latest_state = adverse_state
        self.bus.publish("adverse_research_state", adverse_state)
        self._persist_device_state(adverse_state)

        if avoidance_on:
            verdict = "ADVERSE_ON"
            primary_action = "avoid_on"
            explanation = (
                f"ON side={adverse_side} episode={active_episode_id} "
                f"lead_ms={lead_ms_est:.0f} research_score={total_adverse_score:.0f}"
            )
        else:
            verdict = "ADVERSE_OFF"
            primary_action = "avoid_off"
            explanation = f"OFF research_score={total_adverse_score:.0f} episodes={self.total_episodes}"

        if ae_latest and "ae_1s" in ae_latest:
            explanation += f" [AE_1s: {ae_latest['ae_1s']:+.1f}bp]"

        from ..schema import AgentConclusion
        from dataclasses import asdict

        conclusion = AgentConclusion(
            agent_name="AdverseResearchAgent",
            timestamp=snap.timestamp,
            verdict=verdict,
            confidence=round(adverse_score, 3),
            primary_action=primary_action,
            metrics={
                "agent_rank": "RESEARCH_AGENT",
                "avoidance_on": avoidance_on,
                "adverse_side": adverse_side,
                "adverse_score": total_adverse_score,
                "research_score": total_adverse_score,
                "lead_ms_estimated": lead_ms_est,
                "buy_state": self.state_buy,
                "sell_state": self.state_sell,
                "episode_id": active_episode_id,
                "total_episodes": self.total_episodes,
                "intercepted_kills": self.intercepted_kills,
                "ae_latest": ae_latest,
                "ae_summary": ae_summary,
            },
            parameters={"min_lead_ms_threshold": self.min_lead_ms_threshold},
            hard_veto=False,
            emergency_cancel=False,
            explanation=explanation,
        )
        self.latest_conclusion = conclusion
        self.bus.publish("adverse_conclusion", asdict(conclusion))
        return adverse_state

    def get_latest_conclusion(self) -> Optional[Any]:
        return getattr(self, "latest_conclusion", None)


    def _confirm_threshold(self, baseline: float) -> float:
        """窓成行量に合わせた confirm 閾値。絶対下限 + baseline 比の小さい方。"""
        return max(self.toxic_taker_threshold, min(0.08, baseline * 0.20))

    def _end_episode(self, side: str, now_ts: float) -> None:
        if side == "buy":
            if not self.taker_seen_buy and self.state_buy in ("DEPLETING", "NO_REFILL", "OPP_TAKER"):
                self.false_episodes += 1
            self.state_buy = "NORMAL"
            self.cooldown_until_buy = now_ts + 0.5
            self.taker_seen_buy = False
            self.cum_opp_taker_buy = 0.0
            self.lead_ms_buy = 0.0
        else:
            if not self.taker_seen_sell and self.state_sell in ("DEPLETING", "NO_REFILL", "OPP_TAKER"):
                self.false_episodes += 1
            self.state_sell = "NORMAL"
            self.cooldown_until_sell = now_ts + 0.5
            self.taker_seen_sell = False
            self.cum_opp_taker_sell = 0.0
            self.lead_ms_sell = 0.0

    def _evaluate_side(
        self,
        side: str,
        tip_depth: float,
        opp_taker_vol: float,
        micro_dev: float,
        imbalance: float,
        now_ts: float,
    ) -> Dict[str, Any]:
        """片側エピソード v2。
        fire = tip 比枯渇 AND 絶対減少。confirm = エピソード内累積反対成行。
        cancel_recommended は常に False（研究専用・執行非接続）。
        """
        is_buy = (side == "buy")
        cur_state = self.state_buy if is_buy else self.state_sell
        baseline = self.tip_baseline_buy if is_buy else self.tip_baseline_sell
        ep_start = self.episode_start_ts_buy if is_buy else self.episode_start_ts_sell
        cooldown_until = self.cooldown_until_buy if is_buy else self.cooldown_until_sell
        off = {"score": 0.0, "cancel_recommended": False, "lead_ms": 0.0, "avoidance_on": False}

        if cur_state == "NORMAL":
            if now_ts < cooldown_until:
                return off
            if tip_depth >= self.min_arm_tip:
                if is_buy:
                    self.tip_baseline_buy = tip_depth
                    self.state_buy = "PRE_ADVERSE"
                else:
                    self.tip_baseline_sell = tip_depth
                    self.state_sell = "PRE_ADVERSE"
            return off

        if cur_state == "PRE_ADVERSE":
            # tip が腕を解くほど薄く、かつ fire 条件未達 → アーム解除（ノイズ）
            if tip_depth < self.min_arm_tip * 0.5 and (
                baseline <= 0
                or tip_depth > baseline * self.depletion_ratio_threshold
                or (baseline - tip_depth) < self.min_abs_drop
            ):
                if is_buy:
                    self.state_buy = "NORMAL"
                    self.tip_baseline_buy = 0.0
                else:
                    self.state_sell = "NORMAL"
                    self.tip_baseline_sell = 0.0
                return off

            drop = baseline - tip_depth if baseline > 0 else 0.0
            fire = (
                baseline >= self.min_arm_tip
                and tip_depth <= baseline * self.depletion_ratio_threshold
                and drop >= self.min_abs_drop
            )
            if fire:
                if is_buy:
                    self.state_buy = "DEPLETING"
                    self.episode_start_ts_buy = now_ts
                    self.episode_id_buy += 1
                    self.taker_seen_buy = False
                    self.cum_opp_taker_buy = 0.0
                    self.lead_ms_buy = 0.0
                else:
                    self.state_sell = "DEPLETING"
                    self.episode_start_ts_sell = now_ts
                    self.episode_id_sell += 1
                    self.taker_seen_sell = False
                    self.cum_opp_taker_sell = 0.0
                    self.lead_ms_sell = 0.0
                self.total_episodes += 1
                self.intercepted_kills += 1
                return {"score": 1.0, "cancel_recommended": False, "lead_ms": 0.0, "avoidance_on": True}
            if baseline > 0 and tip_depth > baseline * 1.2:
                if is_buy:
                    self.tip_baseline_buy = tip_depth
                else:
                    self.tip_baseline_sell = tip_depth
            return off

        if cur_state in ("DEPLETING", "NO_REFILL", "OPP_TAKER"):
            age_ms = max(0.0, (now_ts - ep_start) * 1000.0) if ep_start > 0 else 0.0
            if age_ms > self.max_episode_age_ms or (baseline > 0 and tip_depth >= baseline * 0.90):
                self._end_episode(side, now_ts)
                return off

            if is_buy:
                self.cum_opp_taker_buy += max(0.0, float(opp_taker_vol or 0.0))
                cum = self.cum_opp_taker_buy
                seen = self.taker_seen_buy
            else:
                self.cum_opp_taker_sell += max(0.0, float(opp_taker_vol or 0.0))
                cum = self.cum_opp_taker_sell
                seen = self.taker_seen_sell

            lead_ms = self.lead_ms_buy if is_buy else self.lead_ms_sell
            thr = self._confirm_threshold(baseline)
            if (not seen) and cum >= thr:
                lead_ms = age_ms
                self.confirmed_episodes += 1
                self.estimated_lead_ms_history.append(lead_ms)
                if is_buy:
                    self.taker_seen_buy = True
                    self.lead_ms_buy = lead_ms
                    self.state_buy = "OPP_TAKER"
                else:
                    self.taker_seen_sell = True
                    self.lead_ms_sell = lead_ms
                    self.state_sell = "OPP_TAKER"
            elif tip_depth <= 0.01:
                if is_buy:
                    self.state_buy = "NO_REFILL"
                else:
                    self.state_sell = "NO_REFILL"
            return {"score": 1.0, "cancel_recommended": False, "lead_ms": lead_ms, "avoidance_on": True}

        return off

    def _persist_device_state(self, state: Dict[str, Any]) -> None:
        sig = (
            bool(state.get("avoidance_on")),
            state.get("adverse_side", "none"),
            state.get("episode_id", 0),
            state.get("confirmed_episodes", 0),
            state.get("false_episodes", 0),
        )
        now = time.time()
        changed = sig != self._device_sig
        if not changed and now - self._last_device_write < 1.0:
            return
        try:
            target_path = os.path.join(self.save_dir, "adverse_device_state.json")
            tmp_path = target_path + ".tmp"
            payload = {
                "timestamp": now,
                "device_version": self.device_version,
                "avoidance_on": sig[0],
                "adverse_side": sig[1],
                "episode_id": sig[2],
                "total_episodes": state.get("total_episodes", 0),
                "confirmed_episodes": state.get("confirmed_episodes", 0),
                "false_episodes": state.get("false_episodes", 0),
                "confirm_rate": state.get("confirm_rate"),
                "research_score": state.get("research_score", 0.0),
                "lead_ms_estimated": state.get("lead_ms_estimated", 0.0),
            }
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_path, target_path)
            self._last_device_write = now
            self._device_sig = sig
        except Exception:
            pass

    def _persist_toxic_state(self, toxic_data: Dict[str, Any]):
        """S2. Toxic Flow 状態のアトミック保存。1秒に1回。"""
        now = time.time()
        if now - self._last_toxic_write < 1.0:
            return
        try:
            target_path = os.path.join(self.save_dir, "adverse_toxic_state.json")
            tmp_path = target_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(toxic_data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, target_path)
            self._last_toxic_write = now
        except Exception:
            pass

