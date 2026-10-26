#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WOBIMM2 Dry-Run — COVER_WAIT only (CSR-633 · WIRE=NO).

1-variable vs frozen WOBIMM CTRL:
  When short (>=1 lot) and buy pressure persists (wobi>+0.15 or ofi>+0.02),
  pull the COVER bid for up to 10s. Offset / toxicity / wall pins unchanged.

Hourly Discord at :08 with 1h + 24h PnL, same channel as WOBIMM.
Does not replace the running WOBIMM dryrun. Does not touch LIVE.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

from antigravity.quant_pipeline.feeder_board_source import (  # noqa: E402
    FeederBoardSource,
    resolve_market_source,
    SOURCE_SHARED,
)
from antigravity.quant_pipeline.ingestion import LiveBoardFeed, MarketDataIngestion  # noqa: E402
from antigravity.quant_pipeline.event_bus import EventBus  # noqa: E402
from antigravity.quant_pipeline.parquet_logger import ParquetBatchLogger  # noqa: E402
from antigravity.quant_pipeline.run_dryrun_wobimm import (  # noqa: E402
    DISCORD_WEBHOOK_URL,
    FEEDER_EXECS,
    FEEDER_LATEST,
    JST,
    MMLaneAccounting,
    MMQuote,
    MicrostructureMMManager,
    _append,
    _iso,
    _log,
    _sharpe_dd_from_hist,
    _window_stats_mm,
)

OUT_DIR = AG / "data" / "mm_research" / "wobimm2_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = AG / "data" / "dryrun_wobimm2_state.json"
PID_PATH = AG / "data" / "dryrun_wobimm2.pid"
STOP_FLAG = AG / "data" / "WOBIMM2_DRYRUN_STOP.flag"
LEDGER_PATH = OUT_DIR / "paired_ledger.jsonl"

COVER_WAIT_WOBI = 0.15
COVER_WAIT_OFI = 0.02
COVER_WAIT_TIMEOUT_SEC = 10.0
REPORT_MINUTE = 8


class CoverWaitMMManager(MicrostructureMMManager):
    """Frozen WOBIMM quotes + COVER_WAIT on the bid while short."""

    def compute_wobimm_quotes(
        self,
        bids: List[Dict[str, float]],
        asks: List[Dict[str, float]],
        inventory: float,
        ofi: float = 0.0,
        taker_vol_bid: float = 0.0,
        taker_vol_ask: float = 0.0,
        short_hold_sec: float = 0.0,
    ) -> MMQuote:
        quote = super().compute_wobimm_quotes(
            bids, asks, inventory, ofi=ofi, taker_vol_bid=taker_vol_bid, taker_vol_ask=taker_vol_ask
        )
        if inventory > -self.lot_size * 0.5:
            return quote
        if short_hold_sec >= COVER_WAIT_TIMEOUT_SEC:
            return quote
        wobi = self.calculate_wobi(bids, asks)
        if wobi > COVER_WAIT_WOBI or ofi > COVER_WAIT_OFI:
            quote.has_bid = False
            quote.bid_price = 0.0
            quote.pull_reason_bid = (
                f"COVER_WAIT_BUY_PRESSURE (wobi={wobi:+.2f}, ofi={ofi:+.2f}, "
                f"hold={short_hold_sec:.1f}s<{COVER_WAIT_TIMEOUT_SEC:.0f}s)"
            )
        return quote


