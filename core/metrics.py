import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional


class PerformanceMetrics:
    """
    バックテスト結果から定量評価メトリクス（Sharpe, MDD, PF等）を計算するモジュール。
    時間足（1分足〜日足）に応じた正確な年率換算を行います。
    """

    TIMEFRAME_BARS_PER_YEAR = {
        "1m": 365.0 * 24.0 * 60.0,    # 525,600
        "5m": 365.0 * 24.0 * 12.0,    # 105,120
        "15m": 365.0 * 24.0 * 4.0,    # 35,040
        "1h": 365.0 * 24.0,           # 8,760
        "4h": 365.0 * 6.0,            # 2,190
        "1d": 365.0,                  # 365
    }

    @classmethod
    def calculate_metrics(
        cls,
        equity_curve: pd.Series,
        trades: List[Dict[str, Any]],
        timeframe: str = "1h",
        annual_factor: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        資産曲線とトレード一覧から指標を算出。
        """
        if equity_curve.empty or len(equity_curve) < 2:
            return cls.empty_metrics()

        initial_capital = float(equity_curve.iloc[0])
        final_capital = float(equity_curve.iloc[-1])
        total_return_pct = ((final_capital - initial_capital) / initial_capital) * 100.0

        # 年率換算係数の決定
        if annual_factor is None:
            clean_tf = timeframe.lower().replace("min", "m").replace("hour", "h").replace("day", "d")
            annual_factor = cls.TIMEFRAME_BARS_PER_YEAR.get(clean_tf, 8760.0)

        # リターン系列
        returns = equity_curve.pct_change().dropna()

        # シャープレシオ (無リスク金利 = 0% と仮定)
        if len(returns) > 1 and returns.std() > 0:
            mean_ret = returns.mean()
            std_ret = returns.std()
            sharpe_ratio = (mean_ret / std_ret) * np.sqrt(annual_factor)
        else:
            sharpe_ratio = 0.0

        # 最大ドローダウン (MDD: % および 円)
        cummax = equity_curve.cummax()
        drawdown_jpy = cummax - equity_curve
        max_drawdown_jpy = float(drawdown_jpy.max()) if not drawdown_jpy.empty else 0.0
        drawdown = (equity_curve - cummax) / cummax
        max_drawdown_pct = abs(float(drawdown.min())) * 100.0 if not drawdown.empty else 0.0

        # カルマーレシオ
        n_periods = len(equity_curve)
        if n_periods > 0 and max_drawdown_pct > 0 and initial_capital > 0 and final_capital > 0:
            years = n_periods / annual_factor
            if years > 0.01: # 最低数日以上のデータがある場合
                annualized_return_pct = (((final_capital / initial_capital) ** (1.0 / years)) - 1.0) * 100.0
                calmar_ratio = annualized_return_pct / max_drawdown_pct
            else:
                calmar_ratio = total_return_pct / max_drawdown_pct
        else:
            calmar_ratio = 0.0

        # トレード統計
        total_trades = len(trades)
        total_pnl_jpy = 0.0
        avg_trade_pnl_jpy = 0.0
        max_consecutive_losses = 0

        if total_trades > 0:
            winning_trades = [t for t in trades if t.get("pnl", 0) > 0]
            losing_trades = [t for t in trades if t.get("pnl", 0) < 0]
            win_rate_pct = (len(winning_trades) / total_trades) * 100.0

            total_pnl_jpy = sum(t.get("pnl", 0) for t in trades)
            avg_trade_pnl_jpy = total_pnl_jpy / total_trades

            gross_profit = sum(t["pnl"] for t in winning_trades)
            gross_loss = abs(sum(t["pnl"] for t in losing_trades))

            if gross_loss > 0:
                profit_factor = gross_profit / gross_loss
            else:
                profit_factor = 999.0 if gross_profit > 0 else 0.0

            # 最大連続損失数 (連敗) 計算
            cur_consec = 0
            for t in trades:
                if t.get("pnl", 0) < 0:
                    cur_consec += 1
                    if cur_consec > max_consecutive_losses:
                        max_consecutive_losses = cur_consec
                elif t.get("pnl", 0) > 0:
                    cur_consec = 0
        else:
            win_rate_pct = 0.0
            profit_factor = 0.0
            winning_trades = []
            losing_trades = []

        return {
            "initial_capital": float(initial_capital),
            "final_capital": float(final_capital),
            "total_return_pct": round(float(total_return_pct), 2),
            "total_pnl_jpy": round(float(total_pnl_jpy), 1),
            "avg_trade_pnl_jpy": round(float(avg_trade_pnl_jpy), 2),
            "sharpe_ratio": round(float(sharpe_ratio), 2),
            "max_drawdown_pct": round(float(max_drawdown_pct), 2),
            "max_drawdown_jpy": round(float(max_drawdown_jpy), 1),
            "calmar_ratio": round(float(calmar_ratio), 2),
            "total_trades": total_trades,
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate_pct": round(float(win_rate_pct), 2),
            "profit_factor": round(float(profit_factor), 2),
            "max_consecutive_losses": max_consecutive_losses,
            "timeframe": timeframe
        }

    @staticmethod
    def empty_metrics() -> Dict[str, Any]:
        return {
            "initial_capital": 0.0,
            "final_capital": 0.0,
            "total_return_pct": 0.0,
            "total_pnl_jpy": 0.0,
            "avg_trade_pnl_jpy": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "max_drawdown_jpy": 0.0,
            "calmar_ratio": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "max_consecutive_losses": 0,
            "timeframe": "unknown"
        }
