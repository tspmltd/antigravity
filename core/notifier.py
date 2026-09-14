import os
import sys
import json
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

load_dotenv()


class DiscordNotifier:
    """
    Discord Webhook 通知クライアント。
    1時間ごとの定期成績レポートや、ドローダウン検知・戦略ホットリロード等の重要アラートを送信します。
    """

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        system_webhook_url: Optional[str] = None,
        alert_webhook_url: Optional[str] = None
    ):
        self.report_webhook_url = webhook_url if webhook_url is not None else os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        self.system_webhook_url = system_webhook_url if system_webhook_url is not None else os.environ.get("DISCORD_SYSTEM_WEBHOOK_URL", "").strip() or self.report_webhook_url
        self.alert_webhook_url = alert_webhook_url if alert_webhook_url is not None else os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip() or self.report_webhook_url
        self.webhook_url = self.report_webhook_url  # 互換性

    def _get_target_url(self, target: str = "report") -> str:
        if target == "alert":
            return self.alert_webhook_url
        elif target == "system":
            return self.system_webhook_url
        return self.report_webhook_url

    def is_enabled(self, target: str = "report") -> bool:
        """Webhook URLが設定されており有効かどうか"""
        url = self._get_target_url(target)
        return bool(url and url.startswith("http"))

    def send_message(self, content: str, target: str = "report") -> bool:
        """通常のテキストメッセージを送信"""
        url = self._get_target_url(target)
        if not self.is_enabled(target=target):
            print(f"[DiscordNotifier:{target}] (Webhook未設定のためコンソール表示のみ)\n{content}")
            return False

        payload = {"content": content}
        return self._post_payload(payload, webhook_url=url)

    def send_embed(
        self,
        title: str,
        description: str = "",
        fields: Optional[List[Dict[str, Any]]] = None,
        color: int = 0x3498DB,  # デフォルト: ブルー
        footer_text: str = "GapcorePJ Autonomous Trading System",
        target: str = "report"
    ) -> bool:
        """リッチなEmbed形式でメッセージを送信"""
        url = self._get_target_url(target)
        if not self.is_enabled(target=target):
            try:
                print(f"[DiscordNotifier:{target}] (Webhook未設定 - Embed通知): {title} - {description}")
                if fields:
                    for f in fields:
                        print(f"  - {f.get('name')}: {f.get('value')}")
            except Exception:
                pass
            return False

        embed = {
            "title": title,
            "description": description,
            "color": color,
            "fields": fields or [],
            "footer": {"text": footer_text},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        payload = {"embeds": [embed]}
        return self._post_payload(payload, webhook_url=url)

    def send_system_update(
        self,
        title: str,
        description: str = "",
        fields: Optional[List[Dict[str, Any]]] = None,
        color: int = 0x9B59B6,  # デフォルト: パープル
        footer_text: str = "GapcorePJ System Improvement Engine"
    ) -> bool:
        """システム改善・自己修復・新戦略発見・最適化通知を専用チャンネルへ送信"""
        return self.send_embed(
            title=title,
            description=description,
            fields=fields,
            color=color,
            footer_text=footer_text,
            target="system"
        )

    def send_emergency_alert(
        self,
        title: str,
        message: str,
        fields: Optional[List[Dict[str, Any]]] = None,
        level: str = "critical",
    ) -> bool:
        """システムトラブル、大幅ドローダウン、緊急停止等の異常事態を専用アラートチャンネルへ送信"""
        colors = {
            "warning": 0xF39C12,   # オレンジ
            "critical": 0xE74C3C,  # 赤
            "emergency": 0x960018  # 深紅
        }
        color = colors.get(level.lower(), 0xE74C3C)
        return self.send_embed(
            title=f"🚨 {title}",
            description=message,
            fields=fields,
            color=color,
            footer_text="GapcorePJ Emergency Alert System",
            target="alert"
        )

    def send_hourly_report(
        self,
        strategy_name: str,
        symbol: str,
        timeframe: str,
        current_price: float,
        position_btc: float,
        unrealized_pnl: float,
        realized_pnl: float,
        total_trades: int,
        win_rate_pct: float,
        approved_strategies_count: int = 0,
        pipeline_status: str = "稼働中"
    ) -> bool:
        """
        1時間ごとの定期運用・検証状況レポートをリッチ形式で送信
        """
        total_pnl = unrealized_pnl + realized_pnl
        # 損益に応じたカラー設定 (プラス: 緑, マイナス: 赤, 平穏: 青)
        if total_pnl > 0:
            color = 0x2ECC71  # 緑
            pnl_icon = "🟢"
        elif total_pnl < 0:
            color = 0xE74C3C  # 赤
            pnl_icon = "🔴"
        else:
            color = 0x3498DB  # 青
            pnl_icon = "⚪"

        title = f"📊 【定期レポート】{symbol} 運用＆自律検証ステータス"
        description = f"現在時刻: **{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}**\n稼働環境: **bitFlyer Lightning FX (手数料 0.0%)**"

        fields = [
            {"name": "🤖 稼働中戦略", "value": f"`{strategy_name}` ({timeframe})", "inline": True},
            {"name": "💰 現在価格", "value": f"**{current_price:,.0f} 円**", "inline": True},
            {"name": "📦 保有建玉", "value": f"**{position_btc:+.3f} BTC**", "inline": True},
            {
                "name": f"{pnl_icon} 含み損益",
                "value": f"**{unrealized_pnl:+,.1f} 円**",
                "inline": True
            },
            {
                "name": "💵 累計実現損益",
                "value": f"**{realized_pnl:+,.1f} 円**",
                "inline": True
            },
            {
                "name": "📈 合計トータル損益",
                "value": f"**{total_pnl:+,.1f} 円**",
                "inline": True
            },
            {
                "name": "🎯 実稼働勝率 (決済数)",
                "value": f"**{win_rate_pct:.1f}%** ({total_trades} 回)",
                "inline": True
            },
            {
                "name": "🏆 採択済み戦略数",
                "value": f"**{approved_strategies_count} 件**",
                "inline": True
            },
            {
                "name": "⚙️ パイプライン状態",
                "value": f"`{pipeline_status}`",
                "inline": True
            },
        ]

        return self.send_embed(
            title=title,
            description=description,
            fields=fields,
            color=color,
            footer_text="GapcorePJ 1-Hour Status Report"
        )

    def send_periodic_performance_report(
        self,
        symbol: str,
        current_price: float,
        hourly_stats: Dict[str, Any],
        daily_stats: Dict[str, Any],
        total_stats: Dict[str, Any],
        strategies_stats: List[Dict[str, Any]],
        hour_range: str = "",
        today_str: str = "",
    ) -> bool:
        """
        【24時起点・1時間成績＆累積成績 定期レポート】
        1. 直近1時間の成績 (Hourly PnL / 取引回数 / 勝率)
        2. 24時（00:00 JST）起点の本日の累積成績 (Daily Cumulative PnL / 取引回数 / 勝率)
        3. 全期間トータルの累積成績 (Total PnL / 含み損益 / 総取引回数)
        および各戦略ごとの詳細内訳をDiscord定期報告チャンネルへ送信。
        """
        h_pnl = hourly_stats.get("realized_pnl", 0.0)
        h_trades = hourly_stats.get("trades_count", 0)
        h_wr = hourly_stats.get("win_rate_pct", 0.0)
        h_wins = hourly_stats.get("wins_count", 0)
        h_losses = hourly_stats.get("losses_count", 0)

        d_pnl = daily_stats.get("realized_pnl", 0.0)
        d_trades = daily_stats.get("trades_count", 0)
        d_wr = daily_stats.get("win_rate_pct", 0.0)
        d_wins = daily_stats.get("wins_count", 0)
        d_losses = daily_stats.get("losses_count", 0)

        tot_pnl = total_stats.get("total_pnl", 0.0)
        tot_realized = total_stats.get("realized_pnl", 0.0)
        tot_unrealized = total_stats.get("unrealized_pnl", 0.0)
        tot_trades = total_stats.get("trades_count", 0)
        tot_wr = total_stats.get("win_rate_pct", 0.0)

        # 損益に応じたカラー設定
        if h_pnl > 0 or d_pnl > 0:
            color = 0x2ECC71  # 緑
            status_badge = "🟢 好調推移"
        elif h_pnl < 0 or d_pnl < 0:
            color = 0xE74C3C  # 赤
            status_badge = "🔴 調整/ドローダウン警戒"
        else:
            color = 0x3498DB  # 青
            status_badge = "⚪ 平穏推移"

        from datetime import timezone, timedelta
        jst = timezone(timedelta(hours=9))
        now_jst = datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")

        title = f"📊 【定期運用成績レポート】{symbol} ({status_badge})"
        description = (
            f"現在時刻: **{now_jst} JST**\n"
            f"対象市場: **bitFlyer Lightning FX (手数料 0.0%)** | 現在価格: **{current_price:,.0f} 円**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        h_icon = "🟢" if h_pnl > 0 else ("🔴" if h_pnl < 0 else "⚪")
        d_icon = "🟢" if d_pnl > 0 else ("🔴" if d_pnl < 0 else "⚪")
        t_icon = "🟢" if tot_pnl > 0 else ("🔴" if tot_pnl < 0 else "⚪")

        fields = [
            {
                "name": f"⏱️ 直近1時間の成績 ({hour_range})",
                "value": (
                    f"• 確定損益: {h_icon} **{h_pnl:+,.1f} 円**\n"
                    f"• 取引回数: **{h_trades} 回** (勝率: **{h_wr:.1f}%** | {h_wins}勝 {h_losses}敗)"
                ),
                "inline": False,
            },
            {
                "name": f"📅 本日累積成績 (24:00 JST起点 / {today_str})",
                "value": (
                    f"• 確定損益: {d_icon} **{d_pnl:+,.1f} 円**\n"
                    f"• 取引回数: **{d_trades} 回** (勝率: **{d_wr:.1f}%** | {d_wins}勝 {d_losses}敗)"
                ),
                "inline": False,
            },
            {
                "name": "📈 全期間トータル成績 (通算)",
                "value": (
                    f"• 合計損益: {t_icon} **{tot_pnl:+,.1f} 円** (確定: {tot_realized:+,.1f} / 含み: {tot_unrealized:+,.1f})\n"
                    f"• 総取引回数: **{tot_trades} 回** (通算勝率: **{tot_wr:.1f}%**)"
                ),
                "inline": False,
            },
        ]

        # 各戦略ごとの詳細内訳
        for s in strategies_stats:
            s_name = s.get("name", "Unknown")
            pos = s.get("position", 0.0)
            u_pnl = s.get("unrealized_pnl", 0.0)

            s_h = s.get("hourly", {})
            s_h_pnl = s_h.get("realized_pnl", 0.0)
            s_h_cnt = s_h.get("trades_count", 0)

            s_d = s.get("daily", {})
            s_d_pnl = s_d.get("realized_pnl", 0.0)
            s_d_cnt = s_d.get("trades_count", 0)
            s_d_wr = s_d.get("win_rate_pct", 0.0)

            val_text = (
                f"• 直近1h: **{s_h_pnl:+,.1f} 円** ({s_h_cnt}回)\n"
                f"• 本日累計: **{s_d_pnl:+,.1f} 円** ({s_d_cnt}回, 勝率{s_d_wr:.1f}%)\n"
                f"• 建玉: **{pos:+.3f} BTC** (含み: {u_pnl:+,.1f} 円)"
            )
            fields.append({
                "name": f"🤖 {s_name}",
                "value": val_text,
                "inline": True
            })

        return self.send_embed(
            title=title,
            description=description,
            fields=fields,
            color=color,
            footer_text="GapcorePJ 24h-Daily & 1-Hour Performance System",
            target="report"
        )

    def send_multi_strategy_report(
        self,
        strategies_status: List[Dict[str, Any]],
        symbol: str = "FX_BTC_JPY",
        current_price: float = 0.0,
        total_realized_pnl: float = 0.0,
        total_unrealized_pnl: float = 0.0,
        hourly_stats: Optional[Dict[str, Any]] = None,
        daily_stats: Optional[Dict[str, Any]] = None,
        total_stats: Optional[Dict[str, Any]] = None,
        hour_range: str = "",
        today_str: str = ""
    ) -> bool:
        """
        並行稼働中の全戦略の損益・ポジション・勝率を一括でDiscord通知。
        hourly_stats, daily_stats が指定されている場合は24時起点・1時間成績レポートとして送信。
        """
        if hourly_stats and daily_stats and total_stats:
            return self.send_periodic_performance_report(
                symbol=symbol,
                current_price=current_price,
                hourly_stats=hourly_stats,
                daily_stats=daily_stats,
                total_stats=total_stats,
                strategies_stats=strategies_status,
                hour_range=hour_range,
                today_str=today_str
            )

        total_pnl = total_realized_pnl + total_unrealized_pnl
        color = 0x2ECC71 if total_pnl > 0 else (0xE74C3C if total_pnl < 0 else 0x3498DB)

        title = f"📊 【並行稼働ポートフォリオ】{symbol} 全戦略ステータス"
        description = (
            f"現在時刻: **{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}**\n"
            f"対象市場: **bitFlyer Lightning FX (手数料 0.0%)** | 現在価格: **{current_price:,.0f} 円**\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"**全体合計損益**: **{total_pnl:+,.1f} 円** (含み: {total_unrealized_pnl:+,.1f} 円 / 確定: {total_realized_pnl:+,.1f} 円)"
        )

        fields = []
        for s in strategies_status:
            strat_name = s.get("name", "Unknown")
            s_type = s.get("strategy_type", "trend_following")
            pos = s.get("position", 0.0)
            u_pnl = s.get("unrealized_pnl", 0.0)
            r_pnl = s.get("realized_pnl", 0.0)
            sig = s.get("signal_name", "中立")
            trades = s.get("trades_count", 0)
            win_rate = s.get("win_rate_pct", 0.0)

            type_badge = {
                "market_making": "⚡ MM(高回転)",
                "trend_following": "🌊 トレンド(長期保有)",
                "mean_reversion": "🎯 平均回帰(中頻度)"
            }.get(s_type, "🤖 一般")

            status_text = (
                f"• タイプ: **{type_badge}**\n"
                f"• 取引回数: **{trades} 回** (勝率: {win_rate:.1f}%)\n"
                f"• シグナル: **{sig}** | 建玉: **{pos:+.3f} BTC**\n"
                f"• 損益: **{u_pnl + r_pnl:+,.1f} 円** (確定: {r_pnl:+,.1f} / 含み: {u_pnl:+,.1f})"
            )
            fields.append({
                "name": f"{strat_name}",
                "value": status_text,
                "inline": True
            })

        return self.send_embed(
            title=title,
            description=description,
            fields=fields,
            color=color,
            footer_text="GapcorePJ Multi-Strategy Portfolio"
        )

    def send_pipeline_discovery_summary(
        self,
        results_summary: List[Dict[str, Any]],
        symbol: str = "FX_BTC_JPY",
        timeframe: str = "1m"
    ) -> bool:
        """
        全戦略の自律改善・ガバナンス検証結果一覧をDiscordに送信
        """
        title = f"🔬 【自律発見パイプライン】全戦略検証・改善サマリー"
        description = (
            f"検証市場: **bitFlyer Lightning FX ({symbol})** | 時間足: **{timeframe}**\n"
            f"手数料: **0.0% (無料)** | スリッページ: **0.0%**\n"
            f"━━━━━━━━━━━━━━━━━━━━━"
        )

        fields = []
        for res in results_summary:
            theme = res.get("theme", "戦略")
            name = res.get("name", "CustomStrategy")
            status = res.get("status", "REVISE")
            trades = res.get("trades", 0)
            wr = res.get("win_rate", 0.0)
            pf = res.get("pf", 0.0)
            mdd = res.get("mdd", 0.0)
            sharpe = res.get("sharpe", 0.0)
            reason = res.get("reason", "")

            badge = "🏆 [APPROVED]" if status in ["PASS", "APPROVED"] else ("⚠️ [REVISE]" if status == "REVISE" else "❌ [REJECTED]")

            value_text = (
                f"**判定**: {badge}\n"
                f"• 取引数: **{trades}回** | 勝率: **{wr:.1f}%**\n"
                f"• PF: **{pf:.2f}** | MDD: **{mdd:.2f}%** | Sharpe: **{sharpe:.2f}**"
            )
            if reason:
                value_text += f"\n• 備考: *{reason[:60]}*"

            fields.append({
                "name": f"📈 {theme}",
                "value": value_text,
                "inline": False
            })

        return self.send_embed(
            title=title,
            description=description,
            fields=fields,
            color=0x9B59B6,
            footer_text="GapcorePJ Autonomous Discovery Pipeline",
            target="system"
        )

    def send_alert(self, title: str, message: str, level: str = "warning", target: Optional[str] = None) -> bool:
        """アラート（ドローダウン検知、ホットリロード、エラー等）を送信"""
        if target is None:
            target = "alert" if level.lower() == "critical" else "report"

        colors = {
            "info": 0x3498DB,     # 青
            "warning": 0xF39C12,  # オレンジ
            "critical": 0xE74C3C, # 赤
            "success": 0x2ECC71,  # 緑
        }
        color = colors.get(level.lower(), 0xF39C12)
        icons = {
            "info": "ℹ️",
            "warning": "⚠️",
            "critical": "🚨",
            "success": "🎉"
        }
        icon = icons.get(level.lower(), "🔔")

        return self.send_embed(
            title=f"{icon} {title}",
            description=message,
            color=color,
            footer_text="GapcorePJ Live Alert",
            target=target
        )

    def _post_payload(self, payload: Dict[str, Any], webhook_url: Optional[str] = None) -> bool:
        """Webhook URLへJSONペイロードをPOST"""
        url = webhook_url or self.report_webhook_url
        if not url:
            return False
        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (GapcorePJ AlgoTrader Notifier)"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status in [200, 204]
        except Exception as e:
            print(f"[DiscordNotifier] ⚠️ Discord通知送信失敗: {e}")
            return False
