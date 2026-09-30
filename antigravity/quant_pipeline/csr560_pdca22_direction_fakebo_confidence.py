#!/usr/bin/env python3
"""CSR-560 — PDCA2.2: Direction / FakeBO CF / Confidence calibration specs.

Primary = user BizChat synthesis + 27h Council hourly + prior CSR-558/559.
Do NOT open LIVE. Plan/Check SQL only. Economic PASS=NO.

Deliverables:
  1) DuckDB SQL: sql/csr560_pdca22_council_direction_fakebo_confidence.sql
  2) FakeBO Effective-Veto CF evaluation spec (decision BUY/SELL only)
  3) Confidence calibration judgment table + PASS/FAIL gates
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))
SQL_PATH = (
    Path(__file__).resolve().parent
    / "sql"
    / "csr560_pdca22_council_direction_fakebo_confidence.sql"
)

# --- Confidence calibration criteria (judgment table) ---
CONF_BUCKETS: List[Dict[str, Any]] = [
    {
        "bucket": "0-.25",
        "range": "[0.00, 0.25)",
        "role": "noise / EXIT zone",
        "expect": "EV≈0 or negative; not a trade cohort",
        "min_n_for_judge": 30,
    },
    {
        "bucket": ".25-.45",
        "range": "[0.25, 0.45)",
        "role": "weak candidate · usually decision HOLD/EXIT",
        "expect": "EV ≤ mid buckets; gate should block most",
        "min_n_for_judge": 30,
    },
    {
        "bucket": ".45-.65",
        "range": "[0.45, 0.65)",
        "role": "below decision threshold (0.65)",
        "expect": "CF-if-traded often ≤0 → gate valuable",
        "min_n_for_judge": 40,
    },
    {
        "bucket": ".65-.80",
        "range": "[0.65, 0.80)",
        "role": "decision pass-through (weak)",
        "expect": "EV_30s > .45-.65",
        "min_n_for_judge": 40,
    },
    {
        "bucket": ".80-.99",
        "range": "[0.80, 1.00)",
        "role": "high conf unsaturated",
        "expect": "EV_30s ≥ .65-.80",
        "min_n_for_judge": 40,
    },
    {
        "bucket": "1.00",
        "range": "[1.00, 1.00]",
        "role": "CLIPPED / saturated (suspect)",
        "expect": "If EV_30s ≯ .80-.99 → saturation FAIL (log strength_raw)",
        "min_n_for_judge": 50,
        "note": "min(1, |diff|/3000) clips large moves — keep strength_raw separate",
    },
]

CALIBRATION_GATES = {
    "PASS": [
        "n per judged bucket ≥ min_n_for_judge",
        "mono_rate on adjacent [.45-.65 → .65-.80 → .80-.99 → 1.00] ≥ 0.67 (2/3 pairs)",
        "ev_30s(.65-.80) > 0 OR clearly better than .45-.65 CF-if-traded",
        "hit_30s non-decreasing across judged buckets OR EV mono holds",
        "latency_le_80 and latency_gt_80 reported separately (no mix for PASS)",
    ],
    "FAIL": [
        "ev_30s(1.00) ≤ ev_30s(.65-.80) with n_100≥50 and n_65≥40 (saturation)",
        "mono_rate < 0.50",
        "mean_strength_raw at bucket 1.00 >> 1.0 while EV flat (clipping artifact)",
        "any Economic PASS claimed before OOS period",
    ],
    "NEED_MORE": [
        "any judged bucket n < min_n",
        "only rolling Discord windows available (need trade/candidate cohort)",
    ],
    "illustration_only_NOT_measured": {
        ".45-.65": "+0.05bp",
        ".65-.80": "+0.15bp",
        ".80-.99": "+0.30bp",
        "1.00": "+0.45bp",
        "note": "BizChat illustration — NOT real EV; do not treat as target",
    },
}

# --- FakeBO CF spec ---
FAKEBO_CF_SPEC = {
    "title": "Effective FakeBO Veto Counterfactual",
    "population_include": {
        "sql": (
            "decision_action IN ('buy','sell') AND "
            "(veto_effective OR (veto AND fake_bo) OR veto_reason IN ('FAKE_BREAKOUT','FAKE_BO'))"
        ),
        "rationale": "Only tradable decisions blocked by FakeBO (CSR-558 Effective)",
        "27h_hourly_target_n": "≥9 BUY/SELL+veto (from 22 decisions; expand with 5m snaps)",
    },
    "population_exclude": {
        "sql": "decision_action='hold' AND fake_bo",
        "label": "REDUNDANT",
        "rationale": "confidence gate already HOLD — do not credit Veto economics",
    },
    "horizons_sec": [1, 3, 10, 30, 60],
    "metrics": {
        "cf_h_bp": "dir_fwd_bp(decision_action, mid_t0, mid_h) — if executed despite veto",
        "avoided_loss_30s": "max(0, -cf_30s)",
        "missed_profit_30s": "max(0, +cf_30s)",
        "net_veto_value_30s": "-cf_30s  (= avoided_loss - missed_profit)",
        "pct_cf_negative_30s": "share of vetoed decisions with cf_30s < 0",
    },
    "contrast_table": [
        "decision BUY/SELL + no veto",
        "decision BUY/SELL + FAKE veto (effective)",
        "decision HOLD + FAKE veto (redundant audit only)",
    ],
    "PASS": [
        "n_effective ≥ 20 (NEED_MORE if 9≤n<20; do not Economic PASS on n=9 alone)",
        "mean_cf_30s < 0 on effective cohort",
        "mean_cf_30s(effective) < mean_ev_30s(no_veto) on same join rules",
        "redundant cohort excluded from PASS math",
        "OOS second calendar day confirms sign of net_veto_value_30s",
    ],
    "FAIL": [
        "mean_cf_30s > 0 with n≥20 (veto blocking winners)",
        "effective EV ≥ no_veto EV",
    ],
    "gov": {
        "WIRE": "NO",
        "ENFORCE": 0,
        "LIVE": "OFF",
        "Economic_PASS": "NO until PASS+OOS",
    },
}

PRIORITY = {
    "P0": "Trend candidate forward-PnL (csr560_p0_*)",
    "P1": "FakeBO effective veto CF (csr560_fakebo_*)",
    "P2": "Adverse CSR-539 integrity (still blocks Economic PASS)",
    "P3": "UMM Inventory Control CF (CSR-521) — separate lane",
    "P4": "PEG AgingGuard CF — Baseline FROZEN",
    "P5": "Spread Gate reason-level CF",
    "LAST": "Fusion W_PRESSURE — only after pressure has information",
    "frozen": [
        "Arena winners",
        "TF2BP Baseline",
        "LIVE OFF",
        "WIRE=NO",
        "auto_apply=OFF",
        "no threshold knobs",
    ],
}


def build_lock() -> Dict[str, Any]:
    sql_text = SQL_PATH.read_text(encoding="utf-8") if SQL_PATH.exists() else ""
    return {
        "csr": 560,
        "parent": ["CSR-538", "CSR-558", "CSR-559"],
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "PDCA2.2 DuckDB P0 Direction · FakeBO CF · Confidence calibration",
        "primary_source": "user_bizchat_pdca22_council_synthesis",
        "m365_corroboration": False,
        "sql_path": str(SQL_PATH.relative_to(ROOT)) if SQL_PATH.exists() else None,
        "sql_bytes": len(sql_text.encode("utf-8")),
        "confidence_calibration_table": CONF_BUCKETS,
        "calibration_gates": CALIBRATION_GATES,
        "fakebo_cf_spec": FAKEBO_CF_SPEC,
        "teacher_rules": {
            "allowed": ["candidate_action", "decision_action"],
            "forbidden": ["final_action"],
            "adverse": "WHERE adverse_score_valid=true ONLY",
            "latency_split": "≤80 vs >80 mandatory for research PASS",
        },
        "priority": PRIORITY,
        "pdca2": {
            "Plan": "LOCKED",
            "Do": "SQL_READY_SHADOW_ONLY",
            "Check": "PENDING_JOIN_mid",
            "Act": "CLOSED",
        },
        "governance": {
            "NO_CODE_CHANGE_TO_LIVE": True,
            "NO_PARAM_CHANGE": True,
            "WIRE": "NO",
            "ENFORCE": 0,
            "AUTO_APPLY": "OFF",
            "LIVE": "OFF",
            "Economic_PASS": "NO",
        },
        "core": (
            "Prove Direction EV, 0.65 gate value, and Effective FakeBO veto in bp "
            "separately before Fusion weights or LIVE."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-30_csr560_pdca22_direction_fakebo_confidence_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    # also emit calibration table alone for Discord/BizChat paste
    table_path = OUT / "2026-09-30_csr560_confidence_calibration_criteria.json"
    table_path.write_text(
        json.dumps(
            {"buckets": CONF_BUCKETS, "gates": CALIBRATION_GATES},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    fake_path = OUT / "2026-09-30_csr560_fakebo_effective_veto_cf_spec.json"
    fake_path.write_text(json.dumps(FAKEBO_CF_SPEC, ensure_ascii=False, indent=2), encoding="utf-8")
    assert SQL_PATH.exists(), "SQL missing"
    sql_text = SQL_PATH.read_text(encoding="utf-8")
    assert "csr560_fakebo_effective_veto_rows" in sql_text
    assert "csr560_p0_direction_by_conf" in sql_text
    assert lock["fakebo_cf_spec"]["population_exclude"]["label"] == "REDUNDANT"
    print(json.dumps({
        "ok": True,
        "lock": str(path),
        "sql": str(SQL_PATH),
        "buckets": [b["bucket"] for b in CONF_BUCKETS],
        "fakebo_min_n_pass": 20,
        "gov": lock["governance"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
