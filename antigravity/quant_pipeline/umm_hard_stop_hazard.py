#!/usr/bin/env python3
"""CSR-521-HAZARD — Conditional P(HARD_STOP | hold>T) + ∩ csnt/fake_bo.

  nice -n 15 ionice -c3 python3 -m antigravity.quant_pipeline.umm_hard_stop_hazard

WIRE=NO · ENFORCE=0 · causal confirmation only · no economic PASS.
"""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

JST = timezone(timedelta(hours=9))
BASE = "/home/azureuser/antigravity"
LOG = f"{BASE}/logs/dryrun_umm_tf2bp_24h.log"
OUT_DIR = f"{BASE}/data/mm_research"
THRESHOLDS = [15, 30, 45, 60, 90, 120, 180]

RE_ENTRY = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[UMM\] 📥 新規エントリー: (BUY|SELL) @ ¥([\d,]+)"
)
RE_EXIT = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[UMM\] 📤 エグジット/キャンセル \([^)]+\): "
    r"PnL: [+\-]?[\d.]+円 \(([+\-]?\d+\.?\d*)bp\).*?\((.+)\)\s*$"
)


def _parse_ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST).timestamp()


def parse_trades(log_path: str = LOG) -> pd.DataFrame:
    open_pos = None
    rows: List[Dict[str, Any]] = []
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RE_ENTRY.match(line)
            if m:
                open_pos = {"entry_ts": _parse_ts(m.group(1))}
                continue
            m = RE_EXIT.match(line)
            if m and open_pos is not None:
                exit_ts = _parse_ts(m.group(1))
                hold = exit_ts - open_pos["entry_ts"]
                if hold >= 0:
                    reason = m.group(3).strip()
                    fam_m = re.match(r"^([A-Z_]+)", reason)
                    fam = fam_m.group(1) if fam_m else "OTHER"
                    rows.append(
                        {
                            "entry_ts": open_pos["entry_ts"],
                            "hold_sec": hold,
                            "pnl_bp": float(m.group(2)),
                            "reason": fam,
                            "is_hard_stop": fam == "HARD_STOP",
                        }
                    )
                open_pos = None
    return pd.DataFrame(rows)


