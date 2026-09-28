#!/usr/bin/env python3
"""CSR-543 — UMM vs Arena MM component decomposition template.

Question: why Arena MM parts win while integrated UMM loses?
Does NOT invent Gross/AE/Latency without trade-level join.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
STATE_UMM = ROOT / "data" / "dryrun_umm_tf2bp_state.json"
STATE_ARENA = ROOT / "data" / "dryrun_approved_arena_state.json"
JST = timezone(timedelta(hours=9))

# Primary evidence from user overnight log
PRIMARY = {
    "asof_note": "user_presented_log_primary",
    "umm_24h": {
        "trades": 1628,
        "win_rate_pct": 60.0,
        "pnl_bp": -67.06,
        "spread_capturing": "PROMISING",
        "inventory_management": "FAILING",
        "economic_pass": False,
    },
    "arena_06_34": {
        "SpreadCaptureMM": 93.67,
        "InventorySkewMM": 66.64,
        "MicroSpreadMM": 3.34,
        "top3_simple_sum_bp": 163.65,
        "disclaimer": "simple_sum != Arena total attribution",
    },
}

METRICS = [
    "gross_spread_per_trade",
    "ae_1s",
    "ae_3s",
    "hold_p50",
    "l2plus_rate",
    "hard_stop_rate",
    "pnl_per_trade",
    "tail_p1",
    "tail_p5",
    "win_rate_pct",
    "pnl_bp_24h",
    "n_trades_24h",
]


def _blank_metrics() -> Dict[str, Any]:
    return {m: None for m in METRICS}


def _arena_rows() -> List[Dict[str, Any]]:
    if not STATE_ARENA.exists():
        return []
    st = json.loads(STATE_ARENA.read_text(encoding="utf-8"))
    want = {"SpreadCaptureMM", "InventorySkewMM", "MicroSpreadMM", "GridMM"}
    out = []
    for r in st.get("ranking") or []:
        name = r.get("name")
        if name not in want:
            continue
        s24 = r.get("stats_24h") or {}
        m = _blank_metrics()
        m["pnl_bp_24h"] = s24.get("pnl_bp")
        m["n_trades_24h"] = s24.get("total_trades")
        m["win_rate_pct"] = s24.get("win_rate_pct")
        # hold / HS / AE need join
        out.append({"component": name, "source": "arena_state_rolling", **m})
    return out


def _umm_row() -> Dict[str, Any]:
    m = _blank_metrics()
    m["pnl_bp_24h"] = PRIMARY["umm_24h"]["pnl_bp"]
    m["n_trades_24h"] = PRIMARY["umm_24h"]["trades"]
    m["win_rate_pct"] = PRIMARY["umm_24h"]["win_rate_pct"]
    row = {
        "component": "UMM_integrated",
        "source": "user_presented_log_primary",
        "spread_capturing": "PROMISING",
        "inventory_management": "FAILING",
        "economic_pass": False,
        **m,
    }
    if STATE_UMM.exists():
        st = json.loads(STATE_UMM.read_text(encoding="utf-8"))
        s24 = (st.get("umm") or {}).get("stats_24h") or {}
        row["rolling_secondary_24h"] = {
            "pnl_bp": s24.get("pnl_bp"),
            "trades": s24.get("total_trades"),
            "win_rate_pct": s24.get("win_rate_pct"),
            "note": "rolling state · do not mix with wall-clock primary without continuity audit",
        }
    return row


def build_report() -> Dict[str, Any]:
    arena = _arena_rows()
    return {
        "csr": 543,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "question": "Why do Arena MM components win while integrated UMM loses?",
        "primary": PRIMARY,
        "table": {
            "UMM": _umm_row(),
            "Arena_components": arena,
            "columns_need_join": [
                "gross_spread_per_trade",
                "ae_1s",
                "ae_3s",
                "hold_p50",
                "l2plus_rate",
                "hard_stop_rate",
                "pnl_per_trade",
                "tail_p1",
                "tail_p5",
            ],
        },
        "hypothesis": (
            "MM edge exists (Arena parts). UMM Inventory lifecycle destroys it "
            "(many small wins vs few large losses). P0 = loss-tail compression, not winrate."
        ),
        "rules": {
            "latency_in_actual_pnl": False,
            "cf_added_to_actual": False,
            "wall_clock_vs_process_uptime": "NEVER_MIX",
        },
        "gov": {
            "UMM_NOT_PASS": True,
            "CSR521_PROMISING_OBSERVE": True,
            "Arena_STRONG_RESEARCH": True,
            "LIVE": "OFF",
            "ENFORCE": 0,
        },
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report = build_report()
    path = OUT / "2026-09-28_csr543_umm_vs_arena_decomposition.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "umm_primary_24h_bp": PRIMARY["umm_24h"]["pnl_bp"],
        "arena_components_n": len(report["table"]["Arena_components"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
