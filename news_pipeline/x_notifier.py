"""
X (旧Twitter) API v2 自動投稿モジュール (x_notifier.py)
=========================================================
- API v2 POST /2/tweets によるツイート投稿
- Twitter v1.1 media/upload によるチャート画像添付対応
- OAuth 1.0a User Context 認証 (requests-oauthlib)
- 日本語140文字制限に最適化したスマート要約 & ハッシュタグ自動付与
- 未設定時の安全スキップ & エラーハンドリング (Discord送信への波及防止)
- 同一本文の再送禁止 (投稿成功済みの exact tweet text を永続記録)
"""

import os
import hashlib
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("news_pipeline.x_notifier")

# X (Twitter) API エンドポイント
X_TWEET_API_URL = "https://api.twitter.com/2/tweets"
X_MEDIA_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"

JST = timezone(timedelta(hours=9))
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
X_DAILY_STATE_PATH = os.path.join(_DATA_DIR, "x_global_daily_state.json")
# 配信ゲートの「送信済み本文」記録。同一本文の再投稿を遮断する唯一のソース。
X_SENT_POSTS_PATH = os.path.join(_DATA_DIR, "x_sent_posts.json")
X_SENT_POSTS_MAX = 2000
GLOBAL_DAILY_POST_LIMIT = int(os.getenv("X_GLOBAL_DAILY_LIMIT", "48"))  # X無料枠(月1500件=日平均50件)の安全上限


