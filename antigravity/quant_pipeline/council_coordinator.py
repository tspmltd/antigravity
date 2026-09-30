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
from .micro_veto_cf_audit import get_auditor as get_micro_veto_cf_auditor

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
        self._last_mid: float = 0.0

        # EventBus 購読
        self.bus.subscribe("micro_conclusion", self._on_micro_conclusion)
        self.bus.subscribe("trend_conclusion", self._on_trend_conclusion)
        self.bus.subscribe("duckdb_conclusion", self._on_duckdb_conclusion)
        self.bus.subscribe("adverse_conclusion", self._on_adverse_conclusion)
        self.bus.subscribe("orderbook_micro", self._on_orderbook_for_veto_cf)

    def _on_orderbook_for_veto_cf(self, snap: Any):
        """CSR-535: mid tick で Fake-BO CF horizons を解決（執行非接続）。"""
        try:
            mid = float(getattr(snap, "mid_price", 0) or 0)
            ts = int(getattr(snap, "timestamp", 0) or time.time() * 1000)
            if mid > 0:
                self._last_mid = mid
                get_micro_veto_cf_auditor().on_mid(mid, timestamp_ms=ts)
        except Exception:
            pass

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
        4AGENT 合議。CSR-531: candidate / decision / veto / final を分離して記録。
        OBSERVE: execution_allowed=False → final_action は hold（decision は潰さない）。
        パラメータ・重み自動適用はしない。
        """
        now_ms = int(time.time() * 1000)

        micro_c = self.latest_conclusions.get("MicrostructureAgent") or self.micro_agent.get_latest_conclusion()
        trend_c = self.latest_conclusions.get("TrendFollowAgent") or self.trend_agent.get_latest_conclusion()
        duckdb_c = self.latest_conclusions.get("DuckDBOptimizerAgent") or self.duckdb_agent.get_latest_conclusion()
        adverse_c = self.latest_conclusions.get("AdverseResearchAgent") or self.adverse_agent.get_latest_conclusion()

        directives: List[str] = []
        hard_veto = False
        emergency_cancel = False
        adverse_level = "SHADOW_OFF"  # CSR-538/546: never imply Economic SUCCESS via SAFE/0 default
        adverse_score: Optional[float] = None
        adverse_score_valid = False
        size_mult = 1.0
        veto_reason: Optional[str] = None

        if adverse_c:
            adv_on = bool(adverse_c.metrics.get("avoidance_on", False))
            raw_score = float(
                adverse_c.metrics.get("research_score", adverse_c.metrics.get("adverse_score", 0.0))
            )
            adv_side = adverse_c.metrics.get("adverse_side", "none")
            adv_lead = float(adverse_c.metrics.get("lead_ms_estimated", 0.0))
            # CSR-546: OFFの0と「観測して安全だった0」を分離
            adverse_level = "SHADOW_ON" if adv_on else "SHADOW_OFF"
            if adv_on:
                adverse_score = raw_score
                adverse_score_valid = True
            else:
                adverse_score = None
                adverse_score_valid = False
            score_disp = f"{adverse_score:.2f}" if adverse_score_valid else "NULL(SHADOW_OFF)"
            directives.append(
                f"Adverse {adverse_level} (research_score={score_disp} · NOT Economic PASS) "
                f"side={adv_side} lead_ms={adv_lead:.0f}"
            )

        duckdb_params = duckdb_c.parameters if duckdb_c else {}
        w_pressure = duckdb_params.get("W_PRESSURE", {
            "trend": 0.60, "range": 0.40, "high_vol": 0.70, "low_vol": 0.30
        })
        w_conflict = duckdb_params.get("W_CONFLICT", {
            "trend": 0.50, "range": 0.80, "high_vol": 0.60, "low_vol": 0.40
        })
        max_spread = duckdb_params.get("max_spread_jpy", 2500.0)
        # CSR-538: research reference ≠ auto_apply（表記だけ明確化・値は不変）
        directives.append(
            f"Research reference weights: W_PRESSURE={w_pressure} · "
            f"spread_cap=¥{max_spread:,.0f} · auto_apply=OFF"
        )

        # --- Trend → candidate (Direction hypothesis) ---
        regime = "range"
        t_dir = "neutral"
        t_str = 0.0
        if trend_c:
            t_dir = str(trend_c.metrics.get("trend_direction", "neutral") or "neutral")
            t_str = float(trend_c.metrics.get("trend_strength", 0.0) or 0.0)
            regime = str(trend_c.metrics.get("regime_tag", "range") or "range")
            directives.append(
                f"hypothesis_direction={t_dir.upper()} strength={t_str:.2f} regime={regime} "
                f"→ candidate mapping"
            )

        if t_dir == "up":
            candidate_action = "buy"
        elif t_dir == "down":
            candidate_action = "sell"
        else:
            candidate_action = "hold"
        candidate_confidence = round(t_str, 3)
        hypothesis_direction = t_dir  # CSR-538 audit alias (Direction ≠ final)

        # --- Micro → risk / veto ---
        p_side = "none"
        p_score = 0.0
        buy_pressure_v2 = 0.0
        sell_pressure_v2 = 0.0
        fake_bo = False
        micro_state = "NEUTRAL"
        if micro_c:
            p_side = str(micro_c.metrics.get("pressure_side", "none") or "none")
            p_score = float(micro_c.metrics.get("pressure_score", 0.0) or 0.0)
            buy_pressure_v2 = float(micro_c.metrics.get("buy_pressure_v2", 0.0) or 0.0)
            sell_pressure_v2 = float(micro_c.metrics.get("sell_pressure_v2", 0.0) or 0.0)
            fake_bo = bool(micro_c.metrics.get("fake_breakout", False))
            micro_state = str(micro_c.verdict or "NEUTRAL")
            if fake_bo or micro_c.hard_veto:
                hard_veto = True
                veto_reason = "FAKE_BREAKOUT" if fake_bo else "MICRO_HARD_VETO"
                directives.append(f"Micro VETO: {veto_reason} ➔ execution 遮断（candidateは保持）")
            else:
                directives.append(
                    f"Micro risk: {p_side.upper()} score={p_score:.2f} imb={micro_c.metrics.get('imbalance', 0):+.2f}"
                )

        # --- decision (経済判断 · Veto適用前の意図を含む) ---
        intended = candidate_action
        if t_dir == "neutral":
            if p_side == "buy":
                intended = "buy"
            elif p_side == "sell":
                intended = "sell"
            else:
                intended = "hold"

        alignment = (t_dir == p_side and t_dir in ["up", "down"])
        reg_wp = float(w_pressure.get(regime, 0.50))
        reg_wc = float(w_conflict.get(regime, 0.60))
        boost = p_score * reg_wp if alignment else -p_score * reg_wc
        calc_conf = max(0.0, min(1.0, t_str + boost))
        decision_confidence = round(calc_conf, 3)

        if emergency_cancel:
            decision_action = "cancel"
            decision_confidence = 1.0
            size_mult = 0.0
        elif calc_conf >= 0.65 and intended in ("buy", "sell"):
            decision_action = intended
            size_mult = 1.0
            directives.append(f"decision_action={decision_action.upper()} conf={decision_confidence:.2f}")
        elif calc_conf >= 0.20:
            decision_action = "hold"
            directives.append(f"decision_action=HOLD conf={decision_confidence:.2f} (<0.65)")
        else:
            # EXIT ≠ HOLD
            decision_action = "exit"
            directives.append(f"decision_action=EXIT conf={decision_confidence:.2f} (低確信度)")

        # Veto は decision を書き換えず、執行だけ止める（CF用に candidate/decision を残す）
        # CSR-558: Effective vs Redundant
        veto_eff = bool(hard_veto) and decision_action in ("buy", "sell")
        if hard_veto:
            size_mult = 0.0
            tag = "EFFECTIVE" if veto_eff else "REDUNDANT"
            directives.append(
                f"Veto CF ({tag}): candidate={candidate_action.upper()} "
                f"decision={decision_action.upper()} · "
                f"veto_effective={veto_eff}"
            )

        # decision_gate_reason (CSR-558)
        if candidate_action in ("buy", "sell") and decision_action == "hold" and calc_conf < 0.65:
            decision_gate_reason = "CONFIDENCE_GATE"
        elif candidate_action == decision_action:
            decision_gate_reason = "PASS_THROUGH"
        else:
            decision_gate_reason = "OTHER_GATE"
        if hard_veto and not veto_eff:
            decision_gate_reason = f"{decision_gate_reason}+VETO_REDUNDANT"
        elif veto_eff:
            decision_gate_reason = f"{decision_gate_reason}+VETO_EFFECTIVE"

        # OBSERVE / LIVE=OFF: 執行は常に不許可（今夜パラメータ変更なし）
        execution_allowed = False
        if emergency_cancel:
            final_action = "cancel"
            final_confidence = 1.0
        elif not execution_allowed:
            # 許可されない = final は hold（decision/exit/buy は別フィールドに残す）
            final_action = "hold"
            final_confidence = decision_confidence if not hard_veto else 0.0
            directives.append(
                "final_action=HOLD · execution_allowed=false (OBSERVE) "
                f"· 教師ラベルに使うな: candidate={candidate_action}/decision={decision_action}"
            )
        else:
            final_action = "hold" if hard_veto else decision_action
            final_confidence = 0.0 if hard_veto else decision_confidence

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
            trend_direction=t_dir,
            trend_strength=round(t_str, 3),
            trend_regime=regime,
            candidate_action=candidate_action,
            candidate_confidence=candidate_confidence,
            micro_state=micro_state,
            pressure_side=p_side,
            pressure_score=round(p_score, 3),
            veto=hard_veto,
            veto_reason=veto_reason,
            veto_effective=veto_eff,
            adverse_score=(round(adverse_score, 3) if adverse_score_valid and adverse_score is not None else None),
            adverse_score_valid=adverse_score_valid,
            decision_action=decision_action,
            decision_confidence=decision_confidence,
            decision_gate_reason=decision_gate_reason,
            execution_allowed=execution_allowed,
            execution_gate_reason=(
                "HARD_VETO" if hard_veto
                else ("OBSERVE_MODE" if not execution_allowed else "ALLOWED")
            ),
            final_action=final_action,
            final_confidence=final_confidence,
            size_multiplier=size_mult,
            active_regime=regime,
            adverse_risk_level=adverse_level,
            hard_veto_active=hard_veto,
            emergency_cancel_active=emergency_cancel,
            applied_weights={
                "W_PRESSURE": w_pressure,
                "W_CONFLICT": w_conflict,
                "max_spread_jpy": max_spread,
                "auto_apply": False,
                "label": "research_reference_weights_not_applied",
            },
            strategy_directives=directives,
            schema_version="csr564_v1",
            wire="NO",
            enforce=0,
            state_write_ts_ms=now_ms,
            freshness_max_age_sec=120.0,
            buy_pressure_v2=round(buy_pressure_v2, 4),
            sell_pressure_v2=round(sell_pressure_v2, 4),
        )

        self.latest_verdict = verdict
        self._persist_state(verdict)
        self.bus.publish("council_verdict", asdict(verdict))

        # CSR-535 P2: Fake-BO / Micro veto Saved-bp CF (OBSERVE · rising edge · no exec)
        try:
            mid_ref = 0.0
            if micro_c and isinstance(micro_c.metrics, dict):
                mid_ref = float(
                    micro_c.metrics.get("mid_price")
                    or micro_c.metrics.get("mid")
                    or 0.0
                )
            if mid_ref <= 0 and hasattr(self, "_last_mid"):
                mid_ref = float(self._last_mid or 0.0)
            get_micro_veto_cf_auditor().on_council_verdict(
                veto=bool(hard_veto),
                veto_reason=str(veto_reason or ""),
                candidate_action=candidate_action,
                decision_action=decision_action,
                mid=mid_ref,
                trend_strength=t_str,
                trend_direction=t_dir,
                pressure_score=p_score,
                imbalance=float(
                    (micro_c.metrics.get("imbalance") if micro_c else 0.0) or 0.0
                ),
                timestamp_ms=now_ms,
                veto_effective=veto_eff,
            )
        except Exception:
            pass

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
        cand = (v.candidate_action or "hold").upper()
        dec = (v.decision_action or "hold").upper()
        fin = (v.final_action or "hold").upper()
        veto_s = f"YES ({v.veto_reason})" if v.veto else "NO"
        ve = bool(getattr(v, "veto_effective", False))
        veto_tag = "EFFECTIVE" if ve else ("REDUNDANT" if v.veto else "—")
        embed = {
            "title": "🏛️ 【4AGENT 合同評議会・最新分析結論＆戦略反映レポート】",
            "description": (
                f"現在時刻: `{now_str}` · schema=`{getattr(v, 'schema_version', 'csr558_v1')}`\n"
                f"**candidate**: `{cand}` ({v.candidate_confidence:.2f}) · "
                f"**decision**: `{dec}` ({v.decision_confidence:.2f}) · "
                f"**veto**: `{veto_s}` · **veto_effective**: `{ve}` ({veto_tag})\n"
                f"**execution_allowed**: `{bool(v.execution_allowed)}` · "
                f"**final**: `{fin}` ({v.final_confidence:.2f}) · size×`{v.size_multiplier:.1f}`\n"
                f"⚠️ final=HOLD を戦略判断HOLDとして集計するな（OBSERVE時は常に final=HOLD）\n"
                f"市場レジーム: `{v.active_regime.upper()}` | Adverse(SHADOW): `{v.adverse_risk_level}` "
                f"score=`{'NULL' if not getattr(v, 'adverse_score_valid', False) else f'{v.adverse_score:.2f}'}` "
                f"valid=`{bool(getattr(v, 'adverse_score_valid', False))}`"
            ),
            "color": (
                0x2ECC71 if dec in ("BUY", "SELL") and not v.veto
                else (0xE74C3C if v.emergency_cancel_active or v.veto else 0x3498DB)
            ),
            "fields": [
                {
                    "name": "① 【マイクロ板解析】 Risk/Veto",
                    "value": (
                        f"• state: `{v.micro_state}` · pressure: `{v.pressure_side}` ({v.pressure_score:.2f})\n"
                        f"• 判定: `{v.conclusions.get('MicrostructureAgent', {}).get('verdict', 'N/A')}`\n"
                        f"• 結論: {v.conclusions.get('MicrostructureAgent', {}).get('explanation', 'N/A')}"
                    ),
                    "inline": False,
                },
                {
                    "name": "② 【トレンド】 Direction Candidate",
                    "value": (
                        f"• dir: `{v.trend_direction}` · strength: `{v.trend_strength:.2f}` · regime: `{v.trend_regime}`\n"
                        f"• 判定: `{v.conclusions.get('TrendFollowAgent', {}).get('verdict', 'N/A')}`\n"
                        f"• 結論: {v.conclusions.get('TrendFollowAgent', {}).get('explanation', 'N/A')}"
                    ),
                    "inline": False,
                },
                {
                    "name": "③ 【DuckDB】 Research Librarian",
                    "value": (
                        f"• 判定: `{v.conclusions.get('DuckDBOptimizerAgent', {}).get('verdict', 'N/A')}`\n"
                        f"• 結論: {v.conclusions.get('DuckDBOptimizerAgent', {}).get('explanation', 'N/A')}\n"
                        f"• auto_apply: `OFF`"
                    ),
                    "inline": False,
                },
                {
                    "name": "④ 【ADVERSE】 Shadow",
                    "value": (
                        f"• score: `{'NULL' if not getattr(v, 'adverse_score_valid', False) else f'{v.adverse_score:.2f}'}` "
                        f"· valid=`{bool(getattr(v, 'adverse_score_valid', False))}` "
                        f"· level: `{v.adverse_risk_level}`\n"
                        f"• 判定: `{v.conclusions.get('AdverseResearchAgent', {}).get('verdict', 'N/A')}`\n"
                        f"• 結論: {v.conclusions.get('AdverseResearchAgent', {}).get('explanation', 'N/A')}"
                    ),
                    "inline": False,
                },
                {
                    "name": "🎯 Action Layers（教師ラベル用）",
                    "value": (
                        f"• candidate_action=`{cand}` · decision_action=`{dec}` · final_action=`{fin}`\n"
                        f"• HOLD≠EXIT · DuckDB教師は candidate/decision + realized_pnl（final単独禁止）\n"
                        + ("\n".join([f"• {d}" for d in v.strategy_directives[:6]]) or "• 通常観測維持")
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "🏛️ Antigravity 4AGENT · CSR-531 Action schema · WIRE=NO"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 分析サーバーへ配信
        res1 = self.notifier._post(self.notifier.analysis_webhook_url, {"embeds": [embed]})
        # 重大アラート時はアラートサーバーにも通知
        if v.emergency_cancel_active:
            self.notifier._post(self.notifier.alert_webhook_url, {"embeds": [embed]})

        return bool(res1)
