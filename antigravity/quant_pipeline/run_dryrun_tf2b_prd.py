#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TF2B_PRD Dry-Run — Predictability Frontier Profiler-guided TF2BP (WIRE=NO).

Directive (CSR-602 / TF2B_PRD):
  計測器（Predictability Frontier: micro_dev, imbalance, adverse risk）を活用し、
  1. 不利な状況でなければエントリー（有利/平穏局面のみ参加）
  2. ポジション維持（順行+2bp期待を保持）
  3. 不利な状況への変化を検知したら早期ポジション精算（Adverse Cut）
  4. 1時間ごとに Discord Webhook へ進捗通知

Paired lanes:
  - TF2BP_CTRL: frozen TF2BP baseline (unfiltered)
  - TF2B_PRD: Predictability Frontier Profiler-guided TF2BP
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

from antigravity.quant_pipeline.adverse_score_engine import AdverseScoreEngine  # noqa: E402
from antigravity.quant_pipeline.event_bus import EventBus  # noqa: E402
from antigravity.quant_pipeline.feeder_board_source import (  # noqa: E402
    FeederBoardSource,
    resolve_market_source,
    SOURCE_SHARED,
)
from antigravity.quant_pipeline.ingestion import LiveBoardFeed, MarketDataIngestion  # noqa: E402
from antigravity.quant_pipeline.liquidity_vacuum_agent import LiquidityVacuumAgent  # noqa: E402
from antigravity.quant_pipeline.market_shock_sentinel import MarketShockSentinel  # noqa: E402
from antigravity.quant_pipeline.parquet_logger import ParquetBatchLogger  # noqa: E402
from antigravity.strategies.tf2bp_strategy import TF2BPStrategy  # noqa: E402

JST = timezone(timedelta(hours=9))
OUT_DIR = AG / "data" / "mm_research" / "tf2b_prd_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = AG / "data" / "dryrun_tf2b_prd_state.json"
PID_PATH = AG / "data" / "dryrun_tf2b_prd.pid"
LOG_PATH = AG / "logs" / "dryrun_tf2b_prd.log"
LEDGER_PATH = OUT_DIR / "paired_ledger.jsonl"
AUDIT_PATH = OUT_DIR / "audit_events.jsonl"

DEFAULT_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1489176387586883596/"
    "gpGv06d8dVlHi0h6ZsYxC1S5maZhfpJPoP-ZUbswlXTZ_8nbEp71973OQOaz2BcfSrk4"
)
# Note: do not fall back to generic DISCORD_WEBHOOK_URL in .env which points to another channel
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_PRD_WEBHOOK_URL", DEFAULT_WEBHOOK_URL)


def _now_jst() -> datetime:
    return datetime.now(JST)


def _iso() -> str:
    return _now_jst().strftime("%Y-%m-%d %H:%M:%S JST")


def _log(msg: str) -> None:
    line = f"[{_iso()}] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except Exception:
        pass


def _append(p: Path, obj: Dict[str, Any]) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception as exc:
        _log(f"append failed {p.name}: {exc}")


def _load_tf2bp_cfg() -> Dict[str, Any]:
    candidates = [
        AG / "configs" / "approved_arena_config.json",
        AG / "configs" / "umm_tf2bp_config.json",
    ]
    for c in candidates:
        if c.exists():
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
                for s in data.get("strategies", []):
                    if s.get("name") in ("TF2BP_v1", "TF2BP"):
                        return s.get("params", {})
            except Exception:
                pass
    return {
        "order_size_btc": 0.001,
        "spread_threshold_bp": 1.2,
        "raw_score_threshold": 0.55,
        "p2_threshold": 0.50,
        "sl5_bp": 2.0,
        "cost_bp": 0.0,
    }


