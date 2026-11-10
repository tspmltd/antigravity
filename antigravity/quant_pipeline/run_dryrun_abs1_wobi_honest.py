#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ABS1_WOBI_HONEST — Absent Scalp 1 / 居ないスキャル (WIRE=NO).

Post CSR-677 design (thin passive +1bp MM = no EV):
  - Refuse ENTER when book is thin (MIN_ENTRY_SPREAD_BP) — absent from
    the regime where WOBIMM5 tried to farm harvest.
  - Hard MAX_HOLD_SEC even under EV_HOLD — leave the board (ABSENT_DWELL).
  - Stricter never-green DYN_STOP; EV_GONE / dwell → HIT_BID_FORCE.
  - No Position-EV opposite-maker harvest rest. No LIVE. Economic_PASS=NO.

A/B vs MS2_WOBI_HONEST (same HONEST fill stack).
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

os.environ.setdefault("ENABLE_REAL_TRADING", "false")

from antigravity.quant_pipeline.honest_adverse_kit import (  # noqa: E402
    HonestAdverseKit,
    StrategyProfile,
)
from antigravity.quant_pipeline.run_dryrun_ms2_wobi_honest import (  # noqa: E402
    MS2WobiHonestDryRun,
)
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import (  # noqa: E402
    _log,
    calculate_optimal_order_price,
)

OUT_DIR = AG / "data" / "mm_research" / "abs1_wobi_honest_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)

_FORCE_EXIT_TAGS = frozenset({"EV_GONE", "DYN_STOP", "ABSENT_DWELL"})


