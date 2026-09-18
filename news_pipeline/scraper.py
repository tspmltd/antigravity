"""
市場データ・ニュース情報収集モジュール (scraper.py)
9つのタイムスケジュールに対応したデータ収集関数群:
07:00 海外市場のまとめ (米株指数、金利、為替、商品、指標)
07:30 海外個別企業ニュース (注目テック・半導体等の決算・ヘッドライン)
08:00 日本株個別ニュース (適時開示・重要材料)
08:30 PTSトップ5/ワースト5 & S高S安
12:00 社会ニュース (昼時点の国内・政治経済)
16:00 日本株総括 (日経平均、TOPIX、グロース、セクター動向)
17:00 PTSトップ5/ワースト5 (夕方時点)
19:00 海外市場まとめ (欧州寄り付き・アジア振り返り)
21:30 海外市場寄り付き概要 (NY寄り付き・指標速報)
"""

import time
import logging
from typing import Dict, List, Any, Optional
import requests
from bs4 import BeautifulSoup
import feedparser
import yfinance as yf

logger = logging.getLogger("news_pipeline.scraper")

# HTTPリクエスト共通設定
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}
REQUEST_TIMEOUT = 8


# -------------------------------------------------------------
# 汎用ヘルパー関数
# -------------------------------------------------------------

def fetch_yfinance_quotes(tickers: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """
    yfinanceから銘柄の最新価格・前日比・騰落率を取得する。
    tickers: {"表示名": "ティッカーシンボル"}
    """
    results = {}
    for display_name, symbol in tickers.items():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="5d", interval="1d")
            if hist.empty or len(hist) < 1:
                results[display_name] = {"price": "N/A", "change": "-", "change_pct": "-"}
                continue

            last_close = hist["Close"].iloc[-1]
            prev_close = hist["Close"].iloc[-2] if len(hist) >= 2 else last_close
            change = last_close - prev_close
            pct = (change / prev_close) * 100 if prev_close != 0 else 0.0

            sign = "+" if change >= 0 else ""
            results[display_name] = {
                "price": f"{last_close:,.2f}",
                "change": f"{sign}{change:,.2f}",
                "change_pct": f"{sign}{pct:.2f}%"
            }
            time.sleep(0.2)  # レートリミット対策
        except Exception as e:
            logger.warning(f"[Scraper] yfinance取得スキップ ({display_name}/{symbol}): {e}")
            results[display_name] = {"price": "取得スキップ", "change": "-", "change_pct": "-"}

    return results


def fetch_rss_headlines(feed_url: str, max_items: int = 5) -> List[Dict[str, str]]:
    """RSSフィードからヘッドライン記事を取得する"""
    items = []
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:max_items]:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            summary = getattr(entry, "summary", "").strip()
            # HTMLタグ除去
            if summary:
                soup = BeautifulSoup(summary, "html.parser")
                summary = soup.get_text(strip=True)

            if title:
                items.append({
                    "title": title,
                    "link": link,
                    "summary": summary[:200]
                })
        time.sleep(0.3)
    except Exception as e:
        logger.warning(f"[Scraper] RSS取得スキップ ({feed_url}): {e}")
    return items


def fetch_kabutan_table(url: str) -> List[List[str]]:
    """株探のstock_tableをスクレイピングして行・列リストを取得"""
    rows_data = []
    try:
        res = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200:
            logger.warning(f"[Scraper] 株探アクセスエラー: HTTP {res.status_code} ({url})")
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table", class_="stock_table")
        if not table:
            return []

        for tr in table.find_all("tr")[1:]:  # ヘッダー除外
            cols = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            if cols:
                rows_data.append(cols)
        time.sleep(0.5)
    except Exception as e:
        logger.warning(f"[Scraper] 株探スクレイピングスキップ ({url}): {e}")
    return rows_data


# -------------------------------------------------------------
# スケジュール別データ収集関数
# -------------------------------------------------------------

def scrape_0700_us_market() -> Dict[str, Any]:
    """
    07:00 海外市場のまとめ
    米株主要指数 (S&P500, Nasdaq)、米10年債利回り、ドル円、原油、ゴールド、市況ヘッドライン
    """
    tickers = {
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
        "米10年債利回り": "^TNX",
        "ドル円 (USD/JPY)": "JPY=X",
        "WTI原油": "CL=F",
        "NY金先物": "GC=F"
    }
    quotes = fetch_yfinance_quotes(tickers)

    # 市況ニュース
    rss_url = "https://news.google.com/rss/search?q=NYダウ+OR+ナスダック+OR+米国株+when:24h&hl=ja&gl=JP&ceid=JP:ja"
    news = fetch_rss_headlines(rss_url, max_items=4)

    return {
        "title": "🌅 海外市場のまとめ (07:00 JST)",
        "indicators": quotes,
        "news": news
    }


