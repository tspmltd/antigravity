#!/usr/bin/env python3
"""CSR-575 — PEG_v3 Attribution Directive lock (dissect · no PEG_v4).

PEG_v3 is an attribution instrument, not a promotion candidate.
WIRE=NO · ENFORCE=0 · Economic PASS=NO · PEG_v4 forbidden until done.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))


def build() -> dict:
    return {
        "csr": 575,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "PEG_v3 Attribution Directive · dissect not promote · PEG_v4 forbidden",
        "status": {
            "PEG_v3_Promotion": "REJECT",
            "Economic_PASS": "NO",
            "WIRE": "NO",
            "ENFORCE": 0,
            "AUTO_APPLY": "OFF",
            "PEG_v4": "FORBIDDEN_UNTIL_ATTRIBUTION_DONE",
        },
        "role": "dissection_instrument",
        "lake": [
            "orderbook_micro",
            "fusion_log",
            "execution_log",
            "Parquet",
            "DuckDB",
        ],
        "p0": {
            "run_integrity": [
                "run_id",
                "strategy_version",
                "config_hash",
                "started_at",
            ],
            "maker_entry_arms": {
                "CTRL": "TF2BP Baseline",
                "ARM-A": "Maker Entry + Baseline Exit",
                "ARM-B": "Maker Entry + AgingGuard only",
                "ARM-C": "Maker Entry + BE3 only",
                "ARM-D": "Maker Entry + AdaptiveRatio only",
            },
            "aging_value_bp": "actual_exit_bp - cf_baseline_exit_bp",
        },
        "p1": [
            "AE_raw_recompute",
            "Capture_audit_not_gate",
            "FakeLiq_eligible_decompose",
            "BE3_N_A_if_exits_0",
        ],
        "success": [
            "Maker Entry standalone EV",
            "AgingGuard incremental EV",
            "BE3 exposure measurable",
            "FakeLiq zero-trigger cause",
            "AE_3s +1.20bp provenance",
            "Capture extreme cause",
        ],
        "forbid": [
            "PEG_v4",
            "thr_retune",
            "PASS_from_AE_or_Capture",
            "cross_run_sum",
        ],
        "ops_doc": "docs/ops/peg_v3_attribution_directive_csr575.md",
        "core": (
            "Use PEG_v3 as attribution instrument on existing Parquet lake. "
            "Do not build PEG_v4 until Maker/Aging/AE/Capture/FakeLiq/BE3 "
            "attribution complete."
        ),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = build()
    path = OUT / "2026-10-04_csr575_peg_v3_attribution_directive_lock.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