class ABS1WobiHonestDryRun(MS2WobiHonestDryRun):
    """Absent Scalp: thin-book refuse + hard dwell cut."""

    LANE_NAME = "ABS1_WOBI_HONEST"
    STATE_FILE = AG / "data" / "dryrun_abs1_wobi_honest_state.json"
    PID_FILE = AG / "data" / "dryrun_abs1_wobi_honest.pid"
    LEDGER_FILE = OUT_DIR / "paired_ledger.jsonl"
    AUDIT_FILE = OUT_DIR / "audit_events.jsonl"

    # Thin-book absence (not a loss gate — refuse the failed MM regime).
    MIN_ENTRY_SPREAD_BP = 2.5
    # Hard leave even if live EV still agrees.
    MAX_HOLD_SEC = 6.0
    # Stricter never-green than MS2 (5s / 1.5bp).
    DYN_STOP_ENABLED = True
    DYN_STOP_CHECK_S = 4.0
    DYN_STOP_MFE_TOUCH_BP = 1.0
    # Keep MS2 de-repair: no score veto / no path HS exec.
    ENTRY_VETO_ENABLED = False
    HARD_STOP_ENABLED = False
    HS_FORCE_FILL = True
    POST_CUT_COOLDOWN_SEC = 20.0

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ledger_path = Path(self.LEDGER_FILE)
        self.audit_path = Path(self.AUDIT_FILE)
        self.ae = HonestAdverseKit(
            lane=self.LANE_NAME,
            save_dir=str(AG / "data"),
            hard_stop_bp=float(self.HARD_STOP_BP),
            target_mfe_bp=2.0,
            profile=StrategyProfile.SCALP,
            hs_shield_duration_sec=float(self.HS_SHIELD_DURATION_SEC),
            hs_shield_bp=float(self.HS_SHIELD_BP),
            log_fn=_log,
        )
        self.spread_gate_blocks = 0
        self.absent_dwell_cuts = 0
        self.entry_cooldown_until = 0.0

    def _record_gate(self, m: Dict[str, Any]) -> Tuple[bool, str]:
        ok, reason = super()._record_gate(m)
        if not ok:
            return ok, reason
        now = time.time()
        if now < float(getattr(self, "entry_cooldown_until", 0.0) or 0.0):
            return False, "ABSENT_COOLDOWN"
        sp = float(m.get("spread_bp") or 0.0)
        min_sp = float(self.MIN_ENTRY_SPREAD_BP)
        if sp < min_sp:
            self.spread_gate_blocks = int(getattr(self, "spread_gate_blocks", 0)) + 1
            return False, f"ABSENT_THIN_BOOK (spread={sp:.2f}<{min_sp:.1f})"
        return True, reason

    def _eval_wobi3_dynamic_exit(
        self,
        current_mid: float,
        depth_bids: List[Dict[str, float]],
        depth_asks: List[Dict[str, float]],
        m: Dict[str, Any],
        now: float,
    ) -> Tuple[bool, str, float, str]:
        # Hard dwell first — even EV_HOLD must leave (core ABS1 rule).
        st = self.wobi3_trailing
        entry = float(
            getattr(st, "entry_price", 0.0)
            or getattr(self.wobi3, "entry_price", 0.0)
            or 0.0
        )
        if entry <= 0 or current_mid <= 0:
            return False, "HOLD", 0.0, ""
        bid = float(m.get("best_bid") or 0.0)
        mark = bid if bid > 0 else current_mid
        entry_ts = float(getattr(st, "entry_ts", 0.0) or 0.0)
        hold_s = max(0.0, float(now) - entry_ts) if entry_ts > 0 else 0.0
        max_h = float(self.MAX_HOLD_SEC)
        if hold_s >= max_h:
            self.absent_dwell_cuts = int(getattr(self, "absent_dwell_cuts", 0)) + 1
            self.entry_cooldown_until = float(now) + float(self.POST_CUT_COOLDOWN_SEC)
            return (
                True,
                "ABSENT_DWELL",
                mark,
                f"absent_dwell (hold={hold_s:.1f}s>={max_h:.0f}s)",
            )
        return super()._eval_wobi3_dynamic_exit(
            current_mid, depth_bids, depth_asks, m, now
        )

    def _on_honest_fill(
        self,
        side: str,
        px: float,
        src: str,
        snap: Any,
        now: float,
        m: Dict[str, Any],
        depth_bids: List[Dict[str, float]],
        depth_asks: List[Dict[str, float]],
    ) -> None:
        tag = str(getattr(self, "pending_exit_tag", "") or "")
        if side == "sell" and tag in ("ABSENT_DWELL", "DYN_STOP", "EV_GONE"):
            self.entry_cooldown_until = float(now) + float(self.POST_CUT_COOLDOWN_SEC)
        super()._on_honest_fill(side, px, src, snap, now, m, depth_bids, depth_asks)
        # Patch MS2 log lane name if parent still says MS2.
        pass

    def _tick_pair(self, snap: Any, now: float) -> None:
        try:
            self._tick_pair_inner(snap, now)
        except Exception as exc:
            _log(f"[ABS1_WOBI_HONEST] tick error: {exc}")
        if now - self._last_hb >= 60.0:
            self._last_hb = now
            v = self._last_wobi3_verdict or {}
            q = self.quote_side or "FLAT"
            px = f"{self.quote_px:,.0f}" if self.quote_px else "-"
            _log(
                f"[ABS1_WOBI_HONEST] hb quote={q} @{px} "
                f"gate={v.get('reason')} posts={self.honest_posts} "
                f"fills={self.honest_fills} "
                f"thin_blocks={int(self.spread_gate_blocks)} "
                f"dwell_cuts={int(self.absent_dwell_cuts)} "
                f"dyn_stop={int(getattr(self, 'dyn_stop_cuts', 0))} "
                f"min_spread={float(self.MIN_ENTRY_SPREAD_BP):.1f} "
                f"max_hold={float(self.MAX_HOLD_SEC):.0f}s "
                f"profile={self.ae.profile.value}"
            )

    def _tick_pair_inner(self, snap: Any, now: float) -> None:
        # Same as MS2 but force-exit tags include ABSENT_DWELL.
        m = self._extract_metrics(snap, now)
        self._last_m = m
        mid = float(getattr(snap, "mid_price", 0.0) or m.get("mid_price") or 0.0)
        bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        if bid > 0:
            m["best_bid"] = bid
        depth_bids, depth_asks = self._read_latest_depth()
        common = self._tf2bp_common(snap, m, now)
        execs = self._poll_execs()
        self._maybe_fill_board(execs, bid, ask, snap, now, m, depth_bids, depth_asks)
        if self.quote_side == "buy" and not self.wobi3.position_side:
            self.queue_obs.on_resting_tick(now=now, m=m, quote_side="buy")

        if self.wobi3.position_side:
            self.ae.on_tick(mid=mid, now=now, bid=bid, ask=ask, side="buy")

        pre = self.plan_wobi3_pre_tick(snap, now, m, depth_bids, depth_asks)
        if self.wobi3.position_side:
            self._record_gate(m)
            if pre.get("intent") == "EXIT":
                tag = str(pre.get("tag") or "EXIT")
                reason = str(pre.get("reason") or "")
                force = tag in _FORCE_EXIT_TAGS and bid > 0
                if force:
                    px, desc = float(bid), "HIT_BID"
                else:
                    px, desc = calculate_optimal_order_price(
                        depth_bids, depth_asks, "sell", 5, 0.05, 1.0, mid
                    )
                self.pending_exit_tag = tag
                self.pending_exit_reason = f"{reason} [{desc}]"
                self._post_quote(
                    "sell", px, f"EXIT {self.pending_exit_tag} {self.pending_exit_reason}"
                )
                if force:
                    self._maybe_fill_board(
                        execs, bid, ask, snap, now, m, depth_bids, depth_asks
                    )
                    if self.wobi3.position_side and bool(
                        getattr(self, "HS_FORCE_FILL", True)
                    ):
                        self.pending_exit_reason = f"{reason} [HIT_BID_FORCE]"
                        self._on_honest_fill(
                            "sell",
                            float(bid),
                            "HIT_BID_FORCE",
                            snap,
                            now,
                            m,
                            depth_bids,
                            depth_asks,
                        )
            elif self.quote_side == "sell":
                self._cancel_quote("ev_hold")
            elif self.quote_side == "buy":
                self._cancel_quote("now_long")
            return

        if self.quote_side == "sell":
            self._cancel_quote("flat_drop_exit")

        ok, gate_why = self._record_gate(m)
        if pre.get("intent") == "ENTER" or ok:
            why = str(pre.get("reason") or gate_why)
            if pre.get("intent") != "ENTER":
                res = self.wobi3.on_tick(**common)
                post = self.plan_wobi3_post_tick(res, now, m)
                ok, gate_why = self._record_gate(m)
                if not ok:
                    return
                if post.get("intent") == "ENTER":
                    why = str(post.get("reason") or why)
            else:
                if not ok:
                    return
            self._post_bid(m, depth_bids, depth_asks, mid, why, execs)
            return

        res = self.wobi3.on_tick(**common)
        post = self.plan_wobi3_post_tick(res, now, m)
        ok, gate_why = self._record_gate(m)
        if post.get("intent") == "ENTER" and ok:
            self._post_bid(
                m, depth_bids, depth_asks, mid, str(post.get("reason") or gate_why), execs
            )
            return
        self._maybe_pull_bid(
            m, bid, str(post.get("intent") or "HOLD"), str(post.get("reason") or "NO_TF2BP")
        )

    def metrics(self) -> Dict[str, Any]:
        m = super().metrics()
        m["model"] = "ABS1_WOBI_HONEST"
        w = m.get("MS2_WOBI_HONEST") or m.get("MS1_WOBI_HONEST") or {}
        m["ABS1_WOBI_HONEST"] = w
        m["absent"] = {
            "min_entry_spread_bp": float(self.MIN_ENTRY_SPREAD_BP),
            "max_hold_sec": float(self.MAX_HOLD_SEC),
            "thin_book_blocks": int(self.spread_gate_blocks),
            "absent_dwell_cuts": int(self.absent_dwell_cuts),
            "post_cut_cooldown_sec": float(self.POST_CUT_COOLDOWN_SEC),
            "dyn_stop_check_s": float(self.DYN_STOP_CHECK_S),
            "dyn_stop_mfe_touch_bp": float(self.DYN_STOP_MFE_TOUCH_BP),
        }
        m["stack"] = (
            "ABS1 Absent Scalp · thin-book refuse "
            f">={self.MIN_ENTRY_SPREAD_BP:.1f}bp · max_hold={self.MAX_HOLD_SEC:.0f}s · "
            "no +1bp harvest rest · WIRE=NO"
        )
        return m

    def discord_payload(self, kind: str = "hourly") -> Dict[str, Any]:
        payload = super().discord_payload(kind)
        payload["username"] = "ABS1_WOBI_HONEST"
        if payload.get("embeds"):
            title = str(payload["embeds"][0].get("title") or "")
            payload["embeds"][0]["title"] = title.replace("MS2", "ABS1").replace(
                "MS1", "ABS1"
            )
            payload["embeds"][0]["description"] = (
                "ABS1 Absent Scalp · thin-book refuse · hard dwell cut · "
                "no passive +1bp MM · WIRE=NO · A/B vs MS2"
            )
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ABS1_WOBI_HONEST DryRun (Absent Scalp · WIRE=NO)"
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    args = parser.parse_args()
    runner = ABS1WobiHonestDryRun(
        interval_sec=args.interval,
        report_interval_sec=args.report_interval,
        market_source=args.source,
    )

    def _sig(signum, _frame):
        _log(f"signal {signum}")
        runner.stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
