#!/usr/bin/env python3
"""CSR-521 Conditional Inventory Unwind v1 — CANONICAL SPEC (OBSERVE · ENFORCE=0).

NOT a timer Exit. Hazard Control that detects HardStop-bound toxic inventory
while protecting L0/L1 natural Spread Capture.

BLANKET 30s UNWIND = REJECT
CONDITIONAL UNWIND = OBSERVE / CF only
Latency = CONDITION (never hazard points, never Actual PnL debit)
Adverse SHADOW_OFF → points = NULL (not 0)
Actual PnL + CF Δ as 'realized improvement' = FORBIDDEN
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

CSR = "CSR-521"
SPEC_VERSION = "csr521_cu_v1_canonical"
WIRE = "NO"
ENFORCE = 0

# --- Scoring candidates (NOT frozen optima; CF sweep) ---
LADDER_PTS = {"L0": 0, "L1": 0, "L2": 1, "L3": 2, "L4": 3}
PNL_BANDS = [(0.0, 0), (-1.5, 1), (-3.0, 2)]  # evaluated: pnl>0→0; ( -1.5,0]→1; ≤-1.5 handled below
# Clarify: pnl>0 →0; 0..-1.5 →+1; ≤-1.5 →+2; ≤-3.0 →+3
MAE_THRESHOLDS = [(-1.5, 1), (-3.0, 2), (-4.0, 3)]
ADVERSE_BANDS = [(40, 1), (60, 2), (80, 3)]  # <40 →0 if valid

SCORE_BANDS = {
    "SAFE": (0, 2),
    "WATCH": (3, 4),
    "UNWIND_CANDIDATE": (5, 6),
    "URGENT_UNWIND_CANDIDATE": (7, 99),
}

EXIT_CF_POLICIES = [
    "Immediate Taker",
    "Best Maker wait 0.5s → Taker",
    "Best Maker wait 1.0s → Taker",
    "Best Maker wait 2.0s → Taker",
    "Best Maker wait 3.0s → Taker",
]

TRADE_SCHEMA = [
    "trade_id", "timestamp", "side",
    "entry_price", "current_mid", "current_pnl_bp", "mae_bp", "mfe_bp", "pnl_slope_5s",
    "hold_time_s", "hold_ladder",
    "adverse_score", "adverse_valid",
    "imbalance", "micro_dev", "taker_aggressiveness", "cancel_rate", "refill_rate",
    "tip_thin", "fake_breakout",
    "latency_ms", "latency_bucket",
    "hazard_hold", "hazard_pnl", "hazard_mae", "hazard_adverse", "hazard_micro", "hazard_total",
    "recovery_protect",
    "actual_exit_reason", "actual_exit_price", "actual_pnl_bp", "hard_stop_actual",
]

CF_SCHEMA = [
    "cf_triggered",
    "cf_taker_pnl_bp",
    "cf_maker_05s_pnl_bp", "cf_maker_10s_pnl_bp", "cf_maker_20s_pnl_bp", "cf_maker_30s_pnl_bp",
    "cf_delta_taker_bp",
    "cf_delta_maker_05s_bp", "cf_delta_maker_10s_bp",
    "cf_delta_maker_20s_bp", "cf_delta_maker_30s_bp",
]

SUCCESS_GATES = [
    "Conditional CF Δ > 0",
    "HardStop rate down",
    "L0/L1 profit of non-targets preserved",
    "false unwind rate acceptable",
    "multi-regime reproducible",
    "Maker/Taker friction-inclusive still positive",
    "sample size sufficient",
    "id_ok = true",
]


@dataclass
class HazardBreakdown:
    hold: int = 0
    pnl: int = 0
    mae: int = 0
    adverse: Optional[int] = 0  # None when adverse_valid=False
    micro: int = 0
    recovery_protect: bool = False
    total: Optional[int] = None
    band: str = "SAFE"
    eligible: bool = False
    components: Dict[str, Any] = field(default_factory=dict)


def ladder_points(level: str) -> int:
    return int(LADDER_PTS.get(str(level).upper(), 0))


def pnl_points(current_pnl_bp: float) -> int:
    p = float(current_pnl_bp)
    if p > 0:
        return 0
    if p > -1.5:
        return 1
    if p > -3.0:
        return 2
    return 3


def mae_points(mae_bp: Optional[float], mae_deepening: bool = False, pnl_recovering: bool = False) -> int:
    if mae_bp is None:
        pts = 0
    else:
        m = float(mae_bp)
        pts = 0
        for thr, p in MAE_THRESHOLDS:
            if m <= thr:
                pts = max(pts, p)
    if mae_deepening:
        pts += 1
    if pnl_recovering:
        pts = max(0, pts - 1)
    return pts


def adverse_points(adverse_score: Optional[float], adverse_valid: bool) -> Optional[int]:
    if not adverse_valid:
        return None  # NULL — not zero
    if adverse_score is None:
        return None
    s = float(adverse_score)
    pts = 0
    for thr, p in ADVERSE_BANDS:
        if s >= thr:
            pts = max(pts, p)
    return pts


def micro_points(
    *,
    pressure_against: bool = False,
    tip_thin_one_way: bool = False,
    cancel_spike_taker: bool = False,
    micro_dev_against: bool = False,
) -> int:
    pts = 0
    if pressure_against:
        pts += 2
    if tip_thin_one_way:
        pts += 1
    if cancel_spike_taker:
        pts += 1
    if micro_dev_against:
        pts += 1
    return pts


def score_band(total: int) -> str:
    if total >= 7:
        return "URGENT_UNWIND_CANDIDATE"
    if total >= 5:
        return "UNWIND_CANDIDATE"
    if total >= 3:
        return "WATCH"
    return "SAFE"


def eligible_inventory(
    hold_ladder: str,
    *,
    adverse_score: Optional[float] = None,
    adverse_valid: bool = False,
    mae_bp: Optional[float] = None,
    mae_deepening: bool = False,
) -> bool:
    lv = str(hold_ladder).upper()
    if lv in ("L2", "L3", "L4"):
        return True
    # Early toxic L1 research gate
    if lv == "L1":
        adv_hi = adverse_valid and adverse_score is not None and float(adverse_score) >= 80
        mae_bad = mae_deepening and mae_bp is not None and float(mae_bp) <= -3.0
        return bool(adv_hi and mae_bad)
    return False  # L0 protected


def compute_hazard_v1(
    *,
    hold_ladder: str,
    current_pnl_bp: float,
    mae_bp: Optional[float] = None,
    mfe_bp: Optional[float] = None,
    pnl_slope_5s: Optional[float] = None,
    mae_deepening: bool = False,
    adverse_score: Optional[float] = None,
    adverse_valid: bool = False,
    pressure_against: bool = False,
    tip_thin_one_way: bool = False,
    cancel_spike_taker: bool = False,
    micro_dev_against: bool = False,
    apply_recovery_protect: bool = True,
) -> HazardBreakdown:
    elig = eligible_inventory(
        hold_ladder,
        adverse_score=adverse_score,
        adverse_valid=adverse_valid,
        mae_bp=mae_bp,
        mae_deepening=mae_deepening,
    )
    hp = ladder_points(hold_ladder)
    pp = pnl_points(current_pnl_bp)
    pnl_recovering = pnl_slope_5s is not None and float(pnl_slope_5s) > 0
    mp = mae_points(mae_bp, mae_deepening=mae_deepening, pnl_recovering=pnl_recovering)
    ap = adverse_points(adverse_score, adverse_valid)
    microp = micro_points(
        pressure_against=pressure_against,
        tip_thin_one_way=tip_thin_one_way,
        cancel_spike_taker=cancel_spike_taker,
        micro_dev_against=micro_dev_against,
    )

    recovery = False
    if apply_recovery_protect:
        if current_pnl_bp > 0 and pnl_recovering:
            recovery = True
        elif pnl_recovering and mae_bp is not None and not mae_deepening:
            recovery = True

    # If adverse invalid, total still computable without adverse; flag components
    base = hp + pp + mp + microp
    if ap is not None:
        base += ap
    if recovery:
        base = max(0, base - 2)

    urgent_failsafe = (
        current_pnl_bp <= -4.0 and mae_deepening
    )

    band = score_band(base)
    if urgent_failsafe and band != "URGENT_UNWIND_CANDIDATE":
        band = "URGENT_UNWIND_CANDIDATE"

    return HazardBreakdown(
        hold=hp,
        pnl=pp,
        mae=mp,
        adverse=ap,
        micro=microp,
        recovery_protect=recovery,
        total=base,
        band=band,
        eligible=elig,
        components={
            "hold_ladder": hold_ladder,
            "current_pnl_bp": current_pnl_bp,
            "mae_bp": mae_bp,
            "mfe_bp": mfe_bp,
            "pnl_slope_5s": pnl_slope_5s,
            "adverse_valid": adverse_valid,
            "adverse_score": adverse_score,
            "urgent_failsafe_candidate": urgent_failsafe,
            "latency_in_score": False,
        },
    )


def build_lock() -> Dict[str, Any]:
    # smokes
    s_l0 = compute_hazard_v1(hold_ladder="L0", current_pnl_bp=1.0)
    s_l2 = compute_hazard_v1(
        hold_ladder="L2", current_pnl_bp=-2.0, mae_bp=-3.5, mae_deepening=True,
        adverse_score=70, adverse_valid=True, pressure_against=True,
    )
    s_off = compute_hazard_v1(
        hold_ladder="L2", current_pnl_bp=-1.0, adverse_score=0.0, adverse_valid=False,
    )
    return {
        "csr": 521,
        "spec_version": SPEC_VERSION,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "CSR-521 Conditional Inventory Unwind v1 · Hazard Control",
        "purpose": "HardStop tail compression",
        "primary_target": "L2/L3/L4 toxic inventory",
        "protect": "L0/L1 natural spread capture",
        "model": "Hazard Score (points · not OR-exit)",
        "reject": ["BLANKET_30S_UNWIND", "LATENCY_AS_HAZARD_POINTS", "ACTUAL_PLUS_CF_AS_REALIZED"],
        "state_machine": [
            "NORMAL → HOLD",
            "HOLD → CLOSED_NORMAL | HOLD | UNWIND_CANDIDATE",
            "UNWIND_CANDIDATE → Virtual Exit only (OBSERVE)",
            "future ENFORCE: AGGRESSIVE_MAKER → FAILSAFE → FLAT",
        ],
        "eligibility": {
            "primary": "hold_ladder IN (L2,L3,L4)",
            "early_toxic_l1": "Adverse HIGH AND MAE significantly deteriorating",
            "l0": "PROTECTED",
        },
        "scoring": {
            "ladder": LADDER_PTS,
            "pnl": " >0:0 · (0,-1.5]:1 · ≤-1.5:2 · ≤-3:3",
            "mae": MAE_THRESHOLDS,
            "adverse": ADVERSE_BANDS,
            "adverse_invalid": "NULL not 0",
            "micro_candidates": {
                "pressure_against": 2,
                "tip_thin_one_way": 1,
                "cancel_spike_taker": 1,
                "micro_dev_against": 1,
            },
            "recovery_protect": "pnl>0 & slope>0 OR MAE recovery → score−2 (CF parallel)",
            "bands": SCORE_BANDS,
            "urgent_failsafe": "pnl≤-4.0 AND MAE deepening → URGENT (still OBSERVE)",
        },
        "latency": "CONDITION_ONLY — not in hazard_score; slice Hazard×Latency after",
        "exit_cf_policies": EXIT_CF_POLICIES,
        "exit_cf_note": "Do NOT pre-adopt 2s Maker; sweep all waits",
        "trade_schema": TRADE_SCHEMA,
        "cf_schema": CF_SCHEMA,
        "hourly_reports": ["hazard_band_table", "ladder_table"],
        "success_gates": SUCCESS_GATES,
        "smoke": {
            "l0_not_eligible": asdict(s_l0),
            "l2_high": asdict(s_l2),
            "adverse_off_null": asdict(s_off),
        },
        "final_spec": {
            "NAME": "CSR-521 Conditional Inventory Unwind",
            "PURPOSE": "HardStop tail compression",
            "PRIMARY_TARGET": "L2/L3/L4 toxic inventory",
            "PROTECT": "L0/L1 natural spread capture",
            "MODEL": "Hazard Score",
            "FEATURES": ["Ladder", "Current PnL", "MAE/MFE", "PnL slope", "Adverse", "Microstructure"],
            "LATENCY": "Condition only · Not PnL debit · Not initial hazard point",
            "EXIT_CF": EXIT_CF_POLICIES,
            "ACTUAL_EXECUTION": "NONE",
            "WIRE": "NO",
            "ENFORCE": 0,
            "AUTO_APPLY": "OFF",
            "ECONOMIC_PASS": False,
        },
        "ablation_lane": "A5",
        "gov": {"WIRE": WIRE, "ENFORCE": ENFORCE, "LIVE": "OFF", "AUTO_APPLY": "OFF"},
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr521_conditional_unwind_v1_spec_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    assert lock["smoke"]["l0_not_eligible"]["eligible"] is False
    assert lock["smoke"]["l2_high"]["eligible"] is True
    assert lock["smoke"]["adverse_off_null"]["adverse"] is None
    assert lock["final_spec"]["ENFORCE"] == 0
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "l2_band": lock["smoke"]["l2_high"]["band"],
        "l2_total": lock["smoke"]["l2_high"]["total"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
