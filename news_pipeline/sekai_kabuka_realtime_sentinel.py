#!/usr/bin/env python3
"""
世界の株価 リアルタイム急変センチネル (sekai_kabuka_realtime_sentinel.py)
========================================================================
- 世界の株価 (https://sekai-kabuka.com/pc-index.html) 掲載の全30市場を常時監視 (60秒巡回)
- 市場価格が ±1.0% を突破（ブレイクアウト）した瞬間を即座にリアルタイム検知
- スマート・ステート管理により、同一水準での通知スパム（連打）を完全防止
  - 初回 ±1.0% 突破時に即時速報
  - さらに変動が拡大（±1.5%, ±2.0%, ±2.5%...）した際に追加速報
  - 急反転（プラス転・マイナス転）時に即時速報
- 新たに急変した銘柄をハイライトした高精細バーチャート画像を動的生成
- Discord Webhook へリアルタイム画像付き Embed 速報を送信
"""

import os
import io
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from antigravity.discord_mute import discord_muted

import time
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import requests
from dotenv import load_dotenv
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import matplotlib.font_manager as fm
import matplotlib.patches as patches

plt.rcParams["font.family"] = "Noto Sans CJK JP"

from news_pipeline.x_notifier import send_breakout_tweet, DEFAULT_MOVER_X_BUSY_DAILY_CAP
from news_pipeline.post_optimizer import compute_chart_colors, optimize_tags, should_release_sentinel



load_dotenv(override=True)

# ロガー設定
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "sekai_kabuka_sentinel.log")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8")
    ]
)
logger = logging.getLogger("sekai_kabuka_sentinel")

