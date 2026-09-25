#!/usr/bin/env python3
"""CSR-521-CTRL v0 — OBSERVE inventory-control quant (WIRE=NO · ENFORCE=0).

Purpose: reduce HARD_STOP inflow measurement — NOT direction / alpha.
Order locked: OBSERVE design → bp delta observe → Economic PASS → ENFORCE consider.

CTRL-1 MaxHold Risk Ladder (L0–L4)
CTRL-2 Would-Unwind Simulator (virtual exit @30/60/120)
CTRL-3 IPS = f(hold) only (secondary board factors inactive)
CTRL-4 Virtual Size Decay
CTRL-5 One-Side Retreat Observer

Primary: Actual Ledger vs Virtual Exit@30s delta.

  nice -n 15 ionice -c3 python3 -m antigravity.quant_pipeline.umm_ctrl_observe
  nice -n 15 ionice -c3 python3 -m antigravity.quant_pipeline.umm_ctrl_observe --hours 1
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from antigravity.quant_pipeline.umm_pnl_x_hold import parse_log_trades

JST = timezone(timedelta(hours=9))
BASE = "/home/azureuser/antigravity"
OUT_DIR = f"{BASE}/data/mm_research"
CSR = "CSR-521-CTRL"
VERSION = "v0.2-ctrl2b"  # CSR-528 S1: Conditional Unwind KPIs · Hold×Spread · Long/Short

# CTRL-1 ladder
LADDER = [
    ("L0", 0.0, 15.0),
    ("L1", 15.0, 30.0),
    ("L2", 30.0, 60.0),
    ("L3", 60.0, 120.0),
    ("L4", 120.0, 1e9),
]

# CTRL-3 IPS (hold only)
IPS_BY_LEVEL = {"L0": 0.0, "L1": 0.25, "L2": 0.50, "L3": 0.75, "L4": 1.00}

# CTRL-4 size decay fractions after threshold
DECAY = [(30.0, 0.75), (60.0, 0.50), (120.0, 0.25)]


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def risk_hold_level(hold_sec: float) -> str:
    for name, lo, hi in LADDER:
        if lo <= hold_sec < hi:
            return name
    return "L4"


def ips_v01(hold_sec: float) -> float:
    return IPS_BY_LEVEL[risk_hold_level(hold_sec)]


def _reason_family(s: str) -> str:
    m = re.match(r"^([A-Z_]+)", str(s or ""))
    return m.group(1) if m else "OTHER"


def _load_mid_series(
    date: str, max_parts: int = 200, max_rows: int = 20000
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Returns (ts_sec, mid, bid, ask)."""
    paths = sorted(
        glob.glob(f"{BASE}/data/parquet/orderbook_micro/date={date}/part_*.parquet")
    )
    empty = (np.array([]), np.array([]), np.array([]), np.array([]))
    if not paths:
        return empty
    if len(paths) > max_parts:
        idx = np.linspace(0, len(paths) - 1, max_parts).astype(int)
        paths = [paths[i] for i in idx]
    frames = []
    for p in paths:
        try:
            frames.append(pd.read_parquet(p, columns=["timestamp", "best_bid", "best_ask"]))
        except Exception:
            pass
    if not frames:
        return empty
    micro = (
        pd.concat(frames, ignore_index=True)
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
    )
    ts = micro["timestamp"].astype(np.int64).to_numpy()
    ts_sec = ts / 1000.0 if np.nanmedian(ts) > 1e12 else ts.astype(float)
    bid = micro["best_bid"].astype(float).to_numpy()
    ask = micro["best_ask"].astype(float).to_numpy()
    mid = ((bid + ask) / 2.0).astype(float)
    if len(ts_sec) > max_rows:
        ii = np.linspace(0, len(ts_sec) - 1, max_rows).astype(int)
        ts_sec, mid, bid, ask = ts_sec[ii], mid[ii], bid[ii], ask[ii]
    return ts_sec, mid, bid, ask


def _mid_at(ts_sec: np.ndarray, mid: np.ndarray, t: float) -> Optional[float]:
    if len(ts_sec) == 0:
        return None
    j = np.searchsorted(ts_sec, t, side="right") - 1
    if j < 0:
        return None
    return float(mid[j])


def _virtual_pnl_bp(side: str, entry_px: float, mark: float) -> Optional[float]:
    if entry_px <= 0 or mark <= 0:
        return None
    raw = (mark - entry_px) / entry_px * 10000.0
    return round(raw if side == "buy" else -raw, 3)


def _size_frac_at(hold: float) -> float:
    frac = 1.0
    for thr, f in DECAY:
        if hold >= thr:
            frac = f
    return frac


