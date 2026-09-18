"""
EDINET & 重要適時開示 10文字速報センチネル (edinet_sentinel.py)
============================================================
- 金融庁EDINET（大量保有報告書、変更報告書、TOB等）および重要適時開示を常時監視
- 10文字〜15文字前後に超凝縮・要約（ミリ秒投資判断用）
- 【重要】X (旧Twitter API) と Discord へ直接同時送信
- その他の通常通知（LIVE約定・毎時KPI・探索）はX送信を遮断し、Discordのみへ集約してXトークンを節約
- 重複送信防止キャッシュ (edinet_posted_cache.json) による完全二重送信ガード
"""

import sys
import os
import re
import json
import time
import logging

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import feedparser
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

from dotenv import load_dotenv

# X & Discord Notifiers
from news_pipeline.x_notifier import XNotifier
from antigravity.risk_guard.notifier import DiscordNotifier
from news_pipeline.disclosure_dedup_engine import default_dedup_engine

load_dotenv()
logger = logging.getLogger("news_pipeline.edinet_sentinel")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

JST = timezone(timedelta(hours=9))


class EdinetSentinel:
    """EDINET & 適時開示 10文字速報センチネル"""

    FEED_URLS = [
        # 大量保有報告書・変更報告書
        "https://news.google.com/rss/search?q=大量保有報告書+when:1d&hl=ja&gl=JP&ceid=JP:ja",
        # TOB・公開買付
        "https://news.google.com/rss/search?q=(TOB+OR+公開買付)+株式+when:1d&hl=ja&gl=JP&ceid=JP:ja",
        # 自社株買い
        "https://news.google.com/rss/search?q=自社株買い+発表+when:1d&hl=ja&gl=JP&ceid=JP:ja",
    ]

    def __init__(
        self,
        cache_path: Optional[str] = None,
        check_interval_sec: float = 120.0,
        enable_x_post: Optional[bool] = None,
        enable_discord: bool = True,
    ):
        self.check_interval_sec = check_interval_sec
        # Xへの配信は大量保有(5%超)・TOB・自社株買いのキラーニュースに絞り込んで配信
        if enable_x_post is None:
            self.enable_x_post = os.getenv("ENABLE_EDINET_X_POST", "true").lower() in ("true", "1")
        else:
            self.enable_x_post = enable_x_post
        self.enable_discord = enable_discord

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.cache_path = cache_path or os.path.join(base_dir, "data", "edinet_posted_cache.json")
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)

        self.x_notifier = XNotifier()
        news_wh = os.getenv("DISCORD_NEWS_WEBHOOK_URL", "").strip() or os.getenv("DISCORD_TRADE_WEBHOOK", "").strip()
        self.discord_notifier = DiscordNotifier(webhook_url=news_wh)

        self.posted_links: set = set()
        self.posted_titles: set = set()
        self.daily_x_count: int = 0
        self.max_daily_x: int = int(os.getenv("EDINET_MAX_DAILY_X", "4"))  # X無料枠配分: 日次最大4件
        self.last_day: str = datetime.now(JST).strftime("%Y%m%d")
        self._load_cache()

    def _normalize_for_dedup(self, text: str) -> str:
        """重複判定用に文字列を正規化 (空白・記号除去)"""
        return re.sub(r"[\s\W_]+", "", text)

    def _load_cache(self):
        """送信済み記事キャッシュをロード"""
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.posted_links = set(data.get("posted_links", []))
                    self.posted_titles = set(data.get("posted_titles", []))
                    self.daily_x_count = data.get("daily_x_count", 0)
                    self.last_day = data.get("last_day", datetime.now(JST).strftime("%Y%m%d"))
                logger.info(f"[EdinetSentinel] キャッシュロード完了: URL {len(self.posted_links)} 件, タイトル {len(self.posted_titles)} 件, 本日X: {self.daily_x_count}/{self.max_daily_x}")
            except Exception as e:
                logger.warning(f"[EdinetSentinel] キャッシュロード失敗: {e}")

    def _save_cache(self):
        """送信済み記事キャッシュを保存"""
        try:
            today_str = datetime.now(JST).strftime("%Y%m%d")
            if today_str != self.last_day:
                self.daily_x_count = 0
                self.last_day = today_str

            # 最新500件を保持
            saved_links = list(self.posted_links)[-500:]
            saved_titles = list(self.posted_titles)[-500:]
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({
                    "posted_links": saved_links,
                    "posted_titles": saved_titles,
                    "daily_x_count": self.daily_x_count,
                    "last_day": self.last_day,
                    "updated_at": datetime.now(JST).isoformat()
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[EdinetSentinel] キャッシュ保存失敗: {e}")
        try:
            # 最新500件を保持
            saved_list = list(self.posted_links)[-500:]
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"posted_links": saved_list, "updated_at": datetime.now(JST).isoformat()}, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[EdinetSentinel] キャッシュ保存失敗: {e}")

    @staticmethod
    def condense_to_10_chars(raw_title: str) -> str:
        """
        開示タイトルを【10文字〜14文字程度】の超凝縮フォーマットに要約
        """
        clean = re.sub(r" - [^-]+$", "", raw_title).strip()
        clean = re.sub(r"\[大量保有報告書.*?\]", "", clean).strip()
        clean = re.sub(r"\(株探ニュース\)", "", clean).strip()

        # パターン1: 「◯◯について、◯◯は保有割合が5％を超えたと報告」
        m = re.search(r"^(.*?)について、(.*?)は保有割合が([0-9\.]+)％.*報告", clean)
        if m:
            target = m.group(1).replace("株式会社", "").strip()[:6]
            holder = m.group(2).replace("株式会社", "").replace("証券", "証").strip()[:4]
            pct = m.group(3)
            return f"{target}：{holder}{pct}%超保有"

        # パターン2: 「◯◯が◯◯株式の大量保有報告書を提出」
        m = re.search(r"^(.*?)が(.*?)株式の大量保有報告書を提出", clean)
        if m:
            holder = m.group(1).replace("株式会社", "").replace("証券", "証").strip()[:4]
            target = m.group(2).replace("株式会社", "").strip()[:6]
            return f"{target}：{holder}が大量取得"

        # パターン3: 「◯◯、◯◯億円の自社株買いプログラム発表」
        m = re.search(r"^(.*?)、?([0-9]+.*?)の?自社株買い", clean)
        if m:
            target = m.group(1).replace("株式会社", "").strip()[:6]
            scale = m.group(2).strip()[:4]
            return f"{target}：自社株買{scale}"

        # パターン4: TOB関連
        if "TOB" in clean or "公開買付" in clean:
            words = clean.split()
            target = words[0][:6] if words else "銘柄"
            return f"{target}：TOB買収実施"

        # パターン5: 一般短縮 (先頭の重要単語を抽出)
        condensed = re.sub(r"【.*?】|（.*?）|\(.*?\)|株式|会社", "", clean).strip()
        if len(condensed) > 13:
            return condensed[:12] + "…"
        return condensed if condensed else clean[:12]

    def fetch_latest_disclosures(self) -> List[Dict[str, Any]]:
        """新着の開示ニュースを収集"""
        new_items = []
        for url in self.FEED_URLS:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:8]:
                    link = getattr(entry, "link", "").strip()
                    title = getattr(entry, "title", "").strip()
                    if not link or not title or link in self.posted_links:
                        continue

                    # 10文字要約
                    condensed = self.condense_to_10_chars(title)
                    norm_title = self._normalize_for_dedup(condensed)

                    # 重複チェック: URLまたは要約タイトルが既に配信済みの場合はスキップ
                    if norm_title in self.posted_titles:
                        continue

                    new_items.append({
                        "raw_title": title,
                        "condensed_text": condensed,
                        "norm_title": norm_title,
                        "link": link,
                        "published": getattr(entry, "published", ""),
                    })
            except Exception as e:
                logger.warning(f"[EdinetSentinel] RSS取得失敗 ({url}): {e}")
        return new_items

    def broadcast_disclosure(self, item: Dict[str, Any]) -> bool:
        """
        重要ニュースを【X】と【Discord】へ直接送信！
        東証TDnetとの重複を自動判定・排除
        """
        condensed = item["condensed_text"]
        raw = item["raw_title"]
        link = item["link"]
        jst_time = datetime.now(JST).strftime("%H:%M:%S")

        # 0. 東証TDnet x EDINET 重複判定
        is_dup, display_src, dedup_item = default_dedup_engine.check_and_register(
            source="EDINET",
            raw_title=raw,
            condensed_text=condensed,
            link=link,
        )
        if is_dup:
            logger.info(f"[EdinetSentinel] 🛡️ 東証/EDINET重複開示を検知したため、二重投稿をスキップします: {condensed} ({display_src})")
            return False

        logger.info(f"[EdinetSentinel] 🚀 新着開示検知: [{condensed}] ({raw[:30]}...) [Source: {display_src}]")

        # 防護連携文字列の算出
        is_tob = "TOB" in condensed or "買収" in condensed or "公開買付" in raw
        is_large = "大量保有" in raw or "5%" in raw or "変更報告" in raw
        protection_str = "新規エントリー禁止" if is_tob else ("ロット上限50%縮小" if is_large else "通常運転")

        # 1. X (旧Twitter) へ直接投稿 (重複統合フォーマット、日次最大4件)
        x_success = False
        if self.enable_x_post and self.x_notifier.is_configured():
            if self.daily_x_count >= self.max_daily_x:
                logger.info(f"[EdinetSentinel] 🛑 本日のEDINET X投稿上限({self.max_daily_x}件)に達したためスキップします")
            else:
                dedup_item["source"] = display_src
                tweet_text = default_dedup_engine.format_x_disclosure(dedup_item, protection_str)
                try:
                    tweet_id = self.x_notifier.post_tweet(text=tweet_text)
                    if tweet_id:
                        x_success = True
                        self.daily_x_count += 1
                        self._save_cache()
                        logger.info(f"[EdinetSentinel] 🐦 X重要開示速報 投稿成功 (Tweet ID: {tweet_id}, 日次累計: {self.daily_x_count}/{self.max_daily_x})")
                except Exception as ex:
                    logger.warning(f"[EdinetSentinel] X投稿例外: {ex}")
        else:
            logger.info("[EdinetSentinel] X投稿スキップ (未設定または無効)")

        # 2. Discord へ直接送信
        discord_success = False
        if self.enable_discord:
            try:
                discord_success = self.discord_notifier.send_embed(
                    title=f"⚡ 【EDINET 10文字速報】{condensed}",
                    description=f"**検知時刻**: `{jst_time} JST`\n**原文タイトル**: {raw}\n[🔗 開示・記事詳細を見る]({link})",
                    fields=[
                        {"name": "超凝縮要約", "value": f"**`{condensed}`** (文字数: {len(condensed)}文字)", "inline": True},
                        {"name": "X (Twitter) 配信", "value": "🐦 投稿完了" if x_success else "⚪ スキップ/待機", "inline": True},
                    ],
                    color=0x3498DB,
                    footer_text="Antigravity EDINET Flash Sentinel 📰",
                    target="report",
                )
            except Exception as ex:
                logger.warning(f"[EdinetSentinel] Discord送信例外: {ex}")

        # 3. クオンツ基盤へマクロショック連携 (TOB・大型開示時)
        if "TOB" in condensed or "買収" in condensed or "公開買付" in raw:
            try:
                from antigravity.quant_pipeline.market_shock_sentinel import MarketShockSentinel
                MarketShockSentinel().publish_shock(
                    event_name=f"大型開示: {condensed}",
                    level="warning",
                    duration_sec=900.0,
                )
                logger.info(f"[EdinetSentinel] 🛡️ クオンツ基盤へ大型開示ショック連携完了 ({condensed})")
            except Exception as e_shock:
                logger.warning(f"[EdinetSentinel] クオンツショック連携スキップ: {e_shock}")

        # キャッシュに追加
        self.posted_links.add(link)
        norm_t = item.get("norm_title") or self._normalize_for_dedup(condensed)
        self.posted_titles.add(norm_t)
        self._save_cache()
        return True

    def poll_once(self, is_first_run: bool = False) -> int:
        """1回分のポーリング実行 (初回は1件のみテスト送信し残りは既読化)"""
        items = self.fetch_latest_disclosures()
        if not items:
            return 0

        if is_first_run and len(self.posted_links) == 0:
            logger.info(f"[EdinetSentinel] 🔰 初回起動検知: 既存開示 {len(items)} 件を検出。最新1件のみ配信し残りは既読化します。")
            # 最新1件のみ配信
            first_item = items[0]
            self.broadcast_disclosure(first_item)
            # 残りは既読キャッシュに追加
            for it in items[1:]:
                self.posted_links.add(it["link"])
            self._save_cache()
            return 1

        # 通常時: 新着を開示（1回あたり最大3件までに制限してトークン保護）
        sent_count = 0
        for it in items[:3]:
            self.broadcast_disclosure(it)
            sent_count += 1
            time.sleep(3.0)  # レートリミット保護
        
        # 4件目以降があれば次回に回さず既読化（古いもののスパム防止）
        if len(items) > 3:
            for it in items[3:]:
                self.posted_links.add(it["link"])
            self._save_cache()

        return sent_count

    def run_loop(self):
        """常駐監視ループ"""
        logger.info("[EdinetSentinel] 📰 EDINET 10文字速報常駐センチネル起動")
        logger.info(f"  • チェック間隔: {self.check_interval_sec} 秒")
        logger.info(f"  • X投稿連携: {'有効 (直接送信)' if self.enable_x_post else '無効'}")
        logger.info(f"  • Discord連携: {'有効 (直接送信)' if self.enable_discord else '無効'}")
        logger.info("  • その他の通常通知: X送信を完全遮断しDiscordへ集約 (Xトークン保護)")

        # 起動通知
        self.discord_notifier.send_system_update(
            title="📰 【EDINET 10文字速報センチネル】常駐開始",
            description=(
                "• **EDINET・適時開示**: 10文字超要約を行い **X ＋ Discord へ直接送信**\n"
                "• **その他の通知 (LIVE約定/KPI/探索)**: **X直接送信を遮断しDiscordのみへ集約**（Xトークン完全保護）\n"
                "• **監視対象**: 大量保有報告書・変更報告書・TOB・自社株買い"
            ),
        )

        is_first = True
        while True:
            try:
                sent = self.poll_once(is_first_run=is_first)
                if sent > 0:
                    logger.info(f"[EdinetSentinel] {sent} 件の新着開示を配信しました。")
                is_first = False
            except Exception as e:
                logger.error(f"[EdinetSentinel] ループエラー: {e}")
            time.sleep(self.check_interval_sec)


if __name__ == "__main__":
    # X配信は「1%急変速報」に集約するため、EDINET・開示速報はDiscordのみへ配信 (X投稿無効化)
    sentinel = EdinetSentinel(check_interval_sec=120.0, enable_x_post=False, enable_discord=True)
    sentinel.run_loop()

