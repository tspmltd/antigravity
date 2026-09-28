#!/usr/bin/env python3
"""CSR-550 UMM Worst-MAE → Ladder → HS tail attribution template (OBSERVE)."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

COLUMNS = [
    "trade_id",
    "strategy",  # expect UMM
    "entry_ts",
    "mae_bp",
    "ae_1s_bp",
    "ae_3s_bp",
    "ladder_at_mae",
    "ladder_path",  # e.g. L0→L1→L2
    "hs_hit",
    "hold_sec_to_mae",
    "adverse_score",
    "adverse_valid",
    "pnl_slope_pre_mae",
    "exit_reason",
    "realized_pnl_bp",
]


def build() -> Dict[str, Any]:
    return {
        "csr": 550,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "UMM Tail Attribution · Worst-MAE path",
        "primary_2200": {
            "worst10_all_umm": True,
            "mae_tail_bp": [-17.0, -16.6, -16.4, -16.1],
            "ae_masked": {"mae_bp": -15.2, "ae1s": 0.3, "ae3s": 0.1},
        },
        "columns": COLUMNS,
        "hypothesis": (
            "Positions can look non-toxic at AE1–3s then inventoriize into MAE/HS. "
            "Catch with Adverse × Ladder × MAE trajectory — not short AE alone."
        ),
        "rows": [],  # fill via trade-id join later
        "gov": {"WIRE": "NO", "ENFORCE": 0, "LIVE": "OFF"},
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    doc = build()
    path = OUT / "2026-09-28_csr550_umm_tail_attribution_template.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
