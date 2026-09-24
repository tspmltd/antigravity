"""
tests/test_x_24h_delivery.py: 24h 背骨スロット本文・空欠送・急変 X 枠
"""

import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytz

from news_pipeline.morning_summary import MorningSummaryGenerator
from news_pipeline.scheduler import (
    SCHEDULE_JOBS,
    compose_slot_x_text,
    execute_slot,
    format_slot_content,
)
from news_pipeline.scraper import (
    SLOT_SCRAPERS,
    _quote_from_session_bars,
    format_session_quote_lines,
    has_jp_session_quotes,
    select_intraday_window,
)
from news_pipeline.sekai_kabuka_realtime_sentinel import RealtimeMoverSentinel
from news_pipeline.x_notifier import (
    GLOBAL_DAILY_POST_LIMIT,
    X_BACKBONE_SLOTS,
    X_SLOT_HEADERS,
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
    "09:00": [{"name": "寄り付き", "value": "寄り付き 日経平均: 39,120.00 前日比 +0.40%\n寄り付き TOPIX (ETF): 2,810.00 前日比 +0.25%"}],
    "10:30": [{"name": "前場", "value": "前場 日経平均: 39,200.00 前日比 +0.60% 始値比 +0.20%\n前場 TOPIX (ETF): 2,818.00 前日比 +0.45% 始値比 +0.15%"}],
    "11:30": [{"name": "前場引け", "value": "前場引け 日経平均: 39,050.00 前日比 +0.22% 前場 39,250.00-38,900.00\n前場引け TOPIX (ETF): 2,805.00 前日比 +0.10%"}],
    "12:00": [{"name": "NHK", "value": "日銀会合を来週に控え為替が動意\n内閣支持率が小幅低下"}],
    "12:30": [{"name": "後場寄り", "value": "後場寄り 日経平均: 39,080.00 前日比 +0.30% 前場比 +0.08%\n後場寄り TOPIX (ETF): 2,808.00 前日比 +0.18%"}],
    "14:30": [{"name": "大引け前", "value": "大引け前 日経平均: 38,950.00 前日比 -0.15% 始値比 -0.40% 後場 39,100.00-38,880.00\n大引け前 TOPIX (ETF): 2,790.00 前日比 -0.20%"}],
    "16:00": [{"name": "大引け", "value": "日経平均 39000 -0.40%\nTOPIX 2800 -0.20%"}],
    "17:00": [{"name": "夜間PTS", "value": "6758 ソニーG +1.8%\n6857 アドバンテスト -2.2%"}],
    "19:00": [{"name": "欧州", "value": "DAX 寄り付き +0.30%\nFTSE 小幅安"}],
    "21:30": [{"name": "NY", "value": "ダウ寄り付き +0.30%\nナスダック先物 +0.50%"}],
}

SLOT_HEADERS = {
    "07:00": "【海外市場のまとめ】07:00",
    "07:30": "【海外テック】07:30",
    "08:30": "【PTS・ストップ高安】08:30",
    "09:00": "【寄り付き】09:00",
    "10:30": "【前場】10:30",
    "11:30": "【前場引け】11:30",
    "12:00": "【社会ニュース】12:00",
    "12:30": "【後場寄り】12:30",
    "14:30": "【大引け前】14:30",
    "16:00": "【日本株総括】16:00",
    "17:00": "【夜間PTS】17:00",
    "19:00": "【欧州・アジア】19:00",
    "21:30": "【NY市場寄り付き】21:30",
}

EXPECTED_BACKBONE = {
    "07:00", "07:30", "08:00", "08:30",
    "09:00", "10:30", "11:30", "12:00", "12:30", "14:30",
    "16:00", "17:00", "17:30", "19:00", "21:30",
}


def _session_hist(times_hm, closes, as_of, opens=None, highs=None, lows=None):
    jst = pytz.timezone("Asia/Tokyo")
    idx = [
        jst.localize(datetime(as_of.year, as_of.month, as_of.day, int(hm[:2]), int(hm[3:])))
        for hm in times_hm
    ]
    opens = opens or closes
    highs = highs or [c + 20 for c in closes]
    lows = lows or [c - 20 for c in closes]
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1] * len(closes)},
        index=pd.DatetimeIndex(idx),
    )


