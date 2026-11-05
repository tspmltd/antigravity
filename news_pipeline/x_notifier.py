"""
X (旧Twitter) API v2 自動投稿モジュール (x_notifier.py)
=========================================================
- API v2 POST /2/tweets によるツイート投稿
- Twitter v1.1 media/upload によるチャート画像添付対応
- OAuth 1.0a User Context 認証 (requests-oauthlib)
- 日本語140文字制限に最適化したスマート要約 & ハッシュタグ自動付与
- 土日(JST)は緊急ニュース以外を遮断 / 同一内容の再投稿を指紋で遮断
- 未設定時の安全スキップ & エラーハンドリング (Discord送信への波及防止)
"""

import os
import re
import time
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
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
X_DAILY_STATE_PATH = os.path.join(_BASE_DIR, "data", "x_global_daily_state.json")
X_FINGERPRINT_PATH = os.path.join(_BASE_DIR, "data", "x_recent_post_fingerprints.json")
X_KILL_FLAG_PATH = os.path.join(_BASE_DIR, "data", "X_POST_DISABLED.flag")
GLOBAL_DAILY_POST_LIMIT = int(os.getenv("X_GLOBAL_DAILY_LIMIT", "48"))  # X無料枠(月1500件=日平均50件)の安全上限
FINGERPRINT_TTL_SEC = float(os.getenv("X_FINGERPRINT_TTL_SEC", str(72 * 3600)))  # 同一内容の再投稿遮断窓 (既定72時間)
FINGERPRINT_MAX_ENTRIES = int(os.getenv("X_FINGERPRINT_MAX_ENTRIES", "500"))


def is_x_post_disabled() -> bool:
    """X投稿がキルスイッチ(ファイルフラグまたは環境変数)により停止されているか判定"""
    if os.path.exists(X_KILL_FLAG_PATH):
        return True
    val = os.getenv("X_DISABLE_POST", "").strip().lower()
    return val in ("1", "true", "yes", "on")

# 土日でも送ってよい緊急ニュースキーワード (大企業倒産・上場廃止級)
EMERGENCY_KEYWORDS = (
    "倒産", "破産", "破綻", "民事再生", "会社更生", "特別清算",
    "上場廃止", "監理銘柄", "整理銘柄", "特設注意",
    "粉飾", "特捜", "緊急・重要", "急落ショック", "急騰ショック",
)


def is_jst_weekend(now: Optional[datetime] = None) -> bool:
    """JSTで土曜(5)・日曜(6)なら True"""
    dt = now or datetime.now(JST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    else:
        dt = dt.astimezone(JST)
    return dt.weekday() >= 5


def normalize_tweet_text(text: str) -> str:
    """投稿本文の指紋用正規化 (時刻・ハッシュタグ・空白差を吸収)"""
    s = (text or "").lower()
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"#\S+", "", s)
    s = re.sub(r"\d{1,2}:\d{2}(:\d{2})?", "", s)
    s = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?", "", s)
    s = re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)
    return s


def tweet_fingerprint(text: str) -> str:
    return hashlib.sha1(normalize_tweet_text(text).encode("utf-8")).hexdigest()


def is_emergency_text(text: str) -> bool:
    """本文から緊急ニュースかどうかを判定"""
    if not text:
        return False
    return any(k in text for k in EMERGENCY_KEYWORDS)


