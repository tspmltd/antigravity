"""WOBIMM5 LIVE fill semantics — predict vs commit. No venue POST."""
from __future__ import annotations

import json
import time
import unittest
from unittest.mock import MagicMock, patch

from antigravity.quant_pipeline.private_child_order_stream import (
    PrivateChildOrderStream,
)
from antigravity.quant_pipeline.run_wobimm5_live import (
    CLOSE_SETTLE_SEC,
    ENTRY_OFFSET_BP,
    FLATTEN_HARD,
    FLATTEN_HOLD,
    FLATTEN_SOFT,
    GHOST_FAILCLOSED_HITS,
    MIN_ENTRY_SPREAD_BP,
    MIN_HARVEST_BP,
    OPTIMISTIC_FILL_SOURCES,
    SOFT_ADVERSE_ESCALATE_BP,
    SOFT_REPRICE_MAX,
    TOXIC_ADOPT_BP,
    TP_GRACE_SEC,
    advance_flatten_state,
    book_cross_guard_ok,
    ceil_tick,
    classify_flatten_mode,
    dust_neutralize_legs,
    entry_passive_px,
    entry_spread_allows,
    exit_limit_px,
    floor_tick,
    is_isolated_dust,
    is_quote_crossed,
    maker_px,
    public_maker_fill,
    soft_maker_exit_px,
    spread_bp,
    tp_harvest_exit_px,
    WOBIMM5Live,
)


class TestHitTouchBookCrossGuard(unittest.TestCase):
    def test_equal_touch_is_not_crossed(self):
        # SELL@bid must NOT be BOOK_CROSS (old bug: >= made HIT_TOUCH instant-fill)
        self.assertFalse(is_quote_crossed("SELL", 100.0, best_bid=100.0, best_ask=101.0))
        self.assertFalse(is_quote_crossed("BUY", 101.0, best_bid=100.0, best_ask=101.0))

    def test_strict_through_is_crossed(self):
        self.assertTrue(is_quote_crossed("SELL", 100.0, best_bid=100.5, best_ask=101.0))
        self.assertTrue(is_quote_crossed("BUY", 101.0, best_bid=100.0, best_ask=100.5))

    def test_post_guard_window(self):
        now = 1_000_000.0
        self.assertFalse(book_cross_guard_ok(now - 0.05, now=now, guard_sec=0.20))
        self.assertTrue(book_cross_guard_ok(now - 0.25, now=now, guard_sec=0.20))


class TestPublicMakerFillOidFirst(unittest.TestCase):
    def test_oid_beats_book(self):
        execs = [
            {
                "price": 100.0,
                "size": 0.001,
                "side": "SELL",
                "buy_child_order_acceptance_id": "OID-1",
                "sell_child_order_acceptance_id": "",
            }
        ]
        hit = public_maker_fill(
            side="BUY",
            price=100.0,
            acceptance_id="OID-1",
            public_execs=execs,
            best_bid=99.0,
            best_ask=100.0,
            allow_book=True,
        )
        self.assertEqual(hit[0], "PUBLIC_OID")

    def test_allow_book_false_skips_cross(self):
        hit = public_maker_fill(
            side="SELL",
            price=100.0,
            acceptance_id="OID-X",
            public_execs=[],
            best_bid=101.0,
            best_ask=102.0,
            allow_book=False,
        )
        self.assertIsNone(hit)


class TestRealPredictDoesNotCommit(unittest.TestCase):
    def _bare_runner(self, real: bool = True) -> WOBIMM5Live:
        r = object.__new__(WOBIMM5Live)
        r.LANE = "WOBIMM5_LIVE"
        r.real = real
        r.size = 0.001
        r.net = 1
        r.entry_px = 100.0
        r.entry_src = "PUBLIC_OID"
        r.entry_ts = time.time()
        r.working_side = "SELL"
        r.working_px = 100.0
        r.working_id = "OID-SELL"
        r.working_posted_at = time.time() - 1.0  # past guard
        r.pending_exit_why = "ev_gone"
        r.ghosts = []
        r.recent_posts = []
        r.adopted_oids = set()
        r.halt_trading = False
        r.predict_fills = 0
        r.cancels = 0
        r.fills = 0
        r.api_errors = 0
        r.client = MagicMock()
        r.pnl_bp = 0.0
        r.pnl_jpy = 0.0
        r.trades = 0
        r.wins = 0
        r.history = []
        r.max_inv = 0.001
        r.ae = MagicMock()
        r.ae.close.return_value = {}
        return r

    def test_book_cross_parks_optimistic_no_inventory_change(self):
        r = self._bare_runner(real=True)
        # bid through sell price → BOOK_CROSS under strict rule
        r._maybe_fill([], bid=100.5, ask=101.0)
        self.assertEqual(r.net, 1)  # inventory NOT closed
        self.assertEqual(r.fills, 0)
        self.assertEqual(r.predict_fills, 1)
        self.assertEqual(len(r.ghosts), 1)
        self.assertIsNone(r.working_side)
        r.client.cancel_child_order.assert_called()
        self.assertIn(r.ghosts[0]["src"], OPTIMISTIC_FILL_SOURCES)

    def test_public_oid_commits(self):
        r = self._bare_runner(real=True)
        execs = [
            {
                "price": 100.0,
                "size": 0.001,
                "side": "BUY",
                "buy_child_order_acceptance_id": "",
                "sell_child_order_acceptance_id": "OID-SELL",
            }
        ]
        r._maybe_fill(execs, bid=99.0, ask=100.0)
        self.assertEqual(r.net, 0)  # closed
        self.assertEqual(r.fills, 1)
        self.assertEqual(r.predict_fills, 0)
        self.assertEqual(len(r.ghosts), 0)

    def test_post_guard_blocks_immediate_book_cross(self):
        r = self._bare_runner(real=True)
        r.working_posted_at = time.time()  # just posted
        r._maybe_fill([], bid=100.5, ask=101.0)
        self.assertEqual(r.net, 1)
        self.assertEqual(r.predict_fills, 0)
        self.assertEqual(r.working_side, "SELL")

    def test_dry_still_commits_board_imitation(self):
        r = self._bare_runner(real=False)
        r.working_id = "SIM-SELL"
        r._maybe_fill([], bid=100.5, ask=101.0)
        self.assertEqual(r.net, 0)
        self.assertEqual(r.fills, 1)


