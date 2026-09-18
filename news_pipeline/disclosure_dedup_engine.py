"""
news_pipeline/disclosure_dedup_engine.py: TDnet (東証) x EDINET 統合重複判定エンジン
=====================================================================================
1. 企業名・銘柄コード・キーワードの正規化による完全重複判定
2. 発表時刻 (±30分以内) の同一案件の二重投稿を物理遮断
3. 重複検知時は「東証TDnet／EDINET (重複統合)」として一本化
4. X向け最適化フォーマット (1行企業名、10文字要約、防護連携、視認性MAX) を生成
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger("news_pipeline.disclosure_dedup")
JST = timezone(timedelta(hours=9))


class DisclosureDedupEngine:
    """東証TDnet x 金融庁EDINET 重複判定・統合エンジン"""

    def __init__(self, cache_file: Optional[str] = None, window_sec: float = 1800.0):
        self.window_sec = window_sec  # 重複判定ウィンドウ (デフォルト30分)
        if cache_file is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cache_file = os.path.join(base_dir, "data", "disclosure_dedup_cache.json")
        self.cache_file = cache_file
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)

        self.history: List[Dict[str, Any]] = []
        self._load_cache()

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.history = data.get("history", [])
            except Exception as e:
                logger.warning(f"[DedupEngine] キャッシュロード失敗: {e}")

    def _save_cache(self):
        try:
            # 直近300件を保持
            self.history = self.history[-300:]
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump({
                    "history": self.history,
                    "updated_at": datetime.now(JST).isoformat()
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[DedupEngine] キャッシュ保存失敗: {e}")

    @staticmethod
    def extract_entity_key(text: str) -> str:
        """企業名・銘柄コード等の識別キーを抽出して正規化"""
        clean = re.sub(r"[\s\W_]+", "", text)
        clean = re.sub(r"株式会社|有限会社|ホールディングス|HD", "", clean)
        return clean[:8]

    @staticmethod
    def extract_category(text: str) -> str:
        """開示の主要カテゴリを判定"""
        if "TOB" in text or "公開買付" in text or "買収" in text:
            return "TOB/買収"
        if "大量保有" in text or "変更報告" in text:
            return "大量保有報告"
        if "自社株買" in text:
            return "自社株買い"
        if "決算" in text or "業績" in text or "上方修正" in text or "下方修正" in text:
            return "決算/業績修正"
        return "適時開示"

    def check_and_register(
        self,
        source: str,             # "EDINET" or "TDnet"
        raw_title: str,
        condensed_text: str,
        link: str,
        timestamp: Optional[float] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        開示をチェックし、重複しているかを判定。
        Returns:
            (is_duplicate: bool, display_source: str, merged_item: Dict[str, Any])
        """
        now = timestamp or time.time()
        entity = self.extract_entity_key(raw_title)
        category = self.extract_category(raw_title)

        # 過去の開示履歴と照合 (直近30分以内 & 企業名一致 & カテゴリ一致)
        for prev in reversed(self.history):
            if (now - prev["ts"]) > self.window_sec:
                break

            prev_entity = prev["entity"]
            prev_cat = prev["category"]

            # 同一企業 かつ 同一カテゴリ (例: スミダコーポ x 大量保有)
            if entity and prev_entity and (entity in prev_entity or prev_entity in entity) and (category == prev_cat):
                logger.info(f"[DedupEngine] ⚡ 重複開示を検知: {source} [{raw_title[:20]}] vs 既存 {prev['source']} [{prev['raw_title'][:20]}]")
                # 既存のソースが異なる場合は統合タグに更新
                if source != prev["source"]:
                    display_source = "東証TDnet／EDINET (重複統合)"
                else:
                    display_source = f"{source} (重複更新)"

                merged_item = {
                    "entity": entity,
                    "category": category,
                    "condensed_text": condensed_text,
                    "raw_title": raw_title,
                    "link": link,
                    "source": display_source,
                    "is_duplicate": True,
                }
                return True, display_source, merged_item

        # 新規開示として登録
        entry = {
            "ts": now,
            "source": source,
            "entity": entity,
            "category": category,
            "condensed_text": condensed_text,
            "raw_title": raw_title,
            "link": link,
        }
        self.history.append(entry)
        self._save_cache()
        return False, source, entry

    @staticmethod
    def format_x_disclosure(item: Dict[str, Any], protection_str: str = "通常運転") -> str:
        """
        X向け最適化フォーマット (視認性MAX・1行企業名・要約・重複明記)
        """
        entity = item.get("entity", "企業")
        cat = item.get("category", "適時開示")
        summary = item.get("condensed_text", "")
        source = item.get("source", "適時開示")

        # 重要度バッジ
        importance = "高" if "TOB" in cat or "買収" in cat else "中"

        lines = [
            f"【開示速報】{entity}／{cat}",
            f"📌要約：{summary}",
            f"📍発表：{source}",
            f"⭐重要度：{importance}",
            f"⚠️影響：{protection_str}",
        ]
        footer = "\n\n#日本株 #適時開示 #EDINET #TDnet"
        return "\n".join(lines) + footer


# シングルトンインスタンス
default_dedup_engine = DisclosureDedupEngine()
