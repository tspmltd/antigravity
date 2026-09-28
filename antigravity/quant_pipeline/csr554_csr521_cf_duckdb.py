#!/usr/bin/env python3
"""CSR-554 — CSR-521 mm_trade_cf_log schema + DuckDB CF report templates.

RULES:
  CF Δ = Virtual PnL − Actual PnL
  Never rewrite Actual · Never publish Actual+CF as realized improvement
  Latency = CONDITION (GROUP BY) · not PnL debit
  economic_pass=NO · ENFORCE=0 · WIRE=NO · AUTO_APPLY=OFF
  BIG_DAMAGE must be reviewed before any Economic PASS attempt
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
SQL_PATH = Path(__file__).resolve().parent / "sql" / "csr521_conditional_unwind_cf.sql"
JST = timezone(timedelta(hours=9))

CSR = "CSR-554"
PARENT = "CSR-521"

MM_TRADE_CF_LOG_SCHEMA: List[str] = [
    # identity / actual (immutable after close)
    "trade_id",
    "entry_ts",
    "side",
    "actual_exit_ts",
    "actual_exit_reason",
    "actual_pnl_bp",
    # state at decision / path
    "hold_time_s",
    "hold_ladder",
    "current_pnl_bp",
    "mae_bp",
    "mfe_bp",
    "gross_spread_bp",
    "ae_1s_bp",
    "ae_3s_bp",
    "adverse_score",  # NULL when invalid / SHADOW_OFF
    "latency_ms",
    "regime",
    "fake_bo",
    # blanket CF horizons (research only)
    "cf_10s_bp",
    "cf_20s_bp",
    "cf_30s_bp",
    "cf_45s_bp",
    "cf_60s_bp",
    "cf_90s_bp",
    "cf_120s_bp",
    # conditional + maker CF
    "cf_conditional_bp",
    "cf_maker_05s_bp",
    "cf_maker_10s_bp",
    "cf_maker_20s_bp",
    "cf_maker_30s_bp",
    "conditional_trigger",
    "conditional_score",
]

FORBIDDEN = [
    "REWRITE_ACTUAL_PNL",
    "REPORT_ACTUAL_PLUS_CF_AS_REALIZED",
    "LATENCY_COST_DEBIT_FROM_ACTUAL",
    "ECONOMIC_PASS_WITHOUT_BIG_DAMAGE_REVIEW",
    "ENFORCE_1",
    "WIRE_YES",
    "AUTO_APPLY",
]


def empty_policy_table() -> List[Dict[str, Any]]:
    rows = []
    for p in ("10s", "20s", "30s", "45s", "60s", "90s", "120s", "CONDITIONAL"):
        rows.append({
            "policy": p, "n": None, "actual_bp": None, "virtual_bp": None,
            "delta_bp": None, "delta_per_trade_bp": None, "improve_rate_pct": None,
        })
    return rows


def empty_outcome_table() -> List[Dict[str, Any]]:
    return [
        {"outcome": o, "n": None, "total_delta_bp": None, "avg_delta_bp": None}
        for o in ("BIG_SAVE", "SMALL_SAVE", "UNCHANGED", "SMALL_DAMAGE", "BIG_DAMAGE")
    ]


def empty_final_report() -> Dict[str, Any]:
    return {
        "title": "CSR-521 CONDITIONAL UNWIND CF",
        "actual": {
            "trades": None,
            "actual_pnl_bp": None,
            "hard_stops": None,
            "hs_pnl_bp": None,
        },
        "blanket": {
            "10s_delta_bp": None,
            "20s_delta_bp": None,
            "30s_delta_bp": None,
            "60s_delta_bp": None,
        },
        "conditional": {
            "triggered": None,
            "triggered_of": None,
            "conditional_delta_bp": None,
            "delta_per_trade_bp": None,
            "improve_rate_pct": None,
            "BIG_SAVE": None,
            "BIG_DAMAGE": None,
        },
        "hard_stop": {
            "hs_rescue_rate_pct": None,
            "hs_saved_pnl_bp": None,
        },
        "maker_exit": {
            "0.5s_delta_bp": None,
            "1.0s_delta_bp": None,
            "2.0s_delta_bp": None,
            "3.0s_delta_bp": None,
        },
        "condition": {
            "high_adverse_x_l2plus_ev": None,
            "high_latency_x_l2plus_ev": None,
            "high_lat_x_high_adv_x_l2plus": None,
        },
        "governance": {
            "economic_pass": False,
            "enforce": 0,
            "wire": "NO",
            "auto_apply": "OFF",
            "note": (
                "Promote only after Conditional > Blanket across windows, "
                "BIG_DAMAGE acceptable, HS rescue reproducible"
            ),
        },
    }


def build_lock() -> Dict[str, Any]:
    sql_text = SQL_PATH.read_text(encoding="utf-8") if SQL_PATH.exists() else ""
    return {
        "csr": 554,
        "parent": PARENT,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "mm_trade_cf_log + DuckDB CF scorecards (Actual immutable)",
        "schema_note": (
            "Proposed ADD-ON for CSR-521 CF research — not claiming these cols "
            "already exist in legacy Microstructure schema"
        ),
        "mm_trade_cf_log_schema": MM_TRADE_CF_LOG_SCHEMA,
        "formula": "CF_delta_bp = virtual_pnl_bp - actual_pnl_bp",
        "sql_path": str(SQL_PATH.relative_to(ROOT)) if SQL_PATH.exists() else None,
        "sql_bytes": len(sql_text),
        "queries": [
            "Q2_policy_scorecard",
            "Q3_conditional_ladder_adverse",
            "Q4_adverse_latency_inventory_condition",
            "Q5_maker_wait_cf",
            "Q6_save_damage_outcomes",
            "Q7_hardstop_rescue",
        ],
        "tables": {
            "policy_scorecard": empty_policy_table(),
            "save_damage": empty_outcome_table(),
            "final_report": empty_final_report(),
        },
        "forbidden": FORBIDDEN,
        "gov": {
            "economic_pass": False,
            "ENFORCE": 0,
            "WIRE": "NO",
            "AUTO_APPLY": "OFF",
            "LIVE": "OFF",
        },
        "core": (
            "Measure Conditional vs Blanket by Ladder/Adverse/Latency condition; "
            "track BIG_DAMAGE before any PASS talk; Actual stays frozen."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr554_csr521_cf_duckdb_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    # also dump empty final report for Discord paste
    rep = OUT / "2026-09-28_csr554_csr521_cf_report_template.json"
    rep.write_text(json.dumps(empty_final_report(), ensure_ascii=False, indent=2), encoding="utf-8")
    assert lock["gov"]["economic_pass"] is False
    assert "REWRITE_ACTUAL_PNL" in lock["forbidden"]
    assert SQL_PATH.exists()
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "sql": str(SQL_PATH),
        "report": str(rep),
        "cols": len(MM_TRADE_CF_LOG_SCHEMA),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
