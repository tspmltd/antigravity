"""CSR-535/538 P2 — FAKE_BREAKOUT / Micro Hard Veto Saved-bp counterfactual (OBSERVE only).

目的: Strong Trend × Micro Veto の経済価値を測る（勝敗判定はまだしない）。
  rising-edge · 1/3/10/30s · would_enter_side ← Trend Direction（candidate欠落時も）

列:
  price_*s · counterfactual_pnl_*s · saved_bp · missed_profit_bp · net_veto_value_bp

WIRE=NO · ENFORCE=0 · auto_apply=OFF · Micro Economic PASS=NO
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .fusion_pnl_labeler import action_aligned_pnl_bp

JST = timezone(timedelta(hours=9))
BASE = "/home/azureuser/antigravity"
OUT_DIR = f"{BASE}/data/mm_research"
JSONL = f"{OUT_DIR}/micro_veto_audit.jsonl"
STATE = f"{OUT_DIR}/micro_veto_cf_state.json"
HORIZONS_SEC = (1.0, 3.0, 10.0, 30.0)
MAX_PENDING = 64


def _side_from_trend(trend_direction: str, candidate_action: str) -> Optional[str]:
    """CF enter side: prefer candidate buy/sell; else map Trend Direction."""
    cand = str(candidate_action or "hold").lower()
    if cand in ("buy", "sell"):
        return cand
    td = str(trend_direction or "neutral").lower()
    if td in ("up", "buy"):
        return "buy"
    if td in ("down", "sell"):
        return "sell"
    return None


@dataclass
class MicroVetoAuditRow:
    episode_id: str
    timestamp_ms: int
    veto_reason: str
    candidate_action: str
    decision_action: str
    would_enter_side: str  # CF side (Trend Direction mapped)
    hypothesis_direction: str
    entry_mid: float
    trend_strength: float = 0.0
    pressure_score: float = 0.0
    imbalance: float = 0.0
    price_1s: Optional[float] = None
    price_3s: Optional[float] = None
    price_10s: Optional[float] = None
    price_30s: Optional[float] = None
    counterfactual_pnl_1s: Optional[float] = None
    counterfactual_pnl_3s: Optional[float] = None
    counterfactual_pnl_10s: Optional[float] = None
    counterfactual_pnl_30s: Optional[float] = None
    price_move_bp_30s: Optional[float] = None
    saved_bp_30s: Optional[float] = None  # max(0, -cf) capital saved proxy
    missed_profit_bp_30s: Optional[float] = None  # max(0, +cf)
    net_veto_value_bp_30s: Optional[float] = None  # -cf_pnl (= saved - missed)
    cf_status: str = "NULL_PENDING"
    verdict: Optional[str] = None
    wire: str = "NO"
    enforce: int = 0
    schema_version: str = "micro_veto_cf_csr538"
    economic_pass: bool = False


class MicroVetoCfAuditor:
    def __init__(self, jsonl_path: str = JSONL, max_pending: int = MAX_PENDING):
        self.jsonl_path = jsonl_path
        self.max_pending = max_pending
        self._pending: List[Dict[str, Any]] = []
        self._last_veto_key: Optional[Tuple[str, str]] = None
        self.stats = {
            "episodes": 0,
            "resolved": 0,
            "skipped_no_side": 0,
            "skipped_sticky": 0,
            "strong_trend_veto": 0,  # strength>=0.95
            "wire": "NO",
            "enforce": 0,
            "economic_pass": False,
        }

    def on_council_verdict(
        self,
        *,
        veto: bool,
        veto_reason: str,
        candidate_action: str,
        decision_action: str,
        mid: float,
        trend_strength: float = 0.0,
        trend_direction: str = "neutral",
        pressure_score: float = 0.0,
        imbalance: float = 0.0,
        timestamp_ms: Optional[int] = None,
    ) -> Optional[MicroVetoAuditRow]:
        if not veto:
            self._last_veto_key = None
            return None
        side = _side_from_trend(trend_direction, candidate_action)
        if side is None:
            self.stats["skipped_no_side"] += 1
            return None
        if mid is None or float(mid) <= 0:
            return None
        reason = str(veto_reason or "MICRO_HARD_VETO")
        key = (reason, side)
        if key == self._last_veto_key:
            self.stats["skipped_sticky"] += 1
            return None
        self._last_veto_key = key

        ts = int(timestamp_ms or time.time() * 1000)
        t_str = float(trend_strength or 0.0)
        row = MicroVetoAuditRow(
            episode_id=f"mv_{ts}_{uuid.uuid4().hex[:8]}",
            timestamp_ms=ts,
            veto_reason=reason,
            candidate_action=str(candidate_action or "hold").lower(),
            decision_action=str(decision_action or "hold").lower(),
            would_enter_side=side,
            hypothesis_direction=str(trend_direction or "neutral").lower(),
            entry_mid=float(mid),
            trend_strength=t_str,
            pressure_score=float(pressure_score or 0.0),
            imbalance=float(imbalance or 0.0),
        )
        if t_str >= 0.95:
            self.stats["strong_trend_veto"] += 1
        self._append_jsonl(asdict(row))
        self._pending.append(
            {"row": row, "entry_mid": float(mid), "t0_ms": ts, "action": side}
        )
        if len(self._pending) > self.max_pending:
            dropped = self._pending.pop(0)
            dropped["row"].cf_status = "INVALID"
            self._append_jsonl(asdict(dropped["row"]))
        self.stats["episodes"] += 1
        self._persist_stats()
        return row

    def on_mid(self, mid: float, timestamp_ms: Optional[int] = None) -> int:
        if mid is None or float(mid) <= 0 or not self._pending:
            return 0
        now_ms = int(timestamp_ms or time.time() * 1000)
        mid_f = float(mid)
        done_idx: List[int] = []
        newly = 0
        for i, p in enumerate(self._pending):
            row: MicroVetoAuditRow = p["row"]
            age_s = (now_ms - int(p["t0_ms"])) / 1000.0
            entry = float(p["entry_mid"])
            act = p["action"]
            changed = False
            for h in HORIZONS_SEC:
                tag = str(int(h))
                key_p = f"price_{tag}s"
                key_cf = f"counterfactual_pnl_{tag}s"
                if getattr(row, key_cf) is not None:
                    continue
                if age_s + 1e-6 < h:
                    continue
                setattr(row, key_p, mid_f)
                setattr(row, key_cf, action_aligned_pnl_bp(act, entry, mid_f))
                changed = True
            if row.counterfactual_pnl_30s is not None and row.cf_status == "NULL_PENDING":
                cf30 = float(row.counterfactual_pnl_30s)
                raw_move = (mid_f - entry) / entry * 10000.0 if entry > 0 else 0.0
                row.price_move_bp_30s = round(raw_move, 4)
                row.net_veto_value_bp_30s = round(-cf30, 4)
                row.saved_bp_30s = round(max(0.0, -cf30), 4)
                row.missed_profit_bp_30s = round(max(0.0, cf30), 4)
                row.cf_status = "VALID"
                if cf30 < 0:
                    row.verdict = "SAVED_CAPITAL_CANDIDATE"
                elif cf30 > 0:
                    row.verdict = "OPPORTUNITY_COST_CANDIDATE"
                else:
                    row.verdict = "FLAT"
                done_idx.append(i)
                newly += 1
                changed = True
            if changed:
                self._append_jsonl(asdict(row))
        for i in reversed(done_idx):
            self._pending.pop(i)
        if newly:
            self.stats["resolved"] += newly
            self._persist_stats()
        return newly

    def _append_jsonl(self, obj: Dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.jsonl_path), exist_ok=True)
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _persist_stats(self) -> None:
        try:
            os.makedirs(OUT_DIR, exist_ok=True)
            payload = {
                "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
                "stats": self.stats,
                "pending": len(self._pending),
                "horizons_sec": list(HORIZONS_SEC),
                "note": "Strong Trend×Veto CF · Micro Economic PASS=NO until n sufficient",
                "wire": "NO",
                "enforce": 0,
            }
            tmp = STATE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, STATE)
        except Exception:
            pass


_AUDITOR: Optional[MicroVetoCfAuditor] = None


def get_auditor() -> MicroVetoCfAuditor:
    global _AUDITOR
    if _AUDITOR is None:
        _AUDITOR = MicroVetoCfAuditor()
    return _AUDITOR