def _sharpe_dd(hist: List[Dict[str, Any]]) -> Dict[str, float]:
    if not hist:
        return {"sharpe": 0.0, "max_dd_bp": 0.0}
    returns = [float(r.get("pnl_bp") or 0.0) for r in hist]
    n = len(returns)
    if n < 2:
        return {"sharpe": 0.0, "max_dd_bp": min(0.0, returns[0]) if n else 0.0}
    m = sum(returns) / n
    var = sum((x - m) ** 2 for x in returns) / (n - 1)
    std = math.sqrt(var) if var > 1e-12 else 0.0
    sharpe = (m / std) if std > 1e-9 else 0.0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        cum += r
        if cum > peak:
            peak = cum
        dd = cum - peak
        if dd < max_dd:
            max_dd = dd
    return {"sharpe": round(sharpe, 4), "max_dd_bp": round(max_dd, 2)}


def _window_stats(hist: List[Dict[str, Any]], hours: float) -> Dict[str, Any]:
    cutoff = time.time() - hours * 3600.0
    sub = [r for r in hist if float(r.get("ts") or 0) >= cutoff]
    n = len(sub)
    if not n:
        return {
            "window_hours": hours,
            "total_trades": 0,
            "win_trades": 0,
            "loss_trades": 0,
            "win_rate_pct": 0.0,
            "pnl_jpy": 0.0,
            "pnl_bp": 0.0,
            "ev_bp": 0.0,
            "sharpe": 0.0,
            "max_dd_bp": 0.0,
        }
    wins = sum(1 for r in sub if bool(r.get("is_win")))
    losses = n - wins
    pnl_jpy = round(sum(float(r.get("pnl_jpy") or 0.0) for r in sub), 1)
    pnl_bp = round(sum(float(r.get("pnl_bp") or 0.0) for r in sub), 2)
    ev = round(pnl_bp / n, 4)
    sd = _sharpe_dd(sub)
    return {
        "window_hours": hours,
        "total_trades": n,
        "win_trades": wins,
        "loss_trades": losses,
        "win_rate_pct": round(wins / n * 100.0, 1),
        "pnl_jpy": pnl_jpy,
        "pnl_bp": pnl_bp,
        "ev_bp": ev,
        "sharpe": sd["sharpe"],
        "max_dd_bp": sd["max_dd_bp"],
    }


