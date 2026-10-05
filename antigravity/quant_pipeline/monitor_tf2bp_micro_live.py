#!/usr/bin/env python3
"""TF2BP micro LIVE ops monitor.

- Follows logs/tf2bp_micro_live.log
- Rolling 1h realized PnL in bp; Discord ALERT if <= -100bp
- Process death / session-done alerts
- Periodic heartbeat to LIVE webhook

Does not place orders. Does not flip ENABLE_REAL_TRADING.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
import sys

sys.path.insert(0, str(AG))

from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier  # noqa: E402

JST = timezone(timedelta(hours=9))
LOG_PATH = AG / "logs" / "tf2bp_micro_live.log"
PID_PATH = AG / "data" / "tf2bp_micro_live.pid"
STATE_PATH = AG / "data" / "tf2bp_micro_live_monitor_state.json"
SNAP_PATH = AG / "data" / "tf2bp_micro_live_monitor_latest.json"

ENTRY_RE = re.compile(r"\[TF2BP\] ENTRY BUY @ ([\d,]+)")
EXIT_RE = re.compile(
    r"\[TF2BP\] (?:EXIT #(\d+)/(?:\d+|∞)|FLATTEN) pnl=([+-]?\d+(?:\.\d+)?)"
    r"(?: sum=([+-]?\d+(?:\.\d+)?))?"
)
START_RE = re.compile(r"CSR-574 micro soak\s+ts=([^\s]+)\s+real=(True|False)")
DONE_RE = re.compile(r"DONE TF2BP_v1|sequence_done|Economic_PASS")

DEFAULT_SIZE = 0.001
LOSS_BP_1H = -100.0
WINDOW_SEC = 3600.0
ALERT_COOLDOWN_SEC = 900.0
HEARTBEAT_SEC = 600.0
POLL_SEC = 2.0


def _jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z")


def _parse_start_epoch(ts_raw: str) -> Optional[float]:
    try:
        # e.g. 2026-10-05T15:36:23+09:00
        dt = datetime.fromisoformat(ts_raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        return dt.timestamp()
    except Exception:
        return None


def _pnl_to_bp(pnl_jpy: float, entry: float, size: float) -> float:
    notional = entry * size
    if notional <= 0:
        return 0.0
    return pnl_jpy / notional * 10_000.0


def _load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "exits": [],
        "byte_offset": 0,
        "last_alert_ts": 0.0,
        "last_heartbeat_ts": 0.0,
        "last_death_alert_ts": 0.0,
        "session_start_ts": None,
        "armed_notified": False,
        "session_done_notified": False,
    }


def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def _rolling_1h(exits: List[Dict[str, Any]], now: float) -> Tuple[float, float, int]:
    cut = now - WINDOW_SEC
    rows = [e for e in exits if float(e.get("ts", 0)) >= cut]
    bp = sum(float(e.get("pnl_bp", 0)) for e in rows)
    jpy = sum(float(e.get("pnl_jpy", 0)) for e in rows)
    return bp, jpy, len(rows)


def _pid_alive() -> Tuple[bool, Optional[int]]:
    if not PID_PATH.exists():
        return False, None
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip().splitlines()[0])
    except Exception:
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except OSError:
        return False, pid


def _discord_content(title: str, body: str) -> Dict[str, Any]:
    return {"content": f"**{title}**\n```\n{body}\n```"}


class Monitor:
    def __init__(
        self,
        *,
        size: float,
        loss_bp: float,
        dry_discord: bool,
    ) -> None:
        self.size = size
        self.loss_bp = loss_bp
        self.dry_discord = dry_discord
        self.notifier = QuantDiscordNotifier()
        self.state = _load_state()
        self.pending_entry: Optional[float] = None
        self._bootstrap_from_log()

    def _send_alert(self, title: str, body: str) -> bool:
        payload = _discord_content(title, body)
        if self.dry_discord:
            print(f"[DRY] ALERT {title}\n{body}", flush=True)
            return True
        ok_a = self.notifier.post_alert(payload)
        # also LIVE channel for ops visibility
        ok_l = self.notifier._post(self.notifier.live_webhook_url, payload)
        return ok_a or ok_l

    def _send_live(self, title: str, body: str) -> bool:
        payload = _discord_content(title, body)
        if self.dry_discord:
            print(f"[DRY] LIVE {title}\n{body}", flush=True)
            return True
        return self.notifier._post(self.notifier.live_webhook_url, payload)

    def _bootstrap_from_log(self) -> None:
        """Parse existing log once; stamp unknown exits near session start."""
        if not LOG_PATH.exists():
            return
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
        offset = len(text.encode("utf-8"))
        # If we already tracked past this file length, keep state
        if int(self.state.get("byte_offset") or 0) >= offset and self.state.get("exits"):
            return
        session_start = None
        for m in START_RE.finditer(text):
            session_start = _parse_start_epoch(m.group(1))
        if session_start:
            self.state["session_start_ts"] = session_start
        # Rebuild exits from full log for current session (idempotent by n)
        exits: List[Dict[str, Any]] = []
        pending_entry: Optional[float] = None
        base_ts = float(session_start or time.time())
        for i, line in enumerate(text.splitlines()):
            em = ENTRY_RE.search(line)
            if em:
                pending_entry = float(em.group(1).replace(",", ""))
                continue
            xm = EXIT_RE.search(line)
            if not xm:
                continue
            n = int(xm.group(1) or (len(exits) + 1))
            pnl = float(xm.group(2))
            entry = pending_entry or 13_500_000.0
            pending_entry = None
            # Spread exits across elapsed wall from session start to now
            frac = (i + 1) / max(len(text.splitlines()), 1)
            ts = min(time.time(), base_ts + frac * max(1.0, time.time() - base_ts))
            exits.append(
                {
                    "n": n,
                    "ts": ts,
                    "pnl_jpy": pnl,
                    "pnl_bp": round(_pnl_to_bp(pnl, entry, self.size), 3),
                    "entry": entry,
                }
            )
        self.state["exits"] = exits
        self.state["byte_offset"] = offset
        self.pending_entry = pending_entry
        _save_state(self.state)

    def _ingest_chunk(self, chunk: str) -> None:
        now = time.time()
        for line in chunk.splitlines():
            sm = START_RE.search(line)
            if sm:
                self.state["session_start_ts"] = _parse_start_epoch(sm.group(1))
                self.state["session_done_notified"] = False
            em = ENTRY_RE.search(line)
            if em:
                self.pending_entry = float(em.group(1).replace(",", ""))
            xm = EXIT_RE.search(line)
            if xm:
                n = int(xm.group(1) or (len(self.state["exits"]) + 1))
                pnl = float(xm.group(2))
                entry = self.pending_entry or 13_500_000.0
                self.pending_entry = None
                row = {
                    "n": n,
                    "ts": now,
                    "pnl_jpy": pnl,
                    "pnl_bp": round(_pnl_to_bp(pnl, entry, self.size), 3),
                    "entry": entry,
                }
                # replace same n if re-read
                self.state["exits"] = [e for e in self.state["exits"] if int(e.get("n", -1)) != n]
                self.state["exits"].append(row)
                print(
                    f"[monitor] EXIT #{n} pnl={pnl:+.1f}JPY ({row['pnl_bp']:+.2f}bp)",
                    flush=True,
                )
            if DONE_RE.search(line) and not self.state.get("session_done_notified"):
                self._notify_session_done()
                self.state["session_done_notified"] = True

    def _notify_session_done(self) -> None:
        bp, jpy, n = _rolling_1h(self.state["exits"], time.time())
        total_jpy = sum(float(e["pnl_jpy"]) for e in self.state["exits"])
        total_bp = sum(float(e["pnl_bp"]) for e in self.state["exits"])
        body = (
            f"ts={_jst()}\n"
            f"session_exits={len(self.state['exits'])} total={total_jpy:+.1f}JPY ({total_bp:+.1f}bp)\n"
            f"rolling_1h={jpy:+.1f}JPY ({bp:+.1f}bp) n={n}\n"
            f"Economic_PASS=NO · Stage-0 soak complete/ended"
        )
        self._send_live("TF2BP LIVE session end", body)

    def _check_loss_alert(self) -> None:
        now = time.time()
        bp, jpy, n = _rolling_1h(self.state["exits"], now)
        if bp > self.loss_bp:
            return
        last = float(self.state.get("last_alert_ts") or 0)
        if now - last < ALERT_COOLDOWN_SEC:
            return
        body = (
            f"ts={_jst()}\n"
            f"TRIGGER: rolling 1h PnL <= {self.loss_bp:.0f}bp\n"
            f"rolling_1h={bp:+.2f}bp ({jpy:+.1f}JPY) trades={n}\n"
            f"session_exits={len(self.state['exits'])}\n"
            f"pid_alive={_pid_alive()[0]}\n"
            f"action: ops review · kill if needed"
        )
        if self._send_alert("🚨 TF2BP LIVE 1h loss ≥100bp", body):
            self.state["last_alert_ts"] = now
            print(f"[monitor] ALERT sent rolling_1h={bp:+.2f}bp", flush=True)

    def _heartbeat(self) -> None:
        now = time.time()
        last = float(self.state.get("last_heartbeat_ts") or 0)
        if now - last < HEARTBEAT_SEC:
            return
        alive, pid = _pid_alive()
        bp, jpy, n = _rolling_1h(self.state["exits"], now)
        total_jpy = sum(float(e["pnl_jpy"]) for e in self.state["exits"])
        total_bp = sum(float(e["pnl_bp"]) for e in self.state["exits"])
        open_pos = self.pending_entry is not None
        body = (
            f"ts={_jst()}\n"
            f"alive={alive} pid={pid}\n"
            f"open_entry={open_pos}\n"
            f"closed={len(self.state['exits'])}/10\n"
            f"session={total_jpy:+.1f}JPY ({total_bp:+.1f}bp)\n"
            f"rolling_1h={jpy:+.1f}JPY ({bp:+.1f}bp) n={n}\n"
            f"alert_thresh={self.loss_bp:.0f}bp/1h"
        )
        snap = {
            "ts_jst": _jst(),
            "alive": alive,
            "pid": pid,
            "open_entry": open_pos,
            "closed": len(self.state["exits"]),
            "session_pnl_jpy": round(total_jpy, 1),
            "session_pnl_bp": round(total_bp, 2),
            "rolling_1h_bp": round(bp, 2),
            "rolling_1h_jpy": round(jpy, 1),
            "rolling_1h_n": n,
            "loss_alert_bp": self.loss_bp,
        }
        SNAP_PATH.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._send_live("TF2BP LIVE monitor", body)
        self.state["last_heartbeat_ts"] = now

    def _check_process(self) -> None:
        alive, pid = _pid_alive()
        if alive:
            return
        # If session already completed with exits==10, death is expected
        if len(self.state.get("exits") or []) >= 10 or self.state.get("session_done_notified"):
            return
        now = time.time()
        last = float(self.state.get("last_death_alert_ts") or 0)
        if now - last < ALERT_COOLDOWN_SEC:
            return
        body = (
            f"ts={_jst()}\n"
            f"TF2BP micro LIVE process not running\n"
            f"stale_pid={pid}\n"
            f"closed_so_far={len(self.state.get('exits') or [])}\n"
            f"check logs/tf2bp_micro_live.log"
        )
        if self._send_alert("🚨 TF2BP LIVE process DOWN", body):
            self.state["last_death_alert_ts"] = now

    def _arm_notice(self) -> None:
        if self.state.get("armed_notified"):
            return
        alive, pid = _pid_alive()
        bp, jpy, n = _rolling_1h(self.state["exits"], time.time())
        body = (
            f"ts={_jst()}\n"
            f"monitor ARMED\n"
            f"alive={alive} pid={pid}\n"
            f"alert: rolling 1h PnL <= {self.loss_bp:.0f}bp → Discord ALERT+LIVE\n"
            f"heartbeat every {int(HEARTBEAT_SEC/60)}m\n"
            f"now rolling_1h={bp:+.1f}bp ({jpy:+.1f}JPY) n={n}"
        )
        if self._send_live("TF2BP LIVE monitor armed", body):
            self.state["armed_notified"] = True
            self.state["last_heartbeat_ts"] = time.time()  # next HB in HEARTBEAT_SEC

    def run(self) -> int:
        print(
            f"[monitor] start log={LOG_PATH} loss_bp={self.loss_bp}/1h size={self.size}",
            flush=True,
        )
        self._arm_notice()
        _save_state(self.state)
        while True:
            if LOG_PATH.exists():
                size = LOG_PATH.stat().st_size
                off = int(self.state.get("byte_offset") or 0)
                if size < off:
                    # rotated / truncated
                    off = 0
                if size > off:
                    with LOG_PATH.open("rb") as f:
                        f.seek(off)
                        raw = f.read()
                    self.state["byte_offset"] = off + len(raw)
                    self._ingest_chunk(raw.decode("utf-8", errors="replace"))
            self._check_loss_alert()
            self._check_process()
            self._heartbeat()
            # prune old exits (>2h keep)
            cut = time.time() - 2 * WINDOW_SEC
            self.state["exits"] = [e for e in self.state["exits"] if float(e.get("ts", 0)) >= cut]
            _save_state(self.state)
            time.sleep(POLL_SEC)


def main() -> int:
    ap = argparse.ArgumentParser(description="TF2BP micro LIVE ops monitor")
    ap.add_argument("--size", type=float, default=DEFAULT_SIZE)
    ap.add_argument("--loss-bp", type=float, default=LOSS_BP_1H, help="alert if rolling 1h pnl_bp <= this")
    ap.add_argument("--dry-discord", action="store_true")
    ap.add_argument("--once", action="store_true", help="one-shot status + alert check then exit")
    args = ap.parse_args()
    mon = Monitor(size=args.size, loss_bp=args.loss_bp, dry_discord=args.dry_discord)
    if args.once:
        mon._arm_notice()
        mon._check_loss_alert()
        mon._check_process()
        mon._heartbeat()
        _save_state(mon.state)
        return 0
    return mon.run()


if __name__ == "__main__":
    raise SystemExit(main())
