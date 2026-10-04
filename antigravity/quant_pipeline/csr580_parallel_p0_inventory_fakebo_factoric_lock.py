#!/usr/bin/env python3
"""CSR-580 — Parallel P0-A/B/C research priority lock.

P0-A Inventory Decay State
P0-B Effective FakeBO Veto CF (06:20/25/35/50 would-have-lost?)
P0-C FactorIC spread × inventory EER

No new strategy. WIRE=NO · ENFORCE=0 · Economic PASS=NO.
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
        "csr": 580,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Parallel P0-A Inventory Decay · P0-B FakeBO CF · P0-C FactorIC Inventory",
        "priority_model": "parallel_triple_p0",
        "tracks": {
            "P0-A": {
                "name": "Inventory Decay State",
                "question": "cut-early vs must-not-cut-early separators",
                "forbid": "fixed_sec_exit_adopt",
            },
            "P0-B": {
                "name": "Effective FakeBO Veto CF",
                "first_question": (
                    "Did the 4 FakeBO-stopped virtual trades at "
                    "06:20/06:25/06:35/06:50 subsequently lose?"
                ),
                "window": "2026-10-05 06:20–06:50 JST",
                "promote_if": "YES → economic veto candidate (WIRE still gated)",
            },
            "P0-C": {
                "name": "FactorIC spread × inventory",
                "deliverable": "EER vs spread-only",
            },
        },
        "p0b_probe_nearest_proxy": {
            "source": "micro_veto_audit.jsonl",
            "YES_LOST": 1,
            "NO_PROFIT": 3,
            "PENDING": 0,
            "status": "NEED_EPISODE_MATCH",
            "caveat": "candidate_action==decision_action; Discord episode_id not matched",
            "rows": [
                {"anchor": "06:20", "cf_bp_30s": 4.6901, "flag": "NO_PROFIT"},
                {"anchor": "06:25", "cf_bp_30s": 2.3383, "flag": "NO_PROFIT"},
                {"anchor": "06:35", "cf_bp_30s": 1.2253, "flag": "NO_PROFIT"},
                {"anchor": "06:50", "cf_bp_30s": -1.8735, "flag": "YES_LOST"},
            ],
        },
        "lake": [
            "orderbook_micro",
            "fusion_log",
            "execution_log",
            "duckdb_posthoc",
        ],
        "governance": {
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
            "AUTO_APPLY": "OFF",
            "Economic_PASS": "NO",
            "FakeBO_thr_retune": "FORBIDDEN",
        },
        "ops_doc": "docs/ops/parallel_p0_inventory_fakebo_factoric_csr580.md",
        "core": (
            "Run Inventory Decay, FakeBO effective CF, and FactorIC Inventory "
            "in parallel. First answer whether the four morning FakeBO stops "
            "would have lost money."
        ),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = build()
    path = OUT / "2026-10-05_csr580_parallel_p0_inventory_fakebo_factoric_lock.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
