#!/usr/bin/env python3
"""
Antigravity LIVE Engine v1.1 (Production Live-Trading Runner)
=============================================================
- 戦略: EmaTrendTickStrategy (単一集中・大波トレンドフォロー)
- 防護層:
    1. RateLimiter (発注最短3秒間隔 & 1分間上限8回によるAPI BAN完全回避)
    2. OrderStateManager (In-Flight排他制御 & 10秒タイムアウト救済)
    3. PeakDrawdownCircuitBreaker (特権バイパスによる緊急全決済)
- Discord 通知機能:
    1. 1時間毎のLIVE成績レポート (直近1時間 / 当日累計 / 証拠金残高 / ドローダウン)
    2. 新規エントリー速報 (LONG/SHORT @ 価格)
    3. 手仕舞い速報 (大幅収益・大幅損失・確定損益・価格推移)
    4. サーキットブレーカー発動通知 & 手動停止通知
"""
import os
import sys
import time
import signal
import threading
from collections import deque
from datetime import datetime
from typing import Optional, Dict, Any, List

from antigravity.config.settings import Settings
from antigravity.ws_engine.stream import WebSocketTickStream
from antigravity.risk_guard.circuit_breaker import PeakDrawdownCircuitBreaker
from antigravity.risk_guard.daily_pnl_guard import DailyPnLGuard
from antigravity.risk_guard.aging_guard import AgingGuard
from antigravity.risk_guard.order_rate_guard import OrderRateGuard
from antigravity.risk_guard.portfolio_pnl_guard import PortfolioDailyPnLGuard
from antigravity.risk_guard.lot_scale_guard import LotScaleGuard
from antigravity.risk_guard.notifier import DiscordNotifier
from antigravity.strategies.ema_trend import EmaTrendTickStrategy
from antigravity.runtime.client import BitflyerClient
from antigravity.execution.position_manager import PositionManager
from antigravity.execution.portfolio_orchestrator import PortfolioOrchestrator
from antigravity.backtest.regime_detector import RegimeDetector

# 後方互換エイリアス
RateLimiter = OrderRateGuard


# =====================================================================
# 1. In-Flight注文マネージャー（二重発注・ロック膠着防止）
# =====================================================================
class OrderStateManager:
    def __init__(self, timeout_sec: float = 10.0):
        self.lock = threading.Lock()
        self.inflight = False
        self.last_order_id: Optional[str] = None
        self.inflight_start_ts: float = 0.0
        self.timeout_sec = timeout_sec

    def can_place_order(self) -> bool:
        with self.lock:
            # タイムアウト救済：10秒以上応答がない場合はロックを強制解除
            if self.inflight and (time.time() - self.inflight_start_ts > self.timeout_sec):
                print(f"[OrderState] ⚠️ In-Flight タイムアウト検知 ({self.last_order_id})。ロックを自動強制解除します。", flush=True)
                self.inflight = False
                self.last_order_id = None
            return not self.inflight

    def set_inflight(self, order_id: str):
        with self.lock:
            self.inflight = True
            self.last_order_id = order_id
            self.inflight_start_ts = time.time()

    def clear_inflight(self, order_id: Optional[str] = None):
        with self.lock:
            if order_id is None or order_id == self.last_order_id:
                self.inflight = False
                self.last_order_id = None


# =====================================================================
# 3. インメモリ・ポジション＆損益会計エンジン (完全自律ローカル管理)
# =====================================================================
class PositionTracker:
    """
    取引所APIへの常時問い合わせを全廃し、約定イベントに基づいて
    建玉・確定損益・評価損益を自律計算するインメモリ会計エンジン。
    """
    def __init__(self, initial_position: float = 0.0, avg_price: float = 0.0, initial_realized_pnl: float = 0.0):
        self.position: float = initial_position
        self.avg_price: float = avg_price
        self.realized_pnl: float = initial_realized_pnl

    def on_execution(self, side: str, size: float, price: float) -> float:
        """実約定イベントに基づいて建玉と確定損益を厳密計算 (戻り値: 今回の確定損益)"""
        trade_pnl = 0.0
        order_qty = size if side == "BUY" else -size

        # 同方向の新規・増玉
        if (self.position >= 0 and order_qty > 0) or (self.position <= 0 and order_qty < 0):
            new_pos = self.position + order_qty
            if abs(new_pos) > 1e-9:
                self.avg_price = (abs(self.position) * self.avg_price + size * price) / abs(new_pos)
            self.position = round(new_pos, 6)
        # 反対方向（決済またはドテン）
        else:
            close_qty = min(abs(self.position), size)
            if self.position > 0:
                trade_pnl = (price - self.avg_price) * close_qty
            else:
                trade_pnl = (self.avg_price - price) * close_qty

            self.realized_pnl += trade_pnl
            remaining_qty = size - close_qty
            if remaining_qty > 1e-9:
                self.position = round(remaining_qty if side == "BUY" else -remaining_qty, 6)
                self.avg_price = price
            else:
                self.position = round(self.position + order_qty, 6)
                if abs(self.position) < 1e-9:
                    self.position = 0.0
                    self.avg_price = 0.0

        return trade_pnl

    def get_unrealized_pnl(self, current_price: float) -> float:
        """最新Tick価格に基づく含み評価損益 (完全ローカル計算)"""
        if abs(self.position) < 1e-9 or self.avg_price <= 0.0 or current_price <= 0.0:
            return 0.0
        if self.position > 0:
            return (current_price - self.avg_price) * self.position
        else:
            return (self.avg_price - current_price) * abs(self.position)


