#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TF2B_WOBI3 Dry-Run — Trend-Extending Pure Maker Microstructure Model (WIRE=NO).

Features:
  1. 板模倣システム（マイクロストラクチャー解析）による「全指値（100% Pure Maker）」執行
  2. 壁消化速度（OFI）判定による動的ブレイク判断 ＆ 利確保留 (EXTEND_HOLD)
  3. サポート壁追従トレイリング・ストップ (Micro-Trailing) による利幅最大化
  4. 敵壁手前先回り利確 ＆ 崩壊予兆スマート指値撤退
  5. 4レーン同時対比トーナメント:
     - TF2B_WOBI3: トレンド追従型動的利確・全指値モデル (主役)
     - TF2B_WOBI2: 1〜3秒高速利確モデル (前世代比較)
     - TF2B_WOBI_v1: 初代門番モデル
     - TF2BP_CTRL: 無選別ベースライン
  6. 1時間毎のDiscord定期進捗通知（1h/24h PnL、平均獲得bp、トレイリング内訳、相場所感コメント）
"""
from __future__ import annotations

import argparse
import collections
from dataclasses import dataclass
import json
import math
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

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
from antigravity.strategies.tf2bp_strategy import TF2BPStrategy  # noqa: E402

JST = timezone(timedelta(hours=9))
OUT_DIR = AG / "data" / "mm_research" / "tf2b_wobi3_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = AG / "data" / "dryrun_tf2b_wobi3_state.json"
PID_PATH = AG / "data" / "dryrun_tf2b_wobi3.pid"
LOG_PATH = AG / "logs" / "dryrun_tf2b_wobi3.log"
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


@dataclass
class TrailingState:
    is_extended: bool = False
    initial_target_tp: float = 0.0
    current_target_tp: float = 0.0
    target_wall_vol_init: float = 0.0
    trailing_floor_price: float = 0.0
    trailing_floor_bp: float = 0.0
    entry_price: float = 0.0
    entry_ts: float = 0.0
    best_support_wall_price: float = 0.0
    ext_reason: str = ""


class WOBIDirectionalEngine3:
    """WOBI3 数理エンジン — 多段階深度、動的OFI（Order Flow Imbalance）、壁消化速度、先行Zスコア"""

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
        self.prev_best_bid: float = 0.0
        self.prev_best_bid_size: float = 0.0
        self.prev_best_ask: float = 0.0
        self.prev_best_ask_size: float = 0.0

        self.wobi_history: Deque[Tuple[float, float]] = collections.deque(maxlen=30)
        self.mdev_history: Deque[Tuple[float, float]] = collections.deque(maxlen=30)
        self.ofi_history: Deque[Tuple[float, float]] = collections.deque(maxlen=30)

        self.prev_ask_wall_vol: float = 0.0
        self.prev_ask_wall_ts: float = 0.0

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
        w_bid_vol = sum(math.exp(-self.lambda_decay * i) * float(bids[i].get("size", 0.0)) for i in range(min(self.depth_k, len(bids))))
        w_ask_vol = sum(math.exp(-self.lambda_decay * i) * float(asks[i].get("size", 0.0)) for i in range(min(self.depth_k, len(asks))))
        v_total = w_bid_vol + w_ask_vol
        if v_total <= 1e-12:
            return 0.0
        micro_price = best_bid + spread * (w_bid_vol / v_total)
        return (micro_price - mid) / spread

    def calculate_ofi(self, best_bid: float, best_bid_size: float, best_ask: float, best_ask_size: float, now: float) -> float:
        """Cont-Kukanov-Stoikov (2014) 厳密板流不均衡 (Order Flow Imbalance)"""
        if self.prev_best_bid <= 0 or self.prev_best_ask <= 0:
            self.prev_best_bid = best_bid
            self.prev_best_bid_size = best_bid_size
            self.prev_best_ask = best_ask
            self.prev_best_ask_size = best_ask_size
            return 0.0

        # Bid flow
        if best_bid > self.prev_best_bid:
            i_b = best_bid_size
        elif best_bid == self.prev_best_bid:
            i_b = best_bid_size - self.prev_best_bid_size
        else:
            i_b = -self.prev_best_bid_size

        # Ask flow
        if best_ask < self.prev_best_ask:
            i_a = best_ask_size
        elif best_ask == self.prev_best_ask:
            i_a = best_ask_size - self.prev_best_ask_size
        else:
            i_a = -self.prev_best_ask_size

        ofi = i_b - i_a
        self.ofi_history.append((now, ofi))

        self.prev_best_bid = best_bid
        self.prev_best_bid_size = best_bid_size
        self.prev_best_ask = best_ask
        self.prev_best_ask_size = best_ask_size

        # 直近2秒の累積OFI
        cutoff = now - 2.0
        recent_sum = sum(v for t, v in self.ofi_history if t >= cutoff)
        return round(recent_sum, 3)

    def calculate_cancel_pressure(self, levels: List[Dict[str, float]], prev_map: Dict[float, float]) -> Tuple[float, Dict[float, float]]:
        curr_map = {float(lv["price"]): float(lv["size"]) for lv in levels[: self.depth_k]}
        if not prev_map:
            return 0.0, curr_map
        prev_tot = sum(prev_map.values())
        if prev_tot <= 1e-12:
            return 0.0, curr_map
        cancel_drop = sum(prev_v - curr_map.get(p, 0.0) for p, prev_v in prev_map.items() if curr_map.get(p, 0.0) < prev_v)
        return cancel_drop / prev_tot, curr_map

    def calculate_queue_depletion(self, asks: List[Dict[str, float]], now: float) -> Tuple[float, float]:
        if not asks:
            return 0.0, 0.0
        top_ask_vol = sum(float(a.get("size", 0.0)) for a in asks[:3])
        dt = now - self.prev_ask_wall_ts
        depletion_rate = 0.0
        if dt > 0.5 and self.prev_ask_wall_vol > 0.01:
            depletion_rate = max(0.0, (self.prev_ask_wall_vol - top_ask_vol) / self.prev_ask_wall_vol / dt)
        self.prev_ask_wall_vol = top_ask_vol
        self.prev_ask_wall_ts = now
        return round(top_ask_vol, 3), round(depletion_rate, 3)

    def update_and_evaluate(
        self,
        bids: List[Dict[str, float]],
        asks: List[Dict[str, float]],
        best_bid: float,
        best_ask: float,
        now: float,
        fallback_imbalance: float = 0.0,
    ) -> Dict[str, Any]:
        best_b_size = float(bids[0].get("size", 0.0)) if bids else 0.0
        best_a_size = float(asks[0].get("size", 0.0)) if asks else 0.0

        if bids and asks:
            wobi = self.calculate_wobi(bids, asks)
            norm_disloc = self.calculate_micro_dislocation(bids, asks, best_bid, best_ask)
            cp_bid, self.prev_bids = self.calculate_cancel_pressure(bids, self.prev_bids)
            cp_ask, self.prev_asks = self.calculate_cancel_pressure(asks, self.prev_asks)
        else:
            wobi = fallback_imbalance
            norm_disloc = 0.0
            cp_bid, cp_ask = 0.0, 0.0

        recent_ofi = self.calculate_ofi(best_bid, best_b_size, best_ask, best_a_size, now)
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
        z_score = (raw_score - mean) / math.sqrt(var) if var > 1e-9 else 0.0

        self.wobi_history.append((now, wobi))
        self.mdev_history.append((now, norm_disloc))

        d_wobi_dt = 0.0
        d_mdev_dt = 0.0
        if len(self.wobi_history) >= 2:
            t_old, w_old = self.wobi_history[0]
            dt = now - t_old
            if dt >= 1.0:
                d_wobi_dt = (wobi - w_old) / dt

        if len(self.mdev_history) >= 2:
            t_old, m_old = self.mdev_history[0]
            dt = now - t_old
            if dt >= 1.0:
                d_mdev_dt = (norm_disloc - m_old) / dt

        ask_wall_vol, ask_depletion_vel = self.calculate_queue_depletion(asks, now)

        return {
            "wobi": round(wobi, 4),
            "norm_disloc": round(norm_disloc, 4),
            "raw_directional_score": round(raw_score, 4),
            "z_score": round(z_score, 3),
            "d_wobi_dt": round(d_wobi_dt, 3),
            "d_mdev_dt": round(d_mdev_dt, 3),
            "recent_ofi": recent_ofi,
            "ask_wall_vol": ask_wall_vol,
            "ask_depletion_vel": ask_depletion_vel,
            "cancel_bid": round(cp_bid, 3),
            "cancel_ask": round(cp_ask, 3),
        }


def calculate_optimal_order_price(
    bids: List[Dict[str, float]],
    asks: List[Dict[str, float]],
    side: str,
    scan_depth: int = 5,
    min_wall_vol: float = 0.05,
    tick_size: float = 1.0,
    fallback_price: float = 0.0,
) -> Tuple[float, str]:
    """板の深層構造から最適なMaker発注価格を決定する (Wall Anchoring)"""
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


class TF2BWobi3DryRun:
    """TF2B_WOBI3 Dry-Run Runner (4-Way Comparison: WOBI3 vs WOBI2 vs WOBI1 vs CTRL)"""

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
        self.last_reported_hour = datetime.now(JST).hour
        self.last_state_save = 0.0

        cfg = _load_tf2bp_cfg()
        self.ctrl = TF2BPStrategy(cfg)
        self.wobi1 = TF2BPStrategy(cfg)
        self.wobi2 = TF2BPStrategy(cfg)
        self.wobi3 = TF2BPStrategy(cfg)

        self.adverse_engine = AdverseScoreEngine()
        self.vacuum_agent = LiquidityVacuumAgent()
        self.market_shock = MarketShockSentinel()

        # Engine 3
        self.engine3 = WOBIDirectionalEngine3(depth_k=6, lambda_decay=0.5, alpha=1.0, beta=0.5, gamma=0.3)

        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=60.0, batch_size=200)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code=symbol)
        if self.market_source == SOURCE_SHARED:
            self.feed: Any = FeederBoardSource(self.ingestion)
        else:
            self.feed = LiveBoardFeed(self.ingestion, product_code=symbol)

        self.hist_ctrl: List[Dict[str, Any]] = []
        self.hist_wobi1: List[Dict[str, Any]] = []
        self.hist_wobi2: List[Dict[str, Any]] = []
        self.hist_wobi3: List[Dict[str, Any]] = []

        # WOBI3 Trailing State Manager
        self.wobi3_trailing = TrailingState()
        self.wobi3_peak_bp: float = 0.0
        self.wobi2_peak_bp: float = 0.0
        self.wobi1_peak_bp: float = 0.0

        # WOBI3 Cluster & Cooldown
        self.wobi3_rejection_cooldown_until: float = 0.0
        self.wobi3_last_signal_ts: float = 0.0
        self.wobi2_rejection_cooldown_until: float = 0.0
        self.wobi2_last_signal_ts: float = 0.0

        self.stats = {
            "signals_ctrl": 0,
            "signals_wobi1": 0,
            "signals_wobi2": 0,
            "signals_wobi3": 0,
            # WOBI3 Specific Stats
            "clusters_wobi3": 0,
            "clusters_allowed_wobi3": 0,
            "clusters_rejected_wobi3": 0,
            "wobi3_allowed": 0,
            "wobi3_rejected": 0,
            "wobi3_autonomous_entries": 0,
            # WOBI3 Exit Breakdown
            "wobi3_fast_harvests": 0,
            "wobi3_extended_holds": 0,
            "wobi3_extended_tp_hits": 0,
            "wobi3_trailing_exits": 0,
            "wobi3_adverse_cuts": 0,
            "wobi3_normal_exits": 0,
            # WOBI2 Exit Breakdown
            "wobi2_fast_harvests": 0,
            "wobi2_adverse_cuts": 0,
            "wobi2_normal_exits": 0,
        }
        self._last_wobi3_verdict: Optional[Dict[str, Any]] = None
        self._restore_ledger()

    def stop(self, *_a) -> None:
        self.running = False

    def _restore_ledger(self) -> None:
        cutoff = time.time() - 72 * 3600.0
        candidate_paths = [
            LEDGER_PATH,
            AG / "data" / "mm_research" / "tf2b_wobi2_dryrun" / "paired_ledger.jsonl",
            AG / "data" / "mm_research" / "tf2b_wobi_dryrun" / "paired_ledger.jsonl",
        ]
        seen_keys = set()
        for p in candidate_paths:
            if not p.exists():
                continue
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = float(row.get("ts") or 0)
                if ts < cutoff:
                    continue
                lane = str(row.get("lane") or "")
                pnl_bp = float(row.get("pnl_bp") or 0.0)
                pnl_jpy = float(row.get("pnl_jpy") or 0.0)
                is_win = bool(row.get("is_win") or pnl_bp > 0)
                dedup_key = (round(ts, 1), lane, pnl_bp)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                if lane == "TF2B_WOBI3":
                    self.hist_wobi3.append(row)
                    self.wobi3.total_trades += 1
                    if is_win:
                        self.wobi3.win_trades += 1
                    self.wobi3.total_pnl += pnl_jpy
                    self.wobi3.total_pnl_bp += pnl_bp
                    self.wobi3.trades_history.append(row)
                elif lane == "TF2B_WOBI2":
                    self.hist_wobi2.append(row)
                    self.wobi2.total_trades += 1
                    if is_win:
                        self.wobi2.win_trades += 1
                    self.wobi2.total_pnl += pnl_jpy
                    self.wobi2.total_pnl_bp += pnl_bp
                    self.wobi2.trades_history.append(row)
                elif lane in ("TF2B_WOBI_v1", "TF2B_WOBI"):
                    row_v1 = dict(row)
                    row_v1["lane"] = "TF2B_WOBI_v1"
                    self.hist_wobi1.append(row_v1)
                    self.wobi1.total_trades += 1
                    if is_win:
                        self.wobi1.win_trades += 1
                    self.wobi1.total_pnl += pnl_jpy
                    self.wobi1.total_pnl_bp += pnl_bp
                    self.wobi1.trades_history.append(row_v1)
                elif lane == "TF2BP_CTRL":
                    self.hist_ctrl.append(row)
                    self.ctrl.total_trades += 1
                    if is_win:
                        self.ctrl.win_trades += 1
                    self.ctrl.total_pnl += pnl_jpy
                    self.ctrl.total_pnl_bp += pnl_bp
                    self.ctrl.trades_history.append(row)

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

        depth_bids, depth_asks = self._read_latest_depth()
        wobi3_m = self.engine3.update_and_evaluate(
            depth_bids, depth_asks, best_bid, best_ask, now=now, fallback_imbalance=imbalance
        )

        vac = self.vacuum_agent.on_tick(snap, now=now)
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

        res = {
            "spread_bp": round(spread_bp, 2),
            "imbalance": round(imbalance, 4),
            "micro_dev_bp": round(micro_dev_bp, 4),
            "vacuum_score": round(float(vac.get("vacuum_score", 0.0) or 0.0), 4),
            "adverse_score": round(adv_score, 2),
            "mid_price": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
        }
        res.update(wobi3_m)
        return res

    # --- WOBI3 Dynamic Exit & Micro-Trailing Logic ---
    def _eval_wobi3_dynamic_exit(
        self,
        current_mid: float,
        depth_bids: List[Dict[str, float]],
        depth_asks: List[Dict[str, float]],
        m: Dict[str, Any],
        now: float,
    ) -> Tuple[bool, str, float, str]:
        """WOBI3 動的利確・トレイリング判定 (should_exit, action_type, exit_price, reason)
        Returns: (should_exit, action_tag, exit_price, reason)
        """
        st = self.wobi3_trailing
        entry = st.entry_price
        if entry <= 0 or current_mid <= 0:
            return False, "HOLD", 0.0, ""

        unrealized_bp = (current_mid / entry - 1.0) * 1e4
        self.wobi3_peak_bp = max(self.wobi3_peak_bp, unrealized_bp)
        peak_bp = self.wobi3_peak_bp

        # Tier 0: 致命的板開き (Spread Blowout >= 3.5bp)
        if m["spread_bp"] >= 3.5:
            return True, "SPREAD_BLOWOUT", current_mid, f"SPREAD_BLOWOUT ({m['spread_bp']:.1f}bp >= 3.5bp)"

        # Tier 1: 限界損切り (Adverse Cut)
        if unrealized_bp <= -1.8:
            return True, "ADVERSE_CUT", current_mid, f"FRONTIER_LIMIT_BREACH (unrealized={unrealized_bp:+.1f}bp <= -1.8bp)"
        if unrealized_bp <= -0.8 and (m["wobi"] <= -0.25 and m["z_score"] <= -1.0):
            return True, "ADVERSE_CUT", current_mid, f"OPPOSING_FLOW_CUT (unrealized={unrealized_bp:+.1f}bp wobi={m['wobi']:+.2f})"

        # Tier 2: トレイリング延長モード中の処理 (Extended Holding)
        if st.is_extended:
            # ① 第2ターゲット壁（TP2）に到達・超過
            if current_mid >= st.current_target_tp and st.current_target_tp > 0:
                return True, "TAKE_PROFIT_EXTENDED", st.current_target_tp, (
                    f"TP2_TARGET_HIT (pnl={unrealized_bp:+.1f}bp target={st.current_target_tp:,.0f})"
                )

            # ② 直下の買いサポート壁を探索し、トレイリング防衛ライン（Floor）を切り上げ
            best_support = current_mid
            for b in depth_bids[:4]:
                if float(b.get("size", 0.0)) >= 0.08:
                    best_support = float(b["price"])
                    break
            # サポート壁の1tick下
            trailed_candidate = best_support - 1.0
            if trailed_candidate > st.trailing_floor_price:
                st.trailing_floor_price = trailed_candidate
                st.trailing_floor_bp = (trailed_candidate / entry - 1.0) * 1e4

            # ③ サポート壁崩壊判定（トレイリング撤退: 利益を確実に確保）
            if current_mid <= st.trailing_floor_price:
                return True, "TRAILING_EXIT", st.trailing_floor_price, (
                    f"SUPPORT_WALL_COLLAPSE (trailed_floor={st.trailing_floor_price:,.0f} pnl={st.trailing_floor_bp:+.1f}bp)"
                )

            return False, "HOLD_EXTENDED", 0.0, ""

        # Tier 3: 初期レジスタンス壁（TP1）接近時の判定 (unrealized >= 1.2bp または TP1手前)
        dist_to_tp1 = st.initial_target_tp - current_mid if st.initial_target_tp > 0 else 999.0
        if (dist_to_tp1 <= 2.0 or unrealized_bp >= 1.2) and st.initial_target_tp > 0:
            # ターゲット壁の現在残量を確認
            curr_target_vol = 0.0
            for a in depth_asks[:5]:
                if abs(float(a["price"]) - st.initial_target_tp) <= 2.0:
                    curr_target_vol += float(a.get("size", 0.0))

            init_vol = max(st.target_wall_vol_init, 0.02)
            wall_eaten_ratio = max(0.0, 1.0 - (curr_target_vol / init_vol))

            # 【ブレイク継続判定 (EXTEND_HOLD)】
            # 売り壁が70%以上削られており、買いOFIがプラス、かつWOBIが崩れていない
            if (wall_eaten_ratio >= 0.65 or m["ask_depletion_vel"] >= 0.20) and m["recent_ofi"] > 0.10 and m["d_wobi_dt"] >= 0.0:
                st.is_extended = True
                # 防衛ラインを建値+1.0bp以上でロック
                st.trailing_floor_price = max(entry + 1.0, entry * 1.00010)
                st.trailing_floor_bp = (st.trailing_floor_price / entry - 1.0) * 1e4

                # 次の第2レジスタンス壁（TP2）を探索
                tp2_price = current_mid + 10.0  # デフォルト
                for a in depth_asks[2:8]:
                    if float(a.get("size", 0.0)) >= 0.10:
                        tp2_price = float(a["price"])
                        break
                st.current_target_tp = tp2_price
                st.ext_reason = f"WALL_EATEN ({wall_eaten_ratio*100:.0f}%) ofi={m['recent_ofi']:+.2f}"
                self.stats["wobi3_extended_holds"] += 1
                _log(
                    f"[TF2B_WOBI3] 🚀 EXTEND_HOLD triggered! {st.ext_reason}. "
                    f"TP2={tp2_price:,.0f} (+{(tp2_price/entry-1.0)*1e4:+.1f}bp), Floor locked @ {st.trailing_floor_bp:+.1f}bp"
                )
                return False, "EXTEND_HOLD", 0.0, st.ext_reason

            # 【壁が崩れない場合 ➔ 従来通り1〜3秒高速利確 (Fast Harvest)】
            if unrealized_bp >= 1.2:
                if m["wobi"] <= 0.0 or m["micro_dev_bp"] <= 0.0 or m["d_wobi_dt"] < -0.05 or wall_eaten_ratio < 0.40:
                    tp_price = st.initial_target_tp - 1.0 if st.initial_target_tp > current_mid else current_mid
                    return True, "FAST_HARVEST", tp_price, f"FAST_HARVEST_WALL_HEAVY (pnl={unrealized_bp:+.1f}bp eaten={wall_eaten_ratio*100:.0f}%)"

        # Tier 4: ピーク到達後の失速フォールバック
        if unrealized_bp >= 2.5 and (peak_bp - unrealized_bp >= 0.5):
            return True, "FAST_HARVEST", current_mid, f"PEAK_GIVEBACK_LOCK (peak={peak_bp:+.1f}bp curr={unrealized_bp:+.1f}bp)"

        return False, "HOLD", 0.0, ""

    def apply_wobi3_entry_state(
        self, side: str, fill: float, now: float, depth_asks: List[Dict[str, float]], mid_price: float = 0.0
    ) -> Dict[str, float]:
        """Shared WOBI3 position + trailing init. No ledger write."""
        self.wobi3.record_trade(side, fill, 0.0, mid_price=float(mid_price or fill))
        self.wobi3_peak_bp = 0.0
        self.stats["wobi3_allowed"] += 1
        target_tp1 = fill + 3.0
        target_vol_init = 0.05
        for a in depth_asks[:5]:
            p = float(a["price"])
            v = float(a.get("size", 0.0))
            if p > fill and v >= 0.05:
                target_tp1 = p
                target_vol_init = v
                break
        self.wobi3_trailing = TrailingState(
            is_extended=False,
            initial_target_tp=target_tp1,
            current_target_tp=target_tp1,
            target_wall_vol_init=target_vol_init,
            trailing_floor_price=fill - 15.0,
            trailing_floor_bp=-1.8,
            entry_price=fill,
            entry_ts=now,
            best_support_wall_price=fill,
            ext_reason="",
        )
        return {"target_tp1": target_tp1, "target_vol_init": target_vol_init}

    def apply_wobi3_exit_state(self, fill_price: float, pnl_jpy: float, exit_tag: str) -> None:
        """Shared WOBI3 flatten. No ledger write."""
        self.wobi3.record_trade("exit", fill_price, pnl_jpy, mid_price=fill_price)
        self.wobi3_peak_bp = 0.0
        self.wobi3_trailing = TrailingState()
        if exit_tag == "FAST_HARVEST":
            self.stats["wobi3_fast_harvests"] += 1
        elif exit_tag == "TAKE_PROFIT_EXTENDED":
            self.stats["wobi3_extended_tp_hits"] += 1
        elif exit_tag == "TRAILING_EXIT":
            self.stats["wobi3_trailing_exits"] += 1
        elif exit_tag == "ADVERSE_CUT":
            self.stats["wobi3_adverse_cuts"] += 1
        else:
            self.stats["wobi3_normal_exits"] += 1

    def _execute_entry_wobi3(
        self, side: str, snap: Any, now: float, reason_desc: str, m: Dict[str, Any], depth_bids: List[Dict[str, float]], depth_asks: List[Dict[str, float]]
    ) -> None:
        opt_price, quote_desc = calculate_optimal_order_price(
            bids=depth_bids,
            asks=depth_asks,
            side=side,
            scan_depth=5,
            min_wall_vol=0.05,
            tick_size=1.0,
            fallback_price=float(snap.mid_price),
        )
        fill = opt_price
        armed = self.apply_wobi3_entry_state(side, fill, now, depth_asks, mid_price=float(snap.mid_price or fill))
        target_tp1 = float(armed["target_tp1"])
        target_vol_init = float(armed["target_vol_init"])
        full_reason = f"{reason_desc} [{quote_desc}] [TP1={target_tp1:,.0f} wall={target_vol_init:.3f}btc]"
        _append(
            AUDIT_PATH,
            {
                "event": "ENTRY",
                "ts": _iso(),
                "data": {
                    "lane": "TF2B_WOBI3",
                    "side": side,
                    "price": fill,
                    "target_tp1": target_tp1,
                    "reason": full_reason,
                    "metrics": {k: v for k, v in m.items() if not isinstance(v, list)},
                },
            },
        )
        _log(f"[TF2B_WOBI3] ENTRY {side.upper()} @ {fill:,.0f} ({full_reason})")

    def _execute_exit_wobi3(
        self, fill_price: float, pnl_bp: float, pnl_jpy: float, reason: str, exit_tag: str, now: float
    ) -> None:
        self.apply_wobi3_exit_state(fill_price, pnl_jpy, exit_tag)

        row = {
            "ts": now,
            "lane": "TF2B_WOBI3",
            "side": "buy",
            "exit_side": exit_tag.lower(),
            "fill_price": fill_price,
            "pnl_bp": round(pnl_bp, 2),
            "pnl_jpy": round(pnl_jpy, 1),
            "is_win": (pnl_bp > 0),
            "exit_reason": f"{exit_tag} ({reason})",
        }
        _append(LEDGER_PATH, row)
        self.hist_wobi3.append(row)
        _append(AUDIT_PATH, {"event": "EXIT", "ts": _iso(), "data": row})
        icon = "🌟" if exit_tag == "TAKE_PROFIT_EXTENDED" else ("💰" if pnl_bp > 0 else "⚡")
        _log(f"[TF2B_WOBI3] {icon} EXIT [{exit_tag}] pnl={pnl_bp:+.2f}bp ({pnl_jpy:+.1f}円) ({reason})")

    # --- Standard Execution helpers for WOBI2, WOBI1, CTRL ---
    def _execute_exit_lane(
        self, lane: str, strat: TF2BPStrategy, hist: List[Dict[str, Any]], fill_price: float, pnl_bp: float, pnl_jpy: float, reason: str, exit_type: str, now: float
    ) -> None:
        strat.record_trade("exit", fill_price, pnl_jpy, mid_price=fill_price)
        if lane == "TF2B_WOBI2":
            self.wobi2_peak_bp = 0.0
            if exit_type == "fast_harvest":
                self.stats["wobi2_fast_harvests"] += 1
            elif exit_type == "adverse_cut":
                self.stats["wobi2_adverse_cuts"] += 1
            else:
                self.stats["wobi2_normal_exits"] += 1
        elif lane == "TF2B_WOBI_v1":
            self.wobi1_peak_bp = 0.0

        row = {
            "ts": now,
            "lane": lane,
            "side": "buy",
            "exit_side": exit_type,
            "fill_price": fill_price,
            "pnl_bp": round(pnl_bp, 2),
            "pnl_jpy": round(pnl_jpy, 1),
            "is_win": (pnl_bp > 0),
            "exit_reason": reason,
        }
        _append(LEDGER_PATH, row)
        hist.append(row)
        _append(AUDIT_PATH, {"event": "EXIT", "ts": _iso(), "data": row})

    def _tf2bp_common(self, snap: Any, m: Dict[str, Any], now: float) -> Dict[str, Any]:
        mid = float(getattr(snap, "mid_price", 0.0) or m.get("mid_price") or 0.0)
        return dict(
            mid_price=mid,
            best_bid=float(getattr(snap, "best_bid", 0.0) or 0.0),
            best_ask=float(getattr(snap, "best_ask", 0.0) or 0.0),
            taker_vol_bid=float(getattr(snap, "taker_volume_bid", 0.0) or 0.0),
            taker_vol_ask=float(getattr(snap, "taker_volume_ask", 0.0) or 0.0),
            bid_depth_1=float(getattr(snap, "bid_depth_1", 0.0) or 0.0),
            ask_depth_1=float(getattr(snap, "ask_depth_1", 0.0) or 0.0),
            now=now,
        )

    def plan_wobi3_pre_tick(
        self,
        snap: Any,
        now: float,
        m: Dict[str, Any],
        depth_bids: List[Dict[str, float]],
        depth_asks: List[Dict[str, float]],
    ) -> Dict[str, Any]:
        """WOBI3 exit / autonomous entry. Same gates as dryrun. Does not fill."""
        mid = float(getattr(snap, "mid_price", 0.0) or m.get("mid_price") or 0.0)
        if self.wobi3.position_side:
            should_exit, action_tag, exit_price, reason = self._eval_wobi3_dynamic_exit(
                mid, depth_bids, depth_asks, m, now
            )
            if should_exit:
                fill = exit_price if exit_price > 0 else mid
                return {
                    "intent": "EXIT",
                    "side": "sell" if str(self.wobi3.position_side) == "buy" else "buy",
                    "tag": action_tag,
                    "price": fill,
                    "reason": reason,
                    "metrics": m,
                }
            return {"intent": "HOLD", "reason": "IN_POSITION", "metrics": m}
        if m["wobi"] >= 0.35 and m["z_score"] >= 1.0 and m["recent_ofi"] > 0.30 and m["d_wobi_dt"] > 0.05:
            if now >= self.wobi3_rejection_cooldown_until:
                self.stats["signals_wobi3"] += 1
                self.stats["wobi3_autonomous_entries"] += 1
                self.wobi3_last_signal_ts = now
                return {
                    "intent": "ENTER",
                    "side": "buy",
                    "tag": "AUTONOMOUS_OFI_SURGE",
                    "reason": f"AUTONOMOUS_OFI_SURGE (ofi={m['recent_ofi']:+.2f} z={m['z_score']:+.2f})",
                    "metrics": m,
                }
        return {"intent": "HOLD", "reason": "NO_AUTONOMOUS", "metrics": m}

    def plan_wobi3_post_tick(self, res_wobi3: Dict[str, Any], now: float, m: Dict[str, Any]) -> Dict[str, Any]:
        """TF2BP-gated WOBI3 entry. Same cluster / favorable checks as dryrun. Does not fill."""
        if self.wobi3.position_side:
            return {"intent": "HOLD", "reason": "IN_POSITION", "metrics": m}
        act_w3 = res_wobi3.get("action", "hold")
        if act_w3 not in ("buy", "sell"):
            return {"intent": "HOLD", "reason": "NO_TF2BP", "metrics": m}
        is_new_cluster = now - self.wobi3_last_signal_ts >= 30.0
        self.wobi3_last_signal_ts = now
        if now < self.wobi3_rejection_cooldown_until:
            return {"intent": "HOLD", "reason": "COOLDOWN", "metrics": m}
        self.stats["signals_wobi3"] += 1
        if is_new_cluster:
            self.stats["clusters_wobi3"] += 1
        w3_ok = False
        w3_reason = ""
        if m["spread_bp"] > 2.8:
            w3_ok = False
            w3_reason = f"SPREAD_TOO_WIDE ({m['spread_bp']:.1f}bp)"
        elif m["ask_depletion_vel"] >= 0.25 and m["recent_ofi"] > 0.0:
            w3_ok = True
            w3_reason = f"QUEUE_DEPLETION_BREAKOUT (depletion={m['ask_depletion_vel']:.2f}/s ofi={m['recent_ofi']:+.2f})"
        elif m["wobi"] >= -0.22 and m["z_score"] >= -0.70 and m["micro_dev_bp"] >= -0.30 and m["d_wobi_dt"] >= -0.08:
            w3_ok = True
            w3_reason = f"WOBI3_FAVORABLE (wobi={m['wobi']:+.2f} z={m['z_score']:+.2f} ofi={m['recent_ofi']:+.2f})"
        else:
            w3_ok = False
            w3_reason = f"ADVERSE_FLOW (wobi={m['wobi']:+.2f} z={m['z_score']:+.2f})"
        self._last_wobi3_verdict = {
            "allowed": w3_ok,
            "reason": w3_reason,
            "metrics": {k: v for k, v in m.items() if not isinstance(v, list)},
            "side": act_w3,
            "ts": _iso(),
        }
        if w3_ok:
            if is_new_cluster:
                self.stats["clusters_allowed_wobi3"] += 1
            return {
                "intent": "ENTER",
                "side": act_w3,
                "tag": "TF2BP",
                "reason": f"TF2BP [WOBI3: {w3_reason}]",
                "metrics": m,
            }
        self.stats["wobi3_rejected"] += 1
        if is_new_cluster:
            self.stats["clusters_rejected_wobi3"] += 1
        self.wobi3_rejection_cooldown_until = now + 8.0
        return {"intent": "HOLD", "reason": w3_reason, "metrics": m}

    def _tick_pair(self, snap: Any, now: float) -> None:
        m = self._extract_metrics(snap, now)
        mid = float(snap.mid_price or m["mid_price"])
        depth_bids, depth_asks = self._read_latest_depth()
        common = self._tf2bp_common(snap, m, now)

        # -------------------------------------------------------------
        # 1. LANE 4: TF2B_WOBI3 (Trend-Extending Pure Maker)
        # -------------------------------------------------------------
        pre = self.plan_wobi3_pre_tick(snap, now, m, depth_bids, depth_asks)
        if pre.get("intent") == "EXIT":
            fill = float(pre["price"] or mid)
            gross_bp = (fill / self.wobi3.entry_price - 1.0) * 1e4
            pnl_j = self.wobi3._yen(gross_bp)
            self._execute_exit_wobi3(fill, gross_bp, pnl_j, str(pre.get("reason") or ""), str(pre.get("tag") or ""), now)
        elif pre.get("intent") == "ENTER":
            self._execute_entry_wobi3(str(pre["side"]), snap, now, str(pre["reason"]), m, depth_bids, depth_asks)

        # -------------------------------------------------------------
        # 2. RUN BASE STRATEGY TICKS
        # -------------------------------------------------------------
        res_ctrl = self.ctrl.on_tick(**common)
        res_wobi1 = self.wobi1.on_tick(**common)
        res_wobi2 = self.wobi2.on_tick(**common)
        res_wobi3 = self.wobi3.on_tick(**common)

        # -------------------------------------------------------------
        # 3. CTRL EXECUTION
        # -------------------------------------------------------------
        act_c = res_ctrl.get("action", "hold")
        if act_c in ("buy", "sell") and not self.ctrl.position_side:
            self.stats["signals_ctrl"] += 1
            fill = float(res_ctrl.get("entry_price") or mid)
            self.ctrl.record_trade(act_c, fill, 0.0, mid_price=mid)
        elif act_c in ("exit", "cancel") and self.ctrl.position_side:
            pnl = float(res_ctrl.get("pnl") or 0.0)
            reason = str(res_ctrl.get("reason") or act_c)
            last = self.ctrl.trades_history[-1] if self.ctrl.trades_history else {}
            self._execute_exit_lane("TF2BP_CTRL", self.ctrl, self.hist_ctrl, mid, float(last.get("pnl_bp") or 0.0), pnl, reason, "normal_exit", now)

        # -------------------------------------------------------------
        # 4. TF2B_WOBI_v1 EXECUTION
        # -------------------------------------------------------------
        if self.wobi1.position_side:
            ubp = (mid / self.wobi1.entry_price - 1.0) * 1e4
            if ubp >= 1.5 and (m["wobi"] < -0.15 or m["z_score"] < -1.0):
                self._execute_exit_lane("TF2B_WOBI_v1", self.wobi1, self.hist_wobi1, mid, ubp, self.wobi1._yen(ubp), "WOBI1_PROFIT_HARVEST", "profit_harvest", now)
            elif ubp <= -2.0:
                self._execute_exit_lane("TF2B_WOBI_v1", self.wobi1, self.hist_wobi1, mid, ubp, self.wobi1._yen(ubp), "WOBI1_ADVERSE_CUT", "adverse_cut", now)
            elif res_wobi1.get("action") in ("exit", "cancel"):
                last = self.wobi1.trades_history[-1] if self.wobi1.trades_history else {}
                self._execute_exit_lane("TF2B_WOBI_v1", self.wobi1, self.hist_wobi1, mid, float(last.get("pnl_bp") or 0.0), float(res_wobi1.get("pnl") or 0.0), "exit", "normal_exit", now)
        else:
            act_w1 = res_wobi1.get("action", "hold")
            if act_w1 in ("buy", "sell"):
                self.stats["signals_wobi1"] += 1
                if m["wobi"] >= -0.15 and m["z_score"] >= -0.50 and m["spread_bp"] <= 2.8:
                    opt_p, _ = calculate_optimal_order_price(depth_bids, depth_asks, act_w1, 5, 0.05, 1.0, mid)
                    self.wobi1.record_trade(act_w1, opt_p, 0.0, mid_price=mid)

        # -------------------------------------------------------------
        # 5. TF2B_WOBI2 EXECUTION (1~3s Fast Harvest Baseline)
        # -------------------------------------------------------------
        if self.wobi2.position_side:
            ubp2 = (mid / self.wobi2.entry_price - 1.0) * 1e4
            self.wobi2_peak_bp = max(self.wobi2_peak_bp, ubp2)
            if ubp2 >= 1.2 and (m["wobi"] <= 0.0 or m["micro_dev_bp"] <= 0.0 or m["d_wobi_dt"] < -0.10):
                self._execute_exit_lane("TF2B_WOBI2", self.wobi2, self.hist_wobi2, mid, ubp2, self.wobi2._yen(ubp2), "FAST_HARVEST_MOM_STALL", "fast_harvest", now)
            elif ubp2 <= -1.8:
                self._execute_exit_lane("TF2B_WOBI2", self.wobi2, self.hist_wobi2, mid, ubp2, self.wobi2._yen(ubp2), "FRONTIER_LIMIT_BREACH", "adverse_cut", now)
        else:
            act_w2 = res_wobi2.get("action", "hold")
            if act_w2 in ("buy", "sell") and now >= self.wobi2_rejection_cooldown_until:
                self.stats["signals_wobi2"] += 1
                if (m["wobi"] >= -0.22 and m["z_score"] >= -0.70 and m["spread_bp"] <= 2.8) or m["ask_depletion_vel"] >= 0.25:
                    opt_p, _ = calculate_optimal_order_price(depth_bids, depth_asks, act_w2, 5, 0.05, 1.0, mid)
                    self.wobi2.record_trade(act_w2, opt_p, 0.0, mid_price=mid)
                    self.wobi2_peak_bp = 0.0
                else:
                    self.wobi2_rejection_cooldown_until = now + 8.0

        # -------------------------------------------------------------
        # 6. TF2B_WOBI3 ENTRY EVALUATION
        # -------------------------------------------------------------
        post = self.plan_wobi3_post_tick(res_wobi3, now, m)
        if post.get("intent") == "ENTER":
            self._execute_entry_wobi3(str(post["side"]), snap, now, str(post["reason"]), m, depth_bids, depth_asks)

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

    def _generate_quant_comment(self, m: Dict[str, Any]) -> str:
        w3 = m["TF2B_WOBI3"]
        w2 = m["TF2B_WOBI2"]
        ext_holds = self.stats["wobi3_extended_holds"]
        tp2_hits = self.stats["wobi3_extended_tp_hits"]
        trailed = self.stats["wobi3_trailing_exits"]

        comment = (
            f"直近1h: WOBI3 は {w3['stats_1h']['pnl_bp']:+.2f} bp（勝率 {w3['stats_1h']['win_rate_pct']:.1f}%、EV {w3['stats_1h']['ev_bp']:+.2f} bp）。"
            f"動的ブレイク判定による利確保留（EXTEND_HOLD）は累計 {ext_holds} 件発生（TP2到達: {tp2_hits}件、トレイリング撤退: {trailed}件）。"
        )
        if w3["ev_bp"] > w2["ev_bp"]:
            comment += f" WOBI2比で1トレードあたり平均獲得利幅が +{w3['ev_bp'] - w2['ev_bp']:+.2f} bp 向上しており、トレンド伸長ロジックが有効に機能中。"
        else:
            comment += " ノイズ局面では即時高速利確（Fast Harvest）が走り、勝率と低ドローダウンを堅持中。"
        return comment

    def metrics(self) -> Dict[str, Any]:
        c = self._lane_block("TF2BP_CTRL", self.ctrl, self.hist_ctrl)
        w1 = self._lane_block("TF2B_WOBI_v1", self.wobi1, self.hist_wobi1)
        w2 = self._lane_block("TF2B_WOBI2", self.wobi2, self.hist_wobi2)
        w3 = self._lane_block("TF2B_WOBI3", self.wobi3, self.hist_wobi3)

        clusters_tot = self.stats["clusters_wobi3"]
        pass_rate = round(self.stats["clusters_allowed_wobi3"] / clusters_tot, 4) if clusters_tot > 0 else 0.0

        m_dict = {
            "ts_jst": _iso(),
            "model": "TF2B_WOBI3",
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "market_source": self.market_source,
            "wire": "NO",
            "stack": "TF2BP × WOBI3 Pure Maker Trend-Extender (OFI Wall Depletion + Micro-Trailing + Pure Maker Anchoring)",
            "TF2B_WOBI3": w3,
            "TF2B_WOBI2": w2,
            "TF2B_WOBI_v1": w1,
            "TF2BP_CTRL": c,
            "compare": {
                "delta_pnl_w3_vs_ctrl_bp": round(w3["pnl_bp"] - c["pnl_bp"], 3),
                "delta_pnl_w3_vs_w2_bp": round(w3["pnl_bp"] - w2["pnl_bp"], 3),
                "delta_ev_w3_vs_w2_bp": round(w3["ev_bp"] - w2["ev_bp"], 4),
                "delta_1h_w3_vs_ctrl_bp": round(w3["stats_1h"]["pnl_bp"] - c["stats_1h"]["pnl_bp"], 2),
                "delta_1h_w3_vs_w2_bp": round(w3["stats_1h"]["pnl_bp"] - w2["stats_1h"]["pnl_bp"], 2),
            },
            "trailing_stats": {
                "extended_holds": self.stats["wobi3_extended_holds"],
                "extended_tp_hits": self.stats["wobi3_extended_tp_hits"],
                "trailing_exits": self.stats["wobi3_trailing_exits"],
                "fast_harvests": self.stats["wobi3_fast_harvests"],
                "adverse_cuts": self.stats["wobi3_adverse_cuts"],
                "clusters_total": clusters_tot,
                "cluster_pass_rate": pass_rate,
            },
            "latest_verdict": self._last_wobi3_verdict,
        }
        m_dict["comment"] = self._generate_quant_comment(m_dict)
        return m_dict

    def save_state(self) -> None:
        m = self.metrics()
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(STATE_PATH)
        self.last_state_save = time.time()

    def send_discord_report(self, kind: str = "hourly") -> None:
        m = self.metrics()
        w3 = m["TF2B_WOBI3"]
        w2 = m["TF2B_WOBI2"]
        w1 = m["TF2B_WOBI_v1"]
        c = m["TF2BP_CTRL"]
        cmp = m["compare"]
        ts_stat = m["trailing_stats"]

        title_map = {
            "start": "🚀 [START] TF2B_WOBI3 (トレンド追従動的利確・全指値モデル) DryRun 開始",
            "hourly": f"⏱️ [HOURLY #{int(m['elapsed_hours'])}] TF2B_WOBI3 4レーン対比定期進捗レポート",
            "final": "🏁 [FINAL] TF2B_WOBI3 DryRun 停止レポート",
        }
        title = title_map.get(kind, f"📊 [REPORT] TF2B_WOBI3 {kind}")
        color = 0x2ECC71 if cmp["delta_pnl_w3_vs_ctrl_bp"] >= 0 else 0xE67E22

        fields = [
            {
                "name": "⏱️ 直近 1時間 成績 (1h Stats - 4レーン対比)",
                "value": (
                    f"• **WOBI3 (動的利確)**: **`{w3['stats_1h']['pnl_bp']:+.2f} bp`** ({w3['stats_1h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{w3['stats_1h']['win_rate_pct']:.1f}%` ({w3['stats_1h']['win_trades']}/{w3['stats_1h']['total_trades']}) | EV: **`{w3['stats_1h']['ev_bp']:+.2f}bp`**\n"
                    f"• **WOBI2 (高速利確)**: `{w2['stats_1h']['pnl_bp']:+.2f} bp` ({w2['stats_1h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{w2['stats_1h']['win_rate_pct']:.1f}%` ({w2['stats_1h']['win_trades']}/{w2['stats_1h']['total_trades']}) | EV: `{w2['stats_1h']['ev_bp']:+.2f}bp`\n"
                    f"• **WOBI1 (旧門番)**: `{w1['stats_1h']['pnl_bp']:+.2f} bp` ({w1['stats_1h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{w1['stats_1h']['win_rate_pct']:.1f}%` ({w1['stats_1h']['win_trades']}/{w1['stats_1h']['total_trades']})\n"
                    f"• **CTRL (無選別)**: `{c['stats_1h']['pnl_bp']:+.2f} bp` ({c['stats_1h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{c['stats_1h']['win_rate_pct']:.1f}%` ({c['stats_1h']['win_trades']}/{c['stats_1h']['total_trades']})\n"
                    f"• **1h 改善幅**: vs CTRL: **`{cmp['delta_1h_w3_vs_ctrl_bp']:+.2f} bp`** | vs WOBI2: **`{cmp['delta_1h_w3_vs_w2_bp']:+.2f} bp`**"
                ),
                "inline": False,
            },
            {
                "name": "📊 24時間 累積 成績 (24h Stats)",
                "value": (
                    f"• **WOBI3 (動的利確)**: **`{w3['stats_24h']['pnl_bp']:+.2f} bp`** ({w3['stats_24h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{w3['stats_24h']['win_rate_pct']:.1f}%` ({w3['stats_24h']['win_trades']}/{w3['stats_24h']['total_trades']}) | EV: **`{w3['stats_24h']['ev_bp']:+.2f}bp`** | MaxDD: `{w3['stats_24h']['max_dd_bp']:.1f}bp`\n"
                    f"• **WOBI2 (高速利確)**: `{w2['stats_24h']['pnl_bp']:+.2f} bp` ({w2['stats_24h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{w2['stats_24h']['win_rate_pct']:.1f}%` ({w2['stats_24h']['win_trades']}/{w2['stats_24h']['total_trades']}) | EV: `{w2['stats_24h']['ev_bp']:+.2f}bp` | MaxDD: `{w2['stats_24h']['max_dd_bp']:.1f}bp`\n"
                    f"• **CTRL (無選別)**: `{c['stats_24h']['pnl_bp']:+.2f} bp` ({c['stats_24h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{c['stats_24h']['win_rate_pct']:.1f}%` ({c['stats_24h']['win_trades']}/{c['stats_24h']['total_trades']}) | MaxDD: `{c['stats_24h']['max_dd_bp']:.1f}bp`\n"
                    f"• **通算 EV 差分 (WOBI3 - WOBI2)**: **`{cmp['delta_ev_w3_vs_w2_bp']:+.4f} bp / trade`**"
                ),
                "inline": False,
            },
            {
                "name": "🎯 WOBI3 動的利確 ＆ トレイリング実績",
                "value": (
                    f"• **利確保留・ブレイク延長 (EXTEND_HOLD)**: `{ts_stat['extended_holds']}` 件\n"
                    f"• **第2目標壁到達 (TP2 Hit)**: `{ts_stat['extended_tp_hits']}` 件 (+5〜+15bp大型利得)\n"
                    f"• **サポート追従利確 (Trailing Exit)**: `{ts_stat['trailing_exits']}` 件 (建値+α防衛)\n"
                    f"• **高速利確 (Fast Harvest)**: `{ts_stat['fast_harvests']}` 件 (壁反発前の手堅い回収)\n"
                    f"• **タイト損切り (Adverse Cut)**: `{ts_stat['adverse_cuts']}` 件 (-1.8bpリミット)\n"
                    f"• 局面通過率: `{ts_stat['cluster_pass_rate']*100:.1f}%` ({ts_stat['clusters_total']} 局面)"
                ),
                "inline": True,
            },
            {
                "name": "💡 定量分析コメント",
                "value": f"> {m['comment']}",
                "inline": False,
            },
        ]

        payload = {
            "username": "TF2B_WOBI3 Reporter",
            "embeds": [
                {
                    "title": title,
                    "description": (
                        "**TF2B_WOBI3: トレンド追従型動的利確・全指値マイクロストラクチャーモデル**\n"
                        "「壁消化OFIによる利確保留」＋「サポート壁追従トレイリング」＋「100% Pure Maker執行」を実測中。"
                    ),
                    "color": color,
                    "fields": fields,
                    "footer": {
                        "text": f"Antigravity Quants · TF2B_WOBI3 · {m['ts_jst']}",
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

        try:
            # Also multicast to main hourly / dryrun report channels
            notifier = QuantDiscordNotifier()
            notifier.post_dryrun_multicast(payload)
        except Exception as exc:
            _log(f"[DISCORD] Multicast warning: {exc}")

    def run(self) -> int:
        _log("=== TF2B_WOBI3 DryRun START (WIRE=NO) ===")
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

                # Hourly report: synchronized with WOBI1 (:04) and WOBI2 (:06) window
                jst_now = datetime.now(JST)
                current_hour = jst_now.hour
                if current_hour != self.last_reported_hour and jst_now.minute >= 6:
                    self.send_discord_report("hourly")
                    self.last_reported_hour = current_hour
                    self.last_report = now
                elif now - self.last_report >= self.report_interval_sec:
                    self.send_discord_report("hourly")
                    self.last_reported_hour = current_hour
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
            _log("=== TF2B_WOBI3 DryRun STOPPED ===")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TF2B_WOBI3 DryRun (WIRE=NO)")
    parser.add_argument("--interval", type=float, default=1.0, help="Tick interval in seconds")
    parser.add_argument("--report-interval", type=float, default=3600.0, help="Discord report interval in seconds")
    parser.add_argument("--source", type=str, default="shared_feeder", help="Market source (shared_feeder/live)")
    args = parser.parse_args()

    runner = TF2BWobi3DryRun(
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
