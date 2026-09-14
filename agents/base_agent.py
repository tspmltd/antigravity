import os
import re
import json
from abc import ABC
from typing import Dict, Any, Optional


class BaseAgent(ABC):
    """
    各サブエージェントの基底クラス。
    LLM APIとの通信や、APIキー未設定時のフォールバック（モック/テンプレート生成）を担当。
    """

    def __init__(self, name: str, role: str, config: Optional[Dict[str, Any]] = None):
        self.name = name
        self.role = role
        self.config = config or {}
        self.provider = self.config.get("llm", {}).get("provider", "mock")

    def call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """
        LLMにプロンプトを送信し、回答テキストを取得する。
        APIキーがあれば外部LLM（Gemini/OpenAI等）を呼び出し、なければモックロジックへ委任。
        """
        gemini_key = os.environ.get("GEMINI_API_KEY")
        openai_key = os.environ.get("OPENAI_API_KEY")

        if self.provider == "gemini" and gemini_key:
            return self._call_gemini(system_prompt, user_prompt, gemini_key)
        elif self.provider == "openai" and openai_key:
            return self._call_openai(system_prompt, user_prompt, openai_key)
        else:
            return self._mock_response(system_prompt, user_prompt)

    def _call_gemini(self, system_prompt: str, user_prompt: str, api_key: str) -> str:
        # 今後google-genaiやgoogle.generativeaiパッケージ導入時に拡張可能
        return self._mock_response(system_prompt, user_prompt)

    def _call_openai(self, system_prompt: str, user_prompt: str, api_key: str) -> str:
        # 今後openaiパッケージ導入時に拡張可能
        return self._mock_response(system_prompt, user_prompt)

    def _mock_response(self, system_prompt: str, user_prompt: str) -> str:
        """各サブクラスでオーバーライド可能なフォールバック"""
        return "Mock LLM Response"

    @staticmethod
    def extract_python_code(text: str) -> str:
        """Markdownの ```python ... ``` ブロックから純粋なコードを抽出"""
        match = re.search(r"```(?:python)?\s*([\s\S]*?)\s*```", text)
        if match:
            return match.group(1).strip()
        return text.strip()
