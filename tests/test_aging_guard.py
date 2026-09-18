"""
Tests for AgingGuard (GAPCORE-compliant inventory hold timeout risk guard)
"""

import unittest
from antigravity.risk_guard.aging_guard import AgingGuard


class TestAgingGuard(unittest.TestCase):
    def setUp(self):
        self.max_hold_seconds = 10.0
        self.warning_ratio = 0.80

    def test_flat_position_state(self):
        guard = AgingGuard(max_hold_seconds=self.max_hold_seconds)
        res = guard.check(current_position=0.0)
        self.assertFalse(res["is_active"])
        self.assertFalse(res["is_timed_out"])
        self.assertEqual(res["action"], "HOLD")
        self.assertEqual(guard.get_age_seconds(), 0.0)

    def test_open_position_and_aging(self):
        guard = AgingGuard(max_hold_seconds=self.max_hold_seconds, warning_ratio=0.80)
        t0 = 1000.0

        # Open LONG
        guard.on_position_update(0.001, price=10000000.0, timestamp=t0)
        self.assertTrue(guard.has_position)

        # 5 seconds elapsed (under warning)
        res1 = guard.check(current_position=0.001, current_price=10000000.0, now=t0 + 5.0)
        self.assertTrue(res1["is_active"])
        self.assertFalse(res1["is_warning"])
        self.assertFalse(res1["is_timed_out"])
        self.assertEqual(res1["action"], "HOLD")
        self.assertEqual(res1["age_seconds"], 5.0)

        # 8.5 seconds elapsed (warning fired)
        res2 = guard.check(current_position=0.001, current_price=10000000.0, now=t0 + 8.5)
        self.assertTrue(res2["is_warning"])
        self.assertFalse(res2["is_timed_out"])
        self.assertEqual(res2["action"], "HOLD")

        # 10.5 seconds elapsed (timeout exit triggered)
        res3 = guard.check(current_position=0.001, current_price=10000000.0, now=t0 + 10.5)
        self.assertTrue(res3["is_timed_out"])
        self.assertEqual(res3["action"], "EXIT")

    def test_position_close_resets_aging(self):
        guard = AgingGuard(max_hold_seconds=self.max_hold_seconds)
        t0 = 1000.0

        # Open and age 9 seconds
        guard.on_position_update(0.001, price=10000000.0, timestamp=t0)
        guard.check(current_position=0.001, now=t0 + 9.0)

        # Close position
        guard.on_position_update(0.0, price=10005000.0, timestamp=t0 + 9.5)
        self.assertFalse(guard.has_position)
        self.assertEqual(guard.get_age_seconds(now=t0 + 10.0), 0.0)

        # Subsequent check is clear
        res = guard.check(current_position=0.0, now=t0 + 15.0)
        self.assertFalse(res["is_timed_out"])
        self.assertEqual(res["action"], "HOLD")

    def test_position_doten_resets_timestamp(self):
        guard = AgingGuard(max_hold_seconds=self.max_hold_seconds)
        t0 = 1000.0

        # Open LONG at t0
        guard.on_position_update(0.001, price=10000000.0, timestamp=t0)

        # Doten flip to SHORT at t0 + 9.0s
        t_flip = t0 + 9.0
        guard.on_position_update(-0.001, price=9990000.0, timestamp=t_flip)

        # At t0 + 11.0s (total from t0 is 11s, but from flip is only 2s)
        res = guard.check(current_position=-0.001, now=t0 + 11.0)
        self.assertFalse(res["is_timed_out"])
        self.assertEqual(res["age_seconds"], 2.0)
        self.assertEqual(res["action"], "HOLD")

    def test_timeout_callback(self):
        timeout_calls = []

        def on_timeout(reason, age, pos):
            timeout_calls.append((reason, age, pos))

        guard = AgingGuard(
            max_hold_seconds=self.max_hold_seconds,
            on_timeout_callback=on_timeout,
        )
        t0 = 1000.0
        guard.on_position_update(0.001, price=10000000.0, timestamp=t0)

        # Trigger timeout
        guard.check(current_position=0.001, now=t0 + 12.0)
        self.assertEqual(len(timeout_calls), 1)
        self.assertEqual(timeout_calls[0][2], 0.001)


if __name__ == "__main__":
    unittest.main()
