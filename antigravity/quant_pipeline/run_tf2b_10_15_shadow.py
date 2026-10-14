#!/usr/bin/env python3
"""
TF2B_10.15 Shadow Validation — State A hypothesis
=================================================
High Imbalance + Low Ask Depth + High Bid Depth

WIRE=NO · ENFORCE=0 · no orders · no LIVE/PAPER/PEG2/TF2BP/UMM changes
Directive: docs/ops/tf2b_10_15_shadow_validation_directive.md
"""
from __future__ import annotations

import json
import math
import os
import signal
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

BASE_DIR = "/home/azureuser/antigravity"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.quant_pipeline.event_bus import EventBus
from antigravity.quant_pipeline.parquet_logger import ParquetBatchLogger
from antigravity.quant_pipeline.ingestion import MarketDataIngestion
from antigravity.quant_pipeline.feeder_board_source import FeederBoardSource
from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier

JST = timezone(timedelta(hours=9))
OUT = Path(BASE_DIR) / "data" / "mm_research" / "tf2b_10_15_shadow"
LOG_PATH = Path(BASE_DIR) / "logs" / "tf2b_10_15_shadow.log"
PID_PATH = OUT / "tf2b_10_15_shadow.pid"
LANE_LOCK = OUT / "LANE_LOCK.json"

# Frozen calibration (no mid-run retune)
CALIB_WINDOW = 300
CALIB_MIN = 30
STATE_A_COOLDOWN_SEC = 30.0
STATE_B_COOLDOWN_SEC = 30.0
BASELINE_PROBE_SEC = 120.0
DISCORD_INTERVAL_SEC = float(os.environ.get("TF2B_1015_DISCORD_INTERVAL_SEC", "3600"))

# Virtual Baseline Exit (TF2BP constants — do not change)
TP_BP = 15.0
TRAIL_STOP_BP = 4.0
TRAIL_ARM_MFE_BP = 4.0
MIN_HOLD_BEFORE_TRAIL_SEC = 8.0
MAX_HOLD_SEC = 1200.0


