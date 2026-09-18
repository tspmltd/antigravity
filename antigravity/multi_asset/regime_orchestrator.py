"""
antigravity/multi_asset/regime_orchestrator.py: 第3階層 司令塔 (Regime Orchestrator AGENT)
===================================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 に準拠。
- 全体レジーム判定 (Risk-On / Risk-Off / Shock / Neutral)
- 戦略動作モード決定 (HFT / Trend / Hybrid / REDUCE_50 / STOP)
- 資本 & リスクバジェット動的配分 (Risk Budgeting)
- 上位命令の絶対優先（Top-Down Precedence）
"""

import time
import logging
from typing import Dict, Any, Optional, List, Tuple

from .schemas import (
    MicroSignal,
    MacroImpact,
    ExecutionCommand,
    AssetPodState,
)
from .base_agent import BaseAssetPod
from .ipc_bridge import FusionEngineIPCClient

logger = logging.getLogger("antigravity.multi_asset.regime_orchestrator")


class RegimeOrchestratorAgent:
    """
    第3階層: 司令塔 (Regime Orchestrator AGENT)
    全資産クラスのポッドを統括し、マクロ影響度と連動してトップダウンでガバナンスを発令する。
    """

    # レジーム＆5大クロスアセット・プレイブック別リスクバジェット配分比率 (合計 <= 1.0)
    BUDGET_RATIOS = {
        # 基本レジーム
        "RISK_ON": {
            "BTC": 0.30,
            "JP_STOCK": 0.40,
            "FX": 0.30,
            "CASH": 0.00,
        },
        "NEUTRAL": {
            "BTC": 0.25,
            "JP_STOCK": 0.50,
            "FX": 0.25,
            "CASH": 0.00,
        },
        "RISK_OFF": {
            "BTC": 0.10,
            "JP_STOCK": 0.20,
            "FX": 0.20,
            "CASH": 0.50,
        },
        "SHOCK": {
            "BTC": 0.05,
            "JP_STOCK": 0.15,
            "FX": 0.00,
            "CASH": 0.80,
        },
        # 5大クロスアセット・プレイブック (SPEC-FX-20260918-002)
        "CRYPTO_DOMINANT": {
            "BTC": 0.50,
            "JP_STOCK": 0.20,
            "FX": 0.20,
            "CASH": 0.10,
        },
        "FX_MACRO_DOMINANT": {
            "BTC": 0.20,
            "JP_STOCK": 0.15,
            "FX": 0.50,
            "CASH": 0.15,
        },
        "JP_EQUITY_CATALYST": {
            "BTC": 0.25,
            "JP_STOCK": 0.50,
            "FX": 0.15,
            "CASH": 0.10,
        },
        "BALANCED_TRI_ASSET": {
            "BTC": 0.30,
            "JP_STOCK": 0.40,
            "FX": 0.30,
            "CASH": 0.00,
        },
        "DEFENSIVE_FX_ANCHOR": {
            "BTC": 0.00,
            "JP_STOCK": 0.10,
            "FX": 0.40,
            "CASH": 0.50,
        },
    }

    # 各資産のデフォルト最大ポジションサイズ (BTC: 単位BTC, 日本株: 単位株, FX: 単位ロット[万通貨])
    DEFAULT_MAX_POSITION = {
        "BTC": 0.05,
        "JP_STOCK": 1000.0,
        "FX": 5.0,  # 5ロット = 5万通貨
    }

    def __init__(
        self,
        total_daily_risk_budget_jpy: float = 100_000.0,
        total_capital_jpy: float = 10_000_000.0,
        ipc_client: Optional[FusionEngineIPCClient] = None,
    ):
        self.total_daily_risk_budget_jpy = total_daily_risk_budget_jpy
        self.total_capital_jpy = total_capital_jpy
        self.pods: Dict[str, BaseAssetPod] = {}
        self.current_macro: Optional[MacroImpact] = None
        self.last_commands: Dict[str, ExecutionCommand] = {}
        self.history: List[Dict[str, Any]] = []
        self.ipc_client = ipc_client or FusionEngineIPCClient()

    def register_pod(self, pod: BaseAssetPod) -> None:
        """資産ポッドを司令塔に登録"""
        self.pods[pod.asset_class] = pod
        logger.info(f"[ORCHESTRATOR] 資産ポッド登録完了: {pod.asset_class}")

    def evaluate_macro_regime(self, macro: MacroImpact) -> str:
        """マクロ影響度・イベントから全体レジームを判定"""
        self.current_macro = macro

        # 1. 致命的ショック (MIS >= 85 または CRITICAL)
        if macro.impact_score >= 85 or macro.level == "CRITICAL":
            return "SHOCK"

        # 2. 警戒水準 (MIS >= 70 または WARNING)
        if macro.impact_score >= 70 or macro.level == "WARNING":
            return "RISK_OFF"

        # 3. 指定のグローバルレジーム
        if macro.global_regime in self.BUDGET_RATIOS:
            return macro.global_regime

        return "NEUTRAL"

    def determine_playbook(self, macro: MacroImpact, micro_signals: Optional[Dict[str, Any]] = None) -> str:
        """
        マクロ環境 × マイクロ構造から 5大クロスアセット・プレイブックを自動判定 (SPEC-FX-20260918-002)
        ① CRYPTO_DOMINANT: BTC高ボラ・リスクオン
        ② FX_MACRO_DOMINANT: CPI/FOMC/金利差イベント
        ③ JP_EQUITY_CATALYST: 東証適時開示/PTS急変
        ④ BALANCED_TRI_ASSET: 平常中立・分散
        ⑤ DEFENSIVE_FX_ANCHOR: リスクオフ・急落防衛
        """
        # 1. 致命的ショックまたは強いリスクオフ
        if macro.level == "CRITICAL" or macro.impact_score >= 85:
            return "DEFENSIVE_FX_ANCHOR"

        # 2. 為替・マクロ指標イベント (CPI/FOMC/日銀会合等)
        primary_lower = macro.primary_event.lower()
        if any(kw in primary_lower for kw in ["cpi", "fomc", "fed", "boj", "日銀", "雇用統計", "金利"]):
            return "FX_MACRO_DOMINANT"

        # 3. 日本株適時開示・カタリスト集中
        if any(kw in primary_lower for kw in ["tdnet", "edinet", "pts", "決算", "適時開示", "tob"]):
            return "JP_EQUITY_CATALYST"

        # 4. リスクオン・暗号資産モメンタム
        if macro.global_regime == "RISK_ON" or macro.level == "RISK_ON":
            return "CRYPTO_DOMINANT"

        if macro.level == "WARNING" or macro.impact_score >= 70:
            return "DEFENSIVE_FX_ANCHOR"

        # 5. デフォルト平常
        return "BALANCED_TRI_ASSET"

    def compute_risk_allocation(self, regime: str) -> Dict[str, float]:
        """レジームまたはプレイブックに応じた各資産クラスへのリスクバジェット (円) を動的算出"""
        ratios = self.BUDGET_RATIOS.get(regime, self.BUDGET_RATIOS["NEUTRAL"])
        allocation = {}
        for asset, ratio in ratios.items():
            if asset != "CASH":
                allocation[asset] = round(self.total_daily_risk_budget_jpy * ratio, 2)
        return allocation

    def formulate_governance_commands(
        self, macro: MacroImpact, playbook_override: Optional[str] = None
    ) -> Dict[str, ExecutionCommand]:
        """
        マクロインパクトに基づき、全ポッド宛のガバナンス命令 (ExecutionCommand) を起草
        上位命令の絶対優先（Top-Down Precedence）
        """
        playbook = playbook_override or self.determine_playbook(macro)
        regime = self.evaluate_macro_regime(macro)
        # プレイブック比率が存在する場合はプレイブック優先でバジェット配分
        budget_key = playbook if playbook in self.BUDGET_RATIOS else regime
        allocations = self.compute_risk_allocation(budget_key)
        commands: Dict[str, ExecutionCommand] = {}

        now = time.time()

        for asset_class in list(self.pods.keys()):
            allocated_risk = allocations.get(asset_class, 5000.0)
            base_max_pos = self.DEFAULT_MAX_POSITION.get(asset_class, 1.0)

            # A. 致命的ショック時: 即時STOP・新規発注遮断
            if regime == "SHOCK":
                cmd = ExecutionCommand(
                    asset_class=asset_class,
                    target_mode="STOP",
                    allocated_risk_jpy=0.0,
                    max_position_size=0.0,
                    is_halted=True,
                    reason=f"CRITICAL MACRO SHOCK: {macro.primary_event} (MIS={macro.impact_score})",
                    timestamp=now,
                )

            # B. 警戒水準時: リスク半分・建玉縮小
            elif regime == "RISK_OFF":
                # アセット固有インパクト判定
                asset_dir = macro.asset_impact_map.get(asset_class, "NEUTRAL")
                target_mode = "REDUCE_50" if asset_dir in ("BEAR", "BEAR_JPY", "SELL") else "HYBRID"
                cmd = ExecutionCommand(
                    asset_class=asset_class,
                    target_mode=target_mode,
                    allocated_risk_jpy=allocated_risk,
                    max_position_size=round(base_max_pos * 0.5, 4),
                    is_halted=False,
                    reason=f"WARNING REGIME: {macro.primary_event} (MIS={macro.impact_score})",
                    timestamp=now,
                )

            # C. リスクオン時
            elif regime == "RISK_ON":
                cmd = ExecutionCommand(
                    asset_class=asset_class,
                    target_mode="TREND",
                    allocated_risk_jpy=allocated_risk,
                    max_position_size=base_max_pos,
                    is_halted=False,
                    reason=f"RISK_ON REGIME: {macro.primary_event}",
                    timestamp=now,
                )

            # D. 平常 / 中立時
            else:
                cmd = ExecutionCommand(
                    asset_class=asset_class,
                    target_mode="HYBRID",
                    allocated_risk_jpy=allocated_risk,
                    max_position_size=base_max_pos,
                    is_halted=False,
                    reason="NORMAL REGIME",
                    timestamp=now,
                )

            commands[asset_class] = cmd

        self.last_commands = commands
        return commands

    def broadcast_commands(self, commands: Dict[str, ExecutionCommand]) -> None:
        """起草したガバナンス命令を各登録ポッドおよびGo Fusion Engineへ送信・適用"""
        for asset_class, cmd in commands.items():
            if asset_class in self.pods:
                self.pods[asset_class].apply_governance_command(cmd)
                logger.info(
                    f"[ORCHESTRATOR -> {asset_class}] 命令発令: mode={cmd.target_mode}, halted={cmd.is_halted}, risk={cmd.allocated_risk_jpy}円"
                )

        # Go Fusion Engine への Unix Domain Socket ガバナンス連携
        if self.ipc_client and self.ipc_client.is_socket_available():
            any_halted = any(cmd.is_halted or cmd.target_mode == "STOP" for cmd in commands.values())
            any_reduce = any(cmd.target_mode == "REDUCE_50" for cmd in commands.values())

            if any_halted:
                reason = next((cmd.reason for cmd in commands.values() if cmd.is_halted or cmd.target_mode == "STOP"), "STOP")
                self.ipc_client.stop(reason=reason)
            elif any_reduce:
                reason = next((cmd.reason for cmd in commands.values() if cmd.target_mode == "REDUCE_50"), "REDUCE_50")
                self.ipc_client.reduce_50(reason=reason)
            else:
                first_mode = next((cmd.target_mode for cmd in commands.values()), "HYBRID")
                self.ipc_client.resume(target_mode=first_mode, risk_multiplier=1.0, reason="GOVERNANCE NORMAL")

    def coordinate_cycle(
        self,
        macro: MacroImpact,
        market_data_by_asset: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        統合統括サイクル:
        1. マクロ解析 & ガバナンス命令発令
        2. 各ポッドのティック処理実行
        3. ポッド状態と全体リスクの集約
        """
        # 1. マクロガバナンス命令を生成＆配布
        commands = self.formulate_governance_commands(macro)
        self.broadcast_commands(commands)

        # 2. 各ポッドを駆動
        cycle_results: Dict[str, Any] = {}
        total_pnl = 0.0

        for asset_class, pod in self.pods.items():
            m_data = market_data_by_asset.get(asset_class, {})
            sig, ord_cmd, rep = pod.process_tick(m_data, macro)
            state = pod.get_state()
            total_pnl += state.daily_pnl_jpy

            cycle_results[asset_class] = {
                "signal": sig.to_dict() if sig else None,
                "order": ord_cmd.to_dict() if ord_cmd else None,
                "report": rep.to_dict() if rep else None,
                "state": state.to_dict(),
            }

        summary = {
            "timestamp": time.time(),
            "regime": self.evaluate_macro_regime(macro),
            "playbook": self.determine_playbook(macro),
            "macro_mis": macro.impact_score,
            "total_daily_pnl_jpy": round(total_pnl, 2),
            "pods": cycle_results,
        }
        self.history.append(summary)
        return summary
