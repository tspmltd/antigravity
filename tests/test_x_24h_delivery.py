"""
tests/test_x_24h_delivery.py: 24h 背骨スロット本文・空欠送・急変 X 枠
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from news_pipeline.morning_summary import MorningSummaryGenerator
from news_pipeline.scheduler import compose_slot_x_text
from news_pipeline.sekai_kabuka_realtime_sentinel import RealtimeMoverSentinel
from news_pipeline.x_notifier import (
    GLOBAL_DAILY_POST_LIMIT,
    X_BACKBONE_SLOTS,
    XNotifier,
    is_x_backbone_slot,
    strip_tweet_urls,
    tweet_contains_url,
)


def _fmt(slot, title, fields):
    return XNotifier(
        api_key="k", api_secret="s", access_token="t", access_token_secret="ts"
    ).format_news_for_x(slot, title, fields)


class _FakeResponse:
    def __init__(self, status_code=201, tweet_id="tw-1", text=""):
        self.status_code = status_code
        self._tweet_id = tweet_id
        self.text = text or json.dumps({"data": {"id": tweet_id}})

    def json(self):
        return {"data": {"id": self._tweet_id}}


SAMPLE_FIELDS = {
    "07:00": [{"name": "指標", "value": "🔺 **Nasdaq**: `18000` (+1.20%)\n🔺 **S&P 500**: `5200` (+0.80%)"}],
    "07:30": [{"name": "テック", "value": "NVDA +2.1%\nAAPL -0.4%"}],
    "08:30": [{"name": "PTS上昇", "value": "7203 トヨタ +5.2%\n9984 ソフトバンクG +3.1%"}],
    "12:00": [{"name": "NHK", "value": "日銀会合を来週に控え為替が動意\n内閣支持率が小幅低下"}],
    "16:00": [{"name": "大引け", "value": "日経平均 39000 -0.40%\nTOPIX 2800 -0.20%"}],
    "17:00": [{"name": "夜間PTS", "value": "6758 ソニーG +1.8%\n6857 アドバンテスト -2.2%"}],
    "19:00": [{"name": "欧州", "value": "DAX 寄り付き +0.30%\nFTSE 小幅安"}],
    "21:30": [{"name": "NY", "value": "ダウ寄り付き +0.30%\nナスダック先物 +0.50%"}],
}

SLOT_HEADERS = {
    "07:00": "【海外市場のまとめ】07:00",
    "07:30": "【海外テック】07:30",
    "08:30": "【PTS・ストップ高安】08:30",
    "12:00": "【社会ニュース】12:00",
    "16:00": "【日本株総括】16:00",
    "17:00": "【夜間PTS】17:00",
    "19:00": "【欧州・アジア】19:00",
    "21:30": "【NY市場寄り付き】21:30",
}


class TestX24hDelivery(unittest.TestCase):
    def test_all_ten_slots_are_x_backbone(self):
        expected = {
            "07:00", "07:30", "08:00", "08:30", "12:00",
            "16:00", "17:00", "17:30", "19:00", "21:30",
        }
        self.assertEqual(set(X_BACKBONE_SLOTS), expected)
        for slot in expected:
            self.assertTrue(is_x_backbone_slot(slot))

    def test_slot_bodies_are_distinct(self):
        texts = {}
        for slot, fields in SAMPLE_FIELDS.items():
            texts[slot] = _fmt(slot, "ignored", fields)
            self.assertTrue(texts[slot], msg=f"{slot} should have a body")
            self.assertIn(slot, texts[slot])
            self.assertIn(SLOT_HEADERS[slot], texts[slot])
        self.assertEqual(len(set(texts.values())), len(SAMPLE_FIELDS))
        self.assertNotIn("08:00", texts["07:00"])
        self.assertNotIn("日本株寄り前", texts["07:00"])
        self.assertNotEqual(texts["08:30"], texts["17:00"])

    def test_0700_compose_is_overseas_close_not_morning_summary(self):
        text = compose_slot_x_text(
            "07:00",
            {"title": "🌅 海外市場のまとめ (07:00 JST)", "fields": SAMPLE_FIELDS["07:00"]},
        )
        self.assertIn("【海外市場のまとめ】07:00", text)
        self.assertNotIn("【日本株寄り前】08:00", text)
        self.assertNotIn("朝8時", text)

    def test_0730_compose_is_not_empty(self):
        text = compose_slot_x_text("07:30", {"title": "海外テック", "fields": SAMPLE_FIELDS["07:30"]})
        self.assertIn("【海外テック】07:30", text)

    def test_0800_morning_summary_has_slot_identity(self):
        gen = MorningSummaryGenerator()
        market = {
            "us_nasdaq": {"pct": 1.2, "last": 18000, "diff": 200},
            "nikkei_fut": {"pct": 0.35, "last": 39000, "diff": 100},
        }
        text = gen.build_summary(compact_for_x=True, market_data=market)
        self.assertIn("【日本株寄り前】08:00", text)
        self.assertIn("NASDAQ", text)
        self.assertNotIn("ハイテク株主導", text)
        self.assertNotIn("米主要ハイテク株の業績発表とガイダンス", text)
        self.assertFalse(tweet_contains_url(text))

    def test_skip_on_empty_fields(self):
        self.assertEqual(_fmt("07:00", "海外", []), "")
        self.assertEqual(_fmt("07:00", "海外", [{"name": "x", "value": "情報なし"}]), "")
        self.assertEqual(_fmt("16:00", "総括", [{"name": "x", "value": "情報なし (開示待ち)"}]), "")
        self.assertEqual(_fmt("12:00", "社会", [{"name": "x", "value": "情報なし"}]), "")
        gen = MorningSummaryGenerator()
        self.assertEqual(gen.build_summary(compact_for_x=True, market_data={}), "")

    def test_urls_stripped_before_post(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        notifier = XNotifier(
            api_key="k",
            api_secret="s",
            access_token="t",
            access_token_secret="ts",
            daily_limit=48,
            daily_state_path=os.path.join(tmp.name, "daily.json"),
            sent_state_path=os.path.join(tmp.name, "sent.json"),
        )
        oauth = MagicMock()
        oauth.post.return_value = _FakeResponse(tweet_id="tw-1")
        notifier._get_oauth_session = lambda: oauth

        raw = "【海外市場のまとめ】07:00\n・Nasdaq +1.2%\n詳しくは https://example.com/a を参照\n\n#米国株"
        self.assertTrue(tweet_contains_url(raw))
        tid = notifier.post_tweet(raw)
        self.assertEqual(tid, "tw-1")
        sent_text = oauth.post.call_args.kwargs["json"]["text"]
        self.assertFalse(tweet_contains_url(sent_text))
        self.assertNotIn("https://", sent_text)
        self.assertEqual(strip_tweet_urls(raw)[:280], sent_text)

    def test_global_limit_stays_48_and_mover_cap_is_12(self):
        self.assertEqual(GLOBAL_DAILY_POST_LIMIT, 48)
        from news_pipeline.x_notifier import DEFAULT_MOVER_X_BUSY_DAILY_CAP
        self.assertEqual(DEFAULT_MOVER_X_BUSY_DAILY_CAP, 12)
        sentinel = RealtimeMoverSentinel.__new__(RealtimeMoverSentinel)
        sentinel.max_daily_x = 12
        sentinel.x_posted_states = {}
        self.assertEqual(sentinel.remaining_mover_x_posts(0), 12)
        self.assertEqual(sentinel.remaining_mover_x_posts(8), 4)
        self.assertEqual(sentinel.remaining_mover_x_posts(12), 0)
        self.assertEqual(sentinel.remaining_mover_x_posts(20), 0)


if __name__ == "__main__":
    unittest.main()