class TestReconcileDesync(unittest.TestCase):
    def _base(self) -> WOBIMM5Live:
        r = object.__new__(WOBIMM5Live)
        r.LANE = "WOBIMM5_LIVE"
        r.real = True
        r.size = 0.001
        r.net = 0
        r.entry_px = 0.0
        r.entry_src = ""
        r.entry_ts = 0.0
        r.halt_trading = False
        r.halt_reason = ""
        r.desync_halts = 0
        r.api_errors = 0
        r.cancels = 0
        r.fills = 0
        r.predict_fills = 0
        r.adopt_fills = 0
        r.toxic_inflight_cleanups = 0
        r.adverse_market_exits = 0
        r.ev_gone_market_exits = 0
        r.adverse_cuts = 0
        r._adverse_cooldown_until = 0.0
        r.running = True
        r.last_reconcile_ts = 0.0
        r.working_side = None
        r.working_px = 0.0
        r.working_id = ""
        r.working_posted_at = 0.0
        r.pending_exit_why = ""
        r.ghosts = []
        r.recent_posts = []
        r.adopted_oids = set()
        r.history = []
        r.pnl_bp = 0.0
        r.pnl_jpy = 0.0
        r.trades = 0
        r.wins = 0
        r.posts = 0
        r.rejects = 0
        r.max_inv = 0.001
        r.ae = MagicMock()
        r.ae.close.return_value = {}
        r.client = MagicMock()
        r._close_settle_until = 0.0
        r._close_settle_flat_side = ""
        r._last_close_oid = ""
        r._ghost_mismatch_hits = 0
        r._ghost_mismatch_since = 0.0
        r.force_syncs = 0
        r.flatten_state = FLATTEN_HOLD
        return r

    def test_inventory_mismatch_flat_blocks_ghost_open_instead_of_halt(self):
        """P0: first unattributed flat≠venue → GHOST observe; no Force-Sync open."""
        r = self._base()
        r.force_syncs = 0
        r.client.get_child_orders.return_value = []
        r.client.get_positions.return_value = [{"side": "SELL", "size": 0.001}]
        with patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_TRIES", 1
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_SLEEP_SEC", 0.0
        ), patch.object(r, "_feeder_mid", return_value=12_000_000.0):
            r._reconcile_venue(time.time())
        self.assertFalse(r.halt_trading)  # first hit — not yet Fail-Closed
        self.assertTrue(r.running)
        self.assertEqual(r.net, 0)  # never open phantom short via Force-Sync
        self.assertEqual(r.force_syncs, 0)
        self.assertEqual(r.adopt_fills, 0)
        self.assertIsNone(r.working_side)
        self.assertEqual(r._ghost_mismatch_hits, 1)

    def test_cancel_fill_race_adopted_as_legit_position(self):
        """CANCEL then COMPLETED on recent_posts OID → Late-Fill ADOPT, not GHOST."""
        r = self._base()
        oid = "JRF-RACE-CANCEL-FILL"
        # Working cleared after cancel; OID remains in recent_posts (soak3 pattern).
        r.working_side = None
        r.working_id = ""
        r.recent_posts = [
            {"id": oid, "side": "SELL", "px": 11_765_948.0, "ts": time.time()}
        ]
        r.client.get_positions.return_value = [{"side": "SELL", "size": 0.001}]

        def child_orders(
            product, child_order_state="ACTIVE", child_order_acceptance_id=None
        ):
            if (
                child_order_acceptance_id == oid
                and child_order_state == "COMPLETED"
            ):
                return [
                    {
                        "child_order_acceptance_id": oid,
                        "child_order_state": "COMPLETED",
                        "side": "SELL",
                        "average_price": 11_765_948.0,
                        "executed_size": 0.001,
                    }
                ]
            return []

        r.client.get_child_orders.side_effect = child_orders
        with patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_TRIES", 1
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_SLEEP_SEC", 0.0
        ), patch.object(r, "_feeder_mid", return_value=11_766_000.0), patch.object(
            r, "_arm_tp_maker_rest"
        ) as arm_tp, patch.object(
            r, "_maybe_toxic_adopt_cleanup", return_value=False
        ):
            r._reconcile_venue(time.time())
        self.assertEqual(r.net, -1)  # legitimate short adopted
        self.assertEqual(r.adopt_fills, 1)
        self.assertFalse(r.halt_trading)
        self.assertEqual(r._ghost_mismatch_hits, 0)
        self.assertEqual(r.force_syncs, 0)
        arm_tp.assert_called()

    def test_persistent_ghost_triggers_halt_and_flatten(self):
        """Unattributed venue residue for N reconciles → HALT + flatten (Fail-Closed)."""
        r = self._base()
        r.recent_posts = []
        r.client.get_child_orders.return_value = []
        r.client.get_positions.return_value = [{"side": "SELL", "size": 0.001}]
        r.flatten = MagicMock(
            return_value={"flat_ok": True, "venue_net_after": 0.0}
        )
        t0 = time.time()
        with patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_TRIES", 1
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_SLEEP_SEC", 0.0
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.RECONCILE_INTERVAL_SEC", 0.0
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.GHOST_FAILCLOSED_HITS", 3
        ), patch.object(r, "_feeder_mid", return_value=12_000_000.0):
            for i in range(int(GHOST_FAILCLOSED_HITS)):
                r.last_reconcile_ts = 0.0
                r._reconcile_venue(t0 + i * 0.05)
                if r.halt_trading:
                    break
        self.assertTrue(r.halt_trading)
        self.assertIn("GHOST_FAILCLOSED", r.halt_reason)
        self.assertFalse(r.running)
        r.flatten.assert_called()
        self.assertEqual(r.net, 0)
        # New ENTER must be blocked while halted.
        r._post("BUY", 12_000_000.0, "ENTER BUY_PRESSURE")
        self.assertEqual(r.posts, 0)

    def test_hard_ceiling_still_halts(self):
        r = self._base()
        r.client.get_child_orders.return_value = []
        r.client.get_positions.return_value = [{"side": "BUY", "size": 0.005}]
        with patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_TRIES", 1
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_SLEEP_SEC", 0.0
        ):
            r._reconcile_venue(time.time())
        self.assertTrue(r.halt_trading)
        self.assertIn("HARD_INV_CEILING", r.halt_reason)
        self.assertFalse(r.running)

    def test_adopt_completed_working_resolves_desync(self):
        """Private COMPLETED fill must sync local before DESYNC_HALT (feeder lag case)."""
        r = self._base()
        r.working_side = "SELL"
        r.working_px = 12023880.0
        r.working_id = "JRF-ADOPT-1"

        def child_orders(product, child_order_state="ACTIVE", child_order_acceptance_id=None):
            if child_order_acceptance_id == "JRF-ADOPT-1" and child_order_state == "COMPLETED":
                return [
                    {
                        "child_order_acceptance_id": "JRF-ADOPT-1",
                        "child_order_state": "COMPLETED",
                        "side": "SELL",
                        "price": 12023880.0,
                        "average_price": 12023880.0,
                        "size": 0.001,
                        "executed_size": 0.001,
                    }
                ]
            return []

        r.client.get_child_orders.side_effect = child_orders
        r.client.get_positions.return_value = [{"side": "SELL", "size": 0.001}]
        with patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_TRIES", 2
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_SLEEP_SEC", 0.0
        ):
            r._reconcile_venue(time.time())
        self.assertFalse(r.halt_trading)
        self.assertTrue(r.running)
        self.assertEqual(r.net, -1)  # short adopted
        self.assertEqual(r.adopt_fills, 1)
        self.assertEqual(r.fills, 1)
        # FILL → TP Maker rest (cover BUY) while HOLD.
        self.assertEqual(r.working_side, "BUY")
        self.assertEqual(r.pending_exit_why, "ev_hold")

    def test_exit_reprice_adopts_filled_first_leg_no_second_post(self):
        """REAL bug: exit_reprice canceled a filled SELL then posted another → venue short."""
        r = self._base()
        r.net = 1
        r.entry_px = 12_000_000.0
        r.working_side = "SELL"
        r.working_px = 11_974_504.0
        r.working_id = "JRF-EXIT-1"
        r.recent_posts = [
            {"id": "JRF-EXIT-1", "side": "SELL", "px": 11_974_504.0, "ts": time.time()}
        ]

        def child_orders(product, child_order_state="ACTIVE", child_order_acceptance_id=None):
            if child_order_acceptance_id == "JRF-EXIT-1" and child_order_state == "COMPLETED":
                return [
                    {
                        "child_order_acceptance_id": "JRF-EXIT-1",
                        "child_order_state": "COMPLETED",
                        "side": "SELL",
                        "average_price": 11_974_504.0,
                        "executed_size": 0.001,
                    }
                ]
            return []

        r.client.get_child_orders.side_effect = child_orders
        adopted = r._pull("exit_reprice")
        self.assertTrue(adopted)
        self.assertEqual(r.net, 0)
        self.assertEqual(r.adopt_fills, 1)
        # Guard: must not POST another EXIT SELL while flat.
        before_posts = r.posts
        r._post("SELL", 11_974_503.0, "EXIT ev_gone NO_EDGE")
        self.assertIsNone(r.working_side)
        self.assertEqual(r.posts, before_posts)
        r.client.send_order.assert_not_called()

    def test_recent_posts_never_readopt_after_terminate(self):
        """Regression 110154: terminated EXIT OID must not re-ADOPT via recent_posts."""
        r = self._base()
        r.net = 1
        r.entry_px = 11_692_704.0
        r.force_syncs = 0
        oid = "JRF20261109-110154-065897"
        r.recent_posts = [
            {"id": oid, "side": "SELL", "px": 11_695_461.0, "ts": time.time()}
        ]
        r._terminate_oid(oid, "adverse_mkt:test")

        def child_orders(product, child_order_state="ACTIVE", child_order_acceptance_id=None):
            if child_order_acceptance_id == oid and child_order_state == "COMPLETED":
                return [
                    {
                        "child_order_acceptance_id": oid,
                        "child_order_state": "COMPLETED",
                        "side": "SELL",
                        "average_price": 11_695_461.0,
                        "executed_size": 0.001,
                    }
                ]
            return []

        r.client.get_child_orders.side_effect = child_orders
        # New long on venue; stale SELL COMPLETED must NOT close local via adopt.
        r.client.get_positions.return_value = [{"side": "BUY", "size": 0.001}]
        r.last_reconcile_ts = 0.0
        with patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_TRIES", 2
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_SLEEP_SEC", 0.0
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.RECONCILE_INTERVAL_SEC", 0.0
        ):
            # Inflight adopt must refuse claimed OID.
            self.assertFalse(
                r._try_adopt_oid(side="SELL", px_hint=11_695_461.0, oid=oid)
            )
            r._reconcile_venue(time.time())
        self.assertFalse(r.halt_trading)
        self.assertEqual(r.net, 1)  # stays long with venue; no phantom SELL
        self.assertEqual(r.adopt_fills, 0)

    def test_pulled_exit_desync_force_syncs_to_venue_flat(self):
        """Without recent_posts adopt: venue flat + local long → force sync to 0."""
        r = self._base()
        r.net = 1
        r.entry_px = 12_000_000.0
        r.force_syncs = 0
        r.recent_posts = [
            {"id": "JRF-PULLED", "side": "SELL", "px": 12_000_000.0, "ts": time.time()}
        ]
        r.client.get_child_orders.return_value = []
        r.client.get_positions.return_value = []
        r.last_reconcile_ts = 0.0
        with patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_TRIES", 1
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_SLEEP_SEC", 0.0
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.RECONCILE_INTERVAL_SEC", 0.0
        ), patch.object(r, "_feeder_mid", return_value=12_000_000.0):
            r._reconcile_venue(time.time())
        self.assertFalse(r.halt_trading)
        self.assertEqual(r.net, 0)
        self.assertEqual(r.force_syncs, 1)
        self.assertIn("JRF-PULLED", r.claimed_oids)

    def _wire_market_flatten(self, r, nets):
        def get_positions(product="FX_BTC_JPY"):
            v = nets["v"]
            if abs(v) < 1e-12:
                return []
            return [{"side": "BUY" if v > 0 else "SELL", "size": abs(v)}]

        def send_order(product, side, size, order_type="MARKET", price=None):
            self.assertEqual(order_type, "MARKET")
            if side == "BUY":
                nets["v"] += float(size)
            else:
                nets["v"] -= float(size)
            if abs(nets["v"]) < 1e-10:
                nets["v"] = 0.0
            return {"status": "SUBMITTED", "child_order_acceptance_id": f"SIM-{side}"}

        r.client.get_positions.side_effect = get_positions
        r.client.send_order.side_effect = send_order
        r.client.cancel_all_child_orders.return_value = {"status": "ok"}
        r.client.get_execution_price_by_acceptance_id.return_value = 10_000_000.0

    def test_close_settle_debounces_phantom_short_force_sync(self):
        """After EXIT SELL closes long, venue=-0.001 during settle must NOT open short."""
        r = self._base()
        r.net = 1
        r.entry_px = 11_846_310.0
        r.entry_src = "PRIVATE_WS"
        r.entry_ts = time.time()
        r.working_id = "JRF-EXIT-CLOSE"
        r.pending_exit_why = "adverse_fast (mae)"
        r._apply_fill("SELL", 11_842_916.0, "PRIVATE_WS", arm_tp=False)
        self.assertEqual(r.net, 0)
        self.assertTrue(r._in_close_settle())
        self.assertEqual(r._close_settle_flat_side, "SELL")
        self.assertGreaterEqual(CLOSE_SETTLE_SEC, 15.0)
        # Soak bug: reconcile immediately sees venue short.
        r.client.get_child_orders.return_value = []
        r.client.get_positions.return_value = [{"side": "SELL", "size": 0.001}]
        with patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_TRIES", 1
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_SLEEP_SEC", 0.0
        ), patch.object(r, "_feeder_mid", return_value=11_842_000.0):
            r.last_reconcile_ts = 0.0
            r._reconcile_venue(time.time())
        self.assertEqual(r.net, 0)  # no phantom short
        self.assertEqual(r.force_syncs, 0)
        self.assertIsNone(r.working_side)

    def test_late_close_echo_sell_ignored_while_flat(self):
        """Second EXIT SELL fill while flat in settle must not open short."""
        r = self._base()
        r.net = 1
        r.entry_px = 12_000_000.0
        r.entry_ts = time.time()
        r.working_id = "JRF-EXIT-1"
        r.pending_exit_why = "ev_gone"
        r._apply_fill("SELL", 11_999_000.0, "PRIVATE_WS", arm_tp=False)
        self.assertEqual(r.net, 0)
        before_fills = r.fills
        # Echo of another EXIT SELL OID during settle.
        r.working_id = "JRF-EXIT-2"
        r._apply_fill("SELL", 11_998_000.0, "PRIVATE_WS", arm_tp=False)
        self.assertEqual(r.net, 0)
        self.assertEqual(r.fills, before_fills)  # echo ignored

    def test_flat_force_sync_blocked_even_after_settle_expires(self):
        """After settle ends, flat still must not adopt venue residue into the SM."""
        r = self._base()
        r.net = 0
        r._mark_close_settle("SELL", close_oid="JRF-OLD")
        r._close_settle_until = time.time() - 0.01  # expired
        r.client.get_child_orders.return_value = []
        r.client.get_positions.return_value = [{"side": "SELL", "size": 0.001}]
        with patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_TRIES", 1
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_SLEEP_SEC", 0.0
        ), patch.object(r, "_feeder_mid", return_value=12_000_000.0):
            r.last_reconcile_ts = 0.0
            r._reconcile_venue(time.time())
        self.assertEqual(r.net, 0)
        self.assertEqual(r.force_syncs, 0)
        self.assertIsNone(r.working_side)

    def test_close_plus_10s_venue_diff_does_not_open_position(self):
        """Soak2 gap: ~10s after CLOSE, venue=-0.001 must keep local flat."""
        r = self._base()
        r.net = 1
        r.entry_px = 11_769_353.0
        r.entry_src = "PRIVATE_WS"
        r.entry_ts = time.time() - 20.0
        r.working_id = "JRF-EXIT-EV-GONE"
        r.pending_exit_why = "ev_gone NO_EDGE"
        r._apply_fill("SELL", 11_768_983.0, "PRIVATE_WS", arm_tp=False)
        self.assertEqual(r.net, 0)
        # Simulate reconcile 10s after close (inside 15s settle, and flat-open forbid).
        now = time.time()
        r._close_settle_until = now + (CLOSE_SETTLE_SEC - 10.0)
        self.assertTrue(r._in_close_settle(now))
        r.client.get_child_orders.return_value = []
        r.client.get_positions.return_value = [{"side": "SELL", "size": 0.001}]
        with patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_TRIES", 1
        ), patch(
            "antigravity.quant_pipeline.run_wobimm5_live.ADOPT_GRACE_SLEEP_SEC", 0.0
        ), patch.object(r, "_feeder_mid", return_value=11_770_000.0):
            r.last_reconcile_ts = 0.0
            r._reconcile_venue(now)
        self.assertEqual(r.net, 0)
        self.assertEqual(r.force_syncs, 0)
        self.assertEqual(r.flatten_state, FLATTEN_HOLD)
        self.assertIsNone(r.working_side)
        # Must not arm EXIT / enter IN_POS via ghost sync.
        self.assertFalse(str(r.pending_exit_why or "").startswith("EXIT"))

    def test_policy_soft_exits_never_auto_market(self):
        """FLATTEN_SOFT: ev_gone / adverse_fast must not auto-MARKET."""
        r = self._base()
        self.assertFalse(r._is_market_flat_exit("ev_gone NO_EDGE"))
        self.assertFalse(r._is_market_flat_exit("adverse_fast (x)"))

    def test_policy_hard_exits_are_market(self):
        """FLATTEN_HARD: adverse_cut / toxic_adopt|fill / hard_stop → MARKET."""
        r = self._base()
        self.assertTrue(r._is_market_flat_exit("adverse_cut (-2.0bp)"))
        self.assertTrue(r._is_market_flat_exit("toxic_adopt (-1.7bp)"))
        self.assertTrue(r._is_market_flat_exit("toxic_fill (-1.6bp)"))
        self.assertTrue(r._is_market_flat_exit("hard_stop"))

    def test_toxic_maker_exit_posts_limit_not_market(self):
        """adverse_fast is SOFT: maker EXIT LIMIT (not MARKET) if posted."""
        r = self._base()
        r.size = 0.001
        r.net = 1
        r.entry_px = 10_000_000.0
        r.posts = 0
        r._adverse_cooldown_until = 0.0
        r.pending_exit_why = ""
        r.flatten_state = FLATTEN_HOLD
        r.ghosts = []
        r.client.send_order.return_value = {
            "status": "SUBMITTED",
            "child_order_acceptance_id": "JRF-EXIT-LIM",
        }
        now = time.time()
        with patch.object(r, "_feeder_bid_ask", return_value=(9_999_000.0, 10_001_000.0)):
            r._toxic_maker_exit("adverse_fast (mae)", now)
        self.assertEqual(r.pending_exit_why, "adverse_fast (mae)")
        self.assertEqual(r.working_side, "SELL")
        # SOFT passive: SELL rests at ask, never at bid (cross).
        self.assertEqual(r.working_px, 10_001_000.0)
        # adverse_fast is SOFT — no HARD re-entry cooldown from this path.
        self.assertEqual(r._adverse_cooldown_until, 0.0)
        if r.client.send_order.called:
            args = r.client.send_order.call_args
            # positional or kwargs
            flat = args.args if args.args else ()
            kw = args.kwargs or {}
            otype = kw.get("order_type") or (flat[3] if len(flat) > 3 else None)
            self.assertNotEqual(otype, "MARKET")

    def test_private_ws_fill_claims_working_oid_before_public(self):
        """P0-3: private EXECUTION applies once; later adopt/public must CLAIM_DROP."""
        r = self._base()
        r.net = 0
        r.entry_px = 0.0
        r.private_ws_fills = 0
        r.working_side = "BUY"
        r.working_px = 11_700_000.0
        r.working_id = "JRF-PRIV-1"
        r.private_ws = PrivateChildOrderStream(product_code="FX_BTC_JPY")
        r.private_ws.push_for_test(
            {
                "src": "PRIVATE_WS",
                "event_type": "EXECUTION",
                "id": "JRF-PRIV-1",
                "side": "BUY",
                "px": 11_700_000.0,
                "size": 0.001,
            }
        )
        n = r._drain_private_ws_fills()
        self.assertEqual(n, 1)
        self.assertEqual(r.net, 1)
        self.assertEqual(r.private_ws_fills, 1)
        self.assertIn("JRF-PRIV-1", r.claimed_oids)
        # Second delivery must not double-count.
        r.private_ws.push_for_test(
            {
                "src": "PRIVATE_WS",
                "event_type": "EXECUTION",
                "id": "JRF-PRIV-1",
                "side": "BUY",
                "px": 11_700_000.0,
                "size": 0.001,
            }
        )
        self.assertEqual(r._drain_private_ws_fills(), 0)
        self.assertEqual(r.net, 1)

    def test_private_ws_message_parse_execution_only(self):
        s = PrivateChildOrderStream(api_key="k", api_secret="s")
        s._handle_message(
            json.dumps(
                {
                    "params": {
                        "channel": "child_order_events",
                        "message": {
                            "product_code": "FX_BTC_JPY",
                            "event_type": "EXECUTION",
                            "child_order_acceptance_id": "JRF-X",
                            "side": "SELL",
                            "price": 100.0,
                            "size": 0.001,
                        },
                    }
                }
            )
        )
        s._handle_message(
            json.dumps(
                {
                    "params": {
                        "channel": "child_order_events",
                        "message": {
                            "product_code": "FX_BTC_JPY",
                            "event_type": "CANCEL",
                            "child_order_acceptance_id": "JRF-Y",
                            "side": "SELL",
                            "price": 100.0,
                            "size": 0.001,
                        },
                    }
                }
            )
        )
        rows = s.drain()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "JRF-X")
        self.assertEqual(rows[0]["side"], "SELL")

    def test_dust_neutralize_legs_short_and_long(self):
        self.assertTrue(is_isolated_dust(-0.00027482))
        self.assertTrue(is_isolated_dust(0.00072518))
        self.assertFalse(is_isolated_dust(0.0))
        self.assertFalse(is_isolated_dust(0.001))
        short = dust_neutralize_legs(-0.00027482)
        self.assertEqual(short, [("BUY", 0.00127482), ("SELL", 0.001)])
        long = dust_neutralize_legs(0.00072518)
        self.assertEqual(long, [("SELL", 0.00172518), ("BUY", 0.001)])
        self.assertIsNone(dust_neutralize_legs(0.0))
        self.assertIsNone(dust_neutralize_legs(-0.001))

    def test_flatten_clears_isolated_dust_via_market_legs(self):
        """flatten must not leave sub-min dust (prior bug: LIMIT size<0.001 rejected)."""
        r = self._base()
        r.size = 0.001
        r._last_dust_sweep_ts = 0.0
        nets = {"v": -0.00027482}

        def get_positions(product="FX_BTC_JPY"):
            v = nets["v"]
            if abs(v) < 1e-12:
                return []
            side = "BUY" if v > 0 else "SELL"
            return [{"side": side, "size": abs(v)}]

        def send_order(product, side, size, order_type="MARKET", price=None):
            sz = float(size)
            if side == "BUY":
                nets["v"] += sz
            else:
                nets["v"] -= sz
            # float noise
            if abs(nets["v"]) < 1e-10:
                nets["v"] = 0.0
            return {
                "status": "SUBMITTED",
                "child_order_acceptance_id": f"SIM-{side}-{sz}",
            }

        r.client.get_positions.side_effect = get_positions
        r.client.send_order.side_effect = send_order
        r.client.cancel_all_child_orders.return_value = {"status": "ok"}
        r.client.get_execution_price_by_acceptance_id.return_value = 100.0
        with patch(
            "antigravity.quant_pipeline.run_wobimm5_live.FLATTEN_CONFIRM_SLEEP_SEC", 0.0
        ):
            detail = r.flatten("test_dust")
        self.assertTrue(detail.get("flat_ok"))
        self.assertEqual(nets["v"], 0.0)
        # cover then unwind
        self.assertGreaterEqual(r.client.send_order.call_count, 2)

    def test_ghost_public_oid_blocks_later_private_adopt(self):
        """Regression: GHOST_CONFIRM PUBLIC_OID must claim OID so reconcile cannot
        PRIVATE_RECONCILE_ADOPT the same fill again (INVENTORY_MISMATCH root cause).
        """
        r = self._base()
        r.net = 1
        r.entry_px = 11_693_000.0
        r.pending_exit_why = "ev_gone NO_EDGE"
        oid = "JRF20261109-075036-000662"
        r.ghosts = [
            {
                "id": oid,
                "side": "SELL",
                "px": 11_693_385.0,
                "ts": time.time(),
                "pending_exit_why": "ev_gone NO_EDGE",
            }
        ]
        r.recent_posts = [
            {"id": oid, "side": "SELL", "px": 11_693_385.0, "ts": time.time()}
        ]
        execs = [
            {
                "price": 11_693_385.0,
                "size": 0.001,
                "side": "BUY",
                "buy_child_order_acceptance_id": "",
                "sell_child_order_acceptance_id": oid,
            }
        ]
        r._confirm_ghosts(execs)
        self.assertEqual(r.net, 0)
        self.assertEqual(r.fills, 1)
        self.assertIn(oid, r.adopted_oids)
        self.assertEqual(r.ghosts, [])

        def child_orders(product, child_order_state="ACTIVE", child_order_acceptance_id=None):
            if child_order_acceptance_id == oid and child_order_state == "COMPLETED":
                return [
                    {
                        "child_order_acceptance_id": oid,
                        "child_order_state": "COMPLETED",
                        "side": "SELL",
                        "average_price": 11_693_395.0,
                        "executed_size": 0.001,
                    }
                ]
            return []

        r.client.get_child_orders.side_effect = child_orders
        adopted = r._try_adopt_oid(side="SELL", px_hint=11_693_385.0, oid=oid)
        self.assertFalse(adopted)
        self.assertEqual(r.net, 0)  # must NOT open phantom short
        self.assertEqual(r.adopt_fills, 0)
        self.assertEqual(r.fills, 1)

    def test_toxic_adopt_same_stack_hard_market(self):
        """P0: ADOPT with mid already ≤−1.5bp → TOXIC_INFLIGHT → HARD MARKET."""
        self.assertGreater(TOXIC_ADOPT_BP, 0.0)
        r = self._base()
        r.working_side = "BUY"
        r.working_px = 10_000_000.0
        r.working_id = "JRF-TOXIC-1"
        r.flatten_state = FLATTEN_HOLD
        fill_px = 10_000_000.0
        mid = 9_980_000.0

        def child_orders(product, child_order_state="ACTIVE", child_order_acceptance_id=None):
            if child_order_acceptance_id == "JRF-TOXIC-1" and child_order_state == "COMPLETED":
                return [
                    {
                        "child_order_acceptance_id": "JRF-TOXIC-1",
                        "child_order_state": "COMPLETED",
                        "side": "BUY",
                        "average_price": fill_px,
                        "executed_size": 0.001,
                    }
                ]
            return []

        r.client.get_child_orders.side_effect = child_orders
        with patch.object(r, "_feeder_mid", return_value=mid), patch.object(
            r, "_toxic_maker_exit"
        ) as mk:
            ok = r._try_adopt_oid(side="BUY", px_hint=fill_px, oid="JRF-TOXIC-1")
        self.assertTrue(ok)
        self.assertEqual(r.adopt_fills, 1)
        self.assertEqual(r.net, 1)
        self.assertEqual(r.entry_px, fill_px)
        self.assertEqual(r.toxic_inflight_cleanups, 1)
        mk.assert_called_once()
        why = str(mk.call_args[0][0])
        self.assertTrue(why.startswith("toxic_adopt"))
        self.assertIn("-20.00bp", why)
        self.assertEqual(classify_flatten_mode(why), FLATTEN_HARD)

    def test_healthy_adopt_skips_toxic_cleanup(self):
        r = self._base()
        r.working_side = "BUY"
        r.working_px = 10_000_000.0
        r.working_id = "JRF-OK-1"
        fill_px = 10_000_000.0
        mid = 10_000_500.0  # slight favorable

        def child_orders(product, child_order_state="ACTIVE", child_order_acceptance_id=None):
            if child_order_acceptance_id == "JRF-OK-1" and child_order_state == "COMPLETED":
                return [
                    {
                        "child_order_acceptance_id": "JRF-OK-1",
                        "child_order_state": "COMPLETED",
                        "side": "BUY",
                        "average_price": fill_px,
                        "executed_size": 0.001,
                    }
                ]
            return []

        r.client.get_child_orders.side_effect = child_orders
        with patch.object(r, "_feeder_mid", return_value=mid), patch.object(
            r, "_toxic_maker_exit"
        ) as mk:
            ok = r._try_adopt_oid(side="BUY", px_hint=fill_px, oid="JRF-OK-1")
        self.assertTrue(ok)
        self.assertEqual(r.net, 1)
        self.assertEqual(r.toxic_inflight_cleanups, 0)
        mk.assert_not_called()

    def test_exit_adopt_flat_skips_toxic_even_if_mid_moved(self):
        """Closing EXIT adopt must not fire toxic exit after net→0."""
        r = self._base()
        r.net = 1
        r.entry_px = 10_000_000.0
        r.working_side = "SELL"
        r.working_px = 9_980_000.0
        r.working_id = "JRF-EXIT-TOX"
        r.pending_exit_why = "ev_gone"

        def child_orders(product, child_order_state="ACTIVE", child_order_acceptance_id=None):
            if child_order_acceptance_id == "JRF-EXIT-TOX" and child_order_state == "COMPLETED":
                return [
                    {
                        "child_order_acceptance_id": "JRF-EXIT-TOX",
                        "child_order_state": "COMPLETED",
                        "side": "SELL",
                        "average_price": 9_980_000.0,
                        "executed_size": 0.001,
                    }
                ]
            return []

        r.client.get_child_orders.side_effect = child_orders
        with patch.object(r, "_feeder_mid", return_value=9_500_000.0), patch.object(
            r, "_toxic_maker_exit"
        ) as mk:
            ok = r._try_adopt_oid(side="SELL", px_hint=9_980_000.0, oid="JRF-EXIT-TOX")
        self.assertTrue(ok)
        self.assertEqual(r.net, 0)
        self.assertEqual(r.toxic_inflight_cleanups, 0)
        mk.assert_not_called()


