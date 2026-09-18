"""
news_pipeline/market_impact_scorer.py: 市場影響度スコア (MIS) 計算 & 日本株X投稿テンプレ自動生成
========================================================================================
- MIS (Market Impact Score: 0〜100点) 算出ロジック:
    1. 基本点 (ニュース種別): 決算(40), 業績修正(50), 不祥事(60), M&A(35), 資金調達(30), 監査不適正(70), 上場廃止(80), マクロ(50), 軽微IR(10)
    2. サプライズ点: 決算乖離 (営利/EPS/売上), 業績予想修正率, マクロ乖離 (CPI等)
    3. 規模点: 買収・増資金額 (100億未満:+5, 100-500億:+10, 500-1000億:+20, 1000億以上:+30)
    4. 心理点: 内部統制不備(+20), 会計不正(+30), 重大事故(+20), 経営者逮捕(+40)
    5. 価格反応点: |r|>=1%:+10, >=2%:+20, >=3%:+30, >=5%:+40
- 日本株専用 X (旧Twitter) 投稿テンプレ自動生成関数: build_japan_post(event)
- X投稿判定 (MIS >= 70: 即時投稿, 40〜69: 低頻度枠, <40: Discordのみ)
"""

import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field

JST = timezone(timedelta(hours=9))


@dataclass
class MarketEvent:
    """市場イベントデータモデル"""
    event_type: str                  # 決算短信 / 業績修正 / 不祥事 / 監査不適正 / M&A / 資金調達 / マクロ / その他
    name: str                        # 銘柄名 / 企業名 (例: トヨタ, 任天堂)
    symbol: Optional[str] = None     # 証券コード or ティッカー (例: 7203, 7974)
    headline_metric: str = ""        # 要点指標 (例: 営利+12%増, -18%下方修正)
    source: str = "TDnet"            # TDnet / EDINET / Bloomberg / Reuters / 報道
    time_str: str = ""               # 発表時刻 (HH:MM)
    direction: Optional[str] = None  # "up" or "down"
    price_change: Optional[float] = None # 価格変動率 (%) 例: +3.01, -2.17
    price_label: Optional[str] = None    # 急伸 / 急落 / 反発 / 続落
    reason: Optional[str] = None     # 要因1行 (例: 決算サプライズ, 内部統制不備)
    amount: Optional[float] = None   # 金額 (円) M&A・増資等
    op_surprise: Optional[float] = None   # 営業利益サプライズ乖離率 (%)
    eps_surprise: Optional[float] = None  # EPSサプライズ乖離率 (%)
    rev_surprise: Optional[float] = None  # 売上サプライズ乖離率 (%)
    revision_rate: Optional[float] = None # 業績修正率 (%)
    macro_delta: Optional[float] = None   # マクロ指標の予想乖離
    is_scandal: bool = False              # 不祥事フラグ
    is_internal_control_flaw: bool = False # 内部統制不備
    is_arrest: bool = False               # 逮捕・捜査
    mis: int = 0                          # 算出後MIS (0〜100)


