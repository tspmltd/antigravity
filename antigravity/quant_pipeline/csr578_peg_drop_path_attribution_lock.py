#!/usr/bin/env python3
"""CSR-578 — PEG drop-path attribution lock (NO_ENTRY / NO_FILL / AGING_EXIT).

Core: where PEG drops Baseline profit — not generic "why we lose".
Lake: orderbook_micro / fusion_log / execution_log + join keys
candidate_id, order_posted, filled, exit_reason, run_id.
WIRE=NO · ENFORCE=0 · PEG_v4 forbidden · no new alpha.
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
        "csr": 578,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "PEG drop-path attribution · NO_ENTRY / NO_FILL / AGING_EXIT vs Baseline",
        "core_question": (
            "Where does PEG drop Baseline-captured profit — "
            "NO_ENTRY, NO_FILL, or AGING_EXIT?"
        ),
        "decision_target": [
            "keep_AE_improvement_worth",
            "or_terminate_PEG_family",
        ],
        "drop_paths": {
            "NO_ENTRY": "Baseline candidate exists · PEG skips before post",
            "NO_FILL": "PEG order_posted · not filled",
            "AGING_EXIT": "Filled · AgingGuard exit underperforms Baseline exit",
        },
        "lake": [
            "orderbook_micro",
            "fusion_log",
            "execution_log",
            "realized_pnl_teacher_join",
        ],
        "join_keys": [
            "run_id",
            "candidate_id",
            "order_posted",
            "filled",
            "exit_reason",
            "strategy_version",
            "config_hash",
            "started_at",
        ],
        "probe_exec_audit_2026_10_04": {
            "source": "data/mm_research/exec_audit_peg_v3.jsonl",
            "n": 2711,
            "fill_FILLED": 2707,
            "fill_SKIP_FAKE_LIQUIDITY": 4,
            "exit_AGING_GUARD": 1291,
            "gap": "NO_ENTRY/NO_FILL mostly missing — need candidate-aligned logging",
        },
        "extends": "CSR-575",
        "forbid": [
            "PEG_v4",
            "new_alpha",
            "thr_retune",
            "PASS_from_fire_count",
            "cross_run_sum",
        ],
        "governance": {
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
            "AUTO_APPLY": "OFF",
            "Economic_PASS": "NO",
            "PEG_Promotion": "REJECT",
        },
        "ops_doc": "docs/ops/peg_drop_path_attribution_csr578.md",
        "core": (
            "Add candidate_id/order_posted/filled/exit_reason/run_id to lake JOIN. "
            "Attribute Baseline profit drop to NO_ENTRY vs NO_FILL vs AGING_EXIT."
        ),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = build()
    path = OUT / "2026-10-04_csr578_peg_drop_path_attribution_lock.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