def scrape_0730_foreign_stocks() -> Dict[str, Any]:
    """
    07:30 海外個別企業ニュース
    注目テック・半導体 (NVDA, AAPL, MSFT, TSLA, AMD, TSM等) の決算・ヘッドライン
    """
    tickers = {
        "NVIDIA (NVDA)": "NVDA",
        "Apple (AAPL)": "AAPL",
        "Microsoft (MSFT)": "MSFT",
        "Tesla (TSLA)": "TSLA",
        "TSMC (TSM)": "TSM",
    }
    quotes = fetch_yfinance_quotes(tickers)

    rss_url = "https://news.google.com/rss/search?q=NVIDIA+OR+Apple+OR+Tesla+OR+Microsoft+OR+TSMC+when:24h&hl=ja&gl=JP&ceid=JP:ja"
    news = fetch_rss_headlines(rss_url, max_items=5)

    return {
        "title": "🌐 海外個別企業・テック動向 (07:30 JST)",
        "indicators": quotes,
        "news": news
    }


def scrape_0800_japan_stocks() -> Dict[str, Any]:
    """
    08:00 日本株個別ニュース
    適時開示情報 (インパクト開示)・国内企業ヘッドライン
    """
    url = "https://kabutan.jp/warning/?mode=4_4"  # インパクト開示情報
    rows = fetch_kabutan_table(url)

    disclosures = []
    for r in rows[:6]:
        if len(r) >= 9:
            disclosures.append({
                "code": r[0],
                "name": r[1],
                "disclosure": r[4],
                "price": r[5],
                "change_pct": r[8]
            })

    # Yahoo経済・企業ニュース
    news = fetch_rss_headlines("https://news.yahoo.co.jp/rss/categories/business.xml", max_items=4)

    return {
        "title": "🇯🇵 日本株個別ニュース・適時開示 (08:00 JST)",
        "disclosures": disclosures,
        "news": news
    }


def scrape_0830_pts_and_stops() -> Dict[str, Any]:
    """
    08:30 PTSトップ5/ワースト5 & S高S安
    前夜PTSランキング & 前営業日ストップ高/安銘柄
    """
    # 1. PTS夜間上昇率
    pts_up_rows = fetch_kabutan_table("https://kabutan.jp/warning/pts_night_price_increase")
    pts_top = []
    for r in pts_up_rows[:5]:
        if len(r) >= 9:
            pts_top.append(f"{r[1]} ({r[0]}): `{r[6]}円` ({r[8]})")

    # 2. PTS夜間下落率
    pts_down_rows = fetch_kabutan_table("https://kabutan.jp/warning/pts_night_price_decrease")
    pts_worst = []
    for r in pts_down_rows[:5]:
        if len(r) >= 9:
            pts_worst.append(f"{r[1]} ({r[0]}): `{r[6]}円` ({r[8]})")

    # 3. ストップ高
    stop_high_rows = fetch_kabutan_table("https://kabutan.jp/warning/?mode=3_1")
    stop_high = []
    for r in stop_high_rows[:5]:
        if len(r) >= 9:
            stop_high.append(f"{r[1]} ({r[0]}): `{r[5]}円` ({r[8]})")

    # 4. ストップ安
    stop_low_rows = fetch_kabutan_table("https://kabutan.jp/warning/?mode=3_2")
    stop_low = []
    for r in stop_low_rows[:5]:
        if len(r) >= 9:
            stop_low.append(f"{r[1]} ({r[0]}): `{r[5]}円` ({r[8]})")

    return {
        "title": "⚡ 朝のPTSランキング & ストップ高安 (08:30 JST)",
        "pts_top": pts_top or ["情報なし"],
        "pts_worst": pts_worst or ["情報なし"],
        "stop_high": stop_high or ["情報なし (該当なし)"],
        "stop_low": stop_low or ["情報なし (該当なし)"]
    }


def scrape_1200_society_news() -> Dict[str, Any]:
    """
    12:00 社会ニュース
    昼時点の国内主要ニュース、政治・経済の速報
    """
    nhk_news = fetch_rss_headlines("https://www.nhk.or.jp/rss/news/cat0.xml", max_items=5)
    yahoo_topics = fetch_rss_headlines("https://news.yahoo.co.jp/rss/topics/top-picks.xml", max_items=5)

    return {
        "title": "🏛 昼の社会・国内政治経済ニュース (12:00 JST)",
        "nhk_news": nhk_news,
        "yahoo_topics": yahoo_topics
    }


