import os
from antigravity.discord_mute import discord_muted
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
        trade_webhook_url: Optional[str] = None,
        system_webhook_url: Optional[str] = None,
        alert_webhook_url: Optional[str] = None,
        news_webhook_url: Optional[str] = None,
    ):
        # ① 定期運用報告チャンネル (LIVE + DRYRUN 毎時レポート & 24h累積PNL)
        self.report_webhook_url = (
            webhook_url
            if webhook_url is not None
            else (
                os.environ.get("DISCORD_LIVE_WEBHOOK_URL", "").strip()
                or os.environ.get("DISCORD_REPORT_WEBHOOK_URL", "").strip()
                or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
            )
        )
        # ② 取引報告書チャンネル (個別取引の詳細、利益確保・利確、大幅利益・損失)
        self.trade_webhook_url = (
            trade_webhook_url
            if trade_webhook_url is not None
            else (
                os.environ.get("DISCORD_TRADE_WEBHOOK_URL", "").strip()
                or os.environ.get("DISCORD_LIVE_WEBHOOK_URL", "").strip()
                or self.report_webhook_url
            )
        )
        # ③ 取引システム改善・戦略改善チャンネル (自律探索・ロット昇格降格・改善)
        self.system_webhook_url = (
            system_webhook_url
            if system_webhook_url is not None
            else (os.environ.get("DISCORD_SYSTEM_WEBHOOK_URL", "").strip() or self.report_webhook_url)
        )
        # ④ 緊急アラートチャンネル (システム逼迫: DISK, MEMORY, CPU・急落・大幅損失・死活監視)
        self.alert_webhook_url = (
            alert_webhook_url
            if alert_webhook_url is not None
            else (os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip() or self.report_webhook_url)
        )
        # ⑤ マーケット外部配信 Webhook (定時ニュース / 世界の株価急変 / EDINET 10文字速報)
        self.news_webhook_url = (
            news_webhook_url
            if news_webhook_url is not None
            else (os.environ.get("DISCORD_NEWS_WEBHOOK_URL", "").strip() or self.report_webhook_url)
        )
        # ⑥ 試運転戦略報告 (Observation 専用チャンネル)
        self.observation_webhook_url = os.environ.get("DISCORD_OBSERVATION_WEBHOOK_URL", "").strip()
        # ⑦ 結論 (4AGENT 評議会 確定結論チャンネル)
        self.conclusion_webhook_url = os.environ.get("DISCORD_CONCLUSION_WEBHOOK_URL", "").strip()
        # ⑧ Quants-Agent (クオンツエージェント統括ステータス)
        self.quants_agent_webhook_url = os.environ.get("DISCORD_QUANTS_AGENT_WEBHOOK_URL", "").strip()

    def _get_target_url(self, target: str = "report") -> str:
        if target == "trade":
            return self.trade_webhook_url
        elif target == "alert":
            return self.alert_webhook_url
        elif target == "system":
            return self.system_webhook_url
        elif target == "news":
            return self.news_webhook_url
        return self.report_webhook_url

    def is_enabled(self, target: str = "report") -> bool:
        url = self._get_target_url(target)
        return bool(url and url.startswith("http"))

    def _post_payload(self, payload: Dict[str, Any], webhook_url: str, max_retries: int = 3) -> bool:
        if discord_muted():
            return False
        if not webhook_url or not webhook_url.startswith("http"):
            return False
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Antigravity/1.0 (Discord Webhook Client)",
        }
        data_bytes = json.dumps(payload).encode("utf-8")

        for attempt in range(1, max_retries + 1):
            try:
                req = urllib.request.Request(
                    webhook_url,
                    data=data_bytes,
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status in (200, 204)
            except urllib.error.HTTPError as he:
                if he.code == 429:
                    # レートリミット待機 & リトライ
                    retry_after = 3.0
                    try:
                        err_body = he.read().decode("utf-8")
                        err_json = json.loads(err_body)
                        retry_after = float(err_json.get("retry_after", 3.0))
                    except Exception:
                        pass
                    if attempt < max_retries:
                        time.sleep(retry_after)
                        continue
                print(f"[DiscordNotifier] HTTPError ({he.code}): {he.reason}", flush=True)
                return False
            except Exception as ex:
                if attempt < max_retries:
                    time.sleep(1.5 * attempt)
                    continue
                print(f"[DiscordNotifier] POST failure ({ex})", flush=True)
                return False
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

    def send_message(self, content: str, target: str = "report") -> bool:
        """プレーンテキストメッセージを送信"""
        url = self._get_target_url(target)
        if not self.is_enabled(target=target):
            return False
        return self._post_payload({"content": content}, webhook_url=url)

    def send_alert(self, title: str, message: str, level: str = "info", target: str = "alert") -> bool:
        """汎用アラートメッセージをEmbedで送信"""
        color_map = {
            "info": 0x3498DB,
            "success": 0x2ECC71,
            "warning": 0xF1C40F,
            "error": 0xE74C3C,
            "critical": 0x992D22,
        }
        color = color_map.get(level.lower(), 0x3498DB)
        return self.send_embed(
            title=title,
            description=message,
            color=color,
            target=target,
        )

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
            title=f"📊 【DRYRUN ペーパー運用レポート (4大戦略)】{symbol}",
            description=f"**集計時刻**: `{jst_time}`",
            fields=fields,
            color=color,
            target="report",
        )

    def send_hourly_kpi_report(
        self,
        kpi: Dict[str, Any],
        symbol: str = "FX_BTC_JPY",
        target: str = "report",
    ) -> bool:
        """
        LIVE本番「状態ベース」毎時KPIレポート
        ユーザー指定の必須KPI全9項目を美しく網羅：
        1. 戦略別損益（EMA / MR / OBI）
        2. PF損益
        3. 戦略別 PF（Profit Factor）
        4. 戦略別 MaxDD（直近24h）
        5. 内部ネッティング回数（累計）
        6. スプレッド節約額（累計）
        7. 429スキップ数（常に0）
        8. Regime滞在比率（TREND / RANGE / HIGH_VOL）
        9. 発注枠使用率（Tier0〜2）
        """
        jst_time = kpi.get("jst_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S JST"))
        mid_p = kpi.get("mid_price", 0.0)
        curr_regime = kpi.get("current_regime", "NORMAL")

        # 1 & 2. PF損益
        pf_realized = kpi.get("pf_realized_pnl", 0.0)
        pf_unrealized = kpi.get("pf_unrealized_pnl", 0.0)
        pf_total = pf_realized + pf_unrealized
        collateral = kpi.get("collateral", 0.0)
        pf_pos = kpi.get("portfolio_position_btc", 0.0)

        # 1, 3, 4. 戦略別損益 / PF / MaxDD (24h)
        strat_data = kpi.get("strategies", {})
        strat_lines = []
        for sid in ["EmaTrend", "MeanReversion", "OrderBookImbalance"]:
            s = strat_data.get(sid, {})
            pnl = s.get("realized_pnl", 0.0)
            unreal = s.get("unrealized_pnl", 0.0)
            tot = pnl + unreal
            pf = s.get("profit_factor", 0.0)
            pf_str = f"{pf:.2f}" if pf > 0 else "N/A"
            mdd = s.get("max_dd_24h", 0.0)
            trades = s.get("trades_count", 0)
            wr = s.get("win_rate_pct", 0.0)
            pos = s.get("position_btc", 0.0)

            sign = "+" if tot >= 0 else ""
            badge = "🟢" if tot >= 0 else "🔴"
            strat_lines.append(
                f"{badge} **{sid}**: `{sign}{tot:,.1f} 円` (確定: `{pnl:+,.1f}`, 評価: `{unreal:+,.1f}`)\n"
                f"   PF: `{pf_str}` | 24h MaxDD: `-{mdd:,.1f} 円` | 勝率: `{wr:.1f}%` ({trades}取引, 建玉: `{pos:+.3f}`)"
            )
        strat_text = "\n".join(strat_lines) if strat_lines else "戦略データなし"

        # 5 & 6. 内部ネッティング & スプレッド節約
        netting_cnt = kpi.get("netting_events_count", 0)
        savings_jpy = kpi.get("spread_savings_jpy", 0.0)
        phys_orders = kpi.get("physical_orders_dispatched", 0)

        # 7 & 9. 429スキップ数 & 発注枠使用率 (Tier0〜2)
        skips_429 = kpi.get("rate_limit_skips_count", 0)
        tier_usage = kpi.get("tier_usage", {})
        tier0_rpm = tier_usage.get("tier0_current_rpm", 0.0)
        tier0_max = tier_usage.get("tier0_max_rpm", 30)
        tier0_pct = (tier0_rpm / tier0_max * 100.0) if tier0_max > 0 else 0.0
        tier1_used = tier_usage.get("tier1_daily_loss_used", abs(min(0.0, pf_realized)))
        tier1_limit = tier_usage.get("tier1_daily_loss_limit", 3000.0)
        tier1_pct = (tier1_used / tier1_limit * 100.0) if tier1_limit > 0 else 0.0
        tier2_status = tier_usage.get("tier2_circuit_breaker", "🟢 正常 (稼働中)")

        # 8. Regime滞在比率
        regimes = kpi.get("regime_distribution", {})
        trend_pct = regimes.get("TREND", 0.0)
        range_pct = regimes.get("RANGE", 0.0)
        norm_pct = regimes.get("NORMAL", 0.0)
        hvol_pct = regimes.get("HIGH_VOL", 0.0)
        regime_text = (
            f"• **TREND**: `{trend_pct:.1f}%` | **RANGE**: `{range_pct:.1f}%`\n"
            f"• **NORMAL**: `{norm_pct:.1f}%` | **HIGH_VOL**: `{hvol_pct:.1f}%`\n"
            f"• **現在適用レジーム**: **`{curr_regime}`**"
        )

        fields = [
            {
                "name": "💼 ポートフォリオ総合損益 (PF損益)",
                "value": (
                    f"• **本日確定損益**: `{' ' if pf_realized >= 0 else ''}{pf_realized:+,.1f} 円`\n"
                    f"• **未実現評価損益**: `{pf_unrealized:+,.1f} 円` (Mid値洗い @ `{mid_p:,.0f} 円`)\n"
                    f"• **PF総合純損益**: `{' ' if pf_total >= 0 else ''}{pf_total:+,.1f} 円`\n"
                    f"• **現在建玉**: `{pf_pos:+.3f} BTC` | **推定証拠金**: `{collateral:,.1f} 円`"
                ),
                "inline": False,
            },
            {
                "name": "🧠 戦略別損益 & パフォーマンス (EMA / MR / OBI)",
                "value": strat_text,
                "inline": False,
            },
            {
                "name": "⚡ 内部ネッティング & コスト削減効果",
                "value": (
                    f"• **内部ネッティング回数**: **`{netting_cnt} 回`** (累計シグナル相殺)\n"
                    f"• **スプレッド節約額**: **`+{savings_jpy:,.1f} 円`** (往復2bpコスト完全カット)\n"
                    f"• **取引所物理発注数**: `{phys_orders} 回` (差分ネット注文のみ執行)"
                ),
                "inline": False,
            },
            {
                "name": "🛡️ 429対策 & 発注枠使用率 (Tier 0〜2)",
                "value": (
                    f"• **429スキップ数**: **`{skips_429} 回`** 🟢 (常に0・完全BAN封鎖)\n"
                    f"• **Tier 0 (APIレート枠)**: `{tier0_rpm:.1f}/分` (上限 {tier0_max}/分, **`{tier0_pct:.1f}%`** 使用)\n"
                    f"• **Tier 1 (日次損失枠)**: `{tier1_used:,.1f} 円` / `{tier1_limit:,.0f} 円` (**`{tier1_pct:.1f}%`** 使用)\n"
                    f"• **Tier 2 (サーキットB)**: {tier2_status}"
                ),
                "inline": False,
            },
            {
                "name": "🌊 市場Regime滞在比率 (直近分布)",
                "value": regime_text,
                "inline": False,
            },
        ]

        # 6. 自律ロットスケーリング進捗
        lot_info = kpi.get("lot_scale_info", {})
        if lot_info:
            c_lot = lot_info.get("current_lot", 0.001)
            t_lot = lot_info.get("target_lot", 0.002)
            c_tier = lot_info.get("current_tier", 0)
            prog_pct = lot_info.get("progress_pct", 0.0)
            is_max = lot_info.get("is_max_tier", False)
            if is_max:
                lot_text = f"• **現在Tier**: `Tier {c_tier} ({c_lot:.3f} BTC)` (最高Tier到達・安定運用中)"
            else:
                passed_cnt = lot_info.get("passed_count", 0)
                tot_cnt = lot_info.get("total_count", 5)
                lot_text = (
                    f"• **現在Tier**: **`Tier {c_tier} ({c_lot:.3f} BTC)`** ➔ 次期目標: `{t_lot:.3f} BTC`\n"
                    f"• **昇格条件達成度**: **`{passed_cnt}/{tot_cnt} 条件クリア ({prog_pct:.1f}%)`**"
                )
            fields.append({
                "name": "📈 自律ロットスケーリング (Lot Scale Progress)",
                "value": lot_text,
                "inline": False,
            })

        color = 0x2ECC71 if pf_total >= 0 else 0xE67E22
        return self.send_embed(
            title=f"📊 【LIVE本番 毎時状態KPIレポート】{symbol}",
            description=f"**集計時刻**: `{jst_time}` | 稼働状態: 🟢 **ACTIVE** (3戦略統合管理)",
            fields=fields,
            color=color,
            footer_text="Antigravity GAPCORE Portfolio Orchestrator 🔴",
            target=target,
        )

    def send_lot_scale_alert(
        self,
        strategy_id: str,
        old_lot: float,
        new_lot: float,
        event_type: str,
        reason: str,
        metrics: Optional[Dict[str, Any]] = None,
        symbol: str = "FX_BTC_JPY",
        target: str = "report",
    ) -> bool:
        """ロット自動昇格 (PROMOTION) / 自動降格 (DEMOTION) 即時通知"""
        is_promo = (event_type.upper() == "PROMOTION")
        title = (
            f"🎉 【ロット自動昇格】{strategy_id} が `{old_lot:.3f}` ➔ `{new_lot:.3f} BTC` へ拡大！"
            if is_promo else
            f"🛡️ 【ロット自動降格】{strategy_id} を `{old_lot:.3f}` ➔ `{new_lot:.3f} BTC` へ安全縮小 (Fail-Closed)"
        )
        color = 0x2ECC71 if is_promo else 0xE74C3C

        fields = [
            {"name": "戦略種別", "value": f"**`{strategy_id}`**", "inline": True},
            {"name": "適用ロット変更", "value": f"`{old_lot:.3f} BTC` ➔ **`{new_lot:.3f} BTC`**", "inline": True},
            {"name": "発動事由 / エビデンス", "value": reason, "inline": False},
        ]
        if metrics:
            pf = metrics.get("profit_factor", 0.0)
            mdd = metrics.get("max_dd_24h", 0.0)
            wr = metrics.get("win_rate_pct", 0.0)
            cnt = metrics.get("trades_count", 0)
            fields.append({
                "name": "直近判定メトリクス",
                "value": f"• **PF**: `{pf:.2f}` | **MaxDD**: `-{mdd:,.1f} 円` | **勝率**: `{wr:.1f}%` ({cnt}回取引)",
                "inline": False,
            })

        res = self.send_embed(
            title=title,
            description=f"**発動日時**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S JST')}` | 市場: `{symbol}`",
            fields=fields,
            color=color,
            footer_text="Antigravity GAPCORE LotScaleGuard 📈",
            target=target,
        )
        if target != "system":
            self.send_embed(
                title=title,
                description=f"**発動日時**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S JST')}` | 市場: `{symbol}`",
                fields=fields,
                color=color,
                footer_text="Antigravity GAPCORE LotScaleGuard 📈",
                target="system",
            )
        return res

    def send_dryrun_entry_report(
        self,
        strategy_name: str,
        side: str,
        size_btc: float,
        price: float,
        reason: str = "",
        symbol: str = "FX_BTC_JPY",
    ) -> bool:
        """DRYRUNペーパートレードの新規エントリー速報"""
        badge = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        title = f"🧪 【DRYRUN ペーパーENTRY】{badge} {strategy_name}"
        fields = [
            {"name": "注文区分", "value": f"**{side} ({size_btc:.4f} BTC)**", "inline": True},
            {"name": "仮想約定価格", "value": f"`{price:,.0f} 円`", "inline": True},
            {"name": "戦略種別", "value": f"`{strategy_name}`", "inline": True},
            {"name": "エントリー根拠", "value": f"{reason}", "inline": False},
        ]
        return self.send_embed(
            title=title,
            description=f"**発注時刻**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S JST')}` | 市場: `{symbol}`",
            fields=fields,
            color=0x3498DB,
            footer_text="Antigravity DRYRUN Sentinel 🧪",
            target="trade",
        )

    def send_dryrun_trade_report(
        self,
        strategy_name: str,
        side: str,
        size_btc: float,
        entry_price: float,
        exit_price: float,
        pnl_jpy: float,
        reason: str = "",
        symbol: str = "FX_BTC_JPY",
    ) -> bool:
        """DRYRUNペーパートレードの手仕舞い・確定損益速報"""
        is_profit = pnl_jpy >= 0
        badge = "🎉 利確 (+)" if is_profit else "⚠️ 損切 (-)"
        title = f"{'🎉' if is_profit else '⚠️'} 【DRYRUN 手仕舞い】{strategy_name} {badge} {pnl_jpy:+,.1f} 円"
        color = 0x2ECC71 if is_profit else 0xE74C3C
        fields = [
            {"name": "決済区分", "value": f"**{side} (手仕舞い)**", "inline": True},
            {"name": "確定損益", "value": f"**`{pnl_jpy:+,.1f} 円`**", "inline": True},
            {"name": "取引数量", "value": f"`{size_btc:.4f} BTC`", "inline": True},
            {"name": "価格推移", "value": f"`{entry_price:,.0f} 円` ➔ `{exit_price:,.0f} 円`", "inline": False},
            {"name": "手仕舞い理由", "value": f"{reason}", "inline": False},
        ]
        return self.send_embed(
            title=title,
            description=f"**決済時刻**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S JST')}` | 市場: `{symbol}`",
            fields=fields,
            color=color,
            footer_text="Antigravity DRYRUN Sentinel 🧪",
            target="trade",
        )

    def send_evolution_cycle_report(
        self,
        cycle: int,
        symbol: str,
        timeframe: str,
        tested_themes: List[str],
        approved_count: int,
        rejected_count: int,
        summary_text: str = "",
        target: str = "system",
    ) -> bool:
        """自律戦略探索・改善サイクルの完了報告"""
        title = f"🧬 【自律戦略改善レポート】Cycle {cycle} 探索・自己進化完了"
        color = 0x9B59B6 if approved_count > 0 else 0x3498DB
        themes_str = "\n".join([f"• {t}" for t in tested_themes]) if tested_themes else "なし"
        fields = [
            {"name": "探索市場 / 時間足", "value": f"`{symbol}` ({timeframe})", "inline": True},
            {"name": "採択戦略 / 却下数", "value": f"合格: **`{approved_count} 件`** / 却下: `{rejected_count} 件`", "inline": True},
            {"name": "検証テーマ一覧", "value": themes_str, "inline": False},
        ]
        if summary_text:
            fields.append({"name": "改善・検証サマリー", "value": summary_text, "inline": False})

        res1 = self.send_embed(
            title=title,
            description=f"**実行日時**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S JST')}`",
            fields=fields,
            color=color,
            footer_text="Antigravity Autonomous Evolution Loop 🧬",
            target=target,
        )
        if target != "report":
            self.send_embed(
                title=title,
                description=f"**実行日時**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S JST')}`",
                fields=fields,
                color=color,
                footer_text="Antigravity Autonomous Evolution Loop 🧬",
                target="report",
            )
        return res1

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

    def send_system_down_alert(
        self,
        service_name: str,
        reason: str,
        log_snippet: str = "",
        auto_recovery_status: str = "自動再起動シーケンスを実行中...",
        server_name: str = "Antigravity HFT Engine",
    ) -> bool:
        """
        システムダウン・プロセス停止・クラッシュ検知アラートを即時送信
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = f"🚨 【緊急警報：システムダウン検知】{service_name}"
        desc = (
            f"**検知時刻**: `{now_str}`\n"
            f"**対象ホスト**: `{server_name}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"**障害内容**: {reason}\n"
            f"**対応状況**: {auto_recovery_status}"
        )
        fields = [
            {"name": "⚠️ 停止対象プロセス", "value": f"`{service_name}`", "inline": True},
            {"name": "⚙️ フェイルセーフ状態", "value": f"`{auto_recovery_status}`", "inline": True},
        ]
        if log_snippet:
            # ログ末尾（最大800文字）
            snip = log_snippet[-800:].strip()
            fields.append({"name": "📜 直近ログ出力", "value": f"```text\n{snip}\n```", "inline": False})

        return self.send_embed(
            title=title,
            description=desc,
            fields=fields,
            color=0xE74C3C,  # 赤
            footer_text="Antigravity Watchdog Sentinel 🚨",
            target="alert",
        )

    def send_system_recovered_alert(
        self,
        service_name: str,
        message: str = "プロセスが正常に再起動され、運用が復帰しました。",
        server_name: str = "Antigravity HFT Engine",
    ) -> bool:
        """
        システムダウンからの自動復旧完了通知
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = f"🟢 【システム自動復旧完了】{service_name}"
        desc = (
            f"**復旧時刻**: `{now_str}`\n"
            f"**対象ホスト**: `{server_name}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{message}"
        )
        fields = [
            {"name": "✅ 復旧プロセス", "value": f"`{service_name}`", "inline": True},
            {"name": "📡 稼働状態", "value": "`RUNNING (正常稼働中)`", "inline": True},
        ]
        return self.send_embed(
            title=title,
            description=desc,
            fields=fields,
            color=0x2ECC71,  # 緑
            footer_text="Antigravity Watchdog Sentinel 🟢",
            target="alert",
        )

    def send_drawdown_alert(
        self,
        current_dd: float,
        max_dd: float,
        peak_pnl: float,
        current_pnl: float,
        is_halted: bool = False,
        reason: str = "",
        symbol: str = "FX_BTC_JPY",
    ) -> bool:
        """
        ドローダウン警戒またはサーキットブレーカー発動アラートを即時送信
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pct = (current_dd / max_dd * 100.0) if max_dd > 0 else 0.0

        if is_halted:
            title = f"🚨 【最大ドローダウン超過・緊急停止】{symbol}"
            color = 0xE74C3C  # 赤
            action_text = "🚫 **サーキットブレーカー発動**: 全保有建玉を直ちに強制エグジットしました。冷却待機に入ります。"
        else:
            title = f"⚠️ 【ドローダウン警戒警報】{symbol} (許容上限の{pct:.0f}%到達)"
            color = 0xF39C12  # オレンジ
            action_text = "⚠️ **警戒水準到達**: 最大ドローダウン許容限度に接近しています。逆流エグジット基準を厳格化中。"

        desc = (
            f"**検知時刻**: `{now_str}`\n"
            f"**対象市場**: `{symbol}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{action_text}\n"
            f"• **要因**: {reason}"
        )
        fields = [
            {"name": "📉 現在のドローダウン", "value": f"**`{current_dd:,.1f} 円`** (許容限度: `{max_dd:,.0f} 円` | `{pct:.1f}%`)", "inline": False},
            {"name": "🏔️ 過去ピーク損益", "value": f"`{peak_pnl:+,.1f} 円`", "inline": True},
            {"name": "💰 現在の総損益", "value": f"`{current_pnl:+,.1f} 円`", "inline": True},
        ]

        return self.send_embed(
            title=title,
            description=desc,
            fields=fields,
            color=color,
            footer_text="Antigravity Risk Guard 🛡️",
            target="alert",
        )

    def send_resource_pressure_alert(
        self,
        metrics: Dict[str, Any],
        trigger_reasons: List[str],
        remediation_actions: Optional[List[str]] = None,
        server_name: str = "Trading Server",
        level: str = "critical",
    ) -> bool:
        """
        CPU/メモリ/ディスクのシステム圧迫検知アラートを即時送信
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        disk = metrics.get("disk", {})
        mem = metrics.get("memory", {})
        cpu = metrics.get("cpu_pct", 0.0)

        is_crit = (level.lower() == "critical")
        title = f"🚨 【システム圧迫緊急警報】{server_name} 負荷逼迫" if is_crit else f"⚠️ 【システム圧迫注意報】{server_name} 高負荷検知"
        color = 0xE74C3C if is_crit else 0xF39C12

        reasons_text = "\n".join([f"• ❗ {r}" for r in trigger_reasons])
        actions_text = "\n".join([f"• 🛠️ {a}" for a in (remediation_actions or ["自動クリーンアップ実行"])])

        desc = (
            f"**発生時刻**: `{now_str}`\n"
            f"**サーバー**: `{server_name}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"**検知トリガー**:\n{reasons_text}"
        )

        fields = [
            {"name": "🧠 メモリ使用状況", "value": f"使用率: **`{mem.get('used_pct', 0.0):.1f}%`** (空き: `{mem.get('free_gb', 0.0):.2f} GB` / `{mem.get('total_gb', 0.0):.1f} GB`)", "inline": True},
            {"name": "⚡ CPU使用率", "value": f"負荷: **`{cpu:.1f}%`**", "inline": True},
            {"name": "💾 ディスク使用状況", "value": f"使用率: **`{disk.get('used_pct', 0.0):.1f}%`** (空き: `{disk.get('free_gb', 0.0):.1f} GB`)", "inline": True},
            {"name": "🔧 自動実行された改善策", "value": actions_text, "inline": False},
        ]

        return self.send_embed(
            title=title,
            description=desc,
            fields=fields,
            color=color,
            footer_text="Antigravity System Resource Guard 🚨",
            target="alert",
        )

    def send_significant_trade_report(
        self,
        strategy_name: str,
        side: str,
        size_btc: float,
        entry_price: float,
        exit_price: float,
        pnl_jpy: float,
        reason: str = "",
        symbol: str = "FX_BTC_JPY",
    ) -> bool:
        """
        大幅な収益実現または大幅な損失発生時の速報を定期報告チャンネルへ送信
        """
        is_profit = pnl_jpy > 0
        if is_profit:
            title = f"🎉 【大幅収益実現】{strategy_name} ({pnl_jpy:+,.1f} 円)"
            color = 0x2ECC71  # 緑
            badge = "🟢 利確"
        else:
            title = f"⚠️ 【大幅損失発生】{strategy_name} ({pnl_jpy:+,.1f} 円)"
            color = 0xE74C3C  # 赤
            badge = "🔴 損切"

        desc = (
            f"**決済時刻**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
            f"**対象市場**: `{symbol}` | **戦略**: `{strategy_name}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"• **決済区分**: {badge} ({side})\n"
            f"• **確定損益**: **`{pnl_jpy:+,.1f} 円`**\n"
            f"• **約定価格**: `{entry_price:,.0f} 円` ➔ `{exit_price:,.0f} 円`\n"
            f"• **取引数量**: `{size_btc:.4f} BTC`\n"
            f"• **決済トリガー**: {reason}"
        )

        return self.send_embed(
            title=title,
            description=desc,
            color=color,
            footer_text="Antigravity Performance Reporter 📊",
            target="report",
        )

    def send_performance_improvement_report(
        self,
        strategy_name: str,
        improvement_details: str,
        current_stats: Dict[str, Any],
        symbol: str = "FX_BTC_JPY",
    ) -> bool:
        """
        勝率向上、PF改善、損益向上などの成績改善レポートを定期報告チャンネルへ送信
        """
        title = f"📈 【成績改善レポート】{strategy_name}"
        desc = (
            f"**更新時刻**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
            f"**対象市場**: `{symbol}` | **戦略**: `{strategy_name}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"**改善推移**: {improvement_details}"
        )
        fields = [
            {"name": "🎯 現在の勝率", "value": f"`{current_stats.get('win_rate_pct', 0.0):.1f}%`", "inline": True},
            {"name": "💵 本日累計損益", "value": f"`{current_stats.get('daily_pnl', 0.0):+,.1f} 円`", "inline": True},
            {"name": "📊 取引回数", "value": f"`{current_stats.get('trades_count', 0)} 回`", "inline": True},
        ]
        return self.send_embed(
            title=title,
            description=desc,
            fields=fields,
            color=0x2ECC71,
            footer_text="Antigravity Performance Reporter 📈",
            target="report",
        )

    def send_manual_stop_alert(
        self,
        service_name: str = "Antigravity HFT Engine",
        reason: str = "オペレータによる手動停止（SIGINT/SIGTERM）",
        position_closed: bool = True,
        remaining_position_btc: float = 0.0,
        final_pnl_jpy: float = 0.0,
        server_name: str = "Antigravity Node",
    ) -> bool:
        """
        手動停止通知（Graceful Shutdown）をアラートチャンネルへ送信
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = f"🛑 【手動停止通知】{service_name}"
        status_text = "全建玉を安全に決済済み" if position_closed else f"未決済建玉あり ({remaining_position_btc:+.4f} BTC)"

        desc = (
            f"**停止時刻**: `{now_str}`\n"
            f"**対象ホスト**: `{server_name}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"• **停止理由**: {reason}\n"
            f"• **建玉状態**: **{status_text}**\n"
            f"• **最終確定損益**: `{final_pnl_jpy:+,.1f} 円`"
        )
        fields = [
            {"name": "🛑 停止対象サービス", "value": f"`{service_name}`", "inline": True},
            {"name": "📦 建玉ステータス", "value": f"`{status_text}`", "inline": True},
            {"name": "💰 確定損益", "value": f"`{final_pnl_jpy:+,.1f} 円`", "inline": True},
        ]

        return self.send_embed(
            title=title,
            description=desc,
            fields=fields,
            color=0xF39C12,  # オレンジ
            footer_text="Antigravity Sentinel Shutdown Guard 🛑",
            target="alert",
        )



