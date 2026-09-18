import os
import sys
import unittest
from datetime import datetime

# パス追加
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from execution.adaptive_manager import AdaptiveManager
from execution.paper_trader import LivePaperTrader


class TestAdaptiveLoop(unittest.TestCase):

    def test_adaptive_trigger_and_reload(self):
        """損失発生時に自動で再最適化がトリガーされ、戦略がホットリロードされるかのテスト"""
        manager = AdaptiveManager()
        
        # 損失トレードが3回続いたシミュレーションデータ
        mock_trades = [
            {"timestamp": datetime.now().isoformat(), "direction": "BUY", "pnl": -1500.0, "size": 0.001},
            {"timestamp": datetime.now().isoformat(), "direction": "SELL", "pnl": -1800.0, "size": 0.001},
            {"timestamp": datetime.now().isoformat(), "direction": "BUY", "pnl": -1200.0, "size": 0.001},
        ]

        # 評価タイミング判定
        should_eval = manager.should_evaluate(mock_trades)
        self.assertTrue(should_eval, "3トレード終了時に評価が発動するべきです")

        # 改善用戦略情報
        strat_info = {
            "strategy_id": "test_adaptive_01",
            "name": "EmaTrendStrategy_test",
            "file_path": "strategies/proposed/strat_f6c1bf42_v1.py",
            "hypothesis": "初期トレンド戦略",
            "iteration": 1
        }

        from unittest.mock import patch

        # 1. ガバナンスが PASS の場合: ホットリロードされる
        with patch.object(manager.governance, "evaluate", return_value={"status": "PASS", "reason": "テスト合格"}):
            need_switch, new_path, msg = manager.evaluate_and_adapt(
                trades_history=mock_trades,
                initial_capital=100000.0,
                current_cash=95500.0,
                current_strategy_info=strat_info,
                symbol="BTC_JPY",
                timeframe="1m"
            )
            self.assertTrue(need_switch, "合格時には戦略差し替えフラグがTrueになるべきです")
            self.assertIsNotNone(new_path, "新戦略のパスが返されるべきです")
            self.assertTrue(os.path.exists(new_path), f"新戦略ファイルが存在するべきです: {new_path}")
            print(f"\n[UnitTest] 自律適応テスト成功: 新戦略が自動生成され差し替え準備完了 -> {new_path}")

        # 2. ガバナンスが REVISE の場合: 安全ガードが作動し差し替えブロックされる
        with patch.object(manager.governance, "evaluate", return_value={"status": "REVISE", "reason": "Sharpe不足"}):
            need_switch_rev, new_path_rev, msg_rev = manager.evaluate_and_adapt(
                trades_history=mock_trades,
                initial_capital=100000.0,
                current_cash=95500.0,
                current_strategy_info=strat_info,
                symbol="BTC_JPY",
                timeframe="1m"
            )
            self.assertFalse(need_switch_rev, "不合格時には戦略差し替えフラグがFalse（ブロック）になるべきです")
            self.assertIsNone(new_path_rev, "新戦略のパスはNoneであるべきです")
            print(f"[UnitTest] 安全ガードテスト成功: 不合格戦略のホットリロードが正常に遮断されました")


if __name__ == "__main__":
    unittest.main()
