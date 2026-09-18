"""
news_pipeline/tdnet_sentinel.py: 東証適時開示 (TDnet) リアルタイム速報センチネル
================================================================================
- 東証公式速報 (https://www.release.tdnet.info/inbs/I_list_001_{YYYYMMDD}.html) を高頻度巡回
- 証券コード (4桁)、企業名、表題、PDFリンク、発表時刻を完全正規化
- EDINET との重複排除 (disclosure_dedup_engine)
- MarketImpactScorer (MIS 0〜100) による自動格付け & 優先度判定
- X (旧Twitter) への日本株専用テンプレ (build_japan_post) 即時投稿 (MIS >= 70)
- Discord (ニュースチャンネル) への全件 / 重要度別 Embed 配信
"""

import os
import sys
import re
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from news_pipeline.market_impact_scorer import MarketEvent, MarketImpactScorer, build_japan_post
from news_pipeline.disclosure_dedup_engine import default_dedup_engine
from news_pipeline.x_notifier import XNotifier
from antigravity.risk_guard.notifier import DiscordNotifier

load_dotenv()
logger = logging.getLogger("news_pipeline.tdnet_sentinel")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

JST = timezone(timedelta(hours=9))


class TdnetSentinel:
    """東証適時開示 (TDnet) リアルタイム監視センチネル"""

    BASE_URL = "https://www.release.tdnet.info/inbs"

    def __init__(
        self,
        cache_path: Optional[str] = None,
        check_interval_sec: float = 60.0,
        enable_x_post: Optional[bool] = None,
        enable_discord: bool = True,
        min_mis_for_x: int = 60,
    ):
        self.check_interval_sec = check_interval_sec
        self.min_mis_for_x = min_mis_for_x
        if enable_x_post is None:
            # デフォルトでMIS >= 60の厳選重要開示のみX投稿を許可
            self.enable_x_post = os.getenv("ENABLE_TDNET_X_POST", "true").lower() in ("true", "1")
        else:
            self.enable_x_post = enable_x_post
        self.enable_discord = enable_discord

        self.cache_path = cache_path or os.path.join(BASE_DIR, "data", "tdnet_posted_cache.json")
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)

        self.x_notifier = XNotifier()
        news_wh = os.getenv("DISCORD_NEWS_WEBHOOK_URL", "").strip() or os.getenv("DISCORD_TRADE_WEBHOOK", "").strip()
        self.discord_notifier = DiscordNotifier(webhook_url=news_wh)

        self.posted_keys: set = set()
        self.daily_x_count: int = 0
        self.max_daily_x: int = int(os.getenv("TDNET_MAX_DAILY_X", "25"))  # X無料枠配分: 日次最大25件
        self.last_day: str = datetime.now(JST).strftime("%Y%m%d")
        self._load_cache()

    def _load_cache(self):
        """送信済みキャッシュをロード"""
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.posted_keys = set(data.get("posted_keys", []))
                    self.daily_x_count = data.get("daily_x_count", 0)
                    self.last_day = data.get("last_day", datetime.now(JST).strftime("%Y%m%d"))
                logger.info(f"[TdnetSentinel] キャッシュロード完了: {len(self.posted_keys)} 件保持")
            except Exception as e:
                logger.warning(f"[TdnetSentinel] キャッシュロード失敗: {e}")

    def _save_cache(self):
        """送信済みキャッシュを保存"""
        try:
            today_str = datetime.now(JST).strftime("%Y%m%d")
            if today_str != self.last_day:
                self.daily_x_count = 0
                self.last_day = today_str

            saved_keys = list(self.posted_keys)[-800:]
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({
                    "posted_keys": saved_keys,
                    "daily_x_count": self.daily_x_count,
                    "last_day": self.last_day,
                    "updated_at": datetime.now(JST).isoformat()
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[TdnetSentinel] キャッシュ保存失敗: {e}")

    def fetch_disclosures(self) -> List[Dict[str, Any]]:
        """当日のTDnet開示一覧をスクレイピングして取得"""
        now_jst = datetime.now(JST)
        today_str = now_jst.strftime("%Y%m%d")
        url = f"{self.BASE_URL}/I_list_001_{today_str}.html"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        try:
            resp = requests.get(url, headers=headers, timeout=12)
            if resp.status_code == 404:
                # 土日・休場日等でファイル未生成
                return []
            if resp.status_code != 200:
                logger.warning(f"[TdnetSentinel] TDnet取得失敗 HTTP {resp.status_code}")
                return []

            resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.find_all("tr")

            items = []
            for tr in rows:
                tds = tr.find_all("td")
                if len(tds) >= 4:
                    raw_time = tds[0].get_text(strip=True)
                    raw_code = tds[1].get_text(strip=True)
                    raw_name = tds[2].get_text(strip=True)
                    raw_title = tds[3].get_text(strip=True)

                    a_tag = tds[3].find("a")
                    pdf_rel = a_tag["href"] if a_tag and "href" in a_tag.attrs else ""
                    pdf_url = f"{self.BASE_URL}/{pdf_rel}" if pdf_rel else ""

                    if not raw_title or not raw_code:
                        continue

                    # 証券コードが正規フォーマット (4桁数字または英数5桁) でない行はHTMLゴミなので除外
                    if not re.match(r"^[0-9A-Z]{4,5}$", raw_code):
                        continue
                    # 時刻フォーマット (HH:MM) チェック
                    if not re.match(r"^\d{1,2}:\d{2}$", raw_time):
                        continue

                    # 証券コード正規化 (先頭4桁)
                    code_clean = raw_code[:4] if len(raw_code) >= 4 else raw_code
                    # 企業名正規化 (プレフィックス除去)
                    name_clean = re.sub(r"^[ＥＧＰ]－", "", raw_name)

                    items.append({
                        "time_str": raw_time,
                        "symbol": code_clean,
                        "raw_code": raw_code,
                        "name": name_clean,
                        "raw_name": raw_name,
                        "title": raw_title,
                        "pdf_url": pdf_url,
                        "date_str": today_str,
                    })

            # 古い順 (時系列昇順) に反転
            return list(reversed(items))

        except Exception as ex:
            logger.error(f"[TdnetSentinel] TDnetフェッチ例外: {ex}")
            return []

    def process_disclosure(self, item: Dict[str, Any]) -> bool:
        """1件の開示を分析し、重複判定・MIS計算・X/Discord投稿を実行"""
        key = f"{item['date_str']}_{item['raw_code']}_{item['title'][:30]}"
        if key in self.posted_keys:
            return False

        title = item["title"]
        name = item["name"]
        time_str = item["time_str"]
        symbol = item["symbol"]
        pdf_url = item["pdf_url"]

        # 0. 事務的開示・ノイズの即時スキップ (フォロワー獲得のための厳選)
        if MarketImpactScorer.is_noise_disclosure(title):
            self.posted_keys.add(key)
            return False

        # 1. EDINET/既存との重複排除
        is_dup, display_src, merged = default_dedup_engine.check_and_register(
            source="TDnet",
            raw_title=f"{name} {title}",
            condensed_text=title[:30],
            link=pdf_url,
        )

        # 2. 自動分類 & MIS (市場影響度スコア) 計算
        event_type, direction = MarketImpactScorer.classify_raw_disclosure(title)
        if event_type == "事務的開示":
            self.posted_keys.add(key)
            return False

        # 指標抽出 (増益率、下方修正率等のパーセンテージ)
        m_pct = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", title)
        metric_str = f"{m_pct.group(1)}%" if m_pct else ""
        if not metric_str:
            if "増益" in title or "上方" in title or "最高益" in title:
                metric_str = "上方" if "上方" in title else "増益"
            elif "減益" in title or "下方" in title:
                metric_str = "下方" if "下方" in title else "減益"
            elif "増配" in title:
                metric_str = "増配"
            elif "自社株買い" in title or "自己株式取得" in title:
                metric_str = "自社株買い"
            elif "子会社化" in title or "買収" in title or "TOB" in title:
                metric_str = "TOB/買収"

        event = MarketEvent(
            event_type=event_type,
            name=name,
            symbol=symbol,
            headline_metric=metric_str,
            source=display_src,
            time_str=time_str,
            direction=direction,
        )
        mis = MarketImpactScorer.calculate_mis(event)
        event.mis = mis

        logger.info(f"[TdnetSentinel] 開示検知: [{time_str}] {symbol} {name} | {event_type} | MIS: {mis} | {title[:40]}")

        # 3. X (旧Twitter) 投稿判定 (MIS >= 60 のキラー開示、日次最大25件)
        tweet_id = None
        should_post_x = self.enable_x_post and (mis >= self.min_mis_for_x)
        if should_post_x and self.daily_x_count < self.max_daily_x:
            x_text = build_japan_post(event)
            if x_text:
                try:
                    tweet_id = self.x_notifier.post_tweet(text=x_text)
                    if tweet_id:
                        self.daily_x_count += 1
                        logger.info(f"[TdnetSentinel] 🐦 X重要開示速報 投稿成功 (Tweet ID: {tweet_id}, MIS: {mis}, 日次累計: {self.daily_x_count}/{self.max_daily_x})")
                except Exception as ex:
                    logger.warning(f"[TdnetSentinel] X投稿エラー: {ex}")

        # 4. Discord 配信
        if self.enable_discord and self.discord_notifier:
            try:
                imp_label = MarketImpactScorer.get_impact_label(mis)
                color = 0xE74C3C if mis >= 85 else (0xF39C12 if mis >= 70 else (0x3498DB if mis >= 40 else 0x95A5A6))
                fields = [
                    {"name": "🏢 銘柄", "value": f"**{name}** (`{symbol}`)", "inline": True},
                    {"name": "⏰ 発表時刻", "value": f"`{time_str}`", "inline": True},
                    {"name": "📊 市場影響度 (MIS)", "value": f"**{imp_label}** (`MIS {mis}`)", "inline": True},
                    {"name": "📌 区分 / ソース", "value": f"`{event_type}` / `{display_src}`", "inline": True},
                ]
                if tweet_id:
                    fields.append({"name": "🐦 X配信", "value": f"[ポスト確認](https://x.com/suzuhiroltd1/status/{tweet_id})", "inline": True})
                if pdf_url:
                    fields.append({"name": "📄 開示資料 (PDF)", "value": f"[適時開示原文を見る]({pdf_url})", "inline": False})

                self.discord_notifier.send_embed(
                    title=f"【東証適時開示】{name} ({symbol}) - {event_type}",
                    description=f"**{title}**",
                    fields=fields,
                    color=color,
                    target="news",
                )
            except Exception as ex:
                logger.warning(f"[TdnetSentinel] Discord通知エラー: {ex}")

        # 登録 & 保存
        self.posted_keys.add(key)
        self._save_cache()
        return True

    def poll_once(self) -> int:
        """1回の巡回で最新開示を処理"""
        items = self.fetch_disclosures()
        count = 0
        for item in items:
            if self.process_disclosure(item):
                count += 1
        return count

    def run_forever(self):
        """常駐監視ループ"""
        logger.info("=" * 60)
        logger.info("  🚀 東証適時開示 (TDnet) リアルタイム速報センチネル 稼働開始")
        logger.info(f"  • 巡回インターバル: {self.check_interval_sec} 秒")
        logger.info(f"  • X投稿基準: MIS >= {self.min_mis_for_x} (重要開示限定)")
        logger.info(f"  • X投稿有効化: {self.enable_x_post}")
        logger.info(f"  • Discord配信: {self.enable_discord}")
        logger.info("=" * 60)

        while True:
            try:
                processed = self.poll_once()
                if processed > 0:
                    logger.info(f"[TdnetSentinel] 新規開示 {processed} 件を処理しました。")
            except Exception as ex:
                logger.error(f"[TdnetSentinel] 巡回ループエラー: {ex}")

            time.sleep(self.check_interval_sec)


def main():
    sentinel = TdnetSentinel()
    sentinel.run_forever()


if __name__ == "__main__":
    main()
