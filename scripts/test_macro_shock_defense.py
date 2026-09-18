"""
Macro Shock & Information Pipeline Defense 総合検証テスト
===========================================================
世界の株価急変・開示センチネルとクオンツ意思決定 (Fusion & SafetyGate) の
自動連携・防護動作を実機検証する。

1. 平常時テスト (ショックなし)
2. Warning ショック発令テスト (±1.0%突破 ➔ ロット50%半減 & 確信度0.75へ引き上げ)
3. Critical ショック発令テスト (±2.0%超急落 ➔ 完全発注遮断 HALT)
4. 平常復帰テスト (ショック解除 ➔ 自動復帰)
"""
import sys
import os
import time

sys.path.insert(0, "/home/azureuser/antigravity")

from antigravity.quant_pipeline.market_shock_sentinel import MarketShockSentinel
from antigravity.quant_pipeline.safety_gate import SafetyGate, SafetyGateConfig, LiveExecutionState, DryRunStats
from antigravity.quant_pipeline.fusion_engine import SignalFusionEngine
from antigravity.quant_pipeline.event_bus import EventBus
from antigravity.quant_pipeline.parquet_logger import ParquetBatchLogger


def main():
    print("=" * 70)
    print("   🌍 情報収集自動化 & マクロ急変防護 (SafetyGate) 総合検証テスト   ")
    print("=" * 70)

    sentinel = MarketShockSentinel()
    sentinel.clear_shock()  # 一旦初期化

    gate = SafetyGate()
    live_state = LiveExecutionState()
    stats = DryRunStats(total_trades=10, sharpe_ratio=1.0, win_rate=0.6, avg_pnl=5.0)

    # -------------------------------------------------------------
    # 1. 平常時テスト
    # -------------------------------------------------------------
    print("\n[1/4] 🟢 平常時テスト (ショックなし)...")
    normal_signal = {
        "action": "buy",
        "final_confidence": 0.68,
        "size_multiplier": 1.0,
        "fake_breakout_flag": False,
    }
    allowed, reason = gate.allow(normal_signal, live_state, stats)
    print(f"  ➔ 確信度 0.68 の判定: {'✅ 合格' if allowed else '❌ 拒絶'} ({reason})")

    # -------------------------------------------------------------
    # 2. Warning ショック発令テスト (例: 日経平均 +1.2% 急変)
    # -------------------------------------------------------------
    print("\n[2/4] 🟡 Warning ショック発令テスト (日経平均 +1.2% 突破)...")
    sentinel.publish_shock("日経平均株価 +1.2%", level="warning", duration_sec=60.0)

    # 通常なら通る 0.68 は、警戒基準 (0.75) に満たないため遮断されるべき
    allowed_warn_low, reason_warn_low = gate.allow(normal_signal, live_state, stats)
    print(f"  ➔ 通常シグナル (Conf 0.68): {'❌ 誤って合格' if allowed_warn_low else '🛡️ 正常に警戒遮断'} ({reason_warn_low})")

    # 0.78 の超高確信度シグナルは警戒下でも合格できるべき
    high_conf_signal = {
        "action": "buy",
        "final_confidence": 0.78,
        "size_multiplier": 0.5,
        "fake_breakout_flag": False,
    }
    allowed_warn_high, reason_warn_high = gate.allow(high_conf_signal, live_state, stats)
    print(f"  ➔ 超高確信度 (Conf 0.78): {'✅ 防護下で合格' if allowed_warn_high else '❌ 拒絶'} ({reason_warn_high})")

    # -------------------------------------------------------------
    # 3. Critical ショック発令テスト (例: ナスダック -2.4% 急落)
    # -------------------------------------------------------------
    print("\n[3/4] 🔴 Critical ショック発令テスト (ナスダック -2.4% 急落)...")
    sentinel.publish_shock("ナスダック -2.4% 急落", level="critical", duration_sec=60.0)

    # いかなる高確信度シグナルも完全に遮断されるべき
    allowed_crit, reason_crit = gate.allow(high_conf_signal, live_state, stats)
    print(f"  ➔ 超高確信度 (Conf 0.78): {'❌ 誤って合格' if allowed_crit else '🛡️ 完全遮断成功'} ({reason_crit})")

    # -------------------------------------------------------------
    # 4. 平常復帰テスト
    # -------------------------------------------------------------
    print("\n[4/4] 🟢 平常復帰テスト (ショック解除)...")
    sentinel.clear_shock()
    allowed_rec, reason_rec = gate.allow(normal_signal, live_state, stats)
    print(f"  ➔ 通常シグナル (Conf 0.68): {'✅ 正常に合格復帰' if allowed_rec else '❌ 拒絶'} ({reason_rec})")

    print("\n" + "=" * 70)
    all_passed = (allowed and not allowed_warn_low and allowed_warn_high and not allowed_crit and allowed_rec)
    if all_passed:
        print("🎉 【全シナリオ合格】 情報収集自動化 ➔ クオンツリスク遮断連携が完全動作しました！")
    else:
        print("⚠️ 一部テストで期待と異なる挙動がありました。")
    print("=" * 70)


if __name__ == "__main__":
    main()
