import os
from typing import Dict, Any, Optional
from agents.base_agent import BaseAgent


OPTIMIZER_SYSTEM_PROMPT = """あなたはクオンツ改善エンジニア【Optimizer】です。
前回のバックテスト結果・メトリクスとトレード履歴のボトルネックを分析し、
戦略のフィルタ追加やパラメータ再調整を行った改善コード（Next Version）を作成してください。

【改善の着眼点】
1. 最大ドローダウン (MDD) が大きい場合:
   - ATRやボラティリティ指標を用いたストップロス・フィルタの導入
   - 不安定相場（チョッピー相場）でのエントリー抑制
2. 勝率やプロフィットファクター (PF) が低い場合:
   - より上位足のトレンドフィルター（長期EMA同方向等）の追加
   - ダマシ回避のための確認足・しきい値の強化
3. シャープレシオの向上:
   - リスク対リワード比の改善、無駄な取引コストの削減
4. 必ず `from core.base_strategy import BaseStrategy` をインポートし、`CustomStrategy` クラスを定義すること。
5. 出力は ```python ... ``` ブロックで行うこと。
"""


class StrategyOptimizer(BaseAgent):
    """
    【3. 改善 (Optimizer)】
    ボトルネックを分析し、フィルタ追加やパラメータを再調整して検証へ戻す。
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="StrategyOptimizer", role="Bottleneck Analyzer & Strategy Refiner", config=config)

    def optimize_strategy(
        self,
        strategy_info: Dict[str, Any],
        backtest_result: Dict[str, Any],
        output_dir: str = "strategies/optimizing"
    ) -> Dict[str, Any]:
        """
        検証結果を分析し、改善版の戦略コードを生成する。
        """
        os.makedirs(output_dir, exist_ok=True)
        strat_id = strategy_info["strategy_id"]
        current_iter = strategy_info.get("iteration", 1)
        next_iter = current_iter + 1
        version_str = f"v{next_iter}.0"

        metrics = backtest_result.get("in_sample_metrics", {})
        original_code = strategy_info.get("code", "")
        if not original_code and os.path.exists(strategy_info["file_path"]):
            with open(strategy_info["file_path"], "r", encoding="utf-8") as f:
                original_code = f.read()

        user_prompt = f"""【前バージョン評価メトリクス】
- Sharpe Ratio: {metrics.get('sharpe_ratio')}
- Max Drawdown: {metrics.get('max_drawdown_pct')}%
- Total Return: {metrics.get('total_return_pct')}%
- Win Rate: {metrics.get('win_rate_pct')}%
- Profit Factor: {metrics.get('profit_factor')}
- Total Trades: {metrics.get('total_trades')}

【現行コード】
```python
{original_code}
```

上記のボトルネックを解消する改善コードを生成してください。"""

        if self.provider != "mock":
            response = self.call_llm(OPTIMIZER_SYSTEM_PROMPT, user_prompt)
            improved_code = self.extract_python_code(response)
            change_summary = "LLMによるパラメータ調整およびフィルタ追加"
        else:
            # モック最適化ロジック (パラメータの微調整 & フィルター強化)
            improved_code, change_summary = self._mock_optimize(original_code, metrics, next_iter)

        next_filename = f"{strat_id}_v{next_iter}.py"
        next_filepath = os.path.join(output_dir, next_filename)

        with open(next_filepath, "w", encoding="utf-8") as f:
            f.write(improved_code)

        print(f"[Optimizer] 改善版コードを生成: {next_filepath} (変更点: {change_summary})")

        return {
            "strategy_id": strat_id,
            "name": strategy_info.get("name", strat_id),
            "version": version_str,
            "file_path": next_filepath,
            "code": improved_code,
            "hypothesis": strategy_info.get("hypothesis", "") + f" -> 改善{next_iter}: {change_summary}",
            "iteration": next_iter,
            "previous_metrics": metrics,
        }

    def _mock_optimize(self, original_code: str, metrics: Dict[str, Any], next_iter: int) -> tuple[str, str]:
        """モック用の改善ルール適用"""
        code = original_code
        changes = []

        mdd = metrics.get("max_drawdown_pct", 0)
        sharpe = metrics.get("sharpe_ratio", 0)

        # パラメータの最適化例 (期間の延長や閾値調整)
        if "fast_period" in code:
            code = code.replace('"fast_period": 12', '"fast_period": 10')
            code = code.replace('"slow_period": 26', '"slow_period": 30')
            changes.append("EMAスパン調整(10, 30)")
        elif "rsi_oversold" in code:
            code = code.replace('"rsi_oversold": 30', '"rsi_oversold": 25')
            code = code.replace('"rsi_overbought": 70', '"rsi_overbought": 75')
            changes.append("RSI閾値厳格化(25, 75)")
        elif "spread_multiplier" in code:
            code = code.replace('"spread_multiplier": 0.5', '"spread_multiplier": 0.7')
            code = code.replace('"vol_filter_threshold": 1.5', '"vol_filter_threshold": 1.2')
            changes.append("スプレッド拡大(0.7) & ボラフィルター厳格化(1.2)")

        # ドローダウン対策フィルターの注入 (未導入の場合)
        if "regime_filter" not in code:
            injection = """
        # Optimizer追加: レジームフィルター (200期間SMA立脚判定)
        df["ma_regime"] = df["close"].rolling(window=100).mean()
        trend_up = df["close"] > df["ma_regime"]
        trend_down = df["close"] < df["ma_regime"]
        df.loc[(df["signal"] == 1) & (~trend_up), "signal"] = 0
        df.loc[(df["signal"] == -1) & (~trend_down), "signal"] = 0
        df["signal"] = df["signal"].replace(0, np.nan).ffill().fillna(0).astype(int)
        return df"""
            code = code.replace("return df", injection)
            changes.append("100SMAレジームフィルター追加")

        summary = " + ".join(changes) if changes else f"パラメータチューニング (iter {next_iter})"
        return code, summary
