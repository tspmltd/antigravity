#!/usr/bin/env python3
"""CSR-588 — PEG2-B LIVE Trial v1 (Paper→LIVE fill-quality).

Real LIMIT maker entry + CANCEL_AND_WAIT + Baseline exit.
Signal/Threshold/Exit/Inventory/Sizing/Skew FROZEN (PEG2-B constants).
Never writes ENABLE_REAL_TRADING into .env.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

from core.bitflyer_client import BitFlyerClient, OrderSubmitUnknownError  # noqa: E402
from antigravity.quant_pipeline.event_bus import EventBus  # noqa: E402
from antigravity.quant_pipeline.feeder_board_source import FeederBoardSource  # noqa: E402
from antigravity.quant_pipeline.ingestion import MarketDataIngestion  # noqa: E402
from antigravity.quant_pipeline.parquet_logger import ParquetBatchLogger  # noqa: E402
from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier  # noqa: E402

JST = timezone(timedelta(hours=9))
OUT = AG / "data" / "mm_research" / "peg2b_live_trial"
STATE_PATH = OUT / "state.json"
AUDIT_PATH = OUT / "audit.jsonl"
LOCK_PATH = OUT / "LANE_LOCK.json"
PID_PATH = AG / "data" / "peg2b_live_trial.pid"
STOP_FLAG = AG / "data" / "PEG2B_LIVE_TRIAL_STOP.flag"
GO_FLAG = AG / "data" / "GO_PEG2B_LIVE_TRIAL.flag"
PAPER_STATE = AG / "data" / "paper_tf2bp_peg2_state.json"
PAPER_AUDIT = AG / "data" / "mm_research" / "paper_audit_peg2.jsonl"

CONFIRM_TOKEN = "PEG2B588"
NEEDLE = "antigravity.quant_pipeline.run_peg2b_live_trial"
SOAK_NEEDLE = "antigravity.quant_pipeline.run_micro_live_soak_csr574"

# FROZEN PEG2-B constants — retune forbidden
PARAMS = {
    "micro_mom_bp": 2.0,
    "imbalance_high_threshold": 0.20,
    "maker_offset_ratio": 0.97,
    "maker_timeout_sec": 3.0,
    "spread_guard_max_bp": 4.5,
    "fallback_mode": "CANCEL_AND_WAIT",
    "order_size_btc": 0.001,
    "target_bp": 15.0,
    "trail_stop_bp": 4.0,
    "trail_arm_mfe_bp": 4.0,
    "min_hold_before_trail_sec": 8.0,
    "max_hold_sec": 1200.0,
}

PHASE_SEC = {1: 3600.0, 2: 7200.0, 3: 10800.0}
MAX_TOTAL_SEC = 21600.0
REPORT_EVERY_SEC = 1800.0
# Phase-1 continue / stop EV gap (spec CSR-588 lock)
CONTINUE_EV_GAP_BP = 0.50
EV_GE_EPS_BP = 0.10  # 6h review: Live EV >= Paper EV - eps
STOP_PNL_BP = -30.0
STOP_EV_GAP_BP = 0.50
STOP_TIMEOUT_RATE = 0.80
STOP_CONSEC_LOSS = 4
MIN_ATTEMPTS_TIMEOUT_STOP = 10
MIN_TRADES_EV_STOP = 3
CONTINUE_FILL_RATE = 0.20
CONTINUE_TIMEOUT_RATE = 0.70


def _jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def _iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def _append_audit(row: Dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _foreign_pids(needle: str) -> List[int]:
    me = os.getpid()
    found: List[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        # Match the python module runner only (ignore shells that embed the needle).
        if needle in cmd and ("python" in cmd or "-m " in cmd):
            # Prefer processes whose argv contains the module as a discrete token.
            parts = raw.split(b"\x00")
            joined = " ".join(p.decode("utf-8", errors="replace") for p in parts if p)
            if needle in joined and "python" in joined:
                found.append(pid)
    return found


def _fetch_board(product: str = "FX_BTC_JPY") -> Dict[str, Any]:
    url = f"https://api.bitflyer.com/v1/board?product_code={product}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode())


def _fetch_exec_vols(product: str = "FX_BTC_JPY") -> Tuple[float, float]:
    url = f"https://api.bitflyer.com/v1/executions?product_code={product}&count=50"
    buy = sell = 0.0
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            rows = json.loads(resp.read().decode())
        for r in rows or []:
            sz = float(r.get("size") or 0)
            if (r.get("side") or "").upper() == "BUY":
                buy += sz
            else:
                sell += sz
    except Exception:
        pass
    return buy, sell


def _parse_venue_net(rows: List[Dict[str, Any]]) -> Tuple[float, float]:
    net = 0.0
    notional = 0.0
    for p in rows or []:
        side = (p.get("side") or "").upper()
        sz = float(p.get("size") or 0.0)
        px = float(p.get("price") or 0.0)
        if side == "BUY":
            net += sz
            notional += sz * px
        elif side == "SELL":
            net -= sz
            notional -= sz * px
    avg = (notional / net) if abs(net) > 1e-12 else 0.0
    return net, avg


def _load_paper_snapshot() -> Dict[str, Any]:
    if not PAPER_STATE.exists():
        return {"total_pnl_bp": 0.0, "total_trades": 0, "ev_bp": 0.0, "win_rate_pct": 0.0}
    try:
        st = json.loads(PAPER_STATE.read_text(encoding="utf-8"))
        s = st.get("strategy") or {}
        n = int(s.get("total_trades") or 0)
        pnl = float(s.get("total_pnl_bp") or 0.0)
        return {
            "total_pnl_bp": pnl,
            "total_trades": n,
            "ev_bp": (pnl / n) if n else 0.0,
            "win_rate_pct": float(s.get("win_rate_pct") or 0.0),
            "maker_fill_rate_pct": float(s.get("maker_fill_rate_pct") or 0.0),
            "updated_at_jst": st.get("updated_at_jst"),
        }
    except Exception as exc:
        return {"error": str(exc), "total_pnl_bp": 0.0, "total_trades": 0, "ev_bp": 0.0}


def _paper_window_stats(t0: float) -> Dict[str, Any]:
    """Closed paper trades since trial start from audit JSONL."""
    trades: List[Dict[str, Any]] = []
    if not PAPER_AUDIT.exists():
        return {"n": 0, "pnl_bp": 0.0, "ev_bp": 0.0, "wins": 0, "timeouts": 0, "orders": 0, "fills": 0}
    orders = fills = timeouts = 0
    try:
        with PAPER_AUDIT.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("timestamp") or ""
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=JST)
                    if dt.timestamp() < t0:
                        continue
                except Exception:
                    continue
                ev = rec.get("event")
                data = rec.get("data") or {}
                if ev == "ORDER_POSTED":
                    orders += 1
                elif ev == "MAKER_FILLED":
                    fills += 1
                elif ev == "ORDER_TIMEOUT_CANCEL":
                    timeouts += 1
                elif ev == "POSITION_EXIT":
                    trades.append(data)
    except Exception as exc:
        return {"n": 0, "pnl_bp": 0.0, "ev_bp": 0.0, "error": str(exc)}
    pnls = [float(t.get("pnl_bp") or 0.0) for t in trades]
    n = len(pnls)
    pnl = sum(pnls)
    wins = sum(1 for x in pnls if x > 0)
    return {
        "n": n,
        "pnl_bp": round(pnl, 4),
        "ev_bp": round(pnl / n, 4) if n else 0.0,
        "wins": wins,
        "win_rate": round(wins / n, 4) if n else 0.0,
        "orders": orders,
        "fills": fills,
        "timeouts": timeouts,
        "fill_rate": round(fills / orders, 4) if orders else 0.0,
        "timeout_rate": round(timeouts / orders, 4) if orders else 0.0,
    }


class Peg2BLiveTrial:
    def __init__(self, *, size: float, real: bool, interval: float, product: str = "FX_BTC_JPY"):
        if size > 0.001 + 1e-12:
            raise SystemExit("REFUSE: size must be <= 0.001")
        self.size = float(size)
        self.real = bool(real)
        self.interval = float(interval)
        self.product = product
        self.params = dict(PARAMS)
        self.params["order_size_btc"] = self.size
        self.client = BitFlyerClient(enable_real_trading=self.real, max_order_size=max(self.size, 0.05))
        self.notifier = QuantDiscordNotifier()
        self.bus = EventBus()
        self.pq_logger = ParquetBatchLogger(flush_interval_sec=60.0, batch_size=200)
        self.ingestion = MarketDataIngestion(self.bus, self.pq_logger, product_code=self.product)
        self.feeder = FeederBoardSource(self.ingestion)
        self.running = True
        self.stop_reason = ""
        self.phase = 1
        self.t0 = time.time()
        self.phase_deadline = self.t0 + PHASE_SEC[1]
        self.last_report_ts = self.t0
        self.paper0 = _load_paper_snapshot()

        self.price_history: List[float] = []
        self.pending: Optional[Dict[str, Any]] = None
        self.position_side: Optional[str] = None
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.peak_mfe_bp = 0.0
        self.trail_armed = False
        self.acceptance_id: Optional[str] = None

        self.maker_orders = 0
        self.maker_fills = 0
        self.maker_timeouts = 0
        self.maker_cancels = 0
        self.spread_skips = 0
        self.slippages_bp: List[float] = []
        self.trades: List[Dict[str, Any]] = []
        self.total_pnl_bp = 0.0
        self.total_pnl_jpy = 0.0
        self.consec_losses = 0
        self.api_errors = 0
        self.rejects = 0
        self.good_fills: List[Dict[str, Any]] = []
        self.bad_fills: List[Dict[str, Any]] = []
        self.timeout_attr: List[Dict[str, Any]] = []
        self.phase_reports_done = set()
        self.feed_source = "shared_feeder"
        self.position_mismatch = False

    def request_stop(self, reason: str) -> None:
        if not self.stop_reason:
            self.stop_reason = reason
        self.running = False
        print(f"[PEG2B_LIVE] STOP requested: {reason}", flush=True)

    # ----- venue helpers -----
    def venue_net(self) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        if not self.real:
            net = self.size if self.position_side == "BUY" else (-self.size if self.position_side == "SELL" else 0.0)
            return net, self.entry_price, None
        try:
            rows = self.client.get_positions(self.product) or []
            net, avg = _parse_venue_net(rows)
            return net, avg, None
        except Exception as exc:
            return None, None, str(exc)

    def lookup_order(self, acceptance_id: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "active": False,
            "filled": False,
            "canceled": False,
            "rejected": False,
            "avg_px": None,
            "executed_size": 0.0,
        }
        if not self.real:
            return out
        for state, key in (
            ("ACTIVE", "active"),
            ("COMPLETED", "filled"),
            ("CANCELED", "canceled"),
            ("REJECTED", "rejected"),
            ("EXPIRED", "canceled"),
        ):
            try:
                rows = self.client.get_child_orders(
                    self.product,
                    child_order_state=state,
                    child_order_acceptance_id=acceptance_id,
                ) or []
            except Exception as exc:
                self.api_errors += 1
                return {**out, "error": str(exc)}
            if not rows:
                continue
            out[key] = True
            row0 = rows[0] if isinstance(rows[0], dict) else {}
            if state == "COMPLETED":
                try:
                    out["executed_size"] = float(row0.get("executed_size") or row0.get("size") or 0.0)
                except Exception:
                    out["executed_size"] = 0.0
                try:
                    avg = row0.get("average_price")
                    if avg is not None and float(avg) > 0:
                        out["avg_px"] = float(avg)
                except Exception:
                    pass
                if out["avg_px"] is None:
                    try:
                        px = self.client.get_execution_price_by_acceptance_id(
                            self.product, acceptance_id, max_retries=2, retry_interval_sec=0.2
                        )
                        out["avg_px"] = px
                    except Exception:
                        pass
        return out

    def require_venue_flat(self, *, context: str) -> Tuple[bool, Optional[float], Optional[str]]:
        """Hard gate before new LIMIT. Returns (ok, net, err)."""
        if not self.real:
            if self.position_side is not None:
                return False, self.size, "local_position_open"
            return True, 0.0, None
        net, avg, err = self.venue_net()
        if err is not None:
            self.api_errors += 1
            return False, None, err
        if net is None:
            return False, None, "venue_net_unknown"
        if abs(net) >= 1e-8:
            return False, net, f"venue_net_not_flat:{net}:avg={avg}:ctx={context}"
        return True, 0.0, None

    def flatten(self, reason: str) -> Dict[str, Any]:
        detail: Dict[str, Any] = {"reason": reason, "ts": _iso(), "actions": []}
        try:
            if self.real:
                try:
                    self.client.cancel_all_child_orders(self.product)
                    detail["actions"].append("cancel_all")
                except Exception as exc:
                    detail["actions"].append(f"cancel_all_err:{exc}")
                    self.api_errors += 1
            self.pending = None
            self.acceptance_id = None
            net, avg, err = self.venue_net()
            if err:
                detail["venue_error"] = err
                self.api_errors += 1
            detail["venue_net_before"] = net
            if net is not None and abs(net) >= 1e-8:
                side = "SELL" if net > 0 else "BUY"
                sz = abs(net)
                try:
                    if self.real:
                        res = self.client.send_order(self.product, side, sz, "MARKET")
                        detail["actions"].append({"flatten_market": res})
                    else:
                        detail["actions"].append({"flatten_market": "SIMULATED"})
                    # realize approx pnl if we had entry
                    if self.position_side and self.entry_price > 0:
                        board = _fetch_board(self.product)
                        mid = float(board.get("mid_price") or 0) or self.entry_price
                        if self.position_side == "BUY":
                            pnl_bp = (mid - self.entry_price) / self.entry_price * 10_000.0
                        else:
                            pnl_bp = (self.entry_price - mid) / self.entry_price * 10_000.0
                        self._close_trade(mid, "FORCE_FLAT", pnl_bp, hold_sec=time.time() - self.entry_time)
                except OrderSubmitUnknownError as exc:
                    detail["actions"].append(f"flatten_unknown:{exc}")
                    self.api_errors += 1
                    self.request_stop(f"E:order_submit_unknown:{exc}")
                except Exception as exc:
                    detail["actions"].append(f"flatten_err:{exc}")
                    self.api_errors += 1
            self.position_side = None
            self.entry_price = 0.0
            self.entry_time = 0.0
            self.peak_mfe_bp = 0.0
            self.trail_armed = False
            time.sleep(1.0)
            net2, _, err2 = self.venue_net()
            detail["venue_net_after"] = net2
            detail["venue_error_after"] = err2
            if net2 is not None and abs(net2) >= 1e-8:
                detail["flat_ok"] = False
                self.request_stop("E:position_mismatch_after_flat")
            else:
                detail["flat_ok"] = True
        except Exception as exc:
            detail["fatal"] = str(exc)
        _append_audit({"event": "FLATTEN", "ts": _iso(), "data": detail})
        _write_json(OUT / "flatten_last.json", detail)
        return detail

    def _close_trade(self, exit_px: float, reason: str, pnl_bp: float, hold_sec: float) -> None:
        pnl_jpy = pnl_bp / 10_000.0 * self.entry_price * self.size
        self.total_pnl_bp += pnl_bp
        self.total_pnl_jpy += pnl_jpy
        rec = {
            "trade_id": len(self.trades) + 1,
            "side": self.position_side,
            "entry_price": self.entry_price,
            "exit_price": exit_px,
            "hold_sec": round(hold_sec, 3),
            "pnl_bp": round(pnl_bp, 4),
            "pnl_jpy": round(pnl_jpy, 4),
            "mfe_bp": round(self.peak_mfe_bp, 4),
            "exit_reason": reason,
            "ts": _iso(),
        }
        self.trades.append(rec)
        if pnl_bp > 0:
            self.consec_losses = 0
            self.good_fills.append(rec)
        else:
            self.consec_losses += 1
            self.bad_fills.append(rec)
        _append_audit({"event": "POSITION_EXIT", "ts": _iso(), "data": rec})
        self.position_side = None
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.peak_mfe_bp = 0.0
        self.trail_armed = False

    # ----- metrics -----
    def metrics(self) -> Dict[str, Any]:
        attempts = self.maker_orders
        fill_rate = (self.maker_fills / attempts) if attempts else 0.0
        timeout_rate = (self.maker_timeouts / attempts) if attempts else 0.0
        n = len(self.trades)
        wins = sum(1 for t in self.trades if float(t["pnl_bp"]) > 0)
        ev = (self.total_pnl_bp / n) if n else 0.0
        slip = (sum(self.slippages_bp) / len(self.slippages_bp)) if self.slippages_bp else 0.0
        paper = _paper_window_stats(self.t0)
        paper_pnl = float(paper.get("pnl_bp") or 0.0)
        paper_ev = float(paper.get("ev_bp") or 0.0)
        # Capture = realized / MFE when MFE>0 (fill-quality, not PnL chase)
        caps: List[float] = []
        for t in self.trades:
            mfe = float(t.get("mfe_bp") or 0.0)
            if mfe > 1e-9:
                caps.append(float(t.get("pnl_bp") or 0.0) / mfe)
        capture = (sum(caps) / len(caps)) if caps else 0.0
        maker_rate = fill_rate  # PEG2-B entry is maker-only
        return {
            "ts_jst": _jst(),
            "elapsed_sec": round(time.time() - self.t0, 1),
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "phase": self.phase,
            "real": self.real,
            "live_pnl_bp": round(self.total_pnl_bp, 4),
            "live_pnl_jpy": round(self.total_pnl_jpy, 2),
            "paper_pnl_bp_window": paper_pnl,
            "pnl_diff_live_minus_paper_bp": round(self.total_pnl_bp - paper_pnl, 4),
            "fill_rate": round(fill_rate, 4),
            "timeout_rate": round(timeout_rate, 4),
            "maker_fill_pct": round(fill_rate * 100.0, 2),
            "maker_rate": round(maker_rate, 4),
            "capture": round(capture, 4),
            "avg_slippage_bp": round(slip, 4),
            "win_rate": round(wins / n, 4) if n else 0.0,
            "ev_bp": round(ev, 4),
            "paper_ev_bp_window": paper_ev,
            "delta_ev_bp": round(ev - paper_ev, 4),
            "ev_diff_live_minus_paper_bp": round(ev - paper_ev, 4),
            "trades": n,
            "maker_orders": attempts,
            "maker_fills": self.maker_fills,
            "maker_timeouts": self.maker_timeouts,
            "consec_losses": self.consec_losses,
            "api_errors": self.api_errors,
            "rejects": self.rejects,
            "position": self.position_side or "FLAT",
            "position_mismatch": bool(self.position_mismatch),
            "paper_window": paper,
            "stop_reason": self.stop_reason or None,
            "review_only": True,
            "live_promote": False,
        }

    def check_stops(self, m: Dict[str, Any]) -> Optional[str]:
        if float(m["live_pnl_bp"]) <= STOP_PNL_BP:
            return f"A:cum_pnl<={STOP_PNL_BP}bp ({m['live_pnl_bp']})"
        if self.consec_losses >= STOP_CONSEC_LOSS:
            return f"B:{STOP_CONSEC_LOSS}_consec_losses"
        if m["maker_orders"] >= MIN_ATTEMPTS_TIMEOUT_STOP and float(m["timeout_rate"]) > STOP_TIMEOUT_RATE:
            return f"C:timeout_rate>{STOP_TIMEOUT_RATE:.0%} ({m['timeout_rate']})"
        if (
            m["trades"] >= MIN_TRADES_EV_STOP
            and m["paper_window"].get("n", 0) >= MIN_TRADES_EV_STOP
            and float(m["ev_bp"]) < float(m["paper_ev_bp_window"]) - STOP_EV_GAP_BP
        ):
            return (
                f"D:live_ev<{m['paper_ev_bp_window']}-{STOP_EV_GAP_BP} "
                f"(live={m['ev_bp']} paper={m['paper_ev_bp_window']})"
            )
        if self.api_errors >= 5 or self.rejects >= 3:
            return f"E:anomaly api_errors={self.api_errors} rejects={self.rejects}"
        return None

    def phase1_continue_ok(self, m: Dict[str, Any]) -> Tuple[bool, str]:
        """Phase-1 → Phase-2: ALL five conditions required (CSR-588 lock)."""
        live_ev = float(m["ev_bp"])
        paper_ev = float(m["paper_ev_bp_window"])
        # If no closed trades on either side yet, treat EV gap as 0 (neutral).
        if m["trades"] == 0 and int((m.get("paper_window") or {}).get("n") or 0) == 0:
            ev_ok = True
        else:
            ev_ok = live_ev >= (paper_ev - CONTINUE_EV_GAP_BP)
        fill_ok = float(m["fill_rate"]) >= CONTINUE_FILL_RATE
        to_ok = float(m["timeout_rate"]) <= CONTINUE_TIMEOUT_RATE
        pnl_ok = float(m["live_pnl_bp"]) > STOP_PNL_BP
        mismatch = bool(m.get("position_mismatch"))
        mismatch_ok = not mismatch
        ok = ev_ok and fill_ok and to_ok and pnl_ok and mismatch_ok
        reason = (
            f"1:live_ev>=paper_ev-{CONTINUE_EV_GAP_BP}={ev_ok}"
            f"(L={live_ev:.4f} P={paper_ev:.4f}) "
            f"2:fill>={CONTINUE_FILL_RATE}={fill_ok}({m['fill_rate']}) "
            f"3:timeout<={CONTINUE_TIMEOUT_RATE}={to_ok}({m['timeout_rate']}) "
            f"4:pnl>{STOP_PNL_BP}={pnl_ok}({m['live_pnl_bp']}) "
            f"5:no_mismatch={mismatch_ok}"
        )
        return ok, reason

    def phase_good_ok(self, m: Dict[str, Any]) -> Tuple[bool, str]:
        # Phase-2/3: same quality bar as phase1 continue
        return self.phase1_continue_ok(m)

    # ----- reporting -----
    def _report_body(self, m: Dict[str, Any], title: str) -> str:
        lines = [
            f"# {title}",
            "",
            f"- ts: `{m['ts_jst']}`",
            f"- elapsed: `{m['elapsed_hours']}h` · phase=`{m['phase']}` · real=`{m['real']}`",
            f"- stop_reason: `{m.get('stop_reason')}`",
            "",
            "## KPI (30m schema)",
            "",
            f"1. LIVE PnL: **{m['live_pnl_bp']} bp** ({m['live_pnl_jpy']} JPY)",
            f"2. Paper PnL diff (Live−Paper window): **{m['pnl_diff_live_minus_paper_bp']} bp**",
            f"3. Fill Rate: **{m['fill_rate']:.2%}** ({m['maker_fills']}/{m['maker_orders']})",
            f"4. Timeout Rate: **{m['timeout_rate']:.2%}** ({m['maker_timeouts']}/{m['maker_orders']})",
            f"5. Maker Fill % / Maker Rate: **{m['maker_fill_pct']}%** / **{m.get('maker_rate')}**",
            f"6. Average Slippage: **{m['avg_slippage_bp']} bp**",
            f"7. Win Rate: **{m['win_rate']:.2%}** (n={m['trades']})",
            f"8. Live EV: **{m['ev_bp']} bp** · Paper EV: **{m['paper_ev_bp_window']} bp** · ΔEV: **{m.get('delta_ev_bp', m['ev_diff_live_minus_paper_bp'])}**",
            f"9. Capture: **{m.get('capture')}** (mean pnl/MFE)",
            "",
            "## Paper EV / Live EV / ΔEV",
            "",
            f"- Paper EV = `{m['paper_ev_bp_window']}`",
            f"- Live EV = `{m['ev_bp']}`",
            f"- ΔEV (Live−Paper) = `{m.get('delta_ev_bp', m['ev_diff_live_minus_paper_bp'])}`",
            "",
            "## Paper vs Live 差分分析",
            "",
            "```json",
            json.dumps(
                {
                    "live": {
                        "pnl_bp": m["live_pnl_bp"],
                        "ev_bp": m["ev_bp"],
                        "fill_rate": m["fill_rate"],
                        "timeout_rate": m["timeout_rate"],
                        "maker_rate": m.get("maker_rate"),
                        "capture": m.get("capture"),
                        "trades": m["trades"],
                    },
                    "paper_window": m["paper_window"],
                    "diff": {
                        "pnl_bp": m["pnl_diff_live_minus_paper_bp"],
                        "delta_ev_bp": m.get("delta_ev_bp"),
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
            "## Good Fill",
            "",
            f"count={len(self.good_fills)}",
            "```json",
            json.dumps(self.good_fills[-20:], ensure_ascii=False, indent=2),
            "```",
            "",
            "## Bad Fill",
            "",
            f"count={len(self.bad_fills)}",
            "```json",
            json.dumps(self.bad_fills[-20:], ensure_ascii=False, indent=2),
            "```",
            "",
            "## Timeout Attribution",
            "",
            f"count={len(self.timeout_attr)}",
            "```json",
            json.dumps(self.timeout_attr[-30:], ensure_ascii=False, indent=2),
            "```",
            "",
            "## Capture / Maker Rate",
            "",
            f"- Capture = `{m.get('capture')}`",
            f"- Maker Rate = `{m.get('maker_rate')}` ({m.get('maker_fill_pct')}%)",
            "",
            "## Lock",
            "",
            "- Signal/Threshold/Exit/Inventory/Sizing/Skew **FROZEN**",
            "- Forbidden lanes OBSERVE ONLY (CSR-521 / Alpha vs Adverse / PEG_v3 / TF2B_10.15 / 4AGENT)",
            "- End = **auto FLAT · Review Only** · LIVE昇格は別審査（auto promote なし）",
            "",
        ]
        return "\n".join(lines)

    def write_report(self, name: str, title: str) -> Path:
        m = self.metrics()
        path = OUT / name
        path.write_text(self._report_body(m, title), encoding="utf-8")
        print(f"[PEG2B_LIVE] report → {path}", flush=True)
        return path

    def emit_periodic(self) -> None:
        m = self.metrics()
        stamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
        self.write_report(f"reports/30m_{stamp}.md", f"PEG2-B LIVE Trial · 30m report · {stamp}")
        _write_json(STATE_PATH, {"metrics": m, "params_frozen": self.params, "paper0": self.paper0})
        try:
            self.notifier.post_peg2b_live_trial(m, kind="periodic")
        except Exception as exc:
            print(f"[PEG2B_LIVE] discord warn: {exc}", flush=True)

    # ----- strategy steps (frozen logic) -----
    def _signal(self, now: float, best_bid: float, best_ask: float, mid: float, imb: float) -> Optional[Dict[str, Any]]:
        spread_bp = (best_ask - best_bid) / mid * 10_000.0
        if spread_bp > self.params["spread_guard_max_bp"]:
            self.spread_skips += 1
            return None
        mom = 0.0
        if len(self.price_history) >= 6:
            mom = (mid - self.price_history[-6]) / self.price_history[-6] * 10_000.0
        if not (imb >= self.params["imbalance_high_threshold"] and mom >= -0.5):
            return None
        ratio = self.params["maker_offset_ratio"]
        limit_price = best_bid + (mid - best_bid) * (1.0 - ratio)
        return {
            "side": "BUY",
            "limit_price": float(int(limit_price)),  # bitFlyer int tick
            "mid": mid,
            "spread_bp": spread_bp,
            "imbalance": imb,
            "mom": mom,
            "created": now,
            "timeout_at": now + self.params["maker_timeout_sec"],
        }

    def _post_limit(self, intent: Dict[str, Any]) -> None:
        # Hard gate: never stack entries — venue must be flat
        if self.position_side is not None or self.pending is not None:
            return
        ok, net, err = self.require_venue_flat(context="pre_limit")
        if not ok:
            if err and ("venue_net_not_flat" in str(err) or (net is not None and abs(float(net)) >= 1e-8)):
                self.position_mismatch = True
                _append_audit(
                    {
                        "event": "BLOCK_LIMIT_NOT_FLAT",
                        "ts": _iso(),
                        "data": {"net": net, "err": err, "intent": intent},
                    }
                )
                self.request_stop(f"E:block_limit_not_flat:{err}")
            else:
                self.api_errors += 1
                print(f"[PEG2B_LIVE] skip LIMIT venue gate: {err}", flush=True)
            return

        self.maker_orders += 1
        oid = None
        try:
            if self.real:
                res = self.client.send_order(
                    self.product,
                    intent["side"],
                    self.size,
                    "LIMIT",
                    price=intent["limit_price"],
                )
                oid = (res or {}).get("child_order_acceptance_id")
                if not oid and (res or {}).get("status") != "SIMULATED":
                    self.rejects += 1
                    _append_audit({"event": "ORDER_REJECT", "ts": _iso(), "data": res})
                    self.request_stop("E:order_reject")
                    return
            else:
                oid = f"SIM-{int(time.time() * 1000)}"
            self.acceptance_id = oid
            self.pending = {**intent, "acceptance_id": oid}
            _append_audit({"event": "ORDER_POSTED", "ts": _iso(), "data": self.pending})
            print(
                f"[PEG2B_LIVE] POST LIMIT {intent['side']} {self.size} @ {intent['limit_price']} id={oid}",
                flush=True,
            )
        except OrderSubmitUnknownError as exc:
            self.api_errors += 1
            _append_audit({"event": "ORDER_UNKNOWN", "ts": _iso(), "data": {"err": str(exc)}})
            self.request_stop(f"E:order_submit_unknown:{exc}")
        except Exception as exc:
            self.api_errors += 1
            self.rejects += 1
            _append_audit({"event": "ORDER_ERROR", "ts": _iso(), "data": {"err": str(exc)}})
            self.request_stop(f"E:api_error:{exc}")

    def _on_fill(self, px: float, intent: Dict[str, Any], *, source: str = "FILL") -> None:
        mid = float(intent.get("mid") or px)
        # BUY: positive slippage = paid above mid (adverse)
        slip = (px - mid) / mid * 10_000.0
        self.slippages_bp.append(slip)
        self.maker_fills += 1
        self.position_side = intent["side"]
        self.entry_price = px
        self.entry_time = time.time()
        self.peak_mfe_bp = 0.0
        self.trail_armed = False
        self.pending = None
        self.acceptance_id = None
        event = "MAKER_FILLED_ADOPTED" if source.startswith("ADOPT") else "MAKER_FILLED"
        _append_audit(
            {
                "event": event,
                "ts": _iso(),
                "data": {
                    "fill_price": px,
                    "slippage_bp": slip,
                    "intent": intent,
                    "source": source,
                },
            }
        )
        print(
            f"[PEG2B_LIVE] FILLED({source}) {self.position_side} @ {px} slip={slip:.3f}bp",
            flush=True,
        )

    def _timeout_cancel_and_reconcile(self, board_mid: float) -> None:
        """timeout → cancel → fill確認 → COMPLETEDなら ADOPT（TIMEOUTに数えない）.

        Race that caused TECHNICAL_ABORT: local TIMEOUT_CANCEL while venue COMPLETED.
        """
        intent = self.pending or {}
        oid = self.acceptance_id or intent.get("acceptance_id")
        if not oid:
            self.pending = None
            self.acceptance_id = None
            return

        def _try_adopt(st: Dict[str, Any], stage: str) -> bool:
            if not st.get("filled"):
                return False
            px = float(st.get("avg_px") or intent.get("limit_price") or 0.0)
            if px <= 0:
                return False
            # Partial beyond lot → protect
            exe_sz = float(st.get("executed_size") or 0.0)
            if exec_sz > self.size + 1e-8:
                self.position_mismatch = True
                self.request_stop(f"E:partial_oversize_on_adopt:{exec_sz}")
                self.pending = None
                self.acceptance_id = None
                return True
            self._on_fill(px, intent, source=f"ADOPT_{stage}")
            return True

        # 1) fill確認 BEFORE cancel
        if self.real:
            st0 = self.lookup_order(str(oid))
            if st0.get("error"):
                # ambiguous — do not clear pending; bump api error
                return
            if _try_adopt(st0, "PRE_CANCEL"):
                return

            # 2) cancel
            try:
                self.client.cancel_child_order(self.product, str(oid))
                self.maker_cancels += 1
            except Exception as exc:
                self.api_errors += 1
                print(f"[PEG2B_LIVE] cancel warn: {exc}", flush=True)

            # 3) settle poll (fill may land during/after cancel)
            adopted = False
            final_st: Dict[str, Any] = {}
            for i in range(6):
                time.sleep(0.25)
                final_st = self.lookup_order(str(oid))
                if final_st.get("error"):
                    continue
                if _try_adopt(final_st, f"POST_CANCEL_{i}"):
                    adopted = True
                    break
                if final_st.get("canceled") and not final_st.get("active") and not final_st.get("filled"):
                    break
                if not final_st.get("active") and not final_st.get("filled"):
                    # absent / expired path
                    break

            if adopted:
                return

            # 4) venue reconcile — orphan fill without order lookup certainty
            ok, net, err = self.require_venue_flat(context="post_timeout")
            if err and net is None:
                self.request_stop(f"E:venue_unknown_after_timeout:{err}")
                self.pending = None
                self.acceptance_id = None
                return
            if not ok and net is not None and abs(net) >= 1e-8:
                # Venue long while local flat-pending → ADOPT from venue avg
                avg = 0.0
                _, avg2, _ = self.venue_net()
                avg = float(avg2 or intent.get("limit_price") or 0.0)
                if abs(net - self.size) <= 1e-7 and avg > 0:
                    self._on_fill(avg, intent, source="ADOPT_VENUE_RECONCILE")
                    return
                self.position_mismatch = True
                self.request_stop(f"E:position_mismatch_after_timeout:net={net}")
                self.pending = None
                self.acceptance_id = None
                return

            # true timeout / canceled
            self.maker_timeouts += 1
            attr = {
                "ts": _iso(),
                "reason": "TIMEOUT_3S",
                "limit_price": intent.get("limit_price"),
                "mid": board_mid,
                "spread_bp": intent.get("spread_bp"),
                "imbalance": intent.get("imbalance"),
                "acceptance_id": oid,
                "final_lookup": {
                    k: final_st.get(k)
                    for k in ("active", "filled", "canceled", "rejected", "avg_px", "executed_size")
                },
            }
            self.timeout_attr.append(attr)
            _append_audit({"event": "ORDER_TIMEOUT_CANCEL", "ts": _iso(), "data": attr})
            self.pending = None
            self.acceptance_id = None
            return

        # dry-run path
        self.maker_cancels += 1
        self.maker_timeouts += 1
        attr = {
            "ts": _iso(),
            "reason": "TIMEOUT_3S",
            "limit_price": intent.get("limit_price"),
            "mid": board_mid,
            "spread_bp": intent.get("spread_bp"),
            "imbalance": intent.get("imbalance"),
            "acceptance_id": oid,
        }
        self.timeout_attr.append(attr)
        _append_audit({"event": "ORDER_TIMEOUT_CANCEL", "ts": _iso(), "data": attr})
        self.pending = None
        self.acceptance_id = None

    def _eval_pending(self, now: float, best_bid: float, best_ask: float, mid: float) -> None:
        if not self.pending:
            return
        intent = self.pending
        oid = self.acceptance_id or intent.get("acceptance_id")
        if self.real and oid:
            st = self.lookup_order(str(oid))
            if st.get("error"):
                return
            if st.get("filled"):
                px = float(st.get("avg_px") or intent["limit_price"])
                self._on_fill(px, intent, source="LOOKUP")
                return
            if st.get("rejected"):
                self.rejects += 1
                self.pending = None
                self.acceptance_id = None
                self.request_stop("E:order_reject")
                return
            if st.get("canceled") and not st.get("active"):
                # canceled externally — still reconcile venue in case of race
                ok, net, err = self.require_venue_flat(context="ext_cancel")
                if not ok and net is not None and abs(net - self.size) <= 1e-7:
                    _, avg, _ = self.venue_net()
                    self._on_fill(
                        float(avg or intent["limit_price"]),
                        intent,
                        source="ADOPT_EXT_CANCEL_RACE",
                    )
                    return
                self.pending = None
                self.acceptance_id = None
                return
        else:
            # dry: paper-style touch
            if best_bid >= float(intent["limit_price"]):
                self._on_fill(float(intent["limit_price"]), intent, source="DRY_TOUCH")
                return
        if now >= float(intent["timeout_at"]):
            self._timeout_cancel_and_reconcile(mid)

    def _eval_exit(self, now: float, best_bid: float, best_ask: float, mid: float) -> None:
        if not self.position_side:
            return
        # position mismatch check
        net, _, err = self.venue_net()
        if err:
            self.api_errors += 1
        elif self.real and net is not None:
            expect = self.size if self.position_side == "BUY" else -self.size
            if abs(net - expect) > 1e-7 and abs(net) < 1e-8:
                self.position_mismatch = True
                self.request_stop("E:position_mismatch_flat_while_local_long")
                return
            if abs(net) > self.size + 1e-7:
                self.position_mismatch = True
                self.request_stop("E:position_mismatch_oversize")
                return

        hold = now - self.entry_time
        if self.position_side == "BUY":
            cur_bp = (mid - self.entry_price) / self.entry_price * 10_000.0
        else:
            cur_bp = (self.entry_price - mid) / self.entry_price * 10_000.0
        if cur_bp > self.peak_mfe_bp:
            self.peak_mfe_bp = cur_bp

        exit_reason = None
        exit_px = mid
        if cur_bp >= self.params["target_bp"]:
            exit_reason = "TAKE_PROFIT_15BP"
            exit_px = best_bid if self.position_side == "BUY" else best_ask
        if not exit_reason:
            if self.peak_mfe_bp >= self.params["trail_arm_mfe_bp"] and hold >= self.params["min_hold_before_trail_sec"]:
                self.trail_armed = True
            if self.trail_armed and (self.peak_mfe_bp - cur_bp) >= self.params["trail_stop_bp"]:
                exit_reason = "TRAIL_STOP_HIT"
                exit_px = best_bid if self.position_side == "BUY" else best_ask
        if not exit_reason and hold >= self.params["max_hold_sec"]:
            exit_reason = "MAX_HOLD_TIMEOUT"
            exit_px = best_bid if self.position_side == "BUY" else best_ask

        if not exit_reason:
            return

        # LIVE exit = MARKET (fill quality trial; exit rules frozen)
        side = "SELL" if self.position_side == "BUY" else "BUY"
        try:
            if self.real:
                res = self.client.send_order(self.product, side, self.size, "MARKET")
                oid = (res or {}).get("child_order_acceptance_id")
                px = None
                if oid:
                    px = self.client.get_execution_price_by_acceptance_id(
                        self.product, oid, max_retries=4, retry_interval_sec=0.25
                    )
                exit_px = float(px or exit_px)
            if self.position_side == "BUY":
                pnl_bp = (exit_px - self.entry_price) / self.entry_price * 10_000.0
            else:
                pnl_bp = (self.entry_price - exit_px) / self.entry_price * 10_000.0
            self._close_trade(exit_px, exit_reason, pnl_bp, hold)
            print(f"[PEG2B_LIVE] EXIT {exit_reason} pnl={pnl_bp:.3f}bp", flush=True)
            # Post-exit venue reconcile — must be flat before next LIMIT
            if self.real:
                flat_ok = False
                last_net: Optional[float] = None
                for _ in range(8):
                    time.sleep(0.25)
                    ok, net, err = self.require_venue_flat(context="post_exit")
                    last_net = net
                    if ok:
                        flat_ok = True
                        break
                    if err and net is None:
                        self.api_errors += 1
                if not flat_ok:
                    self.position_mismatch = True
                    self.request_stop(f"E:post_exit_not_flat:net={last_net}")
        except OrderSubmitUnknownError as exc:
            self.api_errors += 1
            self.request_stop(f"E:exit_order_unknown:{exc}")
        except Exception as exc:
            self.api_errors += 1
            self.request_stop(f"E:exit_api_error:{exc}")

    def _maybe_phase_transition(self, m: Dict[str, Any]) -> None:
        now = time.time()
        elapsed = now - self.t0
        # milestone reports
        if elapsed >= 3600.0 and 1 not in self.phase_reports_done:
            self.write_report("trial_1h_report.md", "PEG2-B LIVE Trial · trial_1h_report")
            self.phase_reports_done.add(1)
            ok, reason = self.phase1_continue_ok(m)
            _append_audit({"event": "PHASE1_GATE", "ts": _iso(), "data": {"ok": ok, "reason": reason, "m": m}})
            try:
                self.notifier.post_peg2b_live_trial({**m, "phase_gate": {"ok": ok, "reason": reason}}, kind="phase1")
            except Exception:
                pass
            if not ok:
                self.request_stop(f"PHASE1_NO_CONTINUE:{reason}")
                return
            self.phase = 2
            self.phase_deadline = self.t0 + PHASE_SEC[1] + PHASE_SEC[2]
            print(f"[PEG2B_LIVE] → Phase-2 ({reason})", flush=True)

        if elapsed >= 3600.0 + 7200.0 and 3 not in self.phase_reports_done:
            # 3h cumulative report
            self.write_report("trial_3h_report.md", "PEG2-B LIVE Trial · trial_3h_report")
            self.phase_reports_done.add(3)
            ok, reason = self.phase_good_ok(m)
            _append_audit({"event": "PHASE2_GATE", "ts": _iso(), "data": {"ok": ok, "reason": reason, "m": m}})
            if not ok:
                self.request_stop(f"PHASE2_NO_CONTINUE:{reason}")
                return
            self.phase = 3
            self.phase_deadline = self.t0 + MAX_TOTAL_SEC
            print(f"[PEG2B_LIVE] → Phase-3 ({reason})", flush=True)

        if elapsed >= MAX_TOTAL_SEC:
            self.write_report("trial_6h_report.md", "PEG2-B LIVE Trial · trial_6h_report")
            self.phase_reports_done.add(6)
            live_ev = float(m["ev_bp"])
            paper_ev = float(m["paper_ev_bp_window"])
            # Review Only — never auto-promote to standing LIVE
            promote_review = live_ev >= (paper_ev - EV_GE_EPS_BP)
            body = OUT / "LIVE_PROMOTE_REVIEW.md"
            body.write_text(
                self._report_body(m, "PEG2-B LIVE Trial · Review Only（昇格は別審査）")
                + f"\n## Verdict\n\n- review_only=True\n"
                + f"- live_promote=False (auto forbidden)\n"
                + f"- submit_for_separate_review={promote_review}\n"
                + f"- criterion hint: Live EV >= Paper EV - {EV_GE_EPS_BP}bp\n"
                + f"- live_ev={live_ev} paper_ev={paper_ev}\n",
                encoding="utf-8",
            )
            try:
                self.notifier.post_peg2b_live_trial(
                    {**m, "promote_review": promote_review}, kind="final"
                )
            except Exception:
                pass
            self.request_stop("MAX_6H_COMPLETE")

    def save_state(self) -> None:
        m = self.metrics()
        _write_json(
            STATE_PATH,
            {
                "metrics": m,
                "params_frozen": self.params,
                "paper0": self.paper0,
                "pending": self.pending,
                "position_side": self.position_side,
                "entry_price": self.entry_price,
                "trades_tail": self.trades[-10:],
            },
        )

    def run(self) -> int:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "reports").mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
        _write_json(
            LOCK_PATH,
            {
                "csr": "CSR-588",
                "lane": "PEG2B_LIVE_TRIAL_V1",
                "status": "RUNNING",
                "real": self.real,
                "size_btc": self.size,
                "frozen": list(PARAMS.keys()),
                "forbidden": [
                    "CSR-521",
                    "Alpha_vs_Adverse",
                    "PEG_v3",
                    "TF2B_10.15",
                    "4AGENT_Candidate",
                ],
                "started_jst": _jst(),
                "pid": os.getpid(),
            },
        )

        # boot reconcile: must be flat
        net, avg, err = self.venue_net()
        if err:
            print(f"[PEG2B_LIVE] REFUSE boot venue error: {err}", flush=True)
            return 2
        if net is not None and abs(net) >= 1e-8:
            print(f"[PEG2B_LIVE] REFUSE boot not flat venue_net={net} avg={avg}", flush=True)
            return 2
        if self.real:
            try:
                active = self.client.get_child_orders(self.product, "ACTIVE") or []
            except Exception as exc:
                print(f"[PEG2B_LIVE] REFUSE boot open-orders error: {exc}", flush=True)
                return 2
            if active:
                print(f"[PEG2B_LIVE] REFUSE boot ACTIVE orders={len(active)}", flush=True)
                return 2

        m0 = self.metrics()
        try:
            self.notifier.post_peg2b_live_trial(m0, kind="start")
        except Exception as exc:
            print(f"[PEG2B_LIVE] discord start warn: {exc}", flush=True)
        _append_audit({"event": "START", "ts": _iso(), "data": m0})
        print(
            f"[PEG2B_LIVE] START real={self.real} size={self.size} phase=1 "
            f"deadline_phase1={PHASE_SEC[1]}s frozen=PEG2-B feed=shared_feeder+REST",
            flush=True,
        )

        def _sig(signum, _frame):
            self.request_stop(f"signal_{signum}")

        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

        self.feeder.start()
        last_snap = None
        try:
            while self.running:
                if STOP_FLAG.exists():
                    self.request_stop("stop_flag")
                    break
                if (time.time() - self.t0) >= MAX_TOTAL_SEC:
                    self._maybe_phase_transition(self.metrics())
                    break

                now = time.time()
                best_bid = best_ask = mid = imb = 0.0
                snap = self.feeder.get(timeout=self.interval)
                if snap is not None:
                    last_snap = snap
                    self.feed_source = "shared_feeder"
                if last_snap is not None:
                    best_bid = float(last_snap.best_bid or 0.0)
                    best_ask = float(last_snap.best_ask or 0.0)
                    mid = float(last_snap.mid_price or 0.0)
                    imb = float(last_snap.imbalance or 0.0)
                    if mid <= 0 and best_bid > 0 and best_ask > 0:
                        mid = (best_bid + best_ask) / 2.0
                if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask or mid <= 0:
                    # REST fallback when feeder stale
                    try:
                        board = _fetch_board(self.product)
                        buy_v, sell_v = _fetch_exec_vols(self.product)
                        bids = board.get("bids") or []
                        asks = board.get("asks") or []
                        best_bid = float(bids[0]["price"]) if bids else 0.0
                        best_ask = float(asks[0]["price"]) if asks else 0.0
                        mid = float(board.get("mid_price") or 0.0) or (
                            (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
                        )
                        tot = buy_v + sell_v
                        imb = ((buy_v - sell_v) / tot) if tot > 0 else 0.0
                        self.feed_source = "rest_fallback"
                    except Exception as exc:
                        self.api_errors += 1
                        print(f"[PEG2B_LIVE] board error: {exc}", flush=True)
                        time.sleep(self.interval)
                        continue

                if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask or mid <= 0:
                    time.sleep(0.2)
                    continue

                self.price_history.append(mid)
                if len(self.price_history) > 300:
                    self.price_history.pop(0)

                if self.position_side:
                    self._eval_exit(now, best_bid, best_ask, mid)
                elif self.pending:
                    self._eval_pending(now, best_bid, best_ask, mid)
                else:
                    intent = self._signal(now, best_bid, best_ask, mid, imb)
                    if intent:
                        self._post_limit(intent)

                m = self.metrics()
                m["feed_source"] = self.feed_source
                stop = self.check_stops(m)
                if stop:
                    self.request_stop(stop)
                    break

                self._maybe_phase_transition(m)
                if not self.running:
                    break

                if now - self.last_report_ts >= REPORT_EVERY_SEC:
                    self.emit_periodic()
                    self.last_report_ts = now

                if int(now) % 10 == 0:
                    self.save_state()

                time.sleep(0.2)
        finally:
            try:
                self.feeder.stop()
            except Exception:
                pass

        # shutdown: FLAT + reports
        flat = self.flatten(self.stop_reason or "shutdown")
        m = self.metrics()
        stop_path = OUT / "stop_report.md"
        stop_path.write_text(
            self._report_body(m, "PEG2-B LIVE Trial · STOP / FLAT")
            + "\n## Flatten\n\n```json\n"
            + json.dumps(flat, ensure_ascii=False, indent=2)
            + "\n```\n",
            encoding="utf-8",
        )
        # ensure milestone stubs if stopped early
        if 1 not in self.phase_reports_done and (time.time() - self.t0) >= 3500:
            self.write_report("trial_1h_report.md", "PEG2-B LIVE Trial · trial_1h_report (early/stop)")
        self.save_state()
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8")) if LOCK_PATH.exists() else {}
        lock.update({"status": "STOPPED", "stop_reason": self.stop_reason, "stopped_jst": _jst()})
        _write_json(LOCK_PATH, lock)
        try:
            self.notifier.post_peg2b_live_trial({**m, "flatten": flat}, kind="stop")
        except Exception:
            pass
        if PID_PATH.exists():
            try:
                PID_PATH.unlink()
            except Exception:
                pass
        print(f"[PEG2B_LIVE] EXIT reason={self.stop_reason} flat={flat.get('flat_ok')}", flush=True)
        return 0 if (flat.get("flat_ok") and not str(self.stop_reason).startswith("E:")) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="PEG2-B LIVE Trial v1 (CSR-588)")
    ap.add_argument("--size", type=float, default=0.001)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--confirm-go", default="")
    ap.add_argument("--symbol", default="FX_BTC_JPY")
    args = ap.parse_args()

    others = _foreign_pids(NEEDLE)
    if others:
        print(f"REFUSE: peg2b live trial already running pids={others}", file=sys.stderr)
        return 2
    soak = _foreign_pids(SOAK_NEEDLE)
    if soak:
        print(f"REFUSE: TF2BP soak runner active pids={soak}", file=sys.stderr)
        return 2

    if args.real:
        if not GO_FLAG.exists():
            print(f"REFUSE: missing {GO_FLAG}", file=sys.stderr)
            return 2
        if os.environ.get("CONFIRM_GO") != "PEG2B_LIVE_TRIAL_V1":
            print("REFUSE: set CONFIRM_GO=PEG2B_LIVE_TRIAL_V1", file=sys.stderr)
            return 2
        if os.environ.get("ENABLE_REAL_TRADING", "false").lower() != "true":
            print("REFUSE: ENABLE_REAL_TRADING is not true (do not set from .env writer)", file=sys.stderr)
            return 2
        if args.confirm_go != CONFIRM_TOKEN:
            print(f"REFUSE: --confirm-go {CONFIRM_TOKEN} required", file=sys.stderr)
            return 2
    else:
        os.environ["ENABLE_REAL_TRADING"] = "false"

    trial = Peg2BLiveTrial(size=args.size, real=args.real, interval=args.interval, product=args.symbol)
    return trial.run()


if __name__ == "__main__":
    raise SystemExit(main())