class TestPrivateWsAuthThenSubscribe(unittest.TestCase):
    """P0b: subscribe must wait for auth ACK (fixes -32007)."""

    def test_auth_ack_triggers_subscribe_not_on_open(self):
        stream = PrivateChildOrderStream(
            api_key="k", api_secret="s", product_code="FX_BTC_JPY"
        )
        ws = MagicMock()
        stream._on_open(ws)
        # on_open sends auth only
        self.assertEqual(ws.send.call_count, 1)
        auth_payload = json.loads(ws.send.call_args_list[0][0][0])
        self.assertEqual(auth_payload.get("method"), "auth")
        self.assertEqual(auth_payload.get("id"), 1)
        self.assertFalse(stream.subscribed)

        stream._handle_message(json.dumps({"id": 1, "result": True}))
        self.assertTrue(stream.authed)
        self.assertEqual(ws.send.call_count, 2)
        sub_payload = json.loads(ws.send.call_args_list[1][0][0])
        self.assertEqual(sub_payload.get("method"), "subscribe")
        self.assertEqual(sub_payload["params"]["channel"], "child_order_events")

        stream._handle_message(json.dumps({"id": 2, "result": True}))
        self.assertTrue(stream.subscribed)
        self.assertEqual(stream.subscribe_ok_n, 1)

    def test_execution_enqueued_after_filter(self):
        stream = PrivateChildOrderStream(
            api_key="k", api_secret="s", product_code="FX_BTC_JPY"
        )
        stream._handle_message(
            json.dumps(
                {
                    "params": {
                        "channel": "child_order_events",
                        "message": [
                            {
                                "product_code": "FX_BTC_JPY",
                                "event_type": "ORDER",
                                "child_order_acceptance_id": "JRF-X",
                                "side": "BUY",
                                "price": 100.0,
                                "size": 0.001,
                            },
                            {
                                "product_code": "FX_BTC_JPY",
                                "event_type": "EXECUTION",
                                "child_order_acceptance_id": "JRF-Y",
                                "side": "SELL",
                                "price": 12_000_000.0,
                                "size": 0.001,
                            },
                        ],
                    }
                }
            )
        )
        evs = stream.drain()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["id"], "JRF-Y")
        self.assertEqual(evs[0]["side"], "SELL")
        self.assertEqual(stream.dropped_non_exec, 1)


