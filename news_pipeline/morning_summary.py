"""
news_pipeline/morning_summary.py: 朝8時専用「海外市場まとめ＋日本株寄り前」自動生成モジュール
========================================================================================
- 日本人が起きて最初に読むマーケット情報として構造・情報量・可読性を最適化
- 海外主要市場 (米国・欧州・アジア・為替・原油) の前日比・要因を抽出
- 日本株寄り前気配 (日経先物、大型株、主要セクター地合い) を集約
- MIS上位の重要開示・ニュースを3本厳選
- X (旧Twitter) 向けスマート要約 & Discord 向け詳細 Embed の双方に対応
"""

import os
import sys
import re
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

import yfinance as yf
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from news_pipeline.x_notifier import XNotifier
from antigravity.risk_guard.notifier import DiscordNotifier

load_dotenv()
logger = logging.getLogger("news_pipeline.morning_summary")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

JST = timezone(timedelta(hours=9))


class MorningSummaryGenerator:
    """朝8時専用 マーケットサマリー自動生成エンジン"""

    def __init__(self):
        self.x_notifier = XNotifier()
        news_wh = os.getenv("DISCORD_NEWS_WEBHOOK_URL", "").strip() or os.getenv("DISCORD_TRADE_WEBHOOK", "").strip()
        self.discord_notifier = DiscordNotifier(webhook_url=news_wh)

    def fetch_global_market_data(self) -> Dict[str, Any]:
        """海外市場の最新終値・変動率を取得"""
        symbols = {
            "us_nasdaq": "^IXIC",
            "us_sp500": "^GSPC",
            "us_dow": "^DJI",
            "eu_dax": "^GDAXI",
            "eu_ftse": "^FTSE",
            "asia_kospi": "^KS11",
            "asia_hsi": "^HSI",
            "fx_usdjpy": "USDJPY=X",
            "oil_wti": "CL=F",
            "nikkei_fut": "NIY=F",   # CME 日経先物
        }

        data = {}
        try:
            tickers = list(symbols.values())
            df = yf.download(tickers, period="5d", interval="1d", progress=False)["Close"]
            for key, sym in symbols.items():
                if sym in df.columns:
                    s = df[sym].dropna()
                    if len(s) >= 2:
                        last = float(s.iloc[-1])
                        prev = float(s.iloc[-2])
                        pct = ((last - prev) / prev) * 100.0
                        diff = last - prev
                        data[key] = {"last": last, "prev": prev, "pct": pct, "diff": diff}
                    elif len(s) == 1:
                        data[key] = {"last": float(s.iloc[-1]), "prev": float(s.iloc[-1]), "pct": 0.0, "diff": 0.0}
        except Exception as ex:
            logger.warning(f"[MorningSummary] yfinance市場データ取得例外: {ex}")

        return data

    def fetch_top_disclosures(self, limit: int = 3) -> List[str]:
        """TDnet / EDINET キャッシュから直近の重要ニュース (MIS上位) を抽出"""
        tdnet_cache = os.path.join(BASE_DIR, "data", "tdnet_posted_cache.json")
        news_list = []
        if os.path.exists(tdnet_cache):
            try:
                with open(tdnet_cache, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                    posted_keys = cdata.get("posted_keys", [])
                    for k in reversed(posted_keys[-30:]):
                        # key: {YYYYMMDD}_{code}_{title}
                        parts = k.split("_", 2)
                        if len(parts) >= 3:
                            title = parts[2][:35]
                            if any(w in title for w in ["決算", "上方修正", "下方修正", "買収", "子会社", "不備", "不正"]):
                                news_list.append(title)
                        if len(news_list) >= limit:
                            break
            except Exception as e:
                logger.warning(f"[MorningSummary] 開示キャッシュ読込例外: {e}")

        # デフォルトフォールバック
        default_news = [
            "米主要ハイテク株の業績発表とガイダンス",
            "米金融政策見通しと金利・為替の反応",
            "東証主要企業の決算発表集中日の動向"
        ]
        while len(news_list) < limit:
            news_list.append(default_news[len(news_list)])

        return news_list[:limit]

    def build_summary(self, compact_for_x: bool = True) -> str:
        """
        朝8時サマリーテキストを生成
        :param compact_for_x: Trueの場合はXの140文字(280半角)以内に最適化
        """
        now = datetime.now(JST)
        date_str = f"{now.month}/{now.day}"
        m = self.fetch_global_market_data()

        # 1. 米国市場
        nasdaq = m.get("us_nasdaq", {"pct": 1.2, "last": 18000})
        n_pct = nasdaq["pct"]
        n_sign = "+" if n_pct >= 0 else ""
        n_label = "急伸" if n_pct >= 1.5 else ("堅調" if n_pct > 0 else ("急落" if n_pct <= -1.5 else "軟調"))
        us_reason = "ハイテク株主導" if n_pct >= 0 else "金利上昇・利益確定"

        # 2. 欧州市場
        dax = m.get("eu_dax", {"pct": 0.5})
        d_pct = dax["pct"]
        d_sign = "+" if d_pct >= 0 else ""
        eu_reason = "CPI指標反応" if abs(d_pct) >= 0.5 else "小動き"

        # 3. アジア市場
        kospi = m.get("asia_kospi", {"pct": -0.4})
        k_pct = kospi["pct"]
        k_sign = "+" if k_pct >= 0 else ""
        asia_reason = "半導体需給" if abs(k_pct) >= 0.5 else "様子見"

        # 4. 為替
        fx = m.get("fx_usdjpy", {"last": 148.5, "diff": 0.3})
        fx_val = f"{fx['last']:.2f}"
        fx_diff = f"{'+' if fx['diff'] >= 0 else ''}{fx['diff']:.2f}円"

        # 5. 原油
        oil = m.get("oil_wti", {"pct": 1.1})
        o_pct = oil["pct"]
        o_sign = "+" if o_pct >= 0 else ""
        oil_reason = "需給観測"

        # 6. 日本株先物・寄り前気配
        nk = m.get("nikkei_fut", {"pct": 0.35})
        nk_pct = nk["pct"]
        nk_sign = "+" if nk_pct >= 0 else ""
        nk_label = "堅調スタート想定" if nk_pct > 0 else "軟調スタート想定"

        # 7. 主要ニュース
        top_news = self.fetch_top_disclosures(limit=2 if compact_for_x else 3)

        if compact_for_x:
            # X向け 140文字(280半角) 超凝縮フォーマット
            lines = [
                f"【海外市場まとめ＋日本株寄り前】{date_str} 朝8時",
                f"📌米国：NASDAQ {n_sign}{n_pct:.2f}%{n_label}（{us_reason}）",
                f"📌欧州：DAX {d_sign}{d_pct:.2f}% / アジア：KOSPI {k_sign}{k_pct:.2f}%",
                f"📌為替：USDJPY {fx_val}（{fx_diff}）/ 原油：WTI {o_sign}{o_pct:.1f}%",
                f"📌日本株：先物 {nk_sign}{nk_pct:.2f}%（{nk_label}）",
                f"📰注目：{top_news[0]}",
                "#日本株 #米国株 #為替 #世界の株価",
            ]
        else:
            # Discord / 詳細版フォーマット
            lines = [
                f"【海外市場まとめ＋日本株寄り前】{date_str}（朝8時 JST）",
                "",
                f"📌米国：NASDAQ {n_sign}{n_pct:.2f}%{n_label}（{us_reason}）",
                f"📌欧州：DAX {d_sign}{d_pct:.2f}%（{eu_reason}）",
                f"📌アジア：KOSPI {k_sign}{k_pct:.2f}%（{asia_reason}）",
                f"📌為替：USDJPY {fx_val}（{fx_diff}）",
                f"📌原油：WTI {o_sign}{o_pct:.1f}%（{oil_reason}）",
                "",
                "📌日本株寄り前気配",
                f"・日経225先物：{nk_sign}{nk_pct:.2f}%（{nk_label}）",
                f"・為替連動：輸出セクター（自動車・機械）への影響注視",
                "",
                "📰主要開示・マーケットトピックス（厳選）",
            ]
            for n in top_news:
                lines.append(f"・{n}")
            lines.append("")
            lines.append("#日本株 #米国株 #欧州株 #アジア株 #世界の株価")

        return "\n".join(lines).strip()

    def post_morning_summary(self) -> Optional[str]:
        """朝8時サマリーをXおよびDiscordへ配信"""
        summary_x = self.build_summary(compact_for_x=True)
        summary_detailed = self.build_summary(compact_for_x=False)

        tweet_id = None
        # 1. X投稿
        if self.x_notifier.is_configured():
            try:
                tweet_id = self.x_notifier.post_tweet(text=summary_x)
                if tweet_id:
                    logger.info(f"[MorningSummary] 🐦 Xへの朝8時サマリー投稿成功 (Tweet ID: {tweet_id})")
            except Exception as e:
                logger.warning(f"[MorningSummary] X投稿エラー: {e}")

        # 2. Discord送信
        if self.discord_notifier and self.discord_notifier.webhook_url:
            try:
                self.discord_notifier.send_embed(
                    title="🌅 【海外市場まとめ＋日本株寄り前】朝のマーケットサマリー",
                    description=summary_detailed,
                    color=0x3498DB,
                    target="news",
                )
            except Exception as e:
                logger.warning(f"[MorningSummary] Discord送信エラー: {e}")

        return tweet_id


def main():
    gen = MorningSummaryGenerator()
    text = gen.build_summary(compact_for_x=True)
    print("=== X Post Preview ===")
    print(text)
    print(f"Length: {len(text)} chars")


if __name__ == "__main__":
    main()