def _decay_virtual_pnl(
    side: str,
    entry_px: float,
    entry_ts: float,
    exit_ts: float,
    actual_pnl: float,
    ts_sec: np.ndarray,
    mid: np.ndarray,
) -> Optional[float]:
    """Pathwise size-decay approx: full size to 30s, then 75/50/25 of subsequent Δbp."""
    hold = exit_ts - entry_ts
    if hold < 30:
        return round(float(actual_pnl), 3)
    marks = []
    for thr in (30.0, 60.0, 120.0):
        if hold >= thr:
            m = _mid_at(ts_sec, mid, entry_ts + thr)
            marks.append((thr, m))
    exit_m = _mid_at(ts_sec, mid, exit_ts)
    # build pnl checkpoints
    pts: List[Tuple[float, float]] = [(0.0, 0.0)]
    for thr, m in marks:
        if m is None:
            return None
        p = _virtual_pnl_bp(side, entry_px, m)
        if p is None:
            return None
        pts.append((thr, p))
    # actual at exit as final mark if available else actual_pnl
    if exit_m is not None:
        p_exit = _virtual_pnl_bp(side, entry_px, exit_m)
        if p_exit is not None:
            pts.append((hold, p_exit))
        else:
            pts.append((hold, float(actual_pnl)))
    else:
        pts.append((hold, float(actual_pnl)))

    # integrate with size fractions on intervals
    # [0,30)=1.0, [30,60)=0.75, [60,120)=0.50, [120,∞)=0.25
    def frac_for_interval_end(t_end: float) -> float:
        if t_end <= 30:
            return 1.0
        if t_end <= 60:
            return 0.75
        if t_end <= 120:
            return 0.50
        return 0.25

    total = 0.0
    for i in range(1, len(pts)):
        t0, p0 = pts[i - 1]
        t1, p1 = pts[i]
        dp = p1 - p0
        total += dp * frac_for_interval_end(t1)
    return round(total, 3)


def _ladder_table(df: pd.DataFrame) -> List[Dict[str, Any]]:
    rows = []
    for name, lo, hi in LADDER:
        g = df.loc[(df["hold_sec"] >= lo) & (df["hold_sec"] < hi)]
        n = int(len(g))
        hs = g["is_hard_stop"] if n else pd.Series(dtype=bool)
        reasons = (
            g["reason"].value_counts().head(5).to_dict() if n else {}
        )
        rows.append(
            {
                "level": name,
                "hold_range": f"{int(lo)}-{int(hi) if hi < 1e8 else '∞'}s",
                "n": n,
                "sum_pnl_bp": round(float(g["pnl_bp"].sum()), 2) if n else 0.0,
                "mean_pnl_bp": round(float(g["pnl_bp"].mean()), 3) if n else None,
                "hard_stop_rate": round(float(hs.mean()), 4) if n else None,
                "hard_stop_rate_pct": round(100.0 * float(hs.mean()), 1) if n else None,
                "exit_reason_top": {str(k): int(v) for k, v in reasons.items()},
                "ips": IPS_BY_LEVEL[name],
            }
        )
    return rows


def _would_unwind(
    df: pd.DataFrame,
    ts_sec: np.ndarray,
    mid: np.ndarray,
    thr: float,
    *,
    hold_max: Optional[float] = None,
    label: str = "",
) -> Dict[str, Any]:
    """CTRL-2: counterfactual exit at thr for trades with hold in (thr, hold_max].

    Δbp := Virtual@thr − Actual  on the **same trade_id set** (eligible ∩ mid-joined).
    Full-universe identity (must hold):
      Virtual_full = Σ(actual | hold≤thr) + Σ(virtual@thr | hold>thr)
      Δ_full = Virtual_full − Actual_full = Σ(virtual − actual | hold>thr)
    """
    mask = df["hold_sec"] > thr
    if hold_max is not None:
        mask = mask & (df["hold_sec"] <= hold_max)
    eligible = df.loc[mask].copy()
    n = int(len(eligible))
    if n == 0 or len(ts_sec) == 0:
        return {
            "threshold_sec": thr,
            "hold_max_sec": hold_max,
            "label": label or f"hold>{thr:g}s",
            "n_eligible": 0,
            "n_joined": 0,
            "actual_sum_bp": 0.0,
            "virtual_sum_bp": None,
            "delta_bp": None,
            "note": "no eligible or no mid series",
        }
    virt = []
    act = []
    samples = []
    for r in eligible.itertuples():
        m = _mid_at(ts_sec, mid, float(r.entry_ts) + thr)
        if m is None or float(r.entry_price or 0) <= 0:
            continue
        vp = _virtual_pnl_bp(str(r.side), float(r.entry_price), m)
        if vp is None:
            continue
        ap = float(r.pnl_bp)
        virt.append(vp)
        act.append(ap)
        if len(samples) < 8:
            samples.append(
                {
                    "side": r.side,
                    "hold_sec": round(float(r.hold_sec), 1),
                    "actual_pnl_bp": ap,
                    "virtual_pnl_bp": vp,
                    "delta_bp": round(vp - ap, 3),
                    "exit_reason": r.reason,
                }
            )
    if not virt:
        return {
            "threshold_sec": thr,
            "hold_max_sec": hold_max,
            "label": label or f"hold>{thr:g}s",
            "n_eligible": n,
            "n_joined": 0,
            "actual_sum_bp": round(float(eligible["pnl_bp"].sum()), 2),
            "virtual_sum_bp": None,
            "delta_bp": None,
            "note": "mid join failed",
        }
    a_sum = float(np.sum(act))
    v_sum = float(np.sum(virt))
    return {
        "threshold_sec": thr,
        "hold_max_sec": hold_max,
        "label": label or f"hold>{thr:g}s",
        "n_eligible": n,
        "n_joined": len(virt),
        "actual_sum_bp": round(a_sum, 2),
        "virtual_sum_bp": round(v_sum, 2),
        "delta_bp": round(v_sum - a_sum, 2),  # + = virtual better; SAME SET
        "mean_delta_bp": round(float(np.mean(np.array(virt) - np.array(act))), 3),
        "frac_virtual_better": round(float(np.mean(np.array(virt) > np.array(act))), 3),
        "samples": samples,
        "wire": "NO",
        "enforce": 0,
    }