class TestFlattenExitStateMachine(unittest.TestCase):
    """HOLD → FLATTEN_SOFT (passive LIMIT) → FLATTEN_HARD (MARKET) locks."""

    BID = 10_000_000.0
    ASK = 10_001_500.0

    def _runner(self) -> WOBIMM5Live:
        r = object.__new__(WOBIMM5Live)
        r.LANE = "WOBIMM5_LIVE"
        r.real = True
        r.size = 0.001
        r.max_inv = 0.001
        r.net = 1
        r.entry_px = self.BID
        r.entry_src = "PUBLIC_OID"
        r.entry_ts = time.time()
        r.working_side = None
        r.working_px = 0.0
        r.working_id = ""
        r.pending_exit_why = ""
        r.flatten_state = FLATTEN_HOLD
        r.ghosts = []
        r.recent_posts = []
        r.adopted_oids = set()
        r.posts = 0
        r.cancels = 0
        r.api_errors = 0
        r.halt_trading = False
        r.client = MagicMock()
        return r

    def test_classify_soft_vs_hard(self):
        self.assertEqual(classify_flatten_mode("ev_gone NO_EDGE"), FLATTEN_SOFT)
        self.assertEqual(classify_flatten_mode("adverse_fast (mae)"), FLATTEN_SOFT)
        self.assertEqual(classify_flatten_mode("adverse_cut (-1.9bp)"), FLATTEN_HARD)
        self.assertEqual(classify_flatten_mode("toxic_adopt (-1.6bp)"), FLATTEN_HARD)
        self.assertEqual(classify_flatten_mode("toxic_fill (-1.7bp)"), FLATTEN_HARD)
        self.assertEqual(classify_flatten_mode("hard_stop"), FLATTEN_HARD)
        self.assertEqual(classify_flatten_mode("ev_hold"), FLATTEN_HOLD)
        self.assertEqual(classify_flatten_mode(""), FLATTEN_HOLD)

    def test_soft_maker_px_never_crosses(self):
        self.assertEqual(soft_maker_exit_px("SELL", self.BID, self.ASK), self.ASK)
        self.assertEqual(soft_maker_exit_px("BUY", self.BID, self.ASK), self.BID)
        # Must not equal the old hit-touch cross prices.
        self.assertNotEqual(soft_maker_exit_px("SELL", self.BID, self.ASK), self.BID)
        self.assertNotEqual(soft_maker_exit_px("BUY", self.BID, self.ASK), self.ASK)

    def test_exit_px_soft_is_passive_opposite(self):
        r = self._runner()
        self.assertEqual(
            r._exit_px("SELL", self.BID, self.ASK, "ev_gone NO_EDGE"), self.ASK
        )
        self.assertEqual(
            r._exit_px("BUY", self.BID, self.ASK, "adverse_fast (x)"), self.BID
        )
        self.assertFalse(r._hit_touch("ev_gone NO_EDGE"))
        self.assertFalse(r._hit_touch("adverse_fast (x)"))

    def test_exit_px_ev_hold_is_best_opposite_front(self):
        """TP / HOLD uses harvest clamp; wide ask (≥+1bp) keeps best ask."""
        r = self._runner()
        r.entry_px = self.BID
        inside = exit_limit_px("SELL", self.BID, self.ASK)
        # ASK is +1.5bp vs BID entry → clamp keeps ASK.
        self.assertEqual(
            r._exit_px("SELL", self.BID, self.ASK, "ev_hold"), self.ASK
        )
        r.net = -1
        r.entry_px = self.ASK
        self.assertEqual(
            r._exit_px("BUY", self.BID, self.ASK, "ev_hold"), self.BID
        )
        self.assertNotEqual(
            r._exit_px("SELL", self.BID, self.ASK, "ev_hold"), inside
        )

    def test_exit_px_hard_touch_fallback_still_cross(self):
        """HARD prefers MARKET; touch px remains aggressive fallback only."""
        r = self._runner()
        self.assertTrue(r._hit_touch("adverse_cut (-2.0bp)"))
        self.assertEqual(
            r._exit_px("SELL", self.BID, self.ASK, "adverse_cut (-2.0bp)"), self.BID
        )
        self.assertEqual(
            r._exit_px("BUY", self.BID, self.ASK, "toxic_adopt (-1.6bp)"), self.ASK
        )

    def test_advance_irreversible_hold_soft_hard(self):
        self.assertEqual(advance_flatten_state(FLATTEN_HOLD, "ev_hold"), FLATTEN_HOLD)
        self.assertEqual(
            advance_flatten_state(FLATTEN_HOLD, "ev_gone NO_EDGE"), FLATTEN_SOFT
        )
        self.assertEqual(
            advance_flatten_state(FLATTEN_SOFT, "adverse_fast (x)"), FLATTEN_SOFT
        )
        self.assertEqual(
            advance_flatten_state(FLATTEN_SOFT, "adverse_cut (-2.0bp)"), FLATTEN_HARD
        )
        # No downgrade HARD → SOFT / HOLD.
        self.assertEqual(
            advance_flatten_state(FLATTEN_HARD, "ev_gone NO_EDGE"), FLATTEN_HARD
        )
        self.assertEqual(advance_flatten_state(FLATTEN_HARD, "ev_hold"), FLATTEN_HARD)
        self.assertEqual(
            advance_flatten_state(FLATTEN_SOFT, "ev_hold"), FLATTEN_SOFT
        )

    def test_runner_advance_flatten_locks_state(self):
        r = self._runner()
        self.assertEqual(r._advance_flatten("ev_hold"), FLATTEN_HOLD)
        self.assertEqual(r._advance_flatten("ev_gone NO_EDGE"), FLATTEN_SOFT)
        self.assertEqual(r.flatten_state, FLATTEN_SOFT)
        self.assertEqual(r._advance_flatten("adverse_cut (-2.1bp)"), FLATTEN_HARD)
        self.assertEqual(r.flatten_state, FLATTEN_HARD)
        self.assertEqual(r._advance_flatten("ev_gone again"), FLATTEN_HARD)

    def test_double_exit_post_blocked_while_flat(self):
        """After inventory flat, second EXIT must not send_order."""
        r = self._runner()
        r.net = 0
        r.working_side = None
        before = r.posts
        r._post("SELL", self.ASK, "EXIT ev_gone NO_EDGE")
        self.assertIsNone(r.working_side)
        self.assertEqual(r.posts, before)
        r.client.send_order.assert_not_called()

    def test_soft_working_exit_not_duplicated_same_px(self):
        """Already resting SOFT EXIT at ask — tick must not double-post."""
        r = self._runner()
        r.net = 1
        r.working_side = "SELL"
        r.working_px = self.ASK
        r.working_id = "JRF-SOFT-1"
        r.flatten_state = FLATTEN_SOFT
        r.pending_exit_why = "ev_gone NO_EDGE"
        before = r.posts
        # Same-side same-px EXIT: _post path is gated by tick loop; direct
        # _exit_px must stay passive and working quote remains one.
        px = r._exit_px("SELL", self.BID, self.ASK, "ev_gone NO_EDGE")
        self.assertEqual(px, self.ASK)
        self.assertEqual(r.working_id, "JRF-SOFT-1")
        self.assertEqual(r.posts, before)
        r.client.send_order.assert_not_called()

    def test_soft_limit_exit_posts_passive_ask(self):
        r = self._runner()
        r.real = True
        r.flatten_state = FLATTEN_HOLD
        r.adverse_market_exits = 0
        r.client.send_order.return_value = {
            "status": "SUBMITTED",
            "child_order_acceptance_id": "JRF-SOFT",
        }
        with patch.object(r, "_feeder_bid_ask", return_value=(self.BID, self.ASK)):
            r._soft_limit_exit("ev_gone NO_EDGE", time.time())
        self.assertEqual(r.flatten_state, FLATTEN_SOFT)
        self.assertEqual(r.working_side, "SELL")
        self.assertEqual(r.working_px, self.ASK)
        self.assertEqual(r.adverse_market_exits, 0)
        args = r.client.send_order.call_args
        otype = (args.kwargs or {}).get("order_type") or args.args[3]
        self.assertEqual(otype, "LIMIT")

    def test_hard_market_exit_sends_market_and_flattens(self):
        r = self._runner()
        r.real = True
        r.flatten_state = FLATTEN_SOFT
        r.adverse_market_exits = 0
        r.adverse_cuts = 0
        r.ae = MagicMock()
        r.ae.close.return_value = {}
        r.history = []
        r.pnl_bp = 0.0
        r.pnl_jpy = 0.0
        r.trades = 0
        r.wins = 0
        r.fills = 0
        r._adverse_cooldown_until = 0.0
        r.client.send_order.return_value = {
            "status": "SUBMITTED",
            "child_order_acceptance_id": "JRF-HARD",
        }
        r.client.get_execution_price_by_acceptance_id.return_value = self.BID
        now = time.time()
        with patch.object(r, "_feeder_bid_ask", return_value=(self.BID, self.ASK)):
            r._hard_market_exit("adverse_cut (-2.0bp)", now)
        # Flat resets machine to HOLD for the next cycle.
        self.assertEqual(r.net, 0)
        self.assertEqual(r.flatten_state, FLATTEN_HOLD)
        self.assertEqual(r.adverse_market_exits, 1)
        self.assertGreater(r._adverse_cooldown_until, now)
        args = r.client.send_order.call_args
        otype = (args.kwargs or {}).get("order_type") or args.args[3]
        self.assertEqual(otype, "MARKET")

    def test_toxic_dispatch_hard_uses_market(self):
        r = self._runner()
        r.flatten_state = FLATTEN_HOLD
        with patch.object(r, "_hard_market_exit") as hard, patch.object(
            r, "_soft_limit_exit"
        ) as soft:
            r._toxic_maker_exit("toxic_adopt (-1.7bp)", time.time())
        hard.assert_called_once()
        soft.assert_not_called()
        self.assertEqual(r.flatten_state, FLATTEN_HARD)

    def test_classify_soft_escalate_is_hard(self):
        self.assertEqual(
            classify_flatten_mode("soft_escalate_slip (-1.60bp<=-1.5)"),
            FLATTEN_HARD,
        )
        self.assertEqual(
            classify_flatten_mode("soft_escalate_reprice (n=3>=3)"),
            FLATTEN_HARD,
        )


