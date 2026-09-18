"""
世界の株価 (https://sekai-kabuka.com/pc-index.html) 急変監視モジュール
========================================================================
- 世界の主要市場（日米欧アジア株、為替、金利、コモディティ、暗号資産）を包括監視
- 前日比 1%以上変動した対象を自動抽出
- 高精細な騰落率バーチャート画像を動的生成
- Discord Webhook へグラフ画像付きでリッチEmbed通知を送信
"""

import os
import io
import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

import requests
from dotenv import load_dotenv
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

load_dotenv()
logger = logging.getLogger("news_pipeline.sekai_kabuka")

# 世界の株価 (sekai-kabuka.com) 掲載の主要市場シンボル定義
MONITORED_ASSETS = {
    # --- 日本市場 ---
    "^N225": {"name": "日経平均株価", "category": "🇯🇵 日本株"},
    "1306.T": {"name": "TOPIX (投信)", "category": "🇯🇵 日本株"},
    "2516.T": {"name": "東証グロース250", "category": "🇯🇵 日本株"},

    # --- 米国市場 ---
    "^DJI": {"name": "NYダウ", "category": "🇺🇸 米国株"},
    "^GSPC": {"name": "S&P500", "category": "🇺🇸 米国株"},
    "^IXIC": {"name": "ナスダック", "category": "🇺🇸 米国株"},
    "^SOX": {"name": "SOX 半導体指数", "category": "🇺🇸 米国株"},
    "^RUT": {"name": "ラッセル2000", "category": "🇺🇸 米国株"},
    "^VIX": {"name": "VIX 恐怖指数", "category": "🇺🇸 米国株"},

    # --- 欧州・アジア市場 ---
    "^FTSE": {"name": "イギリス FTSE100", "category": "🌍 欧州株"},
    "^GDAXI": {"name": "ドイツ DAX", "category": "🌍 欧州株"},
    "^FCHI": {"name": "フランス CAC40", "category": "🌍 欧州株"},
    "000001.SS": {"name": "上海総合指数", "category": "🌏 アジア株"},
    "^HSI": {"name": "香港ハンセン指数", "category": "🌏 アジア株"},
    "^TWII": {"name": "台湾加権指数", "category": "🌏 アジア株"},
    "^KS11": {"name": "韓国 KOSPI", "category": "🌏 アジア株"},

    # --- 為替・金利 ---
    "USDJPY=X": {"name": "ドル / 円", "category": "💱 為替・金利"},
    "EURJPY=X": {"name": "ユーロ / 円", "category": "💱 為替・金利"},
    "GBPJPY=X": {"name": "ポンド / 円", "category": "💱 為替・金利"},
    "EURUSD=X": {"name": "ユーロ / ドル", "category": "💱 為替・金利"},
    "^TNX": {"name": "米10年債利回り", "category": "💱 為替・金利"},

    # --- コモディティ ---
    "CL=F": {"name": "WTI 原油先物", "category": "🛢️ コモディティ"},
    "GC=F": {"name": "金ゴールド先物", "category": "🛢️ コモディティ"},
    "SI=F": {"name": "銀先物", "category": "🛢️ コモディティ"},
    "HG=F": {"name": "銅先物", "category": "🛢️ コモディティ"},

    # --- 暗号資産 ---
    "BTC-USD": {"name": "ビットコイン (BTC)", "category": "🪙 暗号資産"},
    "ETH-USD": {"name": "イーサリアム (ETH)", "category": "🪙 暗号資産"},
    "SOL-USD": {"name": "ソラナ (SOL)", "category": "🪙 暗号資産"},
}