class TestX24hDelivery(unittest.TestCase):
    def test_all_scheduled_slots_are_x_backbone(self):
        self.assertEqual(set(X_BACKBONE_SLOTS), EXPECTED_BACKBONE)
        for slot in EXPECTED_BACKBONE:
            self.assertTrue(is_x_backbone_slot(slot))
        self.assertEqual(X_SLOT_HEADERS["16:00"], "【日本株総括】16:00")
        self.assertEqual(X_SLOT_HEADERS["09:00"], "【寄り付き】09:00")
        self.assertEqual(X_SLOT_HEADERS["14:30"], "【大引け前】14:30")

    def test_market_hours_are_on_the_job_list(self):
        times = [row[0] for row in SCHEDULE_JOBS]
        for slot in ("09:00", "10:30", "11:30", "12:00", "12:30", "14:30", "16:00"):
            self.assertIn(slot, times)
        self.assertEqual(times.count("16:00"), 1)
        for slot in ("09:00", "10:30", "11:30", "12:30", "14:30"):
            self.assertIn(slot, SLOT_SCRAPERS)

    def test_slot_bodies_are_distinct(self):
        texts = {}
        for slot, fields in SAMPLE_FIELDS.items():
            texts[slot] = _fmt(slot, "ignored", fields)
            self.assertTrue(texts[slot], msg=f"{slot} should have a body")
            self.assertIn(slot, texts[slot])
            self.assertIn(SLOT_HEADERS[slot], texts[slot])
            self.assertFalse(tweet_contains_url(texts[slot]))
        self.assertEqual(len(set(texts.values())), len(SAMPLE_FIELDS))
        self.assertNotIn("08:00", texts["07:00"])
        self.assertNotIn("日本株寄り前", texts["07:00"])
        self.assertNotEqual(texts["08:30"], texts["17:00"])
        self.assertNotEqual(texts["14:30"], texts["16:00"])
        self.assertNotIn("【日本株総括】16:00", texts["14:30"])
        self.assertIn("寄り付き", texts["09:00"])
        self.assertIn("前場引け", texts["11:30"])
        self.assertIn("後場寄り", texts["12:30"])

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
        self.assertEqual(_fmt("09:00", "寄り付き", []), "")
        self.assertEqual(_fmt("09:00", "寄り付き", [{"name": "x", "value": "情報なし"}]), "")
        self.assertEqual(_fmt("14:30", "大引け前", [{"name": "x", "value": "N/A"}]), "")
        gen = MorningSummaryGenerator()
        self.assertEqual(gen.build_summary(compact_for_x=True, market_data={}), "")

    def test_session_empty_skips_discord_and_x(self):
        with patch.dict(SLOT_SCRAPERS, {"09:00": lambda: {"title": "empty", "indicators": {}}}):
            with patch("news_pipeline.scheduler.send_news_embed") as discord:
                with patch("news_pipeline.scheduler.default_x_notifier.post_tweet") as post:
                    ok = execute_slot("09:00", dry_run=False)
        self.assertTrue(ok)
        discord.assert_not_called()
        post.assert_not_called()

    def test_usd_jpy_only_is_not_a_session_post(self):
        quotes = {"ドル円 (USD/JPY)": {"price": "148.20", "change": "+0.10", "change_pct": "+0.07%"}}
        self.assertFalse(has_jp_session_quotes(quotes))
        embed = format_slot_content("09:00", {"title": "寄り付き", "indicators": quotes})
        self.assertEqual(embed["fields"], [])

    def test_session_format_keeps_phase_labels(self):
        quotes = {
            "日経平均": {
                "price": "39,120.00",
                "change": "+120.00",
                "change_pct": "+0.31%",
                "vs_open_pct": "+0.10%",
                "vs_morning_pct": "+0.05%",
                "session_high": "39,200.00",
                "session_low": "38,900.00",
            }
        }
        self.assertIn("寄り付き 日経平均", format_session_quote_lines(quotes, "09:00"))
        self.assertIn("始値比", format_session_quote_lines(quotes, "10:30"))
        self.assertIn("前場 39,200.00-38,900.00", format_session_quote_lines(quotes, "11:30"))
        self.assertIn("前場比", format_session_quote_lines(quotes, "12:30"))
        self.assertIn("大引け前 日経平均", format_session_quote_lines(quotes, "14:30"))
        self.assertNotIn("情報なし", format_session_quote_lines(quotes, "09:00"))

    def test_intraday_window_skips_when_no_bars(self):
        as_of = datetime(2026, 9, 24).date()
        hist = _session_hist(["09:05", "10:25"], [39000, 39100], as_of)
        self.assertTrue(select_intraday_window(hist, "11:00", "11:45", as_of_date=as_of).empty)
        opened = select_intraday_window(hist, "08:55", "09:25", as_of_date=as_of)
        self.assertEqual(len(opened), 1)

    def test_quote_from_session_bars_skips_without_window(self):
        as_of = datetime(2026, 9, 24).date()
        jst = pytz.timezone("Asia/Tokyo")
        daily_idx = [
            jst.localize(datetime(2026, 9, 22, 15, 0)),
            jst.localize(datetime(2026, 9, 24, 15, 0)),
        ]
        daily = pd.DataFrame(
            {"Open": [38800, 39000], "High": [39200, 39300], "Low": [38700, 38900], "Close": [38900, 39100], "Volume": [1, 1]},
            index=pd.DatetimeIndex(daily_idx),
        )
        intra = _session_hist(["10:25"], [39120], as_of)
        self.assertIsNone(_quote_from_session_bars(daily, intra, "09:00", as_of_date=as_of))
        got = _quote_from_session_bars(daily, intra, "10:30", as_of_date=as_of)
        self.assertIsNotNone(got)
        self.assertNotIn(got["price"], ("N/A", "取得スキップ"))

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
