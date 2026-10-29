"""TF2B_WOBI3 LIVE — dryrun plan + post guards. No venue POST."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import TF2BWobi3DryRun
from antigravity.quant_pipeline.run_tf2b_wobi3_live import (
    CONFIRM_ENV,
    CONFIRM_TOKEN,
    SIZE_BTC,
    _refuse_real_gates,
    maker_px,
    post_blocked,
)
from antigravity.quant_pipeline.run_wobimm_live import public_maker_fill


class TestWobi3LiveGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.tmppath = Path(cls.tmpdir.name)
        cls.patchers = [
            patch("antigravity.quant_pipeline.run_tf2b_wobi3_live.GO_FLAG", cls.tmppath / "GO.flag"),
            patch("antigravity.quant_pipeline.run_tf2b_wobi3_live.STOP_FLAG", cls.tmppath / "STOP.flag"),
            patch("antigravity.quant_pipeline.run_tf2b_wobi3_live.AUDIT_PATH", cls.tmppath / "audit.jsonl"),
            patch("antigravity.quant_pipeline.run_tf2b_wobi3_live.LEDGER_PATH", cls.tmppath / "paired_ledger.jsonl"),
            patch("antigravity.quant_pipeline.run_tf2b_wobi3_live.FLATTEN_LAST", cls.tmppath / "flatten_last.json"),
            patch("antigravity.quant_pipeline.run_tf2b_wobi3_live.STATE_PATH", cls.tmppath / "state.json"),
            patch("antigravity.quant_pipeline.run_tf2b_wobi3_live.FEEDER_EXECS", cls.tmppath / "executions.jsonl"),
            patch("antigravity.quant_pipeline.run_tf2b_wobi3_live.AXE_SHADOW_LOG", cls.tmppath / "axe_shadow_audit.jsonl"),
        ]
        for p in cls.patchers:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in reversed(cls.patchers):
            p.stop()
        cls.tmpdir.cleanup()

    def test_refuse_without_go(self):
        with patch.dict(os.environ, {"CONFIRM_GO": "", "ENABLE_REAL_TRADING": "false"}, clear=False):
            msg = _refuse_real_gates("")
        self.assertIsNotNone(msg)
        self.assertIn("REFUSE", msg or "")

    def test_refuse_wrong_confirm(self):
        (self.tmppath / "GO.flag").write_text("status=ACTIVE\n", encoding="utf-8")
        with patch.dict(os.environ, {"CONFIRM_GO": CONFIRM_ENV, "ENABLE_REAL_TRADING": "true"}, clear=False):
            msg = _refuse_real_gates("WOBIMM")
        self.assertIsNotNone(msg)
        self.assertIn("confirm-go", msg or "")

    def test_accept_when_gates_pass(self):
        (self.tmppath / "GO.flag").write_text("status=ACTIVE\n", encoding="utf-8")
        stop = self.tmppath / "STOP.flag"
        if stop.exists():
            stop.unlink()
        with patch.dict(os.environ, {"CONFIRM_GO": CONFIRM_ENV, "ENABLE_REAL_TRADING": "true"}, clear=False):
            self.assertIsNone(_refuse_real_gates(CONFIRM_TOKEN))

    def test_refuse_when_stop_flag(self):
        (self.tmppath / "GO.flag").write_text("status=ACTIVE\n", encoding="utf-8")
        (self.tmppath / "STOP.flag").write_text("STOP\n", encoding="utf-8")
        with patch.dict(os.environ, {"CONFIRM_GO": CONFIRM_ENV, "ENABLE_REAL_TRADING": "true"}, clear=False):
            msg = _refuse_real_gates(CONFIRM_TOKEN)
        self.assertIsNotNone(msg)
        self.assertIn("STOP", msg or "")

    def test_maker_px_never_crosses(self):
        self.assertEqual(maker_px("BUY", 101.0, 100.0, 101.0), 100.0)
        self.assertEqual(maker_px("SELL", 100.0, 100.0, 101.0), 101.0)

    def test_sell_blocked_when_not_long(self):
        self.assertEqual(post_blocked("SELL", 0.0, False, False), "not_long")
        self.assertEqual(post_blocked("SELL", -0.003, False, False), "not_long")

    def test_buy_blocked_when_long(self):
        self.assertEqual(post_blocked("BUY", 0.001, False, False), "already_long")

    def test_cover_buy_allowed_when_short(self):
        self.assertIsNone(post_blocked("BUY", -0.003, False, False))

    def test_sell_allowed_when_long(self):
        self.assertIsNone(post_blocked("SELL", 0.001, False, False))

    def test_block_while_working_or_unsynced(self):
        self.assertEqual(post_blocked("SELL", 0.001, True, False), "working_order")
        self.assertEqual(post_blocked("SELL", 0.001, False, True), "need_venue_sync")

    def test_autonomous_plan_matches_dryrun(self):
        dry = TF2BWobi3DryRun(market_source="shared_feeder")
        m = {
            "spread_bp": 1.2,
            "wobi": 0.40,
            "z_score": 1.2,
            "recent_ofi": 0.40,
            "d_wobi_dt": 0.10,
            "mid_price": 100.0,
            "ask_depletion_vel": 0.0,
            "micro_dev_bp": 0.0,
        }
        snap = SimpleNamespace(mid_price=100.0, best_bid=99.0, best_ask=101.0)
        plan = dry.plan_wobi3_pre_tick(snap, now=10.0, m=m, depth_bids=[], depth_asks=[])
        self.assertEqual(plan["intent"], "ENTER")
        self.assertEqual(plan["side"], "buy")
        self.assertIn("AUTONOMOUS", plan["reason"])

    def test_post_tick_rejects_wide_spread(self):
        dry = TF2BWobi3DryRun(market_source="shared_feeder")
        m = {
            "spread_bp": 4.0,
            "wobi": 0.80,
            "z_score": 2.0,
            "recent_ofi": 0.90,
            "d_wobi_dt": 0.20,
            "ask_depletion_vel": 0.4,
            "micro_dev_bp": 0.1,
        }
        plan = dry.plan_wobi3_post_tick({"action": "buy"}, now=10.0, m=m)
        self.assertEqual(plan["intent"], "HOLD")
        self.assertIn("SPREAD", plan["reason"])

    def test_board_imitation_order(self):
        oid = public_maker_fill(
            side="BUY",
            price=100.0,
            acceptance_id="JRF-1",
            public_execs=[{"buy_child_order_acceptance_id": "JRF-1", "size": 0.001}],
            best_bid=99.0,
            best_ask=101.0,
        )
        self.assertEqual(oid[0], "PUBLIC_OID")
        ghost = public_maker_fill(
            side="BUY",
            price=100.0,
            acceptance_id="JRF-ghost",
            public_execs=[{"side": "SELL", "price": 99.0, "size": 0.01}],
            best_bid=99.0,
            best_ask=100.0,
            allow_book=False,
        )
        self.assertIsNone(ghost)

    def test_size_cap(self):
        self.assertEqual(SIZE_BTC, 0.001)
        from antigravity.quant_pipeline.run_tf2b_wobi3_live import TF2BWobi3Live

        with self.assertRaises(ValueError):
            TF2BWobi3Live(real=False, size=0.002)

    def test_fake_breakout_veto_blocks_entry(self):
        from antigravity.quant_pipeline.run_tf2b_wobi3_live import TF2BWobi3LiveExecutor

        executor = TF2BWobi3LiveExecutor()
        
        # 1. Fake breakout: cancel_spike > 0.35 and taker_aggressiveness < 0.20
        fake_state = SimpleNamespace(
            spread_bp=1.0,
            cancel_spike=0.45,
            taker_aggressiveness=0.10,
            wobi=0.50,
            ofi=0.20,
        )
        self.assertFalse(executor.evaluate_entry_gate("BUY", fake_state))

        # 2. Valid breakout: cancel_spike low, strong taker aggressive
        valid_state = SimpleNamespace(
            spread_bp=1.0,
            cancel_spike=0.10,
            taker_aggressiveness=0.40,
            wobi=0.50,
            ofi=0.20,
        )
        self.assertTrue(executor.evaluate_entry_gate("BUY", valid_state))

        # 3. Spread blowout > 1.2 bp
        wide_state = SimpleNamespace(
            spread_bp=1.8,
            cancel_spike=0.10,
            taker_aggressiveness=0.40,
            wobi=0.50,
            ofi=0.20,
        )
        self.assertFalse(executor.evaluate_entry_gate("BUY", wide_state))

    def test_extend_hold_extends_tp_target(self):
        from antigravity.quant_pipeline.run_tf2b_wobi3_live import TF2BWobi3LiveExecutor

        executor = TF2BWobi3LiveExecutor()
        executor.pos_side = "BUY"
        executor.entry_px = 10_000_000.0

        # Gain >= 1.8 bp with strong WOBI/OFI continuation (+1.8bp is 10,001,800)
        mid = 10_001_850.0
        sustaining_state = SimpleNamespace(
            mid_price=mid,
            wobi=0.50,
            ofi=0.20,
            is_support_stack_collapsed=lambda side: False,
        )
        res = executor.evaluate_dynamic_exit(sustaining_state, now=10.0)
        self.assertEqual(res, "EXTEND_HOLD")
        self.assertTrue(executor.tp_extended)
        self.assertGreater(executor.trailing_floor_px, executor.entry_px)

        # After extension, if mid price drops below trailing floor -> TRAILING_EXIT
        dropped_state = SimpleNamespace(
            mid_price=executor.trailing_floor_px - 10.0,
            wobi=0.10,
            ofi=0.00,
            is_support_stack_collapsed=lambda side: False,
        )
        res_trail = executor.evaluate_dynamic_exit(dropped_state, now=15.0)
        self.assertEqual(res_trail, "TRAILING_EXIT")

    def test_order_lifecycle_microstructure_driven(self):
        from antigravity.quant_pipeline.run_tf2b_wobi3_live import TF2BWobi3LiveExecutor

        executor = TF2BWobi3LiveExecutor()
        executor.active_order_id = "JRF-ORDER-123"
        executor.entry_px = 12750000.0
        executor.order_placed_ts = 100.0

        # 1. 正常時: 時間経過（20秒）しても板構造が健全（壁あり・乖離なし・OFI良好）なら注文を維持
        pulled = executor.execute_order_lifecycle(
            now=120.0,
            best_bid=12750005.0,
            best_ask=12751000.0,
            wobi=0.25,
            recent_ofi=0.10,
            depth_vol=0.080,
        )
        self.assertFalse(pulled)
        self.assertEqual(executor.active_order_id, "JRF-ORDER-123")

        # 2. 板乖離（Book Drift: Best Bid が指値から 10 円以上乖離）時は即座に撤退
        pulled = executor.execute_order_lifecycle(
            now=121.0,
            best_bid=12750020.0,
            best_ask=12751000.0,
            wobi=0.25,
            recent_ofi=0.10,
            depth_vol=0.080,
        )
        self.assertTrue(pulled)
        self.assertIsNone(executor.active_order_id)

    def test_flatten_ensures_zero_position_under_rate_limit(self):
        from antigravity.quant_pipeline.run_tf2b_wobi3_live import TF2BWobi3LiveExecutor

        executor = TF2BWobi3LiveExecutor()
        now = 1000.0
        # Exhaust standard POST rate limit (30 requests in 300s, interval < 2.0s)
        executor.last_post_api_time = now - 0.5
        executor.post_api_calls_300s = [now - 10.0] * 30
        self.assertFalse(executor._can_call_api(now, is_cancel=False))

        # Position is active
        executor.pos_side = "BUY"
        executor.pos_size = 0.001
        executor.entry_px = 12_000_000.0
        executor.active_order_id = "JRF-LIMIT-999"
        executor.order_placed_ts = now - 1.0

        # Flatten must succeed via emergency bypass & priority cancel
        res = executor.flatten(reason="operator_emergency_flatten")
        self.assertTrue(res["flat_ok"])
        self.assertEqual(res["venue_net"], 0.0)
        self.assertEqual(executor.pos_side, "FLAT")
        self.assertEqual(executor.pos_size, 0.0)
        self.assertIsNone(executor.active_order_id)

    def _paper_live(self):
        from antigravity.quant_pipeline.run_tf2b_wobi3_live import TF2BWobi3Live

        runner = TF2BWobi3Live(real=False, size=SIZE_BTC, market_source="shared_feeder")
        runner.need_sync = False
        runner._venue_ok = True
        runner.halt_new_entries = False
        if runner.exec_fp:
            self.addCleanup(runner.exec_fp.close)
        return runner

    def test_unwind_exact_amount_with_dust(self):
        from antigravity.quant_pipeline.run_tf2b_wobi3_live import TF2BWobi3LiveExecutor

        executor = TF2BWobi3LiveExecutor()
        self.assertAlmostEqual(executor.get_unwind_order_size(0.00100198), 0.00100198, places=8)
        self.assertNotEqual(round(executor.get_unwind_order_size(0.00100198), 8), 0.001)

        runner = self._paper_live()
        runner.venue_net = 0.00100198
        runner.confirmed_net = 0.00100198
        order = runner._post_unwind("SELL", 12_000_000.0, time.time(), "FAST_HARVEST")
        self.assertIsNotNone(order)
        self.assertAlmostEqual(order.size, 0.00100198, places=8)
        self.assertNotEqual(round(order.size, 8), 0.001)

    def test_unwind_exact_amount_with_dust_large(self):
        from antigravity.quant_pipeline.run_tf2b_wobi3_live import TF2BWobi3LiveExecutor

        executor = TF2BWobi3LiveExecutor()
        self.assertAlmostEqual(executor.get_unwind_order_size(0.00119978), 0.00119978, places=8)
        self.assertNotEqual(round(executor.get_unwind_order_size(0.00119978), 8), 0.001)

        runner = self._paper_live()
        runner.venue_net = 0.00119978
        runner.confirmed_net = 0.00119978
        order = runner._post_unwind("SELL", 12_000_000.0, time.time(), "TP_EXTENDED")
        self.assertIsNotNone(order)
        self.assertAlmostEqual(order.size, 0.00119978, places=8)
        self.assertNotEqual(round(order.size, 8), 0.001)

    def test_venue_flat_verification(self):
        from antigravity.quant_pipeline.run_tf2b_wobi3_live import TF2BWobi3LiveExecutor

        executor = TF2BWobi3LiveExecutor()
        leftover = 0.00100198 - executor.get_unwind_order_size(0.00100198)
        self.assertAlmostEqual(leftover, 0.0, places=8)
        self.assertTrue(executor.check_post_exit_flat(0.0))
        self.assertFalse(executor.check_post_exit_flat(0.00000198))
        self.assertFalse(executor.check_post_exit_flat(0.00019978))

        runner = self._paper_live()
        runner.venue_net = 0.00100198
        runner.confirmed_net = 0.00100198
        runner.halt_new_entries = True
        order = runner._post_unwind("SELL", 12_000_000.0, time.time(), "ADVERSE_CUT")
        self.assertIsNotNone(order)
        runner._on_confirmed_fill(order, "PUBLIC_OID", 12_000_000.0)
        self.assertAlmostEqual(runner.confirmed_net, 0.0, places=8)
        self.assertAlmostEqual(runner.venue_net, 0.0, places=8)
        self.assertTrue(runner.check_post_exit_flat(runner.venue_net))
        self.assertFalse(runner.halt_new_entries)
        # クリーンな Flat: 次期シグナル待機（新規 BUY はブロックされない）
        self.assertIsNone(post_blocked("BUY", runner.confirmed_net, runner.working is not None, runner.need_sync))

    def test_entry_absorbs_short_dust_then_exit_flats(self):
        from antigravity.quant_pipeline.run_tf2b_wobi3_live import TF2BWobi3LiveExecutor

        dust = -0.00008712
        executor = TF2BWobi3LiveExecutor()
        entry_sz = executor.get_entry_order_size(dust)
        self.assertAlmostEqual(entry_sz, 0.00108712, places=8)
        after_entry = round(dust + entry_sz, 8)
        self.assertAlmostEqual(after_entry, 0.001, places=8)
        exit_sz = executor.get_unwind_order_size(after_entry)
        self.assertAlmostEqual(exit_sz, 0.001, places=8)
        self.assertAlmostEqual(round(after_entry - exit_sz, 8), 0.0, places=8)

        self.assertIsNone(post_blocked("BUY", dust, False, False))
        runner = self._paper_live()
        runner.venue_net = dust
        runner.confirmed_net = dust
        runner.halt_new_entries = False
        order = runner._post(
            "BUY", 12_000_000.0, time.time(), "ENTRY_WITH_DUST",
            size=runner.get_entry_order_size(dust),
        )
        self.assertIsNotNone(order)
        self.assertAlmostEqual(order.size, 0.00108712, places=8)

    def test_axe_shadow_evaluation(self):
        runner = self._paper_live()
        if runner.axe is None:
            self.skipTest("DistilledAxe not loaded")
        self.assertGreater(len(runner.axe.matrix), 0)
        mid = 12_580_000.0
        res = runner.evaluate_shadow_axe(
            event="TEST_EVAL",
            m={"wobi": 0.40, "spread_bp": 1.2, "cancel_ask": 0.05, "norm_disloc": 1.0},
            snap=None,
            live_decision="POST_BUY",
            details={"unit_test": True, "mid": mid},
        )
        self.assertIsNotNone(res)
        self.assertEqual(res["kind"], "predict")
        self.assertNotIn("axe_action", res)
        self.assertNotIn("agreement", res)
        self.assertIsNone(res["p_fill"])
        self.assertIsNone(res["m_adv_1s_bp"])
        self.assertIsNone(res["ev_quote_bp"])
        self.assertEqual(res["mid_t"], mid)
        if res["lut_drift_yen"] is not None:
            self.assertAlmostEqual(res["mu_1s_bp"], res["lut_drift_yen"] / mid * 10000.0, places=4)

        shadow_file = self.tmppath / "axe_shadow_audit.jsonl"
        self.assertTrue(shadow_file.exists())
        import json
        lines = shadow_file.read_text(encoding="utf-8").strip().split("\n")
        last_record = json.loads(lines[-1])
        self.assertEqual(last_record["event"], "TEST_EVAL")
        self.assertEqual(last_record["live_context"], "POST_BUY")
        self.assertEqual(last_record["kind"], "predict")

    def test_axe_realization_backfill(self):
        runner = self._paper_live()
        if runner.axe is None:
            self.skipTest("DistilledAxe not loaded")
        mid_t = 12_580_000.0
        predict = runner.evaluate_shadow_axe(
            event="TEST_EVAL",
            m={"wobi": 0.10, "spread_bp": 1.5},
            live_decision="WAIT",
            details={"mid": mid_t},
        )
        self.assertIsNotNone(predict)
        later_mid = mid_t * (1.0 - 2.0 / 10000.0)
        runner._flush_axe_realizations(time.time() + 1.1, later_mid)
        import json
        rows = [
            json.loads(line)
            for line in (self.tmppath / "axe_shadow_audit.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        realized = [r for r in rows if r.get("kind") == "realized" and r.get("horizon_s") == 1]
        self.assertTrue(realized)
        self.assertAlmostEqual(realized[-1]["delta_mid_bp"], -2.0, places=2)
        self.assertEqual(realized[-1]["id"], predict["id"])

    def test_distilled_axe_returns_numbers_not_verbs(self):
        runner = self._paper_live()
        if runner.axe is None:
            self.skipTest("DistilledAxe not loaded")
        out = runner.axe.evaluate(0.40, 1.2, 0.05, 1.0, mid=12_580_000.0)
        self.assertNotIn("action", out)
        self.assertNotIn("grade", out)
        self.assertIsNone(out["p_fill"])
        self.assertIsNone(out["ev_quote_bp"])
        self.assertIn("mu_1s_bp", out)


if __name__ == "__main__":
    unittest.main()
