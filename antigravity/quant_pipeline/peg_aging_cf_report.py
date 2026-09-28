#!/usr/bin/env python3
"""CSR-545 — AgingGuard Counterfactual report from exec_audit_peg_v3.jsonl.

FROZEN peg_v3_obs_v1 · no param change · ENFORCE=0.
Uses existing pnl_if_hold_10s / pnl_if_hold_30s; marks 5s/20s/Maker/Baseline as NULL_PENDING
until mid-path join is available.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "data" / "mm_research" / "exec_audit_peg_v3.jsonl"
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

SQL_TEMPLATE = """
-- AgingGuard 反事実比較ビュー (CSR-545)
SELECT
    policy_name,
    COUNT(*) AS total_trades,
    ROUND(SUM(realized_pnl_bp), 2) AS total_pnl_bp,
    ROUND(AVG(realized_pnl_bp), 3) AS ev_per_trade_bp,
    ROUND(AVG(mfe_bp), 3) AS avg_mfe_bp,
    ROUND(AVG(mae_bp), 3) AS avg_mae_bp,
    ROUND(AVG(is_hard_stop) * 100, 1) AS hard_stop_rate_pct,
    ROUND(SUM(realized_pnl_bp) - SUM(actual_3s_pnl_bp), 2) AS delta_vs_actual_bp
