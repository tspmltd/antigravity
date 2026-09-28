#!/usr/bin/env python3
"""CSR-545 — PEG_v3 Aging Exit Counterfactual schema (FROZEN params · ENFORCE=0).

Do NOT change aging_guard_sec 3→5. Keep PEG_v3 FROZEN; run same-trade CF grid:

  Actual Aging 3s | CF 5s | CF 10s | CF 20s | CF 30s | CF Best-Maker | CF Baseline Exit

Separates:
  - 3s too short?
  - taker Aging exit itself bad?
  - PEG fill selection bad?

Capture Rate: INVALID for promotion until definition audit.
AE improvement ≠ Economic PASS. Entry redesign HOLD. No PEG_v4.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

CSR = "CSR-545"
AGING_CF_HORIZONS_SEC = (5.0, 10.0, 20.0, 30.0)

# Required fields on each PEG Aging exit / CF row
AGING_EXIT_LOG_SCHEMA: List[str] = [
    "trade_id",
    "entry_ts",
    "entry_price",
    "pnl_1s",
    "pnl_3s",
    "pnl_5s",
    "pnl_10s",
    "pnl_20s",
    "pnl_30s",
    "mfe_bp",
    "mae_bp",
    "exit_reason",
    "exit_price",
    "exit_type",  # maker | taker
    "exit_spread_bp",
    "ae_1s",
    "ae_3s",
    "adverse_score",
    "actual_pnl_bp",
    "cf_5s_bp",
    "cf_10s_bp",
    "cf_20s_bp",
    "cf_30s_bp",
    "cf_bestmaker_bp",
    "cf_baseline_exit_bp",
    "cf_status",  # NULL_PENDING | VALID | INVALID
]

CF_TABLE_COLUMNS = [
    "counterfactual",
    "n",
    "pnl_sum_bp",
    "ev_per_trade",
    "mfe_mean",
    "mae_mean",
    "delta_vs_actual",
]


def empty_cf_table() -> List[Dict[str, Any]]:
    rows = [
        {"counterfactual": "Actual Aging 3s", "n": None, "pnl_sum_bp": None,
         "ev_per_trade": None, "mfe_mean": None, "mae_mean": None, "delta_vs_actual": 0.0},
    ]
    for h in AGING_CF_HORIZONS_SEC:
        rows.append({
            "counterfactual": f"CF Aging {int(h)}s",
            "n": None, "pnl_sum_bp": None, "ev_per_trade": None,
            "mfe_mean": None, "mae_mean": None, "delta_vs_actual": None,
        })
    rows.append({
        "counterfactual": "CF Best-Maker Exit",
        "n": None, "pnl_sum_bp": None, "ev_per_trade": None,
        "mfe_mean": None, "mae_mean": None, "delta_vs_actual": None,
    })
    rows.append({
        "counterfactual": "CF Baseline Exit",
        "n": None, "pnl_sum_bp": None, "ev_per_trade": None,
        "mfe_mean": None, "mae_mean": None, "delta_vs_actual": None,
    })
    return rows


def capture_rate_audit_checklist() -> Dict[str, Any]:
    """Capture = realized_pnl_bp / theory_spread_bp * 100 — can go negative; not a fill-rate."""
    return {
        "status": "INVALID_UNTIL_AUDIT",
        "observed_values_example": [-102.8, -123.3, -126.4, 14.0, 13.8],
        "current_formula": "(realized_pnl_bp / theory_spread_bp) * 100",
        "codepath": "antigravity/quant_pipeline/adverse_excursion.py :: record_exit",
        "audit_items": [
            "numerator (realized_pnl_bp sign / units)",
            "denominator (theory_spread_bp definition)",
            "sign convention (loss → negative capture expected under this formula)",
            "zero denominator handling",
            "rolling-window reset / aggregation",
            "whether Discord label implies fill-rate (misleading if so)",
        ],
        "promotion_gate": False,
        "note": "Do not use Capture for Economic PASS / Promotion until definition locked",
    }


def build_lock() -> Dict[str, Any]:
    return {
        "csr": 545,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "PEG_v3 REJECT continue · Aging Exit CF grid · Capture INVALID",
        "primary_evidence_07h": {
            "source": "user_presented_integrated_log",
            "peg_v3_24h_bp": -55.19,
            "baseline_24h_bp": -4.47,
            "delta_vs_baseline_bp": -50.72,
            "ae_1s_delta_bp": 1.30,
            "ae_3s_delta_bp": 1.20,
            "aging_guard_exits_max": 43,
            "be3_exits": 0,
            "fakeliq_skips": 0,
        },
        "structure": [
            "Entry/AE improved",
            "AgingGuard dominates exits (~3s)",
            "BE3 inactive (0)",
            "FakeLiquidity inactive (0)",
            "Economic PnL << Baseline",
        ],
        "claims_allowed": {
            "ae_not_converted_to_pnl": True,
            "entry_edge_fully_proven": False,
            "agingguard_friction_convicted": False,
            "agingguard_primary_suspect": True,
        },
        "status": {
            "csr": "CSR-522 / PEG_v3",
            "ae_improvement": "PASS_LIKE_EVIDENCE",
            "economic_pnl": "FAIL",
            "aging_guard": "PRIMARY_SUSPECT",
            "be3": "INACTIVE",
            "fake_liquidity": "INACTIVE",
            "capture_rate": "INVALID_UNTIL_AUDIT",
            "entry_redesign": "HOLD",
            "exit_cf_research": "P0",
            "parameter_change": "NO",
            "economic_pass": False,
            "promotion": "REJECT",
            "wire": "NO",
            "enforce": 0,
            "peg_v4": "FORBIDDEN_FOR_NOW",
            "aging_3_to_5_live_change": "FORBIDDEN — CF only",
        },
        "aging_exit_log_schema": AGING_EXIT_LOG_SCHEMA,
        "cf_table_template": empty_cf_table(),
        "cf_table_columns": CF_TABLE_COLUMNS,
        "capture_rate_audit": capture_rate_audit_checklist(),
        "method_align": "Same as CSR-521: FROZEN actual + multi-horizon Virtual CF; never Actual+CF=improved",
        "gov": {
            "LIVE": "OFF",
            "WIRE": "NO",
            "ENFORCE": 0,
            "FROZEN": "YES",
            "AUTO_APPLY": "OFF",
        },
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr545_peg_v3_aging_cf_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    schema_path = OUT / "2026-09-28_csr545_peg_aging_exit_log_schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "schema": AGING_EXIT_LOG_SCHEMA,
                "horizons_sec": list(AGING_CF_HORIZONS_SEC),
                "actual_aging_sec": 3.0,
                "param_change": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "lock": str(path),
                "schema": str(schema_path),
                "promotion": "REJECT",
                "aging_change_live": False,
                "capture": "INVALID_UNTIL_AUDIT",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
