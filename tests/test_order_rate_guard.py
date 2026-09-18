"""
Tests for OrderRateGuard (GAPCORE-compliant API rate limiter & flood guard)
============================================================================
"""

import unittest
import time
from antigravity.risk_guard.order_rate_guard import OrderRateGuard


class TestOrderRateGuard(unittest.TestCase):
    def test_initial_state_allowed(self):
        guard = OrderRateGuard(max_orders_per_min=60, min_order_interval_sec=3.0, global_min_interval_sec=0.5)
        allowed, reason = guard.check_order_allowed(strategy_id="strat_a", now_ts=1000.0)
        self.assertTrue(allowed)
        self.assertEqual(reason, "ALLOWED")
        self.assertTrue(guard.can_send_order(strategy_id="strat_a", now_ts=1000.0))

    def test_global_burst_spacing(self):
        guard = OrderRateGuard(global_min_interval_sec=0.5, min_order_interval_sec=3.0)
        t0 = 1000.0
        guard.record_order(strategy_id="strat_a", now_ts=t0)

        # 0.2s later from a DIFFERENT strategy: rejected by global burst spacing
        allowed, reason = guard.check_order_allowed(strategy_id="strat_b", now_ts=t0 + 0.2)
        self.assertFalse(allowed)
        self.assertIn("グローバルバースト間隔制限", reason)

        # 0.6s later from strat_b: allowed because global spacing (0.5s) is satisfied
        allowed, reason = guard.check_order_allowed(strategy_id="strat_b", now_ts=t0 + 0.6)
        self.assertTrue(allowed)

    def test_per_strategy_cooldown(self):
        guard = OrderRateGuard(global_min_interval_sec=0.5, min_order_interval_sec=3.0)
        t0 = 1000.0
        guard.record_order(strategy_id="strat_a", now_ts=t0)

        # 1.0s later: strat_a still in 3.0s cooldown
        allowed, reason = guard.check_order_allowed(strategy_id="strat_a", now_ts=t0 + 1.0)
        self.assertFalse(allowed)
        self.assertIn("クールダウン待機中", reason)

        # 3.1s later: strat_a cooldown passed
        allowed, reason = guard.check_order_allowed(strategy_id="strat_a", now_ts=t0 + 3.1)
        self.assertTrue(allowed)

    def test_max_orders_per_minute_sliding_window(self):
        guard = OrderRateGuard(max_orders_per_min=3, min_order_interval_sec=1.0, global_min_interval_sec=0.1, window_seconds=60.0)
        t0 = 1000.0

        # Order 1
        self.assertTrue(guard.can_send_order("s1", now_ts=t0))
        guard.record_order("s1", now_ts=t0)

        # Order 2 (after 2s)
        self.assertTrue(guard.can_send_order("s1", now_ts=t0 + 2.0))
        guard.record_order("s1", now_ts=t0 + 2.0)

        # Order 3 (after 4s)
        self.assertTrue(guard.can_send_order("s1", now_ts=t0 + 4.0))
        guard.record_order("s1", now_ts=t0 + 4.0)

        # Order 4 (after 6s): max_orders_per_min=3 reached -> Rejected!
        allowed, reason = guard.check_order_allowed("s1", now_ts=t0 + 6.0)
        self.assertFalse(allowed)
        self.assertIn("1分間発注上限到達", reason)

        # After 61 seconds from t0: Order 1 has expired from the 60s sliding window
        allowed, reason = guard.check_order_allowed("s1", now_ts=t0 + 61.0)
        self.assertTrue(allowed)

    def test_rate_limit_callback_fired(self):
        breached = []

        def on_limit(reason, count, limit):
            breached.append((reason, count, limit))

        guard = OrderRateGuard(
            max_orders_per_min=1,
            min_order_interval_sec=1.0,
            global_min_interval_sec=0.1,
            on_rate_limit_callback=on_limit,
        )
        t0 = 1000.0
        guard.record_order("s1", now_ts=t0)

        # Next order should fail and trigger callback
        res = guard.can_send_order("s1", now_ts=t0 + 0.05)
        self.assertFalse(res)
        self.assertEqual(len(breached), 1)
        self.assertEqual(guard.order_reject_count, 1)

    def test_cancel_guard(self):
        guard = OrderRateGuard(max_cancels_per_min=2, min_cancel_interval_sec=1.0)
        t0 = 1000.0

        self.assertTrue(guard.can_cancel_order(now_ts=t0))
        guard.record_cancel(now_ts=t0)

        # Immediately: rejected by min_cancel_interval
        self.assertFalse(guard.can_cancel_order(now_ts=t0 + 0.5))

        # 1.5s later: allowed
        self.assertTrue(guard.can_cancel_order(now_ts=t0 + 1.5))
        guard.record_cancel(now_ts=t0 + 1.5)

        # Now 2 cancels recorded = limit reached
        self.assertFalse(guard.can_cancel_order(now_ts=t0 + 3.0))


if __name__ == "__main__":
    unittest.main()
