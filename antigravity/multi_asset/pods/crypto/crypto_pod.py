"""
antigravity/multi_asset/pods/crypto/crypto_pod.py: 暗号資産専属ポッド (Crypto Pod)
=============================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 に準拠。
- BITFLYERライン非破壊原則: 既存本番稼働ライン(A1〜A11, run_live.py)を保護しつつ、
  多資産OS統合インターフェースを提供する自律ポッド。
"""

from typing import Optional, List

from ...base_agent import BaseAssetPod
from .crypto_micro_agent import CryptoMicroAgent
from .crypto_alpha_agent import CryptoAlphaAgent
from .crypto_execution_agent import CryptoExecutionAgent


class CryptoPod(BaseAssetPod):
    """
    BTC/JPY 専属ポッド (Crypto Pod)
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        initial_risk_budget_jpy: float = 30000.0,
        micro_agent: Optional[CryptoMicroAgent] = None,
        alpha_agent: Optional[CryptoAlphaAgent] = None,
        execution_agent: Optional[CryptoExecutionAgent] = None,
    ):
        micro = micro_agent or CryptoMicroAgent(symbols=symbols)
        alpha = alpha_agent or CryptoAlphaAgent()
        execution = execution_agent or CryptoExecutionAgent(initial_risk_budget_jpy=initial_risk_budget_jpy)

        super().__init__(
            asset_class="BTC",
            micro_agent=micro,
            alpha_agent=alpha,
            execution_agent=execution,
        )
