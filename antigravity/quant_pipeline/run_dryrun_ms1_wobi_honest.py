#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MS1_WOBI_HONEST — Micro Scalping (旧TF2B_WOBI5_HONEST) (WIRE=NO).

タイトゲート + 壁アンカー + LIVE_PARITY fill (CSR-681)。
板優位・フロー優位時の高回転マイクロ・スキャルピング。
Optimistic alone は commit しない · fee haircut · Soak8 verdict。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

from antigravity.quant_pipeline.live_parity_dryrun import (  # noqa: E402
    LiveParityScalpMixin,
    net_pnl_bp,
)
from antigravity.quant_pipeline.ms_queue_observe import MsQueueObserve  # noqa: E402
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import (  # noqa: E402
    _append,
    _iso,
    _log,
    calculate_optimal_order_price,
)
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi5 import TF2BWobi5DryRun  # noqa: E402
from antigravity.quant_pipeline.wobimm3_policy import (  # noqa: E402
    FAVORABLE_WOBI,
    PULL,
    Wobi3Market,
    Wobi3Quote,
    decide,
    entry_gate,
)

OUT_DIR = AG / "data" / "mm_research" / "ms1_wobi_honest_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)
FEEDER_EXECS = AG / "data" / "market_feeder" / "executions.jsonl"


