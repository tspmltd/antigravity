#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HONEST paper hourly Discord — 3戦略並走 (MM / MS1 / TF1A).

1. WOBIMM4_HONEST (Honest MM)
2. MS1_WOBI_HONEST (Micro Scalping)
3. TF1A_WOBI_HONEST (Pure Trend Following · Target >= 5bp)

WIRE=NO. 専用Webhookへの毎時自動通知。
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

DEFAULT_HONEST_DISCORD_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1565586355050913894/"
    "i2pdup9Zv4sI8crv18sR62Za-BfSW4joTy9LCY7zqqhgG1LuCVsMUduHUMCXs9gQoV2w"
)

MM_LEDGER = AG / "data" / "mm_research" / "wobimm4_honest_dryrun" / "paired_ledger.jsonl"
MS1_LEDGER = AG / "data" / "mm_research" / "ms1_wobi_honest_dryrun" / "paired_ledger.jsonl"
TF1A_LEDGER = AG / "data" / "mm_research" / "tf1a_wobi_honest_dryrun" / "paired_ledger.jsonl"

MM_STATE = AG / "data" / "dryrun_wobimm4_honest_state.json"
MS1_STATE = AG / "data" / "dryrun_ms1_wobi_honest_state.json"
TF1A_STATE = AG / "data" / "dryrun_tf1a_wobi_honest_state.json"

# フォールバック
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


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


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
        rows.append(row)
    return rows


def window_stats(rows: List[Dict[str, Any]], since_ts: float) -> Dict[str, Any]:
    sub = [r for r in rows if float(r.get("ts") or 0.0) >= since_ts]
    n = len(sub)
    if not n:
        return {
            "total_trades": 0,
            "win_trades": 0,
            "win_rate_pct": 0.0,
            "pnl_bp": 0.0,
            "pnl_jpy": 0.0,
            "ev_bp": 0.0,
        }
    wins = sum(1 for r in sub if bool(r.get("is_win")) or float(r.get("pnl_bp") or 0.0) > 0)
    pnl_bp = round(sum(float(r.get("pnl_bp") or 0.0) for r in sub), 2)
    pnl_jpy = round(sum(float(r.get("pnl_jpy") or 0.0) for r in sub), 1)
    return {
        "total_trades": n,
        "win_trades": wins,
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


def collect_honest_hourly(now: Optional[float] = None) -> Dict[str, Any]:
    ts = now if now is not None else time.time()
    midnight = jst_midnight_ts(ts)
    hour_ago = ts - 3600.0

    mm_rows = load_pnl_rows(MM_LEDGER)
    ms1_rows = load_pnl_rows(MS1_LEDGER)
    tf1a_rows = load_pnl_rows(TF1A_LEDGER)

    mm_state = _load_json(MM_STATE)
    ms1_state = _load_json(MS1_STATE) or _load_json(LEGACY_TF_STATE)
    tf1a_state = _load_json(TF1A_STATE)

    mm_net = mm_state.get("net_lots")
    mm_pos = f"net={mm_net:+d}" if mm_net is not None else "FLAT"
    ms1_pos = str((ms1_state.get("MS1_WOBI_HONEST") or ms1_state.get("TF2B_WOBI5_HONEST") or ms1_state.get("TF2B_WOBI5") or {}).get("position") or "FLAT")
    tf1a_pos = str((tf1a_state.get("TF1A_WOBI_HONEST") or {}).get("position") or "FLAT")

    return {
        "ts_jst": datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "midnight_jst": datetime.fromtimestamp(midnight, JST).strftime("%Y-%m-%d 00:00 JST"),
        "wire": "NO",
        "fill": "HONEST_BOARD_IMITATION",
        "WOBIMM4_HONEST": {
            "position": mm_pos,
            "stats_1h": window_stats(mm_rows, hour_ago),
            "stats_since_midnight": window_stats(mm_rows, midnight),
        },
        "MS1_WOBI_HONEST": {
            "position": ms1_pos,
            "stats_1h": window_stats(ms1_rows, hour_ago),
            "stats_since_midnight": window_stats(ms1_rows, midnight),
        },
        "TF1A_WOBI_HONEST": {
            "position": tf1a_pos,
            "stats_1h": window_stats(tf1a_rows, hour_ago),
            "stats_since_midnight": window_stats(tf1a_rows, midnight),
        },
    }


def build_honest_hourly_payload(rep: Dict[str, Any], kind: str = "hourly") -> Dict[str, Any]:
    mm = rep.get("WOBIMM4_HONEST") or {}
    ms1 = rep.get("MS1_WOBI_HONEST") or {}
    tf1a = rep.get("TF1A_WOBI_HONEST") or {}

    mm_1h = mm.get("stats_1h") or {}
    ms1_1h = ms1.get("stats_1h") or {}
    tf1a_1h = tf1a.get("stats_1h") or {}

    mm_mid = mm.get("stats_since_midnight") or {}
    ms1_mid = ms1.get("stats_since_midnight") or {}
    tf1a_mid = tf1a.get("stats_since_midnight") or {}

    day_bp = (
        float(mm_mid.get("pnl_bp") or 0.0) +
        float(ms1_mid.get("pnl_bp") or 0.0) +
        float(tf1a_mid.get("pnl_bp") or 0.0)
    )
    color = 0x2ECC71 if day_bp >= 0 else 0xE67E22
    title = {
        "start": "[START] HONEST 3戦略並走 1h + 0:00累積",
        "hourly": f"[HOURLY] HONEST 3戦略並走 · {rep.get('ts_jst')}",
        "final": "[FINAL] HONEST 3戦略並走",
    }.get(kind, f"[HONEST] {kind}")

    return {
        "username": "HONEST 3-Strategy Reporter",
        "embeds": [
            {
                "title": title,
                "description": (
                    f"BOARD_IMITATION fill · WIRE=NO · 累積起点 `{rep.get('midnight_jst')}`\n"
                    f"MM=`{mm.get('position')}` · MS1=`{ms1.get('position')}` · TF1A=`{tf1a.get('position')}`"
                ),
                "color": color,
                "fields": [
                    {
                        "name": "直近 1時間 (1h)",
                        "value": (
                            f"{_fmt_block('WOBIMM4_HONEST (MM)', mm_1h)}\n"
                            f"{_fmt_block('MS1_WOBI_HONEST (Micro Scalp)', ms1_1h)}\n"
                            f"{_fmt_block('TF1A_WOBI_HONEST (Pure TF)', tf1a_1h)}"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "0:00 JST からの累積 (Daily)",
                        "value": (
                            f"{_fmt_block('WOBIMM4_HONEST (MM)', mm_mid)}\n"
                            f"{_fmt_block('MS1_WOBI_HONEST (Micro Scalp)', ms1_mid)}\n"
                            f"{_fmt_block('TF1A_WOBI_HONEST (Pure TF)', tf1a_mid)}\n"
                            f"• **3戦略合算**: **`{day_bp:+.2f} bp`**"
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
