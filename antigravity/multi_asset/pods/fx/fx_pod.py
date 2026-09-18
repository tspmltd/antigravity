"""
antigravity/multi_asset/pods/fx/fx_pod.py: 為替 (FX) 専属ポッド (FX Pod)
===================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 & 5 に準拠。
- USD/JPY, EUR/JPY等の為替取引を担う第1階層自律ポッド
- MICRO (ピップス・セッション) / ALPHA (金利差・仲値) / EXECUTION (ロット・損益計算)
"""

from typing import Optional, List

from ...base_agent import BaseAssetPod
from .fx_micro_agent import FxMicroAgent
from .fx_alpha_agent import FxAlphaAgent
from .fx_execution_agent import FxExecutionAgent


class FxPod(BaseAssetPod):
    """
    為替 (FX) 専属ポッド (FX Pod)
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        initial_risk_budget_jpy: float = 30000.0,
        micro_agent: Optional[FxMicroAgent] = None,
        alpha_agent: Optional[FxAlphaAgent] = None,
        execution_agent: Optional[FxExecutionAgent] = None,
    ):
        micro = micro_agent or FxMicroAgent(symbols=symbols)
        alpha = alpha_agent or FxAlphaAgent()
        execution = execution_agent or FxExecutionAgent(initial_risk_budget_jpy=initial_risk_budget_jpy)

        super().__init__(
            asset_class="FX",
            micro_agent=micro,
            alpha_agent=alpha,
            execution_agent=execution,
        )
