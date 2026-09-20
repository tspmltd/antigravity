"""
Test Suite for Adverse Score Engine & Final Deliverable Architecture
===================================================================
Score構成:
  - 30% AE
  - 25% Toxic Flow
  - 20% Capture Loss
  - 15% Latency
  - 10% Inventory
出力区分:
  - 0-30   : 安全
  - 30-60  : 注意
  - 60-80  : 危険
  - 80-100 : 発注禁止
"""

import os
import sys
import shutil
import tempfile
import unittest

BASE_DIR = "/home/azureuser/antigravity"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.quant_pipeline.adverse_score_engine import AdverseScoreEngine


class TestAdverseScoreSynthesis(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.engine = AdverseScoreEngine(save_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_safe_tier(self):
        """0-30: 安全 (SAFE) の検証"""
        res = self.engine.calculate_adverse_score(
            ae_1s=0.5,           # 順行 (+0.5bp) -> AEスコア 0
            ae_3s=1.2,
            mae_bp=0.0,
            toxic_score=15.0,    # 低Toxic
            capture_rate_pct=85.0,# 優秀 (>80%) -> Captureスコア 0
            latency_ms=30.0,     # <50ms -> Latencyスコア 0
            inventory_btc=0.0,   # FLAT -> Inventoryスコア 0
            holding_time_sec=0.0,
        )
        score = res["adverse_score"]
        self.assertLess(score, 30.0)
        self.assertEqual(res["tier"], "安全")
        self.assertEqual(res["tier_code"], "SAFE")
        self.assertEqual(res["action"], "allow")

    def test_caution_tier(self):
        """30-60: 注意 (CAUTION) の検証"""
        res = self.engine.calculate_adverse_score(
            ae_1s=-0.8,          # 微小逆行 -> AEスコア 20
            ae_3s=-0.5,
            mae_bp=-1.0,
            toxic_score=45.0,    # 中立Toxic
            capture_rate_pct=60.0,# 普通 -> Captureスコア ~26
            latency_ms=65.0,     # 50-80ms -> Latencyスコア 20
            inventory_btc=0.001, # 在庫あり (拘束60s)
            holding_time_sec=60.0,
        )
        score = res["adverse_score"]
        self.assertGreaterEqual(score, 30.0)
        self.assertLess(score, 60.0)
        self.assertEqual(res["tier"], "注意")
        self.assertEqual(res["tier_code"], "CAUTION")

    def test_warning_tier(self):
        """60-80: 危険 (WARNING / ロット半減) の検証"""
        res = self.engine.calculate_adverse_score(
            ae_1s=-2.5,          # 逆行中 -> AEスコア 62.5
            ae_3s=-2.0,
            mae_bp=-3.0,
            toxic_score=75.0,    # 高Toxic
            capture_rate_pct=30.0,# 要改善 -> Captureスコア ~54
            latency_ms=95.0,     # 80-120ms -> Latencyスコア 55
            inventory_btc=0.002, # 在庫滞留 200s
            holding_time_sec=200.0,
        )
        score = res["adverse_score"]
        self.assertGreaterEqual(score, 60.0)
        self.assertLess(score, 80.0)
        self.assertEqual(res["tier"], "危険")
        self.assertEqual(res["tier_code"], "WARNING")
        self.assertEqual(res["action"], "halve_size_and_caution")

    def test_veto_tier(self):
        """80-100: 発注禁止 (HARD_VETO / HALT) の検証"""
        res = self.engine.calculate_adverse_score(
            ae_1s=-4.0,          # 致命的逆行 -> AEスコア 100
            ae_3s=-5.0,          # 損失拡大加点
            mae_bp=-6.0,
            toxic_score=95.0,    # トキシック直撃
            capture_rate_pct=-20.0,# スプレッド丸負け
            latency_ms=135.0,    # 遅延深刻 (120ms超)
            inventory_btc=0.005, # 最大枠スタック 600s
            holding_time_sec=600.0,
        )
        score = res["adverse_score"]
        self.assertGreaterEqual(score, 80.0)
        self.assertEqual(res["tier"], "発注禁止")
        self.assertEqual(res["tier_code"], "HARD_VETO")
        self.assertEqual(res["action"], "cancel_and_veto")


if __name__ == "__main__":
    unittest.main()
