#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize WOBIMM5 LIVE soak ledger + flatten_last."""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
OUT = AG / "data" / "mm_research" / "wobimm5_live"
LEDGER = OUT / "paired_ledger.jsonl"
FLATTEN = OUT / "flatten_last.json"
STATE = AG / "data" / "wobimm5_live_state.json"
REPORT = OUT / f"soak_report_{time.strftime('%Y%m%d_%H%M%S')}.json"


def load_rows(path: Path):
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main() -> int:
    rows = load_rows(LEDGER)
    n = len(rows)
    pnls = [float(r.get("pnl_bp") or 0.0) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    reasons = Counter(str(r.get("exit_reason") or "") for r in rows)
    # simple max DD on cumulative
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    flatten = {}
    if FLATTEN.exists():
        try:
            flatten = json.loads(FLATTEN.read_text(encoding="utf-8"))
        except Exception:
            pass
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n": n,
        "sum_bp": round(sum(pnls), 2),
        "ev_bp": round(sum(pnls) / n, 4) if n else 0.0,
        "win_rate_pct": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "win_mu_bp": round(sum(wins) / len(wins), 3) if wins else 0.0,
        "loss_mu_bp": round(sum(losses) / len(losses), 3) if losses else 0.0,
        "max_dd_bp": round(max_dd, 2),
        "exit_reasons": dict(reasons),
        "flatten": flatten,
        "state_snippet": {
            k: state.get(k)
            for k in (
                "mode",
                "wire",
                "pnl_bp",
                "total_trades",
                "posts",
                "fills",
                "net_lots",
                "rejects",
                "api_errors",
            )
            if k in state
        },
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"wrote {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
