"""
antigravity/multi_asset/macro_impact_agent.py: 第2階層 MACRO Impact AGENT
=======================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 に準拠。
- 経済指標・法定開示 (TDnet/EDINET)・PTS夜間急変・世界主要市場の統合解析
- MIS (0〜100) および 全資産インパクトマップ (Asset Impact Map) の自動導出
- 第3階層 司令塔 (Regime Orchestrator) への高解像度マクロ情報提供
"""

import time
import logging
from typing import Dict, Any, Optional, List

from .schemas import MacroImpact
from news_pipeline.market_impact_scorer import MarketImpactScorer, MarketEvent
from news_pipeline.pts_causal_engine import PTSFeatureRecord

logger = logging.getLogger("antigravity.multi_asset.macro_impact_agent")


class MacroImpactAgent:
    """
    第2階層: MACRO Impact AGENT
    全資産共通の上位インテリジェンス。
    TDnet・PTS・世界市場センチネルと接続し、マクロショックと資産影響度を評価する。
    """

    def __init__(self):
        self.scorer = MarketImpactScorer()
        self.last_macro: Optional[MacroImpact] = None

    def evaluate_tdnet_event(self, event: MarketEvent) -> MacroImpact:
        """
        東証TDnet開示イベントからMacroImpactを算出
        """
        mis = event.mis if event.mis > 0 else self.scorer.calculate_mis(event)
        event.mis = mis

        # レベル判定
        if mis >= 85:
            level = "CRITICAL"
            regime = "RISK_OFF"
        elif mis >= 70:
            level = "WARNING"
            regime = "RISK_OFF" if event.direction == "down" else "NEUTRAL"
        else:
            level = "NORMAL"
            regime = "NEUTRAL"

        # 資産クラス別インパクトマップ
        asset_map = {}
        if event.direction == "down" and mis >= 70:
            asset_map["JP_STOCK"] = "BEAR"
            asset_map["FX"] = "BEAR_JPY"
            asset_map["BTC"] = "NEUTRAL"
        elif event.direction == "up" and mis >= 70:
            asset_map["JP_STOCK"] = "BULL"
            asset_map["FX"] = "BULL_JPY"
            asset_map["BTC"] = "NEUTRAL"
        else:
            asset_map["JP_STOCK"] = "NEUTRAL"
            asset_map["FX"] = "NEUTRAL"
            asset_map["BTC"] = "NEUTRAL"

        macro = MacroImpact(
            impact_score=mis,
            level=level,
            primary_event=f"TDnet:{event.name}({event.symbol or ''}) {event.headline_metric or event.event_type}",
            global_regime=regime,
            asset_impact_map=asset_map,
            horizon="IMMEDIATE" if mis >= 70 else "INTRADAY",
            timestamp=time.time(),
            details={"source": event.source, "event_type": event.event_type},
        )
        self.last_macro = macro
        return macro

    def evaluate_pts_anomaly(
        self, record: PTSFeatureRecord, cis2_result: Dict[str, Any]
    ) -> MacroImpact:
        """
        PTS夜間急変と因果AI判定からMacroImpactを算出
        """
        cis2_score = cis2_result.get("cis2_score", 0)
        label = cis2_result.get("label", "NONE")  # DIRECT, PARTIAL, NONE

        # PTS急変のMIS換算 (CIS2スコア 0〜200 を 0〜100 に正規化)
        mis = min(100, int(cis2_score * 0.5))
        if abs(record.pts_change_pct) >= 10.0:
            mis = max(mis, 75)

        level = "WARNING" if mis >= 70 else "NORMAL"
        if mis >= 85:
            level = "CRITICAL"

        regime = "RISK_OFF" if (record.pts_change_pct < -5.0 and label == "DIRECT") else "NEUTRAL"

        asset_map = {
            "JP_STOCK": "BULL" if record.pts_change_pct > 0 else "BEAR",
            "BTC": "NEUTRAL",
            "FX": "NEUTRAL",
        }

        macro = MacroImpact(
            impact_score=mis,
            level=level,
            primary_event=f"PTS:{record.name}({record.symbol}) {record.pts_change_pct:+.1f}% ({label})",
            global_regime=regime,
            asset_impact_map=asset_map,
            horizon="IMMEDIATE",
            timestamp=time.time(),
            details={
                "cis2_score": cis2_score,
                "label": label,
                "volume_ratio": record.pts_volume_ratio,
            },
        )
        self.last_macro = macro
        return macro

    def evaluate_world_market_snapshot(
        self, indices: Dict[str, float]
    ) -> MacroImpact:
        """
        世界株価・先物・為替のスナップショットから全体レジームを総合判定
        indices例: {"nikkei_fut": +1.5, "sp500": +1.2, "nasdaq": +1.8, "us_10y_yield": 4.1, "usdjpy": 155.2}
        """
        nikkei = indices.get("nikkei_fut", 0.0)
        sp500 = indices.get("sp500", 0.0)
        nasdaq = indices.get("nasdaq", 0.0)

        avg_us = (sp500 + nasdaq) / 2.0

        # マクロスコア判定
        if avg_us <= -2.5 or nikkei <= -3.0:
            mis = 88
            level = "CRITICAL"
            regime = "RISK_OFF"
        elif avg_us <= -1.5 or nikkei <= -1.5:
            mis = 72
            level = "WARNING"
            regime = "RISK_OFF"
        elif avg_us >= 1.5 and nikkei >= 1.0:
            mis = 65
            level = "NORMAL"
            regime = "RISK_ON"
        else:
            mis = 30
            level = "NORMAL"
            regime = "NEUTRAL"

        asset_map = {
            "JP_STOCK": "BULL" if nikkei > 0.5 else ("BEAR" if nikkei < -0.5 else "NEUTRAL"),
            "BTC": "BULL" if avg_us > 1.0 else ("BEAR" if avg_us < -1.0 else "NEUTRAL"),
            "FX": "BULL_USD" if indices.get("usdjpy_change", 0.0) > 0.5 else "NEUTRAL",
        }

        macro = MacroImpact(
            impact_score=mis,
            level=level,
            primary_event=f"GlobalMarket: US={avg_us:+.2f}%, NK={nikkei:+.2f}%",
            global_regime=regime,
            asset_impact_map=asset_map,
            horizon="INTRADAY",
            timestamp=time.time(),
            details={"indices": indices},
        )
        self.last_macro = macro
        return macro

    def get_default_normal_macro(self) -> MacroImpact:
        """平常時のデフォルトマクロインパクト"""
        return MacroImpact(
            impact_score=20,
            level="NORMAL",
            primary_event="Normal Market Operations",
            global_regime="NEUTRAL",
            asset_impact_map={"JP_STOCK": "NEUTRAL", "BTC": "NEUTRAL", "FX": "NEUTRAL"},
            horizon="INTRADAY",
            timestamp=time.time(),
        )
