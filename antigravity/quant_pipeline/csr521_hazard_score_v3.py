#!/usr/bin/env python3
"""CSR-547 / CSR-521 — Inventory Hazard Score v3 (OBSERVE · ENFORCE=0).

Shift (primary = user log 07:30–22:00, 16 windows):
  - 10s CF: 16/16 · ΣΔ +1074.73bp (COMPARATIVE — overlapping 1h windows; NOT cash claim)
  - 20s CF: 16/16 · ΣΔ +738.09bp
  - 30s CF: 15/16 · counterexample 15:00 −11.96bp
  - L2+L3 alone is insufficient (11:00 L1 = max loss source)
  - Exit EV vs time is NON-MONOTONIC (15:00: 10/20 GOOD · 30/45 BAD · 60 GOOD)

Policy:
  Age 10–20s = detection start · 30s = confirmation point · NOT fixed exit
  Ladder = one feature (demoted) · imb-alone / cancel-alone = +0
  HazardScore → Virtual unwind only → Δ vs Actual · ENFORCE=0
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

CSR = "CSR-547"
PARENT = "CSR-521"
MODE = "OBSERVE_ONLY"
WIRE = "NO"
ENFORCE = 0

# --- Candidate points (NOT frozen optima; OBSERVE CF sweep) ---
AGE_10_20_PTS = 1
AGE_GE_30_PTS = 1
PNL_DETERIORATING_PTS = 2
MAE_DEEPENING_PTS = 2
LADDER_PTS = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 3}  # L1 no longer free
HS_NARROWING_PTS = 2
ADVERSE_HIGH_PTS = 2
TIP_DEPLETION_PTS = 1
TAKER_SAME_SIDE_PTS = 2
IMB_ALONE_PTS = 0
CANCEL_ALONE_PTS = 0

SCORE_THRESHOLDS_CANDIDATE = (4, 5, 6, 7, 8)
CF_HORIZONS_SEC = (10, 20, 30, 45, 60)  # same trade_id compare
MAKER_WAIT_SEC_CF = (0.5, 1.0, 2.0, 3.0)

PRIMARY_CF_SUMMARY = {
    "source": "user_presented_log_0730_2200_primary",
    "m365_corroboration": False,
    "windows_n": 16,
    "disclaimer": "ΣΔ is comparative across overlapping last_1h windows — NOT additive cash",
    "cf_10s": {"improve": "16/16", "worsen": 0, "sum_delta_bp": 1074.73, "mean_bp": 67.17},
    "cf_20s": {"improve": "16/16", "worsen": 0, "sum_delta_bp": 738.09, "mean_bp": 46.13},
    "cf_30s": {"improve": "15/16", "worsen": 1, "sum_delta_bp": 373.79, "mean_bp": 23.36},
    "cf_l2_l3": {"improve": "15/16", "worsen": 1, "sum_delta_bp": 395.77, "mean_bp": 24.74},
}

NEGATIVE_CONTROLS = {
    "15:00": {
        "role": "fixed_timer_falsifier",
        "actual_bp": -25.70,
        "cf": {"10s": 34.37, "20s": 31.91, "30s": -11.96, "L2+L3": -11.96, "45s": -4.71, "60s": 4.60},
        "note": "early cut helps; 30s timing BAD; Exit EV non-monotonic",
    },
    "11:00": {
        "role": "l2_only_falsifier",
        "ladder_pnl_bp": {"L0": -42.93, "L1": -90.55, "L2": -24.44},
        "hs_pct": {"L0": 15.8, "L1": 44.7, "L2": 27.3},
        "actual_bp": -157.92,
        "cf": {"10s": 98.22, "20s": 49.11, "30s": 6.05},
        "note": "L1 max loss source — forbid L2/L3-only CU",
    },
}


@dataclass
class HazardBreakdown:
    age_pts: int = 0
    pnl_pts: int = 0
    mae_pts: int = 0
    ladder_pts: int = 0
    hs_pts: int = 0
    adverse_pts: int = 0
    tip_pts: int = 0
    taker_pts: int = 0
    total: int = 0
    components: Dict[str, Any] = field(default_factory=dict)


def compute_hazard_score_v3(
    *,
    hold_sec: float,
    current_pnl_bp: float,
    pnl_deteriorating: bool = False,
    mae_deepening: bool = False,
    hold_ladder: str = "L0",
    hs_distance_narrowing: bool = False,
    adverse_high: bool = False,
    adverse_score_valid: bool = False,
    tip_depletion_confirmed: bool = False,
    taker_same_side: bool = False,
    imb_extreme_alone: bool = False,
    cancel_alone: bool = False,
    mae_bp: Optional[float] = None,
    pnl_slope_5s: Optional[float] = None,
) -> HazardBreakdown:
    """Inventory Hazard Detection — age 10–20 starts scoring; ladder demoted."""
    age = 0
    if 10.0 <= float(hold_sec) < 30.0:
        age = AGE_10_20_PTS
    elif float(hold_sec) >= 30.0:
        age = AGE_10_20_PTS + AGE_GE_30_PTS  # 10-20 band + ≥30 confirmation point

    # infer deteriorating from slope if flag unset
    if not pnl_deteriorating and pnl_slope_5s is not None and float(pnl_slope_5s) < 0:
        pnl_deteriorating = True

    pnl = PNL_DETERIORATING_PTS if pnl_deteriorating else 0
    mae = MAE_DEEPENING_PTS if mae_deepening else 0
    ladder = int(LADDER_PTS.get(str(hold_ladder).upper(), 0))
    hs = HS_NARROWING_PTS if hs_distance_narrowing else 0
    # CSR-546: SHADOW_OFF adverse never contributes
    adv = ADVERSE_HIGH_PTS if (adverse_high and adverse_score_valid) else 0
    tip = TIP_DEPLETION_PTS if tip_depletion_confirmed else 0
    taker = TAKER_SAME_SIDE_PTS if taker_same_side else 0
    # explicit zeros for audit
    _imb = IMB_ALONE_PTS if imb_extreme_alone else 0
    _cancel = CANCEL_ALONE_PTS if cancel_alone else 0
    assert _imb == 0 and _cancel == 0

    total = age + pnl + mae + ladder + hs + adv + tip + taker
    return HazardBreakdown(
        age_pts=age,
        pnl_pts=pnl,
        mae_pts=mae,
        ladder_pts=ladder,
        hs_pts=hs,
        adverse_pts=adv,
        tip_pts=tip,
        taker_pts=taker,
        total=total,
        components={
            "hold_sec": hold_sec,
            "hold_ladder": hold_ladder,
            "current_pnl_bp": current_pnl_bp,
            "mae_bp": mae_bp,
            "pnl_slope_5s": pnl_slope_5s,
            "pnl_deteriorating": pnl_deteriorating,
            "mae_deepening": mae_deepening,
            "hs_distance_narrowing": hs_distance_narrowing,
            "adverse_high": adverse_high,
            "adverse_score_valid": adverse_score_valid,
            "tip_depletion_confirmed": tip_depletion_confirmed,
            "taker_same_side": taker_same_side,
            "imb_alone_pts": 0,
            "cancel_alone_pts": 0,
            "detection_band": "10_20s_start",
            "confirm_point": "30s_not_fixed_exit",
        },
    )


def would_virtual_unwind(score: HazardBreakdown, threshold: int = 5) -> bool:
    return int(score.total) >= int(threshold)


def build_lock() -> Dict[str, Any]:
    # smoke: L1 toxic can score without L2
    s_l1 = compute_hazard_score_v3(
        hold_sec=15.0,
        current_pnl_bp=-2.0,
        pnl_deteriorating=True,
        mae_deepening=True,
        hold_ladder="L1",
        taker_same_side=True,
        tip_depletion_confirmed=True,
    )
    s_15_30 = compute_hazard_score_v3(
        hold_sec=30.0,
        current_pnl_bp=-1.0,
        hold_ladder="L2",
        pnl_deteriorating=False,
        mae_deepening=False,
    )
    return {
        "csr": 547,
        "parent": PARENT,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Inventory Hazard Detection 10–20s · 30s confirmation not fixed exit",
        "mode": MODE,
        "wire": WIRE,
        "enforce": ENFORCE,
        "primary_cf": PRIMARY_CF_SUMMARY,
        "negative_controls": NEGATIVE_CONTROLS,
        "hypothesis": {
            "old": "Cut toxic inventory that entered L2/L3",
            "new": "Start toxicity detection at 10–20s · Conditional Unwind candidate · 30s=confirm not fixed exit",
        },
        "scoring": {
            "age_10_20": AGE_10_20_PTS,
            "age_ge_30": AGE_GE_30_PTS,
            "pnl_deteriorating": PNL_DETERIORATING_PTS,
            "mae_deepening": MAE_DEEPENING_PTS,
            "ladder": LADDER_PTS,
            "hs_narrowing": HS_NARROWING_PTS,
            "adverse_high": ADVERSE_HIGH_PTS,
            "tip_depletion": TIP_DEPLETION_PTS,
            "taker_same_side": TAKER_SAME_SIDE_PTS,
            "imb_alone": IMB_ALONE_PTS,
            "cancel_alone": CANCEL_ALONE_PTS,
            "thresholds_candidate": list(SCORE_THRESHOLDS_CANDIDATE),
            "cf_horizons_sec": list(CF_HORIZONS_SEC),
            "maker_wait_sec_cf": list(MAKER_WAIT_SEC_CF),
        },
        "smoke": {
            "l1_toxic_15s": asdict(s_l1),
            "age30_ladder_only": asdict(s_15_30),
            "note": "L1+deterioration+taker can exceed L2-age-only",
        },
        "forbidden": [
            "FIXED_10S_ALL_EXIT",
            "FIXED_20S_ALL_EXIT",
            "FIXED_30S_ALL_EXIT",
            "L2_L3_ONLY_RULE",
            "IMB_ALONE_TRIGGER",
            "CANCEL_ALONE_TRIGGER",
            "TREAT_SUM_DELTA_AS_CASH",
            "ECONOMIC_PASS",
            "ENFORCE_1",
            "LIVE",
            "LATENCY_COST_IN_ACTUAL",
            "ADVERSE_SCORE_WHEN_SHADOW_OFF",
        ],
        "status": {
            "CSR521_10s": "VERY_PROMISING",
            "CSR521_20s": "VERY_PROMISING",
            "CSR521_30s_blanket": "NOT_ROBUST",
            "Conditional_Hazard": "P0",
            "ENFORCE": 0,
        },
        "gov": {"WIRE": "NO", "LIVE": "OFF", "AUTO_APPLY": "OFF"},
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr547_inventory_hazard_v3_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    assert lock["primary_cf"]["cf_10s"]["improve"] == "16/16"
    assert lock["negative_controls"]["15:00"]["cf"]["30s"] < 0
    assert lock["smoke"]["l1_toxic_15s"]["ladder_pts"] == 1
    print(json.dumps({"ok": True, "path": str(path), "l1_toxic_total": lock["smoke"]["l1_toxic_15s"]["total"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
