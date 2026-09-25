#!/usr/bin/env python3
"""CSR-521-GO — UMM PnL × Hold Time + InventoryRiskScore (OBSERVE).

Primary source: dryrun_umm_tf2bp_24h.log entry/exit pairs (true hold).
AE holding_time_sec is INVALID for this (auto-complete @ 35s).

  nice -n 15 ionice -c3 python3 -m antigravity.quant_pipeline.umm_pnl_x_hold

WIRE=NO · ENFORCE=0 · no economic PASS.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

import numpy as np
import pandas as pd

JST = timezone(timedelta(hours=9))
BASE = "/home/azureuser/antigravity"
LOG = f"{BASE}/logs/dryrun_umm_tf2bp_24h.log"
STATE = f"{BASE}/data/dryrun_umm_tf2bp_state.json"
OUT_DIR = f"{BASE}/data/mm_research"
HOLD_BINS = [0, 15, 30, 60, 120, 1e9]
HOLD_LABELS = ["0-15s", "15-30s", "30-60s", "60-120s", "120s+"]

RE_ENTRY = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[UMM\] 📥 新規エントリー: (BUY|SELL) @ ¥([\d,]+)"
)
RE_EXIT = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[UMM\] 📤 エグジット/キャンセル \([^)]+\): "
    r"PnL: [+\-]?[\d.]+円 \(([+\-]?\d+\.?\d*)bp\).*?\((.+)\)\s*$"
)


def _parse_ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST).timestamp()


def parse_log_trades(log_path: str = LOG) -> pd.DataFrame:
    open_pos = None
    trades: List[Dict[str, Any]] = []
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RE_ENTRY.match(line)
            if m:
                open_pos = {
                    "entry_ts": _parse_ts(m.group(1)),
                    "side": m.group(2).lower(),
                    "entry_price": float(m.group(3).replace(",", "")),
                }
                continue
            m = RE_EXIT.match(line)
            if m and open_pos is not None:
                exit_ts = _parse_ts(m.group(1))
                hold = exit_ts - open_pos["entry_ts"]
                if hold >= 0:
                    trades.append(
                        {
                            "entry_ts": open_pos["entry_ts"],
                            "exit_ts": exit_ts,
                            "hold_sec": hold,
                            "side": open_pos["side"],
                            "entry_price": open_pos["entry_price"],
                            "pnl_bp": float(m.group(2)),
                            "exit_reason_full": m.group(3).strip()[:80],
                        }
                    )
                open_pos = None
    return pd.DataFrame(trades)


def inventory_risk_score(row: pd.Series) -> float:
    """OBSERVE composite → inventory controller, NOT price predictor."""
    ht = min(float(row["hold_sec"]) / 120.0, 1.5)
    return round(
        0.35 * ht
        + 0.25 * float(row.get("csnt_frac") or 0)
        + 0.15 * float(row.get("fake_bo_frac") or 0)
        + 0.10 * (1.0 if row.get("tip_thin_any") else 0.0)
        + 0.10 * min(float(row.get("mean_spread_bp") or 0) / 5.0, 2.0)
        + 0.05 * min(float(row.get("mean_abs_imb") or 0) / 0.5, 2.0),
        4,
    )


def _bucket_stats(g: pd.DataFrame) -> Dict[str, Any]:
    return {
        "n": int(len(g)),
        "sum_bp": round(float(g["pnl_bp"].sum()), 2) if len(g) else 0.0,
        "mean_bp": round(float(g["pnl_bp"].mean()), 3) if len(g) else None,
        "win_rate": round(float((g["pnl_bp"] > 0).mean()), 3) if len(g) else None,
        "neg_sum_bp": round(float(g.loc[g["pnl_bp"] < 0, "pnl_bp"].sum()), 2) if len(g) else 0.0,
        "median_hold": round(float(g["hold_sec"].median()), 1) if len(g) else None,
    }


def attach_board_env(df: pd.DataFrame, date: str = "2026-09-23") -> pd.DataFrame:
    paths = sorted(
        glob.glob(f"{BASE}/data/parquet/orderbook_micro/date={date}/part_*.parquet")
    )
    if not paths:
        return df
    if len(paths) > 80:
        idx = np.linspace(0, len(paths) - 1, 80).astype(int)
        paths = [paths[i] for i in idx]
    frames = []
    cols = [
        "timestamp",
        "cancel_rate",
        "refill_rate",
        "taker_volume_bid",
        "taker_volume_ask",
        "taker_aggressiveness",
        "imbalance",
        "best_bid",
        "best_ask",
    ]
    for p in paths:
        try:
            frames.append(pd.read_parquet(p, columns=cols))
        except Exception:
            pass
    if not frames:
        return df
    micro = (
        pd.concat(frames, ignore_index=True)
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
    )
    ts = micro["timestamp"].astype(np.int64).to_numpy()
    micro["ts_sec"] = ts / 1000.0 if np.nanmedian(ts) > 1e12 else ts.astype(float)
    if len(micro) > 5000:
        ii = np.linspace(0, len(micro) - 1, 5000).astype(int)
        micro = micro.iloc[ii].reset_index(drop=True)
    cancel = micro["cancel_rate"].astype(float).fillna(0)
    refill = micro["refill_rate"].astype(float).fillna(0)
    cr = cancel - refill
    tb = micro["taker_volume_bid"].astype(float).fillna(0)
    ta = micro["taker_volume_ask"].astype(float).fillna(0)
    tagg = micro["taker_aggressiveness"].astype(float).fillna(0)
    cs = (cancel >= 0.40) & (cr >= 0.15)
    fb = (cancel >= 0.55) & (tagg < 0.15)
    csnt = cs & ~((tb + ta) >= 0.01)
    mid = ((micro["best_bid"] + micro["best_ask"]) / 2.0).astype(float)
    sp = ((micro["best_ask"] - micro["best_bid"]) / mid * 10000).astype(float)
    tip_thin = sp >= float(sp.quantile(0.8))
    imb = micro["imbalance"].astype(float).fillna(0).abs()
    m_ts = micro["ts_sec"].to_numpy()
    m_csnt, m_fb, m_thin = csnt.to_numpy(), fb.to_numpy(), tip_thin.to_numpy()
    m_sp, m_imb = sp.to_numpy(), imb.to_numpy()

    def env_during(entry: float, hold: float) -> Dict[str, Any]:
        exit_ts = entry + hold
        j0 = np.searchsorted(m_ts, entry, side="left")
        j1 = np.searchsorted(m_ts, exit_ts, side="right") - 1
        if j1 < 0:
            return {}
        j0 = max(0, min(j0, len(m_ts) - 1))
        j1 = max(j0, min(j1, len(m_ts) - 1))
        win = slice(j0, j1 + 1)
        return {
            "csnt_any": bool(m_csnt[win].any()),
            "fake_bo_any": bool(m_fb[win].any()),
            "tip_thin_any": bool(m_thin[win].any()),
            "csnt_frac": float(m_csnt[win].mean()),
            "fake_bo_frac": float(m_fb[win].mean()),
            "mean_spread_bp": float(np.nanmean(m_sp[win])),
            "mean_abs_imb": float(np.nanmean(m_imb[win])),
        }

    env = pd.DataFrame(
        [env_during(float(r.entry_ts), float(r.hold_sec)) for r in df.itertuples()]
    )
    return pd.concat([df.reset_index(drop=True), env], axis=1)


def run(date: str = "2026-09-23") -> Dict[str, Any]:
    df = parse_log_trades()
    if df.empty:
        raise SystemExit("no UMM entry/exit pairs in dryrun log")
    df = attach_board_env(df, date=date)
    df["hold_bucket"] = pd.cut(
        df["hold_sec"], bins=HOLD_BINS, labels=HOLD_LABELS, right=False
    )
    df["inventory_risk_score"] = df.apply(inventory_risk_score, axis=1)

    hold_table = [
        {"bucket": lab, **_bucket_stats(df.loc[df["hold_bucket"] == lab])}
        for lab in HOLD_LABELS
    ]
    short = df.loc[df["hold_sec"] < 30]
    longish = df.loc[df["hold_sec"] >= 30]
    pattern = {
        "sum_bp_0_30s": round(float(short["pnl_bp"].sum()), 2),
        "sum_bp_30s_plus": round(float(longish["pnl_bp"].sum()), 2),
        "n_0_30s": int(len(short)),
        "n_30s_plus": int(len(longish)),
        "hold_time_dominates_pattern": bool(
            float(short["pnl_bp"].sum()) > 0 and float(longish["pnl_bp"].sum()) < 0
        ),
    }

    try:
        df["irs_q"] = pd.qcut(
            df["inventory_risk_score"],
            5,
            labels=["Q1", "Q2", "Q3", "Q4", "Q5"],
            duplicates="drop",
        )
    except Exception:
        df["irs_q"] = None
    irs_table = []
    if df["irs_q"] is not None:
        for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
            g = df.loc[df["irs_q"] == q]
            if len(g) == 0:
                continue
            irs_table.append(
                {
                    "quintile": q,
                    **_bucket_stats(g),
                    "mean_irs": round(float(g["inventory_risk_score"].mean()), 4),
                    "mean_hold": round(float(g["hold_sec"].mean()), 1),
                }
            )

    led_sum = None
    if os.path.exists(STATE):
        state = json.load(open(STATE))
        led_sum = round(
            sum(float(t.get("pnl_bp") or 0) for t in state.get("umm", {}).get("trades", [])),
            2,
        )

    report = {
        "date": date,
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "csr": "CSR-521-GO",
        "mode": "pnl_x_hold_time",
        "wire": "NO",
        "enforce": 0,
        "auto_apply": False,
        "source": {
            "primary": "dryrun_umm_tf2bp_24h.log entry/exit pairs",
            "n_trades": int(len(df)),
            "sum_bp": round(float(df["pnl_bp"].sum()), 2),
            "hold_p50": round(float(df["hold_sec"].median()), 1),
            "hold_p90": round(float(df["hold_sec"].quantile(0.9)), 1),
            "ledger_sum_bp_ref": led_sum,
            "ae_hold_invalid": "AE auto-complete @ 35s — do not use for hold buckets",
        },
        "pnl_x_hold": hold_table,
        "short_vs_long_pattern": pattern,
        "inventory_risk_score": {
            "def": "f(hold_time, csnt, fake_breakout, spread, tip_thin, imbalance)",
            "role": "inventory_controller_not_price_predictor",
            "quintiles": irs_table,
        },
        "diagnosis": {
            "framing": "異常板環境で在庫を抱え過ぎる問題（市場方向当てではない）",
            "if_pattern": "0-30s黒字 ∧ 30s+赤字 ⇒ Entryではなく保有時間",
            "p0_join": "CSR-520 spread×inventory_proxy → InventoryRiskScore に吸収",
            "connect_to": "UMM在庫制御器（size/pause/max_hold）· OBSERVE · ENFORCE=0",
        },
        "csnt_observe": {"status": "OBSERVE_CANDIDATE", "enforce": 0, "wire": "NO"},
        "note": "記述のみ · 経済PASSではない · Adoption LOCKED",
    }
    os.makedirs(f"{OUT_DIR}/daily", exist_ok=True)
    path = f"{OUT_DIR}/daily/{date}_umm_pnl_x_hold.json"
    with open(path + ".tmp", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    with open(f"{OUT_DIR}/umm_pnl_x_hold_state.json.tmp", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    os.replace(
        f"{OUT_DIR}/umm_pnl_x_hold_state.json.tmp",
        f"{OUT_DIR}/umm_pnl_x_hold_state.json",
    )
    return report


if __name__ == "__main__":
    rep = run()
    print(
        json.dumps(
            {
                "n": rep["source"]["n_trades"],
                "sum_bp": rep["source"]["sum_bp"],
                "pattern": rep["short_vs_long_pattern"],
                "pnl_x_hold": rep["pnl_x_hold"],
                "irs_q": rep["inventory_risk_score"]["quintiles"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