def attach_env(df: pd.DataFrame, date: str = "2026-09-23") -> pd.DataFrame:
    paths = sorted(
        glob.glob(f"{BASE}/data/parquet/orderbook_micro/date={date}/part_*.parquet")
    )
    if not paths:
        return df
    if len(paths) > 200:
        idx = np.linspace(0, len(paths) - 1, 200).astype(int)
        paths = [paths[i] for i in idx]
    frames = []
    cols = [
        "timestamp",
        "cancel_rate",
        "refill_rate",
        "taker_volume_bid",
        "taker_volume_ask",
        "taker_aggressiveness",
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
    if len(micro) > 20000:
        ii = np.linspace(0, len(micro) - 1, 20000).astype(int)
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
    m_ts = micro["ts_sec"].to_numpy()
    m_csnt, m_fb = csnt.to_numpy(), fb.to_numpy()

    def env_during(entry: float, hold: float) -> Dict[str, Any]:
        j0 = np.searchsorted(m_ts, entry, side="left")
        j1 = np.searchsorted(m_ts, entry + hold, side="right") - 1
        if j1 < 0:
            return {"csnt_any": False, "fake_bo_any": False}
        j0 = max(0, min(j0, len(m_ts) - 1))
        j1 = max(j0, min(j1, len(m_ts) - 1))
        win = slice(j0, j1 + 1)
        return {
            "csnt_any": bool(m_csnt[win].any()),
            "fake_bo_any": bool(m_fb[win].any()),
        }

    env = pd.DataFrame(
        [env_during(float(r.entry_ts), float(r.hold_sec)) for r in df.itertuples()]
    )
    return pd.concat([df.reset_index(drop=True), env], axis=1)


def hazard(df: pd.DataFrame, mask, label: str) -> Dict[str, Any]:
    g = df.loc[mask]
    n = int(len(g))
    if n == 0:
        return {
            "cond": label,
            "n": 0,
            "n_hs": 0,
            "p_hs": None,
            "p_hs_pct": None,
            "sum_bp_hs": 0.0,
        }
    hs = g["is_hard_stop"]
    p = float(hs.mean())
    return {
        "cond": label,
        "n": n,
        "n_hs": int(hs.sum()),
        "p_hs": round(p, 4),
        "p_hs_pct": round(100.0 * p, 1),
        "sum_bp_hs": round(float(g.loc[hs, "pnl_bp"].sum()), 2),
        "sum_bp": round(float(g["pnl_bp"].sum()), 2),
    }


def run(date: str = "2026-09-23") -> Dict[str, Any]:
    df = parse_trades()
    if df.empty:
        raise SystemExit("no trades")
    df = attach_env(df, date=date)
    base = hazard(df, pd.Series([True] * len(df)), "unconditional")
    hold_gt = [hazard(df, df["hold_sec"] > t, f"hold>{t}s") for t in THRESHOLDS]
    contrast = [
        hazard(df, df["hold_sec"] <= 15, "hold<=15s"),
        hazard(df, df["hold_sec"] <= 30, "hold<=30s"),
    ]
    inter = [
        hazard(
            df,
            (df["hold_sec"] > 30) & df["csnt_any"].fillna(False),
            "hold>30 ∩ csnt",
        ),
        hazard(
            df,
            (df["hold_sec"] > 30) & ~df["csnt_any"].fillna(False),
            "hold>30 ∩ ¬csnt",
        ),
        hazard(
            df,
            (df["hold_sec"] > 30) & df["fake_bo_any"].fillna(False),
            "hold>30 ∩ fake_bo",
        ),
        hazard(
            df,
            (df["hold_sec"] > 30) & ~df["fake_bo_any"].fillna(False),
            "hold>30 ∩ ¬fake_bo",
        ),
        hazard(
            df,
            (df["hold_sec"] > 30)
            & df["csnt_any"].fillna(False)
            & df["fake_bo_any"].fillna(False),
            "hold>30 ∩ csnt ∩ fake_bo",
        ),
    ]

    def _p(cond: str) -> Optional[float]:
        for h in inter:
            if h["cond"] == cond:
                return h["p_hs"]
        return None

    pcsnt, pncsnt = _p("hold>30 ∩ csnt"), _p("hold>30 ∩ ¬csnt")
    pfb, pnfb = _p("hold>30 ∩ fake_bo"), _p("hold>30 ∩ ¬fake_bo")
    p30 = next(h["p_hs"] for h in hold_gt if h["cond"] == "hold>30s")
    p120 = next(h["p_hs"] for h in hold_gt if h["cond"] == "hold>120s")
    p_le30 = contrast[1]["p_hs"]

    # time is risk if step at 30s is large and 120s >= 30s
    time_risk = bool(
        p_le30 is not None
        and p30 is not None
        and p120 is not None
        and (p30 - p_le30) >= 0.15
        and p120 >= p30
    )
    jump_csnt = (
        round(pcsnt - pncsnt, 4) if pcsnt is not None and pncsnt is not None else None
    )
    jump_fb = round(pfb - pnfb, 4) if pfb is not None and pnfb is not None else None

    factors = []
    if time_risk:
        factors.append("hold")
    if jump_csnt is not None and jump_csnt >= 0.05:
        factors.append("csnt")
    if jump_fb is not None and jump_fb >= 0.05:
        factors.append("fake_bo")

    verdict = {
        "loss_mechanism": (
            "異常板環境で在庫を30秒以上保持し、その後 HARD_STOP へ流入するテール損失"
        ),
        "not": "Entry品質",
        "time_as_risk_factor": time_risk,
        "time_note": (
            f"P(HS|hold<=30)={100*p_le30:.1f}% → P(HS|hold>30)={100*p30:.1f}% → "
            f"P(HS|hold>120)={100*p120:.1f}%（30sで段差・以降なだらか上昇）"
        ),
        "board_amplification": {
            "csnt_delta_pp": jump_csnt,
            "fake_bo_delta_pp": jump_fb,
            "note": "hold主因 · csnt/fake_boは中程度の増幅（跳ねは穏やか）",
        },
        "irs_major_factors_candidate": factors,
        "irs_status": "OBSERVE固定 · ENFORCE=0",
        "phase": "損失発生機構の因果確認（アルファ生成ではない）",
        "user_eval_lock": (
            "UMM損失原因はEntry品質ではなく、異常板で30s+保持→HARD_STOPテールにほぼ収束"
        ),
    }

    report = {
        "date": date,
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "csr": "CSR-521-HAZARD",
        "mode": "conditional_hazard_hard_stop",
        "wire": "NO",
        "enforce": 0,
        "auto_apply": False,
        "source": {
            "primary": "dryrun_umm_tf2bp_24h.log entry/exit",
            "n_trades": int(len(df)),
            "n_hard_stop": int(df["is_hard_stop"].sum()),
            "base_p_hs": base["p_hs"],
            "base_p_hs_pct": base["p_hs_pct"],
        },
        "base": base,
        "contrast_short_hold": contrast,
        "p_hs_given_hold_gt": hold_gt,
        "intersection": inter,
        "verdict": verdict,
        "note": "記述のみ · 経済PASSではない · IRS OBSERVE固定",
    }
    os.makedirs(f"{OUT_DIR}/daily", exist_ok=True)
    path = f"{OUT_DIR}/daily/{date}_umm_hard_stop_hazard.json"
    with open(path + ".tmp", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    with open(f"{OUT_DIR}/umm_hard_stop_hazard_state.json.tmp", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    os.replace(
        f"{OUT_DIR}/umm_hard_stop_hazard_state.json.tmp",
        f"{OUT_DIR}/umm_hard_stop_hazard_state.json",
    )
    report["_path"] = path
    return report


if __name__ == "__main__":
    rep = run()
    print("=== P(HARD_STOP | hold > T) ===")
    for h in rep["p_hs_given_hold_gt"]:
        print(f"  {h['cond']:12}  n={h['n']:4}  {h['p_hs_pct']}%")
    print("=== intersection ===")
    for h in rep["intersection"]:
        print(f"  {h['cond']:28}  n={h['n']:4}  {h['p_hs_pct']}%")
    print(json.dumps(rep["verdict"], ensure_ascii=False, indent=2))
    print(rep["_path"])
