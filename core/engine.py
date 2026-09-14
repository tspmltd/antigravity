import traceback
from typing import Dict, Any, List, Optional
import pandas as pd
from core.base_strategy import BaseStrategy
from core.metrics import PerformanceMetrics


class BacktestEngine:
    """
    高速・軽量なイベント駆動型バックテストシミュレータ。
    """

    def __init__(
        self,
        initial_capital: float = 1000000.0,
        commission_rate: float = 0.0005,
        slippage_rate: float = 0.0002,
    ):
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.slippage_rate = slippage_rate

    def run(self, strategy: BaseStrategy, df: pd.DataFrame, timeframe: str = "1h") -> Dict[str, Any]:
        """
        戦略のバックテストを実行する。
        
        Returns:
            Dict containing:
                - success: bool
                - error: Optional[str]
                - metrics: Dict[str, Any]
                - trades: List[Dict[str, Any]]
                - equity_curve: pd.Series
        """
        try:
            # 1. シグナル生成
            df_signals = strategy.generate_signals(df.copy())
            if "signal" not in df_signals.columns:
                raise ValueError("戦略コードの generate_signals() に 'signal' 列が含まれていません。")

            # 2. シミュレーション実行
            capital = self.initial_capital
            position = 0.0  # 保有数量 (正: ロング, 負: ショート)
            entry_price = 0.0
            entry_time = None
            trades: List[Dict[str, Any]] = []
            equity_history = []

            for i in range(len(df_signals)):
                row = df_signals.iloc[i]
                current_time = row["timestamp"] if "timestamp" in row else i
                close_p = float(row["close"])
                sig = int(row["signal"]) if not pd.isna(row["signal"]) else 0

                # ポジション評価額
                unrealized_pnl = position * (close_p - entry_price) if position != 0 else 0.0
                current_equity = capital + unrealized_pnl
                equity_history.append(current_equity)

                # 売買ロジック (翌足始値でなく当足終値+スリッページで執行する簡易モデル)
                # シグナル変化時にエグジットまたはドテン
                if sig != 0:
                    desired_pos_sign = 1 if sig > 0 else -1
                    current_pos_sign = 1 if position > 0 else (-1 if position < 0 else 0)

                    if desired_pos_sign != current_pos_sign:
                        # 既存ポジションの決済
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
                                "pnl": net_pnl,
                                "return_pct": (net_pnl / (abs(position) * entry_price)) * 100.0 if entry_price > 0 else 0,
                            })
                            position = 0.0

                        # 新規ポジションのエントリー (全資金の95%を投入と仮定)
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
                    # ポジション解消シグナル
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
                        "pnl": net_pnl,
                        "return_pct": (net_pnl / (abs(position) * entry_price)) * 100.0 if entry_price > 0 else 0,
                    })
                    position = 0.0

            # 最終ポジションの強制クローズ評価
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
                    "pnl": pnl - comm,
                    "return_pct": ((pnl - comm) / (abs(position) * entry_price)) * 100.0 if entry_price > 0 else 0,
                })

            equity_series = pd.Series(equity_history)
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

        except Exception as e:
            tb = traceback.format_exc()
            return {
                "success": False,
                "error": f"{type(e).__name__}: {str(e)}\n\nTraceback:\n{tb}",
                "metrics": PerformanceMetrics.empty_metrics(),
                "trades": [],
                "equity_curve": pd.Series([]),
            }
