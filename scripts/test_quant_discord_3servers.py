"""
Discord 3サーバー総合疎通検証テスト
====================================
あかり専用クオンツ基盤の3サーバーWebhookに対して、本番運用時と同一フォーマットの
リッチEmbed通知を送信し、完全疎通を検証する。

1. LIVE 取引サーバー: 本番エントリー & 利確 & リスク防護通知
2. Quants Dry-run サーバー: Fusion意思決定ストリーム & 仮想トレード決済通知
3. 分析・重み更新サーバー: DuckDB分析サマリー & ΔW重み更新提案
"""
import sys
import os
import time

# antigravity をパスに追加
sys.path.insert(0, "/home/azureuser/antigravity")

from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier


def main():
    print("=" * 70)
    print("   🚀 Discord 3サーバー構成 総合疎通検証テスト (あかり専用)   ")
    print("=" * 70)

    notifier = QuantDiscordNotifier()

    print("\n[1/3] 🟥 LIVE 取引サーバーへの疎通検証中...")
    success_live_1 = notifier.notify_live_trade(
        action="buy",
        symbol="FX_BTC_JPY",
        price=10245000.0,
        size=0.001,
        trade_type="ENTRY",
        daily_pnl=45.0,
        consecutive_losses=0,
        extra_note="✅ SafetyGate承認 (影武者Sharpe 1.25 / 確信度 0.74)",
    )
    time.sleep(1.0)
    success_live_2 = notifier.notify_live_trade(
        action="exit",
        symbol="FX_BTC_JPY",
        price=10260000.0,
        size=0.001,
        pnl=15.0,
        trade_type="TAKE_PROFIT",
        daily_pnl=60.0,
        consecutive_losses=0,
        extra_note="🎯 スプレッド勝ち +15円 利確達成 (ノイズゼロ安全運用)",
    )
    print(f"  ➔ LIVE エントリー/決済通知: {'✅ 成功' if (success_live_1 and success_live_2) else '❌ 失敗'}")

    print("\n[2/3] 🟦 Quants Dry-run サーバーへの疎通検証中...")
    mock_decision = {
        "action": "buy",
        "trend_direction": "up",
        "trend_strength": 0.65,
        "regime_tag": "trend",
        "pressure_side": "buy",
        "pressure_score": 0.72,
        "final_confidence": 0.78,
        "size_multiplier": 1.0,
        "fake_breakout_flag": False,
        "latency_risk_flag": False,
    }
    success_dry_1 = notifier.notify_dryrun_fusion_decision(
        decision=mock_decision,
        mid_price=10245000.0,
        virtual_pnl=185.5,
        virtual_win_rate=0.625,
        force=True,
    )
    time.sleep(1.0)
    success_dry_2 = notifier.notify_dryrun_virtual_trade(
        side="buy",
        entry_price=10245000.0,
        exit_price=10260000.0,
        pnl=15.0,
        reason="TAKE_PROFIT (+¥15.0)",
        total_pnl=200.5,
        win_count=15,
        loss_count=9,
    )
    print(f"  ➔ Dry-run 意思決定/仮想決済通知: {'✅ 成功' if (success_dry_1 and success_dry_2) else '❌ 失敗'}")

    print("\n[3/3] 🟩 分析・重み更新サーバーへの疎通検証中...")
    mock_stats = {
        "total_snapshots": 45280,
        "avg_latency_ms": 142.3,
        "avg_imbalance": -0.124,
    }
    mock_delta_w = [
        {
            "regime_tag": "trend",
            "samples": 340,
            "win_avg_pressure": 0.78,
            "lose_avg_pressure": 0.52,
            "delta_w_pressure": 0.260,
        },
        {
            "regime_tag": "range",
            "samples": 810,
            "win_avg_pressure": 0.41,
            "lose_avg_pressure": 0.65,
            "delta_w_pressure": -0.240,
        },
        {
            "regime_tag": "high_vol",
            "samples": 120,
            "win_avg_pressure": 0.85,
            "lose_avg_pressure": 0.71,
            "delta_w_pressure": 0.140,
        }
    ]
    success_analysis = notifier.notify_analysis_report(
        stats_dict=mock_stats,
        delta_w_list=mock_delta_w,
        approval_command="python3 -m antigravity.quant_pipeline.duckdb_analyzer --post-discord --approve",
    )
    print(f"  ➔ DuckDB 分析・重み更新レポート通知: {'✅ 成功' if success_analysis else '❌ 失敗'}")

    print("\n" + "=" * 70)
    if success_live_1 and success_dry_1 and success_analysis:
        print("🎉 【検証完了】 3サーバーすべてのWebhook疎通およびリッチEmbed送信が完全成功しました！")
    else:
        print("⚠️ 一部サーバーへの送信に失敗しました。")
    print("=" * 70)


if __name__ == "__main__":
    main()
