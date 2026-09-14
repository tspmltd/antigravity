import os
import sys
import time
import json
import glob
from datetime import datetime
from typing import Dict, Any, Optional, List
import pandas as pd

from core.bitflyer_client import BitFlyerClient
from core.dataloader import DataLoader
from core.notifier import DiscordNotifier
from core.order_flow import OrderFlowAnalyzer
from execution.adaptive_manager import AdaptiveManager
from core.performance_aggregator import compute_performance_snapshots, get_jst_now


class MultiStrategyLiveTrader:
    """
    複数戦略同時並行ペーパートレード＆ポートフォリオ監視ランナー。
    複数の異なる戦略（MM、トレンドフォロー、平均回帰等）を同時に並行運用し、
    各戦略の損益・ポジションおよびポートフォリオ合算損益を1時間ごとにDiscordへ一括通知します。
    """

    TICKER_URL = "https://api.bitflyer.com/v1/ticker?product_code="

    def __init__(
        self,
        strategy_paths: List[str],
        product_code: str = "FX_BTC_JPY",
        timeframe: str = "1m",
        order_size_btc: float = 0.001,
        initial_capital_per_strat: float = 100000.0,
        enable_real_trading: bool = False,
        poll_interval_sec: float = 2.0,
        report_interval_sec: float = 3600.0,
    ):
        self.product_code = product_code
        self.timeframe = timeframe
        self.order_size_btc = order_size_btc
        self.poll_interval_sec = poll_interval_sec
        self.report_interval_sec = report_interval_sec
        self.last_report_time = time.time()

        self.client = BitFlyerClient(enable_real_trading=enable_real_trading)
        self.is_real = self.client.enable_real_trading
        self.notifier = DiscordNotifier()
        self.order_flow = OrderFlowAnalyzer(product_code=self.product_code)
        self.last_order_flow_fetch = 0.0

        self.initial_capital_per_strat = initial_capital_per_strat
        self.last_discovery_check_time = 0.0

        # 各戦略のインスタンスと口座状態
        self.strategies: Dict[str, Dict[str, Any]] = {}
        for path in strategy_paths:
            strat_inst = self._load_strategy(path)
            strat_name = getattr(strat_inst, "name", os.path.basename(path))
            strat_type = self._detect_strategy_type(strat_inst, strat_name)
            self.strategies[strat_name] = {
                "instance": strat_inst,
                "strategy_type": strat_type,
                "file_path": path,
                "position_btc": 0.0,
                "entry_price": 0.0,
                "entry_time": None,
                "realized_pnl": 0.0,
                "trades_history": [],
                "initial_capital": initial_capital_per_strat,
                "current_cash": initial_capital_per_strat,
                "adaptive_manager": AdaptiveManager()
            }

        # 初期ヒストリカルデータの取得
        print(f"[MultiTrader] 並行運用対象: {len(self.strategies)} 戦略をロードしました")
        for s_name, s_info in self.strategies.items():
            type_label = {
                "market_making": "MM戦略(高回転)",
                "micro_trend": "マイクロトレンド(2bp初動→20bp)",
                "trend_following": "トレンドフォロー(長期波乗り)",
                "mean_reversion": "平均回帰(中頻度)"
            }.get(s_info["strategy_type"], "一般")
            print(f"  - 戦略: {s_name} [{type_label}]")
        
        self.df_history = DataLoader.load_or_generate_data(
            source="bitflyer",
            symbol=self.product_code,
            timeframe=self.timeframe,
            limit=500
        )
        # timestamp を datetime 型にしておく
        self.df_history["timestamp"] = pd.to_datetime(self.df_history["timestamp"])
        print(f"[MultiTrader] 初期データ準備完了: {len(self.df_history)} 本 ({self.product_code} {self.timeframe})")

    def _detect_strategy_type(self, strat_inst, strat_name: str) -> str:
        """戦略インスタンスや戦略名・仮説からタイプを自動識別 (MM vs トレンド vs 平均回帰)"""
        if hasattr(strat_inst, "strategy_type") and strat_inst.strategy_type:
            return strat_inst.strategy_type
        
        text = (
            str(strat_name) + " " +
            str(getattr(strat_inst, "hypothesis", "")) + " " +
            str(strat_inst.__class__.__name__)
        ).lower()

        if any(k in text for k in ["microtrend", "orderflow", "order_flow"]):
            return "micro_trend"
        elif any(k in text for k in ["mm", "spread", "grid", "market making", "skew"]):
            return "market_making"
        elif any(k in text for k in ["trend", "ema", "donchian", "macd", "momentum"]):
            return "trend_following"
        elif any(k in text for k in ["reversion", "rsi", "mean", "bb"]):
            return "mean_reversion"
        return "trend_following"

    def check_for_new_approved_strategies(self):
        """strategies/approved/ ディレクトリをスキャンし、未登録の新規合格戦略を無停止で自動追加"""
        import glob
        approved_files = glob.glob("strategies/approved/*.py")
        loaded_files = {s_info["file_path"] for s_info in self.strategies.values()}

        for path in approved_files:
            if path not in loaded_files:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        if "class CustomStrategy" not in f.read():
                            continue
                    strat_inst = self._load_strategy(path)
                    s_name = getattr(strat_inst, "name", os.path.basename(path))
                    if s_name in self.strategies:
                        s_name = f"{s_name}_{int(time.time())}"

                    s_type = self._detect_strategy_type(strat_inst, s_name)
                    self.strategies[s_name] = {
                        "instance": strat_inst,
                        "strategy_type": s_type,
                        "file_path": path,
                        "position_btc": 0.0,
                        "entry_price": 0.0,
                        "entry_time": None,
                        "realized_pnl": 0.0,
                        "trades_history": [],
                        "initial_capital": self.initial_capital_per_strat,
                        "current_cash": self.initial_capital_per_strat,
                        "adaptive_manager": AdaptiveManager()
                    }
                    loaded_files.add(path)

                    print("\n" + "=" * 65, flush=True)
                    print(f"[MultiTrader] [AUTO-ADD] 🌟 【新規戦略自動参入】新合格戦略をポートフォリオに追加しました！", flush=True)
                    print(f"             戦略名: {s_name} | ファイル: {path}", flush=True)
                    print(f"             現在稼働中: {len(self.strategies)} 戦略", flush=True)
                    print("=" * 65 + "\n", flush=True)

                    # Discordへ新規戦略自動追加通知を送信
                    self.notifier.send_alert(
                        title="🌟 【新規戦略自動参入】ポートフォリオが自動拡張されました！",
                        message=f"ガバナンス審査を突破した新戦略が、稼働中ポートフォリオへ**無停止で自動追加**されました！\n\n"
                                f"• **新戦略**: `{s_name}`\n"
                                f"• **承認ファイル**: `{path}`\n"
                                f"• **現在の並行運用数**: **{len(self.strategies)} 戦略**\n\n"
                                f"次回の定期レポートより、この新戦略の運用成績も一緒にDiscordへ自動配信されます。",
                        level="success",
                        target="system"
                    )
                except Exception as e:
                    print(f"[MultiTrader] ⚠️ 新規戦略ロード失敗 ({path}): {e}", flush=True)

    def _load_strategy(self, path: str):
        """Pythonファイルを動的インポートしてCustomStrategyインスタンスを生成"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("dynamic_multi_strategy", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "CustomStrategy"):
            raise ValueError(f"CustomStrategyクラスが見つかりません: {path}")
        return mod.CustomStrategy()

    def fetch_current_price(self) -> float:
        """bitFlyerから現在価格 (Ticker) を取得"""
        import urllib.request
        url = self.TICKER_URL + self.product_code
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (GapcorePJ MultiTrader)"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            return float(data.get("ltp") or data.get("best_bid", 0.0))

    def _get_tf_seconds(self) -> float:
        tf = self.timeframe.lower()
        if tf.endswith("m"):
            return float(tf.replace("m", "")) * 60.0
        elif tf.endswith("h"):
            return float(tf.replace("h", "")) * 3600.0
        elif tf.endswith("d"):
            return float(tf.replace("d", "")) * 86400.0
        return 60.0

    def _update_candles(self, current_price: float, now: datetime) -> pd.DataFrame:
        """ローソク足の新規バー自動確定および現在価格のリアルタイム反映"""
        tf_sec = self._get_tf_seconds()
        last_candle_time = pd.to_datetime(self.df_history.iloc[-1]["timestamp"])

        # 新しい足の生成判定 (タイムフレーム秒数を跨いだ場合)
        if (now - last_candle_time).total_seconds() >= tf_sec:
            # 直前バーの確定
            self.df_history.iloc[-1, self.df_history.columns.get_loc("close")] = current_price
            self.df_history.iloc[-1, self.df_history.columns.get_loc("high")] = max(
                self.df_history.iloc[-1]["high"], current_price
            )
            self.df_history.iloc[-1, self.df_history.columns.get_loc("low")] = min(
                self.df_history.iloc[-1]["low"], current_price
            )
            # 新規バーの作成と追加
            new_candle = pd.DataFrame([{
                "timestamp": now,
                "open": current_price,
                "high": current_price,
                "low": current_price,
                "close": current_price,
                "volume": 0.001
            }])
            self.df_history = pd.concat([self.df_history, new_candle], ignore_index=True)
            if len(self.df_history) > 300:
                self.df_history = self.df_history.iloc[-300:].reset_index(drop=True)
            print(f"[MultiTrader] 🕯️ 【新規ローソク足確定】新バー生成 ({now.strftime('%H:%M:%S')})", flush=True)
        else:
            # 既存バーの更新
            self.df_history.iloc[-1, self.df_history.columns.get_loc("close")] = current_price
            self.df_history.iloc[-1, self.df_history.columns.get_loc("high")] = max(
                self.df_history.iloc[-1]["high"], current_price
            )
            self.df_history.iloc[-1, self.df_history.columns.get_loc("low")] = min(
                self.df_history.iloc[-1]["low"], current_price
            )

        return self.df_history

    def execute_step(self) -> Dict[str, Any]:
        """各ステップの並行シグナル判定および戦略特性別ポジション損益更新"""
        # 定期的な新規合格戦略のスキャン＆自動追加 (30秒ごと)
        if time.time() - self.last_discovery_check_time >= 30.0:
            self.check_for_new_approved_strategies()
            self.last_discovery_check_time = time.time()

        current_price = self.fetch_current_price()
        now = datetime.now()

        # 動的ローソク足の更新とロールオーバー
        df_for_signal = self._update_candles(current_price, now)

        # --------------------------------------------------------
        # Taker約定差分量 (Order Flow Delta) の定期解析 (約定ストリーム)
        # --------------------------------------------------------
        if time.time() - self.last_order_flow_fetch >= 10.0:
            try:
                self.order_flow.fetch_recent_executions(count=60)
                self.last_order_flow_fetch = time.time()
            except Exception:
                pass
        of_res = self.order_flow.analyze(window_sec=30.0)

        strategies_status = []
        total_unrealized = 0.0
        total_realized = 0.0

        for name, strat_info in self.strategies.items():
            strat = strat_info["instance"]
            s_type = strat_info.get("strategy_type", "trend_following")
            df_signals = strat.generate_signals(df_for_signal)
            current_signal = int(df_signals["signal"].iloc[-1])

            pos = strat_info["position_btc"]
            entry_p = strat_info["entry_price"]
            entry_t = strat_info.get("entry_time")

            # ポジション損益計算
            u_pnl = (current_price - entry_p) * pos if pos != 0 else 0.0
            r_pnl = strat_info["realized_pnl"]
            total_unrealized += u_pnl
            total_realized += r_pnl

            # ----------------------------------------------------
            # 戦略タイプ別決済ロジック (MM vs マイクロトレンド vs 長期トレンド vs 平均回帰)
            # ----------------------------------------------------
            exit_reason = None
            holding_sec = (now - entry_t).total_seconds() if (pos != 0 and entry_t) else 0.0
            ret_pct = ((current_price - entry_p) / entry_p) * (1.0 if pos > 0 else -1.0) if (pos != 0 and entry_p > 0) else 0.0

            if pos != 0:
                # 1. 全戦略共通: シグナル反転・手仕舞い判定
                if current_signal == 0 or (pos > 0 and current_signal < 0) or (pos < 0 and current_signal > 0):
                    exit_reason = "シグナル変化(SIGNAL_EXIT)"

                # 2. 戦略タイプ別エグジット
                if s_type == "market_making":
                    # MM戦略 (高回転・取引回数多):
                    # ① 逆方向Taker急増による即時Cancel/脱出 (Adverse Selection防止)
                    if pos > 0 and of_res.get("should_cancel_bid", False):
                        exit_reason = f"MM即時Cancel脱出(売りTaker殺到 Delta:{of_res.get('delta_ratio')})"
                    elif pos < 0 and of_res.get("should_cancel_ask", False):
                        exit_reason = f"MM即時Cancel脱出(買いTaker殺到 Delta:{of_res.get('delta_ratio')})"
                    # ② 微小反発(+0.05% = 5bp)利確
                    elif ret_pct >= 0.0005:
                        exit_reason = f"MM利確(+{ret_pct*100:.2f}%)"
                    # ③ タイト損切り
                    elif ret_pct <= -0.0012:
                        exit_reason = f"MM損切({ret_pct*100:.2f}%)"
                    # ④ 5分タイムアウト
                    elif holding_sec >= 300.0:
                        exit_reason = f"MMタイムアウト({int(holding_sec)}s・高回転循環)"

                elif s_type == "micro_trend":
                    # マイクロトレンド戦略 (2bp初動 → 10〜20bp追随):
                    # ① 10bp〜20bpターゲット到達利確 (+0.12%〜+0.20%)
                    if ret_pct >= 0.0012:
                        exit_reason = f"マイクロトレンド大波利確(+{ret_pct*100:.2f}% / 12bp+)"
                    # ② Takerフロー反転による素早い手仕舞い (2〜4bp微小利確/微損)
                    elif (pos > 0 and of_res.get("delta_ratio", 0) < -0.35) or (pos < 0 and of_res.get("delta_ratio", 0) > 0.35):
                        exit_reason = f"Takerフロー反転撤退(Delta:{of_res.get('delta_ratio'):.2f})"
                    # ③ 防衛ストップ
                    elif ret_pct <= -0.0030:
                        exit_reason = f"マイクロトレンド防衛損切({ret_pct*100:.2f}%)"

                elif s_type == "trend_following":
                    # 長期トレンドフォロー戦略 (低頻度・じっくり保有):
                    if ret_pct <= -0.012:
                        exit_reason = f"トレンド防衛損切({ret_pct*100:.2f}%)"

                elif s_type == "mean_reversion":
                    # 平均回帰戦略 (中頻度):
                    if ret_pct >= 0.0018:
                        exit_reason = f"平均回帰利確(+{ret_pct*100:.2f}%)"
                    elif ret_pct <= -0.0035:
                        exit_reason = f"平均回帰損切({ret_pct*100:.2f}%)"
                    elif holding_sec >= 1200.0:
                        exit_reason = f"平均回帰タイムアウト({int(holding_sec)}s)"

                # 決済発注の実行
                if exit_reason:
                    side = "SELL" if pos > 0 else "BUY"
                    qty = abs(pos)
                    self.client.send_order(product_code=self.product_code, side=side, size=qty, order_type="MARKET")
                    pnl = (current_price - entry_p) * pos
                    strat_info["realized_pnl"] += pnl
                    strat_info["trades_history"].append({
                        "time": now.isoformat(),
                        "timestamp": now.timestamp(),
                        "pnl": pnl,
                        "direction": "LONG" if pos > 0 else "SHORT",
                        "entry": entry_p,
                        "exit": current_price,
                        "reason": exit_reason,
                        "holding_sec": holding_sec
                    })
                    print(f"[MultiTrader] [{name}] [EXIT] 決済理由: {exit_reason} | 数量: {qty} BTC | 損益: {pnl:+,.1f} 円 (保有: {holding_sec:.0f}秒)", flush=True)
                    strat_info["position_btc"] = 0.0
                    strat_info["entry_price"] = 0.0
                    strat_info["entry_time"] = None
                    pos = 0.0
                    entry_p = 0.0
                    u_pnl = 0.0

            # ----------------------------------------------------
            # 新規エントリー判定
            # ----------------------------------------------------
            if pos == 0 and current_signal != 0:
                side = "BUY" if current_signal > 0 else "SELL"
                self.client.send_order(product_code=self.product_code, side=side, size=self.order_size_btc, order_type="MARKET")
                strat_info["position_btc"] = self.order_size_btc if side == "BUY" else -self.order_size_btc
                strat_info["entry_price"] = current_price
                strat_info["entry_time"] = now
                pos = strat_info["position_btc"]
                entry_p = current_price
                type_tag = (
                    "MM-REFILL" if s_type == "market_making"
                    else ("uTRD-ENTRY" if s_type == "micro_trend"
                    else ("TREND" if s_type == "trend_following" else "REV"))
                )
                print(f"[MultiTrader] [{name}] [{type_tag}] 新規エントリー: {side} {self.order_size_btc} BTC @ {current_price:,.0f} 円", flush=True)

            sig_label = "買い(1)" if current_signal == 1 else ("売り(-1)" if current_signal == -1 else "中立(0)")
            wins = [t for t in strat_info["trades_history"] if t.get("pnl", 0) > 0]
            wr = (len(wins) / len(strat_info["trades_history"]) * 100.0) if strat_info["trades_history"] else 0.0

            strategies_status.append({
                "name": name,
                "strategy_type": s_type,
                "position": pos,
                "unrealized_pnl": u_pnl,
                "realized_pnl": r_pnl,
                "signal_name": sig_label,
                "trades_count": len(strat_info["trades_history"]),
                "win_rate_pct": wr
            })

        return {
            "timestamp": now.strftime("%H:%M:%S"),
            "current_price": current_price,
            "total_unrealized_pnl": total_unrealized,
            "total_realized_pnl": total_realized,
            "strategies_status": strategies_status
        }

    def start(self, max_steps: Optional[int] = None):
        """複数戦略の並行リアルタイム監視ループの起動"""
        mode_str = "[REAL TRADING] 本番実資金" if self.is_real else "[PAPER TRADING] 仮想売買"
        print("\n" + "=" * 70, flush=True)
        print(f"    GapcorePJ 複数戦略同時並行ポートフォリオ・ランナー ({mode_str})    ", flush=True)
        print("=" * 70, flush=True)
        print(f"対象銘柄    : {self.product_code} (bitFlyer Lightning FX)", flush=True)
        print(f"並行戦略数  : {len(self.strategies)} 戦略", flush=True)
        print(f"監視間隔    : {self.poll_interval_sec} 秒", flush=True)
        print(f"Discord通知 : {self.report_interval_sec / 3600:.1f} 時間ごとに全戦略の損益・建玉を一括配信", flush=True)
        print("-" * 70, flush=True)

        # 起動通知をDiscordへ送信
        strat_details = []
        for name, s_info in self.strategies.items():
            type_label = {
                "market_making": "MM(高回転スプレッド)",
                "micro_trend": "マイクロトレンド(2bp初動→20bp追随)",
                "trend_following": "トレンド(長期波乗り)",
                "mean_reversion": "平均回帰(中頻度)"
            }.get(s_info["strategy_type"], "一般")
            strat_details.append(f"• `{name}` 【{type_label}】")
        strat_names_str = "\n".join(strat_details)

        self.notifier.send_message(
            f"🚀 **【マルチ戦略ポートフォリオ 監視スタート (Order Flow & 戦略特性別 適応モード)】**\n"
            f"対象市場: `{self.product_code}` (bitFlyer FX 手数料0.0%)\n"
            f"足種: `{self.timeframe}` | 監視間隔: `{self.poll_interval_sec}秒`\n"
            f"並行稼働戦略数: **{len(self.strategies)} 戦略**\n{strat_names_str}\n\n"
            f"⚡ **戦略特性別の管理ルール**:\n"
            f"• **MM戦略**: 逆方向Taker急増時の即時Cancel脱出 ＋ 微小利確(+0.05%) ＋ 5分タイムアウト\n"
            f"• **マイクロトレンド戦略**: Taker差分量(Delta)継続＆逆方向僅少時の2bp初動順張り → 10〜20bp追随\n"
            f"• **長期トレンド戦略**: 損小利大を狙いじっくり長期保有\n\n"
            f"定期レポート: **{self.report_interval_sec / 3600:.1f} 時間ごと**に全戦略の損益・取引数を一括通知します。"
        )

        step = 0
        try:
            while True:
                step += 1
                res = self.execute_step()
                price = res["current_price"]
                t_pnl = res["total_unrealized_pnl"] + res["total_realized_pnl"]

                # コンソールに1行サマリー表示 (戦略タイプと取引回数を可視化)
                strat_summaries = []
                for s in res["strategies_status"]:
                    tag = (
                        "MM" if s.get("strategy_type") == "market_making"
                        else ("μTRD" if s.get("strategy_type") == "micro_trend"
                        else ("TRD" if s.get("strategy_type") == "trend_following" else "REV"))
                    )
                    strat_summaries.append(f"[{tag}]{s['name']}:{s['trades_count']}回({s['position']:+.3f})")
                
                print(f" [{res['timestamp']}] {price:10,.0f} 円 | 合計損益: {t_pnl:+7.1f} 円 | {' | '.join(strat_summaries[:3])}", flush=True)

                # 定期レポート送信 (1時間ごと / 24時起点・1時間成績＆累積成績)
                now_epoch = time.time()
                now_jst = get_jst_now()
                is_on_the_hour = (now_jst.minute == 0 and (now_epoch - self.last_report_time > 120.0))
                is_interval_elapsed = (now_epoch - self.last_report_time >= self.report_interval_sec)

                if is_interval_elapsed or is_on_the_hour:
                    trades_map = {name: list(s_info["trades_history"]) for name, s_info in self.strategies.items()}
                    unrealized_map = {}
                    positions_map = {}
                    for name, s_info in self.strategies.items():
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
                    print(f"[MultiTrader] 📊 【定期通知送信完了】24時起点・1時間成績レポートをDiscordへ配信しました。", flush=True)

                if max_steps and step >= max_steps:
                    break

                time.sleep(self.poll_interval_sec)

        except KeyboardInterrupt:
            print("\n[MultiTrader] ユーザーによって中断されました (Ctrl+C)")
