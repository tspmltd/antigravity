#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VR1_WOBI_HONEST — Vacuum Reversal / 真空リバウンド (WIRE=NO).

CSR-677 の一答（薄利受動MMはEV無し）から:
  - 通常は完全不在（MS1/WOBI の常時ENTERを使わない）
  - 大口 SELL taker 急増 + spread 瞬間拡大で ARMED
  - 減速後に Best Bid − N tick へ Post-Only BUY 単発
  - +TP_BP で利確、TIME_CUT Soft rest → MARKET_SIM（HIT_BID_FORCE禁止）
  - CSR-682: TIME_CUT 2.5s（リバウンド窓）+ 成行追随（chase）入口拒否
  - LIVE-parity fill (CSR-680): size-through only · fee haircut · Soak8 verdict
  - Discord: 自ランナー start/hourly + HONEST 艦隊

Not LIVE. Economic_PASS=NO.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))
os.environ.setdefault("ENABLE_REAL_TRADING", "false")

from antigravity.quant_pipeline.run_dryrun_ms1_wobi_honest import (  # noqa: E402
    MS1WobiHonestDryRun,
)
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import (  # noqa: E402
    _append,
    _iso,
    _log,
)

OUT_DIR = AG / "data" / "mm_research" / "vr1_wobi_honest_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Vacuum arm
TAKER_BURST_BTC = 0.50
MIN_VACUUM_SPREAD_BP = 3.0
BURST_WINDOW_SEC = 1.0
# Deceleration: sell flow in trailing 0.35s after arm must drop
DECEL_WINDOW_SEC = 0.35
DECEL_SELL_MAX_BTC = 0.08
ARM_EXPIRE_SEC = 2.0
# CSR-682: reject chase — sell still dumping in last 0.5s
CHASE_WINDOW_SEC = 0.50
CHASE_SELL_MAX_BTC = 0.12
# Entry / exit
ENTRY_OFFSET_TICKS = 2
TICK_JPY = 1.0
TP_BP = 2.0
TIME_CUT_SEC = 2.5  # was 1.0 — rebound window (CSR-682)
QUOTE_TTL_SEC = 1.5
COOLDOWN_SEC = 8.0

_FORCE_EXIT = frozenset({"VR_TP", "VR_TIME_CUT", "VR_FAILSAFE"})