class SekaiKabukaMonitor:
    """世界の株価 1%急変検知・グラフ生成・Discord通報エンジン"""

    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = (
            webhook_url
            or os.getenv("DISCORD_WEBHOOK_URL", "").strip()
            or os.getenv("DISCORD_TRADE_WEBHOOK", "").strip()
        )
        self._setup_japanese_font()

    def _setup_japanese_font(self):
        """日本語フォントの設定"""
        jp_fonts = [f.name for f in fm.fontManager.ttflist if "CJK" in f.name or "Noto" in f.name or "Gothic" in f.name]
        if any("Noto Sans CJK JP" in name for name in jp_fonts):
            plt.rcParams["font.family"] = "Noto Sans CJK JP"
        elif jp_fonts:
            plt.rcParams["font.family"] = jp_fonts[0]
        else:
            plt.rcParams["font.sans-serif"] = ["Noto Sans CJK JP", "DejaVu Sans", "sans-serif"]

    def fetch_market_changes(self) -> List[Dict[str, Any]]:
        """全監視銘柄の直近価格および前日比変動率(%)を取得"""
        symbols = list(MONITORED_ASSETS.keys())
        try:
            df = yf.download(symbols, period="5d", interval="1d", progress=False)["Close"]
        except Exception as e:
            logger.error(f"[SekaiKabuka] データダウンロード失敗: {e}")
            return []

        results = []
        for sym, meta in MONITORED_ASSETS.items():
            try:
                if sym in df and len(df[sym].dropna()) >= 2:
                    series = df[sym].dropna()
                    prev_close = float(series.iloc[-2])
                    curr_price = float(series.iloc[-1])
                    diff = curr_price - prev_close
                    pct_change = (diff / prev_close) * 100.0

                    results.append({
                        "symbol": sym,
                        "name": meta["name"],
                        "category": meta["category"],
                        "current_price": curr_price,
                        "previous_close": prev_close,
                        "diff": diff,
                        "pct_change": pct_change,
                    })
            except Exception:
                continue

        return results

    def filter_significant_movers(self, items: List[Dict[str, Any]], threshold_pct: float = 1.0) -> List[Dict[str, Any]]:
        """変動率の絶対値が threshold_pct (1.0%) 以上の銘柄を抽出してソート"""
        filtered = [item for item in items if abs(item["pct_change"]) >= threshold_pct]
        # 変動幅（絶対値）の大きい順にソート
        filtered.sort(key=lambda x: abs(x["pct_change"]), reverse=True)
        return filtered

    def generate_chart_image(self, movers: List[Dict[str, Any]]) -> bytes:
        """変動率の横棒グラフ（プロ仕様ダークテーマ）画像を生成"""
        # 表示数上限（最大15件）
        plot_items = list(reversed(movers[:15]))
        labels = [item["name"] for item in plot_items]
        values = [item["pct_change"] for item in plot_items]
        colors = ["#2ECC71" if v >= 0 else "#E74C3C" for v in values]

        fig_height = max(5.0, len(plot_items) * 0.45 + 1.5)
        fig, ax = plt.subplots(figsize=(10, fig_height), facecolor="#181825")
        ax.set_facecolor("#181825")

        bars = ax.barh(labels, values, color=colors, height=0.6, edgecolor="#313244", linewidth=0.8)

        # 1%ライン (基準線)
        ax.axvline(1.0, color="#F39C12", linestyle="--", linewidth=1.2, alpha=0.8, label="+1.0% 境界")
        ax.axvline(-1.0, color="#F39C12", linestyle="--", linewidth=1.2, alpha=0.8, label="-1.0% 境界")
        ax.axvline(0.0, color="#6c7086", linestyle="-", linewidth=0.8, alpha=0.5)

        # バーの先端に数値をプロット
        max_val = max([abs(v) for v in values]) if values else 1.0
        offset = max_val * 0.03 + 0.05
        for bar, val in zip(bars, values):
            x_pos = bar.get_width() + (offset if val >= 0 else -offset)
            ha = "left" if val >= 0 else "right"
            ax.text(
                x_pos,
                bar.get_y() + bar.get_height() / 2,
                f"{val:+.2f}%",
                va="center",
                ha=ha,
                color="#cdd6f4",
                fontweight="bold",
                fontsize=10,
            )

        ax.set_title("世界の株価 1%以上急変マーケット一覧", color="#cdd6f4", fontsize=15, pad=16, fontweight="bold")
        ax.tick_params(colors="#bac2de", labelsize=10)
        ax.grid(axis="x", color="#313244", linestyle=":", alpha=0.7)

        for spine in ax.spines.values():
            spine.set_color("#45475a")

        # マージン調整
        x_limit = max_val * 1.25
        ax.set_xlim(-x_limit, x_limit)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", dpi=150, facecolor=fig.get_facecolor())
        buf.seek(0)
        img_bytes = buf.getvalue()
        plt.close(fig)
        return img_bytes

    def send_discord_alert(self, movers: List[Dict[str, Any]], chart_image: bytes) -> bool:
        """Discord Webhook にグラフ画像付き Embed を送信"""
        if not self.webhook_url:
            logger.error("[SekaiKabuka] Discord Webhook URL が未設定です。")
            return False

        up_items = [m for m in movers if m["pct_change"] >= 0]
        down_items = [m for m in movers if m["pct_change"] < 0]

        up_text_lines = [
            f"• **{m['name']}**: `{m['pct_change']:+.2f}%` (現在値: `{m['current_price']:,.2f}`)"
            for m in up_items[:6]
        ]
        down_text_lines = [
            f"• **{m['name']}**: `{m['pct_change']:+.2f}%` (現在値: `{m['current_price']:,.2f}`)"
            for m in down_items[:6]
        ]

        fields = []
        if up_text_lines:
            fields.append({
                "name": f"🚀 急騰市場 (+1%以上: {len(up_items)}件)",
                "value": "\n".join(up_text_lines),
                "inline": False,
            })
        if down_text_lines:
            fields.append({
                "name": f"📉 急落市場 (-1%以下: {len(down_items)}件)",
                "value": "\n".join(down_text_lines),
                "inline": False,
            })

        t_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")
        embed = {
            "title": "🌍 【世界の株価 急変アラート】1%以上変動した市場・資産",
            "description": (
                f"**観測時刻**: `{t_now}`\n"
                f"[世界の株価 (sekai-kabuka.com)](https://sekai-kabuka.com/pc-index.html) の主要資産から、\n"
                f"**前日比で 1.0% 以上の急激な価格変動** が発生している対象（計 `{len(movers)}` 件）を抽出しました。"
            ),
            "color": 0x1E88E5,
            "fields": fields,
            "image": {"url": "attachment://sekai_kabuka_movers.png"},
            "footer": {"text": "Antigravity Global Market Sentinel 🌍"},
        }

        payload = {
            "username": "世界の株価 1%急変速報",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/3314/3314488.png",
            "embeds": [embed],
        }

        files = {
            "payload_json": (None, json.dumps(payload), "application/json"),
            "files[0]": ("sekai_kabuka_movers.png", chart_image, "image/png"),
        }

        try:
            res = requests.post(self.webhook_url, files=files, timeout=15)
            if res.status_code in (200, 204):
                logger.info("[SekaiKabuka] Discordへグラフ付き急変アラート送信成功！")
                return True
            else:
                logger.error(f"[SekaiKabuka] Discord送信失敗 (HTTP {res.status_code}): {res.text}")
                return False
        except Exception as e:
            logger.exception(f"[SekaiKabuka] Discord送信例外: {e}")
            return False

    def check_and_notify(self, threshold_pct: float = 1.0, force_notify: bool = False) -> bool:
        """全フロー実行（取得 ➔ 抽出 ➔ グラフ生成 ➔ Discord送信）"""
        print(f"[SekaiKabuka] 🔍 世界の株価 全市場の変動率を取得中...")
        all_items = self.fetch_market_changes()
        print(f"[SekaiKabuka] 取得完了: {len(all_items)} 銘柄")

        movers = self.filter_significant_movers(all_items, threshold_pct=threshold_pct)
        print(f"[SekaiKabuka] 1%以上変動した対象: {len(movers)} 銘柄")

        if not movers and not force_notify:
            print("[SekaiKabuka] 1%以上変動した対象がないため、通知をスキップしました。")
            return True

        if not movers and force_notify:
            # 強制通知時は全銘柄中トップのものを表示
            all_items.sort(key=lambda x: abs(x["pct_change"]), reverse=True)
            movers = all_items[:5]

        print("[SekaiKabuka] 📊 グラフ画像を生成中...")
        img_bytes = self.generate_chart_image(movers)

        print("[SekaiKabuka] 🚀 Discord へグラフ付きで通知を送信中...")
        success = self.send_discord_alert(movers, img_bytes)
        return success


def main():
    import argparse
    parser = argparse.ArgumentParser(description="世界の株価 1%急変監視チェッカー")
    parser.add_argument("--threshold", type=float, default=1.0, help="検知する変動率閾値 (%) デフォルト: 1.0")
    parser.add_argument("--force", action="store_true", help="1%以上の銘柄がなくても強制送信")
    args = parser.parse_args()

    monitor = SekaiKabukaMonitor()
    success = monitor.check_and_notify(threshold_pct=args.threshold, force_notify=args.force)
    print("完了ステータス:", "成功" if success else "失敗")


if __name__ == "__main__":
    main()
