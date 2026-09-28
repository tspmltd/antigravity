#!/usr/bin/env python3
"""CSR-552 — Arena Shadow Execution (OBSERVE · ENFORCE=0 · WIRE=NO).

Thesis:
  Arena MM = Pricing / Filtering control group (bar mean-reversion)
  UMM      = Execution / Inventory Lifecycle under test
  Never equate Arena +280bp with "UMM should earn that".

On Arena Entry fire (SpreadCapture / InventorySkew / MicroSpread):
  log signal context + AE hooks + CF inventory slots (filled async).

Branch:
  Arena signal + UMM lifecycle still +  → transplant Arena Entry/Pricing filters
  Arena signal + UMM lifecycle dies L2+ → Exit/Unwind is common bottleneck
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
SHADOW_LOG = ROOT / "data" / "mm_research" / "arena_shadow_execution.jsonl"
JST = timezone(timedelta(hours=9))

CSR = "CSR-552"
SCHEMA_VERSION = "csr552_arena_shadow_v1"
WIRE = "NO"
ENFORCE = 0

SHADOW_STRATEGIES = (
    "SpreadCaptureMM",
    "InventorySkewMM",
    "MicroSpreadMM",
)

SHADOW_ENTRY_SCHEMA: List[str] = [
    "schema_version",
    "shadow_id",
    "signal_ts",
    "strategy",
    "strat_id",
    "side",
    "quote_price",
    "mid",
    "spread_bp",
    "reason",
    # Adverse observe (filled when AE tracker resolves / join)
    "ae_1s_bp",
    "ae_3s_bp",
    "ae_10s_bp",
    "ae_status",  # NULL_PENDING | VALID | INVALID
    # Counterfactual inventory under UMM-like lifecycle (research only)
    "cf_fill",
    "cf_inventory_10s_bp",
    "cf_inventory_30s_bp",
    "cf_inventory_60s_bp",
    "cf_mae_bp",
    "cf_ladder_max",
    "cf_hs_hit",
    "cf_status",  # NULL_PENDING | VALID | INVALID
    "wire",
    "enforce",
]

COMPARE_METRICS = [
    "gross_spread_per_trade",
    "ae_1s",
    "ae_3s",
    "l0_l1_ev",
    "l2plus_ev",
    "hs_rate",
    "p1_tail",
    "p5_tail",
    "net_ev_per_trade",
]

ABLATION_ARMS = [
    "Spread_only",
    "InventorySkew_only",
    "Spread_plus_InventorySkew",
    "Spread_plus_InventorySkew_plus_CurrentExit",
]

BENCHMARK = {
    "SpreadCaptureMM": {
        "id": "strat_cbcd5aed",
        "floor_bp": 2.5,
        "atr_mult": 1.2,
        "vol_filter": "ATR<=base*1.3",
        "trend_block": None,
        "skew": None,
        "lifecycle": "bar_exit_only",
    },
    "InventorySkewMM": {
        "id": "strat_de08146e",
        "floor_bp": 2.5,
        "atr_mult": 1.2,
        "vol_filter": None,
        "trend_block": "100EMA_anti_fade",
        "skew": "trend_slope*0.3*30",
        "lifecycle": "bar_exit_only",
    },
    "MicroSpreadMM": {
        "id": "strat_4d3f2c9f",
        "floor_bp": 2.0,
        "atr_mult": 1.2,
        "vol_filter": "ATR<=base*1.3",
        "trend_block": "100EMA_anti_fade",
        "skew": None,
        "lifecycle": "bar_exit_only",
    },
    "UMM": {
        "id": "CSR-504",
        "floor_bp": "measured/1.2bp_dynamic",
        "atr_mult": "dynamic_quote",
        "vol_filter": "other",
        "trend_block": "MetaFusion/4AGENT",
        "skew": "inventory_skew_0.15",
        "lifecycle": "L0-L4 + Adverse + HS-5bp",
    },
}

_lock = threading.Lock()
_writer_ready = False


def _ensure_log() -> None:
    global _writer_ready
    if _writer_ready:
        return
    SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
    _writer_ready = True


def is_shadow_strategy(name: str) -> bool:
    return str(name or "") in SHADOW_STRATEGIES


def on_arena_entry(
    *,
    strategy: str,
    strat_id: str,
    side: str,
    quote_price: float,
    mid: Optional[float] = None,
    spread: Optional[float] = None,
    reason: str = "",
    signal_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """OBSERVE-only: append shadow row. Never mutates strategy / never LIVE."""
    if not is_shadow_strategy(strategy):
        return None
    ts = float(signal_ts if signal_ts is not None else time.time())
    mid_v = float(mid) if mid and mid > 0 else float(quote_price)
    spread_bp = None
    if spread is not None and quote_price > 0:
        spread_bp = round((float(spread) / float(quote_price)) * 10000.0, 4)
    row = {
        "schema_version": SCHEMA_VERSION,
        "shadow_id": f"{strat_id}_{int(ts * 1000)}_{side}",
        "signal_ts": ts,
        "strategy": strategy,
        "strat_id": strat_id,
        "side": side,
        "quote_price": float(quote_price),
        "mid": mid_v,
        "spread_bp": spread_bp,
        "reason": reason,
        "ae_1s_bp": None,
        "ae_3s_bp": None,
        "ae_10s_bp": None,
        "ae_status": "NULL_PENDING",
        "cf_fill": None,
        "cf_inventory_10s_bp": None,
        "cf_inventory_30s_bp": None,
        "cf_inventory_60s_bp": None,
        "cf_mae_bp": None,
        "cf_ladder_max": None,
        "cf_hs_hit": None,
        "cf_status": "NULL_PENDING",
        "wire": WIRE,
        "enforce": ENFORCE,
    }
    try:
        _ensure_log()
        with _lock:
            with SHADOW_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        return row  # never raise into arena
    return row


def empty_compare_table() -> List[Dict[str, Any]]:
    rows = []
    for name in ("UMM", "SpreadCaptureMM", "InventorySkewMM"):
        rows.append({"component": name, **{m: None for m in COMPARE_METRICS}})
    return rows


def empty_ablation() -> Dict[str, Any]:
    return {
        "arms": ABLATION_ARMS,
        "interpretation": {
            "exit_culprit": "Spread+ / Skew+ / Combined+ / Combined+Exit −",
            "interaction_culprit": "Spread+ / Skew+ / Combined −",
        },
        "results": {a: {"pnl_bp": None, "status": "NULL_PENDING"} for a in ABLATION_ARMS},
    }


def empty_umm_attribution() -> Dict[str, Any]:
    return {
        "title": "UMM Economic Attribution",
        "actual": {
            "gross_spread_capture_bp": None,
            "post_fill_adverse_bp": None,
            "inventory_holding_bp": None,
            "hard_stop_exit_bp": None,
            "residual_bp": None,
            "realized_umm_pnl_bp": None,
            "identity_ok": None,
        },
        "condition": {
            "latency_p50_ms": None,
            "latency_p90_ms": None,
            "high_latency_rate_pct": None,
            "regime": None,
            "note": "Latency NEVER deducted from Actual",
        },
        "inventory": {
            "l0_l1_ev": None,
            "l2_l3_l4_ev": None,
            "hs_rate_pct": None,
        },
        "counterfactual": {
            "conditional_unwind_delta_bp": None,
            "blanket_30s_delta_bp": None,
            "maker_exit_cf_delta_bp": None,
            "note": "CF research only — never add to Actual",
        },
        "governance": {"WIRE": "NO", "ENFORCE": 0, "AUTO_APPLY": "OFF", "LIVE": "OFF"},
    }


def build_lock() -> Dict[str, Any]:
    return {
        "csr": 552,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Arena Shadow Execution · Pricing vs Inventory Lifecycle separation",
        "thesis": {
            "arena": "Pricing / Filtering control group",
            "umm": "Execution / Inventory Lifecycle under test",
            "forbidden_conflation": [
                "Arena +280bp ⇒ UMM should earn +280",
                "UMM loss ⇒ Arena Pricing invalid",
                "Arena win ⇒ UMM inventory management correct",
            ],
        },
        "benchmark": BENCHMARK,
        "mm_research_stage": (
            "Not 'how to fill more' — identify which Inventory to kill "
            "while keeping Spread Edge"
        ),
        "flow": [
            "Spread Capture",
            "Maker Fill",
            "L0/L1 natural resolve → KEEP",
            "L2/L3/L4 Inventory Risk",
            "MAE × Adverse × Micro deterioration",
            "Conditional Unwind → Maker failsafe / Taker failsafe",
        ],
        "shadow_entry_schema": SHADOW_ENTRY_SCHEMA,
        "shadow_log": str(SHADOW_LOG),
        "branch_test": {
            "A_filter_wins": "Arena signal + UMM lifecycle still + → transplant Arena filters",
            "B_exit_bottleneck": "Arena signal + UMM lifecycle dies L2+ → Exit/Unwind common bottleneck",
        },
        "compare_metrics": COMPARE_METRICS,
        "compare_table_template": empty_compare_table(),
        "priorities": {
            "P0": "CSR-521 Inventory Hazard Model P(HS|Ladder,MAE,Adverse,Micro,Regime) — not 30s timer",
            "P1": "Adverse × Latency(Condition) × Inventory attribution",
            "P2": "Microstructure quote-risk analysis",
            "P3": "UMM Component Ablation (Spread / Skew / Combined / +Exit)",
        },
        "ablation": empty_ablation(),
        "umm_economic_attribution_template": empty_umm_attribution(),
        "csr521": {
            "role": "Inventory Hazard Model",
            "not": "Exit timer / Blanket 30s",
            "policy": "L2+ × MAE traj × Adverse/Micro deteriorate → Conditional Unwind CF",
        },
        "latency": "CONDITION_ONLY",
        "actual_identity": [
            "Gross Spread", "+ Adverse Markout", "+ Inventory/Holding",
            "+ Exit/HS", "+ Residual", "= Realized PnL",
        ],
        "gov": {
            "WIRE": "NO",
            "LIVE": "OFF",
            "ENFORCE": 0,
            "AUTO_APPLY": "OFF",
            "FROZEN": "YES",
            "param_change": False,
        },
        "core": (
            "Separate Spread Edge PASS from Inventory Edge PASS. "
            "Arena Shadow measures whether Pricing filters are clean or Exit is the killer."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr552_arena_shadow_execution_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    # smoke write one dry row then not required
    assert is_shadow_strategy("SpreadCaptureMM")
    assert not is_shadow_strategy("UMM")
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "shadow_log": str(SHADOW_LOG),
        "strategies": list(SHADOW_STRATEGIES),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
