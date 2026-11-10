"""LIVE-parity DRYRUN fill + Soak8-hard verdict (CSR-680)."""
from __future__ import annotations

import unittest

from antigravity.quant_pipeline import live_parity_dryrun as lp
from antigravity.quant_pipeline.run_dryrun_abs1_wobi_honest import ABS1WobiHonestDryRun
from antigravity.quant_pipeline.run_dryrun_vr1_wobi_honest import VR1WobiHonestDryRun


class TestLiveParityCommit(unittest.TestCase):
    def test_force_banned(self):
        ok, why = lp.live_parity_commit("HIT_BID_FORCE", "sell", 100.0, [])
        self.assertFalse(ok)
        self.assertIn("force", why)

    def test_book_cross_alone_parked(self):
        ok, why = lp.live_parity_commit("BOOK_CROSS", "buy", 100.0, [])
        self.assertFalse(ok)
        self.assertIn("parked", why)

    def test_board_trade_needs_size_through(self):
        execs = [{"side": "SELL", "price": 99.0, "size": 0.0005}]
        ok, _ = lp.live_parity_commit("BOARD_TRADE", "buy", 100.0, execs, lot_btc=0.001)
        self.assertFalse(ok)
        execs2 = [{"side": "SELL", "price": 99.0, "size": 0.002}]
        ok2, why2 = lp.live_parity_commit("BOARD_TRADE", "buy", 100.0, execs2, lot_btc=0.001)
        self.assertTrue(ok2)
        self.assertIn("size_through", why2)

    def test_fee_market_harder_than_maker(self):
        net_m, fee_m = lp.net_pnl_bp(2.0, "BOARD_TRADE")
        net_t, fee_t = lp.net_pnl_bp(2.0, "MARKET_SIM")
        self.assertGreater(fee_t, fee_m)
        self.assertLess(net_t, net_m)

    def test_verdict_n0_after_window_is_fail(self):
        v = lp.lane_verdict(n=0, harvest_ge_min=0, window_sec=1300.0)
        self.assertEqual(v["verdict"], "FAIL")
        self.assertEqual(v["economic_pass"], "NO")

    def test_verdict_short_window_insufficient(self):
        v = lp.lane_verdict(n=10, harvest_ge_min=2, window_sec=60.0)
        self.assertEqual(v["verdict"], "INSUFFICIENT")

    def test_verdict_no_harvest_fail(self):
        v = lp.lane_verdict(n=5, harvest_ge_min=0, window_sec=1300.0)
        self.assertEqual(v["verdict"], "FAIL")

    def test_verdict_observe_still_no_economic_pass(self):
        v = lp.lane_verdict(n=5, harvest_ge_min=1, window_sec=1300.0, net_pnl_bp_sum=3.0)
        self.assertEqual(v["verdict"], "OBSERVE")
        self.assertEqual(v["economic_pass"], "NO")


class TestAbs1Vr1ParityFlags(unittest.TestCase):
    def test_abs1_no_force_fill(self):
        self.assertFalse(ABS1WobiHonestDryRun.HS_FORCE_FILL)
        self.assertTrue(ABS1WobiHonestDryRun.LIVE_PARITY)

    def test_vr1_no_force_fill(self):
        self.assertFalse(VR1WobiHonestDryRun.HS_FORCE_FILL)
        self.assertTrue(VR1WobiHonestDryRun.LIVE_PARITY)


class TestFleetLiveParityFlags(unittest.TestCase):
    def test_ms_tf_mm_flags(self):
        from antigravity.quant_pipeline.run_dryrun_ms1_wobi_honest import MS1WobiHonestDryRun
        from antigravity.quant_pipeline.run_dryrun_ms2_wobi_honest import MS2WobiHonestDryRun
        from antigravity.quant_pipeline.run_dryrun_tf1a_wobi_honest import TF1AWobiHonestDryRun
        from antigravity.quant_pipeline.run_dryrun_tf2a_wobi_honest import TF2AWobiHonestDryRun
        from antigravity.quant_pipeline.run_dryrun_wobimm4_honest import WOBIMM4HonestDryRun
        from antigravity.quant_pipeline.run_dryrun_wobimm5_honest import WOBIMM5HonestDryRun

        for cls in (
            MS1WobiHonestDryRun,
            MS2WobiHonestDryRun,
            TF1AWobiHonestDryRun,
            TF2AWobiHonestDryRun,
            ABS1WobiHonestDryRun,
            VR1WobiHonestDryRun,
        ):
            self.assertFalse(cls.HS_FORCE_FILL, cls.__name__)
            self.assertTrue(cls.LIVE_PARITY, cls.__name__)
        self.assertEqual(WOBIMM4HonestDryRun.FILL_MODEL, "HONEST")
        self.assertTrue(WOBIMM4HonestDryRun.HONEST_STRICT_OWN_SIZE)
        self.assertTrue(WOBIMM4HonestDryRun.LIVE_PARITY)
        self.assertEqual(WOBIMM5HonestDryRun.FILL_MODEL, "HONEST")
        self.assertTrue(WOBIMM5HonestDryRun.HONEST_STRICT_OWN_SIZE)


if __name__ == "__main__":
    unittest.main()
