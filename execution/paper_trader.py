import os
import sys
import time
import json
import importlib.util
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
import pandas as pd
from dotenv import load_dotenv

from core.bitflyer_client import BitFlyerClient
from core.dataloader import DataLoader
from execution.adaptive_manager import AdaptiveManager

load_dotenv()


class LivePaperTrader:
    """
    リアルタイム相場監視 & ペーパートレード (仮想実稼働) / 本番自動売買ランナー。
    bitFlyerの現在価格を監視し、戦略のシグナル発生時に自動で注文・損益計算を行います。
    """

    TICKER_URL = "https://api.bitflyer.com/v1/ticker?product_code="

    def __init__(
        self,
        strategy_path: str,
        product_code: str = "FX_BTC_JPY",
        timeframe: str = "1m",
        order_size_btc: float = 0.001,
        initial_capital_jpy: float = 100000.0,
        enable_real_trading: bool = False,
        poll_interval_sec: float = 5.0,
    ):
        self.strategy_path = strategy_path
        self.product_code = product_code
        self.timeframe = timeframe
        self.order_size_btc = order_size_btc
        self.poll_interval_sec = poll_interval_sec

        # bitFlyerクライアント初期化
        self.client = BitFlyerClient(enable_real_trading=enable_real_trading)
        self.is_real = self.client.enable_real_trading

        # 仮想口座・ペーパートレード管理用
        self.initial_capital_jpy = initial_capital_jpy
        self.virtual_cash_jpy = initial_capital_jpy
        self.virtual_position_btc = 0.0
        self.entry_price = 0.0
        self.realized_pnl_jpy = 0.0
        self.trades_history: List[Dict[str, Any]] = []

        # 自律適応マネージャー
        self.adaptive_manager = AdaptiveManager()

        # Discord通知クライアント
        from core.notifier import DiscordNotifier
        self.notifier = DiscordNotifier()
        self.hourly_interval_sec = 3600.0  # デフォルト: 1時間 (3600秒)
        self.last_hourly_report_time = time.time()

        # 戦略の動的ロード
        self.strategy = self._load_strategy(strategy_path)

        # 履歴データの初期化 (直近足の取得)
        print(f"[LiveTrader] 戦略を初期化中: {self.strategy.name} ({strategy_path})")
        print(f"[LiveTrader] 直近ヒストリカルデータを準備しています...")
        self.df_history = DataLoader.load_or_generate_data(
            source="bitflyer",
            symbol=self.product_code,
            timeframe=self.timeframe,
            limit=200
        )
        print(f"[LiveTrader] 初期データ準備完了: {len(self.df_history)} 本")

    def _load_strategy(self, path: str):
        """Pythonファイルを動的インポートしてCustomStrategyインスタンスを生成"""
        if not os.path.exists(path):
            raise FileNotFoundError(f"戦略ファイルが見つかりません: {path}")

        module_name = f"live_strat_{int(time.time())}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"モジュールをロードできませんでした: {path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        if not hasattr(module, "CustomStrategy"):
            raise AttributeError("ファイル内に 'CustomStrategy' クラスが見つかりません。")

        return module.CustomStrategy()

    def fetch_current_ticker(self) -> Dict[str, Any]:
        """bitFlyer Ticker APIから最新価格情報を超高速取得"""
        url = self.TICKER_URL + self.product_code
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (GapcorePJ AlgoTrader)"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        return {
            "ltp": float(data["ltp"]),         # 最終取引価格 (Last Traded Price)
            "best_bid": float(data["best_bid"]), # 最良買気配
            "best_ask": float(data["best_ask"]), # 最良売気配
            "volume": float(data["volume"]),     # 24時間出来高
            "timestamp": data["timestamp"]
        }

    def execute_step(self) -> Dict[str, Any]:
        """
        1サイクルの監視・シグナル判定・注文実行処理
        """
        # 1. 最新価格取得
        ticker = self.fetch_current_ticker()
        current_price = ticker["ltp"]
        now = datetime.now()

        # 2. 最新バーの更新 (足の確定・追加判定)
        last_time = self.df_history["timestamp"].iloc[-1]
        
        # 簡易的な1分足境界判定 (同一分なら更新、新分なら新しい行を追加)
        if hasattr(last_time, "minute") and last_time.minute == now.minute:
            # 既存の最終行をリアルタイム更新
            self.df_history.loc[self.df_history.index[-1], "close"] = current_price
            self.df_history.loc[self.df_history.index[-1], "high"] = max(
                self.df_history.loc[self.df_history.index[-1], "high"], current_price
            )
            self.df_history.loc[self.df_history.index[-1], "low"] = min(
                self.df_history.loc[self.df_history.index[-1], "low"], current_price
            )
        else:
            # 新しい足を追加
            new_row = pd.DataFrame([{
                "timestamp": now.replace(second=0, microsecond=0),
                "open": current_price,
                "high": current_price,
                "low": current_price,
                "close": current_price,
                "volume": 0.01
            }])
            self.df_history = pd.concat([self.df_history, new_row], ignore_index=True)
            if len(self.df_history) > 300:
                self.df_history = self.df_history.iloc[-300:].reset_index(drop=True)

        # 3. 戦略シグナルの算出
        df_signals = self.strategy.generate_signals(self.df_history.copy())
        current_signal = int(df_signals["signal"].iloc[-1]) if "signal" in df_signals else 0

        # 4. ポジション判定 & 発注
        trade_action = None
        unrealized_pnl = 0.0
        if self.virtual_position_btc > 0:
            unrealized_pnl = (current_price - self.entry_price) * self.virtual_position_btc
        elif self.virtual_position_btc < 0:
            unrealized_pnl = (self.entry_price - current_price) * abs(self.virtual_position_btc)

        # 買いシグナル (保有なしから買い、またはドテン買い)
        if current_signal == 1 and self.virtual_position_btc <= 0:
            # 既存ショートの決済
            if self.virtual_position_btc < 0:
                self._close_position(current_price, "CLOSE_SHORT")
            # 新規ロング
            self._open_position(current_price, "BUY", self.order_size_btc)
            trade_action = "OPEN_LONG"

        # 売りシグナル (保有なしから売り、またはドテン売り)
        elif current_signal == -1 and self.virtual_position_btc >= 0:
            # 既存ロングの決済
            if self.virtual_position_btc > 0:
                self._close_position(current_price, "CLOSE_LONG")
            # 新規ショート
            self._open_position(current_price, "SELL", self.order_size_btc)
            trade_action = "OPEN_SHORT"

        # 中立・手仕舞いシグナル
        elif current_signal == 0 and self.virtual_position_btc != 0:
            action_type = "CLOSE_LONG" if self.virtual_position_btc > 0 else "CLOSE_SHORT"
            self._close_position(current_price, action_type)
            trade_action = "EXIT"

        total_equity = self.virtual_cash_jpy + unrealized_pnl

        return {
            "timestamp": now.strftime("%H:%M:%S"),
            "current_price": current_price,
            "signal": current_signal,
            "position": self.virtual_position_btc,
            "entry_price": self.entry_price,
            "unrealized_pnl": round(unrealized_pnl, 1),
            "realized_pnl": round(self.realized_pnl_jpy, 1),
            "total_equity": round(total_equity, 1),
            "trade_action": trade_action,
        }

    def _open_position(self, price: float, side: str, size: float):
        """新規ポジション構築"""
        order_res = self.client.send_order(
            product_code=self.product_code,
            side=side,
            size=size,
            order_type="MARKET"
        )
        self.entry_price = price
        self.virtual_position_btc = size if side == "BUY" else -size
        print(f"[LiveTrader] [ENTRY] 新規エントリー: {side} {size} BTC @ {price:,.0f} 円", flush=True)

    def _close_position(self, price: float, reason: str):
        """ポジション決済"""
        side = "SELL" if self.virtual_position_btc > 0 else "BUY"
        qty = abs(self.virtual_position_btc)

        order_res = self.client.send_order(
            product_code=self.product_code,
            side=side,
            size=qty,
            order_type="MARKET"
        )

        pnl = (price - self.entry_price) * self.virtual_position_btc
        self.realized_pnl_jpy += pnl
        
        trade_record = {
            "timestamp": datetime.now().isoformat(),
            "reason": reason,
            "direction": "LONG" if self.virtual_position_btc > 0 else "SHORT",
            "entry_price": self.entry_price,
            "exit_price": price,
            "size": qty,
            "pnl": round(pnl, 1),
        }
        self.trades_history.append(trade_record)
        self._save_trade_history()

        print(f"[LiveTrader] [EXIT] ポジション決済 ({reason}): {qty} BTC @ {price:,.0f} 円 | 損益: {pnl:+,.1f} 円", flush=True)
        self.virtual_position_btc = 0.0
        self.entry_price = 0.0

        # 自律適応マネージャーによるリアルタイム成績評価 ＆ 自動改善チェック
        if self.adaptive_manager.should_evaluate(self.trades_history):
            current_info = {
                "strategy_id": getattr(self.strategy, "name", "live_strategy"),
                "name": self.strategy.name,
                "file_path": self.strategy_path,
                "hypothesis": getattr(self.strategy, "hypothesis", ""),
                "iteration": getattr(self.strategy, "iteration", 1)
            }
            need_switch, new_path, msg = self.adaptive_manager.evaluate_and_adapt(
                trades_history=self.trades_history,
                initial_capital=self.initial_capital_jpy,
                current_cash=self.virtual_cash_jpy,
                current_strategy_info=current_info,
                symbol=self.product_code,
                timeframe=self.timeframe
            )
            if need_switch and new_path:
                self.reload_strategy(new_path)

    def reload_strategy(self, new_strategy_path: str):
        """稼働中に新しい戦略コードを無停止で動的ホットリロード（差し替え）"""
        try:
            new_strat = self._load_strategy(new_strategy_path)
            old_name = self.strategy.name
            self.strategy = new_strat
            self.strategy_path = new_strategy_path
            print("\n" + "=" * 60, flush=True)
            print(f"[LiveTrader] [HOT-RELOAD] 【戦略ホットリロード完了】 無停止で差し替えました:", flush=True)
            print(f"             旧戦略: {old_name} -> 新戦略: {new_strat.name}", flush=True)
            print(f"             新ファイル: {new_strategy_path}", flush=True)
            print("=" * 60 + "\n", flush=True)

            # Discordアラート通知
            self.notifier.send_alert(
                title="戦略ホットリロード完了",
                message=f"旧戦略: `{old_name}` ➡️ 新戦略: `{new_strat.name}`\nファイル: `{new_strategy_path}`",
                level="success"
            )
        except Exception as e:
            print(f"[LiveTrader] ⚠️ ホットリロード失敗 (現行戦略を維持します): {e}", flush=True)
            self.notifier.send_alert(
                title="戦略ホットリロード失敗",
                message=f"エラー内容: {e}",
                level="critical"
            )

    def _save_trade_history(self):
        """トレード履歴をJSONに保存"""
        os.makedirs("reports", exist_ok=True)
        log_path = "reports/live_paper_trades.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(self.trades_history, f, indent=2, ensure_ascii=False)

    def start(self, max_steps: Optional[int] = None):
        """リアルタイム監視ループの起動"""
        mode_str = "[REAL TRADING] 本番実資金" if self.is_real else "[PAPER TRADING] 仮想売買"
        print("\n" + "=" * 65, flush=True)
        print(f"       GapcorePJ リアルタイム自動売買ランナー ({mode_str})       ", flush=True)
        print("=" * 65, flush=True)
        print(f"対象銘柄    : {self.product_code}", flush=True)
        print(f"戦略        : {self.strategy.name}", flush=True)
        print(f"発注ロット  : {self.order_size_btc} BTC", flush=True)
        print(f"監視間隔    : {self.poll_interval_sec} 秒", flush=True)
        print(f"仮想初期資金: {self.virtual_cash_jpy:,.0f} 円", flush=True)
        print(f"通知間隔    : {self.hourly_interval_sec / 3600:.1f} 時間ごと", flush=True)
        print("-" * 65, flush=True)
        print(" [時刻]    | 現在価格 (円) | シグナル | 建玉 (BTC) | 含み損益 (円) | 累計実現損益", flush=True)
        print("-" * 65, flush=True)

        # 起動通知
        self.notifier.send_message(
            f"🚀 **【自動売買監視スタート】**\n対象銘柄: `{self.product_code}`\n戦略: `{self.strategy.name}` ({self.timeframe})\nモード: `{mode_str}`\nレポート間隔: {self.hourly_interval_sec / 3600:.1f} 時間ごと"
        )

        step = 0
        try:
            while True:
                step += 1
                status = self.execute_step()
                
                sig_str = "買い(1)" if status["signal"] == 1 else ("売り(-1)" if status["signal"] == -1 else "中立(0)")
                pos_str = f"{status['position']:+.3f}"
                unreal_str = f"{status['unrealized_pnl']:+8.1f}"
                real_str = f"{status['realized_pnl']:+8.1f}"

                print(f" {status['timestamp']} | {status['current_price']:10,.0f} | {sig_str:8} | {pos_str:10} | {unreal_str:10} | {real_str:10}", flush=True)

                # 定期レポート送信 (デフォルト1時間ごと)
                if time.time() - self.last_hourly_report_time >= self.hourly_interval_sec:
                    import glob
                    wins = [t for t in self.trades_history if t.get("pnl", 0) > 0]
                    win_rate = (len(wins) / len(self.trades_history) * 100.0) if self.trades_history else 0.0
                    approved_count = len(glob.glob("strategies/approved/*.py"))
                    self.notifier.send_hourly_report(
                        strategy_name=self.strategy.name,
                        symbol=self.product_code,
                        timeframe=self.timeframe,
                        current_price=status["current_price"],
                        position_btc=status["position"],
                        unrealized_pnl=status["unrealized_pnl"],
                        realized_pnl=status["realized_pnl"],
                        total_trades=len(self.trades_history),
                        win_rate_pct=win_rate,
                        approved_strategies_count=approved_count,
                        pipeline_status="正常稼働中"
                    )
                    self.last_hourly_report_time = time.time()

                if max_steps and step >= max_steps:
                    print(f"\n[LiveTrader] 指定ステップ数 ({max_steps}) に到達したため終了します。")
                    break

                time.sleep(self.poll_interval_sec)

        except KeyboardInterrupt:
            print("\n[LiveTrader] ユーザーによって中断されました (Ctrl+C)")

        finally:
            self._print_summary()

    def _print_summary(self):
        """終了時のサマリーレポート表示"""
        print("\n" + "=" * 65)
        print("                 ペーパートレード 終了サマリー                 ")
        print("=" * 65)
        print(f"総トレード数    : {len(self.trades_history)} 回")
        print(f"累計実現損益    : {self.realized_pnl_jpy:+,.1f} 円")
        print(f"最終仮想資産残高: {self.virtual_cash_jpy:,.1f} 円")
        print(f"トレードログ    : reports/live_paper_trades.json")
        print("=" * 65 + "\n")