def _full_counterfactual_ledger(
    actual_full_bp: float, unwind: Dict[str, Any]
) -> Dict[str, Any]:
    """Build display-safe full ledgers so Δ == Virtual_full − Actual_full.

    Never juxtapose Actual_full with Virtual_eligible_only (that yields bogus Δ).
    """
    a_elig = unwind.get("actual_sum_bp")
    v_elig = unwind.get("virtual_sum_bp")
    d = unwind.get("delta_bp")
    if v_elig is None or a_elig is None or d is None:
        return {
            "actual_full_bp": actual_full_bp,
            "virtual_full_bp": None,
            "delta_bp": None,
            "actual_eligible_bp": a_elig,
            "virtual_eligible_bp": v_elig,
            "identity_ok": None,
        }
    # keep short-hold actual; replace long-hold with virtual@thr
    virtual_full = round(float(actual_full_bp) - float(a_elig) + float(v_elig), 2)
    delta_full = round(virtual_full - float(actual_full_bp), 2)
    identity_ok = abs(delta_full - float(d)) < 0.02  # rounding tolerance
    return {
        "actual_full_bp": round(float(actual_full_bp), 2),
        "virtual_full_bp": virtual_full,
        "delta_bp": delta_full,
        "actual_eligible_bp": round(float(a_elig), 2),
        "virtual_eligible_bp": round(float(v_elig), 2),
        "n_joined": unwind.get("n_joined"),
        "identity_ok": identity_ok,
        "identity": "Δ = V_full−A_full = Σ(V−A | eligible) · same trade set",
    }


def _one_side(df: pd.DataFrame) -> Dict[str, Any]:
    out = {}
    for side, key in (("buy", "long"), ("sell", "short")):
        g = df.loc[df["side"] == side]
        n = int(len(g))
        out[key] = {
            "n": n,
            "sum_pnl_bp": round(float(g["pnl_bp"].sum()), 2) if n else 0.0,
            "mean_pnl_bp": round(float(g["pnl_bp"].mean()), 3) if n else None,
            "hard_stop_rate": round(float(g["is_hard_stop"].mean()), 4) if n else None,
            "hold_p50": round(float(g["hold_sec"].median()), 1) if n else None,
            "hold_p90": round(float(g["hold_sec"].quantile(0.9)), 1) if n else None,
            "frac_hold_gt_30": round(float((g["hold_sec"] > 30).mean()), 3) if n else None,
        }
    return out


def _profit_factor(pnls: np.ndarray) -> Optional[float]:
    if len(pnls) == 0:
        return None
    wins = float(pnls[pnls > 0].sum())
    losses = float(-pnls[pnls < 0].sum())
    if losses <= 1e-12:
        return None if wins <= 0 else 99.0
    return round(wins / losses, 3)


def _mdd_bp(pnls_in_time: np.ndarray) -> float:
    if len(pnls_in_time) == 0:
        return 0.0
    eq = np.cumsum(pnls_in_time)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    return round(float(dd.min()), 2)


def _mae_proxy(
    side: str,
    entry_px: float,
    entry_ts: float,
    exit_ts: float,
    ts_sec: np.ndarray,
    mid: np.ndarray,
    n_samples: int = 8,
) -> Optional[float]:
    """Light path MAE (bp, adverse-positive)."""
    if entry_px <= 0 or exit_ts <= entry_ts or len(ts_sec) == 0:
        return None
    times = np.linspace(entry_ts, exit_ts, n_samples)
    maes = []
    for t in times:
        m = _mid_at(ts_sec, mid, float(t))
        if m is None:
            continue
        vp = _virtual_pnl_bp(side, entry_px, m)
        if vp is None:
            continue
        maes.append(-vp)  # adverse = negative pnl flipped
    if not maes:
        return None
    return round(float(max(0.0, max(maes))), 3)