class TestSoftAdverseHardEscalate(unittest.TestCase):
    """adverse_fast Soft dig-down → HARD MARKET (reprice cap / slip)."""

    BID = 10_000_000.0
    ASK = 10_001_500.0

    def _runner(self) -> WOBIMM5Live:
        r = object.__new__(WOBIMM5Live)
        r.LANE = "WOBIMM5_LIVE"
        r.real = True
        r.size = 0.001
        r.max_inv = 0.001
        r.net = 1
        r.entry_px = self.BID
        r.entry_src = "PRIVATE_WS"
        r.entry_ts = time.time()
        r.working_side = "SELL"
        r.working_px = self.ASK
        r.working_id = "JRF-SOFT-1"
        r.working_posted_at = time.time()
        r.pending_exit_why = "adverse_fast (mae)"
        r.flatten_state = FLATTEN_SOFT
        r._soft_reprice_n = 0
        r._soft_exit_armed_ts = time.time()
        r.soft_to_hard_escalations = 0
        r.adverse_market_exits = 0
        r.adverse_cuts = 0
        r.ghosts = []
        r.recent_posts = []
        r.adopted_oids = set()
        r.posts = 0
        r.cancels = 0
        r.fills = 0
        r.api_errors = 0
        r.halt_trading = False
        r._adverse_cooldown_until = 0.0
        r.pnl_bp = 0.0
        r.pnl_jpy = 0.0
        r.trades = 0
        r.wins = 0
        r.history = []
        r.ae = MagicMock()
        r.ae.close.return_value = {}
        r.client = MagicMock()
        r.client.send_order.return_value = {
            "status": "SUBMITTED",
            "child_order_acceptance_id": "JRF-HARD-ESC",
        }
        r.client.get_execution_price_by_acceptance_id.return_value = self.BID
        return r

    def test_slip_beyond_1_5bp_escalates_to_hard(self):
        r = self._runner()
        why = "adverse_fast (adverse_fast_mae (-2.00bp <= -1.5))"
        esc = r._soft_adverse_escalate_reason(why, -1.60)
        self.assertIsNotNone(esc)
        self.assertIn("soft_escalate_slip", str(esc))
        self.assertGreaterEqual(SOFT_ADVERSE_ESCALATE_BP, 1.5)
        now = time.time()
        with patch.object(r, "_feeder_bid_ask", return_value=(self.BID, self.ASK)), patch.object(
            r, "_pull", return_value=False
        ) as pull:
            r._escalate_soft_to_hard(str(esc), now)
        pull.assert_called()
        self.assertEqual(r.soft_to_hard_escalations, 1)
        self.assertEqual(r.net, 0)  # HARD market flattened
        self.assertEqual(r.adverse_market_exits, 1)
        args = r.client.send_order.call_args
        otype = (args.kwargs or {}).get("order_type") or args.args[3]
        self.assertEqual(otype, "MARKET")

    def test_reprice_cap_escalates_to_hard(self):
        r = self._runner()
        r._soft_reprice_n = SOFT_REPRICE_MAX
        why = "adverse_fast (adverse_fast_mae (-0.40bp <= -1.5))"
        esc = r._soft_adverse_escalate_reason(why, -0.40)
        self.assertIsNotNone(esc)
        self.assertIn("soft_escalate_reprice", str(esc))
        now = time.time()
        with patch.object(r, "_feeder_bid_ask", return_value=(self.BID, self.ASK)), patch.object(
            r, "_pull", return_value=False
        ):
            r._escalate_soft_to_hard(str(esc), now)
        self.assertEqual(r.net, 0)
        self.assertEqual(r.soft_to_hard_escalations, 1)

    def test_under_cap_and_mild_slip_stays_soft(self):
        r = self._runner()
        r._soft_reprice_n = SOFT_REPRICE_MAX - 1
        why = "adverse_fast (adverse_fast_mae (-0.80bp <= -1.5))"
        esc = r._soft_adverse_escalate_reason(why, -0.80)
        self.assertIsNone(esc)

    def test_ev_gone_does_not_escalate_on_slip(self):
        """ev_gone Soft harvest wait must not HARD on −1.5bp alone."""
        r = self._runner()
        r.pending_exit_why = "ev_gone NO_EDGE"
        r.flatten_state = FLATTEN_SOFT
        esc = r._soft_adverse_escalate_reason("ev_gone NO_EDGE", -2.0)
        self.assertIsNone(esc)

    def test_soft_limit_exit_escalates_on_slip_before_chase(self):
        r = self._runner()
        r.working_side = None
        r.working_px = 0.0
        r.working_id = ""
        r.flatten_state = FLATTEN_HOLD
        # Mid already −2bp vs entry while Soft adverse_fast arms.
        bid = self.BID * (1.0 - 0.00025)  # ~−2.5bp for long if mid≈bid
        ask = bid + 1500.0
        mid_adverse_entry = self.BID
        r.entry_px = mid_adverse_entry
        # Force mid ≈ entry * (1 - 2.5bp)
        bid = mid_adverse_entry * (1.0 - 0.00025)
        ask = bid + 1000.0
        now = time.time()
        with patch.object(r, "_feeder_bid_ask", return_value=(bid, ask)), patch.object(
            r, "_hard_market_exit"
        ) as hard:
            # unrealized from mid
            mid = 0.5 * (bid + ask)
            unreal = ((mid / r.entry_px) - 1.0) * 1e4
            self.assertLessEqual(unreal, -SOFT_ADVERSE_ESCALATE_BP)
            r._soft_limit_exit("adverse_fast (mae -2.5bp)", now)
        hard.assert_called_once()
        self.assertFalse(r.client.send_order.called)  # soft LIMIT not posted


