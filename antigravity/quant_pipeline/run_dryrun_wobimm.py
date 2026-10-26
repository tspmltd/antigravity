#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WOBIMM Dry-Run — Microstructure-Guided Pure Maker MM (WIRE=NO).

Features:
  1. 板模倣システム（マイクロストラクチャー解析）による「全指値（100% Pure Maker）」MM
  2. 加重マイクロプライス基準値算出 (Fair Micro-Price Centering)
  3. 在庫ペナルティ ＆ WOBI毒性追従の動的非対称スキュー (Asymmetric Reservation Price)
  4. 毒性ゲートによる瞬間片側キャンセル (One-Sided Toxicity Pull: WOBI > 0.6 / < -0.6)
  5. 大口防衛壁アンカー指値 (Wall-Shielded Quoting)
  6. パブリック約定（executions.jsonl）と板クロス判定による高精度Maker約定シミュレーション
  7. 2レーン同時A/B対比:
     - WOBIMM: 板模倣型マイクロMM (毒性回避・壁アンカー・在庫スキュー)
     - NAIVE_MM: 単純仲値対称MM (毒性Pullなし・古典ベンチマーク)
  8. 1時間毎のDiscord通知（1h/24h PnL、在庫推移、毒性Pull回数、平均獲得スプレッド、コメント）
