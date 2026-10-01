#!/usr/bin/env python3
"""CSR-567 — Autonomous Mutation Loop hygiene · EmaTrend PAUSE · PEG/UMM reinforce.

User add-on (2026-10-02, M365 no extra match — Discord/user log primary):
  - Cycle 232: reject 3 / adopt 0 → safety block works; "improvement" unproven
  - EmaTrend mutations worsen or flat on n=4 → RESEARCH_PAUSE
  - PEG_v3 @07:04 Δ−559 vs TF2BP · Aging fires only → REJECT PROMOTION / ablate
  - UMM still largest −EV@WR54% problem → attribution metrics not win-rate
  - Research priority for NEW alpha: post-restart Arena SpreadCapture/MicroSpread
    over further UMM/PEG tuning (UMM remains HIGH PRIORITY problem research)

WIRE=NO · ENFORCE=0 · LIVE=BLOCKED · FROZEN · auto_apply=OFF · Economic PASS=NO
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
        "csr": 567,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "AUTONOMOUS MUTATION/VALIDATION · EmaTrend PAUSE · PEG reject · UMM metrics",
        "phase": "OBSERVE_RESEARCH",
        "data_source": {
            "primary": "user Discord / Arena cycle logs 2026-10-02",
            "m365": "no matching add-on for this latest window",
            "design_compare": "Parquet micro/fusion/execution → DuckDB before LIVE",
        },
        "autonomous_loop": {
            "rename_from": "戦略自律改善",
            "rename_to": "AUTONOMOUS MUTATION / VALIDATION",
            "cycle_232": {
                "SpreadCaptureMM": {"SR": -11.34, "PF": 0.38, "verdict": "REJECT"},
                "GridMM": {"SR": -7.01, "PF": 0.49, "verdict": "REJECT"},
                "EmaTrend": {"SR": -14.33, "PF": 0.00, "verdict": "REJECT"},
                "adopted": 0,
                "rejected": 3,
                "safety_block": "PASS — bad candidates not promoted",
                "improvement_capability": "UNPROVEN",
            },
            "rules": [
                "code rewrite ≠ improvement",
                "IMPROVED only if OOS beats parent version on locked metrics",
                "n too small (e.g. 4 trades) → INSUFFICIENT_EVIDENCE not IMPROVED",
                "never auto_apply / never LIVE from mutation cycle alone",
            ],
            "forbid_label": ["IMPROVED", "最適化成功", "自律改善PASS"] ,
            "allow_label": [
                "MUTATED",
                "VALIDATED_REJECT",
                "VALIDATED_OOS_WIN",
                "INSUFFICIENT_EVIDENCE",
                "RESEARCH_PAUSE",
            ],
        },
        "ematread_pause": {
            "strategy_id": "EmaTrend_strat_aa70771f",
            "status": "RESEARCH_PAUSE",
            "reason": ["INSUFFICIENT_EVIDENCE", "NO_IMPROVEMENT"],
            "mutation_path": {
                "v1_sharpe": -6.76,
                "v2_sharpe": -14.33,
                "v3_sharpe": -14.33,
                "final_eval_trades": 4,
                "tried": ["EMA span change", "100SMA regime filter"],
            },
            "forbid_now": [
                "4th EMA-number search",
                "parameter grid overfitting on tiny n",
            ],
            "resume_requires": [
                "regime decomposition first: trend / range / high_vol / spread_bucket",
                "locate where parent loses before new mutation",
                "OOS beat parent with n≥ gate (not defined as LIVE)",
            ],
        },
        "peg_v3": {
            "promotion": "REJECT",
            "lock_at_07_04": {
                "tf2bp_bp": 134.95,
                "peg_v3_bp": -424.48,
                "delta_bp": -559.43,
                "aging_exits": 9,
                "be_exits": 0,
                "fake_skip": 0,
            },
            "prior_run_aging_peak": 187,
            "prior_run_be_fake": 0,
            "note": "Do not conclude AgingGuard always bad — ablate first",
            "next": [
                "BASE",
                "BASE+Aging",
                "BASE+BE3",
                "BASE+FakeLiq",
            ],
            "mode": "simultaneous Shadow compare preferred",
            "csr_link": 566,
        },
        "umm": {
            "priority": "HIGH — still largest operational research problem",
            "at_07_00": {
                "bp_24h": -644.95,
                "trades": 2263,
                "win_rate_pct": 54,
            },
            "not_solved_by": "win-rate tuning",
            "primary_metrics": [
                "avg_win_bp",
                "avg_loss_bp",
                "p95_loss_bp",
                "hard_stop_bp",
                "inventory_age",
                "inventory_side",
                "MAE",
                "spread_at_entry",
            ],
            "attribution": "CSR-566 A entry / B hold / C HARD_STOP",
            "teacher_path": "execution_log separate Parquet → DuckDB join features",
        },
        "research_priority_board": {
            "new_alpha_validate_first": [
                "SpreadCaptureMM (post-restart Arena)",
                "MicroSpreadMM (post-restart Arena)",
            ],
            "problem_research_high": ["UMM attribution", "PEG Aging ablation"],
            "paused": ["EmaTrend_strat_aa70771f"],
            "rejected_promotion": ["PEG_v3 composite", "Cycle232 mutations"],
            "note": (
                "Arena winners are higher priority for promotion-candidate research; "
                "UMM remains highest priority PROBLEM research — different tracks"
            ),
        },
        "acceptance_gate_mutation": {
            "id": "MUT-OOS-01",
            "name": "Mutation Improvement Gate",
            "require": [
                "parent_version_id + child_version_id",
                "OOS window disjoint from fit/mutation window",
                "child beats parent on primary metric AND no worse worst_5pct",
                "n_oos ≥ configured minimum (default research floor 30 trades unless stated)",
            ],
            "on_fail": "VALIDATED_REJECT or INSUFFICIENT_EVIDENCE — never IMPROVED",
            "on_pass": "VALIDATED_OOS_WIN — still NOT LIVE / NOT auto_apply",
        },
        "governance": {
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "BLOCKED",
            "FROZEN": True,
            "auto_apply": "OFF",
            "Economic_PASS": "NO",
            "Adverse": "SHADOW/OFF",
        },
        "core": (
            "Cycle232 safety PASS / improvement UNPROVEN — call it MUTATION/VALIDATION. "
            "EmaTrend → RESEARCH_PAUSE (n=4, no improve). PEG_v3 PROMOTION REJECT; ablate. "
            "UMM still WR+/−EV problem research. New-alpha track: Arena SpreadCapture/MicroSpread."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build()
    path = OUT / "2026-10-02_csr567_mutation_loop_ematread_pause_peg_umm_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path), "core": lock["core"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