class MarketImpactScorer:
    """市場影響度スコア (MIS) 計算エンジン"""

    # 1. 基本点 (ニュース種別) - Xフォロワー増加に寄与する重要度順に再編
    BASE_SCORES = {
        "TOB": 75,             # プレミアム急騰期待・最高注目度
        "上場廃止": 85,         # 重大ニュース
        "監査不適正": 80,       # 企業不祥事
        "不祥事": 75,           # 特別調査委員会・粉飾疑義等
        "会計不正": 75,
        "内部統制": 70,
        "業績修正": 55,         # 上方修正・下方修正
        "増配": 65,             # 個人投資家の最人気材料
        "自社株買い": 65,       # 需給改善の好材料
        "決算短信": 45,         # 四半期/通期決算
        "大型M&A": 60,
        "M&A": 50,
        "買収": 50,
        "公募増資": 40,
        "資金調達": 30,
        "指定替え": 65,
        "マクロイベント": 50,
        "マクロ": 50,
        "適時開示": 20,
        "事務的開示": 0,        # 完全遮断
        "その他": 10,
    }

    @classmethod
    def calculate_mis(cls, event: MarketEvent) -> int:
        """
        MIS = 基本点 + サプライズ点 + 規模点 + 心理点 + 価格反応点 (0〜100)
        """
        # 事務的開示 (ノイズ) は即時0点返却
        if event.event_type == "事務的開示":
            return 0

        # 1. 基本点
        base = cls.BASE_SCORES.get(event.event_type, 20)

        # 2. キラー材料・見出しブースト (フォロワー獲得のための高注目度加点)
        killer_boost = 0
        headline = f"{event.headline_metric} {event.reason or ''}"
        if event.event_type == "業績修正":
            if event.direction == "up":
                killer_boost += 20  # 上方修正はMIS 75点到達
            elif event.direction == "down":
                killer_boost += 15  # 下方修正はMIS 70点到達
        elif event.event_type == "決算短信":
            if event.direction == "up":
                killer_boost += 20  # 好決算・増益はMIS 65点到達
            if any(w in headline for w in ["最高益", "黒字", "過去最高"]):
                killer_boost += 25
        elif event.event_type in ["M&A", "買収"]:
            killer_boost += 15

        # 3. サプライズ点
        surprise = 0
        if event.event_type == "決算短信":
            # 営業利益
            if event.op_surprise is not None:
                abs_op = abs(event.op_surprise)
                if abs_op >= 20.0:
                    surprise += 30
                elif abs_op >= 10.0:
                    surprise += 20
                elif abs_op >= 5.0:
                    surprise += 10
            # EPS
            if event.eps_surprise is not None:
                abs_eps = abs(event.eps_surprise)
                if abs_eps >= 20.0:
                    surprise += 30
                elif abs_eps >= 10.0:
                    surprise += 20
                elif abs_eps >= 5.0:
                    surprise += 10
            # 売上
            if event.rev_surprise is not None:
                abs_rev = abs(event.rev_surprise)
                if abs_rev >= 15.0:
                    surprise += 20
                elif abs_rev >= 7.0:
                    surprise += 10
                elif abs_rev >= 3.0:
                    surprise += 5

        elif "業績修正" in event.event_type:
            if event.revision_rate is not None:
                abs_rev = abs(event.revision_rate)
                if abs_rev >= 50.0:
                    surprise += 40
                elif abs_rev >= 30.0:
                    surprise += 30
                elif abs_rev >= 20.0:
                    surprise += 20
                elif abs_rev >= 10.0:
                    surprise += 10

        elif "マクロ" in event.event_type:
            if event.macro_delta is not None:
                abs_d = abs(event.macro_delta)
                if abs_d >= 0.4:
                    surprise += 30
                elif abs_d >= 0.2:
                    surprise += 20
                elif abs_d >= 0.1:
                    surprise += 10

        # タイトル文字列からの簡易サプライズ推定 (指標が明示されていない場合)
        if surprise == 0 and event.headline_metric:
            m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", event.headline_metric)
            if m:
                val = abs(float(m.group(1)))
                if val >= 30.0:
                    surprise += 30
                elif val >= 15.0:
                    surprise += 20
                elif val >= 8.0:
                    surprise += 10

        # 3. 規模点 (M&A・資金調達)
        scale_score = 0
        if event.amount:
            amt = float(event.amount)
            if amt >= 100_000_000_000:       # 1000億円以上
                scale_score = 30
            elif amt >= 50_000_000_000:     # 500〜1000億円
                scale_score = 20
            elif amt >= 10_000_000_000:     # 100〜500億円
                scale_score = 10
            elif amt > 0:
                scale_score = 5

        # 4. 心理点 (ネガティブニュース)
        psych_score = 0
        if event.is_arrest or "逮捕" in event.headline_metric or "特捜" in event.headline_metric:
            psych_score += 40
        elif event.is_scandal or "不正" in event.headline_metric or "架空" in event.headline_metric:
            psych_score += 30
        elif event.is_internal_control_flaw or "不備" in event.headline_metric:
            psych_score += 20

        # 5. 価格反応点 (急変センチネル連動)
        price_score = 0
        if event.price_change is not None:
            abs_p = abs(event.price_change)
            if abs_p >= 5.0:
                price_score = 40
            elif abs_p >= 3.0:
                price_score = 30
            elif abs_p >= 2.0:
                price_score = 20
            elif abs_p >= 1.0:
                price_score = 10

        total_mis = base + killer_boost + surprise + scale_score + psych_score + price_score
        return max(0, min(100, total_mis))

    @classmethod
    def get_impact_label(cls, mis: int) -> str:
        """MISからインパクトラベル判定"""
        if mis >= 85:
            return "CRITICAL"
        elif mis >= 70:
            return "High"
        elif mis >= 40:
            return "Medium"
        else:
            return "Low"

    # ノイズ・事務的開示の判定用キーワード (株価が動かずフォロワーに嫌われる開示)
    NOISE_KEYWORDS = [
        "定款一部変更", "定款の変更", "定款変更",
        "役員の異動", "人事異動", "執行役員", "取締役の辞任", "代表取締役の異動", "幹部職員",
        "本社移転", "本社の移転",
        "新株予約権の行使状況", "新株予約権の大量行使", "大量行使に関するお知らせ",
        "譲渡制限付株式", "株式報酬", "事後交付型",
        "支配株主等に関する事項", "親会社等に関する事項",
        "資金の借入", "借入に関するお知らせ", "コミットメントライン",
        "質疑応答要旨", "書き起こし", "決算説明会 質疑応答", "決算説明会質疑応答",
        "事業計画及び成長可能性に関する事項", "中期経営計画の進捗",
        "受取配当金計上", "配当金受取", "内部取引",
        "件 / 全", "前へ123次へ", "開示された情報1～",
    ]

    @classmethod
    def is_noise_disclosure(cls, title: str) -> bool:
        """株価への影響がほぼ皆無な事務的開示・ノイズを判定"""
        clean_title = title.strip()
        # HTMLゴミやナビゲーション文字列
        if "件 / 全" in clean_title or "次へ" in clean_title or len(clean_title) < 5:
            return True
        return any(k in clean_title for k in cls.NOISE_KEYWORDS)

    @classmethod
    def classify_raw_disclosure(cls, title: str) -> Tuple[str, Optional[str]]:
        """
        開示タイトルからイベント種別と方向性を自動判定
        Returns: (event_type: str, direction: Optional[str])
        """
        # 0. ノイズ開示の即時判定
        if cls.is_noise_disclosure(title):
            return "事務的開示", None

        # 1. 不祥事・監査不適正 (フォロワー注目度: 極高)
        if any(k in title for k in ["不適正", "限定付", "意見不表明", "監査報告"]):
            return "監査不適正", "down"
        if any(k in title for k in ["内部統制報告書", "開示すべき重要な不備", "不備"]):
            return "不祥事", "down"
        if any(k in title for k in ["不祥事", "不正会計", "特別調査委員会", "架空取引", "粉飾", "不正疑義", "横領"]):
            return "不祥事", "down"
        if any(k in title for k in ["上場廃止", "監理銘柄", "整理銘柄", "特設注意市場"]):
            return "上場廃止", "down"

        # 2. TOB・公開買付・大型M&A (フォロワー注目度: 極高)
        if any(k in title for k in ["公開買付", "TOB", "買集め行為"]):
            return "TOB", "up"
        if any(k in title for k in ["買収", "子会社化", "完全子会社化", "資本業務提携", "合併"]):
            return "M&A", "up"

        # 3. 業績予想修正 (上方修正はフォロワー増加の最重要キラー材料)
        if any(k in title for k in ["業績予想の修正", "業績予想修正", "差異に関するお知らせ", "業績予想の開示"]):
            is_up = any(k in title for k in ["上方", "増額", "黒字", "最高益", "改善"])
            is_down = any(k in title for k in ["下方", "減額", "赤字", "特損", "悪化"])
            direction = "up" if is_up else ("down" if is_down else None)
            return "業績修正", direction

        # 4. 増配・自社株買い (個人投資家に最も好まれる材料)
        if any(k in title for k in ["増配", "特別配当", "記念配当", "復配"]):
            return "増配", "up"
        if any(k in title for k in ["自己株式取得", "自社株買い", "自己投資口の取得"]):
            return "自社株買い", "up"

        # 5. 決算短信
        if any(k in title for k in ["決算短信", "四半期決算"]):
            is_up = any(k in title for k in ["増益", "最高益", "黒字浮上", "好調", "進捗率"])
            is_down = any(k in title for k in ["減益", "赤字", "低迷", "損失"])
            direction = "up" if is_up else ("down" if is_down else None)
            return "決算短信", direction

        # 6. 資金調達・新株発行
        if any(k in title for k in ["公募増資", "新株式発行", "第三者割当", "新株予約権"]):
            return "資金調達", "down"

        return "適時開示", None