def scrape_1600_japan_summary() -> Dict[str, Any]:
    """
    16:00 日本株総括
    日経平均、TOPIX、グロース大引け結果、東証業種別動向
    """
    tickers = {
        "日経平均": "^N225",
        "TOPIX (ETF)": "1306.T",
        "グロース250 (ETF)": "2516.T"
    }
    quotes = fetch_yfinance_quotes(tickers)

    # 業種別ランキング
    sector_rows = fetch_kabutan_table("https://kabutan.jp/warning/?mode=9_1")
    top_sectors = []
    worst_sectors = []
    if sector_rows:
        # 上位3業種
        for r in sector_rows[:3]:
            if len(r) >= 8:
                top_sectors.append(f"🔺 {r[1]}: {r[7]}")
        # 下位3業種 (リスト末尾)
        for r in sector_rows[-3:]:
            if len(r) >= 8:
                worst_sectors.append(f"🔻 {r[1]}: {r[7]}")

    rss_url = "https://news.google.com/rss/search?q=日経平均+大引け+OR+東証大引け+when:24h&hl=ja&gl=JP&ceid=JP:ja"
    news = fetch_rss_headlines(rss_url, max_items=3)

    return {
        "title": "📊 日本株 大引け総括 (16:00 JST)",
        "indicators": quotes,
        "top_sectors": top_sectors or ["情報なし"],
        "worst_sectors": worst_sectors or ["情報なし"],
        "news": news
    }


def scrape_1700_pts_ranking() -> Dict[str, Any]:
    """
    17:00 PTSトップ5/ワースト5
    夕方時点（夜間取引開始直後）のPTSランキング
    """
    pts_up_rows = fetch_kabutan_table("https://kabutan.jp/warning/pts_night_price_increase")
    pts_top = []
    for r in pts_up_rows[:5]:
        if len(r) >= 9:
            pts_top.append(f"{r[1]} ({r[0]}): `{r[6]}円` ({r[8]})")

    pts_down_rows = fetch_kabutan_table("https://kabutan.jp/warning/pts_night_price_decrease")
    pts_worst = []
    for r in pts_down_rows[:5]:
        if len(r) >= 9:
            pts_worst.append(f"{r[1]} ({r[0]}): `{r[6]}円` ({r[8]})")

    return {
        "title": "🌙 夕方PTSランキング (17:00 JST)",
        "pts_top": pts_top or ["夜間取引開始待ち / 情報なし"],
        "pts_worst": pts_worst or ["夜間取引開始待ち / 情報なし"]
    }


def scrape_1900_europe_asia_summary() -> Dict[str, Any]:
    """
    19:00 海外市場まとめ
    欧州市場寄り付き動向、アジア市場振り返り
    """
    tickers = {
        "英 FTSE100": "^FTSE",
        "独 DAX": "^GDAXI",
        "仏 CAC40": "^FCHI",
        "香港 ハンセン": "^HSI",
        "上海 総合": "000001.SS",
        "日経平均": "^N225"
    }
    quotes = fetch_yfinance_quotes(tickers)

    rss_url = "https://news.google.com/rss/search?q=欧州株+寄り付き+OR+アジア市場+when:24h&hl=ja&gl=JP&ceid=JP:ja"
    news = fetch_rss_headlines(rss_url, max_items=3)

    return {
        "title": "🌍 欧州寄り付き & アジア市場まとめ (19:00 JST)",
        "indicators": quotes,
        "news": news
    }


def scrape_2130_us_market_open() -> Dict[str, Any]:
    """
    21:30 海外市場寄り付き概要
    NY市場寄り付き状況、夜間発表の米経済指標予定
    """
    tickers = {
        "NYダウ": "^DJI",
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
        "ドル円 (USD/JPY)": "JPY=X",
        "米10年債利回り": "^TNX"
    }
    quotes = fetch_yfinance_quotes(tickers)

    rss_url = "https://news.google.com/rss/search?q=米株式市場+寄り付き+OR+米経済指標+when:24h&hl=ja&gl=JP&ceid=JP:ja"
    news = fetch_rss_headlines(rss_url, max_items=4)

    return {
        "title": "🔔 NY市場寄り付き速報 (21:30 JST)",
        "indicators": quotes,
        "news": news
    }


# スケジュールスロット名と実行関数のマッピング
SLOT_SCRAPERS = {
    "07:00": scrape_0700_us_market,
    "07:30": scrape_0730_foreign_stocks,
    "08:00": scrape_0800_japan_stocks,
    "08:30": scrape_0830_pts_and_stops,
    "12:00": scrape_1200_society_news,
    "16:00": scrape_1600_japan_summary,
    "17:00": scrape_1700_pts_ranking,
    "19:00": scrape_1900_europe_asia_summary,
    "21:30": scrape_2130_us_market_open,
}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("スクレイパー単体テスト (07:00 海外市場)...")
    res = scrape_0700_us_market()
    print("Title:", res["title"])
    print("Indicators:", res["indicators"])
    print("News count:", len(res["news"]))