def _entry_spread_bp(
    entry_ts: float, ts_sec: np.ndarray, mid: np.ndarray, bid: np.ndarray, ask: np.ndarray
) -> Optional[float]:
    if len(ts_sec) == 0:
        return None
    j = np.searchsorted(ts_sec, entry_ts, side="right") - 1
    if j < 0:
        return None
    m = float(mid[j])
    if m <= 0:
        return None
    return round((float(ask[j]) - float(bid[j])) / m * 10000.0, 3)


def _spread_bucket(sp: Optional[float]) -> str:
    if sp is None or (isinstance(sp, float) and np.isnan(sp)):
        return "unknown"
    if sp < 1.2:
        return "spread_<1.2bp"
    if sp < 2.0:
        return "spread_1.2_2.0bp"
    return "spread_>=2.0bp"


def _would_unwind_kpis(
    df: pd.DataFrame,
    ts_sec: np.ndarray,
    mid: np.ndarray,
    thr: float,
    *,
    min_hold: Optional[float] = None,
    hold_max: Optional[float] = None,
    label: str = "",
) -> Dict[str, Any]:
    """CTRL-2B: matched-set Virtual@thr with Economic KPIs (OBSERVE only).

    min_hold: eligible if hold_sec >= min_hold AND hold_sec > thr
      L1PLUS → min_hold=15, L2PLUS → 30, L3PLUS → 60
    """
    mask = df["hold_sec"] > thr
    if min_hold is not None:
        mask = mask & (df["hold_sec"] >= min_hold)
    if hold_max is not None:
        mask = mask & (df["hold_sec"] <= hold_max)
    eligible = df.loc[mask].copy()
    n_elig = int(len(eligible))
    empty = {
        "label": label,
        "threshold_sec": thr,
        "min_hold_sec": min_hold,
        "hold_max_sec": hold_max,
        "n_eligible": n_elig,
        "matched_trade_count": 0,
        "VALID_COMPARISON": False,
        "delta_matched_bp": None,
        "note": "no eligible or no mid",
        "wire": "NO",
        "enforce": 0,
    }
    if n_elig == 0 or len(ts_sec) == 0:
        return empty

    rows = []
    for r in eligible.itertuples():
        m = _mid_at(ts_sec, mid, float(r.entry_ts) + thr)
        if m is None or float(r.entry_price or 0) <= 0:
            continue
        vp = _virtual_pnl_bp(str(r.side), float(r.entry_price), m)
        if vp is None:
            continue
        ap = float(r.pnl_bp)
        rows.append(
            {
                "actual": ap,
                "virtual": vp,
                "delta": vp - ap,
                "is_hs": bool(r.is_hard_stop),
                "exit_ts": float(r.exit_ts),
            }
        )
    if not rows:
        empty["note"] = "mid join failed"
        empty["n_eligible"] = n_elig
        return empty

    act = np.array([x["actual"] for x in rows], dtype=float)
    virt = np.array([x["virtual"] for x in rows], dtype=float)
    delta = virt - act
    hs = np.array([x["is_hs"] for x in rows], dtype=bool)

    # winner truncated: actual winner made worse by early exit
    win_trunc = (act > 0) & (virt < act)
    # loser saved: actual loser improved by early exit
    loser_saved = (act < 0) & (virt > act)
    # HS avoided proxy: was HardStop and virtual strictly better
    hs_avoided = hs & (virt > act)

    # time-ordered equity for MDD / PF on matched set only
    order = np.argsort([x["exit_ts"] for x in rows])
    act_t = act[order]
    # CF path: replace eligible actual with virtual (matched set = all here)
    virt_t = virt[order]

    a_sum = float(act.sum())
    v_sum = float(virt.sum())
    return {
        "label": label,
        "threshold_sec": thr,
        "min_hold_sec": min_hold,
        "hold_max_sec": hold_max,
        "n_eligible": n_elig,
        "matched_trade_count": int(len(rows)),
        "missing_virtual_count": int(n_elig - len(rows)),
        "VALID_COMPARISON": True,
        "actual_matched_bp": round(a_sum, 2),
        "virtual_matched_bp": round(v_sum, 2),
        "delta_matched_bp": round(v_sum - a_sum, 2),
        "frac_virtual_better": round(float(np.mean(delta > 0)), 3),
        "hard_stop_avoided_count": int(hs_avoided.sum()),
        "winner_truncated_count": int(win_trunc.sum()),
        "avg_winner_loss_bp": (
            round(float((act[win_trunc] - virt[win_trunc]).mean()), 3)
            if win_trunc.any()
            else None
        ),
        "avg_loser_saved_bp": (
            round(float((virt[loser_saved] - act[loser_saved]).mean()), 3)
            if loser_saved.any()
            else None
        ),
        "mdd_actual_bp": _mdd_bp(act_t),
        "mdd_virtual_bp": _mdd_bp(virt_t),
        "mdd_improvement_bp": round(_mdd_bp(virt_t) - _mdd_bp(act_t), 2),  # less neg = better
        "pf_actual": _profit_factor(act),
        "pf_virtual": _profit_factor(virt),
        "pf_improvement": (
            round(_profit_factor(virt) - _profit_factor(act), 3)
            if _profit_factor(act) is not None and _profit_factor(virt) is not None
            else None
        ),
        "formula": "delta = virtual_matched - actual_matched (same trade set)",
        "wire": "NO",
        "enforce": 0,
        "economic_pass": False,
    }


