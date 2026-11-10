#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MS2_WOBI_HONEST — MS1-parity de-repair + observe AE (WIRE=NO).

De-repair (2026-11-07):
  - Entry veto OFF (score gate was not predictive)
  - HARD_STOP / ADVERSE_FAST execution OFF (were cutting scalp MFE)
  - Active Pull OFF (Bad Skip / cancel:fill≈4.4x)
  - HonestAdverseKit remains for mae/mfe ledger observe only
  - Exit path = MS1 EV_HOLD / EV_GONE
  - Shadow Pull observe-only

MS1 baseline untouched for A/B. Not LIVE.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

os.environ.setdefault("ENABLE_REAL_TRADING", "false")

from antigravity.quant_pipeline.honest_adverse_kit import (  # noqa: E402
    HonestAdverseKit,
    StrategyProfile,
)
from antigravity.quant_pipeline.live_parity_dryrun import net_pnl_bp  # noqa: E402
from antigravity.quant_pipeline.run_dryrun_ms1_wobi_honest import (  # noqa: E402
    MS1WobiHonestDryRun,
)
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import (  # noqa: E402
    _append,
    _iso,
    _log,
    calculate_optimal_order_price,
)
OUT_DIR = AG / "data" / "mm_research" / "ms2_wobi_honest_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Soft rest tags → LIVE Soft; escalate MARKET_SIM (no HIT_BID_FORCE).
_FORCE_EXIT_TAGS = frozenset({"EV_GONE", "DYN_STOP"})


