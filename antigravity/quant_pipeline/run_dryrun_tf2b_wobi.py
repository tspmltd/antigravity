#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TF2B_WOBI Dry-Run — WOBI & Microstructure Directional Profiler-guided TF2BP (WIRE=NO).

Features:
  1. 深度加重板不均衡 (WOBI: Weighted Order Book Imbalance with depth decay)
  2. マイクロプライス乖離 (Micro-Price Dislocation)
  3. キャンセル圧・見せ板フィルター (Cancel Pressure)
  4. ローリング Z-スコア (O(1) Rolling Standardization)
  5. 動的最適壁アンカー利確 ＆ 早期損切り (Dynamic Profit Harvest & Adverse Cut)
  6. 1時間ごとに 1時間PnL / 24時間累積PnL を指定 Discord Webhook へ自動通知

Paired lanes:
  - TF2BP_CTRL: frozen TF2BP baseline (unfiltered)
  - TF2B_WOBI: WOBI Directional Profiler-guided TF2BP
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
from typing import Any, Dict, List, Optional, Tuple

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
OUT_DIR = AG / "data" / "mm_research" / "tf2b_wobi_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = AG / "data" / "dryrun_tf2b_wobi_state.json"
PID_PATH = AG / "data" / "dryrun_tf2b_wobi.pid"
LOG_PATH = AG / "logs" / "dryrun_tf2b_wobi.log"
FEEDER_LATEST = AG / "data" / "market_feeder" / "latest.json"
LEDGER_PATH = OUT_DIR / "paired_ledger.jsonl"
AUDIT_PATH = OUT_DIR / "audit_events.jsonl"

DISCORD_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1562675909276991543/"
    "MqAEMhpxo0xJmQXsM6dVTjM_5wYcket-l0LGRLHAZRD8P_OV-IbW6b8kvg6q-azVl18D"
)


def _now() -> float:
    return time.time()


def _iso() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def _log(msg: str) -> None:
    print(f"[{_iso()}] {msg}", flush=True)


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


class WOBIDirectionalEngine:
    """板の多段階深度・マイクロプライス乖離・キャンセル圧から方向性ZスコアをO(1)計算する数理エンジン"""

    def __init__(
        self,
        depth_k: int = 5,
        lambda_decay: float = 0.5,
        alpha: float = 1.0,
        beta: float = 0.5,
        gamma: float = 0.3,
        window_size: int = 100,
    ) -> None:
        self.depth_k = depth_k
        self.lambda_decay = lambda_decay
        self.alpha = alpha
        self.beta = beta
        self.gamma = gamma
        self.window_size = window_size

        self.score_history: List[float] = [0.0] * window_size
        self.hist_index: int = 0
        self.hist_count: int = 0
        self.rolling_sum: float = 0.0
        self.rolling_sum_sq: float = 0.0

        self.prev_bids: Dict[float, float] = {}
        self.prev_asks: Dict[float, float] = {}

    def calculate_wobi(self, bids: List[Dict[str, float]], asks: List[Dict[str, float]]) -> float:
        if not bids or not asks:
            return 0.0

        w_bid_sum = 0.0
        k_b = min(self.depth_k, len(bids))
        for i in range(k_b):
            w = math.exp(-self.lambda_decay * i)
            w_bid_sum += w * float(bids[i].get("size", 0.0))

        w_ask_sum = 0.0
        k_a = min(self.depth_k, len(asks))
        for i in range(k_a):
            w = math.exp(-self.lambda_decay * i)
            w_ask_sum += w * float(asks[i].get("size", 0.0))

        total = w_bid_sum + w_ask_sum
        if total <= 1e-12:
            return 0.0
        return (w_bid_sum - w_ask_sum) / total

    def calculate_micro_dislocation(
        self,
        bids: List[Dict[str, float]],
        asks: List[Dict[str, float]],
        best_bid: float,
        best_ask: float,
    ) -> float:
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            return 0.0

        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2.0

        w_bid_vol = 0.0
        k_b = min(self.depth_k, len(bids))
        for i in range(k_b):
            w = math.exp(-self.lambda_decay * i)
            w_bid_vol += w * float(bids[i].get("size", 0.0))

        w_ask_vol = 0.0
        k_a = min(self.depth_k, len(asks))
        for i in range(k_a):
            w = math.exp(-self.lambda_decay * i)
            w_ask_vol += w * float(asks[i].get("size", 0.0))

        v_total = w_bid_vol + w_ask_vol
        if v_total <= 1e-12:
            return 0.0

        micro_price = best_bid + spread * (w_bid_vol / v_total)
        return (micro_price - mid) / spread  # [-0.5, +0.5]

    def calculate_cancel_pressure(
        self,
        levels: List[Dict[str, float]],
        prev_map: Dict[float, float],
    ) -> Tuple[float, Dict[float, float]]:
        curr_map = {float(lv["price"]): float(lv["size"]) for lv in levels[: self.depth_k]}
        if not prev_map:
            return 0.0, curr_map

        prev_tot = sum(prev_map.values())
        if prev_tot <= 1e-12:
            return 0.0, curr_map

        cancel_drop = 0.0
        for p, prev_v in prev_map.items():
            curr_v = curr_map.get(p, 0.0)
            if curr_v < prev_v:
                cancel_drop += prev_v - curr_v

        return cancel_drop / prev_tot, curr_map

    def update_and_evaluate(
        self,
        bids: List[Dict[str, float]],
        asks: List[Dict[str, float]],
        best_bid: float,
        best_ask: float,
        fallback_imbalance: float = 0.0,
    ) -> Tuple[float, float, float, float]:
        """Returns: (wobi, norm_disloc, raw_score, z_score)"""
        if bids and asks:
            wobi = self.calculate_wobi(bids, asks)
            norm_disloc = self.calculate_micro_dislocation(bids, asks, best_bid, best_ask)
            cp_bid, self.prev_bids = self.calculate_cancel_pressure(bids, self.prev_bids)
            cp_ask, self.prev_asks = self.calculate_cancel_pressure(asks, self.prev_asks)
        else:
            wobi = fallback_imbalance
            norm_disloc = 0.0
            cp_bid, cp_ask = 0.0, 0.0

        raw_score = (self.alpha * wobi) + (self.beta * norm_disloc) + (self.gamma * (cp_ask - cp_bid))

        # O(1) rolling statistics
        old_val = self.score_history[self.hist_index]
        self.score_history[self.hist_index] = raw_score
        self.hist_index = (self.hist_index + 1) % self.window_size

        if self.hist_count < self.window_size:
            self.hist_count += 1
            self.rolling_sum += raw_score
            self.rolling_sum_sq += raw_score * raw_score
        else:
            self.rolling_sum += raw_score - old_val
            self.rolling_sum_sq += (raw_score * raw_score) - (old_val * old_val)

        n = float(self.hist_count)
        mean = self.rolling_sum / n
        var = (self.rolling_sum_sq / n) - (mean * mean)

        z_score = 0.0
        if var > 1e-9:
            z_score = (raw_score - mean) / math.sqrt(var)

        return round(wobi, 4), round(norm_disloc, 4), round(raw_score, 4), round(z_score, 3)


