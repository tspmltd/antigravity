"""
news_pipeline/opportunity_assessor.py: 市場機会スコア (OAS: Opportunity Assessment Score) 採点器
=======================================================================================
MIS (Market Impact Score) が市場の「ブレーキ（危険度・破壊力）」なら、
OAS (Opportunity Assessment Score) は市場の「アクセル（収益機会・アルファ期待値）」。

市場がまだ織り込んでいない利益構造（TOB・MBO・自社株買い・大量保有・決算サプライズ）を
0〜100 の定量スコアで評価し、RegimeOrchestrator および後続の AlphaAgent へ供給する。

スコア区分:
  • OAS >= 80: 【超高確度アルファ / SPECIAL_EVENT】 (TOB・MBO・大型自社株買い)
  • 70 <= OAS < 80: 【積極アルファ / ACCUMULATE】 (大量保有・業績サプライズ・増配)
  • 40 <= OAS < 70: 【中立アルファ / DRIFT】 (通常の好決算・軽微な自社株買い)
  • OAS < 40: 【収益機会希薄 / DEFENSE】 (事務的開示、または上場廃止等の純粋危険)
"""

import re
from typing import Tuple, Optional, Dict, Any

# 著名アクティビスト・重要投資家キーワード
ACTIVIST_KEYWORDS = [
    "エフィッシモ", "オアシス", "シルチェスター", "ダルトン", "3D",
    "村上", "レノ", "オフィスサポート", "ストラテジックキャピタル",
    "バリューディベロップメント", "タイヨウ", "アクティビスト",
    "保有目的", "重要提案行為", "買い増し"
]


class OpportunityAssessor:
    """市場機会スコア (OAS) 採点エンジン"""

    @classmethod
    def calculate_oas(cls, event: Any) -> Tuple[int, str, str]:
        """
        イベントから OAS (0〜100)、機会カテゴリ、算出根拠を算出する。
        戻り値: (oas_score, category, rationale)
        """
        event_type = getattr(event, "event_type", "その他")
        headline = f"{getattr(event, 'headline_metric', '')} {getattr(event, 'reason', '') or ''}"
        name = getattr(event, "name", "")
        direction = getattr(event, "direction", "neutral")
        amount = getattr(event, "amount", None)
        is_scandal = getattr(event, "is_scandal", False)
        is_arrest = getattr(event, "is_arrest", False)

        # -------------------------------------------------------------
        # 1. 純粋危険材料 (上場廃止・粉飾・監査不適正・逮捕) ➔ OAS 0〜15点
        # -------------------------------------------------------------
        if event_type in ("上場廃止", "監査不適正", "会計不正") or is_scandal or is_arrest:
            return 10, "NONE", "純粋破滅リスク (下値フロア喪失・空売り規制リスク大のため収益機会なし)"

        if event_type == "事務的開示":
            return 0, "NONE", "事務的開示 (収益機会なし)"

        # -------------------------------------------------------------
        # 2. TOB / MBO (公開買付) ➔ OAS 85〜98点
        # -------------------------------------------------------------
        if event_type in ("TOB", "MBO") or "公開買付" in headline or "TOB" in headline or "MBO" in headline:
            score = 75  # 基本機会点
            # プレミアム加点
            prem_match = re.search(r"(\d+(?:\.\d+)?)\s*%", headline)
            if prem_match:
                prem_val = float(prem_match.group(1))
                if prem_val >= 20.0:
                    score += 20
                elif prem_val >= 10.0:
                    score += 15
                else:
                    score += 10
            else:
                score += 15  # プレミアム明記なしでもTOBは高収束性

            # 友好的・完全子会社化ボーナス
            if any(w in headline for w in ["完全子会社", "賛同", "友好的", "非公開化"]):
                score += 5

            score = min(score, 98)
            return score, "TOB_ARBITRAGE", f"TOB/MBO確定買付価格への価格収束アービトラージ (OAS={score})"

        # -------------------------------------------------------------
        # 3. 大量保有報告書 (アクティビスト・大株主買い増し) ➔ OAS 75〜88点
        # -------------------------------------------------------------
        is_5pct_rule = bool(re.search(r"(?:^|[^\d])5%(?:ルール|保有|超)", headline)) or "大量保有" in headline or "変更報告書" in headline
        if event_type in ("大量保有", "大量保有報告", "5%ルール") or is_5pct_rule:
            score = 65
            if any(k in headline for k in ACTIVIST_KEYWORDS):
                score += 20  # アクティビスト特定加点
            if "買い増し" in headline or "変更報告書" in headline:
                score += 10
            if "重要提案" in headline:
                score += 10

            score = min(score, 90)
            return score, "ACTIVIST_FOLLOW", f"アクティビスト/大株主による継続的買い需要と経営変革アルファ (OAS={score})"

        # -------------------------------------------------------------
        # 4. 自社株買い (Share Buyback) ➔ OAS 70〜85点
        # -------------------------------------------------------------
        if event_type == "自社株買い" or "自己株式取得" in headline or "自社株買い" in headline:
            score = 60
            # 取得枠比率 (株式割合)
            pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", headline)
            if pct_match:
                pct_val = float(pct_match.group(1))
                if pct_val >= 5.0:
                    score += 25
                elif pct_val >= 3.0:
                    score += 18
                elif pct_val >= 1.0:
                    score += 10
            else:
                score += 15

            # 金額規模 (100億超)
            if amount and amount >= 10_000_000_000:
                score += 10
            elif "100億" in headline or "500億" in headline or "兆" in headline:
                score += 10

            score = min(score, 88)
            return score, "BUYBACK_DRIFT", f"確定的な自社株買い支えと需給引き締めドリフト (OAS={score})"

        # -------------------------------------------------------------
        # 5. 業績上方修正 / 決算サプライズ / 大幅増配 ➔ OAS 65〜85点
        # -------------------------------------------------------------
        if event_type in ("業績修正", "決算短信", "増配"):
            if direction == "up" or "上方" in headline or "増益" in headline or "最高益" in headline or "増配" in headline:
                score = 55
                if any(w in headline for w in ["過去最高", "最高益", "黒字転換"]):
                    score += 20
                if "増配" in headline or event_type == "増配":
                    score += 15
                if "上方修正" in headline or event_type == "業績修正":
                    score += 15

                score = min(score, 85)
                return score, "EARNINGS_SURPRISE", f"業績ポジティブサプライズ・決算後ドリフト(PEAD)期待 (OAS={score})"
            elif direction == "down" or "下方" in headline:
                return 20, "NONE", "業績下方修正 (買い機会なし・リスク)"

        # -------------------------------------------------------------
        # 6. PTS夜間急騰 / 世界市場カタリスト
        # -------------------------------------------------------------
        price_change = getattr(event, "price_change", None)
        if price_change is not None and price_change >= 7.0:
            return 75, "PTS_MOMENTUM", f"夜間PTS急騰モメンタム (未織り込みギャップ +{price_change:.1f}%)"

        # -------------------------------------------------------------
        # 7. その他一般開示
        # -------------------------------------------------------------
        return 25, "NONE", f"一般開示 (特異なアルファ機会なし, 種別={event_type})"