def _ladder_x_spread(df: pd.DataFrame) -> List[Dict[str, Any]]:
    rows = []
    for sp_name in ("spread_<1.2bp", "spread_1.2_2.0bp", "spread_>=2.0bp", "unknown"):
        for name, lo, hi in LADDER:
            g = df.loc[
                (df["spread_bucket"] == sp_name)
                & (df["hold_sec"] >= lo)
                & (df["hold_sec"] < hi)
            ]
            n = int(len(g))
            if n == 0 and sp_name == "unknown":
                continue
            rows.append(
                {
                    "spread_bucket": sp_name,
                    "level": name,
                    "n": n,
                    "sum_pnl_bp": round(float(g["pnl_bp"].sum()), 2) if n else 0.0,
                    "wr": round(float((g["pnl_bp"] > 0).mean()), 3) if n else None,
                    "hard_stop_rate": round(float(g["is_hard_stop"].mean()), 4) if n else None,
                    "mae_mean_bp": (
                        round(float(g["mae_bp"].mean()), 3)
                        if n and "mae_bp" in g and g["mae_bp"].notna().any()
                        else None
                    ),
                    "hold_p50": round(float(g["hold_sec"].median()), 1) if n else None,
                    "hold_p90": round(float(g["hold_sec"].quantile(0.9)), 1) if n else None,
                }
            )
    return rows


def _ladder_x_side(df: pd.DataFrame) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for side, key in (("buy", "BUY_inventory"), ("sell", "SELL_inventory")):
        rows = []
        sg = df.loc[df["side"] == side]
        for name, lo, hi in LADDER:
            g = sg.loc[(sg["hold_sec"] >= lo) & (sg["hold_sec"] < hi)]
            n = int(len(g))
            rows.append(
                {
                    "level": name,
                    "n": n,
                    "sum_pnl_bp": round(float(g["pnl_bp"].sum()), 2) if n else 0.0,
                    "mean_pnl_bp": round(float(g["pnl_bp"].mean()), 3) if n else None,
                    "wr": round(float((g["pnl_bp"] > 0).mean()), 3) if n else None,
                    "hard_stop_rate": round(float(g["is_hard_stop"].mean()), 4) if n else None,
                    "mae_mean_bp": (
                        round(float(g["mae_bp"].mean()), 3)
                        if n and "mae_bp" in g and g["mae_bp"].notna().any()
                        else None
                    ),
                    "hold_p50": round(float(g["hold_sec"].median()), 1) if n else None,
                    "hold_p90": round(float(g["hold_sec"].quantile(0.9)), 1) if n else None,
                    "mean_entry_spread_bp": (
                        round(float(g["entry_spread_bp"].mean()), 3)
                        if n and "entry_spread_bp" in g and g["entry_spread_bp"].notna().any()
                        else None
                    ),
                }
            )
        out[key] = rows
    return out


