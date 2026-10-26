"""WOBIMM2 COVER_WAIT — paper only, no venue POST."""
from __future__ import annotations

import unittest

from antigravity.quant_pipeline.run_dryrun_wobimm2 import (
    COVER_WAIT_TIMEOUT_SEC,
    CoverWaitMMManager,
)


def _book(bid=100.0, ask=102.0, bid_sz=0.2, ask_sz=0.05):
    bids = [{"price": bid - i, "size": bid_sz} for i in range(5)]
    asks = [{"price": ask + i, "size": ask_sz} for i in range(5)]
    return bids, asks


class CoverWaitTests(unittest.TestCase):
    def setUp(self):
        self.mgr = CoverWaitMMManager(lot_size=0.001, max_inv_btc=0.004)

    def test_flat_does_not_cover_wait(self):
        bids, asks = _book()
        q = self.mgr.compute_wobimm_quotes(bids, asks, inventory=0.0, ofi=0.08, short_hold_sec=1.0)
        self.assertFalse(q.pull_reason_bid.startswith("COVER_WAIT"))

    def test_short_buy_pressure_pulls_cover_bid(self):
        bids, asks = _book()
        q = self.mgr.compute_wobimm_quotes(bids, asks, inventory=-0.001, ofi=0.08, short_hold_sec=1.0)
        self.assertFalse(q.has_bid)
        self.assertTrue(q.pull_reason_bid.startswith("COVER_WAIT_BUY_PRESSURE"))

    def test_timeout_releases_cover_bid(self):
        bids, asks = _book()
        q = self.mgr.compute_wobimm_quotes(
            bids, asks, inventory=-0.001, ofi=0.08, short_hold_sec=COVER_WAIT_TIMEOUT_SEC + 0.1
        )
        self.assertFalse(q.pull_reason_bid.startswith("COVER_WAIT"))

    def test_no_buy_pressure_keeps_bid(self):
        bids, asks = _book(bid_sz=0.05, ask_sz=0.2)
        q = self.mgr.compute_wobimm_quotes(bids, asks, inventory=-0.001, ofi=-0.05, short_hold_sec=1.0)
        self.assertFalse(q.pull_reason_bid.startswith("COVER_WAIT"))


if __name__ == "__main__":
    unittest.main()
