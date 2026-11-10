#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WOBIMM4_HONEST — inventory MM + LIVE_PARITY fills (WIRE=NO).

CSR-681: same difficulty as ABS1/VR1/MS/TF LIVE_PARITY.
  - FILL_MODEL=HONEST + STRICT own-size (no BOARD_TRADE same-tick fiction)
  - ADVERSE Soft rest then MARKET_SIM (no free HIT_TOUCH_FORCE)
  - Fee haircut on close (taker+slip on MARKET_SIM)
  - INSTANT_HARVEST unchanged (baseline A/B vs MM5).

Not LIVE. Economic_PASS=NO.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

os.environ["ENABLE_REAL_TRADING"] = "false"

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

from antigravity.quant_pipeline.honest_adverse_kit import (  # noqa: E402
    HonestAdverseKit,
    StrategyProfile,
)
from antigravity.quant_pipeline.live_parity_dryrun import (  # noqa: E402
    EXIT_REST_MAX_SEC,
    count_harvests,
    lane_verdict,
    net_pnl_bp,
)
from antigravity.quant_pipeline.run_dryrun_wobimm4 import (  # noqa: E402
    WOBIMM4DryRun,
    _log,
    build_wobimm4_discord_payload,
)

OUT_DIR = AG / "data" / "mm_research" / "wobimm4_honest_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)