class MS2WobiHonestDryRun(MS1WobiHonestDryRun):
    LANE_NAME = "MS2_WOBI_HONEST"
    STATE_FILE = AG / "data" / "dryrun_ms2_wobi_honest_state.json"
    PID_FILE = AG / "data" / "dryrun_ms2_wobi_honest.pid"
    LEDGER_FILE = OUT_DIR / "paired_ledger.jsonl"
    AUDIT_FILE = OUT_DIR / "audit_events.jsonl"
    # De-repair: execution interventions OFF; kit observe ON.
    ENTRY_VETO_ENABLED = False
    HARD_STOP_ENABLED = False
    # CSR-681: Soft rest → MARKET_SIM (LIVE-parity).
    HS_FORCE_FILL = False
    ADVERSE_FAST_MAE_BP = -1.5  # unused while path_toxic OFF
    HARD_STOP_BP = -4.0
    HS_SHIELD_DURATION_SEC = 5.0
    HS_SHIELD_BP = -8.0
    # Axis ③: after CHECK_S, if path MFE never reached TOUCH → early flatten.
    DYN_STOP_ENABLED = True
    DYN_STOP_CHECK_S = 5.0
    DYN_STOP_MFE_TOUCH_BP = 1.5

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ledger_path = Path(self.LEDGER_FILE)
        self.audit_path = Path(self.AUDIT_FILE)
        # Rollback: keep observe-only Shadow Pull (no dry-run cancel).
        self.queue_obs.active_pull = False
        self.entry_cooldown_until = 0.0
        self.resting_ofi_invert_pulls = 0
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
        self.adverse_fast_cuts = 0
        self.adverse_hard_stops = 0
        self.adverse_entry_vetoes = 0
        self.dyn_stop_cuts = 0
        self._entry_feat: Dict[str, Any] = {}

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
        self.honest_fills += 1
        self.board_fills += 1
        self.quote_side = None
        self.quote_px = 0.0
        self.working_id = ""
        _log(f"[MS2_WOBI_HONEST] FILL {side.upper()} @ {px:,.0f} src={src}")
        if side == "buy" and not self.wobi3.position_side:
            mid = float(getattr(snap, "mid_price", px) or px)
            qf = self.queue_obs.on_fill_buy(now=now, m=m)
            self._pending_queue_fields = qf
            self.apply_wobi3_entry_state(
                "buy", px, now, depth_asks, mid_price=mid
            )
            feat = dict(self._entry_feat or {})
            # fill_* = FILL tick; post_* from queue observe (not aliased).
            fill_ofi = qf.get("fill_ofi")
            fill_wobi = qf.get("fill_wobi")
            if fill_ofi is None:
                fill_ofi = float(m.get("recent_ofi") or feat.get("ofi") or 0.0)
            if fill_wobi is None:
                fill_wobi = float(m.get("wobi") or feat.get("wobi") or 0.0)
            self.ae.track_entry(
                px=px,
                now=now,
                side="buy",
                mid=mid,
                spread_bp=float(
                    qf.get("fill_spread_bp")
                    or m.get("spread_bp")
                    or feat.get("spread_bp")
                    or 0.0
                ),
                meta={
                    "lane": self.LANE_NAME,
                    "wire": "NO",
                    # Legacy aliases = FILL-time (kept for old dashboards).
                    "wobi": fill_wobi,
                    "ofi": fill_ofi,
                    "adverse_score": m.get("adverse_score"),
                    "spread_bp": float(m.get("spread_bp") or 0.0),
                    "src": src,
                    "research": "ms2_queue_observe_shadow_pull_post_fill",
                    "adverse_profile": StrategyProfile.SCALP.value,
                    "hs_shield_sec": float(self.HS_SHIELD_DURATION_SEC),
                    "hs_shield_bp": float(self.HS_SHIELD_BP),
                    "post_ofi": qf.get("post_ofi"),
                    "post_wobi": qf.get("post_wobi"),
                    "fill_ofi": fill_ofi,
                    "fill_wobi": fill_wobi,
                    "d_ofi": qf.get("d_ofi"),
                    "d_wobi": qf.get("d_wobi"),
                    "queue_wait_s": qf.get("queue_wait_s"),
                    "shadow_pulled": qf.get("shadow_pulled"),
                    "shadow_pull_ofi": qf.get("shadow_pull_ofi"),
                    "shadow_pull_wait_s": qf.get("shadow_pull_wait_s"),
                },
            )
            return
        if side == "sell" and self.wobi3.position_side:
            entry = float(self.wobi3.entry_price or px)
            pnl_bp = (px / entry - 1.0) * 1e4 if entry > 0 else 0.0
            pnl_j = self.wobi3._yen(pnl_bp)
            tag = self.pending_exit_tag or "HONEST_EXIT"
            reason = self.pending_exit_reason or src
            self.pending_exit_tag = ""
            self.pending_exit_reason = ""
            self._execute_exit_wobi3(px, pnl_bp, pnl_j, reason, tag, now)

    def _execute_exit_wobi3(
        self, fill_price: float, pnl_bp: float, pnl_jpy: float, reason: str, exit_tag: str, now: float
    ) -> None:
        src_hint = "MARKET_SIM" if "MARKET_SIM" in str(reason) else str(reason)
        if "HIT_BID_FORCE" in str(reason):
            src_hint = "HIT_BID_FORCE"
        net, fee = net_pnl_bp(float(pnl_bp), src_hint)
        net_j = self.wobi3._yen(net)
        path = self.ae.close(
            exit_px=fill_price, pnl_bp=net, reason=f"{exit_tag} ({reason})"
        )
        self.apply_wobi3_exit_state(fill_price, net_j, exit_tag)
        qf = dict(self._pending_queue_fields or {})
        if not qf:
            qf = self.queue_obs.consume_filled_ledger_fields()
        self._pending_queue_fields = {}
        row = {
            "ts": now,
            "lane": self.lane_name,
            "side": "buy",
            "exit_side": exit_tag.lower(),
            "fill_price": fill_price,
            "pnl_bp": round(net, 2),
            "pnl_bp_gross": round(float(pnl_bp), 2),
            "pnl_bp_net": round(net, 2),
            "fee_bp": round(fee, 2),
            "pnl_jpy": round(net_j, 1),
            "is_win": (net > 0),
            "exit_reason": f"{exit_tag} ({reason}) [fee={fee:.2f}bp]",
            "live_parity": True,
            "fill_model": str(getattr(self, "FILL_MODEL", "LIVE_PARITY")),
            **path,
            **qf,
        }
        _append(self.ledger_path, row)
        self.hist_wobi3.append(row)
        _append(self.audit_path, {"event": "EXIT", "ts": _iso(), "data": row})
        self.exit_posted_ts = 0.0
        _log(
            f"[MS2_WOBI_HONEST] EXIT [{exit_tag}] pnl={net:+.2f}bp "
            f"(gross={float(pnl_bp):+.2f} fee={fee:.2f}) "
            f"mfe={path.get('mfe_bp')} mae={path.get('mae_bp')} "
            f"hit_order={path.get('hit_order')} profile={path.get('adverse_profile')} "
            f"post_ofi={qf.get('post_ofi')} fill_ofi={qf.get('fill_ofi')} "
            f"shadow={qf.get('shadow_pulled')} ({reason})"
        )

    def _eval_wobi3_dynamic_exit(
        self,
        current_mid: float,
        depth_bids: List[Dict[str, float]],
        depth_asks: List[Dict[str, float]],
        m: Dict[str, Any],
        now: float,
    ):
        del depth_bids, depth_asks
        st = self.wobi3_trailing
        entry = float(st.entry_price or self.wobi3.entry_price or 0.0)
        if entry <= 0 or current_mid <= 0:
            return False, "HOLD", 0.0, ""
        # Mark-to-bid when available (long); mid fallback.
        bid = float(m.get("best_bid") or 0.0)
        mark = bid if bid > 0 else current_mid
        unrealized_bp = (mark / entry - 1.0) * 1e4
        entry_ts = float(getattr(st, "entry_ts", 0.0) or 0.0)
        hold_s = max(0.0, float(now) - entry_ts) if entry_ts > 0 else None

        # De-repair: no HARD_STOP / path_toxic execution — MS1 EV loop only.
        if bool(getattr(self, "HARD_STOP_ENABLED", False)):
            hs, hs_why = self.ae.is_hard_stop(unrealized_bp, holding_time_sec=hold_s)
            if hs:
                self.adverse_hard_stops += 1
                return True, "HARD_STOP", mark, hs_why
            toxic, tw = self.ae.path_toxic()
            if toxic:
                self.adverse_fast_cuts += 1
                return True, "ADVERSE_FAST", current_mid, tw

        # Axis ③: never-green after CHECK_S → cut; greens keep EV_GONE harvest.
        if bool(getattr(self, "DYN_STOP_ENABLED", False)) and hold_s is not None:
            check_s = float(getattr(self, "DYN_STOP_CHECK_S", 5.0))
            touch = float(getattr(self, "DYN_STOP_MFE_TOUCH_BP", 1.5))
            if hold_s >= check_s:
                path_mfe = float(unrealized_bp)
                try:
                    rec = self.ae._find()  # noqa: SLF001 — active AE record
                    if rec is not None:
                        path_mfe = max(path_mfe, float(getattr(rec, "mfe_bp", 0.0) or 0.0))
                except Exception:
                    pass
                if path_mfe < touch:
                    self.dyn_stop_cuts = int(getattr(self, "dyn_stop_cuts", 0)) + 1
                    return (
                        True,
                        "DYN_STOP",
                        mark,
                        f"dyn_stop (hold={hold_s:.1f}s mfe={path_mfe:+.2f}<{touch})",
                    )

        wobi = float(m.get("wobi") or 0.0)
        ofi = float(m.get("recent_ofi") or 0.0)
        if self._live_long_ev(m):
            return False, "EV_HOLD", 0.0, f"ev_hold (wobi={wobi:+.2f} ofi={ofi:+.2f})"
        return True, "EV_GONE", current_mid, f"ev_gone (wobi={wobi:+.2f} ofi={ofi:+.2f})"

    def _record_gate(self, m: Dict[str, Any]) -> tuple[bool, str]:
        ok, reason = super()._record_gate(m)
        if ok and bool(getattr(self, "ENTRY_VETO_ENABLED", False)):
            veto, vwhy = self.ae.should_veto_entry(m.get("adverse_score"))
            if veto:
                self.adverse_entry_vetoes += 1
                self._stamp_veto(vwhy)
                return False, vwhy
        self._entry_feat = {
            "wobi": float(m.get("wobi") or 0.0),
            "ofi": float(m.get("recent_ofi") or 0.0),
            "spread_bp": float(m.get("spread_bp") or 0.0),
            "adverse_score": m.get("adverse_score"),
        }
        return ok, reason

    def _tick_pair(self, snap: Any, now: float) -> None:
        try:
            self._tick_pair_inner(snap, now)
        except Exception as exc:
            _log(f"[MS2_WOBI_HONEST] tick error: {exc}")
        if now - self._last_hb >= 60.0:
            self._last_hb = now
            v = self._last_wobi3_verdict or {}
            q = self.quote_side or "FLAT"
            px = f"{self.quote_px:,.0f}" if self.quote_px else "-"
            adv = getattr(self, "adverse_fast_cuts", 0)
            hs = getattr(self, "adverse_hard_stops", 0)
            veto = getattr(self, "adverse_entry_vetoes", 0)
            _log(
                f"[MS2_WOBI_HONEST] hb quote={q} @{px} "
                f"gate={v.get('reason')} posts={self.honest_posts} "
                f"fills={self.honest_fills} pulls={self.gate_pulls} "
                f"ae_fast={adv} hard_stop={hs} entry_veto={veto} "
                f"shadow_pull={int(self.queue_obs.shadow_events)} "
                f"active_pull=OFF de-repair=ON "
                f"veto={'ON' if self.ENTRY_VETO_ENABLED else 'OFF'} "
                f"hs={'ON' if self.HARD_STOP_ENABLED else 'OFF'} "
                f"dyn_stop={int(getattr(self, 'dyn_stop_cuts', 0))} "
                f"profile={self.ae.profile.value}"
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
        common = self._tf2bp_common(snap, m, now)
        execs = self._poll_execs()
        self._maybe_fill_board(execs, bid, ask, snap, now, m, depth_bids, depth_asks)
        # Observe-only Shadow Pull (ACTIVE_PULL rolled back).
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
                soft = tag in _FORCE_EXIT_TAGS and bid > 0
                if soft:
                    px, desc = float(bid), "SOFT_BID"
                else:
                    px, desc = calculate_optimal_order_price(
                        depth_bids, depth_asks, "sell", 5, 0.05, 1.0, mid
                    )
                self.pending_exit_tag = tag
                self.pending_exit_reason = f"{reason} [{desc}]"
                self._post_quote(
                    "sell", px, f"EXIT {self.pending_exit_tag} {self.pending_exit_reason}"
                )
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
        m["model"] = "MS2_WOBI_HONEST"
        w = m.get("MS1_WOBI_HONEST") or m.get("TF2B_WOBI5") or {}
        m["MS2_WOBI_HONEST"] = w
        m["adverse"] = {
            "profile": self.ae.profile.value,
            "fast_cuts": self.adverse_fast_cuts,
            "hard_stops": self.adverse_hard_stops,
            "entry_vetoes": self.adverse_entry_vetoes,
            "path_toxic": "OFF",
            "entry_veto_enabled": bool(self.ENTRY_VETO_ENABLED),
            "hard_stop_enabled": bool(self.HARD_STOP_ENABLED),
            "hard_stop_bp": float(self.HARD_STOP_BP),
            "hs_shield_sec": float(self.HS_SHIELD_DURATION_SEC),
            "hs_shield_bp": float(self.HS_SHIELD_BP),
            "kit": "HonestAdverseKit(observe)",
        }
        m["queue_observe"] = self.queue_obs.snapshot_stats()
        m["active_pull"] = {
            "enabled": False,
            "rolled_back": "2026-11-07",
            "events": int(self.resting_ofi_invert_pulls),
        }
        m["de_repair"] = {
            "enabled": True,
            "entry_veto": bool(self.ENTRY_VETO_ENABLED),
            "hard_stop": bool(self.HARD_STOP_ENABLED),
            "active_pull": False,
            "dyn_stop": bool(self.DYN_STOP_ENABLED),
            "dyn_stop_check_s": float(self.DYN_STOP_CHECK_S),
            "dyn_stop_mfe_touch_bp": float(self.DYN_STOP_MFE_TOUCH_BP),
            "dyn_stop_cuts": int(getattr(self, "dyn_stop_cuts", 0)),
        }
        m["stack"] = (
            "MS2_WOBI_HONEST de-repair + dyn_stop@5s/mfe<1.5 · "
            "veto/HS/ActivePull OFF · WIRE=NO"
        )
        return m

    def discord_payload(self, kind: str = "hourly") -> Dict[str, Any]:
        payload = super().discord_payload(kind)
        payload["username"] = "MS2_WOBI_HONEST"
        if payload.get("embeds"):
            payload["embeds"][0]["title"] = str(payload["embeds"][0]["title"]).replace(
                "MS1", "MS2"
            )
            payload["embeds"][0]["description"] = (
                "MS2 HONEST de-repair · veto/HS/ActivePull OFF · "
                "AE observe + Shadow observe · WIRE=NO · A/B vs MS1"
            )
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MS2_WOBI_HONEST DryRun (WIRE=NO · de-repair · A/B vs MS1)"
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    args = parser.parse_args()
    runner = MS2WobiHonestDryRun(
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
