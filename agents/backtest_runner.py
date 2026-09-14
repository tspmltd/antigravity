import os
import sys
import importlib.util
from typing import Dict, Any, Optional
import pandas as pd
from agents.base_agent import BaseAgent
from core.engine import BacktestEngine
from core.dataloader import DataLoader


RUNNER_FIX_PROMPT = """あなたはバックテスト実行エンジニア【Backtest Runner】です。
提供された戦略コードを実行したところ、以下のエラーが発生しました。
コードを自己修復し、実行可能な完全なPythonコードを再出力してください。

【制約事項】
1. `from core.base_strategy import BaseStrategy` をインポートし、`CustomStrategy` クラスを定義すること。
2. `generate_signals(self, df: pd.DataFrame)` 内で例外やNaNエラー、インデックスエラーが起きないよう防護措置を取ること。
3. コードは ```python ... ``` ブロックで出力すること。
"""


class BacktestRunner(BaseAgent):
    """
    【2. 検証 (Backtest Runner)】
    バックテストを実行し、エラー修復とメトリクス(Sharpe, MDD等)を抽出する。
    """

    def __init__(self, engine: Optional[BacktestEngine] = None, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="BacktestRunner", role="Execution, Metrics Extractor & Self-Healing", config=config)
        self.engine = engine or BacktestEngine()
        self.max_healing_attempts = self.config.get("pipeline", {}).get("max_self_healing_attempts", 3)

    def load_strategy_from_file(self, file_path: str):
        """Pythonファイルを動的インポートしてCustomStrategyインスタンスを生成"""
        # ファイルのタイムスタンプを含めて常に最新のコードを再ロード
        mtime = int(os.path.getmtime(file_path))
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        module_name = f"dynamic_strat_{base_name}_{mtime}"
        
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"モジュールをロードできませんでした: {file_path}")
        
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        if not hasattr(module, "CustomStrategy"):
            raise AttributeError("ファイル内に 'CustomStrategy' クラスが見つかりません。")

        return module.CustomStrategy()

    def run_backtest_with_healing(
        self,
        strategy_info: Dict[str, Any],
        train_df: pd.DataFrame,
        test_df: Optional[pd.DataFrame] = None,
        timeframe: str = "1h"
    ) -> Dict[str, Any]:
        """
        バックテストを実行し、エラーが発生した場合は最大回数まで自己修復を試みる。
        """
        file_path = strategy_info["file_path"]
        attempts = 0

        while attempts <= self.max_healing_attempts:
            attempts += 1
            try:
                # 戦略の動的ロード
                strategy = self.load_strategy_from_file(file_path)

                # In-Sample バックテスト実行
                is_result = self.engine.run(strategy, train_df, timeframe=timeframe)

                if not is_result["success"]:
                    raise RuntimeError(is_result["error"])

                # Out-of-Sample バックテスト実行 (指定がある場合)
                oos_result = None
                if test_df is not None and not test_df.empty:
                    oos_result = self.engine.run(strategy, test_df, timeframe=timeframe)

                return {
                    "is_success": True,
                    "attempts": attempts,
                    "strategy_info": strategy_info,
                    "in_sample_metrics": is_result["metrics"],
                    "out_of_sample_metrics": oos_result["metrics"] if oos_result else None,
                    "trades": is_result["trades"],
                    "equity_curve": is_result["equity_curve"],
                    "error": None,
                }

            except Exception as e:
                error_msg = str(e)
                print(f"[Runner] 実行エラー検知 (試行 {attempts}/{self.max_healing_attempts}): {error_msg}")

                if attempts > self.max_healing_attempts:
                    return {
                        "is_success": False,
                        "attempts": attempts,
                        "strategy_info": strategy_info,
                        "in_sample_metrics": None,
                        "out_of_sample_metrics": None,
                        "trades": [],
                        "equity_curve": None,
                        "error": f"自己修復上限回数({self.max_healing_attempts})を超過: {error_msg}",
                    }

                # 自己修復プロンプトの実行
                self._heal_strategy_file(file_path, error_msg)

        return {
            "is_success": False,
            "attempts": attempts,
            "strategy_info": strategy_info,
            "error": "実行不能",
        }

    def _heal_strategy_file(self, file_path: str, error_msg: str):
        """エラーログに基づき戦略コードを自己修復して上書き保存"""
        with open(file_path, "r", encoding="utf-8") as f:
            original_code = f.read()

        user_prompt = f"""【発生したエラー】\n{error_msg}\n\n【対象コード】\n```python\n{original_code}\n```\nエラーを修正した完全なコードを出力してください。"""

        if self.provider != "mock":
            repaired_text = self.call_llm(RUNNER_FIX_PROMPT, user_prompt)
            repaired_code = self.extract_python_code(repaired_text)
        else:
            # モック修復ロジック (未定義列の追加やNaN補完など)
            repaired_code = original_code
            if "'signal' 列が含まれていません" in error_msg or "signal" not in original_code:
                repaired_code = repaired_code.replace("return df", "df['signal'] = 0\n        return df")
            elif "KeyError: 'signal'" in error_msg:
                repaired_code = repaired_code.replace("return df", "df['signal'] = 0\n        return df")
            elif ".fillna(0)" not in repaired_code:
                repaired_code = repaired_code.replace("return df", "df['signal'] = df['signal'].fillna(0)\n        return df")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(repaired_code)
        
        print(f"[Runner] 自己修復コードを書き込みました: {file_path}")
