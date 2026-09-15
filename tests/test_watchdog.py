"""
Tests for WatchdogSentinel and urgent alerts
"""

import unittest
import os
import sys
from antigravity.risk_guard.watchdog import WatchdogSentinel
from antigravity.risk_guard.notifier import DiscordNotifier
from antigravity.risk_guard.circuit_breaker import PeakDrawdownCircuitBreaker


class TestWatchdogSentinel(unittest.TestCase):

    def setUp(self):
        self.sentinel = WatchdogSentinel(
            base_dir="/home/azureuser/antigravity",
            check_interval_sec=5.0,
            enable_auto_restart=False,
        )

    def test_pid_detection(self):
        """現在のPythonプロセスが検出できるかテスト"""
        # python のキーワードで検索
        pid = self.sentinel.get_service_pid(["python"])
        self.assertIsNotNone(pid)
        self.assertIsInstance(pid, int)

    def test_drawdown_warning_callback(self):
        """サーキットブレーカーの警戒水準コールバックのテスト"""
        warning_called = []
        trip_called = []

        def on_warn(reason, dd, pnl):
            warning_called.append((reason, dd, pnl))

        def on_trip(reason, dd, pnl):
            trip_called.append((reason, dd, pnl))

        cb = PeakDrawdownCircuitBreaker(
            max_drawdown_limit_jpy=1000.0,
            warning_ratio=0.70,
            on_warning_callback=on_warn,
            on_trip_callback=on_trip,
        )

        # 利益 +500
        cb.update(500.0)
        self.assertEqual(cb.peak_pnl, 500.0)

        # 下落: +500 -> -100 (DD: 600, 60% < 70%) -> No warning
        cb.update(-100.0)
        self.assertEqual(len(warning_called), 0)

        # 下落: +500 -> -250 (DD: 750, 75% >= 70%) -> Warning triggered!
        cb.update(-250.0)
        self.assertEqual(len(warning_called), 1)
        self.assertTrue(cb.is_warning_active)

        # 継続下落: +500 -> -300 (DD: 800) -> 既に警告済みなので再送なし
        cb.update(-300.0)
        self.assertEqual(len(warning_called), 1)

        # 限界突破: +500 -> -550 (DD: 1050 >= 1000) -> Trip triggered!
        cb.update(-550.0)
        self.assertEqual(len(trip_called), 1)
        self.assertTrue(cb.is_halted)

    def test_notifier_methods(self):
        """Notifierの各種アラートメソッドがエラーなく呼び出せるかテスト (Dry-run)"""
        notifier = DiscordNotifier(alert_webhook_url="")

        # 各アラートメソッドの構文・引数チェック
        self.assertFalse(notifier.send_system_down_alert("TestService", "Test Reason", "Log Snippet"))
        self.assertFalse(notifier.send_system_recovered_alert("TestService", "Recovery msg"))
        self.assertFalse(notifier.send_drawdown_alert(700.0, 1000.0, 500.0, -200.0, is_halted=False, reason="Test"))
        self.assertFalse(notifier.send_drawdown_alert(1100.0, 1000.0, 500.0, -600.0, is_halted=True, reason="Test Halt"))
        self.assertFalse(notifier.send_resource_pressure_alert(
            metrics={"disk": {"used_pct": 90.0, "free_gb": 1.0}, "memory": {"used_pct": 90.0, "free_gb": 0.5}, "cpu_pct": 95.0},
            trigger_reasons=["CPU高負荷", "メモリ逼迫"],
            remediation_actions=["GC実行"],
        ))


if __name__ == "__main__":
    unittest.main()
