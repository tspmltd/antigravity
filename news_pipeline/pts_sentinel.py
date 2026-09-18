"""
news_pipeline/pts_sentinel.py: 日本株PTS (夜間取引) 急変 & 出来高急増 リアルタイム監視センチネル
========================================================================================
- 17:00〜23:59 JST にPTS夜間取引の全銘柄を高頻度監視 (60秒インターバル)
- 変動率 |r| >= 5.0% または 出来高急増 (平常比 >= 3倍) の異常値を即時検知
- 出来高 < 1,000株の薄板ノイズを物理除外
- TDnet 当日開示と自動突合し、PTSCausalEngine (因果AI) で開示連動を判定
- クールダウン (同一銘柄30分) & 時間帯別厳選 (21時以降は最重要のみ)
- X (旧Twitter) 専用テンプレ自動投稿 & Discord リアルタイム配信
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
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from news_pipeline.scraper import fetch_kabutan_table
from news_pipeline.pts_causal_engine import PTSFeatureRecord, default_pts_causal_engine
from news_pipeline.x_notifier import XNotifier
from antigravity.risk_guard.notifier import DiscordNotifier

load_dotenv()
logger = logging.getLogger("news_pipeline.pts_sentinel")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

JST = timezone(timedelta(hours=9))


class PTSSentinel:
    """日本株PTS夜間取引センチネル"""

    PTS_UP_URL = "https://kabutan.jp/warning/pts_night_price_increase"
    PTS_DOWN_URL = "https://kabutan.jp/warning/pts_night_price_decrease"

    def __init__(
        self,
        poll_interval_sec: float = 60.0,
        enable_x_post: Optional[bool] = None,
        enable_discord: bool = True,
        min_change_pct: float = 5.0,
        min_volume: int = 1000,
    ):
        self.poll_interval_sec = poll_interval_sec
        self.min_change_pct = min_change_pct
        self.min_volume = min_volume

        if enable_x_post is None:
            self.enable_x_post = os.getenv("ENABLE_PTS_X_POST", "true").lower() in ("true", "1")
        else:
            self.enable_x_post = enable_x_post
        self.enable_discord = enable_discord

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.cache_path = os.path.join(base_dir, "data", "pts_posted_cache.json")
        self.tdnet_cache_path = os.path.join(base_dir, "data", "tdnet_posted_cache.json")
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)

        self.x_notifier = XNotifier()
        news_wh = os.getenv("DISCORD_NEWS_WEBHOOK_URL", "").strip() or os.getenv("DISCORD_TRADE_WEBHOOK", "").strip()
        self.discord_notifier = DiscordNotifier(webhook_url=news_wh)

        self.posted_history: Dict[str, Dict[str, Any]] = {}
        self.daily_x_count: int = 0
        self.max_daily_x: int = int(os.getenv("PTS_MAX_DAILY_X", "15"))  # X無料枠配分: 日次最大15件 (出来高急増×高変動厳選)
        self.last_day: str = datetime.now(JST).strftime("%Y%m%d")
        self._load_cache()

    def _load_cache(self):
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.posted_history = data.get("posted_history", {})
                    self.daily_x_count = data.get("daily_x_count", 0)
                    self.last_day = data.get("last_day", datetime.now(JST).strftime("%Y%m%d"))
                logger.info(f"[PTSSentinel] キャッシュロード完了: {len(self.posted_history)} 銘柄の履歴保持")
            except Exception as e:
                logger.warning(f"[PTSSentinel] キャッシュロード失敗: {e}")

    def _save_cache(self):
        try:
            today_str = datetime.now(JST).strftime("%Y%m%d")
            if today_str != self.last_day:
                self.daily_x_count = 0
                self.last_day = today_str

            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({
                    "posted_history": self.posted_history,
                    "daily_x_count": self.daily_x_count,
                    "last_day": self.last_day,
                    "updated_at": datetime.now(JST).isoformat()
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[PTSSentinel] キャッシュ保存失敗: {e}")

    def is_pts_active_hours(self) -> bool:
        """17:00〜23:59 JST の夜間取引時間帯か判定"""
        now = datetime.now(JST)
        weekday = now.weekday()
        if weekday in (5, 6):  # 土日は休場
            return False
        return 17 <= now.hour <= 23

    def get_today_tdnet_disclosures(self) -> Dict[str, Dict[str, Any]]:
        """当日TDnet開示を銘柄コードをキーとする辞書として抽出"""
        tdnet_map = {}
        if os.path.exists(self.tdnet_cache_path):
            try:
                with open(self.tdnet_cache_path, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                    for k in cdata.get("posted_keys", []):
                        # key format: {YYYYMMDD}_{raw_code}_{title}
                        parts = k.split("_", 2)
                        if len(parts) >= 3:
                            code = parts[1][:4]
                            title = parts[2]
                            tdnet_map[code] = {
                                "date": parts[0],
                                "code": code,
                                "title": title,
                            }
            except Exception as e:
                logger.warning(f"[PTSSentinel] TDnetキャッシュ参照例外: {e}")
        return tdnet_map

    def fetch_pts_movers(self) -> List[Dict[str, Any]]:
        """株探PTS急上昇・急落テーブルから対象銘柄を抽出"""
        movers = []

        # 1. 上昇
        up_rows = fetch_kabutan_table(self.PTS_UP_URL)
        for r in up_rows:
            parsed = self._parse_kabutan_row(r, direction="up")
            if parsed:
                movers.append(parsed)

        # 2. 下落
        down_rows = fetch_kabutan_table(self.PTS_DOWN_URL)
        for r in down_rows:
            parsed = self._parse_kabutan_row(r, direction="down")
            if parsed:
                movers.append(parsed)

        # 変動幅順にソート
        movers.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
        return movers

    def _parse_kabutan_row(self, r: List[str], direction: str) -> Optional[Dict[str, Any]]:
        """株探行からPTSデータをパース"""
        if len(r) < 10:
            return None
        code = r[0].strip()
        if not re.match(r"^\d{4}$|^[0-9A-Z]{4}$", code):
            return None

        name = r[1].strip()
        try:
            # r[5]: 終値, r[6]: PTS現在値, r[7]: 変化額, r[8]: 変化率, r[9]: 出来高
            pts_price = float(re.sub(r"[,円]", "", r[6].strip()))
            change_str = r[8].strip().replace("%", "")
            change_pct = float(change_str)
            volume = int(re.sub(r"[,株]", "", r[9].strip()))
        except Exception:
            return None

        # 出来高ノイズカット (<1,000株)
        if volume < self.min_volume:
            return None

        return {
            "symbol": code,
            "name": name,
            "pts_price": pts_price,
            "change_pct": change_pct,
            "volume": volume,
            "direction": direction,
            "time_str": datetime.now(JST).strftime("%H:%M"),
        }

    def evaluate_pts_event(self, item: Dict[str, Any], tdnet_map: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """1件のPTS銘柄を因果AIと照合してスコアリング"""
        code = item["symbol"]
        name = item["name"]
        pct = item["change_pct"]
        vol = item["volume"]
        abs_pct = abs(pct)

        # 変動率基準 (5%以上) または出来高急増
        if abs_pct < self.min_change_pct and vol < 10000:
            return None

        # 当日開示の有無
        tdnet_item = tdnet_map.get(code)
        disc_title = tdnet_item["title"] if tdnet_item else ""
        has_disc = bool(tdnet_item)

        # 特徴量レコード構築
        disc_type = "決算" if any(w in disc_title for w in ["決算", "四半期"]) else (
            "業績修正" if "業績" in disc_title or "修正" in disc_title else (
                "不祥事" if any(w in disc_title for w in ["不祥事", "不備", "不正", "調査"]) else (
                    "M&A" if any(w in disc_title for w in ["買収", "子会社", "TOB"]) else "その他"
                )
            )
        )
        disc_dir = "positive" if (pct > 0 and ("増益" in disc_title or "上方" in disc_title)) else (
            "negative" if (pct < 0 and ("減益" in disc_title or "下方" in disc_title or "不備" in disc_title)) else "neutral"
        )

        vol_ratio = max(1.0, vol / 5000.0)  # 簡易平常比推定
        feature_rec = PTSFeatureRecord(
            symbol=code,
            name=name,
            disclosure_type=disc_type,
            disclosure_direction=disc_dir,
            disclosure_is_critical=1 if disc_type == "不祥事" else 0,
            disclosure_is_financial=1 if disc_type in ("決算", "業績修正") else 0,
            pts_change_pct=pct,
            pts_volume_ratio=vol_ratio,
            pts_sustained_minutes=15,
            delta_minutes=30 if has_disc else 300,
        )

        # PTS因果AIによる判定
        causal_label, cis2_score, probs = default_pts_causal_engine.predict_causality(feature_rec)
        reason_text = default_pts_causal_engine.format_causal_reason(causal_label, disc_title)

        # PTS総合インパクトスコア (PTS_VIS2) 算出
        # 基本スコア(変動率*10) + 出来高点 + 因果点
        vol_score = 30 if vol >= 50000 else (20 if vol >= 20000 else 10)
        pts_vis2 = int(min(150, (abs_pct * 8.0) + vol_score + (cis2_score * 0.35)))

        # 学習データへ蓄積
        default_pts_causal_engine.save_sample_for_training(feature_rec)

        return {
            "symbol": code,
            "name": name,
            "change_pct": pct,
            "pts_price": item["pts_price"],
            "volume": vol,
            "volume_ratio": vol_ratio,
            "time_str": item["time_str"],
            "has_disclosure": has_disc,
            "disclosure_title": disc_title,
            "causal_label": causal_label,
            "causal_reason": reason_text,
            "cis2_score": cis2_score,
            "pts_vis2": pts_vis2,
        }

    def format_pts_x_post(self, ev: Dict[str, Any]) -> str:
        """PTS急変 X (Twitter) 最適化投稿テキストを生成"""
        pct = ev["change_pct"]
        sign = "+" if pct >= 0 else ""
        label = "急騰" if pct >= 5.0 else ("急落" if pct <= -5.0 else "出来高急増")
        prefix = f"【PTS{label}】"

        imp_label = "CRITICAL" if ev["pts_vis2"] >= 100 or ev["causal_label"] == "DIRECT" else "High"
        icon = "🛑" if imp_label == "CRITICAL" and pct < 0 else "⚠️"

        lines = [
            f"{prefix}{ev['name']} {sign}{pct:.1f}%（{ev['time_str']}）",
            f"📍価格：{sign}{pct:.1f}%{label}（{ev['pts_price']:.0f}円）",
            f"📍出来高：{ev['volume']:,}株（平常比 約{ev['volume_ratio']:.1f}倍）",
            f"📍要因：{ev['causal_reason']}",
            f"{icon}市場影響度：{imp_label}（PTS_VIS {ev['pts_vis2']}）",
            "#日本株 #PTS #夜間取引 #世界の株価",
        ]
        return "\n".join(lines).strip()

    def should_post_to_x(self, ev: Dict[str, Any]) -> bool:
        """投稿頻度最適化・クールダウン判定"""
        if not self.enable_x_post:
            return False
        if self.daily_x_count >= self.max_daily_x:  # 1日最大15件制限 (厳選キラー材料枠)
            return False

        sym = ev["symbol"]
        now_ts = time.time()
        prev = self.posted_history.get(sym)

        # 0. 出来高フィルター (薄商いのダマシを排除)
        vol = ev.get("volume", 0)
        vol_ratio = ev.get("volume_ratio", 1.0)
        if vol < 2000 and vol_ratio < 1.5:
            return False

        # 1. 重要度判定 (PTS_VIS2 >= 65 または DIRECT因果 または 5%以上の急騰急落)
        is_high_impact = (ev["pts_vis2"] >= 65) or (ev["causal_label"] == "DIRECT") or (abs(ev["change_pct"]) >= 5.0)
        if not is_high_impact:
            return False

        # 2. 夜間 (21時以降) の厳選判定: VIS >= 75 または DIRECT または 大商い5%変動
        now_hour = datetime.now(JST).hour
        if now_hour >= 21:
            is_night_worthy = (ev["pts_vis2"] >= 75) or (ev["causal_label"] == "DIRECT") or (abs(ev["change_pct"]) >= 5.0 and vol >= 5000)
            if not is_night_worthy:
                return False

        if prev is None:
            return True

        last_time = prev.get("time", 0.0)
        last_pct = prev.get("pct", 0.0)
        curr_pct = ev["change_pct"]

        # 3. 方向反転 (急騰から急落、またはその逆) は即時投稿
        if (last_pct > 0 and curr_pct < 0) or (last_pct < 0 and curr_pct > 0):
            return True

        # 4. 開示連動 DIRECT はクールダウン短縮 (10分)
        if ev["causal_label"] == "DIRECT" and (now_ts - last_time >= 600.0):
            return True

        # 5. 通常クールダウン (最低30分)
        if now_ts - last_time < 1800.0:
            return False

        # 6. マイルストーン拡大 (前より2%以上変動拡大)
        if abs(curr_pct) >= abs(last_pct) + 2.0:
            return True

        return False

    def scan_and_notify(self) -> int:
        """1回巡回して検知・配信を実行"""
        tdnet_map = self.get_today_tdnet_disclosures()
        movers = self.fetch_pts_movers()
        if not movers:
            return 0

        logger.info(f"[PTSSentinel] PTS巡回: 対象候補 {len(movers)} 銘柄取得")
        count = 0

        for item in movers:
            ev = self.evaluate_pts_event(item, tdnet_map)
            if not ev:
                continue

            sym = ev["symbol"]
            now_ts = time.time()
            post_to_x = self.should_post_to_x(ev)
            tweet_id = None

            # 1. X投稿
            if post_to_x:
                x_text = self.format_pts_x_post(ev)
                try:
                    tweet_id = self.x_notifier.post_tweet(text=x_text)
                    if tweet_id:
                        self.daily_x_count += 1
                        self.posted_history[sym] = {
                            "time": now_ts,
                            "pct": ev["change_pct"],
                            "tweet_id": tweet_id,
                            "vis": ev["pts_vis2"],
                        }
                        self._save_cache()
                        logger.info(f"[PTSSentinel] 🐦 X【PTS急変】投稿成功: {sym} {ev['name']} (ID: {tweet_id}, 日次累計: {self.daily_x_count}/{self.max_daily_x})")
                except Exception as ex:
                    logger.warning(f"[PTSSentinel] X投稿エラー: {ex}")

            # 2. Discord配信 (全件 / 重要度別)
            if self.enable_discord and self.discord_notifier:
                try:
                    color = 0xE74C3C if ev["change_pct"] < 0 else 0x2ECC71
                    fields = [
                        {"name": "🏢 銘柄", "value": f"**{ev['name']}** (`{sym}`)", "inline": True},
                        {"name": "📊 PTS変動率", "value": f"`{'+' if ev['change_pct']>=0 else ''}{ev['change_pct']:.2f}%` ({ev['pts_price']:.0f}円)", "inline": True},
                        {"name": "📈 PTS出来高", "value": f"`{ev['volume']:,}株` (約{ev['volume_ratio']:.1f}倍)", "inline": True},
                        {"name": "🧠 因果判定 (AI)", "value": f"**{ev['causal_label']}** (`CIS2: {ev['cis2_score']:.0f}`)", "inline": True},
                        {"name": "🎯 要因分析", "value": ev["causal_reason"], "inline": False},
                    ]
                    if tweet_id:
                        fields.append({"name": "🐦 X配信", "value": f"[ポスト確認](https://x.com/suzuhiroltd1/status/{tweet_id})", "inline": True})

                    self.discord_notifier.send_embed(
                        title=f"🌙 【日本株PTS夜間速報】{ev['name']} ({sym})",
                        description=f"PTS価格: **{ev['pts_price']:.0f}円** ({'+' if ev['change_pct']>=0 else ''}{ev['change_pct']:.2f}%) | 出来高: **{ev['volume']:,}株**",
                        fields=fields,
                        color=color,
                        target="news",
                    )
                except Exception as ex:
                    logger.warning(f"[PTSSentinel] Discord送信エラー: {ex}")

            count += 1

        return count

    def run_forever(self):
        """夜間常駐ループ"""
        logger.info("=" * 60)
        logger.info("  🌙 日本株PTS (夜間取引) リアルタイム監視センチネル 稼働開始")
        logger.info(f"  • 監視時間帯: 17:00 〜 23:59 JST")
        logger.info(f"  • 巡回インターバル: {self.poll_interval_sec} 秒")
        logger.info(f"  • 基準閾値: 変動率 ±{self.min_change_pct}% / 出来高 {self.min_volume}株以上")
        logger.info(f"  • X投稿有効化: {self.enable_x_post}")
        logger.info("=" * 60)

        while True:
            try:
                # 夜間取引時間帯 (または強制モード)
                if self.is_pts_active_hours():
                    c = self.scan_and_notify()
                    if c > 0:
                        logger.info(f"[PTSSentinel] 今回の巡回で {c} 件のPTS急変を処理しました。")
                else:
                    logger.debug("[PTSSentinel] 現在はPTS夜間取引時間外です (待機中)")
            except Exception as ex:
                logger.error(f"[PTSSentinel] 巡回ループエラー: {ex}")

            time.sleep(self.poll_interval_sec)


def main():
    sentinel = PTSSentinel()
    sentinel.run_forever()


if __name__ == "__main__":
    main()
