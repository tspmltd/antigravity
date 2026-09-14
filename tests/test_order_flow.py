import unittest
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

from core.order_flow import OrderFlowAnalyzer
from agents.strategy_proposer import StrategyProposer


class TestOrderFlow(unittest.TestCase):
    def setUp(self):
        self.analyzer = OrderFlowAnalyzer(
            product_code="FX_BTC_JPY",
            window_seconds=30.0,
            persistence_threshold=0.6,
            noise_ceiling=0.25
        )

    def test_order_flow_delta_calculation(self):
        # 1. モック約定データを作成 (買いフロー優勢: 80% Buy, 20% Sell)
        now = datetime.now(timezone.utc)
        mock_execs = [
            {"id": 1, "side": "BUY", "size": 0.5, "price": 10000000, "exec_date": (now - timedelta(seconds=10)).isoformat()},
            {"id": 2, "side": "BUY", "size": 0.3, "price": 10001000, "exec_date": (now - timedelta(seconds=8)).isoformat()},
            {"id": 3, "side": "SELL", "size": 0.1, "price": 10000500, "exec_date": (now - timedelta(seconds=5)).isoformat()},
            {"id": 4, "side": "BUY", "size": 0.2, "price": 10002000, "exec_date": (now - timedelta(seconds=2)).isoformat()},
        ]
        self.analyzer.add_executions(mock_execs)

        # 継続度を高めるために何度かanalyze
        res = self.analyzer.analyze()
        self.assertEqual(res["taker_buy_vol"], 1.0)
        self.assertEqual(res["taker_sell_vol"], 0.1)
        self.assertAlmostEqual(res["net_delta"], 0.9, places=2)
        # delta_ratio = (1.0 - 0.1) / (1.0 + 0.1) = 0.9 / 1.1 = 0.818
        self.assertGreater(res["delta_ratio"], 0.8)

    def test_dynamic_cancel_on_toxic_reverse_flow(self):
        # 売りフローが急増した局面 (売り95%)
        now = datetime.now(timezone.utc)
        toxic_sell_execs = [
            {"id": 10, "side": "SELL", "size": 2.0, "price": 9990000, "exec_date": (now - timedelta(seconds=2)).isoformat()},
            {"id": 11, "side": "SELL", "size": 1.5, "price": 9988000, "exec_date": (now - timedelta(seconds=1)).isoformat()},
        ]
        analyzer = OrderFlowAnalyzer(product_code="FX_BTC_JPY")
        analyzer.add_executions(toxic_sell_execs)
        res = analyzer.analyze()

        # 売り圧殺到により買い指値即キャンセルがTrueになること
        self.assertTrue(res["should_cancel_bid"])
        self.assertFalse(res["should_cancel_ask"])

    def test_micro_trend_order_flow_strategy(self):
        # Proposerのテンプレート一覧からMicroTrendOrderFlowStrategyを抽出
        from agents.strategy_proposer import SAMPLE_STRATEGIES
        strat_def = next(s for s in SAMPLE_STRATEGIES if s["name"] == "MicroTrendOrderFlowStrategy")
        
        # 動的実行
        local_scope = {}
        exec(strat_def["code"], globals(), local_scope)
        strat_cls = local_scope["CustomStrategy"]
        strat = strat_cls()

        # 2bpずつ上昇する10本のテストDataFrame
        prices = [10000000.0 + (i * 3000.0) for i in range(15)] # 3000円幅 = 約0.03% (3bp)
        df_test = pd.DataFrame({
            "timestamp": pd.date_range("2026-09-14 10:00", periods=15, freq="1min"),
            "open": [p - 1500 for p in prices],
            "high": [p + 200 for p in prices],
            "low": [p - 1800 for p in prices],
            "close": prices,
            "volume": [0.5] * 15
        })

        df_res = strat.generate_signals(df_test)
        self.assertIn("signal", df_res.columns)
        # 上昇マイクロトレンドに乗って後半でシグナル1(買い)が出ていること
        self.assertIn(1, df_res["signal"].values)


if __name__ == "__main__":
    unittest.main()
