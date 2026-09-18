"""
Dry-run & LIVE 同期ロジック実機シミュレーション
================================================
Fusion Engine からのシグナルに対し、
1. Dry-run (影武者) が先行して全シグナルを受け取って検証
2. SafetyGate が本番リスクおよび影武者の Sharpe/勝率を判定
3. 合格シグナルのみ LIVE に執行
の一連の挙動を検証する。
"""
import sys
import os
import time

sys.path.insert(0, "/home/azureuser/antigravity")

from antigravity.quant_pipeline.sync_executor import SyncExecutor
from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier
from antigravity.quant_pipeline.safety_gate import SafetyGate, SafetyGateConfig


def main():
    print("=" * 70)
    print("   🛡️ Dry-run & LIVE 同期ロジック (SafetyGate) 実機シミュレーション   ")
    print("=" * 70)

    notifier = QuantDiscordNotifier()
    # テスト用に閾値を設定
    gate_config = SafetyGateConfig(
        min_dryrun_trades=3,
        min_dryrun_sharpe=0.5,
        min_confidence=0.65,
        cool_down_seconds=2.0
    )
    safety_gate = SafetyGate(gate_config)
    executor = SyncExecutor(notifier=notifier, safety_gate=safety_gate)

    market_price = 11900000.0

    print("\n--- [ケース1: 確信度不足シグナル] ---")
    low_conf_signal = {
        "action": "buy",
        "final_confidence": 0.45,
        "size_multiplier": 1.0,
        "regime_tag": "range",
        "pressure_side": "buy",
        "pressure_score": 0.3,
        "trend_direction": "up",
        "trend_strength": 0.2,
        "fake_breakout_flag": False,
        "latency_risk_flag": False,
    }
    executor.process_signal(low_conf_signal, market_price)

    print("\n--- [ケース2: Fake Breakout 検知シグナル] ---")
    fake_bo_signal = {
        "action": "buy",
        "final_confidence": 0.85,
        "size_multiplier": 0.0,
        "regime_tag": "high_vol",
        "pressure_side": "buy",
        "pressure_score": 0.9,
        "trend_direction": "up",
        "trend_strength": 0.8,
        "fake_breakout_flag": True,  # フェイクブレイク！
        "latency_risk_flag": False,
    }
    executor.process_signal(fake_bo_signal, market_price)

    print("\n--- [ケース3: 影武者の実績作り (Dry-run 先行検証)] ---")
    # 影武者に3回勝たせて Sharpe を高める
    for i in range(3):
        executor.dryrun_sim.feed_signal(
            {"action": "buy", "final_confidence": 0.75, "size_multiplier": 1.0},
            market_price
        )
        executor.dryrun_sim.feed_market_price(market_price + 20.0)  # +20円 利確
        time.sleep(0.5)

    stats = executor.dryrun_sim.get_stats()
    print(f"  影武者成績: {stats.total_trades}戦{stats.win_count}勝, 勝率: {stats.win_rate*100:.1f}%, Sharpe: {stats.sharpe_ratio:.2f}, 累計PnL: +¥{stats.total_pnl:.1f}")

    print("\n--- [ケース4: 合格シグナル ➔ LIVE 発注連動] ---")
    qualified_signal = {
        "action": "buy",
        "final_confidence": 0.78,
        "size_multiplier": 1.0,
        "regime_tag": "trend",
        "pressure_side": "buy",
        "pressure_score": 0.8,
        "trend_direction": "up",
        "trend_strength": 0.7,
        "fake_breakout_flag": False,
        "latency_risk_flag": False,
    }
    executor.process_signal(qualified_signal, market_price)

    print("\n--- [ケース5: 本番ポジション保有中の追加エントリー拒絶] ---")
    executor.process_signal(qualified_signal, market_price)

    print("\n--- [ケース6: イグジットシグナル ➔ LIVE ポジション解消] ---")
    exit_signal = {
        "action": "exit",
        "final_confidence": 0.2,
        "size_multiplier": 0.0,
        "regime_tag": "range",
        "pressure_side": "none",
        "pressure_score": 0.0,
        "trend_direction": "neutral",
        "trend_strength": 0.1,
        "fake_breakout_flag": False,
        "latency_risk_flag": False,
    }
    executor.process_signal(exit_signal, market_price + 15.0)

    print("\n" + "=" * 70)
    print("🎉 同期ロジック (SafetyGate) の全シナリオ検証が完了しました！")
    print("=" * 70)


if __name__ == "__main__":
    main()
