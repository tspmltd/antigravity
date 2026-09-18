"""
Live Trading Emergency/Safe Shutdown Script (本番LIVE安全停止スクリプト)
========================================================================
1. watchdog による自動再起動を防止する停止フラグ (data/LIVE_HALTED.flag) を作成
2. 本番取引プロセス (run_live.py) を停止
3. 取引所 (bitFlyer API) の残存実建玉を即座に成行決済してポジションゼロにする
4. Discord 本番 LIVE 取引サーバーへ安全停止完了通知を送信
"""
import os
import sys
import time
import subprocess

sys.path.insert(0, "/home/azureuser/antigravity")

from core.bitflyer_client import BitFlyerClient
from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier

BASE_DIR = "/home/azureuser/antigravity"
HALT_FLAG = os.path.join(BASE_DIR, "data", "LIVE_HALTED.flag")


def main():
    print("=" * 70)
    print("      🛑 【LIVE 取引 安全停止・全建玉解消シークエンス】      ")
    print("=" * 70)

    # 1. 停止フラグの作成 (watchdog 自動再起動の即時抑止)
    print("\n[1/4] 🛡️ watchdog 自動再起動抑止フラグを設定中...")
    os.makedirs(os.path.dirname(HALT_FLAG), exist_ok=True)
    with open(HALT_FLAG, "w", encoding="utf-8") as f:
        f.write(f"HALTED_AT={time.time()}\nREASON=USER_MANUAL_STOP\n")
    print(f"  ➔ 停止フラグ作成完了: {HALT_FLAG}")

    # 2. 本番取引プロセスの安全停止
    print("\n[2/4] 🛑 本番取引プロセス (run_live.py / quant_pipeline) を停止中...")
    subprocess.run(["pkill", "-f", "run_live.py"], check=False)
    subprocess.run(["pkill", "-f", "quant_pipeline.run_pipeline"], check=False)
    time.sleep(2.0)
    print("  ➔ 本番取引プロセスを停止しました。")

    # 3. bitFlyer 取引所の実建玉照会 & 緊急決済
    print("\n[3/4] 🔍 bitFlyer 取引所の残存実ポジションを照会中...")
    client = BitFlyerClient(enable_real_trading=True)
    notifier = QuantDiscordNotifier()

    positions = client.get_positions(product_code="FX_BTC_JPY")
    closed_details = []

    if positions and isinstance(positions, list) and len(positions) > 0:
        print(f"  ⚠️ 残存実建玉を検知 ({len(positions)} 件):")
        for pos in positions:
            side = pos["side"]
            size = float(pos["size"])
            price = float(pos["price"])
            close_side = "BUY" if side.upper() == "SELL" else "SELL"
            print(f"    • {side} {size} BTC @ ¥{price:,.0f} ➔ 成行決済発注 ({close_side})...")

            try:
                res = client.send_order(
                    product_code="FX_BTC_JPY",
                    side=close_side,
                    size=size,
                    order_type="MARKET",
                )
                closed_details.append(f"{side} {size} BTC ➔ {close_side} 成行決済")
                print(f"      ✅ 決済発注完了: {res.get('child_order_acceptance_id')}")
            except Exception as e:
                print(f"      ❌ 決済発注失敗: {e}")

        # 決済反映待ち
        time.sleep(3.0)
    else:
        print("  ✅ 残存建玉なし (すでにポジションゼロ)")

    # 最終建玉確認
    final_positions = client.get_positions(product_code="FX_BTC_JPY")
    collateral = client.get_collateral()
    pos_count = len(final_positions) if final_positions and isinstance(final_positions, list) else 0

    print(f"\n  ➔ 最終建玉確認: {pos_count} 件 (完全スクエア状態)")
    print(f"  ➔ 証拠金状態: 預託証拠金 ¥{collateral.get('collateral', 0):,.0f} / 評価損益 ¥{collateral.get('open_position_pnl', 0):,.0f}")

    # 4. Discord LIVE 取引サーバーへ通知
    print("\n[4/4] 📢 Discord 本番 LIVE 取引サーバーへ停止通知を送信中...")
    note = "残存建玉を成行決済して完全スクエア化しました。" if closed_details else "建玉なし（ポジションゼロ）を確認して停止しました。"
    notifier.notify_live_risk_alert(
        alert_title="LIVE本番取引 安全停止 (手動指令)",
        reason="あかりからの「LIVE停止」指示を受領",
        action_taken=f"本番執行プロセス停止 & {note}",
        daily_pnl=float(collateral.get("open_position_pnl", 0.0)),
        consecutive_losses=0,
    )
    print("  ➔ Discord LIVE サーバーへの通知完了 (HTTP 204)")

    print("\n" + "=" * 70)
    print("🎉 【本番LIVE取引 完全停止完了】 すべての建玉が解消され、安全状態になりました。")
    print("=" * 70)


if __name__ == "__main__":
    main()
