#!/usr/bin/env python3
"""CSR-564 — OBSERVE collect + State Freshness Acceptance Gate lock.

PDCA reorder (user 2026-10-01):
  P0 State Freshness Acceptance Gate (age / dup / stale snapshot)
  P1 Trend forward-mid COLLECT (logic FROZEN)
  P2 Micro Pressure v2 SHADOW (buy/sell_pressure_v2 · 4-quadrant EV later)
  P3 FakeBO CF COLLECT continue
  P4 Adverse SHADOW_OFF · NULL→0→SAFE forbidden

Architecture stays OBSERVE:
  Market → Freshness Gate → Trend candidate → Micro v2 shadow → FakeBO
  → Decision layer → Adverse OFF → (no execution) → DuckDB research

FusionDecisionLog + forward mid labels = teacher lake; DuckDB is researcher not decider.
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
        "csr": 564,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "OBSERVE collect · State Freshness Acceptance Gate · Trend fwd / Micro v2 Shadow",
        "phase": "OBSERVE_COLLECT",
        "not": [
            "LIVE approach",
            "WIRE / ENFORCE / auto_apply",
            "Trend logic change",
            "decision threshold change",
            "FakeBO redesign",
            "Adverse NULL→0→SAFE",
            "Economic PASS from signal accuracy before freshness gate",
        ],
        "acceptance_gate_p0": {
            "name": "DATA_FRESHNESS_DUP_STALE",
            "checks": [
                "state_age_sec ≤ freshness_max_age_sec (default 120)",
                "freshness_ok on every hourly/council sample in window",
                "teacher_valid (native candidate+decision · not LEGACY · not blackout)",
                "identical signature streak < 3",
                "distinct_sig_n ≥ 2 when n ≥ 3",
            ],
            "module": "quant_pipeline.state_freshness",
            "sql": "sql/csr564_state_freshness_trend_micro_v2.sql",
            "before_gate_forbidden": [
                "signal accuracy PASS/FAIL",
                "forward-mid Economic PASS",
                "FakeBO Economic PASS",
                "confidence calibration adopt",
                "Micro v2 LIVE wire",
            ],
            "teach_blackout": "2026-10-01 03:00–08:00 JST (CSR-563 STALE republish)",
        },
        "pdca": {
            "P0": "State Freshness AUDIT/GATE",
            "P1": "Trend forward-mid COLLECT (strength_raw/clipped · fwd_1s..60s) · logic FROZEN",
            "P2": "buy_pressure_v2 / sell_pressure_v2 SHADOW · 4-quadrant forward EV later",
            "P3": "FakeBO CF COLLECT continue (no re-eval on short window alone)",
            "P4": "Adverse SHADOW_OFF · score=NULL · valid=False · Economic PASS=NO",
        },
        "frozen": {
            "LIVE": "OFF",
            "WIRE": "NO",
            "ENFORCE": 0,
            "auto_apply": "OFF",
            "Trend_logic": "FROZEN",
            "decision_threshold": "FROZEN",
            "FakeBO": "FROZEN",
            "approved_weights": "FROZEN",
        },
        "collect_schema": {
            "trend_forward_mid": [
                "candidate_ts",
                "candidate_side",
                "raw_move",
                "strength_raw",
                "strength_clipped",
                "fwd_1s",
                "fwd_3s",
                "fwd_10s",
                "fwd_30s",
                "fwd_60s",
                "teacher_valid",
                "freshness_ok",
            ],
            "micro_pressure_v2_shadow": [
                "buy_pressure_v2",
                "sell_pressure_v2",
            ],
            "quadrant_ev_later": [
                "Trend BUY × BUY_PRESSURE",
                "Trend BUY × SELL_PRESSURE",
                "Trend SELL × SELL_PRESSURE",
                "Trend SELL × BUY_PRESSURE",
            ],
            "teacher_path": (
                "candidate/decision/veto → FusionDecisionLog (+ council layers) → "
                "forward mid / realized_pnl → DuckDB researcher (not decider)"
            ),
        },
        "architecture": [
            "Market Data",
            "Freshness Gate ← P0",
            "Trend → Candidate (FROZEN logic · COLLECT labels)",
            "Micro Pressure v2 SHADOW (keep v1)",
            "FakeBO (FROZEN · CF COLLECT)",
            "Decision layer",
            "Adverse Gate OFF",
            "Execution eligibility = false (OBSERVE)",
            "Inventory / Exit OBSERVE",
            "DuckDB research lake",
        ],
        "governance": {
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
            "AUTO_APPLY": "OFF",
            "Economic_PASS": "NO",
        },
        "core": (
            "Next Acceptance Gate is data freshness / duplicate / stale-snapshot detection — "
            "not signal accuracy. OBSERVE collect candidate/decision/veto → forward mid; "
            "DuckDB evaluates before any execution wire."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build()
    path = OUT / "2026-10-01_csr564_observe_freshness_acceptance_gate_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path), "core": lock["core"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
