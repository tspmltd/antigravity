"""
antigravity/multi_asset/base_agent.py: 各階層・ポッド用基底クラス (Base Agent & Pod Pattern)
================================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 に準拠。
資産クラス固有の市場構造（取引時間、板呼値、プロトコル）をポッド内にカプセル化。
"""

import time
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Tuple

from .schemas import (
    MicroSignal,
    MacroImpact,
    ExecutionCommand,
    StrategyDraft,
    OrderCommand,
    TradeReport,
    AssetPodState,
)

logger = logging.getLogger("antigravity.multi_asset.base_agent")


class BaseMicroAgent(ABC):
    """
    第1階層: MICRO担当AGENT 基底クラス
    秒〜分単位の短期方向性・板インバランス・流動性レジーム推定
    """

    def __init__(self, asset_class: str, symbols: List[str]):
        self.asset_class = asset_class
        self.symbols = symbols
        self.last_signal: Optional[MicroSignal] = None

    @abstractmethod
    def evaluate_micro(self, market_data: Dict[str, Any]) -> MicroSignal:
        """
        板データ / 歩み値 / スプレッドから MicroSignal を算出
        """
        pass


class BaseAlphaAgent(ABC):
    """
    第1階層: ALPHA分析AGENT 基底クラス
    アルファ源探索・マクロとミクロの結合・戦略起草
    """

    def __init__(self, asset_class: str):
        self.asset_class = asset_class
        self.active_strategies: Dict[str, StrategyDraft] = {}

    @abstractmethod
    def evaluate_alpha(
        self, signal: MicroSignal, macro: Optional[MacroImpact] = None
    ) -> Dict[str, Any]:
        """
        MicroSignal と MacroImpact からエントリー/エグジット期待値を評価
        戻り値: {"action": "BUY"|"SELL"|"HOLD", "confidence": float, "reason": str, ...}
        """
        pass

    @abstractmethod
    def generate_strategy(self, regime: str) -> StrategyDraft:
        """
        現在のレジームに適合する新戦略コード/パラメータを起草
        """
        pass


class BaseExecutionAgent(ABC):
    """
    第1階層: EXECUTION AGENT 基底クラス
    リアルタイム執行・日次リスクリミット・CB・上位命令厳守
    """

    def __init__(self, asset_class: str, initial_risk_budget_jpy: float = 10000.0):
        self.asset_class = asset_class
        self.allocated_risk_jpy = initial_risk_budget_jpy
        self.target_mode = "HYBRID"  # "HFT", "TREND", "HYBRID", "REDUCE_50", "STOP"
        self.is_halted = False
        self.max_position_size = 1.0
        self.active_positions: Dict[str, float] = {}
        self.daily_pnl_jpy = 0.0
        self.trade_history: List[TradeReport] = []
        self.last_command: Optional[ExecutionCommand] = None

    def handle_execution_command(self, command: ExecutionCommand) -> None:
        """
        第3階層 司令塔 (Regime Orchestrator) からのガバナンス命令を受信・即時反映
        上位命令の絶対優先（Top-Down Precedence）
        """
        self.last_command = command
        self.target_mode = command.target_mode
        self.allocated_risk_jpy = command.allocated_risk_jpy
        self.max_position_size = command.max_position_size
        self.is_halted = command.is_halted

        if self.is_halted or self.target_mode == "STOP":
            logger.warning(
                f"[{self.asset_class}:EXEC] 🚨 司令塔STOP命令を受領: is_halted={self.is_halted}, mode={self.target_mode}, 理由={command.reason}"
            )

    @abstractmethod
    def execute_order(self, order: OrderCommand) -> Optional[TradeReport]:
        """
        発注コマンドを実行 (成行/指値、建玉管理、スリッページ適用)
        STOP発令時または日次損失リミット到達時は新規発注を拒否 (None)
        """
        pass

    def get_pod_state(self) -> AssetPodState:
        """現在のポッド状態を返す"""
        return AssetPodState(
            asset_class=self.asset_class,
            current_mode=self.target_mode,
            active_positions=dict(self.active_positions),
            daily_pnl_jpy=self.daily_pnl_jpy,
            allocated_budget_jpy=self.allocated_risk_jpy,
            is_halted=self.is_halted,
            last_command=self.last_command,
            updated_at=time.time(),
        )


class BaseAssetPod(ABC):
    """
    資産クラス専属ポッド (Pod Pattern)
    MICRO / ALPHA / EXECUTION の3役を1つの自律ユニットとしてカプセル化
    """

    def __init__(
        self,
        asset_class: str,
        micro_agent: BaseMicroAgent,
        alpha_agent: BaseAlphaAgent,
        execution_agent: BaseExecutionAgent,
    ):
        self.asset_class = asset_class
        self.micro = micro_agent
        self.alpha = alpha_agent
        self.execution = execution_agent

    def process_tick(
        self, market_data: Dict[str, Any], macro: Optional[MacroImpact] = None
    ) -> Tuple[MicroSignal, Optional[OrderCommand], Optional[TradeReport]]:
        """
        単一ティック / 板データサイクルの処理:
        1. MICROが板・フローから短期シグナルを抽出
        2. ALPHAがマクロ・ミクロを総合して売買シグナル判定
        3. EXECUTIONがガバナンス枠内で発注・約定
        """
        # 1. MICRO判定
        signal = self.micro.evaluate_micro(market_data)

        # 司令塔STOP時は新規エントリー生成をスキップ
        if self.execution.is_halted or self.execution.target_mode == "STOP":
            return signal, None, None

        # 2. ALPHA判定
        alpha_res = self.alpha.evaluate_alpha(signal, macro)
        action = alpha_res.get("action", "HOLD")

        order: Optional[OrderCommand] = None
        report: Optional[TradeReport] = None

        if action in ("BUY", "SELL"):
            symbol = market_data.get("symbol", signal.symbol)
            size = alpha_res.get("size", 100.0 if self.asset_class == "JP_STOCK" else 0.001)
            order = OrderCommand(
                order_id=f"ord_{int(time.time()*1000)}",
                asset_class=self.asset_class,
                symbol=symbol,
                side=action,
                order_type=alpha_res.get("order_type", "MARKET"),
                price=alpha_res.get("price"),
                size=min(size, self.execution.max_position_size),
                stop_loss=alpha_res.get("stop_loss"),
                take_profit=alpha_res.get("take_profit"),
                reason=alpha_res.get("reason", "Alpha Signal"),
                extra=dict(signal.extra_metrics),
                timestamp=time.time(),
            )
            report = self.execution.execute_order(order)

        return signal, order, report

    def apply_governance_command(self, command: ExecutionCommand) -> None:
        """司令塔からのガバナンス命令を反映"""
        self.execution.handle_execution_command(command)

    def get_state(self) -> AssetPodState:
        """ポッド状態を取得"""
        state = self.execution.get_pod_state()
        state.last_signal = self.micro.last_signal
        return state
