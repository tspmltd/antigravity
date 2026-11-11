"""CSR-694 / 694b MS1 LIVE gate + single-order invariant tests."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from antigravity.quant_pipeline import run_ms1_wobi_live as ms1


def _bare_runner(*, real: bool = False) -> ms1.MS1WobiLive:
    r = object.__new__(ms1.MS1WobiLive)
    r.LANE = "MS1_WOBI_LIVE"
    r.real = real
    r.size = ms1.SIZE_BTC
    r.confirmed_net = 0.0
    r.venue_net = 0.0
    r.venue_avg = 0.0
    r.entry_px = 0.0
    r.entry_ts = 0.0
    r.working_side = None
    r.working_px = 0.0
    r.working_id = ""
    r.working_why = ""
    r.cancel_pending = False
    r.pending_exit_tag = ""
    r.pending_exit_reason = ""
    r.halt_new_entries = False
    r.oversize_halts = 0
    r.posts = 0
    r.cancels = 0
    r.fills = 0
    r.rejects = 0
    r.api_errors = 0
    r.blocked_posts = 0
    r.last_api_ts = 0.0
    r.client = mock.MagicMock()
    return r


class TestMS1LiveGates(unittest.TestCase):
    def test_refuse_size(self):
        self.assertIsNone(ms1.refuse_size(0.001))
        self.assertIn("SIZE", ms1.refuse_size(0.002) or "")

    def test_refuse_real_missing_confirm(self):
        with mock.patch.dict(os.environ, {"CONFIRM_GO": "", "ENABLE_REAL_TRADING": "true"}, clear=False):
            reason = ms1.refuse_real_gates("MS1")
            self.assertIsNotNone(reason)
            self.assertIn("CONFIRM_GO", reason or "")

    def test_refuse_real_prep_flag(self):
        with mock.patch.object(ms1, "GO_FLAG", Path("/tmp/ms1_go_prep.flag")):
            with mock.patch.object(Path, "exists", lambda self: True):
                with mock.patch.object(Path, "read_text", lambda self, **k: "status=PREP\n"):
                    with mock.patch.dict(
                        os.environ,
                        {"CONFIRM_GO": "MS1_LIVE", "ENABLE_REAL_TRADING": "true"},
                        clear=False,
                    ):
                        with mock.patch.object(ms1, "STOP_FLAG") as stop:
                            stop.exists.return_value = False
                            reason = ms1.refuse_real_gates("MS1")
        self.assertIsNotNone(reason)
        self.assertIn("ACTIVE", reason or "")

    def test_tokens_and_cap(self):
        self.assertEqual(ms1.CONFIRM_TOKEN, "MS1")
        self.assertEqual(ms1.CONFIRM_ENV, "MS1_LIVE")
        self.assertEqual(ms1.SIZE_BTC, 0.001)
        self.assertEqual(ms1.MAX_INV_BTC, 0.001)


class TestMS1LiveEv(unittest.TestCase):
    def test_live_long_ev(self):
        r = _bare_runner()
        self.assertTrue(r._live_long_ev(0.25, 0.0))
        self.assertFalse(r._live_long_ev(0.25, -0.01))
        self.assertFalse(r._live_long_ev(0.10, 0.05))


class TestMS1SingleOrderInvariant(unittest.TestCase):
    def test_inventory_ok_requires_flat(self):
        r = _bare_runner()
        self.assertTrue(r._inventory_ok_for_buy())
        r.confirmed_net = 0.001
        self.assertFalse(r._inventory_ok_for_buy())
        r.confirmed_net = 0.0
        r.venue_net = 0.004
        self.assertFalse(r._inventory_ok_for_buy())

    def test_inventory_blocks_oversize(self):
        r = _bare_runner()
        r.confirmed_net = 0.004
        r.venue_net = 0.004
        self.assertFalse(r._inventory_ok_for_buy())

    def test_same_side_reprice_cancels_does_not_stack(self):
        """Root bug: second BUY at new px must NOT send_order beside first."""
        r = _bare_runner(real=False)
        r._post("BUY", 100.0, "ENTER a", now=100.0)
        self.assertEqual(r.posts, 1)
        self.assertTrue(r.working_id)
        self.assertEqual(r.working_side, "BUY")
        first_id = r.working_id
        r.client.send_order.reset_mock()
        with mock.patch.object(r, "_cancel", wraps=r._cancel) as cxl:
            r._post("BUY", 101.0, "ENTER b", now=110.0)
            cxl.assert_called()
        # Must not have posted a second order this tick
        r.client.send_order.assert_not_called()
        self.assertEqual(r.posts, 1)
        self.assertNotEqual(r.working_id, first_id)
        self.assertFalse(r.working_id)  # cleared by cancel (DRY)

    def test_cancel_pending_blocks_post(self):
        r = _bare_runner(real=True)
        r.cancel_pending = True
        r.client.send_order.reset_mock()
        r._post("BUY", 100.0, "ENTER", now=1.0)
        r.client.send_order.assert_not_called()
        self.assertEqual(r.blocked_posts, 1)

    def test_sell_blocked_when_not_long(self):
        r = _bare_runner()
        r.confirmed_net = 0.0
        r._post("SELL", 100.0, "EXIT", now=1.0)
        self.assertEqual(r.posts, 0)
        self.assertGreater(r.blocked_posts, 0)


if __name__ == "__main__":
    unittest.main()
