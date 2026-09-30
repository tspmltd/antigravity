#!/usr/bin/env python3
"""CSR-559 — 成績改善PDCA2 アーキテクチャ & 実験検証計画ロック.

Primary = user architecture brief (2026-09-29) + prior gate-order lock.
M365: no extra matching source.

PDCA2 ≠ PDCA1:
  PDCA1 = single-param / blanket rules (e.g. fixed 30s exit, AgingGuard friction death)
  PDCA2 = closed-loop: realized_pnl teacher → DuckDB/Parquet join → orthogonal
           Gross−Adverse−Inventory−Exit attribution → conditional weight/control update

Status:
  Plan = LOCKED (this CSR)
  Do   = NOT_STARTED
  Check/Act = N/A until Do opens after gate sequence

Governance (frozen until explicit GO):
  NO CODE CHANGE / NO PARAM CHANGE / WIRE=NO / ENFORCE=0 / LIVE=OFF / auto_apply=OFF
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

# --- Identity (Check constraint) ---
PNL_IDENTITY = (
    "Realized_PnL = Gross_Spread - Adverse_AE - Inventory_Holding_Loss - Exit_Friction"
)

STATE_SLICE = (
    "Regime(Trend/Range/High-Vol) × Microstructure(Imbalance/Taker/Tip-thin) × News/Alpha"
)

# --- Ledgers (Do) ---
LEDGERS: List[Dict[str, str]] = [
    {
        "name": "orderbook_micro",
        "content": "BBO · depth · taker flow · cancel rate · tip thin",
        "sink": "Parquet → DuckDB",
    },
    {
        "name": "fusion_log",
        "ref": "CSR-538 / CSR-558",
        "content": "candidate · veto · veto_effective · decision · final_action (separated)",
        "sink": "Parquet → DuckDB",
    },
    {
        "name": "execution_log",
        "content": "fill px · hold age · exit_reason · realized_pnl_bp · trade_id",
        "sink": "Parquet → DuckDB",
    },
]

# --- Responsibility split (Plan) ---
RESPONSIBILITIES = {
    "Entry_Filter_Alpha": {
        "owner": "Council / Micro Veto / Weights",
        "inputs": ["trend direction", "board structure", "cancel_spike_no_taker", "fake_breakout"],
        "update": "Veto/Weight optimization · auto_apply=OFF · human promote only",
    },
    "Hazard_Exit": {
        "owner": "CSR-521",
        "inputs": ["Hold Ladder age", "MAE", "Adverse (valid only)"],
        "update": "conditional threshold · ENFORCE=0 · Economic Net EV max (not Accuracy)",
        "net_ev": "HS_avoid_benefit - good_trade_cut_cost - exit_friction",
    },
}

# --- Gate / roadmap (strict order; Do not open before P0-a) ---
GATES: List[Dict[str, Any]] = [
    {
        "id": "P0-a",
        "name": "Simulator parity harness",
        "priority": "P0",
        "status": "PENDING",
        "purpose": (
            "Unify Arena MM (LTP-touch / favorable tip rewrite) vs UMM "
            "(book pierce / strict tip) under one harness: same WS board+trades, "
            "same maker_fill, same TP/SL accounting — kill apparent PnL skew"
        ),
        "exit": "parity report PASS or quantified residual bound",
        "blocks": ["P0-b", "P1-a", "P1-b", "P2"],
    },
    {
        "id": "P0-b",
        "name": "CSR-553 Phase1 Ablation A0/A1/A3",
        "priority": "P0",
        "status": "PENDING",
        "depends_on": ["P0-a"],
        "purpose": (
            "Same-event replay: A0 Spread-only · A1 InventorySkew-only · A3 Combined; "
            "A3 FROZEN; isolate component interaction"
        ),
        "exit": "A0/A1/A3 scorecard filled · no ENFORCE",
        "ref": "CSR-553",
        "blocks": ["P1-a", "P2"],
    },
    {
        "id": "P1-a",
        "name": "PnL Attribution identity closure",
        "priority": "P1",
        "status": "PENDING",
        "depends_on": ["P0-a", "P0-b"],
        "purpose": (
            "Deploy mm_trade_attribution_log join · trade_id Mid-path+Markout · "
            f"enforce {PNL_IDENTITY} with residual→0"
        ),
        "exit": "identity_ok=true on locked cohort",
        "ref": "CSR-552 / CSR-554 / umm_economic_attribution_template",
        "blocks": ["P2"],
    },
    {
        "id": "P1-b",
        "name": "latency_ms pipeline restore",
        "priority": "P1",
        "status": "PENDING",
        "depends_on": ["P0-a"],
        "purpose": "Fix 0.0ms fixed measurement · enable latency condition slice (Condition only, not Edge)",
        "exit": "latency_ms distribution non-degenerate · provenance fields present",
        "ref": "CSR-547 Latency provenance",
        "note": "may run in parallel with P1-a after P0-a; must not precede P0-a",
    },
    {
        "id": "P2",
        "name": "CSR-521 re-score (Act candidate)",
        "priority": "P2",
        "status": "PENDING",
        "depends_on": ["P0-a", "P0-b", "P1-a"],
        "purpose": (
            "Reject blanket time-cut · calibrate hazard for max Net EV; "
            "Early Toxicity 5–15s + Deep Inventory L2+; OOS multi-regime"
        ),
        "exit": "re-score report · Economic PASS still NO without Librarian/Council human promote",
        "ref": "CSR-521 / CSR-555 / CSR-556",
    },
]

HOLD_LADDER = {
    "step_sec": 5,
    "protect": "L0 0–15s profit mass",
    "cliffs": ["L1 15–30s loss surge", "L2+ deep inventory"],
    "note": "L0/L1-good model BROKEN (CSR-556) — ladder is descriptive, not edge proof",
}

ACT_PROMOTION = {
    "require": [
        "OOS multi-regime improvement",
        "Librarian RESEARCH conclusion",
        "Council / human approval",
    ],
    "examples": ["PEG_v4 (currently REJECT path)", "UMM v2"],
    "forbidden_auto": ["auto_apply", "ENFORCE=1 without GO", "WIRE=YES"],
}


def build_lock() -> Dict[str, Any]:
    return {
        "csr": 559,
        "parent": [
            "CSR-538", "CSR-552", "CSR-553", "CSR-554", "CSR-555",
            "CSR-556", "CSR-558", "CSR-521",
        ],
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "成績改善PDCA2 · closed-loop architecture · Do NOT STARTED",
        "primary_source": "user_pdca2_architecture_brief",
        "m365_corroboration": False,
        "essence": {
            "PDCA1": "single-param / blanket rules (fixed 30s, AgingGuard friction death)",
            "PDCA2": (
                "closed-loop feedback: realized_pnl teacher → DuckDB/Parquet CF lake → "
                "4-axis orthogonal attribution → conditional Entry/Filter + Hazard Exit update"
            ),
            "teacher": "realized_pnl (+ forward mid for Council layers; never final_action alone)",
        },
        "architecture": {
            "inputs": [
                "OrderbookMicroSnapshot",
                "NewsAgent Impact/Alpha",
                "ExecutionLog",
            ],
            "fusion": "FusionDecisionLog (async post-join)",
            "lake": "DuckDB counterfactual analysis lake",
            "attribution_axes": ["Gross", "Adverse", "Inventory", "Exit"],
            "identity": PNL_IDENTITY,
            "downstream": {
                "Entry_Filter": "Veto/Weight opt · auto_apply=OFF",
                "Hazard_Exit": "CSR-521 conditional · ENFORCE=0",
            },
        },
        "plan": {
            "state_slice": STATE_SLICE,
            "responsibilities": RESPONSIBILITIES,
        },
        "do": {
            "status": "NOT_STARTED",
            "ledgers": LEDGERS,
            "safety": {
                "WIRE": "NO",
                "ENFORCE": 0,
                "LIVE": "OFF",
                "auto_apply": "OFF",
                "NO_CODE_CHANGE": True,
                "NO_PARAM_CHANGE": True,
            },
        },
        "check": {
            "simulator_parity": "Arena vs UMM harness before any strategy ranking",
            "pnl_identity": PNL_IDENTITY,
            "residual_target": 0,
            "join_key": "trade_id",
            "hold_ladder": HOLD_LADDER,
            "forbidden": [
                "sim_delta_as_strategy_edge",
                "SHADOW_OFF adverse_score=0 as safe",
                "final_action as direction teacher",
                "redundant veto in Veto CF (CSR-558)",
            ],
        },
        "act": {
            "csr521_calibration": RESPONSIBILITIES["Hazard_Exit"],
            "promotion": ACT_PROMOTION,
            "status": "CLOSED until P2 gate + human GO",
        },
        "gate_order": [g["id"] for g in GATES],
        "gates": GATES,
        "roadmap_summary": [
            "P0-a Simulator parity harness",
            "P0-b CSR-553 A0/A1/A3 ablation replay",
            "P1-a mm_trade_attribution_log identity closure",
            "P1-b latency_ms pipeline restore",
            "P2 CSR-521 re-score (Act candidate)",
        ],
        "pdca2": {
            "Plan": "LOCKED",
            "Do": "NOT_STARTED",
            "Check": "N/A",
            "Act": "N/A",
            "note": "Do opens only when P0-a work starts under same governance; this CSR does not open Do",
        },
        "governance": {
            "NO_CODE_CHANGE": True,
            "NO_PARAM_CHANGE": True,
            "WIRE": "NO",
            "ENFORCE": 0,
            "AUTO_APPLY": "OFF",
            "LIVE": "OFF",
            "FROZEN": True,
        },
        "status": {
            "PDCA2_Do": "NOT_STARTED",
            "next_gate": "P0-a Simulator parity harness",
            "CSR559": "LOCKED",
            "WIRE": "NO",
            "ENFORCE": 0,
        },
        "core": (
            "PDCA2 is closed-loop orthogonal attribution, not blanket param churn. "
            "Do has not started. Gate order absolute: parity → ablation → attribution "
            "(+ latency) → CSR-521 re-score. Governance freeze holds."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-29_csr559_pdca2_architecture_experiment_plan_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    # also keep gate-order alias path for prior reference
    alias = OUT / "2026-09-29_csr559_pdca2_gate_order_do_not_started_lock.json"
    alias.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    assert lock["pdca2"]["Do"] == "NOT_STARTED"
    assert lock["gate_order"] == ["P0-a", "P0-b", "P1-a", "P1-b", "P2"]
    assert lock["governance"]["WIRE"] == "NO"
    assert "Gross" in lock["architecture"]["attribution_axes"]
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "Do": lock["pdca2"]["Do"],
        "gates": lock["gate_order"],
        "next": lock["status"]["next_gate"],
        "identity": lock["architecture"]["identity"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
