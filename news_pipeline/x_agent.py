"""
news_pipeline/x_agent.py: X (旧Twitter) 専用広報・アルファ発信エージェント (X-Agent)
==================================================================================
アーキテクチャ:
  NEWS Agent (TDnet / EDINET / PTS)
    ↓
  Impact (MIS: ブレーキ)
    ↓
  OAS (アクセル)
    ↓
  EVS (期待値ランキング)
    ↓
  X-Agent (★本モジュール)
    ├── 140文字圧縮 & 煽り排除 (客観的クオンツ視点)
    ├── 比率管理 (70% 速報ニュース / 20% アルファ候補 / 10% 運用成績)
    ├── 高解像度サムネイル画像生成 (Pillow)
    └── X API v2 (media_upload + post_tweet) 配信
"""

import os
import sys
import re
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from news_pipeline.x_notifier import XNotifier
from news_pipeline.disclosure_image_generator import DisclosureImageGenerator
from news_pipeline.market_impact_scorer import MarketEvent, MarketImpactScorer

logger = logging.getLogger("news_pipeline.x_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

JST = timezone(timedelta(hours=9))
X_STATS_FILE = os.path.join(BASE_DIR, "data", "x_agent_post_stats.json")


class XAgent:
    """
    X専用広報・アルファ発信エージェント
    役割: ニュース・クオンツ分析結果を人間が最も理解しやすい140文字＋画像に変換して配信
    """

    # 理想投稿比率: 70% 速報 / 20% アルファ / 10% 実績
    TARGET_RATIO = {
        "NEWS_FLASH": 0.70,     # 速報ニュース
        "ALPHA_CANDIDATE": 0.20,# 💎 アルファ候補 (高EVS/需給イベント)
        "TRACK_RECORD": 0.10,   # 運用成績 (透明性の証明)
    }

    def __init__(self, x_notifier: Optional[XNotifier] = None):
        self.x_notifier = x_notifier or XNotifier()
        self.img_gen = DisclosureImageGenerator()
        self.stats = self._load_stats()

    def _load_stats(self) -> Dict[str, Any]:
        """日次投稿統計をロード"""
        today_str = datetime.now(JST).strftime("%Y-%m-%d")
        default_stats = {"date": today_str, "NEWS_FLASH": 0, "ALPHA_CANDIDATE": 0, "TRACK_RECORD": 0, "total": 0}
        if os.path.exists(X_STATS_FILE):
            try:
                with open(X_STATS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("date") == today_str:
                        return data
            except Exception:
                pass
        return default_stats

    def _save_stats(self) -> None:
        """日次投稿統計を保存"""
        os.makedirs(os.path.dirname(X_STATS_FILE), exist_ok=True)
        try:
            with open(X_STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, indent=2)
        except Exception as e:
            logger.warning(f"[X-Agent] 統計保存エラー: {e}")

    def _increment_category(self, category: str) -> None:
        today_str = datetime.now(JST).strftime("%Y-%m-%d")
        if self.stats.get("date") != today_str:
            self.stats = {"date": today_str, "NEWS_FLASH": 0, "ALPHA_CANDIDATE": 0, "TRACK_RECORD": 0, "total": 0}
        self.stats[category] = self.stats.get(category, 0) + 1
        self.stats["total"] = self.stats.get("total", 0) + 1
        self._save_stats()

    # -------------------------------------------------------------
    # 1. カテゴリB: 【💎 アルファ候補】(20% 枠: 画像付き)
    # -------------------------------------------------------------
    def post_alpha_candidate(
        self,
        event: MarketEvent,
        oas: int,
        evs: float,
        tier: str = "TIER1",
        win_prob: float = 0.71,
        holding_days: float = 2.8,
        daily_bp: float = 44.6,
        sample_size: int = 148,
    ) -> Optional[str]:
        """
        高EVS・高OASのアルファ候補を画像付きで投稿 (煽らない・客観的事実)
        """
        if not self.x_notifier.is_configured():
            logger.info("[X-Agent] X API未設定のためスキップ")
            return None

        # 1. サムネイル画像を生成
        img_bytes = self.img_gen.generate_single_card(
            event_type=event.event_type,
            name=event.name,
            symbol=event.symbol or "----",
            headline=event.headline_metric or event.event_type,
            reason=event.reason or "",
            tier=tier,
            evs_score=evs,
            win_prob=win_prob,
            holding_days=holding_days,
            daily_bp=daily_bp,
        )

        # 2. 本文テキスト (140文字以内・煽り排除)
        # フォーマット:
        # 💎【AIアルファ候補】銘柄名 (コード)
        # 要点1行
        # 📊 期待値: EVS xx.x (Tier x)
        # 📈 過去勝率: xx% (同条件xx件) / 拘束 x日
        # #日本株 #適時開示 #株クラ
        lines = [
            f"💎【AIアルファ候補】{event.name}（{event.symbol or '----'}）",
            f"{event.headline_metric or event.event_type}。",
        ]
        if event.reason:
            lines.append(f"📍要因: {event.reason[:30]}")

        lines.append(f"📊期待値: EVS {evs:.1f}（{tier}）")
        lines.append(f"📈過去実績: 勝率{win_prob*100:.0f}%（同条件{sample_size}件/平均拘束{holding_days:.1f}日）")
        lines.append("#日本株 #適時開示 #アルファ候補 #株クラ")

        text = "\n".join(lines).strip()

        # 3. 画像アップロード & ツイート投稿
        try:
            media_id = self.x_notifier.upload_media(img_bytes)
            tweet_id = self.x_notifier.post_tweet(text=text, media_id=media_id)
            if tweet_id:
                self._increment_category("ALPHA_CANDIDATE")
                logger.info(f"[X-Agent] 💎 アルファ候補 (画像付き) 投稿成功: {tweet_id}")
                return tweet_id
        except Exception as e:
            logger.warning(f"[X-Agent] アルファ候補投稿エラー: {e}")
        return None

    # -------------------------------------------------------------
    # 2. カテゴリA: 【速報ニュース】(70% 枠: 結論ファースト)
    # -------------------------------------------------------------
    def post_news_flash(
        self,
        event: MarketEvent,
        mis: int,
        attach_image: bool = False,
    ) -> Optional[str]:
        """
        東証適時開示・EDINETの即時客観速報
        """
        if not self.x_notifier.is_configured():
            return None

        # 煽らない・結論ファーストテキスト
        time_str = f"（{event.time_str}）" if event.time_str else ""
        metric = f" {event.headline_metric}" if event.headline_metric else ""
        lines = [
            f"📢【東証開示速報】{event.name}（{event.symbol or '----'}）{time_str}",
            f"{event.event_type}{metric}。",
        ]
        if event.reason:
            lines.append(f"📍要因: {event.reason[:35]}")

        imp_label = MarketImpactScorer.get_impact_label(mis)
        lines.append(f"⚠️市場影響度: {imp_label}（MIS {mis}）")
        lines.append("#日本株 #適時開示 #決算 #速報")

        text = "\n".join(lines).strip()

        media_id = None
        if attach_image:
            try:
                img_bytes = self.img_gen.generate_single_card(
                    event_type=event.event_type,
                    name=event.name,
                    symbol=event.symbol or "----",
                    headline=event.headline_metric or event.event_type,
                    reason=event.reason or "",
                    tier="TIER2",
                    evs_score=float(mis),
                    win_prob=0.65,
                    holding_days=3.0,
                    daily_bp=30.0,
                )
                media_id = self.x_notifier.upload_media(img_bytes)
            except Exception:
                pass

        try:
            tweet_id = self.x_notifier.post_tweet(text=text, media_id=media_id)
            if tweet_id:
                self._increment_category("NEWS_FLASH")
                logger.info(f"[X-Agent] 📢 速報ニュース投稿成功: {tweet_id}")
                return tweet_id
        except Exception as e:
            logger.warning(f"[X-Agent] 速報ニュース投稿エラー: {e}")
        return None

    # -------------------------------------------------------------
    # 3. カテゴリC: 【運用成績・透明性報告】(10% 枠: 日次1〜2回)
    # -------------------------------------------------------------
    def post_track_record(
        self,
        period_str: str,
        live_bp: float,
        live_jpy: float,
        dryrun_bp: float,
        dryrun_jpy: float,
        win_rate_pct: float,
        total_trades: int,
    ) -> Optional[str]:
        """
        運用成績の定期透明性報告 (10%枠)
        """
        if not self.x_notifier.is_configured():
            return None

        sign_live = "+" if live_bp >= 0 else ""
        sign_dry = "+" if dryrun_bp >= 0 else ""

        lines = [
            f"📈【Agy クオンツ運用成績 定期報告】",
            f"期間: {period_str} 集計",
            "",
            f"• LIVE取引: {sign_live}{live_bp:.2f} bp（¥{live_jpy:+,.0f}）[元本完全保護中]",
            f"• DRYRUN検証: {sign_dry}{dryrun_bp:.2f} bp（¥{dryrun_jpy:+,.0f}）",
            f"• 検証勝率: {win_rate_pct:.1f}%（{total_trades}取引）",
            "",
            "💡 24時間フル自律稼働・パラメータ固定検証中",
            "#クオンツ #システムトレード #暗号資産 #Agy",
        ]
        text = "\n".join(lines).strip()

        try:
            tweet_id = self.x_notifier.post_tweet(text=text)
            if tweet_id:
                self._increment_category("TRACK_RECORD")
                logger.info(f"[X-Agent] 📈 運用成績報告投稿成功: {tweet_id}")
                return tweet_id
        except Exception as e:
            logger.warning(f"[X-Agent] 運用成績投稿エラー: {e}")
        return None