class TF2BPrdDryRun:
    def __init__(
        self,
        symbol: str = "FX_BTC_JPY",
        interval_sec: float = 1.0,
        report_interval_sec: float = 3600.0,  # 1 hour
        market_source: str = "shared_feeder",
    ):
        self.symbol = symbol
        self.interval_sec = float(interval_sec)
        self.report_interval_sec = float(report_interval_sec)
        self.market_source = resolve_market_source(market_source)
        self.running = True
        self.t0 = time.time()
        self.last_report = self.t0
        self.last_state_save = 0.0

        cfg = _load_tf2bp_cfg()
        self.ctrl = TF2BPStrategy(cfg)
        self.prd = TF2BPStrategy(cfg)

        self.adverse_engine = AdverseScoreEngine()
        self.vacuum_agent = LiquidityVacuumAgent()
        self.market_shock = MarketShockSentinel()

        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=60.0, batch_size=200)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code=symbol)
        if self.market_source == SOURCE_SHARED:
            self.feed: Any = FeederBoardSource(self.ingestion)
        else:
            self.feed = LiveBoardFeed(self.ingestion, product_code=symbol)

        self.hist_ctrl: List[Dict[str, Any]] = []
        self.hist_prd: List[Dict[str, Any]] = []
        self._restore_ledger()

        self.stats = {
            "signals_ctrl": 0,
            "signals_prd": 0,
            "prd_allowed": 0,
            "prd_rejected": 0,
            "early_exits_prd": 0,
            "normal_exits_prd": 0,
            "entries_ctrl": 0,
            "entries_prd": 0,
            "exits_ctrl": 0,
            "exits_prd": 0,
        }
        self._last_prd_verdict: Optional[Dict[str, Any]] = None

    def stop(self, *_a) -> None:
        self.running = False

    def _restore_ledger(self) -> None:
        if not LEDGER_PATH.exists():
            return
        cutoff = time.time() - 72 * 3600.0
        try:
            lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
        except Exception:
            return
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(row.get("ts") or 0) < cutoff:
                continue
            lane = str(row.get("lane") or "")
            if lane == "TF2B_PRD":
                self.hist_prd.append(row)
            elif lane == "TF2BP_CTRL":
                self.hist_ctrl.append(row)

    def _extract_metrics(self, snap: Any, now: float) -> Dict[str, float]:
        best_bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        best_ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        mid = float(getattr(snap, "mid_price", 0.0) or ((best_bid + best_ask) / 2.0 if best_bid > 0 else 1.0))
        spread_bp = ((best_ask - best_bid) / best_bid * 1e4) if best_bid > 0 else 2.0
        imbalance = float(getattr(snap, "imbalance", 0.0) or 0.0)
        micro_dev = float(getattr(snap, "micro_dev", 0.0) or 0.0)
        micro_dev_bp = (micro_dev / mid * 1e4) if mid > 0 else 0.0
        cancel_rate = float(getattr(snap, "cancel_rate", 0.0) or 0.0)
        latency = float(getattr(snap, "latency_ms", 15.0) or 15.0)

        vac = self.vacuum_agent.on_tick(snap, now=now)
        vac_score = float(vac.get("vacuum_score", 0.0) or 0.0)
        adv_dict = self.adverse_engine.calculate_adverse_score(
            ae_1s=None,
            ae_3s=None,
            mae_bp=None,
            toxic_score=max(0.0, (latency - 20.0) * 0.5 + cancel_rate * 30.0),
            capture_rate_pct=60.0,
            latency_ms=latency,
            inventory_btc=0.0,
            holding_time_sec=0.0,
            micro_dev=micro_dev,
        )
        adv_score = float(adv_dict.get("adverse_score", 30.0) or 30.0)

        return {
            "spread_bp": round(spread_bp, 2),
            "imbalance": round(imbalance, 4),
            "micro_dev_bp": round(micro_dev_bp, 4),
            "vacuum_score": round(vac_score, 4),
            "adverse_score": round(adv_score, 2),
            "mid_price": mid,
        }

    def _eval_prd_entry_condition(self, side: str, m: Dict[str, float]) -> Tuple[bool, str]:
        """
        計測器（Predictability Frontier Profiler）に基づく不利判定:
        - micro_dev (20-30s極短期) が逆行
        - imbalance (60-300s) が逆行
        - spread_bp が広すぎて2bp到達困難 (>2.5bp)
        - adverse_score が高すぎて逆選択リスク大 (>40.0)
        """
        reasons = []
        if m["spread_bp"] > 2.5:
            reasons.append(f"SPREAD_TOO_WIDE ({m['spread_bp']:.1f}bp > 2.5bp)")
        if m["adverse_score"] > 40.0:
            reasons.append(f"HIGH_ADVERSE_RISK ({m['adverse_score']:.1f} > 40)")
        if m["vacuum_score"] > 0.40:
            reasons.append(f"LIQUIDITY_VACUUM ({m['vacuum_score']:.2f} > 0.4)")

        if side == "buy":
            if m["micro_dev_bp"] < -0.25:
                reasons.append(f"MICRO_DEV_REVERSE ({m['micro_dev_bp']:+.2f}bp < -0.25bp)")
            if m["imbalance"] < -0.20:
                reasons.append(f"SELL_IMBALANCE_DOMINANT ({m['imbalance']:+.2f} < -0.20)")
        elif side == "sell":
            if m["micro_dev_bp"] > 0.25:
                reasons.append(f"MICRO_DEV_REVERSE ({m['micro_dev_bp']:+.2f}bp > +0.25bp)")
            if m["imbalance"] > 0.20:
                reasons.append(f"BUY_IMBALANCE_DOMINANT ({m['imbalance']:+.2f} > +0.20)")

        if reasons:
            return False, "; ".join(reasons)
        return True, "FAVORABLE_FRONTIER"

    def _eval_prd_position_exit_condition(
        self, side: str, entry_price: float, current_mid: float, m: Dict[str, float]
    ) -> Tuple[bool, str]:
        """
        ポジション保有中の不利検知（Early Adverse Cut）:
        ポジション維持に耐えられない板悪化・急激な逆行圧力を検知した場合に早期精算
        """
        if entry_price <= 0 or current_mid <= 0:
            return False, ""

        unrealized_bp = (current_mid / entry_price - 1.0) * 1e4 if side == "buy" else (1.0 - current_mid / entry_price) * 1e4

        reasons = []
        # 1. 致命的板崩壊
        if m["spread_bp"] >= 3.0:
            reasons.append(f"SPREAD_BLOWOUT ({m['spread_bp']:.1f}bp >= 3.0bp)")
        if m["adverse_score"] >= 50.0:
            reasons.append(f"SEVERE_ADVERSE ({m['adverse_score']:.1f} >= 50)")

        # 2. 逆行圧力が明白かつ含み損進行
        if side == "buy":
            if m["micro_dev_bp"] <= -0.40 and m["imbalance"] <= -0.25:
                reasons.append(f"STRONG_OPPOSING_FLOW (mdev={m['micro_dev_bp']:+.2f} imb={m['imbalance']:+.2f})")
            if unrealized_bp <= -1.0 and m["micro_dev_bp"] < -0.15:
                reasons.append(f"UNFAVORABLE_DRIFT (unrealized={unrealized_bp:+.1f}bp mdev={m['micro_dev_bp']:+.2f})")
        elif side == "sell":
            if m["micro_dev_bp"] >= 0.40 and m["imbalance"] >= 0.25:
                reasons.append(f"STRONG_OPPOSING_FLOW (mdev={m['micro_dev_bp']:+.2f} imb={m['imbalance']:+.2f})")
            if unrealized_bp <= -1.0 and m["micro_dev_bp"] > 0.15:
                reasons.append(f"UNFAVORABLE_DRIFT (unrealized={unrealized_bp:+.1f}bp mdev={m['micro_dev_bp']:+.2f})")

        if reasons:
            return True, "; ".join(reasons)
        return False, "HOLD_FAVORABLE"

    def _apply_action(
        self,
        name: str,
        strat: TF2BPStrategy,
        res: Dict[str, Any],
        snap: Any,
        now: float,
        metrics: Dict[str, float],
    ) -> None:
        action = res.get("action", "hold")

        # Entry Handling
        if action in ("buy", "sell") and not strat.position_side:
            if name == "TF2BP_CTRL":
                self.stats["signals_ctrl"] += 1
            else:
                self.stats["signals_prd"] += 1
                allowed, reason = self._eval_prd_entry_condition(action, metrics)
                self._last_prd_verdict = {
                    "allowed": allowed,
                    "reason": reason,
                    "metrics": metrics,
                    "side": action,
                    "ts": _iso(),
                }
                if not allowed:
                    self.stats["prd_rejected"] += 1
                    _append(
                        AUDIT_PATH,
                        {
                            "event": "PRD_ENTRY_REJECT",
                            "ts": _iso(),
                            "data": {
                                "side": action,
                                "tf2bp_reason": res.get("reason"),
                                "prd_reason": reason,
                                "metrics": metrics,
                            },
                        },
                    )
                    return
                self.stats["prd_allowed"] += 1
                res = dict(res)
                res["reason"] = f"{res.get('reason')} [PRD ALLOW: {reason}]"

            fill = float(res.get("entry_price") or snap.mid_price)
            strat.record_trade(action, fill, 0.0, mid_price=float(snap.mid_price or fill))
            if name == "TF2B_PRD":
                self.stats["entries_prd"] += 1
                _append(
                    AUDIT_PATH,
                    {
                        "event": "ENTRY",
                        "ts": _iso(),
                        "data": {
                            "lane": name,
                            "side": action,
                            "price": fill,
                            "reason": res.get("reason"),
                            "metrics": metrics,
                        },
                    },
                )
            else:
                self.stats["entries_ctrl"] += 1
                _append(
                    AUDIT_PATH,
                    {
                        "event": "ENTRY",
                        "ts": _iso(),
                        "data": {"lane": name, "side": action, "price": fill, "reason": res.get("reason")},
                    },
                )
            _log(f"[{name}] ENTRY {action.upper()} @ {fill:,.0f} ({res.get('reason')})")

        # Normal Exit from TF2BP Logic
        elif action in ("exit", "cancel") and strat.position_side:
            fill = float(snap.mid_price or 0.0)
            pnl = float(res.get("pnl") or res.get("expected_pnl") or 0.0)
            reason = str(res.get("reason") or action)
            strat.record_trade(action, fill, pnl, mid_price=float(snap.mid_price or fill))
            last = strat.trades_history[-1] if strat.trades_history else {}
            row = {
                "ts": float(last.get("ts") or now),
                "lane": name,
                "side": last.get("side"),
                "exit_side": last.get("exit_side"),
                "fill_price": last.get("fill_price"),
                "pnl_bp": float(last.get("pnl_bp") or 0.0),
                "pnl_jpy": float(last.get("pnl_jpy") or 0.0),
                "is_win": bool(last.get("is_win")),
                "exit_reason": reason,
            }
            _append(LEDGER_PATH, row)
            if name == "TF2B_PRD":
                self.hist_prd.append(row)
                self.stats["exits_prd"] += 1
                self.stats["normal_exits_prd"] += 1
            else:
                self.hist_ctrl.append(row)
                self.stats["exits_ctrl"] += 1
            _append(AUDIT_PATH, {"event": "EXIT", "ts": _iso(), "data": row})
            _log(f"[{name}] NORMAL EXIT {reason} pnl={row['pnl_bp']:+.2f}bp")

    def _check_early_adverse_exit(self, snap: Any, now: float, metrics: Dict[str, float]) -> None:
        """TF2B_PRD lane: check if running position should be cut early due to adverse conditions."""
        if not self.prd.position_side:
            return

        side = self.prd.position_side
        entry = self.prd.entry_price
        mid = float(snap.mid_price or metrics["mid_price"])

        should_cut, cut_reason = self._eval_prd_position_exit_condition(side, entry, mid, metrics)
        if should_cut:
            # Force Early Cut
            fill = mid
            gross_bp = (fill / entry - 1.0) * 1e4 if side == "buy" else (1.0 - fill / entry) * 1e4
            net_bp = gross_bp - float(self.prd.params.get("cost_bp", 0.0))
            pnl_jpy = self.prd._yen(net_bp)

            self.prd.record_trade("exit", fill, pnl_jpy, mid_price=fill)
            last = self.prd.trades_history[-1] if self.prd.trades_history else {}
            row = {
                "ts": float(last.get("ts") or now),
                "lane": "TF2B_PRD",
                "side": last.get("side"),
                "exit_side": "early_cut",
                "fill_price": fill,
                "pnl_bp": float(last.get("pnl_bp") or net_bp),
                "pnl_jpy": float(last.get("pnl_jpy") or pnl_jpy),
                "is_win": (net_bp > 0),
                "exit_reason": f"PRD_ADVERSE_CUT ({cut_reason})",
            }
            _append(LEDGER_PATH, row)
            self.hist_prd.append(row)
            self.stats["exits_prd"] += 1
            self.stats["early_exits_prd"] += 1
            _append(AUDIT_PATH, {"event": "PRD_EARLY_CUT", "ts": _iso(), "data": row})
            _log(f"[TF2B_PRD] ⚡ EARLY ADVERSE CUT pnl={net_bp:+.2f}bp ({cut_reason})")

    def _tick_pair(self, snap: Any, now: float) -> None:
        metrics = self._extract_metrics(snap, now)
        common = dict(
            mid_price=float(snap.mid_price or 0.0),
            best_bid=float(snap.best_bid or 0.0),
            best_ask=float(snap.best_ask or 0.0),
            taker_vol_bid=float(snap.taker_volume_bid or 0.0),
            taker_vol_ask=float(snap.taker_volume_ask or 0.0),
            bid_depth_1=float(snap.bid_depth_1 or 0.0),
            ask_depth_1=float(snap.ask_depth_1 or 0.0),
            now=now,
        )

        # 1. Check early adverse exit for TF2B_PRD if holding
        self._check_early_adverse_exit(snap, now, metrics)

        # 2. Run normal TF2BP tick
        res_c = self.ctrl.on_tick(**common)
        self._apply_action("TF2BP_CTRL", self.ctrl, res_c, snap, now, metrics)

        res_p = self.prd.on_tick(**common)
        self._apply_action("TF2B_PRD", self.prd, res_p, snap, now, metrics)

    def _lane_block(self, name: str, strat: TF2BPStrategy, hist: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = int(strat.total_trades)
        wr = (strat.win_trades / n) if n else 0.0
        ev = (strat.total_pnl_bp / n) if n else 0.0
        sd = _sharpe_dd(hist)
        s1 = _window_stats(hist, 1.0)
        s24 = _window_stats(hist, 24.0)
        return {
            "trades": n,
            "wins": int(strat.win_trades),
            "win_rate": round(wr, 4),
            "pnl_bp": round(float(strat.total_pnl_bp), 4),
            "pnl_jpy": round(float(strat.total_pnl), 2),
            "ev_bp": round(ev, 4),
            "sharpe": sd["sharpe"],
            "max_dd_bp": sd["max_dd_bp"],
            "position": strat.position_side or "FLAT",
            "stats_1h": s1,
            "stats_24h": s24,
        }

    def metrics(self) -> Dict[str, Any]:
        c = self._lane_block("TF2BP_CTRL", self.ctrl, self.hist_ctrl)
        p = self._lane_block("TF2B_PRD", self.prd, self.hist_prd)
        delta_pnl = round(p["pnl_bp"] - c["pnl_bp"], 3)
        delta_ev = round(p["ev_bp"] - c["ev_bp"], 3)
        delta_sharpe = round(p["sharpe"] - c["sharpe"], 4)
        delta_dd = round(p["max_dd_bp"] - c["max_dd_bp"], 2)  # higher is better

        s1_c, s1_p = c["stats_1h"], p["stats_1h"]
        s24_c, s24_p = c["stats_24h"], p["stats_24h"]

        return {
            "ts_jst": _iso(),
            "model": "TF2B_PRD",
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "market_source": self.market_source,
            "wire": "NO",
            "stack": "TF2BP × Predictability Profiler (micro_dev + imbalance + adverse cut)",
            "TF2BP_CTRL": c,
            "TF2B_PRD": p,
            "compare": {
                "delta_pnl_bp": delta_pnl,
                "delta_ev_bp": delta_ev,
                "delta_sharpe": delta_sharpe,
                "delta_max_dd_bp": delta_dd,
                "delta_1h_pnl_bp": round(s1_p["pnl_bp"] - s1_c["pnl_bp"], 2),
                "delta_24h_pnl_bp": round(s24_p["pnl_bp"] - s24_c["pnl_bp"], 2),
                "delta_1h_sharpe": round(s1_p["sharpe"] - s1_c["sharpe"], 4),
                "delta_24h_sharpe": round(s24_p["sharpe"] - s24_c["sharpe"], 4),
                "delta_1h_max_dd_bp": round(s1_p["max_dd_bp"] - s1_c["max_dd_bp"], 2),
                "delta_24h_max_dd_bp": round(s24_p["max_dd_bp"] - s24_c["max_dd_bp"], 2),
                "hypothesis": "Predictability Profiler filters adverse entries & early cuts bad trades",
            },
            "gate": {
                "signals_ctrl": self.stats["signals_ctrl"],
                "signals_prd": self.stats["signals_prd"],
                "prd_allowed": self.stats["prd_allowed"],
                "prd_rejected": self.stats["prd_rejected"],
                "early_exits_prd": self.stats["early_exits_prd"],
                "normal_exits_prd": self.stats["normal_exits_prd"],
                "entries_ctrl": self.stats["entries_ctrl"],
                "entries_prd": self.stats["entries_prd"],
                "exits_ctrl": self.stats["exits_ctrl"],
                "exits_prd": self.stats["exits_prd"],
                "prd_allow_rate": round(
                    self.stats["prd_allowed"] / self.stats["signals_prd"], 4
                )
                if self.stats["signals_prd"]
                else 1.0,
            },
            "latest_prd_verdict": self._last_prd_verdict,
        }

    def save_state(self) -> None:
        m = self.metrics()
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(STATE_PATH)
        self.last_state_save = time.time()

    def send_discord_report(self, kind: str = "hourly") -> None:
        m = self.metrics()
        c = m["TF2BP_CTRL"]
        p = m["TF2B_PRD"]
        cmp = m["compare"]
        gate = m["gate"]

        title_map = {
            "start": "🚀 [START] TF2B_PRD DryRun 観測開始",
            "hourly": f"⏱️ [HOURLY #{int(m['elapsed_hours'])}] TF2B_PRD 定期進捗レポート",
            "final": "🏁 [FINAL] TF2B_PRD DryRun 完了レポート",
        }
        title = title_map.get(kind, f"📊 [REPORT] TF2B_PRD {kind}")

        # Color: Green if delta_pnl >= 0 else Orange
        color = 0x2ECC71 if cmp["delta_pnl_bp"] >= 0 else 0xE67E22

        fields = [
            {
                "name": "📊 累積成績比較 (All-Time)",
                "value": (
                    f"• **TF2B_PRD**: `{p['pnl_bp']:+.2f} bp` ({p['pnl_jpy']:+.1f}円) · "
                    f"WR: `{p['win_rate']*100:.1f}%` ({p['wins']}/{p['trades']}) · "
                    f"Sharpe: `{p['sharpe']:+.3f}` · MaxDD: `{p['max_dd_bp']:.1f}bp`\n"
                    f"• **TF2BP_CTRL**: `{c['pnl_bp']:+.2f} bp` ({c['pnl_jpy']:+.1f}円) · "
                    f"WR: `{c['win_rate']*100:.1f}%` ({c['wins']}/{c['trades']}) · "
                    f"Sharpe: `{c['sharpe']:+.3f}` · MaxDD: `{c['max_dd_bp']:.1f}bp`\n"
                    f"• **差分 (Δ)**: PnL: **`{cmp['delta_pnl_bp']:+.2f} bp`** | "
                    f"Sharpe: **`{cmp['delta_sharpe']:+.3f}`** | "
                    f"MaxDD: **`{cmp['delta_max_dd_bp']:+.2f} bp`**"
                ),
                "inline": False,
            },
            {
                "name": "⏱️ 直近 1時間 成績 (1h Stats)",
                "value": (
                    f"• **PRD (1h)**: `{p['stats_1h']['pnl_bp']:+.2f} bp` | "
                    f"WR: `{p['stats_1h']['win_rate_pct']:.1f}%` ({p['stats_1h']['win_trades']}/{p['stats_1h']['total_trades']}) | "
                    f"Sharpe: `{p['stats_1h']['sharpe']:+.3f}`\n"
                    f"• **CTRL (1h)**: `{c['stats_1h']['pnl_bp']:+.2f} bp` | "
                    f"WR: `{c['stats_1h']['win_rate_pct']:.1f}%` ({c['stats_1h']['win_trades']}/{c['stats_1h']['total_trades']}) | "
                    f"Sharpe: `{c['stats_1h']['sharpe']:+.3f}`\n"
                    f"• **1h 差分**: PnL: **`{cmp['delta_1h_pnl_bp']:+.2f} bp`** | Sharpe: **`{cmp['delta_1h_sharpe']:+.3f}`**"
                ),
                "inline": False,
            },
            {
                "name": "🛡️ PRD 計測器ゲート統計",
                "value": (
                    f"• TF2BP シグナル発生: `{gate['signals_prd']}` 件\n"
                    f"• **有利エントリー許可**: `{gate['prd_allowed']}` 件 (通過率 `{gate['prd_allow_rate']*100:.1f}%`)\n"
                    f"• **不利エントリー拒否**: `{gate['prd_rejected']}` 件 (回避)\n"
                    f"• **不利検知早期精算 (Early Cut)**: `{gate['early_exits_prd']}` 件\n"
                    f"• 通常エグジット: `{gate['normal_exits_prd']}` 件"
                ),
                "inline": True,
            },
            {
                "name": "⚙️ 稼働ステータス",
                "value": (
                    f"• 稼働時間: `{m['elapsed_hours']:.2f}` 時間\n"
                    f"• 執行モード: `WIRE=NO (Paper/DryRun)`\n"
                    f"• PRD ポジション: `{p['position']}`\n"
                    f"• CTRL ポジション: `{c['position']}`"
                ),
                "inline": True,
            },
        ]

        payload = {
            "username": "TF2B_PRD Reporter",
            "embeds": [
                {
                    "title": title,
                    "description": (
                        "**Predictability Frontier Profiler（2bp先回り計測器）** を適用した TF2BP DryRun。\n"
                        "「不利な状況でのエントリー回避」＋「保有中の不利検知早期精算」をリアルタイム実測中。"
                    ),
                    "color": color,
                    "fields": fields,
                    "footer": {
                        "text": f"Antigravity Quants · CSR-602 · {m['ts_jst']}",
                    },
                }
            ],
        }

        try:
            req = urllib.request.Request(
                DISCORD_WEBHOOK_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                _log(f"[DISCORD] {kind} report sent (HTTP {resp.status})")
        except Exception as exc:
            _log(f"[DISCORD] Warning: Failed to send report: {exc}")

    def run(self) -> int:
        _log("=== TF2B_PRD DryRun START (WIRE=NO) ===")
        _log(f"Market source: {self.market_source} | Report interval: {self.report_interval_sec:.0f}s")
        PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")

        self.feed.start()
        self.save_state()
        self.send_discord_report("start")

        last_snap = None
        try:
            while self.running:
                now = time.time()
                try:
                    snap = self.feed.get(timeout=self.interval_sec)
                except Exception:
                    time.sleep(self.interval_sec)
                    continue

                if snap is not None:
                    last_snap = snap
                if last_snap is None:
                    time.sleep(0.2)
                    continue

                best_b = getattr(last_snap, "best_bid", None)
                best_a = getattr(last_snap, "best_ask", None)
                if not (best_b and best_a and best_b < best_a):
                    time.sleep(0.2)
                    continue

                self._tick_pair(last_snap, now)
                if now - self.last_state_save >= 1.0:
                    self.save_state()
                    self.last_state_save = now

                if now - self.last_report >= self.report_interval_sec:
                    self.send_discord_report("hourly")
                    self.last_report = now

                time.sleep(0.05)
        except KeyboardInterrupt:
            _log("KeyboardInterrupt caught, stopping...")
        except Exception as exc:
            _log(f"FATAL Exception in run loop: {exc}")
            import traceback
            traceback.print_exc()
            return 1
        finally:
            self.running = False
            try:
                self.feed.stop()
            except Exception:
                pass
            self.save_state()
            self.send_discord_report("final")
            if PID_PATH.exists():
                try:
                    PID_PATH.unlink()
                except Exception:
                    pass
            _log("=== TF2B_PRD DryRun STOPPED ===")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="TF2B_PRD DryRun Runner")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    args = parser.parse_args()

    runner = TF2BPrdDryRun(
        interval_sec=args.interval,
        report_interval_sec=args.report_interval,
        market_source=args.source,
    )

    def _sig_handler(sig, _frame):
        _log(f"Received signal {sig}, exiting gracefully...")
        runner.stop()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    rc = runner.run()
    sys.exit(rc)


if __name__ == "__main__":
    main()
