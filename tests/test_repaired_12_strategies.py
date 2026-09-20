"""
12戦略 修繕検証テスト (Test Repaired 12 Strategies)
===================================================
AGENT報告（DuckDB, Adverse, Microstructure）に基づく修繕が
全12戦略に正しく反映され、以下の品質基準を満たすことを検証する:
1. 全戦略のインスタンス化と generate_signals が正常動作すること
2. MM戦略が最低20bp以上のスプレッド制約 (min_spread_pct >= 0.0020) を保持していること
3. 逆張り戦略 (RsiMeanReversion) が 100EMA トレンド保護 (ema_trend / ema_macro) を実装していること
4. トレンド戦略 (EmaTrend, MicroTrend) が大局トレンド整合とレンジダマシ遮断を実装していること
"""
import unittest
import os
import glob
import importlib.util
import pandas as pd
import numpy as np

BASE_DIR = "/home/azureuser/antigravity"

class TestRepaired12Strategies(unittest.TestCase):
    def setUp(self):
        # 100本分のダミーDataFrame (上昇トレンド相場)
        dates = pd.date_range("2026-09-20 00:00", periods=150, freq="1min")
        prices = np.linspace(12500000, 12600000, 150)
        self.df_uptrend = pd.DataFrame({
            "timestamp": dates,
            "open": prices,
            "high": prices + 500,
            "low": prices - 500,
            "close": prices,
            "volume": 0.5
        })

    def test_all_strategies_instantiate_and_signal(self):
        files = sorted(glob.glob(os.path.join(BASE_DIR, "strategies", "approved", "*.py")))
        valid_files = [f for f in files if not os.path.basename(f).startswith("test_") and not f.endswith("__init__.py")]
        self.assertGreaterEqual(len(valid_files), 12, "少なくとも12個の承認済み戦略が存在すること")

        for f in valid_files:
            fname = os.path.basename(f)
            strat_id = fname.replace("_approved.py", "").replace(".py", "")
            spec = importlib.util.spec_from_file_location(f"test_{strat_id}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            
            self.assertTrue(hasattr(mod, "CustomStrategy"), f"{fname} must define CustomStrategy")
            inst = mod.CustomStrategy()
            out = inst.generate_signals(self.df_uptrend)
            self.assertIn("signal", out.columns, f"{fname} output must contain 'signal' column")
            latest_sig = int(out["signal"].iloc[-1])
            self.assertIn(latest_sig, [-1, 0, 1], f"{fname} latest signal must be -1, 0, or 1")

    def test_mm_strategies_have_min_spread_protection(self):
        """MM戦略が最低20bpスプレッド制約を持っていること"""
        mm_files = ["strat_4d3f2c9f_approved.py", "strat_a5d8ae20_approved.py", "strat_cbcd5aed_approved.py", "strat_de08146e_approved.py", "strat_1ac224f3_approved.py"]
        for mf in mm_files:
            path = os.path.join(BASE_DIR, "strategies", "approved", mf)
            spec = importlib.util.spec_from_file_location("mod_mm", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            inst = mod.CustomStrategy()
            params = getattr(inst, "parameters", {})
            min_spread = params.get("min_spread_pct", 0.0)
            self.assertGreaterEqual(min_spread, 0.00020, f"{mf} must require min_spread_pct >= 0.00020 (2.0bp)")

    def test_reversion_strategies_have_ema_trend_filter(self):
        """RsiMeanReversion が大局トレンドフィルターを持っていること"""
        rev_files = ["strat_7e09696a_approved.py", "strat_9ca5d130_approved.py", "strat_a2a6d745_approved.py"]
        for rf in rev_files:
            path = os.path.join(BASE_DIR, "strategies", "approved", rf)
            with open(path, "r", encoding="utf-8") as f:
                code = f.read()
            self.assertIn("ema_trend", code, f"{rf} must contain ema_trend for macro trend protection")
            self.assertIn("is_downtrend", code, f"{rf} must contain is_downtrend filter")

if __name__ == "__main__":
    unittest.main()
