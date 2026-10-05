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
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

from core.bitflyer_client import BitFlyerClient  # noqa: E402
from execution.paper_trader import LivePaperTrader  # noqa: E402
from antigravity.strategies.tf2bp_strategy import TF2BPStrategy  # noqa: E402

JST = timezone(timedelta(hours=9))
CAPS_PATH = AG / "configs" / "live_prep_caps_csr573.json"
LEDGER = AG / "data" / "mm_research" / "micro_live_soak_csr574.jsonl"

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


def _load_caps() -> Dict[str, Any]:
    if CAPS_PATH.exists():
        return json.loads(CAPS_PATH.read_text(encoding="utf-8"))
    return {}


def _append_ledger(row: Dict[str, Any]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _fetch_board(product: str) -> Dict[str, Any]:
    url = f"https://api.bitflyer.com/v1/board?product_code={product}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode())


def _fetch_exec_vols(product: str) -> Dict[str, float]:
    url = f"https://api.bitflyer.com/v1/executions?product_code={product}&count=50"
    buy = sell = 0.0
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            rows = json.loads(resp.read().decode())
        for r in rows or []:
            sz = float(r.get("size") or 0)
            if (r.get("side") or "").upper() == "BUY":
                buy += sz
            else:
                sell += sz
    except Exception:
        pass
    return {"buy": buy, "sell": sell}


def run_tf2bp(
    *,
    size: float,
    max_trades: int,
    real: bool,
    interval: float,
    max_runtime_sec: float,
    daily_loss_jpy: float,
) -> Dict[str, Any]:
    client = BitFlyerClient(enable_real_trading=real, max_order_size=size)
    strat = TF2BPStrategy({"order_size_btc": size})
    closed = 0
    position = 0.0
    entry = 0.0
    entry_ts = 0.0
    pnl_sum = 0.0
    trades: List[Dict[str, Any]] = []
    t0 = time.time()
    trade_cap = None if max_trades <= 0 else int(max_trades)
    runtime_cap = None if max_runtime_sec <= 0 else float(max_runtime_sec)
    print(
        f"[TF2BP] start real={real} size={size} max_trades="
        f"{'unlimited' if trade_cap is None else trade_cap} "
        f"runtime={'unlimited' if runtime_cap is None else runtime_cap}",
        flush=True,
    )

    while True:
        if trade_cap is not None and closed >= trade_cap:
            break
        if runtime_cap is not None and (time.time() - t0) >= runtime_cap:
            print(f"[TF2BP] runtime stop after {runtime_cap}s", flush=True)
            break
        if pnl_sum <= -abs(daily_loss_jpy):
            print(f"[TF2BP] daily loss stop pnl={pnl_sum}", flush=True)
            break
        try:
            board = _fetch_board("FX_BTC_JPY")
            vols = _fetch_exec_vols("FX_BTC_JPY")
        except Exception as exc:
            print(f"[TF2BP] board error: {exc}", flush=True)
            time.sleep(interval)
            continue
        bids = board.get("bids") or []
        asks = board.get("asks") or []
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0
        mid = float(board.get("mid_price") or 0.0) or (
            (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
        )
        bid_sz = float(bids[0]["size"]) if bids else 0.0
        ask_sz = float(asks[0]["size"]) if asks else 0.0
        res = strat.on_tick(
            mid_price=mid,
            best_bid=best_bid,
            best_ask=best_ask,
            taker_vol_bid=vols["sell"],
            taker_vol_ask=vols["buy"],
            bid_depth_1=bid_sz,
            ask_depth_1=ask_sz,
        )
        action = (res or {}).get("action")
        if action == "buy" and position == 0.0:
            client.send_order("FX_BTC_JPY", "BUY", size, "MARKET")
            position = size
            entry = best_ask if best_ask > 0 else mid
            entry_ts = time.time()
            strat.record_trade("buy", entry, 0.0, mid_price=mid)
            print(f"[TF2BP] ENTRY BUY @ {entry:,.0f}", flush=True)
        elif action == "exit" and position != 0.0:
            client.send_order("FX_BTC_JPY", "SELL", abs(position), "MARKET")
            exit_px = best_bid if best_bid > 0 else mid
            pnl = (exit_px - entry) * position
            pnl_sum += pnl
            closed += 1
            strat.record_trade("exit", exit_px, pnl, mid_price=mid)
            trades.append(
                {
                    "n": closed,
                    "entry": entry,
                    "exit": exit_px,
                    "pnl": round(pnl, 1),
                    "hold_sec": round(time.time() - entry_ts, 1),
                    "reason": (res or {}).get("reason"),
                }
            )
            cap_s = "∞" if trade_cap is None else str(trade_cap)
            print(f"[TF2BP] EXIT #{closed}/{cap_s} pnl={pnl:+.1f} sum={pnl_sum:+.1f}", flush=True)
            position = 0.0
            entry = 0.0
        time.sleep(interval)

    if position != 0.0:
        client.send_order("FX_BTC_JPY", "SELL", abs(position), "MARKET")
        exit_px = entry
        try:
            board = _fetch_board("FX_BTC_JPY")
            bids = board.get("bids") or []
            exit_px = float(bids[0]["price"]) if bids else entry
        except Exception:
            pass
        pnl = (exit_px - entry) * position
        pnl_sum += pnl
        closed += 1
        trades.append({"n": closed, "entry": entry, "exit": exit_px, "pnl": round(pnl, 1), "reason": "FLATTEN"})
        print(f"[TF2BP] FLATTEN pnl={pnl:+.1f}", flush=True)
        position = 0.0

    return {
        "id": "TF2BP_v1",
        "closed_trades": closed,
        "pnl_jpy": round(pnl_sum, 1),
        "trades": trades,
        "real": real,
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
        # trade-count ceiling removed — continuous LIVE uses max_trades=0

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
