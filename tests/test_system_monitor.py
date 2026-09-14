import unittest
from antigravity.risk_guard.system_monitor import SystemResourceMonitor
from antigravity.risk_guard.notifier import DiscordNotifier


class TestSystemResourceMonitor(unittest.TestCase):
    def setUp(self):
        self.monitor = SystemResourceMonitor()
        self.notifier = DiscordNotifier()

    def test_disk_metrics(self):
        disk = self.monitor.get_disk_metrics()
        self.assertIn("total_gb", disk)
        self.assertIn("used_gb", disk)
        self.assertIn("free_gb", disk)
        self.assertIn("used_pct", disk)
        self.assertGreater(disk["total_gb"], 0)
        self.assertTrue(0.0 <= disk["used_pct"] <= 100.0)

    def test_memory_metrics(self):
        mem = self.monitor.get_memory_metrics()
        self.assertIn("total_gb", mem)
        self.assertIn("used_pct", mem)
        self.assertGreater(mem["total_gb"], 0)
        self.assertTrue(0.0 <= mem["used_pct"] <= 100.0)

    def test_cpu_metrics(self):
        cpu = self.monitor.get_cpu_pct()
        self.assertIsInstance(cpu, float)
        self.assertTrue(0.0 <= cpu <= 100.0)

    def test_collect_all_and_remediate(self):
        metrics = self.monitor.collect_all_metrics()
        self.assertIn("disk", metrics)
        self.assertIn("memory", metrics)
        self.assertIn("cpu_pct", metrics)
        self.assertIn("is_warning", metrics)

        actions = self.monitor.execute_remediation(metrics)
        self.assertIsInstance(actions, list)
        self.assertGreater(len(actions), 0)

    def test_send_resource_report(self):
        metrics = self.monitor.collect_all_metrics()
        actions = self.monitor.execute_remediation(metrics)
        # Webhook未設定またはドライランでも例外が出ないことを確認
        res = self.notifier.send_system_resource_report(
            metrics=metrics,
            remediation_actions=actions,
            server_name="Test Server",
        )
        self.assertIsInstance(res, bool)


if __name__ == "__main__":
    unittest.main()
