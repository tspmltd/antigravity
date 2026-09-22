"""
PEG Research Agent (PEG 専用研究エージェント)
============================================
執行非接続 · WIRE=NO · ENFORCE=0 · research_only。

【参照 CSR / 正本】
  GAPCORE:
    - CSR-022  PEG horizon @10/30/60s（mid@horizon をラベル）
    - CSR-023/024  Adverse 込み PEG EV（特徴に toxic/adverse 代理を記録）
    - CSR-025  pegDiff10 / pegDiff30_10 トレンドゲート
    - CSR-091  FuturePEG place×horizon
    - CSR-148  exhaust_horizon（終焉）
    - CSR-210o 細波1–2bp + MON 継続
    - CSR-231/232 PEG 説明特徴 (c−r, refill, taker, hole 代理)
    - CSR-466〜499 TF2BP 系 · ENFORCE=0 · n未達で経済判定禁止
  ANTIGRAVITY:
    - MEM §13  TF2BP_PEG_v2 Model3+1 OBSERVATION
    - CSR-499 / CSR-504  Baseline ピン（TF2BP / UMM）

【日々観測タスク】
  1. direction_1_5bp — 1〜5bp 帯の方向（horizon 10/30/60s · CSR-022）
  2. trend_continue  — 細波＋同方向 flow の継続（CSR-210o / CSR-025）
  3. trend_end       — 伸びの終焉・逆行（CSR-148 exhaust）
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import asdict
from typing import Any, Deque, Dict, List, Optional, Tuple

from ..event_bus import EventBus
from ..schema import OrderbookMicroSnapshot, AgentConclusion
from ..peg_research_store import PegResearchStore


CSR_REFS = [
    "CSR-022", "CSR-023", "CSR-024", "CSR-025", "CSR-091",
    "CSR-148", "CSR-210o", "CSR-231", "CSR-232", "CSR-499",
]


def _bp(a: float, b: float) -> float:
    if b <= 0:
        return 0.0
    return (a - b) / b * 10000.0


def _snap_ts(snap: OrderbookMicroSnapshot) -> float:
    ts = float(snap.timestamp)
    if ts > 1e12:
        return ts / 1000.0
    if ts > 1e9:
        return ts
    return time.time()


class PegResearchAgent:
    AGENT_NAME = "PegResearchAgent"
    AGENT_VERSION = "peg_research_v1_csr"

    def __init__(
        self,
        bus: Optional[EventBus] = None,
        store: Optional[PegResearchStore] = None,
        pred_cooldown_sec: float = 8.0,
        history_len: int = 180,
    ):
        self.bus = bus
        self.store = store or PegResearchStore()
        self.pred_cooldown_sec = pred_cooldown_sec
        self.mids: Deque[Tuple[float, float]] = deque(maxlen=history_len)
        self.pending: List[Dict[str, Any]] = []
        self.last_pred_ts: Dict[str, float] = {
            "direction_1_5bp": 0.0,
            "trend_continue": 0.0,
            "trend_end": 0.0,
        }
        self.stats = {"predictions": 0, "labeled": 0, "pending": 0}
        self.latest_state: Dict[str, Any] = {}
        self.latest_conclusion: Optional[AgentConclusion] = None
        if bus is not None:
            bus.subscribe("orderbook_micro", self.on_orderbook)

    def on_orderbook(self, snap: OrderbookMicroSnapshot) -> Dict[str, Any]:
        now = _snap_ts(snap)
        mid = float(snap.mid_price)
        self.mids.append((now, mid))
        self._resolve_pending(now, mid)

        features = self._features(snap, now, mid)
        preds: List[Dict[str, Any]] = []

        # --- 1) direction_1_5bp · CSR-022 multi-horizon ---
        if now - self.last_pred_ts["direction_1_5bp"] >= self.pred_cooldown_sec:
            mom = features["mom_5s_bp"]
            # 細波スケールで方向仮説（CSR-210o の 1–2bp 入口と整合、上限5bp）
            if 0.8 <= abs(mom) <= 6.0:
                pred = "up" if mom > 0 else "down"
                conf = min(1.0, abs(mom) / 5.0)
                root = uuid.uuid4().hex[:10]
                for hz in (10.0, 30.0, 60.0):  # CSR-022
                    rec = self._emit(
                        task="direction_1_5bp",
                        pred=pred,
                        confidence=conf,
                        horizon_sec=hz,
                        features=features,
                        now=now,
                        mid=mid,
                        meta={
                            "csr": ["CSR-022", "CSR-210o", "CSR-499"],
                            "root_id": root,
                            "band_bp": [1.0, 5.0],
                        },
                    )
                    preds.append(rec)
                self.last_pred_ts["direction_1_5bp"] = now

        # --- 2) trend_continue · CSR-025 pegDiff + CSR-210o MON ---
        if now - self.last_pred_ts["trend_continue"] >= self.pred_cooldown_sec:
            d10 = features["peg_diff_10"]
            d30_10 = features["peg_diff_30_10"]
            # CSR-025: pegDiff10 < -0.5 ∧ pegDiff30_10 ≥ 0.5 が |trend| 最大帯
            # 研究では両方向対称化: |d10| 小さく伸び、30-10 が同符号で乗る
            fine = 1.0 <= abs(features["mom_5s_bp"]) <= 5.0
            mon_ok = (
                (features["mom_5s_bp"] > 0 and features["net_taker"] >= 0)
                or (features["mom_5s_bp"] < 0 and features["net_taker"] <= 0)
            )
            csr025_shape = abs(d10) <= 2.0 and abs(d30_10) >= 0.5 and (
                (d10 >= 0 and d30_10 >= 0) or (d10 <= 0 and d30_10 <= 0)
            )
            if fine and (mon_ok or csr025_shape) and abs(features["mom_15s_bp"]) >= 1.5:
                side = "up" if features["mom_15s_bp"] > 0 else "down"
                rec = self._emit(
                    task="trend_continue",
                    pred="continue",
                    confidence=min(1.0, abs(features["mom_15s_bp"]) / 8.0),
                    horizon_sec=18.0,  # CSR-210o MON18 相当
                    features=features,
                    now=now,
                    mid=mid,
                    meta={
                        "csr": ["CSR-025", "CSR-210o", "CSR-231"],
                        "trend_side": side,
                        "peg_diff_10": d10,
                        "peg_diff_30_10": d30_10,
                    },
                )
                preds.append(rec)
                self.last_pred_ts["trend_continue"] = now

        # --- 3) trend_end · CSR-148 exhaust ---
        if now - self.last_pred_ts["trend_end"] >= self.pred_cooldown_sec:
            mom30 = features["mom_30s_bp"]
            if abs(mom30) >= 5.0:
                rev = (mom30 > 0 and features["taker_volume_bid"] >= 0.01) or (
                    mom30 < 0 and features["taker_volume_ask"] >= 0.01
                )
                # CSR-231: (c−r) 高 + refill 薄 = 崩れ
                cr = features["cancel_minus_refill"]
                exhaust = cr >= 0.35 and features["refill_rate"] <= 0.20
                # CSR-232: slow_grind / spr 代理
                spr_collapse = features["spread_bp"] <= 1.2 and abs(mom30) >= 5.0
                if rev or exhaust or spr_collapse:
                    side = "up" if mom30 > 0 else "down"
                    conf = 0.35 + (0.25 if rev else 0) + (0.2 if exhaust else 0) + (0.2 if spr_collapse else 0)
                    rec = self._emit(
                        task="trend_end",
                        pred="end",
                        confidence=min(1.0, conf),
                        horizon_sec=15.0,
                        features=features,
                        now=now,
                        mid=mid,
                        meta={
                            "csr": ["CSR-148", "CSR-231", "CSR-232"],
                            "trend_side": side,
                            "rev": rev,
                            "exhaust": exhaust,
                            "spr_collapse": spr_collapse,
                        },
                    )
                    preds.append(rec)
                    self.last_pred_ts["trend_end"] = now

        self.stats["pending"] = len(self.pending)
        self.latest_state = {
            "agent": self.AGENT_NAME,
            "version": self.AGENT_VERSION,
            "research_only": True,
            "wire": "NO",
            "enforce": 0,
            "csr_refs": CSR_REFS,
            "mid": mid,
            "features": features,
            "stats": dict(self.stats),
            "pending": len(self.pending),
            "last_preds": [f"{p['task']}@{p['horizon_sec']:.0f}s={p['pred']}" for p in preds],
        }
        self.store.write_state(self.latest_state)

        verdict = "RESEARCH_PRED" if preds else "RESEARCH_IDLE"
        explanation = (
            f"PEG研究[{self.AGENT_VERSION}] mom5={features['mom_5s_bp']:+.2f} "
            f"d10={features['peg_diff_10']:+.2f} d30_10={features['peg_diff_30_10']:+.2f} "
            f"pending={len(self.pending)} labeled={self.stats['labeled']} WIRE=NO"
        )
        conclusion = AgentConclusion(
            agent_name=self.AGENT_NAME,
            timestamp=snap.timestamp,
            verdict=verdict,
            confidence=0.0,
            primary_action="hold",
            metrics={
                "research_only": True,
                "wire": "NO",
                "enforce": 0,
                "version": self.AGENT_VERSION,
                "csr_refs": CSR_REFS,
                "stats": dict(self.stats),
                "mom_5s_bp": features["mom_5s_bp"],
                "peg_diff_10": features["peg_diff_10"],
                "peg_diff_30_10": features["peg_diff_30_10"],
                "pending": len(self.pending),
            },
            parameters={"csr_refs": CSR_REFS},
            hard_veto=False,
            emergency_cancel=False,
            explanation=explanation,
        )
        self.latest_conclusion = conclusion
        if self.bus is not None:
            self.bus.publish("peg_research_conclusion", asdict(conclusion))
            self.bus.publish("peg_research_state", self.latest_state)
        return self.latest_state

    def _features(self, snap: OrderbookMicroSnapshot, now: float, mid: float) -> Dict[str, Any]:
        mom_5 = self._mom_bp(now, 5.0, mid)
        mom_10 = self._mom_bp(now, 10.0, mid)
        mom_15 = self._mom_bp(now, 15.0, mid)
        mom_30 = self._mom_bp(now, 30.0, mid)
        # CSR-025 proxies
        peg_diff_10 = round(mom_10, 3)
        peg_diff_30_10 = round(mom_30 - mom_10, 3)

        spread = max(0.0, float(snap.best_ask) - float(snap.best_bid))
        cancel_rate = float(getattr(snap, "cancel_rate", 0.0) or 0.0)
        refill_rate = float(getattr(snap, "refill_rate", 0.0) or 0.0)
        aggr = float(getattr(snap, "taker_aggressiveness", 0.0) or 0.0)
        taker_bid = float(snap.taker_volume_bid or 0.0)
        taker_ask = float(snap.taker_volume_ask or 0.0)
        taker_total = taker_bid + taker_ask
        net_taker = taker_ask - taker_bid

        # CSR-231 PEG input candidates (available proxies)
        cancel_minus_refill = cancel_rate - refill_rate
        tip_bid = float(snap.bid_depth_1)
        tip_ask = float(snap.ask_depth_1)
        hole_proxy = 1.0 if min(tip_bid, tip_ask) <= 0.01 else 0.0
        one_way = 1.0 if (tip_bid <= 0.001) != (tip_ask <= 0.001) else 0.0

        opp = tip_ask
        eff = max(0.001, opp * (1.0 - cancel_rate + 0.5 * refill_rate))
        depth_factor = min(max(eff / 0.20, 0.0), 1.0)
        peg_ratio = min(max(0.975 - depth_factor * 0.060 + min(aggr * 0.020, 0.020), 0.910), 0.985)

        return {
            "mid": mid,
            "best_bid": float(snap.best_bid),
            "best_ask": float(snap.best_ask),
            "spread": spread,
            "spread_bp": round((spread / mid * 10000.0) if mid > 0 else 0.0, 3),
            "bid_depth_1": tip_bid,
            "ask_depth_1": tip_ask,
            "imbalance": float(snap.imbalance),
            "micro_dev": float(snap.micro_dev),
            "taker_volume_bid": taker_bid,
            "taker_volume_ask": taker_ask,
            "taker_total": round(taker_total, 4),
            "net_taker": round(net_taker, 4),
            "taker_aggressiveness": aggr,
            "cancel_rate": cancel_rate,
            "refill_rate": refill_rate,
            "cancel_minus_refill": round(cancel_minus_refill, 4),
            "hole_proxy": hole_proxy,
            "one_way": one_way,
            "mom_5s_bp": round(mom_5, 3),
            "mom_10s_bp": round(mom_10, 3),
            "mom_15s_bp": round(mom_15, 3),
            "mom_30s_bp": round(mom_30, 3),
            "peg_diff_10": peg_diff_10,
            "peg_diff_30_10": peg_diff_30_10,
            "peg_ratio_est": round(peg_ratio, 4),
            "n_history": len(self.mids),
        }

    def _mom_bp(self, now: float, lookback: float, mid: float) -> float:
        target = now - lookback
        base = None
        for ts, m in self.mids:
            if ts <= target:
                base = m
            else:
                break
        if base is None:
            if not self.mids:
                return 0.0
            base = self.mids[0][1]
        return _bp(mid, base)

    def _emit(
        self,
        task: str,
        pred: str,
        confidence: float,
        horizon_sec: float,
        features: Dict[str, Any],
        now: float,
        mid: float,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        rec = {
            "episode_id": f"{task}-{uuid.uuid4().hex[:10]}",
            "agent": self.AGENT_NAME,
            "version": self.AGENT_VERSION,
            "research_only": True,
            "wire": "NO",
            "enforce": 0,
            "task": task,
            "pred": pred,
            "confidence": round(confidence, 3),
            "horizon_sec": horizon_sec,
            "ts": now,
            "ts_jst": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "mid0": mid,
            "features": features,
            "meta": meta or {},
            "label_due_ts": now + horizon_sec,
            "labeled": False,
        }
        self.pending.append(rec)
        self.store.append_prediction(rec)
        self.stats["predictions"] += 1
        return rec

    def _resolve_pending(self, now: float, mid: float) -> None:
        keep: List[Dict[str, Any]] = []
        for rec in self.pending:
            if now < float(rec["label_due_ts"]):
                keep.append(rec)
                continue
            mid0 = float(rec["mid0"])
            realized_bp = _bp(mid, mid0)
            task = rec["task"]
            pred = rec["pred"]
            hz = float(rec.get("horizon_sec") or 0.0)
            outcome = {
                "dir_correct": False,
                "hit_1_5bp": False,
                "continue_hit": False,
                "end_hit": False,
                "wrong": False,
                "horizon_sec": hz,
            }

            if task == "direction_1_5bp":
                if pred == "up":
                    outcome["dir_correct"] = realized_bp >= 1.0
                    outcome["hit_1_5bp"] = 1.0 <= realized_bp <= 5.0
                elif pred == "down":
                    outcome["dir_correct"] = realized_bp <= -1.0
                    outcome["hit_1_5bp"] = -5.0 <= realized_bp <= -1.0
                outcome["wrong"] = not outcome["dir_correct"]

            elif task == "trend_continue":
                side = (rec.get("meta") or {}).get("trend_side", "up")
                outcome["continue_hit"] = (
                    realized_bp >= 1.0 if side == "up" else realized_bp <= -1.0
                )
                outcome["dir_correct"] = outcome["continue_hit"]
                outcome["wrong"] = not outcome["continue_hit"]

            elif task == "trend_end":
                side = (rec.get("meta") or {}).get("trend_side", "up")
                outcome["end_hit"] = (
                    realized_bp <= -1.5 if side == "up" else realized_bp >= 1.5
                )
                outcome["dir_correct"] = outcome["end_hit"]
                outcome["wrong"] = not outcome["end_hit"]

            labeled = dict(rec)
            labeled["labeled"] = True
            labeled["labeled_ts"] = now
            labeled["mid1"] = mid
            labeled["realized_bp"] = round(realized_bp, 3)
            labeled["outcome"] = outcome
            self.store.append_labeled(labeled)
            self.stats["labeled"] += 1

        self.pending = keep

    def get_latest_conclusion(self) -> Optional[AgentConclusion]:
        return self.latest_conclusion