def calculate_optimal_order_price(
    bids: List[Dict[str, float]],
    asks: List[Dict[str, float]],
    side: str,
    scan_depth: int = 5,
    min_wall_vol: float = 0.05,
    tick_size: float = 1.0,
    fallback_price: float = 0.0,
) -> Tuple[float, str]:
    """板の深層構造（大口壁と真空地帯）から最適な発注価格を決定する (Wall Anchoring)
    - 最良気配が薄く(真空)、下位に分厚い壁がある場合は毒食いを回避して壁の1tick手前に指す
    - 壁の中に直接アンカーして約定確率を最大化
    """
    if side == "buy":
        if not bids:
            return fallback_price, "NO_BIDS"
        scan = bids[:scan_depth]
        max_idx = 0
        max_vol = 0.0
        for i, lvl in enumerate(scan):
            v = float(lvl.get("size", 0.0) or lvl.get("volume", 0.0))
            if v > max_vol:
                max_vol = v
                max_idx = i
        top_vol = float(scan[0].get("size", 0.0) or scan[0].get("volume", 0.0))
        wall_price = float(scan[max_idx]["price"])
        if max_idx > 0 and top_vol < min_wall_vol:
            opt_price = wall_price + tick_size
            return opt_price, f"WALL_ANCHOR_L{max_idx}_AVOID_VACUUM(wall={max_vol:.3f}btc, top={top_vol:.3f}btc)"
        return float(scan[0]["price"]), f"TOP_OF_BOOK(top={top_vol:.3f}btc)"
    elif side == "sell":
        if not asks:
            return fallback_price, "NO_ASKS"
        scan = asks[:scan_depth]
        max_idx = 0
        max_vol = 0.0
        for i, lvl in enumerate(scan):
            v = float(lvl.get("size", 0.0) or lvl.get("volume", 0.0))
            if v > max_vol:
                max_vol = v
                max_idx = i
        top_vol = float(scan[0].get("size", 0.0) or scan[0].get("volume", 0.0))
        wall_price = float(scan[max_idx]["price"])
        if max_idx > 0 and top_vol < min_wall_vol:
            opt_price = wall_price - tick_size
            return opt_price, f"WALL_ANCHOR_L{max_idx}_AVOID_VACUUM(wall={max_vol:.3f}btc, top={top_vol:.3f}btc)"
        return float(scan[0]["price"]), f"TOP_OF_BOOK(top={top_vol:.3f}btc)"
    return fallback_price, "UNKNOWN_SIDE"