class VR1WobiHonestDryRun(MS1WobiHonestDryRun):
    LANE_NAME = "VR1_WOBI_HONEST"
    STATE_FILE = AG / "data" / "dryrun_vr1_wobi_honest_state.json"
    PID_FILE = AG / "data" / "dryrun_vr1_wobi_honest.pid"
    LEDGER_FILE = OUT_DIR / "paired_ledger.jsonl"
    AUDIT_FILE = OUT_DIR / "audit_events.jsonl"
    REPORT_MINUTE = 14
    HS_FORCE_FILL = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ledger_path = Path(self.LEDGER_FILE)
        self.audit_path = Path(self.AUDIT_FILE)
        self.lane_name = self.LANE_NAME
        self.queue_obs.lane = self.LANE_NAME
        # (ts, sell_btc, buy_btc)
        self._tape: Deque[Tuple[float, float, float]] = deque(maxlen=5000)
        self.vr_phase = "IDLE"  # IDLE|ARMED|POSTED|LONG
        self.arm_ts = 0.0
        self.arm_sell_btc = 0.0
        self.arm_spread_bp = 0.0
        self.post_ts = 0.0
        self.entry_ts = 0.0
        self.cooldown_until = 0.0
        self.vr_arms = 0
        self.vr_posts = 0
        self.vr_fills = 0
        self.vr_tp = 0
        self.vr_time_cuts = 0
        self.vr_expires = 0
        self.vr_chase_blocks = 0
        self._parity_init()

    def _ingest_execs(self, execs: List[Dict[str, Any]], now: float) -> Tuple[float, float]:
        sell = 0.0
        buy = 0.0
        for ex in execs or []:
            try:
                sz = float(ex.get("size") or 0.0)
            except (TypeError, ValueError):
                continue
            if sz <= 0:
                continue
            side = str(ex.get("side") or "").upper()
            if side == "SELL":
                sell += sz
            elif side == "BUY":
                buy += sz
        if sell > 0 or buy > 0:
            self._tape.append((float(now), sell, buy))
        # prune
        cut = float(now) - 5.0
        while self._tape and self._tape[0][0] < cut:
            self._tape.popleft()
        return sell, buy

    def _sum_sell(self, now: float, window: float) -> float:
        t0 = float(now) - float(window)
        return sum(s for ts, s, _b in self._tape if ts >= t0)

    def _sum_buy(self, now: float, window: float) -> float:
        t0 = float(now) - float(window)
        return sum(b for ts, _s, b in self._tape if ts >= t0)

    def _vacuum_signal(self, now: float, spread_bp: float) -> Tuple[bool, float, str]:
        sell_1s = self._sum_sell(now, BURST_WINDOW_SEC)
        if sell_1s < float(TAKER_BURST_BTC):
            return False, sell_1s, "no_burst"
        if float(spread_bp) < float(MIN_VACUUM_SPREAD_BP):
            return False, sell_1s, f"spread={spread_bp:.2f}<{MIN_VACUUM_SPREAD_BP}"
        return True, sell_1s, "armed"

    def _decelerated(self, now: float) -> bool:
        # After arm, recent sell must be small vs burst window.
        recent = self._sum_sell(now, DECEL_WINDOW_SEC)
        return recent <= float(DECEL_SELL_MAX_BTC)

    def _chase_blocked(self, now: float) -> Tuple[bool, str]:
        """CSR-682: refuse entry while sell taker is still chasing down."""
        chase = self._sum_sell(now, CHASE_WINDOW_SEC)
        if chase > float(CHASE_SELL_MAX_BTC):
            return True, f"chase_sell={chase:.3f}>{CHASE_SELL_MAX_BTC}"
        return False, "ok"

    def _entry_px(self, bid: float) -> float:
        return float(bid) - float(ENTRY_OFFSET_TICKS) * float(TICK_JPY)

    def _tp_px(self, entry: float) -> float:
        return float(entry) * (1.0 + float(TP_BP) / 1e4)

    def _reset_flat(self, why: str) -> None:
        if self.quote_side:
            self._cancel_quote(why)
        self.vr_phase = "IDLE"
        self.arm_ts = 0.0
        self.post_ts = 0.0

    def _eval_wobi3_dynamic_exit(
        self,
        current_mid: float,
        depth_bids: List[Dict[str, float]],
        depth_asks: List[Dict[str, float]],
        m: Dict[str, Any],
        now: float,
    ):
        del depth_bids, depth_asks
        if not self.wobi3.position_side:
            return False, "HOLD", 0.0, ""
        entry = float(self.wobi3.entry_price or 0.0)
        bid = float(m.get("best_bid") or 0.0)
        mark = bid if bid > 0 else float(current_mid or 0.0)
        if entry <= 0 or mark <= 0:
            return False, "HOLD", 0.0, ""
        hold_s = max(0.0, float(now) - float(self.entry_ts or now))
        unreal = (mark / entry - 1.0) * 1e4
        if unreal >= float(TP_BP):
            self.vr_tp += 1
            return True, "VR_TP", mark, f"vr_tp (+{unreal:.2f}bp>={TP_BP})"
        if hold_s >= float(TIME_CUT_SEC):
            self.vr_time_cuts += 1
            return (
                True,
                "VR_TIME_CUT",
                mark,
                f"vr_time_cut (hold={hold_s:.2f}s>={TIME_CUT_SEC}s unreal={unreal:+.2f})",
            )
        return False, "VR_HOLD", 0.0, f"vr_hold ({unreal:+.2f}bp age={hold_s:.2f}s)"

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
        super()._on_honest_fill(side, px, src, snap, now, m, depth_bids, depth_asks)
        if side == "buy" and self.wobi3.position_side:
            self.vr_fills += 1
            self.vr_phase = "LONG"
            self.entry_ts = float(now)
            _log(
                f"[VR1_WOBI_HONEST] LONG @ {px:,.0f} arm_sell={self.arm_sell_btc:.3f} "
                f"arm_spread={self.arm_spread_bp:.2f} src={src}"
            )
        if side == "sell":
            self.cooldown_until = float(now) + float(COOLDOWN_SEC)
            self.vr_phase = "IDLE"
            self.arm_ts = 0.0
            self.post_ts = 0.0
            self.entry_ts = 0.0

    def _tick_pair(self, snap: Any, now: float) -> None:
        try:
            self._tick_pair_inner(snap, now)
        except Exception as exc:
            _log(f"[VR1_WOBI_HONEST] tick error: {exc}")
        if now - self._last_hb >= 60.0:
            self._last_hb = now
            q = self.quote_side or "FLAT"
            px = f"{self.quote_px:,.0f}" if self.quote_px else "-"
            pm = self.parity_metrics()
            vd = pm.get("verdict") or {}
            _log(
                f"[VR1_WOBI_HONEST] hb phase={self.vr_phase} quote={q} @{px} "
                f"posts={self.honest_posts} fills={self.honest_fills} "
                f"arms={self.vr_arms} vr_posts={self.vr_posts} vr_fills={self.vr_fills} "
                f"tp={self.vr_tp} time_cut={self.vr_time_cuts} expire={self.vr_expires} "
                f"parked={pm.get('parity_parked')} commits={pm.get('parity_commits')} "
                f"mkt_sim={pm.get('parity_market_sim')} verdict={vd.get('verdict')} "
                f"fill={pm.get('fill_model')}"
            )

    def _tick_pair_inner(self, snap: Any, now: float) -> None:
        m = self._extract_metrics(snap, now)
        self._last_m = m
        mid = float(getattr(snap, "mid_price", 0.0) or m.get("mid_price") or 0.0)
        bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        if bid > 0:
            m["best_bid"] = bid
        depth_bids, depth_asks = self._read_latest_depth()
        execs = self._poll_execs()
        self._ingest_execs(execs, now)
        self._maybe_fill_board(execs, bid, ask, snap, now, m, depth_bids, depth_asks)

        # ---- IN POSITION: TP Soft rest / time-cut Soft→HARD ----
        if self.wobi3.position_side:
            self.vr_phase = "LONG"
            pre = self.plan_wobi3_pre_tick(snap, now, m, depth_bids, depth_asks)
            if pre.get("intent") == "EXIT":
                tag = str(pre.get("tag") or "EXIT")
                reason = str(pre.get("reason") or "")
                soft = tag in _FORCE_EXIT and bid > 0
                # Soft at bid (or TP maker); never HIT_BID_FORCE.
                px = float(bid) if soft else self._tp_px(float(self.wobi3.entry_price or bid))
                self.pending_exit_tag = tag
                self.pending_exit_reason = f"{reason} [SOFT_BID]" if soft else reason
                self._post_quote("sell", px, f"EXIT {tag} {reason}")
                self._parity_mark_exit_posted(now)
                self._maybe_fill_board(
                    execs, bid, ask, snap, now, m, depth_bids, depth_asks
                )
                if self.wobi3.position_side and soft:
                    self._parity_maybe_hard_exit(
                        bid=bid,
                        snap=snap,
                        now=now,
                        m=m,
                        depth_bids=depth_bids,
                        depth_asks=depth_asks,
                        tag=tag,
                        reason=reason,
                    )
            elif self.quote_side == "sell":
                tag = str(getattr(self, "pending_exit_tag", "") or "EXIT")
                reason = str(getattr(self, "pending_exit_reason", "") or "soft_rest")
                self._parity_maybe_hard_exit(
                    bid=bid,
                    snap=snap,
                    now=now,
                    m=m,
                    depth_bids=depth_bids,
                    depth_asks=depth_asks,
                    tag=tag,
                    reason=reason,
                )
                if not self.wobi3.position_side:
                    return
            elif self.quote_side == "buy":
                self._cancel_quote("now_long")
            return

        # ---- FLAT: vacuum machine (no MS1 continuous ENTER) ----
        if self.quote_side == "sell":
            self._cancel_quote("flat_drop_exit")

        if now < float(self.cooldown_until or 0.0):
            if self.quote_side:
                self._cancel_quote("cooldown")
            self.vr_phase = "IDLE"
            return

        spread_bp = float(m.get("spread_bp") or 0.0)

        if self.vr_phase == "POSTED":
            # Fill wait / TTL
            if self.quote_side == "buy":
                age = float(now) - float(self.post_ts or now)
                if age >= float(QUOTE_TTL_SEC):
                    self.vr_expires += 1
                    self._reset_flat("vr_quote_ttl")
                    self.cooldown_until = float(now) + float(COOLDOWN_SEC) * 0.5
                return
            # quote gone without position
            self.vr_phase = "IDLE"
            return

        if self.vr_phase == "ARMED":
            if float(now) - float(self.arm_ts) > float(ARM_EXPIRE_SEC):
                self.vr_expires += 1
                self.vr_phase = "IDLE"
                return
            if bid <= 0:
                return
            if not self._decelerated(now):
                return
            chased, chase_why = self._chase_blocked(now)
            if chased:
                self.vr_chase_blocks = int(getattr(self, "vr_chase_blocks", 0)) + 1
                self.vr_expires += 1
                self.vr_phase = "IDLE"
                self.cooldown_until = float(now) + float(COOLDOWN_SEC) * 0.5
                _log(f"[VR1_WOBI_HONEST] CHASE_BLOCK {chase_why}")
                _append(
                    self.audit_path,
                    {
                        "event": "VR_CHASE_BLOCK",
                        "ts": _iso(),
                        "why": chase_why,
                        "arm_sell_btc": self.arm_sell_btc,
                    },
                )
                return
            px = self._entry_px(bid)
            if px <= 0 or (ask > 0 and px >= ask):
                return
            self._post_quote(
                "buy",
                px,
                (
                    f"VR_ENTRY vacuum sell={self.arm_sell_btc:.3f} "
                    f"spread={self.arm_spread_bp:.2f} offset={ENTRY_OFFSET_TICKS}t"
                ),
            )
            self.vr_posts += 1
            self.vr_phase = "POSTED"
            self.post_ts = float(now)
            _append(
                self.audit_path,
                {
                    "event": "VR_POST",
                    "ts": _iso(),
                    "sell_btc": self.arm_sell_btc,
                    "spread_bp": self.arm_spread_bp,
                    "px": px,
                    "bid": bid,
                },
            )
            return

        # IDLE → maybe ARM
        ok, sell_1s, why = self._vacuum_signal(now, spread_bp)
        if ok:
            self.vr_phase = "ARMED"
            self.arm_ts = float(now)
            self.arm_sell_btc = float(sell_1s)
            self.arm_spread_bp = float(spread_bp)
            self.vr_arms += 1
            _log(
                f"[VR1_WOBI_HONEST] ARMED sell_1s={sell_1s:.3f} "
                f"spread={spread_bp:.2f}bp ({why})"
            )
            _append(
                self.audit_path,
                {
                    "event": "VR_ARM",
                    "ts": _iso(),
                    "sell_btc": sell_1s,
                    "spread_bp": spread_bp,
                },
            )
        elif self.quote_side:
            # Never rest without vacuum arm.
            self._cancel_quote("absent_no_vacuum")

    def metrics(self) -> Dict[str, Any]:
        m = super().metrics()
        m["model"] = "VR1_WOBI_HONEST"
        w = m.get("MS1_WOBI_HONEST") or m.get("TF2B_WOBI5") or {}
        m["VR1_WOBI_HONEST"] = w
        m["vacuum"] = {
            "phase": self.vr_phase,
            "taker_burst_btc": float(TAKER_BURST_BTC),
            "min_vacuum_spread_bp": float(MIN_VACUUM_SPREAD_BP),
            "tp_bp": float(TP_BP),
            "time_cut_sec": float(TIME_CUT_SEC),
            "chase_window_sec": float(CHASE_WINDOW_SEC),
            "chase_sell_max_btc": float(CHASE_SELL_MAX_BTC),
            "arms": int(self.vr_arms),
            "posts": int(self.vr_posts),
            "fills": int(self.vr_fills),
            "tp": int(self.vr_tp),
            "time_cuts": int(self.vr_time_cuts),
            "expires": int(self.vr_expires),
            "chase_blocks": int(getattr(self, "vr_chase_blocks", 0)),
            "arm_sell_btc": float(self.arm_sell_btc),
            "arm_spread_bp": float(self.arm_spread_bp),
        }
        m["parity"] = self.parity_metrics()
        m["stack"] = (
            "VR1 Vacuum Reversal · LIVE_PARITY · chase-block · "
            f"bid-{ENTRY_OFFSET_TICKS}t · TP {TP_BP}bp / time-cut {TIME_CUT_SEC}s · WIRE=NO"
        )
        return m

    def discord_payload(self, kind: str = "hourly") -> Dict[str, Any]:
        payload = super().discord_payload(kind)
        payload["username"] = "VR1_WOBI_HONEST"
        m = self.metrics()
        vac = m.get("vacuum") or {}
        pm = m.get("parity") or {}
        vd = pm.get("verdict") or {}
        if payload.get("embeds"):
            emb = payload["embeds"][0]
            title = str(emb.get("title") or "")
            emb["title"] = (
                title.replace("MS1_WOBI_HONEST", "VR1_WOBI_HONEST")
                .replace("MS1", "VR1")
                .replace("TF2B_WOBI5", "VR1_WOBI_HONEST")
            )
            emb["description"] = (
                "**VR1_WOBI_HONEST** · Vacuum Reversal · LIVE_PARITY · WIRE=NO\n"
                f"burst≥`{TAKER_BURST_BTC}` BTC · spread≥`{MIN_VACUUM_SPREAD_BP}`bp · "
                f"TP `{TP_BP}`bp · time-cut `{TIME_CUT_SEC}`s · chase≤`{CHASE_SELL_MAX_BTC}`\n"
                f"phase=`{vac.get('phase')}` · arms=`{vac.get('arms')}` · "
                f"chase_blocks=`{vac.get('chase_blocks')}` · "
                f"vr_fills=`{vac.get('fills')}` · "
                f"verdict=`{vd.get('verdict')}` · Economic_PASS=`{vd.get('economic_pass')}`"
            )
            if emb.get("footer"):
                emb["footer"]["text"] = (
                    f"Antigravity Quants · VR1 LIVE_PARITY · {m.get('ts_jst')}"
                )
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="VR1_WOBI_HONEST DryRun (Vacuum Reversal · WIRE=NO)"
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    args = parser.parse_args()
    runner = VR1WobiHonestDryRun(
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
