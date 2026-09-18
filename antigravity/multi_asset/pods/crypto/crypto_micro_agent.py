"""
antigravity/multi_asset/pods/crypto/crypto_micro_agent.py: 暗号資産専属 MICRO担当 AGENT
=================================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 に準拠。
- bitFlyer FX_BTC_JPY ミリ秒板インバランス & Taker約定フロー推定
- 出力: MicroSignal (asset_class="BTC")
"""

import time
from typing import Dict, Any, Optional, List

from ...schemas import MicroSignal
from ...base_agent import BaseMicroAgent


class CryptoMicroAgent(BaseMicroAgent):
    """
    BTC/JPY 専属 MICRO担当 AGENT
    """

    def __init__(self, symbols: Optional[List[str]] = None):
        super().__init__(asset_class="BTC", symbols=symbols or ["FX_BTC_JPY"])

    def evaluate_micro(self, market_data: Dict[str, Any]) -> MicroSignal:
        """
        BTC/JPYの板データ・Takerデルタから MicroSignal を算出
        market_data 例:
        {
            "symbol": "FX_BTC_JPY",
            "bid_price": 10005000.0,
            "ask_price": 10006000.0,
            "bid_vol": 4.5,
            "ask_vol": 2.1,
            "taker_delta": +1.5,
            "timestamp": 1726620000.0,
        }
        """
        symbol = market_data.get("symbol", self.symbols[0])
        bid_p = float(market_data.get("bid_price", 10000000.0))
        ask_p = float(market_data.get("ask_price", 10001000.0))
        bid_v = float(market_data.get("bid_vol", 1.0))
        ask_v = float(market_data.get("ask_vol", 1.0))
        spread = max(0.0, ask_p - bid_p)
        taker_delta = float(market_data.get("taker_delta", 0.0))

        imbalance_ratio = bid_v / max(0.01, ask_v)

        if imbalance_ratio >= 1.4 or taker_delta > 1.0:
            direction = "LONG"
            confidence = min(95.0, 50.0 + (imbalance_ratio - 1.0) * 20.0 + max(0.0, taker_delta * 10.0))
        elif imbalance_ratio <= 0.7 or taker_delta < -1.0:
            direction = "SHORT"
            confidence = min(95.0, 50.0 + (1.0 / max(0.01, imbalance_ratio) - 1.0) * 20.0 + max(0.0, -taker_delta * 10.0))
        else:
            direction = "NEUTRAL"
            confidence = 50.0

        if spread > 3000.0:
            regime = "ILLIQUID"
        elif spread <= 500.0 and confidence >= 70.0:
            regime = "TREND"
        else:
            regime = "MEAN_REVERT"

        sig = MicroSignal(
            asset_class=self.asset_class,
            symbol=symbol,
            direction=direction,
            confidence=round(confidence, 1),
            regime=regime,
            spread_jpy=round(spread, 1),
            imbalance_ratio=round(imbalance_ratio, 3),
            timestamp=float(market_data.get("timestamp", time.time())),
            extra_metrics={"taker_delta": taker_delta},
        )
        self.last_signal = sig
        return sig
