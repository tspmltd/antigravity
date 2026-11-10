#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HONEST paper hourly Discord — 10戦略並走 (baseline + repair + ABS1 + VR1).

1. WOBIMM4_HONEST (Honest MM baseline)
2. WOBIMM4X2_HONEST (MM scale-in)
3. MS1_WOBI_HONEST (Micro Scalping)
4. MS1X2_WOBI_HONEST (MS1 scale-in)
5. TF1A_WOBI_HONEST (Pure TF baseline)
6. WOBIMM5_HONEST (MM Position-EV + Adverse path · A/B vs MM4)
7. TF2A_WOBI_HONEST (TF HS HIT_BID_FORCE repair A/B)
8. MS2_WOBI_HONEST (MS Adverse path + entry veto + fast cut · A/B vs MS1)
9. ABS1_WOBI_HONEST (Absent Scalp · LIVE_PARITY · A/B vs MS2)
10. VR1_WOBI_HONEST (Vacuum Reversal · LIVE_PARITY)

WIRE=NO. 専用Webhookへの毎時自動通知。
ABS1/VR1 は Soak8-hard verdict（n=0≠PASS · Economic_PASS=NO）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
JST = timezone(timedelta(hours=9))

from antigravity.quant_pipeline.live_parity_dryrun import (  # noqa: E402
    MIN_HARVEST_BP,
    count_harvests,
    fmt_verdict_line,
    lane_verdict,
)

DEFAULT_HONEST_DISCORD_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1565586355050913894/"
    "i2pdup9Zv4sI8crv18sR62Za-BfSW4joTy9LCY7zqqhgG1LuCVsMUduHUMCXs9gQoV2w"
)

MM_LEDGER = AG / "data" / "mm_research" / "wobimm4_honest_dryrun" / "paired_ledger.jsonl"
MMX2_LEDGER = AG / "data" / "mm_research" / "wobimm4x2_honest_dryrun" / "paired_ledger.jsonl"
MM5_LEDGER = AG / "data" / "mm_research" / "wobimm5_honest_dryrun" / "paired_ledger.jsonl"
MS1_LEDGER = AG / "data" / "mm_research" / "ms1_wobi_honest_dryrun" / "paired_ledger.jsonl"
MS1X2_LEDGER = AG / "data" / "mm_research" / "ms1x2_wobi_honest_dryrun" / "paired_ledger.jsonl"
MS2_LEDGER = AG / "data" / "mm_research" / "ms2_wobi_honest_dryrun" / "paired_ledger.jsonl"
ABS1_LEDGER = AG / "data" / "mm_research" / "abs1_wobi_honest_dryrun" / "paired_ledger.jsonl"
VR1_LEDGER = AG / "data" / "mm_research" / "vr1_wobi_honest_dryrun" / "paired_ledger.jsonl"
TF1A_LEDGER = AG / "data" / "mm_research" / "tf1a_wobi_honest_dryrun" / "paired_ledger.jsonl"
TF2A_LEDGER = AG / "data" / "mm_research" / "tf2a_wobi_honest_dryrun" / "paired_ledger.jsonl"

MM_STATE = AG / "data" / "dryrun_wobimm4_honest_state.json"
MMX2_STATE = AG / "data" / "dryrun_wobimm4x2_honest_state.json"
MM5_STATE = AG / "data" / "dryrun_wobimm5_honest_state.json"
MS1_STATE = AG / "data" / "dryrun_ms1_wobi_honest_state.json"
MS1X2_STATE = AG / "data" / "dryrun_ms1x2_wobi_honest_state.json"
MS2_STATE = AG / "data" / "dryrun_ms2_wobi_honest_state.json"
ABS1_STATE = AG / "data" / "dryrun_abs1_wobi_honest_state.json"
VR1_STATE = AG / "data" / "dryrun_vr1_wobi_honest_state.json"
TF1A_STATE = AG / "data" / "dryrun_tf1a_wobi_honest_state.json"
TF2A_STATE = AG / "data" / "dryrun_tf2a_wobi_honest_state.json"

LEGACY_TF_LEDGER = AG / "data" / "mm_research" / "tf2b_wobi5_honest_dryrun" / "paired_ledger.jsonl"
LEGACY_TF_STATE = AG / "data" / "dryrun_tf2b_wobi5_honest_state.json"


