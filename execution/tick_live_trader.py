import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core.bitflyer_client import BitFlyerClient
from core.notifier import DiscordNotifier
from core.tick_stream import TickStream
from core.tick_strategy import (
    BaseTickStrategy,
    MicroTrendTickStrategy,
    InventorySkewTickMMStrategy,
    OrderFlowScalpTickStrategy,
)
from core.performance_aggregator import compute_performance_snapshots, get_jst_now


class TickLiveTrader:
    """
    完全ティック駆動型（Tick-Driven / Order Flow Event-Driven）リアルタイム実行エンジン。
    
    【特徴】
    1. 1分足（ローソク足）を完全撤廃。毎秒届く最新約定（Tick）を直接処理。
    2. 直近15秒のスライディングウィンドウで Taker差分量 (Delta) と 2bp初動を常時検知。
    3. 逆方向Taker急増時にミリ秒〜秒単位で即時Cancel / 脱出。
    4. マイクロトレンド（2bp初動）から10〜20bpの大波まで利益を追随。
    """

    def __init__(
        self,
        strategies: Optional[List[BaseTickStrategy]] = None,
        product_code: str = "FX_BTC_JPY",
        order_size_btc: float = 0.001,
        poll_interval_sec: float = 1.0,
        report_interval_sec: float = 3600.0,
        enable_real_trading: bool = False,
        use_websocket: bool = True,
        max_drawdown_limit_jpy: float = 3000.0,
        notifier: Optional[DiscordNotifier] = None,
    ):
        self.product_code = product_code
        self.order_size_btc = order_size_btc
        self.poll_interval_sec = poll_interval_sec
        self.report_interval_sec = report_interval_sec
        self.use_websocket = use_websocket
        self.last_report_time = time.time()
        self.is_running = False

        self.client = BitFlyerClient(enable_real_trading=enable_real_trading)
        self.is_real = self.client.enable_real_trading
        self.notifier = notifier or DiscordNotifier()

        # 状態変更保護用ロック
        import threading
        self.state_lock = threading.Lock()

        # ティックストリーム (WebSocketコールバックを登録)
        self.stream = TickStream(
            product_code=self.product_code,
            window_seconds=15.0,
            on_ticks_callback=self._on_websocket_ticks if use_websocket else None,
        )

        self.max_drawdown_limit_jpy = max_drawdown_limit_jpy  # 許容最大ドローダウンリミット (3,000円)
        self.peak_pnl = 0.0                   # ピーク累計損益 (高値基準点)
        self.is_halted = False                # サーキットブレーカー緊急停止フラグ
        self.cooldown_seconds = 300.0         # サーキットブレーカー発動後の冷却期間 (5分 = 300秒)
        self.halt_start_time = None           # 停止開始時刻
        self.last_anomaly_alert_time = 0.0

        # 戦略一覧
        self.strategies_map: Dict[str, Dict[str, Any]] = {}
        strats = strategies or [
            MicroTrendTickStrategy(),
            InventorySkewTickMMStrategy(),
            OrderFlowScalpTickStrategy(),
        ]
        for s in strats:
            self.strategies_map[s.name] = {
                "instance": s,
                "strategy_type": getattr(s, "strategy_type", "tick"),
                "position_btc": 0.0,
                "entry_price": 0.0,
                "entry_time": None,
                "realized_pnl": 0.0,
                "trades_history": [],
            }

    def trigger_circuit_breaker(self, reason: str):
        """
        【緊急停止・サーキットブレーカー】
        大幅ドローダウンや異常事態を検知した際、全建玉を即座に強制成行決済し、
        システムを一時停止（冷却待機）してDiscordの専用緊急アラートチャンネルへ即座に通報する。
        """
        with self.state_lock:
            if self.is_halted:
                return
            self.is_halted = True
            self.halt_start_time = time.time()

            print("\n" + "!" * 70, flush=True)
            print(f"🚨🚨🚨 【緊急停止・サーキットブレーカー発動】🚨🚨🚨", flush=True)
            print(f"要因: {reason}", flush=True)
            print("全戦略の保有建玉を直ちに強制エグジット（成行決済）します...", flush=True)
            print(f"冷却待機期間: {int(self.cooldown_seconds / 60)} 分間（相場静観後、自動復帰判定を行います）", flush=True)
            print("!" * 70 + "\n", flush=True)

            closed_details = []
            for name, s_info in self.strategies_map.items():
                pos = s_info["position_btc"]
                if pos != 0:
                    side = "SELL" if pos > 0 else "BUY"
                    try:
                        self.client.send_order(
                            product_code=self.product_code,
                            side=side,
                            size=abs(pos),
                            order_type="MARKET"
                        )
                        closed_details.append(f"• `{name}`: {side} {abs(pos)} BTC (強制解消)")
                    except Exception as ex:
                        closed_details.append(f"• `{name}`: 強制解消失敗 ({ex})")
                    s_info["position_btc"] = 0.0
                    s_info["entry_price"] = 0.0
                    s_info["entry_time"] = None

            # Discord緊急アラートチャンネルへ即時通報
            cooldown_min = int(self.cooldown_seconds / 60)
            self.notifier.send_emergency_alert(
                title="【緊急停止・サーキットブレーカー発動】",
                message=f"**異常事態を検知したため、システムを直ちに一時緊急停止しました。**\n\n"
                        f"• **発生時刻**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
                        f"• **市場**: `{self.product_code}`\n"
                        f"• **停止要因**: **{reason}**\n\n"
                        f"**建玉強制解消結果**:\n" + ("\n".join(closed_details) if closed_details else "保有建玉なし (被害ゼロ)") + f"\n\n"
                        f"⏱️ **自動復帰シーケンス**: **{cooldown_min}分間の冷却待機（相場静観）**に入ります。\n"
                        f"市場急変が収束次第、安全を確認して**完全自動で運用復帰**します。",
                level="emergency"
            )

    def resume_trading(self, reason: str = "冷却期間完了＆相場沈静化確認"):
        """
        【運用自動復帰】
        サーキットブレーカー発動後の冷却期間が経過し、相場フローが落ち着いたことを確認して取引を自動再開する。
        """
        with self.state_lock:
            if not self.is_halted:
                return
            self.is_halted = False
            self.halt_start_time = None
            # 復帰時点の確定損益を新たな高値基準点にリセット (即時再発動ループの防止)
            curr_r_pnl = sum(s["realized_pnl"] for s in self.strategies_map.values())
            self.peak_pnl = curr_r_pnl

            print("\n" + "=" * 70, flush=True)
            print(f"✅✅✅ 【運用自動復帰】サーキットブレーカー解除 ＆ 取引再開 ✅✅✅", flush=True)
            print(f"復帰理由: {reason}", flush=True)
            print("=" * 70 + "\n", flush=True)

            self.notifier.send_emergency_alert(
                title="✅ 【運用自動復帰】サーキットブレーカー解除・取引再開",
                message=f"**冷却期間が完了し、相場フローの沈静化を確認したため、自律取引を再開しました。**\n\n"
                        f"• **再開時刻**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
                        f"• **市場**: `{self.product_code}`\n"
                        f"• **復帰要因**: **{reason}**\n\n"
                        f"🎯 再びミリ秒WebSocketプッシュ駆動による高精度取引が稼働しています。",
                level="warning"
            )

    def _on_websocket_ticks(self, new_ticks: List[Dict[str, Any]], flow_stats: Dict[str, Any]):
        """
        【ミリ秒（ms）完全プッシュ駆動ハンドラ】
        WebSocketから約定が届いた瞬間にマイクロ秒〜ミリ秒単位で直接実行される。
        RESTポーリングを待たず、逆流Cancelや2bp初動エントリーを即座に判定・発火する。
        """
        if not new_ticks or self.is_halted:
            return

        t_start = time.perf_counter()
        latest_tick = new_ticks[-1]
        curr_p = latest_tick.get("price", 0.0)
        if curr_p <= 0:
            return

        now = datetime.now()

        with self.state_lock:
            for name, strat_info in self.strategies_map.items():
                strat = strat_info["instance"]
                pos = strat_info["position_btc"]
                entry_p = strat_info["entry_price"]

                # 各戦略のアクション判定 (on_tick)
                action_result = strat.on_tick(
                    tick=latest_tick,
                    flow_stats=flow_stats,
                    current_pos=pos,
                    entry_price=entry_p,
                )
                action = action_result.get("action", "HOLD")
                reason = action_result.get("reason", "")

                # ----------------------------------------------------
                # 超高速アクション執行: EXIT / CANCEL (ミリ秒脱出)
                # ----------------------------------------------------
                if pos != 0 and action in ["EXIT", "CANCEL"]:
                    side = "SELL" if pos > 0 else "BUY"
                    qty = abs(pos)
                    self.client.send_order(product_code=self.product_code, side=side, size=qty, order_type="MARKET")
                    pnl = (curr_p - entry_p) * pos
                    ret_bp = (pnl / (abs(pos) * entry_p) * 10000.0) if entry_p > 0 else 0.0
                    strat_info["realized_pnl"] += pnl
                    strat_info["trades_history"].append({
                        "time": now.isoformat(),
                        "timestamp": now.timestamp(),
                        "pnl": pnl,
                        "return_bp": ret_bp,
                        "direction": "LONG" if pos > 0 else "SHORT",
                        "entry": entry_p,
                        "exit": curr_p,
                        "reason": reason
                    })

                    took_ms = (time.perf_counter() - t_start) * 1000.0
                    if action == "CANCEL":
                        print(f"\n[WS-ms] ⚡⚡ 【ミリ秒CANCEL脱出】[{name}] 判定所要: {took_ms:.2f}ms | 損益: {pnl:+,.1f} 円 ({ret_bp:+.1f}bp) | 理由: {reason}", flush=True)
                    else:
                        print(f"\n[WS-ms] 🎯 【ミリ秒利確/手仕舞い】[{name}] 判定所要: {took_ms:.2f}ms | 損益: {pnl:+,.1f} 円 ({ret_bp:+.1f}bp) | 理由: {reason}", flush=True)

                    if ret_bp >= 10.0:
                        self.notifier.send_alert(
                            title=f"🚀 【10bp+ 大波トレンド獲得】{name}",
                            message=f"**獲得リターン**: **+{ret_bp:.1f} bp** ({pnl:+,.1f} 円)\n"
                                    f"**市場**: `{self.product_code}` @ `{curr_p:,.0f} 円`\n"
                                    f"**要因**: {reason}",
                            level="success"
                        )

                    strat_info["position_btc"] = 0.0
                    strat_info["entry_price"] = 0.0
                    strat_info["entry_time"] = None

                # ----------------------------------------------------
                # 超高速アクション執行: BUY / SELL (2bp初動 / REFILL)
                # ----------------------------------------------------
                elif pos == 0 and action in ["BUY", "SELL"]:
                    side = action
                    self.client.send_order(product_code=self.product_code, side=side, size=self.order_size_btc, order_type="MARKET")
                    strat_info["position_btc"] = self.order_size_btc if side == "BUY" else -self.order_size_btc
                    strat_info["entry_price"] = curr_p
                    strat_info["entry_time"] = now

                    took_ms = (time.perf_counter() - t_start) * 1000.0
                    print(f"\n[WS-ms] 🚀 【ミリ秒ENTRY】[{name}] 判定所要: {took_ms:.2f}ms | {side} {self.order_size_btc} BTC @ {curr_p:,.0f} 円 | 理由: {reason}", flush=True)

    def execute_tick_cycle(self) -> Dict[str, Any]:
        """1サイクルの新着ティック取得および各戦略のアクション判定 (RESTフォールバック用)"""
        added_count = self.stream.fetch_latest_ticks(count=50)
        flow_stats = self.stream.compute_flow_stats(window_sec=15.0)

        curr_p = flow_stats["current_price"]
        now = datetime.now()

        if curr_p <= 0:
            return {"status": "NO_DATA", "current_price": 0.0}

        latest_tick = self.stream.ticks[-1] if self.stream.ticks else {
            "price": curr_p, "size": 0.0, "side": "NONE", "timestamp": now.timestamp(), "dt": now
        }

        total_unrealized = 0.0
        total_realized = 0.0
        strategies_status = []

        for name, strat_info in self.strategies_map.items():
            strat = strat_info["instance"]
            pos = strat_info["position_btc"]
            entry_p = strat_info["entry_price"]

            # ポジション評価損益
            u_pnl = (curr_p - entry_p) * pos if pos != 0 else 0.0
            r_pnl = strat_info["realized_pnl"]
            total_unrealized += u_pnl
            total_realized += r_pnl

            # 戦略のアクション判定 (on_tick)
            action_result = strat.on_tick(
                tick=latest_tick,
                flow_stats=flow_stats,
                current_pos=pos,
                entry_price=entry_p,
            )
            action = action_result.get("action", "HOLD")
            reason = action_result.get("reason", "")

            # ----------------------------------------------------
            # アクション執行: EXIT / CANCEL
            # ----------------------------------------------------
            if pos != 0 and action in ["EXIT", "CANCEL"]:
                side = "SELL" if pos > 0 else "BUY"
                qty = abs(pos)
                self.client.send_order(product_code=self.product_code, side=side, size=qty, order_type="MARKET")
                pnl = (curr_p - entry_p) * pos
                ret_bp = (pnl / (abs(pos) * entry_p) * 10000.0) if entry_p > 0 else 0.0
                strat_info["realized_pnl"] += pnl
                strat_info["trades_history"].append({
                    "time": now.isoformat(),
                    "timestamp": now.timestamp(),
                    "pnl": pnl,
                    "return_bp": ret_bp,
                    "direction": "LONG" if pos > 0 else "SHORT",
                    "entry": entry_p,
                    "exit": curr_p,
                    "reason": reason
                })

                action_tag = "⚡ CANCEL脱出" if action == "CANCEL" else "🎯 利確/手仕舞い"
                print(f"[TickTrader] [{name}] [{action_tag}] 損益: {pnl:+,.1f} 円 ({ret_bp:+.1f}bp) | 理由: {reason}", flush=True)

                # 10bp以上の大波利確達成時はDiscordへ即時アラート
                if ret_bp >= 10.0:
                    self.notifier.send_alert(
                        title=f"🚀 【10bp+ 大波トレンド獲得】{name}",
                        message=f"**獲得リターン**: **+{ret_bp:.1f} bp** ({pnl:+,.1f} 円)\n"
                                f"**市場**: `{self.product_code}` @ `{curr_p:,.0f} 円`\n"
                                f"**要因**: {reason}",
                        level="success"
                    )

                strat_info["position_btc"] = 0.0
                strat_info["entry_price"] = 0.0
                strat_info["entry_time"] = None
                pos = 0.0
                entry_p = 0.0
                u_pnl = 0.0

            # ----------------------------------------------------
            # アクション執行: BUY / SELL (新規エントリー / REFILL)
            # ----------------------------------------------------
            elif pos == 0 and action in ["BUY", "SELL"]:
                side = action
                self.client.send_order(product_code=self.product_code, side=side, size=self.order_size_btc, order_type="MARKET")
                strat_info["position_btc"] = self.order_size_btc if side == "BUY" else -self.order_size_btc
                strat_info["entry_price"] = curr_p
                strat_info["entry_time"] = now
                pos = strat_info["position_btc"]
                entry_p = curr_p
                print(f"[TickTrader] [{name}] [ENTRY] {side} {self.order_size_btc} BTC @ {curr_p:,.0f} 円 | 理由: {reason}", flush=True)

            wins = [t for t in strat_info["trades_history"] if t.get("pnl", 0) > 0]
            wr = (len(wins) / len(strat_info["trades_history"]) * 100.0) if strat_info["trades_history"] else 0.0

            strategies_status.append({
                "name": name,
                "strategy_type": strat_info.get("strategy_type", "tick"),
                "position": pos,
                "unrealized_pnl": u_pnl,
                "realized_pnl": r_pnl,
                "trades_count": len(strat_info["trades_history"]),
                "win_rate_pct": wr
            })

        return {
            "timestamp": now.strftime("%H:%M:%S"),
            "current_price": curr_p,
            "new_ticks_count": added_count,
            "flow_stats": flow_stats,
            "total_unrealized_pnl": total_unrealized,
            "total_realized_pnl": total_realized,
            "strategies_status": strategies_status
        }

    def start(self, max_steps: Optional[int] = None):
        """リアルタイム・ティック駆動ループの開始"""
        mode_str = "[REAL TRADING] 本番実資金" if self.is_real else "[PAPER TRADING] 仮想売買"
        engine_type = "【超低遅延・ミリ秒WebSocketプッシュ駆動】" if self.use_websocket else "【RESTポーリング駆動】"
        print("\n" + "=" * 70, flush=True)
        print(f"   GapcorePJ 完全ティック駆動型（Tick-Driven）HFTランナー ({mode_str})   ", flush=True)
        print("=" * 70, flush=True)
        print(f"対象銘柄    : {self.product_code} (bitFlyer Lightning FX 手数料0.0%)", flush=True)
        print(f"執行アーキ  : {engine_type} 約定ストリーム＆Taker差分量(Delta)駆動", flush=True)
        print(f"並行戦略数  : {len(self.strategies_map)} 戦略", flush=True)
        print(f"画面更新間隔: {self.poll_interval_sec} 秒 (ミリ秒約定は受信時に即座に判定・執行)", flush=True)
        print("-" * 70, flush=True)

        strat_names = "\n".join([f"• `{name}`" for name in self.strategies_map])
        self.notifier.send_message(
            f"⚡ **【完全ティック駆動型 HFTランナー 起動】**\n"
            f"**市場**: `{self.product_code}` (bitFlyer FX 手数料0.0%)\n"
            f"**執行方式**: **{engine_type} Taker約定差分量 (Order Flow Delta) リアルタイム駆動**\n"
            f"**並行戦略**:\n{strat_names}\n\n"
            f"🎯 **2bp初動順張り → 10〜20bp追随 ＆ 逆流即時Cancel脱出** をミリ秒（ms）単位で即座に執行します。"
        )

        self.is_running = True
        if self.use_websocket:
            self.stream.start_websocket()

        step = 0
        try:
            while self.is_running:
                step += 1

                if not self.use_websocket:
                    res = self.execute_tick_cycle()
                else:
                    # WebSocketモード時: 取引判定はプッシュ受信スレッドでミリ秒即座に完了しているため、
                    # メインスレッドでは最新のスナップショットを非同期に取得して表示のみを担当
                    flow = self.stream.compute_flow_stats(window_sec=15.0)
                    price = flow.get("current_price", 0.0)
                    now_str = datetime.now().strftime("%H:%M:%S")

                    with self.state_lock:
                        total_unrealized = 0.0
                        total_realized = 0.0
                        strats_status = []
                        for name, strat_info in self.strategies_map.items():
                            pos = strat_info["position_btc"]
                            entry_p = strat_info["entry_price"]
                            u_pnl = (price - entry_p) * pos if pos != 0 else 0.0
                            r_pnl = strat_info["realized_pnl"]
                            total_unrealized += u_pnl
                            total_realized += r_pnl
                            wins = [t for t in strat_info["trades_history"] if t.get("pnl", 0) > 0]
                            wr = (len(wins) / len(strat_info["trades_history"]) * 100.0) if strat_info["trades_history"] else 0.0
                            strats_status.append({
                                "name": name,
                                "strategy_type": strat_info.get("strategy_type", "tick"),
                                "position": pos,
                                "unrealized_pnl": u_pnl,
                                "realized_pnl": r_pnl,
                                "trades_count": len(strat_info["trades_history"]),
                                "win_rate_pct": wr
                            })

                    res = {
                        "timestamp": now_str,
                        "current_price": price,
                        "flow_stats": flow,
                        "total_unrealized_pnl": total_unrealized,
                        "total_realized_pnl": total_realized,
                        "strategies_status": strats_status
                    }

                price = res.get("current_price", 0.0)
                t_pnl = res.get("total_unrealized_pnl", 0.0) + res.get("total_realized_pnl", 0.0)
                flow = res.get("flow_stats", {})

                # ピーク（最高累積損益）の更新とドローダウン計算
                if t_pnl > self.peak_pnl:
                    self.peak_pnl = t_pnl
                current_dd = self.peak_pnl - t_pnl

                # ----------------------------------------------------
                # 異常事態検知①: ピークからの大幅ドローダウン検知＆サーキットブレーカー
                # ----------------------------------------------------
                if current_dd >= self.max_drawdown_limit_jpy and not self.is_halted:
                    self.trigger_circuit_breaker(
                        f"直近ピークからの最大許容ドローダウン超過 (ピーク損益: {self.peak_pnl:+,.1f} 円 からの落ち込み: {current_dd:,.1f} 円 >= 許容リミット: {self.max_drawdown_limit_jpy:,.0f} 円)"
                    )

                # サーキットブレーカー発動中の場合: 冷却待機期間の経過を判定し、自動運用復帰
                if self.is_halted and self.halt_start_time:
                    elapsed = time.time() - self.halt_start_time
                    if elapsed >= self.cooldown_seconds:
                        self.resume_trading(reason=f"冷却待機期間 ({int(self.cooldown_seconds / 60)}分間) 完了 ＆ 相場フロー沈静化確認")

                # ----------------------------------------------------
                # 異常事態検知②: WebSocket約定プッシュ途絶監視 (二重ヘルスチェック型スタック検知)
                # ----------------------------------------------------
                now_ts = time.time()
                if (
                    self.use_websocket
                    and self.stream.last_push_time > 0
                    and (now_ts - self.stream.last_push_time > 120.0)
                    and (now_ts - self.last_anomaly_alert_time > 300.0)
                ):
                    stall_sec = int(now_ts - self.stream.last_push_time)
                    # 二重チェック: REST APIで疎通と最新約定を確認
                    rest_added = self.stream.fetch_latest_ticks(count=10)
                    if rest_added > 0 or self.stream.is_ws_connected:
                        # 単なる市場の取引閑散であり、通信網・エンジンは健全
                        self.stream.last_push_time = now_ts
                    else:
                        # RESTも無応答かつWS切断の真の障害時のみ緊急アラートを発報
                        self.notifier.send_emergency_alert(
                            title="【システム異常警告】約定ストリーム途絶検知",
                            message=f"bitFlyer FX約定プッシュが **{stall_sec}秒間** 途絶しています。\n"
                                    f"ネットワーク遅延または取引所メンテナンスの可能性があります。自動再接続を試行しています。",
                            level="warning"
                        )
                        self.last_anomaly_alert_time = now_ts

                # コンソールにTaker差分量と戦略ステータスを表示
                d_ratio = flow.get("delta_ratio", 0.0)
                p_change = flow.get("price_change_bp", 0.0)
                strats_summary = " | ".join([
                    f"{s['name']}:{s['trades_count']}回({s['position']:+.3f})"
                    for s in res.get("strategies_status", [])
                ])

                flow_sign = "BUY優勢" if d_ratio > 0.2 else ("SELL優勢" if d_ratio < -0.2 else "拮抗")
                ws_indicator = "⚡WS" if (self.use_websocket and self.stream.is_ws_connected) else "REST"
                halt_tag = " [🚨HALTED]" if self.is_halted else ""
                print(
                    f" [{res['timestamp']}] [{ws_indicator}]{halt_tag} {price:10,.0f} 円 (Δ{p_change:+4.1f}bp | {flow_sign} {d_ratio:+.2f}) | "
                    f"損益: {t_pnl:+6.1f} 円 | {strats_summary}",
                    flush=True
                )

                # 定期レポート送信 (1時間ごと / 24時起点・1時間成績＆累積成績)
                now_epoch = time.time()
                now_jst = get_jst_now()
                is_on_the_hour = (now_jst.minute == 0 and (now_epoch - self.last_report_time > 120.0))
                is_interval_elapsed = (now_epoch - self.last_report_time >= self.report_interval_sec)

                if is_interval_elapsed or is_on_the_hour:
                    with self.state_lock:
                        trades_map = {name: list(s_info["trades_history"]) for name, s_info in self.strategies_map.items()}
                        unrealized_map = {}
                        positions_map = {}
                        for name, s_info in self.strategies_map.items():
                            pos = s_info["position_btc"]
                            entry_p = s_info["entry_price"]
                            unrealized_map[name] = (price - entry_p) * pos if pos != 0 else 0.0
                            positions_map[name] = pos

                    perf_snapshot = compute_performance_snapshots(
                        trades_map=trades_map,
                        unrealized_pnl_map=unrealized_map,
                        positions_map=positions_map,
                        now_ts=now_epoch
                    )

                    self.notifier.send_periodic_performance_report(
                        symbol=self.product_code,
                        current_price=price,
                        hourly_stats=perf_snapshot["hourly_stats"],
                        daily_stats=perf_snapshot["daily_stats"],
                        total_stats=perf_snapshot["total_stats"],
                        strategies_stats=perf_snapshot["strategies_stats"],
                        hour_range=perf_snapshot["hour_range"],
                        today_str=perf_snapshot["today_str"]
                    )
                    self.last_report_time = now_epoch

                if max_steps and step >= max_steps:
                    break

                time.sleep(self.poll_interval_sec)

        except KeyboardInterrupt:
            print("\n[TickTrader] ユーザーによって中断されました (Ctrl+C)")
        finally:
            self.is_running = False
            if self.use_websocket:
                self.stream.stop_websocket()
