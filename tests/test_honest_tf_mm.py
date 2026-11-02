"""HONEST TF / MM paper lanes. No venue POST."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from antigravity.quant_pipeline.run_dryrun_tf2b_wobi5_honest import TF2BWobi5HonestDryRun
from antigravity.quant_pipeline.run_dryrun_wobimm4_honest import WOBIMM4HonestDryRun


class TestHonestLanes(unittest.TestCase):
    def test_tf_honest_does_not_instant_fill(self):
        dry = TF2BWobi5HonestDryRun(market_source="shared_feeder")
        self.assertEqual(dry.LANE_NAME, "TF2B_WOBI5_HONEST")
        self.assertTrue(str(dry.state_path).endswith("dryrun_tf2b_wobi5_honest_state.json"))
        snap = SimpleNamespace(mid_price=100.0, best_bid=99.0, best_ask=101.0)
        m = {
            "spread_bp": 1.2,
            "wobi": 0.40,
            "z_score": 0.50,
            "recent_ofi": 0.20,
            "d_wobi_dt": 0.0,
            "ask_depletion_vel": 0.0,
            "micro_dev_bp": 0.0,
            "mid_price": 100.0,
        }
        with patch.object(dry, "_read_latest_depth", return_value=([], [])), patch.object(
            dry, "_poll_execs", return_value=[]
        ), patch.object(dry, "_extract_metrics", return_value=m), patch.object(
            dry, "plan_wobi3_pre_tick", return_value={"intent": "ENTER", "side": "buy", "reason": "TEST"}
        ), patch.object(
            dry, "plan_wobi3_post_tick", return_value={"intent": "HOLD"}
        ):
            dry._tick_pair(snap, now=10.0)
        self.assertIsNone(dry.wobi3.position_side)
        self.assertEqual(dry.quote_side, "buy")
        self.assertGreater(dry.honest_posts, 0)
        self.assertEqual(dry.honest_fills, 0)

    def test_tf_honest_keeps_bid_on_no_tf2bp(self):
        """NO_TF2BP is idle. Do not pull the working BUY."""
        dry = TF2BWobi5HonestDryRun(market_source="shared_feeder")
        snap = SimpleNamespace(mid_price=100.0, best_bid=99.0, best_ask=101.0)
        m = {
            "spread_bp": 1.2,
            "wobi": 0.40,
            "z_score": 0.50,
            "recent_ofi": 0.20,
            "d_wobi_dt": 0.0,
            "ask_depletion_vel": 0.0,
            "micro_dev_bp": 0.0,
            "mid_price": 100.0,
        }
        with patch.object(dry, "_read_latest_depth", return_value=([], [])), patch.object(
            dry, "_poll_execs", return_value=[]
        ), patch.object(dry, "_extract_metrics", return_value=m), patch.object(
            dry, "plan_wobi3_pre_tick", return_value={"intent": "ENTER", "side": "buy", "reason": "TEST"}
        ), patch.object(
            dry, "plan_wobi3_post_tick", return_value={"intent": "HOLD"}
        ):
            dry._tick_pair(snap, now=10.0)
        self.assertEqual(dry.quote_side, "buy")
        dry.wobi3.position_side = None
        with patch.object(dry, "_read_latest_depth", return_value=([], [])), patch.object(
            dry, "_poll_execs", return_value=[]
        ), patch.object(dry, "_extract_metrics", return_value=m), patch.object(
            dry, "plan_wobi3_pre_tick", return_value={"intent": "HOLD", "reason": "NO_AUTONOMOUS"}
        ), patch.object(
            dry, "plan_wobi3_post_tick", return_value={"intent": "HOLD", "reason": "NO_TF2BP"}
        ), patch.object(dry.wobi3, "on_tick", return_value={"action": "hold"}):
            dry._tick_pair(snap, now=11.0)
        self.assertEqual(dry.quote_side, "buy")
        self.assertEqual(dry.honest_cancels, 0)

    def test_tf_honest_leaves_bid_on_edge_drift(self):
        """0.5bp left-behind is not a pull. Quote must sit until invert."""
        dry = TF2BWobi5HonestDryRun(market_source="shared_feeder")
        snap = SimpleNamespace(mid_price=100.0, best_bid=99.0, best_ask=101.0)
        enter = {
            "spread_bp": 1.2,
            "wobi": 0.40,
            "z_score": 0.50,
            "recent_ofi": 0.20,
            "d_wobi_dt": 0.0,
            "ask_depletion_vel": 0.0,
            "micro_dev_bp": 0.0,
            "mid_price": 100.0,
        }
        with patch.object(dry, "_read_latest_depth", return_value=([], [])), patch.object(
            dry, "_poll_execs", return_value=[]
        ), patch.object(dry, "_extract_metrics", return_value=enter), patch.object(
            dry, "plan_wobi3_pre_tick", return_value={"intent": "ENTER", "side": "buy", "reason": "TEST"}
        ), patch.object(
            dry, "plan_wobi3_post_tick", return_value={"intent": "HOLD"}
        ):
            dry._tick_pair(snap, now=10.0)
        self.assertEqual(dry.quote_side, "buy")
        posted = dry.quote_px
        dry.wobi3.position_side = None
        faded = dict(enter, wobi=0.10, recent_ofi=0.00)
        drifted = SimpleNamespace(mid_price=100.006, best_bid=99.006, best_ask=101.006)
        with patch.object(dry, "_read_latest_depth", return_value=([], [])), patch.object(
            dry, "_poll_execs", return_value=[]
        ), patch.object(dry, "_extract_metrics", return_value=faded), patch.object(
            dry, "plan_wobi3_pre_tick", return_value={"intent": "HOLD", "reason": "NO_AUTONOMOUS"}
        ), patch.object(
            dry, "plan_wobi3_post_tick", return_value={"intent": "HOLD", "reason": "NO_TF2BP"}
        ), patch.object(dry.wobi3, "on_tick", return_value={"action": "hold"}):
            dry._tick_pair(drifted, now=11.0)
        self.assertEqual(dry.quote_side, "buy")
        self.assertEqual(dry.quote_px, posted)
        self.assertEqual(dry.honest_cancels, 0)

    def test_tf_honest_pulls_inverted_bid(self):
        dry = TF2BWobi5HonestDryRun(market_source="shared_feeder")
        snap = SimpleNamespace(mid_price=100.0, best_bid=99.0, best_ask=101.0)
        enter = {
            "spread_bp": 1.2,
            "wobi": 0.40,
            "z_score": 0.50,
            "recent_ofi": 0.20,
            "d_wobi_dt": 0.0,
            "ask_depletion_vel": 0.0,
            "micro_dev_bp": 0.0,
            "mid_price": 100.0,
        }
        invert = dict(enter, wobi=-0.40, recent_ofi=-0.20)
        with patch.object(dry, "_read_latest_depth", return_value=([], [])), patch.object(
            dry, "_poll_execs", return_value=[]
        ), patch.object(dry, "_extract_metrics", return_value=enter), patch.object(
            dry, "plan_wobi3_pre_tick", return_value={"intent": "ENTER", "side": "buy", "reason": "TEST"}
        ), patch.object(
            dry, "plan_wobi3_post_tick", return_value={"intent": "HOLD"}
        ):
            dry._tick_pair(snap, now=10.0)
        self.assertEqual(dry.quote_side, "buy")
        dry.wobi3.position_side = None
        with patch.object(dry, "_read_latest_depth", return_value=([], [])), patch.object(
            dry, "_poll_execs", return_value=[]
        ), patch.object(dry, "_extract_metrics", return_value=invert), patch.object(
            dry, "plan_wobi3_pre_tick", return_value={"intent": "HOLD", "reason": "NO_AUTONOMOUS"}
        ), patch.object(
            dry, "plan_wobi3_post_tick", return_value={"intent": "HOLD", "reason": "NO_TF2BP"}
        ), patch.object(dry.wobi3, "on_tick", return_value={"action": "hold"}):
            dry._tick_pair(snap, now=11.0)
        self.assertIsNone(dry.quote_side)
        self.assertGreaterEqual(dry.honest_cancels, 1)

    def test_tf_honest_posts_on_gate_without_tf2bp(self):
        """TF2BP buy is a 1-tick pulse. HONEST must post while the gate is true."""
        dry = TF2BWobi5HonestDryRun(market_source="shared_feeder")
        snap = SimpleNamespace(mid_price=100.0, best_bid=99.0, best_ask=101.0)
        m = {
            "spread_bp": 1.2,
            "wobi": 0.40,
            "z_score": 0.50,
            "recent_ofi": 0.20,
            "d_wobi_dt": 0.0,
            "ask_depletion_vel": 0.0,
            "micro_dev_bp": 0.0,
            "mid_price": 100.0,
        }
        dry.wobi3.position_side = None
        with patch.object(dry, "_read_latest_depth", return_value=([], [])), patch.object(
            dry, "_poll_execs", return_value=[]
        ), patch.object(dry, "_extract_metrics", return_value=m), patch.object(
            dry, "plan_wobi3_pre_tick", return_value={"intent": "HOLD", "reason": "NO_AUTONOMOUS"}
        ), patch.object(
            dry, "plan_wobi3_post_tick", return_value={"intent": "HOLD", "reason": "NO_TF2BP"}
        ), patch.object(dry.wobi3, "on_tick", return_value={"action": "hold"}):
            dry._tick_pair(snap, now=10.0)
        self.assertEqual(dry.quote_side, "buy")
        self.assertGreater(dry.honest_posts, 0)
        self.assertEqual(dry.honest_fills, 0)
        self.assertIsNotNone(dry._last_wobi3_verdict)
        self.assertTrue(dry._last_wobi3_verdict["allowed"])

    def _long(self, dry, entry=100.0):
        dry.wobi3.position_side = "buy"
        dry.wobi3.entry_price = entry
        dry.wobi3_trailing.entry_price = entry

    def test_tf_honest_holds_while_live_ev(self):
        dry = TF2BWobi5HonestDryRun(market_source="shared_feeder")
        self._long(dry)
        m = {
            "wobi": 0.40,
            "recent_ofi": 0.10,
            "spread_bp": 1.0,
            "z_score": 0.5,
            "micro_dev_bp": 0.0,
            "d_wobi_dt": 0.0,
            "ask_depletion_vel": 0.0,
        }
        should, tag, _, reason = dry._eval_wobi3_dynamic_exit(100.25, [], [], m, now=2.0)
        self.assertFalse(should)
        self.assertEqual(tag, "EV_HOLD")
        self.assertIn("ev_hold", reason)

    def test_tf_honest_holds_through_frontier_while_ev(self):
        dry = TF2BWobi5HonestDryRun(market_source="shared_feeder")
        self._long(dry)
        m = {
            "wobi": 0.40,
            "recent_ofi": 0.05,
            "spread_bp": 1.0,
            "z_score": 0.5,
            "micro_dev_bp": 0.0,
            "d_wobi_dt": 0.0,
            "ask_depletion_vel": 0.0,
        }
        should, tag, _, reason = dry._eval_wobi3_dynamic_exit(99.97, [], [], m, now=2.0)
        self.assertFalse(should)
        self.assertEqual(tag, "EV_HOLD")

    def test_tf_honest_flattens_when_ev_gone(self):
        dry = TF2BWobi5HonestDryRun(market_source="shared_feeder")
        self._long(dry)
        m = {
            "wobi": -0.10,
            "recent_ofi": -0.05,
            "spread_bp": 1.0,
            "z_score": 0.0,
            "micro_dev_bp": 0.0,
            "d_wobi_dt": 0.0,
            "ask_depletion_vel": 0.0,
        }
        should, tag, _, reason = dry._eval_wobi3_dynamic_exit(100.20, [], [], m, now=2.0)
        self.assertTrue(should)
        self.assertEqual(tag, "EV_GONE")
        self.assertIn("ev_gone", reason)

    def test_mm_honest_uses_board_imitation(self):
        dry = WOBIMM4HonestDryRun(market_source="shared_feeder")
        self.assertEqual(dry.LANE_NAME, "WOBIMM4_HONEST")
        self.assertEqual(dry.FILL_MODEL, "BOARD_IMITATION")
        self.assertFalse(dry.LONG_TIGHT)

    def test_mm_honest_predict_miss_pulls_stale_enter(self):
        dry = WOBIMM4HonestDryRun(market_source="shared_feeder")
        dry.working_side = "BUY"
        dry.working_px = 100.0
        dry.working_id = "SIM-BUY-1"
        dry._pending_fill = {
            "side": "BUY",
            "px": 100.0,
            "src": "BOARD_TRADE",
            "id": "SIM-BUY-1",
        }
        filled = dry._confirm_predicted_fill([], bid=110.0, ask=112.0)
        self.assertFalse(filled)
        self.assertIsNone(dry.working_side)
        self.assertIsNone(dry._pending_fill)
        self.assertGreaterEqual(dry.cancels, 1)

    def test_mm_honest_confirm_still_books(self):
        dry = WOBIMM4HonestDryRun(market_source="shared_feeder")
        self.assertTrue(dry.INSTANT_HARVEST)
        dry.working_side = "BUY"
        dry.working_px = 103.0
        dry.working_id = "SIM-BUY-2"
        dry._tape = {"wobi": 0.40, "recent_ofi": 0.08}
        dry._pending_fill = {
            "side": "BUY",
            "px": 103.0,
            "src": "BOARD_TRADE",
            "id": "SIM-BUY-2",
        }
        execs = [{"side": "SELL", "price": 103.0, "size": 0.01}]
        filled = dry._confirm_predicted_fill(execs, bid=103.0, ask=105.0)
        self.assertTrue(filled)
        self.assertEqual(dry.net, 1)
        self.assertEqual(dry.entry_src, "BOARD_TRADE")
        self.assertIsNone(dry._pending_fill)
        self.assertFalse(dry.BUY_ONLY)
        self.assertTrue(str(dry.ledger_path).endswith("wobimm4_honest_dryrun/paired_ledger.jsonl"))
        m = dry.metrics()
        self.assertEqual(m["model"], "WOBIMM4_HONEST")
        self.assertEqual(m["fill"], "BOARD_IMITATION")
        self.assertFalse(m["buy_only"])

    def test_mm_honest_confirm_skips_book_cross_enter(self):
        dry = WOBIMM4HonestDryRun(market_source="shared_feeder")
        dry.working_side = "BUY"
        dry.working_px = 103.0
        dry.working_id = "SIM-BUY-XC"
        dry.net = 0
        dry._tape = {"wobi": 0.40, "recent_ofi": 0.08}
        dry._pending_fill = {
            "side": "BUY",
            "px": 103.0,
            "src": "BOOK_CROSS",
            "id": "SIM-BUY-XC",
        }
        filled = dry._confirm_predicted_fill([], bid=100.0, ask=103.0)
        self.assertFalse(filled)
        self.assertEqual(dry.net, 0)
        self.assertIsNone(dry.working_side)
        self.assertIsNone(dry._pending_fill)

    def test_mm_honest_confirm_skips_enter_when_inverted(self):
        dry = WOBIMM4HonestDryRun(market_source="shared_feeder")
        dry.working_side = "BUY"
        dry.working_px = 103.0
        dry.working_id = "SIM-BUY-3"
        dry.net = 0
        dry._tape = {"wobi": -0.40, "recent_ofi": -0.10}
        dry._pending_fill = {
            "side": "BUY",
            "px": 103.0,
            "src": "BOOK_CROSS",
            "id": "SIM-BUY-3",
        }
        filled = dry._confirm_predicted_fill([], bid=100.0, ask=103.0)
        self.assertFalse(filled)
        self.assertEqual(dry.net, 0)
        self.assertIsNone(dry.working_side)
        self.assertIsNone(dry._pending_fill)

    def test_mm_honest_confirm_books_when_tape_is_silent(self):
        """NO_EDGE is silent. Do not skip a BOARD_TRADE ENTER."""
        dry = WOBIMM4HonestDryRun(market_source="shared_feeder")
        dry.working_side = "BUY"
        dry.working_px = 103.0
        dry.working_id = "SIM-BUY-5"
        dry.net = 0
        dry._tape = {"wobi": -0.22, "recent_ofi": -0.01}
        dry._pending_fill = {
            "side": "BUY",
            "px": 103.0,
            "src": "BOARD_TRADE",
            "id": "SIM-BUY-5",
        }
        execs = [{"side": "SELL", "price": 103.0, "size": 0.01}]
        filled = dry._confirm_predicted_fill(execs, bid=103.0, ask=105.0)
        self.assertTrue(filled)
        self.assertEqual(dry.net, 1)

    def test_mm_honest_confirm_still_flattens_when_ev_gone(self):
        dry = WOBIMM4HonestDryRun(market_source="shared_feeder")
        dry.net = -1
        dry.entry_px = 100.0
        dry.working_side = "BUY"
        dry.working_px = 103.0
        dry.working_id = "SIM-BUY-4"
        dry.pending_exit_why = "ev_gone NO_EDGE"
        dry._tape = {"wobi": 0.10, "recent_ofi": 0.00}
        dry._pending_fill = {
            "side": "BUY",
            "px": 103.0,
            "src": "BOOK_CROSS",
            "id": "SIM-BUY-4",
        }
        filled = dry._confirm_predicted_fill([], bid=100.0, ask=103.0)
        self.assertTrue(filled)
        self.assertEqual(dry.net, 0)

    def test_tf_board_posts_at_touch_when_wall_is_thick(self):
        dry = TF2BWobi5HonestDryRun(market_source="shared_feeder")
        snap = SimpleNamespace(mid_price=100.0, best_bid=99.0, best_ask=101.0)
        m = {
            "spread_bp": 2.0,
            "wobi": 0.40,
            "z_score": 0.50,
            "recent_ofi": 0.20,
            "d_wobi_dt": 0.0,
            "ask_depletion_vel": 0.0,
            "micro_dev_bp": 0.0,
            "mid_price": 100.0,
        }
        bids = [{"price": 99.0, "size": 0.5}]
        asks = [{"price": 101.0, "size": 0.1}]
        with patch.object(dry, "_read_latest_depth", return_value=(bids, asks)), patch.object(
            dry, "_poll_execs", return_value=[]
        ), patch.object(dry, "_extract_metrics", return_value=m), patch.object(
            dry, "plan_wobi3_pre_tick", return_value={"intent": "ENTER", "side": "buy", "reason": "TEST"}
        ), patch.object(
            dry, "plan_wobi3_post_tick", return_value={"intent": "HOLD"}
        ):
            dry._tick_pair(snap, now=10.0)
        self.assertEqual(dry.quote_side, "buy")
        self.assertEqual(dry.quote_px, 99.0)
        self.assertGreater(dry.honest_posts, 0)
        self.assertEqual(dry.honest_fills, 0)

    def test_tf_board_trade_fills_resting_bid(self):
        dry = TF2BWobi5HonestDryRun(market_source="shared_feeder")
        snap = SimpleNamespace(mid_price=100.0, best_bid=99.0, best_ask=101.0)
        m = {
            "spread_bp": 1.2,
            "wobi": 0.40,
            "z_score": 0.50,
            "recent_ofi": 0.20,
            "d_wobi_dt": 0.0,
            "ask_depletion_vel": 0.0,
            "micro_dev_bp": 0.0,
            "mid_price": 100.0,
        }
        with patch.object(dry, "_read_latest_depth", return_value=([], [])), patch.object(
            dry, "_poll_execs", return_value=[]
        ), patch.object(dry, "_extract_metrics", return_value=m), patch.object(
            dry, "plan_wobi3_pre_tick", return_value={"intent": "ENTER", "side": "buy", "reason": "TEST"}
        ), patch.object(
            dry, "plan_wobi3_post_tick", return_value={"intent": "HOLD"}
        ):
            dry._tick_pair(snap, now=10.0)
        self.assertEqual(dry.quote_side, "buy")
        self.assertEqual(dry.honest_fills, 0)
        dumps = [{"side": "SELL", "price": dry.quote_px, "size": 0.02}]
        with patch.object(dry, "_read_latest_depth", return_value=([], [])), patch.object(
            dry, "_poll_execs", return_value=dumps
        ), patch.object(dry, "_extract_metrics", return_value=m), patch.object(
            dry, "plan_wobi3_pre_tick", return_value={"intent": "HOLD", "reason": "NO_AUTONOMOUS"}
        ), patch.object(
            dry, "plan_wobi3_post_tick", return_value={"intent": "HOLD", "reason": "NO_TF2BP"}
        ), patch.object(dry.wobi3, "on_tick", return_value={"action": "hold"}):
            dry._tick_pair(snap, now=11.0)
        self.assertIsNone(dry.quote_side)
        self.assertGreaterEqual(dry.honest_fills, 1)


if __name__ == "__main__":
    unittest.main()
