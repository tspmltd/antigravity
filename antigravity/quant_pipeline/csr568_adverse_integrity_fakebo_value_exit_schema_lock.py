#!/usr/bin/env python3
"""CSR-568 — Adverse PROMISING+INTEGRITY BLOCK · FakeBO value test · EXIT schema.

User add-on (2026-10-02; M365 no match for this window — Discord/user logs primary):
  - CSR-558 veto_effective separation PASS (effective vs redundant)
  - FakeBO fires often — measure counterfactual value, not fire-rate / thr retune
  - Adverse Low vs High EV separation looks strong BUT integrity_hold_csr539
    → Economic PASS=NO · WIRE=NO until score provenance repaired
  - 05:00 score collapse to 20–40 bucket = integrity evidence
  - UMM dominates worst MAE lists — adverse×inventory attribution
  - Spread Gate: rejected-EV not pass-rate KPI
  - Inventory @30s still mixed (04/05 worsen) — state-conditioned
  - Propose position_state / candidate_intent / decision_intent (schema PREPARE)

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
        "csr": 568,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Adverse integrity P0 · FakeBO veto value · EXIT semantics · CTRL state",
        "phase": "OBSERVE_RESEARCH",
        "data_source": {
            "primary": "user Discord 4AGENT/Adverse/SpreadGate/CTRL logs",
            "m365": "no matching add-on for this window",
            "architecture": "fake_bo/latency + FusionDecisionLog → realized_pnl DuckDB",
        },
        "formal_verdict": {
            "CSR558_ACTION_SEPARATION": "PASS",
            "FINAL_ONLY_LABEL_PREVENTION": "PASS",
            "FAKE_BO_VETO": "NEED_VALUE_TEST",
            "ADVERSE_SIGNAL": "PROMISING",
            "ADVERSE_INTEGRITY": "BLOCKED",
            "SPREAD_GATE": "NEED_COUNTERFACTUAL",
            "INVENTORY_30s": "MIXED_PROMISING",
            "UMM": "HIGH_PRIORITY_INVESTIGATE",
            "PEG_v3": "NOT_PROMOTABLE",
            "AUTO_APPLY": "OFF",
            "ENFORCE": 0,
            "WIRE": "NO",
            "LIVE": "OFF",
            "Economic_PASS": "NO",
        },
        "evidence": {
            "veto_effective_csr558": {
                "example_07_45": {
                    "candidate": "SELL",
                    "decision": "HOLD",
                    "veto": True,
                    "veto_effective": False,
                    "tag": "REDUNDANT",
                },
                "rule": "decision already HOLD → FakeBO did not additively block entry",
                "split_metrics": [
                    "veto_triggered",
                    "veto_effective",
                    "counterfactual_pnl",
                ],
                "snapshot_fire_warning": (
                    "01:02–01:57 6/12 FakeBO fires = snapshot rate only — "
                    "NOT '50% of market was FakeBO'"
                ),
                "next_classes": ["effective", "redundant", "false/harmful"],
            },
            "fakebo_hourly_share_pct": {
                "02:00": 41.7,
                "03:00": 36.7,
                "03_closed_window": 21.6,
                "05:00": 27.5,
                "06:00": 30.0,
                "07:00": 40.0,
                "08:00": 43.3,
                "co_occurs_with": "cancel_spike_no_taker high",
                "hypothesis_fork": [
                    "true fake breakout detection",
                    "generic cancel/no-taker labeled as FakeBO",
                ],
                "next": "counterfactual Veto PnL before threshold change",
            },
            "adverse_promising_blocked": {
                "surface": {
                    "low_adv_win_pct": 67,
                    "low_adv_ev_bp": 0.40,
                    "high_adv_win_pct": 23,
                    "high_adv_ev_bp": -1.57,
                    "r": -0.353,
                    "beta_bp_per_point": -0.043,
                },
                "integrity_flags": [
                    "integrity_hold_csr539",
                    "score missing/default30 collapse",
                    "Capture provenance",
                    "Economic PASS=NO",
                ],
                "verdict": (
                    "Economic/statistical PROMISING · Integrity FAIL/PENDING · "
                    "n≈17k does NOT override provenance"
                ),
                "wire": "NO",
            },
            "adverse_integrity_05h_collapse": {
                "02_04_bimodal": ["0-20", "60-80"],
                "05_00": {"bucket_20_40": 1000, "other": 0},
                "08_00_n_trades": 3,
                "audit_fields": [
                    "score_source",
                    "score_version",
                    "source_strategy",
                    "run_id",
                    "defaulted",
                    "missing",
                ],
                "csr539_hold": "VALID — do not pool across hours until audit PASS",
            },
            "umm_worst_trade_alignment": {
                "observation": "UMM frequent in 02–07h worst MAE lists",
                "hypothesis_consistent": "many small wins + few large adverse excursions",
                "not": "root-cause proven",
                "duckdb_next": {
                    "filter": "strategy=UMM",
                    "group_by": [
                        "adverse_bucket",
                        "inventory_age_bucket",
                        "hold_ladder",
                        "side",
                        "spread_bucket",
                    ],
                    "metrics": [
                        "N", "AVG pnl", "median pnl", "win rate",
                        "MAE", "HARD_STOP rate",
                    ],
                },
            },
            "spread_gate_pass_rate_pct": {
                "02": 3.5, "03": 16.5, "04": 11.5, "05": 12.7,
                "06": 21.8, "07": 12.1, "08": 6.6,
                "note": "06 in ideal 20–40% band ≠ best gate; use rejected vs accepted EV",
                "kpi_demotion": "pass_rate is diagnostic only",
                "cf_axes": [
                    "Toxic rejection",
                    "Spread rejection",
                    "entry_halt",
                    "confidence rejection",
                ],
            },
            "inventory_30s_02_08": {
                "hourly_delta_bp": {
                    "02": 1.89, "03": 5.40, "04": -14.24, "05": -45.09,
                    "06": 27.00, "07": 34.67, "08": 59.81,
                },
                "sum_bp": 69.44,
                "mean_bp": 9.92,
                "positive_hours": "5/7",
                "counterexamples": {
                    "05": {"actual": -18.26, "v30": -63.35, "delta": -45.09},
                    "08": {"actual": 2.86, "v30": 62.67, "delta": 59.81},
                },
                "direction": "state-conditioned unwind — not horizon search",
            },
            "ctrl_side_asymmetry": {
                "05": {"long": -39.65, "short": 21.39},
                "06": {"long": -15.04, "short": -0.53},
                "07": {"long": 4.51, "short": -2.13},
                "08": {"long": -3.07, "short": 5.93},
                "verdict": "no stable long/short edge in excerpt",
                "dims": ["inventory_age", "side", "regime", "adverse_state"],
            },
            "action_schema_proposal": {
                "status": "PREPARE_ONLY — no LIVE wire",
                "problem": "candidate=HOLD / decision=EXIT ambiguous when FLAT vs in position",
                "example_warmup_07_35": {"candidate": "HOLD", "decision": "EXIT"},
                "add_fields": [
                    "position_state",  # FLAT / LONG / SHORT
                    "candidate_intent",
                    "decision_intent",  # NO_ENTRY vs EXIT_LONG/EXIT_SHORT
                ],
                "keep": [
                    "final≠teacher",
                    "candidate/decision + realized_pnl",
                    "veto_effective",
                ],
            },
        },
        "pdca": {
            "P0a": {
                "name": "CSR-539 Adverse integrity",
                "work": [
                    "score provenance",
                    "missing/default",
                    "version",
                    "run boundary",
                    "capture calculation",
                ],
                "until_done": "Economic PASS forbidden · WIRE forbidden",
            },
            "P0b": {
                "name": "UMM tail-loss attribution",
                "dims": ["Adverse", "inventory", "spread", "hold age"],
            },
            "P1a": {
                "name": "Veto Effectiveness",
                "classes": ["effective", "redundant", "harmful"],
                "uses": "CSR-558 veto_effective + CF pnl",
            },
            "P1b": {
                "name": "Inventory state-conditioned unwind",
                "forbid": "auto-adopt 30s",
            },
            "P2a": {
                "name": "Spread Gate rejected-EV",
                "metric": "EV_without_gate − EV_with_gate",
            },
            "P2b": {
                "name": "Micro tip SHADOW",
                "note": "pressure≈NONE vs tip events — independent feature",
            },
            "P3_schema": {
                "name": "position_state / intent fields PREPARE",
                "enforce": 0,
            },
        },
        "core": (
            "Biggest: Adverse EV split looks strong but CSR-539 integrity blocks WIRE — "
            "fix provenance first. FakeBO needs effective/redundant/harmful CF value, "
            "not fire-rate. UMM tail + state-conditioned CTRL continue. EXIT schema PREPARE."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build()
    path = OUT / "2026-10-02_csr568_adverse_integrity_fakebo_value_exit_schema_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path), "core": lock["core"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
