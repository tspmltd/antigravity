#!/usr/bin/env python3
"""CSR-555 — CSR-521 Hazard Threshold Calibration Procedure (OBSERVE · ENFORCE=0).

NOT: optimize "cut after N seconds"
YES: at each inventory snapshot t, jointly estimate
  P(HardStop or TailLoss | X_t)  and  opportunity cost of cutting now
  → pick threshold by Economic EV, never by classification Accuracy.

Teacher: exit_reason == HARD_STOP from actual logs (NEVER infer from pnl ≤ −4.8bp).
Latency: CONDITION only.
Economic PASS: NO until multi-gate + OOS.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

CSR = "CSR-555"
PARENT = "CSR-521"
SNAPSHOT_AGES_SEC = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120)
HS_HORIZONS_SEC = (10, 30, 60)
THRESHOLDS = (0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90)
EXIT_CF = (
    "Immediate Taker",
    "Best Maker 0.5s → Taker",
    "Best Maker 1s → Taker",
    "Best Maker 2s → Taker",
    "Best Maker 3s → Taker",
)

INVENTORY_SNAPSHOT_SCHEMA: List[str] = [
    "trade_id",
    "snapshot_ts",
    "age_s",
    "hold_ladder",
    "current_pnl_bp",
    "mae_bp",
    "mfe_bp",
    "distance_to_hard_stop_bp",
    "inventory_side",
    # trajectory
    "pnl_change_3s",
    "pnl_change_5s",
    "pnl_slope_5s",
    "mae_change_5s",
    "recovery_from_mae_bp",
    # adverse
    "ae_100ms",
    "ae_1s",
    "ae_3s",
    "adverse_score",
    "adverse_valid",
    "toxic_flag",
    # microstructure
    "imbalance",
    "micro_dev",
    "tip_thin",
    "taker_aggressiveness",
    "cancel_rate",
    "refill_rate",
    "fake_breakout",
    "spread_bp",
    # latency CONDITION
    "latency_ms",
    "latency_bucket",
    # labels (future from this snapshot)
    "hazard_label",          # 1 if eventual HS / tail-loss
    "exit_reason_final",     # from actual log
    "hs_within_10s",
    "hs_within_30s",
    "hs_within_60s",
    "cf_unwind_now_bp",      # opportunity-aware virtual cut at t
]

BUCKET_AXES = ["hold_ladder", "mae_band", "adverse_slice"]

PASS_GATES = [
    "CF Δbp > 0",
    "EV/trigger > 0",
    "HardStop rate decreases",
    "L0/L1 profit nearly preserved",
    "Range and Trend do not collapse",
    "Out-of-Sample positive",
    "Maker/Taker friction-inclusive positive",
]

FORBIDDEN = [
    "OPTIMIZE_CUT_SECONDS_DIRECTLY",
    "MAXIMIZE_CLASSIFICATION_ACCURACY_FOR_THRESHOLD",
    "INFER_HARD_STOP_FROM_PNL_LE_MINUS_4_8",
    "RANDOM_SHUFFLE_TRAIN_TEST",
    "LATENCY_AS_PNL_COST",
    "ADOPT_THRESHOLD_WITHOUT_OOS",
    "ECONOMIC_PASS",
    "ENFORCE_1",
    "WIRE_YES",
]


def empty_bucket_table() -> List[Dict[str, Any]]:
    # Template rows — fill via DuckDB later
    examples = [
        ("L1", ">-1bp", "LOW"),
        ("L2", "-1~-2bp", "LOW"),
        ("L2", "<-2bp", "HIGH"),
        ("L3", "<-2bp", "HIGH"),
        ("L4", "Any", "HIGH"),
    ]
    rows = []
    for ladder, mae, adv in examples:
        rows.append({
            "ladder": ladder,
            "mae": mae,
            "adverse": adv,
            "n": None,
            "forward_hs_rate": None,
            "hold_ev": None,
            "cf_unwind_ev": None,
        })
    return rows


def empty_threshold_table() -> List[Dict[str, Any]]:
    return [
        {
            "threshold": t,
            "trigger_n": None,
            "hs_avoided": None,
            "false_cut": None,
            "cf_delta_bp": None,
            "ev_per_trigger": None,
        }
        for t in THRESHOLDS
    ]


def empty_calibration_report() -> Dict[str, Any]:
    return {
        "title": "CSR-521 Hazard Calibration",
        "data": {
            "trades": None,
            "inventory_snapshots": None,
            "hs_events": None,
        },
        "model": {
            "target": "HS within 30s (also report 10s/60s)",
            "features": "Ladder / MAE / Adverse / Micro",
            "latency": "condition only",
            "method_phase1": "Bucket Ladder×MAE×Adverse",
            "method_phase2": "Logistic P(HS|X) optional after buckets",
        },
        "calibration": {f"threshold_{t:.2f}_delta_bp": None for t in THRESHOLDS},
        "best": {
            "in_sample": None,
            "validation": None,
            "oos": None,
        },
        "good_cut": {"hs_avoided": None, "saved_bp": None},
        "bad_cut": {"normal_inventory_cut": None, "opportunity_loss_bp": None},
        "exit_cf": {
            "taker_bp": None,
            "maker_0_5s_bp": None,
            "maker_1s_bp": None,
            "maker_2s_bp": None,
            "maker_3s_bp": None,
        },
        "final": {
            "candidate_threshold": None,
            "Economic_PASS": "NO/PENDING",
            "ENFORCE": 0,
            "WIRE": "NO",
            "AUTO_APPLY": "OFF",
        },
        "selection_rule": (
            "argmax [HS-avoided bp − good-inventory early-cut opportunity loss − Exit friction]"
        ),
    }


def build_lock() -> Dict[str, Any]:
    return {
        "csr": 555,
        "parent": PARENT,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Hazard threshold calibration · Economic EV not Accuracy",
        "m365_corroboration": False,
        "primary_basis": "user CSR-521 design + Parquet/DuckDB platform",
        "objective": (
            "At each inventory snapshot t, estimate P(HS|Tail | X_t) and cut-now "
            "opportunity cost jointly; choose threshold by net Economic EV"
        ),
        "teacher": {
            "hazard_label_1": "eventual exit_reason == HARD_STOP OR explicit tail-loss criterion",
            "hazard_label_0": "otherwise",
            "forbid": "infer HS from realized_pnl <= -4.8bp",
        },
        "sampling": {
            "unit": "inventory_snapshot_not_trade_row",
            "ages_sec": list(SNAPSHOT_AGES_SEC),
            "why": "learn what changed BEFORE HS",
        },
        "features": {
            "inventory": [
                "hold_time_s", "hold_ladder", "current_pnl_bp", "mae_bp", "mfe_bp",
                "distance_to_hard_stop_bp", "inventory_side",
            ],
            "trajectory": [
                "pnl_change_3s", "pnl_change_5s", "pnl_slope_5s",
                "mae_change_5s", "recovery_from_mae_bp",
            ],
            "adverse": [
                "ae_100ms", "ae_1s", "ae_3s", "adverse_score", "toxic_flag",
            ],
            "microstructure": [
                "imbalance", "micro_dev", "tip_thin", "taker_aggressiveness",
                "cancel_rate", "refill_rate", "fake_breakout", "spread_bp",
            ],
            "latency_condition": ["latency_ms", "latency_bucket"],
        },
        "inventory_snapshot_schema": INVENTORY_SNAPSHOT_SCHEMA,
        "phase1_buckets": {
            "axes": BUCKET_AXES,
            "table_template": empty_bucket_table(),
            "question": "L2 alone vs L2×MAE×High Adverse",
        },
        "phase2_model": {
            "form": "P(HS within H | X_t)",
            "H_sec": list(HS_HORIZONS_SEC),
            "output": "hazard_score in [0,1] = calibrated probability",
            "start_with": "buckets before XGBoost/complex ML",
        },
        "threshold_grid": list(THRESHOLDS),
        "threshold_table_template": empty_threshold_table(),
        "threshold_objective": (
            "HS avoidance benefit − opportunity loss from cutting good inventory − Exit friction"
        ),
        "exit_cf_policies": list(EXIT_CF),
        "pairing_note": (
            "Same trigger × Exit policy: e.g. score 0.6 may wait Maker 2s; "
            "0.9 may prefer Immediate Taker — numbers are calibration TARGETS not adopted"
        ),
        "train_split": {
            "method": "time_ordered",
            "forbid_shuffle": True,
            "blocks": ["Train (older)", "Validation", "OOS (newest)"],
            "regimes_check": ["range", "trend-up", "trend-down", "high-vol"],
        },
        "pass_gates": PASS_GATES,
        "forbidden": FORBIDDEN,
        "report_template": empty_calibration_report(),
        "gov": {
            "Economic_PASS": False,
            "ENFORCE": 0,
            "WIRE": "NO",
            "AUTO_APPLY": "OFF",
            "LIVE": "OFF",
        },
        "core": (
            "Threshold = argmax economic net improvement, not max Accuracy. "
            "Teacher = actual HARD_STOP exit_reason. Sample = age snapshots."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr555_hazard_threshold_calibration_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    rep = OUT / "2026-09-28_csr555_hazard_calibration_report_template.json"
    rep.write_text(json.dumps(empty_calibration_report(), ensure_ascii=False, indent=2), encoding="utf-8")
    assert "INFER_HARD_STOP_FROM_PNL_LE_MINUS_4_8" in lock["forbidden"]
    assert lock["gov"]["Economic_PASS"] is False
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "report": str(rep),
        "ages": list(SNAPSHOT_AGES_SEC),
        "thresholds": list(THRESHOLDS),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
