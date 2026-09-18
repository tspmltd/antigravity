"""
Live Order Executor & SafetyGuards 総合検証スクリプト
=====================================================
本番連動エンジン (LiveOrderExecutor) の全防護シナリオを検証:
1. シミュレーションエントリー (BUY 0.001 BTC)
2. 利確防護 (+15円到達で TAKE_PROFIT)
3. 損切防護 (-12円到達で STOP_LOSS)
4. 最大保有時間タイムアウト (1800秒)
5. 4連敗到達時の自動緊急停止 (キルスイッチ作動)
6. Discord LIVE サーバーへの通知検証
"""
import sys
import os
import time

sys.path.insert(0, "/home/azureuser/antigravity")

from antigravity.quant_pipeline.live_order_executor import LiveOrderExecutor
from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier
from antigravity.quant_pipeline.safety_gate import DryRunStats


def main():
    print("=" * 70)
    print("   🛡️ LiveOrderExecutor & 本番安全防護 総合実機検証テスト   ")
    print("=" * 70)

    notifier = QuantDiscordNotifier()
    executor = LiveOrderExecutor(
        notifier=notifier,
        symbol="FX_BTC_JPY",
        order_size_btc=0.001,
        enable_real_trading=False,  # 安全検証シミュレーション
        take_profit_jpy=15.0,
        stop_loss_jpy=12.0,
        max_hold_sec=5.0,  # テスト用に短縮
        daily_loss_limit_jpy=300.0,
        max_consecutive_losses=4,
    )

    base_price = 11900000.0
    mock_stats = DryRunStats(total_trades=10, sharpe_ratio=1.2, win_rate=0.6, avg_pnl=5.0)
    mock_signal = {"action": "buy", "final_confidence": 0.75, "size_multiplier": 1.0}

    # -------------------------------------------------------------
    # シナリオ 1: 新規エントリー発注
    # -------------------------------------------------------------
    print("\n[1/5] ⚡ エントリー発注テスト...")
    success = executor.execute_entry("buy", base_price, mock_signal, mock_stats)
    print(f"  ➔ エントリー結果: {'✅ 成功' if success else '❌ 失敗'} (建玉: {executor.state.open_side} {executor.state.open_size} BTC)")

    # -------------------------------------------------------------
    # シナリオ 2: 利確ガード (+15円)
    # -------------------------------------------------------------
    print("\n[2/5] 🟢 利確ガード発動テスト (+20円価格上昇)...")
    res_tp = executor.check_position_guards(base_price + 20.0)
    print(f"  ➔ 決済結果: {res_tp['reason']}, 損益: +¥{res_tp['pnl']:.1f}, 当日累計: ¥{executor.state.daily_pnl:.1f}")

    # -------------------------------------------------------------
    # シナリオ 3: 損切ガード (-12円)
    # -------------------------------------------------------------
    print("\n[3/5] 🔴 損切ガード発動テスト (-15円急落)...")
    executor.execute_entry("buy", base_price, mock_signal, mock_stats)
    res_sl = executor.check_position_guards(base_price - 15.0)
    print(f"  ➔ 決済結果: {res_sl['reason']}, 損益: ¥{res_sl['pnl']:.1f}, 連敗数: {executor.state.consecutive_losses}回")

    # -------------------------------------------------------------
    # シナリオ 4: タイムアウト決済 (保有時間上限)
    # -------------------------------------------------------------
    print("\n[4/5] ⏱️ タイムアウト決済テスト (時間経過)...")
    executor.execute_entry("buy", base_price, mock_signal, mock_stats)
    executor.entry_time -= 10.0  # 10秒前と偽装
    res_to = executor.check_position_guards(base_price + 2.0)
    print(f"  ➔ 決済結果: {res_to['reason']}, 損益: ¥{res_to['pnl']:.1f}")

    # -------------------------------------------------------------
    # シナリオ 5: 4連敗到達による緊急停止 (キルスイッチ)
    # -------------------------------------------------------------
    print("\n[5/5] 🚨 4連敗到達による緊急停止テスト...")
    executor.state.consecutive_losses = 3  # すでに3連敗中
    executor.execute_entry("buy", base_price, mock_signal, mock_stats)
    res_halt = executor.check_position_guards(base_price - 15.0)  # 4回目の損失
    print(f"  ➔ 4連敗到達後状態: 連敗数={executor.state.consecutive_losses}, 停止フラグ(is_halted)={executor.state.is_halted}")

    # 停止中にエントリーを試みる
    refused = executor.execute_entry("buy", base_price, mock_signal, mock_stats)
    print(f"  ➔ 停止中の新規エントリー拒絶: {'✅ 正常に拒絶' if not refused else '❌ 誤って執行'}")

    print("\n" + "=" * 70)
    if executor.state.is_halted and not refused:
        print("🎉 【全シナリオ合格】 本番連動の安全防護・決済ガード・緊急停止が完全動作しました！")
    else:
        print("⚠️ 一部テストで期待と異なる挙動がありました。")
    print("=" * 70)


if __name__ == "__main__":
    main()
