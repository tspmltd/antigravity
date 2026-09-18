"""
news_pipeline/post_optimizer.py: X (Twitter) & 配信最適化エンジン
==================================================================
1. 開示重要度ランク自動判定 (A / B / C ランク)
2. 投稿ジャンル自動判定 (市況急変 / 急落ショック / 広域ショック / 開示速報 / 防護 / 毎時)
3. ハッシュタグ最適化 & 重複排除 (正規化, 禁止タグフィルタ, 優先順位, 最大5個制限)
4. ShockSentinel 自動解除判定 (変動率収束, VIX沈静化, 時間的持続)
"""

import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

JST = timezone(timedelta(hours=9))


# -------------------------------------------------------------
# 1. 開示重要度ランク判定 (A / B / C)
# -------------------------------------------------------------
def judge_importance(event: Dict[str, Any]) -> str:
    """
    開示重要度ランク (A / B / C) を機械的に自動判定
    Aランク: 最重要 (即時投稿必須: TOB, 買収, ±3%超大量保有, 大型自社株買い, CRITICAL防護)
    Bランク: 重要 (通常速報: 5%前後大量保有, 10〜50億自社株買い, 重複統合, WARNING防護)
    Cランク: 参考 (X投稿不要・Discordのみ: 軽微な訂正, IR説明資料)
    """
    category = event.get("category", "")
    raw_title = event.get("raw_title", "")
    amount = float(event.get("amount", 0.0))
    change_ratio = float(event.get("change_ratio", 0.0))
    protection = event.get("protection", "")
    is_duplicate = event.get("duplicate", False)

    # 1. カテゴリ一次判定
    if any(k in category or k in raw_title for k in ["TOB", "MBO", "買収", "子会社化", "公開買付"]):
        rank = "A"
    elif any(k in category or k in raw_title for k in ["大量保有", "変更報告", "自社株買", "決算短信", "業績予想修正", "上方修正", "下方修正"]):
        rank = "B"
    else:
        rank = "C"

    # 2. 金額・比率 二次判定 (A/B昇格・分岐)
    if "自社株買" in category or "自社株買い" in raw_title:
        # 100億円以上はA、10億円以上はB
        if amount >= 10_000_000_000:
            rank = "A"
        elif amount >= 1_000_000_000:
            rank = "B"
        # タイトルに大型自社株買いキーワードがある場合
        if re.search(r"[0-9]+[0-9]{2}億|[0-9]+兆", raw_title):
            rank = "A"

    if "大量保有" in category or "大量保有" in raw_title:
        # 持株比率変動幅
        if change_ratio >= 3.0:
            rank = "A"
        elif re.search(r"([3-9]|[1-9][0-9])％|超保有|筆頭株主", raw_title):
            rank = "A"

    # 業績修正の大幅変動 (±10%以上)
    if "上方修正" in raw_title or "特損" in raw_title or "赤字転落" in raw_title:
        rank = "A"

    # 3. 重複統合による判定
    if is_duplicate and rank == "C":
        rank = "B"

    # 4. 防護アクション連動
    if protection == "CRITICAL":
        rank = "A"
    elif protection == "WARNING" and rank == "C":
        rank = "B"

    return rank


# -------------------------------------------------------------
# 2. 投稿ジャンル自動判定
# -------------------------------------------------------------
def classify_post(event: Dict[str, Any]) -> str:
    """
    イベント種別・強度・副作用から投稿ジャンルを自動判定
    """
    etype = event.get("type", "")

    if etype in ("market", "market_shock"):
        change = float(event.get("change_pct", event.get("change", 0.0)))
        if abs(change) >= 2.5:
            return "急落ショック" if change < 0 else "急騰ショック"
        elif event.get("wide_shock", False) or event.get("shock_markets_count", 0) >= 10:
            return "広域ショック"
        else:
            return "市況急変"

    if etype in ("disclosure", "edinet"):
        imp = event.get("importance") or judge_importance(event)
        return f"開示速報{imp}"

    if etype in ("protection", "shock_sentinel"):
        lvl = event.get("level", "WARNING").upper()
        if lvl == "CRITICAL":
            return "防護CRITICAL"
        elif lvl == "WIDE":
            return "防護WIDE"
        return "防護WARNING"

    if etype in ("hourly", "hourly_report"):
        return "毎時レポート"

    if etype in ("system", "capacity_warn"):
        return "システム警告"

    return "市況急変"