def run(
    date: Optional[str] = None,
    hours: Optional[float] = None,
) -> Dict[str, Any]:
    now = datetime.now(JST)
    date = date or now.strftime("%Y-%m-%d")
    df = parse_log_trades()
    if df.empty:
        raise SystemExit("no UMM trades in dryrun log")

    # entry_price may be missing in older parse — re-parse includes it from umm_pnl?
    # parse_log_trades currently has entry_price — check
    if "entry_price" not in df.columns:
        # recover from log again via extended parse
        pass

    df["reason"] = df["exit_reason_full"].map(_reason_family)
    df["is_hard_stop"] = df["reason"] == "HARD_STOP"
    df["risk_hold_level"] = df["hold_sec"].map(risk_hold_level)
    df["ips"] = df["hold_sec"].map(ips_v01)

    if hours is not None and hours > 0:
        cutoff = now.timestamp() - hours * 3600.0
        df = df.loc[df["exit_ts"] >= cutoff].copy()
        window = f"last_{hours:g}h"
    else:
        window = "session_log"

    # CSR-524: 当日 parquet が薄い/未作成でも Virtual Δ を落とさない（隣接日を結合）
    mid_days: List[str] = []
    try:
        d0 = datetime.strptime(date, "%Y-%m-%d").date()
        for k in (0, -1, -2, 1):
            mid_days.append((d0 + timedelta(days=k)).strftime("%Y-%m-%d"))
    except Exception:
        mid_days = [date]
    ts_parts: List[np.ndarray] = []
    mid_parts: List[np.ndarray] = []
    bid_parts: List[np.ndarray] = []
    ask_parts: List[np.ndarray] = []
    for d in mid_days:
        t_i, m_i, b_i, a_i = _load_mid_series(d)
        if len(t_i):
            ts_parts.append(t_i)
            mid_parts.append(m_i)
            bid_parts.append(b_i)
            ask_parts.append(a_i)
    if ts_parts:
        order = np.argsort(np.concatenate(ts_parts))
        ts_sec = np.concatenate(ts_parts)[order]
        mid = np.concatenate(mid_parts)[order]
        bid = np.concatenate(bid_parts)[order]
        ask = np.concatenate(ask_parts)[order]
    else:
        ts_sec, mid = np.array([]), np.array([])
        bid, ask = np.array([]), np.array([])

    # ensure entry_price
    if "entry_price" not in df.columns or df["entry_price"].isna().all():
        # fallback: cannot virtualize path — still ladder/HS
        df["entry_price"] = np.nan

    # Entry spread + light MAE (CSR-528 S1) — subsample MAE for CPU
    entry_spreads = []
    mae_vals = []
    mae_stride = max(1, len(df) // 800)  # cap ~800 MAE path joins
    for i, r in enumerate(df.itertuples()):
        entry_spreads.append(
            _entry_spread_bp(float(r.entry_ts), ts_sec, mid, bid, ask)
            if len(ts_sec)
            else None
        )
        if i % mae_stride == 0 and len(ts_sec) and float(r.entry_price or 0) > 0:
            mae_vals.append(
                _mae_proxy(
                    str(r.side),
                    float(r.entry_price),
                    float(r.entry_ts),
                    float(r.exit_ts),
                    ts_sec,
                    mid,
                )
            )
        else:
            mae_vals.append(None)
    df["entry_spread_bp"] = entry_spreads
    df["spread_bucket"] = [_spread_bucket(x) for x in entry_spreads]
    df["mae_bp"] = mae_vals

    ladder = _ladder_table(df)
    unwind_30 = _would_unwind(df, ts_sec, mid, 30.0, label="hold>30s (blanket)")
    unwind_60 = _would_unwind(df, ts_sec, mid, 60.0, label="hold>60s")
    unwind_120 = _would_unwind(df, ts_sec, mid, 120.0, label="hold>120s")
    # S2: L2/L3滞留のみ (30 < hold ≤ 120) — 一律30sではなく悪い滞留選別
    unwind_l2l3 = _would_unwind(
        df, ts_sec, mid, 30.0, hold_max=120.0, label="L2+L3 only (30s<hold≤120s) @exit30"
    )

    # CSR-528 S1 CTRL-2B Conditional Unwind (OBSERVE · ENFORCE=0)
    # L2PLUS first candidate: age>=30 AND ladder>=L2 ≡ hold>30 @mark30
    ctrl2b = {
        "Virtual30_ALL": _would_unwind_kpis(
            df, ts_sec, mid, 30.0, min_hold=30.0, label="Virtual30_ALL"
        ),
        "Virtual30_L1PLUS": _would_unwind_kpis(
            df, ts_sec, mid, 30.0, min_hold=15.0, label="Virtual30_L1PLUS"
        ),
        "Virtual30_L2PLUS": _would_unwind_kpis(
            df, ts_sec, mid, 30.0, min_hold=30.0, label="Virtual30_L2PLUS (first candidate)"
        ),
        "Virtual30_L3PLUS": _would_unwind_kpis(
            df, ts_sec, mid, 30.0, min_hold=60.0, label="Virtual30_L3PLUS"
        ),
        "forbidden": "age>=30 → 全決済は禁止 · Conditional only · ENFORCE=0",
        "first_candidate": "age>=30 AND ladder>=L2 → Virtual30_L2PLUS",
    }
    hold_x_spread = _ladder_x_spread(df)
    hold_x_side = _ladder_x_side(df)

    cf_30 = _full_counterfactual_ledger(round(float(df["pnl_bp"].sum()), 2), unwind_30)
    cf_60 = _full_counterfactual_ledger(cf_30["actual_full_bp"], unwind_60)
    cf_120 = _full_counterfactual_ledger(cf_30["actual_full_bp"], unwind_120)
    cf_l2l3 = _full_counterfactual_ledger(cf_30["actual_full_bp"], unwind_l2l3)

    # CTRL-4 size decay aggregate
    decay_act = []
    decay_virt = []
    for r in df.itertuples():
        if not (r.entry_price and float(r.entry_price) > 0):
            continue
        vp = _decay_virtual_pnl(
            str(r.side),
            float(r.entry_price),
            float(r.entry_ts),
            float(r.exit_ts),
            float(r.pnl_bp),
            ts_sec,
            mid,
        )
        if vp is None:
            continue
        decay_act.append(float(r.pnl_bp))
        decay_virt.append(vp)
    if decay_virt:
        size_decay = {
            "n_joined": len(decay_virt),
            "actual_sum_bp": round(float(np.sum(decay_act)), 2),
            "virtual_sum_bp": round(float(np.sum(decay_virt)), 2),
            "delta_bp": round(float(np.sum(decay_virt) - np.sum(decay_act)), 2),
            "schedule": "30s→75% · 60s→50% · 120s→25%",
        }
    else:
        size_decay = {
            "n_joined": 0,
            "actual_sum_bp": None,
            "virtual_sum_bp": None,
            "delta_bp": None,
            "schedule": "30s→75% · 60s→50% · 120s→25%",
        }

    actual_sum = round(float(df["pnl_bp"].sum()), 2)
    report = {
        "date": date,
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S JST"),
        "csr": CSR,
        "version": VERSION,
        "mode": "observe_inventory_control",
        "wire": "NO",
        "enforce": 0,
        "auto_apply": False,
        "economic_pass": False,
        "purpose": "HARD_STOP流入を減らす観測（方向予測・アルファ生成ではない）",
        "order_lock": [
            "OBSERVE設計",
            "bp改善観察",
            "Economic PASS",
            "ENFORCE検討",
        ],
        "window": window,
        "source": {
            "trades": "dryrun_umm_tf2bp_24h.log entry/exit",
            "n_trades": int(len(df)),
            "actual_ledger_sum_bp": actual_sum,
            "mid_series_n": int(len(ts_sec)),
        },
        "irs_v01": {
            "primary": ["hold"],
            "secondary_inactive": [
                "csnt",
                "fake_bo",
                "spread",
                "tip_thin",
                "imbalance",
            ],
            "ips_def": "L0=0 · L1=0.25 · L2=0.50 · L3=0.75 · L4=1.0",
            "status": "OBSERVE_ONLY",
        },
        "ctrl1_maxhold_ladder": ladder,
        "ctrl2_would_unwind": {
            "at_30s": unwind_30,
            "at_60s": unwind_60,
            "at_120s": unwind_120,
            "l2_l3_only_at_30s": unwind_l2l3,
            "full_ledger_cf": {
                "at_30s": cf_30,
                "at_60s": cf_60,
                "at_120s": cf_120,
                "l2_l3_only_at_30s": cf_l2l3,
            },
            "priority": "Virtual Exit@30s vs Actual · SAME trade set · full ledger identity enforced",
            "audit_2026_09_25": (
                "BUGFIX display: virtual_30_ledger was eligible-only sum; "
                "juxtaposed with Actual_full caused bogus Δ (e.g. 0.40−29.73=−29.33). "
                "True Δ = V_full−A_full = Σ(V−A|eligible). ENFORCE still blocked."
            ),
        },
        "ctrl2b_conditional_unwind": ctrl2b,
        "hold_ladder_x_spread": hold_x_spread,
        "hold_ladder_x_long_short": hold_x_side,
        "ctrl3_ips": {
            "mean_ips": round(float(df["ips"].mean()), 4) if len(df) else None,
            "by_level": {r["level"]: r["ips"] for r in ladder},
        },
        "ctrl4_virtual_size_decay": size_decay,
        "ctrl5_one_side_retreat": _one_side(df),
        "hourly_watch": {
            "ladder_pnl_hs": [
                {
                    "level": r["level"],
                    "sum_pnl_bp": r["sum_pnl_bp"],
                    "hard_stop_rate_pct": r["hard_stop_rate_pct"],
                    "n": r["n"],
                }
                for r in ladder
            ],
            # Canonical Economic metrics (SAME-SET / full-ledger identity)
            "actual_ledger_bp": cf_30["actual_full_bp"],
            "virtual_30_full_ledger_bp": cf_30["virtual_full_bp"],
            "would_exit_30_delta_bp": cf_30["delta_bp"],
            "would_exit_60_delta_bp": cf_60["delta_bp"],
            "would_exit_120_delta_bp": cf_120["delta_bp"],
            "l2_l3_only_delta_bp": cf_l2l3["delta_bp"],
            "delta_identity_ok": cf_30["identity_ok"],
            # Diagnostics (do NOT subtract these against each other across sets)
            "actual_eligible_gt30_bp": cf_30["actual_eligible_bp"],
            "virtual_30_eligible_sum_bp": cf_30["virtual_eligible_bp"],
            # legacy alias — now equals FULL counterfactual (not eligible-only)
            "virtual_30_ledger_bp": cf_30["virtual_full_bp"],
            "size_decay_delta_bp": size_decay.get("delta_bp"),
            # CSR-528 S1 CTRL-2B watch
            "ctrl2b_L2PLUS_delta_bp": ctrl2b["Virtual30_L2PLUS"].get("delta_matched_bp"),
            "ctrl2b_L3PLUS_delta_bp": ctrl2b["Virtual30_L3PLUS"].get("delta_matched_bp"),
            "ctrl2b_L2PLUS_winner_trunc": ctrl2b["Virtual30_L2PLUS"].get(
                "winner_truncated_count"
            ),
            "ctrl2b_L2PLUS_hs_avoided": ctrl2b["Virtual30_L2PLUS"].get(
                "hard_stop_avoided_count"
            ),
        },
        "observe_lock": "OBSERVE_ONLY · Δ identity enforced · blanket@30 ≠ Economic PASS · ENFORCE blocked",
        "lock": {
            "first_factor": "hold",
            "observe": ["virtual_max_hold", "virtual_unwind", "virtual_size_decay", "ctrl2b"],
            "deferred": ["csnt", "fake_bo"],
            "forbidden": [
                "新アルファ生成",
                "価格予測利用",
                "ENFORCE",
                "LIVE反映",
                "age>=30全決済",
            ],
        },
        "expectation": "L2以上で急悪化の再現確認 · Virtual@30s Δが正なら制御仮説支持（PASSではない）",
        "note": "記述・仮想のみ · 実発注変更なし · 経済PASS不出 · CSR-528 S1 CTRL-2B",
    }

    os.makedirs(f"{OUT_DIR}/daily", exist_ok=True)
    suffix = f"_{window}" if hours else ""
    path = f"{OUT_DIR}/daily/{date}_umm_ctrl_observe{suffix}.json"
    state = f"{OUT_DIR}/umm_ctrl_observe_state.json"
    for p in (path, state):
        with open(p + ".tmp", "w") as f:
            json.dump(_json_safe(report), f, indent=2, ensure_ascii=False)
        os.replace(p + ".tmp", p)
    # also always refresh canonical daily without suffix for librarian/hourly
    canon = f"{OUT_DIR}/daily/{date}_umm_ctrl_observe.json"
    if path != canon:
        with open(canon + ".tmp", "w") as f:
            json.dump(_json_safe(report), f, indent=2, ensure_ascii=False)
        os.replace(canon + ".tmp", canon)
    report["_path"] = path
    report["_state"] = state
    return _json_safe(report)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None, help="limit to last N hours")
    ap.add_argument("--date", type=str, default=None)
    args = ap.parse_args()
    rep = run(date=args.date, hours=args.hours)
    hw = rep["hourly_watch"]
    print(
        json.dumps(
            {
                "csr": CSR,
                "version": VERSION,
                "window": rep["window"],
                "n": rep["source"]["n_trades"],
                "ladder": hw["ladder_pnl_hs"],
                "actual_full": hw["actual_ledger_bp"],
                "virtual_30_full": hw.get("virtual_30_full_ledger_bp"),
                "would_30_delta": hw["would_exit_30_delta_bp"],
                "would_60_delta": hw["would_exit_60_delta_bp"],
                "would_120_delta": hw["would_exit_120_delta_bp"],
                "l2_l3_only_delta": hw.get("l2_l3_only_delta_bp"),
                "delta_identity_ok": hw.get("delta_identity_ok"),
                "ctrl2b": {
                    k: {
                        "n": v.get("matched_trade_count"),
                        "delta": v.get("delta_matched_bp"),
                        "hs_avoided": v.get("hard_stop_avoided_count"),
                        "win_trunc": v.get("winner_truncated_count"),
                        "mdd_impr": v.get("mdd_improvement_bp"),
                        "pf_impr": v.get("pf_improvement"),
                    }
                    for k, v in (rep.get("ctrl2b_conditional_unwind") or {}).items()
                    if isinstance(v, dict) and "matched_trade_count" in v
                },
                "eligible_diag": {
                    "actual_gt30": hw.get("actual_eligible_gt30_bp"),
                    "virtual_gt30": hw.get("virtual_30_eligible_sum_bp"),
                },
                "size_decay_delta": hw["size_decay_delta_bp"],
                "one_side": rep["ctrl5_one_side_retreat"],
                "path": rep["_path"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
