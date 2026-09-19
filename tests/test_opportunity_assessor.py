"""
tests/test_opportunity_assessor.py: 市場機会スコア (OAS) 採点エンジン 単体テスト
=============================================================================
検証項目:
1. TOB/MBO (公開買付): OAS >= 80, TOB_ARBITRAGE
2. 大量保有報告書 (アクティビスト): OAS >= 75, ACTIVIST_FOLLOW
3. 自社株買い (自己株式取得): OAS >= 70, BUYBACK_DRIFT
4. 業績上方修正・好決算: OAS >= 65, EARNINGS_SURPRISE
5. 純粋破壊的リスク (上場廃止・不正会計・粉飾): OAS <= 15, NONE
6. 事務的開示 (ノイズ): OAS == 0, NONE
"""

import unittest
from news_pipeline.market_impact_scorer import MarketEvent
from news_pipeline.opportunity_assessor import OpportunityAssessor


class TestOpportunityAssessor(unittest.TestCase):
    """OAS 採点エンジンの単体テスト"""

    def test_tob_arbitrage_high_oas(self):
        """TOB・公開買付は OAS 80〜98点で SPECIAL_EVENT 対象"""
        event = MarketEvent(
            event_type="TOB",
            name="東京機械製作所",
            symbol="6335",
            headline_metric="公開買付 買付価格2,800円 (プレミアム+25.0%)",
            reason="完全子会社化を目的とした友好的TOB",
        )
        oas, category, rationale = OpportunityAssessor.calculate_oas(event)
        self.assertGreaterEqual(oas, 80)
        self.assertEqual(category, "TOB_ARBITRAGE")
        self.assertIn("TOB", rationale)

    def test_activist_follow_oas(self):
        """著名アクティビストによる大量保有・買い増しは OAS 75〜90点"""
        event = MarketEvent(
            event_type="大量保有",
            name="東洋建設",
            symbol="1890",
            headline_metric="エフィッシモ・キャピタル 保有割合7.5%へ買い増し",
            reason="変更報告書 (5%ルール) 重要提案行為",
        )
        oas, category, rationale = OpportunityAssessor.calculate_oas(event)
        self.assertGreaterEqual(oas, 75)
        self.assertEqual(category, "ACTIVIST_FOLLOW")

    def test_buyback_drift_oas(self):
        """大規模自社株買いは OAS 70〜88点"""
        event = MarketEvent(
            event_type="自社株買い",
            name="ソニーグループ",
            symbol="6758",
            headline_metric="発行済株式の3.5% (上限1000億円) 自己株式取得",
            reason="株主還元および資本効率向上",
            amount=100_000_000_000.0,
        )
        oas, category, rationale = OpportunityAssessor.calculate_oas(event)
        self.assertGreaterEqual(oas, 70)
        self.assertEqual(category, "BUYBACK_DRIFT")

    def test_earnings_surprise_oas(self):
        """好決算・上方修正は OAS 65〜85点"""
        event = MarketEvent(
            event_type="業績修正",
            name="トヨタ自動車",
            symbol="7203",
            headline_metric="通期営業利益+25%上方修正 過去最高益更新",
            direction="up",
        )
        oas, category, rationale = OpportunityAssessor.calculate_oas(event)
        self.assertGreaterEqual(oas, 65)
        self.assertEqual(category, "EARNINGS_SURPRISE")

    def test_destructive_pure_risk_low_oas(self):
        """上場廃止・粉飾・不正会計等の純粋リスクは収益機会希薄 (OAS <= 15)"""
        event = MarketEvent(
            event_type="上場廃止",
            name="破綻企業",
            symbol="9999",
            headline_metric="特設注意市場銘柄指定に伴う上場廃止決定",
            is_scandal=True,
        )
        oas, category, rationale = OpportunityAssessor.calculate_oas(event)
        self.assertLessEqual(oas, 15)
        self.assertEqual(category, "NONE")

    def test_noise_disclosure_zero_oas(self):
        """事務的開示 (役員異動・定款変更等) は OAS == 0"""
        event = MarketEvent(
            event_type="事務的開示",
            name="任天堂",
            symbol="7974",
            headline_metric="定款一部変更に関するお知らせ",
        )
        oas, category, rationale = OpportunityAssessor.calculate_oas(event)
        self.assertEqual(oas, 0)
        self.assertEqual(category, "NONE")


if __name__ == "__main__":
    unittest.main()
