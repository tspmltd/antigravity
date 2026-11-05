#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize TF1A flat-tick gate log + join closes for candidate filter-down EV.

Observation only. Does not trade. WIRE=NO.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

AG = Path(__file__).resolve().parents[2]
DEFAULT_GATE = AG / "data" / "mm_research" / "tf1a_wobi_honest_dryrun" / "gate_ticks.jsonl"
DEFAULT_LOG = AG / "logs" / "dryrun_tf1a_wobi_honest.log"
JST = timezone(timedelta(hours=9))

CANDS = (
    "cand_no_against",
    "cand_persist3",
    "cand_persist5",
    "cand_fresh2",
    "cand_fresh5",
    "cand_ab_v1",
)


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _parse_closes(log_path: Path) -> List[Dict[str, Any]]:
    """FILL BUY → TRADE CLOSED pairs with entry features from nearest prior gate row join later."""
    fill_re = re.compile(
        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) JST\].*FILL BUY @"
    )
    close_re = re.compile(
        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) JST\].*TRADE CLOSED: "
        r"PnL=([+-]?\d+(?:\.\d+)?)bp .* Hold=([+-]?\d+(?:\.\d+)?)s Reason=(.*)$"
    )
    if not log_path.exists():
        return []
    open_ts: Optional[datetime] = None
    out: List[Dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = fill_re.search(line)
        if m and open_ts is None:
            open_ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
            continue
        m = close_re.search(line)
        if m and open_ts is not None:
            reason = m.group(4)
            ru = reason.upper()
            if "STOP_LOSS" in ru or "HARD_STOP" in ru:
                tag = "HARD_STOP"
            elif "TRAILING" in ru:
                tag = "TRAILING"
            elif "LOCK" in ru:
                tag = "LOCK"
            else:
                tag = "OTHER"
            out.append(
                {
                    "fill_ts": open_ts.timestamp(),
                    "pnl": float(m.group(2)),
                    "hold": float(m.group(3)),
                    "tag": tag,
                }
            )
            open_ts = None
    return out


def _nearest_gate(gates: List[Dict[str, Any]], fill_ts: float, window_s: float = 5.0) -> Optional[Dict[str, Any]]:
    """Prefer the last gate tick with posted/working_buy at or before fill."""
    best: Optional[Dict[str, Any]] = None
    best_dt = 1e9
    for g in gates:
        ts = float(g.get("ts") or 0.0)
        if ts > fill_ts + 0.5:
            continue
        dt = fill_ts - ts
        if dt > window_s:
            continue
        if g.get("posted") or g.get("working_buy") or g.get("old_ok"):
            if dt < best_dt:
                best = g
                best_dt = dt
    return best


def _ev(rows: List[Dict[str, Any]]) -> str:
    n = len(rows)
    if not n:
        return "n=0"
    pnls = [float(r["pnl"]) for r in rows]
    mean = sum(pnls) / n
    se = statistics.stdev(pnls) / (n**0.5) if n > 1 else 0.0
    hs = sum(1 for r in rows if r["tag"] == "HARD_STOP")
    hit5 = sum(1 for r in rows if r["tag"] in ("LOCK", "TRAILING"))
    return (
        f"n={n:3d} EV={mean:+.2f}bp se={se:.2f} "
        f"HS={hs / n:.0%} hit5={hit5 / n:.0%} sum={sum(pnls):+.1f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="TF1A gate log summary (observation)")
    ap.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    ap.add_argument("--run-log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--since-hours", type=float, default=0.0, help="Only gate ticks newer than N hours")
    args = ap.parse_args()

    gates = _load_jsonl(args.gate)
    if args.since_hours > 0:
        cut = datetime.now(tz=JST).timestamp() - args.since_hours * 3600.0
        gates = [g for g in gates if float(g.get("ts") or 0.0) >= cut]

    n = len(gates)
    print(f"gate_ticks={n} path={args.gate}")
    if not n:
        print("no gate ticks yet — wait for TF1A flat time after restart")
        return 0

    t0 = float(gates[0]["ts"])
    t1 = float(gates[-1]["ts"])
    hours = max(1e-9, (t1 - t0) / 3600.0)
    print(f"span_h={hours:.2f} old_ok={sum(1 for g in gates if g.get('old_ok'))} "
          f"posted={sum(1 for g in gates if g.get('posted'))}")

    print("\n=== candidate coverage (flat ticks) ===")
    for c in CANDS:
        k = sum(1 for g in gates if g.get(c))
        print(f"  {c:16s} {k:6d} ({100.0 * k / n:.1f}%)")

    old = [g for g in gates if g.get("old_ok")]
    if old:
        print("\n=== among old_ok ticks, candidate overlap ===")
        for c in CANDS:
            k = sum(1 for g in old if g.get(c))
            print(f"  {c:16s} {k:6d}/{len(old)} ({100.0 * k / len(old):.1f}%)")
        both = sum(1 for g in old if g.get("cand_ab_v1"))
        only_old = len(old) - both
        print(f"  old_ok but not cand_ab_v1: {only_old} (these are what A/B would skip)")

    closes = _parse_closes(args.run_log)
    # Only closes whose fill is inside gate span
    joined: List[Dict[str, Any]] = []
    for c in closes:
        if c["fill_ts"] < t0 - 1 or c["fill_ts"] > t1 + 60:
            continue
        g = _nearest_gate(gates, c["fill_ts"])
        if not g:
            continue
        row = dict(c)
        row["gate"] = g
        joined.append(row)

    print(f"\n=== closes joined to gate (fill in span) n={len(joined)} ===")
    if not joined:
        print("need fills after gate logging started")
        return 0

    print(f"  ALL joined {_ev(joined)}")
    for c in CANDS:
        keep = [r for r in joined if r["gate"].get(c)]
        drop = [r for r in joined if not r["gate"].get(c)]
        print(f"  keep {c:16s} {_ev(keep)}")
        if drop:
            print(f"  drop {c:16s} {_ev(drop)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
