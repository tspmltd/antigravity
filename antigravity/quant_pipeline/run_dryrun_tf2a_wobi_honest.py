#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TF2A_WOBI_HONEST — TF1A + HS MARKET immediate (WIRE=NO).

Baseline TF1A_WOBI_HONEST stays Lead-OFF for A/B.
CSR-683:
  - HARD_STOP = immediate MARKET_SIM (no Soft@bid overrun)
  - LEAD_ADVERSE_CUT path OFF (was 0% WR bleed)
  - Lead entry vacuum veto may stay ON (observe)
  - ledger: hs_trigger_bp / hs_fill_bp / hs_overrun_bp / hs_lag_s + fee
HARD_STOP_BP stays −4.0. ENTER v3 unchanged. Not LIVE.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

os.environ.setdefault("ENABLE_REAL_TRADING", "false")

from antigravity.quant_pipeline.live_parity_dryrun import net_pnl_bp  # noqa: E402
from antigravity.quant_pipeline.run_dryrun_tf1a_wobi_honest import (  # noqa: E402
    TF1AWobiHonestDryRun,
)
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import (  # noqa: E402
    _log,
    calculate_optimal_order_price,
)

OUT_DIR = AG / "data" / "mm_research" / "tf2a_wobi_honest_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)


class TF2AWobiHonestDryRun(TF1AWobiHonestDryRun):
    LANE_NAME = "TF2A_WOBI_HONEST"
    STATE_FILE = AG / "data" / "dryrun_tf2a_wobi_honest_state.json"
    PID_FILE = AG / "data" / "dryrun_tf2a_wobi_honest.pid"
    LEDGER_FILE = OUT_DIR / "paired_ledger.jsonl"
    AUDIT_FILE = OUT_DIR / "audit_events.jsonl"
    GATE_LOG_FILE = OUT_DIR / "gate_ticks.jsonl"
    HS_FORCE_FILL = False  # CSR-681 LIVE_PARITY
    # CSR-683: path cut OFF (LEAD_ADVERSE_CUT bleed). Entry vacuum veto keep.
    LEAD_ENTRY_VETO_ENABLED = True
    LEAD_PATH_CUT_ENABLED = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ledger_path = Path(self.LEDGER_FILE)
        self.audit_path = Path(self.AUDIT_FILE)
        self.gate_log_path = Path(self.GATE_LOG_FILE)
        self._hs_trigger_bp: Optional[float] = None
        self._hs_trigger_ts = 0.0
        if getattr(self, "lead", None) is not None:
            self.lead.entry_enabled = bool(self.LEAD_ENTRY_VETO_ENABLED)
            self.lead.path_enabled = bool(self.LEAD_PATH_CUT_ENABLED)

    def _execute_exit_wobi3(
        self, fill_price: float, pnl_bp: float, pnl_jpy: float, reason: str, exit_tag: str, now: float
    ) -> None:
        src_hint = "MARKET_SIM" if "MARKET_SIM" in str(reason) else str(reason)
        if "HIT_BID_FORCE" in str(reason):
            src_hint = "HIT_BID_FORCE"
        net, fee = net_pnl_bp(float(pnl_bp), src_hint)
        net_j = self.wobi3._yen(net)
        path = self._ae_close(fill_price, net, f"{exit_tag} ({reason})")
        self.apply_wobi3_exit_state(fill_price, net_j, exit_tag)
        row: Dict[str, Any] = {
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
            "hold_s": round(now - self.entry_ts, 1) if self.entry_ts else None,
            "entry_spread_bp": round(float(self.entry_spread_bp or 0.0), 3),
            "live_parity": True,
            "fill_model": str(getattr(self, "FILL_MODEL", "LIVE_PARITY")),
            **path,
        }
        if exit_tag == "HARD_STOP" and self._hs_trigger_bp is not None:
            trig = float(self._hs_trigger_bp)
            row["hs_trigger_bp"] = round(trig, 3)
            row["hs_fill_bp"] = round(net, 3)
            row["hs_overrun_bp"] = round(net - trig, 3)
            if self._hs_trigger_ts > 0:
                row["hs_lag_s"] = round(now - self._hs_trigger_ts, 3)
        from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import _append, _iso

        _append(self.ledger_path, row)
        self.hist_wobi3.append(row)
        _append(self.audit_path, {"event": "EXIT", "ts": _iso(), "data": row})
        self.exit_posted_ts = 0.0
        _log(
            f"[TF2A_WOBI_HONEST] EXIT [{exit_tag}] pnl={net:+.2f}bp "
            f"(gross={float(pnl_bp):+.2f} fee={fee:.2f}) "
            f"mfe={path.get('mfe_bp')} mae={path.get('mae_bp')} "
            f"hit_order={path.get('hit_order')} ({reason})"
        )
        self._hs_trigger_bp = None
        self._hs_trigger_ts = 0.0

    def _tick_pair_inner(self, snap: Any, now: float) -> None:
        m = self._extract_metrics(snap, now)
        mid = float(getattr(snap, "mid_price", 0.0) or m.get("mid_price") or 0.0)
        bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        depth_bids, depth_asks = self._read_latest_depth()
        execs = self._poll_execs()

        self._ae_on_tick(mid, now, bid, ask)
        self._maybe_fill_board(execs, bid, ask, snap, now, m, depth_bids, depth_asks)

        if self.wobi3.position_side:
            entry = float(self.wobi3.entry_price or 0.0)
            unreal = ((mid / entry) - 1.0) * 1e4 if entry > 0 and mid > 0 else None
            try:
                self.flow_strength.update(
                    now=now,
                    execs=execs,
                    wobi=float(m.get("wobi") or 0.0),
                    ofi=float(m.get("recent_ofi") or 0.0),
                    unrealized_bp=unreal,
                )
            except Exception as exc:
                _log(f"[TF2A_WOBI_HONEST] flow_strength update err: {exc}")
            should_exit, exit_tag, exit_px, exit_reason = self._eval_tf_exit(mid, m, now)
            if should_exit:
                # CSR-683: HARD_STOP = immediate MARKET; Lead path is OFF.
                if exit_tag == "HARD_STOP" and bid > 0:
                    self._hard_stop_immediate_market(
                        bid=bid,
                        snap=snap,
                        now=now,
                        m=m,
                        depth_bids=depth_bids,
                        depth_asks=depth_asks,
                        reason=exit_reason,
                        unreal=unreal,
                    )
                else:
                    px, desc = calculate_optimal_order_price(
                        depth_bids, depth_asks, "sell", 5, 0.05, 1.0, mid
                    )
                    self.pending_exit_tag = exit_tag
                    self.pending_exit_reason = f"{exit_reason} [{desc}]"
                    self._post_quote(
                        "sell",
                        px,
                        f"EXIT {self.pending_exit_tag} {self.pending_exit_reason}",
                    )
                    self.exit_posted_ts = 0.0
                    self._maybe_fill_board(
                        execs, bid, ask, snap, now, m, depth_bids, depth_asks
                    )
            elif self.quote_side == "sell":
                pass
            elif self.quote_side == "buy":
                self._cancel_quote("cancel_buy_now_long")
            return

        # Flat path: reuse parent ENTER v3 + observation.
        super()._tick_pair_inner(snap, now)

    def metrics(self) -> Dict[str, Any]:
        m = super().metrics()
        m["model"] = self.LANE_NAME
        m["hs_force_fill"] = bool(getattr(self, "HS_FORCE_FILL", True))
        m["lead_path_cut_enabled"] = bool(self.LEAD_PATH_CUT_ENABLED)
        m["lead_entry_veto_enabled"] = bool(self.LEAD_ENTRY_VETO_ENABLED)
        m["hard_stop_exec"] = "MARKET_IMMEDIATE"
        m["stack"] = (
            "TF2A_WOBI_HONEST LIVE_PARITY · HARD_STOP=MARKET immediate · "
            "LEAD_ADVERSE_CUT=OFF · entry vacuum veto ON · A/B vs TF1A"
        )
        if getattr(self, "lead", None) is not None:
            m["tf_adverse_lead"] = self.lead.metrics()
        return m


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TF2A_WOBI_HONEST DryRun (WIRE=NO · HS MARKET · Lead path OFF)"
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    args = parser.parse_args()
    runner = TF2AWobiHonestDryRun(
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
