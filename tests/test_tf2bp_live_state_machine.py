"""TF2BP LIVE State Machine v1.1 — T1–T18 + integration (no REAL orders)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from core.bitflyer_client import BitFlyerClient, OrderSubmitUnknownError
from antigravity.quant_pipeline.tf2bp_live_lock import ExclusiveRunnerLock, RunnerLockBusy
from antigravity.quant_pipeline.tf2bp_live_state_machine import (
    LiveState,
    OrderErrorClass,
    OrderLookupResult,
    TF2BPLiveStateMachine,
    VenueSnapshot,
    classify_order_error,
    heartbeat_belongs_to_runner,
    read_heartbeat,
    watchdog_should_sigterm_on_hb_stall,
)
from antigravity.quant_pipeline import run_micro_live_soak_csr574 as runner_mod
from antigravity.risk_guard.watchdog import WatchdogSentinel


class TestClassifyErrors(unittest.TestCase):
    def test_margin_205_from_live_log_form(self):
        # Exact form from logs/tf2bp_micro_live.log
        exc = RuntimeError(
            'bitFlyer APIエラー (400): {"status":-205,"error_message":'
            '"Margin amount is insufficient for this order.","data":null}'
        )
        self.assertEqual(classify_order_error(exc), OrderErrorClass.NON_RETRYABLE_ENTRY)

    def test_order_submit_unknown(self):
        self.assertEqual(
            classify_order_error(OrderSubmitUnknownError("order_submit_unknown path=/x err=TimeoutError")),
            OrderErrorClass.ORDER_UNKNOWN,
        )


class _SMCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hb = self.root / "hb.json"
        self.audit = self.root / "audit.jsonl"
        self.halt = self.root / "ENTRY_HALT.flag"
        self.lock_path = self.root / "runner.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def _sm(self, **kw) -> TF2BPLiveStateMachine:
        return TF2BPLiveStateMachine(
            max_size=0.001,
            heartbeat_path=self.hb,
            audit_path=self.audit,
            entry_halt_flag_path=self.halt,
            **kw,
        )


class TestStateMachineCore(_SMCase):
    def test_t1_heartbeat_updates_without_trades(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True))
        t0 = read_heartbeat(self.hb)["timestamp"]
        time.sleep(0.05)
        sm.write_heartbeat()
        self.assertGreater(read_heartbeat(self.hb)["timestamp"], t0)
        self.assertEqual(read_heartbeat(self.hb)["pid"], sm.pid)

    def test_t2_boot_reconcile_adopt_long_no_extra_buy(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(
            VenueSnapshot(net=0.001, avg_entry=1e7, open_orders_known=True, open_order_count=0, ok=True)
        )
        self.assertEqual(sm.state, LiveState.LONG)
        ok, _ = sm.can_submit_buy(
            strategy_action="buy",
            venue_recheck=VenueSnapshot(net=0.001, open_orders_known=True, open_order_count=0, ok=True),
        )
        self.assertFalse(ok)

    def test_t3_venue_flat_gate_via_reconciling(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True))
        ok, reason = sm.can_submit_buy(
            strategy_action="buy",
            venue_recheck=VenueSnapshot(net=0.001, avg_entry=1.0, open_orders_known=True, open_order_count=0, ok=True),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "venue_non_flat")
        # READY_FLAT → RECONCILING → LONG (no shortcut)
        self.assertEqual(sm.state, LiveState.LONG)

    def test_t4_margin_205_entry_halt_no_storm(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True))
        sm.on_margin_insufficient(
            venue=VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True)
        )
        self.assertEqual(sm.state, LiveState.ENTRY_HALT)
        buys = sum(
            1
            for _ in range(50)
            if sm.can_submit_buy(
                strategy_action="buy",
                venue_recheck=VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True),
            )[0]
        )
        self.assertEqual(buys, 0)

    def test_t5_order_unknown_no_rebuy(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True))
        sm.mark_entry_pending(order_qty=0.001, order_id="x")
        sm.enter_order_unknown(detail={"reason": "timeout"})
        self.assertEqual(sm.state, LiveState.ENTRY_PENDING)
        ok, _ = sm.can_submit_buy(
            strategy_action="buy",
            venue_recheck=VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True),
        )
        self.assertFalse(ok)

    def test_t6_reconcile_failure_not_ready_flat(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(VenueSnapshot(net=None, ok=False, open_orders_known=False, error="boom"))
        self.assertEqual(sm.state, LiveState.SAFE_HALT)
        ok, _ = sm.can_submit_buy(
            strategy_action="buy",
            venue_recheck=VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True),
        )
        self.assertFalse(ok)

    def test_t8_audit_reconstructs_chain(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True))
        self.assertTrue(
            sm.can_submit_buy(
                strategy_action="buy",
                venue_recheck=VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True),
            )[0]
        )
        sm.mark_entry_pending(order_qty=0.001, order_id="oid-1")
        sm.on_entry_confirmed(
            VenueSnapshot(net=0.001, avg_entry=9.0, open_orders_known=True, open_order_count=0, ok=True),
            order_id="oid-1",
        )
        rows = [json.loads(l) for l in self.audit.read_text().splitlines() if l.strip()]
        self.assertTrue(all(r["run_id"] == sm.run_id for r in rows))
        self.assertIn("ORDER_SUBMIT", {r["event"] for r in rows})
        self.assertEqual(sm.state, LiveState.LONG)

    def test_t15_open_orders_failure(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(
            VenueSnapshot(net=0.0, ok=True, open_orders_known=False, open_order_count=0, error="oo_fail")
        )
        self.assertEqual(sm.state, LiveState.SAFE_HALT)

    def test_t16_partial_unknown_fill(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True))
        sm.mark_entry_pending(order_qty=0.001, order_id="u1")
        sm.enter_order_unknown(detail={"reason": "partial"})
        # Case C unresolved → SAFE_HALT
        sm.recover_order_unknown(
            venue=VenueSnapshot(net=None, ok=False, open_orders_known=False, error="unk"),
            order_lookup=OrderLookupResult(known=False),
            expected_side="BUY",
        )
        self.assertEqual(sm.state, LiveState.SAFE_HALT)
        self.assertFalse(
            sm.can_submit_buy(
                strategy_action="buy",
                venue_recheck=VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True),
            )[0]
        )

    def test_invalid_transition_rejected(self):
        sm = self._sm()
        with self.assertRaises(Exception):
            sm._transition("BAD", LiveState.LONG, {})


class TestLockAndCutover(_SMCase):
    def test_t9_double_runner_lock(self):
        a = ExclusiveRunnerLock(self.lock_path)
        b = ExclusiveRunnerLock(self.lock_path)
        a.acquire(run_id="A", blocking=False)
        with self.assertRaises(RunnerLockBusy):
            b.acquire(run_id="B", blocking=False)
        a.release()
        b.acquire(run_id="B", blocking=False)
        b.release()

    def test_t9_threaded_race_one_winner(self):
        winners: List[str] = []
        errors: List[str] = []

        def worker(name: str):
            lock = ExclusiveRunnerLock(self.lock_path)
            try:
                lock.acquire(run_id=name, blocking=False)
                winners.append(name)
                time.sleep(0.1)
                lock.release()
            except RunnerLockBusy:
                errors.append(name)

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(errors), 1)

    def test_t18_cutover_new_fails_while_old_holds(self):
        old = ExclusiveRunnerLock(self.lock_path)
        old.acquire(run_id="OLD", blocking=False)
        with patch.object(runner_mod, "RUNNER_LOCK", self.lock_path):
            with patch.object(runner_mod, "HB_PATH", self.hb):
                with patch.object(runner_mod, "SM_AUDIT", self.audit):
                    with patch.object(runner_mod, "_foreign_runner_pids", return_value=[]):
                        row = runner_mod.run_tf2bp(
                            size=0.001,
                            max_trades=1,
                            real=False,
                            interval=0.01,
                            max_runtime_sec=0.05,
                            daily_loss_jpy=800,
                        )
        self.assertEqual(row.get("stop_reason"), "runner_lock_busy")
        self.assertFalse(row.get("lock_acquired"))
        old.release()

    def test_h1_refuse_while_old_process_alive_before_lock(self):
        with patch.object(runner_mod, "RUNNER_LOCK", self.lock_path):
            with patch.object(runner_mod, "_foreign_runner_pids", return_value=[21810]):
                row = runner_mod.run_tf2bp(
                    size=0.001,
                    max_trades=1,
                    real=False,
                    interval=0.01,
                    max_runtime_sec=0.05,
                    daily_loss_jpy=800,
                )
        self.assertEqual(row.get("stop_reason"), "old_runner_still_active")
        self.assertFalse(row.get("lock_acquired"))
        self.assertFalse(self.lock_path.exists())


class TestHeartbeatIdentity(_SMCase):
    def test_t11_stale_run_id_not_alive(self):
        sm = self._sm(run_id="NEW")
        sm.begin_reconcile()
        sm.apply_reconcile(VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True))
        # forge stale hb from old run
        stale = {
            "run_id": "OLD",
            "pid": sm.pid + 99999,
            "timestamp": time.time(),
            "state": "READY_FLAT",
        }
        self.assertFalse(heartbeat_belongs_to_runner(stale, expected_pid=sm.pid))
        self.assertFalse(heartbeat_belongs_to_runner(stale, expected_run_id="NEW"))
        self.assertTrue(heartbeat_belongs_to_runner(read_heartbeat(self.hb), expected_pid=sm.pid, expected_run_id="NEW"))


class TestCrashRecovery(_SMCase):
    def test_t12_crash_while_long(self):
        # Simulate new process after crash: fresh SM + venue still long
        sm2 = self._sm(run_id="restart")
        sm2.begin_reconcile()
        sm2.apply_reconcile(
            VenueSnapshot(net=0.001, avg_entry=1.0, open_orders_known=True, open_order_count=0, ok=True)
        )
        self.assertEqual(sm2.state, LiveState.LONG)
        self.assertFalse(
            sm2.can_submit_buy(
                strategy_action="buy",
                venue_recheck=VenueSnapshot(net=0.001, open_orders_known=True, open_order_count=0, ok=True),
            )[0]
        )

    def test_t13_crash_during_entry_pending_recovery(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True))
        sm.mark_entry_pending(order_qty=0.001, order_id="e1")
        sm.enter_order_unknown(detail={"crash": True})
        # restart SM with venue filled
        sm2 = self._sm(run_id="r2")
        sm2.begin_reconcile()
        sm2.apply_reconcile(
            VenueSnapshot(net=0.001, avg_entry=2.0, open_orders_known=True, open_order_count=0, ok=True)
        )
        self.assertEqual(sm2.state, LiveState.LONG)
        # in-process recovery filled
        sm.recover_order_unknown(
            venue=VenueSnapshot(net=0.001, avg_entry=2.0, open_orders_known=True, open_order_count=0, ok=True),
            order_lookup=OrderLookupResult(known=True, filled=True, acceptance_id="e1"),
            expected_side="BUY",
        )
        self.assertEqual(sm.state, LiveState.LONG)

    def test_t14_crash_during_exit_pending(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(
            VenueSnapshot(net=0.001, avg_entry=1.0, open_orders_known=True, open_order_count=0, ok=True)
        )
        sm.mark_exit_pending(order_qty=0.001, order_id="s1")
        self.assertEqual(sm.state, LiveState.EXIT_PENDING)
        sm.recover_order_unknown(
            venue=VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True),
            order_lookup=OrderLookupResult(known=True, filled=True, acceptance_id="s1"),
            expected_side="SELL",
        )
        self.assertEqual(sm.state, LiveState.READY_FLAT)


class TestPostRetry(unittest.TestCase):
    def test_t17_post_count_one_on_read_error(self):
        with patch.dict(os.environ, {"ENABLE_REAL_TRADING": "false"}, clear=False):
            client = BitFlyerClient(enable_real_trading=True, max_order_size=0.01)
            client.api_key = "k"
            client.api_secret = "s"
            client.enable_real_trading = True
            posts = {"n": 0}

            class _Resp:
                def read(self):
                    raise TimeoutError("read timeout after accept")

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            def fake_urlopen(req, timeout=10):
                posts["n"] += 1
                return _Resp()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(OrderSubmitUnknownError):
                    client.send_order("FX_BTC_JPY", "BUY", 0.001, "MARKET")
            self.assertEqual(posts["n"], 1)


class TestWatchdog(_SMCase):
    def test_t7_no_sigterm_while_long(self):
        self.assertFalse(watchdog_should_sigterm_on_hb_stall({"state": "LONG"}))
        root = self.root
        hb = root / "tf2bp_live_heartbeat.json"
        halt = root / "TF2BP_ENTRY_HALT.flag"
        log = root / "tf2bp_micro_live.log"
        log.write_text("quiet\n")
        payload = {
            "run_id": "r1",
            "pid": 12345,
            "timestamp": time.time() - 9999,
            "state": "LONG",
            "venue_position": 0.001,
            "local_position": 0.001,
        }
        hb.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNotNone(read_heartbeat(hb))
        sentinel = WatchdogSentinel(base_dir=str(root), enable_auto_restart=True, heartbeat_timeout_sec=120.0)
        svc = sentinel.services["tf2bp_micro_live"]
        svc["heartbeat_file"] = str(hb)
        svc["entry_halt_flag"] = str(halt)
        svc["log_file"] = str(log)
        killed: List[Any] = []
        with patch.object(sentinel, "notifier", MagicMock()):
            with patch("os.kill", side_effect=lambda *a, **k: killed.append(a)):
                sentinel.last_heartbeat_alert_time = 0.0
                sentinel._check_explicit_heartbeat("tf2bp_micro_live", svc, pid=12345, now=time.time())
        self.assertEqual(killed, [])
        self.assertTrue(halt.exists())

    def test_tf2bp_restart_no_popen(self):
        sentinel = WatchdogSentinel(enable_auto_restart=True)
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="fail", stdout="")) as run:
            with patch("subprocess.Popen") as popen:
                sentinel.restart_service("tf2bp_micro_live")
                popen.assert_not_called()
                run.assert_called()


class TestIntegrationGatePath(_SMCase):
    """Strategy → Runner body gates → Client (transport mocked)."""

    def test_t10_presubmit_concurrency_blocks_second_buy(self):
        """After flat recheck passes, if venue flips before confirm, no double local LONG from second buy."""
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True))
        ok1, _ = sm.can_submit_buy(
            strategy_action="buy",
            venue_recheck=VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True),
        )
        self.assertTrue(ok1)
        sm.mark_entry_pending(order_qty=0.001, order_id="a")
        # concurrent fill from "other execution"
        other = VenueSnapshot(net=0.001, avg_entry=1.0, open_orders_known=True, open_order_count=0, ok=True)
        # second BUY attempt while ENTRY_PENDING
        ok2, reason = sm.can_submit_buy(strategy_action="buy", venue_recheck=other)
        self.assertFalse(ok2)
        self.assertIn("ENTRY_PENDING", reason)
        sm.on_entry_confirmed(other, order_id="a")
        self.assertEqual(sm.state, LiveState.LONG)

    def test_integration_run_tf2bp_uses_gate_and_lock(self):
        board = {
            "mid_price": 10000000.0,
            "bids": [{"price": 9999990.0, "size": 1.0}],
            "asks": [{"price": 10000010.0, "size": 1.0}],
        }
        posts: List[str] = []

        class FakeClient:
            enable_real_trading = False
            max_order_size = 0.05

            def get_positions(self, *a, **k):
                return []

            def get_child_orders(self, *a, **k):
                return []

            def send_order(self, product, side, size, order_type="MARKET"):
                posts.append(side)
                return {"status": "SIMULATED", "child_order_acceptance_id": f"sim-{len(posts)}"}

        with patch.object(runner_mod, "RUNNER_LOCK", self.lock_path):
            with patch.object(runner_mod, "HB_PATH", self.hb):
                with patch.object(runner_mod, "SM_AUDIT", self.audit):
                    with patch.object(runner_mod, "BitFlyerClient", return_value=FakeClient()):
                        with patch.object(runner_mod, "_fetch_board", return_value=board):
                            with patch.object(
                                runner_mod, "_fetch_exec_vols", return_value={"buy": 0.0, "sell": 0.0}
                            ):
                                # Force strategy to hold — no BUY; proves boot reconcile + lock path
                                with patch(
                                    "antigravity.strategies.tf2bp_strategy.TF2BPStrategy.on_tick",
                                    return_value={"action": "hold", "reason": "test"},
                                ), patch.object(runner_mod, "_foreign_runner_pids", return_value=[]):
                                    row = runner_mod.run_tf2bp(
                                        size=0.001,
                                        max_trades=0,
                                        real=False,
                                        interval=0.05,
                                        max_runtime_sec=0.25,
                                        daily_loss_jpy=800,
                                    )
        self.assertTrue(row.get("lock_acquired"))
        self.assertEqual(posts, [])
        self.assertIn(row.get("sm_state"), ("READY_FLAT", "STOPPED", "ENTRY_HALT", "SAFE_HALT"))


class TestPhase1OuterFrame(_SMCase):
    """CSR-608 Gate 1 stubs: consecutive 429 halt, -205 no retry, watchdog no-kill."""

    def test_note_consecutive_429_policy(self):
        n, halt = runner_mod.note_consecutive_429(0, is_429=True, threshold=5)
        self.assertEqual((n, halt), (1, False))
        n, halt = runner_mod.note_consecutive_429(4, is_429=True, threshold=5)
        self.assertEqual((n, halt), (5, True))
        n, halt = runner_mod.note_consecutive_429(5, is_429=False, threshold=5)
        self.assertEqual((n, halt), (0, False))

    def test_on_entry_halt_blocks_buy(self):
        sm = self._sm()
        sm.begin_reconcile()
        sm.apply_reconcile(VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True))
        sm.on_entry_halt(reason="consecutive_429")
        self.assertTrue(sm.entry_halt)
        self.assertEqual(sm.state, LiveState.ENTRY_HALT)
        ok, reason = sm.can_submit_buy(
            strategy_action="buy",
            venue_recheck=VenueSnapshot(net=0.0, open_orders_known=True, open_order_count=0, ok=True),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "entry_halt")
        self.assertTrue(self.halt.exists())

    def test_margin_205_no_buy_retry(self):
        posts: List[str] = []

        class FakeClient:
            enable_real_trading = False
            max_order_size = 0.05

            def get_positions(self, *a, **k):
                return []

            def get_child_orders(self, *a, **k):
                return []

            def send_order(self, product, side, size, order_type="MARKET"):
                posts.append(side)
                raise RuntimeError(
                    'bitFlyer APIエラー (400): {"status":-205,"error_message":'
                    '"Margin amount is insufficient for this order.","data":null}'
                )

        board = {
            "mid_price": 10000000.0,
            "bids": [{"price": 9999990.0, "size": 1.0}],
            "asks": [{"price": 10000010.0, "size": 1.0}],
        }
        stop = self.root / "TF2BP_LIVE_STOP.flag"
        halt = self.root / "ENTRY_HALT.flag"
        with patch.object(runner_mod, "RUNNER_LOCK", self.lock_path), patch.object(
            runner_mod, "HB_PATH", self.hb
        ), patch.object(runner_mod, "SM_AUDIT", self.audit), patch.object(
            runner_mod, "STOP_FLAG", stop
        ), patch.object(
            runner_mod, "ENTRY_HALT_FLAG", halt
        ), patch.object(
            runner_mod, "BitFlyerClient", return_value=FakeClient()
        ), patch.object(
            runner_mod, "_fetch_board", return_value=board
        ), patch.object(
            runner_mod, "_fetch_exec_vols", return_value={"buy": 0.0, "sell": 0.0}
        ), patch.object(
            runner_mod, "_foreign_runner_pids", return_value=[]
        ), patch(
            "antigravity.strategies.tf2bp_strategy.TF2BPStrategy.on_tick",
            return_value={"action": "buy", "reason": "force"},
        ):
            row = runner_mod.run_tf2bp(
                size=0.001,
                max_trades=0,
                real=False,
                interval=0.03,
                max_runtime_sec=0.25,
                daily_loss_jpy=800,
            )
        self.assertEqual(posts, ["BUY"])
        self.assertTrue(row.get("entry_halt"))
        self.assertIn(row.get("sm_state"), ("ENTRY_HALT", "STOPPED"))

    def test_consecutive_429_never_sends_buy(self):
        posts: List[str] = []

        class FakeClient:
            enable_real_trading = False
            max_order_size = 0.05

            def get_positions(self, *a, **k):
                return []

            def get_child_orders(self, *a, **k):
                return []

            def send_order(self, product, side, size, order_type="MARKET"):
                posts.append(side)
                return {"status": "SIMULATED", "child_order_acceptance_id": "x"}

        def boom(*a, **k):
            raise RuntimeError("HTTP Error 429: Too Many Requests")

        stop = self.root / "TF2BP_LIVE_STOP.flag"
        halt = self.root / "ENTRY_HALT.flag"
        with patch.object(runner_mod, "RUNNER_LOCK", self.lock_path), patch.object(
            runner_mod, "HB_PATH", self.hb
        ), patch.object(runner_mod, "SM_AUDIT", self.audit), patch.object(
            runner_mod, "STOP_FLAG", stop
        ), patch.object(
            runner_mod, "ENTRY_HALT_FLAG", halt
        ), patch.object(
            runner_mod, "BitFlyerClient", return_value=FakeClient()
        ), patch.object(
            runner_mod, "_fetch_board", side_effect=boom
        ), patch.object(
            runner_mod, "_fetch_exec_vols", return_value={"buy": 0.0, "sell": 0.0}
        ), patch.object(
            runner_mod, "_foreign_runner_pids", return_value=[]
        ), patch(
            "antigravity.strategies.tf2bp_strategy.TF2BPStrategy.on_tick",
            return_value={"action": "buy", "reason": "force"},
        ):
            row = runner_mod.run_tf2bp(
                size=0.001,
                max_trades=0,
                real=False,
                interval=0.02,
                max_runtime_sec=0.22,
                daily_loss_jpy=800,
            )
        self.assertEqual(posts, [])
        self.assertTrue(row.get("entry_halt"))
        self.assertGreaterEqual(int(row.get("consecutive_429") or 0), 5)
        self.assertIn(row.get("sm_state"), ("ENTRY_HALT", "STOPPED"))

    def test_watchdog_quiet_log_live_hb_no_sigterm(self):
        hb = self.root / "tf2bp_live_heartbeat.json"
        halt = self.root / "TF2BP_ENTRY_HALT.flag"
        log = self.root / "tf2bp_micro_live.log"
        log.write_text("quiet\n")
        stale = time.time() - 300.0
        os.utime(log, (stale, stale))
        pid = 4242
        hb.write_text(
            json.dumps(
                {
                    "run_id": "r1",
                    "pid": pid,
                    "timestamp": time.time(),
                    "state": "LONG",
                    "venue_position": 0.001,
                    "local_position": 0.001,
                }
            ),
            encoding="utf-8",
        )
        sentinel = WatchdogSentinel(
            base_dir=str(self.root),
            enable_auto_restart=True,
            heartbeat_timeout_sec=120.0,
        )
        svc = sentinel.services["tf2bp_micro_live"]
        self.assertFalse(svc.get("kill_on_heartbeat_stall"))
        self.assertEqual(svc.get("heartbeat_mode"), "explicit_file")
        svc["heartbeat_file"] = str(hb)
        svc["entry_halt_flag"] = str(halt)
        svc["log_file"] = str(log)
        killed: List[Any] = []
        with patch.object(sentinel, "notifier", MagicMock()), patch(
            "os.kill", side_effect=lambda *a, **k: killed.append(a)
        ), patch.object(sentinel, "restart_service"):
            now = time.time()
            for elapsed in (120.0, 300.0):
                payload = json.loads(hb.read_text(encoding="utf-8"))
                payload["timestamp"] = now
                hb.write_text(json.dumps(payload), encoding="utf-8")
                os.utime(log, (now - elapsed, now - elapsed))
                sentinel.last_heartbeat_alert_time = 0.0
                sentinel._check_explicit_heartbeat("tf2bp_micro_live", svc, pid=pid, now=now)
        self.assertEqual(killed, [])
        self.assertFalse(halt.exists())


if __name__ == "__main__":
    unittest.main()
