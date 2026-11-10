"""ABS1 Absent Scalp — thin-book refuse + hard dwell (no venue)."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

from antigravity.quant_pipeline import run_dryrun_abs1_wobi_honest as abs1_mod
from antigravity.quant_pipeline.run_dryrun_abs1_wobi_honest import ABS1WobiHonestDryRun


class TestABS1Gates(unittest.TestCase):
    def _runner(self) -> ABS1WobiHonestDryRun:
        r = object.__new__(ABS1WobiHonestDryRun)
        r.MIN_ENTRY_SPREAD_BP = 2.5
        r.MAX_HOLD_SEC = 6.0
        r.POST_CUT_COOLDOWN_SEC = 20.0
        r.DYN_STOP_ENABLED = True
        r.DYN_STOP_CHECK_S = 4.0
        r.DYN_STOP_MFE_TOUCH_BP = 1.0
        r.ENTRY_VETO_ENABLED = False
        r.HARD_STOP_ENABLED = False
        r.spread_gate_blocks = 0
        r.absent_dwell_cuts = 0
        r.entry_cooldown_until = 0.0
        r.dyn_stop_cuts = 0
        r._entry_feat = {}
        r.ae = MagicMock()
        r.ae.should_veto_entry.return_value = (False, "")
        r.ae.is_hard_stop.return_value = (False, "")
        r.ae.path_toxic.return_value = (False, "")
        r.ae._find.return_value = None
        r.wobi3 = MagicMock()
        r.wobi3.position_side = "buy"
        return r

    def test_constants(self):
        self.assertEqual(ABS1WobiHonestDryRun.MIN_ENTRY_SPREAD_BP, 2.5)
        self.assertEqual(ABS1WobiHonestDryRun.MAX_HOLD_SEC, 6.0)
        self.assertEqual(ABS1WobiHonestDryRun.LANE_NAME, "ABS1_WOBI_HONEST")
        self.assertTrue(ABS1WobiHonestDryRun.MS1_MAKER_EXIT)
        self.assertFalse(ABS1WobiHonestDryRun.HS_FORCE_FILL)
        self.assertNotIn("EV_GONE", abs1_mod._FORCE_EXIT_TAGS)
        self.assertIn("ABSENT_DWELL", abs1_mod._FORCE_EXIT_TAGS)

    def test_ev_gone_ms1_identical_maker_wait(self):
        """CSR-682b: EV_GONE ∉ force tags → calculate_optimal_order_price only."""
        r = self._runner()
        r.quote_side = None
        r.quote_px = 0.0
        r.pending_exit_tag = ""
        r.pending_exit_reason = ""
        r.exit_posted_ts = 0.0
        r.honest_posts = 0
        r.working_id = ""
        r._last_wobi3_verdict = {}
        r._last_m = {}
        r._last_hb = time.time()
        r.queue_obs = MagicMock()
        r.ae.on_tick = MagicMock()
        r._extract_metrics = MagicMock(  # type: ignore[method-assign]
            return_value={
                "spread_bp": 3.0,
                "wobi": -0.1,
                "recent_ofi": -0.1,
                "mid_price": 10_000_000.0,
            }
        )
        r._read_latest_depth = MagicMock(return_value=([], []))  # type: ignore[method-assign]
        r._tf2bp_common = MagicMock(return_value={})  # type: ignore[method-assign]
        r._poll_execs = MagicMock(return_value=[])  # type: ignore[method-assign]
        r._maybe_fill_board = MagicMock()  # type: ignore[method-assign]
        r._record_gate = MagicMock(return_value=(True, "ok"))  # type: ignore[method-assign]
        r.plan_wobi3_pre_tick = MagicMock(  # type: ignore[method-assign]
            return_value={"intent": "EXIT", "tag": "EV_GONE", "reason": "ev_gone"}
        )
        r._parity_maybe_hard_exit = MagicMock(return_value=False)  # type: ignore[method-assign]
        r._parity_mark_exit_posted = MagicMock()  # type: ignore[method-assign]
        posted: list = []

        def _post(side, px, why):
            posted.append((side, px, why))

        r._post_quote = _post  # type: ignore[method-assign]
        with mock.patch.object(
            abs1_mod,
            "calculate_optimal_order_price",
            return_value=(10_001_000.0, "WALL_ANCHOR_L1"),
        ) as opt:
            snap = SimpleNamespace(
                mid_price=10_000_000.0, best_bid=9_999_000.0, best_ask=10_001_000.0
            )
            ABS1WobiHonestDryRun._tick_pair_inner(r, snap, time.time())
            opt.assert_called_once()
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][0], "sell")
        self.assertEqual(posted[0][1], 10_001_000.0)
        self.assertNotIn("SOFT_BID", posted[0][2])
        self.assertEqual(r.pending_exit_reason, "ev_gone")  # MS1 style: reason or desc
        r._parity_maybe_hard_exit.assert_not_called()
        r._parity_mark_exit_posted.assert_not_called()
        self.assertEqual(r.exit_posted_ts, 0.0)

    def test_thin_book_refused(self):
        r = self._runner()
        import antigravity.quant_pipeline.run_dryrun_ms2_wobi_honest as ms2

        with mock.patch.object(
            ms2.MS2WobiHonestDryRun, "_record_gate", return_value=(True, "ok")
        ):
            ok, why = ABS1WobiHonestDryRun._record_gate(r, {"spread_bp": 1.2})
        self.assertFalse(ok)
        self.assertIn("ABSENT_THIN_BOOK", why)
        self.assertEqual(r.spread_gate_blocks, 1)

    def test_wide_book_allowed(self):
        r = self._runner()
        import antigravity.quant_pipeline.run_dryrun_ms2_wobi_honest as ms2

        with mock.patch.object(
            ms2.MS2WobiHonestDryRun, "_record_gate", return_value=(True, "ok")
        ):
            ok, why = ABS1WobiHonestDryRun._record_gate(r, {"spread_bp": 3.0})
        self.assertTrue(ok)
        self.assertEqual(r.spread_gate_blocks, 0)

    def test_absent_dwell_cuts_even_if_ev_hold(self):
        r = self._runner()
        r.wobi3_trailing = SimpleNamespace(
            entry_price=10_000_000.0, entry_ts=time.time() - 7.0
        )
        r.wobi3.entry_price = 10_000_000.0
        r._live_long_ev = MagicMock(return_value=True)  # type: ignore[method-assign]
        cut, tag, _mark, why = ABS1WobiHonestDryRun._eval_wobi3_dynamic_exit(
            r,
            10_001_000.0,
            [],
            [],
            {"best_bid": 10_000_500.0, "wobi": 0.5, "recent_ofi": 0.1},
            time.time(),
        )
        self.assertTrue(cut)
        self.assertEqual(tag, "ABSENT_DWELL")
        self.assertIn("absent_dwell", why)
        self.assertEqual(r.absent_dwell_cuts, 1)
        self.assertGreater(r.entry_cooldown_until, time.time())


if __name__ == "__main__":
    unittest.main()