# =====================================================================
# 4. LIVE専用オーケストレーター
# =====================================================================
class LiveRunner:
    def __init__(
        self,
        symbol: str = "FX_BTC_JPY",
        order_size: float = 0.001,
        max_drawdown_limit: float = 1500.0,
        enable_real_order: bool = False,
        initial_collateral: float = 6982.0,
        max_hold_seconds: float = 300.0,
        daily_limit_jpy: Optional[float] = None,
        portfolio_limit_jpy: Optional[float] = None,
        state_dir: Optional[str] = None,
    ):
        self.symbol = symbol
        self.order_size = order_size
        self.enable_real_order = enable_real_order
        self.initial_collateral = initial_collateral
        self.max_hold_seconds = max_hold_seconds
        self.daily_limit_jpy = daily_limit_jpy if daily_limit_jpy is not None else max_drawdown_limit
        self.portfolio_limit_jpy = portfolio_limit_jpy if portfolio_limit_jpy is not None else (self.daily_limit_jpy * 2.0)
        self.state_dir = state_dir

        # コンポーネント群
        self.notifier = DiscordNotifier()
        self.client = BitflyerClient(enable_real_trading=enable_real_order)
        self.order_state = OrderStateManager(timeout_sec=10.0)

        # GAPCORE流 0: APIレートリミット対策ガード (1分間上限 & 戦略クールダウン)
        self.order_rate_guard = OrderRateGuard(
            max_orders_per_min=60,               # ポートフォリオ全体上限 (bitFlyer 429 BAN完全防止)
            min_order_interval_sec=3.0,          # 戦略個別クールダウン (3秒)
            global_min_interval_sec=0.5,         # 全体バースト最小間隔 (0.5秒)
            on_rate_limit_callback=self._on_rate_limit_breach,
        )
        self.rate_limiter = self.order_rate_guard  # 後方互換エイリアス

        # GAPCORE流 1: ポートフォリオ＆戦略階層型 日次損失管理ガード (JST 00:00永続化)
        self.strategy_limits = {
            "EmaTrend": self.daily_limit_jpy,
            "MeanReversion": 1000.0,
            "OrderBookImbalance": 500.0,
        }
        self.portfolio_guard = PortfolioDailyPnLGuard(
            portfolio_limit_jpy=self.portfolio_limit_jpy,
            strategy_limits=self.strategy_limits,
            state_dir=self.state_dir,
            on_portfolio_breach_callback=self._on_portfolio_breach,
        )
        self.daily_pnl_guard = self.portfolio_guard.get_strategy_guard("EmaTrend")

        # GAPCORE Multi-Strategy Portfolio Orchestrator (3戦略同居 & 内部ネッティング)
        self.orchestrator = PortfolioOrchestrator(
            symbol=self.symbol,
            order_size=self.order_size,
            portfolio_daily_limit_jpy=self.portfolio_limit_jpy,
            strategy_daily_limits=self.strategy_limits,
            aging_timeout_sec=self.max_hold_seconds,
            enable_internal_netting=True,
            enable_regime_switch=True,
            portfolio_guard=self.portfolio_guard,
            order_rate_guard=self.order_rate_guard,
        )

        # GAPCORE流 2: 在庫滞留タイムアウトガード (Aging Guard)
        self.aging_guard = AgingGuard(
            max_hold_seconds=self.max_hold_seconds,
            warning_ratio=0.80,
            on_timeout_callback=self._on_aging_timeout,
            on_warning_callback=self._on_aging_warning,
        )

        # GAPCORE流 3: 動的ロットスケーリング・昇格降格ガード (LotScaleGuard)
        self.lot_scale_guard = LotScaleGuard(
            strategy_id="EmaTrend",
            tiers=[0.001, 0.002, 0.003],
            min_trades_count=20,
            min_profit_factor=1.30,
            max_allowed_drawdown=750.0,
            min_win_rate_pct=45.0,
            min_collateral_jpy=6000.0,
            state_dir=self.state_dir,
            on_tier_changed_callback=self._on_lot_scale_changed,
        )
        # 永続化されたロットサイズを適用
        self.order_size = self.lot_scale_guard.current_lot_size
        self.orchestrator.order_size = self.order_size
        if "EmaTrend" in self.orchestrator.strategies:
            self.orchestrator.strategies["EmaTrend"].order_size = self.order_size
        if "EmaTrend" in self.orchestrator.pos_managers:
            self.orchestrator.pos_managers["EmaTrend"].lot_size = self.order_size

        # 戦略＆ポジション管理（後方互換エイリアス兼任）
        self.strategy = self.orchestrator.strategies["EmaTrend"]

        # 状態変数（GAPCORE流 Target / Actual ポジション管理エンジン）
        self.state_lock = threading.RLock()
        self.position_mgr = self.orchestrator.pos_managers["EmaTrend"]
        self.tracker = self.position_mgr  # 後方互換エイリアス
        self.current_price = 0.0
        self.current_mid = 0.0
        self.latest_flow_stats: Dict[str, Any] = {}
        self.current_collateral = initial_collateral + self.daily_pnl_guard.realized_jpy
        self.trade_count = 0
        self.trades_history: List[Dict[str, Any]] = []
        self.regime_counts: Dict[str, int] = {"TREND": 0, "RANGE": 0, "NORMAL": 0, "HIGH_VOL": 0}

        # 定期レポート送信管理
        self.last_hourly_report_time = time.time()

        # サーキットブレーカー
        self.circuit_breaker = PeakDrawdownCircuitBreaker(
            max_drawdown_limit_jpy=max_drawdown_limit,
            cooldown_seconds=600.0,
            on_trip_callback=self._on_emergency_halt,
            on_resume_callback=self._on_resume,
        )

        # WebSocketストリーム
        self.stream = WebSocketTickStream(
            product_code=self.symbol,
            window_seconds=15.0,
            on_ticks_callback=self._on_ticks,
        )
        self.is_running = False

    @property
    def current_position_btc(self) -> float:
        return self.position_mgr.actual_qty

    @current_position_btc.setter
    def current_position_btc(self, val: float):
        self.position_mgr.actual_qty = val

    @property
    def entry_price(self) -> float:
        return self.position_mgr.avg_price

    @entry_price.setter
    def entry_price(self, val: float):
        self.position_mgr.avg_price = val

    @property
    def realized_pnl_jpy(self) -> float:
        return self.position_mgr.realized_pnl

    @realized_pnl_jpy.setter
    def realized_pnl_jpy(self, val: float):
        self.position_mgr.realized_pnl = val

    @property
    def open_position_pnl(self) -> float:
        """GAPCORE流 芯①: LTP依存を廃止し、仲値(Mid)で未実現評価損益を値洗い"""
        eval_price = self.current_mid if self.current_mid > 0 else self.current_price
        return self.position_mgr.get_unrealized_pnl(eval_price)


    def reconcile_with_exchange(self) -> Dict[str, Any]:
        """
        取引所公式残高・建玉とのレコンシリエーション (定期突合・答え合わせ)。
        常時ポーリングではなく、毎時レポート時等の低頻度でのみ実行。
        """
        if not self.enable_real_order:
            return {}

        try:
            col = self.client.get_collateral()
            current_col = float(col.get("collateral", self.current_collateral))
            actual_realized = current_col - self.initial_collateral

            positions = self.client.get_positions(self.symbol)
            net_pos = 0.0
            avg_entry_price = 0.0
            if positions:
                buy_size = sum(float(p.get("size", 0.0)) for p in positions if p.get("side") == "BUY")
                sell_size = sum(float(p.get("size", 0.0)) for p in positions if p.get("side") == "SELL")
                net_pos = round(buy_size - sell_size, 4)
                total_size = sum(float(p.get("size", 0.0)) for p in positions)
                if total_size > 0:
                    avg_entry_price = sum(float(p.get("price", 0.0)) * float(p.get("size", 0.0)) for p in positions) / total_size

            with self.state_lock:
                diff = actual_realized - self.position_mgr.realized_pnl
                self.current_collateral = current_col
                if abs(diff) > 1.0:
                    print(f"[Reconciliation] ℹ️ 取引所残高との微小差分({diff:+,.1f}円)を補正 (スワップ/手数料等)", flush=True)
                    self.position_mgr.realized_pnl = actual_realized

                pos_diff = self.position_mgr.reconcile_actual(net_pos, avg_entry_price)
                if abs(pos_diff) > 1e-9:
                    self.aging_guard.on_position_update(net_pos, price=avg_entry_price)

            return col
        except Exception as ex:
            print(f"[Reconciliation] ⚠️ 突合通信スキップ: {ex}", flush=True)
            return {}

    def sync_account_state(self) -> Dict[str, Any]:
        """後方互換用 (明示的な突合呼び出し)"""
        return self.reconcile_with_exchange()

    def check_initial_account_status(self):
        """起動前のアカウント証拠金・既存建玉の初期同期 (起動時1回のみ実行)"""
        print("\n--- [事前アカウント診断 & 公式API初期同期] ---")
        if not self.enable_real_order:
            print("  • DRYRUN モード: 初期ポジション FLAT / 損益 0 円")
            print("------------------------------------------\n")
            return

        try:
            col = self.client.get_collateral()
            current_col = float(col.get("collateral", self.initial_collateral))
            open_pnl = float(col.get("open_position_pnl", 0.0))
            realized = current_col - self.initial_collateral

            positions = self.client.get_positions(self.symbol)
            net_pos = 0.0
            avg_entry_price = 0.0
            if positions:
                buy_size = sum(float(p.get("size", 0.0)) for p in positions if p.get("side") == "BUY")
                sell_size = sum(float(p.get("size", 0.0)) for p in positions if p.get("side") == "SELL")
                net_pos = round(buy_size - sell_size, 4)
                total_size = sum(float(p.get("size", 0.0)) for p in positions)
                if total_size > 0:
                    avg_entry_price = sum(float(p.get("price", 0.0)) * float(p.get("size", 0.0)) for p in positions) / total_size

            with self.state_lock:
                self.current_collateral = current_col
                self.position_mgr = PositionManager(
                    strategy_id="EmaTrend",
                    product_code=self.symbol,
                    lot_size=self.order_size,
                    initial_actual_qty=net_pos,
                    initial_avg_price=avg_entry_price,
                    initial_realized_pnl=realized,
                )
                self.tracker = self.position_mgr
                if net_pos != 0:
                    self.aging_guard.on_position_update(net_pos, price=avg_entry_price)

            print(f"  • 基準証拠金 (開始時): {self.initial_collateral:,.1f} 円")
            print(f"  • 現在証拠金 (取引所): {current_col:,.1f} 円 (通算確定損益: {realized:+,.1f} 円)")
            print(f"  • 未実現評価損益: {open_pnl:+,.1f} 円")
            print(f"  • 現在の同期建玉: {net_pos:+.3f} BTC (平均価格: {avg_entry_price:,.0f} 円)")
            if net_pos != 0:
                print("  ⚠️ 警告: 既に保有建玉が存在します。ローカルエンジンで自動追随・エグジット監視を行います。")
            else:
                print("  ✅ 建玉なし (FLAT)。新規シグナル待機状態です。")
        except Exception as e:
            print(f"  ⚠️ アカウント情報取得警告: {e}")
        print("------------------------------------------\n")

    def _on_emergency_halt(self, reason: str, dd: float, pnl: float):
        """サーキットブレーカー発動：RateLimiterをバイパスして即座に全成行決済"""
        print(f"\n[CRITICAL] 🚨 サーキットブレーカー発動 (DD: -{dd:,.0f}円): {reason}", flush=True)
        with self.state_lock:
            pos = self.position_mgr.actual_qty
            if pos != 0.0:
                exit_side = "SELL" if pos > 0 else "BUY"
                size = abs(pos)
                self.position_mgr.set_target(0.0, reason)
                print(f"[CRITICAL] 🛡️ 特権バイパス成行全決済実行: {exit_side} {size} BTC", flush=True)
                try:
                    self.client.send_order(
                        product_code=self.symbol,
                        side=exit_side,
                        size=size,
                        order_type="MARKET",
                    )
                    trade_pnl = self.position_mgr.on_fill(exit_side, size, self.current_price)
                    self.daily_pnl_guard.record_trade_pnl(trade_pnl)
                    self.aging_guard.on_position_update(0.0)
                except Exception as e:
                    print(f"[CRITICAL] 緊急決済発注失敗: {e}", flush=True)

        self.notifier.send_drawdown_alert(
            current_dd=dd,
            max_dd=self.circuit_breaker.max_drawdown_limit_jpy,
            peak_pnl=self.circuit_breaker.peak_pnl,
            current_pnl=pnl,
            is_halted=True,
            reason=f"【LIVE運用】{reason}",
            symbol=self.symbol,
        )

    def _on_daily_pnl_breach(self, reason: str, daily_jpy: float, limit_jpy: float):
        """GAPCORE流 個別戦略日次損失限度到達：特権バイパスで即座に全決済し、翌日00:00 JSTまで戦略停止"""
        print(f"\n[CRITICAL] 🚨 GAPCORE Strategy DailyPnLGuard LIMIT BREACH: {reason}", flush=True)
        with self.state_lock:
            pos = self.position_mgr.actual_qty
            if pos != 0.0:
                exit_side = "SELL" if pos > 0 else "BUY"
                size = abs(pos)
                self.position_mgr.set_target(0.0, reason)
                print(f"[CRITICAL] 🛡️ 戦略日次損切リミット到達に伴う緊急全決済: {exit_side} {size} BTC", flush=True)
                try:
                    self.client.send_order(
                        product_code=self.symbol,
                        side=exit_side,
                        size=size,
                        order_type="MARKET",
                    )
                    trade_pnl = self.position_mgr.on_fill(exit_side, size, self.current_price)
                    self.portfolio_guard.record_trade_pnl("EmaTrend", trade_pnl)
                    self.aging_guard.on_position_update(0.0)
                except Exception as e:
                    print(f"[CRITICAL] 日次損切決済発注失敗: {e}", flush=True)

        # 内部サーキットブレーカーも強制トリップ
        self.circuit_breaker.trip(reason=reason, current_dd=abs(daily_jpy), current_total_pnl=daily_jpy)

        try:
            self.notifier.send_embed(
                title="🚨 【CRITICAL】GAPCORE 戦略個別日次損失リミット到達 (取引停止)",
                description=f"**理由**: {reason}\n**対象暦日**: `{self.daily_pnl_guard.jst_day} (JST)`",
                fields=[
                    {"name": "本日確定損益", "value": f"`{daily_jpy:+,.1f} 円`", "inline": True},
                    {"name": "戦略許容限度額", "value": f"`-{limit_jpy:,.0f} 円`", "inline": True},
                    {"name": "復帰タイミング", "value": "翌日 00:00 JST 自動リセット または 手動復帰", "inline": False},
                ],
                color=0xE74C3C,
                footer_text="Antigravity GAPCORE Strategy Sentinel 🚨",
                target="alert",
            )
        except Exception as ex:
            print(f"[Notifier] 戦略リミット到達通知失敗: {ex}", flush=True)

    def _on_portfolio_breach(self, reason: str, portfolio_jpy: float, limit_jpy: float):
        """GAPCORE流 ポートフォリオ全体損失限度到達：特権バイパスで全建玉を強制決済し全戦略凍結"""
        print(f"\n[CRITICAL] 🚨 GAPCORE PortfolioDailyPnLGuard BREACH: {reason}", flush=True)
        with self.state_lock:
            pos = self.position_mgr.actual_qty
            if pos != 0.0:
                exit_side = "SELL" if pos > 0 else "BUY"
                size = abs(pos)
                self.position_mgr.set_target(0.0, reason)
                print(f"[CRITICAL] 🛡️ ポートフォリオ上限到達に伴う全成行決済: {exit_side} {size} BTC", flush=True)
                try:
                    self.client.send_order(
                        product_code=self.symbol,
                        side=exit_side,
                        size=size,
                        order_type="MARKET",
                    )
                    trade_pnl = self.position_mgr.on_fill(exit_side, size, self.current_price)
                    self.portfolio_guard.record_trade_pnl("EmaTrend", trade_pnl)
                    self.aging_guard.on_position_update(0.0)
                except Exception as e:
                    print(f"[CRITICAL] ポートフォリオ緊急決済失敗: {e}", flush=True)

        self.circuit_breaker.trip(reason=reason, current_dd=abs(portfolio_jpy), current_total_pnl=portfolio_jpy)

        try:
            self.notifier.send_embed(
                title="🚨 【CRITICAL】GAPCORE ポートフォリオ日次許容損失リミット到達 (全戦略完全停止)",
                description=f"**理由**: {reason}\n**対象暦日**: `{self.portfolio_guard.jst_day} (JST)`",
                fields=[
                    {"name": "本日ポートフォリオ確定損益", "value": f"`{portfolio_jpy:+,.1f} 円`", "inline": True},
                    {"name": "ポートフォリオ全体限度額", "value": f"`-{limit_jpy:,.0f} 円`", "inline": True},
                    {"name": "防護動作", "value": "全戦略の新規発注を物理遮断 (Fail-Closed)", "inline": False},
                ],
                color=0x962D3E,
                footer_text="Antigravity GAPCORE Portfolio Sentinel 🚨",
                target="alert",
            )
        except Exception as ex:
            print(f"[Notifier] ポートフォリオ通知失敗: {ex}", flush=True)

    def _on_aging_timeout(self, reason: str, age_sec: float, pos: float):
        """GAPCORE流 在庫滞留タイムアウト：保有時間上限到達による強制クローズ発動"""
        print(f"\n[AGING] 🚨 {reason}", flush=True)
        try:
            self.notifier.send_embed(
                title="⏳ 【Aging Guard】在庫滞留タイムアウト強制手仕舞い発動",
                description=f"**理由**: {reason}",
                fields=[
                    {"name": "滞留保有時間", "value": f"`{age_sec:.1f} 秒` (上限: `{self.max_hold_seconds:.0f} 秒`)", "inline": True},
                    {"name": "手仕舞い建玉", "value": f"`{pos:+.4f} BTC`", "inline": True},
                    {"name": "防衛目的", "value": "レンジ停滞・塩漬けによる予期せぬ急変動被弾の根絶", "inline": False},
                ],
                color=0xE67E22,
                footer_text="Antigravity GAPCORE AgingGuard ⏳",
                target="alert",
            )
        except Exception as ex:
            print(f"[Notifier] Aging通知送信失敗: {ex}", flush=True)

    def _on_aging_warning(self, reason: str, age_sec: float, pos: float):
        print(f"\n[AGING WARNING] ⚠️ {reason}", flush=True)

    def _on_resume(self, reason: str):
        print(f"\n[INFO] 🟢 クールダウン完了。取引監視を再開: {reason}", flush=True)

    def _on_rate_limit_breach(self, reason: str, count: int, limit: int):
        print(f"\n[RATE GUARD] ⚠️ APIレートリミット防衛発動: {reason}", flush=True)
        try:
            self.notifier.send_embed(
                title="⚠️ 【API Rate Guard】発注レートリミット一時防衛",
                description=f"**理由**: {reason}",
                fields=[
                    {"name": "直近1分間発注数", "value": f"`{count}/{limit} 回`", "inline": True},
                    {"name": "防護目的", "value": "bitFlyer 429 Too Many Requests BANの物理的完全回避", "inline": False},
                ],
                color=0xF39C12,
                footer_text="Antigravity OrderRateGuard 🛡️",
                target="alert",
            )
        except Exception:
            pass

    def _on_lot_scale_changed(self, strategy_id: str, old_lot: float, new_lot: float, reason: str, event_type: str):
        print(f"\n[LotScaleGuard] 🚀 ロット変更適用: {strategy_id} {old_lot:.3f} -> {new_lot:.3f} BTC ({event_type}): {reason}", flush=True)
        # 即時Hot-Reload
        self.order_size = new_lot
        self.orchestrator.order_size = new_lot
        if strategy_id in self.orchestrator.strategies:
            self.orchestrator.strategies[strategy_id].order_size = new_lot
        if strategy_id in self.orchestrator.pos_managers:
            self.orchestrator.pos_managers[strategy_id].lot_size = new_lot

        # Discord即時通知
        metrics = self.position_mgr.get_metrics(hours=24.0)
        try:
            self.notifier.send_lot_scale_alert(
                strategy_id=strategy_id,
                old_lot=old_lot,
                new_lot=new_lot,
                event_type=event_type,
                reason=reason,
                metrics=metrics,
                symbol=self.symbol,
                target="report",
            )
        except Exception as ex:
            print(f"[Notifier] ロット変更通知送信失敗: {ex}", flush=True)

    def _on_ticks(self, ticks: List[Dict[str, Any]], stats: Dict[str, Any]):
        if not ticks or not self.is_running:
            return

        with self.state_lock:
            latest_price = ticks[-1].get("price", self.current_price)
            latest_mid = stats.get("mid_price", ticks[-1].get("mid", latest_price))
            latest_ts = ticks[-1].get("timestamp", time.time())
            if latest_price > 0:
                self.current_price = latest_price
            if latest_mid > 0:
                self.current_mid = latest_mid
            self.latest_flow_stats = stats

            # GAPCORE流 芯①: LTPノイズを全廃し、仲値(Mid)で未実現評価損益を値洗い
            eval_price = self.current_mid if self.current_mid > 0 else self.current_price
            unrealized = self.position_mgr.get_unrealized_pnl(eval_price)

            self.circuit_breaker.update(self.position_mgr.realized_pnl + unrealized)

            # 防護層1: ポートフォリオ＆戦略 日次損失ガード (JST 00:00基準永続化)
            can_enter, block_reason = self.portfolio_guard.can_enter("EmaTrend")
            if not can_enter:
                # もし未決済ポジションが残っていれば即時手仕舞い
                pos = self.position_mgr.actual_qty
                if pos != 0.0:
                    exit_side = "SELL" if pos > 0 else "BUY"
                    self.position_mgr.set_target(0.0, block_reason)
                    self._dispatch_order(exit_side, abs(pos), latest_price, block_reason, is_exit=True)
                return

            # 防護層2: サーキットブレーカー
            if self.circuit_breaker.is_halted:
                return

            # 防護層3: 在庫滞留タイムアウトガード (Aging Guard - 物理建玉滞留)
            phys_pos = self.orchestrator.exchange_actual_qty
            if abs(phys_pos) > 1e-9:
                aging_status = self.aging_guard.check(
                    current_position=phys_pos,
                    current_price=latest_price,
                    now=latest_ts,
                )
                if aging_status["action"] == "EXIT":
                    exit_side = "SELL" if phys_pos > 0 else "BUY"
                    self._dispatch_order(exit_side, abs(phys_pos), latest_price, aging_status["reason"], is_exit=True)
                    return

            # GAPCORE Multi-Strategy Portfolio Orchestrator (3戦略同居 & 内部ネッティング執行)
            latest_tick = ticks[-1]
            curr_reg = self.orchestrator.current_regime or "NORMAL"
            self.regime_counts[curr_reg] = self.regime_counts.get(curr_reg, 0) + 1

            order_intent = self.orchestrator.on_tick(
                tick=latest_tick,
                flow_stats=stats,
                now_ts=latest_ts,
            )

            # 物理発注が必要な場合 (RequiredQty >= lot_size)
            if order_intent and order_intent.get("action") == "ORDER":
                self._dispatch_order(
                    side=order_intent["side"],
                    size=order_intent["size"],
                    price=latest_price,
                    reason=order_intent["reason"],
                    is_exit=order_intent.get("is_exit", False),
                )


    def _send_entry_notification(self, side: str, size: float, price: float, reason: str):
        """新規ポジション保有のDiscord即時速報"""
        badge = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        title = f"🚀 【LIVE実資金 ENTRY】{badge} {size} BTC @ {price:,.0f} 円"
        color = 0x3498DB  # 青
        fields = [
            {"name": "注文区分", "value": f"**{side} (新規エントリー)**", "inline": True},
            {"name": "数量", "value": f"`{size} BTC`", "inline": True},
            {"name": "実約定価格", "value": f"`{price:,.0f} 円`", "inline": True},
            {"name": "エントリー根拠", "value": f"{reason}", "inline": False},
        ]
        try:
            self.notifier.send_embed(
                title=title,
                description=f"**発注時刻**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S JST')}` | 戦略: `EmaTrend`",
                fields=fields,
                color=color,
                footer_text="Antigravity LIVE Order Sentinel 🔴",
                target="trade",
            )
        except Exception as ex:
            print(f"[Notifier] エントリー速報送信失敗: {ex}", flush=True)

    def _send_trade_notification(self, side: str, size: float, entry_price: float, exit_price: float, pnl: float, reason: str):
        """決済・大幅収益/大幅損失のDiscord即時速報"""
        is_profit = pnl >= 0
        badge = "🎉 利確 (+)" if is_profit else "⚠️ 損切 (-)"
        title = f"{'🎉' if is_profit else '⚠️'} 【LIVE実資金 手仕舞い】{badge} {pnl:+,.1f} 円"
        color = 0x2ECC71 if is_profit else 0xE74C3C
        col_text = f"`{self.current_collateral:,.1f} 円`" if self.enable_real_order else "N/A (SIM)"
        fields = [
            {"name": "決済区分", "value": f"**{side} (手仕舞い)**", "inline": True},
            {"name": "確定損益 (今回)", "value": f"**`{pnl:+,.1f} 円`**", "inline": True},
            {"name": "本日確定損益 (通算)", "value": f"**`{self.realized_pnl_jpy:+,.1f} 円`**", "inline": True},
            {"name": "証拠金残高", "value": col_text, "inline": True},
            {"name": "実約定価格推移", "value": f"`{entry_price:,.0f} 円` ➔ `{exit_price:,.0f} 円`", "inline": False},
            {"name": "手仕舞い理由", "value": f"{reason}", "inline": False},
        ]
        try:
            self.notifier.send_embed(
                title=title,
                description=f"**決済時刻**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S JST')}` | 戦略: `EmaTrend`",
                fields=fields,
                color=color,
                footer_text="Antigravity LIVE Trade Sentinel 🔴",
                target="trade",
            )
        except Exception as ex:
            print(f"[Notifier] 決済速報送信失敗: {ex}", flush=True)

    def _dispatch_order(self, side: str, size: float, price: float, reason: str, is_exit: bool = False):
        # 1. In-Flightチェック（二重発注防止）
        if not self.order_state.can_place_order():
            return

        # 2. GAPCORE流 APIレートリミットチェック (429 BAN物理封鎖 & ポートフォリオ/戦略別クールダウン)
        allowed, rate_reason = self.order_rate_guard.check_order_allowed(strategy_id="portfolio")
        if not allowed and not is_exit:
            print(f"[ORDER GUARD] 🛡️ APIレートリミット待機 (発注保留): {rate_reason}", flush=True)
            return

        # 3. 発注前整合性ガード (物理建玉判定)
        phys_qty = self.orchestrator.exchange_actual_qty
        phys_pend = self.orchestrator.exchange_pending_qty
        if is_exit and abs(phys_qty) < 1e-9 and abs(phys_pend) < 1e-9:
            print(f"[ORDER GUARD] 🛡️ 既に建玉が存在しないため手仕舞い発注をスキップします: {reason}", flush=True)
            return

        # 4. 発注直前に PendingQty を加算（GAPCORE Invariant: 即座に required_qty = 0 になり二重発注防止）
        self.position_mgr.on_order_sent(side, size)
        self.orchestrator.on_order_sent(side, size)

        # 5. 発注送信
        try:
            mode_tag = "【🔴 REAL LIVE発注】" if self.enable_real_order else "【⚪ SIMULATED】"
            print(f"\n[ORDER] {mode_tag} {side} {size} BTC @ {price:,.0f} 円 | 理由: {reason}", flush=True)

            resp = self.client.send_order(
                product_code=self.symbol,
                side=side,
                size=size,
                order_type="MARKET",
            )
            order_id = resp.get("child_order_acceptance_id", f"sim_{int(time.time()*1000)}")

            self.order_state.set_inflight(order_id)
            self.order_rate_guard.record_order(strategy_id="portfolio")

            # 約定待ち & 実約定価格の取得 (発注直後のみ1回取得)
            time.sleep(0.6)
            exec_price = price
            if self.enable_real_order:
                try:
                    my_execs = self.client.get_my_executions(self.symbol, count=3)
                    if my_execs:
                        exec_price = float(my_execs[0].get("price", price))
                except Exception:
                    pass

            # GAPCORE PortfolioOrchestrator で物理約定を適用し、各戦略Shadowに分配
            with self.state_lock:
                prev_entry_price = self.orchestrator.exchange_avg_price or self.position_mgr.avg_price
                trade_pnl = self.orchestrator.on_exchange_fill(side, size, exec_price, timestamp=time.time())
                self.current_collateral = self.initial_collateral + self.portfolio_guard.portfolio_realized_jpy

                # GAPCORE流 AgingGuard: 物理建玉更新を反映 (FIFO滞留タイマー管理)
                self.aging_guard.on_position_update(self.orchestrator.exchange_actual_qty, price=exec_price)

            # 決済損益が発生した場合、または手仕舞いの場合
            if abs(trade_pnl) > 1e-9 or is_exit:
                pf_pnl = self.portfolio_guard.portfolio_realized_jpy
                self.trades_history.append({
                    "timestamp": time.time(),
                    "side": side,
                    "size": size,
                    "entry_price": prev_entry_price,
                    "exit_price": exec_price,
                    "pnl": trade_pnl,
                    "reason": reason,
                })
                self.trade_count += 1
                sign = "+" if trade_pnl >= 0 else ""
                print(
                    f"[SETTLED] 🎯 手仕舞い完了: 損益 {sign}{trade_pnl:,.1f} 円 "
                    f"(本日JST累計: {self.daily_pnl_guard.realized_jpy:+,.1f} 円 / 限度: -{self.daily_limit_jpy:,.0f} 円 | "
                    f"PF累計: {pf_pnl:+,.1f} 円 / 限度: -{self.portfolio_limit_jpy:,.0f} 円, "
                    f"推定証拠金: {self.current_collateral:,.1f} 円)",
                    flush=True
                )

                # Discord手仕舞い・損益速報送信
                self._send_trade_notification(side, size, prev_entry_price, exec_price, trade_pnl, reason)

                # LotScaleGuard にトレード結果を記録 & 即時降格/Fail-Closedチェック
                self.lot_scale_guard.record_trade_result(trade_pnl)
                ema_metrics = self.position_mgr.get_metrics(hours=24.0)
                self.lot_scale_guard.evaluate(
                    metrics=ema_metrics,
                    collateral=self.current_collateral,
                    daily_loss_used=abs(min(0.0, self.daily_pnl_guard.realized_jpy)),
                    daily_loss_limit=self.daily_limit_jpy,
                )

            # ドテンまたは新規エントリーで新ポジションが残っている場合
            if abs(self.orchestrator.exchange_actual_qty) > 1e-9 and not is_exit:
                self.trade_count += 1
                print(f"[ENTERED] 🚀 ポジション保有開始: {self.orchestrator.exchange_actual_qty:+.3f} BTC @ {exec_price:,.0f} 円 (ローカル会計更新)", flush=True)

                # Discordエントリー速報送信
                self._send_entry_notification(side, size, exec_price, reason)

            # In-Flight解除
            self.order_state.clear_inflight(order_id)

        except Exception as e:
            print(f"[ORDER ERROR] ⚠️ 発注処理失敗: {e}", flush=True)
            # 発注失敗時は PendingQty をロールバック！
            self.position_mgr.on_order_failed(side, size)
            self.orchestrator.on_order_failed(side, size)
            self.order_state.clear_inflight()

    def check_and_send_hourly_report(self, force: bool = False):
        """1時間毎のLIVE状態KPIレポート送信 (毎時00分正時、またはインターバル経過、または起動時force)"""
        now = time.time()
        now_dt = datetime.now()
        is_on_the_hour = (now_dt.minute == 0 and (now - self.last_hourly_report_time > 120.0))
        is_interval_elapsed = (now - self.last_hourly_report_time >= 3600.0)

        if not (force or is_on_the_hour or is_interval_elapsed):
            return

        # 最新の取引所残高・建玉を同期
        if self.enable_real_order:
            self.sync_account_state()

        with self.state_lock:
            eval_price = self.current_mid if self.current_mid > 0 else self.current_price
            pf_snap = self.orchestrator.get_snapshot(eval_price)
            strat_snaps = pf_snap.get("strategy_snapshots", {})

            # 1. 戦略別損益 / PF / MaxDD (24h)
            strategies_kpi = {}
            for sid, snap in strat_snaps.items():
                strategies_kpi[sid] = {
                    "realized_pnl": snap.get("realized_pnl", 0.0),
                    "unrealized_pnl": snap.get("unrealized_pnl", 0.0),
                    "profit_factor": snap.get("profit_factor", 0.0),
                    "max_dd_24h": snap.get("max_dd_24h", 0.0),
                    "win_rate_pct": snap.get("win_rate_pct", 0.0),
                    "trades_count": snap.get("trades_count", 0),
                    "position_btc": snap.get("actual_qty", 0.0),
                }

            # 2. PF損益
            pf_realized = self.portfolio_guard.portfolio_realized_jpy
            pf_unrealized = self.open_position_pnl if self.enable_real_order else pf_snap.get("total_unrealized_pnl", 0.0)
            col_val = self.current_collateral

            # 3. 内部ネッティング & コスト節約
            netting_cnt = self.orchestrator.netting_events_count
            spread_savings = self.orchestrator.spread_savings_jpy
            phys_orders = self.orchestrator.physical_orders_dispatched

            # 4. 429対策 & 発注枠使用率 (Tier0〜2)
            skips_429 = self.orchestrator.rate_limit_skips_count
            rate_status = self.order_rate_guard.get_status(now_ts=now)
            tier0_rpm = rate_status.get("orders_last_1m", 0)
            tier0_max = rate_status.get("orders_limit_1m", 30)
            tier1_used = abs(min(0.0, pf_realized))
            tier1_limit = self.portfolio_limit_jpy

            dd_val = max(0.0, self.circuit_breaker.peak_pnl - (pf_realized + pf_unrealized))
            dd_remain = max(0.0, self.circuit_breaker.max_drawdown_limit_jpy - dd_val)
            tier2_status = "🚨 HALTED" if self.circuit_breaker.is_halted else f"🟢 正常 (残余: {dd_remain:,.0f}円)"

            tier_usage = {
                "tier0_current_rpm": float(tier0_rpm),
                "tier0_max_rpm": tier0_max,
                "tier1_daily_loss_used": tier1_used,
                "tier1_daily_loss_limit": tier1_limit,
                "tier2_circuit_breaker": tier2_status,
            }

            # 5. Regime滞在比率
            tot_regime_ticks = sum(self.regime_counts.values())
            if tot_regime_ticks > 0:
                regime_dist = {
                    k: round(v / tot_regime_ticks * 100.0, 1)
                    for k, v in self.regime_counts.items()
                }
            else:
                curr_reg = self.orchestrator.current_regime or "NORMAL"
                regime_dist = {curr_reg: 100.0}

            # 6. 自律ロットスケーリング評価 & 進捗サマリー
            ema_metrics = self.position_mgr.get_metrics(hours=24.0)
            self.lot_scale_guard.evaluate(
                metrics=ema_metrics,
                collateral=col_val,
                daily_loss_used=tier1_used,
                daily_loss_limit=tier1_limit,
                now_ts=now,
            )
            lot_scale_info = self.lot_scale_guard.get_promotion_progress(metrics=ema_metrics, collateral=col_val)

            kpi_data = {
                "jst_time": now_dt.strftime("%Y-%m-%d %H:%M:%S JST"),
                "mid_price": eval_price,
                "current_regime": self.orchestrator.current_regime,
                "pf_realized_pnl": pf_realized,
                "pf_unrealized_pnl": pf_unrealized,
                "portfolio_position_btc": self.orchestrator.exchange_actual_qty,
                "collateral": col_val,
                "strategies": strategies_kpi,
                "netting_events_count": netting_cnt,
                "spread_savings_jpy": spread_savings,
                "physical_orders_dispatched": phys_orders,
                "rate_limit_skips_count": skips_429,
                "tier_usage": tier_usage,
                "regime_distribution": regime_dist,
                "lot_scale_info": lot_scale_info,
            }

        try:
            self.notifier.send_hourly_kpi_report(kpi_data, symbol=self.symbol, target="report")
            print(f"\n[Discord] 📊 LIVE 毎時状態KPIレポートを送信しました ({now_dt.strftime('%H:%M:%S')})", flush=True)
        except Exception as ex:
            print(f"[Discord] 毎時状態KPIレポート送信失敗: {ex}", flush=True)

        self.last_hourly_report_time = now

    def start(self):
        print("=" * 65)
        print("  🚀 ANTIGRAVITY LIVE PRODUCTION RUNNER (GAPCORE Armed)")
        print(f"  • モード: {'【🔴 実資金 LIVE 本番発注】' if self.enable_real_order else '【⚪ DRYRUN / ペーパー検証】'}")
        print(f"  • 対象銘柄: {self.symbol}")
        print(f"  • 注文ロット: {self.order_size} BTC")
        print(f"  • 単一戦略: EmaTrendTickStrategy (Fast:60s / Slow:300s)")
        print(f"  • 防護策: 発注最短3秒間隔 / 1分最大8回 / In-Flightロック")
        print(f"  • 基準証拠金: {self.initial_collateral:,.1f} JPY")
        print(f"  • ピークDDリミット: {self.circuit_breaker.max_drawdown_limit_jpy:,.0f} JPY")
        print(f"  • GAPCORE日次損切リミット: -{self.daily_limit_jpy:,.0f} JPY (JST 00:00永続化)")
        print(f"  • GAPCORE在庫滞留上限: {self.max_hold_seconds:.0f} 秒 (Aging Guard)")
        print(f"  • {self.daily_pnl_guard.startup_log_line()}")
        print("=" * 65, flush=True)

        if self.daily_pnl_guard.is_halted:
            print(
                f"\n[CRITICAL WARN] ⚠️ DailyPnLGuard が HALTED 状態です！\n"
                f"本日({self.daily_pnl_guard.jst_day})の損失額がリミット({self.daily_limit_jpy:,.0f}円)を超過しています。\n"
                f"新規エントリー注文はすべて自動ブロックされます。\n",
                flush=True
            )

        self.check_initial_account_status()


        self.is_running = True
        self.stream.start()

        # RESTから直近Tickを初期取得して現在価格を即時初期化
        try:
            init_ticks = self.stream.fetch_latest_ticks(count=20)
            if init_ticks:
                self.current_price = init_ticks[-1].get("price", 0.0)
                for t in init_ticks:
                    self.strategy._update_emas(t.get("price", 0.0), t.get("timestamp", time.time()))
        except Exception:
            pass

        mode_str = "実資金 LIVE モード" if self.enable_real_order else "DRYRUN モード"

        self.notifier.send_system_update(
            title=f"🚀 Antigravity LIVE Engine 稼働開始 ({mode_str})",
            description=(
                f"**銘柄**: `{self.symbol}`\n"
                f"**戦略**: `EmaTrendTickStrategy (単一集中)`\n"
                f"**ロット**: `{self.order_size} BTC`\n"
                f"**基準証拠金**: `{self.initial_collateral:,.1f} JPY`\n"
                f"**最大DDリミット**: `{self.circuit_breaker.max_drawdown_limit_jpy:,.0f} JPY`\n"
                f"取引所公式API完全同期・レートリミッター・1時間定期レポートが有効です。"
            ),
        )

        # 起動時初期レポート送信
        try:
            self.check_and_send_hourly_report(force=True)
        except Exception as ex:
            print(f"[Notifier] 起動時初回レポート送信失敗: {ex}", flush=True)

    def stop(self, reason: str = "オペレータ手動停止"):
        print(f"\n[INFO] 🛑 停止シーケンス開始: {reason}", flush=True)
        self.is_running = False
        self.stream.stop()

        # 残存建玉の安全決済
        with self.state_lock:
            pos = self.position_mgr.actual_qty
            if pos != 0.0:
                exit_side = "SELL" if pos > 0 else "BUY"
                size = abs(pos)
                self.position_mgr.set_target(0.0, reason)
                print(f"[STOP] 残存建玉を成行決済: {exit_side} {size} BTC", flush=True)
                try:
                    self.client.send_order(
                        product_code=self.symbol,
                        side=exit_side,
                        size=size,
                        order_type="MARKET",
                    )
                    trade_pnl = self.position_mgr.on_fill(exit_side, size, self.current_price)
                    self.daily_pnl_guard.record_trade_pnl(trade_pnl)
                    self.aging_guard.on_position_update(0.0)
                except Exception as e:
                    print(f"[STOP ERROR] 停止時決済失敗: {e}", flush=True)

        try:
            self.notifier.send_manual_stop_alert(
                service_name="Antigravity LIVE Engine",
                reason=reason,
                position_closed=(self.position_mgr.actual_qty == 0.0),
                remaining_position_btc=self.position_mgr.actual_qty,
                final_pnl_jpy=self.position_mgr.realized_pnl,
            )
        except Exception:
            pass
        print("[INFO] エンジンを安全に完全停止しました。", flush=True)

    def run_forever(self):
        self.start()
        last_log_time = 0.0
        try:
            while self.is_running:
                # 1時間毎の成績レポート送信チェック (毎時00分正時、または1時間間隔)
                self.check_and_send_hourly_report()

                now = time.time()
                # 5秒おきにリアルタイムステータス表示 (完全インメモリ・取引所アクセスなし)
                if now - last_log_time >= 5.0:
                    with self.state_lock:
                        snap = self.orchestrator.get_snapshot(self.current_mid or self.current_price)
                        phys_act = snap["exchange_actual_qty"]
                        pf_tgt = snap["portfolio_target_qty"]
                        pf_req = snap["required_exchange_qty"]

                        pos_str = f"{phys_act:+.3f} BTC" if abs(phys_act) > 1e-9 else "FLAT"
                        tgt_str = f"{pf_tgt:+.3f}"
                        req_str = f"{pf_req:+.3f}"

                        age_sec = self.aging_guard.get_age_seconds()
                        age_str = f" | Age:{age_sec:3.0f}s/{self.max_hold_seconds:.0f}s" if abs(phys_act) > 1e-9 else ""

                        daily_pnl = self.daily_pnl_guard.realized_jpy
                        daily_rem = max(0.0, self.daily_limit_jpy + daily_pnl)
                        pf_pnl = self.portfolio_guard.portfolio_realized_jpy
                        pf_rem = max(0.0, self.portfolio_limit_jpy + pf_pnl)
                        guard_badge = " [🚨HALTED]" if (self.daily_pnl_guard.is_halted or self.portfolio_guard.portfolio_halted) else ""

                        fast_e = self.strategy.fast_ema or 0.0
                        slow_e = self.strategy.slow_ema or 0.0
                        spread_e = (fast_e - slow_e)
                        col_str = f"推定残高: {self.current_collateral:,.0f}円" if self.enable_real_order else "SIM"

                        # Strategy breakdown
                        s_ema = snap["strategy_snapshots"].get("EmaTrend", {}).get("actual_qty", 0.0)
                        s_mr = snap["strategy_snapshots"].get("MeanReversion", {}).get("actual_qty", 0.0)
                        s_obi = snap["strategy_snapshots"].get("OrderBookImbalance", {}).get("actual_qty", 0.0)
                        strat_str = f"EMA:{s_ema:+.3f}|MR:{s_mr:+.3f}|OBI:{s_obi:+.3f}"

                        regime = snap["current_regime"]
                        net_cnt = snap["netting_events_count"]
                        net_sav = snap["spread_savings_jpy"]

                    t_str = datetime.now().strftime("%H:%M:%S")
                    mid_val = self.current_mid if self.current_mid > 0 else self.current_price
                    imb_val = self.latest_flow_stats.get("book_imbalance", 0.0)
                    sp_bp = self.latest_flow_stats.get("spread_bp", 0.0)
                    print(
                        f" [{t_str}] Mid: {mid_val:10,.0f} 円 (Sprd:{sp_bp:3.1f}bp, Imb:{imb_val:+.2f}) | "
                        f"PF Pos: {pos_str:9s} (Tgt:{tgt_str}, Req:{req_str}) [{strat_str}]{age_str} | "
                        f"Regime: {regime:<7s} | "
                        f"損益: {snap['total_pnl']:+6.1f} 円 (本日JST: {daily_pnl:+6.1f} 円, PF: {pf_pnl:+6.1f} 円, 残余:{daily_rem:,.0f}円/{pf_rem:,.0f}円, {col_str}){guard_badge} | "
                        f"Netting: {net_cnt}回 (+{net_sav:,.0f}円) | 取引: {self.trade_count}回",
                        flush=True
                    )
                    last_log_time = now

                time.sleep(1.0)

        except KeyboardInterrupt:
            self.stop(reason="ユーザーによるキーボード割り込み (Ctrl+C)")
        except Exception as e:
            self.stop(reason=f"例外発生強制停止: {e}")
            raise


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Antigravity LIVE Production Runner")
    parser.add_argument("--symbol", default="FX_BTC_JPY", help="取引シンボル")
    parser.add_argument("--size", type=float, default=0.001, help="注文ロット (BTC)")
    parser.add_argument("--max-dd", type=float, default=1500.0, help="許容最大ピークドローダウン (円)")
    parser.add_argument("--daily-limit-jpy", type=float, default=None, help="JST暦日 許容最大実現損失 (円) [既定: max-ddと同額]")
    parser.add_argument("--portfolio-limit-jpy", type=float, default=None, help="ポートフォリオ全体 許容最大日次損失 (円) [既定: daily-limitの2倍]")
    parser.add_argument("--max-hold-sec", type=float, default=300.0, help="在庫滞留タイムアウト上限 (秒) [既定: 300秒 / 5分]")
    parser.add_argument("--initial-collateral", type=float, default=6982.0, help="本日運用開始時の基準証拠金 (円)")
    parser.add_argument("--real", action="store_true", help="実資金での本番発注を有効化")
    parser.add_argument("--yes", action="store_true", help="本番確認プロンプトをスキップして即時起動")
    args = parser.parse_args()

    is_real = False
    daily_limit = args.daily_limit_jpy if args.daily_limit_jpy is not None else args.max_dd
    pf_limit = args.portfolio_limit_jpy if args.portfolio_limit_jpy is not None else (daily_limit * 2.0)

    if args.real:
        if not args.yes:
            print("\n" + "!" * 65)
            print("         ⚠️ 【警告: bitFlyer Lightning 実資金取引モード】 ⚠️        ")
            print(f"  • シンボル: {args.symbol}")
            print(f"  • ロット:   {args.size} BTC")
            print(f"  • 基準残高: {args.initial_collateral:,.0f} 円")
            print(f"  • 最大DD:   -{args.max_dd:,.0f} 円")
            print(f"  • 日次損切: -{daily_limit:,.0f} 円 (JST 00:00永続化)")
            print(f"  • PF日次損切: -{pf_limit:,.0f} 円 (JST 00:00永続化)")
            print(f"  • 在庫上限: {args.max_hold_sec:.0f} 秒 (Aging Guard)")
            print("  実際のお金で売買注文が bitFlyer API へ直接送信されます。")
            print("!" * 65)
            confirm = input("\n本当に本番実資金トレードを開始しますか？ (開始する場合は 'YES' と入力): ")
            if confirm.strip() != "YES":
                print("\n[中止] 本番モードはキャンセルされました。ペーパートレードとして起動します。\n")
                is_real = False
            else:
                is_real = True
        else:
            is_real = True

    runner = LiveRunner(
        symbol=args.symbol,
        order_size=args.size,
        max_drawdown_limit=args.max_dd,
        daily_limit_jpy=daily_limit,
        portfolio_limit_jpy=pf_limit,
        max_hold_seconds=args.max_hold_sec,
        enable_real_order=is_real,
        initial_collateral=args.initial_collateral,
    )
    runner.run_forever()



if __name__ == "__main__":
    main()