class WOBIMM4HonestDryRun(WOBIMM4DryRun):
    LANE_NAME = "WOBIMM4_HONEST"
    FILL_MODEL = "HONEST"
    HONEST_STRICT_OWN_SIZE = True
    LONG_TIGHT = False
    BUY_ONLY = False
    INSTANT_HARVEST = True
    # CSR-681: Soft rest adverse; escalate MARKET_SIM (no free HIT_TOUCH).
    ADVERSE_HIT_TOUCH = True
    LIVE_PARITY = True
    EXIT_REST_MAX_SEC = EXIT_REST_MAX_SEC
    STATE_FILE = AG / "data" / "dryrun_wobimm4_honest_state.json"
    PID_FILE = AG / "data" / "dryrun_wobimm4_honest.pid"
    LEDGER_FILE = OUT_DIR / "paired_ledger.jsonl"
    AUDIT_FILE = OUT_DIR / "audit.jsonl"
    STOP_FILE = AG / "data" / "WOBIMM4_HONEST_DRYRUN_STOP.flag"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._last_hb = 0.0
        self.ae = HonestAdverseKit(
            lane=self.LANE_NAME,
            save_dir=str(AG / "data"),
            hard_stop_bp=-4.0,
            target_mfe_bp=3.0,
            profile=StrategyProfile.INVENTORY_MM,
            log_fn=_log,
        )
        self.adverse_fast_cuts = 0
        self.adverse_hard_stops = 0
        self.exit_posted_ts = 0.0
        self.parity_market_sim = 0
        self._lane_started_ts = time.time()

    def _ae_track_from_fill(self, side: str, px: float) -> None:
        tape = getattr(self, "_tape", None) or {}
        mid = 0.0
        try:
            bid = float(tape.get("best_bid") or 0.0)
            ask = float(tape.get("best_ask") or 0.0)
            if bid > 0 and ask > 0:
                mid = 0.5 * (bid + ask)
        except Exception:
            mid = float(px)
        spread_bp = float(tape.get("spread_bp") or 0.0)
        self.ae.track_entry(
            px=float(px),
            now=__import__("time").time(),
            side=side.lower(),
            mid=mid if mid > 0 else float(px),
            spread_bp=spread_bp,
            meta={
                "lane": self.LANE_NAME,
                "wire": "NO",
                "wobi": float(tape.get("wobi") or 0.0),
                "ofi": float(tape.get("recent_ofi") or 0.0),
                "spread_bp": spread_bp,
                "research": "mm4_phase2_hit_touch+inventory_mm",
                "adverse_profile": StrategyProfile.INVENTORY_MM.value,
            },
        )

    def _apply_fill(self, side: str, px: float, src: str) -> None:
        pre_net = int(self.net)
        super()._apply_fill(side, px, src)
        if pre_net == 0 and self.net != 0:
            self._ae_track_from_fill(side, px)

    def _ae_path_override(
        self, *, snap, now: float, bid: float, ask: float, mid: float
    ) -> Optional[Tuple[str, Optional[str], str]]:
        """HARD_STOP then path_toxic before policy harvest/adverse_cut."""
        del snap
        if self.net == 0 or self.entry_px <= 0:
            return None
        side = "buy" if self.net > 0 else "sell"
        self.ae.on_tick(mid=mid, now=now, bid=bid, ask=ask, side=side)

        if self.net > 0 and bid > 0:
            unrealized_bp = ((bid / self.entry_px) - 1.0) * 1e4
        elif self.net < 0 and ask > 0:
            unrealized_bp = ((self.entry_px / ask) - 1.0) * 1e4
        elif mid > 0:
            if self.net > 0:
                unrealized_bp = ((mid / self.entry_px) - 1.0) * 1e4
            else:
                unrealized_bp = ((self.entry_px / mid) - 1.0) * 1e4
        else:
            return None

        flatten = "SELL" if self.net > 0 else "BUY"

        hs, hs_why = self.ae.is_hard_stop(unrealized_bp)
        if hs:
            self.adverse_hard_stops += 1
            self._adverse_trigger_bp = float(unrealized_bp)
            self._adverse_trigger_ts = float(now)
            return EXIT, flatten, f"hard_stop ({hs_why})"

        toxic, tw = self.ae.path_toxic()
        if toxic:
            self.adverse_fast_cuts += 1
            self._adverse_trigger_bp = float(unrealized_bp)
            self._adverse_trigger_ts = float(now)
            return EXIT, flatten, f"adverse_fast ({tw})"
        return None

    def _ae_close_path(self, *, exit_px: float, pnl_bp: float, why: str) -> Dict[str, Any]:
        return self.ae.close(exit_px=exit_px, pnl_bp=pnl_bp, reason=why)

    def _maybe_fill(self, execs, bid: float, ask: float) -> None:
        """LIVE_PARITY: strict HONEST queue (no BOARD_TRADE same-tick fiction)."""
        WOBIMM4DryRun._maybe_fill(self, execs, bid, ask)

    def _hit_touch_force_src(self, why: str) -> str:
        """CSR-681: force path is MARKET_SIM (fee+slip), not free HIT_TOUCH."""
        del why
        return "MARKET_SIM"

    def _close_ledger(self, exit_px: float, src: str, why: str) -> None:
        if self.entry_px <= 0 or self.net == 0:
            return
        if self.net > 0:
            gross = ((exit_px / self.entry_px) - 1.0) * 1e4
        else:
            gross = ((self.entry_px / exit_px) - 1.0) * 1e4
        net, fee = net_pnl_bp(float(gross), str(src))
        # Temporarily book via parent math by patching after — call parent with
        # adjusted entry so parent computes net? Simpler: inline close with fee.
        from antigravity.quant_pipeline.run_dryrun_wobimm4 import SIZE_BTC, _append

        pnl_j = net * SIZE_BTC * self.entry_px / 1e4
        self.pnl_bp += net
        self.pnl_jpy += pnl_j
        self.trades += 1
        if net > 0:
            self.wins += 1
        row: Dict[str, Any] = {
            "ts": time.time(),
            "lane": self.lane_name,
            "action": "CLOSE_LONG" if self.net > 0 else "COVER_SHORT",
            "price": exit_px,
            "size": SIZE_BTC,
            "pnl_bp": round(net, 2),
            "pnl_bp_gross": round(float(gross), 2),
            "pnl_bp_net": round(net, 2),
            "fee_bp": round(fee, 2),
            "pnl_jpy": round(pnl_j, 1),
            "is_win": net > 0,
            "source": src,
            "exit_reason": why,
            "entry_src": self.entry_src or "",
            "hold_s": round(time.time() - self.entry_ts, 1) if self.entry_ts else None,
            "live_parity": True,
            "fill_model": str(self.FILL_MODEL),
        }
        trig = getattr(self, "_adverse_trigger_bp", None)
        t_ts = float(getattr(self, "_adverse_trigger_ts", 0.0) or 0.0)
        if trig is not None and (
            str(why).startswith("adverse_cut")
            or str(why).startswith("adverse_fast")
            or str(why).startswith("hard_stop")
        ):
            row["adverse_trigger_bp"] = round(float(trig), 3)
            row["adverse_fill_bp"] = round(net, 3)
            row["adverse_overrun_bp"] = round(net - float(trig), 3)
            if t_ts > 0:
                row["adverse_lag_s"] = round(time.time() - t_ts, 3)
        ae_path = getattr(self, "_ae_close_path", None)
        if callable(ae_path):
            try:
                row.update(ae_path(exit_px=exit_px, pnl_bp=net, why=why) or {})
            except Exception:
                pass
        _append(self.ledger_path, row)
        _log(
            f"[{self.lane_name}] CLOSE pnl={net:+.2f}bp "
            f"(gross={gross:+.2f} fee={fee:.2f}) src={src} ({why})"
        )
        if why.startswith("adverse_cut"):
            self.adverse_cuts += 1
        self.history.append(
            {
                "ts": time.time(),
                "pnl_bp": round(net, 2),
                "pnl_jpy": round(pnl_j, 1),
                "is_win": net > 0,
            }
        )
        self.net = 0
        self.entry_px = 0.0
        self.entry_src = ""
        self.entry_ts = 0.0
        self._cover_waiting = False
        self._flatten_waiting = False
        self._adverse_trigger_bp = None
        self._adverse_trigger_ts = 0.0
        self.exit_posted_ts = 0.0

    def metrics(self):
        m = super().metrics()
        m["model"] = self.LANE_NAME
        m["adverse_hit_touch"] = bool(self.ADVERSE_HIT_TOUCH)
        m["adverse_profile"] = self.ae.profile.value
        m["adverse_fast_cuts"] = int(getattr(self, "adverse_fast_cuts", 0))
        m["adverse_hard_stops"] = int(getattr(self, "adverse_hard_stops", 0))
        m["fill_model"] = str(self.FILL_MODEL)
        m["honest_strict_own_size"] = bool(
            getattr(self, "HONEST_STRICT_OWN_SIZE", False)
        )
        rows = list(getattr(self, "history", []) or [])
        started = float(getattr(self, "_lane_started_ts", time.time()) or time.time())
        verd = lane_verdict(
            n=len(rows),
            harvest_ge_min=count_harvests(rows),
            window_sec=max(0.0, time.time() - started),
            net_pnl_bp_sum=sum(float(r.get("pnl_bp") or 0.0) for r in rows),
        )
        m["parity"] = {
            "live_parity": True,
            "fill_model": str(self.FILL_MODEL),
            "force_src": "MARKET_SIM",
            "verdict": verd,
        }
        m["note"] = (
            "MM4 LIVE_PARITY · HONEST STRICT · Soft→MARKET_SIM + fee · WIRE=NO"
        )
        return m

    def _tick(self, snap, now: float) -> None:
        super()._tick(snap, now)
        if now - self._last_hb < 60.0:
            return
        self._last_hb = now
        pm = (self.metrics().get("parity") or {})
        vd = pm.get("verdict") or {}
        _log(
            f"[{self.LANE_NAME}] hb net={self.net} working={self.working_side or 'FLAT'} "
            f"sig={self.sig_confirmed} posts={self.posts} fills={self.fills} "
            f"cancels={self.cancels} flatten_waits={self.flatten_waits} "
            f"ae_fast={getattr(self, 'adverse_fast_cuts', 0)} "
            f"hard_stop={getattr(self, 'adverse_hard_stops', 0)} "
            f"fill={self.FILL_MODEL} strict={bool(getattr(self, 'HONEST_STRICT_OWN_SIZE', False))} "
            f"verdict={vd.get('verdict')} profile={self.ae.profile.value}"
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
            vd = ((m.get("parity") or {}).get("verdict") or {})
            payload["embeds"][0]["description"] = (
                "MM4 LIVE_PARITY · HONEST STRICT · Soft→MARKET_SIM + fee · WIRE=NO\n"
                f"verdict=`{vd.get('verdict')}` · Economic_PASS=`{vd.get('economic_pass')}`"
            )
            payload["embeds"][0]["footer"] = {
                "text": f"Antigravity Quants · WOBIMM4 LIVE_PARITY · {m.get('ts_jst', '')}"
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
    parser = argparse.ArgumentParser(
        description="WOBIMM4_HONEST paper (WIRE=NO · Phase2 HIT_TOUCH adverse)"
    )
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
