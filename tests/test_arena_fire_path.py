"""
アリーナ発火経路の修繕テスト
============================
16h 真の0戦だった5本 (MicroTrend / RSI×3 / EmaTrend) について:

1. generate_signals 例外を sig=0 に黙殺しない（strat_id が SIGNAL_ERROR で残る）
2. 形成中1分足 (doji) や ffill/exit で最終バーの ±1 が消えない
3. MicroTrend の 4bp 初動は緩めない。TF2BP レーンへは配線しない
4. regime_gate / confidence 0.45 / spread 上限は据え置き
"""
import os
import importlib.util
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from antigravity.quant_pipeline.arena_signal import (
    bars_for_signal_eval,
    candle_close_position,
    missing_ohlcv_columns,
    read_last_signal,
    same_utc_minute,
    upsert_minute_bar,
)
from antigravity.quant_pipeline.run_dryrun_approved_arena import (
    FailedStrategyStub,
    SingleStrategyState,
    ApprovedStrategyArena,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APPROVED = os.path.join(REPO_ROOT, "strategies", "approved")
ARENA_SRC = os.path.join(REPO_ROOT, "antigravity", "quant_pipeline", "run_dryrun_approved_arena.py")


def load_approved(fname: str):
    path = os.path.join(APPROVED, fname)
    spec = importlib.util.spec_from_file_location(f"fire_{fname}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CustomStrategy()


def ohlcv_uptrend(n=150, start=13_000_000.0, step_bp=1.5, bullish=True):
    """step_bp per bar. bullish candles close near high."""
    closes = [start * ((1.0 + step_bp / 10000.0) ** i) for i in range(n)]
    rows = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        if bullish:
            high = max(o, c) * 1.00005
            low = min(o, c) * 0.99997
        else:
            high = max(o, c) * 1.00003
            low = min(o, c) * 0.99990
        rows.append({
            "timestamp": pd.Timestamp("2026-09-24 00:00", tz="UTC") + pd.Timedelta(minutes=i),
            "open": o,
            "high": high,
            "low": low,
            "close": c,
            "volume": 1.0,
        })
        prev = c
    return pd.DataFrame(rows)


class TestArenaSignalHelpers(unittest.TestCase):
    def test_doji_close_position_is_neutral_not_max_sell(self):
        pos = candle_close_position(high=[100, 100], low=[90, 100], close=[100, 100])
        self.assertAlmostEqual(float(pos[0]), 1.0)
        self.assertAlmostEqual(float(pos[1]), 0.5)

    def test_missing_ohlcv_columns(self):
        self.assertEqual(missing_ohlcv_columns(pd.DataFrame({"close": [1]})), ["open", "high", "low"])
        self.assertEqual(missing_ohlcv_columns(pd.DataFrame({"open": 1, "high": 1, "low": 1, "close": 1}, index=[0])), [])

    def test_read_last_signal_errors_are_explicit(self):
        sig, err = read_last_signal(pd.DataFrame({"close": [1, 2]}))
        self.assertEqual(sig, 0)
        self.assertEqual(err, "missing_signal_column")

        sig, err = read_last_signal(pd.DataFrame({"signal": [1.0, np.nan]}))
        self.assertEqual(sig, 0)
        self.assertEqual(err, "nan_signal")

        sig, err = read_last_signal(pd.DataFrame({"signal": [1, 0, -1]}))
        self.assertEqual(sig, -1)
        self.assertEqual(err, "")

    def test_bars_for_signal_eval_drops_forming_minute(self):
        df = ohlcv_uptrend(5)
        now = df["timestamp"].iloc[-1] + pd.Timedelta(seconds=20)
        eval_df = bars_for_signal_eval(df, now=now)
        self.assertEqual(len(eval_df), 4)
        self.assertEqual(eval_df["timestamp"].iloc[-1], df["timestamp"].iloc[-2])

    def test_bars_for_signal_eval_keeps_closed_minute(self):
        df = ohlcv_uptrend(5)
        now = df["timestamp"].iloc[-1] + pd.Timedelta(minutes=1, seconds=1)
        eval_df = bars_for_signal_eval(df, now=now)
        self.assertEqual(len(eval_df), 5)

    def test_same_utc_minute_naive_vs_aware(self):
        aware = pd.Timestamp("2026-09-24 05:16:40", tz="UTC")
        naive = pd.Timestamp("2026-09-24 05:16:10")
        self.assertTrue(same_utc_minute(aware, naive))
        jst_wrong = pd.Timestamp("2026-09-24 14:16:10")  # naive 14:16 must NOT match 05:16 UTC
        self.assertFalse(same_utc_minute(aware, jst_wrong))

    def test_upsert_minute_bar_does_not_spawn_5s_dojis(self):
        t0 = datetime(2026, 9, 24, 5, 16, 1, tzinfo=timezone.utc)
        df = upsert_minute_bar(None, 13_000_000.0, t0, volume=0.01)
        df = upsert_minute_bar(df, 13_000_100.0, t0 + timedelta(seconds=5), volume=0.01)
        df = upsert_minute_bar(df, 13_000_200.0, t0 + timedelta(seconds=50), volume=0.01)
        self.assertEqual(len(df), 1)
        self.assertEqual(float(df["close"].iloc[-1]), 13_000_200.0)
        df = upsert_minute_bar(df, 13_001_000.0, t0 + timedelta(minutes=1, seconds=2), volume=0.01)
        self.assertEqual(len(df), 2)


class TestSignalFailureStaysVisible(unittest.TestCase):
    def test_failed_load_stub_keeps_strat_id(self):
        stub = FailedStrategyStub("LOAD_FAILED:micro_trend_order_flow", "boom")
        state = SingleStrategyState(
            strat_id="micro_trend_order_flow",
            name=stub.name,
            file_path="strategies/approved/micro_trend_order_flow_approved.py",
            instance=stub,
            params={},
        )
        state.last_action = "LOAD_ERROR"
        state.last_reason = "RuntimeError: boom"
        dumped = state.to_dict()
        self.assertEqual(dumped["strat_id"], "micro_trend_order_flow")
        self.assertEqual(dumped["last_action"], "LOAD_ERROR")
        with self.assertRaises(RuntimeError):
            stub.generate_signals(pd.DataFrame({"open": [1], "high": [1], "low": [1], "close": [1]}))

    def test_record_signal_failure_does_not_look_like_flat_zero(self):
        stub = FailedStrategyStub("broken", "nope")
        state = SingleStrategyState("strat_7e09696a", stub.name, "x.py", stub, {})
        arena = object.__new__(ApprovedStrategyArena)
        arena._log_to_file = MagicMock()
        ApprovedStrategyArena._record_signal_failure(arena, state, RuntimeError("high missing"))
        self.assertEqual(state.last_action, "SIGNAL_ERROR")
        self.assertIn("high missing", state.last_reason)
        self.assertEqual(state.signal_fail_count, 1)
        self.assertIsNone(state.position)
        arena._log_to_file.assert_called()
        dumped = state.to_dict()
        self.assertEqual(dumped["strat_id"], "strat_7e09696a")
        self.assertEqual(dumped["last_action"], "SIGNAL_ERROR")
        self.assertGreaterEqual(dumped["signal_fail_count"], 1)


class TestMicroTrendFirePath(unittest.TestCase):
    def setUp(self):
        self.strat = load_approved("micro_trend_order_flow_approved.py")

    def test_four_bp_threshold_not_relaxed(self):
        self.assertEqual(self.strat.parameters["min_mom_pct"], 0.0004)

    def test_two_bp_move_does_not_fire(self):
        # 2bp/bar would have fired the old TF2BP-prototype 2bp threshold; 4bp must still require more.
        df = ohlcv_uptrend(n=120, step_bp=0.4, bullish=True)  # 1.6bp over 4 bars
        out = self.strat.generate_signals(df)
        self.assertEqual(int(out["signal"].iloc[-1]), 0)

    def test_clear_micro_trend_fires_plus_one(self):
        df = ohlcv_uptrend(n=120, step_bp=2.5, bullish=True)
        out = self.strat.generate_signals(df)
        self.assertEqual(int(out["signal"].iloc[-1]), 1)

    def test_forming_doji_does_not_reverse_exit_long(self):
        df = ohlcv_uptrend(n=120, step_bp=2.5, bullish=True)
        closed = self.strat.generate_signals(df)
        self.assertEqual(int(closed["signal"].iloc[-1]), 1)
        doji = df.copy()
        c = float(doji["close"].iloc[-1])
        doji.loc[doji.index[-1], ["open", "high", "low", "close"]] = c
        out = self.strat.generate_signals(doji)
        self.assertAlmostEqual(float(out["delta_ratio"].iloc[-1]), 0.0)
        self.assertEqual(int(out["signal"].iloc[-1]), 1)
        now = doji["timestamp"].iloc[-1] + pd.Timedelta(seconds=15)
        eval_df = bars_for_signal_eval(doji, now=now)
        out_closed = self.strat.generate_signals(eval_df)
        self.assertEqual(int(out_closed["signal"].iloc[-1]), 1)

    def test_not_wired_to_tf2bp(self):
        with open(os.path.join(APPROVED, "micro_trend_order_flow_approved.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("tf2bp", src.lower())
        self.assertNotIn("TF2BPStrategy", src)


class TestRsiFirePath(unittest.TestCase):
    FILES = [
        "strat_7e09696a_approved.py",
        "strat_9ca5d130_approved.py",
        "strat_a2a6d745_approved.py",
    ]

    def _dip_in_uptrend(self, n_up=100, n_dip=20):
        px = np.concatenate([
            np.linspace(12_800_000, 13_300_000, n_up),
            np.linspace(13_300_000, 13_180_000, n_dip),
        ])
        return pd.DataFrame({
            "timestamp": pd.date_range("2026-09-24", periods=len(px), freq="1min", tz="UTC"),
            "open": px,
            "high": px + 8000,
            "low": px - 8000,
            "close": px,
            "volume": 1.0,
        })

    def test_same_bar_exit_does_not_wipe_entry(self):
        for fname in self.FILES:
            with self.subTest(fname=fname):
                strat = load_approved(fname)
                df = self._dip_in_uptrend()
                out = strat.generate_signals(df)
                self.assertIn(1, set(out["signal"].tolist()), f"{fname} should fire long on dip-in-uptrend")
                last_nz = out.index[out["signal"] != 0][-1]
                self.assertEqual(int(out.loc[last_nz, "signal"]), 1)

    def test_last_closed_bar_survives_forming_mid_reversion(self):
        strat = load_approved("strat_7e09696a_approved.py")
        df = self._dip_in_uptrend()
        out = strat.generate_signals(df)
        self.assertEqual(int(out["signal"].iloc[-1]), 1)
        mid = float(out["bb_mid"].iloc[-1])
        # forming bar snaps to mid (arena used to read this as last=0)
        extra = df.iloc[[-1]].copy()
        extra["timestamp"] = extra["timestamp"] + pd.Timedelta(minutes=1)
        extra[["open", "high", "low", "close"]] = mid
        live = pd.concat([df, extra], ignore_index=True)
        now = live["timestamp"].iloc[-1] + pd.Timedelta(seconds=10)
        eval_df = bars_for_signal_eval(live, now=now)
        out_eval = strat.generate_signals(eval_df)
        self.assertEqual(int(out_eval["signal"].iloc[-1]), 1, "closed-bar eval must keep the oversold long")
        out_live = strat.generate_signals(live)
        self.assertEqual(int(out_live["signal"].iloc[-1]), 0, "mid-touch bar itself is an exit")


class TestEmaTrendFirePath(unittest.TestCase):
    def test_fires_on_volatile_uptrend_and_ffill_keeps_last(self):
        rng = np.random.default_rng(0)
        daily_vol = 0.03
        sig = daily_vol / np.sqrt(1440)
        rets = rng.normal(0.00005, sig, 300)
        px = 13_000_000 * np.exp(np.cumsum(rets))
        noise = np.abs(rng.normal(0, sig, 300))
        df = pd.DataFrame({
            "timestamp": pd.date_range("2026-09-24", periods=300, freq="1min", tz="UTC"),
            "open": np.roll(px, 1),
            "high": px * (1 + noise),
            "low": px * (1 - noise),
            "close": px,
            "volume": 1.0,
        })
        df.loc[0, "open"] = px[0]
        strat = load_approved("strat_efd3fab8_approved.py")
        out = strat.generate_signals(df)
        self.assertIn(int(out["signal"].iloc[-1]), (-1, 1))
        doji = df.copy()
        c = float(doji["close"].iloc[-1])
        doji.loc[doji.index[-1], ["open", "high", "low", "close"]] = c
        now = doji["timestamp"].iloc[-1] + pd.Timedelta(seconds=12)
        eval_df = bars_for_signal_eval(doji, now=now)
        out2 = strat.generate_signals(eval_df)
        self.assertIn(int(out2["signal"].iloc[-1]), (-1, 1))

    def test_min_divergence_not_relaxed(self):
        strat = load_approved("strat_efd3fab8_approved.py")
        self.assertEqual(strat.parameters["min_divergence_pct"], 0.0006)
        self.assertEqual(strat.parameters["min_bandwidth_pct"], 0.005)


class TestGatesUntouched(unittest.TestCase):
    def test_arena_runner_keeps_regime_confidence_spread_caps(self):
        with open(ARENA_SRC, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('block_reason="regime_gate"', src)
        self.assertIn("final_confidence < 0.45", src)
        self.assertIn("min(2500.0, max_spread_allowed)", src)
        self.assertIn('block_reason="spread_gate"', src)
        self.assertIn('block_reason="toxic_flow_gate"', src)
        self.assertNotIn("enable_real_trading=True", src)


if __name__ == "__main__":
    unittest.main()
