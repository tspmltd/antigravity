"""TF1A gate streaks + ENTER v2 predicate (ab_v1 rejected)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from antigravity.quant_pipeline.run_dryrun_tf1a_wobi_honest import (
    ENTER_RET60_MAX_BP,
    ENTER_STREAK_MIN,
    OBS_WOBI_FLOOR,
    TF1AWobiHonestDryRun,
    eval_tf1a_enter_v2,
)


def _ab_v1(wobi, ofi, o_streak, w_streak, ret60):
    return (
        wobi >= OBS_WOBI_FLOOR
        and ofi > 0.0
        and o_streak >= ENTER_STREAK_MIN
        and w_streak >= ENTER_STREAK_MIN
        and ret60 < ENTER_RET60_MAX_BP
    )


class TestTf1aGateStreaks(unittest.TestCase):
    def test_streaks_count_and_reset(self):
        obj = SimpleNamespace(_ofi_streak=0, _wobi_streak=0, _book_streak=0)
        TF1AWobiHonestDryRun._update_flow_streaks(obj, 0.20, 0.05)
        TF1AWobiHonestDryRun._update_flow_streaks(obj, 0.20, 0.05)
        TF1AWobiHonestDryRun._update_flow_streaks(obj, 0.20, 0.05)
        self.assertEqual(obj._ofi_streak, 3)
        self.assertEqual(obj._wobi_streak, 3)
        self.assertEqual(obj._book_streak, 3)
        TF1AWobiHonestDryRun._update_flow_streaks(obj, 0.20, 0.0)
        self.assertEqual(obj._ofi_streak, 0)
        self.assertEqual(obj._wobi_streak, 4)
        self.assertEqual(obj._book_streak, 0)
        TF1AWobiHonestDryRun._update_flow_streaks(obj, -0.10, 0.05)
        self.assertEqual(obj._ofi_streak, 1)
        self.assertEqual(obj._wobi_streak, 0)

    def test_ab_v1_obs_only_not_enter(self):
        self.assertTrue(_ab_v1(0.20, 0.05, 3, 3, 4.9))
        self.assertFalse(_ab_v1(0.20, 0.05, 3, 3, 5.0))

    def test_enter_v3_requires_base_micro_agent(self):
        ok, flags = eval_tf1a_enter_v2(
            trend_up=True,
            book_support=True,
            breakout=False,
            ret60_bp=2.0,
            micro_dev_bp=0.1,
            d_wobi_dt=0.0,
            z_score=0.0,
            agent_direction="up",
            agent_strength=0.5,
            agent_regime="trend",
        )
        self.assertTrue(ok)
        self.assertTrue(flags["base"] and flags["fresh5"] and flags["micro_ok"] and flags["agent_ok"])

    def test_enter_v3_allows_chase_fresh5_observe_only(self):
        ok, flags = eval_tf1a_enter_v2(
            trend_up=True,
            book_support=True,
            breakout=False,
            ret60_bp=5.0,
            micro_dev_bp=0.1,
            d_wobi_dt=0.0,
            z_score=0.0,
            agent_direction="up",
            agent_strength=0.5,
            agent_regime="trend",
        )
        self.assertTrue(ok)  # fresh5 no longer wired
        self.assertFalse(flags["fresh5"])

    def test_enter_v2_blocks_agent_down(self):
        ok, flags = eval_tf1a_enter_v2(
            trend_up=True,
            book_support=True,
            breakout=False,
            ret60_bp=1.0,
            micro_dev_bp=0.1,
            d_wobi_dt=0.0,
            z_score=0.0,
            agent_direction="down",
            agent_strength=0.5,
            agent_regime="trend",
        )
        self.assertFalse(ok)
        self.assertFalse(flags["agent_ok"])

    def test_enter_v2_blocks_hostile_micro(self):
        ok, flags = eval_tf1a_enter_v2(
            trend_up=True,
            book_support=True,
            breakout=False,
            ret60_bp=1.0,
            micro_dev_bp=-0.2,
            d_wobi_dt=-0.1,
            z_score=-0.5,
            agent_direction="up",
            agent_strength=0.5,
            agent_regime="trend",
        )
        self.assertFalse(ok)
        self.assertFalse(flags["micro_ok"])


if __name__ == "__main__":
    unittest.main()
