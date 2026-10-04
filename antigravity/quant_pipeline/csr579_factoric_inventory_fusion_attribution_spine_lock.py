#!/usr/bin/env python3
"""CSR-579 — FactorIC → Inventory Risk → FusionParquet trade-level attribution spine.

Primary research path (not PEG / TrendFollow). Builds one teacher table:
market state × inventory state × micro state → trade outcome.
WIRE=NO · ENFORCE=0 · Economic PASS=NO · no new alpha until table exists.
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
        "csr": 579,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "FactorIC → Inventory Risk → FusionParquet trade-level attribution spine",
        "primary": True,
        "not_primary": ["PEG", "TrendFollow"],
        "spine": [
            "FactorICResearch",
            "MM_Inventory_Risk_EER",
            "FusionParquet_asof_join",
            "trade_level_teacher_table",
        ],
        "teacher_axes": [
            "market_state",
            "inventory_state",
            "micro_state",
            "outcome_pnl",
        ],
        "lake": [
            "orderbook_micro",
            "fusion_log",
            "execution_log",
            "async_parquet",
            "duckdb_posthoc",
        ],
        "effect": (
            "PEG/Council/FakeBO/Inventory-unwind become slices of one teacher table "
            "instead of separate research programs"
        ),
        "governance": {
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
            "AUTO_APPLY": "OFF",
            "Economic_PASS": "NO",
            "new_alpha": "FORBIDDEN_UNTIL_TEACHER_TABLE",
        },
        "ops_doc": "docs/ops/factoric_inventory_fusion_attribution_spine_csr579.md",
        "bizchat_feedback": (
            "最優先は PEG/TF 改修ではなく FactorIC→Inventory→FusionParquet の "
            "trade-level attribution。執行軽量+非同期Parquet+DuckDB後段。"
            "WIRE=NO · ENFORCE=0 · PASS=NO。"
        ),
        "core": (
            "One teacher table explaining which market/inventory/micro state "
            "decided each trade — demote PEG/TF as primary research."
        ),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = build()
    path = OUT / "2026-10-04_csr579_factoric_inventory_fusion_attribution_spine_lock.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
