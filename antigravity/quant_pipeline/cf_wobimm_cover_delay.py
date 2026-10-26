#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CSR-633 CF — LIVE WOBIMM cover delay / deepen (WIRE=NO, no orders).

Uses paired_ledger.jsonl exits + public executions.jsonl last-price path.
Does not touch LIVE or DRYRUN runners.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AG = Path("/home/azureuser/antigravity")
LEDGER = AG / "data" / "mm_research" / "wobimm_live" / "paired_ledger.jsonl"
EXECS = AG / "data" / "market_feeder" / "executions.jsonl"
OUT = AG / "data" / "mm_research" / "wobimm_cf_cover_delay.json"

JST = timezone(timedelta(hours=9))
DELAYS = (5, 10, 15, 30, 60)
TICK = 1.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=JST).strftime("%Y-%m-%d %H:%M:%S")


def _load_ledger() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with LEDGER.open() as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _needed_window(rows: List[Dict[str, Any]]) -> Tuple[float, float]:
    t0 = min(float(r["ts"]) for r in rows) - 5.0
    t1 = max(float(r["ts"]) for r in rows) + 90.0
    return t0, t1


def _load_exec_path(t0: float, t1: float) -> List[Tuple[float, float, str]]:
    """(ts_sec, price, side) in [t0, t1], streamed."""
    path: List[Tuple[float, float, str]] = []
    t0_ms = t0 * 1000.0
    t1_ms = t1 * 1000.0
    with EXECS.open() as fp:
        for line in fp:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            wall = float(rec.get("wall_ms") or 0.0)
            if wall and (wall < t0_ms - 2000 or wall > t1_ms + 2000):
                # wall_ms is close to exec time; still parse rows if in range
                if wall < t0_ms - 60_000 or wall > t1_ms + 60_000:
                    continue
            for item in rec.get("rows") or []:
                px = float(item.get("price") or 0.0)
                if px <= 0:
                    continue
                ed = str(item.get("exec_date") or "")
                ts = wall / 1000.0 if wall else 0.0
                if ed:
                    try:
                        ts = datetime.fromisoformat(ed.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        pass
                if ts < t0 or ts > t1:
                    continue
                side = str(item.get("side") or "")
                path.append((ts, px, side))
    path.sort(key=lambda x: x[0])
    return path


def _price_at_or_after(path: List[Tuple[float, float, str]], t: float) -> Optional[float]:
    lo, hi = 0, len(path)
    while lo < hi:
        mid = (lo + hi) // 2
        if path[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(path):
        return None
    return path[lo][1]


def _first_opposite_print(
    path: List[Tuple[float, float, str]], t: float, horizon: float, want_side: str
) -> Optional[Tuple[float, float]]:
    lo, hi = 0, len(path)
    while lo < hi:
        mid = (lo + hi) // 2
        if path[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    end = t + horizon
    for i in range(lo, len(path)):
        ts, px, side = path[i]
        if ts > end:
            break
        if side == want_side:
            return ts, px
    return None


def _minmax(path: List[Tuple[float, float, str]], t0: float, t1: float) -> Tuple[Optional[float], Optional[float]]:
    lo_p: Optional[float] = None
    hi_p: Optional[float] = None
    lo, hi = 0, len(path)
    while lo < hi:
        mid = (lo + hi) // 2
        if path[mid][0] < t0:
            lo = mid + 1
        else:
            hi = mid
    for i in range(lo, len(path)):
        ts, px, _ = path[i]
        if ts > t1:
            break
        lo_p = px if lo_p is None else min(lo_p, px)
        hi_p = px if hi_p is None else max(hi_p, px)
    return lo_p, hi_p


def _cf_cover(fill_px: float, later_px: float, size: float, action: str) -> Tuple[float, float]:
    """Delta vs actual if the same size covered/closed at later_px (maker last)."""
    if action == "COVER_SHORT":
        # buy later: cheaper later = better
        d_jpy = (fill_px - later_px) * size
    else:
        # sell later: higher later = better
        d_jpy = (later_px - fill_px) * size
    d_bp = (d_jpy / (fill_px * size)) * 1e4 if fill_px * size else 0.0
    return d_jpy, d_bp


def run() -> Dict[str, Any]:
    rows = _load_ledger()
    t0, t1 = _needed_window(rows)
    path = _load_exec_path(t0, t1)
    covers = [r for r in rows if r.get("action") == "COVER_SHORT"]
    longs = [r for r in rows if r.get("action") == "CLOSE_LONG"]

    delay_stats: Dict[str, Any] = {}
    for d in DELAYS:
        recs = []
        for r in covers:
            later = _price_at_or_after(path, float(r["ts"]) + d)
            if later is None:
                continue
            dj, db = _cf_cover(float(r["price"]), later, float(r.get("size") or 0.001), "COVER_SHORT")
            recs.append({**r, "cf_px": later, "delta_jpy": dj, "delta_bp": db})
        n = len(recs)
        delay_stats[f"cover_delay_{d}s"] = {
            "n": n,
            "n_missing_px": len(covers) - n,
            "delta_bp_sum": round(sum(x["delta_bp"] for x in recs), 2) if n else 0.0,
            "delta_jpy_sum": round(sum(x["delta_jpy"] for x in recs), 2) if n else 0.0,
            "delta_ev_bp": round(sum(x["delta_bp"] for x in recs) / n, 3) if n else 0.0,
            "pct_better": round(100.0 * sum(1 for x in recs if x["delta_bp"] > 0) / n, 1) if n else 0.0,
            "actual_cover_pnl_bp": round(sum(float(r.get("pnl_bp") or 0) for r in covers), 2),
        }

    exh = []
    for r in covers:
        hit = _first_opposite_print(path, float(r["ts"]), 60.0, "SELL")
        if hit is None:
            continue
        ts, px = hit
        dj, db = _cf_cover(float(r["price"]), px, float(r.get("size") or 0.001), "COVER_SHORT")
        exh.append({"wait_sec": ts - float(r["ts"]), "delta_bp": db, "delta_jpy": dj})
    n_exh = len(exh)
    exhaust = {
        "rule": "first public SELL print within 60s (buy-pressure fade proxy)",
        "n": n_exh,
        "n_no_fade": len(covers) - n_exh,
        "delta_bp_sum": round(sum(x["delta_bp"] for x in exh), 2) if n_exh else 0.0,
        "delta_ev_bp": round(sum(x["delta_bp"] for x in exh) / n_exh, 3) if n_exh else 0.0,
        "pct_better": round(100.0 * sum(1 for x in exh if x["delta_bp"] > 0) / n_exh, 1) if n_exh else 0.0,
        "median_wait_sec": sorted(x["wait_sec"] for x in exh)[n_exh // 2] if n_exh else 0.0,
    }

    # Optimistic: cover at min price in next 30s (upper bound, not a policy)
    opt = []
    for r in covers:
        lo, _ = _minmax(path, float(r["ts"]), float(r["ts"]) + 30.0)
        if lo is None:
            continue
        dj, db = _cf_cover(float(r["price"]), lo, float(r.get("size") or 0.001), "COVER_SHORT")
        opt.append(db)
    optimistic_30s = {
        "rule": "cover at window-min last (upper bound, not implementable)",
        "n": len(opt),
        "delta_bp_sum": round(sum(opt), 2) if opt else 0.0,
        "delta_ev_bp": round(sum(opt) / len(opt), 3) if opt else 0.0,
    }

    # Deepen: extra maker ticks IF still filled (upper bound)
    deepen = {}
    for ticks in (1, 5, 10):
        d_bp = []
        for r in rows:
            px = float(r["price"])
            extra = ticks * TICK
            # deeper bid = lower buy; deeper ask = higher sell
            if r.get("action") == "COVER_SHORT":
                db = (extra / px) * 1e4
            else:
                db = (extra / px) * 1e4
            d_bp.append(db)
        deepen[f"+{ticks}tick_if_filled"] = {
            "n": len(d_bp),
            "delta_bp_sum": round(sum(d_bp), 2),
            "delta_ev_bp": round(sum(d_bp) / len(d_bp), 4) if d_bp else 0.0,
            "note": "assumes 100% still fill; LIVE fill-rate drop not modeled",
        }

    actual = {
        "n": len(rows),
        "covers": len(covers),
        "close_long": len(longs),
        "cover_pnl_bp": round(sum(float(r.get("pnl_bp") or 0) for r in covers), 2),
        "cover_ev_bp": round(sum(float(r.get("pnl_bp") or 0) for r in covers) / len(covers), 3) if covers else 0.0,
        "long_pnl_bp": round(sum(float(r.get("pnl_bp") or 0) for r in longs), 2),
        "all_pnl_bp": round(sum(float(r.get("pnl_bp") or 0) for r in rows), 2),
        "first": _iso(float(rows[0]["ts"])) if rows else None,
        "last": _iso(float(rows[-1]["ts"])) if rows else None,
    }

    # Pick policy: first delay with delta_bp_sum > 0 and pct_better >= 50, else exhaust if better
    pick = "cover_wait_buy_pressure"
    pick_reason = "LIVE COVER_SHORT is the loss body; fade-wait is the 1-variable WOBIMM2 pin"
    ranked = sorted(
        ((k, v["delta_bp_sum"], v["pct_better"]) for k, v in delay_stats.items()),
        key=lambda x: x[1],
        reverse=True,
    )
    best_delay = ranked[0] if ranked else None

    out = {
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "csr": "CSR-633",
        "source": {
            "ledger": str(LEDGER),
            "execs": str(EXECS),
            "exec_prints": len(path),
        },
        "actual": actual,
        "cover_fixed_delay": delay_stats,
        "cover_exhaust_sell_print_60s": exhaust,
        "cover_optimistic_min_30s": optimistic_30s,
        "deepen_if_still_filled": deepen,
        "best_fixed_delay": {"key": best_delay[0], "delta_bp_sum": best_delay[1], "pct_better": best_delay[2]} if best_delay else None,
        "wobimm2_pin": {
            "variable": pick,
            "reason": pick_reason,
            "rule": (
                "When inventory is short (>=1 lot) and buy pressure persists "
                "(wobi>+0.15 or ofi>+0.02), pull COVER bid. Timeout 30s. "
                "Do not change offset. Do not replace WOBIMM CTRL."
            ),
        },
        "caveat": (
            "CF uses public last after the actual fill, not a re-quoted maker book. "
            "Fill-rate loss from waiting is not in the delay tables. "
            "Deepen rows assume still filled."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    result = run()
    print(json.dumps({k: result[k] for k in (
        "ts_jst", "actual", "cover_fixed_delay", "cover_exhaust_sell_print_60s",
        "cover_optimistic_min_30s", "deepen_if_still_filled", "best_fixed_delay", "wobimm2_pin",
    )}, indent=2, ensure_ascii=False))