def impact_label(mis: int) -> str:
    return MarketImpactScorer.get_impact_label(mis)


def get_title_prefix(event_type: str, direction: Optional[str] = None) -> str:
    """イベント種別からXで目を引く絵文字バッジ付きタイトルPrefixを決定"""
    if event_type == "決算短信":
        return "📈【好決算速報】" if direction == "up" else "📊【決算速報】"
    if "業績修正" in event_type:
        return "🚀【業績上方修正】" if direction == "up" else "⚠️【業績下方修正】"
    if event_type == "増配":
        return "💰【増配速報】"
    if event_type == "自社株買い":
        return "💎【自社株買い速報】"
    if event_type == "TOB":
        return "🎯【TOB・公開買付速報】"
    if event_type in ["不祥事", "監査不適正", "会計不正", "内部統制"]:
        return "🛑【緊急・重要開示】"
    if event_type in ["M&A", "大型M&A", "買収"]:
        return "🤝【M&A速報】"
    if event_type in ["資金調達", "公募増資"]:
        return "📉【増資速報】"
    if event_type == "上場廃止":
        return "🚨【重大発表】"
    if "マクロ" in event_type:
        return "⚡【マクロ速報】"
    return "📢【適時開示】"


def get_hashtags(event_type: str) -> str:
    """イベント種別からフォロワー獲得に最も効果的なハッシュタグを厳選"""
    if event_type in ["決算短信", "業績修正", "増配", "自社株買い"]:
        return "#日本株 #決算 #株クラ #適時開示"
    if event_type in ["TOB", "M&A", "大型M&A", "買収"]:
        return "#日本株 #TOB #買収 #株クラ"
    if event_type in ["不祥事", "監査不適正", "会計不正", "内部統制", "上場廃止"]:
        return "#日本株 #適時開示 #株クラ"
    if "マクロ" in event_type:
        return "#米国株 #為替 #日経平均 #世界の株価"
    return "#日本株 #適時開示 #株クラ"


