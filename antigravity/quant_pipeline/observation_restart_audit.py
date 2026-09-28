#!/usr/bin/env python3
"""CSR-543 P0-Ops — Observation runner restart audit.

Parses dryrun log for start/interrupt/fatal cycles. Distinguishes wall-clock 24h
vs process uptime. Does not restart services by itself.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "logs" / "dryrun_umm_tf2bp_24h.log"
OUT = ROOT / "data" / "mm_research" / "daily"
LEDGER = ROOT / "logs" / "observation_run_ledger.jsonl"
JST = timezone(timedelta(hours=9))

START_RE = re.compile(r"連続Dry-run 観測ランナー")
INTERRUPT_RE = re.compile(r"ユーザー中断|予期せぬエラー|FATAL_ERROR")
TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
EXIT_REASON_BUG = re.compile(r"unexpected keyword argument 'exit_reason'")


def _parse_log(max_bytes: int = 4_000_000) -> Dict[str, Any]:
    if not LOG.exists():
        return {"available": False}
    size = LOG.stat().st_size
    with LOG.open("rb") as f:
        f.seek(max(0, size - max_bytes))
        chunk = f.read().decode("utf-8", errors="replace")
    starts: List[str] = []
    interrupts: List[Dict[str, str]] = []
    exit_reason_bugs = 0
    last_ts = None
    for line in chunk.splitlines():
        mts = TS_RE.search(line)
        if mts:
            last_ts = mts.group(1)
        if START_RE.search(line):
            starts.append(last_ts or "unknown")
        if INTERRUPT_RE.search(line):
            kind = "user_interrupt" if "ユーザー中断" in line else (
                "fatal" if "FATAL" in line else "unexpected"
            )
            interrupts.append({"ts": last_ts or "unknown", "kind": kind, "snippet": line[:160]})
        if EXIT_REASON_BUG.search(line):
            exit_reason_bugs += 1
    return {
        "available": True,
        "log_bytes_scanned": min(size, max_bytes),
        "start_count_in_tail": len(starts),
        "interrupt_count_in_tail": len(interrupts),
        "starts_tail": starts[-20:],
        "interrupts_tail": interrupts[-30:],
        "exit_reason_kwarg_bug_hits": exit_reason_bugs,
        "root_cause_primary": (
            "record_trade(exit_reason=...) passed to TF2BP/PEG without kwargs support"
            if exit_reason_bugs
            else "see interrupts_tail"
        ),
    }


def _live_process() -> Dict[str, Any]:
    # Best-effort: parse /proc for dryrun pid
    import subprocess
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "run_dryrun_umm_tf2bp_24h"], text=True
        ).strip().splitlines()
    except Exception:
        out = []
    rows = []
    for line in out:
        if "pgrep" in line or "rg " in line:
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        pid = int(parts[0])
        try:
            start = float(Path(f"/proc/{pid}/stat").read_text().split()[21])  # not wall clock
        except Exception:
            start = None
        # use etime via ps
        try:
            et = subprocess.check_output(
                ["ps", "-o", "etime=", "-p", str(pid)], text=True
            ).strip()
            lstart = subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(pid)], text=True
            ).strip()
        except Exception:
            et, lstart = None, None
        rows.append({
            "pid": pid,
            "lstart": lstart,
            "etime": et,
            "cmd": parts[1][:120] if len(parts) > 1 else "",
        })
    return {"processes": rows, "n": len(rows)}


def build_run_record(
    *,
    restart_reason: str,
    data_continuity_ok: Optional[bool] = None,
    pnl_ledger_continuity_ok: Optional[bool] = None,
) -> Dict[str, Any]:
    live = _live_process()
    pid = live["processes"][0]["pid"] if live["processes"] else None
    return {
        "run_id": f"obs_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}_{pid or 'nopid'}",
        "process_start_ts": live["processes"][0]["lstart"] if live["processes"] else None,
        "process_end_ts": None,
        "restart_reason": restart_reason,
        "pid": pid,
        "data_continuity_ok": data_continuity_ok,
        "pnl_ledger_continuity_ok": pnl_ledger_continuity_ok,
        "schema_version": "csr543_obs_run_v1",
        "wall_clock_note": "24h Economic eval must use wall-clock window, not process uptime",
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def append_ledger(rec: Dict[str, Any]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    audit = {
        "csr": 543,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Observation restart audit (P0-Ops)",
        "log_audit": _parse_log(),
        "live": _live_process(),
        "required_fields": [
            "run_id", "process_start_ts", "process_end_ts", "restart_reason",
            "PID", "data_continuity_ok", "pnl_ledger_continuity_ok", "schema_version",
        ],
        "rules": {
            "never_mix_wall_clock_24h_with_process_uptime_24h": True,
            "strategy_research_separate_from_ops_restarts": True,
        },
        "fix_applied": {
            "exit_reason_kwarg": "dryrun record_trade now TypeError-falls-back for non-UMM strategies",
        },
    }
    rec = build_run_record(
        restart_reason="audit_snapshot_after_exit_reason_fix",
        data_continuity_ok=False,  # thrash → continuity suspect until proven
        pnl_ledger_continuity_ok=False,
    )
    append_ledger(rec)
    audit["current_run_record"] = rec
    path = OUT / "2026-09-28_csr543_observation_restart_audit.json"
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "ledger": str(LEDGER),
        "starts_tail": audit["log_audit"].get("start_count_in_tail"),
        "exit_reason_bugs": audit["log_audit"].get("exit_reason_kwarg_bug_hits"),
        "live_pids": [p["pid"] for p in audit["live"]["processes"]],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
