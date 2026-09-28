#!/usr/bin/env python3
"""CSR-547 overnight status lock — primary = user log 07:30–22:00."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))


def build() -> dict:
    return {
        "csr": 547,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "primary_source": "user_presented_log_0730_2200",
        "m365_corroboration": False,
        "priorities": {
            "P0": [
                "CSR-521 Hazard/Conditional Unwind — same trade_id CF @10/20/30s",
                "UMM vs Arena MM decomposition (SpreadCapture + InventorySkew)",
            ],
            "P1": [
                "Latency measurement audit + Adverse×Latency×Inventory (no double count)",
                "PEG AgingGuard CF 3/5/10/20/30 + BestMaker",
            ],
            "P2": ["Council candidate/Veto CF"],
        },
        "status": {
            "UMM_CSR504": "Economic FAIL · Inventory lifecycle P0 · WR58%/−309bp → loss-tail not WR",
            "CSR521_10s": "VERY_PROMISING",
            "CSR521_20s": "VERY_PROMISING",
            "CSR521_30s_blanket": "NOT_ROBUST",
            "Conditional_Hazard": "P0",
            "ENFORCE": 0,
            "Arena": "Cumulative STRONG · MM family STRONG · Auto promotion NO",
            "TF2BP": "FROZEN",
            "PEG_v3": "Economic FAIL · Aging dominance STRONG · Promotion REJECT",
            "Adverse": "SHADOW_OFF NULL扱い維持",
            "Latency": "CONDITION ONLY · measurement audit REQUIRED",
            "WIRE": "NO",
            "LIVE": "OFF",
            "AUTO_APPLY": "OFF",
        },
        "core_finding": (
            "CSR-521 is no longer a 'cut at 30s' question. "
            "L1 can already devastate (11:00). 10s CF improved all 16 windows. "
            "P0 = Inventory Hazard Detection starting 10–20s — not L2/L3 Exit."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    doc = build()
    path = OUT / "2026-09-28_csr547_overnight_priority_lock.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