# -------------------------------------------------------------
# 3. ハッシュタグ最適化 & 重複排除
# -------------------------------------------------------------
BLOCKLIST = {
    "市況急変": ["#適時開示", "#edinet", "#tdnet", "#決算"],
    "急落ショック": ["#適時開示", "#edinet", "#tdnet"],
    "急騰ショック": ["#適時開示", "#edinet", "#tdnet"],
    "広域ショック": ["#適時開示", "#edinet", "#tdnet"],
    "開示速報A": ["#世界の株価", "#市況速報", "#fx", "#暗号資産"],
    "開示速報B": ["#世界の株価", "#市況速報", "#fx", "#暗号資産"],
    "開示速報C": ["#世界の株価", "#市況速報", "#fx"],
    "防護CRITICAL": ["#適時開示", "#edinet", "#tdnet"],
    "防護WARNING": ["#適時開示", "#edinet", "#tdnet"],
    "毎時レポート": ["#適時開示", "#edinet", "#tdnet"],
    "システム警告": ["#適時開示", "#世界の株価"],
}

PRIORITY = {
    "市況急変": ["#世界の株価", "#市況速報", "#日本株"],
    "急落ショック": ["#世界の株価", "#市況速報", "#急落"],
    "急騰ショック": ["#世界の株価", "#市況速報", "#急騰"],
    "広域ショック": ["#世界の株価", "#世界同時急変", "#リスク管理"],
    "開示速報A": ["#適時開示", "#EDINET", "#TDnet", "#日本株"],
    "開示速報B": ["#適時開示", "#日本株", "#EDINET"],
    "開示速報C": ["#適時開示", "#日本株"],
    "防護CRITICAL": ["#リスク管理", "#世界の株価", "#自動売買"],
    "防護WARNING": ["#リスク管理", "#世界の株価"],
    "毎時レポート": ["#世界の株価", "#市況まとめ"],
    "システム警告": ["#システム監視", "#インフラ"],
}


def normalize_tag(tag: str) -> str:
    """タグの表記ゆれ正規化"""
    t = tag.strip().replace("＃", "#")
    if not t.startswith("#"):
        t = "#" + t
    return t


def dedupe_preserve_order(tags: List[str]) -> List[str]:
    """順序を保ったまま大文字小文字無視で重複排除"""
    seen = set()
    result = []
    for t in tags:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            result.append(t)
    return result


def optimize_tags(tags: List[str], genre: str = "市況急変") -> str:
    """
    ハッシュタグ最適化エンジン (黄金ルール: 5個以内, 優先順位先頭, 禁止タグ除外)
    """
    # 1. 正規化
    norm_tags = [normalize_tag(t) for t in tags if t.strip()]

    # 2. 重複排除
    unique_tags = dedupe_preserve_order(norm_tags)

    # 3. 禁止タグフィルタ
    block = [b.lower() for b in BLOCKLIST.get(genre, [])]
    filtered_tags = [t for t in unique_tags if t.lower() not in block]

    # 4. 優先タグを先頭に配置
    prio = PRIORITY.get(genre, ["#世界の株価"])
    rest = [t for t in filtered_tags if t.lower() not in [p.lower() for p in prio]]
    ordered_tags = prio + rest

    # 5. 重複再排除 & 最大5個に制限
    final_tags = dedupe_preserve_order(ordered_tags)[:5]
    return " ".join(final_tags)


