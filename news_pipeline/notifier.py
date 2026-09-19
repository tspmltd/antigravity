"""
Discord Webhook 送信モジュール (notifier.py)
- Discord Embed形式 (青色テーマ、タイムスタンプ付き)
- 最大3回までの指数バックオフ・リトライ
- 429 レートリミット対策 (retry_after 待機)
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import requests
from dotenv import load_dotenv

# 環境変数ロード
load_dotenv(override=True)

# ロガー設定
logger = logging.getLogger("news_pipeline.notifier")

# デフォルトWebhook URL (メイン運用報告チャンネル)
DEFAULT_TRADE_WEBHOOK = "https://discord.com/api/webhooks/1490788526533509241/dRn-L0QvDx2OfHc-SQSLv4RfA18jJrSxFNAuHTUu9Xp_wCpkB2SNhav0roQGteu6cZCW"

DISCORD_NEWS_WEBHOOK = os.getenv("DISCORD_NEWS_WEBHOOK_URL", DEFAULT_TRADE_WEBHOOK)
DISCORD_TRADE_WEBHOOK = os.getenv("DISCORD_TRADE_WEBHOOK", DISCORD_NEWS_WEBHOOK)
DISCORD_REPORT_WEBHOOK = os.getenv("DISCORD_REPORT_WEBHOOK", DISCORD_NEWS_WEBHOOK)

# テーマカラー (指示書指定: 青色テーマ)
THEME_COLOR_BLUE = 0x1E88E5      # プライマリブルー
THEME_COLOR_NAVY = 0x0D47A1      # 深いブルー
THEME_COLOR_CYAN = 0x00ACC1      # 明るいシアン


class DiscordNotifier:
    """Discord Webhookへのメッセージ・Embed送信クラス"""

    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or DISCORD_TRADE_WEBHOOK

    def send_embed(
        self,
        title: str,
        description: str,
        fields: Optional[List[Dict[str, Any]]] = None,
        color: int = THEME_COLOR_BLUE,
        footer_text: str = "AGY 24h News Pipeline",
        url: Optional[str] = None,
        max_retries: int = 3,
        webhook_url: Optional[str] = None
    ) -> bool:
        """
        Embed形式のメッセージをDiscord Webhookに送信する。
        最大3回リトライ、429レートリミット待機に対応。
        """
        target_url = webhook_url or self.webhook_url
        if not target_url:
            logger.error("[Notifier] Webhook URLが設定されていません。")
            return False

        # 現在時刻 (UTC ISO8601) をタイムスタンプとして付与
        now_iso = datetime.now(timezone.utc).isoformat()

        # Embedデータの組み立て
        embed: Dict[str, Any] = {
            "title": title[:256],
            "description": description[:4096] if description else "",
            "color": color,
            "timestamp": now_iso,
            "footer": {
                "text": footer_text[:2048]
            }
        }

        if url:
            embed["url"] = url

        # フィールドの追加 (最大25個、各文字数制限対応)
        if fields:
            cleaned_fields = []
            for f in fields[:25]:
                name = str(f.get("name", "項目"))[:256]
                value = str(f.get("value", "-"))[:1024]
                inline = bool(f.get("inline", False))
                cleaned_fields.append({
                    "name": name,
                    "value": value,
                    "inline": inline
                })
            embed["fields"] = cleaned_fields

        payload = {
            "username": "AGY Market News",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/3314/3314488.png",
            "embeds": [embed]
        }

        # 送信 & リトライループ (最大3回)
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(
                    target_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10
                )

                # 成功 (200 OK / 204 No Content)
                if response.status_code in (200, 204):
                    logger.info(f"[Notifier] Discord送信成功: '{title}' (試行回数: {attempt})")
                    return True

                # レートリミット (429 Too Many Requests)
                if response.status_code == 429:
                    retry_after = 5.0
                    try:
                        data = response.json()
                        retry_after = float(data.get("retry_after", 5.0))
                    except Exception:
                        pass
                    logger.warning(f"[Notifier] 429 レートリミット検知。{retry_after:.2f}秒待機します...")
                    time.sleep(retry_after)
                    continue

                # その他のエラーステータス
                logger.warning(
                    f"[Notifier] 送信失敗 (試行 {attempt}/{max_retries}): "
                    f"Status {response.status_code}, Body: {response.text[:200]}"
                )

            except requests.exceptions.RequestException as e:
                logger.warning(f"[Notifier] ネットワーク例外 (試行 {attempt}/{max_retries}): {e}")

            # 指数バックオフ待機 (2s, 4s, 8s)
            if attempt < max_retries:
                sleep_sec = 2 ** attempt
                time.sleep(sleep_sec)

        logger.error(f"[Notifier] 送信が最大試行回数 ({max_retries}回) を超えて完全に失敗しました: '{title}'")
        return False


# シングルトン / 簡易アクセス用関数
_default_notifier = DiscordNotifier()

def send_news_embed(
    title: str,
    description: str,
    fields: Optional[List[Dict[str, Any]]] = None,
    color: int = THEME_COLOR_BLUE,
    category: str = "macro"
) -> bool:
    """
    カテゴリーに応じて適切なWebhook URLへ送信するヘルパー関数
    - macro, alert: DISCORD_TRADE_WEBHOOK
    - report, stock: DISCORD_REPORT_WEBHOOK
    """
    target_url = DISCORD_TRADE_WEBHOOK if category in ("macro", "alert") else DISCORD_REPORT_WEBHOOK
    return _default_notifier.send_embed(
        title=title,
        description=description,
        fields=fields,
        color=color,
        webhook_url=target_url
    )


if __name__ == "__main__":
    # 動作確認用テスト送信
    logging.basicConfig(level=logging.INFO)
    print("Discord Notifier テスト送信を実行します...")
    ok = send_news_embed(
        title="🔔 [テスト] AGY ニュースパイプライン起動確認",
        description="AGY 24時間ニュース配信プロジェクトのDiscord通知モジュール稼働テストです。",
        fields=[
            {"name": "ステータス", "value": "🟢 正常稼働", "inline": True},
            {"name": "配信テーマ", "value": "青色テーマ (Embed)", "inline": True},
            {"name": "監視対象", "value": "マクロ / 海外指標 / 日本株 / PTS / 社会ニュース", "inline": False},
        ],
        color=THEME_COLOR_BLUE
    )
    print(f"送信結果: {'成功' if ok else '失敗'}")