def tweet_content_key(text: str) -> str:
    """投稿本文の identity。exact text の SHA-256 (正規化・曖昧一致はしない)。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class XNotifier:
    """X (旧Twitter) API v2 投稿クライアント"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        access_token_secret: Optional[str] = None,
        daily_limit: int = GLOBAL_DAILY_POST_LIMIT,
        daily_state_path: Optional[str] = None,
        sent_state_path: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("X_API_KEY", "").strip() or os.getenv("TWITTER_API_KEY", "").strip()
        self.api_secret = (
            api_secret
            or os.getenv("X_API_KEY_SECRET", "").strip()
            or os.getenv("X_API_SECRET", "").strip()
            or os.getenv("TWITTER_API_SECRET", "").strip()
        )
        self.access_token = access_token or os.getenv("X_ACCESS_TOKEN", "").strip() or os.getenv("TWITTER_ACCESS_TOKEN", "").strip()
        self.access_token_secret = access_token_secret or os.getenv("X_ACCESS_TOKEN_SECRET", "").strip() or os.getenv("TWITTER_ACCESS_TOKEN_SECRET", "").strip()
        self.daily_limit = daily_limit
        self.daily_state_path = daily_state_path or os.getenv("X_DAILY_STATE_PATH", "").strip() or X_DAILY_STATE_PATH
        self.sent_state_path = sent_state_path or os.getenv("X_SENT_POSTS_PATH", "").strip() or X_SENT_POSTS_PATH

    def _get_daily_count(self) -> int:
        """当日のX投稿累計数を取得 (日付変更時は0リセット)"""
        today_str = datetime.now(JST).strftime("%Y-%m-%d")
        if not os.path.exists(self.daily_state_path):
            return 0
        try:
            with open(self.daily_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today_str:
                return int(data.get("count", 0))
            return 0
        except Exception:
            return 0

    def _increment_daily_count(self) -> int:
        """当日のX投稿累計数をインクリメントして永続化"""
        today_str = datetime.now(JST).strftime("%Y-%m-%d")
        current_count = self._get_daily_count() + 1
        os.makedirs(os.path.dirname(self.daily_state_path) or ".", exist_ok=True)
        try:
            with open(self.daily_state_path, "w", encoding="utf-8") as f:
                json.dump({"date": today_str, "count": current_count, "limit": self.daily_limit}, f, indent=2)
        except Exception as e:
            logger.warning(f"[XNotifier] 日次カウンター永続化失敗: {e}")
        return current_count

    def _load_sent_posts(self) -> Dict[str, Any]:
        """送信済み本文レコードをロード。by_hash が identity の唯一のソース。"""
        if not os.path.exists(self.sent_state_path):
            return {"by_hash": {}}
        try:
            with open(self.sent_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("by_hash"), dict):
                return data
        except Exception as e:
            logger.warning(f"[XNotifier] 送信済み本文ロード失敗: {e}")
        return {"by_hash": {}}

    def _save_sent_posts(self, data: Dict[str, Any]) -> None:
        by_hash = data.get("by_hash") or {}
        if len(by_hash) > X_SENT_POSTS_MAX:
            ordered = sorted(
                by_hash.items(),
                key=lambda item: (item[1] or {}).get("sent_at", ""),
            )
            by_hash = dict(ordered[-X_SENT_POSTS_MAX:])
            data["by_hash"] = by_hash
        os.makedirs(os.path.dirname(self.sent_state_path) or ".", exist_ok=True)
        try:
            with open(self.sent_state_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[XNotifier] 送信済み本文保存失敗: {e}")

    def already_sent(self, text: str) -> bool:
        """同一本文 (exact tweet text) を既に配信済みか。"""
        if not text:
            return False
        key = tweet_content_key(text)
        return key in self._load_sent_posts().get("by_hash", {})

    def _record_sent(self, text: str, tweet_id: Optional[str] = None) -> None:
        """投稿成功 (または X API が duplicate と返した) 本文を記録する。"""
        key = tweet_content_key(text)
        data = self._load_sent_posts()
        data.setdefault("by_hash", {})[key] = {
            "text": text,
            "tweet_id": tweet_id,
            "sent_at": datetime.now(JST).isoformat(),
        }
        self._save_sent_posts(data)

    def get_remaining_daily_posts(self) -> int:
        """本日の残り投稿可能枠数を取得"""
        return max(0, self.daily_limit - self._get_daily_count())

    def is_configured(self) -> bool:
        """必要な4つのAPIクレデンシャルが全て設定されているか判定"""
        return bool(self.api_key and self.api_secret and self.access_token and self.access_token_secret)

    def _get_oauth_session(self):
        try:
            from requests_oauthlib import OAuth1Session
            return OAuth1Session(
                client_key=self.api_key,
                client_secret=self.api_secret,
                resource_owner_key=self.access_token,
                resource_owner_secret=self.access_token_secret,
            )
        except ImportError:
            logger.error("[XNotifier] requests_oauthlib がインストールされていません。")
            return None

    def upload_media(self, image_bytes: bytes) -> Optional[str]:
        """Twitter v1.1 media/upload で画像をアップロードして media_id を取得"""
        if not self.is_configured() or not image_bytes:
            return None

        oauth = self._get_oauth_session()
        if not oauth:
            return None

        try:
            files = {"media": ("chart.png", image_bytes, "image/png")}
            data = {"media_category": "tweet_image"}
            response = oauth.post(X_MEDIA_UPLOAD_URL, files=files, data=data, timeout=20)
            if response.status_code in (200, 201, 202):
                media_id = response.json().get("media_id_string")
                logger.info(f"[XNotifier] 🖼️ 画像アップロード成功 (Media ID: {media_id})")
                return media_id
            else:
                logger.warning(f"[XNotifier] 画像アップロード非対応/失敗 (HTTP {response.status_code}): {response.text}")
                return None
        except Exception as e:
            logger.warning(f"[XNotifier] 画像アップロード例外: {e}")
            return None

    def post_tweet(self, text: str, media_id: Optional[str] = None, _is_retry: bool = False) -> Optional[str]:
        """
        ツイートを投稿する (画像添付対応)。
        X無料枠(1,500件/月 = 日50件)保護のため、日次上限(48件)に達している場合は安全に遮断。
        同一本文 (exact tweet text) は送信済み記録と照合し、再送しない。
        :param text: 投稿本文 (最大140文字推奨)
        :param media_id: アップロード済み画像ID (オプション)
        :param _is_retry: リトライフラグ (内部用)
        :return: 投稿成功時の tweet_id (失敗時・重複スキップ時は None)
        """
        if not self.is_configured():
            logger.info("[XNotifier] X APIクレデンシャルが未設定のため、X投稿をスキップします。")
            return None

        trimmed_text = (text or "")[:280]
        if not trimmed_text.strip():
            logger.info("[XNotifier] 投稿本文が空のため、X投稿をスキップします。")
            return None

        # 同一本文の再送禁止 (配信ゲートの唯一の identity = exact tweet text)
        if self.already_sent(trimmed_text):
            logger.info("[XNotifier] 同一本文は既にXへ配信済みのため、再投稿をスキップします。")
            return None

        # 無料枠上限保護 (日次最大48件ガード)
        if not _is_retry and self.get_remaining_daily_posts() <= 0:
            logger.warning(
                f"[XNotifier] 🛑 X API無料枠保護作動: 本日の全体投稿上限({self.daily_limit}件)に達したため、投稿を安全に遮断しました。"
            )
            return None

        oauth = self._get_oauth_session()
        if not oauth:
            return None

        payload: Dict[str, Any] = {"text": trimmed_text}
        if media_id:
            payload["media"] = {"media_ids": [media_id]}

        try:
            response = oauth.post(X_TWEET_API_URL, json=payload, timeout=15)
            if response.status_code in (200, 201):
                data = response.json().get("data", {})
                tweet_id = data.get("id")
                self._record_sent(trimmed_text, tweet_id)
                new_cnt = self._increment_daily_count()
                logger.info(f"[XNotifier] 🐦 Xへのツイート投稿に成功しました (ID: {tweet_id}, 本日累計: {new_cnt}/{self.daily_limit})")
                return tweet_id
            else:
                logger.error(f"[XNotifier] ⚠️ X投稿失敗 (HTTP {response.status_code}, Media={bool(media_id)}): {response.text}")
                # X側が duplicate と判定した場合は本文を送信済みとして記録し、再試行しない
                if "duplicate" in (response.text or "").lower():
                    self._record_sent(trimmed_text, None)
                    logger.info("[XNotifier] X APIが重複投稿と判定したため、同一本文を送信済みとして記録しました。")
                    return None
                # 画像添付で失敗した場合はテキスト単体で再試行
                if media_id:
                    import time
                    time.sleep(1.0)
                    logger.warning("[XNotifier] 画像付き投稿失敗のため、テキスト単体で再試行します...")
                    return self.post_tweet(text, media_id=None, _is_retry=True)
                return None
        except Exception as e:
            logger.exception(f"[XNotifier] X投稿リクエスト例外: {e}")
            return None

    def format_news_for_x(self, slot: str, title: str, fields: List[Dict[str, Any]]) -> str:
        """Discord用の詳細Embed情報から、X (日本語140文字以内) に最適化されたテキストを成形"""
        hashtags = {
            "07:00": "#米国株 #為替 #マクロ経済",
            "07:30": "#海外テック #半導体 #米国株",
            "08:00": "#日本株 #適時開示 #決算",
            "08:30": "#PTS #ストップ高 #日本株",
            "12:00": "#ニュース #経済 #社会",
            "16:00": "#日本株 #日経平均 #大引け",
            "17:00": "#PTS #夜間取引 #日本株",
            "19:00": "#欧州株 #為替 #世界市場",
            "21:30": "#NY市場 #米国株寄り付き",
        }.get(slot, "#投資 #市場ニュース")

        short_title = title.split("(")[0].strip()

        points = []
        for f in fields:
            val = f.get("value", "")
            lines = [l.strip().lstrip("•- ") for l in val.split("\n") if l.strip()]
            for line in lines:
                if len(line) > 5 and not line.startswith("http") and not line.startswith("※"):
                    points.append(line)
                if len(points) >= 2:
                    break
            if len(points) >= 2:
                break

        body_points = "\n".join([f"・{p[:45]}" for p in points[:2]])
        header = f"【{short_title}】\n"
        footer = f"\n\n{hashtags}"

        max_body_len = 135 - len(header) - len(footer)
        if max_body_len > 0 and len(body_points) > max_body_len:
            body_points = body_points[:max_body_len - 3] + "..."

        tweet_text = f"{header}{body_points}{footer}"
        return tweet_text.strip()

    def format_breakout_for_x(self, triggered_events: List[Dict[str, Any]]) -> str:
        """1%急変速報を X (140文字以内) に最適化して成形 (視認性MAX・3秒理解フォーマット)"""
        if not triggered_events:
            return ""

        top = triggered_events[0]
        sign = "+" if top["pct_change"] >= 0 else ""
        direction = "急伸" if top["pct_change"] >= 0 else "急落"

        # 最大変動率からショックレベルを判定
        max_abs = max(abs(e["pct_change"]) for e in triggered_events)
        if max_abs >= 2.5:
            shock_level = "CRITICAL (新規エントリー停止)"
            shock_emoji = "🛑"
            title_tag = "【急落ショック】" if top["pct_change"] < 0 else "【急騰ショック】"
        else:
            shock_level = "WARNING (ロット50%縮小)"
            shock_emoji = "⚠️"
            title_tag = "【市況急変】"

        t_now = datetime.now().strftime("%H:%M")
        header = f"{title_tag}{top['name']} {sign}{top['pct_change']:.2f}%{direction} ({t_now})\n"
        body_lines = [
            "📍検知：世界株価センチネル",
            f"{shock_emoji}防護：ShockSentinel {shock_level}",
        ]

        if len(triggered_events) > 1:
            others = "、".join([f"{e['name']}{('+' if e['pct_change']>=0 else '')}{e['pct_change']:.1f}%" for e in triggered_events[1:3]])
            body_lines.append(f"🌏他急変：{others}")

        body_lines.append("🖼画像：急変時系列チャート添付")
        footer = "\n\n#日本株 #米国株 #為替 #世界の株価"

        full_text = header + "\n".join(body_lines) + footer
        return full_text.strip()


# シングルトンインスタンス
default_x_notifier = XNotifier()


def send_news_tweet(slot: str, title: str, fields: List[Dict[str, Any]]) -> Optional[str]:
    """スロットニュースをX向けに成形して投稿する簡易関数"""
    text = default_x_notifier.format_news_for_x(slot, title, fields)
    return default_x_notifier.post_tweet(text)


def send_breakout_tweet(triggered_events: List[Dict[str, Any]], image_bytes: Optional[bytes] = None) -> Optional[str]:
    """1%急変速報を画像付きでXに投稿する簡易関数"""
    text = default_x_notifier.format_breakout_for_x(triggered_events)
    media_id = None
    if image_bytes:
        media_id = default_x_notifier.upload_media(image_bytes)
    return default_x_notifier.post_tweet(text, media_id=media_id)
