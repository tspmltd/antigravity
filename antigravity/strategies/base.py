from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class BaseTickStrategy(ABC):
    """
    ティック駆動型（Tick-Driven / Event-Driven）戦略の基底抽象クラス。
    ミリ秒単位の約定ストリームとTaker Deltaを受信し、即座に売買アクションを判定・返却する。
    """

    def __init__(self, name: str, version: str = "v1.0", parameters: Optional[Dict[str, Any]] = None):
        self.name = name
        self.version = version
        self.parameters = parameters or {}
        self.strategy_type = "tick"

    @abstractmethod
    def on_tick(
        self,
        tick: Dict[str, Any],
        flow_stats: Dict[str, Any],
        current_pos: float,
        entry_price: float,
    ) -> Dict[str, Any]:
        """
        最新Tickおよびフロー統計を受け取り、実行アクションを返却。

        Returns:
            Dict containing:
                - action: 'BUY', 'SELL', 'EXIT', 'CANCEL', 'REFILL', 'HOLD'
                - reason: アクション要因
                - target_price: 指値価格（成行はNone）
        """
        pass
