#!/usr/bin/env python3
"""CSR-569 — TF2BP micro LIVE soak DESIGN (PREPARE only · no flag flip).

User GO (2026-10-02): proceed to write LIVE soak design + gates.
Does NOT set ENABLE_REAL_TRADING=true. Does NOT wire PEG. Does NOT auto_apply.

Path: Dry-run FROZEN baseline → Gate checklist → MANUAL GO2 → micro LIVE soak.
Capital protection: ¥6,390 · LIVE currently OFF.
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
        "csr": 569,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "TF2BP micro LIVE soak DESIGN · gates · MANUAL-ONLY",
        "phase": "PREPARE_DESIGN",
        "user_go": "GO → design/gates only (not ENABLE_REAL_TRADING)",
        "status": {
            "ENABLE_REAL_TRADING": False,
            "LIVE": "OFF",
            "WIRE": "NO",
            "ENFORCE": 0,
            "auto_apply": "OFF",
            "Economic_PASS": "NO",
            "PEG_on_LIVE": "FORBIDDEN",
            "Council_on_LIVE": "FORBIDDEN (OBSERVE only)",
            "Adverse_on_LIVE": "FORBIDDEN until CSR-539 integrity PASS",
        },
        "scope": {
            "strategy": "TF2BP_v1 baseline only (CSR-499 pins)",
            "include": [
                "buy_only",
                "tn6_be5 exit",
                "10s bar",
                "thr 0.55 / p2 0.50 (FROZEN — no retune in soak)",
            ],
            "exclude": [
                "TF2BP_PEG_v2",
                "TF2BP_PEG_v3",
                "UMM LIVE",
                "Arena LIVE",
                "Council execution_allowed",
                "auto parameter search",
            ],
        },
        "execution_path_candidate": {
            "preferred": (
                "gapcore scripts/ops/trend_follow_2bp_shadow.py lineage → "
                "explicit micro LIVE runner (new or existing liveapp pin) "
                "with DRY_RUN=0 only after GO2"
            ),
            "antigravity_alt": (
                "run_dryrun_umm_tf2bp_24h path must NOT be flipped wholesale — "
                "UMM shares process; TF2BP-only LIVE process required"
            ),
            "hard_rule": "UMM and PEG must not share the LIVE process with TF2BP soak",
        },
        "size_and_risk_caps": {
            "order_size_btc": 0.001,
            "max_position_btc": 0.001,  # soak tighter than dryrun 0.005
            "max_open_orders": 1,
            "side_mode": "buy_only",
            "daily_loss_stop_jpy": 800,
            "daily_loss_stop_bp": 60,
            "session_loss_stop_bp": 25,
            "max_hold_sec": 1200,
            "hard_kill": [
                "equity < 5000 JPY remaining buffer breach",
                "API / latency_ms p90 > 200 for 10 min",
                "unhandled exception storm",
                "position != flat after flatten command > 60s",
            ],
        },
        "gates_before_GO2_live_flag": {
            "G0_governance": {
                "require": [
                    "ENABLE_REAL_TRADING still false until GO2",
                    "written soak runbook reviewed",
                    "Discord LIVE + Dryrun alert channels tested",
                ],
            },
            "G1_dryrun_stability": {
                "window": "≥7 calendar days continuous dryrun OR ≥5 sessions",
                "primary": "24h rolling mean bp ≥ 0 after cost assumption ≥0.25bp/side",
                "secondary": [
                    "worst 5% trade bp documented",
                    "max losing streak documented",
                    "1h windows: ≤2 consecutive hours < −40bp",
                ],
                "forbid": "promote on single green 24h snapshot",
            },
            "G2_regime_cells": {
                "require": [
                    "scorecard by hour bucket (incl. outside 16–22)",
                    "spread bucket × pnl",
                    "no single cell >70% of profit with rest ≤0 without note",
                ],
            },
            "G3_cost_latency": {
                "require": [
                    "latency-clean subset (≤80ms) still non-negative or explained",
                    "fee/slippage stress: +0.25bp and +0.50bp still not catastrophic",
                ],
            },
            "G4_ops": {
                "require": [
                    "TF2BP-only process (no UMM/PEG)",
                    "flatten script + deadman",
                    "daily loss stop wired and unit-tested",
                    "session.id / run_id in all fills",
                ],
            },
        },
        "soak_plan_after_GO2": {
            "phase_A": {
                "name": "micro LIVE 2h supervised",
                "size_btc": 0.001,
                "max_trades": 20,
                "stop": "session_loss_stop_bp or daily_loss_stop",
                "success": "ops intact · no kill · fills reconciled — NOT Economic PASS",
            },
            "phase_B": {
                "name": "micro LIVE 24h",
                "size_btc": 0.001,
                "compare": "same-window dryrun shadow twin if available",
                "success": "realized vs shadow Δ within agreed band · no kill",
            },
            "phase_C": {
                "name": "scale decision",
                "require": "separate MANUAL GO3 — not automatic",
                "max_next_size_btc": 0.002,
            },
        },
        "acceptance_gate_id": "TF2BP-LIVE-SOAK-01",
        "forbid_now": [
            "ENABLE_REAL_TRADING=true without GO2 checklist PASS",
            "PEG_v3 on LIVE",
            "UMM LIVE piggyback",
            "threshold / exit retune during soak",
            "Economic PASS from dryrun 24h alone",
            "Council final_action as LIVE trigger",
        ],
        "next_actions": [
            "1) Implement TF2BP-only LIVE runner stub (still DRY_RUN=1) with caps + kill",
            "2) Fill G1–G3 scorecard from dryrun lake (DuckDB)",
            "3) User MANUAL GO2 after checklist green",
            "4) Only then flip trading flag for TF2BP-only process",
        ],
        "ops_doc": "docs/ops/tf2bp_micro_live_soak_csr569.md",
        "core": (
            "GO accepted as DESIGN-only. TF2BP micro LIVE soak gates locked; "
            "ENABLE_REAL_TRADING stays false; PEG/UMM/Council excluded; "
            "GO2 required before any real order."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build()
    path = OUT / "2026-10-02_csr569_tf2bp_micro_live_soak_design_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    # also write ops runbook
    ops = ROOT / "docs" / "ops" / "tf2bp_micro_live_soak_csr569.md"
    ops.parent.mkdir(parents=True, exist_ok=True)
    ops.write_text(_ops_md(lock), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "ops": str(ops),
        "ENABLE_REAL_TRADING": False,
        "core": lock["core"],
    }, indent=2, ensure_ascii=False))
    return 0


def _ops_md(lock: dict) -> str:
    caps = lock["size_and_risk_caps"]
    return f"""# TF2BP micro LIVE soak — CSR-569

