"""
tests/test_x_filter_and_boost.py: Xフォロワー増加のためのニュース絞り込み & ノイズ遮断検証テスト
"""

import unittest
from news_pipeline.market_impact_scorer import MarketImpactScorer, MarketEvent, build_japan_post


class TestXFilterAndBoostSuite(unittest.TestCase):
    """X投稿フィルタリング・エンゲージメント最適化の総合検証"""

    def test_01_noise_administrative_disclosures_completely_blocked(self):
        """株価への影響がない事務的開示・ノイズが確実に完全遮断 (MIS=0 & X投稿None) されることの検証"""
        noise_titles = [
            "定款一部変更に関するお知らせ",
            "定款の変更に関するお知らせ",
            "代表取締役の異動（辞任予定）に関するお知らせ",
            "人事異動に関するお知らせ",
            "執行役員選任に関するお知らせ",
            "本社移転に関するお知らせ",
            "本社の移転について",
            "第20回新株予約権（行使価額修正条項付）の大量行使に関するお知らせ",
            "新株予約権の行使状況に関するお知らせ",
            "譲渡制限付株式報酬としての自己株式処分の払込完了に関するお知らせ",
            "支配株主等に関する事項について",
            "資金の借入に関するお知らせ",
            "コミットメントライン契約及びアンコミットメントライン契約の期間延長に関するお知らせ",
            "第2四半期決算説明会 質疑応答要旨",
            "決算説明会 書き起こし",
            "事業計画及び成長可能性に関する事項",
            "1～100件 / 全238件前へ123次へ",
        ]

        for title in noise_titles:
            self.assertTrue(MarketImpactScorer.is_noise_disclosure(title), f"Must be recognized as noise: {title}")
            event_type, direction = MarketImpactScorer.classify_raw_disclosure(title)
            self.assertEqual(event_type, "事務的開示", f"Must be classified as 事務的開示: {title}")

            ev = MarketEvent(
                event_type=event_type,
                name="テスト企業",
                symbol="9999",
                source="TDnet",
                time_str="15:30",
                direction=direction,
            )
            mis = MarketImpactScorer.calculate_mis(ev)
            self.assertEqual(mis, 0, f"MIS must be 0 for noise: {title}")

            x_text = build_japan_post(ev)
            self.assertIsNone(x_text, f"X post must be None for noise: {title}")

    def test_02_killer_high_engagement_news_pass_with_boost(self):
        """フォロワー増加に直結するキラー材料がブーストされて高スコア合格し、魅力的なX投稿が生成されることの検証"""
        killer_cases = [
            {
                "title": "通期業績予想の修正（大幅上方修正）に関するお知らせ",
                "name": "ソニーG",
                "symbol": "6758",
                "expected_type": "業績修正",
                "expected_dir": "up",
                "min_mis": 75,
                "expected_prefix": "🚀【業績上方修正】",
            },
            {
                "title": "自己株式取得に係る事項の決定（自社株買い）",
                "name": "トヨタ",
                "symbol": "7203",
                "expected_type": "自社株買い",
                "expected_dir": "up",
                "min_mis": 65,
                "expected_prefix": "💎【自社株買い速報】",
            },
            {
                "title": "剰余金の配当（大幅増配・記念配当）に関するお知らせ",
                "name": "アドバンテスト",
                "symbol": "6857",
                "expected_type": "増配",
                "expected_dir": "up",
                "min_mis": 65,
                "expected_prefix": "💰【増配速報】",
            },
            {
                "title": "株式会社〇〇に対する公開買付けの開始（TOB）に関するお知らせ",
                "name": "ＳＢＧ",
                "symbol": "9984",
                "expected_type": "TOB",
                "expected_dir": "up",
                "min_mis": 75,
                "expected_prefix": "🎯【TOB・公開買付速報】",
            },
            {
                "title": "特別調査委員会の設置及び不正会計の疑義に関するお知らせ",
                "name": "三洋貿易",
                "symbol": "3176",
                "expected_type": "不祥事",
                "expected_dir": "down",
                "min_mis": 75,
                "expected_prefix": "🛑【緊急・重要開示】",
            },
        ]

        for case in killer_cases:
            event_type, direction = MarketImpactScorer.classify_raw_disclosure(case["title"])
            self.assertEqual(event_type, case["expected_type"], f"Type match for {case['title']}")
            if case["expected_dir"]:
                self.assertEqual(direction, case["expected_dir"])

            ev = MarketEvent(
                event_type=event_type,
                name=case["name"],
                symbol=case["symbol"],
                source="TDnet",
                time_str="15:30",
                direction=direction,
            )
            mis = MarketImpactScorer.calculate_mis(ev)
            ev.mis = mis
            self.assertGreaterEqual(mis, case["min_mis"], f"MIS must be >= {case['min_mis']} for {case['name']}")

            x_text = build_japan_post(ev)
            self.assertIsNotNone(x_text, f"X post must NOT be None for {case['name']}")
            self.assertIn(case["expected_prefix"], x_text)
            self.assertIn(case["name"], x_text)
            self.assertIn("#日本株", x_text)
            self.assertIn("#株クラ", x_text)

    def test_03_pts_night_filtering(self):
        """PTS夜間フィルターが薄商いノイズを弾き、5%急変・大商いを合格させることの検証"""
        from news_pipeline.pts_sentinel import PTSSentinel
        sentinel = PTSSentinel(enable_x_post=True)
        sentinel.daily_x_count = 0  # テスト用に日次カウントをリセット

        # ケースA: 薄商いノイズ (出来高500株, 変動+2.0%, VIS 40) -> 却下
        thin_noise = {
            "symbol": "1111",
            "name": "薄商い株",
            "volume": 500,
            "volume_ratio": 0.8,
            "pts_vis2": 40,
            "causal_label": "UNKNOWN",
            "change_pct": 2.0,
        }
        self.assertFalse(sentinel.should_post_to_x(thin_noise))

        # ケースB: 本物の急変 (出来高50,000株, 変動+8.5%, VIS 85, DIRECT因果) -> 合格
        real_mover = {
            "symbol": "9984",
            "name": "ＳＢＧ",
            "volume": 50000,
            "volume_ratio": 4.5,
            "pts_vis2": 85,
            "causal_label": "DIRECT",
            "change_pct": 8.5,
        }
        self.assertTrue(sentinel.should_post_to_x(real_mover))

        # ケースC: 日次上限到達時 -> 却下
        sentinel.daily_x_count = sentinel.max_daily_x
        self.assertFalse(sentinel.should_post_to_x(real_mover))

    def test_04_x_global_daily_cap(self):
        """XNotifierの無料枠保護グローバル日次上限ガードの検証"""
        from news_pipeline.x_notifier import XNotifier
        notifier = XNotifier(daily_limit=10)
        self.assertEqual(notifier.daily_limit, 10)
        # 残り枠数の確認
        remaining = notifier.get_remaining_daily_posts()
        self.assertIsInstance(remaining, int)
        self.assertGreaterEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
