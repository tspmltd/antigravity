#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WOBIMM5 LIVE — Position-EV MM + Adverse HIT_TOUCH (clean chassis).

Single runner. Does not subclass paper DryRun. Does not patch run_wobimm_live.
Libraries only: wobimm4_policy, HonestAdverseKit, BitFlyerClient, FeederBoardSource,
WOBIDirectionalEngine3.

Default DRY (no venue POST). REAL requires GO gates + --real + --confirm-go WOBIMM5.
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

from core.bitflyer_client import BitFlyerClient, OrderSubmitUnknownError  # noqa: E402

from antigravity.quant_pipeline.event_bus import EventBus  # noqa: E402
from antigravity.quant_pipeline.feeder_board_source import (  # noqa: E402
    FeederBoardSource,
    SOURCE_SHARED,
    resolve_market_source,
)
from antigravity.quant_pipeline.honest_adverse_kit import (  # noqa: E402
    HonestAdverseKit,
    StrategyProfile,
)
from antigravity.quant_pipeline.ingestion import LiveBoardFeed, MarketDataIngestion  # noqa: E402
from antigravity.quant_pipeline.live_discord import (  # noqa: E402
    live_discord_webhook_url,
    live_trade_embed,
    post_live_discord,
)
from antigravity.quant_pipeline.private_child_order_stream import (  # noqa: E402
    PrivateChildOrderStream,
)
from antigravity.quant_pipeline.parquet_logger import ParquetBatchLogger  # noqa: E402
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import WOBIDirectionalEngine3  # noqa: E402
from antigravity.quant_pipeline.run_dryrun_wobimm import _window_stats_mm  # noqa: E402
from antigravity.quant_pipeline.wobimm4_policy import (  # noqa: E402
    CONFIRM_TICKS,
    ENTER,
    EXIT,
    KEEP,
    LIVE_CONFIRM_TICKS,
    PULL,
    WAIT,
    Wobimm4State,
    decide,
    signed_signal,
    step_confirm,
)

JST = timezone(timedelta(hours=9))
PRODUCT = "FX_BTC_JPY"
SIZE_BTC = 0.001
MAX_INV_BTC = 0.001
# Venue min lot is 0.001; dust cover needs SIZE+|dust| (<= ~0.002).
DUST_COVER_MAX_BTC = 0.002
EXACT_FLAT_EPS_BTC = 1e-8
CONFIRM_TOKEN = "WOBIMM5"
CONFIRM_ENV = "WOBIMM5_LIVE"
# REAL: ignore BOOK_CROSS for this long after POST (HIT_TOUCH self-cross guard).
POST_BOOK_GUARD_SEC = 0.20
OPTIMISTIC_FILL_SOURCES = frozenset({"BOOK_CROSS", "BOARD_TRADE"})
RECONCILE_INTERVAL_SEC = 15.0
INV_MISMATCH_BTC = 0.0005
# Soft drift → venue force-sync. Hard halt only beyond physical ceiling.
HARD_HALT_BTC = 0.004
# Working/ghost private-adopt retries before force-sync (not halt).
ADOPT_GRACE_TRIES = 3
ADOPT_GRACE_SLEEP_SEC = 0.35
FLATTEN_CONFIRM_TRIES = 6
FLATTEN_CONFIRM_SLEEP_SEC = 0.5
DUST_SWEEP_COOLDOWN_SEC = 30.0
# Cancel stale entries; cool off after toxic tape.
# Exit machine: FLATTEN_SOFT (ev_gone/adverse_fast) = passive opposite maker LIMIT.
# FLATTEN_HARD (adverse_cut / toxic_adopt|fill / hard_stop) = MARKET only.
ENTRY_STALE_CANCEL_SEC = 8.0
ADVERSE_REENTRY_COOLDOWN_SEC = 45.0
# ADOPT blank-window guard: if mid already this far against fill at discover
# time, escalate to FLATTEN_HARD (MARKET) instead of resting into toxicity.
TOXIC_ADOPT_BP = 1.5
# After EXIT fill closes inventory, venue positions can lag / echo a second EXIT fill.
# Settling window covers REST/WS report delay (soak2: phantom at ~10s post-close).
CLOSE_SETTLE_SEC = 15.0
# Unattributed flat≠venue residue (true ghost): observe briefly, then Fail-Closed.
# Do NOT infinite-loop GHOST_POSITION_BLOCKED while leaving real venue inventory open.
GHOST_FAILCLOSED_HITS = 3
GHOST_FAILCLOSED_SEC = 3.0
# Soft adverse_fast dig-down guards → escalate to HARD MARKET.
# Reprice cap: after N Soft reprices still unfilled. Slip: unrealized ≤ −1.5bp.
SOFT_REPRICE_MAX = 3
SOFT_ADVERSE_ESCALATE_BP = 1.5
# After ENTRY: healthy positions defer Soft ev_gone so TP Maker can harvest.
# adverse_fast / HARD paths are never deferred.
TP_GRACE_SEC = 8.0
# Minimum harvest clamp for HOLD / ev_hold TP (bp). Soft ev_gone after grace
# falls back to best-opposite soft_maker (no clamp).
MIN_HARVEST_BP = 1.0
# ENTER only when book is wide enough to harvest without mid-trend reliance.
# 1.2 = MIN_HARVEST_BP(1.0) + 0.2bp margin (soak4–6: typical tape ~0.85bp).
MIN_ENTRY_SPREAD_BP = 1.2
# Passive ENTER behind the touch so clamped TP (+MIN_HARVEST_BP) lands near
# the opposite best (soak7: front-touch fill → TP buried → Soft→HARD −1.1bp).
# 0.5bp ≈ 590 JPY at ~11.8M.
ENTRY_OFFSET_BP = 0.5
TICK_JPY = 1.0

# 3-state exit machine (irreversible rank: HOLD < SOFT < HARD).
FLATTEN_HOLD = "HOLD"
FLATTEN_SOFT = "FLATTEN_SOFT"
FLATTEN_HARD = "FLATTEN_HARD"
_FLATTEN_RANK = {FLATTEN_HOLD: 0, FLATTEN_SOFT: 1, FLATTEN_HARD: 2}

OUT_DIR = AG / "data" / "mm_research" / "wobimm5_live"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = AG / "data" / "wobimm5_live_state.json"
PID_PATH = OUT_DIR / "wobimm5_live.pid"
LEDGER_PATH = OUT_DIR / "paired_ledger.jsonl"
AUDIT_PATH = OUT_DIR / "audit.jsonl"
FLATTEN_LAST = OUT_DIR / "flatten_last.json"
GO_FLAG = AG / "data" / "GO_WOBIMM5_LIVE.flag"
STOP_FLAG = AG / "data" / "WOBIMM5_LIVE_STOP.flag"
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


