"""CSR-539 P0-B — Inventory Would-Unwind @30s FIXED primary evaluation (OBSERVE).

Primary horizon = 30s only (60/120 secondary). Breakdowns:
  ALL · L0..L4 · long/short
ENFORCE=0 · LIVE=OFF · Economic PASS=NO · dryrun非中断.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pandas as pd

from antigravity.quant_pipeline.umm_pnl_x_hold import parse_log_trades
from antigravity.quant_pipeline.umm_ctrl_observe import (
    LADDER,
    _ladder_table,
    _load_mid_series,
    _would_unwind_kpis,
    risk_hold_level,
    ips_v01,
    _reason_family,
)

JST = timezone(timedelta(hours=9))
OUT = "/home/azureuser/antigravity/data/mm_research/daily"
PRIMARY = 30.0


def _side_mask(df: pd.DataFrame, side: str) -> pd.Series:
    s = df["side"].astype(str).str.lower()
    if side == "long":
        return s.isin(["buy", "long"])
    if side == "short":
        return s.isin(["sell", "short"])
    return pd.Series(True, index=df.index)


def _slice_kpis(df, ts_sec, mid, mask, label: str) -> Dict[str, Any]:
    sub = df.loc[mask].copy()
    if len(sub) == 0:
        return {"label": label, "n": 0, "delta_matched_bp": None, "matched_trade_count": 0}
    k = _would_unwind_kpis(sub, ts_sec, mid, PRIMARY, min_hold=PRIMARY, label=label)
    return {
        "label": label,
        "n": int(len(sub)),
        "delta_matched_bp": k.get("delta_matched_bp"),
        "matched_trade_count": k.get("matched_trade_count"),
        "actual_matched_bp": k.get("actual_matched_bp"),
        "virtual_matched_bp": k.get("virtual_matched_bp"),
        "winner_truncated_count": k.get("winner_truncated_count"),
        "hard_stop_avoided_count": k.get("hard_stop_avoided_count"),
        "VALID_COMPARISON": k.get("VALID_COMPARISON"),
        "wire": "NO",
        "enforce": 0,
    }


def run(hours: float = 24.0) -> Dict[str, Any]:
    now = datetime.now(JST)
    df = parse_log_trades()
    if df is None or df.empty:
        return {"status": "NO_TRADES", "wire": "NO", "enforce": 0}

    df["reason"] = df["exit_reason_full"].map(_reason_family)
    df["is_hard_stop"] = df["reason"] == "HARD_STOP"
    df["risk_hold_level"] = df["hold_sec"].map(risk_hold_level)
    df["ips"] = df["hold_sec"].map(ips_v01)

    if hours and hours > 0:
        cutoff = now.timestamp() - hours * 3600.0
        df = df.loc[df["exit_ts"] >= cutoff].copy()

    date = now.strftime("%Y-%m-%d")
    mid_days = []
    d0 = now.date()
    for k in (0, -1, -2, 1):
        mid_days.append((d0 + timedelta(days=k)).strftime("%Y-%m-%d"))
    # merge mid series across adjacent days (same as umm_ctrl_observe.run)
    import numpy as np

    ts_parts, mid_parts, bid_parts, ask_parts = [], [], [], []
    for d in mid_days:
        t_i, m_i, b_i, a_i = _load_mid_series(d)
        if len(t_i):
            ts_parts.append(t_i)
            mid_parts.append(m_i)
            bid_parts.append(b_i)
            ask_parts.append(a_i)
    if ts_parts:
        ts_sec = np.concatenate(ts_parts)
        mid = np.concatenate(mid_parts)
        bid = np.concatenate(bid_parts)
        ask = np.concatenate(ask_parts)
        order = np.argsort(ts_sec)
        ts_sec, mid, bid, ask = ts_sec[order], mid[order], bid[order], ask[order]
    else:
        ts_sec = mid = bid = ask = np.array([])

    rows = {"ALL": _slice_kpis(df, ts_sec, mid, df.index == df.index, "ALL@30s")}
    for name, lo, hi in LADDER:
        m = (df["hold_sec"] >= lo) & (df["hold_sec"] < hi)
        rows[name] = _slice_kpis(df, ts_sec, mid, m, f"{name}@30s")
    rows["long"] = _slice_kpis(df, ts_sec, mid, _side_mask(df, "long"), "long@30s")
    rows["short"] = _slice_kpis(df, ts_sec, mid, _side_mask(df, "short"), "short@30s")

    secondary = {}
    for thr in (60.0, 120.0):
        k = _would_unwind_kpis(df, ts_sec, mid, thr, min_hold=thr, label=f"ALL@{thr:g}s")
        secondary[f"@{int(thr)}s"] = {
            "delta_matched_bp": k.get("delta_matched_bp"),
            "matched_trade_count": k.get("matched_trade_count"),
            "hard_stop_avoided_count": k.get("hard_stop_avoided_count"),
        }

    out = {
        "csr": "CSR-539",
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S JST"),
        "primary_horizon_sec": PRIMARY,
        "secondary_horizons_sec": [60, 120],
        "window_hours": hours,
        "n_trades": int(len(df)),
        "primary_30s_breakdown": rows,
        "secondary_all": secondary,
        "hold_ladder": _ladder_table(df),
        "economic_pass": False,
        "hardcode_30s_exit": False,
        "note_ja": (
            "30sは固定primary評価軸。全面30s決済の本番化は禁止（CSR-536反例）。"
            "L3/L4 HS↑ → Conditional Unwind。"
        ),
        "wire": "NO",
        "enforce": 0,
    }
    os.makedirs(OUT, exist_ok=True)
    path = f"{OUT}/{now.strftime('%Y-%m-%d')}_csr539_inventory_30s_primary.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)
    out["path"] = path
    return out


if __name__ == "__main__":
    print(json.dumps(run(hours=24.0), indent=2, ensure_ascii=False, default=str))
