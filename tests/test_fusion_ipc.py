"""
tests/test_fusion_ipc.py
========================
Python司令塔 (Regime Orchestrator) と Go Fusion Engine 間の
Unix Domain Socket (UDS) 双方向IPC結合テスト。
"""

import os
import time
import unittest
import subprocess
import requests

from antigravity.multi_asset.ipc_bridge import FusionEngineIPCClient
from antigravity.multi_asset.regime_orchestrator import RegimeOrchestratorAgent
from antigravity.multi_asset.schemas import MacroImpact
from antigravity.multi_asset.pods.fx import FxPod


class TestFusionEngineIPC(unittest.TestCase):
    TEST_SOCK = "/tmp/test_py_fusion.sock"
    TEST_PORT = 9191
    GO_BIN = "/home/azureuser/antigravity/fusion_engine/fusion_engine"

    @classmethod
    def setUpClass(cls):
        # 既存ソケットのクリーンアップ
        if os.path.exists(cls.TEST_SOCK):
            os.remove(cls.TEST_SOCK)

        # Go Fusion Engine デーモンをサブプロセス起動
        cls.go_proc = subprocess.Popen(
            [
                cls.GO_BIN,
                "--mode=daemon",
                "--symbol=USDJPY",
                f"--ipc-sock={cls.TEST_SOCK}",
                f"--port={cls.TEST_PORT}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # 起動待機 (ソケットファイル生成待機: 最大5秒)
        for _ in range(50):
            if os.path.exists(cls.TEST_SOCK):
                break
            time.sleep(0.1)

        cls.client = FusionEngineIPCClient(socket_path=cls.TEST_SOCK, timeout=2.0)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "go_proc") and cls.go_proc:
            cls.go_proc.terminate()
            try:
                cls.go_proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                cls.go_proc.kill()

        if os.path.exists(cls.TEST_SOCK):
            os.remove(cls.TEST_SOCK)

    def test_01_ipc_ping_and_get_state(self):
        """Goエンジンとの死活確認および状態取得"""
        self.assertTrue(self.client.is_socket_available(), "Socket file must exist")
        self.assertTrue(self.client.ping(), "Ping must return True")

        state = self.client.get_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.get("status"), "OK")
        gov = state.get("governance", {})
        self.assertFalse(gov.get("is_halted"))
        metrics = state.get("metrics", {})
        self.assertEqual(metrics.get("symbol"), "USDJPY")

    def test_02_governance_stop_circuit_breaker(self):
        """STOP 命令による物理遮断伝達"""
        ok = self.client.stop(reason="TEST CRITICAL SHOCK")
        self.assertTrue(ok)

        state = self.client.get_state()
        gov = state.get("governance", {})
        self.assertTrue(gov.get("is_halted"))
        self.assertEqual(gov.get("target_mode"), "STOP")

    def test_03_governance_reduce_50(self):
        """REDUCE_50 命令伝達"""
        ok = self.client.reduce_50(reason="TEST RISK OFF")
        self.assertTrue(ok)

        state = self.client.get_state()
        gov = state.get("governance", {})
        self.assertFalse(gov.get("is_halted"))
        self.assertEqual(gov.get("target_mode"), "REDUCE_50")
        self.assertEqual(gov.get("risk_multiplier"), 0.5)

    def test_04_governance_resume(self):
        """RESUME 命令伝達"""
        ok = self.client.resume(target_mode="HYBRID", risk_multiplier=1.0, reason="TEST RECOVERY")
        self.assertTrue(ok)

        state = self.client.get_state()
        gov = state.get("governance", {})
        self.assertFalse(gov.get("is_halted"))
        self.assertEqual(gov.get("target_mode"), "HYBRID")
        self.assertEqual(gov.get("risk_multiplier"), 1.0)

    def test_05_regime_orchestrator_automatic_broadcast(self):
        """RegimeOrchestratorAgent がマクロショック時に自動でGoエンジンを物理遮断することを確認"""
        orch = RegimeOrchestratorAgent(ipc_client=self.client)
        fx_pod = FxPod()
        orch.register_pod(fx_pod)

        # 致命的マクロショック (MIS = 92)
        critical_macro = MacroImpact(
            impact_score=92,
            level="CRITICAL",
            primary_event="WORLD FINANCIAL SHOCK",
            global_regime="SHOCK",
            asset_impact_map={"FX": "BEAR", "BTC": "BEAR", "JP_STOCK": "BEAR"},
        )

        orch.coordinate_cycle(critical_macro, {"FX": {"bid": 155.0, "ask": 155.02}})

        # Goエンジンの状態確認
        state = self.client.get_state()
        gov = state.get("governance", {})
        self.assertTrue(gov.get("is_halted"), "Go Fusion Engine must be halted automatically by Orchestrator")
        self.assertEqual(gov.get("target_mode"), "STOP")

    def test_06_prometheus_metrics_endpoint(self):
        """Prometheus HTTP エンドポイント (:9191/metrics) の到達性・出力検証"""
        url = f"http://127.0.0.1:{self.TEST_PORT}/metrics"
        resp = requests.get(url, timeout=2.0)
        self.assertEqual(resp.status_code, 200)
        content = resp.text

        self.assertIn("fusion_latency_sig_to_ord_us", content)
        self.assertIn("fusion_hawkes_lambda_buy", content)
        self.assertIn("fusion_governance_halted", content)
        self.assertIn("fusion_pnl_total_jpy", content)

    def test_07_multi_asset_daemon_verification(self):
        """--symbols=USDJPY,BTCJPY,7203,NQ マルチアセットデーモンの起動・IPC・Prometheus検証"""
        multi_sock = "/tmp/test_multi_py_fusion.sock"
        multi_port = 9192
        if os.path.exists(multi_sock):
            os.remove(multi_sock)

        multi_proc = subprocess.Popen(
            [
                self.GO_BIN,
                "--mode=daemon",
                "--symbols=USDJPY,BTCJPY,7203,NQ",
                f"--ipc-sock={multi_sock}",
                f"--port={multi_port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            for _ in range(50):
                if os.path.exists(multi_sock):
                    break
                time.sleep(0.1)

            client = FusionEngineIPCClient(socket_path=multi_sock, timeout=2.0)
            self.assertTrue(client.is_socket_available())
            self.assertTrue(client.ping())

            state = client.get_state()
            self.assertEqual(state.get("status"), "OK")
            metrics = state.get("metrics", {})
            self.assertIn("multi_total_pnl_jpy", metrics)

            # Prometheus 検証 (ラベル付きメトリクス)
            url = f"http://127.0.0.1:{multi_port}/metrics"
            resp = requests.get(url, timeout=2.0)
            self.assertEqual(resp.status_code, 200)
            text = resp.text
            self.assertIn('symbol="USDJPY"', text)
            self.assertIn('symbol="BTCJPY"', text)
            self.assertIn('symbol="7203"', text)
            self.assertIn('symbol="NQ"', text)

            # REST /health 検証
            h_resp = requests.get(f"http://127.0.0.1:{multi_port}/health", timeout=2.0)
            self.assertEqual(h_resp.status_code, 200)
            h_json = h_resp.json()
            self.assertEqual(h_json.get("total_assets"), 4)
            self.assertIn("USDJPY", h_json.get("assets", {}))
            self.assertIn("BTCJPY", h_json.get("assets", {}))

        finally:
            multi_proc.terminate()
            try:
                multi_proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                multi_proc.kill()
            if os.path.exists(multi_sock):
                os.remove(multi_sock)


if __name__ == "__main__":
    unittest.main()