# -------------------------------------------------------------
# 4. ShockSentinel 自動解除判定
# -------------------------------------------------------------
def should_release_sentinel(
    current_change: float,
    stable_count: int,
    shock_markets_count: int = 0,
    max_market_change: float = 0.0,
    vix_change_pct: float = 0.0,
    fx_change_pct: float = 0.0,
    current_level: str = "WARNING",
) -> Tuple[bool, int, str]:
    """
    ShockSentinel の解除条件を4軸で自動判定
    Returns:
        (should_release: bool, new_stable_count: int, reason: str)
    """
    # 1. 変動率が 0.5% 未満に収束しているか
    if abs(current_change) < 0.5:
        new_count = stable_count + 1
    else:
        new_count = 0

    # 2. 広域ショック解除判定 (急変市場数 < 5 かつ 最大変動率 < 1.0%)
    wide_release = (shock_markets_count < 5 and abs(max_market_change) < 1.0)

    # 3. ボラティリティ平常化 (VIX < +3.0% かつ ドル円変動 < ±0.4%)
    vol_release = (vix_change_pct < 3.0 and abs(fx_change_pct) < 0.4)

    # 4. レベル別判定
    if current_level == "WARNING":
        if new_count >= 5:  # 5分間継続安定
            return True, new_count, "単一市場変動率 <0.5% を5分間継続安定"

    elif current_level == "CRITICAL":
        if new_count >= 5 and wide_release and vol_release:
            return True, new_count, "変動率収束 + 広域ショック解除 + VIX平常化"

    elif current_level == "WIDE":
        if wide_release:
            return True, new_count, f"急変市場数 {shock_markets_count} < 5 に沈静化"

    return False, new_count, "継続警戒中"


# -------------------------------------------------------------
# 5. 急変チャート色分けエンジン (値動き x 強度 x 時間帯 x 資産クラス)
# -------------------------------------------------------------
def compute_chart_colors(
    category: str,
    change_pct: float,
    now_hour: int = 12,
    is_wide_shock: bool = False,
) -> Dict[str, Any]:
    """
    急変チャートの色分け設定を算出
    """
    abs_c = abs(change_pct)

    # 1. 資産クラス別の固有カラー
    if "VIX" in category or "恐怖指数" in category:
        line_color = "#9B59B6" if change_pct >= 0 else "#8E44AD"
    elif "為替" in category or "金利" in category:
        line_color = "#3498DB" if change_pct >= 0 else "#2980B9"
    elif "コモディティ" in category:
        line_color = "#E67E22" if change_pct >= 0 else "#D35400"
    elif "暗号資産" in category:
        line_color = "#1ABC9C" if change_pct >= 0 else "#16A085"
    else:
        # 株式市場: 上昇(緑) vs 下落(赤)
        if change_pct >= 0:
            if abs_c >= 3.0:
                line_color = "#00FF66"  # 高彩度・超急騰
            elif abs_c >= 1.5:
                line_color = "#00E676"  # 中彩度
            else:
                line_color = "#2ECC71"  # 通常緑
        else:
            if abs_c >= 3.0:
                line_color = "#D50000"  # 高彩度・ショック級急落
            elif abs_c >= 1.5:
                line_color = "#FF1744"  # 中彩度
            else:
                line_color = "#E74C3C"  # 通常赤

    # 2. 背景カラー (広域ショック時はダークワインレッド)
    if is_wide_shock or abs_c >= 3.0:
        bg_color = "#24161a"  # 赤みを帯びたダーク警戒背景
        grid_color = "#4a2a32"
    else:
        bg_color = "#181825"  # 通常ダーク背景
        grid_color = "#313244"

    # 3. マーカー・強調色
    marker_color = "#FFD700" if abs_c >= 1.0 else "#F1C40F"

    return {
        "line_color": line_color,
        "bg_color": bg_color,
        "grid_color": grid_color,
        "marker_color": marker_color,
        "fill_alpha": 0.20 if abs_c >= 2.0 else 0.12,
    }
