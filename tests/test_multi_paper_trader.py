import unittest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from execution.multi_paper_trader import MultiStrategyLiveTrader
from agents.governance import StrategyGovernance
from core.base_strategy import BaseStrategy


class DummyMMStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(name="MicroSpreadMMStrategy", version="v1.0")
        self.strategy_type = "market_making"
        self.hypothesis = "微小スプレッド高頻度マーケットメイク"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"] = 1  # 常に買いシグナル
        return df


class DummyTrendStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(name="EmaTrendStrategy", version="v1.0")
        self.strategy_type = "trend_following"
        self.hypothesis = "長期EMAトレンドフォロー"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"] = 1  # 常に買いシグナル
        return df


class TestMultiPaperTrader(unittest.TestCase):
    def setUp(self):
        # 簡易データ
        now = datetime(2026, 9, 14, 10, 0, 0)
        times = [now - timedelta(minutes=i) for i in range(10, 0, -1)]
        df = pd.DataFrame({
            "timestamp": times,
            "open": [10000000.0] * 10,
            "high": [10010000.0] * 10,
            "low": [9990000.0] * 10,
            "close": [10000000.0] * 10,
            "volume": [1.0] * 10,
        })
        self.dummy_df = df

    def test_detect_strategy_type(self):
        trader = MultiStrategyLiveTrader(
            strategy_paths=[],
            product_code="FX_BTC_JPY",
            timeframe="1m"
        )
        trader.df_history = self.dummy_df.copy()

        mm_strat = DummyMMStrategy()
        trend_strat = DummyTrendStrategy()

        self.assertEqual(trader._detect_strategy_type(mm_strat, "MicroSpreadMM"), "market_making")
        self.assertEqual(trader._detect_strategy_type(trend_strat, "EmaTrend"), "trend_following")

    def test_candle_rollover(self):
        trader = MultiStrategyLiveTrader(
            strategy_paths=[],
            product_code="FX_BTC_JPY",
            timeframe="1m"
        )
        trader.df_history = self.dummy_df.copy()
        initial_len = len(trader.df_history)

        # 1. タイムフレーム内 (30秒後) -> バー数変わらず
        t1 = trader.df_history.iloc[-1]["timestamp"] + timedelta(seconds=30)
        df_updated = trader._update_candles(10050000.0, t1)
        self.assertEqual(len(df_updated), initial_len)
        self.assertEqual(df_updated.iloc[-1]["close"], 10050000.0)

        # 2. タイムフレーム経過 (70秒後) -> 新規バー生成
        t2 = trader.df_history.iloc[-1]["timestamp"] + timedelta(seconds=70)
        df_updated = trader._update_candles(10080000.0, t2)
        self.assertEqual(len(df_updated), initial_len + 1)
        self.assertEqual(df_updated.iloc[-1]["close"], 10080000.0)

    def test_strategy_type_exit_behavior(self):
        trader = MultiStrategyLiveTrader(
            strategy_paths=[],
            product_code="FX_BTC_JPY",
            timeframe="1m"
        )
        trader.df_history = self.dummy_df.copy()

        # 手動でMM戦略とトレンド戦略を登録
        trader.strategies["MM"] = {
            "instance": DummyMMStrategy(),
            "strategy_type": "market_making",
            "file_path": "dummy_mm.py",
            "position_btc": 0.001,
            "entry_price": 10000000.0,
            "entry_time": datetime.now() - timedelta(seconds=60),
            "realized_pnl": 0.0,
            "trades_history": [],
            "initial_capital": 100000.0,
            "current_cash": 100000.0,
            "adaptive_manager": None
        }
        trader.strategies["Trend"] = {
            "instance": DummyTrendStrategy(),
            "strategy_type": "trend_following",
            "file_path": "dummy_trend.py",
            "position_btc": 0.001,
            "entry_price": 10000000.0,
            "entry_time": datetime.now() - timedelta(seconds=60),
            "realized_pnl": 0.0,
            "trades_history": [],
            "initial_capital": 100000.0,
            "current_cash": 100000.0,
            "adaptive_manager": None
        }

        # テスト実行時は外部APIフェッチをモックして中立Order Flowに設定
        trader.order_flow.fetch_recent_executions = lambda count=60: []
        trader.order_flow.analyze = lambda window_sec=30.0: trader.order_flow._empty_result()

        # 価格が +0.06% (6,000円幅) 上昇した状態をシミュレート
        trader.fetch_current_price = lambda: 10006000.0
        res = trader.execute_step()

        # MM戦略は +0.05% を超えたため即時利確決済され、trades_count が 1 になる
        mm_stat = [s for s in res["strategies_status"] if s["name"] == "MM"][0]
        self.assertEqual(mm_stat["trades_count"], 1)
        self.assertTrue(any(k in trader.strategies["MM"]["trades_history"][0]["reason"] for k in ["MM利確", "MM即時Cancel脱出"]))

        # トレンド戦略はマイクロTPを行わないためポジション維持 (position=0.001, trades=0)
        trend_stat = [s for s in res["strategies_status"] if s["name"] == "Trend"][0]
        self.assertEqual(trend_stat["position"], 0.001)
        self.assertEqual(trend_stat["trades_count"], 0)

    def test_governance_profile_selection(self):
        gov = StrategyGovernance()
        mm_profile = gov._get_profile("1m", strategy_type="market_making")
        trend_profile = gov._get_profile("1m", strategy_type="trend_following")
        micro_profile = gov._get_profile("1m", strategy_type="micro_trend")

        # MM戦略は高回転のため min_total_trades が 15
        self.assertEqual(mm_profile["min_total_trades"], 15)
        # マイクロトレンド戦略は 10
        self.assertEqual(micro_profile["min_total_trades"], 10)
        # トレンド戦略は厳選エントリーのため min_total_trades が 4
        self.assertEqual(trend_profile["min_total_trades"], 4)


if __name__ == "__main__":
    unittest.main()