**Status:** PREPARE / DESIGN only · `ENABLE_REAL_TRADING=false` · LIVE=OFF  
**GO meaning:** design+gates locked · **not** real-order enable  
**GO2 required** before any LIVE flag flip.

## In scope
- TF2BP baseline only (`buy_only`, `tn6_be5`, FROZEN entry pins)
- Caps: size `{caps['order_size_btc']}` BTC · max pos `{caps['max_position_btc']}` BTC
- Daily loss stop `{caps['daily_loss_stop_jpy']}` JPY / `{caps['daily_loss_stop_bp']}` bp
- Session loss stop `{caps['session_loss_stop_bp']}` bp

## Out of scope
- PEG_v2 / PEG_v3
- UMM LIVE (separate process forever for soak)
- Council / Adverse WIRE
- Parameter retune during soak

## Gates before GO2
| ID | Check |
|----|--------|
| G0 | Governance + alerts ready · flag still false |
| G1 | ≥7d dryrun stability (costed) · no promote on 1 snapshot |
| G2 | hour / spread cell scorecard |
| G3 | latency-clean + fee stress |
| G4 | TF2BP-only process · flatten · deadman · run_id |

## Soak phases (after GO2)
1. **A** 2h supervised · max 20 trades · ops success ≠ Economic PASS  
2. **B** 24h micro · shadow twin compare  
3. **C** scale only with **GO3**

## Kill immediately
{chr(10).join('- ' + k for k in caps['hard_kill'])}

## Commands (placeholders — implement before GO2)
```bash
# still dry
# bash scripts/ops/start-tf2bp-micro-live.sh DRY_RUN=1

# only after GO2 checklist PASS + explicit user GO2
# ENABLE_REAL_TRADING=true bash scripts/ops/start-tf2bp-micro-live.sh DRY_RUN=0
```

## Forbidden
- Flip `.env` ENABLE_REAL_TRADING without GO2
- Attach PEG or UMM to the soak process
- Call Economic PASS from design lock alone
"""


if __name__ == "__main__":
    raise SystemExit(main())