def _pct(xs: List[float], q: float) -> float:
    if not xs:
        return float("nan")
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    pos = max(0.0, min(1.0, q)) * (len(ys) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ys[lo]
    w = pos - lo
    return ys[lo] * (1.0 - w) + ys[hi] * w


def _tercile(v: float, lo: float, hi: float) -> str:
    if not math.isfinite(v) or not math.isfinite(lo) or not math.isfinite(hi):
        return "unk"
    if v <= lo:
        return "low"
    if v >= hi:
        return "high"
    return "mid"


def _mean(xs: List[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 4) if xs else None


def _median(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    if n % 2:
        return round(ys[mid], 4)
    return round(0.5 * (ys[mid - 1] + ys[mid]), 4)


def _regime(spread_bp: float, mom_abs: float) -> str:
    # lightweight proxy — frozen heuristic, not optimized
    vol = "high_vol" if spread_bp >= 2.0 or mom_abs >= 3.0 else "low_vol"
    trend = "trend" if mom_abs >= 1.5 else "range"
    return f"{trend}+{vol}"


@dataclass
class OpenSignal:
    signal_id: str
    state_id: str  # A | B | BASELINE
    ts: float
    symbol: str
    imbalance: float
    bid_depth: float
    ask_depth: float
    micro_dev: float
    spread: float
    spread_bp: float
    taker_buy: float
    taker_sell: float
    entry_price: float
    side: str  # buy for +imb state
    regime: str
    flags: Dict[str, Any] = field(default_factory=dict)
    mfe_bp: float = 0.0
    mae_bp: float = 0.0
    path: List[Tuple[float, float]] = field(default_factory=list)
    # virtual exit
    peak_mfe: float = 0.0
    trail_armed: bool = False
    exited: bool = False
    exit_ts: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    virtual_pnl_bp: Optional[float] = None
    futures: Dict[str, Optional[float]] = field(default_factory=dict)


class TF2B1015Shadow:
    def __init__(self, symbol: str = "FX_BTC_JPY", interval: float = 1.0):
        self.symbol = symbol
        self.interval = interval
        self.running = True
        self.start_time = time.time()
        self.ticks = 0
        self.obs_count = 0
        self.signal_count = 0
        self.state_a_count = 0
        self.state_b_count = 0
        self.baseline_count = 0
        self.validation_ready = 0
        self.last_state_a = 0.0
        self.last_state_b = 0.0
        self.last_baseline = 0.0
        self.last_persist = 0.0
        self.last_discord = 0.0
        self.last_daily_key = ""
        self.last_verdict = "MORE_DATA"
        self.latest_signal: Optional[Dict[str, Any]] = None

        self.imb_hist: Deque[float] = deque(maxlen=CALIB_WINDOW)
        self.ask_hist: Deque[float] = deque(maxlen=CALIB_WINDOW)
        self.bid_hist: Deque[float] = deque(maxlen=CALIB_WINDOW)
        self.mid_hist: Deque[Tuple[float, float]] = deque(maxlen=120)
        self.cuts = {"imb": None, "ask": None, "bid": None}

        self.open: List[OpenSignal] = []
        self.completed: List[Dict[str, Any]] = []

        OUT.mkdir(parents=True, exist_ok=True)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.signals_jsonl = OUT / "signals.jsonl"
        self.notifier = QuantDiscordNotifier()

        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=30.0, batch_size=100)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code=symbol)
        self.feeder = FeederBoardSource(self.ingestion)

        PID_PATH.write_text(str(os.getpid()))
        self._log("START TF2B_10.15 Shadow · State A validation · WIRE=NO")

    def _log(self, msg: str) -> None:
        print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

    def stop(self) -> None:
        self.running = False

    def _update_cuts(self, imb: float, ask: float, bid: float) -> None:
        if math.isfinite(imb):
            self.imb_hist.append(imb)
        if math.isfinite(ask) and ask > 0:
            self.ask_hist.append(ask)
        if math.isfinite(bid) and bid > 0:
            self.bid_hist.append(bid)
        if len(self.imb_hist) >= CALIB_MIN:
            xs = list(self.imb_hist)
            self.cuts["imb"] = (_pct(xs, 1 / 3), _pct(xs, 2 / 3))
        if len(self.ask_hist) >= CALIB_MIN:
            xs = list(self.ask_hist)
            self.cuts["ask"] = (_pct(xs, 1 / 3), _pct(xs, 2 / 3))
        if len(self.bid_hist) >= CALIB_MIN:
            xs = list(self.bid_hist)
            self.cuts["bid"] = (_pct(xs, 1 / 3), _pct(xs, 2 / 3))

    def _classify(self, imb: float, ask: float, bid: float) -> Dict[str, Any]:
        ready = all(self.cuts[k] is not None for k in ("imb", "ask", "bid"))
        if not ready:
            return {
                "ready": False,
                "imb_tier": "unk",
                "ask_tier": "unk",
                "bid_tier": "unk",
                "state_a": False,
                "state_b": False,
            }
        ilo, ihi = self.cuts["imb"]
        alo, ahi = self.cuts["ask"]
        blo, bhi = self.cuts["bid"]
        imb_t = _tercile(imb, ilo, ihi)
        ask_t = _tercile(ask, alo, ahi)
        bid_t = _tercile(bid, blo, bhi)
        high_imb = imb_t == "high"
        low_ask = ask_t == "low"
        high_bid = bid_t == "high"
        return {
            "ready": True,
            "imb_tier": imb_t,
            "ask_tier": ask_t,
            "bid_tier": bid_t,
            "state_a": bool(high_imb and low_ask and high_bid),
            "state_b": bool(high_imb),  # High Imb only (coverage arm per directive)
            "cuts": {
                "imb": {"low": ilo, "high": ihi},
                "ask": {"low": alo, "high": ahi},
                "bid": {"low": blo, "high": bhi},
            },
        }

    def _signed_bp(self, side: str, entry: float, mid: float) -> float:
        raw = (mid - entry) / entry * 10000.0
        return raw if side == "buy" else -raw

    def _fire(self, state_id: str, now: float, mid: float, feat: Dict[str, Any], flags: Dict[str, Any]) -> None:
        sid = f"{state_id}_{int(now*1000)}_{self.signal_count}"
        mom = 0.0
        if len(self.mid_hist) >= 2:
            t0, m0 = self.mid_hist[0]
            if now - t0 >= 1:
                mom = abs((mid - m0) / m0 * 10000.0)
        sig = OpenSignal(
            signal_id=sid,
            state_id=state_id,
            ts=now,
            symbol=self.symbol,
            imbalance=feat["imbalance"],
            bid_depth=feat["bid_depth"],
            ask_depth=feat["ask_depth"],
            micro_dev=feat["micro_dev"],
            spread=feat["spread"],
            spread_bp=feat["spread_bp"],
            taker_buy=feat["taker_buy"],
            taker_sell=feat["taker_sell"],
            entry_price=mid,
            side="buy",  # State A/B as defined are long-imbalance states
            regime=_regime(feat["spread_bp"], mom),
            flags=flags,
        )
        sig.path.append((now, mid))
        self.open.append(sig)
        self.signal_count += 1
        if state_id == "A":
            self.state_a_count += 1
            self.last_state_a = now
        elif state_id == "B":
            self.state_b_count += 1
            self.last_state_b = now
        else:
            self.baseline_count += 1
            self.last_baseline = now
        self.latest_signal = {
            "signal_id": sid,
            "state_id": state_id,
            "ts_jst": datetime.fromtimestamp(now, JST).strftime("%Y-%m-%d %H:%M:%S"),
            "imbalance": feat["imbalance"],
            "bid_depth": feat["bid_depth"],
            "ask_depth": feat["ask_depth"],
            "spread_bp": feat["spread_bp"],
        }
        self._log(f"SIGNAL {state_id} imb={feat['imbalance']:+.3f} bid={feat['bid_depth']:.4f} ask={feat['ask_depth']:.4f}")

    def _update_open(self, now: float, mid: float, best_bid: float, best_ask: float) -> None:
        still: List[OpenSignal] = []
        for sig in self.open:
            if sig.exited:
                continue
            bp = self._signed_bp(sig.side, sig.entry_price, mid)
            sig.path.append((now, mid))
            if bp > sig.mfe_bp:
                sig.mfe_bp = bp
            if bp < sig.mae_bp:
                sig.mae_bp = bp
            if bp > sig.peak_mfe:
                sig.peak_mfe = bp

            age = now - sig.ts
            # futures marks
            for name, horizon in (("future_30s", 30), ("future_60s", 60), ("future_120s", 120), ("future_240s", 240)):
                if name not in sig.futures and age + 1e-9 >= horizon:
                    # mark nearest mid at/after horizon
                    marks = [m for t, m in sig.path if t >= sig.ts + horizon]
                    mark = marks[0] if marks else mid
                    sig.futures[name] = round(self._signed_bp(sig.side, sig.entry_price, mark), 4)

            # virtual baseline exit
            exit_reason = None
            exit_px = mid
            if bp >= TP_BP:
                exit_reason = "TAKE_PROFIT_15BP"
                exit_px = best_bid if sig.side == "buy" else best_ask
            else:
                if sig.peak_mfe >= TRAIL_ARM_MFE_BP and age >= MIN_HOLD_BEFORE_TRAIL_SEC:
                    sig.trail_armed = True
                if sig.trail_armed and (sig.peak_mfe - bp) >= TRAIL_STOP_BP:
                    exit_reason = "TRAIL_STOP"
                    exit_px = best_bid if sig.side == "buy" else best_ask
                elif age >= MAX_HOLD_SEC:
                    exit_reason = "MAX_HOLD"
                    exit_px = best_bid if sig.side == "buy" else best_ask

            # complete when 240s futures ready (primary research horizon) or exited
            if exit_reason:
                sig.exited = True
                sig.exit_ts = now
                sig.exit_price = exit_px
                sig.exit_reason = exit_reason
                sig.virtual_pnl_bp = round(self._signed_bp(sig.side, sig.entry_price, exit_px), 4)

            if "future_240s" in sig.futures or sig.exited:
                # if exited early, still fill remaining futures with last mid
                for name, horizon in (("future_30s", 30), ("future_60s", 60), ("future_120s", 120), ("future_240s", 240)):
                    if name not in sig.futures:
                        if age + 1e-9 >= horizon or sig.exited:
                            sig.futures[name] = round(bp, 4)
                if "future_120s" in sig.futures:
                    self._complete(sig)
                    continue
            still.append(sig)
        self.open = still

    def _complete(self, sig: OpenSignal) -> None:
        row = {
            "timestamp": sig.ts,
            "timestamp_jst": datetime.fromtimestamp(sig.ts, JST).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": sig.symbol,
            "signal_id": sig.signal_id,
            "state_id": sig.state_id,
            "imbalance": sig.imbalance,
            "bid_depth": sig.bid_depth,
            "ask_depth": sig.ask_depth,
            "micro_dev": sig.micro_dev,
            "spread": sig.spread,
            "spread_bp": sig.spread_bp,
            "taker_buy": sig.taker_buy,
            "taker_sell": sig.taker_sell,
            "entry_price": sig.entry_price,
            "future_30s": sig.futures.get("future_30s"),
            "future_60s": sig.futures.get("future_60s"),
            "future_120s": sig.futures.get("future_120s"),
            "future_240s": sig.futures.get("future_240s"),
            "MFE": round(sig.mfe_bp, 4),
            "MAE": round(sig.mae_bp, 4),
            "virtual_pnl": sig.virtual_pnl_bp,
            "exit_reason": sig.exit_reason,
            "regime": sig.regime,
            "flags": sig.flags,
            "hit_mfe5_120": bool((sig.mfe_bp >= 5.0) and (sig.futures.get("future_120s") is not None)),
        }
        self.completed.append(row)
        if sig.state_id == "A" and row["future_120s"] is not None:
            self.validation_ready = sum(
                1 for r in self.completed if r["state_id"] == "A" and r.get("future_120s") is not None
            )
        with self.signals_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _metrics_for(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {"n": 0}
        r120 = [r["future_120s"] for r in rows if r.get("future_120s") is not None]
        r240 = [r["future_240s"] for r in rows if r.get("future_240s") is not None]
        hits = [1 for r in rows if r.get("hit_mfe5_120")]
        return {
            "n": len(rows),
            "hit_rate": round(sum(hits) / len(rows), 4) if rows else None,
            "ret120": _mean(r120),
            "ret240": _mean(r240),
            "median_ret120": _median(r120),
            "avg_mfe": _mean([r["MFE"] for r in rows]),
            "avg_mae": _mean([r["MAE"] for r in rows]),
            "avg_virtual_pnl": _mean([r["virtual_pnl"] for r in rows if r.get("virtual_pnl") is not None]),
        }

    def _judge(self) -> Dict[str, Any]:
        a_all = [r for r in self.completed if r["state_id"] == "A" and r.get("future_120s") is not None]
        n = len(a_all)
        # chronological discovery / validation split
        half = n // 2
        disc = a_all[:half]
        val = a_all[half:]
        m_disc = self._metrics_for(disc)
        m_val = self._metrics_for(val)
        m_all = self._metrics_for(a_all)
        b_all = self._metrics_for([r for r in self.completed if r["state_id"] == "B" and r.get("future_120s") is not None])
        base = self._metrics_for([r for r in self.completed if r["state_id"] == "BASELINE" and r.get("future_120s") is not None])

        n_val = m_val.get("n", 0)
        if n_val < 20:
            verdict = "MORE_DATA"
            reason = "Validation N<20 · 結論禁止"
        elif n_val < 50:
            verdict = "MORE_DATA"
            reason = "Validation N<50 · PENDING"
        else:
            # promotion checks (need n_val>=100 for PROMOTE)
            same_sign = (
                m_disc.get("ret120") is not None
                and m_val.get("ret120") is not None
                and (m_disc["ret120"] > 0) == (m_val["ret120"] > 0)
            )
            mfe_adv = (
                m_val.get("avg_mfe") is not None
                and base.get("avg_mfe") is not None
                and m_val["avg_mfe"] > base["avg_mfe"]
            )
            if (
                n_val >= 100
                and same_sign
                and (m_val.get("ret120") or 0) > 0
                and (m_val.get("ret240") or 0) > 0
                and mfe_adv
            ):
                verdict = "PROMOTE_TO_PAPER"
                reason = "Validation N>=100 · same-sign · Ret120/240>0 · MFE优势"
            elif (m_val.get("ret120") is not None and m_val["ret120"] <= 0 and n_val >= 100):
                verdict = "REJECT"
                reason = "Validation Ret120<=0 with N>=100"
            else:
                verdict = "MORE_DATA"
                reason = "初回判定待ち / 条件未充足"

        return {
            "verdict": verdict,
            "reason": reason,
            "state_a": {"discovery": m_disc, "validation": m_val, "all": m_all},
            "state_b": b_all,
            "baseline": base,
            "sample_guard": {
                "validation_n": n_val,
                "conclusion_forbidden": n_val < 20,
                "pending": n_val < 50,
                "first_judge_ok": n_val >= 100,
                "promote_review_ok": n_val >= 200,
            },
        }

    def _spread_bucket(self, bp: float) -> str:
        if bp < 1.2:
            return "<1.2"
        if bp < 1.6:
            return "1.2-1.6"
        if bp < 2.0:
            return "1.6-2.0"
        return ">=2.0"

    def _failure_analysis(self) -> Dict[str, Any]:
        fails = [
            r for r in self.completed
            if r["state_id"] == "A" and r.get("future_120s") is not None and r["future_120s"] <= 0
        ]
        wins = [
            r for r in self.completed
            if r["state_id"] == "A" and r.get("future_120s") is not None and r["future_120s"] > 0
        ]
        def pack(rows):
            if not rows:
                return {"n": 0}
            return {
                "n": len(rows),
                "avg_spread_bp": _mean([r["spread_bp"] for r in rows]),
                "avg_micro_dev": _mean([r["micro_dev"] for r in rows]),
                "avg_imbalance": _mean([r["imbalance"] for r in rows]),
                "avg_taker_buy": _mean([r["taker_buy"] for r in rows]),
                "avg_taker_sell": _mean([r["taker_sell"] for r in rows]),
            }
        return {"failures": pack(fails), "successes": pack(wins)}

    def _persist(self, final: bool = False) -> Dict[str, Any]:
        judge = self._judge()
        # regime / spread breakdown for State A
        a_rows = [r for r in self.completed if r["state_id"] == "A" and r.get("future_120s") is not None]
        by_reg: Dict[str, List] = {}
        by_sp: Dict[str, List] = {}
        for r in a_rows:
            by_reg.setdefault(r.get("regime") or "unk", []).append(r)
            by_sp.setdefault(self._spread_bucket(float(r.get("spread_bp") or 0)), []).append(r)

        health = {
            "updated_at_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "elapsed_hours": round((time.time() - self.start_time) / 3600.0, 3),
            "ticks": self.ticks,
            "observations": self.obs_count,
            "signals": self.signal_count,
            "state_a_count": self.state_a_count,
            "state_b_count": self.state_b_count,
            "baseline_count": self.baseline_count,
            "validation_labeled_n": judge["sample_guard"]["validation_n"],
            "open_signals": len(self.open),
            "latest_signal": self.latest_signal,
            "calib_ready": all(self.cuts[k] is not None for k in ("imb", "ask", "bid")),
            "wire": "NO",
            "enforce": 0,
        }
        summary = {
            **health,
            "verdict": judge["verdict"],
            "reason": judge["reason"],
            "sample_guard": judge["sample_guard"],
            "coverage": {
                "A": judge["state_a"],
                "B": judge["state_b"],
                "Baseline": judge["baseline"],
            },
            "regime": {k: self._metrics_for(v) for k, v in sorted(by_reg.items())},
            "spread_buckets": {k: self._metrics_for(v) for k, v in sorted(by_sp.items())},
            "failure_analysis": self._failure_analysis(),
            "comparison_note": "TF2B_10.10 / 11.3 reference stats are offline; live Shadow compares A vs B vs Baseline arms.",
            "final": final,
        }

        (OUT / "state.json").write_text(json.dumps({"health": health, "cuts": {
            k: ({"low": v[0], "high": v[1]} if v else None) for k, v in self.cuts.items()
        }}, ensure_ascii=False, indent=2))
        (OUT / "health.json").write_text(json.dumps(health, ensure_ascii=False, indent=2))
        (OUT / "tf2b_10_15_daily_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

        day = datetime.now(JST).strftime("%Y-%m-%d")
        daily_dir = OUT / "daily"
        daily_dir.mkdir(exist_ok=True)
        (daily_dir / f"{day}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

        self._write_report(summary)
        self._write_parquet()
        self._update_lane_lock(judge["verdict"])
        return summary

    def _write_report(self, summary: Dict[str, Any]) -> None:
        a = summary["coverage"]["A"]
        lines = [
            "# TF2B_10.15 Shadow Report — State A Validation",
            f"Updated: {summary['updated_at_jst']}",
            f"Elapsed: {summary['elapsed_hours']}h · WIRE=NO · ENFORCE=0",
            "",
            f"## Verdict: **{summary['verdict']}**",
            f"Reason: {summary['reason']}",
            f"Sample guard: {summary['sample_guard']}",
            "",
            "## Health",
            f"- observations={summary['observations']} signals={summary['signals']}",
            f"- StateA={summary['state_a_count']} StateB={summary['state_b_count']} Baseline={summary['baseline_count']}",
            f"- latest={summary['latest_signal']}",
            "",
            "## Coverage",
            f"- A discovery={a['discovery']}",
            f"- A validation={a['validation']}",
            f"- A all={a['all']}",
            f"- B={summary['coverage']['B']}",
            f"- Baseline={summary['coverage']['Baseline']}",
            "",
            "## Regime",
            json.dumps(summary.get("regime"), ensure_ascii=False, indent=2),
            "",
            "## Spread buckets",
            json.dumps(summary.get("spread_buckets"), ensure_ascii=False, indent=2),
            "",
            "## Failure analysis (State A)",
            json.dumps(summary.get("failure_analysis"), ensure_ascii=False, indent=2),
            "",
            "PROMOTE_TO_PAPER ≠ LIVE. No signal/threshold retune.",
        ]
        (OUT / "tf2b_10_15_shadow_report.md").write_text("\n".join(lines) + "\n")

    def _write_parquet(self) -> None:
        if not self.completed:
            return
        try:
            import pandas as pd
            df = pd.json_normalize(self.completed)
            df.to_parquet(OUT / "tf2b_10_15_signals.parquet", index=False)
            a = df[df["state_id"] == "A"].copy() if "state_id" in df.columns else df
            # validation = second chronological half of A with ret120
            if len(a) > 0 and "future_120s" in a.columns:
                a = a[a["future_120s"].notna()].sort_values("timestamp")
                half = len(a) // 2
                val = a.iloc[half:]
                val.to_parquet(OUT / "tf2b_10_15_validation.parquet", index=False)
        except Exception as e:
            self._log(f"parquet write skip: {e}")

    def _update_lane_lock(self, verdict: str) -> None:
        lock = {
            "lane": "TF2B_10.15 Shadow Validation",
            "status": "RUNNING" if self.running else "STOPPED",
            "strategy_id": "TF2B_10.15",
            "hypothesis": "State A = High Imb + Low Ask Depth + High Bid Depth",
            "wire": "NO",
            "enforce": 0,
            "auto_apply": False,
            "verdict": verdict,
            "pid": os.getpid(),
            "directive": "docs/ops/tf2b_10_15_shadow_validation_directive.md",
            "forbidden": [
                "LIVE", "PAPER", "PEG2", "TF2BP", "UMM", "orders",
                "signal_retune", "threshold_retune", "state_def_change",
                "execution_change", "exit_change", "mid_run_optimization",
            ],
            "portfolio": {
                "LIVE": "TF2BP Baseline",
                "PAPER": "PEG2-B",
                "RESEARCH-1": "UMM+Maker Shadow",
                "RESEARCH-2": "TF2B_10.15 Shadow",
                "REJECT": "PEG_v3 / AgingGuard",
            },
            "updated_at_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        }
        LANE_LOCK.write_text(json.dumps(lock, ensure_ascii=False, indent=2))

    def _discord(self, summary: Dict[str, Any], kind: str) -> None:
        try:
            ok = self.notifier.post_tf2b_10_15_shadow(summary, kind=kind)
            self._log(f"discord {kind} ok={ok} verdict={summary.get('verdict')}")
            self.last_discord = time.time()
            self.last_verdict = str(summary.get("verdict"))
        except Exception as e:
            self._log(f"discord error: {e}")

    def run(self) -> None:
        self.feeder.start()
        self._update_lane_lock("MORE_DATA")
        boot = self._persist(False)
        self.last_daily_key = datetime.now(JST).strftime("%Y-%m-%d")  # avoid start+daily double ping
        self._discord(boot, "start")

        def _sig(sig, _f):
            self._log(f"signal {sig} → stop")
            self.stop()

        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

        last_snap = None
        while self.running:
            try:
                now = time.time()
                snap = self.feeder.get(timeout=self.interval)
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
                if bid <= 0 or ask <= 0 or bid >= ask or mid <= 0:
                    time.sleep(0.2)
                    continue

                self.ticks += 1
                self.obs_count += 1
                imb = float(getattr(last_snap, "imbalance", 0.0) or 0.0)
                ask_d = float(getattr(last_snap, "total_ask_depth", 0.0) or getattr(last_snap, "ask_depth_1", 0.0) or 0.0)
                bid_d = float(getattr(last_snap, "total_bid_depth", 0.0) or getattr(last_snap, "bid_depth_1", 0.0) or 0.0)
                if ask_d <= 0:
                    ask_d = float(getattr(last_snap, "ask_depth_1", 0.0) or 0.0)
                if bid_d <= 0:
                    bid_d = float(getattr(last_snap, "bid_depth_1", 0.0) or 0.0)
                micro = float(getattr(last_snap, "micro_dev", 0.0) or 0.0)
                spread = ask - bid
                spread_bp = spread / mid * 10000.0
                taker_buy = float(getattr(last_snap, "taker_volume_bid", 0.0) or 0.0)
                taker_sell = float(getattr(last_snap, "taker_volume_ask", 0.0) or 0.0)

                self.mid_hist.append((now, mid))
                self._update_cuts(imb, ask_d, bid_d)
                flags = self._classify(imb, ask_d, bid_d)
                feat = {
                    "imbalance": imb,
                    "bid_depth": bid_d,
                    "ask_depth": ask_d,
                    "micro_dev": micro,
                    "spread": spread,
                    "spread_bp": spread_bp,
                    "taker_buy": taker_buy,
                    "taker_sell": taker_sell,
                }

                if flags.get("ready"):
                    if flags["state_a"] and (now - self.last_state_a) >= STATE_A_COOLDOWN_SEC:
                        self._fire("A", now, mid, feat, flags)
                    elif flags["state_b"] and (now - self.last_state_b) >= STATE_B_COOLDOWN_SEC:
                        # B-only fires when A is false (avoid double-count identical A rows as B)
                        if not flags["state_a"]:
                            self._fire("B", now, mid, feat, flags)
                    if (now - self.last_baseline) >= BASELINE_PROBE_SEC:
                        self._fire("BASELINE", now, mid, feat, flags)

                self._update_open(now, mid, bid, ask)

                if now - self.last_persist >= 15.0:
                    summary = self._persist(False)
                    self.last_persist = now
                    day = datetime.now(JST).strftime("%Y-%m-%d")
                    kind = None
                    if self.last_daily_key != day:
                        self.last_daily_key = day
                        kind = "daily"
                    elif summary["verdict"] != self.last_verdict and summary["verdict"] in ("REJECT", "PROMOTE_TO_PAPER"):
                        kind = "verdict"
                    elif now - self.last_discord >= DISCORD_INTERVAL_SEC:
                        kind = "periodic"
                    if kind:
                        self._discord(summary, kind)

                time.sleep(0.25)
            except Exception as e:
                self._log(f"ERROR {e}\n{traceback.format_exc()}")
                time.sleep(2.0)

        summary = self._persist(True)
        self._discord(summary, "final")
        try:
            self.feeder.stop()
        except Exception:
            pass
        if PID_PATH.exists():
            try:
                PID_PATH.unlink()
            except Exception:
                pass
        self._log("stopped cleanly")


def main():
    import argparse
    p = argparse.ArgumentParser(description="TF2B_10.15 State A Shadow Validation")
    p.add_argument("--symbol", default="FX_BTC_JPY")
    p.add_argument("--interval", type=float, default=1.0)
    args = p.parse_args()
    TF2B1015Shadow(symbol=args.symbol, interval=args.interval).run()


if __name__ == "__main__":
    main()