def honest_discord_webhook_url() -> str:
    return (
        os.environ.get("DISCORD_HONEST_WEBHOOK_URL")
        or DEFAULT_HONEST_DISCORD_WEBHOOK_URL
    ).strip()


HONEST_DISCORD_WEBHOOK_URL = honest_discord_webhook_url()


def jst_midnight_ts(now: Optional[float] = None) -> float:
    dt = datetime.fromtimestamp(now if now is not None else time.time(), JST)
    return datetime(dt.year, dt.month, dt.day, tzinfo=JST).timestamp()


def jst_one_hour_window(now: Optional[float] = None) -> tuple[float, float, str]:
    """1h = one JST clock hour, not a rolling 3600s."""
    dt = datetime.fromtimestamp(now if now is not None else time.time(), JST)
    floor = dt.replace(minute=0, second=0, microsecond=0)
    elapsed = (dt - floor).total_seconds()
    if elapsed < 120.0:
        end = floor
        start = floor - timedelta(hours=1)
    else:
        start = floor
        end = dt
    label = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} JST"
    return start.timestamp(), end.timestamp(), label


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def is_ledger_junk(row: Dict[str, Any]) -> bool:
    if "price" not in row:
        return False
    try:
        px = float(row.get("price"))
    except (TypeError, ValueError):
        return False
    return px == 103.0 or (px > 0.0 and px < 1000.0)


def load_pnl_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    target = path if path.exists() else None
    if not target and path == MS1_LEDGER and LEGACY_TF_LEDGER.exists():
        target = LEGACY_TF_LEDGER
    if not target or not target.exists():
        return rows
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except Exception:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or "pnl_bp" not in row:
            continue
        if is_ledger_junk(row):
            continue
        rows.append(row)
    return rows


def window_stats(
    rows: List[Dict[str, Any]],
    since_ts: float,
    until_ts: Optional[float] = None,
) -> Dict[str, Any]:
    sub = []
    for r in rows:
        if is_ledger_junk(r):
            continue
        ts = float(r.get("ts") or 0.0)
        if ts < since_ts:
            continue
        if until_ts is not None and ts >= until_ts:
            continue
        sub.append(r)
    n = len(sub)
    if not n:
        return {
            "total_trades": 0,
            "win_trades": 0,
            "scratch_trades": 0,
            "win_rate_pct": 0.0,
            "pnl_bp": 0.0,
            "pnl_jpy": 0.0,
            "ev_bp": 0.0,
        }
    pnls = [float(r.get("pnl_bp") or 0.0) for r in sub]
    wins = sum(1 for x in pnls if x > 0.0)
    scratches = sum(1 for x in pnls if x == 0.0)
    pnl_bp = round(sum(pnls), 2)
    pnl_jpy = round(sum(float(r.get("pnl_jpy") or 0.0) for r in sub), 1)
    return {
        "total_trades": n,
        "win_trades": wins,
        "scratch_trades": scratches,
        "win_rate_pct": round(100.0 * wins / n, 1),
        "pnl_bp": pnl_bp,
        "pnl_jpy": pnl_jpy,
        "ev_bp": round(pnl_bp / n, 4),
    }


def _fmt_block(name: str, s: Dict[str, Any]) -> str:
    return (
        f"• **{name}**: **`{s['pnl_bp']:+.2f} bp`** ({s['pnl_jpy']:+.1f}円) | "
        f"`{s['win_trades']}/{s['total_trades']}` 勝率 `{s['win_rate_pct']:.1f}%` | "
        f"EV **`{s['ev_bp']:+.2f}bp`**"
    )


def _pos_mm(state: Dict[str, Any]) -> str:
    net = state.get("net_lots")
    return f"net={net:+d}" if net is not None else "FLAT"


