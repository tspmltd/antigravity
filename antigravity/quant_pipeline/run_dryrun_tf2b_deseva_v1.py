#!/usr/bin/env python3
"""TF2B_DeSeva_v1 Dry-Run — TF2BP Baseline × SEVA high-EV filter (WIRE=NO).

Hypothesis (CSR-599 → live dryrun proof):
  SEVA が高評価した局面だけ TF2BP を残すと、Baseline より Sharpe↑ / MaxDD↓ するか。

Paired lanes:
  - TF2BP_CTRL: frozen TF2BP baseline (no SEVA)
  - TF2B_DeSeva_v1: same TF2BP signal/exit, enter only if SEVA.TF2BP.entry_allowed

Discord: start + periodic(900s) + final · ENABLE_REAL_TRADING=false.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
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
from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier  # noqa: E402
from antigravity.quant_pipeline.strategy_expected_value_agent import (  # noqa: E402
    StrategyExpectedValueAgent,
)
from antigravity.strategies.tf2bp_strategy import TF2BPStrategy  # noqa: E402

JST = timezone(timedelta(hours=9))
OUT = AG / "data" / "mm_research" / "tf2b_deseva_v1_dryrun"
STATE_PATH = AG / "data" / "dryrun_tf2b_deseva_v1_state.json"
PID_PATH = AG / "data" / "dryrun_tf2b_deseva_v1.pid"
LOG_PATH = AG / "logs" / "dryrun_tf2b_deseva_v1.log"
AUDIT_PATH = OUT / "audit.jsonl"
LEDGER_PATH = OUT / "trades_ledger.jsonl"
COMPARE_PATH = OUT / "compare_latest.json"
CONFIG_PATH = AG / "configs" / "umm_tf2bp_config.json"
STOP_FLAG = AG / "data" / "TF2B_DESEVA_V1_DRYRUN_STOP.flag"
SEVA_MODEL_DIR = AG / "data" / "mm_research" / "seva" / "models"


def _jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def _iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    line = f"[{_jst()}] {msg}"
    print(line, flush=True)
    # When stdout is already redirected to LOG_PATH (nohup start script), skip file write.
    try:
        out = Path("/proc/self/fd/1").resolve()
        if out == LOG_PATH.resolve():
            return
    except Exception:
        pass
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _append(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_tf2bp_cfg() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return dict(data.get("tf2bp_config") or {})
        except Exception:
            pass
    return {}


def _sharpe_dd(trades: List[Dict[str, Any]]) -> Dict[str, float]:
    """Per-trade Sharpe (mean/std) and max drawdown on cum pnl_bp equity."""
    pnls = [float(t.get("pnl_bp") or 0.0) for t in trades]
    n = len(pnls)
    if n == 0:
        return {"sharpe": 0.0, "max_dd_bp": 0.0, "n": 0}
    mean = sum(pnls) / n
    if n == 1:
        sharpe = 0.0
    else:
        var = sum((x - mean) ** 2 for x in pnls) / (n - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / std) if std > 1e-12 else 0.0
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in pnls:
        eq += x
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    return {"sharpe": round(sharpe, 4), "max_dd_bp": round(max_dd, 4), "n": n}


def _window_stats(trades: List[Dict[str, Any]], hours: float) -> Dict[str, Any]:
    now = time.time()
    cutoff = now - hours * 3600.0
    recent = [t for t in trades if float(t.get("ts") or 0) >= cutoff]
    n = len(recent)
    wins = sum(1 for t in recent if t.get("is_win"))
    pnl_bp = sum(float(t.get("pnl_bp") or 0) for t in recent)
    pnl_jpy = sum(float(t.get("pnl_jpy") or 0) for t in recent)
    wr = (wins / n * 100.0) if n else 0.0
    sd = _sharpe_dd(recent)
    return {
        "window_hours": hours,
        "total_trades": n,
        "win_trades": wins,
        "loss_trades": n - wins,
        "win_rate_pct": round(wr, 1),
        "pnl_jpy": round(pnl_jpy, 1),
        "pnl_bp": round(pnl_bp, 2),
        "ev_bp": round(pnl_bp / n, 4) if n else 0.0,
        "sharpe": sd["sharpe"],
        "max_dd_bp": sd["max_dd_bp"],
    }


class Tf2bDesevaV1DryRun:
    def __init__(
        self,
        *,
        symbol: str = "FX_BTC_JPY",
        interval_sec: float = 1.0,
        report_interval_sec: float = 900.0,
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
        self.deseva = TF2BPStrategy(cfg)

        self.seva = StrategyExpectedValueAgent(
            model_dir=str(SEVA_MODEL_DIR),
            min_ev_bp=0.20,
            min_confidence=0.65,
        )
        self.adverse_engine = AdverseScoreEngine()
        self.vacuum_agent = LiquidityVacuumAgent()
        self.market_shock = MarketShockSentinel()

        self.notifier = QuantDiscordNotifier()
        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=60.0, batch_size=200)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code=symbol)
        if self.market_source == SOURCE_SHARED:
            self.feed: Any = FeederBoardSource(self.ingestion)
        else:
            self.feed = LiveBoardFeed(self.ingestion, product_code=symbol)

        self.hist_ctrl: List[Dict[str, Any]] = []
        self.hist_deseva: List[Dict[str, Any]] = []
        self._restore_ledger()

        self.stats = {
            "signals_ctrl": 0,
            "signals_deseva": 0,
            "seva_allowed": 0,
            "seva_rejected": 0,
            "entries_ctrl": 0,
            "entries_deseva": 0,
            "exits_ctrl": 0,
            "exits_deseva": 0,
            "seva_models": list(self.seva.models.keys()),
        }
        self._last_seva: Optional[Dict[str, Any]] = None

    def stop(self, *_a) -> None:
        self.running = False

    def _restore_ledger(self) -> None:
        if not LEDGER_PATH.exists():
            return
        cutoff = time.time() - 48 * 3600.0
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
            if lane == "TF2B_DeSeva_v1":
                self.hist_deseva.append(row)
            elif lane == "TF2BP_CTRL":
                self.hist_ctrl.append(row)

    def _build_features(self, snap: Any, now: float) -> Dict[str, float]:
        best_bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        best_ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        bid_depth_1 = float(getattr(snap, "bid_depth_1", 0.0) or 0.0)
        ask_depth_1 = float(getattr(snap, "ask_depth_1", 0.0) or 0.0)
        total_bid_depth = float(getattr(snap, "total_bid_depth", 0.0) or 0.0)
        total_ask_depth = float(getattr(snap, "total_ask_depth", 0.0) or 0.0)
        imbalance = float(getattr(snap, "imbalance", 0.0) or 0.0)
        taker_bid = float(getattr(snap, "taker_volume_bid", 0.0) or 0.0)
        taker_ask = float(getattr(snap, "taker_volume_ask", 0.0) or 0.0)
        taker_agg = float(getattr(snap, "taker_aggressiveness", 0.0) or 0.0)
        cancel_rate = float(getattr(snap, "cancel_rate", 0.0) or 0.0)
        refill_rate = float(getattr(snap, "refill_rate", 0.0) or 0.0)
        latency = float(getattr(snap, "latency_ms", 15.0) or 15.0)
        micro_dev = float(getattr(snap, "micro_dev", 0.0) or 0.0)
        spread_bp = ((best_ask - best_bid) / best_bid * 10000.0) if best_bid > 0 else 2.0
        depth_ratio = (total_bid_depth / total_ask_depth) if total_ask_depth > 0 else 1.0

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
        toxic_score = float(adv_dict.get("toxic_score", 25.0) or 25.0)
        shock = self.market_shock.get_current_shock()

        return {
            "spread_bp": spread_bp,
            "imbalance": imbalance,
            "micro_dev": micro_dev,
            "depth_ratio": depth_ratio,
            "bid_depth_1": bid_depth_1,
            "ask_depth_1": ask_depth_1,
            "total_bid_depth": total_bid_depth,
            "total_ask_depth": total_ask_depth,
            "taker_volume_bid": taker_bid,
            "taker_volume_ask": taker_ask,
            "taker_aggressiveness": taker_agg,
            "pressure_score": imbalance * 50.0,
            "micro_accel": float(getattr(snap, "micro_accel", 0.0) or 0.0),
            "cancel_rate": cancel_rate,
            "refill_rate": refill_rate,
            "vacuum_score": vac_score,
            "latency_ms": latency,
            "toxic_score": toxic_score,
            "adverse_score": adv_score,
            "news_impact_score": float(getattr(shock, "impact_score", 0.0) or 0.0),
            "news_risk_level": 1.0 if getattr(shock, "shock_active", False) else 0.0,
            "news_alpha_flag": 0.0,
        }

    def _tf2bp_seva_gate(self, snap: Any, now: float) -> Dict[str, Any]:
        feats = self._build_features(snap, now)
        verdict = self.seva.evaluate_and_rank(feats)
        tf = next((s for s in verdict.ranked_strategies if s.strategy_name == "TF2BP"), None)
        out = {
            "allowed": bool(tf.entry_allowed) if tf else False,
            "ev_bp": float(tf.ev_bp) if tf else 0.0,
            "confidence": float(tf.confidence) if tf else 0.0,
            "reason": (tf.rejection_reason if tf and not tf.entry_allowed else "SEVA_ALLOW"),
            "rank": int(tf.rank) if tf else -1,
            "inference_ms": round(self.seva.last_inference_time_ms, 3),
            "top_strategy": verdict.top_strategy,
        }
        self._last_seva = out
        return out

    def _apply_action(
        self,
        name: str,
        strat: TF2BPStrategy,
        res: Dict[str, Any],
        snap: Any,
        now: float,
        *,
        seva_gate: Optional[Dict[str, Any]] = None,
    ) -> None:
        action = res.get("action", "hold")
        if action in ("buy", "sell") and not strat.position_side:
            if name == "TF2BP_CTRL":
                self.stats["signals_ctrl"] += 1
            else:
                self.stats["signals_deseva"] += 1
                gate = seva_gate or self._tf2bp_seva_gate(snap, now)
                if not gate.get("allowed"):
                    self.stats["seva_rejected"] += 1
                    _append(
                        AUDIT_PATH,
                        {
                            "event": "SEVA_REJECT",
                            "ts": _iso(),
                            "data": {
                                "side": action,
                                "tf2bp_reason": res.get("reason"),
                                "seva": gate,
                            },
                        },
                    )
                    return
                self.stats["seva_allowed"] += 1
                res = dict(res)
                res["reason"] = (
                    f"{res.get('reason')} [DeSeva EV={gate.get('ev_bp'):+.2f} "
                    f"conf={float(gate.get('confidence') or 0)*100:.0f}%]"
                )

            fill = float(res.get("entry_price") or snap.mid_price)
            strat.record_trade(action, fill, 0.0, mid_price=float(snap.mid_price or fill))
            if name == "TF2B_DeSeva_v1":
                self.stats["entries_deseva"] += 1
                # stash seva at entry on last trade meta via audit
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
                            "seva": seva_gate or self._last_seva,
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
            if name == "TF2B_DeSeva_v1":
                self.hist_deseva.append(row)
                self.stats["exits_deseva"] += 1
            else:
                self.hist_ctrl.append(row)
                self.stats["exits_ctrl"] += 1
            _append(AUDIT_PATH, {"event": "EXIT", "ts": _iso(), "data": row})
            _log(f"[{name}] EXIT {reason} pnl={row['pnl_bp']:+.2f}bp")

    def _tick_pair(self, snap: Any, now: float) -> None:
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
        # Precompute SEVA once when DeSeva may enter
        need_seva = (not self.deseva.position_side)
        seva_gate = self._tf2bp_seva_gate(snap, now) if need_seva else self._last_seva

        res_c = self.ctrl.on_tick(**common)
        self._apply_action("TF2BP_CTRL", self.ctrl, res_c, snap, now)
        res_d = self.deseva.on_tick(**common)
        self._apply_action(
            "TF2B_DeSeva_v1", self.deseva, res_d, snap, now, seva_gate=seva_gate
        )

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
            "total_trades": n,
            "win_trades": int(strat.win_trades),
            "total_pnl_bp": round(float(strat.total_pnl_bp), 4),
            "total_pnl": round(float(strat.total_pnl), 2),
        }

    def metrics(self) -> Dict[str, Any]:
        ctrl = self._lane_block("TF2BP_CTRL", self.ctrl, self.hist_ctrl)
        arm = self._lane_block("TF2B_DeSeva_v1", self.deseva, self.hist_deseva)
        compare = {
            "delta_pnl_bp": round(arm["pnl_bp"] - ctrl["pnl_bp"], 4),
            "delta_ev_bp": round(arm["ev_bp"] - ctrl["ev_bp"], 4),
            "delta_sharpe": round(arm["sharpe"] - ctrl["sharpe"], 4),
            "delta_max_dd_bp": round(arm["max_dd_bp"] - ctrl["max_dd_bp"], 4),
            "delta_1h_pnl_bp": round(arm["stats_1h"]["pnl_bp"] - ctrl["stats_1h"]["pnl_bp"], 2),
            "delta_24h_pnl_bp": round(arm["stats_24h"]["pnl_bp"] - ctrl["stats_24h"]["pnl_bp"], 2),
            "delta_1h_sharpe": round(arm["stats_1h"]["sharpe"] - ctrl["stats_1h"]["sharpe"], 4),
            "delta_24h_sharpe": round(arm["stats_24h"]["sharpe"] - ctrl["stats_24h"]["sharpe"], 4),
            "delta_1h_max_dd_bp": round(
                arm["stats_1h"]["max_dd_bp"] - ctrl["stats_1h"]["max_dd_bp"], 4
            ),
            "delta_24h_max_dd_bp": round(
                arm["stats_24h"]["max_dd_bp"] - ctrl["stats_24h"]["max_dd_bp"], 4
            ),
            "hypothesis": "SEVA-filtered TF2BP improves Sharpe and MaxDD vs baseline",
            "pass_sharpe_24h": bool(arm["stats_24h"]["sharpe"] > ctrl["stats_24h"]["sharpe"]),
            "pass_dd_24h": bool(arm["stats_24h"]["max_dd_bp"] > ctrl["stats_24h"]["max_dd_bp"]),  # less negative
        }
        allowed = int(self.stats["seva_allowed"])
        rejected = int(self.stats["seva_rejected"])
        denom = allowed + rejected
        return {
            "ts_jst": _jst(),
            "model": "TF2B_DeSeva_v1",
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "market_source": self.market_source,
            "wire": "NO",
            "stack": "TF2BP × SEVA(entry_allowed)",
            "TF2BP_CTRL": ctrl,
            "TF2B_DeSeva_v1": arm,
            "tf2bp_ctrl": ctrl,
            "tf2b_deseva_v1": arm,
            "compare": compare,
            "delta_pnl_bp": compare["delta_pnl_bp"],
            "delta_ev_bp": compare["delta_ev_bp"],
            "delta_sharpe": compare["delta_sharpe"],
            "delta_max_dd_bp": compare["delta_max_dd_bp"],
            "gate": {
                **self.stats,
                "seva_allow_rate": round(allowed / denom, 4) if denom else 0.0,
            },
            "latest_seva": self._last_seva,
        }

    def save_state(self) -> None:
        m = self.metrics()
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, STATE_PATH)
        OUT.mkdir(parents=True, exist_ok=True)
        COMPARE_PATH.write_text(
            json.dumps(m.get("compare") or {}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (OUT / "LANE_LOCK.json").write_text(
            json.dumps(
                {
                    "lane": "TF2B_DeSeva_v1",
                    "model": "TF2B_DeSeva_v1",
                    "status": "RUNNING" if self.running else "STOPPED",
                    "wire": "NO",
                    "started_jst": datetime.fromtimestamp(self.t0, JST).strftime(
                        "%Y-%m-%d %H:%M:%S JST"
                    ),
                    "pid": os.getpid(),
                    "metrics": m,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def report(self, *, kind: str = "periodic") -> None:
        m = self.metrics()
        try:
            self.notifier.post_tf2b_deseva_dryrun(m, kind=kind)
        except Exception as exc:
            _log(f"discord warn: {exc}")
        _append(AUDIT_PATH, {"event": "REPORT", "ts": _iso(), "data": m})
        c = m.get("compare") or {}
        _log(
            f"[REPORT/{kind}] DeSeva pnl={m['TF2B_DeSeva_v1']['pnl_bp']:+.2f}bp "
            f"CTRL={m['TF2BP_CTRL']['pnl_bp']:+.2f}bp Δpnl={c.get('delta_pnl_bp'):+.2f} "
            f"ΔSharpe={c.get('delta_sharpe'):+.3f} ΔDD={c.get('delta_max_dd_bp'):+.2f} "
            f"allow={m['gate'].get('seva_allow_rate')}"
        )

    def run(self) -> int:
        OUT.mkdir(parents=True, exist_ok=True)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        _log(
            f"START TF2B_DeSeva_v1 dryrun models={self.stats['seva_models']} "
            f"source={self.market_source} report={self.report_interval_sec}s WIRE=NO"
        )
        self.feed.start()
        self.report(kind="start")
        last_snap = None
        try:
            while self.running:
                if STOP_FLAG.exists():
                    _log("stop_flag")
                    break
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
                if not (
                    last_snap.best_bid
                    and last_snap.best_ask
                    and last_snap.best_bid < last_snap.best_ask
                ):
                    time.sleep(0.2)
                    continue
                self._tick_pair(last_snap, now)
                if now - self.last_state_save >= 10.0:
                    self.save_state()
                    self.last_state_save = now
                if now - self.last_report >= self.report_interval_sec:
                    self.report(kind="periodic")
                    self.last_report = now
                time.sleep(0.05)
        finally:
            try:
                self.feed.stop()
            except Exception:
                pass
            self.running = False
            self.save_state()
            self.report(kind="final")
            if PID_PATH.exists():
                try:
                    PID_PATH.unlink()
                except Exception:
                    pass
            lock = OUT / "LANE_LOCK.json"
            if lock.exists():
                try:
                    d = json.loads(lock.read_text(encoding="utf-8"))
                    d["status"] = "STOPPED"
                    d["stopped_jst"] = _jst()
                    lock.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                except Exception:
                    pass
            _log("STOPPED TF2B_DeSeva_v1 dryrun")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="TF2B_DeSeva_v1 Dry-Run (TF2BP × SEVA)")
    ap.add_argument("--symbol", default="FX_BTC_JPY")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--report-interval", type=float, default=900.0)
    ap.add_argument("--market-source", default=os.environ.get("MARKET_SOURCE", "shared_feeder"))
    args = ap.parse_args()
    me = os.getpid()
    needle = "antigravity.quant_pipeline.run_dryrun_tf2b_deseva_v1"
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if needle in cmd and "python" in cmd:
            print(f"REFUSE: already running pid={pid}", file=sys.stderr)
            return 2
    os.environ["ENABLE_REAL_TRADING"] = "false"
    return Tf2bDesevaV1DryRun(
        symbol=args.symbol,
        interval_sec=args.interval,
        report_interval_sec=args.report_interval,
        market_source=args.market_source,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
