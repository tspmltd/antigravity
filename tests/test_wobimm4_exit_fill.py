"""WOBIMM4 DRYRUN: board-imitation predict + venue confirm. No bp trigger."""
from __future__ import annotations

import unittest

from antigravity.quant_pipeline.run_dryrun_wobimm4 import (
    board_predicts_fill,
    exit_limit_px,
    exit_quote_fillable,
    maker_px,
)


class TestWobimm4ExitFill(unittest.TestCase):
    def test_exit_buy_is_ask_minus_one(self):
        self.assertEqual(exit_limit_px("BUY", 100.0, 103.0), 102.0)

    def test_exit_sell_is_bid_plus_one(self):
        self.assertEqual(exit_limit_px("SELL", 100.0, 103.0), 101.0)

    def test_enter_maker_still_clamps_to_bid(self):
        self.assertEqual(maker_px("BUY", 102.0, 100.0, 103.0), 100.0)

    def test_stale_cover_bid_does_not_predict_fill(self):
        src = board_predicts_fill("BUY", 90.0, 100.0, 103.0, [])
        self.assertIsNone(src)

    def test_cover_at_ask_predicts_book_cross(self):
        src = board_predicts_fill("BUY", 103.0, 100.0, 103.0, [])
        self.assertEqual(src, "BOOK_CROSS")

    def test_cover_at_ask_minus_one_fills_on_bid_sell_print(self):
        execs = [{"side": "SELL", "price": 100.0, "size": 0.01}]
        src = board_predicts_fill("BUY", 102.0, 100.0, 103.0, execs)
        self.assertEqual(src, "BOARD_TRADE")

    def test_stale_cover_misses_higher_sell_print(self):
        execs = [{"side": "SELL", "price": 110.0, "size": 0.01}]
        src = board_predicts_fill("BUY", 90.0, 110.0, 112.0, execs)
        self.assertIsNone(src)

    def test_exit_at_ask_minus_one_stays_without_a_print(self):
        self.assertTrue(exit_quote_fillable("BUY", 102.0, 100.0, 103.0))

    def test_exit_left_behind_the_bid_is_unfillable(self):
        self.assertFalse(exit_quote_fillable("BUY", 90.0, 110.0, 112.0))

    def test_flatten_above_the_ask_is_unfillable(self):
        self.assertFalse(exit_quote_fillable("SELL", 120.0, 110.0, 112.0))


if __name__ == "__main__":
    unittest.main()