class TestTpGraceAndPrice(unittest.TestCase):
    """TP grace (defer healthy ev_gone) + best-opposite TP price."""

    BID = 10_000_000.0
    ASK = 10_001_500.0

    def _runner(self) -> WOBIMM5Live:
        r = object.__new__(WOBIMM5Live)
        r.LANE = "WOBIMM5_LIVE"
        r.real = True
        r.size = 0.001
        r.max_inv = 0.001
        r.net = 1
        r.entry_px = self.BID
        r.entry_src = "PRIVATE_WS"
        r.entry_ts = time.time()
        r.working_side = None
        r.working_px = 0.0
        r.working_id = ""
        r.working_posted_at = 0.0
        r.pending_exit_why = ""
        r.flatten_state = FLATTEN_HOLD
        r.ghosts = []
        r.recent_posts = []
        r.adopted_oids = set()
        r.posts = 0
        r.cancels = 0
        r.api_errors = 0
        r.halt_trading = False
        r.client = MagicMock()
        r.client.send_order.return_value = {
            "status": "SUBMITTED",
            "child_order_acceptance_id": "JRF-TP-1",
        }
        return r

    def test_tp_grace_sec_in_band(self):
        self.assertGreaterEqual(TP_GRACE_SEC, 5.0)
        self.assertLessEqual(TP_GRACE_SEC, 10.0)

    def test_arm_tp_posts_at_best_ask_front(self):
        r = self._runner()
        r.entry_px = self.BID
        with patch.object(r, "_feeder_bid_ask", return_value=(self.BID, self.ASK)):
            r._arm_tp_maker_rest()
        self.assertEqual(r.working_side, "SELL")
        # ASK is +1.5bp vs entry → clamp keeps ASK.
        self.assertEqual(float(r.working_px), self.ASK)
        self.assertEqual(r.pending_exit_why, "ev_hold")
        self.assertEqual(r.flatten_state, FLATTEN_HOLD)
        # Must not use inside-spread improve tick.
        self.assertNotEqual(float(r.working_px), exit_limit_px("SELL", self.BID, self.ASK))

    def test_tp_price_clamped_to_min_harvest_bp(self):
        """Tight ask (+0.2bp) → lift to entry+1.0bp; wide ask (+1.8bp) → keep ask."""
        self.assertEqual(MIN_HARVEST_BP, 1.0)
        entry = 10_000_000.0
        bid = entry - 500.0
        # +0.2bp ask — below harvest floor.
        ask_tight = entry * (1.0 + 0.2 / 1e4)  # 10,000,200
        floor = ceil_tick(entry * (1.0 + MIN_HARVEST_BP / 1e4))
        self.assertEqual(floor, 10_001_000.0)
        self.assertEqual(
            tp_harvest_exit_px("SELL", bid, ask_tight, entry), floor
        )
        r = self._runner()
        r.entry_px = entry
        with patch.object(r, "_feeder_bid_ask", return_value=(bid, ask_tight)):
            r._arm_tp_maker_rest()
        self.assertEqual(float(r.working_px), floor)
        # +1.8bp ask — already above floor → adopt ask.
        ask_wide = entry * (1.0 + 1.8 / 1e4)  # 10,001,800
        self.assertEqual(
            tp_harvest_exit_px("SELL", bid, ask_wide, entry), ask_wide
        )
        r2 = self._runner()
        r2.entry_px = entry
        r2.client.send_order.return_value = {
            "status": "SUBMITTED",
            "child_order_acceptance_id": "JRF-TP-WIDE",
        }
        with patch.object(r2, "_feeder_bid_ask", return_value=(bid, ask_wide)):
            r2._arm_tp_maker_rest()
        self.assertEqual(float(r2.working_px), ask_wide)
        # Soft ev_gone path must NOT clamp (post-grace safe descent).
        self.assertEqual(
            r2._exit_px("SELL", bid, ask_tight, "ev_gone NO_EDGE"), ask_tight
        )

    def test_tp_grace_defers_ev_gone_when_healthy(self):
        r = self._runner()
        r.entry_px = self.BID
        r.entry_ts = time.time() - 2.0  # inside grace
        now = time.time()
        self.assertTrue(r._should_defer_ev_gone("ev_gone NO_EDGE", now))
        with patch.object(r, "_feeder_bid_ask", return_value=(self.BID, self.ASK)):
            deferred = r._maybe_defer_soft_exit("ev_gone NO_EDGE", now, self.BID, self.ASK)
        self.assertTrue(deferred)
        self.assertEqual(r.flatten_state, FLATTEN_HOLD)
        self.assertEqual(r.pending_exit_why, "ev_hold")
        self.assertEqual(r.working_side, "SELL")
        self.assertEqual(float(r.working_px), self.ASK)

    def test_tp_grace_allows_adverse_fast(self):
        r = self._runner()
        r.entry_ts = time.time() - 1.0
        now = time.time()
        self.assertFalse(
            r._should_defer_ev_gone("adverse_fast (mae -1.6bp)", now)
        )
        self.assertFalse(
            r._maybe_defer_soft_exit(
                "adverse_fast (mae -1.6bp)", now, self.BID, self.ASK
            )
        )

    def test_tp_grace_expires_then_ev_gone_not_deferred(self):
        r = self._runner()
        r.entry_ts = time.time() - (TP_GRACE_SEC + 1.0)
        now = time.time()
        self.assertFalse(r._in_tp_grace(now))
        self.assertFalse(r._should_defer_ev_gone("ev_gone NO_EDGE", now))
        self.assertFalse(
            r._maybe_defer_soft_exit("ev_gone NO_EDGE", now, self.BID, self.ASK)
        )
        self.assertIsNone(r.working_side)  # did not auto-arm on non-defer


