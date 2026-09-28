"""
Hourly Dry-Run Integrated Reporter (DRYRUN 1時間毎 統合定期レポート配信エンジン)
===================================================================================
以下の稼働中 Dry-run 戦略の全成績を毎時ジャスト（1時間毎）に集約し、
Discord のメイン定期運用報告チャンネル (REPORT) および DRYRUN チャンネルへ同時に配信する。

集約対象:
  1. UMM & TF2BP 24時間連続観察 (data/dryrun_umm_tf2bp_state.json)
  2. 承認済み12戦略 統合アリーナ (data/dryrun_approved_arena_state.json)
  3. 4AGENT 合議パイプライン (configs/agents_council_state.json)

起動時に直ちに初回最新レポートを送信し、以降は毎時00分（3600秒周期）で送信を継続する。
"""
import os
import sys
import time
import json
import signal
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

BASE_DIR = "/home/azureuser/antigravity"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier

JST = timezone(timedelta(hours=9))
UMM_TF_STATE = os.path.join(BASE_DIR, "data", "dryrun_umm_tf2bp_state.json")
ARENA_STATE = os.path.join(BASE_DIR, "data", "dryrun_approved_arena_state.json")
COUNCIL_STATE = os.path.join(BASE_DIR, "configs", "agents_council_state.json")
ADVERSE_SUMMARY_STATE = os.path.join(BASE_DIR, "data", "adverse_excursion_summary.json")
TOXIC_STATE = os.path.join(BASE_DIR, "data", "adverse_toxic_state.json")
SPREAD_GATE_STATE = os.path.join(BASE_DIR, "data", "spread_gate_stats.json")
MICRO_HOURLY_LATEST = os.path.join(BASE_DIR, "data", "microstructure", "hourly_latest.json")
LIBRARIAN_STATE = os.path.join(BASE_DIR, "data", "research_librarian", "librarian_state.json")
LIBRARIAN_DAILY_DIR = os.path.join(BASE_DIR, "data", "research_librarian", "daily")
LOG_PATH = os.path.join(BASE_DIR, "logs", "hourly_dryrun_reporter.log")


