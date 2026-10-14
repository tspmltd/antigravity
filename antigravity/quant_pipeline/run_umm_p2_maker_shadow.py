#!/usr/bin/env python3
"""
UMM-P2 Maker Shadow Runner
==========================
Paired Shadow: UMM Baseline (CTRL) vs UMM+Maker (ARM)
Execution-only divergence · WIRE=NO · LIVE untouched

Directive: docs/ops/umm_p2_maker_shadow_directive.md
Outputs:   data/mm_research/umm_p2_maker_shadow/
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

BASE_DIR = "/home/azureuser/antigravity"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.quant_pipeline.event_bus import EventBus
from antigravity.quant_pipeline.parquet_logger import ParquetBatchLogger
from antigravity.quant_pipeline.ingestion import MarketDataIngestion
from antigravity.quant_pipeline.feeder_board_source import FeederBoardSource
from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier
from antigravity.strategies.umm_p2_maker_shadow import (
    UMMArmShadow,
    compare_kpis,
    MAKER_OFFSET_RATIO,
    MAKER_TIMEOUT_SEC,
    FALLBACK_MODE,
)

JST = timezone(timedelta(hours=9))
OUT_DIR = os.path.join(BASE_DIR, "data", "mm_research", "umm_p2_maker_shadow")
DAILY_DIR = os.path.join(OUT_DIR, "daily")
STATE_PATH = os.path.join(OUT_DIR, "state_arm.json")
PAIRED_PATH = os.path.join(OUT_DIR, "paired_summary.json")
AUDIT_PATH = os.path.join(OUT_DIR, "audit_arm.jsonl")
REPORT_PATH = os.path.join(OUT_DIR, "report_latest.md")
DAILY_LATEST = os.path.join(OUT_DIR, "daily_monitor_latest.md")
PID_PATH = os.path.join(OUT_DIR, "umm_p2_maker_shadow.pid")
LOG_PATH = os.path.join(BASE_DIR, "logs", "umm_p2_maker_shadow.log")
LANE_LOCK = os.path.join(OUT_DIR, "LANE_LOCK.json")
DRYRUN_STATE = os.path.join(BASE_DIR, "data", "dryrun_umm_tf2bp_state.json")


class UMMP2MakerShadowRunner:
    def __init__(self, symbol: str = "FX_BTC_JPY", poll_interval_sec: float = 1.0):
        self.symbol = symbol
        self.poll_interval_sec = poll_interval_sec
        self.running = True
        self.start_time = time.time()
        self.last_persist = 0.0
        self.ticks = 0

        self.ctrl = UMMArmShadow("CTRL")
        self.arm = UMMArmShadow("ARM")
        self._last_daily_key = ""
        self._last_discord_ts = 0.0
        self._last_discord_verdict = ""
        self.discord_interval_sec = float(os.environ.get("UMM_P2_DISCORD_INTERVAL_SEC", "3600"))
        self.notifier = QuantDiscordNotifier()

        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=30.0, batch_size=100)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code=self.symbol)
        self.feeder_source = FeederBoardSource(self.ingestion)

        os.makedirs(OUT_DIR, exist_ok=True)
        os.makedirs(DAILY_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(PID_PATH, "w") as f:
            f.write(str(os.getpid()))

        self._log(
            f"START UMM-P2 Shadow · WIRE=NO · exec={MAKER_OFFSET_RATIO}/{MAKER_TIMEOUT_SEC}s/{FALLBACK_MODE}"
        )

    def _log(self, msg: str) -> None:
        # Prefer stdout (nohup >> LOG_PATH). Avoid double-append to the same file.
        line = f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)

    def _audit(self, ev: Dict[str, Any]) -> None:
        ev = {**ev, "ts_wall": time.time(), "ts_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")}
        try:
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        self._log("UMM Baseline (CTRL) vs UMM+Maker (ARM) · Signal/Inv/Skew/Size/Exit/Params FROZEN")
        self._update_lane_lock("RUNNING")
        self.feeder_source.start()
        # Start notice (empty paired snapshot is fine — shows gate MORE_DATA)
        try:
            boot = {
                "verdict": "MORE_DATA",
                "promote": False,
                "promote_label": False,
                "sample_floor_met": False,
                "elapsed_hours": 0.0,
                "gate": {"reason": "Shadow started · WIRE=NO", "improve_flags": {}},
                "delta": {},
                "daily_monitor": {},
                "comparison": {"CTRL": self.ctrl.kpis(), "ARM": self.arm.kpis()},
            }
            ok = self.notifier.post_umm_p2_maker_shadow(boot, kind="start")
            self._log(f"discord start notify ok={ok}")
            self._last_discord_ts = time.time()
            self._last_discord_verdict = "MORE_DATA"
        except Exception as e:
            self._log(f"discord start notify error: {e}")

        def _sig(sig, _frame):
            self._log(f"signal {sig} → clean stop")
            self.stop()

        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

        last_snap = None
        while self.running:
            try:
                now = time.time()
                snap = self.feeder_source.get(timeout=self.poll_interval_sec)
                if snap is not None:
                    last_snap = snap
                if last_snap is None:
                    time.sleep(0.2)
                    continue

                mid = float(last_snap.mid_price or 0.0)
                bid = float(last_snap.best_bid or 0.0)
                ask = float(last_snap.best_ask or 0.0)
                if mid <= 0 and bid > 0 and ask > 0:
                    mid = (bid + ask) / 2.0
                if bid <= 0 or ask <= 0 or bid >= ask:
                    time.sleep(0.2)
                    continue

                self.ticks += 1
                kwargs = dict(
                    now=now,
                    mid=mid,
                    best_bid=bid,
                    best_ask=ask,
                    imbalance=float(getattr(last_snap, "imbalance", 0.0) or 0.0),
                    last_sell=float(getattr(last_snap, "last_sell_price", 0.0) or 0.0),
                    last_buy=float(getattr(last_snap, "last_buy_price", 0.0) or 0.0),
                    taker_bid=float(getattr(last_snap, "taker_volume_bid", 0.0) or 0.0),
                    taker_ask=float(getattr(last_snap, "taker_volume_ask", 0.0) or 0.0),
                )
                for ev in self.ctrl.step(**kwargs):
                    self._audit(ev)
                for ev in self.arm.step(**kwargs):
                    self._audit(ev)
                    if ev.get("event") in ("ENTRY", "EXIT", "NO_FILL_CANCEL", "ORDER_POSTED"):
                        if self.ticks % 20 == 0 or ev.get("event") in ("EXIT", "NO_FILL_CANCEL"):
                            self._log(f"ARM {ev.get('event')} { {k:ev.get(k) for k in ('side','pnl_bp','hold_bucket','never_green','ae_worsen','reason','waited_sec') if k in ev or ev.get(k) is not None} }")

                if now - self.last_persist >= 10.0:
                    self._persist(mid, bid, ask)
                    self.last_persist = now

                time.sleep(0.3)
            except Exception as e:
                self._log(f"ERROR {e}\n{traceback.format_exc()}")
                time.sleep(2.0)

        self._persist(
            float(getattr(last_snap, "mid_price", 0) or 0) if last_snap else 0.0,
            float(getattr(last_snap, "best_bid", 0) or 0) if last_snap else 0.0,
            float(getattr(last_snap, "best_ask", 0) or 0) if last_snap else 0.0,
            final=True,
        )
        try:
            if os.path.exists(PAIRED_PATH):
                paired = json.loads(open(PAIRED_PATH, encoding="utf-8").read())
                ok = self.notifier.post_umm_p2_maker_shadow(paired, kind="final")
                self._log(f"discord final notify ok={ok}")
        except Exception as e:
            self._log(f"discord final notify error: {e}")
        try:
            self.feeder_source.stop()
        except Exception:
            pass
        if os.path.exists(PID_PATH):
            try:
                os.remove(PID_PATH)
            except Exception:
                pass
        self._update_lane_lock("STOPPED")
        self._log("stopped cleanly")

    def _dryrun_ctrl_headline(self) -> Optional[Dict[str, Any]]:
        try:
            with open(DRYRUN_STATE, "r", encoding="utf-8") as f:
                d = json.load(f)
            u = d.get("umm") or {}
            return {
                "source": "production_dryrun_umm (untouched)",
                "stats_1h": u.get("stats_1h"),
                "stats_24h": u.get("stats_24h"),
                "total_pnl_bp": u.get("total_pnl_bp"),
                "frozen": u.get("frozen"),
            }
        except Exception:
            return None

    def _persist(self, mid: float, bid: float, ask: float, final: bool = False) -> None:
        elapsed_h = (time.time() - self.start_time) / 3600.0
        attempts = self.arm.attempts
        sample_floor_met = bool(elapsed_h >= 72 or attempts >= 2000)
        cmp = compare_kpis(self.ctrl, self.arm, sample_floor_met=sample_floor_met)
        cmp["sample_floor_met"] = sample_floor_met
        cmp["promote"] = False  # never auto-LIVE / never auto-apply

        state = {
            "lane": "UMM-P2 Shadow",
            "wire": "NO",
            "updated_at_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "elapsed_hours": round(elapsed_h, 3),
            "ticks": self.ticks,
            "mid": mid,
            "best_bid": bid,
            "best_ask": ask,
            "execution_frozen": {
                "maker_offset_ratio": MAKER_OFFSET_RATIO,
                "maker_timeout_sec": MAKER_TIMEOUT_SEC,
                "fallback_mode": FALLBACK_MODE,
            },
            "forbidden_unchanged": [
                "signal", "inventory", "skew", "sizing", "exit", "params", "LIVE", "dryrun_ctrl"
            ],
            "shadow_CTRL": self.ctrl.kpis(),
            "shadow_ARM": self.arm.kpis(),
            "production_dryrun_ctrl_readonly": self._dryrun_ctrl_headline(),
            "recent_arm_trades": self.arm.trades[-20:],
            "final": final,
        }
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)

        paired = {
            "updated_at_jst": state["updated_at_jst"],
            "elapsed_hours": state["elapsed_hours"],
            "sample_floor_met": sample_floor_met,
            "verdict": cmp["verdict"],
            "promote": False,
            "promote_label": cmp.get("promote_label", False),
            "gate": cmp.get("gate"),
            "comparison": cmp,
            "primary_kpis": cmp["primary_kpis"],
            "delta": cmp["delta_ARM_minus_CTRL"],
            "daily_monitor": self._daily_monitor_payload(cmp, state),
        }
        tmp2 = PAIRED_PATH + ".tmp"
        with open(tmp2, "w", encoding="utf-8") as f:
            json.dump(paired, f, ensure_ascii=False, indent=2)
        os.replace(tmp2, PAIRED_PATH)

        self._write_report(paired, state)
        daily_rolled = self._write_daily_monitor(paired, state)
        self._maybe_discord(paired, daily_rolled=daily_rolled, final=final)

    def _maybe_discord(self, paired: Dict[str, Any], *, daily_rolled: bool, final: bool = False) -> None:
        """Notify Discord: start handled in run(); here periodic / daily / verdict / final."""
        if final:
            return  # final sent explicitly after last persist
        now = time.time()
        verdict = str(paired.get("verdict") or "MORE_DATA")
        kind = None
        if daily_rolled:
            kind = "daily"
        elif verdict != self._last_discord_verdict and verdict in ("REJECT", "PROMOTE"):
            kind = "verdict"
        elif (now - self._last_discord_ts) >= self.discord_interval_sec:
            kind = "periodic"
        if not kind:
            return
        try:
            ok = self.notifier.post_umm_p2_maker_shadow(paired, kind=kind)
            self._log(f"discord {kind} notify ok={ok} verdict={verdict}")
            self._last_discord_ts = now
            self._last_discord_verdict = verdict
        except Exception as e:
            self._log(f"discord {kind} notify error: {e}")

    def _daily_monitor_payload(self, cmp: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        pk = cmp.get("primary_kpis") or {}
        return {
            "never_green_rate": pk.get("never_green_rate"),
            "ae_worsen_rate": pk.get("ae_worsen_rate"),
            "bucket_ev": pk.get("bucket_ev"),
            "sink_10s_plus": pk.get("sink_10s_plus"),
            "focus": "10s+ sink improvement (PnL/EV) vs CTRL",
            "elapsed_hours": state.get("elapsed_hours"),
        }

    def _write_daily_monitor(self, paired: Dict[str, Any], state: Dict[str, Any]) -> bool:
        """Daily (and continuously refreshed) primary monitor card. Returns True on JST day roll."""
        day = datetime.now(JST).strftime("%Y-%m-%d")
        mon = paired.get("daily_monitor") or {}
        gate = paired.get("gate") or {}
        d = paired.get("delta") or {}
        lines = [
            "# UMM-P2 Daily Monitor",
            f"Date: {day}",
            f"Updated: {paired['updated_at_jst']}",
            f"Elapsed: {paired['elapsed_hours']}h · sample_floor_met={paired['sample_floor_met']}",
            "",
            "## Primary KPIs (daily)",
            f"- Never-green率: {mon.get('never_green_rate')}",
            f"- AE worsen率: {mon.get('ae_worsen_rate')}",
            "",
            "## 別EV (hold buckets)",
        ]
        for b, v in (mon.get("bucket_ev") or {}).items():
            lines.append(f"- {b}: CTRL={v.get('CTRL')} ARM={v.get('ARM')} Δ={v.get('delta')}")
        sink = mon.get("sink_10s_plus") or {}
        lines += [
            "",
            "## 10s+ sink (追従フォーカス)",
            f"- CTRL: {sink.get('CTRL')}",
            f"- ARM:  {sink.get('ARM')}",
            f"- ΔPnL(sink)={sink.get('delta_pnl_bp')} · ΔEV(sink)={sink.get('delta_ev_bp')}",
            "",
            "## Headline Δ(ARM−CTRL)",
            f"- ΔPnL={d.get('pnl_bp')} ΔPF={d.get('pf')} ΔDD={d.get('dd_bp')} ΔAE1s={d.get('avg_ae_1s')}",
            "",
            "## Gate",
            f"- verdict={paired.get('verdict')} · promote_label={paired.get('promote_label')} · auto_promote=False",
            f"- reason={gate.get('reason')}",
            f"- flags={gate.get('improve_flags')}",
            "",
            "Rules: REJECT if ΔPnL<=0 (after floor) · MORE_DATA if improving but under-sampled · "
            "PROMOTE if PnL+PF+DD+never_green↓+AE all improve (Paper-candidate only).",
        ]
        text = "\n".join(lines) + "\n"
        with open(DAILY_LATEST, "w", encoding="utf-8") as f:
            f.write(text)
        # Snapshot once per JST day (overwrite same-day file as it accrues)
        daily_path = os.path.join(DAILY_DIR, f"{day}.md")
        daily_json = os.path.join(DAILY_DIR, f"{day}.json")
        with open(daily_path, "w", encoding="utf-8") as f:
            f.write(text)
        with open(daily_json, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": day,
                    "updated_at_jst": paired["updated_at_jst"],
                    "daily_monitor": mon,
                    "delta": d,
                    "verdict": paired.get("verdict"),
                    "gate": gate,
                    "sample_floor_met": paired.get("sample_floor_met"),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        rolled = False
        if self._last_daily_key != day:
            self._last_daily_key = day
            self._log(f"daily monitor rolled → {daily_path}")
            rolled = True
        return rolled

    def _write_report(self, paired: Dict[str, Any], state: Dict[str, Any]) -> None:
        c = paired["comparison"]["CTRL"]["headline"]
        a = paired["comparison"]["ARM"]["headline"]
        d = paired["delta"]
        mon = paired.get("daily_monitor") or {}
        lines = [
            "# UMM-P2 Shadow — paired summary",
            f"Updated: {paired['updated_at_jst']}",
            f"Elapsed: {paired['elapsed_hours']}h · ticks={state['ticks']}",
            "",
            "WIRE=NO · auto PROMOTE=False · Execution-only",
            "",
            "## Headline",
            f"- CTRL: n={c.get('n')} EV={c.get('ev_bp')} PnL={c.get('pnl_bp')} PF={c.get('pf')} DD={c.get('dd_bp')} never_green={c.get('never_green_rate')} ae_worsen={c.get('ae_worsen_rate')} avgAE1s={c.get('avg_ae_1s')}",
            f"- ARM:  n={a.get('n')} EV={a.get('ev_bp')} PnL={a.get('pnl_bp')} PF={a.get('pf')} DD={a.get('dd_bp')} never_green={a.get('never_green_rate')} ae_worsen={a.get('ae_worsen_rate')} avgAE1s={a.get('avg_ae_1s')} fill_rate={a.get('fill_rate')} attempts={a.get('attempts')} timeouts={a.get('timeouts')}",
            f"- Δ(ARM-CTRL): pnl={d.get('pnl_bp')} pf={d.get('pf')} dd={d.get('dd_bp')} never_green={d.get('never_green_rate')} ae1s={d.get('avg_ae_1s')} sink_pnl={d.get('sink_10s_plus_pnl_bp')}",
            "",
            "## Daily primary",
            f"- Never-green: {mon.get('never_green_rate')}",
            f"- AE worsen: {mon.get('ae_worsen_rate')}",
            f"- 別EV: {mon.get('bucket_ev')}",
            f"- 10s+ sink: {mon.get('sink_10s_plus')}",
            "",
            "## Hold buckets",
        ]
        for b, v in paired["primary_kpis"]["hold_buckets"].items():
            lines.append(f"- {b}: CTRL={v.get('CTRL')} | ARM={v.get('ARM')}")
        lines += [
            "",
            f"## Verdict: {paired['verdict']} (promote_label={paired.get('promote_label')} · auto_promote=False)",
            f"sample_floor_met={paired['sample_floor_met']} · gate={paired.get('gate')}",
            "",
            "Production dryrun CTRL (readonly): "
            + json.dumps(state.get("production_dryrun_ctrl_readonly"), ensure_ascii=False),
        ]
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _update_lane_lock(self, status: str) -> None:
        try:
            if os.path.exists(LANE_LOCK):
                with open(LANE_LOCK, "r", encoding="utf-8") as f:
                    lock = json.load(f)
            else:
                lock = {}
            lock["status"] = status
            lock["pid_path"] = PID_PATH
            lock["updated_at_jst"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
            lock["runner"] = "antigravity.quant_pipeline.run_umm_p2_maker_shadow"
            with open(LANE_LOCK, "w", encoding="utf-8") as f:
                json.dump(lock, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def main():
    import argparse
    p = argparse.ArgumentParser(description="UMM-P2 Maker Shadow (WIRE=NO)")
    p.add_argument("--symbol", default="FX_BTC_JPY")
    p.add_argument("--interval", type=float, default=1.0)
    args = p.parse_args()
    # Refuse if someone tries to unlock champion via env
    if os.environ.get("UMM_CHAMPION_UNLOCK") == "1":
        print("REFUSE: UMM_CHAMPION_UNLOCK=1 is forbidden on this lane", file=sys.stderr)
        sys.exit(2)
    runner = UMMP2MakerShadowRunner(symbol=args.symbol, poll_interval_sec=args.interval)
    runner.run()


if __name__ == "__main__":
    main()
