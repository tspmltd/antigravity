"""CSR-683: HARD_STOP immediate MARKET · TF2A LEAD path OFF."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from antigravity.quant_pipeline.run_dryrun_tf1a_wobi_honest import TF1AWobiHonestDryRun
from antigravity.quant_pipeline.run_dryrun_tf2a_wobi_honest import TF2AWobiHonestDryRun


class TestCsr683TfHemostasis(unittest.TestCase):
    def test_tf2a_lead_path_off(self):
        self.assertFalse(TF2AWobiHonestDryRun.LEAD_PATH_CUT_ENABLED)
        self.assertTrue(TF2AWobiHonestDryRun.LEAD_ENTRY_VETO_ENABLED)

    def test_hard_stop_immediate_market_no_soft_wait(self):
        r = object.__new__(TF1AWobiHonestDryRun)
        r.HARD_STOP_BP = -4.0
        r.wobi3 = MagicMock()
        r.wobi3.position_side = "buy"
        r.pending_exit_tag = ""
        r.pending_exit_reason = ""
        r.exit_posted_ts = 99.0
        r.parity_market_sim = 0
        r.quote_side = None
        fills = []

        def _fill(side, px, src, *a, **k):
            fills.append((side, px, src))
            r.wobi3.position_side = None

        r._post_quote = MagicMock()
        r._on_honest_fill = _fill  # type: ignore[method-assign]
        TF1AWobiHonestDryRun._hard_stop_immediate_market(
            r,
            bid=9_999_000.0,
            snap=SimpleNamespace(),
            now=time.time(),
            m={},
            depth_bids=[],
            depth_asks=[],
            reason="stop_loss (-4.1bp <= -4.0bp)",
            unreal=-4.1,
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0][0], "sell")
        self.assertEqual(fills[0][2], "MARKET_SIM")
        self.assertIn("immediate", r.pending_exit_reason)
        self.assertEqual(r.exit_posted_ts, 0.0)
        self.assertEqual(r.parity_market_sim, 1)
        r._post_quote.assert_called_once()


if __name__ == "__main__":
    unittest.main()
