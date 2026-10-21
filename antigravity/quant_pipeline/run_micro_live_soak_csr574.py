#!/usr/bin/env python3
"""CSR-574 — Sequential micro LIVE soak (0.001 BTC).

Order: TF2BP → SpreadCaptureMM → MicroSpreadMM
Default: DRY_RUN (paper). Real orders require ALL of:
  - --real
  - ENABLE_REAL_TRADING=true already in environment
  - --confirm-go MICRO574
Never writes ENABLE_REAL_TRADING into .env.

Trade / runtime caps:
  --max-trades 0      → unlimited closed trades
  --max-runtime-sec 0 → no wall-clock stop (stop via signal / daily loss)
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

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

from core.bitflyer_client import BitFlyerClient, OrderSubmitUnknownError  # noqa: E402
from execution.paper_trader import LivePaperTrader  # noqa: E402
from antigravity.strategies.tf2bp_strategy import TF2BPStrategy  # noqa: E402
from antigravity.quant_pipeline.tf2bp_live_lock import (  # noqa: E402
    ExclusiveRunnerLock,
    RunnerLockBusy,
)
from antigravity.quant_pipeline.tf2bp_live_state_machine import (  # noqa: E402
    LiveState,
    OrderErrorClass,
    OrderLookupResult,
    TF2BPLiveStateMachine,
    VenueSnapshot,
    classify_order_error,
)
from antigravity.quant_pipeline.tf2bp_exec_lanes import (  # noqa: E402
    PEG2B_NEEDLE,
    assert_taker_market,
)
from antigravity.quant_pipeline.feeder_board_source import (  # noqa: E402
    DEFAULT_LATEST,
    read_feeder_board,
)
from antigravity.quant_pipeline.live_latency_probe import (  # noqa: E402
    LiveLatencyProbe,
    LiveSysMetricsSampler,
    mono_ns,
    set_order_ack_hook,
    wall_ns,
)

JST = timezone(timedelta(hours=9))
CAPS_PATH = AG / "configs" / "live_prep_caps_csr573.json"
LEDGER = AG / "data" / "mm_research" / "micro_live_soak_csr574.jsonl"
HB_PATH = AG / "data" / "tf2bp_live_heartbeat.json"
SM_AUDIT = AG / "data" / "mm_research" / "tf2bp_live_sm_audit.jsonl"
RUNNER_LOCK = AG / "data" / "tf2bp_live_runner.lock"
STOP_FLAG = AG / "data" / "TF2BP_LIVE_STOP.flag"
ENTRY_HALT_FLAG = AG / "data" / "TF2BP_ENTRY_HALT.flag"
RETEST_WEBHOOK = AG / "data" / "tf2bp_retest_webhook.url"
GO2_MARK = AG / "data" / "GO2_TF2BP.flag"
CONSECUTIVE_429_HALT = 5
FEEDER_LATEST = DEFAULT_LATEST
FEEDER_MAX_AGE_MS = 5000.0


def note_consecutive_429(
    prev: int,
    *,
    is_429: bool,
    threshold: int = CONSECUTIVE_429_HALT,
) -> Tuple[int, bool]:
    """Board-fetch 429 counter. Success resets. Never used as BUY/STALL signal."""
    if not is_429:
        return 0, False
    n = int(prev) + 1
    return n, n >= int(threshold)

STRATS = [
    {
        "id": "TF2BP_v1",
        "kind": "tf2bp",
        "name": "TF2BP baseline",
    },
    {
        "id": "strat_cbcd5aed",
        "kind": "custom",
        "name": "SpreadCaptureMM",
        "path": str(AG / "strategies" / "approved" / "strat_cbcd5aed_approved.py"),
    },
    {
        "id": "strat_4d3f2c9f",
        "kind": "custom",
        "name": "MicroSpreadMM",
        "path": str(AG / "strategies" / "approved" / "strat_4d3f2c9f_approved.py"),
    },
]


def _jst() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


_RUNNER_NEEDLE = "antigravity.quant_pipeline.run_micro_live_soak_csr574"


def _send_taker_market(client: Any, side: str, size: float) -> Any:
    """TF2BP soak is taker-only. LIMIT is a lane violation (CSR-615)."""
    assert_taker_market("MARKET")
    return client.send_order("FX_BTC_JPY", side, size, "MARKET")


def _foreign_peg2b_pids() -> List[int]:
    me = os.getpid()
    found: List[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        if PEG2B_NEEDLE in cmd and "python" in cmd:
            found.append(pid)
    return found


def _foreign_runner_pids() -> List[int]:
    """Other soak-runner processes. Pre-v1.1 runners do not hold the flock."""
    me = os.getpid()
    found: List[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        if _RUNNER_NEEDLE in cmd:
            found.append(pid)
    return found


def _refuse_old_runner(pids: List[int]) -> Dict[str, Any]:
    print(f"[TF2BP] REFUSE start: old runner still active pids={pids}", flush=True)
    return {
        "id": "TF2BP_v1",
        "closed_trades": 0,
        "pnl_jpy": 0.0,
        "trades": [],
        "real": False,
        "stop_reason": "old_runner_still_active",
        "sm_state": "BOOT",
        "run_id": None,
        "venue_position": None,
        "lock_acquired": False,
    }


def _retest_set() -> int:
    try:
        for ln in GO2_MARK.read_text(encoding="utf-8").splitlines():
            if ln.startswith("set="):
                return max(1, int(ln.split("=", 1)[1]))
    except Exception:
        pass
    return 1


def _retest_discord(content: str) -> None:
    """Post to operator webhook. Never logs the URL."""
    try:
        url = RETEST_WEBHOOK.read_text(encoding="utf-8").strip()
    except Exception:
        return
    if not url.startswith("https://discord.com/api/webhooks/"):
        return
    body = json.dumps({"content": content[:1900]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "TF2BP-Retest/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
    except Exception as exc:
        print(f"[TF2BP] discord warn: {exc}", flush=True)


def _load_caps() -> Dict[str, Any]:
    if CAPS_PATH.exists():
        return json.loads(CAPS_PATH.read_text(encoding="utf-8"))
    return {}


def _append_ledger(row: Dict[str, Any]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_feeder_board() -> Optional[Dict[str, Any]]:
    """Primary board: shared_feeder IPC. None → REST fallback (CSR-616)."""
    return read_feeder_board(FEEDER_LATEST, max_stale_ms=FEEDER_MAX_AGE_MS)


def _fetch_market(
    product: str, marks: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    feeder = _read_feeder_board()
    if feeder:
        if marks is not None:
            now = mono_ns()
            marks.setdefault("t0_ns", now)
            marks.setdefault("t1_ns", now)
            marks["feed_source"] = "shared_feeder"
        return feeder, {"buy": 0.0, "sell": 0.0, "newest_exec_ts": None}, "shared_feeder"
    board = _fetch_board(product, marks=marks)
    vols = _fetch_exec_vols(product, marks=marks)
    if marks is not None:
        marks["feed_source"] = "rest_fallback"
    return board, vols, "rest_fallback"


def _fetch_board(product: str, marks: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Public board fetch. Optional `marks` records T0/T1 without changing URL/timeout."""
    url = f"https://api.bitflyer.com/v1/board?product_code={product}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        raw = resp.read()
        if marks is not None:
            marks["t0_ns"] = mono_ns()
            marks["t0_wall_ns"] = wall_ns()
        text = raw.decode()
        data = json.loads(text)
        if marks is not None:
            marks["t1_ns"] = mono_ns()
            marks["board_bytes"] = len(raw)
        return data


