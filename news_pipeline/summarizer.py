"""
テキスト要約・構造化モジュール (summarizer.py)
- LLM (Gemini API: google-genai) を利用した3行要約・構造化
- ハルシネーション禁止プロンプト (存在しない数値や事実の創作厳禁、不明時は「情報なし」)
- APIキー未設定時またはAPIエラー時の自動ルールベース・フォールバック
"""

import os
import re
import logging
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("news_pipeline.summarizer")

# Gemini APIクライアント初期化 (可能であれば)
genai_client = None
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    try:
        from google import genai
        genai_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("[Summarizer] Gemini Clientが初期化されました。")
    except Exception as e:
        logger.warning(f"[Summarizer] Gemini Client初期化失敗 (ルールベースへフォールバックします): {e}")


class Summarizer:
    """テキストを要約・構造化するクラス"""

    SYSTEM_INSTRUCTION = """あなたは金融・経済・株式市場のプロフェッショナル要約アシスタントです。
以下のルールを厳格に遵守して要約してください:
1. 【ハルシネーションの絶対禁止】: 提供された入力テキストに存在しない数値、銘柄、事実、憶測を勝手に創作しないでください。テキストから読み取れない情報は「情報なし」と記述してください。
2. 【3行要約の徹底】: 各トピックまたは全体について、要点を最大3行（箇条書き）で簡潔かつ正確にまとめてください。
3. 【専門用語・数値の正確性】: 指数、株価、利回り、騰落率などの数値は入力ソースの表記を正確に反映してください。
4. 【フォーマット】: 余計な前置きや挨拶は一切含めず、要約本文のみを出力してください。
"""

    @classmethod
    def summarize_text(cls, text: str, max_lines: int = 3, topic_hint: str = "") -> str:
        """
        与えられたテキストを要約する。
        LLMが利用可能な場合はGeminiで要約し、不可時はルールベースで整形。
        """
        if not text or not text.strip():
            return "情報なし"

        cleaned_text = text.strip()

        # 1. Gemini APIによる要約試行
        if genai_client:
            try:
                prompt = (
                    f"【対象トピック】: {topic_hint or '市場ニュース'}\n"
                    f"【入力テキスト】:\n{cleaned_text[:4000]}\n\n"
                    f"上記テキストの内容を厳密に基づいて{max_lines}行以内の箇条書きで要約してください。"
                )
                response = genai_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config={
                        "system_instruction": cls.SYSTEM_INSTRUCTION,
                        "temperature": 0.2,  # 低温度で事実重視
                    }
                )
                if response and response.text:
                    summary = response.text.strip()
                    logger.info("[Summarizer] Geminiによる要約生成成功")
                    return summary
            except Exception as e:
                logger.warning(f"[Summarizer] Gemini API呼び出し失敗 (ルールベースへ移行): {e}")

        # 2. ルールベースによるフォールバック要約
        return cls._rule_based_fallback(cleaned_text, max_lines)

    @classmethod
    def _rule_based_fallback(cls, text: str, max_lines: int = 3) -> str:
        """
        LLMが利用できない場合のフォールバック要約。
        改行や句点で分割し、主要な文を抽出して箇条書き化。
        """
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return "情報なし"

        # すでに箇条書き形式の場合はそのまま活用
        bullet_candidates = []
        for line in lines:
            # 記号を除去
            clean = re.sub(r"^[・\-\*0-9\.\s]+", "", line).strip()
            if len(clean) >= 10:  # 意味のある長さの文
                bullet_candidates.append(clean)

        if not bullet_candidates:
            # 句点で分割
            sentences = [s.strip() for s in re.split(r"[。\n]", text) if len(s.strip()) >= 10]
            bullet_candidates = sentences

        selected = bullet_candidates[:max_lines]
        if not selected:
            return "情報なし"

        return "\n".join(f"• {item}" for item in selected)

    @classmethod
    def structure_market_indicators(cls, indicators: Dict[str, Any]) -> str:
        """
        市場指標辞書を綺麗なMarkdown箇条書きに構造化する
        """
        if not indicators:
            return "情報なし"

        lines = []
        for name, data in indicators.items():
            if isinstance(data, dict):
                price = data.get("price", "N/A")
                change = data.get("change", "")
                pct = data.get("change_pct", "")
                trend_emoji = "🔺" if "+" in str(change) or "+" in str(pct) else ("🔻" if "-" in str(change) or "-" in str(pct) else "▫️")
                lines.append(f"{trend_emoji} **{name}**: `{price}` ({change}, {pct})")
            else:
                lines.append(f"▫️ **{name}**: `{data}`")

        return "\n".join(lines) if lines else "情報なし"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample_text = """
    ニューヨーク株式市場は反発。ハイテク株を中心に買いが先行し、S&P500指数とナスダック総合指数はともに最高値を更新した。
    半導体大手エヌビディアが次世代AIチップの需要堅調を背景に3%超の上昇となり相場を牽引。
    一方、発表された米消費者物価指数(CPI)は市場予想と一致し、利下げ観測を支える結果となった。原油先物は小幅に続落している。
    """
    res = Summarizer.summarize_text(sample_text, max_lines=3, topic_hint="海外市場")
    print("=== 要約結果 ===")
    print(res)
