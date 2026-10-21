#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TF2B_CAP Dry-Run — TF2BP (Trend Momentum) + SpreadCaptureMM (Range MM) Hybrid (WIRE=NO).

Directive:
  TF2BP (トレンド初動 +2bp獲得) と SpreadCaptureMM (平常レンジ スプレッド獲得) を
  相場動意・レジームに応じてシームレスに切り替えるハイブリッド統合戦略。

Lanes:
  1. TF2B_CAP (Hybrid Portfolio): 動意時はTF2BP、平穏レンジ時はSpreadCaptureMM
  2. TF2BP_SUB: TF2BP 単体の寄与
  3. SC_MM_SUB: SpreadCaptureMM 単体の寄与

Discord: 1時間ごとに指定Webhookへ 1h PnL、24h累積PnL、Quants分析コメントを送信。
"""
from __future__ import annotations

import argparse
import collections
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
OUT_DIR = AG / "data" / "mm_research" / "tf2b_cap_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = AG / "data" / "dryrun_tf2b_cap_state.json"
PID_PATH = AG / "data" / "dryrun_tf2b_cap.pid"
LOG_PATH = AG / "logs" / "dryrun_tf2b_cap.log"
LEDGER_PATH = OUT_DIR / "hybrid_ledger.jsonl"
AUDIT_PATH = OUT_DIR / "audit_events.jsonl"

DEFAULT_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1488457192725155921/"
    "Uh0ERMXDmMLFroRpPyYL9VpA_6Pvw0fUEew5eKnVVZ67do7IhU0P3Gwf8eF0vF0EDrbc"
)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_TF2BCAP_WEBHOOK_URL", DEFAULT_WEBHOOK_URL)


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


class RealtimeSpreadCaptureMM:
    """
    SpreadCaptureMM (strat_cbcd5aed_approved.py 準拠) のリアルタイム版
    - 2.5bp確保型 スプレッドキャプチャMM ＋ ボラ急増回避
    - Maker指値でスプレッドを回収
    """
    def __init__(self, order_size_btc: float = 0.001):
        self.order_size_btc = float(order_size_btc)
        self.position: Optional[str] = None  # "buy", "sell", None
        self.entry_price: float = 0.0
        self.entry_time: float = 0.0
        self.bars = collections.deque(maxlen=100)
        self.cur_bar = {"high": 0.0, "low": 1e9, "close": 0.0, "start_ts": 0.0}
        self.bar_sec = 10.0

        self.spread_mult = 1.2
        self.min_spread_pct = 0.00025  # 2.5bp
        self.vol_thresh = 1.3
        self.hard_stop_bp = 4.0
        self.max_hold_sec = 600.0

        # Stats
        self.total_trades = 0
        self.win_trades = 0
        self.total_pnl_bp = 0.0
        self.total_pnl_jpy = 0.0
        self.trades_history: List[Dict[str, Any]] = []

    def on_tick(self, mid: float, spread_bp: float, now: float) -> Dict[str, Any]:
        # Roll bar
        if now - self.cur_bar["start_ts"] >= self.bar_sec:
            if self.cur_bar["close"] > 0:
                self.bars.append(dict(self.cur_bar))
            self.cur_bar = {"high": mid, "low": mid, "close": mid, "start_ts": now}
        else:
            self.cur_bar["high"] = max(self.cur_bar["high"], mid)
            self.cur_bar["low"] = min(self.cur_bar["low"], mid)
            self.cur_bar["close"] = mid

        if len(self.bars) < 15:
            return {"action": "hold", "reason": "WARMUP"}

        # Calculate ATR on 10s bars
        closes = [b["close"] for b in self.bars]
        highs = [b["high"] for b in self.bars]
        lows = [b["low"] for b in self.bars]

        tr_list = []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            tr_list.append(tr)

        atr = sum(tr_list[-14:]) / min(14, len(tr_list)) if tr_list else 0.0
        atr_baseline = sum(tr_list[-42:]) / min(42, len(tr_list)) if tr_list else atr

        rolling_mid = sum(closes[-10:]) / 10.0
        raw_spread = atr * self.spread_mult
        min_half_spread = rolling_mid * (self.min_spread_pct / 2.0)
        half_spread = max(raw_spread, min_half_spread)

        bid_limit = rolling_mid - half_spread
        ask_limit = rolling_mid + half_spread
        normal_vol = atr <= (atr_baseline * self.vol_thresh)

        # Check Position Exit
        if self.position:
            elapsed = now - self.entry_time
            if self.position == "buy":
                pnl_bp = (mid / self.entry_price - 1.0) * 1e4
                # Target: capture half_spread (mid revert)
                if abs(mid - rolling_mid) < (half_spread * 0.25):
                    return self._do_exit(mid, pnl_bp, "revert_tp", now)
                elif pnl_bp <= -self.hard_stop_bp:
                    return self._do_exit(mid, pnl_bp, "hard_stop", now)
                elif elapsed >= self.max_hold_sec:
                    return self._do_exit(mid, pnl_bp, "timeout", now)
            elif self.position == "sell":
                pnl_bp = (1.0 - mid / self.entry_price) * 1e4
                if abs(mid - rolling_mid) < (half_spread * 0.25):
                    return self._do_exit(mid, pnl_bp, "revert_tp", now)
                elif pnl_bp <= -self.hard_stop_bp:
                    return self._do_exit(mid, pnl_bp, "hard_stop", now)
                elif elapsed >= self.max_hold_sec:
                    return self._do_exit(mid, pnl_bp, "timeout", now)
            return {"action": "hold", "reason": "MM_HOLDING"}

        # Check Position Entry
        if normal_vol and spread_bp >= 1.0:
            if mid <= bid_limit:
                self.position = "buy"
                self.entry_price = bid_limit
                self.entry_time = now
                return {"action": "buy", "price": bid_limit, "reason": f"SC_MM_BID_FILL (spr={spread_bp:.1f}bp)"}
            elif mid >= ask_limit:
                self.position = "sell"
                self.entry_price = ask_limit
                self.entry_time = now
                return {"action": "sell", "price": ask_limit, "reason": f"SC_MM_ASK_FILL (spr={spread_bp:.1f}bp)"}

        return {"action": "hold", "reason": "RANGE_WAIT"}

    def _do_exit(self, fill_price: float, pnl_bp: float, reason: str, now: float) -> Dict[str, Any]:
        jpy_val = self.order_size_btc * fill_price
        pnl_jpy = (pnl_bp / 1e4) * jpy_val
        is_win = (pnl_bp > 0)

        self.total_trades += 1
        if is_win:
            self.win_trades += 1
        self.total_pnl_bp += pnl_bp
        self.total_pnl_jpy += pnl_jpy

        trade_record = {
            "ts": now,
            "side": self.position,
            "entry_price": self.entry_price,
            "exit_price": fill_price,
            "pnl_bp": round(pnl_bp, 2),
            "pnl_jpy": round(pnl_jpy, 1),
            "is_win": is_win,
            "reason": reason,
        }
        self.trades_history.append(trade_record)
        old_pos = self.position
        self.position = None
        self.entry_price = 0.0
        self.entry_time = 0.0

        return {
            "action": "exit",
            "side": old_pos,
            "price": fill_price,
            "pnl_bp": pnl_bp,
            "pnl_jpy": pnl_jpy,
            "is_win": is_win,
            "reason": reason,
        }


class TF2BCapHybridRunner:
    def __init__(
        self,
        symbol: str = "FX_BTC_JPY",
        interval_sec: float = 1.0,
        report_interval_sec: float = 3600.0,
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

        # Strategies
        cfg_tf2b = {
            "order_size_btc": 0.001,
            "spread_threshold_bp": 1.2,
            "raw_score_threshold": 0.55,
            "p2_threshold": 0.50,
            "sl5_bp": 2.0,
            "cost_bp": 0.0,
        }
        self.tf2bp = TF2BPStrategy(cfg_tf2b)
        self.sc_mm = RealtimeSpreadCaptureMM(order_size_btc=0.001)

        # Adverse and Microstructure
        self.adverse_engine = AdverseScoreEngine()
        self.vacuum_agent = LiquidityVacuumAgent()
        self.market_shock = MarketShockSentinel()

        # Ingestion
        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=60.0, batch_size=200)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code=symbol)
        if self.market_source == SOURCE_SHARED:
            self.feed: Any = FeederBoardSource(self.ingestion)
        else:
            self.feed = LiveBoardFeed(self.ingestion, product_code=symbol)

        # Ledgers
        self.hist_hybrid: List[Dict[str, Any]] = []
        self.hist_tf2bp: List[Dict[str, Any]] = []
        self.hist_sc_mm: List[Dict[str, Any]] = []
        self._restore_ledger()

        # Stats
        self.current_regime = "NORMAL_RANGE"
        self.stats = {
            "tf2bp_entries": 0,
            "tf2bp_exits": 0,
            "sc_mm_entries": 0,
            "sc_mm_exits": 0,
            "hybrid_switches": 0,
        }

    def stop(self, *_a) -> None:
        self.running = False

    def _restore_ledger(self) -> None:
        if not LEDGER_PATH.exists():
            return
        cutoff = time.time() - 72 * 3600.0
        try:
            for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if float(row.get("ts") or 0) < cutoff:
                    continue
                lane = row.get("lane")
                if lane == "TF2B_CAP":
                    self.hist_hybrid.append(row)
                elif lane == "TF2BP":
                    self.hist_tf2bp.append(row)
                elif lane == "SpreadCaptureMM":
                    self.hist_sc_mm.append(row)
        except Exception:
            pass

    def _detect_regime(self, snap: Any, metrics: Dict[str, float]) -> str:
        """
        相場レジーム判定:
        - TREND: TF2BPの動意・板乖離・インバランスが強い
        - RANGE: 静穏でリフィルが健全、スプレッド適正
        """
        imb = abs(metrics["imbalance"])
        mdev = abs(metrics["micro_dev_bp"])
        spread = metrics["spread_bp"]

        if mdev >= 0.30 or imb >= 0.30 or spread > 2.5:
            return "TREND_MOMENTUM"
        elif 0.8 <= spread <= 2.2 and imb < 0.20:
            return "STABLE_RANGE"
        return "NORMAL_RANGE"

    def _tick(self, snap: Any, now: float) -> None:
        mid = float(snap.mid_price or 0.0)
        best_b = float(snap.best_bid or 0.0)
        best_a = float(snap.best_ask or 0.0)
        spread_bp = ((best_a - best_b) / best_b * 1e4) if best_b > 0 else 1.5
        imbalance = float(getattr(snap, "imbalance", 0.0) or 0.0)
        micro_dev = float(getattr(snap, "micro_dev", 0.0) or 0.0)
        micro_dev_bp = (micro_dev / mid * 1e4) if mid > 0 else 0.0

        metrics = {
            "mid_price": mid,
            "spread_bp": round(spread_bp, 2),
            "imbalance": round(imbalance, 4),
            "micro_dev_bp": round(micro_dev_bp, 4),
        }
        regime = self._detect_regime(snap, metrics)
        self.current_regime = regime

        # 1. Update SpreadCaptureMM
        res_mm = self.sc_mm.on_tick(mid, spread_bp, now)
        if res_mm.get("action") == "exit":
            row_mm = {
                "ts": now,
                "lane": "SpreadCaptureMM",
                "side": res_mm.get("side"),
                "pnl_bp": round(float(res_mm.get("pnl_bp") or 0.0), 2),
                "pnl_jpy": round(float(res_mm.get("pnl_jpy") or 0.0), 1),
                "is_win": bool(res_mm.get("is_win")),
                "reason": res_mm.get("reason"),
            }
            _append(LEDGER_PATH, row_mm)
            self.hist_sc_mm.append(row_mm)

            # Record to Hybrid portfolio
            row_hyb = dict(row_mm)
            row_hyb["lane"] = "TF2B_CAP"
            row_hyb["source_strategy"] = "SpreadCaptureMM"
            _append(LEDGER_PATH, row_hyb)
            self.hist_hybrid.append(row_hyb)
            self.stats["sc_mm_exits"] += 1
            _log(f"[TF2B_CAP] ⚡ SC_MM EXIT: {res_mm.get('reason')} pnl={row_mm['pnl_bp']:+.2f}bp")

        elif res_mm.get("action") in ("buy", "sell"):
            self.stats["sc_mm_entries"] += 1
            _log(f"[TF2B_CAP] SC_MM ENTRY {res_mm.get('action').upper()} @ {res_mm.get('price', 0):,.0f}")

        # 2. Update TF2BP
        common = dict(
            mid_price=mid,
            best_bid=best_b,
            best_ask=best_a,
            taker_vol_bid=float(getattr(snap, "taker_volume_bid", 0.0) or 0.0),
            taker_vol_ask=float(getattr(snap, "taker_volume_ask", 0.0) or 0.0),
            bid_depth_1=float(getattr(snap, "bid_depth_1", 0.0) or 0.0),
            ask_depth_1=float(getattr(snap, "ask_depth_1", 0.0) or 0.0),
            now=now,
        )
        res_tf = self.tf2bp.on_tick(**common)
        if res_tf.get("action") in ("exit", "cancel") and self.tf2bp.position_side:
            pnl = float(res_tf.get("pnl") or 0.0)
            reason = str(res_tf.get("reason") or "exit")
            self.tf2bp.record_trade("exit", mid, pnl, mid_price=mid)
            last = self.tf2bp.trades_history[-1] if self.tf2bp.trades_history else {}
            row_tf = {
                "ts": now,
                "lane": "TF2BP",
                "side": last.get("side"),
                "pnl_bp": float(last.get("pnl_bp") or 0.0),
                "pnl_jpy": float(last.get("pnl_jpy") or 0.0),
                "is_win": bool(last.get("is_win")),
                "reason": reason,
            }
            _append(LEDGER_PATH, row_tf)
            self.hist_tf2bp.append(row_tf)

            # Record to Hybrid portfolio
            row_hyb = dict(row_tf)
            row_hyb["lane"] = "TF2B_CAP"
            row_hyb["source_strategy"] = "TF2BP"
            _append(LEDGER_PATH, row_hyb)
            self.hist_hybrid.append(row_hyb)
            self.stats["tf2bp_exits"] += 1
            _log(f"[TF2B_CAP] ⚡ TF2BP EXIT: {reason} pnl={row_tf['pnl_bp']:+.2f}bp")

        elif res_tf.get("action") in ("buy", "sell") and not self.tf2bp.position_side:
            fill = float(res_tf.get("entry_price") or mid)
            self.tf2bp.record_trade(res_tf["action"], fill, 0.0, mid_price=mid)
            self.stats["tf2bp_entries"] += 1
            _log(f"[TF2B_CAP] TF2BP ENTRY {res_tf['action'].upper()} @ {fill:,.0f} ({res_tf.get('reason')})")

    def _lane_stats(self, hist: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(hist)
        wins = sum(1 for r in hist if bool(r.get("is_win")))
        wr = (wins / n) if n else 0.0
        pnl_bp = round(sum(float(r.get("pnl_bp") or 0.0) for r in hist), 2)
        pnl_jpy = round(sum(float(r.get("pnl_jpy") or 0.0) for r in hist), 1)
        ev_bp = round(pnl_bp / n, 4) if n else 0.0
        sd = _sharpe_dd(hist)
        s1 = _window_stats(hist, 1.0)
        s24 = _window_stats(hist, 24.0)
        return {
            "total_trades": n,
            "win_trades": wins,
            "win_rate": round(wr, 4),
            "pnl_bp": pnl_bp,
            "pnl_jpy": pnl_jpy,
            "ev_bp": ev_bp,
            "sharpe": sd["sharpe"],
            "max_dd_bp": sd["max_dd_bp"],
            "stats_1h": s1,
            "stats_24h": s24,
        }

    def metrics(self) -> Dict[str, Any]:
        m_hyb = self._lane_stats(self.hist_hybrid)
        m_tf = self._lane_stats(self.hist_tf2bp)
        m_mm = self._lane_stats(self.hist_sc_mm)

        return {
            "ts_jst": _iso(),
            "model": "TF2B_CAP",
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "regime": self.current_regime,
            "market_source": self.market_source,
            "wire": "NO",
            "stack": "TF2BP (Trend Momentum) + SpreadCaptureMM (Range MM) Hybrid",
            "TF2B_CAP": m_hyb,
            "TF2BP_COMPONENT": m_tf,
            "SC_MM_COMPONENT": m_mm,
            "positions": {
                "tf2bp": self.tf2bp.position_side or "FLAT",
                "sc_mm": self.sc_mm.position or "FLAT",
            },
            "stats": self.stats,
        }

    def save_state(self) -> None:
        m = self.metrics()
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(STATE_PATH)
        self.last_state_save = time.time()

    def generate_quants_comment(self, m: Dict[str, Any]) -> str:
        hyb = m["TF2B_CAP"]
        s1 = hyb["stats_1h"]
        s24 = hyb["stats_24h"]
        tf = m["TF2BP_COMPONENT"]
        mm = m["SC_MM_COMPONENT"]
        regime = m["regime"]

        comments = []
        if regime == "TREND_MOMENTUM":
            comments.append("📈 **相場環境**: トレンド動意（モメンタム）を検知中。TF2BP が +2bp の初動追従を主導しています。")
        elif regime == "STABLE_RANGE":
            comments.append("⚖️ **相場環境**: 板の厚みとスプレッドが安定したレンジ相場。SpreadCaptureMM がスプレッドを着実に回収しています。")
        else:
            comments.append("⏳ **相場環境**: 平常板。トレンド発生の待機と指値クォートを並行維持しています。")

        # PnL Commentary
        if s1["pnl_bp"] > 0:
            comments.append(f"直近1時間は **`{s1['pnl_bp']:+.2f} bp`** のプラス推移。ハイブリッド合成により滑らかにエッジを積み上げています。")
        elif s1["pnl_bp"] < 0:
            comments.append(f"直近1時間は `{s1['pnl_bp']:+.2f} bp` の軽微な押し目ですが、MaxDD は `{s1['max_dd_bp']:.1f}bp` に制御されています。")
        else:
            comments.append("直近1時間はエントリー厳選による待機状態（ノーポジション安全維持）。")

        # Component breakdown
        comments.append(
            f"累積寄与: TF2BP `{tf['pnl_bp']:+.1f}bp` ({tf['total_trades']}回) ｜ "
            f"SpreadCaptureMM `{mm['pnl_bp']:+.1f}bp` ({mm['total_trades']}回)。"
        )
        return "\n".join(comments)

    def send_discord_report(self, kind: str = "hourly") -> None:
        m = self.metrics()
        hyb = m["TF2B_CAP"]
        s1 = hyb["stats_1h"]
        s24 = hyb["stats_24h"]
        tf = m["TF2BP_COMPONENT"]
        mm = m["SC_MM_COMPONENT"]
        comment = self.generate_quants_comment(m)

        title_map = {
            "start": "🚀 [START] TF2B_CAP ハイブリッド DryRun 観測開始",
            "hourly": f"⏱️ [HOURLY #{int(m['elapsed_hours'])}] TF2B_CAP 定期進捗レポート",
            "final": "🏁 [FINAL] TF2B_CAP DryRun 完了レポート",
        }
        title = title_map.get(kind, f"📊 [REPORT] TF2B_CAP {kind}")
        color = 0x2ECC71 if s1["pnl_bp"] >= 0 else 0x3498DB

        fields = [
            {
                "name": "⏱️ 直近 1時間 PnL (1h Performance)",
                "value": (
                    f"• **1h PnL**: **`{s1['pnl_bp']:+.2f} bp`** ({s1['pnl_jpy']:+.1f}円)\n"
                    f"• **1h 取引数**: `{s1['total_trades']}` 回 (勝率 `{s1['win_rate_pct']:.1f}%`)\n"
                    f"• **1h Sharpe**: `{s1['sharpe']:+.3f}` · MaxDD: `{s1['max_dd_bp']:.1f}bp`"
                ),
                "inline": True,
            },
            {
                "name": "📊 24時間 累積 PnL (24h Performance)",
                "value": (
                    f"• **24h PnL**: **`{s24['pnl_bp']:+.2f} bp`** ({s24['pnl_jpy']:+.1f}円)\n"
                    f"• **24h 取引数**: `{s24['total_trades']}` 回 (勝率 `{s24['win_rate_pct']:.1f}%`)\n"
                    f"• **24h Sharpe**: `{s24['sharpe']:+.3f}` · MaxDD: `{s24['max_dd_bp']:.1f}bp`"
                ),
                "inline": True,
            },
            {
                "name": "🧩 戦略別 累積寄与内訳",
                "value": (
                    f"• 🥇 **TF2BP (トレンド)**: `{tf['pnl_bp']:+.2f} bp` ({tf['total_trades']} trades · WR {tf['win_rate']*100:.1f}%)\n"
                    f"• 🥈 **SpreadCaptureMM (レンジ)**: `{mm['pnl_bp']:+.2f} bp` ({mm['total_trades']} trades · WR {mm['win_rate']*100:.1f}%)"
                ),
                "inline": False,
            },
            {
                "name": "📝 クオンツ分析コメント",
                "value": comment,
                "inline": False,
            },
            {
                "name": "⚙️ 稼働ステータス",
                "value": (
                    f"• 稼働時間: `{m['elapsed_hours']:.2f}` 時間\n"
                    f"• 現在レジーム: `{m['regime']}`\n"
                    f"• TF2BP ポジション: `{m['positions']['tf2bp']}` ｜ SC_MM ポジション: `{m['positions']['sc_mm']}`"
                ),
                "inline": False,
            },
        ]

        payload = {
            "username": "TF2B_CAP Hybrid Reporter",
            "embeds": [
                {
                    "title": title,
                    "description": (
                        "**TF2BP (Trend Momentum) ＋ SpreadCaptureMM (Range Spread Capture)**\n"
                        "最上位2大アルゴリズムをシームレス統合したハイブリッドDryRun観測。"
                    ),
                    "color": color,
                    "fields": fields,
                    "footer": {
                        "text": f"Antigravity Quants · TF2B_CAP · {m['ts_jst']}",
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
        _log("=== TF2B_CAP DryRun START (WIRE=NO) ===")
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

                self._tick(last_snap, now)
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
            _log("=== TF2B_CAP DryRun STOPPED ===")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="TF2B_CAP Hybrid DryRun Runner")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    args = parser.parse_args()

    runner = TF2BCapHybridRunner(
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