def build_japan_post(ev: MarketEvent) -> Optional[str]:
    """
    日本株専用 X (旧Twitter) 投稿テキスト自動生成
    ============================================
    ・MIS < 40 は None を返して X投稿を遮断 (Discordのみ)
    ・1行目: イベント種別 + 銘柄名 + 要点 + 時刻
    ・2行目: 📍ソース
    ・3行目: 📍価格 (価格変動がある場合)
    ・4行目: 📍要因 (要因が明示されている場合)
    ・5行目: ⚠️/🛑市場影響度 (High / CRITICAL / Medium) + MIS
    ・6行目: 厳選ハッシュタグ
    """
    if ev.mis == 0:
        ev.mis = MarketImpactScorer.calculate_mis(ev)

    # 事務的開示 (ノイズ) または MIS 50未満はX投稿除外 (フォロワー増加のための厳選配信)
    if ev.event_type == "事務的開示" or ev.mis < 50:
        return None

    time_suffix = f"（{ev.time_str}）" if ev.time_str else ""
    metric_str = f" {ev.headline_metric}" if ev.headline_metric else ""
    title = f"{get_title_prefix(ev.event_type, ev.direction)}{ev.name}{metric_str}{time_suffix}"

    lines = [title]
    lines.append(f"📍ソース：{ev.source}")

    if ev.price_change is not None:
        sign = "+" if ev.price_change >= 0 else ""
        label = ev.price_label or ("急伸" if ev.price_change >= 0 else "急落")
        price_time = f"（{ev.time_str}）" if ev.time_str else ""
        lines.append(f"📍価格：{sign}{ev.price_change:.2f}%{label}{price_time}")

    if ev.reason:
        lines.append(f"📍要因：{ev.reason}")

    label = impact_label(ev.mis)
    is_critical_type = ev.event_type in ["不祥事", "監査不適正", "会計不正", "上場廃止"] or label == "CRITICAL"
    icon = "🛑" if is_critical_type else "⚠️"
    lines.append(f"{icon}市場影響度：{label}（MIS {ev.mis}）")

    lines.append(get_hashtags(ev.event_type))

    post_text = "\n".join(lines).strip()
    return post_text
