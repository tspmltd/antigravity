#!/usr/bin/env python3
"""
Discord 3系統 Webhook ＆ 3大緊急アラート 実機検証テストスクリプト

検証対象:
1. 【定期レポートチャンネル】(DISCORD_WEBHOOK_URL)
   - 24時起点・1時間成績＆累積運用レポート
   - 大幅収益/損失の即時速報
2. 【システム改善チャンネル】(DISCORD_SYSTEM_WEBHOOK_URL)
   - 自律発見パイプライン戦略検証・改善サマリー
   - システム自己修復・最適化通知
3. 【緊急アラートチャンネル】(DISCORD_ALERT_WEBHOOK_URL) - 3大緊急アラート
   - 🚨 アラート①: システムダウン・死活監視クラッシュ警報
   - 🚨 アラート②: 30秒周期システムリソース圧迫即時検知警報
   - 🚨 アラート③: 最大ドローダウン超過・サーキットブレーカー強制決済警報
   - 🛑 手動停止通知 (Graceful Shutdown)
"""

import os
import sys
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# リポジトリルートを sys.path に追加
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from antigravity.risk_guard.notifier import DiscordNotifier
from antigravity.risk_guard.system_monitor import SystemResourceMonitor


def main():
    print("=" * 70)
    print("      Discord 3系統 Webhook ＆ 3大緊急アラート 実機総合疎通検証      ")
    print("=" * 70)

    notifier = DiscordNotifier()
    monitor = SystemResourceMonitor()

    print(f"• 定期レポート URL : {'設定済 ✅' if notifier.is_enabled('report') else '未設定 ❌'}")
    print(f"• システム改善 URL : {'設定済 ✅' if notifier.is_enabled('system') else '未設定 ❌'}")
    print(f"• 緊急アラート URL : {'設定済 ✅' if notifier.is_enabled('alert') else '未設定 ❌'}")
    print("-" * 70)

    # -------------------------------------------------------------
    # 1. 定期運用成績報告の検証 (定期レポートチャンネル)
    # -------------------------------------------------------------
    print("\n[TEST 1] 定期運用成績レポート送信テスト (定期レポートチャンネル)...")
    mock_snapshot = {
        "jst_time_str": "2026-09-15 15:00:00 (JST)",
        "overall": {
            "position_btc": 0.005,
            "unrealized_pnl": 240.0,
            "net_profit_daily": 3480.0,
            "net_profit_total": 12850.0,
            "hourly": {
                "realized_pnl": 620.0,
                "trades_count": 4,
                "wins_count": 3,
                "losses_count": 1,
                "win_rate_pct": 75.0,
            },
            "daily_cumulative": {
                "realized_pnl": 3480.0,
                "trades_count": 28,
                "wins_count": 20,
                "losses_count": 8,
                "win_rate_pct": 71.4,
            },
            "total_cumulative": {
                "realized_pnl": 12610.0,
                "trades_count": 142,
                "win_rate_pct": 68.3,
            },
        },
        "strategies": [
            {
                "name": "Mock_EmaTrendTickStrategy (テスト用)",
                "daily": {"realized_pnl": 2100.0, "trades_count": 12, "win_rate_pct": 75.0},
                "hourly": {"realized_pnl": 450.0},
            },
            {
                "name": "Mock_MicroSpreadMM (テスト用)",
                "daily": {"realized_pnl": 1380.0, "trades_count": 16, "win_rate_pct": 68.8},
                "hourly": {"realized_pnl": 170.0},
            },
        ],
    }
    r1 = notifier.send_regular_report(mock_snapshot, symbol="FX_BTC_JPY [🧪疎通テスト用モック]")
    print(f"  ➔ 結果: {'成功 (HTTP 200/204)' if r1 else '失敗または未設定'}")
    time.sleep(0.5)


    # -------------------------------------------------------------
    # 2. システム改善・自律発見パイプライン通知 (システム改善チャンネル)
    # -------------------------------------------------------------
    print("\n[TEST 2] システム改善・自律進化レポート送信テスト (システム改善チャンネル)...")
    r2 = notifier.send_system_update(
        title="🧬 【自律進化】新規マイクロ秒スプレッドMM戦略 適合承認",
        description="自律発見パイプライン Cycle 16 にて新戦略が Sharpe 2.45, MDD 0.48% を記録し本番プールへ採択されました。",
        fields=[
            {"name": "戦略ID", "value": "`strat_spread_hft_v4`", "inline": True},
            {"name": "期待Sharpe", "value": "**2.45**", "inline": True},
            {"name": "最大DD", "value": "**0.48%**", "inline": True},
        ]
    )
    print(f"  ➔ 結果: {'成功 (HTTP 200/204)' if r2 else '失敗または未設定'}")
    time.sleep(0.5)

    # -------------------------------------------------------------
    # 3. 3大緊急アラート (緊急アラートチャンネル)
    # -------------------------------------------------------------
    print("\n[TEST 3-1] 🚨 緊急アラート①: システムダウン・死活監視検知テスト...")
    r3_1 = notifier.send_system_down_alert(
        service_name="Antigravity Trading Engine (FX_BTC_JPY)",
        reason="Heartbeat途絶検知 (プロセス応答なし / タイムアウト120秒超過)",
        log_snippet="[ERROR] Connection reset by peer\n[FATAL] Main thread terminated unexpectedly.\nWatchdog Sentinel activated auto-recovery.",
        auto_recovery_status="再起動コマンドを実行中... (自動復旧シーケンス 1/3)",
        server_name="Azure VM Production Node",
    )
    print(f"  ➔ 結果: {'成功 (HTTP 200/204)' if r3_1 else '失敗または未設定'}")
    time.sleep(0.5)

    print("\n[TEST 3-2] 🚨 緊急アラート②: 30秒周期システムリソース圧迫即時検知テスト...")
    metrics = monitor.collect_all_metrics()
    metrics["memory"]["used_pct"] = 88.5
    metrics["cpu_pct"] = 92.4
    r3_2 = notifier.send_resource_pressure_alert(
        metrics=metrics,
        trigger_reasons=[
            "CPU使用率が警告閾値(80%)を超過: 92.4%",
            "メモリ使用率が警告閾値(80%)を超過: 88.5%",
        ],
        remediation_actions=[
            "ガベージコレクション実行 (解放オブジェクト: 1,420個)",
            "Tickバッファおよび一時キャッシュの圧縮を実施",
            "肥大化ログの縮退ローテーション実施 (0件)",
        ],
        server_name="Azure VM Production Node",
        level="critical",
    )
    print(f"  ➔ 結果: {'成功 (HTTP 200/204)' if r3_2 else '失敗または未設定'}")
    time.sleep(0.5)

    print("\n[TEST 3-3] 🚨 緊急アラート③: 最大ドローダウン超過・サーキットブレーカー強制決済テスト...")
    r3_3 = notifier.send_drawdown_alert(
        current_dd=15800.0,
        max_dd=15000.0,
        peak_pnl=52000.0,
        current_pnl=36200.0,
        is_halted=True,
        reason="ピーク利益比ドローダウンが許容上限(15,000円)を突破 (15,800円 / 105.3%)",
        symbol="FX_BTC_JPY",
    )
    print(f"  ➔ 結果: {'成功 (HTTP 200/204)' if r3_3 else '失敗または未設定'}")
    time.sleep(0.5)

    print("\n[TEST 3-4] 🛑 手動停止通知 (Graceful Shutdown) テスト...")
    r3_4 = notifier.send_manual_stop_alert(
        service_name="Antigravity HFT Engine (FX_BTC_JPY)",
        reason="オペレータによる手動保守停止（Ctrl+C / SIGINT）",
        position_closed=True,
        remaining_position_btc=0.0,
        final_pnl_jpy=36200.0,
        server_name="Azure VM Production Node",
    )
    print(f"  ➔ 結果: {'成功 (HTTP 200/204)' if r3_4 else '失敗または未設定'}")

    print("\n" + "=" * 70)
    print("                   実機検証テスト完了                   ")
    print("=" * 70)


if __name__ == "__main__":
    main()
