"""
tests/test_morning_summary_and_pts.py: 朝8時サマリー & PTSセンチネル & 因果AI 単体テスト
"""

import unittest
from news_pipeline.morning_summary import MorningSummaryGenerator
from news_pipeline.pts_causal_engine import PTSFeatureRecord, PTSCausalEngine
from news_pipeline.pts_sentinel import PTSSentinel


class TestMorningSummaryAndPTS(unittest.TestCase):
    """朝8時サマリー & PTSセンチネルテスト"""

    def test_morning_summary_build(self):
        """朝8時サマリーのテキスト生成 & 構造検証"""
        gen = MorningSummaryGenerator()
        compact_text = gen.build_summary(compact_for_x=True)
        self.assertIn("【海外市場まとめ＋日本株寄り前】", compact_text)
        self.assertIn("📌米国：", compact_text)
        self.assertIn("📌欧州：", compact_text)
        self.assertIn("📌為替：", compact_text)
        self.assertIn("📌日本株：", compact_text)
        self.assertIn("#日本株 #米国株", compact_text)

        # X文字数制限 (半角280以内) チェック
        self.assertLessEqual(len(compact_text), 280)

    def test_pts_causal_engine_direct(self):
        """PTS因果AI: 決算発表直後 + 急騰 -> DIRECT判定"""
        engine = PTSCausalEngine()
        rec = PTSFeatureRecord(
            symbol="7203",
            name="トヨタ",
            disclosure_type="決算",
            disclosure_direction="positive",
            disclosure_strength=12.0,
            disclosure_is_financial=1,
            pts_change_pct=6.8,
            pts_volume_ratio=14.2,
            pts_sustained_minutes=18,
            delta_minutes=15,
            is_large_cap=1,
        )
        label, score, _ = engine.predict_causality(rec)
        self.assertEqual(label, "DIRECT")
        self.assertGreaterEqual(score, 120.0)

        reason = engine.format_causal_reason(label, "営利+12%増決算")
        self.assertIn("TDnet開示連動", reason)
        self.assertIn("主因", reason)

    def test_pts_causal_engine_none(self):
        """PTS因果AI: 開示なし・単なる思惑 -> NONE判定"""
        engine = PTSCausalEngine()
        rec = PTSFeatureRecord(
            symbol="1234",
            name="テスト企業",
            pts_change_pct=3.2,
            pts_volume_ratio=2.0,
            delta_minutes=300,
        )
        label, score, _ = engine.predict_causality(rec)
        self.assertEqual(label, "NONE")
        self.assertLess(score, 50.0)

        reason = engine.format_causal_reason(label)
        self.assertIn("開示連動なし", reason)

    def test_pts_sentinel_formatting(self):
        """PTSセンチネル: X投稿フォーマット生成テスト"""
        sentinel = PTSSentinel(enable_x_post=False, enable_discord=False)
        ev = {
            "symbol": "4436",
            "name": "ミンカブ",
            "change_pct": 20.0,
            "pts_price": 600.0,
            "volume": 20800,
            "volume_ratio": 4.2,
            "time_str": "17:30",
            "causal_label": "DIRECT",
            "causal_reason": "TDnet開示連動（上方修正）が主因",
            "pts_vis2": 150,
        }
        post = sentinel.format_pts_x_post(ev)
        self.assertIn("【PTS急騰】ミンカブ +20.0%（17:30）", post)
        self.assertIn("📍価格：+20.0%急騰（600円）", post)
        self.assertIn("📍出来高：20,800株", post)
        self.assertIn("📍要因：TDnet開示連動（上方修正）が主因", post)
        self.assertIn("市場影響度：CRITICAL（PTS_VIS 150）", post)
        self.assertIn("#日本株 #PTS #夜間取引 #世界の株価", post)


if __name__ == "__main__":
    unittest.main()