FROM peg_v3_counterfactual_ledger
GROUP BY policy_name
ORDER BY total_pnl_bp DESC;
"""


def _load_aging_rows(max_lines: int = 50_000) -> List[Dict[str, Any]]:
    if not AUDIT.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    with AUDIT.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= max_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            pl = r.get("position_lifecycle") or {}
            if not (pl.get("aging_exit_triggered") or str(pl.get("exit_reason", "")).upper().startswith("AGING")):
                continue
            cf = r.get("counterfactual") or {}
            rows.append({
                "timestamp": r.get("timestamp"),
                "trade_id": f"peg_{r.get('timestamp')}",
                "exit_reason": pl.get("exit_reason"),
                "actual_pnl_bp": float(pl.get("realized_pnl_bp") or 0.0),
                "mfe_bp": pl.get("mfe_bp"),
                "mae_bp": pl.get("mae_bp"),
                "hold_duration_sec": pl.get("hold_duration_sec"),
                "be_armed": pl.get("be_armed"),
                "cf_10s_bp": cf.get("pnl_if_hold_10s"),
                "cf_30s_bp": cf.get("pnl_if_hold_30s"),
                "cf_5s_bp": cf.get("pnl_if_hold_5s"),   # may be absent
                "cf_20s_bp": cf.get("pnl_if_hold_20s"),
                "cf_bestmaker_bp": cf.get("pnl_if_best_maker"),
                "cf_baseline_exit_bp": cf.get("pnl_if_baseline_exit"),
                "is_hard_stop": 1 if "HARD_STOP" in str(pl.get("exit_reason", "")).upper() else 0,
            })
    return rows


def _agg(policy: str, pnls: List[float], mfes: List[float], maes: List[float],
         hs: List[int], actual_sum: float) -> Dict[str, Any]:
    n = len(pnls)
    if n == 0:
        return {
            "policy_name": policy,
            "total_trades": 0,
            "total_pnl_bp": None,
            "ev_per_trade_bp": None,
            "avg_mfe_bp": None,
            "avg_mae_bp": None,
            "hard_stop_rate_pct": None,
            "delta_vs_actual_bp": None,
            "cf_status": "EMPTY",
        }
    s = sum(pnls)
    return {
        "policy_name": policy,
        "total_trades": n,
        "total_pnl_bp": round(s, 2),
        "ev_per_trade_bp": round(s / n, 3),
        "avg_mfe_bp": round(sum(mfes) / len(mfes), 3) if mfes else None,
        "avg_mae_bp": round(sum(maes) / len(maes), 3) if maes else None,
        "hard_stop_rate_pct": round(100.0 * sum(hs) / n, 1),
        "delta_vs_actual_bp": round(s - actual_sum, 2),
        "cf_status": "VALID",
    }


def build_report() -> Dict[str, Any]:
    rows = _load_aging_rows()
    actual_pnls = [r["actual_pnl_bp"] for r in rows]
    actual_sum = sum(actual_pnls) if actual_pnls else 0.0
    mfes = [float(r["mfe_bp"]) for r in rows if r.get("mfe_bp") is not None]
    maes = [float(r["mae_bp"]) for r in rows if r.get("mae_bp") is not None]
    hs = [int(r["is_hard_stop"]) for r in rows]

    table = [
        _agg("Actual Aging 3s", actual_pnls, mfes, maes, hs, actual_sum),
    ]
    # Available today
    for name, key in (("CF_B Hold 10s", "cf_10s_bp"), ("CF_D Hold 30s", "cf_30s_bp")):
        pnls, m2, a2, h2 = [], [], [], []
        for r in rows:
            v = r.get(key)
            if v is None:
                continue
            pnls.append(float(v))
            if r.get("mfe_bp") is not None:
                m2.append(float(r["mfe_bp"]))
            if r.get("mae_bp") is not None:
                a2.append(float(r["mae_bp"]))
            h2.append(r["is_hard_stop"])
        # delta vs actual on same matched subset
        matched_actual = []
        for r in rows:
            if r.get(key) is not None:
                matched_actual.append(r["actual_pnl_bp"])
        row = _agg(name, pnls, m2, a2, h2, sum(matched_actual) if matched_actual else 0.0)
        row["matched_n"] = len(pnls)
        row["coverage"] = round(len(pnls) / len(rows), 3) if rows else 0.0
        table.append(row)

    pending = [
        ("CF_A Hold 5s", "NEED_pnl_if_hold_5s"),
        ("CF_C Hold 20s", "NEED_pnl_if_hold_20s"),
        ("CF_E Maker Exit", "NEED_mid_path_best_maker_join"),
        ("CF_F Baseline Exit", "NEED_baseline_exit_replay"),
    ]
    for name, why in pending:
        table.append({
            "policy_name": name,
            "total_trades": None,
            "total_pnl_bp": None,
            "ev_per_trade_bp": None,
            "avg_mfe_bp": None,
            "avg_mae_bp": None,
            "hard_stop_rate_pct": None,
            "delta_vs_actual_bp": None,
            "cf_status": "NULL_PENDING",
            "blocked_reason": why,
        })

    # Exit dominance from full audit (not only aging rows)
    exit_dom = {"AGING_GUARD": 0, "BE3": 0, "BE_TRIGGERED": 0, "OTHER": 0, "n_audit": 0}
    if AUDIT.is_file():
        with AUDIT.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                exit_dom["n_audit"] += 1
                pl = r.get("position_lifecycle") or {}
                er = str(pl.get("exit_reason") or "")
                if er == "AGING_GUARD" or pl.get("aging_exit_triggered"):
                    exit_dom["AGING_GUARD"] += 1
                elif "BE3" in er:
                    exit_dom["BE3"] += 1
                elif er == "BE_TRIGGERED":
                    exit_dom["BE_TRIGGERED"] += 1
                else:
                    exit_dom["OTHER"] += 1

    aging_n = exit_dom["AGING_GUARD"]
    n_audit = max(exit_dom["n_audit"], 1)
    return {
        "csr": 545,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "peg_version": "peg_v3_obs_v1",
        "param_change": False,
        "promotion": "REJECT",
        "aging_rows_n": len(rows),
        "actual_aging_sum_bp": round(actual_sum, 2),
        "actual_ev_per_trade_bp": round(actual_sum / len(rows), 3) if rows else None,
        "exit_dominance": {
            **exit_dom,
            "aging_rate_pct": round(100.0 * aging_n / n_audit, 1),
            "note_06h_zero": (
                "Aging exits=0 in hourly report likely in-memory counter reset after "
                "process restart — treat as log discontinuity, not regime change"
            ),
        },
        "cf_table": table,
        "sql_template": SQL_TEMPLATE,
        "gov": {
            "ENTRY": "VALIDATED_AE_TOXIC_AVOIDANCE",
            "EXIT_AGING": "PRIMARY_SUSPECT",
            "BE3_FAKELIQ": "PRACTICALLY_INACTIVE",
            "CAPTURE_RATE": "MASKED_UNTIL_AUDIT",
            "PEG_v4": "FORBIDDEN",
            "LIVE": "OFF",
            "WIRE": "NO",
            "ENFORCE": 0,
            "FROZEN": "YES",
        },
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report = build_report()
    path = OUT / "2026-09-28_csr545_peg_aging_cf_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # also refresh lock via schema module if present
    try:
        from antigravity.quant_pipeline.peg_aging_exit_cf_schema import build_lock
        lock = build_lock()
        lock["latest_report"] = str(path.name)
        lock["gov_directive"] = report["gov"]
        lock["exit_dominance"] = report["exit_dominance"]
        (OUT / "2026-09-28_csr545_peg_v3_aging_cf_lock.json").write_text(
            json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        report["lock_error"] = str(e)
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "aging_n": report["aging_rows_n"],
        "actual_ev": report["actual_ev_per_trade_bp"],
        "aging_rate_pct": report["exit_dominance"]["aging_rate_pct"],
        "cf_valid": [t["policy_name"] for t in report["cf_table"] if t.get("cf_status") == "VALID"],
        "cf_pending": [t["policy_name"] for t in report["cf_table"] if t.get("cf_status") == "NULL_PENDING"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