def _write_json(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def soft_maker_exit_px(side: str, bid: float, ask: float) -> float:
    """FLATTEN_SOFT passive wait: SELL@best_ask / BUY@best_bid (never cross)."""
    side_u = str(side or "").upper()
    if side_u == "SELL":
        return float(ask) if ask > 0 else 0.0
    if side_u == "BUY":
        return float(bid) if bid > 0 else 0.0
    return 0.0


def spread_bp(bid: float, ask: float) -> float:
    """Best bid/ask spread in basis points (vs bid)."""
    b = float(bid or 0.0)
    a = float(ask or 0.0)
    if b <= 0.0 or a <= 0.0 or a < b:
        return 0.0
    return ((a - b) / b) * 1e4


def entry_spread_allows(
    bid: float, ask: float, *, min_bp: float = MIN_ENTRY_SPREAD_BP
) -> Tuple[bool, float]:
    """Return (allowed, current_spread_bp) for Spread Gate."""
    sp = spread_bp(bid, ask)
    return bool(sp >= float(min_bp)), float(sp)


def ceil_tick(px: float, tick: float = TICK_JPY) -> float:
    """Round price up to the next venue tick (FX_BTC_JPY = 1 JPY)."""
    p = float(px or 0.0)
    t = float(tick or 0.0)
    if p <= 0 or t <= 0:
        return 0.0
    return float(math.ceil(p / t - 1e-12) * t)


def floor_tick(px: float, tick: float = TICK_JPY) -> float:
    """Round price down to the previous venue tick."""
    p = float(px or 0.0)
    t = float(tick or 0.0)
    if p <= 0 or t <= 0:
        return 0.0
    return float(math.floor(p / t + 1e-12) * t)


def entry_passive_px(
    side: str,
    bid: float,
    ask: float,
    *,
    offset_bp: float = ENTRY_OFFSET_BP,
) -> float:
    """ENTER maker behind touch (Post-Only safe).

    BUY:  floor_tick(best_bid * (1 - offset_bp/1e4))
    SELL: ceil_tick(best_ask  * (1 + offset_bp/1e4))
    """
    side_u = str(side or "").upper()
    ob = max(0.0, float(offset_bp or 0.0))
    if side_u == "BUY":
        b = float(bid or 0.0)
        if b <= 0:
            return 0.0
        return floor_tick(b * (1.0 - ob / 1e4))
    if side_u == "SELL":
        a = float(ask or 0.0)
        if a <= 0:
            return 0.0
        return ceil_tick(a * (1.0 + ob / 1e4))
    return 0.0


def tp_harvest_exit_px(
    side: str,
    bid: float,
    ask: float,
    entry_px: float,
    *,
    min_bp: float = MIN_HARVEST_BP,
) -> float:
    """HOLD/ev_hold TP: best opposite front, clamped to ≥ +min_bp vs entry.

    Long (SELL TP): max(best_ask, ceil_tick(entry * (1 + min_bp/1e4)))
    Short (BUY TP): min(best_bid, floor_tick(entry * (1 - min_bp/1e4)))
    Still LIMIT maker (never hits through the book intentionally).
    """
    side_u = str(side or "").upper()
    front = soft_maker_exit_px(side_u, bid, ask)
    ep = float(entry_px or 0.0)
    mb = float(min_bp or 0.0)
    if ep <= 0 or mb <= 0:
        return float(front)
    if side_u == "SELL":
        floor_px = ceil_tick(ep * (1.0 + mb / 1e4))
        if front <= 0:
            return float(floor_px)
        return float(max(front, floor_px))
    if side_u == "BUY":
        ceil_px = floor_tick(ep * (1.0 - mb / 1e4))
        if front <= 0:
            return float(ceil_px)
        return float(min(front, ceil_px))
    return 0.0


def classify_flatten_mode(why: str) -> str:
    """Map exit why → HOLD / FLATTEN_SOFT / FLATTEN_HARD."""
    w = str(why or "").strip()
    if not w:
        return FLATTEN_HOLD
    if (
        w.startswith("adverse_cut")
        or w.startswith("toxic_adopt")
        or w.startswith("toxic_fill")
        or w.startswith("hard_stop")
        or w.startswith("soft_escalate")
    ):
        return FLATTEN_HARD
    if (
        w.startswith("ev_gone")
        or w.startswith("adverse_fast")
        or w.startswith("EXIT ev_gone")
        or w.startswith("EXIT adverse_fast")
    ):
        return FLATTEN_SOFT
    # "EXIT adverse_cut ..." from post tags
    if "adverse_cut" in w:
        return FLATTEN_HARD
    if "toxic_adopt" in w or "toxic_fill" in w or "hard_stop" in w:
        return FLATTEN_HARD
    if "soft_escalate" in w:
        return FLATTEN_HARD
    if "ev_gone" in w or "adverse_fast" in w:
        return FLATTEN_SOFT
    if w.startswith("ev_hold") or "ev_hold" in w:
        return FLATTEN_HOLD
    return FLATTEN_HOLD


def advance_flatten_state(current: str, why: str) -> str:
    """Irreversible HOLD → SOFT → HARD. Never downgrade."""
    cur = current if current in _FLATTEN_RANK else FLATTEN_HOLD
    nxt = classify_flatten_mode(why)
    if _FLATTEN_RANK[nxt] > _FLATTEN_RANK[cur]:
        return nxt
    return cur


def exit_quote_fillable(side: str, px: float, bid: float, ask: float) -> bool:
    if not side or px <= 0:
        return False
    if side == "BUY":
        return bid <= 0 or px >= bid
    return ask <= 0 or px <= ask


def is_quote_crossed(side: str, price: float, best_bid: float, best_ask: float) -> bool:
    """Strict book-through. Equal touch is NOT crossed (HIT_TOUCH self-hit guard)."""
    side_u = side.upper()
    if side_u == "BUY" and best_ask > 0 and best_ask < price:
        return True
    if side_u == "SELL" and best_bid > 0 and best_bid > price:
        return True
    return False


def book_cross_guard_ok(last_post_ts: float, now: Optional[float] = None, guard_sec: float = POST_BOOK_GUARD_SEC) -> bool:
    """False within guard window after POST — suppresses same-tick BOOK_CROSS."""
    ts = float(last_post_ts or 0.0)
    if ts <= 0:
        return True
    t = float(now if now is not None else time.time())
    return (t - ts) >= float(guard_sec)


def public_trade_through(side: str, price: float, public_execs: List[Dict[str, Any]]) -> bool:
    side_u = side.upper()
    for ex in public_execs:
        try:
            px = float(ex.get("price") or 0.0)
            ex_side = str(ex.get("side") or "").upper()
        except Exception:
            continue
        if px <= 0:
            continue
        if side_u == "BUY" and ex_side == "SELL" and px <= price + 1e-12:
            return True
        if side_u == "SELL" and ex_side == "BUY" and px >= price - 1e-12:
            return True
    return False


def public_maker_fill(
    *,
    side: str,
    price: float,
    acceptance_id: str,
    public_execs: List[Dict[str, Any]],
    best_bid: float,
    best_ask: float,
    allow_book: bool = True,
) -> Optional[Tuple[str, float]]:
    """PUBLIC_OID → BOOK_CROSS → BOARD_TRADE. Local copy (no import from old LIVE)."""
    oid = str(acceptance_id or "")
    side_u = side.upper()
    for ex in public_execs:
        if not oid:
            continue
        buy_id = str(ex.get("buy_child_order_acceptance_id") or "")
        sell_id = str(ex.get("sell_child_order_acceptance_id") or "")
        matched = (side_u == "BUY" and buy_id == oid) or (side_u == "SELL" and sell_id == oid)
        if matched:
            try:
                sz = float(ex.get("size") or 0.0)
            except Exception:
                sz = 0.0
            return "PUBLIC_OID", sz
    if not allow_book:
        return None
    if is_quote_crossed(side_u, price, best_bid, best_ask):
        return "BOOK_CROSS", 0.0
    if public_trade_through(side_u, price, public_execs):
        return "BOARD_TRADE", 0.0
    return None


def parse_venue_net(positions: List[Dict[str, Any]]) -> float:
    net = 0.0
    for p in positions or []:
        try:
            sz = float(p.get("size") or 0.0)
            side = str(p.get("side") or "").upper()
        except Exception:
            continue
        if side == "BUY":
            net += sz
        elif side == "SELL":
            net -= sz
    return net


def is_isolated_dust(net: float, min_lot: float = SIZE_BTC) -> bool:
    """True when |net| is non-zero but below venue minimum order size."""
    abs_net = abs(float(net))
    return EXACT_FLAT_EPS_BTC < abs_net < (float(min_lot) - 1e-12)


def dust_neutralize_legs(
    net: float, lot: float = SIZE_BTC
) -> Optional[List[Tuple[str, float]]]:
    """Two MARKET legs that clear isolated dust to exact flat.

    short -d: BUY(lot+d) → +lot, then SELL(lot) → 0
    long  +d: SELL(lot+d) → -lot, then BUY(lot) → 0
    """
    net = float(net)
    lot = float(lot)
    if not is_isolated_dust(net, lot):
        return None
    dust = abs(net)
    cover = round(lot + dust, 8)
    if net < 0:
        return [("BUY", cover), ("SELL", round(lot, 8))]
    return [("SELL", cover), ("BUY", round(lot, 8))]


def refuse_real_gates(confirm_go: str) -> Optional[str]:
    if confirm_go != CONFIRM_TOKEN:
        return f"REFUSE: --confirm-go must be {CONFIRM_TOKEN}"
    if os.environ.get("CONFIRM_GO") != CONFIRM_ENV:
        return f"REFUSE: set CONFIRM_GO={CONFIRM_ENV}"
    if os.environ.get("ENABLE_REAL_TRADING", "false").lower() != "true":
        return "REFUSE: ENABLE_REAL_TRADING is not true"
    if STOP_FLAG.exists():
        return f"REFUSE: STOP flag present ({STOP_FLAG})"
    if not GO_FLAG.exists():
        return f"REFUSE: {GO_FLAG} missing"
    try:
        txt = GO_FLAG.read_text(encoding="utf-8")
    except Exception as exc:
        return f"REFUSE: cannot read GO flag: {exc}"
    if "status=ACTIVE" not in txt:
        return f"REFUSE: {GO_FLAG} is not status=ACTIVE"
    return None


class WOBIMM5Live:
    """MM5 LIVE chassis — decide/AE/venue/ledger in one place."""

    LANE = "WOBIMM5_LIVE"

    def __init__(
        self,
        *,
        size: float = SIZE_BTC,
        interval_sec: float = 1.0,
        market_source: Optional[str] = None,
        report_interval_sec: float = 3600.0,
        real: bool = False,
        max_runtime_sec: float = 0.0,
        max_fills: int = 0,
    ) -> None:
        if size > SIZE_BTC + 1e-12:
            raise ValueError(f"size must be <= {SIZE_BTC}")
        self.size = float(size)
        self.max_inv = MAX_INV_BTC
        self.interval_sec = float(interval_sec)
        self.market_source = resolve_market_source(market_source)
        self.report_interval_sec = float(report_interval_sec)
        self.real = bool(real)
        self.max_runtime_sec = float(max_runtime_sec or 0.0)
        self.max_fills = int(max_fills or 0)
        self.running = True
        self.t0 = time.time()
        self.last_report = time.time()
        self.last_reported_hour = -1
        self.last_state_save = 0.0

        self.net = 0  # lots: -1/0/+1
        self.entry_px = 0.0
        self.entry_src = ""
        self.entry_ts = 0.0
        self.working_side: Optional[str] = None
        self.working_px = 0.0
        self.working_id = ""
        self.working_posted_at = 0.0
        self.pending_exit_why = ""
        self.flatten_state = FLATTEN_HOLD  # HOLD → FLATTEN_SOFT → FLATTEN_HARD
        # Close-settle guard (Phantom Short/Long debounce).
        self._close_settle_until = 0.0
        self._close_settle_flat_side = ""  # side of the closing fill (SELL closed long)
        self._last_close_oid = ""
        self._soft_reprice_n = 0
        self._soft_exit_armed_ts = 0.0
        self.soft_to_hard_escalations = 0
        self.ghosts: List[Dict[str, Any]] = []  # optimistic predict — no inventory yet
        # Recent POSTs — adopt even after pull/reprice cleared working (anti double-exit).
        self.recent_posts: List[Dict[str, Any]] = []
        self.adopted_oids: set[str] = set()
        # Unattributed venue residue while local flat (Fail-Closed streak).
        self._ghost_mismatch_hits = 0
        self._ghost_mismatch_since = 0.0
        self.halt_trading = False
        self.halt_reason = ""
        self.last_reconcile_ts = 0.0
        self.predict_fills = 0
        self.adopt_fills = 0
        self.desync_halts = 0
        self.pnl_bp = 0.0
        self.pnl_jpy = 0.0
        self.trades = 0
        self.wins = 0
        self.posts = 0
        self.cancels = 0
        self.fills = 0
        self.rejects = 0
        self.api_errors = 0
        self.adverse_cuts = 0
        self.adverse_fast_cuts = 0
        self.adverse_market_exits = 0
        self.ev_gone_market_exits = 0
        self.entry_stale_cancels = 0
        self.force_syncs = 0
        self.spread_gate_blocks = 0
        self.edge_pulls = 0
        self.live_disagrees = 0
        self._live_disagreeing = False
        self._adverse_trigger_bp: Optional[float] = None
        self._adverse_trigger_ts = 0.0
        self._adverse_cooldown_until = 0.0
        self.sig_pending = 0
        self.sig_streak = 0
        self.sig_confirmed = 0
        self.history: List[Dict[str, Any]] = self._load_ledger()
        if self.history:
            self.pnl_bp = sum(float(r.get("pnl_bp") or 0.0) for r in self.history)
            self.pnl_jpy = sum(float(r.get("pnl_jpy") or 0.0) for r in self.history)
            self.trades = len(self.history)
            self.wins = sum(1 for r in self.history if r.get("is_win"))

        self.ae = HonestAdverseKit(
            lane=self.LANE,
            save_dir=str(AG / "data"),
            hard_stop_bp=-4.0,
            target_mfe_bp=3.0,
            profile=StrategyProfile.INVENTORY_MM,
            log_fn=_log,
        )
        self.engine = WOBIDirectionalEngine3(
            depth_k=6, lambda_decay=0.5, alpha=1.0, beta=0.5, gamma=0.3
        )
        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=60.0, batch_size=200)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code=PRODUCT)
        if self.market_source == SOURCE_SHARED:
            self.feed: Any = FeederBoardSource(self.ingestion)
        else:
            self.feed = LiveBoardFeed(self.ingestion, product_code=PRODUCT)
        self.exec_fp: Optional[Any] = None
        self._init_exec_stream()
        self.private_ws: Optional[PrivateChildOrderStream] = None
        self.private_ws_fills = 0
        self.toxic_inflight_cleanups = 0

        # Venue client: DRY never enables real trading via env leak.
        # REAL max_order allows SIZE+|dust| cover so flatten can clear sub-lot dust.
        if self.real:
            self.client = BitFlyerClient(
                enable_real_trading=True, max_order_size=DUST_COVER_MAX_BTC
            )
            self.private_ws = PrivateChildOrderStream(product_code=PRODUCT)
            if self.private_ws.start():
                _log(f"[{self.LANE}] PRIVATE_WS child_order_events started")
            else:
                _log(f"[{self.LANE}] PRIVATE_WS unavailable — public feeder only")
                self.private_ws = None
        else:
            os.environ["ENABLE_REAL_TRADING"] = "false"
            self.client = BitFlyerClient(
                enable_real_trading=False, max_order_size=self.size
            )
        self._last_dust_sweep_ts = 0.0

    def stop(self, *_a) -> None:
        self.running = False
        if self.private_ws is not None:
            try:
                self.private_ws.stop()
            except Exception:
                pass

    def _load_ledger(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not LEDGER_PATH.exists():
            return rows
        try:
            with LEDGER_PATH.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except Exception:
            pass
        return rows

    def _init_exec_stream(self) -> None:
        if FEEDER_EXECS.exists():
            try:
                self.exec_fp = open(FEEDER_EXECS, "r", encoding="utf-8")
                self.exec_fp.seek(0, 2)
            except Exception as exc:
                _log(f"exec stream: {exc}")

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

    def _drain_private_ws_fills(self) -> int:
        """Apply PRIVATE_WS EXECUTION for working/ghost OIDs (P0-3 fast path)."""
        if not self.real or self.private_ws is None:
            return 0
        applied = 0
        for ev in self.private_ws.drain():
            oid = str(ev.get("id") or "")
            side = str(ev.get("side") or "").upper()
            try:
                px = float(ev.get("px") or 0.0)
            except Exception:
                px = 0.0
            if not oid or side not in ("BUY", "SELL") or px <= 0:
                continue
            if oid in self.claimed_oids:
                _log(f"[{self.LANE}] CLAIM_DROP PRIVATE_WS OID {oid}")
                continue
            tracked = False
            if self.working_id and str(self.working_id) == oid:
                tracked = True
            elif any(str(g.get("id") or "") == oid for g in self.ghosts):
                tracked = True
            elif any(str(rec.get("id") or "") == oid for rec in self.recent_posts):
                # Cancel/pull race: working cleared but OID is ours → Late Fill ADOPT.
                tracked = True
            if not tracked:
                # Foreign stream event — seal so it cannot invent inventory later.
                self._terminate_oid(oid, "private_ws_untracked")
                continue
            # Prefer private REST truth when available (avg px / COMPLETED).
            if any(str(rec.get("id") or "") == oid for rec in self.recent_posts) and (
                not self.working_id or str(self.working_id) != oid
            ):
                if self._try_adopt_oid(side=side, px_hint=px, oid=oid):
                    self.private_ws_fills += 1
                    applied += 1
                    self._reset_ghost_mismatch()
                continue
            if not self._claim_committed_oid(oid):
                continue
            self.ghosts = [g for g in self.ghosts if str(g.get("id")) != oid]
            if self.working_id == oid:
                pass
            elif not self.working_side:
                self.working_side = side
                self.working_px = px
                self.working_id = oid
            mid = float(self._feeder_mid() or 0.0)
            _log(
                f"[{self.LANE}] PRIVATE_WS_FILL {side} @ {px:,.0f} id={oid}"
            )
            _append(
                AUDIT_PATH,
                {
                    "event": "PRIVATE_WS_FILL",
                    "ts": _iso(),
                    "side": side,
                    "px": px,
                    "id": oid,
                    "size": ev.get("size"),
                    "mid": mid,
                },
            )
            self._apply_fill(side, px, "PRIVATE_WS", arm_tp=False)
            self.private_ws_fills += 1
            toxic = self._maybe_toxic_entry_cleanup(
                side=side, fill_px=px, oid=oid, mid=mid, src="PRIVATE_WS"
            )
            if not toxic and self.net != 0:
                self._arm_tp_maker_rest()
            self._reset_ghost_mismatch()
            applied += 1
        return applied

    def _depth(self) -> Tuple[List[Dict[str, float]], List[Dict[str, float]]]:
        if not FEEDER_LATEST.exists():
            return [], []
        try:
            raw = json.loads(FEEDER_LATEST.read_text(encoding="utf-8"))
            return raw.get("bids", [])[:10], raw.get("asks", [])[:10]
        except Exception:
            return [], []

    def _hit_touch(self, why: str) -> bool:
        """True only for HARD paths that still need touch pricing fallback.

        FLATTEN_SOFT (ev_gone / adverse_fast) must NOT hit-touch — that was the
        spread-friction bug (LIMIT skin, cross price).
        """
        return classify_flatten_mode(why) == FLATTEN_HARD

    def _is_adverse_exit(self, why: str) -> bool:
        """Toxic / hard path — escalate to FLATTEN_HARD (MARKET), not soft rest."""
        return classify_flatten_mode(why) == FLATTEN_HARD

    def _is_market_flat_exit(self, why: str) -> bool:
        """FLATTEN_HARD only: adverse_cut / toxic_adopt|fill / hard_stop → MARKET."""
        return classify_flatten_mode(why) == FLATTEN_HARD

    def _advance_flatten(self, why: str) -> str:
        """Bump irreversible flatten_state from why; return new state."""
        cur = str(getattr(self, "flatten_state", FLATTEN_HOLD) or FLATTEN_HOLD)
        nxt = advance_flatten_state(cur, why)
        # Arm Soft adverse counters when first entering Soft from HOLD.
        if cur == FLATTEN_HOLD and nxt == FLATTEN_SOFT:
            self._soft_reprice_n = 0
            self._soft_exit_armed_ts = time.time()
        if nxt == FLATTEN_HARD or nxt == FLATTEN_HOLD:
            if nxt == FLATTEN_HOLD:
                self._soft_reprice_n = 0
                self._soft_exit_armed_ts = 0.0
        self.flatten_state = nxt
        return nxt

    def _is_adverse_fast_soft(self, why: str = "") -> bool:
        """True when Soft flatten is driven by adverse_fast (dig-down risk)."""
        w = str(why or "")
        p = str(getattr(self, "pending_exit_why", "") or "")
        if "adverse_fast" in w or "adverse_fast" in p:
            return True
        return False

    def _soft_adverse_escalate_reason(
        self, why: str, unrealized_bp: float
    ) -> Optional[str]:
        """If Soft adverse_fast should escalate to HARD, return escalate why."""
        st = str(getattr(self, "flatten_state", FLATTEN_HOLD) or FLATTEN_HOLD)
        if st == FLATTEN_HARD:
            return None
        mode = classify_flatten_mode(why)
        if mode != FLATTEN_SOFT and st != FLATTEN_SOFT:
            return None
        if not self._is_adverse_fast_soft(why):
            return None
        if float(unrealized_bp) <= -float(SOFT_ADVERSE_ESCALATE_BP):
            return (
                f"soft_escalate_slip ({float(unrealized_bp):+.2f}bp"
                f"<=-{SOFT_ADVERSE_ESCALATE_BP:.1f})"
            )
        if int(getattr(self, "_soft_reprice_n", 0) or 0) >= int(SOFT_REPRICE_MAX):
            return (
                f"soft_escalate_reprice (n={int(self._soft_reprice_n)}"
                f">={SOFT_REPRICE_MAX})"
            )
        return None

    def _escalate_soft_to_hard(self, why: str, now: float) -> None:
        """SOFT → HARD: pull resting Soft LIMIT, MARKET flatten + cooldown."""
        self.soft_to_hard_escalations = int(
            getattr(self, "soft_to_hard_escalations", 0) or 0
        ) + 1
        _log(f"[{self.LANE}] SOFT_TO_HARD_ESCALATE ({why})")
        _append(
            AUDIT_PATH,
            {
                "event": "SOFT_TO_HARD_ESCALATE",
                "ts": _iso(),
                "why": why,
                "net_lots": self.net,
                "entry_px": self.entry_px,
                "soft_reprice_n": int(getattr(self, "_soft_reprice_n", 0) or 0),
                "flatten_state": self.flatten_state,
            },
        )
        if self.working_side:
            self._pull(f"soft_escalate:{why}")
            if self.net == 0:
                return
        self.pending_exit_why = why
        self._hard_market_exit(why, now)

    def _emergency_adverse_exit(self, why: str, now: float) -> None:
        """Dispatch Soft LIMIT vs Hard MARKET via flatten mode."""
        self._toxic_maker_exit(why, now)

    def _feeder_bid_ask(self) -> Tuple[float, float]:
        try:
            raw = json.loads(FEEDER_LATEST.read_text(encoding="utf-8"))
            return float(raw.get("best_bid") or 0.0), float(raw.get("best_ask") or 0.0)
        except Exception:
            return 0.0, 0.0

    def _hard_market_exit(self, why: str, now: float) -> None:
        """FLATTEN_HARD: cancel working + MARKET flatten (+45s cooldown)."""
        self._advance_flatten(why)
        w = str(why)
        if w.startswith("adverse_cut"):
            self.adverse_cuts += 1
        self.adverse_market_exits += 1
        _log(f"[{self.LANE}] HARD_MARKET_EXIT ({why})")
        _append(
            AUDIT_PATH,
            {
                "event": "HARD_MARKET_EXIT",
                "ts": _iso(),
                "why": why,
                "net_lots": self.net,
                "entry_px": self.entry_px,
                "flatten_state": self.flatten_state,
            },
        )
        if self.net == 0:
            return
        side = "SELL" if self.net > 0 else "BUY"
        if self.working_side:
            self._pull(f"hard_replace:{why}")
            if self.net == 0:
                return
        self.pending_exit_why = why
        bid, ask = self._feeder_bid_ask()
        mark_px = self._exit_px(side, bid, ask, why)
        if mark_px <= 0:
            mark_px = float(bid if side == "SELL" else ask) or float(self.entry_px or 0.0)
        fill_px = mark_px
        if self.real:
            try:
                res = self._send_venue_market(side, self.size)
                oid = str((res or {}).get("child_order_acceptance_id") or "")
                if oid:
                    try:
                        got = self.client.get_execution_price_by_acceptance_id(
                            PRODUCT, oid, max_retries=8, retry_interval_sec=0.35
                        )
                        if got and float(got) > 0:
                            fill_px = float(got)
                    except Exception:
                        pass
                    self._claim_committed_oid(oid)
            except Exception as exc:
                self.api_errors += 1
                _log(f"[{self.LANE}] HARD_MARKET_EXIT fail: {exc}")
                # Fallback: aggressive LIMIT at touch (still HARD pricing).
                if mark_px > 0 and not self.working_side:
                    self._post(side, mark_px, f"EXIT {why}")
                self._adverse_cooldown_until = float(now) + ADVERSE_REENTRY_COOLDOWN_SEC
                return
        if self.net != 0 and fill_px > 0:
            self._apply_fill(side, float(fill_px), "HARD_MARKET")
        self._adverse_cooldown_until = float(now) + ADVERSE_REENTRY_COOLDOWN_SEC
        _log(
            f"[{self.LANE}] ADVERSE_COOLDOWN {ADVERSE_REENTRY_COOLDOWN_SEC:.0f}s "
            f"(no new ENTER)"
        )

    def _soft_limit_exit(self, why: str, now: float) -> None:
        """FLATTEN_SOFT: passive opposite maker LIMIT (never cross / never MARKET)."""
        self._advance_flatten(why)
        _log(f"[{self.LANE}] SOFT_LIMIT_EXIT ({why})")
        _append(
            AUDIT_PATH,
            {
                "event": "SOFT_LIMIT_EXIT",
                "ts": _iso(),
                "why": why,
                "net_lots": self.net,
                "entry_px": self.entry_px,
                "flatten_state": self.flatten_state,
            },
        )
        if self.net == 0:
            return
        side = "SELL" if self.net > 0 else "BUY"
        bid, ask = self._feeder_bid_ask()
        # Slip guard even on first Soft post (board already ran away).
        mid = 0.5 * (bid + ask) if bid > 0 and ask > 0 else 0.0
        unreal = 0.0
        if self.entry_px > 0 and mid > 0:
            if self.net > 0:
                unreal = ((mid / self.entry_px) - 1.0) * 1e4
            else:
                unreal = ((self.entry_px / mid) - 1.0) * 1e4
        esc = self._soft_adverse_escalate_reason(why, unreal)
        if esc:
            self._escalate_soft_to_hard(esc, now)
            return
        px = self._exit_px(side, bid, ask, why)
        if px <= 0:
            # Soft must stay passive — never fall back to cross touch.
            px = soft_maker_exit_px(side, bid, ask)
        if px <= 0:
            return
        if self.working_side and self.working_side != side:
            self._pull(f"soft_replace:{why}")
        elif self.working_side == side:
            want = float(px)
            if abs(float(self.working_px) - want) > 1e-9:
                if self._is_adverse_fast_soft(why):
                    self._soft_reprice_n = int(self._soft_reprice_n) + 1
                    esc = self._soft_adverse_escalate_reason(why, unreal)
                    if esc:
                        self._escalate_soft_to_hard(esc, now)
                        return
                self._pull(f"soft_reprice:{why}")
        self.pending_exit_why = why
        if not self.working_side:
            self._post(side, px, f"EXIT {why}")

    def _toxic_maker_exit(self, why: str, now: float) -> None:
        """Compat entry: Soft → passive LIMIT, Hard → MARKET."""
        mode = self._advance_flatten(why)
        if mode == FLATTEN_HARD:
            self._hard_market_exit(why, now)
            return
        self._soft_limit_exit(why, now)

    def _should_block_entry_for_spread(self, bid: float, ask: float) -> bool:
        """Spread Gate: block ENTER when book is narrower than MIN_ENTRY_SPREAD_BP."""
        ok, sp = entry_spread_allows(bid, ask)
        if ok:
            return False
        self.spread_gate_blocks = int(
            getattr(self, "spread_gate_blocks", 0) or 0
        ) + 1
        _log(
            f"[{self.LANE}] SPREAD_TOO_NARROW "
            f"(curr={sp:.2f} < min={float(MIN_ENTRY_SPREAD_BP):.1f})"
        )
        _append(
            AUDIT_PATH,
            {
                "event": "SPREAD_TOO_NARROW",
                "ts": _iso(),
                "spread_bp": round(sp, 4),
                "min_bp": float(MIN_ENTRY_SPREAD_BP),
                "bid": float(bid),
                "ask": float(ask),
            },
        )
        return True

    def _in_tp_grace(self, now: Optional[float] = None) -> bool:
        """True within TP_GRACE_SEC after entry while inventory is open."""
        if int(self.net) == 0:
            return False
        et = float(getattr(self, "entry_ts", 0.0) or 0.0)
        if et <= 0:
            return False
        t = float(now if now is not None else time.time())
        return (t - et) < float(TP_GRACE_SEC)

    def _should_defer_ev_gone(self, why: str, now: Optional[float] = None) -> bool:
        """Healthy TP grace: defer Soft ev_gone; never defer adverse/HARD."""
        w = str(why or "").strip().lower()
        if not w:
            return False
        if (
            "adverse_fast" in w
            or "adverse_cut" in w
            or "toxic_adopt" in w
            or "toxic_fill" in w
            or "hard_stop" in w
            or "soft_escalate" in w
        ):
            return False
        if not (w.startswith("ev_gone") or "ev_gone" in w):
            return False
        return self._in_tp_grace(now)

    def _maybe_defer_soft_exit(
        self, why: str, now: float, bid: float, ask: float
    ) -> bool:
        """If TP grace applies, keep/rest ev_hold TP and skip Soft advance."""
        if not self._should_defer_ev_gone(why, now):
            return False
        age = now - float(getattr(self, "entry_ts", 0.0) or now)
        _log(
            f"[{self.LANE}] TP_GRACE defer {why} age={age:.1f}s/{TP_GRACE_SEC:.0f}s "
            f"— keep TP Maker (ev_hold)"
        )
        _append(
            AUDIT_PATH,
            {
                "event": "TP_GRACE_DEFER",
                "ts": _iso(),
                "why": why,
                "age_sec": round(age, 3),
                "grace_sec": float(TP_GRACE_SEC),
                "net": int(self.net),
            },
        )
        # Stay HOLD; do not promote flatten_state on deferred ev_gone.
        self.flatten_state = FLATTEN_HOLD
        side = "SELL" if self.net > 0 else "BUY"
        want = tp_harvest_exit_px(side, bid, ask, float(self.entry_px or 0.0))
        if not self.working_side:
            self._arm_tp_maker_rest()
            return True
        # Already quoting — keep / reprice to clamped harvest TP if drifted.
        if (
            want > 0
            and str(self.working_side).upper() == side
            and abs(float(self.working_px) - float(want)) > 1e-9
        ):
            if self._pull("tp_grace_reprice"):
                return True
            self._arm_tp_maker_rest()
        elif not str(getattr(self, "pending_exit_why", "") or "").startswith("ev_hold"):
            self.pending_exit_why = "ev_hold"
        return True

    def _arm_tp_maker_rest(self) -> None:
        """After ENTRY fill: rest opposite maker TP with min-harvest clamp."""
        if self.net == 0 or self.halt_trading:
            return
        if self.working_side:
            return
        side = "SELL" if self.net > 0 else "BUY"
        bid, ask = self._feeder_bid_ask()
        # Harvest TP = best opposite front, floored/ceiled to ≥ +MIN_HARVEST_BP.
        px = tp_harvest_exit_px(side, bid, ask, float(self.entry_px or 0.0))
        if px <= 0:
            px = soft_maker_exit_px(side, bid, ask)
        if px <= 0:
            px = exit_limit_px(side, bid, ask)
        if px <= 0:
            return
        self.flatten_state = FLATTEN_HOLD
        self.pending_exit_why = "ev_hold"
        self._post(side, px, "EXIT ev_hold")

    def _exit_px(self, side: str, bid: float, ask: float, why: str) -> float:
        mode = classify_flatten_mode(why)
        if mode == FLATTEN_SOFT:
            # After TP_GRACE / Soft path: plain best-opposite (no harvest clamp).
            return soft_maker_exit_px(side, bid, ask)
        if mode == FLATTEN_HARD:
            # Pricing fallback if MARKET path falls back to LIMIT; still touch.
            if side == "SELL" and bid > 0:
                return float(bid)
            if side == "BUY" and ask > 0:
                return float(ask)
            return 0.0
        # HOLD / ev_hold TP: min-harvest clamp vs entry.
        return tp_harvest_exit_px(side, bid, ask, float(self.entry_px or 0.0))

    def _force_src(self, why: str) -> str:
        if str(why).startswith("hard_stop"):
            return "HIT_BID_FORCE"
        return "HIT_TOUCH_FORCE"

    # --- venue I/O ---
    def _post(self, side: str, px: float, why: str) -> None:
        if self.halt_trading:
            return
        # Do not free a new slot while optimistic ghosts await venue truth.
        if self.real and self.ghosts:
            return
        why_u = str(why).upper()
        is_exit = why_u.startswith("EXIT") or "EXIT " in why_u
        # Inventory-facing guards (REAL double-exit root cause: EXIT SELL while already flat).
        if is_exit:
            if side == "SELL" and self.net <= 0:
                _log(f"[{self.LANE}] SKIP_POST EXIT SELL net={self.net} ({why})")
                return
            if side == "BUY" and self.net >= 0:
                _log(f"[{self.LANE}] SKIP_POST EXIT BUY net={self.net} ({why})")
                return
        elif why_u.startswith("ENTER"):
            if self.net != 0:
                _log(f"[{self.LANE}] SKIP_POST ENTER net={self.net} ({why})")
                return
        if abs(self.net) * self.size >= self.max_inv - 1e-12 and (
            (side == "BUY" and self.net >= 0) or (side == "SELL" and self.net <= 0)
        ):
            return
        px_i = int(round(px))
        if px_i <= 0:
            return
        oid = ""
        try:
            if self.real:
                res = self.client.send_order(
                    PRODUCT, side, self.size, "LIMIT", price=px_i
                )
                oid = str((res or {}).get("child_order_acceptance_id") or "")
                if not oid:
                    self.rejects += 1
                    _append(
                        AUDIT_PATH,
                        {"event": "ORDER_REJECT", "ts": _iso(), "data": res, "why": why},
                    )
                    _log(f"[{self.LANE}] REJECT {side} @ {px_i} ({why})")
                    return
            else:
                oid = f"SIM-{side}-{int(time.time() * 1000)}"
        except OrderSubmitUnknownError as exc:
            self.api_errors += 1
            _append(AUDIT_PATH, {"event": "ORDER_UNKNOWN", "ts": _iso(), "err": str(exc)})
            _log(f"[{self.LANE}] ORDER_UNKNOWN {exc}")
            return
        except Exception as exc:
            self.api_errors += 1
            self.rejects += 1
            _append(AUDIT_PATH, {"event": "ORDER_ERROR", "ts": _iso(), "err": str(exc)})
            _log(f"[{self.LANE}] ORDER_ERROR {exc}")
            return

        self.working_side = side
        self.working_px = float(px_i)
        self.working_id = oid
        self.working_posted_at = time.time()
        self.posts += 1
        self.recent_posts.append(
            {
                "id": oid,
                "side": side,
                "px": float(px_i),
                "ts": time.time(),
                "why": why,
            }
        )
        if len(self.recent_posts) > 40:
            self.recent_posts = self.recent_posts[-40:]
        _append(
            AUDIT_PATH,
            {
                "event": "ORDER_POSTED",
                "ts": _iso(),
                "side": side,
                "px": px_i,
                "id": oid,
                "why": why,
                "real": self.real,
            },
        )
        mode = "REAL" if self.real else "DRY"
        _log(f"[{self.LANE}] POST[{mode}] {side} @ {px_i:,.0f} ({why}) id={oid}")

    def _clear_working(self) -> None:
        self.working_side = None
        self.working_px = 0.0
        self.working_id = ""
        self.working_posted_at = 0.0

    def _tracked_oids(self) -> set:
        ids = set()
        if self.working_id:
            ids.add(str(self.working_id))
        for g in self.ghosts:
            oid = str(g.get("id") or "")
            if oid:
                ids.add(oid)
        return ids

    def _request_cancel(self, oid: str, why: str) -> None:
        if not (self.real and oid and not str(oid).startswith("SIM-")):
            return
        try:
            self.client.cancel_child_order(PRODUCT, child_order_acceptance_id=oid)
        except Exception as exc:
            _log(f"[{self.LANE}] cancel err: {exc}")
            self.api_errors += 1

    def _pull(self, why: str) -> bool:
        """Cancel working. If REAL and already filled, adopt first.

        Returns True when a private fill was adopted (caller must not re-POST exit).
        """
        if not self.working_side:
            return False
        oid = str(self.working_id or "")
        side = str(self.working_side)
        px_hint = float(self.working_px or 0.0)
        adopted = False
        # Fill-before-cancel race: check private COMPLETED before / after cancel.
        if self.real and oid and oid not in self.adopted_oids:
            if self._try_adopt_oid(side=side, px_hint=px_hint, oid=oid):
                adopted = True
        if not adopted:
            self._request_cancel(oid, why)
            _append(
                AUDIT_PATH,
                {
                    "event": "CANCEL",
                    "ts": _iso(),
                    "side": side,
                    "id": oid,
                    "where": why,
                    "real": self.real,
                },
            )
            _log(f"[{self.LANE}] CANCEL {side} {oid} ({why})")
            self.cancels += 1
            # Cancel may lose a race to fill — recheck once.
            if self.real and oid and oid not in self.adopted_oids:
                if self._try_adopt_oid(side=side, px_hint=px_hint, oid=oid):
                    adopted = True
                    _log(f"[{self.LANE}] ADOPT_AFTER_CANCEL {side} {oid} ({why})")
        if not adopted:
            self._clear_working()
        return adopted

    def _park_optimistic(self, *, side: str, px: float, oid: str, src: str) -> None:
        """Board-imitation predict: cancel venue, no inventory commit, hold ghost."""
        self.predict_fills += 1
        self._request_cancel(oid, f"PREDICTED_FILL_{src}")
        ghost = {
            "side": side,
            "px": float(px),
            "id": oid,
            "src": src,
            "ts": time.time(),
            "pending_exit_why": self.pending_exit_why,
        }
        self.ghosts = [g for g in self.ghosts if str(g.get("id")) != str(oid)]
        self.ghosts.append(ghost)
        _append(
            AUDIT_PATH,
            {
                "event": "PREDICT_FILL",
                "ts": _iso(),
                "side": side,
                "px": px,
                "src": src,
                "id": oid,
                "real": self.real,
            },
        )
        _log(
            f"[{self.LANE}] PREDICT[{src}] {side} @ {px:,.0f} id={oid} — cancel+ghost, no inventory"
        )
        self._clear_working()
        self.cancels += 1

    def _send_venue_market(self, side: str, size: float) -> Dict[str, Any]:
        """REAL MARKET reduce/cover. size must be >= venue min unless caller knows."""
        size = round(float(size), 8)
        if size <= EXACT_FLAT_EPS_BTC:
            return {"status": "SKIP", "reason": "size_zero"}
        return self.client.send_order(PRODUCT, side, size, "MARKET")

    def _poll_venue_net(self, tries: int = FLATTEN_CONFIRM_TRIES) -> float:
        vnet = 0.0
        for _ in range(max(1, int(tries))):
            time.sleep(FLATTEN_CONFIRM_SLEEP_SEC)
            try:
                positions = self.client.get_positions(PRODUCT) or []
                vnet = parse_venue_net(positions)
            except Exception as exc:
                self.api_errors += 1
                _log(f"[{self.LANE}] poll positions err: {exc}")
                continue
            if abs(vnet) < EXACT_FLAT_EPS_BTC:
                return 0.0
        return float(vnet)

    def _neutralize_venue_dust(
        self, vnet: float, detail: Optional[Dict[str, Any]] = None
    ) -> float:
        """Clear sub-min-lot venue dust via SIZE+|dust| overshoot then unwind.

        bitFlyer rejects orders < 0.001, so isolated dust cannot be closed directly.
        """
        actions = detail.setdefault("actions", []) if detail is not None else []
        legs = dust_neutralize_legs(vnet, lot=float(self.size) or SIZE_BTC)
        if not legs:
            return float(vnet)
        _log(
            f"[{self.LANE}] DUST_NEUTRALIZE net={vnet:+.8f} "
            f"legs={legs[0][0]} {legs[0][1]} → {legs[1][0]} {legs[1][1]}"
        )
        actions.append({"dust_neutralize": {"net": vnet, "legs": legs}})
        for side, sz in legs:
            try:
                res = self._send_venue_market(side, sz)
                actions.append({"dust_leg": {"side": side, "size": sz, "res": res}})
                oid = str((res or {}).get("child_order_acceptance_id") or "")
                if oid:
                    try:
                        self.client.get_execution_price_by_acceptance_id(
                            PRODUCT, oid, max_retries=8, retry_interval_sec=0.35
                        )
                    except Exception:
                        pass
            except Exception as exc:
                actions.append({"dust_leg_fail": {"side": side, "size": sz, "err": str(exc)}})
                _log(f"[{self.LANE}] DUST_NEUTRALIZE fail {side} {sz}: {exc}")
                break
            vnet = self._poll_venue_net()
        self._last_dust_sweep_ts = time.time()
        return float(vnet)

    def _reduce_venue_to_flat(self, detail: Dict[str, Any]) -> float:
        """Cancel → reduce tradeable lots → neutralize leftover dust → return venue_net."""
        actions = detail.setdefault("actions", [])
        try:
            positions = self.client.get_positions(PRODUCT) or []
            vnet = parse_venue_net(positions)
        except Exception as exc:
            actions.append(f"positions_err:{exc}")
            return 0.0
        detail["venue_net"] = vnet
        lot = float(self.size) or SIZE_BTC

        # 1) Tradeable inventory (>= min lot): MARKET reduce in lot-sized chunks.
        for red_try in range(1, 5):
            if abs(vnet) < EXACT_FLAT_EPS_BTC or is_isolated_dust(vnet, lot):
                break
            if abs(vnet) + 1e-12 < lot:
                break
            side = "SELL" if vnet > 0 else "BUY"
            sz = round(min(abs(vnet), lot), 8)
            try:
                res = self._send_venue_market(side, sz)
                actions.append(
                    {"reduce_market": res, "side": side, "size": sz, "try": red_try}
                )
            except Exception as exc:
                actions.append(f"reduce_market_fail:{exc}")
                _log(f"[{self.LANE}] flatten MARKET reduce fail: {exc}")
                break
            vnet = self._poll_venue_net()
            actions.append({"venue_net_after_reduce": vnet, "try": red_try})

        # 2) Isolated dust (< min lot): overshoot cover then unwind to exact 0.
        if is_isolated_dust(vnet, lot):
            vnet = self._neutralize_venue_dust(vnet, detail)
            actions.append({"venue_net_after_dust": vnet})

        return float(vnet)

    def flatten(self, reason: str = "") -> Dict[str, Any]:
        detail: Dict[str, Any] = {
            "reason": reason,
            "ts": _iso(),
            "actions": [],
            "real": self.real,
            "flat_ok": False,
            "net_before": self.net,
        }
        _log(f"[{self.LANE}] FLATTEN start ({reason})")
        # Seal OIDs before pull/cancel so fill-before-cancel cannot re-ADOPT later.
        self._terminate_inflight_oids(f"flatten:{reason}")
        self._pull(f"flatten:{reason}")
        # Cancel any optimistic ghosts still tracked.
        for g in list(self.ghosts):
            self._request_cancel(str(g.get("id") or ""), f"flatten_ghost:{reason}")
        self.ghosts = []
        if self.real:
            for attempt in range(1, 6):
                try:
                    self.client.cancel_all_child_orders(PRODUCT)
                    detail["actions"].append(f"cancel_all_ok_{attempt}")
                    break
                except Exception as exc:
                    detail["actions"].append(f"cancel_all_fail_{attempt}:{exc}")
                    time.sleep(0.3)
            try:
                vnet2 = self._reduce_venue_to_flat(detail)
                # One more dust pass if still leftover after reduce races.
                lot = float(self.size) or SIZE_BTC
                if is_isolated_dust(vnet2, lot):
                    vnet2 = self._neutralize_venue_dust(vnet2, detail)
                detail["venue_net_after"] = vnet2
                detail["flat_ok"] = abs(vnet2) < EXACT_FLAT_EPS_BTC
                if detail["flat_ok"] and self.net != 0:
                    # Align local ledger to flat venue after emergency reduce.
                    try:
                        raw = json.loads(FEEDER_LATEST.read_text(encoding="utf-8"))
                        bid = float(raw.get("best_bid") or 0.0)
                        ask = float(raw.get("best_ask") or 0.0)
                    except Exception:
                        bid = ask = self.entry_px
                    exit_px = bid if self.net > 0 else ask
                    if exit_px <= 0:
                        exit_px = self.entry_px or 1.0
                    self._apply_fill(
                        "SELL" if self.net > 0 else "BUY",
                        exit_px,
                        "FLATTEN_VENUE",
                    )
                elif not detail["flat_ok"]:
                    _log(
                        f"[{self.LANE}] FLATTEN leftover venue_net={vnet2:+.8f} "
                        f"(could not reach exact flat)"
                    )
            except Exception as exc:
                detail["actions"].append(f"positions_err:{exc}")
        else:
            # DRY: book ledger close at mid/touch if inventory open.
            if self.net != 0 and self.entry_px > 0:
                try:
                    raw = json.loads(FEEDER_LATEST.read_text(encoding="utf-8"))
                    bid = float(raw.get("best_bid") or 0.0)
                    ask = float(raw.get("best_ask") or 0.0)
                except Exception:
                    bid = ask = self.entry_px
                exit_px = bid if self.net > 0 else ask
                if exit_px <= 0:
                    exit_px = self.entry_px
                self._apply_fill(
                    "SELL" if self.net > 0 else "BUY",
                    exit_px,
                    "FLATTEN_DRY",
                )
            detail["flat_ok"] = self.net == 0
            detail["net_after"] = self.net
        self.pending_exit_why = ""
        _append(AUDIT_PATH, {"event": "FLATTEN", "ts": _iso(), "data": detail})
        _write_json(FLATTEN_LAST, detail)
        _log(f"[{self.LANE}] FLATTEN done flat_ok={detail.get('flat_ok')}")
        return detail

    # --- fills / ledger ---
    def _maybe_fill(self, execs: List[Dict[str, Any]], bid: float, ask: float) -> None:
        """Two-phase fill.

        REAL: PUBLIC_OID commits inventory. BOOK_CROSS/BOARD_TRADE = predict only
        (cancel + ghost). DRY: board imitation still commits (paper).
        """
        self._confirm_ghosts(execs or [])
        if not self.working_side or self.working_px <= 0:
            return
        oid = self.working_id if self.real else ""
        # Phase A: OID-only (always).
        hit = public_maker_fill(
            side=self.working_side,
            price=self.working_px,
            acceptance_id=oid,
            public_execs=execs or [],
            best_bid=bid,
            best_ask=ask,
            allow_book=False,
        )
        if hit and hit[0] == "PUBLIC_OID":
            if not self._claim_committed_oid(str(oid) if oid else ""):
                self._clear_working()
                return
            self._apply_fill(self.working_side, float(self.working_px), "PUBLIC_OID")
            return
        # Phase B: board imitation.
        allow_book = True
        if self.real and not book_cross_guard_ok(self.working_posted_at):
            allow_book = False
        if not allow_book:
            return
        hit = public_maker_fill(
            side=self.working_side,
            price=self.working_px,
            acceptance_id=oid,
            public_execs=execs or [],
            best_bid=bid,
            best_ask=ask,
            allow_book=True,
        )
        if not hit:
            return
        src, _sz = hit
        if src == "PUBLIC_OID":
            if not self._claim_committed_oid(str(oid) if oid else ""):
                self._clear_working()
                return
            self._apply_fill(self.working_side, float(self.working_px), src)
            return
        if src in OPTIMISTIC_FILL_SOURCES:
            if self.real:
                self._park_optimistic(
                    side=str(self.working_side),
                    px=float(self.working_px),
                    oid=str(self.working_id),
                    src=src,
                )
                return
            # DRY paper: board imitation commits.
            self._apply_fill(self.working_side, float(self.working_px), src)

    @property
    def claimed_oids(self) -> set:
        """OrderLedger: OIDs that must never again move local inventory."""
        return self.adopted_oids

    def _claim_committed_oid(self, oid: str) -> bool:
        """Claim OID for a single inventory apply.

        Returns False if already claimed (caller must NOT apply again).
        Empty/SIM ids are not tracked and always return True.
        """
        if not oid or str(oid).startswith("SIM-"):
            return True
        oid = str(oid)
        if oid in self.claimed_oids:
            return False
        self.claimed_oids.add(oid)
        return True

    def _terminate_oid(self, oid: str, reason: str = "") -> None:
        """Seal OID as terminal — later COMPLETED/ADOPT must be dropped (no inventory)."""
        if not oid or str(oid).startswith("SIM-"):
            return
        oid = str(oid)
        fresh = oid not in self.claimed_oids
        self.claimed_oids.add(oid)
        if fresh:
            _log(f"[{self.LANE}] OID_TERMINATED id={oid} ({reason})")
            _append(
                AUDIT_PATH,
                {"event": "OID_TERMINATED", "ts": _iso(), "id": oid, "reason": reason},
            )

    def _terminate_inflight_oids(self, reason: str) -> None:
        """Seal working/ghost/recent POST OIDs so late COMPLETED cannot re-ADOPT."""
        if self.working_id:
            self._terminate_oid(str(self.working_id), reason)
        for g in list(self.ghosts):
            self._terminate_oid(str(g.get("id") or ""), reason)
        for rec in list(self.recent_posts):
            self._terminate_oid(str(rec.get("id") or ""), reason)

    def _confirm_ghosts(self, execs: List[Dict[str, Any]]) -> None:
        if not self.ghosts:
            return
        kept: List[Dict[str, Any]] = []
        for g in self.ghosts:
            oid = str(g.get("id") or "")
            side = str(g.get("side") or "")
            px = float(g.get("px") or 0.0)
            hit = public_maker_fill(
                side=side,
                price=px,
                acceptance_id=oid,
                public_execs=execs,
                best_bid=0.0,
                best_ask=0.0,
                allow_book=False,
            )
            if hit and hit[0] == "PUBLIC_OID":
                # Venue filled before/while cancel — commit once.
                # Must claim OID here: working-path PUBLIC_OID already did, but
                # ghost confirm previously skipped adopted_oids → later
                # PRIVATE_RECONCILE_ADOPT double-applied the same fill →
                # INVENTORY_MISMATCH halt.
                if not self._claim_committed_oid(oid):
                    _log(
                        f"[{self.LANE}] GHOST_CONFIRM skip already-committed "
                        f"{side} id={oid}"
                    )
                    continue
                if g.get("pending_exit_why"):
                    self.pending_exit_why = str(g.get("pending_exit_why") or "")
                self._apply_fill(side, px, "PUBLIC_OID")
                _log(f"[{self.LANE}] GHOST_CONFIRM PUBLIC_OID {side} id={oid}")
                continue
            # Drop stale ghosts after 30s; reconcile will catch orphans.
            if time.time() - float(g.get("ts") or 0.0) > 30.0:
                _append(
                    AUDIT_PATH,
                    {"event": "PREDICT_MISS", "ts": _iso(), "id": oid, "side": side, "px": px},
                )
                _log(f"[{self.LANE}] PREDICT_MISS drop ghost id={oid}")
                continue
            kept.append(g)
        self.ghosts = kept

    def _private_fill_info(self, oid: str) -> Optional[Tuple[float, float, str]]:
        """Private venue truth for an OID.

        Returns (executed_size, avg_px, state) when any fill exists, else None.
        """
        if not oid or str(oid).startswith("SIM-"):
            return None
        for state in ("COMPLETED", "ACTIVE", "CANCELED"):
            try:
                rows = (
                    self.client.get_child_orders(
                        PRODUCT,
                        child_order_state=state,
                        child_order_acceptance_id=oid,
                    )
                    or []
                )
            except Exception as exc:
                self.api_errors += 1
                _log(f"[{self.LANE}] private child_orders err: {exc}")
                continue
            for co in rows:
                try:
                    exec_sz = float(co.get("executed_size") or 0.0)
                except Exception:
                    exec_sz = 0.0
                st = str(co.get("child_order_state") or state)
                if exec_sz > 1e-12 or st == "COMPLETED":
                    try:
                        px = float(co.get("average_price") or co.get("price") or 0.0)
                    except Exception:
                        px = 0.0
                    if px <= 0:
                        try:
                            px = float(
                                self.client.get_execution_price_by_acceptance_id(
                                    PRODUCT, oid, max_retries=2, retry_interval_sec=0.2
                                )
                                or 0.0
                            )
                        except Exception:
                            px = 0.0
                    return exec_sz if exec_sz > 0 else self.size, px, st
        return None

    def _try_adopt_oid(self, *, side: str, px_hint: float, oid: str) -> bool:
        """Commit local inventory from private COMPLETED/partial fill (working/ghost only)."""
        if not oid:
            return False
        if oid in self.claimed_oids:
            _log(f"[{self.LANE}] CLAIM_DROP OID {oid} already processed — ignore adopt")
            return False
        info = self._private_fill_info(oid)
        if not info:
            return False
        exec_sz, avg_px, st = info
        if exec_sz <= 1e-12:
            return False
        px = float(avg_px) if avg_px > 0 else float(px_hint)
        if px <= 0:
            return False
        # Claim before apply so PUBLIC_OID / concurrent adopt cannot double-count.
        if not self._claim_committed_oid(str(oid)):
            _log(f"[{self.LANE}] CLAIM_DROP OID {oid} race — ignore adopt")
            return False
        # If this OID was parked as ghost, drop it before apply.
        self.ghosts = [g for g in self.ghosts if str(g.get("id")) != str(oid)]
        if self.working_id == oid:
            pass
        elif not self.working_side:
            self.working_side = side
            self.working_px = px
            self.working_id = oid
        mid = float(self._feeder_mid() or 0.0)
        adv_at_adopt = self._adverse_bp_vs_mid(side, px, mid) if mid > 0 else 0.0
        _log(
            f"[{self.LANE}] ADOPT_FILL {side} @ {px:,.0f} id={oid} "
            f"exec_sz={exec_sz} state={st} mid={mid:,.0f} "
            f"adv={adv_at_adopt:+.2f}bp src=PRIVATE_RECONCILE_ADOPT"
        )
        _append(
            AUDIT_PATH,
            {
                "event": "ADOPT_FILL",
                "ts": _iso(),
                "side": side,
                "px": px,
                "id": oid,
                "exec_sz": exec_sz,
                "state": st,
                "mid": mid,
                "adverse_bp": round(adv_at_adopt, 4),
                "src": "PRIVATE_RECONCILE_ADOPT",
            },
        )
        # Venue already filled — apply local inventory, then same-stack toxic gate.
        # Exit adopts that flatten net skip the gate (net==0 after apply).
        # Defer TP arm until after toxic HARD so we do not post Soft then MARKET.
        self._apply_fill(side, px, "PRIVATE_RECONCILE_ADOPT", arm_tp=False)
        self.adopt_fills += 1
        toxic = self._maybe_toxic_adopt_cleanup(
            side=side, fill_px=px, oid=str(oid), mid=mid
        )
        if not toxic and self.net != 0:
            self._arm_tp_maker_rest()
        return True

    def _try_adopt_inflight(self) -> bool:
        """Adopt working + optimistic-ghost OIDs (claimed OIDs are skipped)."""
        adopted = False
        if self.working_side and self.working_id:
            if self._try_adopt_oid(
                side=str(self.working_side),
                px_hint=float(self.working_px),
                oid=str(self.working_id),
            ):
                adopted = True
        for g in list(self.ghosts):
            if self._try_adopt_oid(
                side=str(g.get("side") or ""),
                px_hint=float(g.get("px") or 0.0),
                oid=str(g.get("id") or ""),
            ):
                adopted = True
        return adopted

    def _try_adopt_recent_posts(
        self, venue_net: Optional[float] = None
    ) -> bool:
        """Late-Fill ADOPT: cancelled/pulled OIDs in recent_posts that COMPLETED.

        Cancel↔fill races clear working before EXECUTION/COMPLETED is visible.
        Venue Truth for *our* OIDs must still enter the SM (not GHOST).
        """
        adopted = False
        target_lots: Optional[int] = None
        if venue_net is not None:
            target_lots = self._venue_to_lots(float(venue_net))
        # Newest first — race fills are recent.
        for rec in reversed(list(self.recent_posts)):
            oid = str(rec.get("id") or "")
            side = str(rec.get("side") or "").upper()
            if not oid or side not in ("BUY", "SELL"):
                continue
            if oid in self.claimed_oids:
                continue
            # While flat, only adopt a fill that explains venue residue direction.
            if int(self.net) == 0 and target_lots is not None and target_lots != 0:
                if target_lots < 0 and side != "SELL":
                    continue
                if target_lots > 0 and side != "BUY":
                    continue
            if self._try_adopt_oid(
                side=side,
                px_hint=float(rec.get("px") or 0.0),
                oid=oid,
            ):
                adopted = True
                _log(
                    f"[{self.LANE}] LATE_FILL_ADOPT {side} id={oid} "
                    f"net={self.net} venue_lots={target_lots}"
                )
                if venue_net is not None:
                    local_btc = float(self.net) * float(self.size)
                    if abs(local_btc - float(venue_net)) <= INV_MISMATCH_BTC:
                        break
        return adopted

    def _venue_to_lots(self, venue_net: float) -> int:
        """Map venue BTC net to strategy lots (-1/0/+1). Isolated dust → 0 lots."""
        lot = float(self.size) or SIZE_BTC
        if abs(float(venue_net)) < EXACT_FLAT_EPS_BTC:
            return 0
        if is_isolated_dust(venue_net, lot):
            return 0
        lots = int(round(float(venue_net) / lot))
        if lots > 1:
            lots = 1
        if lots < -1:
            lots = -1
        return lots

    def _feeder_mid(self) -> float:
        try:
            raw = json.loads(FEEDER_LATEST.read_text(encoding="utf-8"))
            bid = float(raw.get("best_bid") or 0.0)
            ask = float(raw.get("best_ask") or 0.0)
            if bid > 0 and ask > 0:
                return 0.5 * (bid + ask)
            return bid or ask or float(self.entry_px or 0.0)
        except Exception:
            return float(self.entry_px or 0.0)

    @staticmethod
    def _adverse_bp_vs_mid(side: str, fill_px: float, mid: float) -> float:
        """Mark vs fill in bp. Negative = already adverse to the filled side."""
        if fill_px <= 0 or mid <= 0:
            return 0.0
        side_u = str(side or "").upper()
        if side_u == "BUY":
            return ((mid / fill_px) - 1.0) * 1e4
        if side_u == "SELL":
            return ((fill_px / mid) - 1.0) * 1e4
        return 0.0

    def _maybe_toxic_entry_cleanup(
        self,
        *,
        side: str,
        fill_px: float,
        oid: str,
        mid: float,
        src: str,
    ) -> bool:
        """If entry fill opens inventory already past TOXIC_ADOPT_BP → FLATTEN_HARD."""
        if self.net == 0:
            return False
        adv = self._adverse_bp_vs_mid(side, fill_px, mid)
        if adv > -TOXIC_ADOPT_BP:
            return False
        self.toxic_inflight_cleanups += 1
        tag = "toxic_adopt" if "ADOPT" in str(src).upper() else "toxic_fill"
        why = f"{tag} ({adv:+.2f}bp<=-{TOXIC_ADOPT_BP:.1f})"
        _log(
            f"[{self.LANE}] TOXIC_INFLIGHT_CLEANUP {side} @ {fill_px:,.0f} "
            f"mid={mid:,.0f} adv={adv:+.2f}bp id={oid} src={src} — HARD MARKET"
        )
        _append(
            AUDIT_PATH,
            {
                "event": "TOXIC_INFLIGHT_CLEANUP",
                "ts": _iso(),
                "side": side,
                "px": fill_px,
                "mid": mid,
                "adverse_bp": round(adv, 4),
                "threshold_bp": -TOXIC_ADOPT_BP,
                "id": oid,
                "src": src,
                "exit": "MARKET",
            },
        )
        self._toxic_maker_exit(why, time.time())
        return True

    def _maybe_toxic_adopt_cleanup(
        self, *, side: str, fill_px: float, oid: str, mid: float
    ) -> bool:
        """Compat wrapper — ADOPT path."""
        return self._maybe_toxic_entry_cleanup(
            side=side,
            fill_px=fill_px,
            oid=oid,
            mid=mid,
            src="PRIVATE_RECONCILE_ADOPT",
        )

    def _mark_close_settle(self, close_side: str, close_oid: str = "") -> None:
        """Arm settling window after inventory flat from an EXIT fill."""
        self._close_settle_until = time.time() + float(CLOSE_SETTLE_SEC)
        self._close_settle_flat_side = str(close_side or "").upper()
        self._last_close_oid = str(close_oid or "")

    def _in_close_settle(self, now: Optional[float] = None) -> bool:
        t = float(now if now is not None else time.time())
        return t < float(getattr(self, "_close_settle_until", 0.0) or 0.0)

    def _is_late_close_echo(self, side: str) -> bool:
        """Same-side fill while flat in settle window = late/double EXIT echo."""
        if int(self.net) != 0:
            return False
        if not self._in_close_settle():
            return False
        side_u = str(side or "").upper()
        flat_side = str(getattr(self, "_close_settle_flat_side", "") or "").upper()
        return bool(side_u and flat_side and side_u == flat_side)

    def _should_block_force_sync_open(self, venue_net: float) -> bool:
        """Local FLAT + unattributed venue residue → do not Force-Sync-open into SM."""
        if int(self.net) != 0:
            return False
        return self._venue_to_lots(venue_net) != 0

    def _should_debounce_force_sync_open(
        self, venue_net: float, now: Optional[float] = None
    ) -> bool:
        """Compat alias for flat unattributed open block."""
        del now
        return self._should_block_force_sync_open(venue_net)

    def _reset_ghost_mismatch(self) -> None:
        self._ghost_mismatch_hits = 0
        self._ghost_mismatch_since = 0.0

    def _log_ghost_position_blocked(
        self, venue_net: float, *, reason: str, now: float
    ) -> None:
        target = self._venue_to_lots(venue_net)
        _log(
            f"[{self.LANE}] GHOST_POSITION_BLOCKED "
            f"venue={venue_net:+.4f} target_lots={target} "
            f"hits={int(getattr(self, '_ghost_mismatch_hits', 0) or 0)} "
            f"settle={self._in_close_settle(now)} "
            f"settle_side={self._close_settle_flat_side} "
            f"close_oid={self._last_close_oid} reason={reason or 'flat_open_forbid'}"
        )
        _append(
            AUDIT_PATH,
            {
                "event": "GHOST_POSITION_BLOCKED",
                "ts": _iso(),
                "venue_net": venue_net,
                "target_lots": target,
                "hits": int(getattr(self, "_ghost_mismatch_hits", 0) or 0),
                "settle": self._in_close_settle(now),
                "settle_side": self._close_settle_flat_side,
                "close_oid": self._last_close_oid,
                "reason": reason or "flat_open_forbid",
            },
        )

    def _fail_closed_ghost_flatten(
        self, venue_net: float, *, reason: str = ""
    ) -> None:
        """Halt ENTER, MARKET-flatten venue residue, align local to 0."""
        self.halt_trading = True
        self.halt_reason = (
            f"GHOST_FAILCLOSED venue={venue_net:+.4f} "
            f"hits={int(getattr(self, '_ghost_mismatch_hits', 0) or 0)}"
        )
        self.desync_halts += 1
        _log(f"[{self.LANE}] 🚨 {self.halt_reason} — halt+flatten ({reason})")
        _append(
            AUDIT_PATH,
            {
                "event": "GHOST_FAILCLOSED",
                "ts": _iso(),
                "venue_net": venue_net,
                "hits": int(getattr(self, "_ghost_mismatch_hits", 0) or 0),
                "reason": reason or "persistent_ghost",
            },
        )
        try:
            self.flatten(f"ghost_failclosed:{reason or 'persistent_ghost'}")
        except Exception as exc:
            _log(f"[{self.LANE}] GHOST_FAILCLOSED flatten err: {exc}")
        # Local must match flat venue after emergency reduce.
        if int(self.net) != 0:
            mid = float(self._feeder_mid() or self.entry_px or 1.0)
            close_side = "SELL" if self.net > 0 else "BUY"
            self.pending_exit_why = self.pending_exit_why or "ghost_failclosed"
            self._close_ledger(
                mid if mid > 0 else 1.0,
                "GHOST_FAILCLOSED",
                self.pending_exit_why,
                close_side=close_side,
            )
            self.pending_exit_why = ""
        self._clear_working()
        self.ghosts = []
        self._reset_ghost_mismatch()
        self.running = False

    def _block_ghost_position(
        self, venue_net: float, *, reason: str = "", now: Optional[float] = None
    ) -> None:
        """Unattributed venue residue while local flat.

        Close-settle: observe only (Phantom report lag) — never Force-Sync-open.
        Otherwise: streak toward Fail-Closed (halt + flatten). Never seal
        recent_posts here — that would kill Late-Fill ADOPT of cancel races.
        """
        t = float(now if now is not None else time.time())
        self._clear_working()
        self.ghosts = []
        if self._in_close_settle(t):
            self._log_ghost_position_blocked(
                venue_net, reason=reason or "close_settle_observe", now=t
            )
            return
        if float(getattr(self, "_ghost_mismatch_since", 0.0) or 0.0) <= 0.0:
            self._ghost_mismatch_since = t
        self._ghost_mismatch_hits = int(
            getattr(self, "_ghost_mismatch_hits", 0) or 0
        ) + 1
        self._log_ghost_position_blocked(
            venue_net, reason=reason or "flat_open_forbid", now=t
        )
        elapsed = t - float(self._ghost_mismatch_since or t)
        if (
            self._ghost_mismatch_hits >= int(GHOST_FAILCLOSED_HITS)
            or elapsed >= float(GHOST_FAILCLOSED_SEC)
        ):
            self._fail_closed_ghost_flatten(
                venue_net, reason=reason or "persistent_ghost"
            )

    def _force_sync_local_to_venue(self, venue_net: float) -> None:
        """Venue Truth overwrite for non-flat→flat / in-pos sync.

        Flat → non-flat open is not done here; caller must Late-Fill ADOPT first,
        else route to GHOST observe / Fail-Closed.
        """
        target = self._venue_to_lots(venue_net)
        old_net = int(self.net)
        old_btc = float(old_net) * float(self.size)
        if target == old_net:
            _log(
                f"[{self.LANE}] VENUE_FORCE_SYNC lots unchanged "
                f"local={old_btc:+.4f} venue={venue_net:+.4f} lots={target}"
            )
            return
        # Flat → non-flat open is forbidden here (use Late-Fill or Fail-Closed).
        if old_net == 0 and target != 0:
            self._block_ghost_position(venue_net, reason="force_sync_open")
            return
        # Seal only after we know this is a close/align sync, not a late-fill window.
        for rec in list(self.recent_posts):
            self._terminate_oid(str(rec.get("id") or ""), "venue_force_sync")
        mid = self._feeder_mid() or abs(float(venue_net) and 1.0)
        if mid <= 0:
            mid = 1.0
        _log(
            f"[{self.LANE}] VENUE_FORCE_SYNC overwrite lots {old_net}→{target} "
            f"local_btc={old_btc:+.4f} venue={venue_net:+.4f}"
        )
        _append(
            AUDIT_PATH,
            {
                "event": "VENUE_FORCE_SYNC",
                "ts": _iso(),
                "old_lots": old_net,
                "new_lots": target,
                "venue_net": venue_net,
                "mid": mid,
            },
        )
        # Close open local book if leaving a position.
        if old_net != 0 and target != old_net:
            self.pending_exit_why = self.pending_exit_why or "venue_force_sync"
            # Accounting close only — venue already holds target inventory.
            close_side = "SELL" if old_net > 0 else "BUY"
            self._close_ledger(
                mid,
                "VENUE_FORCE_SYNC",
                self.pending_exit_why,
                close_side=close_side,
            )
            self.pending_exit_why = ""
        self._clear_working()
        self.ghosts = []
        self._reset_ghost_mismatch()

    def _reconcile_venue(self, now: float) -> None:
        if not self.real or self.halt_trading:
            return
        if now - self.last_reconcile_ts < RECONCILE_INTERVAL_SEC:
            return
        self.last_reconcile_ts = now
        try:
            active = self.client.get_child_orders(PRODUCT, child_order_state="ACTIVE") or []
            positions = self.client.get_positions(PRODUCT) or []
        except Exception as exc:
            self.api_errors += 1
            _log(f"[{self.LANE}] reconcile err: {exc}")
            return
        venue_net = parse_venue_net(positions)
        local_btc = float(self.net) * self.size
        n_active = len(active)
        matched = abs(local_btc - venue_net) <= INV_MISMATCH_BTC
        _log(
            f"[{self.LANE}] RECONCILE local={local_btc:+.4f} venue={venue_net:+.4f} "
            f"active={n_active} ghosts={len(self.ghosts)} fills={self.fills} "
            f"predict={self.predict_fills} adopt={self.adopt_fills} match={matched}"
        )
        _append(
            AUDIT_PATH,
            {
                "event": "RECONCILE",
                "ts": _iso(),
                "local_btc": local_btc,
                "venue_net": venue_net,
                "active": n_active,
                "ghosts": len(self.ghosts),
                "match": matched,
            },
        )
        # Local flat + venue isolated dust: auto-clear (min-lot overshoot unwind).
        if (
            abs(local_btc) <= EXACT_FLAT_EPS_BTC
            and is_isolated_dust(venue_net, float(self.size) or SIZE_BTC)
            and n_active == 0
            and not self.ghosts
            and (now - float(getattr(self, "_last_dust_sweep_ts", 0.0) or 0.0))
            >= DUST_SWEEP_COOLDOWN_SEC
        ):
            _log(
                f"[{self.LANE}] RECONCILE_DUST_SWEEP venue={venue_net:+.8f} "
                f"(local flat, sub-min lot)"
            )
            venue_net = self._neutralize_venue_dust(venue_net)
            local_btc = float(self.net) * self.size
            matched = abs(local_btc - venue_net) <= INV_MISMATCH_BTC
            _append(
                AUDIT_PATH,
                {
                    "event": "RECONCILE_DUST_SWEEP",
                    "ts": _iso(),
                    "venue_net_after": venue_net,
                    "flat_ok": abs(venue_net) < EXACT_FLAT_EPS_BTC,
                },
            )

        if abs(local_btc - venue_net) > INV_MISMATCH_BTC:
            _log(
                f"[{self.LANE}] RECONCILE_DRIFT local={local_btc:+.4f} "
                f"venue={venue_net:+.4f} — working/ghost/recent Late-Fill adopt "
                f"then venue force-sync / ghost fail-closed"
            )
            for grace_i in range(ADOPT_GRACE_TRIES):
                got = False
                if self._try_adopt_inflight():
                    got = True
                if self._try_adopt_recent_posts(venue_net):
                    got = True
                if got:
                    local_btc = float(self.net) * self.size
                    try:
                        positions = self.client.get_positions(PRODUCT) or []
                        venue_net = parse_venue_net(positions)
                    except Exception as exc:
                        self.api_errors += 1
                        _log(f"[{self.LANE}] adopt refresh positions err: {exc}")
                    if abs(local_btc - venue_net) <= INV_MISMATCH_BTC:
                        _log(
                            f"[{self.LANE}] RECONCILE_RESOLVED after adopt "
                            f"(grace={grace_i}) local={local_btc:+.4f} venue={venue_net:+.4f}"
                        )
                        self._reset_ghost_mismatch()
                        break
                time.sleep(ADOPT_GRACE_SLEEP_SEC)
                try:
                    positions = self.client.get_positions(PRODUCT) or []
                    venue_net = parse_venue_net(positions)
                except Exception as exc:
                    self.api_errors += 1
                    _log(f"[{self.LANE}] adopt grace positions err: {exc}")
                local_btc = float(self.net) * self.size
                if abs(local_btc - venue_net) <= INV_MISMATCH_BTC:
                    _log(
                        f"[{self.LANE}] RECONCILE_RESOLVED venue caught up "
                        f"(grace={grace_i}) local={local_btc:+.4f} venue={venue_net:+.4f}"
                    )
                    self._reset_ghost_mismatch()
                    break

            local_btc = float(self.net) * self.size
            if abs(local_btc - venue_net) > INV_MISMATCH_BTC:
                # Physical ceiling only — otherwise Venue Truth overwrite & continue.
                if abs(venue_net) > HARD_HALT_BTC + 1e-12:
                    self.desync_halts += 1
                    self.halt_trading = True
                    self.halt_reason = (
                        f"HARD_INV_CEILING venue={venue_net:+.4f} "
                        f"> ±{HARD_HALT_BTC}"
                    )
                    _log(f"[{self.LANE}] 🚨 {self.halt_reason} — halt+flatten")
                    _append(
                        AUDIT_PATH,
                        {
                            "event": "DESYNC_HALT",
                            "ts": _iso(),
                            "local_btc": local_btc,
                            "venue_net": venue_net,
                            "kind": "hard_ceiling",
                        },
                    )
                    self.running = False
                    return
                if self._should_block_force_sync_open(venue_net):
                    # Unattributed residue — observe / Fail-Closed (never silent loop).
                    self._block_ghost_position(
                        venue_net,
                        reason="reconcile_flat_open_forbid",
                        now=now,
                    )
                else:
                    self._force_sync_local_to_venue(venue_net)
                    self.force_syncs += 1
        else:
            self._reset_ghost_mismatch()

        tracked = self._tracked_oids()
        try:
            active = self.client.get_child_orders(PRODUCT, child_order_state="ACTIVE") or []
        except Exception:
            active = []
        for vo in active:
            oid = str(vo.get("child_order_acceptance_id") or "")
            if oid and oid not in tracked:
                _log(f"[{self.LANE}] PURGE_ORPHAN {oid}")
                self._request_cancel(oid, "PURGE_ORPHAN")
                self.cancels += 1
                _append(
                    AUDIT_PATH,
                    {"event": "PURGE_ORPHAN", "ts": _iso(), "id": oid, "order": vo},
                )

    def _apply_fill(
        self, side: str, px: float, src: str, *, arm_tp: bool = True
    ) -> None:
        side_u = str(side or "").upper()
        # Late/double EXIT echo while flat in settle window — do not open phantom.
        if self.net == 0 and self._is_late_close_echo(side_u):
            _log(
                f"[{self.LANE}] LATE_CLOSE_ECHO ignore {side_u} @ {px:,.0f} "
                f"src={src} settle_side={self._close_settle_flat_side} "
                f"close_oid={self._last_close_oid}"
            )
            _append(
                AUDIT_PATH,
                {
                    "event": "LATE_CLOSE_ECHO",
                    "ts": _iso(),
                    "side": side_u,
                    "px": px,
                    "src": src,
                    "settle_side": self._close_settle_flat_side,
                    "close_oid": self._last_close_oid,
                },
            )
            self._clear_working()
            return
        self.fills += 1
        close_oid = str(self.working_id or "")
        _append(
            AUDIT_PATH,
            {
                "event": "FILL",
                "ts": _iso(),
                "side": side_u,
                "px": px,
                "src": src,
                "id": close_oid,
                "real": self.real,
            },
        )
        _log(f"[{self.LANE}] FILL {side_u} @ {px:,.0f} src={src}")
        self._clear_working()
        close_why = self.pending_exit_why or "flip_flatten"
        opened = False
        if side_u == "BUY":
            if self.net < 0:
                self._close_ledger(
                    px, src, close_why, close_side="BUY", close_oid=close_oid
                )
                self.pending_exit_why = ""
            else:
                self.net = 1
                self.entry_px = px
                self.entry_src = src
                self.entry_ts = time.time()
                self.flatten_state = FLATTEN_HOLD
                self._ae_track(side_u, px)
                self._notify_live_trade(
                    "FILL",
                    f"ENTRY BUY @ {px:,.0f} src={src} size={self.size}",
                )
                opened = True
        else:
            if self.net > 0:
                self._close_ledger(
                    px, src, close_why, close_side="SELL", close_oid=close_oid
                )
                self.pending_exit_why = ""
            else:
                self.net = -1
                self.entry_px = px
                self.entry_src = src
                self.entry_ts = time.time()
                self.flatten_state = FLATTEN_HOLD
                self._ae_track(side_u, px)
                self._notify_live_trade(
                    "FILL",
                    f"ENTRY SELL @ {px:,.0f} src={src} size={self.size}",
                )
                opened = True
        if opened and arm_tp and src != "HARD_MARKET":
            self._arm_tp_maker_rest()

    def _ae_track(self, side: str, px: float) -> None:
        bid = ask = mid = 0.0
        spread_bp = 0.0
        try:
            raw = json.loads(FEEDER_LATEST.read_text(encoding="utf-8"))
            bid = float(raw.get("best_bid") or 0.0)
            ask = float(raw.get("best_ask") or 0.0)
            if bid > 0 and ask > 0:
                mid = 0.5 * (bid + ask)
                spread_bp = ((ask - bid) / bid) * 1e4
        except Exception:
            mid = float(px)
        self.ae.track_entry(
            px=float(px),
            now=time.time(),
            side=side.lower(),
            mid=mid if mid > 0 else float(px),
            spread_bp=spread_bp,
            meta={"lane": self.LANE, "wire": "YES" if self.real else "DRY", "real": self.real},
        )

    def _close_ledger(
        self,
        exit_px: float,
        src: str,
        why: str,
        *,
        close_side: str = "",
        close_oid: str = "",
    ) -> None:
        if self.entry_px <= 0 or self.net == 0:
            return
        if self.net > 0:
            pnl_bp = ((exit_px / self.entry_px) - 1.0) * 1e4
            inferred_side = "SELL"
        else:
            pnl_bp = ((self.entry_px / exit_px) - 1.0) * 1e4
            inferred_side = "BUY"
        pnl_j = pnl_bp * self.size * self.entry_px / 1e4
        self.pnl_bp += pnl_bp
        self.pnl_jpy += pnl_j
        self.trades += 1
        if pnl_bp > 0:
            self.wins += 1
        row: Dict[str, Any] = {
            "ts": time.time(),
            "lane": self.LANE,
            "action": "CLOSE_LONG" if self.net > 0 else "COVER_SHORT",
            "price": exit_px,
            "size": self.size,
            "pnl_bp": round(pnl_bp, 2),
            "pnl_jpy": round(pnl_j, 1),
            "is_win": pnl_bp > 0,
            "source": src,
            "exit_reason": why,
            "entry_src": self.entry_src or "",
            "hold_s": round(time.time() - self.entry_ts, 1) if self.entry_ts else None,
            "real": self.real,
        }
        try:
            path = self.ae.close(exit_px=exit_px, pnl_bp=pnl_bp, reason=why) or {}
            row.update(path)
        except Exception:
            pass
        if why.startswith("adverse_cut"):
            self.adverse_cuts += 1
        _append(LEDGER_PATH, row)
        self.history.append(
            {
                "ts": time.time(),
                "pnl_bp": round(pnl_bp, 2),
                "pnl_jpy": round(pnl_j, 1),
                "is_win": pnl_bp > 0,
            }
        )
        _log(f"[{self.LANE}] CLOSE pnl={pnl_bp:+.2f}bp src={src} ({why})")
        self._notify_live_trade(
            "CLOSE",
            (
                f"pnl={pnl_bp:+.2f}bp (¥{pnl_j:+.0f}) exit={exit_px:,.0f} "
                f"src={src} why={why} Σ={self.pnl_bp:+.2f}bp n={self.trades}"
            ),
            color=0x2ECC71 if pnl_bp >= 0 else 0xE74C3C,
        )
        self.net = 0
        self.entry_px = 0.0
        self.entry_src = ""
        self.entry_ts = 0.0
        self.flatten_state = FLATTEN_HOLD
        self._adverse_trigger_bp = None
        self._adverse_trigger_ts = 0.0
        self._soft_reprice_n = 0
        self._soft_exit_armed_ts = 0.0
        # Arm settle debounce so venue lag / second EXIT fill cannot open phantom.
        self._mark_close_settle(
            close_side or inferred_side, close_oid=close_oid
        )

    def _ae_override(
        self, *, now: float, bid: float, ask: float, mid: float
    ) -> Optional[Tuple[str, Optional[str], str]]:
        if self.net == 0:
            return None
        side = "buy" if self.net > 0 else "sell"
        self.ae.on_tick(mid=mid, now=now, bid=bid, ask=ask, side=side)
        toxic, tw = self.ae.path_toxic()
        if not toxic:
            return None
        self.adverse_fast_cuts += 1
        flatten = "SELL" if self.net > 0 else "BUY"
        if self.entry_px > 0 and mid > 0:
            if self.net > 0:
                self._adverse_trigger_bp = ((mid / self.entry_px) - 1.0) * 1e4
            else:
                self._adverse_trigger_bp = ((self.entry_px / mid) - 1.0) * 1e4
            self._adverse_trigger_ts = float(now)
        return EXIT, flatten, f"adverse_fast ({tw})"

    def metrics(self) -> Dict[str, Any]:
        n = self.trades
        return {
            "ts_jst": _iso(),
            "model": self.LANE,
            "mode": "REAL" if self.real else "DRY",
            "wire": "YES" if self.real else "DRY",
            "series": "MM5",
            "instant_harvest": False,
            "rest_exit_on_ev_hold": True,
            "adverse_hit_touch": False,
            "flatten_state": self.flatten_state,
            "size": self.size,
            "max_inv": self.max_inv,
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "pnl_bp": round(self.pnl_bp, 2),
            "pnl_jpy": round(self.pnl_jpy, 1),
            "ev_bp": round(self.pnl_bp / n, 4) if n else 0.0,
            "win_rate_pct": round(100.0 * self.wins / n, 1) if n else 0.0,
            "total_trades": n,
            "posts": self.posts,
            "cancels": self.cancels,
            "fills": self.fills,
            "rejects": self.rejects,
            "api_errors": self.api_errors,
            "adverse_cuts": self.adverse_cuts,
            "adverse_fast_cuts": self.adverse_fast_cuts,
            "adverse_market_exits": self.adverse_market_exits,
            "ev_gone_market_exits": self.ev_gone_market_exits,
            "soft_to_hard_escalations": int(
                getattr(self, "soft_to_hard_escalations", 0) or 0
            ),
            "soft_reprice_n": int(getattr(self, "_soft_reprice_n", 0) or 0),
            "entry_stale_cancels": self.entry_stale_cancels,
            "force_syncs": self.force_syncs,
            "spread_gate_blocks": int(getattr(self, "spread_gate_blocks", 0) or 0),
            "claimed_oids": len(self.claimed_oids),
            "private_ws_fills": self.private_ws_fills,
            "toxic_inflight_cleanups": self.toxic_inflight_cleanups,
            "private_ws": (
                None
                if self.private_ws is None
                else {
                    "connected": bool(self.private_ws.connected),
                    "authed": bool(self.private_ws.authed),
                    "subscribed": bool(self.private_ws.subscribed),
                    "exec_n": int(self.private_ws.exec_n),
                    "auth_ok_n": int(getattr(self.private_ws, "auth_ok_n", 0) or 0),
                    "subscribe_ok_n": int(
                        getattr(self.private_ws, "subscribe_ok_n", 0) or 0
                    ),
                    "errors": int(self.private_ws.errors),
                    "last_error": str(self.private_ws.last_error or ""),
                }
            ),
            "adverse_cooldown_until": self._adverse_cooldown_until,
            "edge_pulls": self.edge_pulls,
            "predict_fills": self.predict_fills,
            "adopt_fills": self.adopt_fills,
            "desync_halts": self.desync_halts,
            "ghosts": len(self.ghosts),
            "halt_trading": self.halt_trading,
            "halt_reason": self.halt_reason,
            "net_lots": self.net,
            "entry_px": self.entry_px,
            "working_side": self.working_side,
            "working_px": self.working_px,
            "signal": self.sig_confirmed,
            "signal_streak": self.sig_streak,
            "stats_1h": _window_stats_mm(self.history, 1.0),
            "stats_24h": _window_stats_mm(self.history, 24.0),
            "max_runtime_sec": self.max_runtime_sec,
            "note": (
                "MM5 LIVE · OID claim · venue force-sync · private child_order WS · "
                "SOFT passive LIMIT · HARD MARKET · FILL→TP maker · "
                "hard halt only ±HARD_HALT_BTC"
            ),
        }

    def save_state(self) -> None:
        STATE_PATH.write_text(
            json.dumps(self.metrics(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _notify_live_trade(
        self, kind: str, description: str, *, color: Optional[int] = None
    ) -> None:
        """Every LIVE fill/close goes to the canonical LIVE Discord channel."""
        try:
            payload = live_trade_embed(
                lane=self.LANE,
                kind=kind,
                description=description,
                footer_ts=_iso(),
                color=color,
            )
            status = post_live_discord(payload)
            _log(f"[DISCORD] LIVE {kind} sent (HTTP {status})")
        except Exception as exc:
            _log(f"[DISCORD] LIVE {kind} failed: {exc}")

    def send_discord(self, kind: str = "hourly") -> None:
        m = self.metrics()
        n = int(m.get("total_trades") or 0)
        payload = {
            "username": "WOBIMM5_LIVE",
            "embeds": [
                {
                    "title": f"WOBIMM5 LIVE {kind} · {m.get('mode')}",
                    "description": (
                        f"Σ={m.get('pnl_bp'):+.2f}bp EV={m.get('ev_bp'):+.3f} "
                        f"n={n} WR={m.get('win_rate_pct')}% "
                        f"posts={m.get('posts')} fills={m.get('fills')}"
                    ),
                    "footer": {"text": f"Antigravity · WOBIMM5_LIVE · {m.get('ts_jst')}"},
                    "color": 0x3498DB,
                }
            ],
        }
        try:
            status = post_live_discord(payload)
            _log(f"[DISCORD] {kind} sent (HTTP {status}) → LIVE webhook")
        except Exception as exc:
            _log(f"[DISCORD] {exc}")

    def _maybe_discord(self, now: float) -> None:
        jst_now = datetime.now(JST)
        if jst_now.hour != self.last_reported_hour and jst_now.minute >= REPORT_MINUTE:
            self.send_discord("hourly")
            self.last_reported_hour = jst_now.hour
            self.last_report = now
        elif now - self.last_report >= self.report_interval_sec:
            self.send_discord("hourly")
            self.last_reported_hour = jst_now.hour
            self.last_report = now

    def _tick(self, snap: Any, now: float) -> None:
        bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0:
            return
        spread_bp = ((ask - bid) / bid) * 1e4
        bids, asks = self._depth()
        m = self.engine.update_and_evaluate(bids, asks, bid, ask, now=now)
        # P0-3: private WS executions beat public feeder lag.
        self._drain_private_ws_fills()
        execs = self._poll_execs()
        self._maybe_fill(execs, bid, ask)
        self._reconcile_venue(now)
        if self.halt_trading:
            return

        # Stale entry quote: cancel before it becomes toxic fill fodder.
        if (
            self.real
            and self.net == 0
            and self.working_side
            and self.working_posted_at > 0
            and (now - float(self.working_posted_at)) >= ENTRY_STALE_CANCEL_SEC
        ):
            self.entry_stale_cancels += 1
            _log(
                f"[{self.LANE}] ENTRY_STALE_CANCEL "
                f"{self.working_side} age={now - self.working_posted_at:.1f}s "
                f">={ENTRY_STALE_CANCEL_SEC:.0f}s"
            )
            self._pull("entry_stale")

        wobi = float(m.get("wobi") or 0.0)
        ofi = float(m.get("recent_ofi") or 0.0)
        mid = (bid + ask) / 2.0
        unrealized_bp = 0.0
        if self.net != 0 and self.entry_px > 0 and mid > 0:
            if self.net > 0:
                unrealized_bp = ((mid / self.entry_px) - 1.0) * 1e4
            else:
                unrealized_bp = ((self.entry_px / mid) - 1.0) * 1e4

        raw, raw_why = signed_signal(wobi, ofi)
        confirm_need = LIVE_CONFIRM_TICKS if self.real else CONFIRM_TICKS
        self.sig_pending, self.sig_streak, self.sig_confirmed = step_confirm(
            self.sig_pending,
            self.sig_streak,
            self.sig_confirmed,
            raw,
            need=confirm_need,
        )
        if self.sig_confirmed == 0 and raw != 0:
            why_sig = f"confirming {raw_why} {self.sig_streak}/{confirm_need}"
        elif self.sig_confirmed != 0 and raw != self.sig_confirmed and raw != 0:
            why_sig = f"confirming_flip {raw_why} {self.sig_streak}/{confirm_need}"
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
            # REAL: skip thin-edge ENTER; pull when live edge leaves the quote.
            entry_tight=bool(self.real),
            edge_soft_pull=bool(self.real),
            instant_harvest=False,
            rest_exit_on_ev_hold=True,
            scale_in=False,
            max_lots=1,
        )
        override = self._ae_override(now=now, bid=bid, ask=ask, mid=mid)
        if override:
            action, side, why = override
        else:
            action, side, why = decide(st)

        if action == KEEP:
            return
        if action == WAIT:
            if why.startswith("live_disagree"):
                if not self._live_disagreeing:
                    self.live_disagrees += 1
                    self._live_disagreeing = True
            else:
                self._live_disagreeing = False
            return
        if action == PULL:
            if why.startswith("edge_"):
                self.edge_pulls += 1
            self._pull(why)
            return
        # Soft = passive opposite LIMIT; Hard = MARKET. Never cross Soft.
        if action in (ENTER, EXIT) and side:
            if (
                action == ENTER
                and self.real
                and now < float(getattr(self, "_adverse_cooldown_until", 0.0) or 0.0)
            ):
                return
            if action == ENTER and self._should_block_entry_for_spread(bid, ask):
                return
            if action == EXIT:
                # Healthy TP grace: defer Soft ev_gone; keep best-opposite TP.
                if self._maybe_defer_soft_exit(why, now, bid, ask):
                    self._live_disagreeing = False
                    return
                mode = self._advance_flatten(why)
                # Soft adverse_fast dig-down → HARD before another Soft chase.
                if mode == FLATTEN_SOFT or self.flatten_state == FLATTEN_SOFT:
                    esc = self._soft_adverse_escalate_reason(why, unrealized_bp)
                    if esc:
                        self._escalate_soft_to_hard(esc, now)
                        self._live_disagreeing = False
                        return
                if mode == FLATTEN_HARD:
                    if str(why).startswith("adverse_cut"):
                        self._adverse_trigger_bp = float(unrealized_bp)
                        self._adverse_trigger_ts = float(now)
                    self._hard_market_exit(why, now)
                    self._live_disagreeing = False
                    return
            if self.working_side and self.working_side != side:
                if self._pull("replace"):
                    # Pulled order already filled — inventory updated; stop this tick.
                    return
            if self.working_side:
                if action == EXIT and self.working_side == side:
                    want_px = self._exit_px(side, bid, ask, why)
                    if want_px > 0 and abs(float(self.working_px) - float(want_px)) > 1e-9:
                        if self._is_adverse_fast_soft(why):
                            self._soft_reprice_n = int(
                                getattr(self, "_soft_reprice_n", 0) or 0
                            ) + 1
                            esc = self._soft_adverse_escalate_reason(
                                why, unrealized_bp
                            )
                            if esc:
                                self._escalate_soft_to_hard(esc, now)
                                self._live_disagreeing = False
                                return
                        if self._pull("exit_reprice"):
                            # First EXIT already filled on venue — do NOT post a 2nd EXIT.
                            return
                    elif exit_quote_fillable(self.working_side, self.working_px, bid, ask):
                        return
                    else:
                        if self._pull("exit_unfillable"):
                            return
                else:
                    return
            # Flat after adopt-on-pull: nothing to exit / nothing to enter wrongly.
            if action == EXIT and self.net == 0:
                return
            if action == ENTER and self.net != 0:
                return
            if action == EXIT:
                px = self._exit_px(side, bid, ask, why)
            else:
                # Passive behind touch (ENTRY_OFFSET_BP); never improve into spread.
                px = maker_px(side, entry_passive_px(side, bid, ask), bid, ask)
            if px <= 0:
                return
            self._post(side, px, f"{action} {why}")
            self._live_disagreeing = False
            if action == EXIT:
                self.pending_exit_why = why
                # Soft EXIT rests; do not force same-tick fill (that was friction death).
                # HARD already returned via MARKET above.

    def run(self) -> int:
        mode = "REAL" if self.real else "DRY"
        _log(
            f"=== {self.LANE} START mode={mode} size={self.size} "
            f"max_runtime={self.max_runtime_sec or 'none'} ==="
        )
        PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.feed.start()
        self.save_state()
        self.send_discord("start")
        self.last_reported_hour = datetime.now(JST).hour
        self.last_report = time.time()
        last = None
        stop_reason = "clean"
        try:
            while self.running:
                if STOP_FLAG.exists():
                    stop_reason = "operator_stop_flag"
                    _log("STOP flag — flatten + exit")
                    break
                now = time.time()
                if self.halt_trading:
                    stop_reason = self.halt_reason or "desync_halt"
                    _log(f"halt_trading — flatten + exit ({stop_reason})")
                    break
                if self.max_runtime_sec > 0 and (now - self.t0) >= self.max_runtime_sec:
                    stop_reason = "max_runtime"
                    _log(f"max_runtime {self.max_runtime_sec}s — flatten + exit")
                    break
                if self.max_fills > 0 and self.fills >= self.max_fills:
                    stop_reason = f"max_fills:{self.fills}"
                    _log(f"max_fills {self.max_fills} reached — flatten + exit")
                    break
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
                self._maybe_discord(now)
                time.sleep(self.interval_sec)
        except KeyboardInterrupt:
            stop_reason = "SIGINT"
            _log("SIGINT")
        except Exception as exc:
            stop_reason = f"fatal:{exc}"
            _log(f"fatal: {exc}")
            import traceback

            traceback.print_exc()
            return 1
        finally:
            try:
                self.flatten(stop_reason)
            except Exception as exc:
                _log(f"flatten err: {exc}")
            self.send_discord("final")
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
            if self.private_ws is not None:
                try:
                    self.private_ws.stop()
                except Exception:
                    pass
            if PID_PATH.exists():
                PID_PATH.unlink(missing_ok=True)
            _log(f"=== {self.LANE} STOPPED reason={stop_reason} ===")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="WOBIMM5 LIVE clean chassis (MM5 Position-EV + Adverse)"
    )
    parser.add_argument("--size", type=float, default=SIZE_BTC)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--max-runtime-sec", type=float, default=0.0)
    parser.add_argument(
        "--max-fills",
        type=int,
        default=0,
        help="Stop after N committed fills (PUBLIC_OID/DRY). 0=unlimited",
    )
    parser.add_argument("--real", action="store_true", help="Venue POST (requires GO gates)")
    parser.add_argument("--confirm-go", type=str, default="")
    args = parser.parse_args()

    if args.size > SIZE_BTC + 1e-12:
        print(f"REFUSE: size must be <= {SIZE_BTC}", flush=True)
        return 2

    real = bool(args.real)
    if real:
        reason = refuse_real_gates(str(args.confirm_go or ""))
        if reason:
            print(reason, flush=True)
            return 0

    runner = WOBIMM5Live(
        size=args.size,
        interval_sec=args.interval,
        market_source=args.source,
        report_interval_sec=args.report_interval,
        real=real,
        max_runtime_sec=args.max_runtime_sec,
        max_fills=int(args.max_fills or 0),
    )

    def _sig(signum, _frame):
        _log(f"signal {signum}")
        runner.stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
