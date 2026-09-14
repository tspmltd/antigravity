import os
import sys
import unittest
import pandas as pd
from core.dataloader import DataLoader
from agents.backtest_runner import BacktestRunner
from agents.governance import StrategyGovernance


class TestPipelineComponents(unittest.TestCase):

    def setUp(self):
        self.df = DataLoader.load_or_generate_data(n_bars=500, seed=123)
        self.runner = BacktestRunner()
        self.governance = StrategyGovernance()

    def test_self_healing(self):
        """Runnerがエラーのあるコードを検知して修復を試みるテスト"""
        test_dir = "strategies/test_healing"
        os.makedirs(test_dir, exist_ok=True)
        file_path = os.path.join(test_dir, "broken_strat.py")

        # 意図的に未定義変数でクラッシュするコードを作成
        broken_code = """import pandas as pd
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(name="BrokenStrat")

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        # signal 列を作成せず返すバグ
        return df
"""
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(broken_code)

        strat_info = {
            "strategy_id": "test_broken_001",
            "name": "BrokenStrat",
            "file_path": file_path,
        }

        result = self.runner.run_backtest_with_healing(strat_info, self.df)
        # 自己修復が働き、signal列が補完されて成功することを確認
        self.assertTrue(result["is_success"], "自己修復によってバックテストが成功するべきです")
        self.assertIsNotNone(result["in_sample_metrics"])
        print("\n[UnitTest] 自己修復テスト成功: バグのある戦略コードが自動修復されました。")

    def test_governance_approval(self):
        """基準を満たした戦略がApprovedに昇格しレポートが出力されるテスト"""
        strat_info = {
            "strategy_id": "test_golden_001",
            "name": "GoldenStrategy",
            "version": "v1.0",
            "hypothesis": "低ドローダウンかつ高勝率の検証用戦略",
            "file_path": "strategies/proposed/sample_golden.py",
        }
        os.makedirs("strategies/proposed", exist_ok=True)
        with open(strat_info["file_path"], "w", encoding="utf-8") as f:
            f.write("# Dummy golden strategy")

        # 合格基準 (Sharpe >= 1.8, MDD <= 5.0%, Trades >= 30, PF >= 1.5) を満たすモック結果
        mock_bt_result = {
            "in_sample_metrics": {
                "sharpe_ratio": 2.5,
                "max_drawdown_pct": 3.8,
                "total_trades": 45,
                "profit_factor": 2.1,
                "total_return_pct": 42.5,
                "win_rate_pct": 65.0,
                "calmar_ratio": 3.2,
            },
            "out_of_sample_metrics": {
                "sharpe_ratio": 2.2,
                "max_drawdown_pct": 4.1,
                "total_return_pct": 30.0,
                "profit_factor": 1.8,
                "total_trades": 20,
            }
        }

        decision = self.governance.evaluate(strat_info, mock_bt_result, max_iterations=3, timeframe="1h")
        self.assertEqual(decision["status"], "PASS")
        self.assertTrue(os.path.exists(decision["report_path"]), "レポートファイルが生成されている必要があります")
        self.assertTrue(os.path.exists(decision["approved_file"]), "Approvedファイルがコピーされている必要があります")
        print("\n[UnitTest] ガバナンス合格テスト成功: レポートと承認コードが正常に生成されました。")


if __name__ == "__main__":
    unittest.main()
