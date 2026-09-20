"""
4AGENT Council Coordinator (4エージェント合議・戦略反映コーディネーター)
========================================================================
4つの専門エージェント:
  ① MicrostructureAgent    : 板厚・Imbalance・フェイクブレイク・瞬間圧力
  ② TrendFollowAgent       : 方向性・トレンド強度・市場レジーム
  ③ DuckDBOptimizerAgent   : Parquet過去ログ分析・勝敗要因・レジーム別最適重み/スプレッド
  ④ AdverseResearchAgent   : 逆選択ミリ秒観測・リードタイム・指値緊急退避(Cancel/Veto)

の分析結論を集約し、総合合議判定 (CouncilVerdict) を策定して、
戦略エンジン (SignalFusionEngine / SafetyGate / SyncExecutor / LiveOrderExecutor)
へ有効に反映させる司令塔。
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import asdict

from .event_bus import EventBus
from .schema import AgentConclusion, CouncilVerdict
from .agents.microstructure_agent import MicrostructureAgent
from .agents.trend_agent import TrendFollowAgent
from .agents.adverse_agent import AdverseResearchAgent
from .agents.duckdb_optimizer_agent import DuckDBOptimizerAgent
from .quant_discord_notifier import QuantDiscordNotifier

JST = timezone(timedelta(hours=9))
BASE_DIR = "/home/azureuser/antigravity"
STATE_FILE = os.path.join(BASE_DIR, "configs", "agents_council_state.json")


class FourAgentsCouncil:
    """
    4AGENT 合同評議会・意思決定＆戦略反映コーディネーター
    """

    def __init__(
        self,
        bus: EventBus,
        micro_agent: MicrostructureAgent,
        trend_agent: TrendFollowAgent,
        duckdb_agent: DuckDBOptimizerAgent,
        adverse_agent: AdverseResearchAgent,
        notifier: Optional[QuantDiscordNotifier] = None,
        state_path: str = STATE_FILE,
        discord_broadcast_interval_sec: float = 300.0,  # 5分に1回、定常総括
    ):
        self.bus = bus
        self.micro_agent = micro_agent
        self.trend_agent = trend_agent
        self.duckdb_agent = duckdb_agent
        self.adverse_agent = adverse_agent
        self.notifier = notifier or QuantDiscordNotifier()
        self.state_path = state_path
        self.discord_broadcast_interval_sec = discord_broadcast_interval_sec

        self.latest_conclusions: Dict[str, AgentConclusion] = {}
        self.latest_verdict: Optional[CouncilVerdict] = None
        self.last_discord_broadcast_ts: float = 0.0

        # EventBus 購読
        self.bus.subscribe("micro_conclusion", self._on_micro_conclusion)
        self.bus.subscribe("trend_conclusion", self._on_trend_conclusion)
        self.bus.subscribe("duckdb_conclusion", self._on_duckdb_conclusion)
        self.bus.subscribe("adverse_conclusion", self._on_adverse_conclusion)

    def _on_micro_conclusion(self, data: Dict[str, Any]):
        self._update_conclusion_dict("MicrostructureAgent", data)
        self.deliberate()

    def _on_trend_conclusion(self, data: Dict[str, Any]):
        self._update_conclusion_dict("TrendFollowAgent", data)
        self.deliberate()

    def _on_duckdb_conclusion(self, data: Dict[str, Any]):
        self._update_conclusion_dict("DuckDBOptimizerAgent", data)
        self.deliberate()

    def _on_adverse_conclusion(self, data: Dict[str, Any]):
        self._update_conclusion_dict("AdverseResearchAgent", data)
        self.deliberate()

    def _update_conclusion_dict(self, agent_name: str, data: Dict[str, Any]):
        if isinstance(data, dict):
            c = AgentConclusion(
                agent_name=data.get("agent_name", agent_name),
                timestamp=data.get("timestamp", int(time.time() * 1000)),
                verdict=data.get("verdict", "UNKNOWN"),
                confidence=float(data.get("confidence", 0.0)),
                primary_action=data.get("primary_action", "hold"),
                metrics=data.get("metrics", {}),
                parameters=data.get("parameters", {}),
                hard_veto=bool(data.get("hard_veto", False)),
                emergency_cancel=bool(data.get("emergency_cancel", False)),
                explanation=data.get("explanation", ""),
            )
            self.latest_conclusions[agent_name] = c

    def deliberate(self) -> CouncilVerdict:
        """
        4AGENT の分析結論を統合合議し、戦略ディレクティブ (CouncilVerdict) を策定
        """
        now_ms = int(time.time() * 1000)

        # 各エージェントから最新結論を取得（キャッシュまたは直接）
        micro_c = self.latest_conclusions.get("MicrostructureAgent") or self.micro_agent.get_latest_conclusion()
        trend_c = self.latest_conclusions.get("TrendFollowAgent") or self.trend_agent.get_latest_conclusion()
        duckdb_c = self.latest_conclusions.get("DuckDBOptimizerAgent") or self.duckdb_agent.get_latest_conclusion()
        adverse_c = self.latest_conclusions.get("AdverseResearchAgent") or self.adverse_agent.get_latest_conclusion()

        directives: List[str] = []
        hard_veto = False
        emergency_cancel = False
        adverse_level = "SAFE"
        final_action = "hold"
        confidence = 0.0
        size_mult = 1.0

        # -------------------------------------------------------------
        # 👑 最上位研究エージェント (Tier-0: AdverseResearchAgent): 逆選択ミリ秒防護 ＆ AE分析
        # 「勝つシグナル探索 ↓ Adverse回避 ↓ Execution改善」の最高意思決定
        # -------------------------------------------------------------
        if adverse_c:
            adv_score = float(adverse_c.metrics.get("adverse_score", 0.0))
            adv_side = adverse_c.metrics.get("adverse_side", "none")
            adv_lead = float(adverse_c.metrics.get("lead_ms_estimated", 0.0))
            adv_tier = adverse_c.metrics.get("tier", "安全")
            ae_latest = adverse_c.metrics.get("ae_latest", {})

            ae_str = ""
            if ae_latest and "ae_1s" in ae_latest:
                ae_str = f" [AE_1s:{ae_latest.get('ae_1s')}bp, AE_3s:{ae_latest.get('ae_3s')}bp]"

            # 4段階リスク制御 (0-30: 安全 / 30-60: 注意 / 60-80: 危険 / 80-100: 発注禁止)
            if adv_score >= 80.0 or adverse_c.emergency_cancel:
                emergency_cancel = True
                hard_veto = True
                adverse_level = "CRITICAL"
                size_mult = 0.0
                directives.append(
                    f"👑🔴 [最上位:発注禁止] AdverseScore: {adv_score:.1f}/100 ➔ 新規遮断＆指値緊急退避発動！{ae_str}"
                )
            elif adv_score >= 60.0 or adverse_c.hard_veto:
                hard_veto = True
                adverse_level = "WARNING"
                size_mult = 0.5  # ロット半減
                directives.append(
                    f"👑🟠 [最上位:危険] AdverseScore: {adv_score:.1f}/100 ➔ ロット半減＆逆張り見送り ({adv_side.upper()}側警戒){ae_str}"
                )
            elif adv_score >= 30.0:
                adverse_level = "CAUTION"
                size_mult = 0.8
                directives.append(
                    f"👑🟡 [最上位:注意] AdverseScore: {adv_score:.1f}/100 ➔ スプレッド厳格フィルター適用{ae_str}"
                )
            else:
                adverse_level = "SAFE"
                size_mult = 1.0
                directives.append(f"👑🟢 [最上位:安全] AdverseScore: {adv_score:.1f}/100 ➔ 逆選択リスク極小・フル稼働許可{ae_str}")

        # -------------------------------------------------------------
        # 1. 第3エージェント (DuckDB): 最適重み＆安全パラメータの取得
        # -------------------------------------------------------------
        duckdb_params = duckdb_c.parameters if duckdb_c else {}
        w_pressure = duckdb_params.get("W_PRESSURE", {
            "trend": 0.60, "range": 0.40, "high_vol": 0.70, "low_vol": 0.30
        })
        w_conflict = duckdb_params.get("W_CONFLICT", {
            "trend": 0.50, "range": 0.80, "high_vol": 0.60, "low_vol": 0.40
        })
        max_spread = duckdb_params.get("max_spread_jpy", 2500.0)
        directives.append(f"DuckDB最適重み適用: W_PRESSURE={w_pressure}, 許容スプレッド上限=¥{max_spread:,.0f}")

        # -------------------------------------------------------------
        # 2. 第2エージェント (Trend): 方向性とレジーム
        # -------------------------------------------------------------
        regime = "range"
        t_dir = "neutral"
        t_str = 0.0
        if trend_c:
            t_dir = trend_c.metrics.get("trend_direction", "neutral")
            t_str = float(trend_c.metrics.get("trend_strength", 0.0))
            regime = trend_c.metrics.get("regime_tag", "range")
            directives.append(f"トレンド判定: {t_dir.upper()} (強度:{t_str:.2f}, レジーム:{regime})")

        # -------------------------------------------------------------
        # 3. 第1エージェント (Micro): 板圧力とフェイクブレイク判定
        # -------------------------------------------------------------
        p_side = "none"
        p_score = 0.0
        fake_bo = False
        if micro_c:
            p_side = micro_c.metrics.get("pressure_side", "none")
            p_score = float(micro_c.metrics.get("pressure_score", 0.0))
            fake_bo = bool(micro_c.metrics.get("fake_breakout", False))
            if fake_bo or micro_c.hard_veto:
                hard_veto = True
                directives.append("🛡️ Micro: フェイクブレイクだまし検知 ➔ 新規エントリー遮断 (Hard Veto)")
            else:
                directives.append(f"板構造圧力: {p_side.upper()} (強度:{p_score:.2f}, Imbalance:{micro_c.metrics.get('imbalance', 0):+.2f})")

        # -------------------------------------------------------------
        # 4. 総合シグナルとアクションの策定
        # -------------------------------------------------------------
        if emergency_cancel:
            final_action = "cancel"
            confidence = 1.0
            size_mult = 0.0
        elif hard_veto:
            final_action = "hold"
            confidence = 0.0
            size_mult = 0.0
        else:
            # トレンドと板圧力の整合性による確信度計算
            intended_action = "buy" if (t_dir == "up" or (t_dir == "neutral" and p_side == "buy")) else (
                "sell" if (t_dir == "down" or (t_dir == "neutral" and p_side == "sell")) else "hold"
            )

            alignment = (t_dir == p_side and t_dir in ["up", "down"])
            base_score = t_str
            reg_wp = float(w_pressure.get(regime, 0.50))
            reg_wc = float(w_conflict.get(regime, 0.60))

            if alignment:
                boost = p_score * reg_wp
            else:
                boost = -p_score * reg_wc

            calc_conf = max(0.0, min(1.0, base_score + boost))
            confidence = round(calc_conf, 3)

            if calc_conf >= 0.65:
                final_action = intended_action
                size_mult = 1.0
                directives.append(f"✅ 戦略発注合意: {final_action.upper()} (確信度: {confidence:.2f})")
            elif calc_conf >= 0.20:
                final_action = "hold"
                directives.append(f"静観維持: HOLD (確信度不足: {confidence:.2f} < 0.65)")
            else:
                final_action = "exit"
                directives.append(f"反対圧力によるポジション手仕舞い: EXIT (確信度低: {confidence:.2f})")

        # 評議会合議判定オブジェクト
        conclusions_dict = {
            name: asdict(c) if c else {}
            for name, c in [
                ("MicrostructureAgent", micro_c),
                ("TrendFollowAgent", trend_c),
                ("DuckDBOptimizerAgent", duckdb_c),
                ("AdverseResearchAgent", adverse_c),
            ]
        }

        verdict = CouncilVerdict(
            timestamp=now_ms,
            conclusions=conclusions_dict,
            final_action=final_action,
            final_confidence=confidence,
            size_multiplier=size_mult,
            active_regime=regime,
            adverse_risk_level=adverse_level,
            hard_veto_active=hard_veto,
            emergency_cancel_active=emergency_cancel,
            applied_weights={
                "W_PRESSURE": w_pressure,
                "W_CONFLICT": w_conflict,
                "max_spread_jpy": max_spread,
            },
            strategy_directives=directives,
        )

        self.latest_verdict = verdict
        self._persist_state(verdict)
        self.bus.publish("council_verdict", asdict(verdict))

        # 定期 Discord 報告
        now_ts = time.time()
        if (now_ts - self.last_discord_broadcast_ts) >= self.discord_broadcast_interval_sec:
            self.broadcast_to_discord(verdict)
            self.last_discord_broadcast_ts = now_ts

        return verdict

    def _persist_state(self, verdict: CouncilVerdict):
        """最新合議判定をファイルへアトミック保存"""
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp_path = f"{self.state_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(asdict(verdict), f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.state_path)
        except Exception as e:
            pass

    def broadcast_to_discord(self, verdict: Optional[CouncilVerdict] = None) -> bool:
        """
        4AGENT 合議の最新結論と戦略への反映状況を Discord へ配信
        """
        v = verdict or self.latest_verdict
        if not v or not self.notifier:
            return False

        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        embed = {
            "title": "🏛️ 【4AGENT 合同評議会・最新分析結論＆戦略反映レポート】",
            "description": (
                f"現在時刻: `{now_str}`\n"
                f"合議最終アクション: **`{v.final_action.upper()}`** (確信度: `{v.final_confidence:.2f}` | ロット乗数: `{v.size_multiplier:.1f}`)\n"
                f"市場レジーム: `{v.active_regime.upper()}` | 逆選択防護レベル: `{v.adverse_risk_level}`"
            ),
            "color": 0x2ECC71 if v.final_action in ("buy", "sell") else (0xE74C3C if v.emergency_cancel_active else 0x3498DB),
            "fields": [
                {
                    "name": "① 【マイクロ板解析】 (Microstructure)",
                    "value": (
                        f"• 判定: `{v.conclusions.get('MicrostructureAgent', {}).get('verdict', 'N/A')}`\n"
                        f"• 結論: {v.conclusions.get('MicrostructureAgent', {}).get('explanation', 'N/A')}"
                    ),
                    "inline": False,
                },
                {
                    "name": "② 【トレンド追従】 (TrendFollow)",
                    "value": (
                        f"• 判定: `{v.conclusions.get('TrendFollowAgent', {}).get('verdict', 'N/A')}`\n"
                        f"• 結論: {v.conclusions.get('TrendFollowAgent', {}).get('explanation', 'N/A')}"
                    ),
                    "inline": False,
                },
                {
                    "name": "③ 【DuckDB 最適化】 (DuckDBOptimizer)",
                    "value": (
                        f"• 判定: `{v.conclusions.get('DuckDBOptimizerAgent', {}).get('verdict', 'N/A')}`\n"
                        f"• 結論: {v.conclusions.get('DuckDBOptimizerAgent', {}).get('explanation', 'N/A')}"
                    ),
                    "inline": False,
                },
                {
                    "name": "④ 🛡️ 【ADVERSE 専門防護】 (AdverseResearch)",
                    "value": (
                        f"• 判定: `{v.conclusions.get('AdverseResearchAgent', {}).get('verdict', 'N/A')}`\n"
                        f"• 結論: {v.conclusions.get('AdverseResearchAgent', {}).get('explanation', 'N/A')}"
                    ),
                    "inline": False,
                },
                {
                    "name": "🎯 【戦略への有効反映ディレクティブ】",
                    "value": "\n".join([f"• {d}" for d in v.strategy_directives[:5]]) or "• 通常観測維持",
                    "inline": False,
                },
            ],
            "footer": {"text": "🏛️ Antigravity 4AGENT Autonomous Governance Engine"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 分析サーバーへ配信
        res1 = self.notifier._post(self.notifier.analysis_webhook_url, {"embeds": [embed]})
        # 重大アラート時はアラートサーバーにも通知
        if v.emergency_cancel_active:
            self.notifier._post(self.notifier.alert_webhook_url, {"embeds": [embed]})

        return bool(res1)
