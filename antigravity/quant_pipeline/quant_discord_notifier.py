"""
Antigravity Quant Discord Notifier
==================================
あかり専用 Discord 3サーバー連携クライアント:
1. LIVE取引サーバー (本番: 約定・TP/SL・リスク・日次損益) - ノイズゼロ・高重要度
2. Quants Dry-run サーバー (観測: Fusion Engine意思決定・板圧力・確信度・仮想PnL)
3. 分析・重み更新サーバー (研究: DuckDB集計結果・ΔW重み更新推奨・戦略承認)
"""
import os
import time
import json
import urllib.request
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

JST = timezone(timedelta(hours=9))


class QuantDiscordNotifier:
    def __init__(
        self,
        live_webhook_url: Optional[str] = None,
        dryrun_webhook_url: Optional[str] = None,
        analysis_webhook_url: Optional[str] = None,
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

        # 送信レートリミット制御用タイムスタンプ
        self._last_dryrun_sent_ts: float = 0.0
        self._min_dryrun_interval_sec: float = 5.0  # Dry-runのスパム抑止インターバル

    def _post(self, webhook_url: str, payload: Dict[str, Any]) -> bool:
        if not webhook_url or not webhook_url.startswith("http"):
            return False
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Antigravity-Quant-Notifier/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            print(f"[QuantDiscordNotifier] ⚠️ 送信エラー ({webhook_url[:35]}...): {e}", flush=True)
            return False

    def post_dryrun_multicast(self, payload: Dict[str, Any]) -> bool:
        """
        DRYRUN定期報告をメイン運用報告チャンネル (REPORT) および DRYRUNチャンネルの両方に配信
        """
        urls = set()
        if self.dryrun_webhook_url:
            urls.add(self.dryrun_webhook_url)
        if self.live_webhook_url:
            urls.add(self.live_webhook_url)
        report_url = os.environ.get("DISCORD_REPORT_WEBHOOK_URL", "").strip()
        if report_url:
            urls.add(report_url)

        success = False
        for url in urls:
            if self._post(url, payload):
                success = True
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
