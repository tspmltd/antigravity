#!/usr/bin/env python3
"""CSR-576 — 4AGENT Council = common context label generator (lock).

Council is NOT a strategy to “win”. It emits shared regime / candidate /
decision / FakeBO labels so UMM, Arena, and PEG compare on one coordinate
system via FusionDecisionLog → Parquet → DuckDB.

Prerequisite: CSR-564 freshness Gate.
Parallel: CSR-575 PEG attribution · CSR-572b UMM anatomy.
WIRE=NO · ENFORCE=0 · LIVE=OFF · auto_apply=OFF · Economic PASS=NO
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
        "csr": 576,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "4AGENT Council = common context label generator · not a winner to promote",
        "role": "shared_context_label_generator",
        "not_the_goal": [
            "make Council beat baseline PnL",
            "Council promotion / WIRE / ENFORCE",
            "final-alone teacher",
        ],
        "coordinate_system": {
            "regime_tag": ["trend", "range", "high_vol", "low_vol"],
            "decision_stack": ["candidate", "decision", "veto"],
            "fake_bo": ["effective", "redundant"],
            "consumers": ["UMM", "Arena", "PEG", "TF2BP_CTRL"],
        },
        "fusion_decision_log_fields": [
            "trend_direction",
            "trend_strength",
            "regime_tag",
            "pressure_side",
            "fake_breakout_flag",
            "realized_pnl",
        ],
        "lake": [
            "orderbook_micro",
            "fusion_log",
            "execution_log",
            "Parquet",
            "DuckDB",
        ],
        "prerequisite": "CSR-564 freshness Gate",
        "parallel": ["CSR-575 PEG attribution", "CSR-572b UMM anatomy"],
        "governance": {
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
            "AUTO_APPLY": "OFF",
            "Economic_PASS": "NO",
        },
        "ops_doc": "docs/ops/council_context_label_generator_csr576.md",
        "schema_sot": "antigravity/quant_pipeline/schema.py::FusionDecisionLog",
        "core": (
            "Council emits shared regime/candidate/decision/FakeBO labels "
            "so UMM, Arena, and PEG compare on one coordinate system. "
            "Do not optimize Council to win."
        ),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = build()
    path = OUT / "2026-10-04_csr576_council_context_label_generator_lock.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