class TF2BWobiDryRun:
    def __init__(
        self,
        symbol: str = "FX_BTC_JPY",
        interval_sec: float = 1.0,
        report_interval_sec: float = 3600.0,
        market_source: Optional[str] = None,
    ) -> None:
        self.symbol = symbol
        self.interval_sec = interval_sec
        self.report_interval_sec = report_interval_sec
        self.market_source = resolve_market_source(market_source)
        self.running = True
        self.t0 = time.time()
        self.last_report = time.time()
        self.last_state_save = 0.0

        cfg = _load_tf2bp_cfg()
        self.ctrl = TF2BPStrategy(cfg)
        self.wobi_strat = TF2BPStrategy(cfg)

        self.adverse_engine = AdverseScoreEngine()
        self.vacuum_agent = LiquidityVacuumAgent()
        self.market_shock = MarketShockSentinel()
        self.wobi_engine = WOBIDirectionalEngine(depth_k=5, lambda_decay=0.5, alpha=1.0, beta=0.5, gamma=0.3)

        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=60.0, batch_size=200)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code=symbol)
        if self.market_source == SOURCE_SHARED:
            self.feed: Any = FeederBoardSource(self.ingestion)
        else:
            self.feed = LiveBoardFeed(self.ingestion, product_code=symbol)

        self.hist_ctrl: List[Dict[str, Any]] = []
        self.hist_wobi: List[Dict[str, Any]] = []

        self.stats = {
            "signals_ctrl": 0,
            "signals_wobi": 0,
            "wobi_allowed": 0,
            "wobi_rejected": 0,
            "profit_harvests_wobi": 0,
            "early_exits_wobi": 0,
            "normal_exits_wobi": 0,
            "entries_ctrl": 0,
            "entries_wobi": 0,
            "exits_ctrl": 0,
            "exits_wobi": 0,
        }
        self._wobi_peak_bp: float = 0.0
        self._last_wobi_verdict: Optional[Dict[str, Any]] = None
        self._restore_ledger()

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
            pnl_bp = float(row.get("pnl_bp") or 0.0)
            pnl_jpy = float(row.get("pnl_jpy") or 0.0)
            is_win = bool(row.get("is_win") or pnl_bp > 0)
            if lane == "TF2B_WOBI":
                self.hist_wobi.append(row)
                self.wobi_strat.total_trades += 1
                if is_win:
                    self.wobi_strat.win_trades += 1
                self.wobi_strat.total_pnl += pnl_jpy
                self.wobi_strat.total_pnl_bp += pnl_bp
                self.wobi_strat.trades_history.append(row)
                self.stats["entries_wobi"] += 1
                self.stats["exits_wobi"] += 1
                exit_side = str(row.get("exit_side") or "")
                if exit_side == "profit_harvest":
                    self.stats["profit_harvests_wobi"] += 1
                elif exit_side == "early_cut":
                    self.stats["early_exits_wobi"] += 1
                else:
                    self.stats["normal_exits_wobi"] += 1
            elif lane == "TF2BP_CTRL":
                self.hist_ctrl.append(row)
                self.ctrl.total_trades += 1
                if is_win:
                    self.ctrl.win_trades += 1
                self.ctrl.total_pnl += pnl_jpy
                self.ctrl.total_pnl_bp += pnl_bp
                self.ctrl.trades_history.append(row)
                self.stats["entries_ctrl"] += 1
                self.stats["exits_ctrl"] += 1

    def _read_latest_depth(self) -> Tuple[List[Dict[str, float]], List[Dict[str, float]]]:
        if not FEEDER_LATEST.exists():
            return [], []
        try:
            raw = json.loads(FEEDER_LATEST.read_text(encoding="utf-8"))
            bids = raw.get("bids", [])[:10]
            asks = raw.get("asks", [])[:10]
            return bids, asks
        except Exception:
            return [], []

    def _extract_metrics(self, snap: Any, now: float) -> Dict[str, Any]:
        best_bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        best_ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        mid = float(getattr(snap, "mid_price", 0.0) or ((best_bid + best_ask) / 2.0 if best_bid > 0 else 1.0))
        spread_bp = ((best_ask - best_bid) / best_bid * 1e4) if best_bid > 0 else 2.0
        imbalance = float(getattr(snap, "imbalance", 0.0) or 0.0)
        micro_dev = float(getattr(snap, "micro_dev", 0.0) or 0.0)
        micro_dev_bp = (micro_dev / mid * 1e4) if mid > 0 else 0.0
        cancel_rate = float(getattr(snap, "cancel_rate", 0.0) or 0.0)
        latency = float(getattr(snap, "latency_ms", 15.0) or 15.0)

        # Depth scan from feeder
        depth_bids, depth_asks = self._read_latest_depth()
        wobi, norm_disloc, raw_score, z_score = self.wobi_engine.update_and_evaluate(
            depth_bids, depth_asks, best_bid, best_ask, fallback_imbalance=imbalance
        )

        vac = self.vacuum_agent.on_tick(snap, now=now)
        vac_score = float(vac.get("vacuum_score", 0.0) or 0.0)
        is_vac = bool(vac.get("is_vacuum", False))
        vac_side = str(vac.get("vacuum_side", "none"))
        d_mdev_dt = float(vac.get("metrics", {}).get("d_micro_dev_dt", 0.0))

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
            "wobi": wobi,
            "norm_disloc": norm_disloc,
            "raw_directional_score": raw_score,
            "z_score": z_score,
            "vacuum_score": round(vac_score, 4),
            "is_vacuum": is_vac,
            "vacuum_side": vac_side,
            "d_micro_dev_dt": round(d_mdev_dt, 2),
            "adverse_score": round(adv_score, 2),
            "mid_price": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
        }

    def _eval_wobi_entry_condition(self, side: str, m: Dict[str, Any]) -> Tuple[bool, str]:
        """WOBI & Microstructure Directional Profiler による有利判定"""
        reasons = []
        # スプレッドが広すぎる場合のみ阻止（スプレッド負け回避）
        if m["spread_bp"] > 2.8:
            reasons.append(f"SPREAD_TOO_WIDE ({m['spread_bp']:.1f}bp > 2.8bp)")

        if side == "buy":
            # 買い方向:
            # 1. 深層板の売り壁が分厚い (WOBI < -0.15)
            if m["wobi"] < -0.15:
                reasons.append(f"WOBI_ASK_HEAVY ({m['wobi']:+.2f} < -0.15)")
            # 2. 複合フローZスコアが弱気・下降トレンド (Z < -0.50)
            if m["z_score"] < -0.50:
                reasons.append(f"DIRECTIONAL_Z_BEARISH ({m['z_score']:+.2f} < -0.50)")
            # 3. マイクロプライスが大幅逆行 (micro_dev_bp < -0.30bp)
            if m["micro_dev_bp"] < -0.30:
                reasons.append(f"MICRO_DEV_REVERSE ({m['micro_dev_bp']:+.2f}bp < -0.30bp)")
        elif side == "sell":
            if m["wobi"] > 0.15:
                reasons.append(f"WOBI_BID_HEAVY ({m['wobi']:+.2f} > +0.15)")
            if m["z_score"] > 0.50:
                reasons.append(f"DIRECTIONAL_Z_BULLISH ({m['z_score']:+.2f} > +0.50)")
            if m["micro_dev_bp"] > 0.30:
                reasons.append(f"MICRO_DEV_REVERSE ({m['micro_dev_bp']:+.2f}bp > +0.30bp)")

        if reasons:
            return False, "; ".join(reasons)
        return True, f"WOBI_FAVORABLE (wobi={m['wobi']:+.2f} z={m['z_score']:+.2f})"

    def _eval_wobi_position_exit_condition(
        self, side: str, entry_price: float, current_mid: float, m: Dict[str, Any]
    ) -> Tuple[bool, str, bool]:
        """4層 動的エグジット＆ホールド判定: (should_exit, reason, is_profit_harvest)"""
        if entry_price <= 0 or current_mid <= 0:
            return False, "", False

        unrealized_bp = (current_mid / entry_price - 1.0) * 1e4 if side == "buy" else (1.0 - current_mid / entry_price) * 1e4
        self._wobi_peak_bp = max(getattr(self, "_wobi_peak_bp", 0.0), unrealized_bp)
        peak_bp = self._wobi_peak_bp

        # Tier 1: 致命的板崩壊・スプレッド急拡大
        if m["spread_bp"] >= 3.5:
            return True, f"SPREAD_BLOWOUT ({m['spread_bp']:.1f}bp >= 3.5bp)", (unrealized_bp > 0)

        # Tier 2: 動的利益確保 (Profit Harvesting: +1.5bp以上)
        if side == "buy" and unrealized_bp >= 1.5:
            # 2a. +3.0bp到達時の頭打ち (WOBI失速またはピークから0.8bp押し戻し)
            if unrealized_bp >= 3.0:
                if m["wobi"] <= 0.0 or m["micro_dev_bp"] <= 0.05:
                    return True, f"PROFIT_TARGET_PLATEAU (pnl={unrealized_bp:+.1f}bp wobi={m['wobi']:+.2f})", True
                if peak_bp - unrealized_bp >= 0.8:
                    return True, f"PROFIT_GIVEBACK_PEAK (peak={peak_bp:+.1f}bp curr={unrealized_bp:+.1f}bp)", True

            # 2b. +1.5〜+3.0bp 圏でWOBI反転・売り圧力検知
            if m["wobi"] < -0.15:
                return True, f"WOBI_REVERSED_TO_ASK (pnl={unrealized_bp:+.1f}bp wobi={m['wobi']:+.2f} < -0.15)", True
            if m["z_score"] < -1.0:
                return True, f"DIRECTIONAL_Z_BEARISH_FLIP (pnl={unrealized_bp:+.1f}bp z={m['z_score']:+.2f})", True
            if m["micro_dev_bp"] < 0.0:
                return True, f"MOMENTUM_STALL_DEV_FLIP (pnl={unrealized_bp:+.1f}bp mdev={m['micro_dev_bp']:+.2f}bp < 0)", True

        elif side == "sell" and unrealized_bp >= 1.5:
            if unrealized_bp >= 3.0:
                if m["wobi"] >= 0.0 or m["micro_dev_bp"] >= -0.05:
                    return True, f"PROFIT_TARGET_PLATEAU (pnl={unrealized_bp:+.1f}bp wobi={m['wobi']:+.2f})", True
                if peak_bp - unrealized_bp >= 0.8:
                    return True, f"PROFIT_GIVEBACK_PEAK (peak={peak_bp:+.1f}bp curr={unrealized_bp:+.1f}bp)", True
            if m["wobi"] > 0.15:
                return True, f"WOBI_REVERSED_TO_BID (pnl={unrealized_bp:+.1f}bp wobi={m['wobi']:+.2f} > +0.15)", True
            if m["z_score"] > 1.0:
                return True, f"DIRECTIONAL_Z_BULLISH_FLIP (pnl={unrealized_bp:+.1f}bp z={m['z_score']:+.2f})", True
            if m["micro_dev_bp"] > 0.0:
                return True, f"MOMENTUM_STALL_DEV_FLIP (pnl={unrealized_bp:+.1f}bp mdev={m['micro_dev_bp']:+.2f}bp > 0)", True

        # Tier 3: 動的損切り (Early Adverse Cut)
        if side == "buy":
            # 3a. 強烈な売り壁・下落圧力
            if m["wobi"] <= -0.30 and m["z_score"] <= -1.5:
                return True, f"OPPOSING_WOBI_DOMINANT (wobi={m['wobi']:+.2f} z={m['z_score']:+.2f})", False
            # 3b. 含み損進行かつ下落フロー
            if unrealized_bp <= -1.0 and (m["wobi"] < -0.15 or m["micro_dev_bp"] < -0.15):
                return True, f"UNFAVORABLE_DRIFT (unrealized={unrealized_bp:+.1f}bp wobi={m['wobi']:+.2f})", False
            # 3c. CSR-602 物理予測限界（2bp）違反
            if unrealized_bp <= -2.0:
                return True, f"FRONTIER_LIMIT_BREACH (unrealized={unrealized_bp:+.1f}bp <= -2.0bp)", False
        elif side == "sell":
            if m["wobi"] >= 0.30 and m["z_score"] >= 1.5:
                return True, f"OPPOSING_WOBI_DOMINANT (wobi={m['wobi']:+.2f} z={m['z_score']:+.2f})", False
            if unrealized_bp <= -1.0 and (m["wobi"] > 0.15 or m["micro_dev_bp"] > 0.15):
                return True, f"UNFAVORABLE_DRIFT (unrealized={unrealized_bp:+.1f}bp wobi={m['wobi']:+.2f})", False
            if unrealized_bp <= -2.0:
                return True, f"FRONTIER_LIMIT_BREACH (unrealized={unrealized_bp:+.1f}bp <= -2.0bp)", False

        # Tier 4: ポジション維持 (Hold Favorable)
        return False, "HOLD_FAVORABLE", False

    def _apply_action(
        self,
        name: str,
        strat: TF2BPStrategy,
        res: Dict[str, Any],
        snap: Any,
        now: float,
        metrics: Dict[str, Any],
    ) -> None:
        action = res.get("action", "hold")

        # Entry Handling
        if action in ("buy", "sell") and not strat.position_side:
            if name == "TF2BP_CTRL":
                self.stats["signals_ctrl"] += 1
                fill = float(res.get("entry_price") or snap.mid_price)
                strat.record_trade(action, fill, 0.0, mid_price=float(snap.mid_price or fill))
                self.stats["entries_ctrl"] += 1
                _append(
                    AUDIT_PATH,
                    {
                        "event": "ENTRY",
                        "ts": _iso(),
                        "data": {"lane": name, "side": action, "price": fill, "reason": res.get("reason")},
                    },
                )
            else:
                self.stats["signals_wobi"] += 1
                allowed, reason = self._eval_wobi_entry_condition(action, metrics)
                self._last_wobi_verdict = {
                    "allowed": allowed,
                    "reason": reason,
                    "metrics": {k: v for k, v in metrics.items() if not isinstance(v, list)},
                    "side": action,
                    "ts": _iso(),
                }
                if not allowed:
                    self.stats["wobi_rejected"] += 1
                    _append(
                        AUDIT_PATH,
                        {
                            "event": "WOBI_ENTRY_REJECT",
                            "ts": _iso(),
                            "data": {
                                "side": action,
                                "tf2bp_reason": res.get("reason"),
                                "wobi_reason": reason,
                                "metrics": {k: v for k, v in metrics.items() if not isinstance(v, list)},
                            },
                        },
                    )
                    return
                self.stats["wobi_allowed"] += 1
                bids_depth, asks_depth = self._read_latest_depth()
                opt_price, quote_desc = calculate_optimal_order_price(
                    bids=bids_depth,
                    asks=asks_depth,
                    side=action,
                    scan_depth=5,
                    min_wall_vol=0.05,
                    tick_size=1.0,
                    fallback_price=float(res.get("entry_price") or snap.mid_price),
                )
                fill = opt_price
                res = dict(res)
                res["reason"] = f"{res.get('reason')} [WOBI ALLOW: {reason}] [{quote_desc}]"
                strat.record_trade(action, fill, 0.0, mid_price=float(snap.mid_price or fill))
                self._wobi_peak_bp = 0.0
                self.stats["entries_wobi"] += 1
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
                            "metrics": {k: v for k, v in metrics.items() if not isinstance(v, list)},
                        },
                    },
                )
            _log(f"[{name}] ENTRY {action.upper()} @ {fill:,.0f} ({res.get('reason')})")

        # Normal Exit from TF2BP Logic
        elif action in ("exit", "cancel") and strat.position_side:
            fill = float(snap.mid_price or 0.0)
            pnl = float(res.get("pnl") or res.get("expected_pnl") or 0.0)
            reason = str(res.get("reason") or action)
            strat.record_trade(action, fill, pnl, mid_price=float(snap.mid_price or fill))
            if name == "TF2B_WOBI":
                self._wobi_peak_bp = 0.0
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
            if name == "TF2B_WOBI":
                self.hist_wobi.append(row)
                self.stats["exits_wobi"] += 1
                self.stats["normal_exits_wobi"] += 1
            else:
                self.hist_ctrl.append(row)
                self.stats["exits_ctrl"] += 1
            _append(AUDIT_PATH, {"event": "EXIT", "ts": _iso(), "data": row})
            _log(f"[{name}] NORMAL EXIT {reason} pnl={row['pnl_bp']:+.2f}bp")

    def _check_early_wobi_exit(self, snap: Any, now: float, metrics: Dict[str, Any]) -> None:
        """TF2B_WOBI lane: check if running position should be cut early or harvested for profit."""
        if not self.wobi_strat.position_side:
            return

        side = self.wobi_strat.position_side
        entry = self.wobi_strat.entry_price
        mid = float(snap.mid_price or metrics["mid_price"])

        should_exit, reason, is_harvest = self._eval_wobi_position_exit_condition(side, entry, mid, metrics)
        if should_exit:
            fill = mid
            gross_bp = (fill / entry - 1.0) * 1e4 if side == "buy" else (1.0 - fill / entry) * 1e4
            net_bp = gross_bp - float(self.wobi_strat.params.get("cost_bp", 0.0))
            pnl_jpy = self.wobi_strat._yen(net_bp)

            self.wobi_strat.record_trade("exit", fill, pnl_jpy, mid_price=fill)
            self.wobi_strat.cooldown_until_ms = int(now * 1000) + 10_000
            self._wobi_peak_bp = 0.0

            tag = "WOBI_PROFIT_HARVEST" if is_harvest else "WOBI_ADVERSE_CUT"
            icon = "💰" if is_harvest else "⚡"
            exit_side = "profit_harvest" if is_harvest else "early_cut"

            last = self.wobi_strat.trades_history[-1] if self.wobi_strat.trades_history else {}
            row = {
                "ts": float(last.get("ts") or now),
                "lane": "TF2B_WOBI",
                "side": last.get("side"),
                "exit_side": exit_side,
                "fill_price": fill,
                "pnl_bp": float(last.get("pnl_bp") or net_bp),
                "pnl_jpy": float(last.get("pnl_jpy") or pnl_jpy),
                "is_win": (net_bp > 0),
                "exit_reason": f"{tag} ({reason})",
            }
            _append(LEDGER_PATH, row)
            self.hist_wobi.append(row)
            self.stats["exits_wobi"] += 1
            if is_harvest:
                self.stats["profit_harvests_wobi"] = self.stats.get("profit_harvests_wobi", 0) + 1
            else:
                self.stats["early_exits_wobi"] += 1
            _append(AUDIT_PATH, {"event": tag, "ts": _iso(), "data": row})
            _log(f"[TF2B_WOBI] {icon} {tag} pnl={net_bp:+.2f}bp ({reason})")

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

        # 1. Check early exit for TF2B_WOBI if holding
        self._check_early_wobi_exit(snap, now, metrics)

        # 2. Run normal TF2BP tick
        res_c = self.ctrl.on_tick(**common)
        self._apply_action("TF2BP_CTRL", self.ctrl, res_c, snap, now, metrics)

        res_w = self.wobi_strat.on_tick(**common)
        self._apply_action("TF2B_WOBI", self.wobi_strat, res_w, snap, now, metrics)

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
        w = self._lane_block("TF2B_WOBI", self.wobi_strat, self.hist_wobi)
        delta_pnl = round(w["pnl_bp"] - c["pnl_bp"], 3)
        delta_ev = round(w["ev_bp"] - c["ev_bp"], 3)
        delta_sharpe = round(w["sharpe"] - c["sharpe"], 4)
        delta_dd = round(w["max_dd_bp"] - c["max_dd_bp"], 2)

        s1_c, s1_w = c["stats_1h"], w["stats_1h"]
        s24_c, s24_w = c["stats_24h"], w["stats_24h"]

        return {
            "ts_jst": _iso(),
            "model": "TF2B_WOBI",
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "market_source": self.market_source,
            "wire": "NO",
            "stack": "TF2BP × WOBI Microstructure Directional Profiler (WOBI + MicroPrice Dislocation + Wall Anchoring)",
            "TF2BP_CTRL": c,
            "TF2B_WOBI": w,
            "compare": {
                "delta_pnl_bp": delta_pnl,
                "delta_ev_bp": delta_ev,
                "delta_sharpe": delta_sharpe,
                "delta_max_dd_bp": delta_dd,
                "delta_1h_pnl_bp": round(s1_w["pnl_bp"] - s1_c["pnl_bp"], 2),
                "delta_24h_pnl_bp": round(s24_w["pnl_bp"] - s24_c["pnl_bp"], 2),
                "delta_1h_sharpe": round(s1_w["sharpe"] - s1_c["sharpe"], 4),
                "delta_24h_sharpe": round(s24_w["sharpe"] - s24_c["sharpe"], 4),
                "delta_1h_max_dd_bp": round(s1_w["max_dd_bp"] - s1_c["max_dd_bp"], 2),
                "delta_24h_max_dd_bp": round(s24_w["max_dd_bp"] - s24_c["max_dd_bp"], 2),
                "hypothesis": "WOBI Directional Profiler filters adverse entries, harvests wall-anchored profits & cuts adverse drift",
            },
            "gate": {
                "signals_ctrl": self.stats["signals_ctrl"],
                "signals_wobi": self.stats["signals_wobi"],
                "wobi_allowed": self.stats["wobi_allowed"],
                "wobi_rejected": self.stats["wobi_rejected"],
                "profit_harvests_wobi": self.stats.get("profit_harvests_wobi", 0),
                "early_exits_wobi": self.stats["early_exits_wobi"],
                "normal_exits_wobi": self.stats["normal_exits_wobi"],
                "entries_ctrl": self.stats["entries_ctrl"],
                "entries_wobi": self.stats["entries_wobi"],
                "exits_ctrl": self.stats["exits_ctrl"],
                "exits_wobi": self.stats["exits_wobi"],
                "wobi_allow_rate": round(
                    self.stats["wobi_allowed"] / self.stats["signals_wobi"], 4
                )
                if self.stats["signals_wobi"]
                else 1.0,
            },
            "latest_wobi_verdict": self._last_wobi_verdict,
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
        w = m["TF2B_WOBI"]
        cmp = m["compare"]
        gate = m["gate"]

        title_map = {
            "start": "🚀 [START] TF2B_WOBI DryRun 観測開始",
            "hourly": f"⏱️ [HOURLY #{int(m['elapsed_hours'])}] TF2B_WOBI 定期進捗レポート (1h & 24h PnL)",
            "final": "🏁 [FINAL] TF2B_WOBI DryRun 停止レポート",
        }
        title = title_map.get(kind, f"📊 [REPORT] TF2B_WOBI {kind}")

        # Color: Green if delta_pnl >= 0 else Orange
        color = 0x2ECC71 if cmp["delta_pnl_bp"] >= 0 else 0xE67E22

        fields = [
            {
                "name": "⏱️ 直近 1時間 成績 (1h Stats)",
                "value": (
                    f"• **WOBI (1h)**: **`{w['stats_1h']['pnl_bp']:+.2f} bp`** ({w['stats_1h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{w['stats_1h']['win_rate_pct']:.1f}%` ({w['stats_1h']['win_trades']}/{w['stats_1h']['total_trades']})\n"
                    f"• **CTRL (1h)**: `{c['stats_1h']['pnl_bp']:+.2f} bp` ({c['stats_1h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{c['stats_1h']['win_rate_pct']:.1f}%` ({c['stats_1h']['win_trades']}/{c['stats_1h']['total_trades']})\n"
                    f"• **1h 差分 (Δ)**: PnL: **`{cmp['delta_1h_pnl_bp']:+.2f} bp`** | Sharpe: **`{cmp['delta_1h_sharpe']:+.3f}`**"
                ),
                "inline": False,
            },
            {
                "name": "📊 24時間 累積 成績 (24h Stats)",
                "value": (
                    f"• **WOBI (24h)**: **`{w['stats_24h']['pnl_bp']:+.2f} bp`** ({w['stats_24h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{w['stats_24h']['win_rate_pct']:.1f}%` ({w['stats_24h']['win_trades']}/{w['stats_24h']['total_trades']}) | "
                    f"MaxDD: `{w['stats_24h']['max_dd_bp']:.1f}bp`\n"
                    f"• **CTRL (24h)**: `{c['stats_24h']['pnl_bp']:+.2f} bp` ({c['stats_24h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{c['stats_24h']['win_rate_pct']:.1f}%` ({c['stats_24h']['win_trades']}/{c['stats_24h']['total_trades']}) | "
                    f"MaxDD: `{c['stats_24h']['max_dd_bp']:.1f}bp`\n"
                    f"• **24h 差分 (Δ)**: PnL: **`{cmp['delta_24h_pnl_bp']:+.2f} bp`** | MaxDD改善: **`{cmp['delta_24h_max_dd_bp']:+.2f} bp`**"
                ),
                "inline": False,
            },
            {
                "name": "⚔️ 通算 成績比較 (All-Time)",
                "value": (
                    f"• **TF2B_WOBI**: `{w['pnl_bp']:+.2f} bp` ({w['pnl_jpy']:+.1f}円) · "
                    f"WR: `{w['win_rate']*100:.1f}%` ({w['wins']}/{w['trades']}) · "
                    f"Sharpe: `{w['sharpe']:+.3f}` · MaxDD: `{w['max_dd_bp']:.1f}bp`\n"
                    f"• **TF2BP_CTRL**: `{c['pnl_bp']:+.2f} bp` ({c['pnl_jpy']:+.1f}円) · "
                    f"WR: `{c['win_rate']*100:.1f}%` ({c['wins']}/{c['trades']}) · "
                    f"Sharpe: `{c['sharpe']:+.3f}` · MaxDD: `{c['max_dd_bp']:.1f}bp`\n"
                    f"• **通算差分 (Δ)**: PnL: **`{cmp['delta_pnl_bp']:+.2f} bp`** | "
                    f"Sharpe: **`{cmp['delta_sharpe']:+.3f}`** | "
                    f"MaxDD改善: **`{cmp['delta_max_dd_bp']:+.2f} bp`**"
                ),
                "inline": False,
            },
            {
                "name": "🛡️ WOBI ゲート統計",
                "value": (
                    f"• TF2BP シグナル発生: `{gate['signals_wobi']}` 件\n"
                    f"• **有利エントリー許可**: `{gate['wobi_allowed']}` 件 (通過率 `{gate['wobi_allow_rate']*100:.1f}%`)\n"
                    f"• **不利エントリー拒否**: `{gate['wobi_rejected']}` 件 (壁ブロック)\n"
                    f"• **動的利益確保 (Profit Harvest)**: `{gate.get('profit_harvests_wobi', 0)}` 件\n"
                    f"• **不利検知早期損切り (Adverse Cut)**: `{gate['early_exits_wobi']}` 件\n"
                    f"• 通常エグジット: `{gate['normal_exits_wobi']}` 件"
                ),
                "inline": True,
            },
            {
                "name": "⚙️ 稼働ステータス",
                "value": (
                    f"• 稼働時間: `{m['elapsed_hours']:.2f}` 時間\n"
                    f"• 執行モード: `WIRE=NO (Paper/DryRun)`\n"
                    f"• WOBI ポジション: `{w['position']}`\n"
                    f"• CTRL ポジション: `{c['position']}`"
                ),
                "inline": True,
            },
        ]

        payload = {
            "username": "TF2B_WOBI Reporter",
            "embeds": [
                {
                    "title": title,
                    "description": (
                        "**WOBI & Microstructure Directional Profiler（板の深層・壁追従計測器）** を適用した TF2BP DryRun。\n"
                        "「WOBIによる不利エントリー遮断」＋「大口壁アンカー動的利益確保」＋「早期損切り」を実測中。"
                    ),
                    "color": color,
                    "fields": fields,
                    "footer": {
                        "text": f"Antigravity Quants · TF2B_WOBI · {m['ts_jst']}",
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
        _log("=== TF2B_WOBI DryRun START (WIRE=NO) ===")
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
                if not best_b or not best_a:
                    time.sleep(0.1)
                    continue

                self._tick_pair(last_snap, now)

                # Periodic state save (every 10s)
                if now - self.last_state_save >= 10.0:
                    self.save_state()

                # Hourly report
                if now - self.last_report >= self.report_interval_sec:
                    self.send_discord_report("hourly")
                    self.last_report = now

                time.sleep(self.interval_sec)
        except KeyboardInterrupt:
            _log("Stopped by operator (SIGINT)")
        except Exception as exc:
            _log(f"Fatal loop exception: {exc}")
            import traceback
            traceback.print_exc()
            return 1
        finally:
            self.send_discord_report("final")
            self.save_state()
            try:
                self.feed.stop()
            except Exception:
                pass
            if PID_PATH.exists():
                PID_PATH.unlink(missing_ok=True)
            _log("=== TF2B_WOBI DryRun STOPPED ===")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TF2B_WOBI DryRun (WIRE=NO)")
    parser.add_argument("--interval", type=float, default=1.0, help="Tick interval in seconds")
    parser.add_argument("--report-interval", type=float, default=3600.0, help="Discord report interval in seconds")
    parser.add_argument("--source", type=str, default="shared_feeder", help="Market source (shared_feeder/live)")
    args = parser.parse_args()

    runner = TF2BWobiDryRun(
        interval_sec=args.interval,
        report_interval_sec=args.report_interval,
        market_source=args.source,
    )

    def _sig_handler(signum, _frame):
        _log(f"Received signal {signum}, exiting gracefully...")
        runner.stop()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