# 世界の株価 (sekai-kabuka.com) 掲載の主要30市場シンボル
MONITORED_ASSETS = {
    # --- 日本市場 ---
    "^N225": {"name": "日経平均株価", "category": "🇯🇵 日本株"},
    "1306.T": {"name": "TOPIX (東証株価指数)", "category": "🇯🇵 日本株"},
    "2516.T": {"name": "東証グロース250", "category": "🇯🇵 日本株"},

    # --- 米国市場 ---
    "^DJI": {"name": "NYダウ", "category": "🇺🇸 米国株"},
    "^GSPC": {"name": "S&P500", "category": "🇺🇸 米国株"},
    "^IXIC": {"name": "ナスダック", "category": "🇺🇸 米国株"},
    "^SOX": {"name": "SOX 半導体指数", "category": "🇺🇸 米国株"},
    "^RUT": {"name": "ラッセル2000 (中小型株)", "category": "🇺🇸 米国株"},
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
    "USDJPY=X": {"name": "ドル / 円 (USD/JPY)", "category": "💱 為替・金利"},
    "EURJPY=X": {"name": "ユーロ / 円 (EUR/JPY)", "category": "💱 為替・金利"},
    "GBPJPY=X": {"name": "ポンド / 円 (GBP/JPY)", "category": "💱 為替・金利"},
    "EURUSD=X": {"name": "ユーロ / ドル (EUR/USD)", "category": "💱 為替・金利"},
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


def is_market_active(category: str, sym: str = "") -> bool:
    """銘柄が現在リアルタイム取引中（アクティブ）か判定 (JST基準)"""
    now = datetime.now(timezone(timedelta(hours=9)))
    weekday = now.weekday()  # 0: Mon, 6: Sun
    hour = now.hour
    minute = now.minute
    hm = hour + minute / 60.0

    # 🪙 暗号資産: 24時間365日常時アクティブ
    if "暗号資産" in category or sym in ("BTC-USD", "ETH-USD", "SOL-USD"):
        return True

    # 土日は原則休場
    if weekday == 5 and hm >= 7.0:
        return False
    if weekday == 6:
        return False
    if weekday == 0 and hm < 6.0:
        return False

    # 💱 為替・金利 / 🛢️ コモディティ: 平日ほぼ24時間
    if "為替" in category or "金利" in category or "コモディティ" in category:
        return True

    # 🇯🇵 日本株: 09:00 - 15:35 JST
    if "日本株" in category:
        return 9.0 <= hm <= 15.6

    # 🌏 アジア株: 09:30 - 17:30 JST
    if "アジア株" in category:
        return 9.5 <= hm <= 17.5

    # 🌍 欧州株: 16:00 - 01:30 JST
    if "欧州株" in category:
        return 16.0 <= hm or hm <= 1.5

    # 🇺🇸 米国株: 21:30 - 06:30 JST
    if "米国株" in category:
        return 21.5 <= hm or hm <= 6.5

    return True


class RealtimeMoverSentinel:
    """リアルタイム1%急変検知・通知エンジン"""

    def __init__(
        self,
        poll_interval_sec: float = 60.0,
        base_threshold_pct: float = 1.0,
        step_threshold_pct: float = 0.5,
        webhook_url: Optional[str] = None
    ):
        self.poll_interval_sec = poll_interval_sec
        self.base_threshold_pct = base_threshold_pct
        self.step_threshold_pct = step_threshold_pct
        self.webhook_url = (
            webhook_url
            or os.getenv("DISCORD_NEWS_WEBHOOK_URL", "").strip()
            or os.getenv("DISCORD_TRADE_WEBHOOK", "").strip()
            or os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        )
        self.is_running = False

        # キャッシュファイルのパス
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.price_cache_path = os.path.join(base_dir, "data", "market_price_cache.json")
        self.states_cache_path = os.path.join(base_dir, "data", "sentinel_notified_states.json")
        self.x_states_cache_path = os.path.join(base_dir, "data", "sentinel_x_posted_states.json")

        # キャッシュ読み込み（プロセス再起動時も引き継ぎ）
        self.market_price_cache: Dict[str, Dict[str, Any]] = self._load_price_cache()
        self.notified_states: Dict[str, Dict[str, Any]] = self._load_notified_states()
        self.x_posted_states: Dict[str, Dict[str, Any]] = self._load_x_states()
        self.max_daily_x: int = int(os.getenv("SEKAI_MAX_DAILY_X", str(DEFAULT_MOVER_X_BUSY_DAILY_CAP)))  # 急変 X 繁忙枠。グローバル48は別。

        self.stable_cycle_count: int = 0
        self.active_shock_level: str = "NORMAL"
        try:
            from antigravity.quant_pipeline.market_shock_sentinel import MarketShockSentinel
            curr_shock = MarketShockSentinel().get_current_shock()
            if curr_shock.shock_active:
                self.active_shock_level = curr_shock.shock_level.upper()
                logger.info(f"[Sentinel] 🛡️ 起動時ショック状態引継ぎ: レベル={self.active_shock_level}")
        except Exception:
            pass

        self._setup_japanese_font()

    def remaining_mover_x_posts(self, daily_count: Optional[int] = None) -> int:
        """急変 overlay の本日残 X 枠。Discord は検知全件、X は繁忙上限 max_daily_x（既定12）。"""
        if daily_count is None:
            today_str = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
            daily_meta = self.x_posted_states.get("_daily_meta", {}) or {}
            if daily_meta.get("date") != today_str:
                daily_count = 0
            else:
                daily_count = int(daily_meta.get("count", 0))
        return max(0, int(self.max_daily_x) - int(daily_count))

    def _load_price_cache(self) -> Dict[str, Dict[str, Any]]:
        if os.path.exists(self.price_cache_path):
            try:
                with open(self.price_cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[Sentinel] 価格キャッシュ読込エラー: {e}")
        return {}

    def _save_price_cache(self):
        try:
            os.makedirs(os.path.dirname(self.price_cache_path), exist_ok=True)
            with open(self.price_cache_path, "w", encoding="utf-8") as f:
                json.dump(self.market_price_cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[Sentinel] 価格キャッシュ保存エラー: {e}")

    def _load_notified_states(self) -> Dict[str, Dict[str, Any]]:
        if os.path.exists(self.states_cache_path):
            try:
                with open(self.states_cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[Sentinel] 通知ステート読込エラー: {e}")
        return {}

    def _save_notified_states(self):
        try:
            os.makedirs(os.path.dirname(self.states_cache_path), exist_ok=True)
            with open(self.states_cache_path, "w", encoding="utf-8") as f:
                json.dump(self.notified_states, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[Sentinel] 通知ステート保存エラー: {e}")

    def _load_x_states(self) -> Dict[str, Dict[str, Any]]:
        if os.path.exists(self.x_states_cache_path):
            try:
                with open(self.x_states_cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[Sentinel] X投稿ステート読込エラー: {e}")
        return {}

    def _save_x_states(self):
        try:
            os.makedirs(os.path.dirname(self.x_states_cache_path), exist_ok=True)
            with open(self.x_states_cache_path, "w", encoding="utf-8") as f:
                json.dump(self.x_posted_states, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[Sentinel] X投稿ステート保存エラー: {e}")

    def _setup_japanese_font(self):
        jp_fonts = [f.name for f in fm.fontManager.ttflist if "CJK" in f.name or "Noto" in f.name]
        if any("Noto Sans CJK JP" in name for name in jp_fonts):
            plt.rcParams["font.family"] = "Noto Sans CJK JP"
        elif jp_fonts:
            plt.rcParams["font.family"] = jp_fonts[0]
        else:
            plt.rcParams["font.sans-serif"] = ["Noto Sans CJK JP", "DejaVu Sans", "sans-serif"]

    def fetch_all_prices(self) -> Dict[str, Dict[str, Any]]:
        """全監視銘柄の現在価格および前日比変動率(%)を取得 (閉場中価格完全固定ガード付き)"""
        symbols = list(MONITORED_ASSETS.keys())
        try:
            df = yf.download(symbols, period="5d", interval="1d", progress=False)["Close"]
        except Exception as e:
            logger.error(f"[Sentinel] 株価データダウンロード失敗: {e}")
            return {}

        results = {}
        cache_updated = False
        for sym, meta in MONITORED_ASSETS.items():
            category = meta.get("category", "")
            active = is_market_active(category, sym)

            # 🛡️ 閉場中固定ルール:
            # 閉場している市場は、データソースが深夜に何を返そうと引け値（キャッシュ）を完全固定して返却。
            # これにより「閉場中なのに値が更新されて新規変動と誤認される」問題を物理的に完全遮断。
            if not active:
                cached = self.market_price_cache.get(sym)
                if cached is not None:
                    results[sym] = cached
                    continue

            try:
                if sym in df and len(df[sym].dropna()) >= 2:
                    series = df[sym].dropna()
                    prev_close = float(series.iloc[-2])
                    curr_price = float(series.iloc[-1])
                    diff = curr_price - prev_close
                    pct_change = (diff / prev_close) * 100.0

                    item_data = {
                        "symbol": sym,
                        "name": meta["name"],
                        "category": meta["category"],
                        "current_price": curr_price,
                        "previous_close": prev_close,
                        "diff": diff,
                        "pct_change": pct_change,
                    }
                    results[sym] = item_data
                    # 開場中の最新確定値をキャッシュに記録
                    self.market_price_cache[sym] = item_data
                    cache_updated = True
            except Exception:
                continue

        if cache_updated:
            self._save_price_cache()

        return results

    def detect_breakout_events(self, current_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        1%突破（および変動拡大）イベントを判定・抽出
        """
        now = time.time()
        triggered_events = []

        for sym, item in current_data.items():
            pct = item["pct_change"]
            abs_pct = abs(pct)
            category = item.get("category", "")

            # 0.5%刻みのレベル算出 (1.0, 1.5, 2.0, 2.5...)
            sign = 1 if pct > 0 else -1
            level_idx = int(abs_pct / self.step_threshold_pct)
            current_level = (level_idx * self.step_threshold_pct) * sign

            # VIX 特殊ルール:
            # VIXの下落(マイナス)は市場安定化(恐怖後退)であり、急変ショックではない！
            # VIXが急騰（+5.0%以上）した場合のみ警戒アラートを発出
            if sym == "^VIX":
                if pct < 5.0:
                    continue

            # 閉場中の市場は昨夜の静止データのため、リアルタイム急変速報トリガーから除外
            # （※右側の参考一覧パネルには表示されます）
            if not is_market_active(category, sym):
                # 閉場中に1%を超えている市場は、次回開場時にいきなり初回突破と誤認されないようステートを事前登録
                if abs_pct >= self.base_threshold_pct and sym not in self.notified_states:
                    self.notified_states[sym] = {
                        "last_level": current_level,
                        "last_time": now,
                        "last_pct": pct,
                    }
                    self._save_notified_states()
                continue

            # チャタリング防止 (ヒステリシス):
            # 一度1%を突破した銘柄は、0.7%未満まで十分に落ち着くまではステートを保持
            # これにより 0.99% と 1.01% の境界を行き来する連打ノイズを完全遮断
            reset_threshold_pct = 0.70
            if abs_pct < reset_threshold_pct:
                if sym in self.notified_states:
                    del self.notified_states[sym]
                    self._save_notified_states()
                continue

            prev_state = self.notified_states.get(sym)

            should_trigger = False
            event_type = ""

            if prev_state is None:
                # 初回 1% 突破！
                should_trigger = True
                event_type = "🚨 初回 1%突破"
            else:
                prev_level = prev_state.get("last_level", 0.0)
                # 符号が反転した場合（急騰から急落へ転落、またはその逆）
                if (prev_level > 0 and current_level < 0) or (prev_level < 0 and current_level > 0):
                    should_trigger = True
                    event_type = "⚡ 急反転 (符号変化)"
                # 変動幅がさらに次のマイルストーン（0.5%刻み）に拡大した場合
                elif abs(current_level) > abs(prev_level):
                    should_trigger = True
                    event_type = f"📈 変動拡大 ({abs(current_level):.1f}%到達)"
                # 同じレベルに滞在していても、2時間以上経過した場合はリマインド通知
                elif now - prev_state.get("last_time", 0.0) >= 7200.0:
                    should_trigger = True
                    event_type = f"⏱ 継続警戒 ({abs(current_level):.1f}%水準)"

            if should_trigger:
                triggered_events.append({
                    **item,
                    "event_type": event_type,
                    "level": current_level,
                })
                # ステート更新
                self.notified_states[sym] = {
                    "last_level": current_level,
                    "last_time": now,
                    "last_pct": pct,
                }
                self._save_notified_states()

        return triggered_events

    def generate_breakout_chart(
        self,
        all_movers: List[Dict[str, Any]],
        triggered_symbols: List[str]
    ) -> bytes:
        """
        急変銘柄の時系列折れ線チャート (高視認性 16:9 モダンダーク) を生成。
        - 1%突破した主役銘柄のイントラデイ推移 (JST時間軸・急変の勢いと傾きを可視化)
        - 0.0%基準線 & ±1.0%急変警戒ライン (重なり防止ラベル配置)
        - 最新値マーカー & 吹き出しアノテーションバナー (矢印ポインタ付き)
        - 右側にその他1%変動市場のサマリーパネル (文字切れなし・高コントラストバッジ)
        """
        # 主役に選ぶ銘柄 (今回トリガー銘柄を最優先、なければアクティブ市場優先、なければ最大変動銘柄)
        primary_sym = None
        if triggered_symbols:
            primary_sym = triggered_symbols[0]
        else:
            active_movers = [m for m in all_movers if is_market_active(m.get("category", ""), m.get("symbol", ""))]
            if active_movers:
                primary_sym = active_movers[0]["symbol"]
            elif all_movers:
                primary_sym = all_movers[0]["symbol"]

        primary_item = MONITORED_ASSETS.get(primary_sym, {"name": primary_sym or "市場指数"})
        primary_name = primary_item.get("name", primary_sym)

        # 1. イントラデイ時系列データを取得 (当日・5分足)
        pct_series = None
        hist_data = None
        if primary_sym:
            try:
                tk = yf.Ticker(primary_sym)
                hist = tk.history(period="1d", interval="5m")
                if len(hist) < 10:
                    hist_2d = tk.history(period="2d", interval="5m")
                    if len(hist_2d) >= 15:
                        hist = hist_2d

                if len(hist) >= 3:
                    # JSTにタイムゾーン統一
                    if hist.index.tzinfo is None:
                        hist.index = hist.index.tz_localize("UTC").tz_convert("Asia/Tokyo")
                    else:
                        hist.index = hist.index.tz_convert("Asia/Tokyo")

                    prev_c = tk.info.get("previousClose") or hist["Close"].iloc[0]
                    if prev_c and prev_c > 0:
                        pct_series = (hist["Close"] - prev_c) / prev_c * 100.0
                        hist_data = hist
            except Exception as e:
                logger.warning(f"[Sentinel] イントラデイデータ取得例外 ({primary_sym}): {e}")

        # 2. テーマカラー・設定 (高視認性モダンダーク 16:9)
        bg_canvas = "#0c1017"      # 深みのあるモダンダーク
        bg_card = "#151b26"        # カード背景
        border_color = "#2d3748"   # 境界線
        grid_color = "#1e293b"     # グリッド
        text_main = "#ffffff"      # メイン白文字
        text_sub = "#94a3b8"       # サブ薄グレー文字

        latest_pct = 0.0
        latest_px = 0.0
        latest_time = None
        if pct_series is not None and len(pct_series) >= 1:
            latest_pct = pct_series.iloc[-1]
            latest_px = hist_data["Close"].iloc[-1]
            latest_time = hist_data.index[-1]
        elif all_movers:
            latest_pct = all_movers[0]["pct_change"]
            latest_px = all_movers[0].get("current_price", 0.0)

        is_up = latest_pct >= 0
        line_color = "#00e676" if is_up else "#ff3366"  # ネオングリーン vs ビビッドレッド
        fill_color = "#00e676" if is_up else "#ff3366"
        marker_color = "#ffd600"                         # ゴールドアクセント

        # 16:9 比率 (13.33 x 7.5 インチ)
        fig = plt.figure(figsize=(13.33, 7.5), facecolor=bg_canvas)
        gs = gridspec.GridSpec(1, 2, width_ratios=[3.0, 1.25], wspace=0.14, left=0.07, right=0.96, top=0.88, bottom=0.10)

        # --- [左パネル] 時系列折れ線グラフ ---
        ax0 = fig.add_subplot(gs[0])
        ax0.set_facecolor(bg_card)

        if pct_series is not None and len(pct_series) >= 2:
            # 折れ線 & グラデーション塗りつぶし
            ax0.plot(hist_data.index, pct_series.values, color=line_color, linewidth=3.2, zorder=4)
            ax0.fill_between(hist_data.index, pct_series.values, 0, color=fill_color, alpha=0.18, zorder=3)

            # 基準線 (0.0%) & ±1.0% 急変警戒ライン
            ax0.axhline(0.0, color="#64748b", linestyle="--", linewidth=1.2, alpha=0.8, zorder=2)
            ax0.axhline(1.0, color="#f59e0b", linestyle=":", linewidth=1.4, alpha=0.9, zorder=2)
            ax0.axhline(-1.0, color="#f59e0b", linestyle=":", linewidth=1.4, alpha=0.9, zorder=2)

            # ラベル (線と文字の重なり防止 bbox 付与)
            x_first = hist_data.index[0]
            bbox_lbl = dict(boxstyle="round,pad=0.2", fc=bg_card, ec="none", alpha=0.9)
            ax0.text(x_first, 0.05, " 基準線 0.0%", color="#94a3b8", fontsize=9, va="bottom", ha="left", bbox=bbox_lbl)
            ax0.text(x_first, 1.05, " ±1.0% 急変ライン", color="#f59e0b", fontsize=9, va="bottom", ha="left", bbox=bbox_lbl)
            ax0.text(x_first, -0.95, " ±1.0% 急変ライン", color="#f59e0b", fontsize=9, va="top", ha="left", bbox=bbox_lbl)

            # 最新値マーカー
            ax0.scatter([latest_time], [latest_pct], color=marker_color, s=160, zorder=6, edgecolor="#ffffff", linewidth=2.0)

            # 最新値吹き出しバナー (重なり防止で位置を微調整)
            sign = "+" if latest_pct >= 0 else ""
            px_fmt = f"{latest_px:,.1f}" if latest_px >= 500 else (f"{latest_px:,.2f}" if latest_px >= 1 else f"{latest_px:,.4f}")
            badge_txt = f" 最新 {sign}{latest_pct:.2f}%\n ({px_fmt}) "
            y_offset = (pct_series.max() - pct_series.min()) * 0.10
            if is_up:
                y_text = latest_pct + max(y_offset, 0.25)
            else:
                y_text = latest_pct - max(y_offset, 0.35)

            ax0.annotate(
                badge_txt,
                xy=(latest_time, latest_pct),
                xytext=(latest_time, y_text),
                color="#ffffff",
                fontweight="bold",
                fontsize=12,
                ha="right",
                va="center",
                bbox=dict(boxstyle="round,pad=0.4", fc="#0f172a", ec=marker_color, lw=1.8, alpha=0.95),
                arrowprops=dict(arrowstyle="->", color=marker_color, lw=1.5),
                zorder=7,
            )

            # Y軸レンジ
            y_min = min(pct_series.min(), -1.3) * 1.2
            y_max = max(pct_series.max(), 1.3) * 1.2
            ax0.set_ylim(y_min, y_max)
            ax0.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz="Asia/Tokyo"))
        else:
            ax0.text(0.5, 0.5, f"【{primary_name}】\nリアルタイム 1%急変検知中", color=marker_color, fontsize=15, ha="center", va="center")
            ax0.set_xlim(0, 1)
            ax0.set_ylim(0, 1)

        sign = "+" if latest_pct >= 0 else ""
        dir_str = "急伸 ▲" if is_up else "急落 ▼"
        t_str = datetime.now(timezone(timedelta(hours=9))).strftime("%Y/%m/%d %H:%M JST")
        ax0.set_title(
            f"世界の株価 リアルタイム急変チャート: 【{primary_name}】 {sign}{latest_pct:.2f}% ({dir_str})",
            color=text_main,
            fontsize=15,
            fontweight="bold",
            pad=14,
            loc="left"
        )
        ax0.set_ylabel("前日比 変動率 (%)", color=text_sub, fontsize=12, labelpad=8)
        ax0.tick_params(colors=text_sub, labelsize=11, length=4)
        ax0.grid(True, color=grid_color, linestyle=":", alpha=0.7, zorder=1)
        for sp in ax0.spines.values():
            sp.set_color(border_color)
            sp.set_linewidth(1.2)

        # --- [右パネル] 1%以上変動市場サマリーカード ---
        ax1 = fig.add_subplot(gs[1])
        ax1.set_facecolor(bg_card)
        ax1.axis("off")

        # 背景カード枠
        rect = patches.FancyBboxPatch(
            (0.02, 0.02), 0.96, 0.96,
            boxstyle="round,pad=0.03",
            linewidth=1.2,
            edgecolor=border_color,
            facecolor=bg_card,
            transform=ax1.transAxes,
            clip_on=False,
        )
        ax1.add_patch(rect)

        # 右パネルヘッダー
        ax1.text(0.08, 0.93, "【1%急変・警戒市場】", color="#f59e0b", fontsize=13, fontweight="bold", va="top")
        ax1.text(0.08, 0.87, f"更新: {t_str}", color=text_sub, fontsize=9, va="top")

        # 市場アイテム一覧 (文字切れなし・高コントラストバッジ)
        y_pos = 0.78
        display_movers = [m for m in all_movers if m.get("symbol") != primary_sym][:7]
        for item in display_movers:
            mname = item.get("name", item.get("symbol", ""))
            clean_name = mname.split("(")[0].strip()
            mpct = item.get("pct_change", 0.0)
            c = "#00e676" if mpct >= 0 else "#ff3366"
            badge = "▲" if mpct >= 0 else "▼"
            msign = "+" if mpct >= 0 else ""

            ax1.text(0.08, y_pos, clean_name, color=text_main, fontsize=11, fontweight="bold", va="center")
            badge_str = f" {badge} {msign}{mpct:.2f}% "
            ax1.text(
                0.92, y_pos,
                badge_str,
                color=c,
                fontsize=11,
                fontweight="bold",
                va="center",
                ha="right",
                bbox=dict(boxstyle="round,pad=0.25", fc="#0f172a", ec=c, lw=1.0, alpha=0.9),
            )
            y_pos -= 0.10

        ax1.text(0.08, 0.05, "Antigravity Market Sentinel 24h", color="#475569", fontsize=9, va="bottom")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", dpi=130, facecolor=fig.get_facecolor())
        buf.seek(0)
        img_bytes = buf.getvalue()
        plt.close(fig)
        return img_bytes

    def send_breakout_alert(
        self,
        triggered_events: List[Dict[str, Any]],
        all_movers: List[Dict[str, Any]],
        chart_image: bytes
    ) -> bool:
        """Discord Webhook へリアルタイム急変速報を送信"""
        if discord_muted():
            return False
        if not self.webhook_url:
            logger.error("[Sentinel] Webhook URL未設定")
            return False

        t_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")

        # トリガーされた銘柄サマリー
        breakout_lines = []
        for ev in triggered_events:
            sign_badge = "🟢 急騰" if ev["pct_change"] >= 0 else "🔴 急落"
            breakout_lines.append(
                f"• **{ev['name']}**: **`{ev['pct_change']:+.2f}%`** ({sign_badge})\n"
                f"  現在値: `{ev['current_price']:,.2f}` | 区分: `{ev['event_type']}`"
            )

        fields = [
            {
                "name": f"⚡ 1%突破検知マーケット ({len(triggered_events)} 銘柄)",
                "value": "\n".join(breakout_lines),
                "inline": False,
            }
        ]

        # 現在1%以上動いている全銘柄一覧（参考）
        other_movers = [m for m in all_movers if m["symbol"] not in [e["symbol"] for e in triggered_events]]
        if other_movers:
            other_lines = [
                f"• {m['name']}: `{m['pct_change']:+.2f}%`"
                for m in other_movers[:8]
            ]
            fields.append({
                "name": "📋 その他 1%以上変動継続中の市場",
                "value": "\n".join(other_lines),
                "inline": False,
            })

        embed = {
            "title": "🚨 【世界の株価 リアルタイム急変速報】1%突破を検知！",
            "description": (
                f"**検知時刻**: `{t_now}`\n"
                f"[世界の株価 (sekai-kabuka.com)](https://sekai-kabuka.com/pc-index.html) の監視市場にて、\n"
                f"**変動率が 1.0% ラインを突破（または変動拡大）** した対象を即座に検知しました。"
            ),
            "color": 0xF39C12,  # オレンジ/ゴールド警戒
            "fields": fields,
            "image": {"url": "attachment://sekai_kabuka_breakout.png"},
            "footer": {"text": "Antigravity Real-Time Market Sentinel ⚡"},
        }

        payload = {
            "username": "世界の株価 リアルタイム急変速報",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/3314/3314488.png",
            "embeds": [embed],
        }

        files = {
            "payload_json": (None, json.dumps(payload), "application/json"),
            "files[0]": ("sekai_kabuka_breakout.png", chart_image, "image/png"),
        }

        try:
            res = requests.post(self.webhook_url, files=files, timeout=15)
            if res.status_code in (200, 204):
                logger.info(f"[Sentinel] 🚀 1%突破リアルタイム速報をDiscordに送信しました！ ({len(triggered_events)}件)")
                return True
            else:
                logger.error(f"[Sentinel] 送信失敗 (HTTP {res.status_code}): {res.text}")
                return False
        except Exception as e:
            logger.exception(f"[Sentinel] 送信例外: {e}")
            return False

    def send_release_alert(self, reason: str) -> bool:
        """Discord Webhook へ防護自動解除の通知を送信"""
        if discord_muted():
            return False
        if not self.webhook_url:
            return False

        t_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")
        embed = {
            "title": "🟢 【世界の株価 センチネル自動解除】市場安定化を検知",
            "description": (
                f"**検知時刻**: `{t_now}`\n"
                f"市場の急変・ボラティリティが沈静化し、自動解除条件を満たしました。\n"
                f"クオンツ防護レベルを **平常（NORMAL）** に自動復帰させました。"
            ),
            "color": 0x2ECC71,  # エメラルドグリーン
            "fields": [
                {
                    "name": "📋 解除理由",
                    "value": f"• {reason}",
                    "inline": False,
                },
                {
                    "name": "🛡️ クオンツ基盤ステータス",
                    "value": "• SafetyGate: **NORMAL (通常運転再開)**\n• ロット制限: **100% (制限解除)**",
                    "inline": False,
                },
            ],
            "footer": {"text": "Antigravity Real-Time Market Sentinel 🛡️"},
        }

        payload = {
            "username": "世界の株価 リアルタイム急変センチネル",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/3314/3314488.png",
            "embeds": [embed],
        }

        try:
            res = requests.post(self.webhook_url, json=payload, timeout=10)
            if res.status_code in (200, 204):
                logger.info(f"[Sentinel] 🟢 防護自動解除通知をDiscordに送信しました: {reason}")
                return True
            else:
                logger.error(f"[Sentinel] 解除通知送信失敗 (HTTP {res.status_code}): {res.text}")
                return False
        except Exception as e:
            logger.exception(f"[Sentinel] 解除通知送信例外: {e}")
            return False

    def scan_once(self, force_notify: bool = False):
        """1回スキャンを実行"""
        all_data = self.fetch_all_prices()
        if not all_data:
            return

        # 1%以上動いている全銘柄
        all_movers = [item for item in all_data.values() if abs(item["pct_change"]) >= self.base_threshold_pct]
        all_movers.sort(key=lambda x: abs(x["pct_change"]), reverse=True)

        # 今回新たに1%突破した銘柄を検出
        triggered = self.detect_breakout_events(all_data)

        if force_notify and not triggered and all_movers:
            triggered = all_movers[:2]

        if triggered:
            logger.info(f"[Sentinel] ⚡ 1%突破イベント検知: {[t['name'] for t in triggered]}")
            triggered_syms = [t["symbol"] for t in triggered]
            chart_bytes = self.generate_breakout_chart(all_movers, triggered_syms)

            # 1. Discord 送信
            self.send_breakout_alert(triggered, all_movers, chart_bytes)

            # 2. X (Twitter) 重複排除・厳選投稿
            # ・「継続警戒」はX投稿から完全除外 (Discordのみ)
            # ・初動 (1%) / 急反転 / 新マイルストーン (0.5%刻み拡大) のみ投稿
            # ・同一銘柄の再投稿は最低1800秒 (30分) クールダウン
            x_eligible_events = []
            now_ts = time.time()
            for ev in triggered:
                sym = ev["symbol"]
                ev_type = ev.get("event_type", "")
                if "継続警戒" in ev_type:
                    continue  # 継続警戒はX投稿除外

                prev_x = self.x_posted_states.get(sym)
                if prev_x is None:
                    # 初回
                    x_eligible_events.append(ev)
                else:
                    last_x_time = prev_x.get("time", 0.0)
                    last_x_level = prev_x.get("level", 0.0)
                    curr_level = ev.get("level", 0.0)

                    # 急反転 (符号変化) または マイルストーン拡大 (0.5%以上進行)
                    is_reversal = (last_x_level > 0 and curr_level < 0) or (last_x_level < 0 and curr_level > 0)
                    is_milestone_expanded = abs(curr_level) > abs(last_x_level)

                    # クールダウン (同一水準なら最低1800秒以内は再投稿遮断)
                    if is_reversal or (is_milestone_expanded and (now_ts - last_x_time >= 300.0)):
                        x_eligible_events.append(ev)
                    elif now_ts - last_x_time >= 3600.0:  # 1時間以上経過した重大変動
                        x_eligible_events.append(ev)

            # X日次上限チェック（繁忙 12。グローバル 48 は XNotifier 側）
            today_str = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
            daily_meta = self.x_posted_states.get("_daily_meta", {})
            if daily_meta.get("date") != today_str:
                daily_meta = {"date": today_str, "count": 0}
            curr_daily_x = daily_meta.get("count", 0)

            if x_eligible_events and self.remaining_mover_x_posts(curr_daily_x) <= 0:
                logger.info(f"[Sentinel] 🛑 世界の株価 X急変速報 本日上限({self.max_daily_x}件)到達のためスキップ (対象: {[t['name'] for t in x_eligible_events]})")
                x_eligible_events = []

            if x_eligible_events:
                try:
                    tweet_id = send_breakout_tweet(x_eligible_events, chart_bytes)
                    if tweet_id:
                        daily_meta["count"] = curr_daily_x + 1
                        self.x_posted_states["_daily_meta"] = daily_meta
                        logger.info(f"[Sentinel] 🐦 X急変速報の投稿に成功しました (Tweet ID: {tweet_id}, 対象: {[t['name'] for t in x_eligible_events]}, 日次累計: {daily_meta['count']}/{self.max_daily_x})")
                        for ev in x_eligible_events:
                            self.x_posted_states[ev["symbol"]] = {
                                "time": now_ts,
                                "level": ev.get("level", 0.0),
                                "pct": ev["pct_change"],
                            }
                        self._save_x_states()
                except Exception as ex:
                    logger.warning(f"[Sentinel] X急変速報投稿エラー: {ex}")
            else:
                logger.info("[Sentinel] 🐦 X急変速報は重複排除・マイルストーン未達・継続警戒のためスキップしました。")

            # 3. クオンツ基盤へマクロショック伝播 (SafetyGate自動防護連携)
            try:
                from antigravity.quant_pipeline.market_shock_sentinel import MarketShockSentinel
                max_change = max(abs(t["pct_change"]) for t in triggered)
                shock_lvl = "critical" if max_change >= 2.0 else "warning"
                if len(all_movers) >= 10:
                    self.active_shock_level = "WIDE"
                else:
                    self.active_shock_level = shock_lvl.upper()

                self.stable_cycle_count = 0
                shock_event = " / ".join([f"{t['name']} {t['pct_change']:+.1f}%" for t in triggered[:2]])
                MarketShockSentinel().publish_shock(event_name=shock_event, level=shock_lvl, duration_sec=1800.0)
                logger.info(f"[Sentinel] 🛡️ クオンツ基盤へマクロショック連携完了 (レベル: {self.active_shock_level})")
            except Exception as e_shock:
                logger.warning(f"[Sentinel] クオンツショック連携スキップ: {e_shock}")
        else:
            logger.info(f"[Sentinel] 巡回完了: 1%以上継続中 {len(all_movers)}件 (新規突破なし・通知スキップ)")
            # ショック発令中であれば自動解除を判定
            if self.active_shock_level != "NORMAL":
                try:
                    from antigravity.quant_pipeline.market_shock_sentinel import MarketShockSentinel
                    max_market_change = max([abs(x["pct_change"]) for x in all_data.values()], default=0.0)
                    vix_change = all_data.get("^VIX", {}).get("pct_change", 0.0)
                    fx_change = all_data.get("USDJPY=X", {}).get("pct_change", 0.0)

                    should_rel, new_stable, rel_reason = should_release_sentinel(
                        current_change=max_market_change,
                        stable_count=self.stable_cycle_count,
                        shock_markets_count=len(all_movers),
                        max_market_change=max_market_change,
                        vix_change_pct=vix_change,
                        fx_change_pct=fx_change,
                        current_level=self.active_shock_level,
                    )
                    self.stable_cycle_count = new_stable

                    if should_rel:
                        logger.info(f"[Sentinel] 🟢 防護自動解除条件達成: {rel_reason}")
                        MarketShockSentinel().clear_shock()
                        self.send_release_alert(rel_reason)
                        self.active_shock_level = "NORMAL"
                        self.stable_cycle_count = 0
                    else:
                        logger.info(f"[Sentinel] 🛡️ 防護維持中: レベル={self.active_shock_level}, 安定カウント={self.stable_cycle_count}/5 ({rel_reason})")
                except Exception as e_rel:
                    logger.warning(f"[Sentinel] 自動解除判定例外: {e_rel}")


    def run_forever(self):
        """常時監視メインループ"""
        logger.info("=" * 60)
        logger.info("  🚀 世界の株価 リアルタイム急変センチネル 稼働開始")
        logger.info(f"  • 巡回インターバル: {self.poll_interval_sec} 秒")
        logger.info(f"  • トリガー基準: 前日比 ±{self.base_threshold_pct}% 突破時")
        logger.info(f"  • マイルストーン刻み: {self.step_threshold_pct}%")
        logger.info("=" * 60)

        self.is_running = True
        while self.is_running:
            try:
                self.scan_once()
            except Exception as e:
                logger.exception(f"[Sentinel] 巡回ループ例外 (自動継続): {e}")
            time.sleep(self.poll_interval_sec)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="世界の株価 リアルタイム1%急変センチネル")
    parser.add_argument("--interval", type=float, default=60.0, help="巡回間隔(秒) デフォルト: 60")
    parser.add_argument("--test", action="store_true", help="起動時に即座に1回判定テストを実行")
    parser.add_argument("--force", action="store_true", help="テスト時に強制送信")
    args = parser.parse_args()

    sentinel = RealtimeMoverSentinel(poll_interval_sec=args.interval)
    if args.test or args.force:
        sentinel.scan_once(force_notify=args.force)
    else:
        sentinel.run_forever()


if __name__ == "__main__":
    main()
