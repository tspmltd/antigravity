#!/usr/bin/env python3
"""CSR-553 — UMM Component Ablation Plan v1 (OBSERVE · ENFORCE=0).

Hypothesis (unproven):
  Spread Capture has positive Edge; UMM integration may destroy it via
  Inventory lifecycle and/or Exit — separate via incremental lanes on SAME market events.

Lanes A0–A5 (components on/off):
  A0 Spread-only
  A1 Spread + Inventory Skew
  A2 Spread + Current Hold/Exit (no Skew)
  A3 Current UMM Champion (CSR-504 control — NEVER mutate mid-run)
  A4 UMM without Exit intervention (HS failsafe tracked separately)
  A5 Conditional-Unwind UMM (CSR-521 CF only · WIRE=NO · ENFORCE=0)

Phase 1 P0: A0 vs A1 vs A3 only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

CSR = "CSR-553"
WIRE = "NO"
ENFORCE = 0

LANES: Dict[str, Dict[str, Any]] = {
    "A0": {
        "name": "Spread-only",
        "spread_capture": True,
        "inventory_skew": False,
        "continuous_hold": False,
        "current_exit": False,
        "conditional_unwind": False,
        "path": "Spread → Maker Fill → opposite Fill → Close",
        "phase": 1,
    },
    "A1": {
        "name": "Spread + Inventory Skew",
        "spread_capture": True,
        "inventory_skew": True,
        "continuous_hold": False,
        "current_exit": False,
        "conditional_unwind": False,
        "path": "A0 + Skew; minimize Hold/Exit intervention",
        "phase": 1,
        "delta_vs": "A0",
        "delta_meaning": "Inventory Skew incremental value",
    },
    "A2": {
        "name": "Spread + Current Hold/Exit",
        "spread_capture": True,
        "inventory_skew": False,
        "continuous_hold": True,
        "current_exit": True,
        "conditional_unwind": False,
        "path": "Spread + current inventory lifecycle (no Skew)",
        "phase": 2,
    },
    "A3": {
        "name": "Current UMM Champion (CSR-504)",
        "spread_capture": True,
        "inventory_skew": True,
        "continuous_hold": True,
        "current_exit": True,
        "conditional_unwind": False,
        "path": "Spread+Skew+ContinuousQuote+Hold+CurrentExit+HardStop",
        "phase": 1,
        "frozen_control": True,
        "note": "NEVER mutate mid-experiment",
    },
    "A4": {
        "name": "UMM without Exit intervention",
        "spread_capture": True,
        "inventory_skew": True,
        "continuous_hold": True,
        "current_exit": False,
        "conditional_unwind": False,
        "path": "A3 minus mid-path Exit; HS failsafe = separate research item",
        "phase": 2,
        "delta_vs": "A3",
        "delta_meaning": "Current Exit layer incremental impact ≈ A3−A4",
    },
    "A5": {
        "name": "Conditional-Unwind UMM (CSR-521)",
        "spread_capture": True,
        "inventory_skew": True,
        "continuous_hold": True,
        "current_exit": False,
        "conditional_unwind": True,
        "path": "A3 − suspect Exit + Conditional Unwind",
        "phase": 3,
        "mode": "COUNTERFACTUAL_ONLY",
        "wire": "NO",
        "enforce": 0,
        "delta_vs": "A3",
        "delta_meaning": "Conditional Unwind improvement = A5−A3",
    },
}

CONTRASTS = {
    "skew_contribution": "A1 - A0",
    "hold_exit_contribution": "A3 - A1",
    "current_exit_contribution": "A3 - A4",
    "conditional_unwind_improvement": "A5 - A3",
    "note": "incremental experimental contrast — not strict causal attribution without controls",
}

PHASE1_CASES = {
    "case1_integration_breaks": {
        "pattern": "A0+ A1+ A3−",
        "meaning": "Spread/Skew work alone; UMM lifecycle integration destroys Edge",
    },
    "case2_skew_suspect": {
        "pattern": "A0+ A1− A3−",
        "meaning": "Inventory Skew interaction is primary suspect",
    },
    "case3_spread_reaudit": {
        "pattern": "A0−",
        "meaning": "Re-audit pure Spread Edge first",
    },
}

KPI_ECONOMIC = ["net_pnl_bp", "ev_per_trade", "profit_factor", "win_rate", "gross_spread_per_trade"]
KPI_ADVERSE = ["ae_100ms", "ae_1s", "ae_3s", "ae_10s"]
KPI_INVENTORY = ["hold_p50", "hold_p90", "l0_l1_ev", "l2_ev", "l3_l4_ev", "hs_rate", "mae_p90"]
KPI_TAIL = ["avg_trade", "median", "p10", "p5", "p1", "worst_trade", "hs_count"]

LATENCY_BUCKETS = ["≤40ms", "40–80ms", "80–150ms", ">150ms"]
MICRO_REGIMES = [
    "Normal", "Tip Thin", "Cancel Spike", "Fake BO", "High Adverse", "Trend", "Range",
]

EVENT_KEYS = ["market_event_id", "snapshot_id", "timestamp", "side", "reference_mid"]


def empty_lane_kpi() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in KPI_ECONOMIC + KPI_ADVERSE + KPI_INVENTORY:
        out[k] = None
    out["tail"] = {k: None for k in KPI_TAIL}
    return out


def empty_report() -> Dict[str, Any]:
    return {
        "title": "UMM COMPONENT ABLATION",
        "lanes": {lid: {"meta": LANES[lid], "kpi": empty_lane_kpi(), "delta_vs_ref": None}
                  for lid in ("A0", "A1", "A2", "A3", "A4", "A5")},
        "contrasts": {k: None for k in CONTRASTS if k != "note"},
        "component_verdict": {
            "Spread_Edge": "NEED_MORE",
            "Inventory_Skew": "NEED_MORE",
            "Hold_Lifecycle": "NEED_MORE",
            "Current_Exit": "NEED_MORE",
            "Conditional_Unwind": "NEED_MORE",
            "Adverse_sensitivity": None,
            "Tail_compression": None,
        },
        "governance": {"WIRE": "NO", "ENFORCE": 0, "AUTO_APPLY": "OFF", "LIVE": "OFF"},
    }


def build_lock() -> Dict[str, Any]:
    return {
        "csr": 553,
        "parent": "CSR-552",
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "UMM Component Ablation Plan v1 · A0–A5 same-event",
        "hypothesis": (
            "Spread Capture has positive Edge; UMM integration may destroy it via "
            "Inventory lifecycle and/or Exit — UNPROVEN until ablation"
        ),
        "separates": [
            "Spread bad",
            "Inventory Skew bad",
            "Spread × Skew interaction bad",
            "Hold bad",
            "Exit bad",
            "Adverse regime bad",
        ],
        "lanes": LANES,
        "contrasts": CONTRASTS,
        "phase1_p0": ["A0", "A1", "A3"],
        "phase1_cases": PHASE1_CASES,
        "phases": {
            "1": "A0 vs A1 vs A3 → cut Spread / Skew / Integration",
            "2": "A2 + A4 → cut Hold / Exit",
            "3": "A5 → CSR-521 Conditional Unwind CF",
            "4": "Adverse × Latency × Ladder × Regime interaction",
        },
        "same_event_keys": EVENT_KEYS,
        "same_event_rule": "Compare A0–A5 on identical market_event_id — never cross-regime naive compare",
        "kpi": {
            "economic": KPI_ECONOMIC,
            "adverse": KPI_ADVERSE,
            "inventory": KPI_INVENTORY,
            "tail": KPI_TAIL,
        },
        "latency": {
            "role": "CONDITION_ONLY",
            "buckets": LATENCY_BUCKETS,
            "forbid": "DEDUCT_FROM_ACTUAL_PNL",
        },
        "micro_regimes": MICRO_REGIMES,
        "report_template": empty_report(),
        "goal": (
            "Find the single breakage boundary A0→A1→A2/A3→A4→A5 "
            "before 'improving UMM' — swap only the failing component"
        ),
        "gov": {"WIRE": WIRE, "ENFORCE": ENFORCE, "AUTO_APPLY": "OFF", "LIVE": "OFF", "A3_FROZEN": True},
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr553_umm_ablation_plan_v1_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    assert lock["lanes"]["A3"]["frozen_control"] is True
    assert lock["phase1_p0"] == ["A0", "A1", "A3"]
    print(json.dumps({"ok": True, "path": str(path), "phase1": lock["phase1_p0"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
