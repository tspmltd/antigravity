#!/usr/bin/env python3
"""CSR-619 — watch 20-trade sets; auto-continue on PASS up to 5. No .env write."""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

AG = Path("/home/azureuser/antigravity")
LOG = AG / "logs" / "tf2bp_micro_live.log"
STOP = AG / "data" / "TF2BP_LIVE_STOP.flag"
GO2 = AG / "data" / "GO2_TF2BP.flag"
STATUS = AG / "data" / "tf2bp_retest_campaign.json"
WEBHOOK = AG / "data" / "tf2bp_retest_webhook.url"
NEEDLE = "antigravity.quant_pipeline.run_micro_live_soak_csr574"
FAIL_KEYS = (
    "will retry",
    "MARGIN_INSUFFICIENT",
    "UNEXPECTED_EXPOSURE",
    "heartbeat stall",
    "kill_on_heartbeat",
    "SAFE_FLATTEN",
    "auto flatten",
)
JST = timezone(timedelta(hours=9))


def _jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z")


def _discord(text: str) -> None:
    try:
        url = WEBHOOK.read_text(encoding="utf-8").strip()
    except Exception:
        return
    if not url.startswith("https://discord.com/api/webhooks/"):
        return
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps({"content": text[:1900]}, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "TF2BP-Retest/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
    except Exception as exc:
        print(f"[SUPER] discord warn: {exc}", flush=True)


def _set_n() -> int:
    try:
        for ln in GO2.read_text(encoding="utf-8").splitlines():
            if ln.startswith("set="):
                return int(ln.split("=", 1)[1])
    except Exception:
        pass
    return 1


def _write_go2(n: int) -> None:
    GO2.write_text(
        "\n".join(
            [
                "GO2 TF2BP",
                "status=ACTIVE",
                f"set={n}",
                "max_trades=20",
                "size=0.001",
                "csr=619",
                f"issued_jst={_jst()}",
                "Economic_PASS=NO",
                "auto_continue=PASS_ONLY",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _soak_alive() -> bool:
    try:
        out = subprocess.check_output(["ps", "-eo", "args"], text=True, errors="replace")
    except Exception:
        return False
    return any(NEEDLE in ln and "python" in ln for ln in out.splitlines())


def _review(mark: int) -> dict:
    text = ""
    if LOG.exists():
        raw = LOG.read_bytes()
        text = raw[mark:].decode("utf-8", errors="replace")
    fails = [k for k in FAIL_KEYS if k in text]
    stop_txt = STOP.read_text(encoding="utf-8") if STOP.exists() else ""
    max20 = "set_complete max_trades" in stop_txt
    closed = text.count("[TF2BP] EXIT #")
    verdict = "PASS" if max20 and not fails else "HOLD"
    if fails:
        verdict = "FAIL"
    return {
        "verdict": verdict,
        "max_trades_done": max20,
        "exit_lines": closed,
        "fails": sorted(set(fails)),
        "stop": stop_txt.strip(),
    }


def _write_status(row: dict) -> None:
    STATUS.write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _start_unit() -> None:
    subprocess.check_call(["systemctl", "--user", "start", "tf2bp-micro-live.service"])


def main() -> int:
    print(f"[SUPER] watch start {_jst()}", flush=True)
    mark = LOG.stat().st_size if LOG.exists() else 0
    last = ""
    while True:
        n = _set_n()
        alive = _soak_alive()
        stop = STOP.exists()
        row = {
            "ts_jst": _jst(),
            "set": n,
            "alive": alive,
            "stop": stop,
            "phase": "running" if alive else "idle",
        }
        _write_status(row)
        if (not alive) and stop and STOP.read_text(encoding="utf-8").startswith("set_complete"):
            rev = _review(mark)
            print(f"SET_REVIEW set={n} {rev['verdict']} {rev}", flush=True)
            comment = (
                f"TF2BP SET-{n} 終了 採点={rev['verdict']} "
                f"closes={rev['exit_lines']} max20={rev['max_trades_done']} "
                f"fails={rev['fails'] or 'none'} stop={rev['stop']}"
            )
            (AG / "data" / "tf2bp_retest_chat_comment.txt").write_text(
                comment + "\n", encoding="utf-8"
            )
            _discord(
                f"TF2BP SET-{n} 採点 {rev['verdict']} exits={rev['exit_lines']} "
                f"max20={rev['max_trades_done']} fails={rev['fails'] or 'none'}"
            )
            _write_status({**row, "phase": "reviewed", "review": rev})
            if rev["verdict"] == "PASS" and n < 5:
                nxt = n + 1
                _write_go2(nxt)
                STOP.unlink(missing_ok=True)
                mark = LOG.stat().st_size if LOG.exists() else 0
                _start_unit()
                _discord(f"TF2BP SET-{nxt} 自動継続（前セット PASS）")
                print(f"[SUPER] auto-continue SET-{nxt}", flush=True)
                time.sleep(8)
                continue
            print(f"[SUPER] stop campaign verdict={rev['verdict']} set={n}", flush=True)
            return 0 if rev["verdict"] == "PASS" else 1
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
