import time
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional

from ..config.settings import Settings
from ..ws_engine.stream import WebSocketTickStream
from ..risk_guard.circuit_breaker import PeakDrawdownCircuitBreaker
from ..risk_guard.performance import PerformanceTracker, get_jst_now
from ..risk_guard.notifier import DiscordNotifier
from ..risk_guard.system_monitor import SystemResourceMonitor
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
            on_warning_callback=self._on_circuit_breaker_warning,
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
        self.last_entry_notification_time: Dict[str, float] = {}
        self.last_trade_notification_time: Dict[str, float] = {}

        # システムリソース監視・自動改善エンジン
        self.sys_monitor = SystemResourceMonitor()
        self.last_resource_check_time = time.time()
        self.last_resource_alert_time = 0.0

        # WebSocketストリーム初期化
        self.stream = WebSocketTickStream(
            product_code=self.product_code,
            window_seconds=15.0,
            on_ticks_callback=self._on_ticks_received,
            on_connect_callback=self._on_ws_connected,
            on_disconnect_callback=self._on_ws_disconnected,
        )

    def _on_circuit_breaker_warning(self, reason: str, current_dd: float, total_pnl: float):
        """ドローダウン警戒コールバック：許容上限に接近した段階でDiscordへ早期警戒通報"""
        print(f"\n[AntigravityRunner] ⚠️ DRAWDOWN WARNING: {reason}", flush=True)
        self.notifier.send_drawdown_alert(
            current_dd=current_dd,
            max_dd=self.circuit_breaker.max_drawdown_limit_jpy,
            peak_pnl=self.circuit_breaker.peak_pnl,
            current_pnl=total_pnl,
            is_halted=False,
            reason=reason,
            symbol=self.product_code,
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

        self.notifier.send_drawdown_alert(
            current_dd=current_dd,
            max_dd=self.circuit_breaker.max_drawdown_limit_jpy,
            peak_pnl=self.circuit_breaker.peak_pnl,
            current_pnl=total_pnl,
            is_halted=True,
            reason=reason,
            symbol=self.product_code,
        )

    def _on_circuit_breaker_resumed(self, reason: str):
        """運用自動復帰コールバック"""
        print(f"\n[AntigravityRunner] 🟢 RESUMING TRADING: {reason}", flush=True)
        self.notifier.send_system_recovered_alert(
            service_name=f"Antigravity HFT ({self.product_code})",
            message=f"冷却期間が経過し、相場の安定を確認したため取引を自動再開しました。\n要因: {reason}",
        )

    def _on_ws_connected(self):
        """WebSocket接続確立コールバック"""
        print(f"[AntigravityRunner] 🟢 WebSocket接続確立: {self.product_code}", flush=True)

    def _on_ws_disconnected(self, reason: str):
        """WebSocket切断コールバック"""
        print(f"[AntigravityRunner] ⚠️ WebSocket切断検知: {reason}", flush=True)
        self.notifier.send_emergency_alert(
            title="【WebSocket切断・通信障害検知】",
            message=(
                f"**bitFlyer Lightning WebSocketストリームの切断を検知しました。**\n\n"
                f"• **市場**: `{self.product_code}`\n"
                f"• **要因**: `{reason}`\n"
                f"• **状態**: 自動再接続ループが継続実行中です。"
            ),
            level="warning",
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
            sign = "+" if trade_pnl >= 0 else ""
            print(f"[WS-ms] 🎯 【ミリ秒手仕舞い】[{name}] 損益: {sign}{trade_pnl:.1f} 円 | 理由: {reason}", flush=True)

            # DRYRUN 手仕舞い速報 (0.5円以上の損益、または15秒以上間隔が空いた決済をDiscord通知)
            last_t_notif = self.last_trade_notification_time.get(name, 0.0)
            if abs(trade_pnl) >= 0.5 or (now - last_t_notif >= 15.0):
                try:
                    self.notifier.send_dryrun_trade_report(
                        strategy_name=name,
                        side=side,
                        size_btc=abs(pos),
                        entry_price=ep,
                        exit_price=price,
                        pnl_jpy=trade_pnl,
                        reason=reason,
                        symbol=self.product_code,
                    )
                    self.last_trade_notification_time[name] = now
                except Exception as ex:
                    print(f"[AntigravityRunner] 決済速報送信失敗: {ex}", flush=True)
        elif pos == 0:
            # 新規エントリー
            trade_size = self.order_size
            s_info["position_btc"] = trade_size if side == "BUY" else -trade_size
            s_info["entry_price"] = price
            s_info["entry_time"] = now
            print(f"[WS-ms] 🚀 【ミリ秒ENTRY】[{name}] {side} {trade_size} BTC @ {price:,.0f} 円 | 理由: {reason}", flush=True)

            # DRYRUN 新規エントリー速報 (過剰連投防止スロットリング: 戦略ごとに15秒間隔)
            last_e_notif = self.last_entry_notification_time.get(name, 0.0)
            if now - last_e_notif >= 15.0:
                try:
                    self.notifier.send_dryrun_entry_report(
                        strategy_name=name,
                        side=side,
                        size_btc=trade_size,
                        price=price,
                        reason=reason,
                        symbol=self.product_code,
                    )
                    self.last_entry_notification_time[name] = now
                except Exception as ex:
                    print(f"[AntigravityRunner] エントリー速報送信失敗: {ex}", flush=True)


    def check_and_send_regular_report(self, force: bool = False):
        """定期レポート送信チェック (毎時00分正時、またはインターバル経過、または起動時force)"""
        now = time.time()
        now_jst = get_jst_now()
        is_on_the_hour = (now_jst.minute == 0 and (now - self.last_report_time > 120.0))
        is_interval_elapsed = (now - self.last_report_time >= self.report_interval_sec)

        if force or is_on_the_hour or is_interval_elapsed:
            with self.state_lock:
                trades_map = {name: list(s["trades_history"]) for name, s in self.strategies_map.items()}
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
            success = self.notifier.send_regular_report(snapshot, symbol=self.product_code)
            self.last_report_time = now
            t_str = now_jst.strftime("%Y-%m-%d %H:%M:%S JST")
            status_str = "成功" if success else "送信失敗/Webhook未設定"
            trigger_reason = "起動時" if force else ("正時(00分)" if is_on_the_hour else "インターバル経過")
            print(f"[AntigravityRunner] 📊 定期運用レポート送信 ({trigger_reason}): {t_str} -> {status_str}", flush=True)

    def check_system_resources_and_remediate(self, force: bool = False):
        """
        サーバーリソース（CPU/MEM/DISK）を常時監視し、
        圧迫検知時は直ちに（その都度）緊急アラートを発報し自己修復を実行。
        定時（毎時00分正時）には健全性レポートを送信。
        """
        now = time.time()
        now_jst = get_jst_now()
        is_on_the_hour = (now_jst.minute == 0 and (now - self.last_resource_check_time > 120.0))
        is_interval = (now - self.last_resource_check_time >= self.report_interval_sec)

        # 30秒ごとに高負荷・圧迫の早期検知チェック
        should_check_early = (now - self.last_resource_alert_time >= 30.0)

        if force or is_on_the_hour or is_interval or should_check_early:
            metrics = self.sys_monitor.collect_all_metrics()
            is_warning = metrics.get("is_warning", False)
            is_critical = metrics.get("is_critical", False)
            is_anomaly = is_warning or is_critical

            if is_anomaly:
                # 異常検知時：クールダウンが経過しているか、またはcriticalへの昇格時に即時通報
                if force or (now - self.last_resource_alert_time >= 300.0) or is_critical:
                    actions = self.sys_monitor.execute_remediation(metrics)
                    reasons = []
                    disk = metrics.get("disk", {})
                    mem = metrics.get("memory", {})
                    cpu = metrics.get("cpu_pct", 0.0)
                    if disk.get("used_pct", 0.0) >= self.sys_monitor.disk_warning_pct:
                        reasons.append(f"ディスク容量逼迫 ({disk.get('used_pct', 0.0):.1f}% 使用中)")
                    if mem.get("used_pct", 0.0) >= self.sys_monitor.mem_warning_pct:
                        reasons.append(f"メモリ逼迫 ({mem.get('used_pct', 0.0):.1f}% 使用中)")
                    if cpu >= self.sys_monitor.cpu_warning_pct:
                        reasons.append(f"CPU高負荷逼迫 ({cpu:.1f}%)")

                    level = "critical" if is_critical else "warning"
                    self.notifier.send_resource_pressure_alert(
                        metrics=metrics,
                        trigger_reasons=reasons,
                        remediation_actions=actions,
                        server_name=f"Antigravity HFT ({self.product_code})",
                        level=level,
                    )
                    self.last_resource_alert_time = now
                    self.last_resource_check_time = now
            elif force or is_on_the_hour or is_interval:
                # 正常時の定時診断レポート
                actions = self.sys_monitor.execute_remediation(metrics)
                self.notifier.send_system_resource_report(
                    metrics=metrics,
                    remediation_actions=actions,
                    server_name=f"Antigravity HFT ({self.product_code})",
                )
                self.last_resource_check_time = now

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

        # 起動時初期リソース診断＆通知
        try:
            self.check_system_resources_and_remediate(force=True)
        except Exception as ex:
            print(f"[AntigravityRunner] 初期リソース診断通知失敗: {ex}", flush=True)

        # 起動時初回定期レポート送信
        try:
            self.check_and_send_regular_report(force=True)
        except Exception as ex:
            print(f"[AntigravityRunner] 初回定期レポート送信失敗: {ex}", flush=True)

    def stop(self, reason: str = "オペレータによる手動停止（SIGINT/SIGTERM）"):
        """取引実行を安全に停止し、必要に応じて建玉を決済して通知を送信"""
        print(f"[AntigravityRunner] 🛑 停止処理中 ({reason})...", flush=True)
        self.is_running = False
        self.stream.stop()

        # 安全な残存建玉の決済処理
        closed_all = True
        total_remaining_btc = 0.0
        with self.state_lock:
            for name, s_info in self.strategies_map.items():
                pos = s_info["position_btc"]
                if abs(pos) > 1e-6:
                    exit_side = "SELL" if pos > 0 else "BUY"
                    try:
                        print(f"[AntigravityRunner] 🛑 停止時緊急決済: {name} {pos:+.4f} BTC ({exit_side})", flush=True)
                        self.client.send_order(
                            product_code=self.product_code,
                            side=exit_side,
                            size=abs(pos),
                            order_type="MARKET",
                        )
                        s_info["position_btc"] = 0.0
                    except Exception as ex:
                        print(f"[AntigravityRunner] ⚠️ 停止時決済エラー ({name}): {ex}", flush=True)
                        closed_all = False
                        total_remaining_btc += pos

            tot_realized = sum(s["realized_pnl"] for s in self.strategies_map.values())

        # 手動停止通知の送信
        try:
            self.notifier.send_manual_stop_alert(
                service_name=f"Antigravity HFT ({self.product_code})",
                reason=reason,
                position_closed=closed_all,
                remaining_position_btc=total_remaining_btc,
                final_pnl_jpy=tot_realized,
            )
        except Exception as ex:
            print(f"[AntigravityRunner] 手動停止通知送信失敗: {ex}", flush=True)



    def run_forever(self, poll_interval: float = 1.0):
        """メインブロッキングループ"""
        self.start()
        try:
            while self.is_running:
                self.check_and_send_regular_report()
                self.check_system_resources_and_remediate()

                # コンソール・ログへのリアルタイム稼働状況表示
                with self.state_lock:
                    tot_realized = sum(s["realized_pnl"] for s in self.strategies_map.values())
                    tot_unrealized = sum(
                        (self.current_price - s["entry_price"]) * s["position_btc"]
                        for s in self.strategies_map.values()
                        if s["position_btc"] != 0 and s["entry_price"] > 0
                    )
                    tot_pnl = tot_realized + tot_unrealized
                    strats_summary = " | ".join([
                        f"{name}:{len(s['trades_history'])}回({s['position_btc']:+.3f})"
                        for name, s in self.strategies_map.items()
                    ])
                    stats = self.stream.compute_flow_stats()
                    d_ratio = stats.get("delta_ratio", 0.0)
                    p_change = stats.get("price_change_bp", 0.0)
                    flow_sign = "BUY優勢" if d_ratio > 0.2 else ("SELL優勢" if d_ratio < -0.2 else "拮抗")
                    ws_indicator = "⚡WS" if self.stream.is_ws_connected else "REST"
                    halt_tag = " [🚨HALTED]" if self.circuit_breaker.is_halted else ""

                t_str = datetime.now().strftime("%H:%M:%S")
                print(
                    f" [{t_str}] [{ws_indicator}]{halt_tag} {self.current_price:10,.0f} 円 "
                    f"(Δ{p_change:+4.1f}bp | {flow_sign} {d_ratio:+.2f}) | "
                    f"損益: {tot_pnl:+6.1f} 円 | {strats_summary}",
                    flush=True
                )

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\n[AntigravityRunner] KeyboardInterrupt 検知。終了処理中...")
            self.stop(reason="ユーザー操作（Ctrl+C / KeyboardInterrupt）による手動停止")
        finally:
            if self.is_running:
                self.stop(reason="プロセス終了（SIGTERM / システムシャットダウン）")