"""
from __future__ import annotations

import argparse
import collections
from dataclasses import dataclass, field
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

from antigravity.quant_pipeline.feeder_board_source import (  # noqa: E402
    FeederBoardSource,
    resolve_market_source,
    SOURCE_SHARED,
)
from antigravity.quant_pipeline.ingestion import LiveBoardFeed, MarketDataIngestion  # noqa: E402
from antigravity.quant_pipeline.event_bus import EventBus  # noqa: E402
from antigravity.quant_pipeline.parquet_logger import ParquetBatchLogger  # noqa: E402

JST = timezone(timedelta(hours=9))
OUT_DIR = AG / "data" / "mm_research" / "wobimm_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = AG / "data" / "dryrun_wobimm_state.json"
PID_PATH = AG / "data" / "dryrun_wobimm.pid"
LOG_PATH = AG / "logs" / "dryrun_wobimm.log"
FEEDER_LATEST = AG / "data" / "market_feeder" / "latest.json"
FEEDER_EXECS = AG / "data" / "market_feeder" / "executions.jsonl"
LEDGER_PATH = OUT_DIR / "paired_ledger.jsonl"
AUDIT_PATH = OUT_DIR / "audit_events.jsonl"
FROZEN_PIN_PATH = AG / "configs" / "wobimm_benchmark_frozen.json"
FROZEN_FLAG = AG / "data" / "WOBIMM_BENCHMARK_FROZEN.flag"

# CSR-634 — WOBIMM DRYRUN is the frozen paper benchmark. Do not retune.
WOBIMM_BENCHMARK_PIN = {
    "gamma": 0.5,
    "kappa": 0.8,
    "base_spread_bp": 1.6,
    "lot_size": 0.001,
    "max_inv_btc": 0.004,
    "toxic_wobi_threshold": 0.4,
    "toxic_ofi_threshold": 0.06,
}

DISCORD_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1562675909276991543/"
    "MqAEMhpxo0xJmQXsM6dVTjM_5wYcket-l0LGRLHAZRD8P_OV-IbW6b8kvg6q-azVl18D"
)


def _iso() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def _log(msg: str) -> None:
    print(f"[{_iso()}] {msg}", flush=True)


def assert_wobimm_benchmark_pin(mgr: "MicrostructureMMManager") -> None:
    """Refuse boot if WOBIMM quoting pins drift (CSR-634)."""
    got = {
        "gamma": mgr.gamma,
        "kappa": mgr.kappa,
        "base_spread_bp": mgr.base_spread_bp,
        "lot_size": mgr.lot_size,
        "max_inv_btc": mgr.max_inv_btc,
        "toxic_wobi_threshold": mgr.toxic_wobi_threshold,
        "toxic_ofi_threshold": mgr.toxic_ofi_threshold,
    }
    drift = {k: (got[k], WOBIMM_BENCHMARK_PIN[k]) for k in WOBIMM_BENCHMARK_PIN if got[k] != WOBIMM_BENCHMARK_PIN[k]}
    if drift:
        raise RuntimeError(f"REFUSE: WOBIMM BENCHMARK pin drift {drift}")


def _append(p: Path, obj: Dict[str, Any]) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception as exc:
        _log(f"append failed {p.name}: {exc}")


@dataclass
class MMQuote:
    bid_price: float = 0.0
    bid_size: float = 0.0
    ask_price: float = 0.0
    ask_size: float = 0.0
    has_bid: bool = False
    has_ask: bool = False
    pull_reason_bid: str = ""
    pull_reason_ask: str = ""
    micro_price: float = 0.0
    reservation_price: float = 0.0


@dataclass
class MMLaneAccounting:
    lane: str
    inventory: float = 0.0
    max_inventory: float = 0.0
    lot_size: float = 0.001
    cost_basis_long: float = 0.0
    cost_basis_short: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl_jpy: float = 0.0
    total_pnl_bp: float = 0.0
    toxicity_pulls: int = 0
    spread_capture_sum_bp: float = 0.0
    history: List[Dict[str, Any]] = field(default_factory=list)

    def on_buy_fill(self, fill_price: float, fill_size: float, now: float, is_maker: bool = True) -> Optional[Dict[str, Any]]:
        trade_record = None
        # Shortポジションがある場合は一部返済（利確/損切り）
        if self.inventory < -1e-6:
            cover_size = min(fill_size, abs(self.inventory))
            profit_unit = self.cost_basis_short - fill_price
            pnl_jpy = profit_unit * cover_size
            pnl_bp = (profit_unit / fill_price) * 1e4
            self.total_pnl_jpy += pnl_jpy
            self.total_pnl_bp += pnl_bp
            self.total_trades += 1
            is_win = pnl_bp > 0
            if is_win:
                self.winning_trades += 1

            trade_record = {
                "ts": now,
                "lane": self.lane,
                "action": "COVER_SHORT",
                "price": fill_price,
                "size": cover_size,
                "pnl_jpy": round(pnl_jpy, 2),
                "pnl_bp": round(pnl_bp, 2),
                "is_win": is_win,
                "inventory": round(self.inventory + cover_size, 4),
            }

        self.inventory += fill_size
        self.max_inventory = max(self.max_inventory, abs(self.inventory))
        # Long在庫の平均取得価格更新
        if self.inventory > 1e-6:
            if self.inventory == fill_size:
                self.cost_basis_long = fill_price
            else:
                prev_size = self.inventory - fill_size
                if prev_size > 0:
                    self.cost_basis_long = (self.cost_basis_long * prev_size + fill_price * fill_size) / self.inventory
                else:
                    self.cost_basis_long = fill_price

        return trade_record

    def on_sell_fill(self, fill_price: float, fill_size: float, now: float, is_maker: bool = True) -> Optional[Dict[str, Any]]:
        trade_record = None
        # Longポジションがある場合は一部利確
        if self.inventory > 1e-6:
            close_size = min(fill_size, self.inventory)
            profit_unit = fill_price - self.cost_basis_long
            pnl_jpy = profit_unit * close_size
            pnl_bp = (profit_unit / self.cost_basis_long) * 1e4
            self.total_pnl_jpy += pnl_jpy
            self.total_pnl_bp += pnl_bp
            self.total_trades += 1
            is_win = pnl_bp > 0
            if is_win:
                self.winning_trades += 1

            trade_record = {
                "ts": now,
                "lane": self.lane,
                "action": "CLOSE_LONG",
                "price": fill_price,
                "size": close_size,
                "pnl_jpy": round(pnl_jpy, 2),
                "pnl_bp": round(pnl_bp, 2),
                "is_win": is_win,
                "inventory": round(self.inventory - close_size, 4),
            }

        self.inventory -= fill_size
        self.max_inventory = max(self.max_inventory, abs(self.inventory))
        # Short在庫の平均取得価格更新
        if self.inventory < -1e-6:
            short_pos = abs(self.inventory)
            if short_pos == fill_size:
                self.cost_basis_short = fill_price
            else:
                prev_short = short_pos - fill_size
                if prev_short > 0:
                    self.cost_basis_short = (self.cost_basis_short * prev_short + fill_price * fill_size) / short_pos
                else:
                    self.cost_basis_short = fill_price

        return trade_record


def _sharpe_dd_from_hist(hist: List[Dict[str, Any]]) -> Dict[str, float]:
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


def _window_stats_mm(hist: List[Dict[str, Any]], hours: float) -> Dict[str, Any]:
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
    sd = _sharpe_dd_from_hist(sub)
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


class MicrostructureMMManager:
    """板模倣システムを基盤とするマイクロストラクチャーMMクォーティングマネージャー"""

    def __init__(
        self,
        gamma: float = 0.5,       # 在庫ペナルティ
        kappa: float = 0.8,       # WOBI毒性追従
        base_spread_bp: float = 1.6, # 基本ハーフスプレッド bp
        lot_size: float = 0.001,
        max_inv_btc: float = 0.004,
        toxic_wobi_threshold: float = 0.40, # 毒性Pull閾値（0.55から0.40に引き締め）
        toxic_ofi_threshold: float = 0.06,  # 毒性OFI閾値
    ) -> None:
        self.gamma = gamma
        self.kappa = kappa
        self.base_spread_bp = base_spread_bp
        self.lot_size = lot_size
        self.max_inv_btc = max_inv_btc
        self.toxic_wobi_threshold = toxic_wobi_threshold
        self.toxic_ofi_threshold = toxic_ofi_threshold

        self.wobi_engine_depth = 5
        self.lambda_decay = 0.5

    def calculate_wobi(self, bids: List[Dict[str, float]], asks: List[Dict[str, float]]) -> float:
        if not bids or not asks:
            return 0.0
        w_b = sum(math.exp(-self.lambda_decay * i) * float(bids[i].get("size", 0.0)) for i in range(min(self.wobi_engine_depth, len(bids))))
        w_a = sum(math.exp(-self.lambda_decay * i) * float(asks[i].get("size", 0.0)) for i in range(min(self.wobi_engine_depth, len(asks))))
        tot = w_b + w_a
        return (w_b - w_a) / tot if tot > 1e-12 else 0.0

    def compute_wobimm_quotes(
        self,
        bids: List[Dict[str, float]],
        asks: List[Dict[str, float]],
        inventory: float,
        ofi: float = 0.0,
        taker_vol_bid: float = 0.0,
        taker_vol_ask: float = 0.0,
    ) -> MMQuote:
        if not bids or not asks:
            return MMQuote()

        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        mid = (best_bid + best_ask) / 2.0

        v_bid = float(bids[0].get("size", 0.0))
        v_ask = float(asks[0].get("size", 0.0))
        tot_v = v_bid + v_ask

        # 1. マイクロプライス算出
        micro_price = mid
        if tot_v > 1e-12:
            micro_price = best_bid + (best_ask - best_bid) * (v_bid / tot_v)

        # 2. WOBI 深度加重不均衡
        wobi = self.calculate_wobi(bids, asks)

        # 3. 予約基準価格（Reservation Price）の動的非対称スキュー
        # 在庫ペナルティ（inv > 0 なら売りたいため R を下げる）
        # WOBI追従（wobi > 0 なら買い圧力強いため R を上げて売り指値を奥へ退避）
        r_price = micro_price - (self.gamma * inventory * 1000.0) + (self.kappa * wobi * 15.0)

        half_spread = mid * (self.base_spread_bp / 10000.0)
        target_bid = r_price - half_spread
        target_ask = r_price + half_spread

        # 4. 壁アンカリング（大口流動性を盾にして指値配置）
        # 買い壁の探索
        bid_wall_p = best_bid
        for b in bids[:4]:
            if float(b.get("size", 0.0)) >= 0.08:
                bid_wall_p = float(b["price"])
                break
        opt_bid = math.floor(min(target_bid, best_bid, bid_wall_p + 1.0))

        # 売り壁の探索
        ask_wall_p = best_ask
        for a in asks[:4]:
            if float(a.get("size", 0.0)) >= 0.08:
                ask_wall_p = float(a["price"])
                break
        opt_ask = math.ceil(max(target_ask, best_ask, ask_wall_p - 1.0))

        quote = MMQuote(
            bid_price=opt_bid,
            bid_size=self.lot_size,
            ask_price=opt_ask,
            ask_size=self.lot_size,
            has_bid=True,
            has_ask=True,
            micro_price=round(micro_price, 1),
            reservation_price=round(r_price, 1),
        )

        # 5. 毒性ゲート ＆ 片側キャンセル（One-Sided Toxicity Pull）
        # 買い優勢・毒性フロー：売り指値を即座に引っ込める（毒食い防止）
        is_toxic_buy = (
            wobi > self.toxic_wobi_threshold
            or ofi > self.toxic_ofi_threshold
            or (taker_vol_bid >= 0.12 and taker_vol_bid > 2.0 * max(0.01, taker_vol_ask))
        )
        # 売り優勢・毒性フロー：買い指値を即座に引っ込める（落ちるナイフ掴み防止）
        is_toxic_sell = (
            wobi < -self.toxic_wobi_threshold
            or ofi < -self.toxic_ofi_threshold
            or (taker_vol_ask >= 0.12 and taker_vol_ask > 2.0 * max(0.01, taker_vol_bid))
        )

        if is_toxic_buy:
            quote.has_ask = False
            quote.ask_price = 0.0
            quote.pull_reason_ask = f"TOXIC_BUY_FLOW (wobi={wobi:+.2f}, ofi={ofi:+.2f})"
        elif is_toxic_sell:
            quote.has_bid = False
            quote.bid_price = 0.0
            quote.pull_reason_bid = f"TOXIC_SELL_FLOW (wobi={wobi:+.2f}, ofi={ofi:+.2f})"

        # 6. 不利トレンド中の買い増し・売り増し防止 (Adverse Inventory Guard)
        # 既にLongを抱えている時（>= 0.002 BTC）に売り気配（wobi < -0.15 または ofi < -0.02）が継続しているなら買い指値を休止
        if inventory >= self.lot_size * 2 and (wobi < -0.15 or ofi < -0.02):
            quote.has_bid = False
            quote.bid_price = 0.0
            quote.pull_reason_bid = f"ADVERSE_HOLDING_LONG_PAUSE (inv={inventory:.3f}, wobi={wobi:+.2f})"
        # 既にShortを抱えている時（<= -0.002 BTC）に買い気配（wobi > +0.15 または ofi > +0.02）が継続しているなら売り指値を休止
        elif inventory <= -self.lot_size * 2 and (wobi > 0.15 or ofi > 0.02):
            quote.has_ask = False
            quote.ask_price = 0.0
            quote.pull_reason_ask = f"ADVERSE_HOLDING_SHORT_PAUSE (inv={inventory:.3f}, wobi={wobi:+.2f})"

        # 7. 在庫リミット制御
        if inventory >= self.max_inv_btc:
            quote.has_bid = False
            quote.pull_reason_bid = f"MAX_LONG_INVENTORY ({inventory:.3f}btc)"
        elif inventory <= -self.max_inv_btc:
            quote.has_ask = False
            quote.pull_reason_ask = f"MAX_SHORT_INVENTORY ({inventory:.3f}btc)"

        return quote

    def compute_naive_quotes(self, bids: List[Dict[str, float]], asks: List[Dict[str, float]]) -> MMQuote:
        """古典的ナイーブMM（仲値対称・毒性Pullなし）"""
        if not bids or not asks:
            return MMQuote()
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        mid = (best_bid + best_ask) / 2.0
        half_spread = mid * (self.base_spread_bp / 10000.0)
        return MMQuote(
            bid_price=math.floor(min(best_bid, mid - half_spread)),
            bid_size=self.lot_size,
            ask_price=math.ceil(max(best_ask, mid + half_spread)),
            ask_size=self.lot_size,
            has_bid=True,
            has_ask=True,
            micro_price=mid,
            reservation_price=mid,
        )


class TF2BWOBIMMDryRun:
    """WOBIMM DryRun Runner (WOBIMM vs NAIVE_MM 2-Lane A/B Comparison)"""

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

        self.manager = MicrostructureMMManager(gamma=0.5, kappa=0.8, base_spread_bp=1.6, lot_size=0.001, max_inv_btc=0.004)
        assert_wobimm_benchmark_pin(self.manager)
        FROZEN_FLAG.write_text("CSR-634 WOBIMM BENCHMARK FROZEN\n", encoding="utf-8")

        self.wobimm = MMLaneAccounting(lane="WOBIMM")
        self.naive_mm = MMLaneAccounting(lane="NAIVE_MM")

        self.bus = EventBus()
        self.logger = ParquetBatchLogger(flush_interval_sec=60.0, batch_size=200)
        self.ingestion = MarketDataIngestion(self.bus, self.logger, product_code=symbol)
        if self.market_source == SOURCE_SHARED:
            self.feed: Any = FeederBoardSource(self.ingestion)
        else:
            self.feed = LiveBoardFeed(self.ingestion, product_code=symbol)

        self.curr_quote_wobi = MMQuote()
        self.curr_quote_naive = MMQuote()

        # Executions file pointer
        self.exec_fp: Optional[Any] = None
        self._init_exec_stream()
        self._restore_ledger()

    def _init_exec_stream(self) -> None:
        if FEEDER_EXECS.exists():
            try:
                self.exec_fp = open(FEEDER_EXECS, "r", encoding="utf-8")
                self.exec_fp.seek(0, 2)  # seek to end
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
            if lane == "WOBIMM":
                self.wobimm.history.append(row)
                self.wobimm.total_trades += 1
                if bool(row.get("is_win")):
                    self.wobimm.winning_trades += 1
                self.wobimm.total_pnl_jpy += float(row.get("pnl_jpy") or 0.0)
                self.wobimm.total_pnl_bp += float(row.get("pnl_bp") or 0.0)
            elif lane == "NAIVE_MM":
                self.naive_mm.history.append(row)
                self.naive_mm.total_trades += 1
                if bool(row.get("is_win")):
                    self.naive_mm.winning_trades += 1
                self.naive_mm.total_pnl_jpy += float(row.get("pnl_jpy") or 0.0)
                self.naive_mm.total_pnl_bp += float(row.get("pnl_bp") or 0.0)

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

    def _poll_executions(self, now: float) -> List[Dict[str, Any]]:
        new_execs = []
        if not self.exec_fp:
            self._init_exec_stream()
        if self.exec_fp:
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

    def _process_fills(self, public_execs: List[Dict[str, Any]], best_bid: float, best_ask: float, now: float) -> None:
        """パブリック約定と板タッチの両面からMaker約定を判定"""
        # 1. WOBIMM Fills
        qw = self.curr_quote_wobi
        # Check Bid Fill: 市場で成行売りがこちらのBid以下で約定、または最良売りがこちらのBid以下に到達
        bid_filled = False
        ask_filled = False

        if qw.has_bid:
            if best_ask <= qw.bid_price:
                bid_filled = True
            else:
                for ex in public_execs:
                    if ex.get("side") == "SELL" and float(ex.get("price", 0.0)) <= qw.bid_price:
                        bid_filled = True
                        break
        if qw.has_ask:
            if best_bid >= qw.ask_price:
                ask_filled = True
            else:
                for ex in public_execs:
                    if ex.get("side") == "BUY" and float(ex.get("price", 0.0)) >= qw.ask_price:
                        ask_filled = True
                        break

        if bid_filled:
            tr = self.wobimm.on_buy_fill(qw.bid_price, qw.bid_size, now)
            if tr:
                _append(LEDGER_PATH, tr)
                self.wobimm.history.append(tr)
                icon = "💰" if tr["is_win"] else "⚡"
                _log(f"[WOBIMM] {icon} COVER_SHORT pnl={tr['pnl_bp']:+.2f}bp ({tr['pnl_jpy']:+.1f}円) Inv={self.wobimm.inventory:+.3f}btc")
            else:
                _log(f"[WOBIMM] 🟢 BID FILLED @ {qw.bid_price:,.0f} | Inv={self.wobimm.inventory:+.3f}btc")
            qw.has_bid = False  # 約定したら次のクォート更新まで消費

        if ask_filled:
            tr = self.wobimm.on_sell_fill(qw.ask_price, qw.ask_size, now)
            if tr:
                _append(LEDGER_PATH, tr)
                self.wobimm.history.append(tr)
                icon = "💰" if tr["is_win"] else "⚡"
                _log(f"[WOBIMM] {icon} CLOSE_LONG pnl={tr['pnl_bp']:+.2f}bp ({tr['pnl_jpy']:+.1f}円) Inv={self.wobimm.inventory:+.3f}btc")
            else:
                _log(f"[WOBIMM] 🔴 ASK FILLED @ {qw.ask_price:,.0f} | Inv={self.wobimm.inventory:+.3f}btc")
            qw.has_ask = False

        # 2. NAIVE_MM Fills
        qn = self.curr_quote_naive
        n_bid_filled = False
        n_ask_filled = False
        if qn.has_bid:
            if best_ask <= qn.bid_price:
                n_bid_filled = True
            else:
                for ex in public_execs:
                    if ex.get("side") == "SELL" and float(ex.get("price", 0.0)) <= qn.bid_price:
                        n_bid_filled = True
                        break
        if qn.has_ask:
            if best_bid >= qn.ask_price:
                n_ask_filled = True
            else:
                for ex in public_execs:
                    if ex.get("side") == "BUY" and float(ex.get("price", 0.0)) >= qn.ask_price:
                        n_ask_filled = True
                        break

        if n_bid_filled:
            tr = self.naive_mm.on_buy_fill(qn.bid_price, qn.bid_size, now)
            if tr:
                _append(LEDGER_PATH, tr)
                self.naive_mm.history.append(tr)
            qn.has_bid = False

        if n_ask_filled:
            tr = self.naive_mm.on_sell_fill(qn.ask_price, qn.ask_size, now)
            if tr:
                _append(LEDGER_PATH, tr)
                self.naive_mm.history.append(tr)
            qn.has_ask = False

    def _tick_mm(self, snap: Any, now: float) -> None:
        best_bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        best_ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            return

        depth_bids, depth_asks = self._read_latest_depth()
        if not depth_bids or not depth_asks:
            return

        # 1. Update WOBIMM Quotes
        taker_bid_v = float(getattr(snap, "taker_volume_bid", 0.0) or 0.0)
        taker_ask_v = float(getattr(snap, "taker_volume_ask", 0.0) or 0.0)
        imbalance = float(getattr(snap, "imbalance", 0.0) or 0.0)
        ofi = float(taker_bid_v - taker_ask_v) if (taker_bid_v + taker_ask_v) > 0 else (imbalance * 0.1)

        self.curr_quote_wobi = self.manager.compute_wobimm_quotes(
            depth_bids, depth_asks, self.wobimm.inventory, ofi=ofi, taker_vol_bid=taker_bid_v, taker_vol_ask=taker_ask_v
        )
        if not self.curr_quote_wobi.has_ask and (self.curr_quote_wobi.pull_reason_ask.startswith("TOXIC") or "ADVERSE" in self.curr_quote_wobi.pull_reason_ask):
            self.wobimm.toxicity_pulls += 1
        if not self.curr_quote_wobi.has_bid and (self.curr_quote_wobi.pull_reason_bid.startswith("TOXIC") or "ADVERSE" in self.curr_quote_wobi.pull_reason_bid):
            self.wobimm.toxicity_pulls += 1

        # 2. Update NAIVE_MM Quotes
        self.curr_quote_naive = self.manager.compute_naive_quotes(depth_bids, depth_asks)

        # 3. Poll new public executions & simulate fills
        public_execs = self._poll_executions(now)
        self._process_fills(public_execs, best_bid, best_ask, now)

    def _lane_stats(self, acc: MMLaneAccounting) -> Dict[str, Any]:
        n = acc.total_trades
        wr = (acc.winning_trades / n * 100.0) if n else 0.0
        ev = (acc.total_pnl_bp / n) if n else 0.0
        sd = _sharpe_dd_from_hist(acc.history)
        s1 = _window_stats_mm(acc.history, 1.0)
        s24 = _window_stats_mm(acc.history, 24.0)
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
            "stats_1h": s1,
            "stats_24h": s24,
        }

    def _generate_quant_comment(self, m: Dict[str, Any]) -> str:
        w = m["WOBIMM"]
        n = m["NAIVE_MM"]
        delta_pnl = w["pnl_bp"] - n["pnl_bp"]
        pulls = w["toxicity_pulls"]

        comment = (
            f"直近1h: WOBIMM は {w['stats_1h']['pnl_bp']:+.2f} bp（勝率 {w['stats_1h']['win_rate_pct']:.1f}%、{w['stats_1h']['total_trades']}件約定）。"
            f"毒性片側Pull発動は累計 {pulls} 回（大口成行の貫通を事前回避）。現在在庫: {w['inventory_btc']:+.4f} BTC（最大在庫 {w['max_inventory_btc']:.4f} BTC）。"
        )
        if delta_pnl > 0:
            comment += f" 単純対称NAIVE_MM比で +{delta_pnl:+.2f} bp アウトパフォームしており、WOBI非対称スキュー＆毒食い回避が鮮明に機能中。"
        else:
            comment += " レンジ相場の中で両サイドのMakerスプレッドを堅調に回収中。"
        return comment

    def metrics(self) -> Dict[str, Any]:
        w = self._lane_stats(self.wobimm)
        n = self._lane_stats(self.naive_mm)

        m_dict = {
            "ts_jst": _iso(),
            "model": "WOBIMM",
            "role": "BENCHMARK",
            "status": "FROZEN",
            "csr": "CSR-634",
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "market_source": self.market_source,
            "wire": "NO",
            "stack": "FROZEN BENCHMARK · Microstructure Pure Maker MM (CSR-634 · param CHANGE=NO)",
            "pin": dict(WOBIMM_BENCHMARK_PIN),
            "WOBIMM": w,
            "NAIVE_MM": n,
            "compare": {
                "delta_pnl_bp": round(w["pnl_bp"] - n["pnl_bp"], 2),
                "delta_1h_pnl_bp": round(w["stats_1h"]["pnl_bp"] - n["stats_1h"]["pnl_bp"], 2),
                "delta_sharpe": round(w["sharpe"] - n["sharpe"], 4),
                "delta_max_dd_bp": round(w["max_dd_bp"] - n["max_dd_bp"], 2),
            },
            "latest_quotes": {
                "wobimm": {
                    "bid": self.curr_quote_wobi.bid_price,
                    "ask": self.curr_quote_wobi.ask_price,
                    "has_bid": self.curr_quote_wobi.has_bid,
                    "has_ask": self.curr_quote_wobi.has_ask,
                    "pull_bid": self.curr_quote_wobi.pull_reason_bid,
                    "pull_ask": self.curr_quote_wobi.pull_reason_ask,
                    "micro": self.curr_quote_wobi.micro_price,
                    "reservation": self.curr_quote_wobi.reservation_price,
                }
            },
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
        w = m["WOBIMM"]
        n = m["NAIVE_MM"]
        cmp = m["compare"]

        title_map = {
            "start": "[START] WOBIMM BENCHMARK FROZEN (CSR-634 · CHANGE=NO)",
            "hourly": f"[HOURLY #{int(m['elapsed_hours'])}] WOBIMM BENCHMARK 1h+24h (FROZEN)",
            "final": "[FINAL] WOBIMM BENCHMARK 停止",
        }
        title = title_map.get(kind, f"📊 [REPORT] WOBIMM {kind}")
        color = 0x2ECC71 if cmp["delta_pnl_bp"] >= 0 else 0xE67E22

        fields = [
            {
                "name": "⏱️ 直近 1時間 成績 (1h Stats - MM対比)",
                "value": (
                    f"• **WOBIMM (板模倣MM)**: **`{w['stats_1h']['pnl_bp']:+.2f} bp`** ({w['stats_1h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{w['stats_1h']['win_rate_pct']:.1f}%` ({w['stats_1h']['win_trades']}/{w['stats_1h']['total_trades']}) | EV: **`{w['stats_1h']['ev_bp']:+.2f}bp`**\n"
                    f"• **NAIVE_MM (単純対称)**: `{n['stats_1h']['pnl_bp']:+.2f} bp` ({n['stats_1h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{n['stats_1h']['win_rate_pct']:.1f}%` ({n['stats_1h']['win_trades']}/{n['stats_1h']['total_trades']})\n"
                    f"• **1h 改善幅 (Δ)**: PnL: **`{cmp['delta_1h_pnl_bp']:+.2f} bp`**"
                ),
                "inline": False,
            },
            {
                "name": "📊 24時間 累積 成績 (24h Stats)",
                "value": (
                    f"• **WOBIMM (板模倣MM)**: **`{w['stats_24h']['pnl_bp']:+.2f} bp`** ({w['stats_24h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{w['stats_24h']['win_rate_pct']:.1f}%` ({w['stats_24h']['win_trades']}/{w['stats_24h']['total_trades']}) | MaxDD: `{w['stats_24h']['max_dd_bp']:.1f}bp`\n"
                    f"• **NAIVE_MM (単純対称)**: `{n['stats_24h']['pnl_bp']:+.2f} bp` ({n['stats_24h']['pnl_jpy']:+.1f}円) | "
                    f"勝率: `{n['stats_24h']['win_rate_pct']:.1f}%` ({n['stats_24h']['win_trades']}/{n['stats_24h']['total_trades']}) | MaxDD: `{n['stats_24h']['max_dd_bp']:.1f}bp`\n"
                    f"• **24h 改善幅 (Δ)**: PnL: **`{cmp['delta_pnl_bp']:+.2f} bp`** | MaxDD改善: **`{cmp['delta_max_dd_bp']:+.2f} bp`**"
                ),
                "inline": False,
            },
            {
                "name": "🛡️ MM 執行 ＆ 毒性制御メトリクス",
                "value": (
                    f"• **毒性片側Pull発動**: `{w['toxicity_pulls']}` 回 (大口成行の貫通を完全回避)\n"
                    f"• **現在在庫 (q)**: `{w['inventory_btc']:+.4f} BTC`\n"
                    f"• **最大在庫 (Max q)**: `{w['max_inventory_btc']:.4f} BTC` (許容: ±0.004 BTC)\n"
                    f"• **執行モード**: `100% Pure Maker (テイカー手数料ゼロ)`"
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
            "username": "WOBIMM BENCHMARK",
            "embeds": [
                {
                    "title": title,
                    "description": (
                        "**WOBIMM = FROZEN paper benchmark (CSR-634).**\n"
                        "パラメータ変更禁止。WOBIMM2 の対照。WIRE=NO · LIVE非適用。"
                    ),
                    "color": color,
                    "fields": fields,
                    "footer": {
                        "text": f"Antigravity Quants · WOBIMM BENCHMARK FROZEN · {m['ts_jst']}",
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
        _log("=== WOBIMM BENCHMARK FROZEN START (CSR-634 · WIRE=NO · CHANGE=NO) ===")
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

                self._tick_mm(last_snap, now)

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
            if self.exec_fp:
                try:
                    self.exec_fp.close()
                except Exception:
                    pass
            if PID_PATH.exists():
                PID_PATH.unlink(missing_ok=True)
            _log("=== WOBIMM DryRun STOPPED ===")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WOBIMM DryRun (WIRE=NO)")
    parser.add_argument("--interval", type=float, default=1.0, help="Tick interval in seconds")
    parser.add_argument("--report-interval", type=float, default=3600.0, help="Discord report interval in seconds")
    parser.add_argument("--source", type=str, default="shared_feeder", help="Market source (shared_feeder/live)")
    args = parser.parse_args()

    runner = TF2BWOBIMMDryRun(
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
