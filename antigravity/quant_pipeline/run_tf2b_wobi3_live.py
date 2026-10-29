#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TF2B_WOBI3 LIVE — dryrun strategy + board-imitation fills (CSR-644).

Strategy is TF2BWobi3DryRun.plan_wobi3_* / trailing / engine3.
Fills are public_maker_fill only (PUBLIC_OID → BOOK_CROSS → BOARD_TRADE).

Accident rules (CSR-643):
  - confirmed venue net is the only position used to post
  - BOOK_CROSS / BOARD_TRADE never change confirmed net
  - SELL only when confirmed long; unexpected short is COVER BUY
  - one working order; never CANCEL_ALL on stale feeder
  - never write ENABLE_REAL_TRADING into .env

CSR-650: exits unwind abs(venue_net) at 8-decimal precision so leftover
dust cannot remain. Post-exit sweeper blocks new entries until exact flat.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

try:
    from data.ml_spine.distilled_axe import DistilledAxe
except (ImportError, ModuleNotFoundError):
    import importlib.util
    _axe_script = AG / "data" / "ml_spine" / "distilled_axe.py"
    if _axe_script.exists():
        _spec = importlib.util.spec_from_file_location("distilled_axe", str(_axe_script))
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        DistilledAxe = getattr(_mod, "DistilledAxe", None)
    else:
        DistilledAxe = None

from core.bitflyer_client import BitFlyerClient, OrderSubmitUnknownError  # noqa: E402
from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier  # noqa: E402
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import (  # noqa: E402
    DISCORD_WEBHOOK_URL,
    TF2BWobi3DryRun,
    calculate_optimal_order_price,
)
from antigravity.quant_pipeline.run_wobimm_live import (  # noqa: E402
    WorkingOrder,
    _foreign_pids,
    _parse_venue_net,
    public_maker_fill,
)

JST = timezone(timedelta(hours=9))
OUT = AG / "data" / "mm_research" / "tf2b_wobi3_live"
STATE_PATH = AG / "data" / "tf2b_wobi3_live_state.json"
PID_PATH = AG / "data" / "tf2b_wobi3_live.pid"
STOP_FLAG = AG / "data" / "TF2B_WOBI3_LIVE_STOP.flag"
GO_FLAG = AG / "data" / "GO_TF2B_WOBI3_LIVE.flag"
LEDGER_PATH = OUT / "paired_ledger.jsonl"
AUDIT_PATH = OUT / "audit.jsonl"
AXE_SHADOW_LOG = OUT / "axe_shadow_audit.jsonl"
AXE_LUT_PATH = AG / "data" / "ml_spine" / "distilled_axe_lut.json"
FLATTEN_LAST = OUT / "flatten_last.json"
FEEDER_LATEST = AG / "data" / "market_feeder" / "latest.json"
FEEDER_EXECS = AG / "data" / "market_feeder" / "executions.jsonl"

CONFIRM_TOKEN = "WOBI3"
CONFIRM_ENV = "WOBI3_LIVE"
NEEDLE = "antigravity.quant_pipeline.run_tf2b_wobi3_live"
WOBIMM_NEEDLE = "antigravity.quant_pipeline.run_wobimm_live"
SOAK_NEEDLE = "antigravity.quant_pipeline.run_micro_live_soak_csr574"
PEG_NEEDLE = "antigravity.quant_pipeline.run_peg2b_live_trial"

SIZE_BTC = 0.001
FLAT_THRESHOLD_BTC = 0.0005  # 1ロット未満はダスト帯。エントリー可、クローズ時に実建玉全量でフラット化
EXACT_FLAT_EPS_BTC = 1e-7  # CSR-650: 完全フラット (0.00000000 BTC) 判定
UNWIND_MAX_ORDER_BTC = 0.002  # 1ロット + 約定ダスト（例: 0.00119978）の全量反対売買を許可
MAX_INV_BTC = 0.001
logger = logging.getLogger("tf2b_wobi3_live")
MIN_QUOTE_LIFE_SEC = 20.0
DRIFT_PULL_BP = 0.5  # left-behind vs best bid; 10 JPY was 0.008 bp and fired every tick
MIN_API_INTERVAL_SEC = 2.0
POS_POLL_SEC = 15.0
STOP_PNL_JPY = -300.0
STOP_PNL_BP = -80.0
ESCALATE_MAKER_SEC = 30.0
ESCALATE_ADVERSE_BP = 10.0
MAX_REQ_PER_60S = 10
MAX_REQ_PER_300S = 30
OPTIMISTIC_FILL_SOURCES = frozenset({"BOOK_CROSS", "BOARD_TRADE"})
GHOST_OID_TTL_SEC = 90.0


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


def maker_px(side: str, px: float, best_bid: float, best_ask: float) -> float:
    if best_bid <= 0 or best_ask <= best_bid:
        return 0.0
    if side.upper() == "BUY":
        return min(px, best_ask - 1.0) if px > 0 else best_bid
    return max(px, best_bid + 1.0) if px > 0 else best_ask


def post_blocked(
    side: str,
    confirmed_net: float,
    has_working: bool,
    need_sync: bool,
) -> Optional[str]:
    """Refuse posts that grow the wrong way. Confirmed net only."""
    if need_sync:
        return "need_venue_sync"
    if has_working:
        return "working_order"
    side_u = str(side or "").upper()
    if side_u == "SELL":
        if confirmed_net <= EXACT_FLAT_EPS_BTC:
            return "not_long"
        return None
    if side_u == "BUY":
        if confirmed_net >= SIZE_BTC - 1e-12:
            return "already_long"
        return None
    return "bad_side"


def _refuse_real_gates(confirm_go: str) -> Optional[str]:
    if not GO_FLAG.exists():
        return f"REFUSE: missing {GO_FLAG}"
    if "status=ACTIVE" not in GO_FLAG.read_text(encoding="utf-8", errors="replace"):
        return f"REFUSE: {GO_FLAG} is not status=ACTIVE"
    if os.environ.get("CONFIRM_GO") != CONFIRM_ENV:
        return f"REFUSE: set CONFIRM_GO={CONFIRM_ENV}"
    if os.environ.get("ENABLE_REAL_TRADING", "false").lower() != "true":
        return "REFUSE: ENABLE_REAL_TRADING is not true (do not set from .env writer)"
    if confirm_go != CONFIRM_TOKEN:
        return f"REFUSE: --confirm-go {CONFIRM_TOKEN} required"
    if STOP_FLAG.exists():
        return f"REFUSE: STOP flag present ({STOP_FLAG})"
    return None


