"""
tests/test_alpha_opportunity_engine.py: 第4階層 Alpha Opportunity Engine 単体テスト
=================================================================================
検証項目:
1. ForecastAgent: TOB収束価格、自社株買いドリフト、業績サプライズPEADの予測精度
2. StrategyAgent: PEG_v2指値/TWAP/成行 スタイル選定、動的ロット、利確/損切りライン
3. AlphaOpportunityEngine: 二次元判定 (MIS & OAS) からの自律プラン起草パイプライン
"""

import unittest
from antigravity.multi_asset.schemas import MacroImpact
from news_pipeline.market_impact_scorer import MarketEvent
from antigravity.multi_asset.alpha_opportunity_engine import (
    ForecastAgent,
    StrategyAgent,
    AlphaOpportunityEngine,
)


class TestAlphaOpportunityEngine(unittest.TestCase):
    """第4階層 Alpha Opportunity Engine の単体テスト"""

    def setUp(self):
        self.engine = AlphaOpportunityEngine()

    def test_forecast_tob_arbitrage(self):
        """TOBイベントに対する未織り込みアービトラージ予測"""
        event = MarketEvent(
            event_type="TOB",
            name="東京機械",
            symbol="6335",
            headline_metric="公開買付 買付価格2500円 (プレミアム+25.0%)",
        )
        macro = MacroImpact(
            impact_score=75,
            level="WARNING",
            primary_event="TOB 公開買付",
            global_regime="SPECIAL_EVENT",
            opportunity_score=95,
            opportunity_type="TOB_ARBITRAGE",
            is_special_event=True,
        )
        forecast = ForecastAgent.generate_forecast(event, macro, current_market_price=2000.0)
        self.assertIsNotNone(forecast)
        self.assertEqual(forecast.symbol, "6335")
        self.assertEqual(forecast.opportunity_type, "TOB_ARBITRAGE")
        self.assertEqual(forecast.target_price, 2500.0)
        self.assertGreater(forecast.expected_return_bp, 2000.0)  # +25% = 2500bp
        self.assertEqual(forecast.confidence, 95.0)

    def test_strategy_tob_peg_v2(self):
        """TOBアービトラージに対する PEG_v2 執行プラン起草"""
        event = MarketEvent(
            event_type="TOB",
            name="東京機械",
            symbol="6335",
            headline_metric="買付価格2500円 (プレミアム+20.0%)",
        )
        macro = MacroImpact(
            impact_score=75,
            level="WARNING",
            primary_event="TOB",
            opportunity_score=90,
            opportunity_type="TOB_ARBITRAGE",
            is_special_event=True,
        )
        forecast = ForecastAgent.generate_forecast(event, macro, current_market_price=2000.0)
        plan = StrategyAgent.formulate_plan(forecast, current_price=2000.0)

        self.assertIsNotNone(plan)
        self.assertEqual(plan.order_style, "PEG_v2")
        self.assertEqual(plan.target_mode, "SPECIAL_EVENT")
        self.assertEqual(plan.action, "BUY")
        self.assertEqual(plan.target_size, 500.0)
        self.assertGreater(plan.take_profit_bp, 1000.0)
        self.assertEqual(plan.stop_loss_bp, 150.0)

    def test_buyback_drift_pipeline(self):
        """自社株買いイベントに対する ALPHA_ACCUMULATE 執行プラン起草"""
        event = MarketEvent(
            event_type="自社株買い",
            name="ソニーグループ",
            symbol="6758",
            headline_metric="発行済株式の4.0% 自己株式取得",
        )
        macro = MacroImpact(
            impact_score=55,
            level="NORMAL",
            primary_event="自社株買い",
            opportunity_score=80,
            opportunity_type="BUYBACK_DRIFT",
            is_special_event=False,
        )
        forecast, plan = self.engine.evaluate_opportunity(event, macro, current_market_price=3000.0)

        self.assertIsNotNone(forecast)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.order_style, "LIMIT")
        self.assertEqual(plan.target_mode, "ALPHA_ACCUMULATE")
        self.assertGreater(forecast.expected_return_bp, 100.0)
        self.assertEqual(plan.target_size, 300.0)

    def test_low_oas_event_skipped(self):
        """OASが低いイベント (上場廃止・ノイズ等) ではアルファ執行プランを生成しない"""
        event = MarketEvent(
            event_type="上場廃止",
            name="破綻企業",
            symbol="9999",
            headline_metric="上場廃止決定",
        )
        macro = MacroImpact(
            impact_score=90,
            level="CRITICAL",
            primary_event="上場廃止",
            opportunity_score=10,
            opportunity_type="NONE",
            is_special_event=False,
        )
        forecast, plan = self.engine.evaluate_opportunity(event, macro)
        self.assertIsNone(forecast)
        self.assertIsNone(plan)


if __name__ == "__main__":
    unittest.main()