def _fetch_exec_vols(
    product: str, marks: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Public executions fetch. Returns buy/sell + optional newest exec_date."""
    url = f"https://api.bitflyer.com/v1/executions?product_code={product}&count=50"
    buy = sell = 0.0
    newest_exec_ts: Optional[str] = None
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            raw = resp.read()
            if marks is not None and marks.get("t0_ns") is None:
                marks["t0_ns"] = mono_ns()
                marks["t0_wall_ns"] = wall_ns()
            rows = json.loads(raw.decode())
            if marks is not None and marks.get("t1_ns") is None:
                marks["t1_ns"] = mono_ns()
        for r in rows or []:
            sz = float(r.get("size") or 0)
            if (r.get("side") or "").upper() == "BUY":
                buy += sz
            else:
                sell += sz
            ed = r.get("exec_date")
            if ed and (newest_exec_ts is None or str(ed) > newest_exec_ts):
                newest_exec_ts = str(ed)
    except Exception as exc:
        if marks is not None:
            marks["exec_error"] = str(exc)
        return {"buy": buy, "sell": sell, "newest_exec_ts": None}
    return {"buy": buy, "sell": sell, "newest_exec_ts": newest_exec_ts}


def _parse_venue_net(rows: List[Dict[str, Any]]) -> Tuple[float, float]:
    """BUY positive / SELL negative → (net_size, avg_entry)."""
    net = 0.0
    notional = 0.0
    for p in rows or []:
        side = (p.get("side") or "").upper()
        sz = float(p.get("size") or 0.0)
        px = float(p.get("price") or 0.0)
        if side == "BUY":
            net += sz
            notional += sz * px
        elif side == "SELL":
            net -= sz
            notional -= sz * px
    avg = (notional / net) if abs(net) > 1e-12 else 0.0
    return net, avg


def _fetch_venue_snapshot(
    client: BitFlyerClient,
    *,
    real: bool,
    paper_net: float = 0.0,
    paper_entry: float = 0.0,
    product: str = "FX_BTC_JPY",
) -> VenueSnapshot:
    """Exchange SoT. Failure → unknown (never invent flat)."""
    if not real:
        return VenueSnapshot(
            net=float(paper_net),
            avg_entry=float(paper_entry),
            open_orders_known=True,
            open_order_count=0,
            ok=True,
        )
    try:
        rows = client.get_positions(product) or []
        net, avg = _parse_venue_net(rows)
    except Exception as exc:
        print(f"[TF2BP] venue position sync failed: {exc}", flush=True)
        return VenueSnapshot(net=None, ok=False, open_orders_known=False, error=str(exc))
    try:
        orders = client.get_child_orders(product, child_order_state="ACTIVE") or []
        n_open = len(orders)
        open_known = True
    except Exception as exc:
        print(f"[TF2BP] open orders sync failed: {exc}", flush=True)
        return VenueSnapshot(
            net=net,
            avg_entry=avg,
            ok=True,
            open_orders_known=False,
            open_order_count=0,
            error=str(exc),
        )
    return VenueSnapshot(
        net=net,
        avg_entry=avg,
        open_orders_known=open_known,
        open_order_count=n_open,
        ok=True,
    )


def _write_pos_alert(kind: str, detail: Dict[str, Any]) -> None:
    path = AG / "data" / "TF2BP_POS_ALERT.json"
    row = {"ts_jst": _jst(), "kind": kind, **detail}
    path.write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[TF2BP] POS_ALERT {kind} → {path} {row}", flush=True)


def _lookup_child_order(
    client: BitFlyerClient,
    *,
    acceptance_id: Optional[str],
    real: bool,
) -> OrderLookupResult:
    """GET-only order inquiry. Never implies re-POST."""
    if not acceptance_id:
        return OrderLookupResult(known=False, detail={"reason": "no_acceptance_id"})
    if not real:
        return OrderLookupResult(
            known=True,
            rejected_or_absent=True,
            acceptance_id=acceptance_id,
            detail={"paper": True},
        )
    found_any = False
    filled = False
    rejected = False
    detail: Dict[str, Any] = {"states_seen": []}
    for state in ("ACTIVE", "COMPLETED", "CANCELED", "REJECTED", "EXPIRED"):
        try:
            rows = client.get_child_orders(
                "FX_BTC_JPY",
                child_order_state=state,
                child_order_acceptance_id=acceptance_id,
            ) or []
        except Exception as exc:
            return OrderLookupResult(
                known=False,
                acceptance_id=acceptance_id,
                detail={"lookup_error": str(exc), "state": state},
            )
        if not rows:
            continue
        found_any = True
        detail["states_seen"].append(state)
        if state == "COMPLETED":
            filled = True
        if state in ("CANCELED", "REJECTED", "EXPIRED"):
            rejected = True
        if state == "ACTIVE":
            # still working — not decisive fill/reject
            pass
    if not found_any:
        # Absent across states → treat as rejected/absent for Case B (with venue flat)
        return OrderLookupResult(
            known=True,
            rejected_or_absent=True,
            filled=False,
            acceptance_id=acceptance_id,
            detail={"reason": "not_found_any_state"},
        )
    return OrderLookupResult(
        known=True,
        filled=filled,
        rejected_or_absent=rejected and not filled,
        acceptance_id=acceptance_id,
        detail=detail,
    )


def run_tf2bp(
    *,
    size: float,
    max_trades: int,
    real: bool,
    interval: float,
    max_runtime_sec: float,
    daily_loss_jpy: float,
) -> Dict[str, Any]:
    """TF2BP LIVE loop driven by State Machine v1.1 (lifecycle only; alpha unchanged)."""
    # H1: flock does not cover the pre-v1.1 runner. Process absence is required
    # before the lock, and again before BOOT→RECONCILING.
    others = _foreign_runner_pids()
    if others:
        return _refuse_old_runner(others)
    peg2b = _foreign_peg2b_pids()
    if peg2b:
        print(f"[TF2BP] REFUSE start: PEG2-B maker lane active pids={peg2b}", flush=True)
        return {
            "id": "TF2BP_v1",
            "closed_trades": 0,
            "pnl_jpy": 0.0,
            "trades": [],
            "real": real,
            "stop_reason": "peg2b_lane_active",
            "sm_state": "BOOT",
            "run_id": None,
            "venue_position": None,
            "lock_acquired": False,
        }

    # P0-1: exclusive lock BEFORE any strategy/orders (atomic flock, not check-then-act)
    run_id_pre = f"pre{os.getpid()}"
    runner_lock = ExclusiveRunnerLock(path=RUNNER_LOCK)
    try:
        lock_info = runner_lock.acquire(run_id=run_id_pre, blocking=False)
    except RunnerLockBusy as exc:
        print(f"[TF2BP] REFUSE start: {exc}", flush=True)
        return {
            "id": "TF2BP_v1",
            "closed_trades": 0,
            "pnl_jpy": 0.0,
            "trades": [],
            "real": real,
            "stop_reason": "runner_lock_busy",
            "sm_state": "BOOT",
            "run_id": None,
            "venue_position": None,
            "lock_acquired": False,
        }

    client = BitFlyerClient(enable_real_trading=real, max_order_size=max(size, 0.05))
    strat = TF2BPStrategy({"order_size_btc": size})
    sm = TF2BPLiveStateMachine(
        max_size=size,
        heartbeat_path=HB_PATH,
        audit_path=SM_AUDIT,
        entry_halt_flag_path=ENTRY_HALT_FLAG,
        pid=os.getpid(),
    )
    # refresh lock metadata with real run_id
    try:
        if runner_lock._fd is not None:
            runner_lock._fd.seek(0)
            runner_lock._fd.truncate()
            runner_lock._fd.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "run_id": sm.run_id,
                        "acquired_ts": lock_info.acquired_ts,
                        "ts_jst": _jst(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            runner_lock._fd.flush()
            os.fsync(runner_lock._fd.fileno())
    except Exception as exc:
        print(f"[TF2BP] lock meta refresh failed: {exc}", flush=True)

    others = _foreign_runner_pids()
    if others:
        runner_lock.release()
        print(f"[TF2BP] exclusive lock released; old runner appeared pids={others}", flush=True)
        return _refuse_old_runner(others)

    closed = 0
    entry_ts = 0.0
    pnl_sum = 0.0
    trades: List[Dict[str, Any]] = []
    t0 = time.time()
    last_hb_log = 0.0
    last_audit = 0.0
    trade_cap = None if max_trades <= 0 else int(max_trades)
    runtime_cap = None if max_runtime_sec <= 0 else float(max_runtime_sec)
    stop_flag = STOP_FLAG
    stop_reason = ""
    shutting_down = False
    paper_net = 0.0
    paper_entry = 0.0

    print(
        f"[TF2BP] start real={real} size={size} max_trades="
        f"{'unlimited' if trade_cap is None else trade_cap} "
        f"runtime={'unlimited' if runtime_cap is None else runtime_cap} "
        f"sm_run_id={sm.run_id} pid={sm.pid} state={sm.state.value} lock=HELD",
        flush=True,
    )
    if real:
        _retest_discord(
            f"TF2BP SET-{_retest_set()} 開始 size={size} max_trades="
            f"{'unlimited' if trade_cap is None else trade_cap} run_id={sm.run_id}"
        )

    def _request_shutdown(reason: str) -> None:
        nonlocal stop_reason, shutting_down
        stop_reason = reason
        shutting_down = True
        print(f"[TF2BP] shutdown requested reason={reason}", flush=True)

    def _on_signal(signum, _frame) -> None:
        _request_shutdown(f"signal_{signum}")

    if real:
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

    try:
      # BOOT → RECONCILING → READY_FLAT | LONG | SAFE_HALT
      # Absolute ban: start → position=0 → strategy → BUY
      return _run_tf2bp_body(
          sm=sm,
          client=client,
          strat=strat,
          size=size,
          real=real,
          interval=interval,
          trade_cap=trade_cap,
          runtime_cap=runtime_cap,
          daily_loss_jpy=daily_loss_jpy,
          t0=t0,
          stop_flag=stop_flag,
          get_shutting_down=lambda: shutting_down,
          get_stop_reason=lambda: stop_reason,
          set_shutdown=_request_shutdown,
      )
    finally:
        runner_lock.release()
        print(f"[TF2BP] exclusive lock released run_id={sm.run_id}", flush=True)


def _run_tf2bp_body(
    *,
    sm: TF2BPLiveStateMachine,
    client: BitFlyerClient,
    strat: TF2BPStrategy,
    size: float,
    real: bool,
    interval: float,
    trade_cap: Optional[int],
    runtime_cap: Optional[float],
    daily_loss_jpy: float,
    t0: float,
    stop_flag: Path,
    get_shutting_down,
    get_stop_reason,
    set_shutdown,
) -> Dict[str, Any]:
    closed = 0
    entry_ts = 0.0
    pnl_sum = 0.0
    trades: List[Dict[str, Any]] = []
    last_hb_log = 0.0
    last_audit = 0.0
    paper_net = 0.0
    paper_entry = 0.0
    stop_reason = ""
    consecutive_429 = 0
    # Phase-0 latency baseline (async only; does not alter trading logic)
    lat_probe = LiveLatencyProbe(enabled=True)
    sys_sampler = LiveSysMetricsSampler(interval_sec=2.0, enabled=True)
    order_ack_box: Dict[str, Any] = {}

    def _on_order_ack(payload: Dict[str, Any]) -> None:
        order_ack_box.clear()
        order_ack_box.update(payload)

    set_order_ack_hook(_on_order_ack)
    prev_mid: Optional[float] = None
    prev_board_fp: Optional[int] = None

    sm.begin_reconcile()
    boot_snap = _fetch_venue_snapshot(client, real=real, paper_net=paper_net, paper_entry=paper_entry)
    sm.apply_reconcile(boot_snap)
    print(
        f"[TF2BP] boot reconcile state={sm.state.value} venue={sm.venue_position} "
        f"entry_allowed={sm.entry_allowed}",
        flush=True,
    )
    if sm.state == LiveState.LONG:
        entry_ts = time.time()
        paper_net = float(sm.local_position)
        paper_entry = float(sm.entry_price)
        # Sync strategy inventory so exit logic can run after ADOPT_LONG.
        try:
            if paper_entry > 0:
                strat.record_trade("buy", paper_entry, 0.0, mid_price=paper_entry)
        except Exception as exc:
            print(f"[TF2BP] adopt record_trade warn: {exc}", flush=True)
        _write_pos_alert(
            "ADOPT_LONG_ON_BOOT",
            {"venue_net": sm.venue_position, "run_id": sm.run_id, "additional_buy": "BLOCK"},
        )
    elif sm.state == LiveState.SAFE_HALT:
        _write_pos_alert(
            "SAFE_HALT_ON_BOOT",
            {"venue_net": sm.venue_position, "run_id": sm.run_id, "error": boot_snap.error},
        )

    while True:
        if get_shutting_down() or stop_flag.exists():
            if stop_flag.exists() and not stop_reason:
                stop_reason = "stop_flag"
            stop_reason = stop_reason or get_stop_reason() or "stop"
            print(f"[TF2BP] leave loop reason={stop_reason} (no auto-flatten)", flush=True)
            break
        if trade_cap is not None and closed >= trade_cap:
            set_shutdown("max_trades")
            stop_reason = "max_trades"
            break
        if runtime_cap is not None and (time.time() - t0) >= runtime_cap:
            set_shutdown("runtime")
            stop_reason = "runtime"
            break
        if pnl_sum <= -abs(daily_loss_jpy):
            set_shutdown("daily_loss")
            stop_reason = "daily_loss"
            break

        sm.sync_external_halt_flag()

        # SAFE_HALT: no new ENTRY; heartbeat only; wait operator / policy
        if sm.state == LiveState.SAFE_HALT:
            sm.write_heartbeat()
            now = time.time()
            if now - last_hb_log >= 30.0:
                print(f"[TF2BP] SAFE_HALT hb run_id={sm.run_id} venue={sm.venue_position}", flush=True)
                last_hb_log = now
            time.sleep(interval)
            continue

        if sm.state == LiveState.ENTRY_HALT:
            # May still exit if somehow long — normally flat+halt
            sm.write_heartbeat()

        event_id = lat_probe.next_event_id()
        feed_marks: Dict[str, Any] = {}
        http_429 = False
        fetch_error: Optional[str] = None
        try:
            board, vols, feed_source = _fetch_market("FX_BTC_JPY", marks=feed_marks)
        except Exception as exc:
            fetch_error = str(exc)
            http_429 = "429" in fetch_error
            consecutive_429, halt_429 = note_consecutive_429(
                consecutive_429,
                is_429=http_429,
                threshold=CONSECUTIVE_429_HALT,
            )
            if halt_429 and not sm.entry_halt:
                sm.on_entry_halt(reason="consecutive_429")
                print(
                    f"[TF2BP] ENTRY_HALT consecutive_429 n={consecutive_429} "
                    f"(BUY blocked · not STALL)",
                    flush=True,
                )
            print(f"[TF2BP] board error: {exc}", flush=True)
            lat_probe.emit_loop(
                event_id=event_id,
                loop_kind="idle_poll",
                t={"t0": feed_marks.get("t0_ns"), "t1": feed_marks.get("t1_ns")},
                wall={"t0": feed_marks.get("t0_wall_ns")},
                http_429=http_429,
                fetch_error=fetch_error,
                run_id=sm.run_id,
                integrity={"board_fetch_failure": True},
            )
            sm.write_heartbeat()
            time.sleep(interval)
            continue

        consecutive_429, _ = note_consecutive_429(consecutive_429, is_429=False)

        bids = board.get("bids") or []
        asks = board.get("asks") or []
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0
        mid = float(board.get("mid_price") or 0.0) or (
            (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
        )
        bid_sz = float(bids[0]["size"]) if bids else 0.0
        ask_sz = float(asks[0]["size"]) if asks else 0.0
        t2_ns = mono_ns()
        t2_wall_ns = wall_ns()
        board_fp = hash(
            (
                round(best_bid, 1),
                round(best_ask, 1),
                round(bid_sz, 6),
                round(ask_sz, 6),
                round(float(vols.get("buy") or 0), 6),
                round(float(vols.get("sell") or 0), 6),
            )
        )
        integrity: Dict[str, Any] = {
            "duplicate_board": bool(prev_board_fp is not None and board_fp == prev_board_fp),
            "mid_reversal": bool(
                prev_mid is not None and mid > 0 and prev_mid > 0 and abs(mid - prev_mid) / prev_mid > 0.01
            ),
        }
        prev_board_fp = board_fp
        source_exec_ts = vols.get("newest_exec_ts")
        now = time.time()

        # Periodic venue re-sync (SoT). Desync → adopt LONG, never invent FLAT→BUY.
        if now - last_audit >= 20.0:
            last_audit = now
            snap = _fetch_venue_snapshot(
                client, real=real, paper_net=paper_net, paper_entry=paper_entry
            )
            if snap.is_unknown() or not snap.open_orders_known:
                sm.on_state_fault("periodic_reconcile_failure")
                _write_pos_alert("RECONCILE_FAILURE", {"error": snap.error, "run_id": sm.run_id})
                continue
            assert snap.net is not None
            sm.venue_position = snap.net
            if snap.net > size + 1e-9 or snap.net < -1e-12:
                sm.on_state_fault("unexpected_exposure")
                _write_pos_alert(
                    "UNEXPECTED_EXPOSURE",
                    {"venue_net": snap.net, "max_size": size, "run_id": sm.run_id},
                )
                continue
            if abs(snap.net) > 1e-12 and sm.state in (LiveState.READY_FLAT, LiveState.ENTRY_HALT):
                sm.begin_reconcile()
                sm.apply_reconcile(snap)
                paper_net = float(sm.local_position)
                paper_entry = float(sm.entry_price or paper_entry)
                entry_ts = time.time()
                _write_pos_alert("DESYNC_ADOPT", {"venue_net": snap.net, "run_id": sm.run_id})
            elif abs(snap.net) <= 1e-12 and sm.state == LiveState.LONG:
                # venue flat is fact — allow READY_FLAT via reconcile
                sm.begin_reconcile()
                sm.apply_reconcile(snap)
                paper_net = 0.0
                paper_entry = 0.0
            elif sm.state == LiveState.LONG and abs(snap.net) > 1e-12:
                sm.local_position = snap.net
                sm.venue_position = snap.net
                paper_net = snap.net

        strat._latency_marks = {}
        t_before_strat = mono_ns()
        res = strat.on_tick(
            mid_price=mid,
            best_bid=best_bid,
            best_ask=best_ask,
            taker_vol_bid=float(vols.get("sell") or 0.0),
            taker_vol_ask=float(vols.get("buy") or 0.0),
            bid_depth_1=bid_sz,
            ask_depth_1=ask_sz,
        )
        t_after_strat = mono_ns()
        lm = getattr(strat, "_latency_marks", {}) or {}
        t3_ns = lm.get("t3_ns")
        t4_ns = lm.get("t4_ns") or t_before_strat
        t5_ns = lm.get("t5_ns") or t_after_strat
        action = (res or {}).get("action")
        reason = (res or {}).get("reason")
        decide_wall_ns = wall_ns()
        market_age_ms = None
        if source_exec_ts:
            try:
                # bitFlyer exec_date like 2026-10-07T02:30:00.123
                from datetime import datetime as _dt

                src = _dt.fromisoformat(source_exec_ts.replace("Z", "+00:00"))
                if src.tzinfo is None:
                    src = src.replace(tzinfo=timezone.utc)
                market_age_ms = max(0.0, (decide_wall_ns / 1e9 - src.timestamp()) * 1000.0)
            except Exception:
                market_age_ms = None
        if market_age_ms is None and feed_marks.get("t0_wall_ns"):
            market_age_ms = max(0.0, (decide_wall_ns - float(feed_marks["t0_wall_ns"])) / 1e6)
        book_age_ms = max(0.0, (decide_wall_ns - t2_wall_ns) / 1e6)
        feature_age_ms = (
            max(0.0, (decide_wall_ns - (lm.get("t3_wall_ns") or decide_wall_ns)) / 1e6)
            if lm.get("t3_wall_ns")
            else book_age_ms
        )
        loop_kind = "idle_poll"
        if action == "buy":
            loop_kind = "signal_buy"
        elif action == "exit":
            loop_kind = "signal_exit"
        elif (res or {}).get("reason") == "BAR_OPEN":
            loop_kind = "idle_poll"
        else:
            loop_kind = "bar_close_hold"
        # Emit non-order loops here; order loops re-emit with T6..T8 after submit.
        pending_lat = {
            "event_id": event_id,
            "loop_kind": loop_kind,
            "t": {
                "t0": feed_marks.get("t0_ns"),
                "t1": feed_marks.get("t1_ns"),
                "t2": t2_ns,
                "t3": t3_ns,
                "t4": t4_ns,
                "t5": t5_ns,
            },
            "wall": {"t0": feed_marks.get("t0_wall_ns")},
            "mid": mid,
            "action": action,
            "reason": reason,
            "source_exec_ts": source_exec_ts,
            "market_age_ms": market_age_ms,
            "book_age_ms": book_age_ms,
            "feature_age_ms": feature_age_ms,
            "integrity": integrity,
        }
        if action not in ("buy", "exit"):
            lat_probe.emit_loop(run_id=sm.run_id, **pending_lat)
        prev_mid = mid if mid > 0 else prev_mid
        if now - last_hb_log >= 30.0:
            print(
                f"[TF2BP] hb mid={mid:,.0f} state={sm.state.value} "
                f"venue={sm.venue_position} entry_halt={sm.entry_halt} action={action} "
                f"feed={feed_source}",
                flush=True,
            )
            last_hb_log = now
        sm.write_heartbeat()

        # ENTRY_PENDING / EXIT_PENDING: lookup only. Never a second same-side POST.
        if sm.state in (LiveState.ENTRY_PENDING, LiveState.EXIT_PENDING):
            if action in ("buy", "exit"):
                lat_probe.emit_loop(run_id=sm.run_id, **pending_lat)
            if sm.pending_needs_recovery(timeout_sec=max(5.0, interval)):
                side = sm.last_order_side or ("BUY" if sm.state == LiveState.ENTRY_PENDING else "SELL")
                ol = _lookup_child_order(client, acceptance_id=sm.last_order_id, real=real)
                ven = _fetch_venue_snapshot(
                    client, real=real, paper_net=paper_net, paper_entry=paper_entry
                )
                sm.recover_order_unknown(venue=ven, order_lookup=ol, expected_side=side)
                if sm.state == LiveState.LONG:
                    paper_net = float(sm.local_position)
                    paper_entry = float(sm.entry_price or paper_entry)
                elif sm.state in (LiveState.READY_FLAT, LiveState.ENTRY_HALT):
                    paper_net = 0.0
                    paper_entry = 0.0
                print(
                    f"[TF2BP] pending recovery → state={sm.state.value} side={side} "
                    f"venue={sm.venue_position}",
                    flush=True,
                )
            time.sleep(interval)
            continue

        try:
            if action == "buy":
                recheck = _fetch_venue_snapshot(
                    client, real=real, paper_net=paper_net, paper_entry=paper_entry
                )
                ok, reason = sm.can_submit_buy(
                    strategy_action="buy",
                    venue_recheck=recheck,
                    risk_pass=True,
                    conflicting_open_orders=(
                        recheck.open_orders_known and recheck.open_order_count > 0
                    ),
                )
                t6_ns = mono_ns()
                if not ok:
                    pending_lat["t"]["t6"] = t6_ns
                    pending_lat["loop_kind"] = "signal_buy"
                    lat_probe.emit_loop(run_id=sm.run_id, **pending_lat)
                    if reason == "venue_non_flat":
                        _write_pos_alert(
                            "BUY_BLOCKED_VENUE_NOT_FLAT",
                            {"venue_net": recheck.net, "run_id": sm.run_id},
                        )
                        paper_net = float(sm.local_position or 0.0)
                        paper_entry = float(sm.entry_price or paper_entry)
                    time.sleep(interval)
                    continue

                sm.mark_entry_pending(order_qty=size)
                oid: Optional[str] = None
                order_ack_box.clear()
                t7_ns = mono_ns()
                try:
                    order_res = _send_taker_market(client, "BUY", size)
                    oid = (order_res or {}).get("child_order_acceptance_id")
                    sm.last_order_id = oid
                    if ask_sz + 1e-12 >= size:
                        sm.mark_predicted_fill(side="BUY", source="board_mimic_ask")
                    t8_ns = order_ack_box.get("t8_ns") or mono_ns()
                    t7_ns = order_ack_box.get("t7_ns") or t7_ns
                    pending_lat["t"]["t6"] = t6_ns
                    pending_lat["t"]["t7"] = t7_ns
                    pending_lat["t"]["t8"] = t8_ns
                    pending_lat["loop_kind"] = "order_submit"
                    lat_probe.emit_loop(
                        run_id=sm.run_id,
                        acceptance_id=oid,
                        **pending_lat,
                    )
                except OrderSubmitUnknownError as exc:
                    pending_lat["t"]["t6"] = t6_ns
                    pending_lat["t"]["t7"] = t7_ns
                    pending_lat["t"]["t8"] = mono_ns()
                    pending_lat["loop_kind"] = "order_submit"
                    lat_probe.emit_loop(
                        run_id=sm.run_id,
                        fetch_error=str(exc),
                        **pending_lat,
                    )
                    print(f"[TF2BP] BUY ORDER_UNKNOWN (no re-POST): {exc}", flush=True)
                    sm.enter_order_unknown(detail={"exc": str(exc), "side": "BUY"}, order_id=oid)
                    _write_pos_alert("ORDER_UNKNOWN", {"exc": str(exc), "run_id": sm.run_id})
                    time.sleep(interval)
                    continue
                except Exception as exc:
                    pending_lat["t"]["t6"] = t6_ns
                    pending_lat["t"]["t7"] = t7_ns
                    pending_lat["t"]["t8"] = mono_ns()
                    pending_lat["loop_kind"] = "order_submit"
                    lat_probe.emit_loop(
                        run_id=sm.run_id,
                        fetch_error=str(exc),
                        **pending_lat,
                    )
                    cls = classify_order_error(exc)
                    print(f"[TF2BP] BUY error class={cls.value}: {exc}", flush=True)
                    if cls == OrderErrorClass.NON_RETRYABLE_ENTRY:
                        v = _fetch_venue_snapshot(
                            client, real=real, paper_net=paper_net, paper_entry=paper_entry
                        )
                        sm.on_margin_insufficient(venue=v)
                        _write_pos_alert(
                            "MARGIN_INSUFFICIENT",
                            {"venue_net": v.net, "action": "ENTRY_HALT", "run_id": sm.run_id},
                        )
                        time.sleep(interval)
                        continue
                    # Ambiguous / timeout / other → ORDER_UNKNOWN recovery (never TRANSIENT re-BUY)
                    sm.enter_order_unknown(detail={"exc": str(exc), "side": "BUY", "class": cls.value})
                    _write_pos_alert("ORDER_UNKNOWN", {"exc": str(exc), "run_id": sm.run_id})
                    time.sleep(interval)
                    continue

                time.sleep(0.5)
                post = _fetch_venue_snapshot(
                    client, real=real, paper_net=paper_net + size, paper_entry=best_ask or mid
                )
                if not real:
                    post = VenueSnapshot(
                        net=size,
                        avg_entry=best_ask if best_ask > 0 else mid,
                        open_orders_known=True,
                        open_order_count=0,
                        ok=True,
                    )
                sm.on_entry_confirmed(post, order_id=oid if real else "SIMULATED")
                if sm.state == LiveState.LONG:
                    paper_net = float(sm.local_position)
                    paper_entry = float(sm.entry_price)
                    entry_ts = time.time()
                    strat.record_trade("buy", paper_entry, 0.0, mid_price=mid)
                    print(
                        f"[TF2BP] ENTRY BUY @ {paper_entry:,.0f} venue_pos={sm.venue_position} "
                        f"run_id={sm.run_id}",
                        flush=True,
                    )
                    if real:
                        _retest_discord(
                            f"TF2BP SET-{_retest_set()} ENTRY BUY @ {paper_entry:,.0f} "
                            f"venue={sm.venue_position} run_id={sm.run_id}"
                        )

            elif action == "exit":
                ok, reason = sm.can_submit_exit(strategy_action="exit")
                t6_ns = mono_ns()
                if not ok:
                    pending_lat["t"]["t6"] = t6_ns
                    pending_lat["loop_kind"] = "signal_exit"
                    lat_probe.emit_loop(run_id=sm.run_id, **pending_lat)
                    time.sleep(interval)
                    continue
                sell_sz = abs(float(sm.local_position or sm.venue_position or paper_net))
                if sell_sz <= 1e-12:
                    pending_lat["t"]["t6"] = t6_ns
                    lat_probe.emit_loop(run_id=sm.run_id, **pending_lat)
                    time.sleep(interval)
                    continue
                sm.mark_exit_pending(order_qty=sell_sz)
                order_ack_box.clear()
                t7_ns = mono_ns()
                try:
                    order_res = _send_taker_market(client, "SELL", sell_sz)
                    sm.last_order_id = (order_res or {}).get("child_order_acceptance_id")
                    if bid_sz + 1e-12 >= sell_sz:
                        sm.mark_predicted_fill(side="SELL", source="board_mimic_bid")
                    t8_ns = order_ack_box.get("t8_ns") or mono_ns()
                    t7_ns = order_ack_box.get("t7_ns") or t7_ns
                    pending_lat["t"]["t6"] = t6_ns
                    pending_lat["t"]["t7"] = t7_ns
                    pending_lat["t"]["t8"] = t8_ns
                    pending_lat["loop_kind"] = "order_submit"
                    lat_probe.emit_loop(
                        run_id=sm.run_id,
                        acceptance_id=sm.last_order_id,
                        **pending_lat,
                    )
                except OrderSubmitUnknownError as exc:
                    pending_lat["t"]["t6"] = t6_ns
                    pending_lat["t"]["t7"] = t7_ns
                    pending_lat["t"]["t8"] = mono_ns()
                    pending_lat["loop_kind"] = "order_submit"
                    lat_probe.emit_loop(
                        run_id=sm.run_id, fetch_error=str(exc), **pending_lat
                    )
                    sm.enter_order_unknown(detail={"exc": str(exc), "side": "SELL"})
                    time.sleep(interval)
                    continue
                except Exception as exc:
                    pending_lat["t"]["t6"] = t6_ns
                    pending_lat["t"]["t7"] = t7_ns
                    pending_lat["t"]["t8"] = mono_ns()
                    pending_lat["loop_kind"] = "order_submit"
                    lat_probe.emit_loop(
                        run_id=sm.run_id, fetch_error=str(exc), **pending_lat
                    )
                    cls = classify_order_error(exc)
                    print(f"[TF2BP] SELL error class={cls.value}: {exc}", flush=True)
                    sm.enter_order_unknown(
                        detail={"exc": str(exc), "side": "SELL", "class": cls.value}
                    )
                    time.sleep(max(interval, 15.0))
                    continue

                time.sleep(0.5)
                post = _fetch_venue_snapshot(client, real=real, paper_net=0.0, paper_entry=0.0)
                if not real:
                    post = VenueSnapshot(
                        net=0.0, avg_entry=0.0, open_orders_known=True, open_order_count=0, ok=True
                    )
                exit_px = best_bid if best_bid > 0 else mid
                entry_px = float(sm.entry_price or paper_entry)
                sm.on_exit_venue_check(post)
                if sm.state in (LiveState.READY_FLAT, LiveState.ENTRY_HALT) and abs(
                    float(sm.venue_position or 0.0)
                ) <= 1e-12:
                    pnl = (exit_px - entry_px) * sell_sz
                    pnl_sum += pnl
                    closed += 1
                    strat.record_trade("exit", exit_px, pnl, mid_price=mid)
                    trades.append(
                        {
                            "n": closed,
                            "entry": entry_px,
                            "exit": exit_px,
                            "pnl": round(pnl, 1),
                            "hold_sec": round(time.time() - entry_ts, 1) if entry_ts else None,
                            "reason": (res or {}).get("reason"),
                            "size": sell_sz,
                        }
                    )
                    paper_net = 0.0
                    paper_entry = 0.0
                    cap_s = "∞" if trade_cap is None else str(trade_cap)
                    print(
                        f"[TF2BP] EXIT #{closed}/{cap_s} pnl={pnl:+.1f} sum={pnl_sum:+.1f} "
                        f"size={sell_sz} state={sm.state.value}",
                        flush=True,
                    )
                    if real:
                        _retest_discord(
                            f"TF2BP SET-{_retest_set()} EXIT #{closed}/{cap_s} "
                            f"pnl={pnl:+.1f} sum={pnl_sum:+.1f} state={sm.state.value}"
                        )
                else:
                    _write_pos_alert(
                        "EXIT_INCOMPLETE",
                        {"venue_net": sm.venue_position, "run_id": sm.run_id},
                    )
        except Exception as exc:
            cls = classify_order_error(exc)
            print(f"[TF2BP] order error class={cls.value}: {exc}", flush=True)
            if cls == OrderErrorClass.NON_RETRYABLE_ENTRY:
                v = _fetch_venue_snapshot(
                    client, real=real, paper_net=paper_net, paper_entry=paper_entry
                )
                sm.on_margin_insufficient(venue=v)
                _write_pos_alert(
                    "MARGIN_INSUFFICIENT",
                    {"venue_net": v.net, "action": "ENTRY_HALT", "run_id": sm.run_id},
                )
            elif cls in (OrderErrorClass.ORDER_UNKNOWN, OrderErrorClass.TRANSIENT):
                sm.enter_order_unknown(detail={"exc": str(exc), "class": cls.value})
            elif cls == OrderErrorClass.STATE_FAULT:
                sm.on_state_fault(str(exc))
            time.sleep(interval)
            continue
        time.sleep(interval)

    # Stop policy: no automatic flatten. Positioned → SAFE_HALT alert.
    final = _fetch_venue_snapshot(client, real=real, paper_net=paper_net, paper_entry=paper_entry)
    if final.ok and final.net is not None:
        sm.venue_position = final.net
    sm.stop(reason=stop_reason or get_stop_reason() or "end")
    try:
        set_order_ack_hook(None)
        lat_probe.stop()
        sys_sampler.stop()
        print(
            f"[TF2BP] latency_probe written={lat_probe.written} dropped={lat_probe.dropped} "
            f"sys_rows={sys_sampler.written}",
            flush=True,
        )
    except Exception:
        pass
    if sm.state == LiveState.SAFE_HALT:
        _write_pos_alert(
            "STOP_WHILE_POSITIONED",
            {
                "venue_net": sm.venue_position,
                "stop_reason": stop_reason,
                "run_id": sm.run_id,
                "note": "no_auto_flatten",
            },
        )
    print(
        f"[TF2BP] stopped state={sm.state.value} venue={sm.venue_position} run_id={sm.run_id}",
        flush=True,
    )
    # CSR-618: a finished REAL set must not be auto-restarted as the next 20.
    done = stop_reason or get_stop_reason() or ""
    if real and done in ("max_trades", "daily_loss", "runtime"):
        try:
            STOP_FLAG.write_text(f"set_complete {done} {_jst()}\n", encoding="utf-8")
            print(f"[TF2BP] STOP flag pinned reason={done} (no auto SET+1)", flush=True)
            _retest_discord(
                f"TF2BP SET-{_retest_set()} 終了 reason={done} closed={closed} "
                f"pnl_sum={pnl_sum:+.1f} state={sm.state.value} venue={sm.venue_position}"
            )
        except Exception as exc:
            print(f"[TF2BP] STOP pin warn: {exc}", flush=True)
    return {
        "id": "TF2BP_v1",
        "closed_trades": closed,
        "pnl_jpy": round(pnl_sum, 1),
        "trades": trades,
        "real": real,
        "stop_reason": stop_reason or get_stop_reason() or None,
        "sm_state": sm.state.value,
        "run_id": sm.run_id,
        "venue_position": sm.venue_position,
        "lock_acquired": True,
        "entry_halt": bool(sm.entry_halt),
        "consecutive_429": consecutive_429,
    }


def run_custom(
    *,
    strat_id: str,
    path: str,
    size: float,
    max_trades: int,
    real: bool,
    interval: float,
    max_runtime_sec: float,
    daily_loss_jpy: float,
    max_steps: Optional[int],
) -> Dict[str, Any]:
    trader = LivePaperTrader(
        strategy_path=path,
        product_code="FX_BTC_JPY",
        timeframe="1m",
        order_size_btc=size,
        enable_real_trading=real,
        poll_interval_sec=interval,
        min_profit_jpy=15.0,
        stop_loss_jpy=25.0,
        max_hold_sec=1200.0,
        daily_loss_limit_jpy=daily_loss_jpy,
        max_consecutive_losses=4,
        max_closed_trades=max_trades,
        allow_adaptive=False,
    )
    # Bound wall time via max_steps if provided; else estimate from runtime
    steps = max_steps
    if steps is None and max_runtime_sec > 0:
        steps = max(1, int(max_runtime_sec / max(interval, 0.5)))
    print(f"[{strat_id}] start real={real} max_trades={max_trades} steps={steps}", flush=True)
    trader.start(max_steps=steps)
    return {
        "id": strat_id,
        "closed_trades": len(trader.trades_history),
        "pnl_jpy": round(trader.realized_pnl_jpy, 1),
        "trades": trader.trades_history[-max_trades:],
        "halt_reason": trader.halt_reason,
        "real": real,
    }


def main() -> int:
    caps = _load_caps()
    shared = caps.get("shared_caps") or {}
    ap = argparse.ArgumentParser(description="CSR-574 micro LIVE soak sequential")
    ap.add_argument("--real", action="store_true", help="real orders (requires env + confirm)")
    ap.add_argument("--confirm-go", default="", help="must be MICRO574 for --real")
    ap.add_argument("--size", type=float, default=float(shared.get("order_size_btc", 0.001)))
    ap.add_argument(
        "--max-trades",
        type=int,
        default=int(shared.get("data_soak_max_trades", 0)),
        help="closed-trade cap; 0 = unlimited",
    )
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument(
        "--max-runtime-sec",
        type=float,
        default=float(shared.get("max_runtime_sec", 0)),
        help="per strategy wall clock; 0 = unlimited",
    )
    ap.add_argument("--daily-loss-jpy", type=float, default=float(shared.get("daily_loss_stop_jpy", 800)))
    ap.add_argument("--only", default="", help="comma ids: TF2BP_v1,strat_cbcd5aed,strat_4d3f2c9f")
    ap.add_argument("--smoke", action="store_true", help="short dry run (steps/runtime capped)")
    args = ap.parse_args()

    real = bool(args.real)
    env_real = os.environ.get("ENABLE_REAL_TRADING", "false").lower() == "true"
    if real:
        if args.confirm_go != "MICRO574":
            print("REFUSE: --real requires --confirm-go MICRO574", file=sys.stderr)
            return 2
        if not env_real:
            print("REFUSE: ENABLE_REAL_TRADING is not true in environment", file=sys.stderr)
            return 2
        if args.size > 0.001 + 1e-12:
            print("REFUSE: size > 0.001", file=sys.stderr)
            return 2
        if int(args.max_trades) < 1 or int(args.max_trades) > 20:
            print("REFUSE: --real requires --max-trades 1..20", file=sys.stderr)
            return 2

    # BitFlyerClient ORs with env; force env false for dry unless real approved
    if not real:
        os.environ["ENABLE_REAL_TRADING"] = "false"

    only = {x.strip() for x in args.only.split(",") if x.strip()} if args.only else None
    if args.smoke:
        max_runtime = 90.0
        max_trades = 2 if args.max_trades <= 0 else min(2, int(args.max_trades))
        max_steps_custom = 20
    else:
        max_runtime = float(args.max_runtime_sec)  # 0 = unlimited
        max_trades = int(args.max_trades)  # 0 = unlimited
        max_steps_custom = None

    results = []
    print("=" * 70)
    print(f"CSR-574 micro soak  ts={_jst()}  real={real}  smoke={args.smoke}")
    print("=" * 70)

    for spec in STRATS:
        if only and spec["id"] not in only:
            continue
        if spec["kind"] == "tf2bp":
            row = run_tf2bp(
                size=args.size,
                max_trades=max_trades,
                real=real,
                interval=args.interval,
                max_runtime_sec=max_runtime,
                daily_loss_jpy=args.daily_loss_jpy,
            )
        else:
            row = run_custom(
                strat_id=spec["id"],
                path=spec["path"],
                size=args.size,
                max_trades=max_trades,
                real=real,
                interval=max(args.interval, 5.0),
                max_runtime_sec=max_runtime,
                daily_loss_jpy=args.daily_loss_jpy,
                max_steps=max_steps_custom,
            )
        row["name"] = spec["name"]
        row["ts_jst"] = _jst()
        results.append(row)
        _append_ledger({"csr": 574, "event": "strategy_done", **row})
        print(f"DONE {spec['id']} closed={row.get('closed_trades')} pnl={row.get('pnl_jpy')}", flush=True)

    summary = {
        "csr": 574,
        "event": "sequence_done",
        "ts_jst": _jst(),
        "real": real,
        "smoke": args.smoke,
        "results": results,
        "Economic_PASS": "NO",
    }
    _append_ledger(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