class MS1WobiHonestDryRun(LiveParityScalpMixin, TF2BWobi5DryRun):
    ENTRY_POLICY = "ms1_wobi"
    LANE_NAME = "MS1_WOBI_HONEST"
    STATE_FILE = AG / "data" / "dryrun_ms1_wobi_honest_state.json"
    PID_FILE = AG / "data" / "dryrun_ms1_wobi_honest.pid"
    LEDGER_FILE = OUT_DIR / "paired_ledger.jsonl"
    AUDIT_FILE = OUT_DIR / "audit_events.jsonl"
    REPORT_MINUTE = 12
    HS_FORCE_FILL = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.quote_side: Optional[str] = None
        self.quote_px = 0.0
        self.working_id = ""
        self.pending_exit_tag = ""
        self.pending_exit_reason = ""
        self.exec_fp: Optional[Any] = None
        self.honest_posts = 0
        self.honest_cancels = 0
        self.honest_fills = 0
        self.gate_pulls = 0
        self.queue_vetoes = 0
        self.micro_vetoes = 0
        self.prefill_pulls = 0
        self.board_fills = 0
        self._last_hb = 0.0
        self._last_m: Dict[str, Any] = {}
        self._pending_queue_fields: Dict[str, Any] = {}
        self.queue_obs = MsQueueObserve(
            lane=str(self.LANE_NAME),
            append_fn=_append,
            shadow_path=Path(self.LEDGER_FILE).parent / "shadow_pull.jsonl",
            audit_path=Path(self.AUDIT_FILE),
            iso_fn=_iso,
            log_fn=_log,
        )
        self._init_exec_stream()
        self._parity_init()

    def _init_exec_stream(self) -> None:
        if FEEDER_EXECS.exists():
            try:
                self.exec_fp = open(FEEDER_EXECS, "r", encoding="utf-8")
                self.exec_fp.seek(0, 2)
            except Exception as exc:
                _log(f"exec stream: {exc}")

    def _poll_execs(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.exec_fp:
            self._init_exec_stream()
        if not self.exec_fp:
            return out
        try:
            while True:
                line = self.exec_fp.readline()
                if not line:
                    break
                row = json.loads(line)
                out.extend(row.get("rows") or [])
        except Exception:
            pass
        return out

    def _post_quote(self, side: str, px: float, why: str) -> None:
        side = side.lower()
        if self.quote_side == side and abs(self.quote_px - px) <= 1e-9:
            return
        if side == "buy" and self.quote_side == "buy":
            return
        if self.quote_side and self.quote_side != side:
            self.honest_cancels += 1
        self.quote_side = side
        self.quote_px = float(px)
        self.working_id = f"SIM-{side.upper()}-{int(time.time() * 1000)}"
        self.honest_posts += 1
        _log(f"[MS1_WOBI_HONEST] POST {side.upper()} @ {px:,.0f} ({why})")
        if side == "buy":
            self.queue_obs.on_post_buy(
                now=time.time(),
                px=float(px),
                working_id=self.working_id,
                m=dict(self._last_m or {}),
            )

    def _cancel_quote(self, why: str) -> None:
        if not self.quote_side:
            return
        _log(f"[MS1_WOBI_HONEST] CANCEL {self.quote_side.upper()} ({why})")
        if self.quote_side == "buy":
            self.queue_obs.on_cancel_clear(why=why)
        self.honest_cancels += 1
        self.quote_side = None
        self.quote_px = 0.0
        self.working_id = ""
        self.pending_exit_tag = ""
        self.pending_exit_reason = ""

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
        was_buy_quote = side == "buy"
        self.quote_side = None
        self.quote_px = 0.0
        self.working_id = ""
        _log(f"[MS1_WOBI_HONEST] FILL {side.upper()} @ {px:,.0f} src={src}")
        if side == "buy" and not self.wobi3.position_side:
            if was_buy_quote:
                self._pending_queue_fields = self.queue_obs.on_fill_buy(now=now, m=m)
            self.apply_wobi3_entry_state(
                "buy", px, now, depth_asks, mid_price=float(getattr(snap, "mid_price", px) or px)
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
            **qf,
        }
        _append(self.ledger_path, row)
        self.hist_wobi3.append(row)
        _append(self.audit_path, {"event": "EXIT", "ts": _iso(), "data": row})
        self.exit_posted_ts = 0.0
        icon = "💰" if net > 0 else "⚡"
        _log(
            f"[MS1_WOBI_HONEST] {icon} EXIT [{exit_tag}] pnl={net:+.2f}bp "
            f"(gross={float(pnl_bp):+.2f} fee={fee:.2f}) ({net_j:+.1f}円) "
            f"post_ofi={qf.get('post_ofi')} fill_ofi={qf.get('fill_ofi')} "
            f"shadow={qf.get('shadow_pulled')} ({reason})"
        )

    def _live_long_ev(self, m: Dict[str, Any]) -> bool:
        """Book still bid, prints not against. ofi=0 is silent, not opposite."""
        return float(m.get("wobi") or 0.0) >= FAVORABLE_WOBI and float(
            m.get("recent_ofi") or 0.0
        ) >= 0.0

    def _eval_wobi3_dynamic_exit(
        self,
        current_mid: float,
        depth_bids: List[Dict[str, float]],
        depth_asks: List[Dict[str, float]],
        m: Dict[str, Any],
        now: float,
    ):
        """Hold the long while live EV remains. Flatten when it is gone."""
        del depth_bids, depth_asks, now
        st = self.wobi3_trailing
        entry = float(st.entry_price or self.wobi3.entry_price or 0.0)
        if entry <= 0 or current_mid <= 0:
            return False, "HOLD", 0.0, ""
        wobi = float(m.get("wobi") or 0.0)
        ofi = float(m.get("recent_ofi") or 0.0)
        if self._live_long_ev(m):
            return False, "EV_HOLD", 0.0, f"ev_hold (wobi={wobi:+.2f} ofi={ofi:+.2f})"
        return True, "EV_GONE", current_mid, f"ev_gone (wobi={wobi:+.2f} ofi={ofi:+.2f})"

    def _record_gate(self, m: Dict[str, Any]) -> tuple[bool, str]:
        mkt = Wobi3Market(
            spread_bp=float(m.get("spread_bp") or 0.0),
            wobi=float(m.get("wobi") or 0.0),
            z_score=float(m.get("z_score") or 0.0),
            micro_dev_bp=float(m.get("micro_dev_bp") or 0.0),
            d_wobi_dt=float(m.get("d_wobi_dt") or 0.0),
            recent_ofi=float(m.get("recent_ofi") or 0.0),
            ask_depletion_vel=float(m.get("ask_depletion_vel") or 0.0),
        )
        # CSR-662 / fleet re-check: maker spread width ≠ loss; observe wide books, do not hard-block.
        ok, reason = entry_gate(mkt, skip_spread_gate=True)
        spread_bp = float(mkt.spread_bp or 0.0)
        if spread_bp > 2.0 and ok:
            reason = f"{reason} [spread_obs={spread_bp:.1f}bp]"
        self._last_wobi3_verdict = {
            "allowed": ok,
            "reason": reason,
            "metrics": {k: v for k, v in m.items() if not isinstance(v, list)},
            "side": "buy",
            "ts": _iso(),
            "spread_bp": spread_bp,
            "spread_gate": "observe_only",
        }
        return ok, reason

    def _stamp_veto(self, why: str) -> None:
        prev = self._last_wobi3_verdict or {}
        self._last_wobi3_verdict = {
            "allowed": False,
            "reason": why,
            "metrics": prev.get("metrics") or {},
            "side": "buy",
            "ts": _iso(),
        }

    def _post_bid(
        self,
        m: Dict[str, Any],
        depth_bids: List,
        depth_asks: List,
        mid: float,
        why: str,
        execs: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        del execs
        pref, desc = calculate_optimal_order_price(
            depth_bids, depth_asks, "buy", 5, 0.05, 1.0, mid
        )
        if pref <= 0:
            return
        self._post_quote("buy", pref, f"{why} [{desc}]")

    def _maybe_pull_bid(self, m: Dict[str, Any], bid: float, plan_intent: str, plan_reason: str) -> None:
        if self.quote_side != "buy":
            return
        del bid
        action, why = decide(
            Wobi3Quote(
                working_side="BUY",
                is_long=False,
                plan_intent=plan_intent,
                plan_reason=plan_reason,
                drift_bp=0.0,
                wobi=float(m.get("wobi") or 0.0),
                recent_ofi=float(m.get("recent_ofi") or 0.0),
            )
        )
        if action == PULL:
            if why in ("edge_drift", "edge_mu"):
                return
            self._cancel_quote(why)
            self.gate_pulls += 1

    def _tick_pair(self, snap: Any, now: float) -> None:
        try:
            self._tick_pair_inner(snap, now)
        except Exception as exc:
            _log(f"[MS1_WOBI_HONEST] tick error: {exc}")
        if now - self._last_hb >= 60.0:
            self._last_hb = now
            v = self._last_wobi3_verdict or {}
            q = self.quote_side or "FLAT"
            px = f"{self.quote_px:,.0f}" if self.quote_px else "-"
            sh = int(getattr(self.queue_obs, "shadow_events", 0) or 0)
            _log(
                f"[MS1_WOBI_HONEST] hb quote={q} @{px} "
                f"gate={v.get('reason')} posts={self.honest_posts} "
                f"fills={self.honest_fills} pulls={self.gate_pulls} "
                f"shadow_pull={sh}"
            )

    def _tick_pair_inner(self, snap: Any, now: float) -> None:
        m = self._extract_metrics(snap, now)
        self._last_m = m
        mid = float(getattr(snap, "mid_price", 0.0) or m.get("mid_price") or 0.0)
        bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        depth_bids, depth_asks = self._read_latest_depth()
        common = self._tf2bp_common(snap, m, now)
        execs = self._poll_execs()
        self._maybe_fill_board(execs, bid, ask, snap, now, m, depth_bids, depth_asks)
        # Observe-only: resting BUY ofi flip → Shadow Pull log (does not cancel).
        if self.quote_side == "buy" and not self.wobi3.position_side:
            self.queue_obs.on_resting_tick(now=now, m=m, quote_side="buy")

        pre = self.plan_wobi3_pre_tick(snap, now, m, depth_bids, depth_asks)
        if self.wobi3.position_side:
            self._record_gate(m)
            if pre.get("intent") == "EXIT":
                px, desc = calculate_optimal_order_price(
                    depth_bids, depth_asks, "sell", 5, 0.05, 1.0, mid
                )
                self.pending_exit_tag = str(pre.get("tag") or "EXIT")
                self.pending_exit_reason = str(pre.get("reason") or desc)
                self._post_quote("sell", px, f"EXIT {self.pending_exit_tag} {self.pending_exit_reason}")
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
                self._record_gate(m)
                if post.get("intent") == "ENTER":
                    why = str(post.get("reason") or why)
            self._post_bid(m, depth_bids, depth_asks, mid, why, execs)
            return

        res = self.wobi3.on_tick(**common)
        post = self.plan_wobi3_post_tick(res, now, m)
        self._record_gate(m)
        if post.get("intent") == "ENTER":
            self._post_bid(m, depth_bids, depth_asks, mid, str(post.get("reason") or gate_why), execs)
            return
        self._maybe_pull_bid(
            m, bid, str(post.get("intent") or "HOLD"), str(post.get("reason") or "NO_TF2BP")
        )

    def metrics(self) -> Dict[str, Any]:
        m = super().metrics()
        m["model"] = "MS1_WOBI_HONEST"
        m["fill"] = "LIVE_PARITY_SIZE_THROUGH"
        w5 = m.get("MS1_WOBI_HONEST") or m.get("TF2B_WOBI5_HONEST") or m.get("TF2B_WOBI5")
        if w5 is not None:
            m["MS1_WOBI_HONEST"] = w5
        m["honest_fill"] = {
            "posts": self.honest_posts,
            "cancels": self.honest_cancels,
            "fills": self.honest_fills,
            "gate_pulls": self.gate_pulls,
            "board_fills": self.board_fills,
            **self.queue_obs.snapshot_stats(),
        }
        m["parity"] = self.parity_metrics()
        m["stack"] = (
            "MS1_WOBI_HONEST LIVE_PARITY · tight gate + ev_hold + "
            "size-through fill + fee haircut · Soft→MARKET_SIM"
        )
        m["comment"] = self._generate_quant_comment(m)
        return m

    def _generate_quant_comment(self, m: Dict[str, Any]) -> str:
        w5 = m.get("MS1_WOBI_HONEST") or m.get("TF2B_WOBI5_HONEST") or m.get("TF2B_WOBI5") or {}
        hf = m.get("honest_fill") or {}
        pm = m.get("parity") or {}
        vd = pm.get("verdict") or {}
        s1 = w5.get("stats_1h") or {}
        return (
            f"直近1h: MS1_WOBI_HONEST {s1.get('pnl_bp', 0):+.2f} bp "
            f"({s1.get('total_trades', 0)}件)。fill=LIVE_PARITY。 "
            f"posts={hf.get('posts', 0)} fills={hf.get('fills', 0)} "
            f"verdict={vd.get('verdict')}."
        )

    def discord_payload(self, kind: str = "hourly") -> Dict[str, Any]:
        payload = super().discord_payload(kind)
        payload["username"] = "MS1_WOBI_HONEST"
        if payload.get("embeds"):
            emb = payload["embeds"][0]
            emb["title"] = str(emb.get("title") or "").replace("TF2B_WOBI5", "MS1_WOBI_HONEST").replace("TF2B_WOBI5_HONEST", "MS1_WOBI_HONEST")
            pm = self.parity_metrics()
            vd = pm.get("verdict") or {}
            emb["description"] = (
                "**MS1_WOBI_HONEST** · Micro Scalp · LIVE_PARITY · WIRE=NO\n"
                f"size-through fill · fee haircut · Soft→MARKET_SIM · "
                f"verdict=`{vd.get('verdict')}` · Economic_PASS=`{vd.get('economic_pass')}`"
            )
            m = self.metrics()
            if emb.get("footer"):
                emb["footer"]["text"] = f"Antigravity Quants · MS1 LIVE_PARITY · {m.get('ts_jst')}"
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="MS1_WOBI_HONEST DryRun (WIRE=NO · Micro Scalp)")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    args = parser.parse_args()
    runner = MS1WobiHonestDryRun(
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
