"""WOBIMM4 — UKI laws + LIVE lessons: live-edge ENTER, symmetric flatten wait."""
from __future__ import annotations

import unittest

from antigravity.quant_pipeline.wobimm4_policy import (
    ADVERSE_CUT_BP,
    CONFIRM_TICKS,
    ENTER,
    HONEST_LONG_OFI,
    HONEST_LONG_WOBI,
    EXIT,
    KEEP,
    MAX_INV_BTC,
    PULL,
    SIZE_BTC,
    WAIT,
    Wobimm4State,
    decide,
    invert_against,
    signed_signal,
    step_confirm,
)


class TestWobimm4Policy(unittest.TestCase):
    def test_one_lot_no_stack(self):
        self.assertEqual(SIZE_BTC, 0.001)
        self.assertEqual(MAX_INV_BTC, 0.001)

    def test_signal_requires_wobi_and_ofi(self):
        self.assertEqual(signed_signal(0.50, 0.00)[0], 0)
        self.assertEqual(signed_signal(0.00, 0.10)[0], 0)
        self.assertEqual(signed_signal(0.25, 0.03)[0], 1)
        self.assertEqual(signed_signal(-0.25, -0.03)[0], -1)

    def test_enter_buy_on_pressure(self):
        action, side, _ = decide(Wobimm4State(wobi=0.30, recent_ofi=0.05, spread_bp=1.2))
        self.assertEqual(action, ENTER)
        self.assertEqual(side, "BUY")

    def test_honest_long_waits_below_tight_gate(self):
        self.assertGreater(HONEST_LONG_WOBI, 0.20)
        self.assertGreater(HONEST_LONG_OFI, 0.02)
        action, side, reason = decide(
            Wobimm4State(wobi=0.30, recent_ofi=0.05, spread_bp=1.2, honest_fill=True)
        )
        self.assertEqual(action, WAIT)
        self.assertIsNone(side)
        self.assertIn("long_tight", reason)

    def test_honest_long_enters_when_tight_gate_clears(self):
        action, side, _ = decide(
            Wobimm4State(wobi=0.40, recent_ofi=0.08, spread_bp=1.2, honest_fill=True)
        )
        self.assertEqual(action, ENTER)
        self.assertEqual(side, "BUY")

    def test_honest_short_keeps_base_gate(self):
        action, side, _ = decide(
            Wobimm4State(wobi=-0.25, recent_ofi=-0.03, spread_bp=1.2, honest_fill=True)
        )
        self.assertEqual(action, ENTER)
        self.assertEqual(side, "SELL")

    def test_enter_sell_on_pressure(self):
        action, side, _ = decide(Wobimm4State(wobi=-0.30, recent_ofi=-0.05, spread_bp=1.2))
        self.assertEqual(action, ENTER)
        self.assertEqual(side, "SELL")

    def test_no_edge_waits(self):
        action, side, _ = decide(Wobimm4State(wobi=0.05, recent_ofi=0.00, spread_bp=1.2))
        self.assertEqual(action, WAIT)
        self.assertIsNone(side)

    def test_hold_keeps_working_same_signal(self):
        action, side, reason = decide(
            Wobimm4State(working_side="BUY", wobi=0.30, recent_ofi=0.05, spread_bp=1.2)
        )
        self.assertEqual(action, KEEP)
        self.assertEqual(side, "BUY")
        self.assertEqual(reason, "working_quote")

    def test_signal_flip_pulls_working_not_timer(self):
        action, _, reason = decide(
            Wobimm4State(working_side="BUY", wobi=-0.30, recent_ofi=-0.05, spread_bp=1.2)
        )
        self.assertEqual(action, PULL)
        self.assertIn("signal_flip", reason)

    def test_noise_ofi_keeps_working(self):
        action, side, reason = decide(
            Wobimm4State(working_side="BUY", wobi=0.05, recent_ofi=0.0, spread_bp=1.2)
        )
        self.assertEqual(action, KEEP)
        self.assertEqual(side, "BUY")
        self.assertEqual(reason, "working_quote")

    def test_no_live_edge_flattens_long(self):
        action, side, reason = decide(
            Wobimm4State(
                net_lots=1,
                wobi=-0.05,
                recent_ofi=0.00,
                spread_bp=1.2,
                signal=-1,
            )
        )
        self.assertEqual(action, EXIT)
        self.assertEqual(side, "SELL")
        self.assertIn("ev_gone", reason)

    def test_against_edge_flattens_short(self):
        """Opposite WOBI∩OFI is not EV. Flatten. Do not cover-wait."""
        action, side, reason = decide(
            Wobimm4State(
                net_lots=-1,
                wobi=0.30,
                recent_ofi=0.05,
                spread_bp=1.2,
                signal=-1,
            )
        )
        self.assertEqual(action, EXIT)
        self.assertEqual(side, "BUY")
        self.assertIn("ev_gone", reason)

    def test_latch_is_not_ev_for_a_short(self):
        action, side, reason = decide(
            Wobimm4State(
                net_lots=-1,
                wobi=0.05,
                recent_ofi=0.00,
                spread_bp=1.2,
                signal=-1,
                signal_why="latched SELL",
            )
        )
        self.assertEqual(action, EXIT)
        self.assertEqual(side, "BUY")
        self.assertIn("ev_gone", reason)

    def test_live_ev_holds_long(self):
        action, side, reason = decide(
            Wobimm4State(net_lots=1, wobi=0.40, recent_ofi=0.08, spread_bp=1.2)
        )
        self.assertEqual(action, KEEP)
        self.assertIsNone(side)
        self.assertIn("ev_hold", reason)

    def test_live_ev_holds_short(self):
        action, side, reason = decide(
            Wobimm4State(net_lots=-1, wobi=-0.40, recent_ofi=-0.08, spread_bp=1.2)
        )
        self.assertEqual(action, KEEP)
        self.assertIsNone(side)
        self.assertIn("ev_hold", reason)

    def test_instant_harvest_exits_long_while_live_ev(self):
        action, side, reason = decide(
            Wobimm4State(
                net_lots=1,
                wobi=0.40,
                recent_ofi=0.08,
                spread_bp=1.2,
                instant_harvest=True,
            )
        )
        self.assertEqual(action, EXIT)
        self.assertEqual(side, "SELL")
        self.assertIn("harvest", reason)

    def test_instant_harvest_exits_short_while_live_ev(self):
        action, side, reason = decide(
            Wobimm4State(
                net_lots=-1,
                working_side="BUY",
                wobi=-0.40,
                recent_ofi=-0.08,
                spread_bp=1.2,
                instant_harvest=True,
            )
        )
        self.assertEqual(action, EXIT)
        self.assertEqual(side, "BUY")
        self.assertIn("harvest", reason)

    def test_ofi_alone_is_not_ev(self):
        action, side, reason = decide(
            Wobimm4State(net_lots=-1, wobi=-0.40, recent_ofi=0.05, spread_bp=1.2)
        )
        self.assertEqual(action, EXIT)
        self.assertEqual(side, "BUY")
        self.assertIn("ev_gone", reason)

    def test_wide_spread_still_enters_as_maker(self):
        action, side, reason = decide(
            Wobimm4State(wobi=0.40, recent_ofi=0.08, spread_bp=3.5)
        )
        self.assertEqual(action, ENTER)
        self.assertEqual(side, "BUY")
        self.assertNotIn("SPREAD_TOO_WIDE", reason)

    def test_confirm_needs_consecutive_ticks(self):
        p = s = c = 0
        p, s, c = step_confirm(p, s, c, 1)
        self.assertEqual(c, 0)
        p, s, c = step_confirm(p, s, c, 1)
        self.assertEqual(c, 0)
        p, s, c = step_confirm(p, s, c, 1)
        self.assertEqual(c, 1)
        self.assertEqual(s, CONFIRM_TICKS)

    def test_confirm_zero_does_not_clear_live_side(self):
        p = s = c = 0
        for _ in range(CONFIRM_TICKS):
            p, s, c = step_confirm(p, s, c, -1)
        self.assertEqual(c, -1)
        p, s, c = step_confirm(p, s, c, 0)
        self.assertEqual(c, -1)
        self.assertEqual(p, 0)

    def test_confirm_flip_needs_full_streak(self):
        p = s = c = 0
        for _ in range(CONFIRM_TICKS):
            p, s, c = step_confirm(p, s, c, 1)
        p, s, c = step_confirm(p, s, c, -1)
        self.assertEqual(c, 1)
        p, s, c = step_confirm(p, s, c, -1)
        self.assertEqual(c, 1)
        p, s, c = step_confirm(p, s, c, -1)
        self.assertEqual(c, -1)

    def test_unconfirmed_signal_waits_to_enter(self):
        action, side, _ = decide(
            Wobimm4State(wobi=0.40, recent_ofi=0.08, spread_bp=1.2, signal=0, signal_why="confirming")
        )
        self.assertEqual(action, WAIT)
        self.assertIsNone(side)

    def test_adverse_cut_exits_long_without_waiting_for_flip(self):
        action, side, reason = decide(
            Wobimm4State(
                net_lots=1,
                wobi=0.40,
                recent_ofi=0.08,
                spread_bp=1.2,
                unrealized_bp=-ADVERSE_CUT_BP,
            )
        )
        self.assertEqual(action, EXIT)
        self.assertEqual(side, "SELL")
        self.assertIn("adverse_cut", reason)

    def test_adverse_cut_covers_short_even_in_buy_pressure(self):
        action, side, reason = decide(
            Wobimm4State(
                net_lots=-1,
                wobi=0.40,
                recent_ofi=0.08,
                spread_bp=1.2,
                signal=1,
                unrealized_bp=-2.4,
            )
        )
        self.assertEqual(action, EXIT)
        self.assertEqual(side, "BUY")
        self.assertIn("adverse_cut", reason)

    def test_sell_pressure_flattens_long(self):
        action, side, reason = decide(
            Wobimm4State(
                net_lots=1,
                wobi=-0.30,
                recent_ofi=-0.05,
                spread_bp=1.2,
                signal=-1,
                signal_why="SELL_PRESSURE",
            )
        )
        self.assertEqual(action, EXIT)
        self.assertEqual(side, "SELL")
        self.assertIn("ev_gone", reason)
        self.assertTrue(invert_against("BUY", -0.40, -0.20))

    def test_working_buy_invert_pulls(self):
        action, side, reason = decide(
            Wobimm4State(working_side="BUY", wobi=-0.40, recent_ofi=-0.20, spread_bp=1.2, signal=1)
        )
        self.assertEqual(action, PULL)
        self.assertEqual(side, "BUY")
        self.assertEqual(reason, "edge_invert")

    def test_working_left_behind_does_not_drift_pull(self):
        """0.5bp drift pull cancelled ENTER before fill. Invert / flip only."""
        action, side, reason = decide(
            Wobimm4State(
                working_side="BUY",
                wobi=0.30,
                recent_ofi=0.05,
                spread_bp=1.2,
                drift_bp=0.8,
            )
        )
        self.assertEqual(action, KEEP)
        self.assertEqual(side, "BUY")
        self.assertEqual(reason, "working_quote")

    def test_mild_opposite_does_not_invert_working(self):
        action, side, reason = decide(
            Wobimm4State(
                working_side="BUY",
                wobi=-0.25,
                recent_ofi=-0.05,
                spread_bp=1.2,
                signal=1,
                signal_why="still confirmed BUY",
            )
        )
        self.assertEqual(action, KEEP)
        self.assertEqual(side, "BUY")
        self.assertEqual(reason, "working_quote")
        self.assertFalse(invert_against("BUY", -0.25, -0.05))

    def test_latched_buy_does_not_enter_on_no_edge(self):
        """Paper hole: ENTER NO_EDGE after flatten because confirm latched."""
        action, side, reason = decide(
            Wobimm4State(
                wobi=0.75,
                recent_ofi=0.00,
                spread_bp=1.2,
                signal=1,
                signal_why="NO_EDGE (wobi=+0.75 ofi=+0.00)",
            )
        )
        self.assertEqual(action, WAIT)
        self.assertIsNone(side)
        self.assertIn("live_disagree", reason)

    def test_latched_buy_does_not_enter_while_confirming_sell(self):
        action, side, reason = decide(
            Wobimm4State(
                wobi=-0.28,
                recent_ofi=-0.08,
                spread_bp=1.2,
                signal=1,
                signal_why="confirming_flip SELL_PRESSURE 1/3",
            )
        )
        self.assertEqual(action, WAIT)
        self.assertIsNone(side)
        self.assertIn("live_disagree", reason)

    def test_latched_buy_enters_when_live_still_agrees(self):
        action, side, _ = decide(
            Wobimm4State(
                wobi=0.30,
                recent_ofi=0.05,
                spread_bp=1.2,
                signal=1,
                signal_why="BUY_PRESSURE",
            )
        )
        self.assertEqual(action, ENTER)
        self.assertEqual(side, "BUY")

    def test_wide_spread_still_holds_when_ev_alive(self):
        action, side, reason = decide(
            Wobimm4State(net_lots=1, wobi=0.40, recent_ofi=0.08, spread_bp=3.5)
        )
        self.assertEqual(action, KEEP)
        self.assertIsNone(side)
        self.assertIn("ev_hold", reason)

    def test_wide_spread_does_not_keep_a_dead_position(self):
        action, side, reason = decide(
            Wobimm4State(
                net_lots=1,
                working_side="SELL",
                wobi=-0.10,
                recent_ofi=0.00,
                spread_bp=3.1,
            )
        )
        self.assertEqual(action, EXIT)
        self.assertEqual(side, "SELL")
        self.assertIn("ev_gone", reason)

    def test_wide_spread_keeps_flat_entry_quote(self):
        action, side, reason = decide(
            Wobimm4State(working_side="BUY", wobi=0.40, recent_ofi=0.08, spread_bp=3.5)
        )
        self.assertEqual(action, KEEP)
        self.assertEqual(side, "BUY")
        self.assertEqual(reason, "working_quote")

    def test_ev_hold_pulls_flatten_quote(self):
        action, side, reason = decide(
            Wobimm4State(
                net_lots=-1,
                working_side="BUY",
                wobi=-0.30,
                recent_ofi=-0.05,
                spread_bp=1.2,
            )
        )
        self.assertEqual(action, PULL)
        self.assertEqual(side, "BUY")
        self.assertIn("ev_hold", reason)

    def test_against_edge_keeps_working_flatten(self):
        action, side, reason = decide(
            Wobimm4State(
                net_lots=1,
                working_side="SELL",
                wobi=-0.30,
                recent_ofi=-0.05,
                spread_bp=1.2,
                honest_fill=True,
            )
        )
        self.assertEqual(action, EXIT)
        self.assertEqual(side, "SELL")
        self.assertIn("ev_gone", reason)


if __name__ == "__main__":
    unittest.main()
