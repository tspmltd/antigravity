import time
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional

from ..config.settings import Settings
from ..ws_engine.stream import WebSocketTickStream
from ..risk_guard.circuit_breaker import PeakDrawdownCircuitBreaker
from ..risk_guard.performance import PerformanceTracker
from ..risk_guard.notifier import DiscordNotifier
from ..strategies.base import BaseTickStrategy
from .client import BitflyerClient


class AntigravityRunner:
    """
    Antigravity HFT 高頻度取引総合ランタイム。
    WebSocketのミリ秒プッシュ受信、多戦略並行実行、ポジション管理、
    ピークドローダウン監視、およびDiscord定期レポートを統括します。
    """

    def __init__(
        self,
        strategies: List[BaseTickStrategy],
        product_code: str = Settings.PRODUCT_CODE,
        order_size: float = Settings.ORDER_SIZE_BTC,
        maker_fee_pct: float = Settings.MAKER_FEE_PCT,
        taker_fee_pct: float = Settings.TAKER_FEE_PCT,
        max_drawdown_jpy: float = Settings.MAX_DRAWDOWN_LIMIT_JPY,
        cooldown_sec: float = Settings.CIRCUIT_BREAKER_COOLDOWN_SEC,
        report_interval_sec: float = Settings.REPORT_INTERVAL_SEC,
        notifier: Optional[DiscordNotifier] = None,
        client: Optional[BitflyerClient] = None,
    ):
        self.product_code = product_code
        self.order_size = order_size
        self.maker_fee_pct = maker_fee_pct
        self.taker_fee_pct = taker_fee_pct
        self.report_interval_sec = report_interval_sec

        self.notifier = notifier or DiscordNotifier()
        self.client = client or BitflyerClient()
        self.tracker = PerformanceTracker()

        # サーキットブレーカーの初期化
        self.circuit_breaker = PeakDrawdownCircuitBreaker(
            max_drawdown_limit_jpy=max_drawdown_jpy,
            cooldown_seconds=cooldown_sec,
            on_trip_callback=self._on_circuit_breaker_tripped,
            on_resume_callback=self._on_circuit_breaker_resumed,
        )

        # 戦略マップ
        self.strategies_map: Dict[str, Dict[str, Any]] = {}
        for s in strategies:
            self.strategies_map[s.name] = {
                "instance": s,
                "position_btc": 0.0,
                "entry_price": 0.0,
                "entry_time": None,
                "realized_pnl": 0.0,
                "trades_history": [],
            }

        self.state_lock = threading.Lock()
        self.is_running = False
        self.last_report_time = time.time()
        self.current_price = 0.0

        # WebSocketストリーム初期化
        self.stream = WebSocketTickStream(
            product_code=self.product_code,
            window_seconds=15.0,
            on_ticks_callback=self._on_ticks_received,
        )

    def _on_circuit_breaker_tripped(self, reason: str, current_dd: float, total_pnl: float):
        """緊急停止コールバック：保有建玉を直ちに一括決済しDiscordへ通報"""
        print(f"\n[AntigravityRunner] 🚨 EMERGENCY HALT: {reason}", flush=True)
        with self.state_lock:
            for name, s_info in self.strategies_map.items():
                pos = s_info["position_btc"]
                if pos != 0:
                    exit_side = "SELL" if pos > 0 else "BUY"
                    try:
                        self.client.send_order(
                            product_code=self.product_code,
                            side=exit_side,
                            size=abs(pos),
                            order_type="MARKET",
                        )
                    except Exception:
                        pass
                    s_info["position_btc"] = 0.0
                    s_info["entry_price"] = 0.0
                    s_info["entry_time"] = None

        self.notifier.send_emergency_alert(
            title="【緊急停止・サーキットブレーカー発動】",
            message=(
                f"**許容最大ドローダウンを超過したため、全建玉を強制エグジットしました。**\n\n"
                f"• **市場**: `{self.product_code}`\n"
                f"• **トリガー要因**: {reason}\n"
                f"• **ピークからの落ち込み**: `{current_dd:,.1f} 円`\n"
                f"• **現在の総損益**: `{total_pnl:+,.1f} 円`\n"
                f"• **冷却待機期間**: `{int(self.circuit_breaker.cooldown_seconds / 60)} 分間`"
            ),
            level="critical",
        )

    def _on_circuit_breaker_resumed(self, reason: str):
        """運用自動復帰コールバック"""
        print(f"\n[AntigravityRunner] 🟢 RESUMING TRADING: {reason}", flush=True)
        self.notifier.send_emergency_alert(
            title="【自動運用復帰・サーキットブレーカー解除】",
            message=f"冷却期間が経過し、相場の安定を確認したため取引を自動再開しました。\n要因: {reason}",
            level="info",
        )

    def _on_ticks_received(self, ticks: List[Dict[str, Any]], stats: Dict[str, Any]):
        """WebSocketからミリ秒プッシュされた約定の処理"""
        if not ticks or not self.is_running:
            return

        with self.state_lock:
            latest_p = ticks[-1].get("price", self.current_price)
            if latest_p > 0:
                self.current_price = latest_p

            # サーキットブレーカー更新
            tot_realized = sum(s["realized_pnl"] for s in self.strategies_map.values())
            tot_unrealized = sum(
                (self.current_price - s["entry_price"]) * s["position_btc"]
                for s in self.strategies_map.values()
                if s["position_btc"] != 0 and s["entry_price"] > 0
            )
            self.circuit_breaker.update(tot_realized + tot_unrealized)

            # 停止中は新規発注を行わない
            if self.circuit_breaker.is_halted:
                return

            for tick in ticks:
                for name, s_info in self.strategies_map.items():
                    strat: BaseTickStrategy = s_info["instance"]
                    pos = s_info["position_btc"]
                    ep = s_info["entry_price"]

                    signal = strat.on_tick(tick, stats, pos, ep)
                    act = signal.get("action", "HOLD")

                    if act == "BUY" and pos <= 0:
                        self._execute_trade(name, s_info, "BUY", tick["price"], signal.get("reason", ""))
                    elif act == "SELL" and pos >= 0:
                        self._execute_trade(name, s_info, "SELL", tick["price"], signal.get("reason", ""))
                    elif act in ("EXIT", "CANCEL") and pos != 0:
                        exit_side = "SELL" if pos > 0 else "BUY"
                        self._execute_trade(name, s_info, exit_side, tick["price"], signal.get("reason", ""))

    def _execute_trade(self, name: str, s_info: Dict[str, Any], side: str, price: float, reason: str):
        pos = s_info["position_btc"]
        ep = s_info["entry_price"]
        now = time.time()

        if pos != 0 and ((pos > 0 and side == "SELL") or (pos < 0 and side == "BUY")):
            # ポジション決済
            trade_pnl = ((price - ep) if pos > 0 else (ep - price)) * abs(pos)
            s_info["realized_pnl"] += trade_pnl
            s_info["trades_history"].append({
                "time": datetime.fromtimestamp(now).isoformat(),
                "timestamp": now,
                "strategy": name,
                "side": side,
                "price": price,
                "size": abs(pos),
                "pnl": trade_pnl,
                "reason": reason,
            })
            s_info["position_btc"] = 0.0
            s_info["entry_price"] = 0.0
            s_info["entry_time"] = None
        elif pos == 0:
            # 新規エントリー
            trade_size = self.order_size
            s_info["position_btc"] = trade_size if side == "BUY" else -trade_size
            s_info["entry_price"] = price
            s_info["entry_time"] = now

    def check_and_send_regular_report(self):
        """定期レポート送信チェック"""
        now = time.time()
        if now - self.last_report_time >= self.report_interval_sec:
            with self.state_lock:
                trades_map = {name: s["trades_history"] for name, s in self.strategies_map.items()}
                unrealized_map = {
                    name: (self.current_price - s["entry_price"]) * s["position_btc"]
                    if s["position_btc"] != 0 and s["entry_price"] > 0 else 0.0
                    for name, s in self.strategies_map.items()
                }
                positions_map = {name: s["position_btc"] for name, s in self.strategies_map.items()}

            snapshot = self.tracker.compute_snapshots(
                trades_map=trades_map,
                unrealized_pnl_map=unrealized_map,
                positions_map=positions_map,
                now_ts=now,
            )
            self.notifier.send_regular_report(snapshot, symbol=self.product_code)
            self.last_report_time = now

    def start(self):
        """取引実行を開始"""
        print(f"[AntigravityRunner] 🚀 起動中... 対象シンボル: {self.product_code}", flush=True)
        self.is_running = True
        self.stream.start()

        # RESTから初期Tickをプレフェッチ
        self.stream.fetch_latest_ticks(count=50)

        # 起動通知
        self.notifier.send_system_update(
            title="🚀 Antigravity HFT エンジン稼働開始",
            description=f"**市場**: `{self.product_code}`\n**戦略数**: `{len(self.strategies_map)}`\nミリ秒常時接続が確立されました。",
        )

    def stop(self):
        """取引実行を停止"""
        print("[AntigravityRunner] 🛑 停止中...", flush=True)
        self.is_running = False
        self.stream.stop()

    def run_forever(self, poll_interval: float = 1.0):
        """メインブロッキングループ"""
        self.start()
        try:
            while self.is_running:
                self.check_and_send_regular_report()
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\n[AntigravityRunner] KeyboardInterrupt 検知。終了処理中...")
        finally:
            self.stop()