class TestSpreadGate(unittest.TestCase):
    """ENTER blocked unless best bid/ask spread >= MIN_ENTRY_SPREAD_BP."""

    def _runner(self) -> WOBIMM5Live:
        r = object.__new__(WOBIMM5Live)
        r.LANE = "WOBIMM5_LIVE"
        r.real = True
        r.size = 0.001
        r.max_inv = 0.001
        r.net = 0
        r.entry_px = 0.0
        r.working_side = None
        r.working_px = 0.0
        r.working_id = ""
        r.posts = 0
        r.cancels = 0
        r.api_errors = 0
        r.rejects = 0
        r.halt_trading = False
        r.ghosts = []
        r.recent_posts = []
        r.adopted_oids = set()
        r.spread_gate_blocks = 0
        r.client = MagicMock()
        r.client.send_order.return_value = {
            "status": "SUBMITTED",
            "child_order_acceptance_id": "JRF-ENTER-1",
        }
        return r

    def test_spread_gate_constant(self):
        self.assertEqual(MIN_ENTRY_SPREAD_BP, 1.2)
        self.assertGreater(MIN_ENTRY_SPREAD_BP, MIN_HARVEST_BP)

    def test_spread_gate_blocks_entry_when_narrow(self):
        """spread=0.85bp → ENTER blocked even if signal would fire."""
        bid = 10_000_000.0
        ask = bid * (1.0 + 0.85 / 1e4)  # +0.85bp
        self.assertAlmostEqual(spread_bp(bid, ask), 0.85, places=2)
        ok, sp = entry_spread_allows(bid, ask)
        self.assertFalse(ok)
        self.assertLess(sp, MIN_ENTRY_SPREAD_BP)
        r = self._runner()
        self.assertTrue(r._should_block_entry_for_spread(bid, ask))
        self.assertEqual(r.spread_gate_blocks, 1)
        # Must not place ENTER.
        before = r.posts
        if r._should_block_entry_for_spread(bid, ask):
            pass
        else:
            r._post("BUY", bid, "ENTER BUY_PRESSURE")
        self.assertEqual(r.posts, before)
        self.assertIsNone(r.working_side)

    def test_spread_gate_allows_entry_when_wide(self):
        """spread=1.30bp → gate open; ENTER posts."""
        bid = 10_000_000.0
        ask = bid * (1.0 + 1.30 / 1e4)  # +1.30bp
        self.assertAlmostEqual(spread_bp(bid, ask), 1.30, places=2)
        ok, sp = entry_spread_allows(bid, ask)
        self.assertTrue(ok)
        self.assertGreaterEqual(sp, MIN_ENTRY_SPREAD_BP)
        r = self._runner()
        self.assertFalse(r._should_block_entry_for_spread(bid, ask))
        self.assertEqual(r.spread_gate_blocks, 0)
        r._post("BUY", bid, "ENTER BUY_PRESSURE")
        self.assertEqual(r.posts, 1)
        self.assertEqual(r.working_side, "BUY")


