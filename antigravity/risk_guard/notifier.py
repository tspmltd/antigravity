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
    Discord Webhook 通知エンジン。
    3系統の通知チャンネル（定期レポート、システム改善・自己進化、緊急アラート・サーキットブレーカー）に対応。
    """

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        system_webhook_url: Optional[str] = None,
        alert_webhook_url: Optional[str] = None,
    ):
        self.report_webhook_url = webhook_url if webhook_url is not None else os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        self.system_webhook_url = system_webhook_url if system_webhook_url is not None else os.environ.get("DISCORD_SYSTEM_WEBHOOK_URL", "").strip() or self.report_webhook_url
        self.alert_webhook_url = alert_webhook_url if alert_webhook_url is not None else os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip() or self.report_webhook_url

    def _get_target_url(self, target: str = "report") -> str:
        if target == "alert":
            return self.alert_webhook_url
        elif target == "system":
            return self.system_webhook_url
        return self.report_webhook_url

    def is_enabled(self, target: str = "report") -> bool:
        url = self._get_target_url(target)
        return bool(url and url.startswith("http"))

    def _post_payload(self, payload: Dict[str, Any], webhook_url: str) -> bool:
        if not webhook_url or not webhook_url.startswith("http"):
            return False
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Antigravity/1.0 (Discord Webhook Client)",
        }
        try:
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 204)
        except Exception as ex:
            print(f"[DiscordNotifier] POST failure ({ex})", flush=True)
            return False

    def send_embed(
        self,
        title: str,
        description: str = "",
        fields: Optional[List[Dict[str, Any]]] = None,
        color: int = 0x3498DB,
        footer_text: str = "Antigravity Autonomous HFT Engine",
        target: str = "report",
    ) -> bool:
        url = self._get_target_url(target)
        if not self.is_enabled(target=target):
            return False

        embed = {
            "title": title,
            "description": description,
            "color": color,
            "fields": fields or [],
            "footer": {"text": footer_text},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self._post_payload({"embeds": [embed]}, webhook_url=url)

    def send_regular_report(self, snapshot: Dict[str, Any], symbol: str = "FX_BTC_JPY") -> bool:
        """24時起点・1時間成績を定期レポートチャンネルへ送信"""
        overall = snapshot.get("overall", {})
        hourly = overall.get("hourly", {})
        daily = overall.get("daily_cumulative", {})
        total = overall.get("total_cumulative", {})
        strats = snapshot.get("strategies", [])
        jst_time = snapshot.get("jst_time_str", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        fields = [
            {
                "name": "⏱ 直近1時間の成績",
                "value": (
                    f"• **損益**: `{hourly.get('realized_pnl', 0.0):+,.1f} 円`\n"
                    f"• **約定回数**: `{hourly.get('trades_count', 0)} 回` "
                    f"(勝: {hourly.get('wins_count', 0)} / 負: {hourly.get('losses_count', 0)})\n"
                    f"• **勝率**: `{hourly.get('win_rate_pct', 0.0):.1f}%`"
                ),
                "inline": False,
            },
            {
                "name": "📅 本日累積成績 (24:00起点)",
                "value": (
                    f"• **確定損益**: `{daily.get('realized_pnl', 0.0):+,.1f} 円`\n"
                    f"• **約定回数**: `{daily.get('trades_count', 0)} 回` "
                    f"(勝: {daily.get('wins_count', 0)} / 負: {daily.get('losses_count', 0)})\n"
                    f"• **勝率**: `{daily.get('win_rate_pct', 0.0):.1f}%`"
                ),
                "inline": False,
            },
            {
                "name": "📈 全期間トータル成績",
                "value": (
                    f"• **累計損益**: `{total.get('realized_pnl', 0.0):+,.1f} 円`\n"
                    f"• **評価損益**: `{overall.get('unrealized_pnl', 0.0):+,.1f} 円`\n"
                    f"• **総純損益**: `{overall.get('net_profit_total', 0.0):+,.1f} 円`\n"
                    f"• **建玉**: `{overall.get('position_btc', 0.0):+.4f} BTC`"
                ),
                "inline": False,
            },
        ]

        strat_lines = []
        for s in strats:
            s_name = s.get("name", "")
            s_d_pnl = s.get("daily", {}).get("realized_pnl", 0.0)
            s_d_cnt = s.get("daily", {}).get("trades_count", 0)
            s_d_wr = s.get("daily", {}).get("win_rate_pct", 0.0)
            s_h_pnl = s.get("hourly", {}).get("realized_pnl", 0.0)
            strat_lines.append(
                f"• **{s_name}**: 本日 `{s_d_pnl:+,.1f}円` ({s_d_cnt}回, 勝率{s_d_wr:.0f}%) | 1H `{s_h_pnl:+,.1f}円`"
            )

        if strat_lines:
            fields.append({
                "name": "🤖 戦略別ブレークダウン",
                "value": "\n".join(strat_lines),
                "inline": False,
            })

        color = 0x2ECC71 if overall.get("net_profit_daily", 0.0) >= 0 else 0xE74C3C
        return self.send_embed(
            title=f"📊 【定期運用レポート】{symbol}",
            description=f"**集計時刻**: `{jst_time}`",
            fields=fields,
            color=color,
            target="report",
        )

    def send_system_update(self, title: str, description: str = "", fields: Optional[List[Dict[str, Any]]] = None) -> bool:
        """自己進化・パラメータ最適化・新戦略導入などの改善通知"""
        return self.send_embed(
            title=title,
            description=description,
            fields=fields,
            color=0x9B59B6,
            footer_text="Antigravity System Evolution",
            target="system",
        )

    def send_emergency_alert(
        self,
        title: str,
        message: str,
        fields: Optional[List[Dict[str, Any]]] = None,
        level: str = "critical",
    ) -> bool:
        """サーキットブレーカー発動、ドローダウン超過等の異常事態通報"""
        color = 0xE74C3C if level == "critical" else 0xF39C12
        return self.send_embed(
            title=title,
            description=message,
            fields=fields,
            color=color,
            footer_text="Antigravity Risk Guard 🚨",
            target="alert",
        )

    def send_system_resource_report(
        self,
        metrics: Dict[str, Any],
        remediation_actions: Optional[List[str]] = None,
        server_name: str = "Trading Server",
    ) -> bool:
        """
        DISK / MEMORY / CPU の稼働状況と改善策をDiscordのアラートチャンネルへ送信。
        """
        disk = metrics.get("disk", {})
        mem = metrics.get("memory", {})
        cpu = metrics.get("cpu_pct", 0.0)
        proc_mem = metrics.get("process_rss_mb", 0.0)
        is_warn = metrics.get("is_warning", False)
        is_crit = metrics.get("is_critical", False)

        if is_crit:
            title = f"🚨 【システムリソース緊急警告】{server_name} 負荷逼迫"
            color = 0xE74C3C  # 赤
        elif is_warn:
            title = f"⚠️ 【システムリソース注意報】{server_name} 高負荷検知"
            color = 0xF39C12  # オレンジ
        else:
            title = f"🖥️ 【システムリソース定時診断】{server_name} 健全性レポート"
            color = 0x2ECC71  # 緑

        disk_text = (
            f"• **使用率**: `{disk.get('used_pct', 0.0):.1f}%`\n"
            f"• **空き容量**: `{disk.get('free_gb', 0.0):.1f} GB` / `{disk.get('total_gb', 0.0):.1f} GB`"
        )
        mem_text = (
            f"• **使用率**: `{mem.get('used_pct', 0.0):.1f}%`\n"
            f"• **使用中**: `{mem.get('used_gb', 0.0):.1f} GB` (空き: `{mem.get('free_gb', 0.0):.1f} GB` / 総量: `{mem.get('total_gb', 0.0):.1f} GB`)"
        )
        cpu_text = (
            f"• **CPU使用率**: `{cpu:.1f}%`\n"
            f"• **プロセス消費**: `{proc_mem:.1f} MB`"
        )

        actions_text = "\n".join([f"• {a}" for a in (remediation_actions or ["自己修復・最適化処理実施済み"])])

        fields = [
            {"name": "💾 DISK ストレージ", "value": disk_text, "inline": True},
            {"name": "🧠 MEMORY メモリ", "value": mem_text, "inline": True},
            {"name": "⚡ CPU ＆ プロセス", "value": cpu_text, "inline": False},
            {"name": "🛠️ 実行された改善策・自動修復アクション", "value": actions_text, "inline": False},
        ]

        return self.send_embed(
            title=title,
            description=f"**診断時刻**: `{metrics.get('iso_time', '')}` | 監視周期: **1時間ごと**",
            fields=fields,
            color=color,
            footer_text="Antigravity System Resource Guard 🛡️",
            target="alert",
        )

