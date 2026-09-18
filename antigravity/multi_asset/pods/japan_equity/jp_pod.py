"""
antigravity/multi_asset/pods/japan_equity/jp_pod.py: 日本株専属ポッド (Japan Equity Pod)
==================================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 & 5 に準拠。
- MICRO (板・フロー) / ALPHA (TDnet・PTS因果AI・乖離) / EXECUTION (100株単位・呼値・CB) を一体化。
- 東証適時開示 (TDnet) および 夜間PTS取引からのシームレスな入力受付インターフェースを完備。
"""

from typing import Dict, Any, Optional, List

from ...base_agent import BaseAssetPod
from .jp_micro_agent import JpMicroAgent
from .jp_alpha_agent import JpAlphaAgent
from .jp_execution_agent import JpExecutionAgent
from news_pipeline.market_impact_scorer import MarketEvent
from news_pipeline.pts_causal_engine import PTSFeatureRecord


class JapanEquityPod(BaseAssetPod):
    """
    日本株専属ポッド (Japan Equity Pod)
    東証プライム/グロース現物株取引を担当する第1階層自律ポッド。
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        initial_risk_budget_jpy: float = 20000.0,
        micro_agent: Optional[JpMicroAgent] = None,
        alpha_agent: Optional[JpAlphaAgent] = None,
        execution_agent: Optional[JpExecutionAgent] = None,
    ):
        micro = micro_agent or JpMicroAgent(symbols=symbols)
        alpha = alpha_agent or JpAlphaAgent()
        execution = execution_agent or JpExecutionAgent(initial_risk_budget_jpy=initial_risk_budget_jpy)

        super().__init__(
            asset_class="JP_STOCK",
            micro_agent=micro,
            alpha_agent=alpha,
            execution_agent=execution,
        )

    def inject_tdnet_event(self, event: MarketEvent) -> None:
        """TDnet開示イベントをポッド（ALPHA）へ注入"""
        if event.symbol:
            self.alpha.register_disclosure(
                symbol=event.symbol,
                event_data={
                    "mis": event.mis,
                    "direction": event.direction,
                    "event_type": event.event_type,
                    "label": "DIRECT",
                },
            )

    def inject_pts_anomaly(
        self, record: PTSFeatureRecord, cis2_result: Dict[str, Any]
    ) -> None:
        """PTS夜間取引急変および因果AI判定をポッド（ALPHA）へ注入"""
        direction = "positive" if record.pts_change_pct > 0 else "negative"
        mis = min(100, int(cis2_result.get("cis2_score", 0) * 0.5))
        self.alpha.register_disclosure(
            symbol=record.symbol,
            event_data={
                "mis": mis,
                "direction": direction,
                "event_type": "PTS夜間急変",
                "label": cis2_result.get("label", "NONE"),
            },
        )
