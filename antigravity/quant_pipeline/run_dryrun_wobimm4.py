#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WOBIMM4 paper — UKI one-sided scalper (WIRE=NO).

Does not touch LIVE. ENABLE_REAL_TRADING is forced false.
Fill = board imitation (BOOK_CROSS / BOARD_TRADE). Not mid. Not queue-honest.
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

os.environ["ENABLE_REAL_TRADING"] = "false"

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

from antigravity.quant_pipeline.event_bus import EventBus  # noqa: E402
from antigravity.quant_pipeline.feeder_board_source import (  # noqa: E402
    FeederBoardSource,
    resolve_market_source,
    SOURCE_SHARED,
)
from antigravity.quant_pipeline.ingestion import LiveBoardFeed, MarketDataIngestion  # noqa: E402
from antigravity.quant_pipeline.parquet_logger import ParquetBatchLogger  # noqa: E402
from antigravity.quant_pipeline.mm_discord import MM_DISCORD_WEBHOOK_URL  # noqa: E402
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import WOBIDirectionalEngine3  # noqa: E402
from antigravity.quant_pipeline.run_dryrun_wobimm import _window_stats_mm  # noqa: E402
from antigravity.quant_pipeline.honest_paper_fill import (  # noqa: E402
    HonestPaperBook,
    honest_enter_px,
    micro_veto,
    prefill_should_pull,
    queue_join_ok,
)
from antigravity.quant_pipeline.run_wobimm_live import public_maker_fill  # noqa: E402
from antigravity.quant_pipeline.wobimm4_policy import (  # noqa: E402
    CONFIRM_TICKS,
    ENTER,
    EXIT,
    KEEP,
    MAX_INV_BTC,
    PULL,
    SIZE_BTC,
    WAIT,
    Wobimm4State,
    decide,
    signed_signal,
    step_confirm,
)

JST = timezone(timedelta(hours=9))
OUT_DIR = AG / "data" / "mm_research" / "wobimm4_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = AG / "data" / "dryrun_wobimm4_state.json"
PID_PATH = AG / "data" / "dryrun_wobimm4.pid"
STOP_FLAG = AG / "data" / "WOBIMM4_DRYRUN_STOP.flag"
LEDGER_PATH = OUT_DIR / "paired_ledger.jsonl"
AUDIT_PATH = OUT_DIR / "audit.jsonl"
FEEDER_LATEST = AG / "data" / "market_feeder" / "latest.json"
FEEDER_EXECS = AG / "data" / "market_feeder" / "executions.jsonl"
REPORT_MINUTE = 10


def _iso() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def _log(msg: str) -> None:
    print(f"[{_iso()}] {msg}", flush=True)


