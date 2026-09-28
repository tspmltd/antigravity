#!/usr/bin/env python3
"""CSR-544 / CSR-521 — Conditional Unwind v2 (OBSERVE-only · Risk Points · ENFORCE=0).

Safety corrections vs OR-trigger drafts:
  1. Stage score (not single OR) — hold>=60s alone must NOT force unwind
  2. Separate current_pnl_bp / mae_bp / mfe_bp / pnl_slope_5s
  3. Aggressive Maker wait = CF grid {0.5,1,2,3}s — not fixed 2s
  4. Virtual PnL only; never Actual exit; never LIVE

All numeric weights/thresholds are CANDIDATES for CF evaluation — not frozen optima.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

JST = timezone(timedelta(hours=9))

CSR = "CSR-544"
MODE = "OBSERVE_ONLY"
WIRE = "NO"
ENFORCE = 0

# --- Candidate scoring (NOT locked optima) ---
LADDER_POINTS = {"L0": 0, "L1": 0, "L2": 1, "L3": 2, "L4": 3}
PNL_POINTS = [  # (threshold_bp, points) evaluated high-to-low
    (-3.0, 3),
    (-1.5, 2),
]
ADVERSE_POINTS = [
    (80.0, 3),
    (60.0, 2),
]
HS_DISTANCE_LE_BP = 2.0
HS_DISTANCE_POINTS = 2

# Candidate thresholds to sweep in Virtual CF (not production)
SCORE_THRESHOLDS_CANDIDATE = (4, 5, 6, 7)
MAKER_WAIT_SEC_CF = (0.5, 1.0, 2.0, 3.0)

STATE_FEATURES = [
    "current_pnl_bp",
    "mae_bp",
    "mfe_bp",
    "pnl_slope_5s",
    "hold_ladder",
    "hold_sec",
    "adverse_score",
    "hs_distance_bp",  # hard_stop_bp - (-current_mid_pnl) style distance to HS
    "side",
]


@dataclass
class RiskScoreBreakdown:
    ladder_pts: int = 0
    pnl_pts: int = 0
    adverse_pts: int = 0
    hs_distance_pts: int = 0
    total: int = 0
    components: Dict[str, Any] = field(default_factory=dict)


def ladder_points(level: str) -> int:
    return int(LADDER_POINTS.get(str(level).upper(), 0))


def pnl_points(current_pnl_bp: float) -> int:
    pts = 0
    for thr, p in PNL_POINTS:
        if current_pnl_bp <= thr:
            pts = max(pts, p)
    return pts


def adverse_points(adverse_score: Optional[float]) -> int:
    if adverse_score is None:
        return 0
    pts = 0
    for thr, p in ADVERSE_POINTS:
        if float(adverse_score) >= thr:
            pts = max(pts, p)
    return pts


def hs_distance_points(hs_distance_bp: Optional[float]) -> int:
    if hs_distance_bp is None:
        return 0
    return HS_DISTANCE_POINTS if float(hs_distance_bp) <= HS_DISTANCE_LE_BP else 0


def compute_risk_score(
    *,
    hold_ladder: str,
    current_pnl_bp: float,
    adverse_score: Optional[float] = None,
    hs_distance_bp: Optional[float] = None,
    mae_bp: Optional[float] = None,
    mfe_bp: Optional[float] = None,
    pnl_slope_5s: Optional[float] = None,
    hold_sec: Optional[float] = None,
) -> RiskScoreBreakdown:
    """Score toxic inventory. hold_sec alone never adds points (anti 23:00 false cut)."""
    lp = ladder_points(hold_ladder)
    pp = pnl_points(float(current_pnl_bp))
    ap = adverse_points(adverse_score)
    hp = hs_distance_points(hs_distance_bp)
    total = lp + pp + ap + hp
    return RiskScoreBreakdown(
        ladder_pts=lp,
        pnl_pts=pp,
        adverse_pts=ap,
        hs_distance_pts=hp,
        total=total,
        components={
            "hold_ladder": hold_ladder,
            "current_pnl_bp": current_pnl_bp,
            "mae_bp": mae_bp,
            "mfe_bp": mfe_bp,
            "pnl_slope_5s": pnl_slope_5s,
            "hold_sec": hold_sec,
            "adverse_score": adverse_score,
            "hs_distance_bp": hs_distance_bp,
            "note": "hold_sec is diagnostic only — not a score adder",
        },
    )


def would_virtual_unwind(score: int, threshold: int) -> bool:
    return int(score) >= int(threshold)


def recovery_vs_deteriorating(
    *,
    current_pnl_bp: float,
    mae_bp: Optional[float],
    pnl_slope_5s: Optional[float],
) -> str:
    """Label path — same current pnl can be recovery or deteriorating."""
    if mae_bp is None:
        return "UNKNOWN"
    depth = float(mae_bp)  # typically negative
    # recovery: was much worse than now
    if depth <= current_pnl_bp - 1.5 and (pnl_slope_5s is None or pnl_slope_5s >= 0):
        return "RECOVERY"
    if pnl_slope_5s is not None and pnl_slope_5s < 0:
        return "DETERIORATING"
    if abs(float(mae_bp) - float(current_pnl_bp)) < 0.3:
        return "NEAR_MAE_WORSENING"
    return "MIXED"


def build_observe_event(
    *,
    trade_id: str,
    hold_ladder: str,
    current_pnl_bp: float,
    mae_bp: Optional[float],
    mfe_bp: Optional[float],
    pnl_slope_5s: Optional[float],
    adverse_score: Optional[float],
    hs_distance_bp: Optional[float],
    hold_sec: Optional[float],
    threshold: int = 5,
) -> Dict[str, Any]:
    br = compute_risk_score(
        hold_ladder=hold_ladder,
        current_pnl_bp=current_pnl_bp,
        adverse_score=adverse_score,
        hs_distance_bp=hs_distance_bp,
        mae_bp=mae_bp,
        mfe_bp=mfe_bp,
        pnl_slope_5s=pnl_slope_5s,
        hold_sec=hold_sec,
    )
    path = recovery_vs_deteriorating(
        current_pnl_bp=current_pnl_bp, mae_bp=mae_bp, pnl_slope_5s=pnl_slope_5s
    )
    hits = {
        thr: would_virtual_unwind(br.total, thr) for thr in SCORE_THRESHOLDS_CANDIDATE
    }
    return {
        "csr": CSR,
        "mode": MODE,
        "wire": WIRE,
        "enforce": ENFORCE,
        "trade_id": trade_id,
        "risk_score": br.total,
        "breakdown": asdict(br),
        "path_label": path,
        "trigger_hits_by_threshold": hits,
        "virtual_unwind_candidate": would_virtual_unwind(br.total, threshold),
        "threshold_used": threshold,
        "maker_wait_cf_sec": list(MAKER_WAIT_SEC_CF),
        "failsafe_taker_cf": True,
        "actual_exit": False,
        "forbidden": [
            "HOLD_GE_60S_FORCE_EXIT",
            "FIXED_30S_BLANKET",
            "FIXED_2S_MAKER_ONLY",
            "ENFORCE",
            "LIVE",
        ],
    }


def design_lock_payload() -> Dict[str, Any]:
    return {
        "csr": 544,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "CSR-521 Conditional Unwind v2 — Risk Points OBSERVE",
        "implementation_order": [
            "P0-A: score trigger hit logging + Virtual PnL only",
            "P0-B: maker_wait CF grid 0.5/1/2/3s + failsafe taker CF",
            "P1: Latency×Adverse×Ladder + MAE/MFE + exit_reason + realized_pnl",
            "P2: Pearson audit checklist (hypothesis, not confirmed bug)",
            "LAST: Economic PASS judgment",
        ],
        "scoring_candidates": {
            "ladder_points": LADDER_POINTS,
            "pnl_points": PNL_POINTS,
            "adverse_points": ADVERSE_POINTS,
            "hs_distance_le_bp": HS_DISTANCE_LE_BP,
            "hs_distance_points": HS_DISTANCE_POINTS,
            "score_thresholds": list(SCORE_THRESHOLDS_CANDIDATE),
            "status": "CANDIDATE_NOT_OPTIMA",
        },
        "state_features": STATE_FEATURES,
        "maker_wait_cf_sec": list(MAKER_WAIT_SEC_CF),
        "rules": {
            "or_trigger": False,
            "hold_alone_forces_exit": False,
            "current_pnl_vs_mae": "SEPARATED",
            "aggressive_maker_2s_fixed": False,
            "virtual_only": True,
            "blanket_30s": "REJECTED",
        },
        "gov": {
            "LIVE": "OFF",
            "WIRE": "NO",
            "ENFORCE": 0,
            "AUTO_APPLY": "OFF",
            "FROZEN": "YES",
        },
        "examples": {
            "hold60_pnl_neg_only_l0": asdict(
                compute_risk_score(
                    hold_ladder="L0",
                    current_pnl_bp=-0.5,
                    hold_sec=90,
                )
            ),
            "toxic_l3_adverse80": asdict(
                compute_risk_score(
                    hold_ladder="L3",
                    current_pnl_bp=-3.2,
                    adverse_score=85,
                    hs_distance_bp=1.5,
                    mae_bp=-4.0,
                    pnl_slope_5s=-0.4,
                )
            ),
        },
    }


def main() -> int:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    out = root / "data" / "mm_research" / "daily"
    out.mkdir(parents=True, exist_ok=True)
    payload = design_lock_payload()
    path = out / "2026-09-28_csr544_conditional_unwind_v2_lock.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # smoke: hold-only must not hit threshold 5
    smoke = build_observe_event(
        trade_id="smoke_hold60",
        hold_ladder="L1",
        current_pnl_bp=-0.2,
        mae_bp=-0.3,
        mfe_bp=0.5,
        pnl_slope_5s=0.0,
        adverse_score=20,
        hs_distance_bp=4.5,
        hold_sec=90,
        threshold=5,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(path),
                "smoke_score": smoke["risk_score"],
                "smoke_virtual": smoke["virtual_unwind_candidate"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