class TestEntryPassiveOffset(unittest.TestCase):
    """ENTER rests ENTRY_OFFSET_BP behind touch (not at best bid/ask)."""

    def test_entry_price_with_passive_offset(self):
        self.assertEqual(ENTRY_OFFSET_BP, 0.5)
        bid = 11_800_000.0
        ask = bid * (1.0 + 1.5 / 1e4)  # wide enough for gate
        want_buy = floor_tick(bid * (1.0 - ENTRY_OFFSET_BP / 1e4))
        want_sell = ceil_tick(ask * (1.0 + ENTRY_OFFSET_BP / 1e4))
        # ≈ 590 JPY behind at 11.8M.
        self.assertEqual(want_buy, 11_799_410.0)
        self.assertLess(want_buy, bid)
        self.assertGreater(want_sell, ask)
        self.assertEqual(entry_passive_px("BUY", bid, ask), want_buy)
        self.assertEqual(entry_passive_px("SELL", bid, ask), want_sell)
        # maker_px must not pull the quote back to the touch.
        self.assertEqual(
            maker_px("BUY", entry_passive_px("BUY", bid, ask), bid, ask),
            want_buy,
        )
        self.assertEqual(
            maker_px("SELL", entry_passive_px("SELL", bid, ask), bid, ask),
            want_sell,
        )
        # Clamped TP from offset entry sits at/near opposite best (not buried).
        tp = tp_harvest_exit_px("SELL", bid, ask, want_buy)
        floor = ceil_tick(want_buy * (1.0 + MIN_HARVEST_BP / 1e4))
        self.assertEqual(tp, max(ask, floor))
        self.assertLessEqual(abs(tp - ask) / bid * 1e4, 0.6)


if __name__ == "__main__":
    unittest.main()