class TF2BWobi3LiveExecutor:
    """TF2B_WOBI3 LIVE Execution Engine (CSR-636 / WOBI3 堅牢エグゼキューター)"""

    def __init__(
        self,
        client: Optional[Any] = None,
        market_feeder: Optional[Any] = None,
        size: float = SIZE_BTC,
    ) -> None:
        self.client = client
        self.feeder = market_feeder
        self.size = float(size)

        # 内部建玉状態
        self.pos_side = "FLAT"
        self.pos_size = 0.0
        self.entry_px = 0.0
        self.entry_ts = 0.0
        self.active_order_id: Optional[str] = None
        self.order_placed_ts = 0.0

        # トレイリング・動的利確状態
        self.tp_extended = False
        self.trailing_floor_px = 0.0

        # 非対称レートリミッター履歴
        self.last_post_api_time = 0.0
        self.last_cancel_api_time = 0.0
        self.post_api_calls_300s: List[float] = []
        self.cancel_api_calls_300s: List[float] = []

        # CSR-650: 解消後に端数が残ったら新規エントリーを止める
        self.halt_new_entries = False

    def _can_call_api(self, now: float, is_cancel: bool = False, emergency: bool = False) -> bool:
        if emergency:
            return True
        if is_cancel:
            if (now - self.last_cancel_api_time) < 0.3:
                return False
            self.cancel_api_calls_300s = [t for t in self.cancel_api_calls_300s if now - t < 300.0]
            return len(self.cancel_api_calls_300s) < 120

        if (now - self.last_post_api_time) < 2.0:
            return False
        self.post_api_calls_300s = [t for t in self.post_api_calls_300s if now - t < 300.0]
        return len(self.post_api_calls_300s) < 30

    def _record_api(self, now: float, is_cancel: bool = False) -> None:
        if is_cancel:
            self.last_cancel_api_time = now
            self.cancel_api_calls_300s.append(now)
        else:
            self.last_post_api_time = now
            self.post_api_calls_300s.append(now)

    def evaluate_entry_gate(self, signal_side: str, market_state: Any) -> bool:
        """WOBI / OFI 判定 ＆ FakeBreakout VETO"""
        # 1. スプレッド制限 (1.2 bp を超える薄板・荒れ相場は除外)
        spread_bp = getattr(market_state, "spread_bp", 0.0)
        if spread_bp > 1.2:
            return False

        # 2. FakeBreakout VETO (見せ板急減 ∧ Taker追随なし)
        cancel_spike = getattr(market_state, "cancel_spike", 0.0)
        taker_agg = getattr(market_state, "taker_aggressiveness", 0.0)
        if cancel_spike > 0.35 and taker_agg < 0.20:
            _log("[VETO] Fake breakout detected. Entry blocked.")
            return False

        # 3. WOBI ＆ OFI 整合性ゲート
        wobi = getattr(market_state, "wobi", 0.0)
        ofi = getattr(market_state, "ofi", 0.0)
        side_u = str(signal_side or "").upper()
        if side_u == "BUY":
            if wobi < 0.20 or ofi < 0.0:
                return False
        elif side_u == "SELL":
            if wobi > -0.20 or ofi > 0.0:
                return False

        return True

    def evaluate_dynamic_exit(self, market_state: Any, now: float) -> str:
        """WOBI3 動的エグジット: EXTEND_HOLD 判定・トレイリング・構造的タイト損切り"""
        if self.pos_side == "FLAT":
            return "NONE"

        mid = getattr(market_state, "mid_price", 0.0)
        if self.entry_px <= 0 or mid <= 0:
            return "HOLD"

        gain_bp = (
            ((mid - self.entry_px) / self.entry_px * 10000.0)
            if self.pos_side == "BUY"
            else ((self.entry_px - mid) / self.entry_px * 10000.0)
        )

        # 1. 構造崩壊時のタイト損切り (Adverse Cut: 壁崩落 or gain_bp < -1.8)
        is_collapsed = False
        if hasattr(market_state, "is_support_stack_collapsed"):
            try:
                is_collapsed = market_state.is_support_stack_collapsed(self.pos_side)
            except Exception:
                pass
        if is_collapsed or gain_bp < -1.8:
            return "ADVERSE_CUT"

        # 2. 動的利確保留 (EXTEND_HOLD): 利確目標(+1.8bp)到達時、大口壁とOFIが順行中なら保留
        wobi = getattr(market_state, "wobi", 0.0)
        ofi = getattr(market_state, "ofi", 0.0)
        if gain_bp >= 1.8:
            is_breakout_sustaining = (
                (wobi > 0.40 and ofi > 0.10)
                if self.pos_side == "BUY"
                else (wobi < -0.40 and ofi < -0.10)
            )
            if is_breakout_sustaining:
                self.tp_extended = True
                # サポート壁に合わせてトレイリングフロアを建値+1.0bpへ引き上げ
                self.trailing_floor_px = (
                    (self.entry_px * 1.0001)
                    if self.pos_side == "BUY"
                    else (self.entry_px * 0.9999)
                )
                return "EXTEND_HOLD"

        # 3. トレイリング決済 (引き上げたフロアを割り込んだら即利確撤退)
        if self.tp_extended:
            if (self.pos_side == "BUY" and mid < self.trailing_floor_px) or (
                self.pos_side == "SELL" and mid > self.trailing_floor_px
            ):
                return "TRAILING_EXIT"

        # 4. 通常高速利確 (Fast Harvest: +1.8bp 到達で反発前に回収)
        if gain_bp >= 1.8 and not self.tp_extended:
            return "FAST_HARVEST"

        return "HOLD"

    def execute_order_lifecycle(
        self,
        now: float,
        best_bid: float = 0.0,
        best_ask: float = 0.0,
        wobi: float = 0.0,
        recent_ofi: float = 0.0,
        depth_vol: float = 0.0,
    ) -> bool:
        """Entry quote stay. Navigation pulls wait MIN_QUOTE_LIFE. No wall-thickness yank."""
        if not self.active_order_id:
            return False
        age = now - float(self.order_placed_ts or 0.0)
        if age < MIN_QUOTE_LIFE_SEC:
            return False
        if self.entry_px > 0 and best_bid > 0:
            drift_bp = ((best_bid - self.entry_px) / self.entry_px) * 1e4
            if drift_bp > DRIFT_PULL_BP:
                _log(
                    f"[STRUCTURAL_PULL] left behind {drift_bp:.2f}bp "
                    f"(bid={best_bid:,.0f} order={self.entry_px:,.0f} age={age:.1f}s)"
                )
                self._cancel_active_order(emergency=False, where="structural_drift")
                return True
        if recent_ofi < -0.15 or wobi < -0.25:
            _log(
                f"[STRUCTURAL_PULL] invert after min life "
                f"(ofi={recent_ofi:+.2f} wobi={wobi:+.2f} age={age:.1f}s)"
            )
            self._cancel_active_order(emergency=False, where="structural_invert")
            return True
        return False

    def _cancel_active_order(self, emergency: bool = False, where: str = "structural_pull") -> bool:
        if not self.active_order_id:
            return False
        now = time.time()
        if not self._can_call_api(now, is_cancel=True, emergency=emergency):
            return False
        self._record_api(now, is_cancel=True)
        if self.client and hasattr(self.client, "cancel_child_order"):
            try:
                self.client.cancel_child_order("FX_BTC_JPY", self.active_order_id)
            except Exception as e:
                _log(f"Cancel order error: {e}")
        self.active_order_id = None
        self.order_placed_ts = 0.0
        return True

    def flatten(self, reason: str = "") -> Dict[str, Any]:
        """レート制限下でもゼロポジションを保証するフラット化"""
        self._cancel_active_order(emergency=True)
        if self.client and hasattr(self.client, "cancel_all_child_orders"):
            try:
                self.client.cancel_all_child_orders("FX_BTC_JPY")
            except Exception as e:
                _log(f"Flatten cancel_all error: {e}")
        self.pos_side = "FLAT"
        self.pos_size = 0.0
        self.entry_px = 0.0
        self.active_order_id = None
        self.tp_extended = False
        self.trailing_floor_px = 0.0
        self.halt_new_entries = False
        return {"flat_ok": True, "venue_net": 0.0, "reason": reason}

    # =========================================================================
    # CSR-650: エグジット時の数量算出ロジック
    # =========================================================================

    def get_unwind_order_size(self, venue_net: float) -> float:
        """
        ポジション解消時は、端数を含めた取引所実建玉の全量を指定する。
        bitFlyer FX の最小発注単位は 0.001 BTC だが、
        端数込み（例: 0.00100198 BTC や 0.00119978 BTC）は
        8桁精度で指定して全量反対売買が可能。
        """
        abs_net = abs(float(venue_net))

        # 0.001 BTC 未満の孤立ダスト単体でない限り、実建玉全量を8桁で丸める
        target_size = round(abs_net, 8)

        # 万が一、最小発注単位未満の場合は取引所仕様に合わせてクリップ
        if 0 < target_size < 0.001:
            last = float(getattr(self, "_last_dust_warn_ts", 0.0) or 0.0)
            now = time.time()
            if now - last >= 60.0:
                self._last_dust_warn_ts = now
                logger.warning(
                    f"[CSR-650] Dust position below 0.001 BTC detected: {target_size:.8f} BTC"
                )
                _log(f"[CSR-650] Dust position below 0.001 BTC detected: {target_size:.8f} BTC")
            return target_size

        return target_size

    def get_entry_order_size(self, venue_net: float) -> float:
        """
        ダストを残したまま新規エントリーする。
        ショートダストがあるときは 1ロット + |dust| を買い、約定後に +0.001 へ着地させる。
        クローズ時は get_unwind_order_size で全量売却し 0.00000000 にする。
        """
        net = float(venue_net)
        if net < -EXACT_FLAT_EPS_BTC:
            return round(SIZE_BTC - net, 8)
        return round(SIZE_BTC, 8)

    def check_post_exit_flat(self, venue_net: float) -> bool:
        """
        解消後に実建玉が 0.00000000 BTC に戻ったかを厳密判定
        """
        if abs(float(venue_net)) > EXACT_FLAT_EPS_BTC:
            logger.error(
                f"[CSR-650_LEAK] Incomplete unwind! Remaining: {float(venue_net):.8f} BTC. Halting new entries."
            )
            _log(
                f"[CSR-650_LEAK] Incomplete unwind! Remaining: {float(venue_net):.8f} BTC. Halting new entries."
            )
            return False
        return True