class XNotifier:
    """X (旧Twitter) API v2 投稿クライアント"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        access_token_secret: Optional[str] = None,
        daily_limit: int = GLOBAL_DAILY_POST_LIMIT,
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
        self._fp_cache: Optional[List[Dict[str, Any]]] = None

    def _get_daily_count(self) -> int:
        """当日のX投稿累計数を取得 (日付変更時は0リセット)"""
        today_str = datetime.now(JST).strftime("%Y-%m-%d")
        if not os.path.exists(X_DAILY_STATE_PATH):
            return 0
        try:
            with open(X_DAILY_STATE_PATH, "r", encoding="utf-8") as f:
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
        os.makedirs(os.path.dirname(X_DAILY_STATE_PATH), exist_ok=True)
        try:
            with open(X_DAILY_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"date": today_str, "count": current_count, "limit": self.daily_limit}, f, indent=2)
        except Exception as e:
            logger.warning(f"[XNotifier] 日次カウンター永続化失敗: {e}")
        return current_count

    def get_remaining_daily_posts(self) -> int:
        """本日の残り投稿可能枠数を取得"""
        return max(0, self.daily_limit - self._get_daily_count())

    def is_configured(self) -> bool:
        """必要な4つのAPIクレデンシャルが全て設定されており、かつキルスイッチが無効か判定"""
        if is_x_post_disabled():
            return False
        return bool(self.api_key and self.api_secret and self.access_token and self.access_token_secret)

    def _load_fingerprints(self) -> List[Dict[str, Any]]:
        if self._fp_cache is not None:
            return self._fp_cache
        posts: List[Dict[str, Any]] = []
        if os.path.exists(X_FINGERPRINT_PATH):
            try:
                with open(X_FINGERPRINT_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                posts = data.get("posts", []) if isinstance(data, dict) else []
            except Exception as e:
                logger.warning(f"[XNotifier] 指紋キャッシュ読込失敗: {e}")
                posts = []
        now = time.time()
        posts = [p for p in posts if isinstance(p, dict) and (now - float(p.get("ts", 0))) <= FINGERPRINT_TTL_SEC]
        self._fp_cache = posts
        return posts

    def _save_fingerprints(self, posts: List[Dict[str, Any]]) -> None:
        self._fp_cache = posts[-FINGERPRINT_MAX_ENTRIES:]
        os.makedirs(os.path.dirname(X_FINGERPRINT_PATH), exist_ok=True)
        try:
            with open(X_FINGERPRINT_PATH, "w", encoding="utf-8") as f:
                json.dump({"posts": self._fp_cache}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[XNotifier] 指紋キャッシュ保存失敗: {e}")

    def _is_duplicate_text(self, text: str) -> bool:
        """直近の同一/ほぼ同一投稿があれば True"""
        norm = normalize_tweet_text(text)
        if len(norm) < 12:
            return False
        fp = tweet_fingerprint(text)
        now = time.time()
        for prev in self._load_fingerprints():
            if (now - float(prev.get("ts", 0))) > FINGERPRINT_TTL_SEC:
                continue
            if prev.get("fp") == fp:
                return True
            # 正規化本文の部分一致 (長文の再送・微差再送)
            prev_head = str(prev.get("head") or "")
            if prev_head and len(prev_head) >= 20 and (prev_head in norm or norm in prev_head):
                return True
        return False

    def _remember_text(self, text: str) -> None:
        posts = self._load_fingerprints()
        posts.append(
            {
                "ts": time.time(),
                "fp": tweet_fingerprint(text),
                "head": normalize_tweet_text(text)[:80],
            }
        )
        self._save_fingerprints(posts)

    def should_skip_weekend(self, text: str, is_emergency: bool = False) -> bool:
        """土日かつ緊急でなければ投稿スキップ対象"""
        if os.getenv("X_ALLOW_WEEKEND", "").strip() in ("1", "true", "True"):
            return False
        if not is_jst_weekend():
            return False
        return not (is_emergency or is_emergency_text(text))

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
        if is_x_post_disabled():
            logger.info("🛑 [XNotifier] X投稿キルスイッチが有効なため、画像アップロードをスキップしました。")
            return None
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

    def post_tweet(
        self,
        text: str,
        media_id: Optional[str] = None,
        _is_retry: bool = False,
        is_emergency: bool = False,
    ) -> Optional[str]:
        """
        ツイートを投稿する (画像添付対応)。
        X無料枠(1,500件/月 = 日50件)保護のため、日次上限(48件)に達している場合は安全に遮断。
        土日(JST)は緊急ニュース以外を遮断。同一内容の再投稿も指紋で遮断。
        :param text: 投稿本文 (最大140文字推奨)
        :param media_id: アップロード済み画像ID (オプション)
        :param _is_retry: リトライフラグ (内部用)
        :param is_emergency: 緊急ニュース明示フラグ (倒産・上場廃止級)
        :return: 投稿成功時の tweet_id (失敗時は None)
        """
        if is_x_post_disabled():
            logger.info("🛑 [XNotifier] X投稿キルスイッチ (X_POST_DISABLED) が有効なため、ツイート投稿を完全遮断・スキップしました。")
            return None

        if not self.is_configured():
            logger.info("[XNotifier] X APIクレデンシャルが未設定または無効化のため、X投稿をスキップします。")
            return None

        # 土日: 日本株休場のため緊急以外は送らない
        if not _is_retry and self.should_skip_weekend(text, is_emergency=is_emergency):
            logger.info(
                "[XNotifier] 🛑 土日(JST)のため通常投稿を遮断しました "
                "(緊急ニュース: 倒産・破産・上場廃止等のみ許可)。"
            )
            return None

        # 同一ニュースの再送遮断
        if not _is_retry and self._is_duplicate_text(text):
            logger.info("[XNotifier] 🛑 同一/ほぼ同一の投稿済みニュースのため重複送付を遮断しました。")
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

        trimmed_text = text[:280]
        payload: Dict[str, Any] = {"text": trimmed_text}
        if media_id:
            payload["media"] = {"media_ids": [media_id]}

        try:
            response = oauth.post(X_TWEET_API_URL, json=payload, timeout=15)
            if response.status_code in (200, 201):
                data = response.json().get("data", {})
                tweet_id = data.get("id")
                new_cnt = self._increment_daily_count()
                self._remember_text(text)
                logger.info(f"[XNotifier] 🐦 Xへのツイート投稿に成功しました (ID: {tweet_id}, 本日累計: {new_cnt}/{self.daily_limit})")
                return tweet_id
            else:
                logger.error(f"[XNotifier] ⚠️ X投稿失敗 (HTTP {response.status_code}, Media={bool(media_id)}): {response.text}")
                # 画像添付で失敗した場合はテキスト単体で再試行 (重複エラーでない場合)
                if media_id and "duplicate" not in response.text.lower():
                    import time as _time
                    _time.sleep(1.0)
                    logger.warning("[XNotifier] 画像付き投稿失敗のため、テキスト単体で再試行します...")
                    return self.post_tweet(text, media_id=None, _is_retry=True, is_emergency=is_emergency)
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
    return default_x_notifier.post_tweet(text, is_emergency=is_emergency_text(text))


def send_breakout_tweet(triggered_events: List[Dict[str, Any]], image_bytes: Optional[bytes] = None) -> Optional[str]:
    """1%急変速報を画像付きでXに投稿する簡易関数"""
    text = default_x_notifier.format_breakout_for_x(triggered_events)
    media_id = None
    if image_bytes:
        media_id = default_x_notifier.upload_media(image_bytes)
    # 急落/急騰ショックのみ土日許可。通常の市況急変は土日遮断。
    return default_x_notifier.post_tweet(text, media_id=media_id, is_emergency=is_emergency_text(text))
