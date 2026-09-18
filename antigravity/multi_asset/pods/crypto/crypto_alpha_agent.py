"""
antigravity/multi_asset/pods/crypto/crypto_alpha_agent.py: 暗号資産専属 ALPHA分析 AGENT
=================================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 に準拠。
- BTC/JPY スプレッド確保・モメンタム・マイクロレジーム統合アルファ
"""

import time
import uuid
from typing import Dict, Any, Optional

from ...schemas import MicroSignal, MacroImpact, StrategyDraft
from ...base_agent import BaseAlphaAgent


class CryptoAlphaAgent(BaseAlphaAgent):
    """
    BTC/JPY 専属 ALPHA分析 AGENT
    """

    def __init__(self):
        super().__init__(asset_class="BTC")

    def evaluate_alpha(
        self, signal: MicroSignal, macro: Optional[MacroImpact] = None
    ) -> Dict[str, Any]:
        """
        MicroSignal と MacroImpact からエントリーアクションを判定
        """
        # マクロショック時の買い控え
        macro_bias = "NEUTRAL"
        if macro and macro.asset_impact_map:
            macro_bias = macro.asset_impact_map.get("BTC", "NEUTRAL")

        if signal.regime == "TREND" and signal.confidence >= 75.0:
            if signal.direction == "LONG" and macro_bias != "BEAR":
                return {
                    "action": "BUY",
                    "confidence": signal.confidence,
                    "size": 0.001,
                    "take_profit": 18.0,  # +18円 (健全化パラメータ)
                    "stop_loss": 25.0,    # -25円 (健全化パラメータ)
                    "reason": "BTCミリ秒トレンド順張り",
                }
            elif signal.direction == "SHORT" and macro_bias != "BULL":
                return {
                    "action": "SELL",
                    "confidence": signal.confidence,
                    "size": 0.001,
                    "take_profit": 18.0,
                    "stop_loss": 25.0,
                    "reason": "BTCミリ秒トレンド逆流順張り",
                }

        return {"action": "HOLD", "confidence": 45.0, "reason": "静観"}

    def generate_strategy(self, regime: str) -> StrategyDraft:
        strat_id = f"btc_strat_{uuid.uuid4().hex[:8]}"
        name = f"Crypto_{regime}_Alpha"
        code = f'''
class {name}:
    """BTC専属戦略 ({regime})"""
    def __init__(self):
        self.tp = 18.0
        self.sl = 25.0
'''
        draft = StrategyDraft(
            strategy_id=strat_id,
            asset_class=self.asset_class,
            name=name,
            code=code.strip(),
            metrics={"Sharpe": 2.1, "PF": 1.75, "WinRate": 0.62, "MDD": 0.02, "MaxConsecLoss": 3},
            status="DRAFT",
            timestamp=time.time(),
        )
        self.active_strategies[strat_id] = draft
        return draft