def _pos_tf(state: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        block = state.get(k)
        if isinstance(block, dict) and block.get("position"):
            return str(block.get("position"))
    return "FLAT"


def _diff_line(label: str, base: Dict[str, Any], repair: Dict[str, Any]) -> str:
    b_ev = float(base.get("ev_bp") or 0.0)
    r_ev = float(repair.get("ev_bp") or 0.0)
    b_n = int(base.get("total_trades") or 0)
    r_n = int(repair.get("total_trades") or 0)
    d_ev = r_ev - b_ev
    d_pnl = float(repair.get("pnl_bp") or 0.0) - float(base.get("pnl_bp") or 0.0)
    # Zero-sample Δ is not a tie / win — mark insufficient.
    if b_n <= 0 and r_n <= 0:
        return (
            f"• **{label}**: `INSUFFICIENT` n=0/0 · Δ not scored · Economic_PASS=NO"
        )
    return (
        f"• **{label}**: ΔEV `{d_ev:+.2f}bp` (base `{b_ev:+.2f}` n={b_n} → "
        f"repair `{r_ev:+.2f}` n={r_n}) · Δpnl `{d_pnl:+.2f}bp`"
    )


def _parity_verdict_from_rows(
    rows: List[Dict[str, Any]], since_ts: float, now_ts: float
) -> Dict[str, Any]:
    sub = []
    for r in rows or []:
        try:
            ts = float(r.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts < since_ts:
            continue
        sub.append(r)
    net_sum = 0.0
    for r in sub:
        try:
            net_sum += float(r.get("pnl_bp_net", r.get("pnl_bp")) or 0.0)
        except (TypeError, ValueError):
            pass
    return lane_verdict(
        n=len(sub),
        harvest_ge_min=count_harvests(sub, MIN_HARVEST_BP),
        window_sec=max(0.0, float(now_ts) - float(since_ts)),
        net_pnl_bp_sum=net_sum,
    )


def collect_honest_hourly(now: Optional[float] = None) -> Dict[str, Any]:
    ts = now if now is not None else time.time()
    midnight = jst_midnight_ts(ts)
    h0, h1, hour_label = jst_one_hour_window(ts)

    mm_rows = load_pnl_rows(MM_LEDGER)
    mmx2_rows = load_pnl_rows(MMX2_LEDGER)
    mm5_rows = load_pnl_rows(MM5_LEDGER)
    ms1_rows = load_pnl_rows(MS1_LEDGER)
    ms1x2_rows = load_pnl_rows(MS1X2_LEDGER)
    ms2_rows = load_pnl_rows(MS2_LEDGER)
    abs1_rows = load_pnl_rows(ABS1_LEDGER)
    vr1_rows = load_pnl_rows(VR1_LEDGER)
    tf1a_rows = load_pnl_rows(TF1A_LEDGER)
    tf2a_rows = load_pnl_rows(TF2A_LEDGER)

    mm_state = _load_json(MM_STATE)
    mmx2_state = _load_json(MMX2_STATE)
    mm5_state = _load_json(MM5_STATE)
    ms1_state = _load_json(MS1_STATE) or _load_json(LEGACY_TF_STATE)
    ms1x2_state = _load_json(MS1X2_STATE)
    ms2_state = _load_json(MS2_STATE)
    abs1_state = _load_json(ABS1_STATE)
    vr1_state = _load_json(VR1_STATE)
    tf1a_state = _load_json(TF1A_STATE)
    tf2a_state = _load_json(TF2A_STATE)

    return {
        "ts_jst": datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "midnight_jst": datetime.fromtimestamp(midnight, JST).strftime("%Y-%m-%d 00:00 JST"),
        "hour_label": hour_label,
        "hour_start_ts": h0,
        "hour_end_ts": h1,
        "wire": "NO",
        "fill": "FLEET_LIVE_PARITY",
        "WOBIMM4_HONEST": {
            "position": _pos_mm(mm_state),
            "stats_1h": window_stats(mm_rows, h0, h1),
            "stats_since_midnight": window_stats(mm_rows, midnight),
            "verdict_day": _parity_verdict_from_rows(mm_rows, midnight, ts),
        },
        "WOBIMM4X2_HONEST": {
            "position": _pos_mm(mmx2_state),
            "stats_1h": window_stats(mmx2_rows, h0, h1),
            "stats_since_midnight": window_stats(mmx2_rows, midnight),
            "verdict_day": _parity_verdict_from_rows(mmx2_rows, midnight, ts),
        },
        "WOBIMM5_HONEST": {
            "position": _pos_mm(mm5_state),
            "stats_1h": window_stats(mm5_rows, h0, h1),
            "stats_since_midnight": window_stats(mm5_rows, midnight),
            "verdict_day": _parity_verdict_from_rows(mm5_rows, midnight, ts),
        },
        "MS1_WOBI_HONEST": {
            "position": _pos_tf(
                ms1_state, "MS1_WOBI_HONEST", "TF2B_WOBI5_HONEST", "TF2B_WOBI5"
            ),
            "stats_1h": window_stats(ms1_rows, h0, h1),
            "stats_since_midnight": window_stats(ms1_rows, midnight),
            "verdict_day": _parity_verdict_from_rows(ms1_rows, midnight, ts),
        },
        "MS1X2_WOBI_HONEST": {
            "position": _pos_tf(ms1x2_state, "MS1X2_WOBI_HONEST", "MS1_WOBI_HONEST"),
            "stats_1h": window_stats(ms1x2_rows, h0, h1),
            "stats_since_midnight": window_stats(ms1x2_rows, midnight),
            "verdict_day": _parity_verdict_from_rows(ms1x2_rows, midnight, ts),
        },
        "MS2_WOBI_HONEST": {
            "position": _pos_tf(
                ms2_state, "MS2_WOBI_HONEST", "MS1_WOBI_HONEST", "TF2B_WOBI5"
            ),
            "stats_1h": window_stats(ms2_rows, h0, h1),
            "stats_since_midnight": window_stats(ms2_rows, midnight),
            "verdict_day": _parity_verdict_from_rows(ms2_rows, midnight, ts),
        },
        "ABS1_WOBI_HONEST": {
            "position": _pos_tf(
                abs1_state, "ABS1_WOBI_HONEST", "MS2_WOBI_HONEST", "TF2B_WOBI5"
            ),
            "stats_1h": window_stats(abs1_rows, h0, h1),
            "stats_since_midnight": window_stats(abs1_rows, midnight),
            "absent": (abs1_state.get("absent") or {}),
            "parity": (abs1_state.get("parity") or {}),
            "verdict_day": _parity_verdict_from_rows(abs1_rows, midnight, ts),
        },
        "VR1_WOBI_HONEST": {
            "position": _pos_tf(
                vr1_state, "VR1_WOBI_HONEST", "MS1_WOBI_HONEST", "TF2B_WOBI5"
            ),
            "stats_1h": window_stats(vr1_rows, h0, h1),
            "stats_since_midnight": window_stats(vr1_rows, midnight),
            "vacuum": (vr1_state.get("vacuum") or {}),
            "parity": (vr1_state.get("parity") or {}),
            "verdict_day": _parity_verdict_from_rows(vr1_rows, midnight, ts),
        },
        "TF1A_WOBI_HONEST": {
            "position": _pos_tf(
                tf1a_state, "TF1A_WOBI_HONEST", "TF2B_WOBI5_HONEST", "TF2B_WOBI5"
            ),
            "stats_1h": window_stats(tf1a_rows, h0, h1),
            "stats_since_midnight": window_stats(tf1a_rows, midnight),
            "verdict_day": _parity_verdict_from_rows(tf1a_rows, midnight, ts),
        },
        "TF2A_WOBI_HONEST": {
            "position": _pos_tf(tf2a_state, "TF2A_WOBI_HONEST", "TF1A_WOBI_HONEST"),
            "stats_1h": window_stats(tf2a_rows, h0, h1),
            "stats_since_midnight": window_stats(tf2a_rows, midnight),
            "verdict_day": _parity_verdict_from_rows(tf2a_rows, midnight, ts),
        },
    }


def build_honest_hourly_payload(rep: Dict[str, Any], kind: str = "hourly") -> Dict[str, Any]:
    mm = rep.get("WOBIMM4_HONEST") or {}
    mmx2 = rep.get("WOBIMM4X2_HONEST") or {}
    mm5 = rep.get("WOBIMM5_HONEST") or {}
    ms1 = rep.get("MS1_WOBI_HONEST") or {}
    ms1x2 = rep.get("MS1X2_WOBI_HONEST") or {}
    ms2 = rep.get("MS2_WOBI_HONEST") or {}
    abs1 = rep.get("ABS1_WOBI_HONEST") or {}
    vr1 = rep.get("VR1_WOBI_HONEST") or {}
    tf1a = rep.get("TF1A_WOBI_HONEST") or {}
    tf2a = rep.get("TF2A_WOBI_HONEST") or {}

    mm_1h = mm.get("stats_1h") or {}
    mmx2_1h = mmx2.get("stats_1h") or {}
    mm5_1h = mm5.get("stats_1h") or {}
    ms1_1h = ms1.get("stats_1h") or {}
    ms1x2_1h = ms1x2.get("stats_1h") or {}
    ms2_1h = ms2.get("stats_1h") or {}
    abs1_1h = abs1.get("stats_1h") or {}
    vr1_1h = vr1.get("stats_1h") or {}
    tf1a_1h = tf1a.get("stats_1h") or {}
    tf2a_1h = tf2a.get("stats_1h") or {}

    mm_mid = mm.get("stats_since_midnight") or {}
    mmx2_mid = mmx2.get("stats_since_midnight") or {}
    mm5_mid = mm5.get("stats_since_midnight") or {}
    ms1_mid = ms1.get("stats_since_midnight") or {}
    ms1x2_mid = ms1x2.get("stats_since_midnight") or {}
    ms2_mid = ms2.get("stats_since_midnight") or {}
    abs1_mid = abs1.get("stats_since_midnight") or {}
    vr1_mid = vr1.get("stats_since_midnight") or {}
    tf1a_mid = tf1a.get("stats_since_midnight") or {}
    tf2a_mid = tf2a.get("stats_since_midnight") or {}

    day_bp = (
        float(mm_mid.get("pnl_bp") or 0.0)
        + float(mmx2_mid.get("pnl_bp") or 0.0)
        + float(mm5_mid.get("pnl_bp") or 0.0)
        + float(ms1_mid.get("pnl_bp") or 0.0)
        + float(ms1x2_mid.get("pnl_bp") or 0.0)
        + float(ms2_mid.get("pnl_bp") or 0.0)
        + float(abs1_mid.get("pnl_bp") or 0.0)
        + float(vr1_mid.get("pnl_bp") or 0.0)
        + float(tf1a_mid.get("pnl_bp") or 0.0)
        + float(tf2a_mid.get("pnl_bp") or 0.0)
    )
    color = 0x2ECC71 if day_bp >= 0 else 0xE67E22
    abs_meta = abs1.get("absent") or {}
    vac_meta = vr1.get("vacuum") or {}
    lane_vs = [
        ("MM4", mm.get("verdict_day") or {}),
        ("MM5", mm5.get("verdict_day") or {}),
        ("MMX2", mmx2.get("verdict_day") or {}),
        ("MS1", ms1.get("verdict_day") or {}),
        ("MS2", ms2.get("verdict_day") or {}),
        ("ABS1", abs1.get("verdict_day") or {}),
        ("VR1", vr1.get("verdict_day") or {}),
        ("MS1X2", ms1x2.get("verdict_day") or {}),
        ("TF1A", tf1a.get("verdict_day") or {}),
        ("TF2A", tf2a.get("verdict_day") or {}),
    ]
    hard = {str(v.get("verdict")) for _, v in lane_vs}
    if "FAIL" in hard:
        color = 0xE74C3C
    elif "INSUFFICIENT" in hard and day_bp >= 0:
        color = 0xF1C40F
    verdict_block = "\n".join(fmt_verdict_line(n, v) for n, v in lane_vs)
    title = {
        "start": "[START] HONEST 10戦略並走 1h + 0:00累積",
        "hourly": f"[HOURLY] HONEST 10戦略並走 · {rep.get('ts_jst')}",
        "final": "[FINAL] HONEST 10戦略並走",
    }.get(kind, f"[HONEST] {kind}")

    return {
        "username": "HONEST 10-Strategy Reporter",
        "embeds": [
            {
                "title": title,
                "description": (
                    f"`{rep.get('fill')}` · WIRE=NO · 累積起点 `{rep.get('midnight_jst')}`\n"
                    f"MM=`{mm.get('position')}` · MM5=`{mm5.get('position')}` · "
                    f"MMX2=`{mmx2.get('position')}` · "
                    f"MS1=`{ms1.get('position')}` · MS2=`{ms2.get('position')}` · "
                    f"ABS1=`{abs1.get('position')}` · VR1=`{vr1.get('position')}` · "
                    f"MS1X2=`{ms1x2.get('position')}` · "
                    f"TF1A=`{tf1a.get('position')}` · TF2A=`{tf2a.get('position')}`\n"
                    f"ABS1 thin=`{int(abs_meta.get('thin_book_blocks') or 0)}` · "
                    f"VR1 arms=`{int(vac_meta.get('arms') or 0)}` fills=`{int(vac_meta.get('fills') or 0)}`\n"
                    f"{verdict_block}"
                ),
                "color": color,
                "fields": [
                    {
                        "name": f"1時間 {rep.get('hour_label')}",
                        "value": (
                            f"{_fmt_block('WOBIMM4 (MM base)', mm_1h)}\n"
                            f"{_fmt_block('WOBIMM5 (MM Pos-EV+AE)', mm5_1h)}\n"
                            f"{_fmt_block('WOBIMM4X2 (MM add)', mmx2_1h)}\n"
                            f"{_fmt_block('MS1 (Micro Scalp)', ms1_1h)}\n"
                            f"{_fmt_block('MS2 (MS Adverse)', ms2_1h)}\n"
                            f"{_fmt_block('ABS1 (Absent Scalp)', abs1_1h)}\n"
                            f"{_fmt_block('VR1 (Vacuum Rebound)', vr1_1h)}\n"
                            f"{_fmt_block('MS1X2 (MS1 add)', ms1x2_1h)}\n"
                            f"{_fmt_block('TF1A (TF base)', tf1a_1h)}\n"
                            f"{_fmt_block('TF2A (TF HS force)', tf2a_1h)}"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "Repair A/B Δ (1h)",
                        "value": (
                            f"{_diff_line('MM4→MM5', mm_1h, mm5_1h)}\n"
                            f"{_diff_line('MS1→MS2', ms1_1h, ms2_1h)}\n"
                            f"{_diff_line('MS2→ABS1', ms2_1h, abs1_1h)}\n"
                            f"{_diff_line('TF1A→TF2A', tf1a_1h, tf2a_1h)}"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "0:00 JST からの累積 (Daily)",
                        "value": (
                            f"{_fmt_block('WOBIMM4 (MM base)', mm_mid)}\n"
                            f"{_fmt_block('WOBIMM5 (MM Pos-EV+AE)', mm5_mid)}\n"
                            f"{_fmt_block('WOBIMM4X2 (MM add)', mmx2_mid)}\n"
                            f"{_fmt_block('MS1 (Micro Scalp)', ms1_mid)}\n"
                            f"{_fmt_block('MS2 (MS Adverse)', ms2_mid)}\n"
                            f"{_fmt_block('ABS1 (Absent Scalp)', abs1_mid)}\n"
                            f"{_fmt_block('VR1 (Vacuum Rebound)', vr1_mid)}\n"
                            f"{_fmt_block('MS1X2 (MS1 add)', ms1x2_mid)}\n"
                            f"{_fmt_block('TF1A (TF base)', tf1a_mid)}\n"
                            f"{_fmt_block('TF2A (TF HS force)', tf2a_mid)}"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "Repair A/B Δ (day) + 合算",
                        "value": (
                            f"{_diff_line('MM4→MM5 day', mm_mid, mm5_mid)}\n"
                            f"{_diff_line('MS1→MS2 day', ms1_mid, ms2_mid)}\n"
                            f"{_diff_line('MS2→ABS1 day', ms2_mid, abs1_mid)}\n"
                            f"{_diff_line('TF1A→TF2A day', tf1a_mid, tf2a_mid)}\n"
                            f"• **10戦略合算**: **`{day_bp:+.2f} bp`**"
                        ),
                        "inline": False,
                    },
                ],
                "footer": {"text": f"Antigravity Quants · HONEST Fleet · {rep.get('ts_jst')}"},
            }
        ],
    }


def post_honest_hourly(kind: str = "hourly", now: Optional[float] = None) -> bool:
    payload = build_honest_hourly_payload(collect_honest_hourly(now), kind)
    url = honest_discord_webhook_url()
    if not url:
        return False
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return 200 <= int(resp.status) < 300


if __name__ == "__main__":
    import pprint
    data = collect_honest_hourly()
    pprint.pprint(data)