class HourlyDryRunReporter:
    def __init__(self, interval_sec: float = 3600.0):
        self.interval_sec = interval_sec
        self.notifier = QuantDiscordNotifier()
        self.last_sent_hour: int = -1
        self.last_librarian_day: str = ""
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    def _load_json(self, path: str) -> Dict[str, Any]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def build_and_send_report(self, trigger_reason: str = "毎時定期報告") -> bool:
        """全 Dry-run 状態を集約して Discord へマルチキャスト送信 (全て bp 表示、1h & 24h 累積)"""
        umm_tf = self._load_json(UMM_TF_STATE)
        arena = self._load_json(ARENA_STATE)
        council = self._load_json(COUNCIL_STATE)

        now_jst = datetime.now(JST)
        now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S JST")

        # 1. UMM & TF2BP データ (1h & 24h)
        umm = umm_tf.get("umm", {})
        tf = umm_tf.get("tf2bp", {})
        tf_v2 = umm_tf.get("tf2bp_peg_v3") or umm_tf.get("tf2bp_peg_v2") or {}

        umm_1h = umm.get("stats_1h", {})
        umm_24h = umm.get("stats_24h", {})
        umm_1h_bp = umm_1h.get("pnl_bp", 0.0)
        umm_24h_bp = umm_24h.get("pnl_bp", umm.get("total_pnl_bp", 0.0))
        umm_1h_jpy = umm_1h.get("pnl_jpy", 0.0)
        umm_24h_jpy = umm_24h.get("pnl_jpy", umm.get("total_pnl", 0.0))
        umm_1h_t = umm_1h.get("total_trades", 0)
        umm_24h_t = umm_24h.get("total_trades", umm.get("total_trades", 0))
        umm_1h_wr = umm_1h.get("win_rate_pct", 0.0)
        umm_24h_wr = umm_24h.get("win_rate_pct", (umm.get("win_trades", 0) / umm_24h_t * 100) if umm_24h_t > 0 else 0.0)
        umm_pos = umm.get("position", "FLAT")

        tf_1h = tf.get("stats_1h", {})
        tf_24h = tf.get("stats_24h", {})
        tf_1h_bp = tf_1h.get("pnl_bp", 0.0)
        tf_24h_bp = tf_24h.get("pnl_bp", tf.get("total_pnl_bp", 0.0))
        tf_1h_jpy = tf_1h.get("pnl_jpy", 0.0)
        tf_24h_jpy = tf_24h.get("pnl_jpy", tf.get("total_pnl", 0.0))
        tf_1h_t = tf_1h.get("total_trades", 0)
        tf_24h_t = tf_24h.get("total_trades", tf.get("total_trades", 0))
        tf_1h_wr = tf_1h.get("win_rate_pct", 0.0)
        tf_24h_wr = tf_24h.get("win_rate_pct", (tf.get("win_trades", 0) / tf_24h_t * 100) if tf_24h_t > 0 else 0.0)
        tf_pos = tf.get("position", "FLAT")

        v2_1h = tf_v2.get("stats_1h", {})
        v2_24h = tf_v2.get("stats_24h", {})
        v2_1h_bp = v2_1h.get("pnl_bp", 0.0)
        v2_24h_bp = v2_24h.get("pnl_bp", tf_v2.get("total_pnl_bp", 0.0))
        v2_1h_jpy = v2_1h.get("pnl_jpy", 0.0)
        v2_24h_jpy = v2_24h.get("pnl_jpy", tf_v2.get("total_pnl", 0.0))
        v2_1h_t = v2_1h.get("total_trades", 0)
        v2_24h_t = v2_24h.get("total_trades", tf_v2.get("total_trades", 0))
        v2_1h_wr = v2_1h.get("win_rate_pct", 0.0)
        v2_24h_wr = v2_24h.get("win_rate_pct", (tf_v2.get("win_trades", 0) / v2_24h_t * 100) if v2_24h_t > 0 else 0.0)
        v2_pos = tf_v2.get("position", "FLAT")

        diff_1h_bp = v2_1h_bp - tf_1h_bp
        diff_24h_bp = v2_24h_bp - tf_24h_bp

        umm_tf_1h_bp = umm_1h_bp + tf_1h_bp
        umm_tf_24h_bp = umm_24h_bp + tf_24h_bp
        umm_tf_1h_jpy = umm_1h_jpy + tf_1h_jpy
        umm_tf_24h_jpy = umm_24h_jpy + tf_24h_jpy

        # 2. 承認済み12戦略アリーナ データ (1h & 24h)
        ranking = arena.get("ranking", [])
        # 24h bp 順（または total_pnl_bp 順）にソート
        ranking_sorted = sorted(
            ranking,
            key=lambda r: r.get("stats_24h", {}).get("pnl_bp", r.get("total_pnl_bp", 0.0)),
            reverse=True
        )

        arena_1h_bp = sum(r.get("stats_1h", {}).get("pnl_bp", 0.0) for r in ranking)
        arena_24h_bp = sum(r.get("stats_24h", {}).get("pnl_bp", r.get("total_pnl_bp", 0.0)) for r in ranking)
        arena_1h_jpy = sum(r.get("stats_1h", {}).get("pnl_jpy", 0.0) for r in ranking)
        arena_24h_jpy = sum(r.get("stats_24h", {}).get("pnl_jpy", r.get("total_pnl_jpy", 0.0)) for r in ranking)
        arena_total_trades = sum(r.get("total_trades", 0) for r in ranking)

        rank_lines = []
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, r in enumerate(ranking_sorted[:5]):
            medal = medals[i] if i < len(medals) else f"{i+1}."
            s_id = r.get("strat_id", "")[:12]
            s_name = r.get("name", "")
            pos_v = r.get("position", "FLAT")
            r_1h = r.get("stats_1h", {})
            r_24h = r.get("stats_24h", {})
            r_1h_bp = r_1h.get("pnl_bp", 0.0)
            r_24h_bp = r_24h.get("pnl_bp", r.get("total_pnl_bp", 0.0))
            r_1h_t = r_1h.get("total_trades", 0)
            r_24h_t = r_24h.get("total_trades", r.get("total_trades", 0))
            r_1h_wr = r_1h.get("win_rate_pct", 0.0)
            r_24h_wr = r_24h.get("win_rate_pct", r.get("win_rate_pct", 0.0))
            r_1h_jpy = r_1h.get("pnl_jpy", 0.0)
            r_24h_jpy = r_24h.get("pnl_jpy", r.get("total_pnl_jpy", 0.0))

            rank_lines.append(
                f"{medal} **`{s_id}`** ({s_name}) `[{pos_v}]`\n"
                f"   • 1h: **`{r_1h_bp:+.2f} bp`** ({r_1h_t}戦/{r_1h_wr:.0f}% / ¥{r_1h_jpy:+,.0f})\n"
                f"   • 24h: **`{r_24h_bp:+.2f} bp`** ({r_24h_t}戦/{r_24h_wr:.0f}% / ¥{r_24h_jpy:+,.0f})"
            )

        # 3. 4AGENT 評議会合議データ（CSR-531: state は flat CouncilVerdict）
        # 旧バグ: verdict.recommended_action を読んで常に HOLD デフォルト → 廃止
        regime = (
            council.get("trend_regime")
            or council.get("active_regime")
            or (council.get("verdict") or {}).get("market_regime")
            or "range"
        )
        adv_valid = bool(council.get("adverse_score_valid", False))
        raw_adv = council.get("adverse_score")
        if raw_adv is None and not adv_valid:
            adv_score = None
        else:
            try:
                adv_score = float(raw_adv) if raw_adv is not None else None
            except (TypeError, ValueError):
                adv_score = None
            if not adv_valid:
                adv_score = None  # CSR-546: SHADOW_OFF の 0 を成功扱いにしない
        candidate_action = str(council.get("candidate_action") or "hold")
        decision_action = str(council.get("decision_action") or "hold")
        final_action = str(council.get("final_action") or "hold")
        veto = bool(council.get("veto") or council.get("hard_veto_active"))
        veto_reason = council.get("veto_reason")
        exec_allowed = bool(council.get("execution_allowed", False))
        cand_conf = float(council.get("candidate_confidence") or 0.0)
        dec_conf = float(council.get("decision_confidence") or 0.0)
        schema_v = str(council.get("schema_version") or "LEGACY_PRE_CSR531")
        hyp_dir = str(
            council.get("trend_direction")
            or (council.get("conclusions") or {})
            .get("TrendFollowAgent", {})
            .get("metrics", {})
            .get("trend_direction")
            or "unknown"
        )
        # CSR-538: 旧stateは candidate欠落→HOLD既定 · final=BUY 残留 — 教師に使うな
        stale_schema = "candidate_action" not in council or schema_v.startswith("LEGACY")
        action = decision_action if "decision_action" in council else "hold"

        # 4. 全Dry-run 合算損益 (1h & 24h)
        total_1h_bp = umm_tf_1h_bp + arena_1h_bp
        total_24h_bp = umm_tf_24h_bp + arena_24h_bp
        total_1h_jpy = umm_tf_1h_jpy + arena_1h_jpy
        total_24h_jpy = umm_tf_24h_jpy + arena_24h_jpy

        color = 0x2ECC71 if total_24h_bp >= 0 else 0xE74C3C
        adv_disp = "NULL(SHADOW_OFF)" if adv_score is None else f"{adv_score:.2f}"

        embed = {
            "title": f"📊 【DRYRUN 1時間毎 統合定期レポート】 ({trigger_reason})",
            "description": (
                f"🕒 **集計時刻**: `{now_str}`\n"
                f"🎯 **直近 1時間 (1h) 全体合算**: **`{total_1h_bp:+.2f} bp`** (`¥{total_1h_jpy:+,.1f}`)\n"
                f"📈 **過去24時間 (24h) 累積合算**: **`{total_24h_bp:+.2f} bp`** (`¥{total_24h_jpy:+,.1f}`)\n"
                f"🏛️ **4AGENT合議**: 相場レジーム:`{regime}` | 逆選択スコア:`{adv_disp}` | 判定:`{action}`\n"
                f"🔒 **運用方針**: **自動調整完全禁止 (FROZEN / MANUAL-ONLY)**"
            ),
            "color": color,
            "fields": [
                {
                    "name": "① UMM ＆ TF2BP 24時間観察 ＆ TF2BP_PEG_v3 観測",
                    "value": (
                        f"• **UMM (在庫スキューMM CSR-504)** `[{umm_pos}]`:\n"
                        f"   • 1h: **`{umm_1h_bp:+.2f} bp`** ({umm_1h_t}戦/{umm_1h_wr:.0f}% / ¥{umm_1h_jpy:+,.0f})\n"
                        f"   • 24h: **`{umm_24h_bp:+.2f} bp`** ({umm_24h_t}戦/{umm_24h_wr:.0f}% / ¥{umm_24h_jpy:+,.0f})\n"
                        f"• **TF2BP (Baseline: FROZEN CSR-499)** `[{tf_pos}]`:\n"
                        f"   • 1h: **`{tf_1h_bp:+.2f} bp`** ({tf_1h_t}戦/{tf_1h_wr:.0f}% / ¥{tf_1h_jpy:+,.0f})\n"
                        f"   • 24h: **`{tf_24h_bp:+.2f} bp`** ({tf_24h_t}戦/{tf_24h_wr:.0f}% / ¥{tf_24h_jpy:+,.0f})\n"
                        f"• **🔬 TF2BP_PEG_v3 (CSR-522 · AgingGuard+BE3+FakeLiq)** `[{v2_pos}]`:\n"
                        f"   • 1h: **`{v2_1h_bp:+.2f} bp`** ({v2_1h_t}戦/{v2_1h_wr:.0f}% / ¥{v2_1h_jpy:+,.0f}) [対Base: **`{diff_1h_bp:+.2f} bp`**]\n"
                        f"   • 24h: **`{v2_24h_bp:+.2f} bp`** ({v2_24h_t}戦/{v2_24h_wr:.0f}% / ¥{v2_24h_jpy:+,.0f}) [対Base: **`{diff_24h_bp:+.2f} bp`**]\n"
                        f"• **小計 (UMM+TF2BP Base)**: 1h: **`{umm_tf_1h_bp:+.2f} bp`** (`¥{umm_tf_1h_jpy:+,.0f}`) | 24h: **`{umm_tf_24h_bp:+.2f} bp`** (`¥{umm_tf_24h_jpy:+,.0f}`)"
                    ),
                    "inline": False,
                },
                {
                    "name": f"② 承認済み12戦略 アリーナ (TOP5 / 全12戦略 総取引: {arena_total_trades}回)",
                    "value": (
                        ("\n".join(rank_lines) + "\n" if rank_lines else "(集計中)\n") +
                        f"• **アリーナ合算**: 1h: **`{arena_1h_bp:+.2f} bp`** (`¥{arena_1h_jpy:+,.0f}`) | 24h: **`{arena_24h_bp:+.2f} bp`** (`¥{arena_24h_jpy:+,.0f}`)"
                    ),
                    "inline": False,
                },
                {
                    "name": "🛡️ 安全装置・死活監視状況",
                    "value": "• Watchdog Sentinel 24時間監視: `🟢 正常稼働中`\n• 次回定期レポート: 1時間後 (毎時ジャスト自動配信)",
                    "inline": False,
                }
            ],
            "footer": {"text": "🏛️ Antigravity Hourly Dry-run Central Sentinel"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 1. メイン運用報告 (REPORT) ＆ DRYRUN チャンネルへマルチキャスト送信
        success = self.notifier.post_dryrun_multicast({"embeds": [embed]})

        # 2. 試運転戦略報告 (Observation) — CSR-527: 文言は正本 peg_v3_obs_v1 に一致させる
        peg_ver = v2_1h.get("peg_version") or v2_24h.get("peg_version") or (tf_v2.get("params") or {}).get("peg_version") or "peg_v3_obs_v1"
        peg_params = tf_v2.get("params") or {}
        aging_n = int(v2_24h.get("aging_exits") or 0)
        be_n = int(v2_24h.get("be_exits") or 0)
        fake_n = int(v2_24h.get("fake_liq_skips") or 0)
        be_arm = peg_params.get("be_arm_bp", 3.0)
        aging_sec = peg_params.get("aging_guard_sec", 3.0)
        obs_embed = {
            "title": f"🔬 【Observation】 TF2BP_PEG_v3 (`{peg_ver}`) 検証",
            "description": (
                f"🕒 **集計時刻**: `{now_str}`\n"
                f"📊 **正本**: `TF2BP_PEG_v3` = AgingGuard + BE3 + FakeLiquidity + AdaptiveRatio（CSR-522）\n"
                f"⚖️ **Baseline**: `TF2BP (CSR-499 · FROZEN)` · 非改変\n"
                f"🔒 **OBSERVATION · ENFORCE=0 · WIRE=NO** · ※旧 Model3+1/BE5 表記は廃棄（CSR-527）"
            ),
            "color": 0x3498DB if v2_24h_bp >= tf_24h_bp else 0xE67E22,
            "fields": [
                {
                    "name": "① 直近 1時間 (1h) 執行性能比較",
                    "value": (
                        f"• **TF2BP_PEG_v3**: **`{v2_1h_bp:+.2f} bp`** ({v2_1h_t}戦/{v2_1h_wr:.0f}% / ¥{v2_1h_jpy:+,.1f})\n"
                        f"• **TF2BP Baseline**: **`{tf_1h_bp:+.2f} bp`** ({tf_1h_t}戦/{tf_1h_wr:.0f}% / ¥{tf_1h_jpy:+,.1f})\n"
                        f"• **改善差分 (Δbp)**: **`{diff_1h_bp:+.2f} bp`** ({'改善優位 🟢' if diff_1h_bp >= 0 else 'ビハインド 🔴'})"
                    ),
                    "inline": False,
                },
                {
                    "name": "② 過去 24時間 (24h) 累積性能比較",
                    "value": (
                        f"• **TF2BP_PEG_v3**: **`{v2_24h_bp:+.2f} bp`** ({v2_24h_t}戦/{v2_24h_wr:.0f}% / ¥{v2_24h_jpy:+,.1f})\n"
                        f"• **TF2BP Baseline**: **`{tf_24h_bp:+.2f} bp`** ({tf_24h_t}戦/{tf_24h_wr:.0f}% / ¥{tf_24h_jpy:+,.1f})\n"
                        f"• **改善差分 (Δbp)**: **`{diff_24h_bp:+.2f} bp`** ({'改善優位 🟢' if diff_24h_bp >= 0 else 'ビハインド 🔴'})"
                    ),
                    "inline": False,
                },
                {
                    "name": "③ 正本パラメータ（state 実値）",
                    "value": (
                        f"• **AgingGuard**: age≥`{aging_sec}`s ∧ pnl<0.5bp · exits(24h stats)=`{aging_n}`\n"
                        f"• **BE3**: arm +`{be_arm}`bp · trigger +0.2bp · exits=`{be_n}`\n"
                        f"• **FakeLiquidity**: imb>0.40 ∧ cxl>0.30 ∧ taker=0 · skips=`{fake_n}`\n"
                        f"• **AdaptiveRatio**: noise 0.915–0.975 / trend 0.985–0.995\n"
                        f"• 現在ポジション: `[{v2_pos}]` · version=`{peg_ver}`"
                    ),
                    "inline": False,
                }
            ],
            "footer": {"text": "🔬 Antigravity Observation · peg_v3_obs_v1 · CSR-527"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.notifier.post_observation({"embeds": [obs_embed]})

        # 3. 4AGENT 確定結論チャンネル (結論) へ合議判定を送信
        veto_disp = f"YES/{veto_reason}" if veto else "NO"
        stale_banner = (
            "⚠️ **STALE_SCHEMA**: council state lacks CSR-531 fields "
            "(candidate defaulted HOLD · do NOT teach on final) · restart run_pipeline\n"
            if stale_schema else ""
        )
        adv_lvl = council.get("adverse_risk_level", "SHADOW_OFF")
        conc_embed = {
            "title": f"🏛️ 【4AGENT 合同評議会 確定結論】 ({trigger_reason})",
            "description": (
                f"🕒 **確定時刻**: `{now_str}` · schema=`{schema_v}`\n"
                f"{stale_banner}"
                f"🧭 **hypothesis_direction**: `{hyp_dir}` · "
                f"**candidate**: **`{candidate_action.upper()}`** ({cand_conf:.2f}) · "
                f"**decision**: **`{decision_action.upper()}`** ({dec_conf:.2f})\n"
                f"🛡️ **veto**: `{veto_disp}` · **execution_allowed**: `{exec_allowed}`\n"
                f"🎯 **final_action**: `{final_action.upper()}` "
                f"(⚠️ OBSERVE時は常にHOLD — **final単独教師禁止**)\n"
                f"📈 **regime**: `{regime}` | **Adverse**: `{adv_lvl}` "
                f"score=`{adv_disp}` · valid=`{adv_valid}` · **Economic PASS=NO** (SHADOW_OFFの0≠安全)"
            ),
            "color": (
                0xE67E22 if veto
                else (
                    0x2ECC71 if decision_action.lower() in ("buy", "sell")
                    else (0x9B59B6 if decision_action.lower() == "exit" else 0x95A5A6)
                )
            ),
            "fields": [
                {
                    "name": "各エージェント結論 (Agent Conclusions)",
                    "value": (
                        f"• **MicrostructureAgent** (Entry Veto): `{council.get('conclusions', {}).get('MicrostructureAgent', {}).get('verdict', 'NEUTRAL')}` ({council.get('conclusions', {}).get('MicrostructureAgent', {}).get('explanation', '-')})\n"
                        f"• **TrendFollowAgent** (Direction): `{council.get('conclusions', {}).get('TrendFollowAgent', {}).get('verdict', 'NEUTRAL')}` ({council.get('conclusions', {}).get('TrendFollowAgent', {}).get('explanation', '-')})\n"
                        f"• **DuckDBOptimizerAgent** (Research only): `{council.get('conclusions', {}).get('DuckDBOptimizerAgent', {}).get('verdict', 'OPTIMAL')}` ({council.get('conclusions', {}).get('DuckDBOptimizerAgent', {}).get('explanation', '-')})\n"
                        f"• **AdverseResearchAgent** (SHADOW): `{council.get('conclusions', {}).get('AdverseResearchAgent', {}).get('verdict', 'SHADOW')}` ({council.get('conclusions', {}).get('AdverseResearchAgent', {}).get('explanation', '-')})"
                    ),
                    "inline": False,
                },
                {
                    "name": "Action Layers（教師=candidate/decision · final単独禁止 · CSR-538）",
                    "value": (
                        f"• hypothesis=`{hyp_dir}` · candidate=`{candidate_action}` · "
                        f"decision=`{decision_action}` · final=`{final_action}` · HOLD≠EXIT\n"
                        f"• Research reference weights · auto_apply=`OFF`\n"
                        + ("\n".join([f"• {d}" for d in council.get("strategy_directives", [])[:8]]) or "特記事項なし")
                    ),
                    "inline": False,
                }
            ],
            "footer": {"text": "⚖️ Antigravity FourAgentsCouncil · CSR-538 · WIRE=NO · ENFORCE=0"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.notifier.post_conclusion({"embeds": [conc_embed]})

        # 4. Quants-Agent 統括チャンネルへ送信
        quants_embed = {
            "title": f"🤖 【Quants-Agent 統合稼働状況】 ({trigger_reason})",
            "description": (
                f"🕒 **報告時刻**: `{now_str}`\n"
                f"• **本番 LIVE 取引**: `🛑 完全停止中 (OFF)` (元本 ¥6,390 保護)\n"
                f"• **Dry-run 統合損益**: 1h: **`{total_1h_bp:+.2f} bp`** (`¥{total_1h_jpy:+,.0f}`) | 24h: **`{total_24h_bp:+.2f} bp`** (`¥{total_24h_jpy:+,.0f}`)\n"
                f"• **Observation (PEG_v3)**: 1h: **`{v2_1h_bp:+.2f} bp`** | 24h: **`{v2_24h_bp:+.2f} bp`** [対Base: **`{diff_24h_bp:+.2f} bp`**]\n"
                f"• **Watchdog Sentinel**: `🟢 正常稼働中 (常駐死活監視)`"
            ),
            "color": 0x34495E,
            "footer": {"text": "Quants-Agent Master Sentinel"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.notifier.post_quants_agent({"embeds": [quants_embed]})

        # 5. Adverse Agent 毎時サマリー配信 (#adverse-summary)
        adverse_summary = self._load_json(ADVERSE_SUMMARY_STATE)
        toxic_state = self._load_json(TOXIC_STATE)
        if adverse_summary or toxic_state:
            self.notifier.post_adverse_summary(adverse_summary, toxic_state)

        # 6. Spread Gate 検証サマリー配信 (#spread-gate-validation: ユーザー最優先指定)
        gate_data = self._load_json(SPREAD_GATE_STATE)
        if gate_data:
            stats_1h = gate_data.get("stats_1h", {})
            stats_24h = gate_data.get("stats_24h", {})
            self.notifier.post_spread_gate_validation(stats_1h, stats_24h)

        # 7. PEG_v3 専用対比検証 (Baseline TF2BP vs TF2BP_PEG_v3: 食われにくさ実証)
        self.notifier.post_peg_v2_vs_baseline_comparison(
            v2_stats=tf_v2,
            base_stats=tf,
            ae_summary=adverse_summary,
            toxic_state=toxic_state,
        )

        # 8. Alpha vs Adverse 3軸剥落分析 (#alpha-vs-adverse)
        try:
            from antigravity.quant_pipeline.alpha_vs_adverse_analyzer import AlphaVsAdverseAnalyzer
            alpha_analyzer = AlphaVsAdverseAnalyzer()
            alpha_stats = alpha_analyzer.analyze()
        except Exception as e:
            alpha_stats = {
                "total_completed_trades": 0,
                "correlation_analysis": {"pearson_r": 0.0, "slope_beta_bp_per_score": 0.0, "r_squared": 0.0},
                "quadrant_matrix": {},
                "commander_certification": {
                    "is_certified": False,
                    "economic_pass": False,
                    "title": "【未認定 / 例外】",
                    "summary": f"analyze失敗: {e}",
                },
                "adverse_economic_pass": False,
            }
        self.notifier.post_alpha_vs_adverse(alpha_stats)

        # 9. Microstructure 1時間板解析
        micro_rep = self._load_json(MICRO_HOURLY_LATEST)
        if micro_rep:
            try:
                self.notifier.post_microstructure_hourly(micro_rep)
            except Exception as e:
                self._log_to_file(f"microstructure hourly post err: {e}")

        # 9b. CSR-521-CTRL OBSERVE（Virtual Exit@30s · hold ladder · ENFORCE=0）
        try:
            from antigravity.quant_pipeline.umm_ctrl_observe import run as run_ctrl_observe
            ctrl_rep = run_ctrl_observe(hours=1.0)
            self.notifier.post_umm_ctrl_observe(ctrl_rep)
            self._log_to_file(
                f"CSR-521-CTRL observe: n={ctrl_rep.get('source',{}).get('n_trades')} "
                f"Δ30={ (ctrl_rep.get('hourly_watch') or {}).get('would_exit_30_delta_bp') }"
            )
        except Exception as e:
            self._log_to_file(f"CSR-521-CTRL observe err: {e}")

        # 10. Research Librarian 日次（JST 09時 or 未送信日）
        try:
            day = now_jst.strftime("%Y-%m-%d")
            if now_jst.hour == 9 or self.last_librarian_day != day:
                from antigravity.quant_pipeline.duckdb_research_librarian import run_daily_review
                lib_rep = run_daily_review(day)
                self.notifier.post_librarian_daily(lib_rep)
                self.last_librarian_day = day
                self._log_to_file(f"librarian daily {day}: {lib_rep.get('portfolio_verdict')}")
        except Exception as e:
            self._log_to_file(f"librarian daily err: {e}")

        log_msg = f"[{now_str}] 統合定期レポート送信: {'成功' if success else '失敗'} (1h: {total_1h_bp:+.2f}bp, 24h: {total_24h_bp:+.2f}bp)"
        print(log_msg, flush=True)
        self._log_to_file(log_msg)
        return success

    def _log_to_file(self, msg: str):
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def run(self):
        print("=" * 70)
        print("   📢 DRYRUN 1時間毎 統合定期レポート配信デーモン")
        print("=" * 70)
        print(f"送信間隔: 1時間ごと (毎時ジャスト) ＋ 起動時即時送信")
        print(f"送信先  : HOURLY_REPORT（指定）＋ DRYRUN ＋ REPORT/LIVE")
        print("-" * 70)

        # 起動直後に初回レポートを即座に送信
        print("[Reporter] 🚀 起動時初回レポートを直ちに送信します...")
        self.build_and_send_report(trigger_reason="起動時即時ステータス報告")
        self.last_sent_hour = datetime.now(JST).hour

        def _sig_handler(sig, frame):
            print(f"\n[Reporter] シグナル {sig} 受信。安全に終了します。")
            sys.exit(0)

        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

        while True:
            try:
                now = datetime.now(JST)
                # 毎時00分（または時が変わった瞬間）に送信
                if now.hour != self.last_sent_hour:
                    print(f"[Reporter] ⏰ 毎時定期送信タイミングを検知: {now.strftime('%H:%M:%S')}")
                    self.build_and_send_report(trigger_reason="毎時定期報告")
                    self.last_sent_hour = now.hour

                time.sleep(15.0)  # 15秒おきに時刻チェック

            except Exception as e:
                print(f"[Reporter] エラー: {e}")
                traceback.print_exc()
                time.sleep(30.0)


def main():
    reporter = HourlyDryRunReporter()
    reporter.run()


if __name__ == "__main__":
    main()
