import traceback
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np
from core.base_strategy import BaseStrategy
from core.metrics import PerformanceMetrics


class BacktestEngine:
    """
    リアルタイムLIVE執行環境 (LivePaperTrader) と完全同期した高精度バックテストシミュレータ。
    - 気配値スプレッドモデル (Best Bid / Best Ask)
    - ハードストップロス (SL) & 目標利幅ガード (TP)
    - 連続損失サーキットブレーカー & 日次損失上限ガード
    - 最大保有時間タイムアウト
    """

    def __init__(
        self,
        initial_capital: float = 100000.0,
        commission_rate: float = 0.0000,
        slippage_rate: float = 0.0001,
        order_size_btc: float = 0.001,
        min_profit_jpy: float = 18.0,
        stop_loss_jpy: float = 25.0,
        spread_jpy: float = 2000.0,
        daily_loss_limit_jpy: float = 300.0,
        max_consecutive_losses: int = 4,
        max_hold_bars: int = 30,
        execution_mode: str = "live_aligned",
    ):
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.slippage_rate = slippage_rate
        self.order_size_btc = order_size_btc
        self.min_profit_jpy = min_profit_jpy
        self.stop_loss_jpy = stop_loss_jpy
        self.spread_jpy = spread_jpy
        self.daily_loss_limit_jpy = daily_loss_limit_jpy
        self.max_consecutive_losses = max_consecutive_losses
        self.max_hold_bars = max_hold_bars
        self.execution_mode = execution_mode

    def run(self, strategy: BaseStrategy, df: pd.DataFrame, timeframe: str = "1h") -> Dict[str, Any]:
        """
        戦略のバックテストを実行する。
        """
        try:
            # 1. シグナル生成
            df_signals = strategy.generate_signals(df.copy())
            if "signal" not in df_signals.columns:
                raise ValueError("戦略コードの generate_signals() に 'signal' 列が含まれていません。")

            if self.execution_mode == "legacy_close":
                return self._run_legacy_close(df_signals, timeframe)
            else:
                return self._run_live_aligned(df_signals, timeframe)

        except Exception as e:
            tb = traceback.format_exc()
            return {
                "success": False,
                "error": f"{type(e).__name__}: {str(e)}\n\nTraceback:\n{tb}",
                "metrics": PerformanceMetrics.empty_metrics(),
                "trades": [],
                "equity_curve": pd.Series([], dtype=float),
            }

    def _run_live_aligned(self, df_signals: pd.DataFrame, timeframe: str) -> Dict[str, Any]:
        """
        LIVE執行エンジン (LivePaperTrader) と完全同期した執行シミュレーション。
        """
        capital = self.initial_capital
        position_btc = 0.0
        entry_price = 0.0
        entry_time = None
        entry_bar_idx = 0
        consecutive_losses = 0
        daily_realized_pnl = 0.0
        is_halted = False
        last_date = None

        trades: List[Dict[str, Any]] = []
        equity_history = []

        half_spread = self.spread_jpy / 2.0

        for i in range(len(df_signals)):
            row = df_signals.iloc[i]
            current_time = row["timestamp"] if "timestamp" in row else i
            open_p = float(row["open"]) if "open" in row else float(row["close"])
            high_p = float(row["high"]) if "high" in row else float(row["close"])
            low_p = float(row["low"]) if "low" in row else float(row["close"])
            close_p = float(row["close"])
            sig = int(row["signal"]) if not pd.isna(row["signal"]) else 0

            # 日付判定 (JST日付切り替わりで日次リミットリセット)
            curr_date = None
            if isinstance(current_time, (pd.Timestamp, str)):
                try:
                    ts = pd.to_datetime(current_time)
                    # UTC+9 JST換算
                    jst_ts = ts + pd.Timedelta(hours=9)
                    curr_date = jst_ts.date()
                except Exception:
                    pass

            if curr_date is not None and curr_date != last_date:
                daily_realized_pnl = 0.0
                if is_halted and consecutive_losses < self.max_consecutive_losses:
                    # 日次損失上限による停止は日を跨げば解除
                    is_halted = False
                last_date = curr_date

            # 気配値推定 (Best Bid, Best Ask)
            best_bid = close_p - half_spread
            best_ask = close_p + half_spread

            # 1. 保有中建玉のバー内決済判定 (SL, タイムアウト, TP/シグナル)
            if position_btc != 0.0:
                hold_bars = i - entry_bar_idx
                exit_triggered = False
                exit_price = 0.0
                exit_reason = ""

                if position_btc > 0:
                    # ロング保有時:
                    # 最悪価格 (安値でのBest Bid)
                    worst_bid = low_p - half_spread
                    worst_unrealized_pnl = (worst_bid - entry_price) * position_btc

                    # A. ハードストップロス判定 (バー内の急落で損切閾値到達)
                    if worst_unrealized_pnl <= -abs(self.stop_loss_jpy):
                        exit_triggered = True
                        exit_price = entry_price - (abs(self.stop_loss_jpy) / position_btc)
                        exit_reason = "STOP_LOSS_LONG"

                    # B. タイムアウト判定 (最大保有時間到達)
                    elif hold_bars >= self.max_hold_bars:
                        exit_triggered = True
                        exit_price = best_bid
                        exit_reason = "TIMEOUT_LONG"

                    # C. シグナル決済判定 (シグナル解消 or ドテン)
                    elif sig <= 0:
                        cur_unrealized = (best_bid - entry_price) * position_btc
                        if cur_unrealized >= self.min_profit_jpy or sig == -1:
                            exit_triggered = True
                            exit_price = best_bid
                            exit_reason = "TAKE_PROFIT_LONG" if sig == 0 else "DOTEN_CLOSE_LONG"
                        # min_profit_jpy 未達かつ sig == 0 の場合は HOLD_GUARD (見送り)

                else:
                    # ショート保有時:
                    # 最悪価格 (高値でのBest Ask)
                    worst_ask = high_p + half_spread
                    worst_unrealized_pnl = (entry_price - worst_ask) * abs(position_btc)

                    # A. ハードストップロス判定
                    if worst_unrealized_pnl <= -abs(self.stop_loss_jpy):
                        exit_triggered = True
                        exit_price = entry_price + (abs(self.stop_loss_jpy) / abs(position_btc))
                        exit_reason = "STOP_LOSS_SHORT"

                    # B. タイムアウト判定
                    elif hold_bars >= self.max_hold_bars:
                        exit_triggered = True
                        exit_price = best_ask
                        exit_reason = "TIMEOUT_SHORT"

                    # C. シグナル決済判定
                    elif sig >= 0:
                        cur_unrealized = (entry_price - best_ask) * abs(position_btc)
                        if cur_unrealized >= self.min_profit_jpy or sig == 1:
                            exit_triggered = True
                            exit_price = best_ask
                            exit_reason = "TAKE_PROFIT_SHORT" if sig == 0 else "DOTEN_CLOSE_SHORT"

                # 決済執行処理
                if exit_triggered:
                    # スリッページ反映
                    if position_btc > 0:
                        exit_price = exit_price * (1.0 - self.slippage_rate)
                    else:
                        exit_price = exit_price * (1.0 + self.slippage_rate)

                    trade_pnl = (exit_price - entry_price) * position_btc
                    comm = abs(position_btc * exit_price) * self.commission_rate
                    net_pnl = trade_pnl - comm

                    capital += net_pnl
                    daily_realized_pnl += net_pnl

                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": current_time,
                        "direction": "LONG" if position_btc > 0 else "SHORT",
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "size": abs(position_btc),
                        "pnl": net_pnl,
                        "return_pct": (net_pnl / (abs(position_btc) * entry_price)) * 100.0 if entry_price > 0 else 0.0,
                        "reason": exit_reason,
                        "hold_bars": hold_bars,
                    })

                    # 連敗カウンター & サーキットブレーカー判定
                    if net_pnl < 0:
                        consecutive_losses += 1
                        if consecutive_losses >= self.max_consecutive_losses:
                            is_halted = True
                        if daily_realized_pnl <= -abs(self.daily_loss_limit_jpy):
                            is_halted = True
                    elif net_pnl > 0:
                        consecutive_losses = 0

                    position_btc = 0.0
                    entry_price = 0.0
                    entry_time = None

            # 2. 新規エントリー判定 (ノーポジ かつ サーキットブレーカー非作動)
            if position_btc == 0.0 and not is_halted:
                if sig == 1:
                    # ロングエントリー: Best Ask + スリッページで約定
                    exec_p = best_ask * (1.0 + self.slippage_rate)
                    position_btc = self.order_size_btc
                    entry_price = exec_p
                    entry_time = current_time
                    entry_bar_idx = i
                elif sig == -1:
                    # ショートエントリー: Best Bid - スリッページで約定
                    exec_p = best_bid * (1.0 - self.slippage_rate)
                    position_btc = -self.order_size_btc
                    entry_price = exec_p
                    entry_time = current_time
                    entry_bar_idx = i

            # 3. 資産評価額の記録 (時価評価)
            unrealized = 0.0
            if position_btc > 0:
                unrealized = (best_bid - entry_price) * position_btc
            elif position_btc < 0:
                unrealized = (entry_price - best_ask) * abs(position_btc)

            equity_history.append(capital + unrealized)

        # 最終バーでの残存ポジション手仕舞い
        if position_btc != 0.0:
            final_row = df_signals.iloc[-1]
            close_p = float(final_row["close"])
            best_bid = close_p - half_spread
            best_ask = close_p + half_spread
            exit_p = (best_bid if position_btc > 0 else best_ask) * (1.0 - self.slippage_rate if position_btc > 0 else 1.0 + self.slippage_rate)
            trade_pnl = (exit_p - entry_price) * position_btc
            comm = abs(position_btc * exit_p) * self.commission_rate
            net_pnl = trade_pnl - comm
            capital += net_pnl
            trades.append({
                "entry_time": entry_time,
                "exit_time": final_row.get("timestamp", len(df_signals)-1),
                "direction": "LONG" if position_btc > 0 else "SHORT",
                "entry_price": entry_price,
                "exit_price": exit_p,
                "size": abs(position_btc),
                "pnl": net_pnl,
                "return_pct": (net_pnl / (abs(position_btc) * entry_price)) * 100.0 if entry_price > 0 else 0.0,
                "reason": "FINAL_BAR_CLOSE",
                "hold_bars": len(df_signals) - 1 - entry_bar_idx,
            })

        equity_series = pd.Series(equity_history, dtype=float)
        if not equity_series.empty:
            equity_series.iloc[-1] = capital

        metrics = PerformanceMetrics.calculate_metrics(equity_series, trades, timeframe=timeframe)

        return {
            "success": True,
            "error": None,
            "metrics": metrics,
            "trades": trades,
            "equity_curve": equity_series,
        }

    def run_ticks(self, strategy: BaseStrategy, df_ticks: pd.DataFrame, max_hold_sec: float = 1800.0) -> Dict[str, Any]:
        """
        ティック/気配値データ (2秒刻み等) を用いた完全LIVE同期バックテスト。
        - リアルタイムBest Bid / Best Askスプレッド
        - ティック単位の利確・損切・タイムアウト
        - サーキットブレーカー (4連敗 / 日次損失上限)
        """
        try:
            # シグナル生成 (必要に応じてclose列から算出、または事前算出シグナル)
            df = df_ticks.copy()
            if "close" not in df.columns and "mid_price" in df.columns:
                df["close"] = df["mid_price"]
            
            df_signals = strategy.generate_signals(df)
            if "signal" not in df_signals.columns:
                raise ValueError("戦略コードの generate_signals() に 'signal' 列が含まれていません。")

            capital = self.initial_capital
            position_btc = 0.0
            entry_price = 0.0
            entry_time = None
            entry_ts_sec = 0.0
            consecutive_losses = 0
            daily_realized_pnl = 0.0
            is_halted = False
            last_date = None

            trades: List[Dict[str, Any]] = []
            equity_history = []

            for i in range(len(df_signals)):
                row = df_signals.iloc[i]
                current_time = row.get("timestamp", i)
                
                # タイムスタンプ秒の算出
                cur_ts_sec = 0.0
                if isinstance(current_time, (int, float)):
                    cur_ts_sec = current_time / 1000.0 if current_time > 1e11 else float(current_time)
                elif isinstance(current_time, (pd.Timestamp, str)):
                    try:
                        cur_ts_sec = pd.to_datetime(current_time).timestamp()
                    except Exception:
                        cur_ts_sec = float(i)

                # 気配値取得
                mid_p = float(row.get("mid_price", row.get("close", 0.0)))
                half_spread = (self.spread_jpy / 2.0)
                best_bid = float(row.get("best_bid", mid_p - half_spread))
                best_ask = float(row.get("best_ask", mid_p + half_spread))
                sig = int(row["signal"]) if not pd.isna(row["signal"]) else 0

                # JST日付変更判定
                try:
                    jst_date = (pd.to_datetime(cur_ts_sec, unit="s") + pd.Timedelta(hours=9)).date()
                    if last_date is not None and jst_date != last_date:
                        daily_realized_pnl = 0.0
                        if is_halted and consecutive_losses < self.max_consecutive_losses:
                            is_halted = False
                    last_date = jst_date
                except Exception:
                    pass

                # 1. 保有中建玉の判定
                if position_btc != 0.0:
                    elapsed_sec = cur_ts_sec - entry_ts_sec if entry_ts_sec > 0 else 0.0
                    exit_triggered = False
                    exit_price = 0.0
                    exit_reason = ""

                    if position_btc > 0:
                        # ロング保有時: 即時決済はBest Bid
                        unrealized_pnl = (best_bid - entry_price) * position_btc

                        if unrealized_pnl <= -abs(self.stop_loss_jpy):
                            exit_triggered = True
                            exit_price = best_bid
                            exit_reason = "STOP_LOSS_LONG"
                        elif elapsed_sec >= max_hold_sec:
                            exit_triggered = True
                            exit_price = best_bid
                            exit_reason = "TIMEOUT_LONG"
                        elif sig <= 0:
                            if unrealized_pnl >= self.min_profit_jpy or sig == -1:
                                exit_triggered = True
                                exit_price = best_bid
                                exit_reason = "TAKE_PROFIT_LONG" if sig == 0 else "DOTEN_CLOSE_LONG"
                    else:
                        # ショート保有時: 即時決済はBest Ask
                        unrealized_pnl = (entry_price - best_ask) * abs(position_btc)

                        if unrealized_pnl <= -abs(self.stop_loss_jpy):
                            exit_triggered = True
                            exit_price = best_ask
                            exit_reason = "STOP_LOSS_SHORT"
                        elif elapsed_sec >= max_hold_sec:
                            exit_triggered = True
                            exit_price = best_ask
                            exit_reason = "TIMEOUT_SHORT"
                        elif sig >= 0:
                            if unrealized_pnl >= self.min_profit_jpy or sig == 1:
                                exit_triggered = True
                                exit_price = best_ask
                                exit_reason = "TAKE_PROFIT_SHORT" if sig == 0 else "DOTEN_CLOSE_SHORT"

                    if exit_triggered:
                        trade_pnl = (exit_price - entry_price) * position_btc
                        capital += trade_pnl
                        daily_realized_pnl += trade_pnl

                        trades.append({
                            "entry_time": entry_time,
                            "exit_time": current_time,
                            "direction": "LONG" if position_btc > 0 else "SHORT",
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "size": abs(position_btc),
                            "pnl": trade_pnl,
                            "return_pct": (trade_pnl / (abs(position_btc) * entry_price)) * 100.0 if entry_price > 0 else 0.0,
                            "reason": exit_reason,
                            "hold_sec": elapsed_sec,
                        })

                        if trade_pnl < 0:
                            consecutive_losses += 1
                            if consecutive_losses >= self.max_consecutive_losses:
                                is_halted = True
                            if daily_realized_pnl <= -abs(self.daily_loss_limit_jpy):
                                is_halted = True
                        elif trade_pnl > 0:
                            consecutive_losses = 0

                        position_btc = 0.0
                        entry_price = 0.0
                        entry_time = None
                        entry_ts_sec = 0.0

                # 2. 新規エントリー
                if position_btc == 0.0 and not is_halted:
                    if sig == 1:
                        position_btc = self.order_size_btc
                        entry_price = best_ask
                        entry_time = current_time
                        entry_ts_sec = cur_ts_sec
                    elif sig == -1:
                        position_btc = -self.order_size_btc
                        entry_price = best_bid
                        entry_time = current_time
                        entry_ts_sec = cur_ts_sec

                unrealized = 0.0
                if position_btc > 0:
                    unrealized = (best_bid - entry_price) * position_btc
                elif position_btc < 0:
                    unrealized = (entry_price - best_ask) * abs(position_btc)

                equity_history.append(capital + unrealized)

            # 最終残存決済
            if position_btc != 0.0:
                final_row = df_signals.iloc[-1]
                mid_p = float(final_row.get("mid_price", final_row.get("close", 0.0)))
                half_spread = (self.spread_jpy / 2.0)
                best_bid = float(final_row.get("best_bid", mid_p - half_spread))
                best_ask = float(final_row.get("best_ask", mid_p + half_spread))
                exit_p = best_bid if position_btc > 0 else best_ask
                trade_pnl = (exit_p - entry_price) * position_btc
                capital += trade_pnl
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": final_row.get("timestamp", len(df_signals)-1),
                    "direction": "LONG" if position_btc > 0 else "SHORT",
                    "entry_price": entry_price,
                    "exit_price": exit_p,
                    "size": abs(position_btc),
                    "pnl": trade_pnl,
                    "return_pct": (trade_pnl / (abs(position_btc) * entry_price)) * 100.0 if entry_price > 0 else 0.0,
                    "reason": "FINAL_TICK_CLOSE",
                    "hold_sec": cur_ts_sec - entry_ts_sec,
                })

            equity_series = pd.Series(equity_history, dtype=float)
            if not equity_series.empty:
                equity_series.iloc[-1] = capital

            metrics = PerformanceMetrics.calculate_metrics(equity_series, trades, timeframe="tick")

            return {
                "success": True,
                "error": None,
                "metrics": metrics,
                "trades": trades,
                "equity_curve": equity_series,
            }
        except Exception as e:
            tb = traceback.format_exc()
            return {
                "success": False,
                "error": f"{type(e).__name__}: {str(e)}\n\nTraceback:\n{tb}",
                "metrics": PerformanceMetrics.empty_metrics(),
                "trades": [],
                "equity_curve": pd.Series([], dtype=float),
            }

    def _run_legacy_close(self, df_signals: pd.DataFrame, timeframe: str) -> Dict[str, Any]:
        """従来の簡易終値モデル (後方互換用)"""
        capital = self.initial_capital
        position = 0.0
        entry_price = 0.0
        entry_time = None
        trades: List[Dict[str, Any]] = []
        equity_history = []

        for i in range(len(df_signals)):
            row = df_signals.iloc[i]
            current_time = row["timestamp"] if "timestamp" in row else i
            close_p = float(row["close"])
            sig = int(row["signal"]) if not pd.isna(row["signal"]) else 0

            unrealized_pnl = position * (close_p - entry_price) if position != 0 else 0.0
            equity_history.append(capital + unrealized_pnl)

            if sig != 0:
                desired_pos_sign = 1 if sig > 0 else -1
                current_pos_sign = 1 if position > 0 else (-1 if position < 0 else 0)

                if desired_pos_sign != current_pos_sign:
                    if position != 0:
                        exit_p = close_p * (1 - self.slippage_rate if position > 0 else 1 + self.slippage_rate)
                        pnl = position * (exit_p - entry_price)
                        comm = abs(position * exit_p) * self.commission_rate
                        net_pnl = pnl - comm
                        capital += net_pnl
                        trades.append({
                            "entry_time": entry_time,
                            "exit_time": current_time,
                            "direction": "LONG" if position > 0 else "SHORT",
                            "entry_price": entry_price,
                            "exit_price": exit_p,
                            "size": abs(position),
                            "pnl": net_pnl,
                            "return_pct": (net_pnl / (abs(position) * entry_price)) * 100.0 if entry_price > 0 else 0,
                        })
                        position = 0.0

                    exec_p = close_p * (1 + self.slippage_rate if desired_pos_sign > 0 else 1 - self.slippage_rate)
                    trade_size_capital = capital * 0.95
                    if trade_size_capital > 0 and exec_p > 0:
                        qty = (trade_size_capital / exec_p) * desired_pos_sign
                        comm = abs(qty * exec_p) * self.commission_rate
                        capital -= comm
                        position = qty
                        entry_price = exec_p
                        entry_time = current_time

            elif sig == 0 and position != 0:
                exit_p = close_p * (1 - self.slippage_rate if position > 0 else 1 + self.slippage_rate)
                pnl = position * (exit_p - entry_price)
                comm = abs(position * exit_p) * self.commission_rate
                net_pnl = pnl - comm
                capital += net_pnl
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": current_time,
                    "direction": "LONG" if position > 0 else "SHORT",
                    "entry_price": entry_price,
                    "exit_price": exit_p,
                    "size": abs(position),
                    "pnl": net_pnl,
                    "return_pct": (net_pnl / (abs(position) * entry_price)) * 100.0 if entry_price > 0 else 0,
                })
                position = 0.0

        if position != 0:
            final_row = df_signals.iloc[-1]
            close_p = float(final_row["close"])
            exit_p = close_p * (1 - self.slippage_rate if position > 0 else 1 + self.slippage_rate)
            pnl = position * (exit_p - entry_price)
            comm = abs(position * exit_p) * self.commission_rate
            capital += (pnl - comm)
            trades.append({
                "entry_time": entry_time,
                "exit_time": final_row.get("timestamp", len(df_signals)-1),
                "direction": "LONG" if position > 0 else "SHORT",
                "entry_price": entry_price,
                "exit_price": exit_p,
                "size": abs(position),
                "pnl": pnl - comm,
                "return_pct": ((pnl - comm) / (abs(position) * entry_price)) * 100.0 if entry_price > 0 else 0,
            })

        equity_series = pd.Series(equity_history, dtype=float)
        if not equity_series.empty:
            equity_series.iloc[-1] = capital

        metrics = PerformanceMetrics.calculate_metrics(equity_series, trades, timeframe=timeframe)

        return {
            "success": True,
            "error": None,
            "metrics": metrics,
            "trades": trades,
            "equity_curve": equity_series,
        }
