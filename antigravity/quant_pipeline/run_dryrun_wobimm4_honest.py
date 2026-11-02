#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WOBIMM4_HONEST — UKI one-sided + board-imitation fills (WIRE=NO).

Two-sided. Same public_maker_fill as LIVE (PUBLIC_OID → BOOK_CROSS → BOARD_TRADE).
BOARD_TRADE books on the print tick (the print is consumed; next snap is MISS).
ENTER BOOK_CROSS is still refused. PREDICT_MISS pulls a stale CROSS wait.
Not LIVE.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

os.environ["ENABLE_REAL_TRADING"] = "false"

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

from antigravity.quant_pipeline.run_dryrun_wobimm4 import (  # noqa: E402
    WOBIMM4DryRun,
    _log,
    board_predicts_fill,
    build_wobimm4_discord_payload,
)
from antigravity.quant_pipeline.wobimm4_policy import (  # noqa: E402
    BUY,
    SELL,
    signed_signal,
)

OUT_DIR = AG / "data" / "mm_research" / "wobimm4_honest_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)


class WOBIMM4HonestDryRun(WOBIMM4DryRun):
    LANE_NAME = "WOBIMM4_HONEST"
    FILL_MODEL = "BOARD_IMITATION"
    LONG_TIGHT = False
    BUY_ONLY = False
    INSTANT_HARVEST = True
    STATE_FILE = AG / "data" / "dryrun_wobimm4_honest_state.json"
    PID_FILE = AG / "data" / "dryrun_wobimm4_honest.pid"
    LEDGER_FILE = OUT_DIR / "paired_ledger.jsonl"
    AUDIT_FILE = OUT_DIR / "audit.jsonl"
    STOP_FILE = AG / "data" / "WOBIMM4_HONEST_DRYRUN_STOP.flag"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._last_hb = 0.0

    def _maybe_fill(self, execs, bid: float, ask: float) -> None:
        """BOARD_TRADE is the print in hand. Waiting a snap deletes it.

        2.9h after CSR-664: PREDICT BOARD_TRADE 51/51 MISS, CROSS skipped,
        closes 0. Book the uncrossed print now. CROSS ENTER still waits
        then confirm_book_cross. EXIT CROSS still confirms.
        """
        if self._confirm_predicted_fill(execs, bid, ask):
            return
        if not self.working_side or self._pending_fill:
            return
        src = board_predicts_fill(self.working_side, self.working_px, bid, ask, execs)
        if not src:
            return
        if src == "BOARD_TRADE":
            if self.net == 0:
                tape = getattr(self, "_tape", None) or {}
                live, live_why = signed_signal(
                    float(tape.get("wobi") or 0.0),
                    float(tape.get("recent_ofi") or 0.0),
                )
                want = BUY if str(self.working_side).upper() == "BUY" else SELL
                if live != 0 and live != want:
                    _log(
                        f"[{self.lane_name}] CONFIRM_SKIP {self.working_side} @ "
                        f"{self.working_px:,.0f} ev_gone {live_why}"
                    )
                    self._pull("confirm_ev_gone")
                    return
            _log(
                f"[{self.lane_name}] CONFIRM {self.working_side} @ "
                f"{self.working_px:,.0f} src=BOARD_TRADE same-tick"
            )
            self._apply_fill(self.working_side, self.working_px, src)
            return
        self._predict_fill(src)

    def _confirm_predicted_fill(self, execs, bid: float, ask: float) -> bool:
        """Venue miss → drop stale px. ENTER confirm needs live EV.

        Booking a crossed ENTER when the confirm tape is already ev_gone
        was the −3bp round-trip. EXIT still fills — flatten must complete.
        No new bp gate.
        """
        pend = self._pending_fill
        if pend and self.net == 0:
            src = board_predicts_fill(str(pend["side"]), float(pend["px"]), bid, ask, execs)
            if src:
                tape = getattr(self, "_tape", None) or {}
                live, live_why = signed_signal(
                    float(tape.get("wobi") or 0.0),
                    float(tape.get("recent_ofi") or 0.0),
                )
                want = BUY if str(pend["side"]).upper() == "BUY" else SELL
                # ofi=0 / NO_EDGE is silent, not opposite. Only skip invert.
                # Skipping NO_EDGE ate 68/74 confirms overnight (3 closes / 9.7h).
                if live != 0 and live != want:
                    _log(
                        f"[{self.lane_name}] CONFIRM_SKIP {pend['side']} @ "
                        f"{float(pend['px']):,.0f} ev_gone {live_why}"
                    )
                    self._pending_fill = None
                    self._pull("confirm_ev_gone")
                    return False
                pred_src = str(pend.get("src") or "")
                if src == "BOOK_CROSS" or pred_src == "BOOK_CROSS":
                    _log(
                        f"[{self.lane_name}] CONFIRM_SKIP {pend['side']} @ "
                        f"{float(pend['px']):,.0f} book_cross pred={pred_src} src={src}"
                    )
                    self._pending_fill = None
                    self._pull("confirm_book_cross")
                    return False
        if super()._confirm_predicted_fill(execs, bid, ask):
            return True
        if (
            pend
            and self.working_side == pend.get("side")
            and abs(self.working_px - float(pend.get("px") or 0.0)) <= 1e-9
        ):
            self._pull("predict_miss")
        return False

    def _tick(self, snap, now: float) -> None:
        super()._tick(snap, now)
        if now - self._last_hb < 60.0:
            return
        self._last_hb = now
        _log(
            f"[{self.LANE_NAME}] hb net={self.net} working={self.working_side or 'FLAT'} "
            f"sig={self.sig_confirmed} posts={self.posts} fills={self.fills} "
            f"cancels={self.cancels} flatten_waits={self.flatten_waits}"
        )

    def send_discord_report(self, kind: str = "hourly") -> None:
        import json
        import urllib.request

        from antigravity.quant_pipeline.mm_discord import MM_DISCORD_WEBHOOK_URL

        m = self.metrics()
        payload = build_wobimm4_discord_payload(m, kind)
        payload["username"] = "WOBIMM4_HONEST"
        if payload.get("embeds"):
            payload["embeds"][0]["title"] = str(payload["embeds"][0]["title"]).replace(
                "WOBIMM4", "WOBIMM4_HONEST"
            )
            payload["embeds"][0]["description"] = (
                "MM HONEST · same-tick BOARD_TRADE · no CROSS ENTER · harvest ask-1 · "
                "WIRE=NO. Not LIVE. Not CSR-635."
            )
            payload["embeds"][0]["footer"] = {
                "text": f"Antigravity Quants · WOBIMM4_HONEST · {m.get('ts_jst', '')}"
            }
        try:
            req = urllib.request.Request(
                MM_DISCORD_WEBHOOK_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                _log(f"[DISCORD] MM HONEST {kind} sent (HTTP {resp.status})")
        except Exception as exc:
            _log(f"[DISCORD] Warning: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="WOBIMM4_HONEST paper (WIRE=NO · two-sided · board-imitation)")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    parser.add_argument("--report-interval", type=float, default=3600.0)
    args = parser.parse_args()
    runner = WOBIMM4HonestDryRun(
        interval_sec=args.interval,
        market_source=args.source,
        report_interval_sec=args.report_interval,
    )

    def _sig(signum, _frame):
        _log(f"signal {signum}")
        runner.stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
