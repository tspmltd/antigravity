#!/usr/bin/env python3
"""CSR-547 — UMM vs Arena MM head-to-head comparison template (OBSERVE).

Primary snapshot @22:00 (user log):
  UMM 24h −309.21bp / 3855 / WR58% → P0 = loss-tail compression (not WR)
  Arena: SpreadCapture +191.26 · InventorySkew +145.48 · MicroSpread +117.01
  Arena cumulative ~49h→63h display +243.47bp (comparative)

Join metrics (hold/L1/HS/AE/MAE) stay NULL until trade-level provenance.
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

COMPARE_METRICS = [
    "spread_per_trade",
    "hold_p50",
    "l1_rate",
    "l2plus_rate",
    "hs_rate",
    "ae_1s",
    "ae_3s",
    "mae_p90",
    "loss_p5",
    "pnl_per_trade",
    "pnl_bp_24h",
    "n_trades_24h",
    "win_rate_pct",
]

PRIMARY_22H = {
    "asof": "22:00_user_log_primary",
    "umm": {
        "pnl_bp_24h": -309.21,
        "n_trades_24h": 3855,
        "win_rate_pct": 58.0,
        "diagnosis": "Inventory lifecycle / Exit / exposure — NOT MM-family failure",
        "p0": "loss_tail_compression",
        "economic_pass": False,
    },
    "arena_24h": {
        "SpreadCaptureMM": 191.26,
        "InventorySkewMM": 145.48,
        "MicroSpreadMM": 117.01,
    },
    "arena_cumulative_display": {
        "49.05h_bp": 299.27,
        "60.06h_bp": 517.27,
        "61.06h_bp": 554.27,
        "63.06h_bp": 542.74,
        "delta_49_to_63_bp": 243.47,
        "note": "display cumulative · not identical to 24h rolling",
    },
}


def _blank() -> Dict[str, Any]:
    return {m: None for m in COMPARE_METRICS}


def _fill_known(*, pnl: Optional[float], n: Optional[int], wr: Optional[float]) -> Dict[str, Any]:
    r = _blank()
    r["pnl_bp_24h"] = pnl
    r["n_trades_24h"] = n
    r["win_rate_pct"] = wr
    if pnl is not None and n and n > 0:
        r["pnl_per_trade"] = round(float(pnl) / float(n), 4)
    return r


def build_comparison() -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    u = PRIMARY_22H["umm"]
    rows.append({
        "component": "UMM",
        "source": "user_log_primary",
        **_fill_known(pnl=u["pnl_bp_24h"], n=u["n_trades_24h"], wr=u["win_rate_pct"]),
    })
    for name, pnl in PRIMARY_22H["arena_24h"].items():
        rows.append({
            "component": name,
            "source": "user_log_primary",
            **_fill_known(pnl=pnl, n=None, wr=None),
        })

    # optional live state overlay (null-safe)
    live: Dict[str, Any] = {"umm": None, "arena": []}
    try:
        if STATE_UMM.exists():
            st = json.loads(STATE_UMM.read_text(encoding="utf-8"))
            live["umm"] = {
                "pnl_bp_24h": (st.get("stats_24h") or {}).get("pnl_bp"),
                "n_trades": (st.get("stats_24h") or {}).get("total_trades"),
            }
        if STATE_ARENA.exists():
            st = json.loads(STATE_ARENA.read_text(encoding="utf-8"))
            for r in st.get("ranking") or []:
                if r.get("name") in PRIMARY_22H["arena_24h"]:
                    s24 = r.get("stats_24h") or {}
                    live["arena"].append({
                        "name": r.get("name"),
                        "pnl_bp_24h": s24.get("pnl_bp"),
                        "n_trades": s24.get("total_trades"),
                    })
    except Exception as e:
        live["error"] = str(e)

    return {
        "csr": 547,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "UMM vs SpreadCapture + InventorySkew decomposition",
        "primary": PRIMARY_22H,
        "compare_metrics": COMPARE_METRICS,
        "rows": rows,
        "join_pending": [m for m in COMPARE_METRICS if m not in ("pnl_bp_24h", "n_trades_24h", "win_rate_pct", "pnl_per_trade")],
        "live_overlay": live,
        "verdict": {
            "umm": "Economic FAIL · Inventory lifecycle P0",
            "arena_mm_family": "STRONG evidence · Auto promotion NO",
            "next": "fill hold/L1/L2+/HS/AE/MAE/loss_p5 via trade-id join",
        },
        "gov": {"WIRE": "NO", "LIVE": "OFF", "ENFORCE": 0, "AUTO_APPLY": "OFF"},
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    doc = build_comparison()
    path = OUT / "2026-09-28_csr547_umm_vs_arena_compare.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path), "rows": len(doc["rows"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
