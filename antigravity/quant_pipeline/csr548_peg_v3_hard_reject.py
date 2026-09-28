#!/usr/bin/env python3
"""CSR-548 — PEG_v3 / CSR-522 HARD REJECT · Entry freeze · Aging CF matrix.

Primary = user-presented log through 22:00. M365: no extra matching source.

Policy:
  - Economic FAIL / Promotion HARD REJECT
  - Keep Entry/AE as research asset (do NOT redesign Entry)
  - Detach Exit layer; single-variable CF on same 138 trades
  - Frozen: peg_v3_obs_v1 · AdaptiveRatio · BE3 · FakeLiquidity
  - Forbidden: PEG_v4, Aging 3→5 live, BE2, FakeLiq v2, AdaptiveRatio v4
  - AgingGuard = P0 SUSPECT, NOT convicted (+303bp claim FORBIDDEN)
  - Capture Rate = NOT VALIDATED · economic gate EXCLUDED
  - ENFORCE=0 · WIRE=NO · LIVE=OFF
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

CSR = "CSR-548"
PARENT = "CSR-522"

PRIMARY_22H = {
    "source": "user_presented_log_2200_primary",
    "m365_corroboration": False,
    "asof": "22:00",
    "peg_v3": {
        "pnl_24h_bp": -252.50,
        "trades": 138,
        "win_rate_pct": 10.0,
        "ev_per_trade_bp": round(-252.50 / 138, 4),  # ≈ -1.83
        "ae_1s_bp": -0.50,
        "ae_3s_bp": -0.97,
        "aging_exits": 94,
        "be3_exits": 2,
        "fakeliq_skips": 1,
    },
    "baseline": {
        "pnl_24h_bp": 50.50,
        "trades": 183,
        "win_rate_pct": 45.0,
        "ev_per_trade_bp": round(50.50 / 183, 4),  # ≈ +0.28
        "ae_1s_bp": -1.16,
        "ae_3s_bp": -2.17,
    },
    "delta_vs_baseline_bp": -303.00,
    "ae_1s_improve_bp": 0.66,
    "ae_3s_improve_bp": 1.20,
    "worsening_0730_to_2200_24h_bp": -193.58,  # user: 24h PnL worsened another 193.58bp
}

DIAGNOSTIC_WINDOWS = {
    "10:00": {
        "peg": {"bp": -45.56, "n": 15, "wr_pct": 7.0},
        "base": {"bp": -2.05, "n": 9, "wr_pct": 56.0},
        "delta_bp": -43.51,
    },
    "22:00": {
        "peg": {"bp": -40.91, "n": 13, "wr_pct": 0.0},
        "base": {"bp": 42.82, "n": 11, "wr_pct": 54.0},
        "delta_bp": -83.73,
        "note": "AE better on PEG · Economic dramatically worse → loss source ≠ Entry quality alone",
    },
}

AGING_COUNTERS = {
    "07:30": {"aging": 3, "be3": 1, "fake_skips": 0},
    "22:00": {"aging": 94, "be3": 2, "fake_skips": 1},
}

CAPTURE_OBSERVED = [
    -61.3, -39.5, -10.9, 16.3, 416.5, 444.5, 924.5, 952.5,
]

# Same-trade CF matrix: time vs exit method
CF_MATRIX_POLICIES: List[str] = [
    "Actual 3s Taker",
    "CF 5s Taker",
    "CF 10s Taker",
    "CF 20s Taker",
    "CF 30s Taker",
    "CF 3s Best-Maker",
    "CF 5s Best-Maker",
    "CF Baseline Exit",
]

CF_MATRIX_COLUMNS = [
    "policy",
    "n",
    "total_pnl_bp",
    "ev_per_trade_bp",
    "delta_vs_3s_taker_bp",
    "hs_rate",
    "cf_status",  # NULL_PENDING | VALID | INVALID
]

FROZEN = [
    "peg_v3_obs_v1",
    "Entry",
    "AdaptiveRatio",
    "BE3",
    "FakeLiquidity",
]

FORBIDDEN = [
    "PEG_v4",
    "AGING_3_TO_5_LIVE",
    "BE2",
    "FAKELIQ_V2",
    "ADAPTIVERATIO_V4",
    "CLAIM_AGING_OFF_EQUALS_PLUS_303BP",
    "ENTRY_REDESIGN",
    "MULTI_VARIABLE_CHANGE",
    "CAPTURE_AS_ECONOMIC_GATE",
    "PROMOTION",
    "ENFORCE_1",
    "WIRE_YES",
    "LIVE",
]


def empty_cf_matrix() -> List[Dict[str, Any]]:
    rows = []
    for i, p in enumerate(CF_MATRIX_POLICIES):
        rows.append({
            "policy": p,
            "n": 138 if i == 0 else None,  # same 138 trades; actual n known from primary
            "total_pnl_bp": None,
            "ev_per_trade_bp": None,
            "delta_vs_3s_taker_bp": 0.0 if i == 0 else None,
            "hs_rate": None,
            "cf_status": "ACTUAL" if i == 0 else "NULL_PENDING",
        })
    return rows


def ev_per_trade(pnl: float, n: int) -> float:
    if n <= 0:
        return float("nan")
    return round(pnl / n, 4)


def build_lock() -> Dict[str, Any]:
    peg = PRIMARY_22H["peg_v3"]
    base = PRIMARY_22H["baseline"]
    return {
        "csr": 548,
        "parent": PARENT,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "PEG_v3 HARD REJECT · save Entry/AE · Aging CF one-variable",
        "primary": PRIMARY_22H,
        "ev_check": {
            "peg_ev_per_trade_bp": peg["ev_per_trade_bp"],
            "baseline_ev_per_trade_bp": base["ev_per_trade_bp"],
            "sample_excuse": "REJECTED — 138 vs 183 · Δ−303bp · not small-n luck",
        },
        "structure": {
            "peg_maker_entry": {
                "ae_improve": True,
                "toxic_improve": True,
            },
            "exit_lifecycle": {
                "AgingGuard": 94,
                "BE3": 2,
                "FakeLiq": 1,
                "dominant": "AgingGuard",
            },
            "net_pnl_bp": -252.50,
        },
        "diagnostic_windows": DIAGNOSTIC_WINDOWS,
        "aging_counters": AGING_COUNTERS,
        "claims_allowed": {
            "economic_fail": True,
            "hard_reject": True,
            "ae_1s_improved": True,
            "ae_3s_improved": True,
            "aging_dominant_exit_path": True,
            "aging_p0_suspect": True,
            "aging_convicted": False,
            "be3_dead": False,  # 2 activations — not zero
            "be3_economically_material": False,
            "fakeliq_dead": False,
            "fakeliq_economically_material": False,
            "removing_aging_equals_plus_303": False,
        },
        "cf_matrix": {
            "purpose": "time bad vs exit method bad (3s Taker vs 3s Best-Maker)",
            "frozen_entry": True,
            "same_trade_n": 138,
            "policies": CF_MATRIX_POLICIES,
            "columns": CF_MATRIX_COLUMNS,
            "rows": empty_cf_matrix(),
            "note": "Do not interpret empty CF cells as 0. NULL_PENDING until mid path join.",
        },
        "capture_rate": {
            "status": "NOT_VALIDATED",
            "economic_gate": "EXCLUDED",
            "observed": CAPTURE_OBSERVED,
            "reason": "values swing through −61% … +952% — not a fill/capture rate; definition not presented",
        },
        "frozen": FROZEN,
        "forbidden": FORBIDDEN,
        "status": {
            "TF2BP_PEG_v3": "CSR-522",
            "Economic_result": "FAIL",
            "pnl_24h_bp": -252.50,
            "vs_Baseline_bp": -303.00,
            "win_rate_pct": 10,
            "AE_1s": "IMPROVED",
            "AE_3s": "IMPROVED",
            "AgingGuard": "DOMINANT EXIT PATH / P0 SUSPECT",
            "BE3": "VERY LOW ACTIVATION",
            "FakeLiquidity": "VERY LOW ACTIVATION",
            "Capture_Rate": "INVALID UNTIL DEFINITION AUDIT",
            "Entry_redesign": "STOP",
            "Exit_Counterfactual": "P0",
            "Promotion": "HARD REJECT",
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
            "AUTO_APPLY": "OFF",
        },
        "core": (
            "AE improved + Economic collapsed → keep Entry asset, isolate Exit CF. "
            "Replay same 138 trades: Actual 3s Taker vs 5/10/20/30 Taker + Best-Maker + Baseline."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr548_peg_v3_hard_reject_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    assert lock["primary"]["delta_vs_baseline_bp"] == -303.00
    assert lock["claims_allowed"]["aging_convicted"] is False
    assert lock["capture_rate"]["economic_gate"] == "EXCLUDED"
    assert lock["status"]["Promotion"] == "HARD REJECT"
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "peg_ev": lock["ev_check"]["peg_ev_per_trade_bp"],
        "base_ev": lock["ev_check"]["baseline_ev_per_trade_bp"],
        "cf_policies": len(CF_MATRIX_POLICIES),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
