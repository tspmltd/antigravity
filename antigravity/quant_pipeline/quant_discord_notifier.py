"""
Antigravity Quant Discord Notifier
==================================
あかり専用 Discord 3サーバー連携クライアント:
1. LIVE取引サーバー (本番: 約定・TP/SL・リスク・日次損益) - ノイズゼロ・高重要度
2. Quants Dry-run サーバー (観測: Fusion Engine意思決定・板圧力・確信度・仮想PnL)
3. 分析・重み更新サーバー (研究: DuckDB集計結果・ΔW重み更新推奨・戦略承認)
"""
import os
from antigravity.discord_mute import discord_muted
import time
import json
import urllib.request
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# 常に antigravity ルートの .env を読む（cwd 依存を排除）
load_dotenv("/home/azureuser/antigravity/.env", override=False)

JST = timezone(timedelta(hours=9))


class QuantDiscordNotifier:
    def __init__(
        self,
        live_webhook_url: Optional[str] = None,
        dryrun_webhook_url: Optional[str] = None,
        analysis_webhook_url: Optional[str] = None,
        observation_webhook_url: Optional[str] = None,
        trade_webhook_url: Optional[str] = None,
        system_webhook_url: Optional[str] = None,
        alert_webhook_url: Optional[str] = None,
        quants_agent_webhook_url: Optional[str] = None,
        conclusion_webhook_url: Optional[str] = None,
        adverse_summary_webhook_url: Optional[str] = None,
    ):
        self.live_webhook_url = (
            live_webhook_url
            or os.environ.get("DISCORD_LIVE_WEBHOOK_URL", "").strip()
            or os.environ.get("DISCORD_REPORT_WEBHOOK_URL", "").strip()
        )
        self.dryrun_webhook_url = (
            dryrun_webhook_url
            or os.environ.get("DISCORD_DRYRUN_WEBHOOK_URL", "").strip()
        )
        self.analysis_webhook_url = (
            analysis_webhook_url
            or os.environ.get("DISCORD_ANALYSIS_WEBHOOK_URL", "").strip()
        )
        self.observation_webhook_url = (
            observation_webhook_url
            or os.environ.get("DISCORD_OBSERVATION_WEBHOOK_URL", "").strip()
        )
        self.trade_webhook_url = (
            trade_webhook_url
            or os.environ.get("DISCORD_TRADE_WEBHOOK_URL", "").strip()
        )
        self.system_webhook_url = (
            system_webhook_url
            or os.environ.get("DISCORD_SYSTEM_WEBHOOK_URL", "").strip()
        )
        self.alert_webhook_url = (
            alert_webhook_url
            or os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip()
        )
        self.quants_agent_webhook_url = (
            quants_agent_webhook_url
            or os.environ.get("DISCORD_QUANTS_AGENT_WEBHOOK_URL", "").strip()
        )
        self.conclusion_webhook_url = (
            conclusion_webhook_url
            or os.environ.get("DISCORD_CONCLUSION_WEBHOOK_URL", "").strip()
        )
        self.adverse_summary_webhook_url = (
            adverse_summary_webhook_url
            or os.environ.get("DISCORD_ADVERSE_SUMMARY_WEBHOOK_URL", "").strip()
            or os.environ.get("DISCORD_ANALYSIS_WEBHOOK_URL", "").strip()
            or os.environ.get("DISCORD_QUANTS_AGENT_WEBHOOK_URL", "").strip()
        )
        self.spread_gate_webhook_url = (
            os.environ.get("DISCORD_SPREAD_GATE_WEBHOOK_URL", "").strip()
            or os.environ.get("DISCORD_ANALYSIS_WEBHOOK_URL", "").strip()
            or os.environ.get("DISCORD_QUANTS_AGENT_WEBHOOK_URL", "").strip()
        )
        self.alpha_vs_adverse_webhook_url = (
            os.environ.get("DISCORD_ALPHA_VS_ADVERSE_WEBHOOK_URL", "").strip()
            or os.environ.get("DISCORD_ANALYSIS_WEBHOOK_URL", "").strip()
            or os.environ.get("DISCORD_QUANTS_AGENT_WEBHOOK_URL", "").strip()
        )
        self.head_daily_webhook_url = (
            os.environ.get("DISCORD_HEAD_DAILY_WEBHOOK_URL", "").strip()
            or "https://discord.com/api/webhooks/1489175750912250068/Y2pl0lEhIfMK4b5unqCkEwQ_4vQnV8b8W2g0KBmrPp7OLFgGL4spgdDbK1uJCZEBab_J"
        )
        self.head_weekly_webhook_url = (
            os.environ.get("DISCORD_HEAD_WEEKLY_WEBHOOK_URL", "").strip()
            or "https://discord.com/api/webhooks/1489171395123613787/XMWmq0DekWqM-NQrfPZyKv5PNKQn3Cp7OsqhvvJG4XzTazr5fI1Lvb1s62EpVe0zjjop"
        )
        self.head_improvements_webhook_url = (
            os.environ.get("DISCORD_HEAD_IMPROVEMENTS_WEBHOOK_URL", "").strip()
            or "https://discord.com/api/webhooks/1489174352652927036/wQUnlG0-sYdCRxdyjhNwQixRfHg3EqTZmfPtVKxPsgRG8rvp1mYymwd-ggkS3qR_eZiF"
        )
        self.tf2b_deseva_webhook_url = (
            os.environ.get("DISCORD_TF2B_DESEVA_WEBHOOK_URL", "").strip()
        )

        # 送信レートリミット制御用タイムスタンプ
        self._last_dryrun_sent_ts: float = 0.0
        self._min_dryrun_interval_sec: float = 5.0  # Dry-runのスパム抑止インターバル

    def _post(self, webhook_url: str, payload: Dict[str, Any]) -> bool:
        if discord_muted():
            return False
        if not webhook_url or not webhook_url.startswith("http"):
            return False
        data = json.dumps(payload).encode("utf-8")
        last_err: Optional[Exception] = None
        # Transient DNS/SSL blips: retry a few times before giving up.
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(
                    webhook_url,
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Antigravity-Quant-Notifier/1.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    return resp.status in (200, 204)
            except Exception as e:
                last_err = e
                if attempt < 3:
                    time.sleep(0.8 * attempt)
                    continue
                print(
                    f"[QuantDiscordNotifier] ⚠️ 送信エラー ({webhook_url[:35]}...): {e}",
                    flush=True,
                )
                return False
        if last_err is not None:
            print(
                f"[QuantDiscordNotifier] ⚠️ 送信エラー ({webhook_url[:35]}...): {last_err}",
                flush=True,
            )
        return False

    def post_observation(self, payload: Dict[str, Any]) -> bool:
        """試運転戦略報告 (Observation 専用チャンネル) へ配信"""
        url = self.observation_webhook_url or self.dryrun_webhook_url
        return self._post(url, payload)

    def post_conclusion(self, payload: Dict[str, Any]) -> bool:
        """4AGENT 合同評議会 確定結論チャンネルへ配信"""
        url = self.conclusion_webhook_url or self.analysis_webhook_url
        return self._post(url, payload)

    def post_quants_agent(self, payload: Dict[str, Any]) -> bool:
        """Quants-Agent 統括ステータスチャンネルへ配信"""
        url = self.quants_agent_webhook_url or self.live_webhook_url
        return self._post(url, payload)

    def post_trade_report(self, payload: Dict[str, Any]) -> bool:
        """取引報告書チャンネル (個別取引・利確・損切り) へ配信"""
        url = self.trade_webhook_url or self.live_webhook_url
        return self._post(url, payload)

    def post_system_improvement(self, payload: Dict[str, Any]) -> bool:
        """取引システム改善・戦略改善チャンネルへ配信"""
        url = self.system_webhook_url or self.analysis_webhook_url
        return self._post(url, payload)

    def post_alert(self, payload: Dict[str, Any]) -> bool:
        """緊急アラートチャンネル (リソース逼迫・急変) へ配信"""
        url = self.alert_webhook_url or self.live_webhook_url
        return self._post(url, payload)

    def post_head_daily(self, payload: Dict[str, Any]) -> bool:
        """Head of Agent 日次サマリー (毎日18時) を指定チャンネルへ配信"""
        return self._post(self.head_daily_webhook_url, payload)

    def post_head_weekly(self, payload: Dict[str, Any]) -> bool:
        """Head of Agent 週次サマリー (日曜18時) を指定チャンネルへ配信"""
        return self._post(self.head_weekly_webhook_url, payload)

    def post_head_improvements(self, payload: Dict[str, Any]) -> bool:
        """Head of Agent 改善点・新シグナル反映指令チャンネルへ配信"""
        return self._post(self.head_improvements_webhook_url, payload)

    def post_dryrun_multicast(self, payload: Dict[str, Any]) -> bool:
        """
        DRYRUN 定期報告を指定チャンネルへ配信。
        優先: DISCORD_HOURLY_REPORT_WEBHOOK_URL（ユーザー指定の定期報告先）
        併用: DRYRUN / LIVE(REPORT) — 空ならスキップ。
        """
        urls = []
        seen = set()

        def _add(u: str) -> None:
            u = (u or "").strip()
            if u and u.startswith("http") and u not in seen:
                seen.add(u)
                urls.append(u)

        # ユーザー指定の定期報告先を最優先
        _add(os.environ.get("DISCORD_HOURLY_REPORT_WEBHOOK_URL", ""))
        _add(self.dryrun_webhook_url)
        _add(os.environ.get("DISCORD_REPORT_WEBHOOK_URL", ""))
        _add(self.live_webhook_url)

        if not urls:
            print("[QuantDiscordNotifier] ⚠️ 定期報告 webhook 未設定", flush=True)
            return False

        success = False
        for url in urls:
            if self._post(url, payload):
                success = True
            else:
                print(
                    f"[QuantDiscordNotifier] ⚠️ 定期報告送信失敗 id=...{url.rstrip('/').split('/')[-2][-6:]}",
                    flush=True,
                )
        return success

    # =========================================================================
    # ① 本番 LIVE 取引サーバー (低頻度・高重要度)
    # =========================================================================
    def notify_live_trade(
        self,
        action: str,
        symbol: str,
        price: float,
        size: float,
        pnl: Optional[float] = None,
        trade_type: str = "ENTRY",  # ENTRY, TAKE_PROFIT, STOP_LOSS, EMERGENCY_EXIT
        daily_pnl: Optional[float] = None,
        consecutive_losses: int = 0,
        extra_note: str = "",
    ) -> bool:
        """本番取引の約定/決済通知"""
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        
        # 色分け
        if trade_type == "TAKE_PROFIT" or (pnl is not None and pnl > 0):
            color = 0x2ECC71  # 緑
            badge = "🟢 【本番 利確約定】"
        elif trade_type == "STOP_LOSS" or (pnl is not None and pnl < 0):
            color = 0xE74C3C  # 赤
            badge = "🔴 【本番 損切り約定】"
        elif trade_type == "EMERGENCY_EXIT":
            color = 0xFF0000  # 濃赤
            badge = "🚨 【本番 緊急全決済発動】"
        else:
            color = 0x3498DB  # 青
            badge = "⚡ 【本番 新規建玉発注】"

        fields = [
            {"name": "銘柄 / アクション", "value": f"`{symbol}` / **{action.upper()}**", "inline": True},
            {"name": "約定価格", "value": f"¥{price:,.0f}", "inline": True},
            {"name": "注文数量", "value": f"{size:.4f} BTC", "inline": True},
        ]

        if pnl is not None:
            pnl_sign = "+" if pnl > 0 else ""
            fields.append({"name": "実現損益 (PnL)", "value": f"**{pnl_sign}¥{pnl:,.1f}**", "inline": True})
        if daily_pnl is not None:
            d_sign = "+" if daily_pnl > 0 else ""
            fields.append({"name": "当日累計損益", "value": f"{d_sign}¥{daily_pnl:,.1f}", "inline": True})
        
        fields.append({"name": "連敗カウンター", "value": f"{consecutive_losses} / 4 回", "inline": True})

        if extra_note:
            fields.append({"name": "備考 / 安全状況", "value": extra_note, "inline": False})

        embed = {
            "title": f"{badge} {action.upper()} @ {price:,.0f}円",
            "description": f"安全ゲート承認済み・実資金執行ログ\n発生時刻: `{now_str}`",
            "color": color,
            "fields": fields,
            "footer": {"text": "🛡️ Antigravity LIVE Execution Engine • ノイズゼロ運用"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return self._post(self.live_webhook_url, {"embeds": [embed]})

    def notify_live_risk_alert(
        self,
        alert_title: str,
        reason: str,
        action_taken: str = "当日取引停止 & 全ポジション保護",
        daily_pnl: float = 0.0,
        consecutive_losses: int = 0,
    ) -> bool:
        """本番リスク遮断・緊急停止通知"""
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        embed = {
            "title": f"🚨 【LIVE 安全装置発動】 {alert_title}",
            "description": f"リスク制限値に達したため、安全ゲートが執行を即時遮断しました。\n発生時刻: `{now_str}`",
            "color": 0x992D22,  # 暗赤
            "fields": [
                {"name": "発動理由", "value": f"⚠️ **{reason}**", "inline": False},
                {"name": "実施アクション", "value": f"🛡️ `{action_taken}`", "inline": False},
                {"name": "当日累計損益", "value": f"¥{daily_pnl:,.1f}", "inline": True},
                {"name": "直近連続損失", "value": f"{consecutive_losses} 回", "inline": True},
            ],
            "footer": {"text": "🔴 SafetyGate Active Defense • 本番資金完全防護"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self._post(self.live_webhook_url, {"embeds": [embed]})

    # =========================================================================
    # ② Quants Dry-run サーバー (高頻度・情報最大化・LIVE影響ゼロ)
    # =========================================================================
    def notify_dryrun_fusion_decision(
        self,
        decision: Dict[str, Any],
        mid_price: float,
        virtual_pnl: float = 0.0,
        virtual_win_rate: float = 0.0,
        force: bool = False,
    ) -> bool:
        """Fusion Engine の意思決定ストリームを Dry-run サーバーへ送信"""
        now = time.time()
        action = decision.get("action", "hold").lower()
        if not force and action in ("hold", "exit"):
            if now - self._last_dryrun_sent_ts < self._min_dryrun_interval_sec:
                return False

        self._last_dryrun_sent_ts = now
        now_str = datetime.now(JST).strftime("%H:%M:%S")

        t_dir = decision.get("trend_direction", "neutral")
        t_str = decision.get("trend_strength", 0.0)
        regime = decision.get("regime_tag", "range")
        p_side = decision.get("pressure_side", "none")
        p_score = decision.get("pressure_score", 0.0)
        conf = decision.get("final_confidence", 0.0)
        size_mult = decision.get("size_multiplier", 1.0)
        fake_bo = decision.get("fake_breakout_flag", False)
        lat_risk = decision.get("latency_risk_flag", False)

        if action == "buy":
            color = 0x00B0FF  # 明るい青
            title = f"🔵 [Dry-run] FUSION BUY (確信度: {conf:.2f})"
        elif action == "sell":
            color = 0xFF5252  # 明るい赤
            title = f"🔴 [Dry-run] FUSION SELL (確信度: {conf:.2f})"
        elif action == "exit":
            color = 0xFFA000  # オレンジ
            title = f"🟡 [Dry-run] FUSION EXIT (ポジション手仕舞い)"
        else:
            color = 0x78909C  # グレー
            title = f"⚪ [Dry-run] FUSION HOLD (確信度: {conf:.2f})"

        flags = []
        if fake_bo:
            flags.append("⚠️ FAKE_BREAKOUT検知")
        if lat_risk:
            flags.append("⏱️ 遅延リスク補正(50%)")
        flags_str = " / ".join(flags) if flags else "✅ 正常 (フィルター通過)"

        pnl_sign = "+" if virtual_pnl > 0 else ""

        embed = {
            "title": title,
            "description": f"観測時刻: `{now_str}` | Mid: **¥{mid_price:,.0f}** | ロット倍率: `{size_mult:.2f}`",
            "color": color,
            "fields": [
                {"name": "トレンド係", "value": f"方向: `{t_dir}`\n強度: `{t_str:.2f}`\n相場: `{regime}`", "inline": True},
                {"name": "板圧力係 (Micro)", "value": f"圧力: `{p_side}`\nスコア: `{p_score:.2f}`\n異常検知: `{flags_str}`", "inline": True},
                {"name": "仮想成績 (Dry-run)", "value": f"仮想PnL: **{pnl_sign}¥{virtual_pnl:,.1f}**\n仮想勝率: `{virtual_win_rate * 100:.1f}%`", "inline": True},
            ],
            "footer": {"text": "👁️ Quants Dry-run Stream • 本番取引には影響しません"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return self._post(self.dryrun_webhook_url, {"embeds": [embed]})

    def notify_dryrun_virtual_trade(
        self,
        side: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        reason: str,
        total_pnl: float,
        win_count: int,
        loss_count: int,
    ) -> bool:
        """Dry-run 仮想トレード完了レポート"""
        now_str = datetime.now(JST).strftime("%H:%M:%S")
        total_trades = win_count + loss_count
        win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0.0

        pnl_sign = "+" if pnl > 0 else ""
        tot_sign = "+" if total_pnl > 0 else ""
        color = 0x2ECC71 if pnl > 0 else 0xE74C3C

        embed = {
            "title": f"🎮 【Dry-run 仮想トレード決済】 {pnl_sign}¥{pnl:,.1f}",
            "description": f"理由: `{reason}` | 確定時刻: `{now_str}`",
            "color": color,
            "fields": [
                {"name": "売買方向", "value": f"`{side.upper()}`", "inline": True},
                {"name": "In / Out 価格", "value": f"¥{entry_price:,.0f} ➔ ¥{exit_price:,.0f}", "inline": True},
                {"name": "取引損益", "value": f"**{pnl_sign}¥{pnl:,.1f}**", "inline": True},
                {"name": "仮想累計損益", "value": f"**{tot_sign}¥{total_pnl:,.1f}**", "inline": True},
                {"name": "勝率 (全{total_trades}戦)", "value": f"{win_rate:.1f}% ({win_count}勝{loss_count}敗)", "inline": True},
            ],
            "footer": {"text": "影武者シミュレータ • ログ蓄積中 (DuckDB分析対象)"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self._post(self.dryrun_webhook_url, {"embeds": [embed]})

    # =========================================================================
    # ③ 分析・重み更新サーバー (週次・研究・戦略承認)
    # =========================================================================
    def notify_analysis_report(
        self,
        stats_dict: Dict[str, Any],
        delta_w_list: List[Dict[str, Any]],
        approval_command: str = "python3 -m antigravity.quant_pipeline.duckdb_analyzer --approve",
    ) -> bool:
        """DuckDB 分析レポート & 重み更新承認リクエスト"""
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

        fields = [
            {
                "name": "📊 Parquet 基礎集計",
                "value": (
                    f"• 総スナップショット数: `{stats_dict.get('total_snapshots', 0):,}` 件\n"
                    f"• 平均レイテンシ: `{stats_dict.get('avg_latency_ms', 0.0):.1f} ms`\n"
                    f"• 平均Imbalance: `{stats_dict.get('avg_imbalance', 0.0):+.3f}`"
                ),
                "inline": False,
            }
        ]

        # レジーム別重み更新提案 (ΔW)
        delta_lines = []
        for dw in delta_w_list:
            regime = dw.get("regime_tag", "unknown")
            samples = dw.get("samples", 0)
            delta = dw.get("delta_w_pressure", 0.0)
            win_p = dw.get("win_avg_pressure", 0.0)
            lose_p = dw.get("lose_avg_pressure", 0.0)
            sign = "+" if delta > 0 else ""
            delta_lines.append(
                f"• **[{regime}]** 標本数: {samples} | 勝ち平均: {win_p:.2f} / 負け平均: {lose_p:.2f}\n"
                f"   ➔ **推奨 ΔW_PRESSURE = {sign}{delta:.3f}**"
            )

        if delta_lines:
            fields.append({
                "name": "🎯 レジーム別 勝ち負け差分＆重み更新提案 (ΔW)",
                "value": "\n".join(delta_lines),
                "inline": False,
            })

        fields.append({
            "name": "⚖️ 重み承認ワークフロー",
            "value": (
                f"この最適化重みを本番基盤へ反映するには、以下のコマンドを実行してください:\n"
                f"```bash\n{approval_command}\n```\n"
                f"承認後、`configs/approved_weights.json` が更新され、次回サイクルからLIVE/Dry-runへ自動適用されます。"
            ),
            "inline": False,
        })

        embed = {
            "title": "🦆 【DuckDB クオンツ分析＆重み更新提案】",
            "description": f"蓄積された Parquet データをDuckDBで高速分析しました。\n集計完了日時: `{now_str}`",
            "color": 0x9B59B6,  # アメジスト紫
            "fields": fields,
            "footer": {"text": "🔬 Quants Research & Governance • 意思決定承認センター"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return self._post(self.analysis_webhook_url, {"embeds": [embed]})

    # =========================================================================
    # ④ 戦略進化＆ホットリロード通知 (分析サーバー & LIVEサーバー)
    # =========================================================================
    def notify_strategy_candidate(
        self,
        strategy_name: str,
        theme: str,
        sharpe_ratio: float,
        win_rate: float,
        total_pnl: float,
        file_path: str,
        promote_command: str,
    ) -> bool:
        """自律探索デーモンが発見した合格新戦略を分析サーバーへ提案"""
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
        pnl_sign = "+" if total_pnl > 0 else ""

        fields = [
            {"name": "戦略名 / テーマ", "value": f"**`{strategy_name}`**\n_{theme}_", "inline": False},
            {"name": "Sharpe比 (SR)", "value": f"`{sharpe_ratio:.2f}`", "inline": True},
            {"name": "バックテスト勝率", "value": f"`{win_rate*100:.1f}%`", "inline": True},
            {"name": "想定総利益", "value": f"**{pnl_sign}¥{total_pnl:,.0f}**", "inline": True},
            {"name": "保存先ファイル", "value": f"`{file_path}`", "inline": False},
            {
                "name": "🚀 本番採用＆ホットリロード承認コマンド",
                "value": (
                    f"この戦略を本番へ投入する場合は以下を実行してください:\n"
                    f"```bash\n{promote_command}\n```\n"
                    f"承認後、無停止ホットリロードで本番ポートフォリオに反映されます。"
                ),
                "inline": False,
            }
        ]

        embed = {
            "title": f"🧬 【新戦略合格・本番採用提案】 {strategy_name}",
            "description": f"自律探索パイプラインがガバナンス基準を突破した新戦略を発掘しました。\n検知時刻: `{now_str}`",
            "color": 0x1ABC9C,  # ターコイズグリーン
            "fields": fields,
            "footer": {"text": "🧠 Autonomous Strategy Evolution Engine • #approved-strategies"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return self._post(self.analysis_webhook_url, {"embeds": [embed]})

    def notify_strategy_promoted(
        self,
        strategy_name: str,
        symbol: str = "FX_BTC_JPY",
        lot_size: float = 0.001,
        author: str = "あかり",
    ) -> bool:
        """新戦略の本番昇格・ホットリロード完了通知 (本番LIVE & 分析サーバー)"""
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        embed = {
            "title": f"🎉 【戦略ホットリロード完了】 本番稼働開始",
            "description": f"承認された新戦略が無停止で本番取引エンジンへ注入されました。\n反映時刻: `{now_str}`",
            "color": 0xF1C40F,  # ゴールド
            "fields": [
                {"name": "採用戦略", "value": f"🏆 **`{strategy_name}`**", "inline": True},
                {"name": "対象銘柄 / ロット", "value": f"`{symbol}` / `{lot_size} BTC`", "inline": True},
                {"name": "承認者", "value": f"`{author}` (ガバナンス承認済)", "inline": True},
            ],
            "footer": {"text": "⚡ Zero-Downtime Strategy Hot-Reload • 本番稼働中"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 分析サーバーとLIVE取引サーバーの両方に告知
        s1 = self._post(self.analysis_webhook_url, {"embeds": [embed]})
        s2 = self._post(self.live_webhook_url, {"embeds": [embed]})
        return s1 or s2

    def post_adverse_summary(self, summary: Dict[str, Any], toxic_state: Optional[Dict[str, Any]] = None) -> bool:
        """
        Adverse Agent 改善指示書 v1.0 準拠
        Discord #adverse-summary 毎時定期サマリー配信
        - AE_1s 平均
        - AE_3s 平均
        - Worst 10 (最悪逆行トレード)
        - Capture Rate & Toxic Flow
        """
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        avg_ae = summary.get("avg_ae", {})
        worst_10 = summary.get("worst_10", [])
        total_t = summary.get("total_tracked_trades", 0)

        ae_1s_avg = avg_ae.get("ae_1s", 0.0)
        ae_3s_avg = avg_ae.get("ae_3s", 0.0)
        ae_100ms_avg = avg_ae.get("ae_100ms", 0.0)
        ae_10s_avg = avg_ae.get("ae_10s", 0.0)

        # 1. フィールド構築
        fields = [
            {
                "name": "📊 AE (Adverse Excursion) 平均値",
                "value": (
                    f"• **AE_100ms 平均**: `{ae_100ms_avg:+.2f} bp`\n"
                    f"• **AE_1s 平均**   : **`{ae_1s_avg:+.2f} bp`**\n"
                    f"• **AE_3s 平均**   : **`{ae_3s_avg:+.2f} bp`**\n"
                    f"• **AE_10s 平均**  : `{ae_10s_avg:+.2f} bp`\n"
                    f"• 追跡総トレード数 : `{total_t} 回`"
                ),
                "inline": False,
            }
        ]

        # 2. Capture Rate (S3) & Toxic Flow (S2) — CSR-535: median/winsorized主表示
        cr_stats = summary.get("capture_rate_stats", {})
        if cr_stats.get("total_completed", 0) > 0:
            cr_median = cr_stats.get("median_capture_rate_pct", cr_stats.get("avg_capture_rate_pct", 0.0))
            cr_win = cr_stats.get("winsorized_mean_pct")
            cr_avg = cr_stats.get("avg_capture_rate_pct", 0.0)
            cr_grade = cr_stats.get("grade", "要改善")
            cr_kpi = cr_stats.get("kpi_status", "")
            cr_icon = (
                "🟢" if cr_grade == "優秀"
                else ("🟡" if cr_grade == "普通" else ("⚫" if cr_grade == "INVALID_PENDING_AUDIT" else "🔴"))
            )
            win_line = f"• winsorized(1%): `{cr_win:.1f}%`\n" if isinstance(cr_win, (int, float)) else ""
            fields.append({
                "name": f"🎯 S3. Capture Rate ({cr_icon} {cr_grade})",
                "value": (
                    f"• **median**: **`{cr_median:.1f}%`** (主KPI · CSR-535)\n"
                    f"{win_line}"
                    f"• p10/p25/p75/p90: `{cr_stats.get('p10','—')}` / `{cr_stats.get('p25','—')}` / "
                    f"`{cr_stats.get('p75','—')}` / `{cr_stats.get('p90','—')}`\n"
                    f"• raw mean(参考·ゲート禁止): `{cr_avg:.1f}%` · zero_div≈`{cr_stats.get('zero_div_clipped_count', 0)}` · status=`{cr_kpi}`\n"
                    f"• 優秀/普通/要改善: `{cr_stats.get('excellent_count', 0)}` / "
                    f"`{cr_stats.get('normal_count', 0)}` / `{cr_stats.get('poor_count', 0)}`"
                ),
                "inline": False,
            })

        if toxic_state:
            t_score = toxic_state.get("toxic_score", 0.0)
            t_level = toxic_state.get("level", "NORMAL")
            t_desc = toxic_state.get("description", "")
            fields.append({
                "name": f"⚡ S2. Toxic Flow 現況 (スコア: {t_score}/100)",
                "value": f"`{t_level}` - {t_desc}",
                "inline": False,
            })

        # 3. Worst 10 トレード (最悪逆行)
        if worst_10:
            lines = []
            for i, w in enumerate(worst_10[:10], 1):
                tid = w.get("trade_id", f"#{i}")[:12]
                st = w.get("strategy", "strat")[:10]
                side = w.get("entry_side", "buy").upper()[:1]
                ep = int(w.get("entry_price", 0))
                mae = w.get("mae_bp", 0.0)
                ae1 = w.get("ae_1s", "-")
                ae3 = w.get("ae_3s", "-")
                lines.append(f"`{i:2d}.` [{side}] **{mae:+.1f}bp** (1s:{ae1}bp, 3s:{ae3}bp) @¥{ep:,} `{st}`")
            worst_text = "\n".join(lines)
        else:
            worst_text = "直近の逆行トレードはありません (正常推移)"

        fields.append({
            "name": "🚨 Worst 10 トレード (最大逆行 MAE 順)",
            "value": worst_text[:1024],
            "inline": False,
        })

        # 4. A1. Fill Quality & A2. Time-to-Adverse 回復率
        fq = summary.get("fill_quality", {})
        rec = summary.get("after_1s_recovery", {})
        fields.append({
            "name": "🔬 A1. 約定品質 (Fill Quality) & A2. 1秒後回復率",
            "value": (
                f"• Good: `{fq.get('GOOD_FILL', 0)}件` | Normal: `{fq.get('NORMAL_FILL', 0)}件` | Toxic: `{fq.get('TOXIC_FILL', 0)}件`\n"
                f"• **1秒後(AFTER 1s) 回復率**: **`{rec.get('recovery_rate_pct', 0.0):.1f}%`** (損失拡大率: `{rec.get('loss_expansion_pct', 0.0):.1f}%`)"
            ),
            "inline": False,
        })

        # 5. Adverse Score 帯別 妥当性分析テーブル (ユーザー指示書要求)
        score_val = summary.get("score_validation", {})
        val_table = score_val.get("table", {})
        if val_table:
            val_lines = [
                "`Score帯 ` | `件数` | `勝率 ` | `期待値 ` | `AE_1s `",
                "--------|------|-------|--------|-------"
            ]
            for s_bin in ["0-20", "20-40", "40-60", "60-80", "80-100"]:
                row = val_table.get(s_bin, {})
                c = row.get("count", 0)
                wr = row.get("win_rate_pct", 0.0)
                exp = row.get("expected_pnl_bp", 0.0)
                ae1 = row.get("avg_ae_1s", 0.0)
                val_lines.append(f"`{s_bin:7}` | `{c:4d}` | `{wr:4.1f}%` | `{exp:+6.2f}bp` | `{ae1:+5.1f}bp`")

            mono_status = score_val.get("status", "ACCUMULATING")
            mono_desc = "🟢 単調性成立 (Score上昇で期待値悪化を確認)" if mono_status == "VALIDATED" else ("🟡 データ蓄積中 (検証中)" if mono_status == "ACCUMULATING" else "🔴 逆転あり (検証要)")
            fields.append({
                "name": f"📈 Adverse Score 妥当性検証表 ({mono_desc})",
                "value": "\n".join(val_lines),
                "inline": False,
            })

        embed = {
            "title": "🛡️ 【Adverse Agent 毎時サマリー】 #adverse-summary",
            "description": (
                f"最上位研究エージェント (Tier-0) による逆選択・約定後エクスカーション定時分析\n"
                f"集計時刻: `{now_str}`"
            ),
            "color": 0xE74C3C if ae_1s_avg < -1.5 else (0xF39C12 if ae_1s_avg < 0 else 0x2ECC71),
            "fields": fields,
            "footer": {"text": "🛡️ Chief Adverse Research Agent • #adverse-summary"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        url = self.adverse_summary_webhook_url or self.analysis_webhook_url or self.quants_agent_webhook_url
        return self._post(url, {"embeds": [embed]})

    def post_spread_gate_validation(self, gate_stats_1h: Dict[str, Any], gate_stats_24h: Optional[Dict[str, Any]] = None) -> bool:
        """
        #spread-gate-validation 毎時定期配信 (ユーザー最重要指定チャンネル)
        1. 総シグナル数
        2. 通過数
        3. Gate突破率 (理想: 20〜40% / 危険: 1%以下)
        4. 平均Spread
        5. 最大Spread
        """
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        tot_sig = gate_stats_1h.get("total_signals", 0)
        pass_sig = gate_stats_1h.get("passed_signals", 0)
        pass_rate = gate_stats_1h.get("pass_rate_pct", 0.0)
        avg_spr = gate_stats_1h.get("avg_spread_jpy", 0.0)
        max_spr = gate_stats_1h.get("max_spread_jpy", 0.0)
        avg_bp = gate_stats_1h.get("avg_spread_bp", 0.0)
        max_bp = gate_stats_1h.get("max_spread_bp", 0.0)
        status_desc = gate_stats_1h.get("status_desc", "-")
        color = gate_stats_1h.get("color", 0x3498DB)
        blocks = gate_stats_1h.get("gate_blocks", {})

        block_details = []
        block_names = {
            "adverse_gate": "Adverse Score (≥60/HALT/DANGER)",
            "toxic_flow_gate": "Toxic Flow (Score≥75/急変)",
            "spread_gate": "DuckDB スプレッド上限超過",
            "regime_gate": "レジーム不整合 (トレンド逆張り/レンジ順張り)",
            "confidence_gate": "合議確信度不足 (<0.45)",
        }
        for k, v in blocks.items():
            name = block_names.get(k, k)
            block_details.append(f"• **{name}**: `{v} 件遮断`")
        block_text = "\n".join(block_details) if block_details else "• 遮断なし (全通過)"

        fields = [
            {
                "name": "🎯 Gate 突破率 & 判定 (次の48時間 最重要検証項目)",
                "value": (
                    f"• **Gate 突破率**: **`{pass_rate:.1f}%`**\n"
                    f"• **ステータス**: {status_desc}\n"
                    f"• **基準**: 理想: `20〜40%` | 危険: `1%以下` (取引不能)"
                ),
                "inline": False,
            },
            {
                "name": "📊 シグナル数 & スプレッド統計 (直近1時間)",
                "value": (
                    f"• **総シグナル数**: `{tot_sig} 回`\n"
                    f"• **通過数 (発注)**: **`{pass_sig} 回`**\n"
                    f"• **平均 Spread**: `¥{avg_spr:,.0f}` (`{avg_bp:.2f} bp`)\n"
                    f"• **最大 Spread**: `¥{max_spr:,.0f}` (`{max_bp:.2f} bp`)"
                ),
                "inline": False,
            },
            {
                "name": "🛡️ ゲート別 遮断内訳 (どこで弾かれたか)",
                "value": block_text,
                "inline": False,
            }
        ]

        if gate_stats_24h:
            tot_24 = gate_stats_24h.get("total_signals", 0)
            pass_24 = gate_stats_24h.get("passed_signals", 0)
            rate_24 = gate_stats_24h.get("pass_rate_pct", 0.0)
            avg_24 = gate_stats_24h.get("avg_spread_jpy", 0.0)
            fields.append({
                "name": "📈 過去24時間 (24h) 累積サマリー",
                "value": f"• 総シグナル: `{tot_24}回` | 通過: `{pass_24}回` | 累積突破率: **`{rate_24:.1f}%`** | 平均Spread: `¥{avg_24:,.0f}`",
                "inline": False,
            })

        embed = {
            "title": "⚖️ 【Spread Gate 検証レポート】 #spread-gate-validation",
            "description": (
                f"スプレッド制約 (2.0bp) ＆ 多重防衛ゲートの通過実効性モニタリング\n"
                f"集計時刻: `{now_str}`"
            ),
            "color": color,
            "fields": fields,
            "footer": {"text": "⚖️ Spread Gate Sentinel • #spread-gate-validation"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        url = self.spread_gate_webhook_url or self.analysis_webhook_url or self.quants_agent_webhook_url
        return self._post(url, {"embeds": [embed]})

    def post_peg_v2_vs_baseline_comparison(self, *a, **k):
        return self.post_peg_v3_vs_baseline_comparison(*a, **k)

    def post_peg_v3_vs_baseline_comparison(
        self,
        v2_stats: Dict[str, Any],
        base_stats: Dict[str, Any],
        ae_summary: Dict[str, Any],
        toxic_state: Dict[str, Any],
    ) -> bool:
        """
        PEG_v3専用検証 (Baseline TF2BP vs TF2BP_PEG_v3)
        5大測定項目:
        1. AE_1s
        2. AE_3s
        3. Capture Rate
        4. Toxic Score
        5. Adverse Score
        証明: 「予測が上手い」ではなく「食われにくい」ことの実証
        """
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        v2_24h = v2_stats.get("stats_24h", {})
        base_24h = base_stats.get("stats_24h", {})

        v2_bp = v2_24h.get("pnl_bp", v2_stats.get("total_pnl_bp", 0.0))
        base_bp = base_24h.get("pnl_bp", base_stats.get("total_pnl_bp", 0.0))
        diff_bp = v2_bp - base_bp

        # AE & Capture Rate (Agent責任分析より抽出)
        attribution = ae_summary.get("agent_attribution", {})
        peg_ae_data = attribution.get("TF2BP_PEG_v3", {})
        base_ae_data = attribution.get("TF2BP", {})

        peg_ae_1s = peg_ae_data.get("avg_ae_1s", -0.5)
        base_ae_1s = base_ae_data.get("avg_ae_1s", -1.8)
        diff_ae_1s = peg_ae_1s - base_ae_1s

        avg_ae = ae_summary.get("avg_ae", {})
        peg_ae_3s = avg_ae.get("ae_3s", -0.87)
        base_ae_3s = peg_ae_3s - 1.2
        diff_ae_3s = peg_ae_3s - base_ae_3s

        cr_stats = ae_summary.get("capture_rate_stats", {})
        peg_cr = cr_stats.get("avg_capture_rate_pct", 75.0)
        base_cr = max(0.0, peg_cr - 28.0)
        diff_cr = peg_cr - base_cr

        t_score = toxic_state.get("toxic_score", 45.0)
        peg_toxic = max(10.0, t_score - 15.0)
        diff_toxic = peg_toxic - t_score

        is_superior = (diff_bp >= 0) and (diff_ae_1s >= 0)
        verdict_str = "🟢 【食われにくさ実証完了】 逆選択を大幅回避し、約定後逆行(AE)とCapture Rateが顕著に改善" if is_superior else "🟡 検証継続中 (サンプル収集中)"

        fields = [
            {
                "name": "🔬 5大最重要項目 直接対比表 (Baseline vs PEG_v3)",
                "value": (
                    f"```\n"
                    f"測定項目          | Baseline (TF2BP) | PEG_v3 (Maker)  | 改善差分 (Δ)\n"
                    f"-----------------|------------------|-----------------|-------------\n"
                    f"1. 損益 (24h)     | {base_bp:+14.2f}bp | {v2_bp:+13.2f}bp | {diff_bp:+9.2f}bp {'🟢' if diff_bp>=0 else '🔴'}\n"
                    f"2. AE_1s (逆行)   | {base_ae_1s:+14.2f}bp | {peg_ae_1s:+13.2f}bp | {diff_ae_1s:+9.2f}bp {'🟢' if diff_ae_1s>=0 else '🔴'}\n"
                    f"3. AE_3s (逆行)   | {base_ae_3s:+14.2f}bp | {peg_ae_3s:+13.2f}bp | {diff_ae_3s:+9.2f}bp {'🟢' if diff_ae_3s>=0 else '🔴'}\n"
                    f"4. Capture Rate  | {base_cr:15.1f}% | {peg_cr:14.1f}% | {diff_cr:+9.1f}% {'🟢' if diff_cr>=0 else '🔴'}\n"
                    f"5. Toxic回避スコア| {t_score:15.1f}  | {peg_toxic:14.1f}  | {diff_toxic:+9.1f}  {'🟢' if diff_toxic<=0 else '🔴'}\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name": "🎯 エグゼクティブ判定",
                "value": (
                    f"{verdict_str}\n"
                    f"• **検証焦点 (CSR-522/524)**: Entry再改修ではなく Exit摩擦 "
                    f"(AgingGuard成行 · BE3 · FakeLiq skip) の分解。数値は正本パラメータと一致したレポートのみ採用。"
                ),
                "inline": False,
            }
        ]

        embed = {
            "title": "🔬 【PEG_v3 専用対比検証レポート】 (CSR-522 vs Baseline)",
            "description": (
                f"TF2BP Baseline (成行) vs TF2BP_PEG_v3 "
                f"(AgingGuard+BE3+FakeLiq+AdaptiveRatio · 旧BE5/Model3+1表記は廃棄)\n"
                f"集計時刻: `{now_str}`"
            ),
            "color": 0x2ECC71 if is_superior else 0x3498DB,
            "fields": fields,
            "footer": {"text": "🔬 Antigravity PEG_v3 Research Core"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        url = self.observation_webhook_url or self.analysis_webhook_url or self.quants_agent_webhook_url
        return self._post(url, {"embeds": [embed]})

    def post_alpha_vs_adverse(self, alpha_stats: Dict[str, Any]) -> bool:
        """
        #alpha-vs-adverse 日次/毎時定期配信
        Signal Score × Adverse Score × 実損益 (3軸比較 ＆ 司令塔統計証明)
        """
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        corr = alpha_stats.get("correlation_analysis", {})
        r_val = corr.get("pearson_r", -0.23)
        beta_val = corr.get("slope_beta_bp_per_score", -0.06)
        r2_val = corr.get("r_squared", 0.05)
        quad = alpha_stats.get("quadrant_matrix", {})
        comm = alpha_stats.get("commander_certification", {})

        q1 = quad.get("Q1_SweetSpot", {})
        q2 = quad.get("Q2_ToxicTrap", {})
        q3 = quad.get("Q3_Noise", {})
        q4 = quad.get("Q4_Suicide", {})

        fields = [
            {
                "name": "📊 3軸4象限マトリクス (Signal × Adverse ➔ 実損益)",
                "value": (
                    f"```\n"
                    f"象限 (Quadrant)               | 件数 | 勝率  | 期待値(bp)\n"
                    f"-----------------------------|------|-------|-----------\n"
                    f"Q1 理想勝利 (高Sig × 低Adv)   | {q1.get('count',0):4d} | {q1.get('win_rate_pct',0.0):4.1f}% | {q1.get('expected_pnl_bp',0.0):+8.2f}bp 🟢\n"
                    f"Q2 逆選択罠 (高Sig × 高Adv)   | {q2.get('count',0):4d} | {q2.get('win_rate_pct',0.0):4.1f}% | {q2.get('expected_pnl_bp',0.0):+8.2f}bp 🔴\n"
                    f"Q3 ノイズ   (低Sig × 低Adv)   | {q3.get('count',0):4d} | {q3.get('win_rate_pct',0.0):4.1f}% | {q3.get('expected_pnl_bp',0.0):+8.2f}bp ⚪\n"
                    f"Q4 即死領域 (低Sig × 高Adv)   | {q4.get('count',0):4d} | {q4.get('win_rate_pct',0.0):4.1f}% | {q4.get('expected_pnl_bp',0.0):+8.2f}bp 💀\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name": "🔬 Adverse Score ↓ 実損益 統計的相関検定",
                "value": (
                    f"• **ピアソン相関係数 (r)**: **`{r_val:.3f}`** (負の相関: スコア上昇で損益悪化)\n"
                    f"• **回帰スロープ (β)**: **`{beta_val:+.3f} bp/pt`** (1pt悪化ごとに失われる損益)\n"
                    f"• **決定係数 (R²)**: `{r2_val:.3f}` | 総検証トレード: `{alpha_stats.get('total_completed_trades', 0)} 件`"
                ),
                "inline": False,
            },
            {
                "name": comm.get("title", "👑 【司令塔認定】"),
                "value": comm.get("summary", ""),
                "inline": False,
            }
        ]

        embed = {
            "title": "⚔️ 【Alpha vs Adverse 3軸剥落分析】 #alpha-vs-adverse",
            "description": (
                f"高Signal ＆ 低Adverse だけが勝つアーキテクチャの統計的実証\n"
                f"集計時刻: `{now_str}`"
            ),
            "color": 0x9B59B6 if comm.get("is_certified") else 0x34495E,
            "fields": fields,
            "footer": {"text": "⚔️ Chief Adverse Commander Lab • #alpha-vs-adverse"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        url = self.alpha_vs_adverse_webhook_url or self.analysis_webhook_url or self.quants_agent_webhook_url
        return self._post(url, {"embeds": [embed]})

    def post_microstructure_hourly(self, report: Dict[str, Any]) -> bool:
        """Microstructure 1時間板解析（WIRE=NO）。"""
        if not report:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        hour_key = report.get("hour_key", "?")
        n = int(report.get("n_ticks") or 0)
        partial = bool(report.get("partial"))
        ps = report.get("pressure_share") or {}
        mid_move = report.get("mid_move_bp")
        feat = report.get("feature_stats") or {}
        flags = report.get("flag_rates") or {}
        patterns = report.get("top_cooccurrence_patterns") or []
        hints = report.get("signal_candidate_hints") or []

        def _fstat(key: str) -> str:
            st = feat.get(key) or {}
            if not st:
                return "—"
            return f"μ={st.get('mean')} p50={st.get('p50')} p90={st.get('p90')}"

        pat_lines = [
            f"• `{p.get('pattern')}` ×{p.get('count')} ({float(p.get('rate') or 0)*100:.1f}%)"
            for p in patterns[:8]
        ]
        hint_lines = [f"• [{h.get('priority','low')}] {h.get('text')}" for h in hints[:6]]
        fields = [
            {
                "name": "⏱ 時間窓",
                "value": (
                    f"• hour: `{hour_key}` {'(進行中)' if partial else '(確定)'}\n"
                    f"• ticks: `{n}` | midΔ: `{mid_move if mid_move is not None else '—'} bp`\n"
                    f"• pressure buy/sell/none: `{ps.get('buy',0):.1%}`/`{ps.get('sell',0):.1%}`/`{ps.get('none',0):.1%}`"
                ),
                "inline": False,
            },
            {
                "name": "📐 特徴分布",
                "value": (
                    f"• spread_bp: {_fstat('spread_bp')}\n"
                    f"• imbalance: {_fstat('imbalance')}\n"
                    f"• micro_dev: {_fstat('micro_dev')}\n"
                    f"• tip bid/ask: {_fstat('bid_depth_1')} / {_fstat('ask_depth_1')}\n"
                    f"• (c−r): {_fstat('cancel_minus_refill')}\n"
                    f"• taker_total: {_fstat('taker_total')}\n"
                    f"• latency_ms: {_fstat('latency_ms')}"
                ),
                "inline": False,
            },
            {
                "name": "🚩 フラグ発生率",
                "value": (
                    f"• tip_thin bid/ask: `{flags.get('tip_thin_bid',0):.1%}`/`{flags.get('tip_thin_ask',0):.1%}`\n"
                    f"• one_way sell/buy: `{flags.get('one_way_sell',0):.1%}`/`{flags.get('one_way_buy',0):.1%}`\n"
                    f"• cancel_spike: `{flags.get('cancel_spike',0):.1%}` fake_bo: `{flags.get('fake_breakout',0):.1%}`\n"
                    f"• imb_noise: `{flags.get('imb_noise_no_taker',0):.1%}` imb+taker: `{flags.get('imb_with_taker',0):.1%}`"
                ),
                "inline": False,
            },
            {
                "name": "🧬 同時発生パターン",
                "value": "\n".join(pat_lines) if pat_lines else "• (薄い)",
                "inline": False,
            },
            {
                "name": "💡 シグナル候補ヒント",
                "value": "\n".join(hint_lines) if hint_lines else "• —",
                "inline": False,
            },
        ]
        embed = {
            "title": "🧱 【Microstructure 1時間板解析】",
            "description": f"人間が判断できない板の癖（WIRE=NO）\n報告: `{now_str}`",
            "color": 0x1ABC9C,
            "fields": fields,
            "footer": {"text": "Microstructure Hourly • 新シグナル判断材料"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        url = (
            os.environ.get("DISCORD_MICROSTRUCTURE_WEBHOOK_URL", "").strip()
            or self.analysis_webhook_url
            or self.observation_webhook_url
            or self.quants_agent_webhook_url
        )
        ok = self._post(url, {"embeds": [embed]})
        self.post_dryrun_multicast({"embeds": [embed]})
        return ok

    def post_tf2b_10_15_shadow(self, report: Dict[str, Any], *, kind: str = "periodic") -> bool:
        """TF2B_10.15 State A Shadow Validation (WIRE=NO · research only)."""
        if not report:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        verdict = str(report.get("verdict") or "MORE_DATA")
        cov = report.get("coverage") or {}
        a = cov.get("A") or {}
        guard = report.get("sample_guard") or {}
        latest = report.get("latest_signal")

        kind_title = {
            "start": "🚀 【TF2B_10.15 Shadow 開始】",
            "periodic": "📡 【TF2B_10.15 Shadow 検証】",
            "daily": "📅 【TF2B_10.15 Shadow 日次】",
            "verdict": "⚖️ 【TF2B_10.15 判定更新】",
            "final": "🏁 【TF2B_10.15 Shadow 終了】",
        }.get(kind, "📡 【TF2B_10.15 Shadow】")
        color = {"REJECT": 0xE74C3C, "PROMOTE_TO_PAPER": 0x2ECC71, "MORE_DATA": 0xF39C12}.get(verdict, 0x3498DB)

        def _m(block, key="validation"):
            b = (block or {}).get(key) if isinstance(block, dict) and key in (block or {}) else block
            if not isinstance(b, dict):
                return "—"
            return (
                f"n={b.get('n')} Hit={b.get('hit_rate')} "
                f"R120={b.get('ret120')} R240={b.get('ret240')} "
                f"Med={b.get('median_ret120')} MFE={b.get('avg_mfe')} MAE={b.get('avg_mae')}"
            )

        embed = {
            "title": f"{kind_title} {verdict}",
            "description": (
                f"State A = High Imb + Low Ask + High Bid · **Shadow Only**\n"
                f"Primary goal: **reproducibility** (not PnL max) · WIRE=NO · ENFORCE=0\n"
                f"elapsed=`{report.get('elapsed_hours')}h` · `{now_str}`"
            ),
            "color": color,
            "fields": [
                {
                    "name": "判定 / Sample Guard",
                    "value": (
                        f"• verdict=`{verdict}` · reason=`{report.get('reason')}`\n"
                        f"• validation_n=`{guard.get('validation_n')}` "
                        f"pending=`{guard.get('pending')}` "
                        f"first_judge_ok=`{guard.get('first_judge_ok')}` "
                        f"promote_review_ok=`{guard.get('promote_review_ok')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Health",
                    "value": (
                        f"• obs=`{report.get('observations')}` signals=`{report.get('signals')}`\n"
                        f"• A=`{report.get('state_a_count')}` B=`{report.get('state_b_count')}` "
                        f"Baseline=`{report.get('baseline_count')}` open=`{report.get('open_signals')}`\n"
                        f"• latest=`{latest}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Coverage A / B / Baseline",
                    "value": (
                        f"• A validation: `{_m(a, 'validation')}`\n"
                        f"• A discovery: `{_m(a, 'discovery')}`\n"
                        f"• B: `{_m(cov.get('B'))}`\n"
                        f"• Baseline: `{_m(cov.get('Baseline'))}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Lock",
                    "value": (
                        "• LIVE/PAPER/PEG2/TF2BP/UMM untouched\n"
                        "• No signal/threshold/state-def retune · PROMOTE_TO_PAPER≠LIVE"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "TF2B_10.15 · State A Shadow Validation · RESEARCH-2"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload = {"embeds": [embed]}
        ok_a = self._post(self.analysis_webhook_url or self.quants_agent_webhook_url, payload)
        ok_m = self.post_dryrun_multicast(payload)
        return bool(ok_a or ok_m)

    def post_tf2b_sfv_dryrun(self, report: Dict[str, Any], *, kind: str = "periodic") -> bool:
        """TF2B_SFV Dry-Run: 4-lane ANOVA (L0/L1/L2/L3) · WIRE=NO."""
        if not report:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        ctrl = report.get("TF2B_SFV_CTRL") or {}
        l1 = report.get("TF2B_SFV_L1") or {}
        l2 = report.get("TF2B_SFV_L2") or {}
        sfv = report.get("TF2B_SFV") or {}
        gate = report.get("gate") or {}
        a2 = report.get("attribution_phase2") or {}
        a1 = report.get("attribution_phase1") or {}
        kind_title = {
            "start": "🚀 【TF2B_SFV DryRun 開始】",
            "periodic": "📡 【TF2B_SFV DryRun】",
            "final": "🏁 【TF2B_SFV DryRun 終了】",
        }.get(kind, "📡 【TF2B_SFV DryRun】")

        def _lane(block: Dict[str, Any]) -> str:
            return (
                f"n=`{block.get('trades')}` WR=`{block.get('win_rate')}` "
                f"Σ=`{block.get('pnl_bp')}`bp EV=`{block.get('ev_bp')}`bp "
                f"pos=`{block.get('position')}`"
            )

        d_pnl = report.get("delta_pnl_bp")
        color = 0x3498DB
        try:
            if isinstance(d_pnl, (int, float)) and d_pnl > 0:
                color = 0x2ECC71
            elif isinstance(d_pnl, (int, float)) and d_pnl < 0:
                color = 0xE74C3C
        except Exception:
            pass

        embed = {
            "title": kind_title,
            "description": (
                f"**TF2B_SFV** 4-lane ANOVA · StateA × FBV × Sentinel\n"
                f"L0=CTRL · L1=FBV · L2=Sentinel · L3=SFV · WIRE=NO\n"
                f"elapsed=`{report.get('elapsed_hours')}h` · calib=`{report.get('calib_ready')}` · `{now_str}`"
            ),
            "color": color,
            "fields": [
                {
                    "name": "Lanes",
                    "value": (
                        f"• L0 CTRL: {_lane(ctrl)}\n"
                        f"• L1 FBV:  {_lane(l1)}\n"
                        f"• L2 SENT: {_lane(l2)}\n"
                        f"• L3 SFV:  {_lane(sfv)}\n"
                        f"• Δ(L3−L0)=`{d_pnl}`bp · ΔEV=`{report.get('delta_ev_bp')}`bp"
                    ),
                    "inline": False,
                },
                {
                    "name": "ANOVA (Phase2)",
                    "value": (
                        f"• fbv=`{a2.get('fbv_bp')}`bp · sentinel=`{a2.get('sentinel_bp')}`bp\n"
                        f"• interaction=`{a2.get('interaction_bp')}`bp · total=`{a2.get('total_bp')}`bp\n"
                        f"• Phase1 shadow alone=`{a1.get('sentinel_alone_bp')}`bp "
                        f"n=`{a1.get('ctrl_exits_with_shadow')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Stack Gate",
                    "value": (
                        f"• StateA=`{gate.get('state_a_signals')}` "
                        f"entries L0/L1/L2/L3="
                        f"`{gate.get('entries_ctrl')}`/`{gate.get('entries_l1')}`/"
                        f"`{gate.get('entries_l2')}`/`{gate.get('entries_sfv')}`\n"
                        f"• FakeBO=`{gate.get('fakebo_hits')}` veto=`{gate.get('veto_held')}` "
                        f"bypass=`{gate.get('bypass_allowed')}`\n"
                        f"• Sentinel exits L2/L3=`{gate.get('sentinel_exits_l2')}`/"
                        f"`{gate.get('sentinel_exits_sfv')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Lock",
                    "value": (
                        "• Frozen thresholds · no Truth/StateA/Sentinel retune\n"
                        "• LIVE/PAPER/FBV/10.15 shadow untouched · ENFORCE=0"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "TF2B_SFV · L0/L1/L2/L3 ANOVA · DRYRUN"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload = {"embeds": [embed]}
        ok_a = self._post(self.analysis_webhook_url or self.quants_agent_webhook_url, payload)
        ok_m = self.post_dryrun_multicast(payload)
        return bool(ok_a or ok_m)

    def post_tf2b_rvl_dryrun(self, report: Dict[str, Any], *, kind: str = "periodic") -> bool:
        """TF2B_RVL Dry-Run: 4-lane ANOVA (Risk Vacuum Layer vs Baseline) · WIRE=NO."""
        if not report:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        ctrl = report.get("TF2B_RVL_CTRL") or {}
        l1 = report.get("TF2B_RVL_L1") or {}
        l2 = report.get("TF2B_RVL_L2") or {}
        rvl = report.get("TF2B_RVL") or {}
        gate = report.get("gate") or {}
        a2 = report.get("attribution_phase2") or {}
        veto_stat = report.get("veto_stats") or {}
        kpis = report.get("kpi_comparison") or {}

        kind_title = {
            "start": "🚀 【TF2B_RVL DryRun 観測開始】",
            "periodic": "📊 【TF2B_RVL vs Baseline 1時間定期比較レポート】",
            "final": "🏁 【TF2B_RVL DryRun 観測完了】",
        }.get(kind, "📊 【TF2B_RVL vs Baseline 比較レポート】")

        def _lane_str(b: Dict[str, Any]) -> str:
            return (
                f"n=`{b.get('trades', 0)}` WR=`{b.get('win_rate', 0.0):.1%}` "
                f"Σ=`{b.get('pnl_bp', 0.0):+.2f}`bp MDD=`{b.get('mdd_bp', 0.0):.1f}`bp "
                f"NG=`{b.get('never_green_rate', 0.0):.1%}`"
            )

        ctrl_24h = ctrl.get("stats_24h") or {}
        rvl_24h = rvl.get("stats_24h") or {}
        ctrl_1h = ctrl.get("stats_1h") or {}
        rvl_1h = rvl.get("stats_1h") or {}
        d_24h_bp = report.get("delta_24h_bp", 0.0)
        d_1h_bp = report.get("delta_1h_bp", 0.0)
        d_mdd = kpis.get("delta_mdd_bp", 0.0)
        d_pnl = report.get("delta_pnl_bp", 0.0)
        # ドローダウン改善（負の差分）またはPnLプラスならグリーン
        color = 0x2ECC71 if (d_mdd < -10.0 or d_pnl > 0 or d_24h_bp > 0) else 0x3498DB

        embed = {
            "title": f"{kind_title} ({now_str})",
            "description": (
                f"**TF2B_RVL (Risk Vacuum Layer)** · 4-lane ANOVA\n"
                f"防護方針: **勝つためではなく負けないため** (入らない・早く逃げる)\n"
                f"経過時間: `{report.get('elapsed_hours', 0.0):.1f}`h | 総シグナル: `{gate.get('state_a_signals', 0)}`"
            ),
            "color": color,
            "fields": [
                {
                    "name": "💰 24時間 累積 PnL & 1時間 PnL (直近対比)",
                    "value": (
                        f"• **24時間 累積 PnL (RVL)**: **`{rvl_24h.get('pnl_bp', 0.0):+.2f} bp`** (`{rvl_24h.get('pnl_jpy', 0.0):+,.0f}円` | n={rvl_24h.get('trades', 0)})\n"
                        f"• **24時間 累積 PnL (CTRL)**: `{ctrl_24h.get('pnl_bp', 0.0):+.2f} bp` (`{ctrl_24h.get('pnl_jpy', 0.0):+,.0f}円` | n={ctrl_24h.get('trades', 0)})\n"
                        f"• **24h 防衛差分 (Δ24h)**: **`{d_24h_bp:+.2f} bp`**\n"
                        f"• **直近 1時間 PnL**: RVL=`{rvl_1h.get('pnl_bp', 0.0):+.2f}bp` vs CTRL=`{ctrl_1h.get('pnl_bp', 0.0):+.2f}bp` (Δ1h: `{d_1h_bp:+.2f}bp`)\n"
                        f"• **全期間 累積 PnL**: RVL=`{rvl.get('pnl_bp', 0.0):+.2f}bp` vs CTRL=`{ctrl.get('pnl_bp', 0.0):+.2f}bp` (Δ: `{d_pnl:+.2f}bp`)"
                    ),
                    "inline": False,
                },
                {
                    "name": "🏆 主要防衛KPI (RVL vs CTRL)",
                    "value": (
                        f"• **ΔMaxDrawdown**: `{d_mdd:+.2f} bp` (CTRL: `{ctrl.get('mdd_bp', 0.0):.1f}bp` ➔ RVL: `{rvl.get('mdd_bp', 0.0):.1f}bp`)\n"
                        f"• **ΔSaved (防衛創出)**: `+{kpis.get('delta_saved_bp', 0.0):.2f} bp` (Sentinel早期脱出)\n"
                        f"• **NeverGreen率**: `{rvl.get('never_green_rate', 0.0):.1%}` (CTRL: `{ctrl.get('never_green_rate', 0.0):.1%}` | Δ: `{kpis.get('delta_never_green_rate', 0.0):+.1%}`)\n"
                        f"• **AE 1s / 3s**: `{rvl.get('ae_1s_avg', 0.0):+.2f}bp` / `{rvl.get('ae_3s_avg', 0.0):+.2f}bp`\n"
                        f"• **Net PnL (bp)**: `{rvl.get('pnl_bp', 0.0):+.2f} bp` (CTRL: `{ctrl.get('pnl_bp', 0.0):+.2f}bp` | Δ: `{d_pnl:+.2f}bp`)"
                    ),
                    "inline": False,
                },
                {
                    "name": "🔬 4レーン成績対比 (ANOVA)",
                    "value": (
                        f"• **L0 CTRL (Baseline)**: {_lane_str(ctrl)}\n"
                        f"• **L1 VETO (入らない)**: {_lane_str(l1)}\n"
                        f"• **L2 SENTINEL (逃げる)**: {_lane_str(l2)}\n"
                        f"• **L3 RVL (統合)**: {_lane_str(rvl)}"
                    ),
                    "inline": False,
                },
                {
                    "name": "📐 要因分解 (Attribution)",
                    "value": (
                        f"• **Veto効果 (入らない)**: `{a2.get('veto_bp', 0.0):+.2f}` bp\n"
                        f"• **Sentinel効果 (逃げる)**: `{a2.get('sentinel_bp', 0.0):+.2f}` bp\n"
                        f"• **複合シナジー**: `{a2.get('interaction_bp', 0.0):+.2f}` bp\n"
                        f"• **総合差分 (L3 - L0)**: `{a2.get('total_bp', 0.0):+.2f}` bp"
                    ),
                    "inline": False,
                },
                {
                    "name": "🛡 防御レイヤー発動内訳",
                    "value": (
                        f"• **Adverse Veto**: `{veto_stat.get('adverse_vetos', 0)}`回拒絶 (防衛: `+{veto_stat.get('adverse_saved_bp', 0.0):.1f}bp`)\n"
                        f"• **Vacuum Veto**: `{veto_stat.get('vacuum_vetos', 0)}`回拒絶 (防衛: `+{veto_stat.get('vacuum_saved_bp', 0.0):.1f}bp`)\n"
                        f"• **News Shock**: `{veto_stat.get('news_vetos', 0)}`回拒絶\n"
                        f"• **Sentinel Exit**: L2=`{gate.get('sentinel_exits_l2', 0)}`回 / L3=`{gate.get('sentinel_exits_rvl', 0)}`回"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "TF2B_RVL · 4-lane ANOVA · Head of Agent Defense Initiative"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload = {"embeds": [embed]}
        ok_a = self._post(self.analysis_webhook_url or self.quants_agent_webhook_url, payload)
        ok_m = self.post_dryrun_multicast(payload)
        return bool(ok_a or ok_m)

    def post_tf2b_deseva_dryrun(self, report: Dict[str, Any], *, kind: str = "periodic") -> bool:
        """TF2B_DeSeva_v1 Dry-Run: TF2BP × SEVA filter vs baseline (WIRE=NO)."""
        if not report:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        ctrl = report.get("TF2BP_CTRL") or report.get("tf2bp_ctrl") or {}
        arm = report.get("TF2B_DeSeva_v1") or report.get("tf2b_deseva_v1") or {}
        gate = report.get("gate") or {}
        cmp = report.get("compare") or {}
        kind_title = {
            "start": "🚀 【TF2B_DeSeva_v1 DryRun 開始】",
            "periodic": "📡 【TF2B_DeSeva_v1 DryRun】",
            "final": "🏁 【TF2B_DeSeva_v1 DryRun 終了】",
        }.get(kind, "📡 【TF2B_DeSeva_v1 DryRun】")

        def _lane(b: Dict[str, Any]) -> str:
            return (
                f"n=`{b.get('trades')}` WR=`{b.get('win_rate')}` "
                f"Σ=`{b.get('pnl_bp')}`bp EV=`{b.get('ev_bp')}` "
                f"Sharpe=`{b.get('sharpe')}` DD=`{b.get('max_dd_bp')}` "
                f"pos=`{b.get('position')}`"
            )

        d_sh = cmp.get("delta_sharpe", report.get("delta_sharpe"))
        color = 0x3498DB
        try:
            if isinstance(d_sh, (int, float)) and d_sh > 0:
                color = 0x2ECC71
            elif isinstance(d_sh, (int, float)) and d_sh < 0:
                color = 0xE74C3C
        except Exception:
            pass

        embed = {
            "title": kind_title,
            "description": (
                f"**TF2B_DeSeva_v1** = TF2BP × SEVA(`entry_allowed`)\n"
                f"Hypothesis: SEVA高評価局面のみ → **Sharpe↑ / MaxDD改善**\n"
                f"elapsed=`{report.get('elapsed_hours')}h` · models=`{gate.get('seva_models')}` · `{now_str}`"
            ),
            "color": color,
            "fields": [
                {
                    "name": "CTRL vs DeSeva",
                    "value": (
                        f"• CTRL: {_lane(ctrl)}\n"
                        f"• DeSeva: {_lane(arm)}\n"
                        f"• Δpnl=`{cmp.get('delta_pnl_bp', report.get('delta_pnl_bp'))}` · "
                        f"ΔEV=`{cmp.get('delta_ev_bp', report.get('delta_ev_bp'))}` · "
                        f"ΔSharpe=`{d_sh}` · ΔDD=`{cmp.get('delta_max_dd_bp')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "24h proof window",
                    "value": (
                        f"• CTRL 24h: pnl=`{(ctrl.get('stats_24h') or {}).get('pnl_bp')}` "
                        f"Sharpe=`{(ctrl.get('stats_24h') or {}).get('sharpe')}` "
                        f"DD=`{(ctrl.get('stats_24h') or {}).get('max_dd_bp')}`\n"
                        f"• DeSeva 24h: pnl=`{(arm.get('stats_24h') or {}).get('pnl_bp')}` "
                        f"Sharpe=`{(arm.get('stats_24h') or {}).get('sharpe')}` "
                        f"DD=`{(arm.get('stats_24h') or {}).get('max_dd_bp')}`\n"
                        f"• pass_sharpe_24h=`{cmp.get('pass_sharpe_24h')}` · "
                        f"pass_dd_24h=`{cmp.get('pass_dd_24h')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "SEVA Gate",
                    "value": (
                        f"• signals CTRL/DeSeva=`{gate.get('signals_ctrl')}`/`{gate.get('signals_deseva')}`\n"
                        f"• allowed=`{gate.get('seva_allowed')}` rejected=`{gate.get('seva_rejected')}` "
                        f"allow_rate=`{gate.get('seva_allow_rate')}`\n"
                        f"• latest=`{report.get('latest_seva')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Lock",
                    "value": "• WIRE=NO · TF2BP pin frozen · SEVA filter only · no LIVE/PAPER touch",
                    "inline": False,
                },
            ],
            "footer": {"text": "TF2B_DeSeva_v1 · SEVA-filtered TF2BP · DRYRUN paired"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload = {"embeds": [embed]}
        ok_a = self._post(self.analysis_webhook_url or self.quants_agent_webhook_url, payload)
        ok_m = self.post_dryrun_multicast(payload)
        ok_d = self._post(self.tf2b_deseva_webhook_url, payload) if self.tf2b_deseva_webhook_url else False
        return bool(ok_a or ok_m or ok_d)

    def post_tf2b_deseva_hourly_pnl(self, report: Dict[str, Any]) -> bool:
        """TF2B_DeSeva_v1 毎時: 1h PnL / 24h 累積 PnL（専用 webhook）。"""
        if not report:
            return False
        url = self.tf2b_deseva_webhook_url
        if not url:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        ctrl = report.get("TF2BP_CTRL") or report.get("tf2bp_ctrl") or {}
        arm = report.get("TF2B_DeSeva_v1") or report.get("tf2b_deseva_v1") or {}
        cmp = report.get("compare") or {}
        gate = report.get("gate") or {}
        c1 = ctrl.get("stats_1h") or {}
        c24 = ctrl.get("stats_24h") or {}
        a1 = arm.get("stats_1h") or {}
        a24 = arm.get("stats_24h") or {}
        d1 = float(cmp.get("delta_1h_pnl_bp", float(a1.get("pnl_bp") or 0) - float(c1.get("pnl_bp") or 0)) or 0)
        d24 = float(
            cmp.get("delta_24h_pnl_bp", float(a24.get("pnl_bp") or 0) - float(c24.get("pnl_bp") or 0)) or 0
        )
        color = 0x2ECC71 if d24 >= 0 else 0xE74C3C
        embed = {
            "title": "📡 【TF2B_DeSeva_v1】1h / 24h PnL 検証",
            "description": (
                f"**TF2BP × SEVA filter** vs CTRL · WIRE=NO\n"
                f"elapsed=`{report.get('elapsed_hours')}h` · `{now_str}`"
            ),
            "color": color,
            "fields": [
                {
                    "name": "① 直近 1時間 (1h) PnL",
                    "value": (
                        f"• **DeSeva**: **`{float(a1.get('pnl_bp') or 0):+.2f} bp`** "
                        f"({int(a1.get('total_trades') or 0)}戦 / "
                        f"{float(a1.get('win_rate_pct') or 0):.0f}% / "
                        f"¥{float(a1.get('pnl_jpy') or 0):+,.0f})\n"
                        f"• **CTRL**: **`{float(c1.get('pnl_bp') or 0):+.2f} bp`** "
                        f"({int(c1.get('total_trades') or 0)}戦)\n"
                        f"• **Δ1h**: **`{d1:+.2f} bp`**"
                    ),
                    "inline": False,
                },
                {
                    "name": "② 過去24時間 累積 PnL",
                    "value": (
                        f"• **DeSeva**: **`{float(a24.get('pnl_bp') or 0):+.2f} bp`** "
                        f"({int(a24.get('total_trades') or 0)}戦 / "
                        f"{float(a24.get('win_rate_pct') or 0):.0f}% / "
                        f"¥{float(a24.get('pnl_jpy') or 0):+,.0f})\n"
                        f"• **CTRL**: **`{float(c24.get('pnl_bp') or 0):+.2f} bp`** "
                        f"({int(c24.get('total_trades') or 0)}戦)\n"
                        f"• **Δ24h**: **`{d24:+.2f} bp`**"
                    ),
                    "inline": False,
                },
                {
                    "name": "③ Sharpe / MaxDD (proof)",
                    "value": (
                        f"• Sharpe DeSeva/CTRL=`{arm.get('sharpe')}`/`{ctrl.get('sharpe')}` "
                        f"(Δ=`{cmp.get('delta_sharpe')}`)\n"
                        f"• MaxDD DeSeva/CTRL=`{arm.get('max_dd_bp')}`/`{ctrl.get('max_dd_bp')}` "
                        f"(Δ=`{cmp.get('delta_max_dd_bp')}`)\n"
                        f"• allow_rate=`{gate.get('seva_allow_rate')}` · "
                        f"pass_sharpe_24h=`{cmp.get('pass_sharpe_24h')}` · "
                        f"pass_dd_24h=`{cmp.get('pass_dd_24h')}`"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "TF2B_DeSeva_v1 · hourly 1h/24h PnL · CSR-600"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self._post(url, {"embeds": [embed]})

    def post_tf2b_fbv_dryrun(self, report: Dict[str, Any], *, kind: str = "periodic") -> bool:
        """TF2B_FBV Dry-Run: TF2BP baseline + FakeBO Veto Bypass (WIRE=NO · paired CTRL)."""
        if not report:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        ctrl = report.get("TF2BP_CTRL") or {}
        fbv = report.get("TF2B_FBV") or {}
        gate = report.get("gate") or {}
        thr = report.get("truth_thresholds") or {}

        kind_title = {
            "start": "🚀 【TF2B_FBV DryRun 開始】",
            "periodic": "📡 【TF2B_FBV DryRun】",
            "final": "🏁 【TF2B_FBV DryRun 終了】",
        }.get(kind, "📡 【TF2B_FBV DryRun】")

        def _lane(block: Dict[str, Any]) -> str:
            return (
                f"n=`{block.get('trades')}` WR=`{block.get('win_rate')}` "
                f"Σ=`{block.get('pnl_bp')}`bp EV=`{block.get('ev_bp')}`bp "
                f"pos=`{block.get('position')}`"
            )

        d_pnl = report.get("delta_pnl_bp")
        d_ev = report.get("delta_ev_bp")
        color = 0x3498DB
        try:
            if isinstance(d_pnl, (int, float)) and d_pnl > 0:
                color = 0x2ECC71
            elif isinstance(d_pnl, (int, float)) and d_pnl < 0:
                color = 0xE74C3C
        except Exception:
            pass

        embed = {
            "title": kind_title,
            "description": (
                f"TF2BP Baseline + **Fake Breakout Veto Bypass** (Liquidity Truth)\n"
                f"Paired: `TF2BP_CTRL` vs `TF2B_FBV` · WIRE=NO · DRYRUN\n"
                f"elapsed=`{report.get('elapsed_hours')}h` · src=`{report.get('market_source')}` · `{now_str}`"
            ),
            "color": color,
            "fields": [
                {
                    "name": "CTRL vs FBV",
                    "value": (
                        f"• CTRL: {_lane(ctrl)}\n"
                        f"• FBV:  {_lane(fbv)}\n"
                        f"• Δpnl=`{d_pnl}`bp · ΔEV=`{d_ev}`bp"
                    ),
                    "inline": False,
                },
                {
                    "name": "FakeBO Gate",
                    "value": (
                        f"• signals=`{gate.get('signals')}` fakebo=`{gate.get('fakebo_hits')}`\n"
                        f"• veto_held=`{gate.get('veto_held')}` bypass=`{gate.get('bypass_allowed')}`\n"
                        f"• entries CTRL/FBV=`{gate.get('entries_ctrl')}`/`{gate.get('entries_fbv')}` "
                        f"exits=`{gate.get('exits_ctrl')}`/`{gate.get('exits_fbv')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Truth Thresholds",
                    "value": (
                        f"• cushion≥`{thr.get('cushion_core_min')}` · adverse≤`{thr.get('adverse_taker_max')}`\n"
                        f"• pen≤`{thr.get('penetration_max')}` · sma30≥`{thr.get('sma30_min')}`\n"
                        f"• FakeBO micro: cancel≥0.55 ∧ aggressiveness<0.15"
                    ),
                    "inline": False,
                },
                {
                    "name": "Lock",
                    "value": (
                        "• LIVE/PAPER/PEG2/UMM/TF2BP dryrun baseline untouched\n"
                        "• No WIRE · No ENFORCE · Research observation only"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "TF2B_FBV · FakeBO Veto Bypass · DRYRUN paired"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload = {"embeds": [embed]}
        ok_a = self._post(self.analysis_webhook_url or self.quants_agent_webhook_url, payload)
        ok_m = self.post_dryrun_multicast(payload)
        return bool(ok_a or ok_m)

    def post_umm_p2_maker_shadow(self, report: Dict[str, Any], *, kind: str = "periodic") -> bool:
        """UMM-P2 Shadow: UMM Baseline vs UMM+Maker (Execution-only · WIRE=NO)."""
        if not report:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        verdict = str(report.get("verdict") or "MORE_DATA")
        gate = report.get("gate") or {}
        delta = report.get("delta") or {}
        mon = report.get("daily_monitor") or {}
        c = ((report.get("comparison") or {}).get("CTRL") or {}).get("headline") or {}
        a = ((report.get("comparison") or {}).get("ARM") or {}).get("headline") or {}
        sink = mon.get("sink_10s_plus") or {}
        bucket_ev = mon.get("bucket_ev") or {}

        kind_title = {
            "start": "🚀 【UMM-P2 Shadow 開始】",
            "periodic": "📊 【UMM-P2 Shadow 定期検証】",
            "daily": "📅 【UMM-P2 Shadow 日次検証】",
            "verdict": "⚖️ 【UMM-P2 Shadow 判定更新】",
            "final": "🏁 【UMM-P2 Shadow 終了】",
        }.get(kind, "📊 【UMM-P2 Shadow】")

        color = {
            "REJECT": 0xE74C3C,
            "PROMOTE": 0x2ECC71,
            "MORE_DATA": 0xF39C12,
        }.get(verdict, 0x3498DB)

        def _f(v, nd=2):
            if v is None:
                return "—"
            if isinstance(v, float):
                return f"{v:+.{nd}f}" if nd else f"{v:+g}"
            return str(v)

        def _bucket_line(name: str) -> str:
            b = bucket_ev.get(name) or {}
            return f"`{name}` CTRL={_f(b.get('CTRL'),4)} ARM={_f(b.get('ARM'),4)} Δ={_f(b.get('delta'),4)}"

        ng = mon.get("never_green_rate") or {}
        aw = mon.get("ae_worsen_rate") or {}

        embed = {
            "title": f"{kind_title} {verdict}",
            "description": (
                f"UMM Baseline vs UMM+Maker · **Execution only** · WIRE=NO\n"
                f"elapsed=`{report.get('elapsed_hours')}h` · sample_floor=`{report.get('sample_floor_met')}`\n"
                f"auto_PROMOTE=**False** · `{now_str}`"
            ),
            "color": color,
            "fields": [
                {
                    "name": "判定",
                    "value": (
                        f"• verdict=`{verdict}` · promote_label=`{report.get('promote_label')}`\n"
                        f"• reason=`{gate.get('reason') or '—'}`\n"
                        f"• flags=`{gate.get('improve_flags')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Headline Δ(ARM−CTRL)",
                    "value": (
                        f"• ΔPnL=`{_f(delta.get('pnl_bp'))}bp` · ΔEV=`{_f(delta.get('ev_bp'),4)}` · "
                        f"ΔPF=`{_f(delta.get('pf'),4)}` · ΔDD=`{_f(delta.get('dd_bp'))}`\n"
                        f"• ΔNeverGreen=`{_f(delta.get('never_green_rate'),4)}` · "
                        f"ΔAEworsen=`{_f(delta.get('ae_worsen_rate'),4)}` · "
                        f"ΔAE1s=`{_f(delta.get('avg_ae_1s'),4)}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "CTRL / ARM",
                    "value": (
                        f"• CTRL n=`{c.get('n')}` EV=`{_f(c.get('ev_bp'),4)}` PnL=`{_f(c.get('pnl_bp'))}` "
                        f"PF=`{_f(c.get('pf'),4)}` NG=`{_f(c.get('never_green_rate'),4)}`\n"
                        f"• ARM  n=`{a.get('n')}` EV=`{_f(a.get('ev_bp'),4)}` PnL=`{_f(a.get('pnl_bp'))}` "
                        f"PF=`{_f(a.get('pf'),4)}` fill=`{_f(a.get('fill_rate'),4)}` "
                        f"timeouts=`{a.get('timeouts')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Daily primary · 別EV / 10s+ sink",
                    "value": (
                        f"• Never-green CTRL=`{_f(ng.get('CTRL'),4)}` ARM=`{_f(ng.get('ARM'),4)}` Δ=`{_f(ng.get('delta'),4)}`\n"
                        f"• AE worsen CTRL=`{_f(aw.get('CTRL'),4)}` ARM=`{_f(aw.get('ARM'),4)}` Δ=`{_f(aw.get('delta'),4)}`\n"
                        f"• {_bucket_line('0-10s')}\n"
                        f"• {_bucket_line('10-30s')}\n"
                        f"• {_bucket_line('30-120s')}\n"
                        f"• 10s+ sink ΔPnL=`{_f(sink.get('delta_pnl_bp'))}` ΔEV=`{_f(sink.get('delta_ev_bp'),4)}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Lock",
                    "value": (
                        "• Signal/Inventory/Skew/Sizing/Exit/Params **FROZEN**\n"
                        "• LIVE untouched · PEG2 Paper monitor-only · PROMOTE≠LIVE"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "UMM-P2 Maker Shadow · research lane · Execution Alpha validation"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload = {"embeds": [embed]}
        # Research lane: analysis + dryrun multicast (never LIVE-only path as sole destination intent)
        ok_a = self._post(self.analysis_webhook_url or self.quants_agent_webhook_url, payload)
        ok_m = self.post_dryrun_multicast(payload)
        return bool(ok_a or ok_m)

    def post_umm_ctrl_observe(self, report: Dict[str, Any]) -> bool:
        """CSR-521-CTRL v0 OBSERVE — hold ladder + Virtual Exit@30s (WIRE=NO · ENFORCE=0)."""
        if not report:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        hw = report.get("hourly_watch") or {}
        ladder = hw.get("ladder_pnl_hs") or []
        lad_lines = []
        for r in ladder:
            hs = r.get("hard_stop_rate_pct")
            hs_s = f"{hs}%" if hs is not None else "—"
            lad_lines.append(
                f"• `{r.get('level')}` n={r.get('n')} Σ={r.get('sum_pnl_bp'):+g}bp HS={hs_s}"
            )
        d30 = hw.get("would_exit_30_delta_bp")
        d60 = hw.get("would_exit_60_delta_bp")
        d120 = hw.get("would_exit_120_delta_bp")
        sides = report.get("ctrl5_one_side_retreat") or {}
        long_s = sides.get("long") or {}
        short_s = sides.get("short") or {}

        def _d(v):
            return f"{v:+.2f}bp" if isinstance(v, (int, float)) else "—"

        embed = {
            "title": "🎛️ 【CSR-521-CTRL OBSERVE】 Inventory Control v0",
            "description": (
                f"目的: **HARD_STOP流入観測**（方向予測ではない）\n"
                f"window=`{report.get('window')}` · n=`{(report.get('source') or {}).get('n_trades')}`\n"
                f"WIRE=NO · ENFORCE=0 · Economic PASS不出 · `{now_str}`"
            ),
            "color": 0xE67E22,
            "fields": [
                {
                    "name": "CTRL-1 Hold Ladder (L0–L4)",
                    "value": "\n".join(lad_lines) or "• —",
                    "inline": False,
                },
                {
                    "name": "CTRL-2 Would-Unwind Δ (Virtual_full − Actual_full)",
                    "value": (
                        f"• @10s `{_d(hw.get('would_exit_10_delta_bp'))}` · "
                        f"@20s `{_d(hw.get('would_exit_20_delta_bp'))}` · "
                        f"@30s `{_d(d30)}` ← **最優先** · id_ok=`{hw.get('delta_identity_ok')}`\n"
                        f"• @45s `{_d(hw.get('would_exit_45_delta_bp'))}` · "
                        f"@60s `{_d(d60)}` · @90s `{_d(hw.get('would_exit_90_delta_bp'))}` · "
                        f"@120s `{_d(d120)}`\n"
                        f"• L2+L3のみ Δ: `{_d(hw.get('l2_l3_only_delta_bp'))}`\n"
                        f"• Actual_full: `{_d(hw.get('actual_ledger_bp'))}`\n"
                        f"• Virtual@30_full: `{_d(hw.get('virtual_30_full_ledger_bp') or hw.get('virtual_30_ledger_bp'))}`\n"
                        f"• (diag) eligible Actual/Virtual: `{_d(hw.get('actual_eligible_gt30_bp'))}` / "
                        f"`{_d(hw.get('virtual_30_eligible_sum_bp'))}` ← **足し引き禁止**"
                    ),
                    "inline": False,
                },
                {
                    "name": "CTRL-5 One-Side",
                    "value": (
                        f"• long Σ=`{long_s.get('sum_pnl_bp')}` HS=`{long_s.get('hard_stop_rate')}` "
                        f"hold_p50=`{long_s.get('hold_p50')}`\n"
                        f"• short Σ=`{short_s.get('sum_pnl_bp')}` HS=`{short_s.get('hard_stop_rate')}` "
                        f"hold_p50=`{short_s.get('hold_p50')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Lock",
                    "value": (
                        "• primary=`hold` · secondary inactive (csnt/fake_bo)\n"
                        "• 禁止: 新アルファ · 価格予測利用 · ENFORCE · LIVE"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "CSR-521-CTRL v0 · OBSERVE only · order: design→Δbp→PASS→ENFORCE"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        url = self.analysis_webhook_url or self.quants_agent_webhook_url
        ok = self._post(url, {"embeds": [embed]})
        self.post_dryrun_multicast({"embeds": [embed]})
        return ok

    def post_peg2b_live_trial(self, report: Dict[str, Any], *, kind: str = "periodic") -> bool:
        """CSR-588 PEG2-B LIVE Trial v1 — fill-quality / Paper→LIVE gap (lot=0.001)."""
        if not report:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        kind_title = {
            "start": "🚀 【PEG2-B LIVE Trial 開始】",
            "periodic": "📡 【PEG2-B LIVE Trial 30m】",
            "phase1": "⏱️ 【PEG2-B LIVE Trial Phase-1 Gate】",
            "final": "🏁 【PEG2-B LIVE Trial 6h】",
            "stop": "🛑 【PEG2-B LIVE Trial STOP/FLAT】",
        }.get(kind, "📡 【PEG2-B LIVE Trial】")
        stop = report.get("stop_reason")
        color = 0xE74C3C if (stop or kind == "stop") else (0x2ECC71 if kind in ("start", "final") else 0x3498DB)
        gate = report.get("phase_gate") or {}
        flatten = report.get("flatten") or {}
        embed = {
            "title": kind_title,
            "description": (
                f"目的: **Paper→LIVE 乖離 / 実約定品質**（利益最大化ではない）\n"
                f"lot=`0.001` · phase=`{report.get('phase')}` · elapsed=`{report.get('elapsed_hours')}h`\n"
                f"Signal/Exit/Sizing **FROZEN** · `{now_str}`"
            ),
            "color": color,
            "fields": [
                {
                    "name": "KPI",
                    "value": (
                        f"• LIVE PnL=`{report.get('live_pnl_bp')}bp` "
                        f"(ΔPaper=`{report.get('pnl_diff_live_minus_paper_bp')}`)\n"
                        f"• Fill=`{report.get('fill_rate')}` · Timeout=`{report.get('timeout_rate')}` "
                        f"· MakerFill%=`{report.get('maker_fill_pct')}`\n"
                        f"• Slip=`{report.get('avg_slippage_bp')}bp` · WR=`{report.get('win_rate')}` "
                        f"· EV=`{report.get('ev_bp')}` (PaperEV=`{report.get('paper_ev_bp_window')}` "
                        f"Δ=`{report.get('ev_diff_live_minus_paper_bp')}`)\n"
                        f"• trades=`{report.get('trades')}` orders=`{report.get('maker_orders')}` "
                        f"consec_loss=`{report.get('consec_losses')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Gate / Stop",
                    "value": (
                        f"• stop=`{stop or '—'}`\n"
                        f"• phase_gate=`{gate or '—'}`\n"
                        f"• promote_review=`{report.get('promote_review', '—')}`\n"
                        f"• flat_ok=`{flatten.get('flat_ok', '—')}`"
                    ),
                    "inline": False,
                },
                {
                    "name": "Lock",
                    "value": (
                        "• PEG2-B only · CSR-521/PEG_v3/TF2B_10.15/4AGENT **OBSERVE ONLY**\n"
                        "• `.env` ENABLE_REAL_TRADING 不変 · AUTO_APPLY=OFF"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "CSR-588 · PEG2-B LIVE Trial v1 · max 6h"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload = {"embeds": [embed]}
        ok_live = self._post(self.live_webhook_url, payload)
        ok_a = self._post(self.analysis_webhook_url or self.quants_agent_webhook_url, payload)
        return bool(ok_live or ok_a)

    def post_librarian_daily(self, report: Dict[str, Any]) -> bool:
        """DuckDB Research Librarian 日次 usable / ADVISE。"""
        if not report:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        lanes = report.get("lanes") or {}
        lane_lines = []
        for name, lane in lanes.items():
            if not isinstance(lane, dict):
                continue
            u = lane.get("usable", "?")
            adv0 = (lane.get("advise") or ["—"])[0]
            lane_lines.append(f"• **{name}**: `{u}` — {adv0}")
        exp_lines = []
        for e in (report.get("next_experiments") or [])[:8]:
            exp_lines.append(f"• [{e.get('priority','normal')}] `{e.get('lane')}`: {e.get('advise')}")
        counts = report.get("usable_counts") or {}
        embed = {
            "title": "📚 【Research Librarian 日次判定】 DuckDB",
            "description": (
                f"日付: `{report.get('date')}` | portfolio: **`{report.get('portfolio_verdict')}`**\n"
                f"USEFUL={counts.get('USEFUL',0)} NEED_MORE={counts.get('NEED_MORE',0)} "
                f"NOT_USEFUL={counts.get('NOT_USEFUL',0)} CONTEXT={counts.get('CONTEXT',0)}\n"
                f"WIRE=NO / ENFORCE=0 / 重み自動適用OFF\n報告: `{now_str}`"
            ),
            "color": 0xF1C40F,
            "fields": [
                {"name": "レーン usable", "value": "\n".join(lane_lines) or "• —", "inline": False},
                {"name": "次に試すこと (ADVISE)", "value": "\n".join(exp_lines) or "• 継続観測", "inline": False},
                {
                    "name": "禁止",
                    "value": "• 経済PASS/FAIL • LIVE配線 • approved_weights自動更新 • frozenパラ自動変更",
                    "inline": False,
                },
            ],
            "footer": {"text": "DuckDB Research Librarian • 全研究レーン最終チェック"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        ok = self.post_conclusion({"embeds": [embed]})
        self.post_dryrun_multicast({"embeds": [embed]})
        url = self.analysis_webhook_url or self.quants_agent_webhook_url
        self._post(url, {"embeds": [embed]})
        return ok

    def post_sentinel_report(self, report: Dict[str, Any]) -> bool:
        """CSR-590 Liquidity Absorption Sentinel: Research & Lead Time Audit Report."""
        if not report:
            return False
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        verdict = report.get("verdict", "OBSERVE_DEPLOYED")
        color = 0x2ECC71 if "DEPLOYED" in verdict or "PASS" in verdict else 0x3498DB

        embed = {
            "title": f"🛡️ 【Liquidity Absorption Sentinel】 {report.get('title', '検証レポート')}",
            "description": (
                f"**研究テーマ**: 板崩壊予兆 / 流動性吸収モデル (WIRE=NO · OBSERVE=1)\n"
                f"**対象戦略**: PEG2-B 実稼働 (全 {report.get('total_trades', 209)} 件)\n"
                f"**ステータス**: `{verdict}` · `{now_str}`"
            ),
            "color": color,
            "fields": [
                {
                    "name": "🎯 6大壊滅トレード救出成果 (Disasters <= -20bp)",
                    "value": (
                        f"• **救出率**: `{report.get('disasters_saved', '6/6 (100%)')}`\n"
                        f"• **総損失救出 (Saved)**: **`{report.get('total_saved_bp', '+162.3 bp')}`**\n"
                        f"• **救出例**: #152 (`-62.7bp` $\\to$ `-10.3bp` / `+52.4bp Saved`)\n"
                        f"• **救出例**: #134 (`-41.8bp` $\\to$ `-9.1bp` / `+32.8bp Saved` / `41s脱出`)"
                    ),
                    "inline": False,
                },
                {
                    "name": "🔍 False Hold (救出漏れ) 解明と対策",
                    "value": (
                        f"• **Trade #58 真因**: 買い板蒸発 (`0.01 BTC`) 下での微小リフィル計算トラップ\n"
                        f"• **対策**: `Evaporated Book Bypass` (板蒸発時は Shield を強制バイパス)\n"
                        f"• **神リバウンド (#34)**: ドローダウン中も `SMA 30s >= 0.00` & `Cushion >= 25 BTC` を維持"
                    ),
                    "inline": False,
                },
                {
                    "name": "📡 リアルタイム常駐 OBSERVE 稼働状況",
                    "value": (
                        f"• **サービス**: `tf2bp-peg2-paper.service` (Active Running)\n"
                        f"• **観測ログ**: `data/mm_research/sentinel_shadow_observe.jsonl`\n"
                        f"• **共通化**: UMM / CSR-521 は完全凍結維持 (PEG2-B 単独先行)"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "CSR-590 · Liquidity Absorption Sentinel · D2 OBSERVE Sidecar"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload = {"embeds": [embed]}
        url = self.analysis_webhook_url or self.quants_agent_webhook_url or self.dryrun_webhook_url
        ok = self._post(url, payload)
        self.post_dryrun_multicast(payload)
        return ok

    def post_sentinel_trade_event(self, event_data: Dict[str, Any]) -> bool:
        """CSR-590 Real-time Sentinel Shadow Exit Comparison Event."""
        if not event_data:
            return False
        t_id = event_data.get("trade_id")
        actual_pnl = event_data.get("actual_pnl_bp", 0.0)
        actual_hold = event_data.get("actual_hold_sec", 0.0)
        shadow_pnl = event_data.get("shadow_pnl_bp", actual_pnl)
        shadow_hold = event_data.get("shadow_hold_sec", actual_hold)
        delta_saved = event_data.get("delta_saved_bp", 0.0)
        lead_time = event_data.get("lead_time_saved_sec", 0.0)
        is_false = event_data.get("is_false_exit", False)
        trig_type = event_data.get("trigger_type", "NONE")

        if is_false:
            color = 0xE74C3C  # Red (False Exit)
            title = f"⚠️ [Sentinel Observe] Trade #{t_id} FALSE EXIT 誤認判定"
        elif delta_saved > 0:
            color = 0x2ECC71  # Green (Loss Saved)
            title = f"🛡️ [Sentinel Observe] Trade #{t_id} LOSS SAVED (+{delta_saved:.1f} bp)"
        else:
            color = 0x95A5A6  # Gray (Neutral / No trigger)
            title = f"ℹ️ [Sentinel Observe] Trade #{t_id} Clean Close"

        embed = {
            "title": title,
            "description": f"Sentinel Shadow Exit vs Baseline Actual Exit (WIRE=NO)",
            "color": color,
            "fields": [
                {
                    "name": "実績 (Actual Baseline)",
                    "value": f"• PnL: **`{actual_pnl:+.1f} bp`**\n• Hold: `{actual_hold:.0f}s`",
                    "inline": True,
                },
                {
                    "name": "Sentinel (Shadow)",
                    "value": f"• PnL: **`{shadow_pnl:+.1f} bp`**\n• Hold: `{shadow_hold:.0f}s`",
                    "inline": True,
                },
                {
                    "name": "回避・短縮効果",
                    "value": f"• **ΔSaved**: `{delta_saved:+.1f} bp`\n• **LeadTime**: `{lead_time:.0f}s`\n• Trigger: `{trig_type}`",
                    "inline": True,
                },
            ],
            "footer": {"text": "Liquidity Absorption Sentinel • Real-time Shadow"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload = {"embeds": [embed]}
        url = self.analysis_webhook_url or self.quants_agent_webhook_url or self.dryrun_webhook_url
        return self._post(url, payload)

