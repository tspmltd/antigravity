#!/usr/bin/env python3
"""CSR-577 — Exec-node overload hypothesis + slim architecture lock.

Hypothesis: dry-run/analysis co-residence on the execution node (not raw
2 vCPU shortage) drives 120s heartbeat stalls. 2026-10-04 04:15 3-engine
stop was planned exchange-break (CSR-570); dig pre-break stall chain.
P0 fix: restore 1 WS → 2 Agents → Fusion → async Parquet on exec node.
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
        "csr": 577,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Exec-node overload hypothesis · slim to 1WS→2Agents→Fusion→Parquet",
        "hypothesis": {
            "primary": "dryrun_analysis_coresidence_on_exec_node",
            "secondary_deferred": "raw_2vcpu_capacity_shortage",
        },
        "probe_2026_10_04_0410_0416": {
            "three_engine_stop": "PLANNED_exchange_break_CSR570",
            "oom": False,
            "journal_err": False,
            "psi_at_0400": 8.70,
            "sar_0410_idle_pct": 84.99,
            "sar_0410_ldavg1": 0.26,
            "resume_pids": {
                "run_pipeline": 1404656,
                "umm_tf2bp": 1404666,
                "arena": 1404671,
            },
            "pre_break": [
                "UMM dryrun stall restarts",
                "Fusion pipeline stall restart",
                "Arena stall restart",
                "JP Pod stall 122s → pid 1358112",
            ],
            "pidstat_1s": "UNAVAILABLE_sysstat_10min_only",
        },
        "p0_slim_exec": [
            "1x WebSocket",
            "MicrostructureAgent",
            "TrendFollowAgent",
            "SignalFusionEngine",
            "async ParquetBatchLogger",
        ],
        "off_exec_node": [
            "12strat_arena_dryrun",
            "umm_tf2bp_24h_dryrun",
            "duckdb_heavy",
            "librarian_search",
        ],
        "nice_policy": {"exec": -10, "research": 15},
        "jp_off_hours": "interval_10s_to_60s_or_sleep",
        "scale_up": "recommend_4vcpu_after_P0_slim",
        "governance": {
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "unchanged",
            "Economic_PASS": "NO",
        },
        "ops_doc": "docs/ops/exec_node_overload_slim_arch_csr577.md",
        "core": (
            "04:15 was planned stop. Pre-break 120s stalls + nice0 dryrun/JP "
            "co-residence support exec-node overload hypothesis. Slim first."
        ),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = build()
    path = OUT / "2026-10-04_csr577_exec_node_overload_slim_arch_lock.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
