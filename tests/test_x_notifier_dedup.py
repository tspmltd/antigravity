"""
tests/test_x_notifier_dedup.py: X配信ゲートの同一本文再送禁止
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from news_pipeline.x_notifier import XNotifier, tweet_content_key


class _FakeResponse:
    def __init__(self, status_code=201, tweet_id="tw-1", text=""):
        self.status_code = status_code
        self._tweet_id = tweet_id
        self.text = text or json.dumps({"data": {"id": tweet_id}})

    def json(self):
        return {"data": {"id": self._tweet_id}}


class TestXNotifierDedup(unittest.TestCase):
    """同一 tweet text を二度送らないことの検証"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.daily_path = os.path.join(self._tmpdir.name, "daily.json")
        self.sent_path = os.path.join(self._tmpdir.name, "sent.json")
        self.notifier = XNotifier(
            api_key="k",
            api_secret="s",
            access_token="t",
            access_token_secret="ts",
            daily_limit=48,
            daily_state_path=self.daily_path,
            sent_state_path=self.sent_path,
        )
        self.oauth = MagicMock()
        self.oauth.post.return_value = _FakeResponse(tweet_id="tw-1")
        self.notifier._get_oauth_session = lambda: self.oauth

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_same_exact_text_is_not_posted_twice(self):
        text = "【開示速報】トヨタ／決算\n📌要約：営利+12%\n\n#日本株"
        first = self.notifier.post_tweet(text)
        second = self.notifier.post_tweet(text)

        self.assertEqual(first, "tw-1")
        self.assertIsNone(second)
        self.assertEqual(self.oauth.post.call_count, 1)
        self.assertTrue(self.notifier.already_sent(text[:280]))

    def test_different_text_is_posted(self):
        self.oauth.post.side_effect = [
            _FakeResponse(tweet_id="tw-1"),
            _FakeResponse(tweet_id="tw-2"),
        ]
        first = self.notifier.post_tweet("本文A")
        second = self.notifier.post_tweet("本文B")

        self.assertEqual(first, "tw-1")
        self.assertEqual(second, "tw-2")
        self.assertEqual(self.oauth.post.call_count, 2)

    def test_whitespace_only_difference_is_not_same(self):
        """conservative exact match: 空白差は別本文として扱う"""
        self.oauth.post.side_effect = [
            _FakeResponse(tweet_id="tw-1"),
            _FakeResponse(tweet_id="tw-2"),
        ]
        first = self.notifier.post_tweet("本文A")
        second = self.notifier.post_tweet("本文A ")
        self.assertEqual(first, "tw-1")
        self.assertEqual(second, "tw-2")

    def test_sent_record_survives_new_notifier(self):
        self.notifier.post_tweet("永続本文")
        other = XNotifier(
            api_key="k",
            api_secret="s",
            access_token="t",
            access_token_secret="ts",
            daily_state_path=self.daily_path,
            sent_state_path=self.sent_path,
        )
        other._get_oauth_session = lambda: self.oauth
        skipped = other.post_tweet("永続本文")
        self.assertIsNone(skipped)
        self.assertEqual(self.oauth.post.call_count, 1)

    def test_duplicate_api_response_records_text(self):
        self.oauth.post.return_value = _FakeResponse(
            status_code=403,
            tweet_id=None,
            text='{"detail":"duplicate content"}',
        )
        result = self.notifier.post_tweet("重複扱い本文")
        self.assertIsNone(result)
        self.assertTrue(self.notifier.already_sent("重複扱い本文"))
        self.assertEqual(self.oauth.post.call_count, 1)

        self.oauth.post.return_value = _FakeResponse(tweet_id="should-not-post")
        again = self.notifier.post_tweet("重複扱い本文")
        self.assertIsNone(again)
        self.assertEqual(self.oauth.post.call_count, 1)

    def test_empty_text_skipped_without_record(self):
        result = self.notifier.post_tweet("   ")
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(self.sent_path))
        self.oauth.post.assert_not_called()

    def test_duplicate_does_not_increment_daily_count(self):
        self.notifier.post_tweet("カウント対象")
        self.assertEqual(self.notifier._get_daily_count(), 1)
        self.notifier.post_tweet("カウント対象")
        self.assertEqual(self.notifier._get_daily_count(), 1)

    def test_content_key_is_sha256_of_exact_text(self):
        text = "exact"
        self.assertEqual(tweet_content_key(text), tweet_content_key("exact"))
        self.assertNotEqual(tweet_content_key(text), tweet_content_key("exact "))


if __name__ == "__main__":
    unittest.main()
