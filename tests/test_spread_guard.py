import unittest
from antigravity.quant_pipeline.safety_gate import (
    SafetyGate,
    SafetyGateConfig,
    LiveExecutionState,
    DryRunStats,
)


class TestSpreadGuard(unittest.TestCase):

    def setUp(self):
        self.config = SafetyGateConfig(
            min_dryrun_trades=3,
            min_dryrun_sharpe=0.5,
            min_dryrun_win_rate=0.4,
            max_daily_loss=300.0,
            max_consecutive_losses=4,
            min_confidence=0.60,
            max_spread_jpy=2500.0,
        )
        self.gate = SafetyGate(self.config)
        self.live_state = LiveExecutionState(daily_pnl=0.0, consecutive_losses=0)
        self.good_stats = DryRunStats(
            total_trades=10,
            win_count=6,
            loss_count=4,
            win_rate=0.60,
            sharpe_ratio=1.2,
            avg_pnl=5.0,
            consecutive_losses=0,
        )

    def test_spread_within_limit_allowed(self):
        """スプレッドが許容範囲内(2,500円以下)なら合格"""
        signal = {
            "action": "buy",
            "final_confidence": 0.80,
            "size_multiplier": 1.0,
            "fake_breakout_flag": False,
        }
        allowed, reason = self.gate.allow(
            signal, self.live_state, self.good_stats, spread_jpy=1500.0
        )
        self.assertTrue(allowed, f"スプレッド 1,500円は許容されるべき: {reason}")
        self.assertIn("合格", reason)

    def test_spread_exceeded_blocked(self):
        """スプレッドが2,500円を超えている場合は遮断"""
        signal = {
            "action": "buy",
            "final_confidence": 0.80,
            "size_multiplier": 1.0,
            "fake_breakout_flag": False,
        }
        allowed, reason = self.gate.allow(
            signal, self.live_state, self.good_stats, spread_jpy=3500.0
        )
        self.assertFalse(allowed, "スプレッド 3,500円は遮断されるべき")
        self.assertIn("スプレッド過大遮断", reason)
        print(f"\n[UnitTest] スプレッドガード成功: {reason}")


if __name__ == "__main__":
    unittest.main()