class WOBIMM2DryRun:
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

        self.ctrl_mgr = MicrostructureMMManager(
            gamma=0.5, kappa=0.8, base_spread_bp=1.6, lot_size=0.001, max_inv_btc=0.004
        )
        self.w2_mgr = CoverWaitMMManager(
            gamma=0.5, kappa=0.8, base_spread_bp=1.6, lot_size=0.001, max_inv_btc=0.004
        )
        self.ctrl = MMLaneAccounting(lane="WOBIMM")
        self.w2 = MMLaneAccounting(lane="WOBIMM2")
        self.w2_short_since: Optional[float] = None
        self.cover_waits = 0

        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=60.0, batch_size=200)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code=symbol)
        if self.market_source == SOURCE_SHARED:
            self.feed: Any = FeederBoardSource(self.ingestion)
        else:
            self.feed = LiveBoardFeed(self.ingestion, product_code=symbol)

        self.q_ctrl = MMQuote()
        self.q_w2 = MMQuote()
        self.exec_fp: Optional[Any] = None
        self._init_exec_stream()
        self._restore_ledger()

    def _init_exec_stream(self) -> None:
        if FEEDER_EXECS.exists():
            try:
                self.exec_fp = open(FEEDER_EXECS, "r", encoding="utf-8")
                self.exec_fp.seek(0, 2)
            except Exception as exc:
                _log(f"Warning: could not open executions.jsonl: {exc}")

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
            acc = self.w2 if lane == "WOBIMM2" else self.ctrl if lane == "WOBIMM" else None
            if acc is None:
                continue
            acc.history.append(row)
            acc.total_trades += 1
            if bool(row.get("is_win")):
                acc.winning_trades += 1
            acc.total_pnl_jpy += float(row.get("pnl_jpy") or 0.0)
            acc.total_pnl_bp += float(row.get("pnl_bp") or 0.0)

    def _read_latest_depth(self) -> Tuple[List[Dict[str, float]], List[Dict[str, float]]]:
        if not FEEDER_LATEST.exists():
            return [], []
        try:
            raw = json.loads(FEEDER_LATEST.read_text(encoding="utf-8"))
            return raw.get("bids", [])[:10], raw.get("asks", [])[:10]
        except Exception:
            return [], []

    def _poll_executions(self) -> List[Dict[str, Any]]:
        new_execs: List[Dict[str, Any]] = []
        if not self.exec_fp:
            self._init_exec_stream()
        if not self.exec_fp:
            return new_execs
        try:
            while True:
                line = self.exec_fp.readline()
                if not line:
                    break
                row = json.loads(line)
                for item in row.get("rows", []):
                    new_execs.append(item)
        except Exception:
            pass
        return new_execs

    def _fill_lane(
        self,
        acc: MMLaneAccounting,
        quote: MMQuote,
        public_execs: List[Dict[str, Any]],
        best_bid: float,
        best_ask: float,
        now: float,
        tag: str,
    ) -> None:
        bid_filled = False
        ask_filled = False
        if quote.has_bid:
            if best_ask <= quote.bid_price:
                bid_filled = True
            else:
                for ex in public_execs:
                    if ex.get("side") == "SELL" and float(ex.get("price", 0.0)) <= quote.bid_price:
                        bid_filled = True
                        break
        if quote.has_ask:
            if best_bid >= quote.ask_price:
                ask_filled = True
            else:
                for ex in public_execs:
                    if ex.get("side") == "BUY" and float(ex.get("price", 0.0)) >= quote.ask_price:
                        ask_filled = True
                        break
        if bid_filled:
            tr = acc.on_buy_fill(quote.bid_price, quote.bid_size, now)
            if tr:
                _append(LEDGER_PATH, tr)
                acc.history.append(tr)
                icon = "WIN" if tr["is_win"] else "LOSS"
                _log(f"[{tag}] {icon} COVER_SHORT pnl={tr['pnl_bp']:+.2f}bp inv={acc.inventory:+.3f}")
            quote.has_bid = False
        if ask_filled:
            tr = acc.on_sell_fill(quote.ask_price, quote.ask_size, now)
            if tr:
                _append(LEDGER_PATH, tr)
                acc.history.append(tr)
                icon = "WIN" if tr["is_win"] else "LOSS"
                _log(f"[{tag}] {icon} CLOSE_LONG pnl={tr['pnl_bp']:+.2f}bp inv={acc.inventory:+.3f}")
            quote.has_ask = False

    def _tick(self, snap: Any, now: float) -> None:
        best_bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        best_ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            return
        depth_bids, depth_asks = self._read_latest_depth()
        if not depth_bids or not depth_asks:
            return
        taker_bid_v = float(getattr(snap, "taker_volume_bid", 0.0) or 0.0)
        taker_ask_v = float(getattr(snap, "taker_volume_ask", 0.0) or 0.0)
        imbalance = float(getattr(snap, "imbalance", 0.0) or 0.0)
        ofi = float(taker_bid_v - taker_ask_v) if (taker_bid_v + taker_ask_v) > 0 else (imbalance * 0.1)

        if self.w2.inventory <= -0.0005:
            if self.w2_short_since is None:
                self.w2_short_since = now
            short_hold = now - self.w2_short_since
        else:
            self.w2_short_since = None
            short_hold = 0.0

        self.q_ctrl = self.ctrl_mgr.compute_wobimm_quotes(
            depth_bids, depth_asks, self.ctrl.inventory, ofi=ofi,
            taker_vol_bid=taker_bid_v, taker_vol_ask=taker_ask_v,
        )
        self.q_w2 = self.w2_mgr.compute_wobimm_quotes(
            depth_bids, depth_asks, self.w2.inventory, ofi=ofi,
            taker_vol_bid=taker_bid_v, taker_vol_ask=taker_ask_v,
            short_hold_sec=short_hold,
        )
        if self.q_w2.pull_reason_bid.startswith("COVER_WAIT"):
            self.cover_waits += 1
        if not self.q_ctrl.has_ask and (
            self.q_ctrl.pull_reason_ask.startswith("TOXIC") or "ADVERSE" in self.q_ctrl.pull_reason_ask
        ):
            self.ctrl.toxicity_pulls += 1
        if not self.q_w2.has_ask and (
            self.q_w2.pull_reason_ask.startswith("TOXIC") or "ADVERSE" in self.q_w2.pull_reason_ask
        ):
            self.w2.toxicity_pulls += 1

        public_execs = self._poll_executions()
        self._fill_lane(self.ctrl, self.q_ctrl, public_execs, best_bid, best_ask, now, "WOBIMM")
        self._fill_lane(self.w2, self.q_w2, public_execs, best_bid, best_ask, now, "WOBIMM2")

    def _lane_stats(self, acc: MMLaneAccounting) -> Dict[str, Any]:
        n = acc.total_trades
        wr = (acc.winning_trades / n * 100.0) if n else 0.0
        ev = (acc.total_pnl_bp / n) if n else 0.0
        sd = _sharpe_dd_from_hist(acc.history)
        return {
            "total_trades": n,
            "winning_trades": acc.winning_trades,
            "win_rate_pct": round(wr, 1),
            "pnl_bp": round(acc.total_pnl_bp, 2),
            "pnl_jpy": round(acc.total_pnl_jpy, 1),
            "ev_bp": round(ev, 4),
            "sharpe": sd["sharpe"],
            "max_dd_bp": sd["max_dd_bp"],
            "inventory_btc": round(acc.inventory, 4),
            "max_inventory_btc": round(acc.max_inventory, 4),
            "toxicity_pulls": acc.toxicity_pulls,
            "stats_1h": _window_stats_mm(acc.history, 1.0),
            "stats_24h": _window_stats_mm(acc.history, 24.0),
        }

    def metrics(self) -> Dict[str, Any]:
        w2 = self._lane_stats(self.w2)
        ctrl = self._lane_stats(self.ctrl)
        comment = (
            f"直近1h: WOBIMM2 は {w2['stats_1h']['pnl_bp']:+.2f} bp（勝率 {w2['stats_1h']['win_rate_pct']:.1f}%、"
            f"{w2['stats_1h']['total_trades']}件）。CTRL WOBIMM は {ctrl['stats_1h']['pnl_bp']:+.2f} bp。"
            f"COVER_WAIT 発動 {self.cover_waits} 回（timeout {COVER_WAIT_TIMEOUT_SEC:.0f}s）。"
        )
        return {
            "ts_jst": _iso(),
            "model": "WOBIMM2",
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "market_source": self.market_source,
            "wire": "NO",
            "stack": "WOBIMM + COVER_WAIT_BUY_PRESSURE (CSR-633 · 1-variable)",
            "WOBIMM2": w2,
            "WOBIMM": ctrl,
            "cover_waits": self.cover_waits,
            "cover_wait_timeout_sec": COVER_WAIT_TIMEOUT_SEC,
            "compare": {
                "delta_pnl_bp": round(w2["pnl_bp"] - ctrl["pnl_bp"], 2),
                "delta_1h_pnl_bp": round(w2["stats_1h"]["pnl_bp"] - ctrl["stats_1h"]["pnl_bp"], 2),
                "delta_24h_pnl_bp": round(w2["stats_24h"]["pnl_bp"] - ctrl["stats_24h"]["pnl_bp"], 2),
                "delta_sharpe": round(w2["sharpe"] - ctrl["sharpe"], 4),
            },
            "latest_quotes": {
                "wobimm2": {
                    "bid": self.q_w2.bid_price,
                    "ask": self.q_w2.ask_price,
                    "has_bid": self.q_w2.has_bid,
                    "has_ask": self.q_w2.has_ask,
                    "pull_bid": self.q_w2.pull_reason_bid,
                    "pull_ask": self.q_w2.pull_reason_ask,
                }
            },
            "comment": comment,
        }

    def save_state(self) -> None:
        m = self.metrics()
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(STATE_PATH)
        self.last_state_save = time.time()

    def send_discord_report(self, kind: str = "hourly") -> None:
        m = self.metrics()
        w2 = m["WOBIMM2"]
        ctrl = m["WOBIMM"]
        cmp = m["compare"]
        title_map = {
            "start": "[START] WOBIMM2 DryRun (COVER_WAIT vs WOBIMM CTRL)",
            "hourly": f"[HOURLY #{int(m['elapsed_hours'])}] WOBIMM2 定期進捗 (1h+24h · CTRL対比)",
            "final": "[FINAL] WOBIMM2 DryRun 停止",
        }
        color = 0x3498DB if cmp["delta_1h_pnl_bp"] >= 0 else 0xE67E22
        payload = {
            "username": "WOBIMM2 Reporter",
            "embeds": [
                {
                    "title": title_map.get(kind, f"[REPORT] WOBIMM2 {kind}"),
                    "description": (
                        "WOBIMM2 = frozen WOBIMM + COVER_WAIT only (CSR-633).\n"
                        "ショート中に買い圧力が残る間はカバーBidを最大10s引く。Offset非変更。WIRE=NO。"
                    ),
                    "color": color,
                    "fields": [
                        {
                            "name": "直近 1時間 (1h)",
                            "value": (
                                f"• **WOBIMM2**: **`{w2['stats_1h']['pnl_bp']:+.2f} bp`** "
                                f"({w2['stats_1h']['pnl_jpy']:+.1f}円) | 勝率 `{w2['stats_1h']['win_rate_pct']:.1f}%` "
                                f"({w2['stats_1h']['win_trades']}/{w2['stats_1h']['total_trades']}) | "
                                f"EV `{w2['stats_1h']['ev_bp']:+.2f}bp`\n"
                                f"• **WOBIMM CTRL**: `{ctrl['stats_1h']['pnl_bp']:+.2f} bp` "
                                f"({ctrl['stats_1h']['pnl_jpy']:+.1f}円) | 勝率 `{ctrl['stats_1h']['win_rate_pct']:.1f}%` "
                                f"({ctrl['stats_1h']['win_trades']}/{ctrl['stats_1h']['total_trades']})\n"
                                f"• **1h Δ**: **`{cmp['delta_1h_pnl_bp']:+.2f} bp`**"
                            ),
                            "inline": False,
                        },
                        {
                            "name": "24時間 累積 (24h)",
                            "value": (
                                f"• **WOBIMM2**: **`{w2['stats_24h']['pnl_bp']:+.2f} bp`** "
                                f"({w2['stats_24h']['pnl_jpy']:+.1f}円) | 勝率 `{w2['stats_24h']['win_rate_pct']:.1f}%` "
                                f"({w2['stats_24h']['win_trades']}/{w2['stats_24h']['total_trades']}) | "
                                f"MaxDD `{w2['stats_24h']['max_dd_bp']:.1f}bp`\n"
                                f"• **WOBIMM CTRL**: `{ctrl['stats_24h']['pnl_bp']:+.2f} bp` "
                                f"({ctrl['stats_24h']['pnl_jpy']:+.1f}円) | 勝率 `{ctrl['stats_24h']['win_rate_pct']:.1f}%` "
                                f"({ctrl['stats_24h']['win_trades']}/{ctrl['stats_24h']['total_trades']}) | "
                                f"MaxDD `{ctrl['stats_24h']['max_dd_bp']:.1f}bp`\n"
                                f"• **24h Δ**: **`{cmp['delta_24h_pnl_bp']:+.2f} bp`**"
                            ),
                            "inline": False,
                        },
                        {
                            "name": "COVER_WAIT",
                            "value": (
                                f"• 発動: `{self.cover_waits}` 回 · timeout `{COVER_WAIT_TIMEOUT_SEC:.0f}s`\n"
                                f"• 在庫 W2 `{w2['inventory_btc']:+.4f}` / CTRL `{ctrl['inventory_btc']:+.4f}` BTC\n"
                                f"• WIRE=NO · LIVE非触 · Offset凍結"
                            ),
                            "inline": True,
                        },
                        {
                            "name": "コメント",
                            "value": f"> {m['comment']}",
                            "inline": False,
                        },
                    ],
                    "footer": {"text": f"Antigravity Quants · WOBIMM2 · {m['ts_jst']}"},
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
        _log("=== WOBIMM2 DryRun START (WIRE=NO · COVER_WAIT · CSR-633) ===")
        _log(f"Market source: {self.market_source} | Report :{REPORT_MINUTE:02d} + {self.report_interval_sec:.0f}s")
        PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.feed.start()
        self.save_state()
        self.send_discord_report("start")
        last_snap = None
        try:
            while self.running:
                if STOP_FLAG.exists():
                    _log("STOP flag present — exiting")
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
                if not getattr(last_snap, "best_bid", None) or not getattr(last_snap, "best_ask", None):
                    time.sleep(0.1)
                    continue
                self._tick(last_snap, now)
                if now - self.last_state_save >= 10.0:
                    self.save_state()
                jst_now = datetime.now(JST)
                current_hour = jst_now.hour
                if current_hour != self.last_reported_hour and jst_now.minute >= REPORT_MINUTE:
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
            if self.exec_fp:
                try:
                    self.exec_fp.close()
                except Exception:
                    pass
            if PID_PATH.exists():
                PID_PATH.unlink(missing_ok=True)
            _log("=== WOBIMM2 DryRun STOPPED ===")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WOBIMM2 DryRun COVER_WAIT (WIRE=NO)")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    args = parser.parse_args()
    runner = WOBIMM2DryRun(
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