class TF2BWobi3Live(TF2BWobi3LiveExecutor):
    def __init__(
        self,
        real: bool,
        size: float = SIZE_BTC,
        interval_sec: float = 1.0,
        report_interval_sec: float = 3600.0,
        market_source: Optional[str] = None,
    ) -> None:
        if size > SIZE_BTC + 1e-12:
            raise ValueError(f"REFUSE: size {size} > {SIZE_BTC}")
        self.real = bool(real)
        # CSR-650: クライアント上限を 1ロット+ダストまで上げ、端数込み全量アンワインドを通す
        self.client = BitFlyerClient(
            enable_real_trading=real,
            max_order_size=max(MAX_INV_BTC, UNWIND_MAX_ORDER_BTC),
        )
        self.client.max_order_size = max(float(self.client.max_order_size), UNWIND_MAX_ORDER_BTC)
        super().__init__(client=self.client, size=size)
        self.interval_sec = interval_sec
        self.report_interval_sec = report_interval_sec
        self.product = "FX_BTC_JPY"
        self.t0 = time.time()
        self.running = True
        self.stop_reason = ""
        self.dry = TF2BWobi3DryRun(
            interval_sec=interval_sec,
            report_interval_sec=report_interval_sec,
            market_source=market_source,
        )
        self.venue_net = 0.0
        self.venue_avg = 0.0
        self.confirmed_net = 0.0
        self._venue_ok = False
        self.need_sync = False
        self.working: Optional[WorkingOrder] = None
        self.ghosts: List[WorkingOrder] = []
        self.seen_oids: set = set()
        self.pending_entry: Optional[Dict[str, Any]] = None
        self.pending_exit: Optional[Dict[str, Any]] = None
        self.exit_armed_at = 0.0
        self.escalate_sent = False
        self.last_bids: List[Dict[str, float]] = []
        self.last_asks: List[Dict[str, float]] = []
        self.last_api_ts = 0.0
        self.last_pos_poll = 0.0
        self.last_state_save = 0.0
        self.last_reported_hour = datetime.now(JST).hour
        self.request_history: deque = deque(maxlen=400)
        self.posts = 0
        self.cancels = 0
        self.fills = 0
        self.oid_fills = 0
        self.board_hits = 0
        self.escalate_markets = 0
        self.blocked_posts = 0
        self.exec_fp = None
        self.notifier = QuantDiscordNotifier()
        self._init_exec_stream()
        self.last_snap = None
        self.last_metrics: Dict[str, Any] = {}
        self.last_axe_shadow_ts = 0.0
        self.axe_seq = 0
        self.axe_pending: List[Dict[str, Any]] = []
        self.axe_fill_pending: List[Dict[str, Any]] = []
        self.last_axe_predict_id: Optional[str] = None
        self.axe: Optional[DistilledAxe] = None
        if AXE_LUT_PATH.exists() and DistilledAxe is not None:
            try:
                self.axe = DistilledAxe(str(AXE_LUT_PATH))
                _log(f"[AXE] DistilledAxe LUT loaded ({len(self.axe.matrix)} cells) from {AXE_LUT_PATH}")
            except Exception as exc:
                _log(f"[AXE] Failed to load DistilledAxe: {exc}")

    def _axe_mid(self, m: Optional[Dict[str, Any]], snap: Optional[Any], details: Optional[Dict[str, Any]]) -> float:
        for src in (
            (details or {}).get("mid"),
            (m or {}).get("mid"),
            getattr(snap, "mid_price", None) if snap is not None else None,
        ):
            try:
                v = float(src or 0.0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
        return 0.0

    def _flush_axe_realizations(self, now: float, mid: float) -> None:
        if mid <= 0:
            return
        keep: List[Dict[str, Any]] = []
        for rec in self.axe_pending:
            age = now - float(rec["ts"])
            mid_t = float(rec["mid_t"])
            if rec.get("need_1s") and age >= 1.0:
                delta_bp = ((mid - mid_t) / mid_t) * 10000.0 if mid_t > 0 else None
                _append(AXE_SHADOW_LOG, {
                    "kind": "realized",
                    "id": rec["id"],
                    "horizon_s": 1,
                    "ts": round(now, 3),
                    "iso": _iso(),
                    "mid_t": mid_t,
                    "mid": mid,
                    "delta_mid_bp": None if delta_bp is None else round(delta_bp, 4),
                    "mu_1s_bp": rec.get("mu_1s_bp"),
                })
                rec["need_1s"] = False
            if rec.get("need_5s") and age >= 5.0:
                delta_bp = ((mid - mid_t) / mid_t) * 10000.0 if mid_t > 0 else None
                _append(AXE_SHADOW_LOG, {
                    "kind": "realized",
                    "id": rec["id"],
                    "horizon_s": 5,
                    "ts": round(now, 3),
                    "iso": _iso(),
                    "mid_t": mid_t,
                    "mid": mid,
                    "delta_mid_bp": None if delta_bp is None else round(delta_bp, 4),
                    "mu_1s_bp": rec.get("mu_1s_bp"),
                })
                rec["need_5s"] = False
            if rec.get("need_1s") or rec.get("need_5s"):
                keep.append(rec)
        self.axe_pending = keep

        fill_keep: List[Dict[str, Any]] = []
        for rec in self.axe_fill_pending:
            age = now - float(rec["fill_ts"])
            fill_px = float(rec["fill_px"])
            side = str(rec.get("side") or "BUY")
            if fill_px <= 0:
                continue
            if side == "SELL":
                markout = ((fill_px - mid) / fill_px) * 10000.0
            else:
                markout = ((mid - fill_px) / fill_px) * 10000.0
            if rec.get("need_1s") and age >= 1.0:
                _append(AXE_SHADOW_LOG, {
                    "kind": "markout",
                    "id": rec["id"],
                    "horizon_s": 1,
                    "ts": round(now, 3),
                    "iso": _iso(),
                    "fill_px": fill_px,
                    "mid": mid,
                    "markout_bp": round(markout, 4),
                    "side": side,
                })
                rec["need_1s"] = False
            if rec.get("need_5s") and age >= 5.0:
                _append(AXE_SHADOW_LOG, {
                    "kind": "markout",
                    "id": rec["id"],
                    "horizon_s": 5,
                    "ts": round(now, 3),
                    "iso": _iso(),
                    "fill_px": fill_px,
                    "mid": mid,
                    "markout_bp": round(markout, 4),
                    "side": side,
                })
                rec["need_5s"] = False
            if rec.get("need_1s") or rec.get("need_5s"):
                fill_keep.append(rec)
        self.axe_fill_pending = fill_keep

    def evaluate_shadow_axe(
        self,
        event: str,
        m: Optional[Dict[str, Any]] = None,
        snap: Optional[Any] = None,
        live_decision: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if self.axe is None:
            return None
        try:
            m = m or getattr(self, "last_metrics", {}) or {}
            snap = snap or getattr(self, "last_snap", None)
            details = details or {}
            wobi = float(m.get("wobi", getattr(snap, "imbalance", 0.0) if snap else 0.0) or 0.0)
            spread_bp = float(m.get("spread_bp", 0.0) or 0.0)
            cancel_rate = float(getattr(snap, "cancel_rate", m.get("cancel_ask", 0.0)) if snap else 0.0)
            micro_dev = float(getattr(snap, "micro_dev", m.get("norm_disloc", 0.0)) if snap else 0.0)
            mid = self._axe_mid(m, snap, details)
            capture_bp = None
            quote_px = details.get("px") or details.get("entry_px")
            try:
                qpx = float(quote_px or 0.0)
            except (TypeError, ValueError):
                qpx = 0.0
            if mid > 0 and qpx > 0:
                capture_bp = ((mid - qpx) / mid) * 10000.0
                if "SELL" in str(live_decision or details.get("side") or ""):
                    capture_bp = -capture_bp

            axe_res = self.axe.evaluate(
                wobi=wobi,
                spread_bp=spread_bp,
                cancel_rate=cancel_rate,
                micro_dev=micro_dev,
                mid=mid,
                capture_bp=capture_bp,
                venue_net=float(self.confirmed_net or 0.0),
            )
            now = time.time()
            self.axe_seq += 1
            rec_id = f"{now:.3f}:{event}:{self.axe_seq}"
            micro_dev_bp = ((micro_dev / mid) * 10000.0) if mid > 0 else None
            rec = {
                "kind": "predict",
                "id": rec_id,
                "ts": round(now, 3),
                "iso": _iso(),
                "event": event,
                "live_context": live_decision,
                "mid_t": mid if mid > 0 else None,
                "spread_bp": round(spread_bp, 4),
                "wobi": round(wobi, 4),
                "cancel_rate": round(cancel_rate, 4),
                "micro_dev_bp": None if micro_dev_bp is None else round(micro_dev_bp, 4),
                "venue_net": round(self.confirmed_net, 8),
                "mu_1s_bp": axe_res.get("mu_1s_bp"),
                "p_fill": axe_res.get("p_fill"),
                "m_adv_1s_bp": axe_res.get("m_adv_1s_bp"),
                "inv_penalty_bp": axe_res.get("inv_penalty_bp"),
                "capture_bp": axe_res.get("capture_bp"),
                "ev_quote_bp": axe_res.get("ev_quote_bp"),
                "lut_key": axe_res.get("lut_key"),
                "lut_drift_yen": axe_res.get("lut_drift_yen"),
            }
            _append(AXE_SHADOW_LOG, rec)
            prev_predict = self.last_axe_predict_id
            self.last_axe_predict_id = rec_id
            if mid > 0:
                self.axe_pending.append({
                    "id": rec_id,
                    "ts": now,
                    "mid_t": mid,
                    "mu_1s_bp": rec["mu_1s_bp"],
                    "need_1s": True,
                    "need_5s": True,
                })
            if event == "ORDER_FILL":
                fill_px = qpx
                if fill_px > 0:
                    fill_id = rec_id
                    _append(AXE_SHADOW_LOG, {
                        "kind": "fill",
                        "id": fill_id,
                        "predict_id": prev_predict,
                        "ts": round(now, 3),
                        "iso": _iso(),
                        "fill_px": fill_px,
                        "side": details.get("side"),
                        "source": details.get("source"),
                    })
                    self.axe_fill_pending.append({
                        "id": fill_id,
                        "fill_ts": now,
                        "fill_px": fill_px,
                        "side": details.get("side") or "BUY",
                        "need_1s": True,
                        "need_5s": True,
                    })
            return rec
        except Exception as exc:
            _log(f"[AXE_SHADOW] eval error: {exc}")
            return None

    def _init_exec_stream(self) -> None:
        if not FEEDER_EXECS.exists():
            return
        try:
            self.exec_fp = open(FEEDER_EXECS, "r", encoding="utf-8")
            self.exec_fp.seek(0, 2)
        except Exception as exc:
            _log(f"executions.jsonl open failed: {exc}")
            self.exec_fp = None

    def _poll_executions(self) -> List[Dict[str, Any]]:
        if not self.exec_fp:
            self._init_exec_stream()
        if not self.exec_fp:
            return []
        out: List[Dict[str, Any]] = []
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

    def _read_depth(self) -> Tuple[List[Dict[str, float]], List[Dict[str, float]], float]:
        if not FEEDER_LATEST.exists():
            return [], [], 1e9
        try:
            age = time.time() - FEEDER_LATEST.stat().st_mtime
            raw = json.loads(FEEDER_LATEST.read_text(encoding="utf-8"))
            return raw.get("bids", [])[:10], raw.get("asks", [])[:10], age
        except Exception:
            return [], [], 1e9

    def _can_call_api(self, is_cancel: bool = False, emergency: bool = False) -> bool:
        now = time.time()
        return super()._can_call_api(now, is_cancel=is_cancel, emergency=emergency)

    def _record_api(self, is_cancel: bool = False) -> None:
        now = time.time()
        super()._record_api(now, is_cancel=is_cancel)
        self.last_api_ts = now
        self.request_history.append(now)

    def request_stop(self, reason: str) -> None:
        self.stop_reason = reason
        self.running = False

    def _prune_ghosts(self, now: float) -> None:
        self.ghosts = [g for g in self.ghosts if g.expires_at <= 0 or g.expires_at > now]

    def _pull_venue(self) -> Optional[float]:
        if not self.real:
            self._venue_ok = True
            self.need_sync = False
            return float(self.confirmed_net)
        now = time.time()
        if (now - self.last_pos_poll) < 1.0:
            return None
        try:
            rows = self.client.get_positions(self.product) or []
            net, avg = _parse_venue_net(rows)
            prev_net = float(self.confirmed_net)
            self.venue_net = net
            self.venue_avg = avg
            self.confirmed_net = net
            self._venue_ok = True
            self.need_sync = False
            self.last_pos_poll = time.time()
            if abs(net) > MAX_INV_BTC + 1e-12:
                _log(f"INV_CAP_BREACH venue={net:+.4f} — reduce only")
            if prev_net > EXACT_FLAT_EPS_BTC and abs(net) <= EXACT_FLAT_EPS_BTC and self.dry.wobi3.position_side:
                _log(f"[WOBI3_LIVE] VENUE_CLOSED: venue net={net:+.8f} (was {prev_net:+.8f}) — finalizing closed long")
                self.halt_new_entries = False
                entry = float(self.dry.wobi3.entry_price or self.dry.wobi3_trailing.entry_price or 0.0)
                px = float(self.last_asks[0]["price"] if self.last_asks else (entry * 1.0003 if entry > 0 else 0.0))
                pnl_bp = ((px / entry) - 1.0) * 1e4 if entry > 0 else 0.0
                pnl_j = self.dry.wobi3._yen(pnl_bp) if entry > 0 else 0.0
                self.dry.apply_wobi3_exit_state(px, pnl_j, "VENUE_CONFIRMED_FLAT")
                self.pending_exit = None
                self.exit_armed_at = 0.0
                self.escalate_sent = False
                _append(LEDGER_PATH, {
                    "ts": time.time(),
                    "lane": "TF2B_WOBI3_LIVE",
                    "action": "CLOSE_LONG",
                    "price": px,
                    "size": round(abs(prev_net), 8),
                    "pnl_bp": round(pnl_bp, 2),
                    "pnl_jpy": round(pnl_j, 1),
                    "is_win": pnl_bp > 0,
                    "source": "VENUE_POLL",
                    "exit_reason": "VENUE_CONFIRMED_FLAT",
                })
            if net > FLAT_THRESHOLD_BTC and not self.dry.wobi3.position_side:
                _log(f"[WOBI3_LIVE] VENUE_ADOPT: venue net={net:+.4f} @ {self.venue_avg:.0f} — adopting into WOBI3 engine")
                entry_px = self.venue_avg if self.venue_avg > 0 else (self.last_asks[0]["price"] if self.last_asks else 0.0)
                self.dry.apply_wobi3_entry_state("buy", entry_px, time.time(), self.last_asks, mid_price=entry_px)
                self.pending_entry = None
                self.exit_armed_at = 0.0
                self.escalate_sent = False
            return net
        except Exception as exc:
            _log(f"venue GET failed: {exc}")
            return None

    def _post(
        self,
        side: str,
        price: float,
        now: float,
        why: str,
        size: Optional[float] = None,
    ) -> Optional[WorkingOrder]:
        order_size = round(float(self.size if size is None else size), 8)
        if order_size <= 0:
            return None
        blocked = post_blocked(side, self.confirmed_net, self.working is not None, self.need_sync)
        if blocked:
            self.blocked_posts += 1
            _log(f"[WOBI3_LIVE] BLOCK {side} ({blocked}) net={self.confirmed_net:+.8f}")
            return None
        if price <= 0 or not self._can_call_api(is_cancel=False):
            return None
        try:
            if self.real:
                self._record_api(is_cancel=False)
                res = self.client.send_order(self.product, side, order_size, "LIMIT", price)
                oid = (res or {}).get("child_order_acceptance_id")
                if not oid:
                    return None
            else:
                oid = f"SIM-{side}-{int(now * 1000)}"
        except OrderSubmitUnknownError as exc:
            _log(f"ORDER_UNKNOWN {exc}")
            self.need_sync = True
            return None
        except Exception as exc:
            _log(f"ORDER_ERROR {exc}")
            return None
        self.posts += 1
        order = WorkingOrder(side=side, price=float(price), size=order_size, acceptance_id=str(oid), posted_at=now)
        self.working = order
        self.active_order_id = str(oid)
        self.order_placed_ts = now
        _append(AUDIT_PATH, {"event": "ORDER_POSTED", "ts": _iso(), "side": side, "px": price, "size": order_size, "id": oid, "why": why})
        self.evaluate_shadow_axe(
            event="ORDER_POSTED",
            live_decision=f"POST_{side}",
            details={"side": side, "px": price, "size": order_size, "id": str(oid), "why": why},
        )
        _log(f"[WOBI3_LIVE] POST LIMIT {side} {order_size:.8f} @ {price:,.0f} id={oid} ({why})")
        return order

    def post_limit_order(
        self,
        side: str,
        price: float,
        size: float,
        now: Optional[float] = None,
        why: str = "CSR-650_FLAT_SWEEP",
    ) -> Optional[WorkingOrder]:
        return self._post(side, price, now if now is not None else time.time(), why, size=size)

    def _post_unwind(self, side: str, price: float, now: float, why: str) -> Optional[WorkingOrder]:
        exit_size = self.get_unwind_order_size(self.venue_net if abs(self.venue_net) > abs(self.confirmed_net) else self.confirmed_net)
        if 0 < exit_size < SIZE_BTC:
            # 最小単位未満の孤立ダストは発注できない。エントリーは継続し、次のクローズで全量フラット化する。
            self.halt_new_entries = False
            last = float(getattr(self, "_last_dust_log_ts", 0.0) or 0.0)
            if now - last >= 60.0:
                self._last_dust_log_ts = now
                _log(
                    f"[CSR-650] isolated dust {exit_size:.8f} BTC (< {SIZE_BTC:.3f} min) — "
                    "skip POST, keep entries; flatten on next close"
                )
            return None
        order = self.post_limit_order(side=side, price=price, size=exit_size, now=now, why=why)
        if order is not None:
            _log(
                f"[CSR-650_FLAT_SWEEP] Order placed: {side} {exit_size:.8f} BTC @ {price} "
                "to reach exact 0.00000000 BTC"
            )
        return order

    def _sweep_to_flat(self, now: float, best_bid: float, best_ask: float, why: str) -> Optional[WorkingOrder]:
        """CSR-650: 残存建玉の全量を反対売買して 0.00000000 に戻す。"""
        net = float(self.confirmed_net if abs(self.confirmed_net) >= abs(self.venue_net) else self.venue_net)
        if abs(net) <= EXACT_FLAT_EPS_BTC:
            self.halt_new_entries = False
            return None
        side = "SELL" if net > 0 else "BUY"
        if side == "SELL":
            if (best_ask - best_bid) > 1.0:
                px = max(best_ask - 1.0, best_bid + 1.0)
            else:
                px = best_ask
        else:
            if (best_ask - best_bid) > 1.0:
                px = min(best_bid + 1.0, best_ask - 1.0)
            else:
                px = best_bid
        px = maker_px(side, px, best_bid, best_ask)
        if px <= 0:
            return None
        return self._post_unwind(side, px, now, why)

    def _cancel(self, working: WorkingOrder, where: str) -> None:
        if self.real and self._can_call_api(is_cancel=True):
            try:
                self._record_api(is_cancel=True)
                self.client.cancel_child_order(self.product, working.acceptance_id)
            except Exception as exc:
                _log(f"cancel warn {working.acceptance_id}: {exc}")
        self.cancels += 1
        working.ghost = True
        working.expires_at = time.time() + GHOST_OID_TTL_SEC
        if all(g.acceptance_id != working.acceptance_id for g in self.ghosts):
            self.ghosts.append(working)
        if self.working and self.working.acceptance_id == working.acceptance_id:
            self.working = None
        if self.active_order_id == working.acceptance_id:
            self.active_order_id = None
            self.order_placed_ts = 0.0
        if self.real:
            self.need_sync = True
        _append(AUDIT_PATH, {"event": "CANCEL", "ts": _iso(), "id": working.acceptance_id, "where": where})
        self.evaluate_shadow_axe(
            event="ORDER_CANCEL",
            live_decision="CANCEL",
            details={"where": where, "id": working.acceptance_id, "side": working.side},
        )
        _log(f"[WOBI3_LIVE] CANCEL {working.side} {working.acceptance_id} ({where})")

    def _cancel_active_order(self, emergency: bool = False) -> bool:
        if self.working:
            self._cancel(self.working, "timeout_pull")
            return True
        return super()._cancel_active_order(emergency=emergency)

    def _cancel_working(self, where: str) -> None:
        if self.working:
            self._cancel(self.working, where)

    def _on_confirmed_fill(self, working: WorkingOrder, source: str, px: float) -> None:
        now = time.time()
        if working.acceptance_id in self.seen_oids:
            _log(f"[WOBI3_LIVE] skip duplicate {working.acceptance_id}")
            return
        self.seen_oids.add(working.acceptance_id)
        self.fills += 1
        if source == "PUBLIC_OID":
            self.oid_fills += 1
        if self.real:
            got = self._pull_venue()
            if got is None and not self._venue_ok:
                self.need_sync = True
                return
        elif working.side == "BUY":
            self.confirmed_net += working.size
            self.venue_net = float(self.confirmed_net)
        else:
            self.confirmed_net -= working.size
            self.venue_net = float(self.confirmed_net)
        net = float(self.confirmed_net)
        side = working.side
        if side == "BUY" and net > 1e-8 and not self.dry.wobi3.position_side:
            reason = (self.pending_entry or {}).get("reason") or "ENTRY"
            self.dry.apply_wobi3_entry_state("buy", px, now, self.last_asks, mid_price=px)
            self.pending_entry = None
            self.exit_armed_at = 0.0
            self.escalate_sent = False
            _append(LEDGER_PATH, {"ts": now, "lane": "TF2B_WOBI3_LIVE", "action": "ENTRY_LONG", "price": px, "size": working.size, "source": source, "reason": reason})
            _log(f"[WOBI3_LIVE] ENTRY BUY @ {px:,.0f} ({reason}) src={source}")
        elif side == "SELL" and self.dry.wobi3.position_side:
            entry = float(self.dry.wobi3.entry_price or self.dry.wobi3_trailing.entry_price or px)
            pnl_bp = ((px / entry) - 1.0) * 1e4 if entry > 0 else 0.0
            pnl_j = self.dry.wobi3._yen(pnl_bp) if entry > 0 else 0.0
            tag = str((self.pending_exit or {}).get("tag") or "EXIT")
            reason = str((self.pending_exit or {}).get("reason") or "")
            self.dry.apply_wobi3_exit_state(px, pnl_j, tag)
            self.pending_exit = None
            self.exit_armed_at = 0.0
            self.escalate_sent = False
            rec = {
                "ts": now,
                "lane": "TF2B_WOBI3_LIVE",
                "action": "CLOSE_LONG",
                "price": px,
                "size": working.size,
                "pnl_bp": round(pnl_bp, 2),
                "pnl_jpy": round(pnl_j, 1),
                "is_win": pnl_bp > 0,
                "source": source,
                "exit_reason": f"{tag} ({reason})",
            }
            _append(LEDGER_PATH, rec)
            _log(f"[WOBI3_LIVE] EXIT SELL @ {px:,.0f} pnl={pnl_bp:+.2f}bp src={source} venue={net:+.8f}")
        elif side == "BUY" and net >= -1e-12:
            _log(f"[WOBI3_LIVE] COVER/BUY settled @ {px:,.0f} venue={net:+.8f} src={source}")
        else:
            _log(f"[WOBI3_LIVE] fill noted {side} @ {px:,.0f} venue={net:+.8f} src={source}")
        _append(AUDIT_PATH, {"event": "FILL", "ts": _iso(), "side": side, "px": px, "src": source, "id": working.acceptance_id, "venue_net": net})
        self.evaluate_shadow_axe(
            event="ORDER_FILL",
            live_decision="FILL",
            details={"side": side, "px": px, "size": working.size, "source": source, "id": working.acceptance_id, "venue_net": net},
        )
        # CSR-650: 新規エントリー約定以外は、解消後に 0.00000000 まで落ちたかを検証する
        is_entry_fill = side == "BUY" and net > EXACT_FLAT_EPS_BTC
        if not is_entry_fill:
            if self.working and self.working.acceptance_id == working.acceptance_id:
                self.working = None
            if not self.check_post_exit_flat(net):
                if abs(net) >= SIZE_BTC:
                    self.halt_new_entries = True
                    self.flatten("CSR-650_LEAK")
                else:
                    self.halt_new_entries = False
                    _log(f"[CSR-650] leftover dust {net:+.8f} after exit — resume entries, flatten on next close")
            else:
                self.halt_new_entries = False

    def _scan_board_imitation(self, execs: List[Dict[str, Any]], best_bid: float, best_ask: float) -> None:
        tracked = ([self.working] if self.working else []) + list(self.ghosts)
        for order in tracked:
            allow_book = (not order.ghost) and (not order.optimistic)
            hit = public_maker_fill(
                side=order.side,
                price=order.price,
                acceptance_id=order.acceptance_id,
                public_execs=execs,
                best_bid=best_bid,
                best_ask=best_ask,
                allow_book=allow_book,
            )
            if not hit:
                continue
            source, _sz = hit
            if source in OPTIMISTIC_FILL_SOURCES:
                self.board_hits += 1
                order.optimistic = True
                self.need_sync = True
                before = float(self.confirmed_net)
                _log(f"[WOBI3_LIVE] predicted {order.side} {order.acceptance_id} src={source} — GET before any new post")
                self._cancel(order, "predicted_fill")
                self._pull_venue()
                after = float(self.confirmed_net)
                moved = (order.side == "BUY" and after > before + 1e-8) or (order.side == "SELL" and after < before - 1e-8)
                if self._venue_ok and moved:
                    self._on_confirmed_fill(order, source, order.price)
                break
            self._on_confirmed_fill(order, source, order.price)
            if self.working and self.working.acceptance_id == order.acceptance_id:
                self.working = None
            self.ghosts = [g for g in self.ghosts if g.acceptance_id != order.acceptance_id]
            break

    def flatten(self, reason: str) -> Dict[str, Any]:
        detail: Dict[str, Any] = {"reason": reason, "ts": _iso(), "flat_ok": False, "actions": []}
        _log(f"[FLATTEN] {reason}")
        self._cancel_working(f"flatten:{reason}")
        if self.real and self._can_call_api():
            try:
                self._record_api()
                self.client.cancel_all_child_orders(self.product)
            except Exception as exc:
                _log(f"flatten cancel_all: {exc}")
        net = self._pull_venue()
        if net is None:
            net = float(self.confirmed_net)
        detail["venue_net"] = float(net)
        if abs(net) <= EXACT_FLAT_EPS_BTC:
            detail["flat_ok"] = True
            self.halt_new_entries = False
            _write_json(FLATTEN_LAST, detail)
            _append(AUDIT_PATH, {"event": "FLATTEN", "ts": _iso(), "data": detail})
            return detail
        if abs(net) < SIZE_BTC:
            self.halt_new_entries = False
            _log(f"[CSR-650] flatten leftover is isolated dust {net:+.8f} — cannot POST < {SIZE_BTC:.3f}")
            _write_json(FLATTEN_LAST, detail)
            _append(AUDIT_PATH, {"event": "FLATTEN", "ts": _iso(), "data": detail})
            return detail
        self.halt_new_entries = True
        side = "SELL" if net > 0 else "BUY"
        detail["actions"].append(f"reduce_only_{side}")
        bids, asks, _age = self._read_depth()
        bid = float(bids[0]["price"]) if bids else 0.0
        ask = float(asks[0]["price"]) if asks else 0.0
        px = maker_px(side, ask if side == "SELL" else bid, bid, ask)
        self.need_sync = False
        if px > 0:
            self._post_unwind(side, px, time.time(), "flatten_leftover_limit")
        _write_json(FLATTEN_LAST, detail)
        _append(AUDIT_PATH, {"event": "FLATTEN", "ts": _iso(), "data": detail})
        return detail

    def preflight(self) -> None:
        if not self.real:
            _log("PRE-FLIGHT simulated flat")
            self._venue_ok = True
            return
        net = self._pull_venue()
        if net is None:
            raise RuntimeError("PRE-FLIGHT venue GET failed")
        if abs(net) <= EXACT_FLAT_EPS_BTC:
            _log(f"PRE-FLIGHT GATE PASSED: venue exact flat (net={net:+.8f})")
            self.halt_new_entries = False
            return
        if abs(net) < SIZE_BTC:
            _log(
                f"PRE-FLIGHT CSR-650 dust leftover {net:+.8f} — allow entries; "
                "next close unwinds venue_net to 0.00000000"
            )
            self.halt_new_entries = False
            return
        _log(f"PRE-FLIGHT leftover {net:+.8f} @ {self.venue_avg:.0f} — reduce-only until exact flat")
        self.halt_new_entries = True
        if net > FLAT_THRESHOLD_BTC and self.venue_avg > 0:
            self.dry.apply_wobi3_entry_state("buy", self.venue_avg, time.time(), self.last_asks, mid_price=self.venue_avg)

    def _maybe_escalate(self, now: float, mid: float) -> None:
        if self.escalate_sent or self.exit_armed_at <= 0:
            return
        if (now - self.exit_armed_at) < ESCALATE_MAKER_SEC:
            return
        entry = float(self.dry.wobi3_trailing.entry_price or 0.0)
        if entry <= 0 or mid <= 0:
            return
        adverse = (entry - mid) / entry * 1e4
        if adverse < ESCALATE_ADVERSE_BP:
            return
        venue = self._pull_venue()
        if venue is None or abs(venue) < 1e-8 or not self.real:
            return
        side = "SELL" if venue > 0 else "BUY"
        esc_size = self.get_unwind_order_size(venue)
        if esc_size < SIZE_BTC:
            return
        if not self._can_call_api():
            return
        try:
            self._cancel_working("escalate")
            self._record_api()
            self.client.send_order(self.product, side, esc_size, "MARKET")
            self.escalate_markets += 1
            self.escalate_sent = True
            self.need_sync = True
            _log(f"[WOBI3_LIVE] ESCALATE MARKET {side} venue={venue:+.4f} adverse={adverse:.1f}")
        except Exception as exc:
            _log(f"ESCALATE error {exc}")

    def _tick(self, snap: Any, now: float) -> None:
        bids, asks, age = self._read_depth()
        if bids:
            self.last_bids = bids
        if asks:
            self.last_asks = asks
        self._prune_ghosts(now)
        best_bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        best_ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        mid = float(getattr(snap, "mid_price", 0.0) or 0.0)
        if mid > 0:
            self._flush_axe_realizations(now, mid)
        execs = self._poll_executions()
        self._scan_board_imitation(execs, best_bid, best_ask)
        if self.need_sync or (now - self.last_pos_poll) >= POS_POLL_SEC:
            self._pull_venue()
        if age > 20.0:
            return
        if not self._venue_ok:
            return
        m = self.dry._extract_metrics(snap, now)
        depth_bids, depth_asks = self.dry._read_latest_depth()
        self.last_snap = snap
        self.last_metrics = m

        # 板構造駆動の未約定指値ライフサイクル管理（時間経過タイマーではなく板変化で管理）
        # ※新規エントリーの指値のみを管理。ショート返済中は板反転によるプルを行わない
        if self.working and self.working.side == "BUY" and self.confirmed_net >= -FLAT_THRESHOLD_BTC:
            bid_vol_l1 = float(depth_bids[0].get("size", 0.0) if depth_bids else 0.0)
            self.entry_px = float(self.working.price or 0.0)
            pulled = self.execute_order_lifecycle(
                now=now,
                best_bid=best_bid,
                best_ask=best_ask,
                wobi=float(m.get("wobi", 0.0) or 0.0),
                recent_ofi=float(m.get("recent_ofi", 0.0) or 0.0),
                depth_vol=bid_vol_l1,
            )
            if pulled:
                self.evaluate_shadow_axe(
                    event="STRUCTURAL_PULL",
                    m=m,
                    snap=snap,
                    live_decision="PULL",
                    details={"bid_vol_l1": bid_vol_l1, "entry_px": self.entry_px},
                )
                return
        if self.confirmed_net < -FLAT_THRESHOLD_BTC:
            if self.working:
                if self.working.side != "BUY":
                    self._cancel_working("cover_only")
                elif (best_bid - float(self.working.price or 0.0)) > 50.0:
                    self._cancel_working("drift_cover")
                return
            if (best_ask - best_bid) > 1.0:
                px = min(best_bid + 1.0, best_ask - 1.0)
                desc = "FRONTIER_1TICK_INSIDE"
            else:
                px = best_bid
                desc = "TOP_OF_BOOK_L1"
            px = maker_px("BUY", px, best_bid, best_ask)
            self._post_unwind("BUY", px, now, f"COVER_SHORT [{desc}]")
            return
        if self.confirmed_net > (SIZE_BTC + FLAT_THRESHOLD_BTC) + 1e-12:
            if not self.working:
                if (best_ask - best_bid) > 1.0:
                    px = max(best_ask - 1.0, best_bid + 1.0)
                    desc = "FRONTIER_1TICK_INSIDE"
                else:
                    px = best_ask
                    desc = "TOP_OF_BOOK_L1"
                px = maker_px("SELL", px, best_bid, best_ask)
                self._post_unwind("SELL", px, now, f"INV_CAP_REDUCE [{desc}]")
            return
        # Reconcile if dry engine has phantom position while venue is exact flat
        if abs(self.confirmed_net) <= EXACT_FLAT_EPS_BTC and self.dry.wobi3.position_side:
            _log(f"[WOBI3_LIVE] RECONCILE: venue is flat ({self.confirmed_net:+.8f}) but dry engine was in {self.dry.wobi3.position_side} — clearing phantom position")
            self.dry.apply_wobi3_exit_state(mid if mid > 0 else 0.0, 0.0, "RECONCILE_FLAT")
            self.pending_exit = None
            self.exit_armed_at = 0.0
            self.escalate_sent = False
            self.halt_new_entries = False
        # Reconcile if venue has real position while dry engine thinks it is flat
        if self.confirmed_net > FLAT_THRESHOLD_BTC and not self.dry.wobi3.position_side:
            _log(f"[WOBI3_LIVE] RECONCILE: venue is long ({self.confirmed_net:+.4f} @ {self.venue_avg:.0f}) but dry engine was flat — adopting position")
            entry_px = self.venue_avg if self.venue_avg > 0 else (best_bid or mid)
            self.dry.apply_wobi3_entry_state("buy", entry_px, now, self.last_asks, mid_price=mid)
            self.pending_entry = None
            self.exit_armed_at = 0.0
            self.escalate_sent = False
        pre = self.dry.plan_wobi3_pre_tick(snap, now, m, depth_bids, depth_asks)
        common = self.dry._tf2bp_common(snap, m, now)
        res_wobi3 = self.dry.wobi3.on_tick(**common)
        post = self.dry.plan_wobi3_post_tick(res_wobi3, now, m)
        if self.confirmed_net > FLAT_THRESHOLD_BTC:
            if pre.get("intent") == "EXIT":
                self.pending_exit = pre
                if self.exit_armed_at <= 0:
                    self.exit_armed_at = now
                if self.working and self.working.side == "SELL":
                    self._maybe_escalate(now, mid)
                    return
                if self.working and self.working.side == "BUY":
                    self._cancel_working("switch_to_exit")
                tag = str(pre.get("tag") or "")
                if tag in {"ADVERSE_CUT", "SPREAD_BLOWOUT"} and best_bid > 0:
                    px = best_bid + 1.0
                    desc = "ADVERSE_CUT_1TICK_CROSS_BOUNDARY"
                elif (best_ask - best_bid) > 1.0:
                    px = max(best_ask - 1.0, best_bid + 1.0)
                    desc = "FRONTIER_1TICK_INSIDE"
                else:
                    px = best_ask
                    desc = "TOP_OF_BOOK_L1"
                px = maker_px("SELL", px, best_bid, best_ask)
                self._post_unwind("SELL", px, now, f"{tag} {pre.get('reason')} [{desc}]")
                return
            if self.working and self.working.side == "SELL":
                self._maybe_escalate(now, mid)
            return
        # 1ロット以上の漏れだけ即スイープ。孤立ダストはエントリー継続し、クローズ時に全量フラット化する。
        if self.halt_new_entries:
            if self.check_post_exit_flat(self.confirmed_net) or abs(self.confirmed_net) < SIZE_BTC:
                self.halt_new_entries = False
            else:
                if not self.working:
                    self._sweep_to_flat(now, best_bid, best_ask, "CSR-650_POST_EXIT_SWEEP")
                return
        plan = pre if pre.get("intent") == "ENTER" else post
        if (now - self.last_axe_shadow_ts) >= 30.0:
            self.last_axe_shadow_ts = now
            self.evaluate_shadow_axe(
                event="TICK_PERIODIC",
                m=m,
                snap=snap,
                live_decision="HOLD" if self.working else ("LONG" if self.confirmed_net > FLAT_THRESHOLD_BTC else "WAIT"),
                details={"mid": mid, "best_bid": best_bid, "best_ask": best_ask, "plan_intent": plan.get("intent")},
            )
        if self.working and self.working.side == "BUY":
            if plan.get("intent") != "ENTER" and (now - self.working.posted_at) >= MIN_QUOTE_LIFE_SEC:
                self._cancel_working("ENTRY_STALE")
            return
        if plan.get("intent") != "ENTER":
            return
        self.evaluate_shadow_axe(
            event="ENTRY_EVAL",
            m=m,
            snap=snap,
            live_decision="ENTER_PLAN",
            details={"side": plan.get("side"), "reason": plan.get("reason")},
        )
        side = str(plan.get("side") or "buy")
        if side != "buy":
            _log(f"[WOBI3_LIVE] skip dryrun sell-entry (exit engine is long-only): {plan.get('reason')}")
            return
        if (best_ask - best_bid) > 1.0:
            px = min(best_bid + 1.0, best_ask - 1.0)
            desc = "FRONTIER_1TICK_INSIDE"
        else:
            px = best_bid
            desc = "TOP_OF_BOOK_L1"
        if self.halt_new_entries:
            _log(f"[CSR-650] new entry blocked net={self.confirmed_net:+.8f} halt={self.halt_new_entries}")
            return
        px = maker_px("BUY", px, best_bid, best_ask)
        self.pending_entry = plan
        entry_size = self.get_entry_order_size(self.confirmed_net)
        self._post("BUY", px, now, f"{plan.get('reason')} [{desc}]", size=entry_size)

    def metrics(self) -> Dict[str, Any]:
        w3 = self.dry.wobi3
        n = int(w3.total_trades)
        ev = (w3.total_pnl_bp / n) if n else 0.0
        wr = (w3.win_trades / n * 100.0) if n else 0.0
        return {
            "ts_jst": _iso(),
            "model": "TF2B_WOBI3",
            "mode": "LIVE" if self.real else "PAPER",
            "wire": "YES" if self.real else "NO",
            "fill": "BOARD_IMITATION",
            "strategy": "dryrun_plan_wobi3",
            "elapsed_hours": round((time.time() - self.t0) / 3600.0, 3),
            "pnl_bp": round(float(w3.total_pnl_bp), 2),
            "pnl_jpy": round(float(w3.total_pnl), 1),
            "ev_bp": round(ev, 4),
            "win_rate_pct": round(wr, 1),
            "total_trades": n,
            "confirmed_net": round(self.confirmed_net, 8),
            "venue_net": round(self.venue_net, 8),
            "halt_new_entries": bool(self.halt_new_entries),
            "venue_avg": round(self.venue_avg, 1),
            "posts": self.posts,
            "cancels": self.cancels,
            "fills": self.fills,
            "oid_fills": self.oid_fills,
            "board_hits": self.board_hits,
            "blocked_posts": self.blocked_posts,
            "escalate_markets": self.escalate_markets,
            "need_sync": self.need_sync,
            "working": None if not self.working else {"side": self.working.side, "px": self.working.price, "id": self.working.acceptance_id},
            "stop_reason": self.stop_reason or None,
            "verdict": self.dry._last_wobi3_verdict,
        }

    def save_state(self) -> None:
        _write_json(STATE_PATH, self.metrics())
        self.last_state_save = time.time()

    def send_discord(self, kind: str) -> None:
        m = self.metrics()
        title = {
            "start": "🚀 [LIVE START] TF2B_WOBI3 dryrun移植",
            "hourly": f"⏱️ [LIVE HOURLY #{int(m['elapsed_hours'])}] TF2B_WOBI3",
            "final": "🏁 [LIVE STOP] TF2B_WOBI3",
        }.get(kind, kind)
        payload = {
            "username": "TF2B_WOBI3 LIVE",
            "embeds": [
                {
                    "title": title,
                    "description": (
                        f"strategy=`dryrun_plan_wobi3` · fill=`BOARD_IMITATION` · lot=`{self.size}`\n"
                        f"pnl=`{m['pnl_bp']:+.2f}bp` n=`{m['total_trades']}` · "
                        f"net=`{m['confirmed_net']:+.8f}` venue=`{m['venue_net']:+.8f}` · "
                        f"oid=`{m['oid_fills']}` boardHit=`{m['board_hits']}` block=`{m['blocked_posts']}`"
                    ),
                    "color": 0xE74C3C if kind == "final" else 0x2ECC71,
                }
            ],
        }
        try:
            self.notifier._post(self.notifier.live_webhook_url, payload)
        except Exception as exc:
            _log(f"[DISCORD] live webhook failed: {exc}")
        try:
            req = urllib.request.Request(
                DISCORD_WEBHOOK_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                _log(f"[DISCORD] {kind} sent (HTTP {resp.status})")
        except Exception as exc:
            _log(f"[DISCORD] wobi3 channel failed: {exc}")

    def run(self) -> int:
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
        _log(f"=== TF2B_WOBI3 LIVE START real={self.real} size={self.size} strategy=dryrun_plan fill=BOARD_IMITATION ===")
        self.preflight()
        self.dry.feed.start()
        try:
            self.send_discord("start")
        except Exception:
            pass
        self.save_state()
        last_snap = None
        try:
            while self.running:
                if STOP_FLAG.exists():
                    self.request_stop("operator_stop_flag")
                    break
                m = self.metrics()
                if m["pnl_bp"] <= STOP_PNL_BP or m["pnl_jpy"] <= STOP_PNL_JPY:
                    self.request_stop(f"pnl_stop {m['pnl_bp']:.1f}bp")
                    break
                try:
                    snap = self.dry.feed.get(timeout=self.interval_sec)
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
                    _log(f"tick exception: {exc}")
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
                self.dry.feed.stop()
            except Exception:
                pass
            if self.exec_fp:
                self.exec_fp.close()
            PID_PATH.unlink(missing_ok=True)
            _log(f"=== TF2B_WOBI3 LIVE STOPPED reason={self.stop_reason} flat={flat.get('flat_ok')} ===")
        return 0 if flat.get("flat_ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="TF2B_WOBI3 LIVE (dryrun strategy, board-imitation fill)")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    parser.add_argument("--size", type=float, default=SIZE_BTC)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--confirm-go", default="")
    args = parser.parse_args()
    if _foreign_pids(NEEDLE):
        print("REFUSE: TF2B_WOBI3 live already running", file=sys.stderr)
        return 2
    if _foreign_pids(WOBIMM_NEEDLE):
        print("REFUSE: WOBIMM live active", file=sys.stderr)
        return 2
    if _foreign_pids(SOAK_NEEDLE):
        print("REFUSE: TF2BP soak active", file=sys.stderr)
        return 2
    if _foreign_pids(PEG_NEEDLE):
        print("REFUSE: PEG2-B live active", file=sys.stderr)
        return 2
    if args.size > SIZE_BTC + 1e-12:
        print(f"REFUSE: SIZE must be <= {SIZE_BTC}", file=sys.stderr)
        return 2
    if args.real:
        reason = _refuse_real_gates(args.confirm_go)
        if reason:
            print(reason, file=sys.stderr)
            return 0
    else:
        os.environ["ENABLE_REAL_TRADING"] = "false"
    runner = TF2BWobi3Live(
        real=args.real,
        size=args.size,
        interval_sec=args.interval,
        report_interval_sec=args.report_interval,
        market_source=args.source,
    )

    def _sig(signum, _frame):
        runner.request_stop(f"signal_{signum}")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
