#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MS1_WOBI LIVE — Micro Scalp 1 venue chassis (CSR-694).

Greenfield LIVE runner. Does **not** subclass TF2B_WOBI3 LIVE or paper DryRun.
Libraries only: entry_gate / calculate_optimal_order_price / WOBIDirectionalEngine3 /
BitFlyerClient / FeederBoardSource / public_maker_fill / live_discord.

Policy (MS1):
  - BUY-only ENTER via entry_gate (spread observe-only)
  - Long hold while live EV (wobi≥FAVORABLE ∧ ofi≥0) → EV_HOLD
  - EV_GONE → wall-anchor Maker SELL (calculate_optimal_order_price)
  - LIMIT only · lot ≤ 0.001 · confirmed net gates
  - CSR-694b: **one live child order max** — same-side reprice = cancel then
    wait (never stack). Venue net hard-capped at MAX_INV_BTC.

Default DRY (no venue POST). REAL requires GO ACTIVE + CONFIRM_GO=MS1_LIVE +
operator-set ENABLE_REAL_TRADING=true + --real --confirm-go MS1.
Never writes ENABLE_REAL_TRADING into .env. Economic_PASS=NO.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
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
from antigravity.quant_pipeline.ingestion import LiveBoardFeed, MarketDataIngestion  # noqa: E402
from antigravity.quant_pipeline.live_discord import (  # noqa: E402
    live_discord_webhook_url,
    post_live_discord,
)
from antigravity.quant_pipeline.parquet_logger import ParquetBatchLogger  # noqa: E402
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import (  # noqa: E402
    WOBIDirectionalEngine3,
    calculate_optimal_order_price,
)
from antigravity.quant_pipeline.run_wobimm_live import (  # noqa: E402
    _foreign_pids,
    _parse_venue_net,
    public_maker_fill,
)
from antigravity.quant_pipeline.wobimm3_policy import (  # noqa: E402
    FAVORABLE_WOBI,
    Wobi3Market,
    entry_gate,
)

JST = timezone(timedelta(hours=9))
PRODUCT = "FX_BTC_JPY"
SIZE_BTC = 0.001
MAX_INV_BTC = 0.001  # hard cap — never accumulate past 1 lot (CSR-694b)
EXACT_FLAT_EPS = 1e-7
CONFIRM_TOKEN = "MS1"
CONFIRM_ENV = "MS1_LIVE"
NEEDLE = "antigravity.quant_pipeline.run_ms1_wobi_live"
FOREIGN_NEEDLES = (
    "antigravity.quant_pipeline.run_wobimm_live",
    "antigravity.quant_pipeline.run_wobimm5_live",
    "antigravity.quant_pipeline.run_tf2b_wobi3_live",
    "antigravity.quant_pipeline.run_peg2b_live_trial",
    "antigravity.quant_pipeline.run_micro_live_soak_csr574",
)

OUT = AG / "data" / "mm_research" / "ms1_wobi_live"
STATE_PATH = AG / "data" / "ms1_wobi_live_state.json"
PID_PATH = AG / "data" / "ms1_wobi_live.pid"
STOP_FLAG = AG / "data" / "MS1_WOBI_LIVE_STOP.flag"
GO_FLAG = AG / "data" / "GO_MS1_WOBI_LIVE.flag"
LEDGER_PATH = OUT / "paired_ledger.jsonl"
AUDIT_PATH = OUT / "audit.jsonl"
FEEDER_LATEST = AG / "data" / "market_feeder" / "latest.json"
FEEDER_EXECS = AG / "data" / "market_feeder" / "executions.jsonl"

POS_POLL_SEC = 15.0
POS_POLL_ACTIVE_SEC = 2.0  # while working / non-flat
MIN_API_INTERVAL_SEC = 2.0
STOP_PNL_BP = -80.0
OPTIMISTIC_FILL_SOURCES = frozenset({"BOOK_CROSS", "BOARD_TRADE"})


def _iso() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def _log(msg: str) -> None:
    print(f"[{_iso()}] {msg}", flush=True)


