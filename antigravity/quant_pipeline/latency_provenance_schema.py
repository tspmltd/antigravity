#!/usr/bin/env python3
"""CSR-547 — Latency measurement provenance (CONDITION only · no Actual PnL deduct).

Primary anomaly (user log):
  07:30 mean/p50/p90 = 141.2 / 121.9 / 218.8 ms
  08:00+ mean/p50/p90 = 1.0 / 1.0 / 1.0 ms  → measurement path switch suspect

Store latency_source / latency_measurement_version / latency_valid so
218ms→1ms is never mistaken for performance improvement.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

LATENCY_FIELDS = [
    "latency_ms",
    "latency_risk_flag",
    "latency_source",              # e.g. board_tick_delta | order_ack | synthetic_stub
    "latency_measurement_version", # schema/version tag
    "latency_valid",               # False if stub/placeholder/path switch
]

ANOMALY = {
    "07:30": {"mean": 141.2, "p50": 121.9, "p90": 218.8, "suspect_valid": True},
    "08:00_plus": {"mean": 1.0, "p50": 1.0, "p90": 1.0, "suspect_valid": False, "reason": "path_or_definition_switch"},
}


def normalize_latency_row(
    *,
    latency_ms: Optional[float],
    latency_source: str,
    latency_measurement_version: str,
    latency_valid: Optional[bool] = None,
) -> Dict[str, Any]:
    if latency_valid is None:
        # heuristic: exact 1.0 across report often = stub
        latency_valid = not (
            latency_ms is not None and abs(float(latency_ms) - 1.0) < 1e-9
            and "stub" in latency_source.lower()
        )
    return {
        "latency_ms": latency_ms,
        "latency_risk_flag": bool(latency_ms is not None and float(latency_ms) >= 100.0 and latency_valid),
        "latency_source": latency_source,
        "latency_measurement_version": latency_measurement_version,
        "latency_valid": bool(latency_valid),
        "role": "CONDITION_ONLY",
        "forbidden": "DO_NOT_DEDUCT_FROM_ACTUAL_PNL",
    }


def build_lock() -> Dict[str, Any]:
    return {
        "csr": 547,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Latency provenance audit fields",
        "fields": LATENCY_FIELDS,
        "anomaly_primary": ANOMALY,
        "examples": {
            "valid_path": normalize_latency_row(
                latency_ms=141.2,
                latency_source="board_tick_delta",
                latency_measurement_version="v1_real",
                latency_valid=True,
            ),
            "suspect_1ms": normalize_latency_row(
                latency_ms=1.0,
                latency_source="synthetic_stub",
                latency_measurement_version="unknown",
                latency_valid=False,
            ),
        },
        "policy": {
            "latency": "CONDITION_ONLY",
            "adverse": "ACTUAL_ONCE_IF_VALID",
            "double_count": "FORBIDDEN",
            "p1": "audit measurement path before using in Adverse×Latency×Inventory",
        },
        "gov": {"WIRE": "NO", "LIVE": "OFF", "ENFORCE": 0},
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr547_latency_provenance_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    assert lock["examples"]["suspect_1ms"]["latency_valid"] is False
    print(json.dumps({"ok": True, "path": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