def _append(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def frontier_px(side: str, bid: float, ask: float) -> float:
    if side == "BUY":
        if bid <= 0:
            return 0.0
        if ask - bid > 1.0:
            return min(bid + 1.0, ask - 1.0)
        return bid
    if ask <= 0:
        return 0.0
    if ask - bid > 1.0:
        return max(ask - 1.0, bid + 1.0)
    return ask


def maker_px(side: str, px: float, bid: float, ask: float) -> float:
    if side == "BUY":
        return min(px, bid) if bid > 0 else px
    return max(px, ask) if ask > 0 else px


def exit_limit_px(side: str, bid: float, ask: float) -> float:
    """EXIT sits where board-imitation can fill after venue confirm.

    Cover BUY: ask-1 (sells print at bid still go through). Flatten SELL: bid+1.
    Not a bp trigger. ENTER stays on maker_px.
    """
    if side == "BUY":
        if ask <= 0:
            return bid
        if bid > 0 and ask - bid > 1.0:
            return ask - 1.0
        return bid
    if bid <= 0:
        return ask
    if ask > 0 and ask - bid > 1.0:
        return bid + 1.0
    return ask


def exit_quote_fillable(side: str, px: float, bid: float, ask: float) -> bool:
    """Opposite-side prints at the touch can still go through this EXIT.

    BUY below the bid, or SELL above the ask, is left behind. Not a bp gate.
    """
    if not side or px <= 0:
        return False
    if side == "BUY":
        return bid <= 0 or px >= bid
    return ask <= 0 or px <= ask


def board_predicts_fill(
    side: str,
    px: float,
    bid: float,
    ask: float,
    execs: List[Dict[str, Any]],
) -> Optional[str]:
    """Board-imitation only. None = cannot fill at this price."""
    if not side or px <= 0:
        return None
    hit = public_maker_fill(
        side=side,
        price=px,
        acceptance_id="",
        public_execs=execs or [],
        best_bid=bid,
        best_ask=ask,
    )
    if not hit:
        return None
    return str(hit[0])


def load_wobimm4_ledger(path: Path = LEDGER_PATH) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def build_wobimm4_discord_payload(m: Dict[str, Any], kind: str) -> Dict[str, Any]:
    s1 = m.get("stats_1h") or {}
    s24 = m.get("stats_24h") or {}
    title_map = {
        "start": "[START] WOBIMM4 paper (UKI one-sided · WIRE=NO)",
        "hourly": f"[HOURLY #{int(m.get('elapsed_hours') or 0)}] WOBIMM4 1h+24h",
        "final": "[FINAL] WOBIMM4 DryRun 停止",
    }
    pnl_1h = float(s1.get("pnl_bp") or 0.0)
    color = 0x2ECC71 if pnl_1h >= 0 else 0xE67E22
    return {
        "username": "WOBIMM4",
        "embeds": [
            {
                "title": title_map.get(kind, f"[REPORT] WOBIMM4 {kind}"),
                "description": (
                    "UKI one-sided maker · live EV hold · flatten when EV gone · "
                    "no spread flatten. WIRE=NO · BOARD_IMITATION."
                ),
                "color": color,
                "fields": [
                    {
                        "name": "⏱️ 直近 1時間 (1h)",
                        "value": (
                            f"• **WOBIMM4**: **`{s1.get('pnl_bp' , 0):+.2f} bp`** "
                            f"({s1.get('pnl_jpy', 0):+.1f}円) | "
                            f"勝率 `{s1.get('win_rate_pct', 0):.1f}%` "
                            f"({s1.get('win_trades', 0)}/{s1.get('total_trades', 0)}) | "
                            f"EV **`{s1.get('ev_bp' , 0):+.2f}bp`**"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "📊 24時間 累積 (24h)",
                        "value": (
                            f"• **WOBIMM4**: **`{s24.get('pnl_bp', 0):+.2f} bp`** "
                            f"({s24.get('pnl_jpy', 0):+.1f}円) | "
                            f"勝率 `{s24.get('win_rate_pct', 0):.1f}%` "
                            f"({s24.get('win_trades', 0)}/{s24.get('total_trades', 0)}) | "
                            f"EV `{s24.get('ev_bp', 0):+.2f}bp` | "
                            f"MaxDD `{s24.get('max_dd_bp', 0):.1f}bp`"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "執行",
                        "value": (
                            f"• 累積 `{m.get('pnl_bp', 0):+.2f} bp` ({m.get('pnl_jpy', 0):+.1f}円) · "
                            f"n={m.get('total_trades', 0)} · WR `{m.get('win_rate_pct', 0):.1f}%`\n"
                            f"• net `{m.get('net_lots', 0):+d}` · working `{m.get('working') or '—'}`\n"
                            f"• posts `{m.get('posts', 0)}` · fills `{m.get('fills', 0)}` · "
                            f"cancels `{m.get('cancels', 0)}`\n"
                            f"• cover_wait `{m.get('cover_waits', 0)}` · "
                            f"flatten_wait `{m.get('flatten_waits', 0)}` · "
                            f"live_disagree `{m.get('live_disagrees', 0)}` · "
                            f"adverse_cut `{m.get('adverse_cuts', 0)}` · "
                            f"edge_pull `{m.get('edge_pulls', 0)}`"
                        ),
                        "inline": False,
                    },
                ],
                "footer": {"text": f"Antigravity Quants · WOBIMM4 · {m.get('ts_jst', '')}"},
            }
        ],
    }


class WOBIMM4DryRun:
    def __init__(
        self,
        interval_sec: float = 1.0,
        market_source: Optional[str] = None,
        report_interval_sec: float = 3600.0,
    ) -> None:
        self.interval_sec = interval_sec
        self.market_source = resolve_market_source(market_source)
        self.report_interval_sec = report_interval_sec
        self.running = True
        self.t0 = time.time()
        self.last_report = time.time()
        self.last_reported_hour = -1
        self.lane_name = getattr(self, "LANE_NAME", "WOBIMM4")
        self.fill_model = getattr(self, "FILL_MODEL", "BOARD_IMITATION")
        self.buy_only = bool(getattr(self, "BUY_ONLY", False))
        self.state_path = Path(getattr(self, "STATE_FILE", STATE_PATH))
        self.pid_path = Path(getattr(self, "PID_FILE", PID_PATH))
        self.ledger_path = Path(getattr(self, "LEDGER_FILE", LEDGER_PATH))
        self.audit_path = Path(getattr(self, "AUDIT_FILE", AUDIT_PATH))
        self.stop_flag = Path(getattr(self, "STOP_FILE", STOP_FLAG))
        self.paper = None
        self.net = 0
        self.entry_px = 0.0
        self.working_side: Optional[str] = None
        self.working_px = 0.0
        self.working_id = ""
        self.pnl_bp = 0.0
        self.pnl_jpy = 0.0
        self.trades = 0
        self.wins = 0
        self.posts = 0
        self.cancels = 0
        self.fills = 0
        self.sig_pending = 0
        self.sig_streak = 0
        self.sig_confirmed = 0
        self.cover_waits = 0
        self.flatten_waits = 0
        self.live_disagrees = 0
        self.adverse_cuts = 0
        self.edge_pulls = 0
        self.queue_vetoes = 0
        self.micro_vetoes = 0
        self.prefill_pulls = 0
        self.pending_exit_why = ""
        self.entry_src = ""
        self.entry_ts = 0.0
        self._pending_fill: Optional[Dict[str, Any]] = None
        self._tape: Dict[str, Any] = {}
        self._cover_waiting = False
        self._flatten_waiting = False
        self._live_disagreeing = False
        self.history: List[Dict[str, Any]] = load_wobimm4_ledger(self.ledger_path)
        if self.history:
            self.pnl_bp = sum(float(r.get("pnl_bp") or 0.0) for r in self.history)
            self.pnl_jpy = sum(float(r.get("pnl_jpy") or 0.0) for r in self.history)
            self.trades = len(self.history)
            self.wins = sum(1 for r in self.history if r.get("is_win"))
        self.engine = WOBIDirectionalEngine3(depth_k=6, lambda_decay=0.5, alpha=1.0, beta=0.5, gamma=0.3)
        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=60.0, batch_size=200)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code="FX_BTC_JPY")
        if self.market_source == SOURCE_SHARED:
            self.feed: Any = FeederBoardSource(self.ingestion)
        else:
            self.feed = LiveBoardFeed(self.ingestion, product_code="FX_BTC_JPY")
        self.exec_fp: Optional[Any] = None
        self.last_state_save = 0.0
        self._init_exec_stream()
        self._resume_paper_state()

    def stop(self, *_a) -> None:
        self.running = False

    def _resume_paper_state(self) -> None:
        """Adopt leftover paper inventory. Drop ghosts with no entry price."""
        if not self.state_path.exists():
            return
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if st.get("model") not in {self.lane_name, "WOBIMM4"} or st.get("wire") != "NO":
            return
        net = int(st.get("net_lots") or 0)
        entry = float(st.get("entry_px") or 0.0)
        if net != 0 and entry <= 0:
            _log("resume dropped ghost paper inventory (no entry_px)")
            net = 0
            entry = 0.0
        self.net = net
        self.entry_px = entry
        if not self.history:
            self.pnl_bp = float(st.get("pnl_bp") or 0.0)
            self.pnl_jpy = float(st.get("pnl_jpy") or 0.0)
            self.trades = int(st.get("total_trades") or 0)
            wr = float(st.get("win_rate_pct") or 0.0)
            self.wins = int(round(self.trades * wr / 100.0)) if self.trades else 0
        self.posts = int(st.get("posts") or 0)
        self.cancels = int(st.get("cancels") or 0)
        self.fills = int(st.get("fills") or 0)
        self.sig_confirmed = int(st.get("signal") or 0)
        self.sig_streak = int(st.get("signal_streak") or 0)
        self.cover_waits = int(st.get("cover_waits") or 0)
        self.flatten_waits = int(st.get("flatten_waits") or 0)
        self.live_disagrees = int(st.get("live_disagrees") or 0)
        self.adverse_cuts = int(st.get("adverse_cuts") or 0)
        self.edge_pulls = int(st.get("edge_pulls") or 0)
        if self.net:
            _log(f"resume paper net={self.net:+d} @ {self.entry_px:,.0f}")

    def _init_exec_stream(self) -> None:
        if FEEDER_EXECS.exists():
            try:
                self.exec_fp = open(FEEDER_EXECS, "r", encoding="utf-8")
                self.exec_fp.seek(0, 2)
            except Exception as exc:
                _log(f"exec stream: {exc}")

    def _depth(self) -> Tuple[List[Dict[str, float]], List[Dict[str, float]]]:
        if not FEEDER_LATEST.exists():
            return [], []
        try:
            raw = json.loads(FEEDER_LATEST.read_text(encoding="utf-8"))
            return raw.get("bids", [])[:10], raw.get("asks", [])[:10]
        except Exception:
            return [], []

    def _poll_execs(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.exec_fp:
            self._init_exec_stream()
        if not self.exec_fp:
            return out
        try:
            while True:
                line = self.exec_fp.readline()
                if not line:
                    break
                row = json.loads(line)
                for item in row.get("rows", []):
                    out.append(item)
        except Exception:
            pass
        return out

    def _post(self, side: str, px: float, why: str) -> None:
        if abs(self.net) * SIZE_BTC >= MAX_INV_BTC - 1e-12 and (
            (side == "BUY" and self.net >= 0) or (side == "SELL" and self.net <= 0)
        ):
            return
        self.working_side = side
        self.working_px = px
        self.working_id = f"SIM-{side}-{int(time.time() * 1000)}"
        self.posts += 1
        _append(
            self.audit_path,
            {"event": "ORDER_POSTED", "ts": _iso(), "side": side, "px": px, "id": self.working_id, "why": why},
        )
        _log(f"[{self.lane_name}] POST {side} @ {px:,.0f} ({why})")

    def _pull(self, why: str) -> None:
        self._pending_fill = None
        if not self.working_side:
            return
        _append(
            self.audit_path,
            {"event": "CANCEL", "ts": _iso(), "side": self.working_side, "id": self.working_id, "where": why},
        )
        _log(f"[{self.lane_name}] CANCEL {self.working_side} {self.working_id} ({why})")
        self.cancels += 1
        self.working_side = None
        self.working_px = 0.0
        self.working_id = ""

    def _close_ledger(self, exit_px: float, src: str, why: str) -> None:
        if self.entry_px <= 0 or self.net == 0:
            return
        if self.net > 0:
            pnl_bp = ((exit_px / self.entry_px) - 1.0) * 1e4
        else:
            pnl_bp = ((self.entry_px / exit_px) - 1.0) * 1e4
        pnl_j = pnl_bp * SIZE_BTC * self.entry_px / 1e4
        self.pnl_bp += pnl_bp
        self.pnl_jpy += pnl_j
        self.trades += 1
        if pnl_bp > 0:
            self.wins += 1
        _append(
            self.ledger_path,
            {
                "ts": time.time(),
                "lane": self.lane_name,
                "action": "CLOSE_LONG" if self.net > 0 else "COVER_SHORT",
                "price": exit_px,
                "size": SIZE_BTC,
                "pnl_bp": round(pnl_bp, 2),
                "pnl_jpy": round(pnl_j, 1),
                "is_win": pnl_bp > 0,
                "source": src,
                "exit_reason": why,
                "entry_src": self.entry_src or "",
                "hold_s": round(time.time() - self.entry_ts, 1) if self.entry_ts else None,
            },
        )
        _log(f"[{self.lane_name}] CLOSE pnl={pnl_bp:+.2f}bp src={src} ({why})")
        if why.startswith("adverse_cut"):
            self.adverse_cuts += 1
        self.history.append(
            {
                "ts": time.time(),
                "pnl_bp": round(pnl_bp, 2),
                "pnl_jpy": round(pnl_j, 1),
                "is_win": pnl_bp > 0,
            }
        )
        self.net = 0
        self.entry_px = 0.0
        self.entry_src = ""
        self.entry_ts = 0.0
        self._cover_waiting = False
        self._flatten_waiting = False

    def _maybe_fill(self, execs: List[Dict[str, Any]], bid: float, ask: float) -> None:
        if self._confirm_predicted_fill(execs, bid, ask):
            return
        if not self.working_side or self._pending_fill:
            return
        if self.fill_model == "HONEST":
            self._maybe_fill_honest(execs, bid, ask)
            return
        src = board_predicts_fill(self.working_side, self.working_px, bid, ask, execs)
        if not src:
            return
        self._predict_fill(src)

    def _predict_fill(self, src: str) -> None:
        """Board imitation hit. Do not book until the next snapshot confirms."""
        self._pending_fill = {
            "side": self.working_side,
            "px": self.working_px,
            "src": src,
            "id": self.working_id,
        }
        _append(
            self.audit_path,
            {
                "event": "PREDICT_FILL",
                "ts": _iso(),
                "side": self.working_side,
                "px": self.working_px,
                "src": src,
                "id": self.working_id,
            },
        )
        _log(
            f"[{self.lane_name}] PREDICT {self.working_side} @ {self.working_px:,.0f} "
            f"src={src} — wait venue confirm"
        )

    def _confirm_predicted_fill(
        self,
        execs: List[Dict[str, Any]],
        bid: float,
        ask: float,
    ) -> bool:
        pend = self._pending_fill
        if not pend:
            return False
        src = board_predicts_fill(str(pend["side"]), float(pend["px"]), bid, ask, execs)
        if src:
            _log(
                f"[{self.lane_name}] CONFIRM {pend['side']} @ {float(pend['px']):,.0f} "
                f"src={src}"
            )
            self._pending_fill = None
            self._apply_fill(str(pend["side"]), float(pend["px"]), src)
            return True
        _log(
            f"[{self.lane_name}] PREDICT_MISS {pend['side']} @ {float(pend['px']):,.0f} "
            f"— venue did not confirm"
        )
        self._pending_fill = None
        return False

    def _maybe_fill_honest(self, execs: List[Dict[str, Any]], bid: float, ask: float) -> None:
        if self.paper is None:
            self.paper = HonestPaperBook()
        now = time.time()
        bids, asks = self._depth()
        self.paper.sync_quote(
            has_bid=self.working_side == "BUY",
            bid_price=self.working_px if self.working_side == "BUY" else 0.0,
            bid_size=SIZE_BTC,
            has_ask=self.working_side == "SELL",
            ask_price=self.working_px if self.working_side == "SELL" else 0.0,
            ask_size=SIZE_BTC,
            bids=bids,
            asks=asks,
            now=now,
        )
        if (
            self.net == 0
            and not self.pending_exit_why
            and self.working_side
        ):
            live = self.paper.bid if self.working_side == "BUY" else self.paper.ask
            if live is not None:
                pull, _eat, why = prefill_should_pull(
                    live.side, live.price, live.queue_left, execs
                )
                if pull:
                    self.paper.kill_side(live.side)
                    self.prefill_pulls += 1
                    self._pull(why)
                    return
        for order, src in self.paper.consume(execs, bid, ask, now):
            self._apply_fill(order.side, order.price, src)
            return

    def _apply_fill(self, side: str, px: float, src: str) -> None:
        self.fills += 1
        _append(
            self.audit_path,
            {"event": "FILL", "ts": _iso(), "side": side, "px": px, "src": src, "id": self.working_id},
        )
        _log(f"[{self.lane_name}] FILL {side} @ {px:,.0f} src={src}")
        self.working_side = None
        self.working_px = 0.0
        self.working_id = ""
        close_why = self.pending_exit_why or "flip_flatten"
        if side == "BUY":
            if self.net < 0:
                self._close_ledger(px, src, close_why)
                self.pending_exit_why = ""
            else:
                self.net = 1
                self.entry_px = px
                self.entry_src = src
                self.entry_ts = time.time()
        else:
            if self.net > 0:
                self._close_ledger(px, src, close_why)
                self.pending_exit_why = ""
            elif self.buy_only:
                _log(f"[{self.lane_name}] ignore short fill (BUY_ONLY)")
            else:
                self.net = -1
                self.entry_px = px
                self.entry_src = src
                self.entry_ts = time.time()

    def _count_pressure_wait(self, why: str) -> None:
        if why == "cover_wait_buy_pressure":
            if not self._cover_waiting:
                self.cover_waits += 1
                self._cover_waiting = True
            self._flatten_waiting = False
            return
        if why == "flatten_wait_sell_pressure":
            if not self._flatten_waiting:
                self.flatten_waits += 1
                self._flatten_waiting = True
            self._cover_waiting = False
            return
        self._cover_waiting = False
        self._flatten_waiting = False

    def metrics(self) -> Dict[str, Any]:
        n = self.trades
        s1 = _window_stats_mm(self.history, 1.0)
        s24 = _window_stats_mm(self.history, 24.0)
        return {
            "ts_jst": _iso(),
            "model": self.lane_name,
            "mode": "PAPER",
            "wire": "NO",
            "series": "MM",
            "buy_only": self.buy_only,
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "pnl_bp": round(self.pnl_bp, 2),
            "pnl_jpy": round(self.pnl_jpy, 1),
            "ev_bp": round(self.pnl_bp / n, 4) if n else 0.0,
            "win_rate_pct": round(100.0 * self.wins / n, 1) if n else 0.0,
            "total_trades": n,
            "net_lots": self.net,
            "working": self.working_side,
            "posts": self.posts,
            "cancels": self.cancels,
            "fills": self.fills,
            "fill": self.fill_model,
            "confirm_ticks": CONFIRM_TICKS,
            "signal": self.sig_confirmed,
            "signal_streak": self.sig_streak,
            "entry_px": self.entry_px,
            "cover_waits": self.cover_waits,
            "flatten_waits": self.flatten_waits,
            "live_disagrees": self.live_disagrees,
            "adverse_cuts": self.adverse_cuts,
            "edge_pulls": self.edge_pulls,
            "queue_vetoes": self.queue_vetoes,
            "micro_vetoes": self.micro_vetoes,
            "prefill_pulls": self.prefill_pulls,
            "instant_harvest": bool(getattr(self, "INSTANT_HARVEST", False)),
            "stats_1h": s1,
            "stats_24h": s24,
            "note": (
                "UKI one-sided · harvest ask-1 · no CROSS ENTER · "
                "ADVERSE_CUT −1.8bp · WIRE=NO"
                if getattr(self, "INSTANT_HARVEST", False)
                else
                "UKI one-sided · live EV hold · ev_gone flatten · "
                "confirm 3 ticks ENTER · EDGE_PULL · ADVERSE_CUT −1.8bp · "
                "WIRE=NO"
            ),
        }

    def save_state(self) -> None:
        self.state_path.write_text(
            json.dumps(self.metrics(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def send_discord_report(self, kind: str = "hourly") -> None:
        payload = build_wobimm4_discord_payload(self.metrics(), kind)
        try:
            req = urllib.request.Request(
                MM_DISCORD_WEBHOOK_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                _log(f"[DISCORD] {kind} report sent (HTTP {resp.status})")
        except Exception as exc:
            _log(f"[DISCORD] Warning: Failed to send report: {exc}")

    def _maybe_hourly_discord(self, now: float) -> None:
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

    def _tick(self, snap: Any, now: float) -> None:
        bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0:
            return
        spread_bp = ((ask - bid) / bid) * 1e4
        bids, asks = self._depth()
        m = self.engine.update_and_evaluate(bids, asks, bid, ask, now=now)
        self._tape = m
        execs = self._poll_execs()
        self._maybe_fill(execs, bid, ask)
        if self._pending_fill:
            return
        wobi = float(m.get("wobi") or 0.0)
        ofi = float(m.get("recent_ofi") or 0.0)
        mid = (bid + ask) / 2.0
        unrealized_bp = 0.0
        if self.net != 0 and self.entry_px > 0 and mid > 0:
            if self.net > 0:
                unrealized_bp = ((mid / self.entry_px) - 1.0) * 1e4
            else:
                unrealized_bp = ((self.entry_px / mid) - 1.0) * 1e4
        drift_bp = 0.0
        if self.working_side == "BUY" and self.working_px > 0 and bid > 0:
            drift_bp = ((bid - self.working_px) / self.working_px) * 1e4
        elif self.working_side == "SELL" and self.working_px > 0 and ask > 0:
            drift_bp = ((self.working_px - ask) / self.working_px) * 1e4
        raw, raw_why = signed_signal(wobi, ofi)
        self.sig_pending, self.sig_streak, self.sig_confirmed = step_confirm(
            self.sig_pending, self.sig_streak, self.sig_confirmed, raw
        )
        if self.sig_confirmed == 0 and raw != 0:
            why_sig = f"confirming {raw_why} {self.sig_streak}/{CONFIRM_TICKS}"
        elif self.sig_confirmed != 0 and raw != self.sig_confirmed and raw != 0:
            why_sig = f"confirming_flip {raw_why} {self.sig_streak}/{CONFIRM_TICKS}"
        else:
            why_sig = raw_why
        st = Wobimm4State(
            net_lots=self.net,
            working_side=self.working_side,
            wobi=wobi,
            recent_ofi=ofi,
            spread_bp=spread_bp,
            signal=self.sig_confirmed,
            signal_why=why_sig,
            unrealized_bp=unrealized_bp,
            drift_bp=drift_bp,
            honest_fill=self.fill_model == "HONEST",
            long_tight=bool(getattr(self, "LONG_TIGHT", False)),
            instant_harvest=bool(getattr(self, "INSTANT_HARVEST", False)),
        )
        action, side, why = decide(st)
        if (
            self.fill_model == "HONEST"
            and action == PULL
            and str(why).startswith("edge_drift")
            and self.working_side == "BUY"
        ):
            self._pull(f"reprice {why}")
            action, side, why = ENTER, "BUY", f"reprice {why}"
        if action == KEEP:
            self._count_pressure_wait(why)
            return
        if action == WAIT:
            if why.startswith("live_disagree"):
                if not self._live_disagreeing:
                    self.live_disagrees += 1
                    self._live_disagreeing = True
            else:
                self._live_disagreeing = False
            self._cover_waiting = False
            self._flatten_waiting = False
            return
        if action == PULL:
            self._count_pressure_wait(why)
            if why.startswith("edge_"):
                self.edge_pulls += 1
            self._pull(why)
            return
        if action in (ENTER, EXIT) and side:
            if self.buy_only and action == ENTER and side == "SELL":
                return
            if self.working_side and self.working_side != side:
                self._pull("replace")
            if self.working_side:
                if action == EXIT and self.working_side == side:
                    if exit_quote_fillable(self.working_side, self.working_px, bid, ask):
                        return
                    self._pull("exit_unfillable")
                else:
                    return
            if action == EXIT:
                px = exit_limit_px(side, bid, ask)
            else:
                px = maker_px(side, frontier_px(side, bid, ask), bid, ask)
            if px <= 0:
                return
            if action == ENTER and self.fill_model == "HONEST":
                veto, _sz, vwhy = micro_veto(side, execs)
                if veto:
                    self.micro_vetoes += 1
                    _log(f"[{self.lane_name}] VETO {side} {vwhy}")
                    return
                levels = bids if side == "BUY" else asks
                px, ok_q, qwhy = honest_enter_px(side, bid, ask, levels, preferred=px)
                if not ok_q:
                    self.queue_vetoes += 1
                    _log(f"[{self.lane_name}] VETO {side} {qwhy}")
                    return
                why = f"{why} {qwhy}"
            self._post(side, px, f"{action} {why}")
            self._live_disagreeing = False
            if action == EXIT:
                self.pending_exit_why = why
                self._cover_waiting = False
                self._flatten_waiting = False

    def run(self) -> int:
        _log(
            "=== WOBIMM4 DryRun START (WIRE=NO · harvest ask-1 · no CROSS ENTER) ==="
            if getattr(self, "INSTANT_HARVEST", False)
            else
            "=== WOBIMM4 DryRun START (WIRE=NO · live EV hold · ev_gone flatten) ==="
        )
        self.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.feed.start()
        self.save_state()
        self.send_discord_report("start")
        self.last_reported_hour = datetime.now(JST).hour
        self.last_report = time.time()
        last = None
        try:
            while self.running:
                if self.stop_flag.exists():
                    _log("STOP flag — exiting")
                    break
                now = time.time()
                try:
                    snap = self.feed.get(timeout=self.interval_sec)
                except Exception:
                    time.sleep(self.interval_sec)
                    continue
                if snap is not None:
                    last = snap
                if last is None:
                    time.sleep(0.2)
                    continue
                self._tick(last, now)
                if now - self.last_state_save >= 10.0:
                    self.save_state()
                    self.last_state_save = now
                self._maybe_hourly_discord(now)
                time.sleep(self.interval_sec)
        except KeyboardInterrupt:
            _log("SIGINT")
        except Exception as exc:
            _log(f"fatal: {exc}")
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
            if self.pid_path.exists():
                self.pid_path.unlink(missing_ok=True)
            _log("=== WOBIMM4 DryRun STOPPED ===")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WOBIMM4 paper UKI scalper (WIRE=NO)")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    parser.add_argument("--report-interval", type=float, default=3600.0)
    args = parser.parse_args()
    runner = WOBIMM4DryRun(
        interval_sec=args.interval,
        market_source=args.source,
        report_interval_sec=args.report_interval,
    )

    def _sig(signum, _frame):
        _log(f"signal {signum}")
        runner.stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