def _append(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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


def refuse_size(size: float) -> Optional[str]:
    if float(size) > SIZE_BTC + 1e-12:
        return f"REFUSE: SIZE must be <= {SIZE_BTC}"
    return None


def refuse_concurrent() -> Optional[str]:
    if _foreign_pids(NEEDLE):
        return "REFUSE: MS1 live already running"
    for n in FOREIGN_NEEDLES:
        if _foreign_pids(n):
            return f"REFUSE: other LIVE active ({n.split('.')[-1]})"
    return None


class MS1WobiLive:
    """MS1 micro-scalp LIVE — BUY enter / Maker EV_GONE exit / LIMIT only."""

    LANE = "MS1_WOBI_LIVE"

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
        bad = refuse_size(size)
        if bad:
            raise ValueError(bad)
        self.size = float(size)
        self.interval_sec = float(interval_sec)
        self.market_source = resolve_market_source(market_source)
        self.report_interval_sec = float(report_interval_sec)
        self.real = bool(real)
        self.max_runtime_sec = float(max_runtime_sec or 0.0)
        self.max_fills = int(max_fills or 0)
        self.running = True
        self.stop_reason = ""
        self.t0 = time.time()
        self.last_reported_hour = datetime.now(JST).hour
        self.last_state_save = 0.0
        self.last_api_ts = 0.0
        self.last_pos_poll = 0.0

        self.confirmed_net = 0.0
        self.venue_net = 0.0
        self.venue_avg = 0.0
        self.entry_px = 0.0
        self.entry_ts = 0.0
        self.working_side: Optional[str] = None
        self.working_px = 0.0
        self.working_id = ""
        self.working_why = ""
        self.cancel_pending = False  # cancel sent; do not POST until cleared
        self.pending_exit_tag = ""
        self.pending_exit_reason = ""
        self.halt_new_entries = False
        self.oversize_halts = 0

        self.posts = 0
        self.cancels = 0
        self.fills = 0
        self.rejects = 0
        self.api_errors = 0
        self.blocked_posts = 0
        self.pnl_bp = 0.0
        self.pnl_jpy = 0.0
        self.trades = 0
        self.wins = 0
        self.history: List[Dict[str, Any]] = self._load_ledger()
        if self.history:
            self.pnl_bp = sum(float(r.get("pnl_bp") or 0.0) for r in self.history)
            self.pnl_jpy = sum(float(r.get("pnl_jpy") or 0.0) for r in self.history)
            self.trades = len(self.history)
            self.wins = sum(1 for r in self.history if r.get("is_win"))

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

        if self.real:
            self.client = BitFlyerClient(
                enable_real_trading=True, max_order_size=self.size
            )
        else:
            os.environ["ENABLE_REAL_TRADING"] = "false"
            self.client = BitFlyerClient(
                enable_real_trading=False, max_order_size=self.size
            )

        OUT.mkdir(parents=True, exist_ok=True)
        _write_json(PID_PATH, {"pid": os.getpid(), "ts": _iso()})
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

    def request_stop(self, reason: str) -> None:
        self.stop_reason = str(reason or "stop")
        self.running = False

    def stop(self, *_a) -> None:
        self.request_stop("stop")

    def _load_ledger(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not LEDGER_PATH.exists():
            return rows
        try:
            for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
                if line.strip():
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
                _log(f"[{self.LANE}] exec stream: {exc}")

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
                out.extend(row.get("rows") or [])
        except Exception:
            pass
        return out

    def _depth(self) -> Tuple[List[Dict[str, float]], List[Dict[str, float]]]:
        if not FEEDER_LATEST.exists():
            return [], []
        try:
            raw = json.loads(FEEDER_LATEST.read_text(encoding="utf-8"))
            return list(raw.get("bids") or [])[:10], list(raw.get("asks") or [])[:10]
        except Exception:
            return [], []

    def _is_long(self) -> bool:
        return self.confirmed_net >= self.size - 1e-12

    def _is_flat(self) -> bool:
        return abs(self.confirmed_net) <= EXACT_FLAT_EPS

    def _live_long_ev(self, wobi: float, ofi: float) -> bool:
        return float(wobi) >= FAVORABLE_WOBI and float(ofi) >= 0.0

    def _entry_ok(self, m: Dict[str, Any]) -> Tuple[bool, str]:
        ok, reason = entry_gate(
            Wobi3Market(
                spread_bp=float(m.get("spread_bp") or 0.0),
                wobi=float(m.get("wobi") or 0.0),
                z_score=float(m.get("z_score") or 0.0),
                micro_dev_bp=float(m.get("micro_dev_bp") or 0.0),
                d_wobi_dt=float(m.get("d_wobi_dt") or 0.0),
                recent_ofi=float(m.get("recent_ofi") or 0.0),
                ask_depletion_vel=float(m.get("ask_depletion_vel") or 0.0),
            ),
            skip_spread_gate=True,
        )
        spread_bp = float(m.get("spread_bp") or 0.0)
        if spread_bp > 2.0 and ok:
            reason = f"{reason} [spread_obs={spread_bp:.1f}bp]"
        return ok, reason

    def _can_api(self, now: float) -> bool:
        return (now - self.last_api_ts) >= MIN_API_INTERVAL_SEC

    def _clear_working(self) -> None:
        self.working_side = None
        self.working_px = 0.0
        self.working_id = ""
        self.working_why = ""

    def _inventory_ok_for_buy(self) -> bool:
        """BUY only from exact flat and under hard cap (venue+local)."""
        if self.halt_new_entries or self.cancel_pending:
            return False
        if not self._is_flat():
            return False
        if abs(self.venue_net) > EXACT_FLAT_EPS:
            return False
        if self.confirmed_net > MAX_INV_BTC + 1e-12:
            return False
        return True

    def _count_active_orders(self) -> int:
        """Venue ACTIVE child orders. -1 = unknown (refuse POST)."""
        if not self.real:
            return 1 if self.working_id else 0
        try:
            ords = self.client._request(
                "GET",
                f"/v1/me/getchildorders?product_code={PRODUCT}&child_order_state=ACTIVE",
            )
            if isinstance(ords, list):
                return len(ords)
            return -1
        except Exception as exc:
            _log(f"[{self.LANE}] ACTIVE_ORDERS_ERR {exc}")
            return -1

    def _scrub_orphans(self, now: float, why: str) -> bool:
        """Cancel-all when venue has leftover orders. Returns True if scrubbed."""
        n = self._count_active_orders()
        if n <= 0:
            return False
        _log(f"[{self.LANE}] ORPHAN_SCRUB n={n} ({why})")
        try:
            if self.real:
                self.client.cancel_all_child_orders(PRODUCT)
                self.last_api_ts = now
        except Exception as exc:
            self.api_errors += 1
            _log(f"[{self.LANE}] ORPHAN_SCRUB_ERR {exc}")
        self._clear_working()
        self.cancel_pending = True
        self.cancels += 1
        _append(
            AUDIT_PATH,
            {"event": "ORPHAN_SCRUB", "ts": _iso(), "n": n, "why": why, "real": self.real},
        )
        return True

    def _post(self, side: str, px: float, why: str, now: float) -> None:
        """Single-order invariant (CSR-694b): never stack venue child orders.

        If a working order exists (any side/price), CANCEL it and return this tick.
        Next tick may POST only when working is clear and inventory gates pass.
        """
        side_u = side.upper()
        if side_u not in ("BUY", "SELL"):
            return
        if self.cancel_pending:
            self.blocked_posts += 1
            return

        # Same quote already working — keep it.
        if (
            self.working_id
            and self.working_side == side_u
            and abs(self.working_px - float(px)) <= 1e-9
        ):
            return

        # ANY existing working order: cancel first, never POST beside it.
        if self.working_id:
            self._cancel("replace_wait", now)
            self.blocked_posts += 1
            return

        if side_u == "BUY":
            if not self._inventory_ok_for_buy():
                self.blocked_posts += 1
                return
        else:  # SELL
            if not self._is_long() or self.confirmed_net < self.size - 1e-12:
                self.blocked_posts += 1
                return
            if self.confirmed_net > MAX_INV_BTC + 1e-12:
                # oversize: still allow reduce-only SELL of one lot
                pass

        if not self._can_api(now):
            return

        # REAL: refuse POST while venue still has ACTIVE orphans.
        if self.real:
            n_act = self._count_active_orders()
            if n_act < 0:
                self.blocked_posts += 1
                return
            if n_act > 0:
                self._scrub_orphans(now, f"pre_post_{side_u}")
                self.blocked_posts += 1
                return

        px_i = int(round(px))
        if px_i <= 0:
            return
        oid = ""
        try:
            if self.real:
                res = self.client.send_order(
                    PRODUCT, side_u, self.size, "LIMIT", price=px_i
                )
                oid = str((res or {}).get("child_order_acceptance_id") or "")
                if not oid:
                    self.rejects += 1
                    _append(
                        AUDIT_PATH,
                        {"event": "ORDER_REJECT", "ts": _iso(), "data": res, "why": why},
                    )
                    _log(f"[{self.LANE}] REJECT {side_u} @ {px_i} ({why})")
                    return
            else:
                oid = f"SIM-{side_u}-{int(time.time() * 1000)}"
        except OrderSubmitUnknownError as exc:
            self.api_errors += 1
            self.cancel_pending = True  # unknown — do not double-POST
            _append(AUDIT_PATH, {"event": "ORDER_UNKNOWN", "ts": _iso(), "err": str(exc)})
            _log(f"[{self.LANE}] ORDER_UNKNOWN {exc}")
            return
        except Exception as exc:
            self.api_errors += 1
            self.rejects += 1
            _append(AUDIT_PATH, {"event": "ORDER_ERROR", "ts": _iso(), "err": str(exc)})
            _log(f"[{self.LANE}] ORDER_ERROR {exc}")
            return

        self.last_api_ts = now
        self.working_side = side_u
        self.working_px = float(px_i)
        self.working_id = oid
        self.working_why = why
        self.cancel_pending = False
        self.posts += 1
        _append(
            AUDIT_PATH,
            {
                "event": "ORDER_POSTED",
                "ts": _iso(),
                "side": side_u,
                "px": px_i,
                "id": oid,
                "why": why,
                "real": self.real,
            },
        )
        mode = "REAL" if self.real else "DRY"
        _log(f"[{self.LANE}] POST[{mode}] {side_u} @ {px_i:,.0f} ({why}) id={oid}")

    def _cancel(self, why: str, now: float) -> None:
        if not self.working_id:
            return
        oid = self.working_id
        side = self.working_side
        if self.real:
            try:
                self.client.cancel_child_order(PRODUCT, oid)
                self.last_api_ts = now
            except Exception as exc:
                self.api_errors += 1
                _log(f"[{self.LANE}] CANCEL_ERR {exc}")
                # Still mark pending — do not POST another beside a live OID.
        _log(f"[{self.LANE}] CANCEL {side} ({why}) id={oid}")
        self.cancels += 1
        _append(
            AUDIT_PATH,
            {
                "event": "ORDER_CANCEL",
                "ts": _iso(),
                "side": side,
                "id": oid,
                "why": why,
            },
        )
        self._clear_working()
        # REAL: block new POST until venue poll shows no ACTIVE / inventory sync.
        self.cancel_pending = bool(self.real)

    def _on_fill(self, side: str, px: float, src: str, now: float) -> None:
        side_u = side.upper()
        self.fills += 1
        _log(f"[{self.LANE}] FILL {side_u} @ {px:,.0f} src={src}")
        _append(
            AUDIT_PATH,
            {
                "event": "FILL",
                "ts": _iso(),
                "side": side_u,
                "px": px,
                "src": src,
                "real": self.real,
            },
        )
        self._clear_working()
        if side_u == "BUY" and self._is_flat():
            self.confirmed_net = self.size
            self.entry_px = float(px)
            self.entry_ts = now
            if not self.real:
                self.venue_net = self.size
                self.venue_avg = float(px)
            return
        if side_u == "SELL" and self._is_long():
            entry = float(self.entry_px or px)
            pnl_bp = ((px / entry) - 1.0) * 1e4 if entry > 0 else 0.0
            pnl_j = pnl_bp * entry * self.size / 1e4 if entry > 0 else 0.0
            tag = self.pending_exit_tag or "EXIT"
            reason = self.pending_exit_reason or src
            self.pending_exit_tag = ""
            self.pending_exit_reason = ""
            row = {
                "ts": now,
                "lane": self.LANE,
                "side": "buy",
                "exit_side": tag.lower(),
                "entry_px": entry,
                "fill_price": px,
                "pnl_bp": round(pnl_bp, 2),
                "pnl_jpy": round(pnl_j, 1),
                "is_win": pnl_bp > 0,
                "exit_reason": f"{tag} ({reason})",
                "fill_src": src,
                "real": self.real,
            }
            _append(LEDGER_PATH, row)
            self.history.append(row)
            self.trades += 1
            if pnl_bp > 0:
                self.wins += 1
            self.pnl_bp += pnl_bp
            self.pnl_jpy += pnl_j
            icon = "💰" if pnl_bp > 0 else "⚡"
            _log(
                f"[{self.LANE}] {icon} EXIT [{tag}] pnl={pnl_bp:+.2f}bp "
                f"({pnl_j:+.1f}円) src={src}"
            )
            self.confirmed_net = 0.0
            self.entry_px = 0.0
            self.entry_ts = 0.0
            if not self.real:
                self.venue_net = 0.0
                self.venue_avg = 0.0
            if self.max_fills > 0 and self.fills >= self.max_fills:
                self.request_stop(f"max_fills={self.max_fills}")

    def _maybe_fill(self, execs: List[Dict[str, Any]], bid: float, ask: float, now: float) -> None:
        if not self.working_side or not self.working_id:
            return
        hit = public_maker_fill(
            side=self.working_side,
            price=self.working_px,
            acceptance_id=self.working_id,
            public_execs=execs,
            best_bid=bid,
            best_ask=ask,
            allow_book=True,
        )
        if not hit:
            return
        src, _sz = hit
        if self.real and src in OPTIMISTIC_FILL_SOURCES:
            # Venue truth via position poll; optimistic board only arms observe.
            return
        self._on_fill(self.working_side, self.working_px, src, now)

    def _reconcile_venue(self, now: float) -> None:
        if not self.real:
            # DRY: clear cancel_pending immediately (no venue orphans).
            if self.cancel_pending and not self.working_id:
                self.cancel_pending = False
            return
        need_fast = bool(
            self.working_id
            or self.cancel_pending
            or abs(self.confirmed_net) > EXACT_FLAT_EPS
            or abs(self.venue_net) > EXACT_FLAT_EPS
        )
        interval = POS_POLL_ACTIVE_SEC if need_fast else POS_POLL_SEC
        if (now - self.last_pos_poll) < interval and self.last_pos_poll > 0:
            return
        self.last_pos_poll = now
        try:
            rows = self.client.get_positions(PRODUCT) or []
            net, avg = _parse_venue_net(rows)
        except Exception as exc:
            self.api_errors += 1
            _log(f"[{self.LANE}] POS_POLL_ERR {exc}")
            return
        prev = self.confirmed_net
        self.venue_net = float(net)
        self.venue_avg = float(avg)
        self.confirmed_net = float(net)

        # Oversize / short = halt new entries (CSR-694b).
        if self.confirmed_net > MAX_INV_BTC + 1e-12:
            if not self.halt_new_entries:
                self.oversize_halts += 1
                self.halt_new_entries = True
                _log(
                    f"[{self.LANE}] HALT_OVERSIZE net={self.confirmed_net:+.6f} "
                    f"> max={MAX_INV_BTC}"
                )
            self._scrub_orphans(now, "oversize")
        elif self.confirmed_net < -EXACT_FLAT_EPS:
            if not self.halt_new_entries:
                self.oversize_halts += 1
                self.halt_new_entries = True
                _log(f"[{self.LANE}] HALT_SHORT net={self.confirmed_net:+.6f}")
            self._scrub_orphans(now, "short")
        elif (
            self.halt_new_entries
            and abs(self.confirmed_net) <= EXACT_FLAT_EPS
            and self._count_active_orders() == 0
        ):
            self.halt_new_entries = False
            _log(f"[{self.LANE}] HALT_CLEAR flat+no_active")

        # Release cancel_pending once venue has no ACTIVE and we know inventory.
        if self.cancel_pending and not self.working_id:
            n_act = self._count_active_orders()
            if n_act == 0:
                self.cancel_pending = False

        if abs(prev) <= EXACT_FLAT_EPS and self.confirmed_net >= self.size - 1e-12:
            # Cap adopt to one lot mentally; oversize already halted above.
            self.entry_px = float(avg or self.entry_px or 0.0)
            self.entry_ts = now
            self._clear_working()
            self.cancel_pending = False
            _log(f"[{self.LANE}] RECONCILE adopt long net={net:+.4f} @ {avg:.0f}")
        elif prev >= self.size - 1e-12 and abs(self.confirmed_net) <= EXACT_FLAT_EPS:
            if self.entry_px > 0 and avg <= 0:
                px = float(self.working_px or self.entry_px)
                self._on_fill("SELL", px, "VENUE_FLAT", now)
            else:
                self.confirmed_net = 0.0
                self.entry_px = 0.0
                self._clear_working()
                self.cancel_pending = False
                _log(f"[{self.LANE}] RECONCILE flat")

    def flatten(self, reason: str = "") -> Dict[str, Any]:
        now = time.time()
        if self.working_id:
            self._cancel(f"flatten:{reason}", now)
        if self.real and self._is_long():
            bids, asks = self._depth()
            mid = 0.0
            if bids and asks:
                mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2.0
            px, desc = calculate_optimal_order_price(
                bids, asks, "sell", 5, 0.05, 1.0, mid
            )
            if px <= 0 and asks:
                px = float(asks[0]["price"])
                desc = "TOP_ASK_FALLBACK"
            if px > 0:
                self.pending_exit_tag = "FLATTEN"
                self.pending_exit_reason = f"{reason} [{desc}]"
                self._post("SELL", px, f"FLATTEN {reason} [{desc}]", now)
        return {
            "flat_ok": self._is_flat() and not self.working_id,
            "confirmed_net": self.confirmed_net,
            "reason": reason,
        }

    def metrics(self) -> Dict[str, Any]:
        n = self.trades
        return {
            "ts_jst": _iso(),
            "model": self.LANE,
            "mode": "LIVE" if self.real else "DRY_RUN",
            "wire": "YES" if self.real else "NO",
            "fill": "BOARD_IMITATION",
            "strategy": "ms1_ev_hold_maker",
            "size": self.size,
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "pnl_bp": round(self.pnl_bp, 2),
            "pnl_jpy": round(self.pnl_jpy, 1),
            "ev_bp": round(self.pnl_bp / n, 4) if n else 0.0,
            "win_rate_pct": round(100.0 * self.wins / n, 1) if n else 0.0,
            "total_trades": n,
            "confirmed_net": round(self.confirmed_net, 8),
            "venue_net": round(self.venue_net, 8),
            "entry_px": self.entry_px,
            "working": self.working_side,
            "working_px": self.working_px,
            "posts": self.posts,
            "cancels": self.cancels,
            "fills": self.fills,
            "rejects": self.rejects,
            "api_errors": self.api_errors,
            "blocked_posts": self.blocked_posts,
            "halt_new_entries": self.halt_new_entries,
            "cancel_pending": bool(self.cancel_pending),
            "oversize_halts": int(getattr(self, "oversize_halts", 0)),
            "max_inv": MAX_INV_BTC,
            "economic_pass": False,
            "csr": "CSR-694b",
        }

    def save_state(self) -> None:
        self.last_state_save = time.time()
        _write_json(STATE_PATH, self.metrics())

    def send_discord(self, kind: str = "hourly") -> None:
        m = self.metrics()
        color = 0x2ECC71 if float(m["pnl_bp"]) >= 0 else 0xE67E22
        title = {
            "start": f"🚀 [START] {self.LANE} ({m['mode']})",
            "hourly": f"⏱️ [HOURLY] {self.LANE} {m['mode']}",
            "final": f"🏁 [FINAL] {self.LANE} stop={self.stop_reason or '-'}",
        }.get(kind, f"📊 {self.LANE} {kind}")
        payload = {
            "username": self.LANE,
            "embeds": [
                {
                    "title": title,
                    "description": (
                        f"**MS1** micro-scalp · Maker EV_GONE · LIMIT · lot=`{self.size}`\n"
                        f"mode=`{m['mode']}` · wire=`{m['wire']}` · Economic_PASS=`NO` · CSR-694"
                    ),
                    "color": color,
                    "fields": [
                        {
                            "name": "PnL",
                            "value": (
                                f"`{m['pnl_bp']:+.2f}bp` / `{m['pnl_jpy']:+.1f}円` · "
                                f"n={m['total_trades']} · EV `{m['ev_bp']:+.2f}` · "
                                f"WR `{m['win_rate_pct']:.1f}%`"
                            ),
                            "inline": False,
                        },
                        {
                            "name": "Pos / quote",
                            "value": (
                                f"net=`{m['confirmed_net']:+.4f}` · "
                                f"working=`{m['working'] or 'FLAT'}` @ `{m['working_px']}` · "
                                f"posts={m['posts']} fills={m['fills']}"
                            ),
                            "inline": False,
                        },
                    ],
                    "footer": {"text": f"Antigravity · {m['ts_jst']}"},
                }
            ],
        }
        try:
            post_live_discord(payload)
            _log(f"[{self.LANE}] discord {kind} → {live_discord_webhook_url()[:48]}…")
        except Exception as exc:
            _log(f"[{self.LANE}] discord err: {exc}")

    def _tick(self, snap: Any, now: float) -> None:
        bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0:
            return
        mid = float(getattr(snap, "mid_price", 0.0) or 0.0) or (bid + ask) / 2.0
        spread_bp = ((ask - bid) / bid) * 1e4 if bid > 0 else 0.0
        bids, asks = self._depth()
        eng = self.engine.update_and_evaluate(bids, asks, bid, ask, now=now) or {}
        m = {
            "spread_bp": spread_bp,
            "wobi": float(eng.get("wobi") or 0.0),
            "z_score": float(eng.get("z_score") or 0.0),
            "micro_dev_bp": float(eng.get("micro_dev_bp") or 0.0),
            "d_wobi_dt": float(eng.get("d_wobi_dt") or 0.0),
            "recent_ofi": float(eng.get("recent_ofi") or 0.0),
            "ask_depletion_vel": float(eng.get("ask_depletion_vel") or 0.0),
            "mid": mid,
        }
        execs = self._poll_execs()
        self._maybe_fill(execs, bid, ask, now)
        self._reconcile_venue(now)

        # While cancel/replace in flight, do not plan new posts this tick.
        if self.cancel_pending:
            return

        if self._is_long():
            if self._live_long_ev(m["wobi"], m["recent_ofi"]):
                if self.working_side == "SELL":
                    self._cancel("ev_hold", now)
                return
            # EV_GONE → wall-anchor Maker SELL (replace = cancel-wait, not stack)
            px, desc = calculate_optimal_order_price(
                bids, asks, "sell", 5, 0.05, 1.0, mid
            )
            if px <= 0:
                return
            self.pending_exit_tag = "EV_GONE"
            self.pending_exit_reason = (
                f"ev_gone (wobi={m['wobi']:+.2f} ofi={m['recent_ofi']:+.2f}) [{desc}]"
            )
            self._post("SELL", px, f"EXIT EV_GONE {self.pending_exit_reason}", now)
            return

        # Flat — cancel stray exit quotes
        if self.working_side == "SELL":
            self._cancel("flat_drop_exit", now)
            return

        if self.halt_new_entries:
            return
        ok, gate_why = self._entry_ok(m)
        if not ok:
            if self.working_side == "BUY":
                self._cancel(f"gate_fail:{gate_why}", now)
            return
        px, desc = calculate_optimal_order_price(
            bids, asks, "buy", 5, 0.05, 1.0, mid
        )
        if px <= 0:
            return
        self._post("BUY", px, f"ENTER {gate_why} [{desc}]", now)

    def run(self) -> int:
        mode = "REAL" if self.real else "DRY_RUN"
        _log(
            f"=== {self.LANE} START mode={mode} size={self.size} "
            f"source={self.market_source} CSR-694 ==="
        )
        try:
            self.feed.start()
        except Exception as exc:
            _log(f"[{self.LANE}] feed.start: {exc}")
        self.save_state()
        try:
            self.send_discord("start")
        except Exception:
            pass
        last_snap = None
        try:
            while self.running:
                if STOP_FLAG.exists():
                    self.request_stop("operator_stop_flag")
                    break
                if self.max_runtime_sec > 0 and (time.time() - self.t0) >= self.max_runtime_sec:
                    self.request_stop(f"max_runtime={self.max_runtime_sec}")
                    break
                m = self.metrics()
                if m["pnl_bp"] <= STOP_PNL_BP:
                    self.request_stop(f"pnl_stop {m['pnl_bp']:.1f}bp")
                    break
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
                try:
                    self._tick(last_snap, time.time())
                except Exception as exc:
                    _log(f"[{self.LANE}] tick exception: {exc}")
                if time.time() - self.last_state_save >= 10.0:
                    self.save_state()
                jst_now = datetime.now(JST)
                if jst_now.hour != self.last_reported_hour and jst_now.minute >= 7:
                    self.send_discord("hourly")
                    self.last_reported_hour = jst_now.hour
                time.sleep(self.interval_sec)
        except KeyboardInterrupt:
            self.request_stop("SIGINT")
        finally:
            flat = self.flatten(self.stop_reason or "shutdown")
            self.save_state()
            try:
                self.send_discord("final")
            except Exception:
                pass
            try:
                self.feed.stop()
            except Exception:
                pass
            if self.exec_fp:
                self.exec_fp.close()
            PID_PATH.unlink(missing_ok=True)
            _log(
                f"=== {self.LANE} STOPPED reason={self.stop_reason} "
                f"flat={flat.get('flat_ok')} ==="
            )
        return 0 if flat.get("flat_ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MS1_WOBI LIVE (CSR-694 · greenfield · LIMIT BUY-only)"
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    parser.add_argument("--size", type=float, default=SIZE_BTC)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--confirm-go", default="")
    parser.add_argument("--max-runtime-sec", type=float, default=0.0)
    parser.add_argument("--max-fills", type=int, default=0)
    args = parser.parse_args()

    bad = refuse_concurrent()
    if bad:
        print(bad, file=sys.stderr)
        return 2
    bad = refuse_size(args.size)
    if bad:
        print(bad, file=sys.stderr)
        return 2
    if args.real:
        reason = refuse_real_gates(str(args.confirm_go or ""))
        if reason:
            print(reason, file=sys.stderr)
            return 0
    else:
        os.environ["ENABLE_REAL_TRADING"] = "false"

    runner = MS1WobiLive(
        real=bool(args.real),
        size=float(args.size),
        interval_sec=float(args.interval),
        report_interval_sec=float(args.report_interval),
        market_source=args.source,
        max_runtime_sec=float(args.max_runtime_sec or 0.0),
        max_fills=int(args.max_fills or 0),
    )

    def _sig(signum, _frame):
        runner.request_stop(f"signal_{signum}")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
