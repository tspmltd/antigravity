"""
tests/test_news_pipeline_mis_and_tdnet.py: MIS (市場影響度スコア) & TDnet 連携テスト
"""

import unittest
from news_pipeline.market_impact_scorer import (
    MarketEvent,
    MarketImpactScorer,
    build_japan_post,
    get_title_prefix,
    get_hashtags,
    impact_label,
)


class TestMarketImpactScorer(unittest.TestCase):
    """市場影響度スコア (MIS) 計算 & 日本株テンプレ生成テスト"""

    def test_toyota_earnings_surprise(self):
        """決算サプライズ (営業利益+12%, 価格反応+1.03%) -> MIS 70 (High)"""
        ev = MarketEvent(
            event_type="決算短信",
            name="トヨタ",
            symbol="7203",
            headline_metric="営利+12%増",
            source="TDnet / Bloomberg",
            time_str="10:32",
            direction="up",
            op_surprise=12.0,
            price_change=1.03,
            price_label="急伸",
            reason="決算サプライズ（営利+12%）",
        )
        mis = MarketImpactScorer.calculate_mis(ev)
        self.assertGreaterEqual(mis, 70)  # キラーブースト加算
        self.assertIn(impact_label(mis), ["High", "CRITICAL"])

        post = build_japan_post(ev)
        self.assertIsNotNone(post)
        self.assertIn("トヨタ", post)
        self.assertIn("営利+12%", post)
        self.assertIn("#日本株", post)

    def test_nintendo_downward_revision(self):
        """業績予想下方修正 (-18%, 価格反応-2.17%) -> キラーブーストで高MIS (>=80)"""
        ev = MarketEvent(
            event_type="業績修正",
            name="任天堂",
            symbol="7974",
            headline_metric="営利-18%",
            source="TDnet",
            time_str="11:05",
            direction="down",
            revision_rate=-18.0,
            price_change=-2.17,
            price_label="急落",
            reason="主力ゲーム機販売減速",
        )
        mis = MarketImpactScorer.calculate_mis(ev)
        self.assertGreaterEqual(mis, 80)
        self.assertIn(impact_label(mis), ["High", "CRITICAL"])

        post = build_japan_post(ev)
        self.assertIsNotNone(post)
        self.assertIn("任天堂", post)
        self.assertIn("営利-18%", post)

    def test_internal_control_flaw(self):
        """不祥事・内部統制不備 -> キラーブーストで高MIS (>=80)"""
        ev = MarketEvent(
            event_type="不祥事",
            name="ABC商事",
            symbol="9999",
            headline_metric="内部統制報告書に不備",
            source="EDINET",
            time_str="14:20",
            direction="down",
            is_internal_control_flaw=True,
            reason="子会社における不正会計疑惑",
        )
        mis = MarketImpactScorer.calculate_mis(ev)
        self.assertGreaterEqual(mis, 80)

        post = build_japan_post(ev)
        self.assertIsNotNone(post)
        self.assertIn("ABC商事", post)
        self.assertIn("内部統制報告書に不備", post)

    def test_minor_ir_skipped_for_x(self):
        """軽微な適時開示 (役員人事、株式異動等) -> ノイズブラックリストにより MIS=0 / X投稿除外 (None)"""
        ev = MarketEvent(
            event_type="適時開示",
            name="サンプル企業",
            symbol="1234",
            headline_metric="役員の異動に関するお知らせ",
            source="TDnet",
            time_str="15:00",
        )
        mis = MarketImpactScorer.calculate_mis(ev)
        self.assertLess(mis, 40)

        post = build_japan_post(ev)
        self.assertIsNone(post)

    def test_large_ma_post(self):
        """大型M&A (買収金額 150億円) -> MIS >= 45"""
        ev = MarketEvent(
            event_type="M&A",
            name="サンマルクHD",
            symbol="3395",
            headline_metric="「つるとんたん」運営会社を買収",
            source="TDnet / 報道",
            time_str="15:30",
            amount=15_000_000_000,
            reason="外食×うどんチェーンの多角化",
        )
        mis = MarketImpactScorer.calculate_mis(ev)
        self.assertGreaterEqual(mis, 45)

        post = build_japan_post(ev)
        self.assertIsNotNone(post)
        self.assertIn("サンマルクHD", post)
        self.assertIn("「つるとんたん」運営会社を買収", post)

    def test_classify_raw_disclosure(self):
        """開示タイトルの自動分類テスト"""
        t1 = "2026年３月期 第１四半期決算短信〔日本基準〕（連結）"
        e1, d1 = MarketImpactScorer.classify_raw_disclosure(t1)
        self.assertEqual(e1, "決算短信")

        t2 = "通期業績予想の修正に関するお知らせ（上方修正）"
        e2, d2 = MarketImpactScorer.classify_raw_disclosure(t2)
        self.assertEqual(e2, "業績修正")
        self.assertEqual(d2, "up")

        t3 = "内部統制報告書の訂正及び開示すべき重要な不備に関するお知らせ"
        e3, d3 = MarketImpactScorer.classify_raw_disclosure(t3)
        self.assertEqual(e3, "不祥事")
        self.assertEqual(d3, "down")

        t4 = "株式会社長谷川美芸の完全子会社化に関するお知らせ"
        e4, d4 = MarketImpactScorer.classify_raw_disclosure(t4)
        self.assertEqual(e4, "M&A")


if __name__ == "__main__":
    unittest.main()
