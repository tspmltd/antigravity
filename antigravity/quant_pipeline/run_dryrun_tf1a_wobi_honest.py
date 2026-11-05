#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TF1A_WOBI_HONEST — 本来のトレンドフォロー戦略 (WIRE=NO · HONEST約定).

設計思想:
- 期待値のあるトレンドを捉えて収益を最大化する。
- 保有時間は比較的長め（ノイズを許容しトレンドをホールド）。
- 1回の収益目標は +5.0 bp 以上（損小利大設計）。
- ENTER (v3, 2026-11-04 gate validation):
  base = trend_up ∧ (book_support ∨ breakout)
  ∧ micro_ok (micro_dev_bp>0 ∨ d_wobi_dt≥0 ∨ z_score≥0)
  ∧ agent_ok (TrendFollow: not down; prefer up/trend)
  fresh5 / SPREAD_MAX=2.0 は検証で非有効 → 観測のみ（POSTしない条件から外す）。
  ab_v1 (streak≥3) は失策 → 不採用。cand_ab_v1 は観測のみ。
- トレイリングストップ:
    * 含み益 < +5.0 bp: ハードストップ -4.0 bp または上位トレンド崩壊までホールド（微小ノイズでの途中撤退を排除）
    * 含み益 >= +5.0 bp: ストップを +3.0 bp へ引き上げ（+5 到達を +0.5 まで戻さない）
    * 含み益 >= +8.0 bp: ピークから -2.5 bp の動的トレイリングで利益最大化
    * HARD_STOP の EXIT は bid に当てて即 Flatten（壁避け指値が -4 を -6〜-14 にするのを止める）
- 約定: BOARD_IMITATION (public_maker_fill)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import (  # noqa: E402
    calculate_optimal_order_price,
    _append,
    _iso,
    _log,
)
from antigravity.quant_pipeline.adverse_excursion import (  # noqa: E402
    AdverseExcursionTracker,
)
from antigravity.quant_pipeline.position_flow_strength import (  # noqa: E402
    PositionFlowStrength,
)
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi5 import TF2BWobi5DryRun  # noqa: E402
from antigravity.quant_pipeline.run_wobimm_live import public_maker_fill  # noqa: E402
from antigravity.quant_pipeline.wobimm3_policy import (  # noqa: E402
    Wobi3Market,
    entry_gate,
    SPREAD_MAX_BP,  # observational threshold only for TF1A (CSR-662 / gate_validation)
)

# TF1A maker: do not hard-block on wobimm3 SPREAD_MAX (validated 2026-11-04).
TF1A_USE_SPREAD_GATE = False
# fresh5 was wired in v2; validation showed drop_fresh5 lifts fwd60/300 → observe only.
TF1A_USE_FRESH5_GATE = False
ENTER_RET60_OBS_BP = 5.0  # cand_fresh5 / ab_v1 observe ceiling

OUT_DIR = AG / "data" / "mm_research" / "tf1a_wobi_honest_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)
FEEDER_EXECS = AG / "data" / "market_feeder" / "executions.jsonl"
GATE_LOG_FILE = OUT_DIR / "gate_ticks.jsonl"

# ENTER + observation streak floors (board pressure, not lag momentum).
OBS_WOBI_FLOOR = 0.15
OBS_OFI_FLOOR = 0.0  # ofi > 0
ENTER_STREAK_MIN = 3
ENTER_RET60_MAX_BP = ENTER_RET60_OBS_BP  # observe cand_fresh5; not required when TF1A_USE_FRESH5_GATE=False
# TrendFollowAgent-aligned (yen move over mid window). Soft floor vs agent ¥400.
AGENT_TREND_MIN_YEN = 200.0
AGENT_STRENGTH_FLOOR = 0.15
COUNCIL_STATE_PATH = AG / "configs" / "agents_council_state.json"
COUNCIL_MAX_AGE_SEC = 30.0


def eval_tf1a_enter_v2(
    *,
    trend_up: bool,
    book_support: bool,
    breakout: bool,
    ret60_bp: float,
    micro_dev_bp: float,
    d_wobi_dt: float,
    z_score: float,
    agent_direction: str,
    agent_strength: float,
    agent_regime: str,
) -> tuple[bool, Dict[str, bool]]:
    """ENTER predicate (v3). ab_v1 never used. fresh5 optional via TF1A_USE_FRESH5_GATE."""
    base = bool(trend_up and (book_support or breakout))
    fresh5 = bool(ret60_bp < ENTER_RET60_MAX_BP)
    micro_ok = bool(micro_dev_bp > 0.0 or d_wobi_dt >= 0.0 or z_score >= 0.0)
    # Block clear agent DOWN / warmup only. Neutral/range still allowed with base.
    agent_ok = bool(agent_direction != "down" and agent_regime != "warmup")
    if agent_direction == "up" and float(agent_strength) >= AGENT_STRENGTH_FLOOR:
        agent_ok = True
    flags = {
        "base": base,
        "fresh5": fresh5,
        "micro_ok": micro_ok,
        "agent_ok": agent_ok,
    }
    ok = bool(base and micro_ok and agent_ok)
    if TF1A_USE_FRESH5_GATE:
        ok = bool(ok and fresh5)
    return ok, flags


class TF1AWobiHonestDryRun(TF2BWobi5DryRun):
    ENTRY_POLICY = "tf1a_wobi"
    LANE_NAME = "TF1A_WOBI_HONEST"
    STATE_FILE = AG / "data" / "dryrun_tf1a_wobi_honest_state.json"
    PID_FILE = AG / "data" / "dryrun_tf1a_wobi_honest.pid"
    LEDGER_FILE = OUT_DIR / "paired_ledger.jsonl"
    AUDIT_FILE = OUT_DIR / "audit_events.jsonl"
    REPORT_MINUTE = 12

    # TF パラメータ
    TARGET_MIN_PROFIT_BP = 5.0     # 最低目標利益: +5bp
    TRAILING_ACTIVATION_BP = 8.0   # トレイリング開始利益: +8bp
    TRAILING_STEP_BP = 2.5         # トレイリング許容後退幅: 2.5bp
    HARD_STOP_BP = -4.0            # ノイズを耐えるハードストップ: -4.0bp
    PROFIT_LOCK_BP = 3.0           # +5bp到達後は +3bp を割ったら切る（旧 +0.5 は目標を吐き出す）

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.quote_side: Optional[str] = None
        self.quote_px = 0.0
        self.working_id = ""
        self.pending_exit_tag = ""
        self.pending_exit_reason = ""
        self.exec_fp: Optional[Any] = None

        # トレンドフォロー管理ステート
        self.peak_pnl_bp = 0.0
        self.entry_ts = 0.0
        self.entry_spread_bp = 0.0
        self.breakeven_locked = False

        # カウンター
        self.honest_posts = 0
        self.honest_cancels = 0
        self.honest_fills = 0
        self.gate_pulls = 0
        self.board_fills = 0
        self._last_hb = 0.0
        self._price_history: List[tuple[float, float]] = []  # (ts, mid)
        # Flat-tick gate observation (WIRE=NO). Does not affect ENTER.
        self.gate_log_path = Path(getattr(self, "GATE_LOG_FILE", GATE_LOG_FILE))
        self._ofi_streak = 0
        self._wobi_streak = 0
        self._book_streak = 0
        # Reuse Adverse Agent path instruments (MFE/MAE/AE). Observation only.
        self.ae_tracker = AdverseExcursionTracker(save_dir=str(AG / "data"))
        self.current_trade_id: Optional[str] = None
        # Same-direction flow strength → MFE research (observation only; no EXIT wire).
        self.flow_strength = PositionFlowStrength()
        self._last_flat_feat: Dict[str, Any] = {}
        self._council_cache: Dict[str, Any] = {}
        self._council_cache_ts = 0.0
        self._init_exec_stream()

    def _local_agent_trend(self, mid: float, now: float) -> Dict[str, Any]:
        """TrendFollowAgent-compatible local estimate from mid history."""
        hist = [p for t, p in self._price_history if t >= now - 60.0]
        if mid > 0:
            if not hist or abs(hist[-1] - mid) > 1e-9:
                hist = list(hist) + [mid]
        n = len(hist)
        if n < 20 or hist[0] <= 0:
            return {
                "direction": "neutral",
                "strength": 0.0,
                "regime": "warmup",
                "raw_move": 0.0,
                "source": "local_warmup",
            }
        price_delta = hist[-1] - hist[0]
        p_range = max(hist) - min(hist)
        strength = min(1.0, abs(price_delta) / 2500.0)
        if price_delta >= AGENT_TREND_MIN_YEN:
            direction = "up"
        elif price_delta <= -AGENT_TREND_MIN_YEN:
            direction = "down"
        else:
            direction = "neutral"
        if strength >= 0.35:
            regime = "trend"
        elif p_range >= 3500.0:
            regime = "high_vol"
        elif p_range <= 800.0:
            regime = "low_vol"
        else:
            regime = "range"
        return {
            "direction": direction,
            "strength": round(strength, 3),
            "regime": regime,
            "raw_move": round(price_delta, 2),
            "source": "local",
        }

    def _read_agent_trend(self, mid: float, now: float) -> Dict[str, Any]:
        """Prefer live council TrendFollowAgent; fall back to local mid window."""
        local = self._local_agent_trend(mid, now)
        try:
            if now - self._council_cache_ts >= 1.0:
                if COUNCIL_STATE_PATH.exists():
                    raw = json.loads(COUNCIL_STATE_PATH.read_text(encoding="utf-8"))
                    self._council_cache = raw if isinstance(raw, dict) else {}
                    self._council_cache_ts = now
            raw = self._council_cache
            ts = float(raw.get("timestamp") or 0.0)
            if ts > 1e12:  # epoch ms
                ts /= 1000.0
            age_ok = True
            if ts > 1e9:
                age_ok = (now - ts) <= COUNCIL_MAX_AGE_SEC
            conc = (raw.get("conclusions") or {}).get("TrendFollowAgent") or {}
            metrics = conc.get("metrics") if isinstance(conc, dict) else None
            if not isinstance(metrics, dict):
                metrics = {}
            direction = str(
                metrics.get("trend_direction")
                or raw.get("trend_direction")
                or ""
            ).lower()
            regime = str(
                metrics.get("regime_tag")
                or raw.get("trend_regime")
                or ""
            ).lower()
            strength = float(
                metrics.get("trend_strength")
                if metrics.get("trend_strength") is not None
                else (raw.get("trend_strength") or 0.0)
            )
            if age_ok and direction in ("up", "down", "neutral") and regime:
                return {
                    "direction": direction,
                    "strength": strength,
                    "regime": regime,
                    "raw_move": float(metrics.get("raw_move") or metrics.get("price_delta") or 0.0),
                    "source": "council",
                }
        except Exception:
            pass
        return local

    @staticmethod
    def _regime_tag(spread_bp: float, ret60_bp: float) -> str:
        """Same lightweight proxy as TF2B-10/15 shadow research."""
        mom_abs = abs(float(ret60_bp))
        vol = "high_vol" if float(spread_bp) >= 2.0 or mom_abs >= 3.0 else "low_vol"
        trend = "trend" if mom_abs >= 1.5 else "range"
        return f"{trend}_{vol}"

    def _find_ae_record(self):
        """Look up active first, then history.

        AdverseExcursion marks AE checkpoints complete at 30s and moves the
        record to history — TF holds are often longer, so active-only lookup
        yields mfe/mae=None on CLOSE.
        """
        tid = self.current_trade_id
        if not tid:
            return None
        for r in self.ae_tracker.active_records:
            if r.trade_id == tid:
                return r
        for r in self.ae_tracker.history_records:
            if r.trade_id == tid:
                return r
        return None

    def _active_ae_record(self):
        return self._find_ae_record()

    def _ae_track_entry(self, px: float, now: float, snap: Any, m: Dict[str, Any]) -> None:
        feat = dict(self._last_flat_feat or {})
        mid = float(getattr(snap, "mid_price", 0.0) or m.get("mid_price") or px)
        spread_bp = float(m.get("spread_bp") or feat.get("spread_bp") or 0.0)
        ret60 = float(feat.get("ret60_bp") or 0.0)
        trade_id = f"TF1A_{int(now * 1000)}"
        self.current_trade_id = trade_id
        try:
            self.ae_tracker.track_entry(
                trade_id=trade_id,
                side="buy",
                entry_price=float(px),
                entry_time=now,
                strategy_name=self.LANE_NAME,
                mid_price=mid,
                micro_price=mid,
                regime=self._regime_tag(spread_bp, ret60),
                spread_bp=spread_bp,
                inventory=0.001,
                meta={
                    "lane": self.LANE_NAME,
                    "wire": "NO",
                    "ret15_bp": feat.get("ret15_bp"),
                    "ret60_bp": feat.get("ret60_bp"),
                    "wobi": feat.get("wobi"),
                    "ofi": feat.get("ofi"),
                    "depletion": feat.get("depletion"),
                    "ofi_streak": feat.get("ofi_streak"),
                    "wobi_streak": feat.get("wobi_streak"),
                    "book_streak": feat.get("book_streak"),
                    "adverse_score": m.get("adverse_score"),
                    "vacuum_score": m.get("vacuum_score"),
                    "old_ok": feat.get("old_ok"),
                    "cand_ab_v1": feat.get("cand_ab_v1"),
                    "cand_fresh2": feat.get("cand_fresh2"),
                    "cand_persist3": feat.get("cand_persist3"),
                    "cand_legacy_trend_up": feat.get("cand_legacy_trend_up"),
                    "research": "adverse_excursion+tf2b10_regime_proxy+ab_v1_enter",
                },
            )
        except Exception as exc:
            _log(f"[TF1A_WOBI_HONEST] ae track_entry err: {exc}")
            self.current_trade_id = None

    def _ae_extend_path(self, rec: Any, mid: float, now: float, bid: float) -> None:
        """Keep MFE/MAE alive after AE's 30s checkpoint completion."""
        if mid <= 0 or float(rec.entry_price or 0.0) <= 0:
            return
        elapsed = now - float(rec.entry_time)
        if elapsed < 0:
            return
        eval_px = bid if bid > 0 else mid
        diff_bp = ((eval_px - float(rec.entry_price)) / float(rec.entry_price)) * 1e4
        if diff_bp < float(rec.mae_bp):
            rec.mae_bp = float(diff_bp)
            rec.peak_adverse_time = elapsed
            # Barrier first-touch clocks (for hit_order when both fire).
            if (
                float(diff_bp) <= self.HARD_STOP_BP
                and getattr(rec, "t_mae4", None) is None
            ):
                rec.t_mae4 = elapsed
        if diff_bp > float(rec.mfe_bp):
            rec.mfe_bp = float(diff_bp)
            if (
                float(diff_bp) >= self.TARGET_MIN_PROFIT_BP
                and getattr(rec, "t_mfe5", None) is None
            ):
                rec.t_mfe5 = elapsed

    def _ae_on_tick(self, mid: float, now: float, bid: float, ask: float) -> None:
        if not self.current_trade_id or mid <= 0:
            return
        try:
            self.ae_tracker.on_tick(
                current_price=mid,
                timestamp=now,
                best_bid=bid if bid > 0 else None,
                best_ask=ask if ask > 0 else None,
            )
        except Exception as exc:
            _log(f"[TF1A_WOBI_HONEST] ae on_tick err: {exc}")
            return
        rec = self._find_ae_record()
        if rec is None:
            return
        # AE tracker stops updating once checkpoints fill (~30s). TF holds longer.
        self._ae_extend_path(rec, mid, now, bid)

    def _ae_path_fields(self) -> Dict[str, Any]:
        rec = self._find_ae_record()
        if rec is None:
            return {}
        # Final extend from last known state is already on rec via ticks.
        hit5 = bool(rec.mfe_bp >= self.TARGET_MIN_PROFIT_BP)
        hit_stop = bool(rec.mae_bp <= self.HARD_STOP_BP)
        t5 = getattr(rec, "t_mfe5", None)
        t4 = getattr(rec, "t_mae4", None)
        if hit5 and hit_stop and t5 is not None and t4 is not None:
            hit_order = "mfe5_first" if float(t5) <= float(t4) else "mae4_first"
        elif hit5 and hit_stop:
            hit_order = "both"
        elif hit5:
            hit_order = "mfe5_first"
        elif hit_stop:
            hit_order = "mae4_first"
        else:
            hit_order = "neither"
        return {
            "mfe_bp": round(float(rec.mfe_bp), 3),
            "mae_bp": round(float(rec.mae_bp), 3),
            "ae_1s": rec.excursions.get("ae_1s"),
            "ae_3s": rec.excursions.get("ae_3s"),
            "ae_10s": rec.excursions.get("ae_10s"),
            "ae_30s": rec.excursions.get("ae_30s"),
            "time_to_adverse": rec.time_to_adverse_bucket,
            "fill_quality": rec.fill_quality,
            "regime": rec.regime,
            "hit5": hit5,
            "hit_stop_path": hit_stop,
            "hit_order": hit_order,
            "t_mfe5_s": None if t5 is None else round(float(t5), 2),
            "t_mae4_s": None if t4 is None else round(float(t4), 2),
            "entry_ret60_bp": (rec.meta or {}).get("ret60_bp"),
            "entry_wobi": (rec.meta or {}).get("wobi"),
            "entry_ofi": (rec.meta or {}).get("ofi"),
            "entry_ofi_streak": (rec.meta or {}).get("ofi_streak"),
            "entry_wobi_streak": (rec.meta or {}).get("wobi_streak"),
            "entry_adverse_score": (rec.meta or {}).get("adverse_score"),
            "entry_vacuum_score": (rec.meta or {}).get("vacuum_score"),
        }

    def _ae_close(self, exit_px: float, pnl_bp: float, reason: str) -> Dict[str, Any]:
        path = self._ae_path_fields()
        # Attach hold-period same-direction flow meter (research → MFE).
        try:
            flow = self.flow_strength.close_fields(
                now=time.time(),
                mfe_bp=path.get("mfe_bp"),
            )
            path.update(flow)
        except Exception as exc:
            _log(f"[TF1A_WOBI_HONEST] flow_strength close err: {exc}")
        tid = self.current_trade_id
        if tid:
            try:
                self.ae_tracker.on_close(
                    trade_id=tid,
                    exit_price=float(exit_px),
                    pnl_bp=float(pnl_bp),
                    exit_reason=reason,
                )
            except Exception as exc:
                _log(f"[TF1A_WOBI_HONEST] ae on_close err: {exc}")
        self.current_trade_id = None
        self.flow_strength.clear()
        return path

    def _init_exec_stream(self) -> None:
        if FEEDER_EXECS.exists():
            try:
                self.exec_fp = open(FEEDER_EXECS, "r", encoding="utf-8")
                self.exec_fp.seek(0, 2)
            except Exception as exc:
                _log(f"exec stream: {exc}")

    def _poll_execs(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.exec_fp:
            self._init_exec_stream()
        if not self.exec_fp:
            return out
        try:
            while True:
                line = self.exec_fp.readline()
                if not line:
                    break
                row = json.loads(line)
                out.extend(row.get("rows") or [])
        except Exception:
            pass
        return out

    def _update_trend_context(self, mid: float, now: float) -> tuple[float, float]:
        """直近の価格推移から短期(15s)と中期(60s)のモメンタムを算出"""
        self._price_history.append((now, mid))
        cutoff = now - 180.0
        self._price_history = [(t, p) for t, p in self._price_history if t >= cutoff]
        
        p15 = [p for t, p in self._price_history if t >= now - 15.0]
        p60 = [p for t, p in self._price_history if t >= now - 60.0]
        
        ret15_bp = ((mid / p15[0] - 1.0) * 1e4) if p15 else 0.0
        ret60_bp = ((mid / p60[0] - 1.0) * 1e4) if p60 else 0.0
        return ret15_bp, ret60_bp

    def _update_flow_streaks(self, wobi: float, ofi: float) -> None:
        """Consecutive flat ticks. Reset on break. Observation only."""
        if ofi > OBS_OFI_FLOOR:
            self._ofi_streak += 1
        else:
            self._ofi_streak = 0
        if wobi >= OBS_WOBI_FLOOR:
            self._wobi_streak += 1
        else:
            self._wobi_streak = 0
        if wobi >= OBS_WOBI_FLOOR and ofi > OBS_OFI_FLOOR:
            self._book_streak += 1
        else:
            self._book_streak = 0

    def _log_gate_tick(
        self,
        *,
        now: float,
        mid: float,
        spread_bp: float,
        ret15_bp: float,
        ret60_bp: float,
        wobi: float,
        ofi: float,
        depletion: float,
        trend_up: bool,
        book_support: bool,
        breakout: bool,
        old_ok: bool,
        posted: bool,
        working_buy: bool,
        spread_block: bool,
    ) -> None:
        """One JSONL row per flat tick. Candidate flags are offline-only."""
        row = {
            "ts": now,
            "ts_jst": _iso(),
            "mid": round(mid, 1),
            "spread_bp": round(spread_bp, 3),
            "ret15_bp": round(ret15_bp, 3),
            "ret60_bp": round(ret60_bp, 3),
            "wobi": round(wobi, 4),
            "ofi": round(ofi, 4),
            "depletion": round(depletion, 4),
            "ofi_streak": int(self._ofi_streak),
            "wobi_streak": int(self._wobi_streak),
            "book_streak": int(self._book_streak),
            "trend_up": bool(trend_up),
            "book_support": bool(book_support),
            "breakout": bool(breakout),
            "old_ok": bool(old_ok),
            "posted": bool(posted),
            "working_buy": bool(working_buy),
            "spread_block": bool(spread_block),
            # Observation candidates for A/B design (not used to trade).
            "cand_no_against": bool(wobi >= 0.0),
            "cand_persist3": bool(
                self._ofi_streak >= 3 and self._wobi_streak >= 3 and wobi >= 0.0
            ),
            "cand_persist5": bool(
                self._ofi_streak >= 5 and self._wobi_streak >= 5 and wobi >= 0.0
            ),
            "cand_fresh2": bool(ret60_bp < 2.0),
            "cand_fresh5": bool(ret60_bp < 5.0),
            # Rejected 2026-11-04 — observation only, never wired to POST.
            "cand_ab_v1": bool(
                wobi >= OBS_WOBI_FLOOR
                and ofi > OBS_OFI_FLOOR
                and self._ofi_streak >= ENTER_STREAK_MIN
                and self._wobi_streak >= ENTER_STREAK_MIN
                and ret60_bp < ENTER_RET60_MAX_BP
            ),
            # base only (pre-v2 filters)
            "cand_legacy_trend_up": bool(trend_up and (book_support or breakout)),
            "enter_v2_base": (self._last_flat_feat or {}).get("enter_v2_base"),
            "enter_v2_fresh5": (self._last_flat_feat or {}).get("enter_v2_fresh5"),
            "enter_v2_micro_ok": (self._last_flat_feat or {}).get("enter_v2_micro_ok"),
            "enter_v2_agent_ok": (self._last_flat_feat or {}).get("enter_v2_agent_ok"),
            "agent_direction": (self._last_flat_feat or {}).get("agent_direction"),
            "agent_strength": (self._last_flat_feat or {}).get("agent_strength"),
            "agent_regime": (self._last_flat_feat or {}).get("agent_regime"),
            "agent_source": (self._last_flat_feat or {}).get("agent_source"),
            "micro_dev_bp": (self._last_flat_feat or {}).get("micro_dev_bp"),
            "d_wobi_dt": (self._last_flat_feat or {}).get("d_wobi_dt"),
            "z_score": (self._last_flat_feat or {}).get("z_score"),
            "adverse_score": (self._last_flat_feat or {}).get("adverse_score"),
            "vacuum_score": (self._last_flat_feat or {}).get("vacuum_score"),
            "regime": (self._last_flat_feat or {}).get("regime")
            or self._regime_tag(spread_bp, ret60_bp),
        }
        try:
            _append(self.gate_log_path, row)
        except Exception as exc:
            _log(f"[TF1A_WOBI_HONEST] gate_log err: {exc}")

    def _post_quote(self, side: str, px: float, why: str) -> None:
        side = side.lower()
        if self.quote_side == side and abs(self.quote_px - px) <= 1e-9:
            return
        if side == "buy" and self.quote_side == "buy":
            return
        if self.quote_side and self.quote_side != side:
            self.honest_cancels += 1
        self.quote_side = side
        self.quote_px = float(px)
        self.working_id = f"SIM-{side.upper()}-{int(time.time() * 1000)}"
        self.honest_posts += 1
        _log(f"[TF1A_WOBI_HONEST] POST {side.upper()} @ {px:,.0f} ({why})")

    def _cancel_quote(self, why: str) -> None:
        if not self.quote_side:
            return
        _log(f"[TF1A_WOBI_HONEST] CANCEL {self.quote_side.upper()} ({why})")
        self.honest_cancels += 1
        self.quote_side = None
        self.quote_px = 0.0
        self.working_id = ""
        self.pending_exit_tag = ""
        self.pending_exit_reason = ""

    def _on_honest_fill(
        self,
        side: str,
        px: float,
        src: str,
        snap: Any,
        now: float,
        m: Dict[str, Any],
        depth_bids: List[Dict[str, float]],
        depth_asks: List[Dict[str, float]],
    ) -> None:
        self.honest_fills += 1
        self.board_fills += 1
        self.quote_side = None
        self.quote_px = 0.0
        self.working_id = ""
        _log(f"[TF1A_WOBI_HONEST] FILL {side.upper()} @ {px:,.0f} src={src}")
        
        if side == "buy" and not self.wobi3.position_side:
            self.entry_ts = now
            self.peak_pnl_bp = 0.0
            self.breakeven_locked = False
            self.entry_spread_bp = float(
                (self._last_flat_feat or {}).get("spread_bp")
                or m.get("spread_bp")
                or 0.0
            )
            self._ofi_streak = 0
            self._wobi_streak = 0
            self._book_streak = 0
            self.flow_strength.reset(
                side="buy",
                entry_ts=now,
                target_mfe_bp=float(self.TARGET_MIN_PROFIT_BP),
            )
            self._ae_track_entry(px, now, snap, m)
            self.apply_wobi3_entry_state("buy", px, now, depth_asks, mid_price=float(getattr(snap, "mid_price", px) or px))
            return
            
        if side == "sell" and self.wobi3.position_side:
            entry = float(self.wobi3.entry_price or px)
            pnl_bp = (px / entry - 1.0) * 1e4 if entry > 0 else 0.0
            pnl_j = self.wobi3._yen(pnl_bp)
            hold_sec = now - self.entry_ts if self.entry_ts > 0 else 0.0
            tag = self.pending_exit_tag or "TF_EXIT"
            reason = self.pending_exit_reason or src
            self.pending_exit_tag = ""
            self.pending_exit_reason = ""
            _log(f"[TF1A_WOBI_HONEST] TRADE CLOSED: PnL={pnl_bp:+.2f}bp ({pnl_j:+.1f}円) Hold={hold_sec:.1f}s Reason={reason}")
            self._execute_exit_wobi3(px, pnl_bp, pnl_j, reason, tag, now)

    def _execute_exit_wobi3(
        self, fill_price: float, pnl_bp: float, pnl_jpy: float, reason: str, exit_tag: str, now: float
    ) -> None:
        """Parent ledger + Adverse path fields (MFE/MAE/AE). Entry rules unchanged."""
        path = self._ae_close(fill_price, pnl_bp, f"{exit_tag} ({reason})")
        self.apply_wobi3_exit_state(fill_price, pnl_jpy, exit_tag)
        row = {
            "ts": now,
            "lane": self.lane_name,
            "side": "buy",
            "exit_side": exit_tag.lower(),
            "fill_price": fill_price,
            "pnl_bp": round(pnl_bp, 2),
            "pnl_jpy": round(pnl_jpy, 1),
            "is_win": (pnl_bp > 0),
            "exit_reason": f"{exit_tag} ({reason})",
            "hold_s": round(now - self.entry_ts, 1) if self.entry_ts else None,
            "entry_spread_bp": round(float(self.entry_spread_bp or 0.0), 3),
            **path,
        }
        _append(self.ledger_path, row)
        self.hist_wobi3.append(row)
        _append(self.audit_path, {"event": "EXIT", "ts": _iso(), "data": row})
        _log(
            f"[TF1A_WOBI_HONEST] EXIT [{exit_tag}] pnl={pnl_bp:+.2f}bp "
            f"mfe={path.get('mfe_bp')} mae={path.get('mae_bp')} "
            f"hit_order={path.get('hit_order')} ({reason})"
        )

    def _eval_tf_exit(self, current_mid: float, m: Dict[str, Any], now: float) -> tuple[bool, str, float, str]:
        """トレンドフォロー向けダイナミック・エグジット
        
        マイクロノイズ(WOBI/OFI一時ブレ)では切らず、目標達成・トレイリング・構造崩壊でエグジットする。
        """
        entry = float(self.wobi3.entry_price or 0.0)
        if entry <= 0 or current_mid <= 0:
            return False, "HOLD", 0.0, ""

        unrealized_bp = (current_mid / entry - 1.0) * 1e4
        hold_time = now - self.entry_ts if self.entry_ts > 0 else 0.0
        if unrealized_bp > self.peak_pnl_bp:
            self.peak_pnl_bp = unrealized_bp

        # 1. ハードストップロス (-4.0bp)
        if unrealized_bp <= self.HARD_STOP_BP:
            return True, "HARD_STOP", current_mid, f"stop_loss ({unrealized_bp:+.2f}bp <= {self.HARD_STOP_BP}bp)"

        # 2. +5bp 突破後は +3bp を残す。+0.5 まで戻すと目標を吐き出す。
        if self.peak_pnl_bp >= self.TARGET_MIN_PROFIT_BP:
            self.breakeven_locked = True
            if unrealized_bp <= self.PROFIT_LOCK_BP:
                return True, "BREAKEVEN_LOCK", current_mid, (
                    f"lock_profit (+5bp hit, locked at {unrealized_bp:+.2f}bp "
                    f"floor={self.PROFIT_LOCK_BP:+.1f}bp)"
                )

        # 3. +8bp 突破後の動的トレイリングストップ (Trailing Stop)
        if self.peak_pnl_bp >= self.TRAILING_ACTIVATION_BP:
            trailing_floor = self.peak_pnl_bp - self.TRAILING_STEP_BP
            if unrealized_bp <= trailing_floor:
                return True, "TRAILING_STOP", current_mid, f"trailing_exit (peak={self.peak_pnl_bp:+.2f}bp cur={unrealized_bp:+.2f}bp <= {trailing_floor:+.2f}bp)"

        # 4. 上位トレンド構造崩壊判定 (長期反転)
        ret15_bp, ret60_bp = self._update_trend_context(current_mid, now)
        wobi = float(m.get("wobi") or 0.0)
        ofi = float(m.get("recent_ofi") or 0.0)
        
        # 60秒モメンタムが大幅逆行かつ板も強く売り転換した場合のみトレンド崩壊とみなす
        if ret60_bp <= -3.0 and wobi <= -0.40 and ofi <= -0.15:
            return True, "TREND_COLLAPSE", current_mid, f"trend_collapsed (ret60={ret60_bp:+.1f}bp wobi={wobi:+.2f})"

        return False, "HOLD", 0.0, f"holding_trend (unrealized={unrealized_bp:+.2f}bp peak={self.peak_pnl_bp:+.2f}bp hold={hold_time:.0f}s)"

    def _maybe_fill_board(
        self,
        execs: List[Dict[str, Any]],
        bid: float,
        ask: float,
        snap: Any,
        now: float,
        m: Dict[str, Any],
        depth_bids: List,
        depth_asks: List,
    ) -> None:
        if not self.quote_side or self.quote_px <= 0:
            return
        hit = public_maker_fill(
            side=self.quote_side.upper(),
            price=self.quote_px,
            acceptance_id=self.working_id,
            public_execs=execs,
            best_bid=bid,
            best_ask=ask,
        )
        if not hit:
            return
        src, _sz = hit
        self._on_honest_fill(self.quote_side, self.quote_px, src, snap, now, m, depth_bids, depth_asks)

    def _post_bid(
        self,
        depth_bids: List,
        depth_asks: List,
        mid: float,
        why: str,
    ) -> None:
        pref, desc = calculate_optimal_order_price(
            depth_bids, depth_asks, "buy", 5, 0.05, 1.0, mid
        )
        if pref <= 0:
            return
        self._post_quote("buy", pref, f"{why} [{desc}]")

    def _tick_pair(self, snap: Any, now: float) -> None:
        try:
            self._tick_pair_inner(snap, now)
        except Exception as exc:
            _log(f"[TF1A_WOBI_HONEST] tick error: {exc}")
        if now - self._last_hb >= 60.0:
            self._last_hb = now
            pos = "LONG" if self.wobi3.position_side else "FLAT"
            q = self.quote_side or "FLAT"
            px = f"{self.quote_px:,.0f}" if self.quote_px else "-"
            _log(
                f"[TF1A_WOBI_HONEST] hb pos={pos} quote={q} @{px} "
                f"posts={self.honest_posts} fills={self.honest_fills}"
            )

    def _extract_metrics(self, snap: Any, now: float) -> Dict[str, Any]:
        """Parent metrics + live AE into AdverseScore while a TF1A trade is open."""
        m = super()._extract_metrics(snap, now)
        rec = self._active_ae_record()
        if rec is None:
            return m
        try:
            ae1 = rec.excursions.get("ae_1s")
            ae3 = rec.excursions.get("ae_3s")
            adv = self.adverse_engine.calculate_adverse_score(
                ae_1s=ae1,
                ae_3s=ae3,
                mae_bp=float(rec.mae_bp),
                toxic_score=max(0.0, float(m.get("adverse_score") or 0.0)),
                capture_rate_pct=60.0,
                latency_ms=15.0,
                inventory_btc=0.001,
                holding_time_sec=max(0.0, now - float(rec.entry_time)),
                micro_dev=float(m.get("micro_dev_bp") or 0.0) * 1e-4,
            )
            m["adverse_score"] = round(float(adv.get("adverse_score", m.get("adverse_score") or 0.0)), 2)
            m["ae_1s"] = ae1
            m["ae_3s"] = ae3
            m["mae_bp"] = round(float(rec.mae_bp), 3)
            m["mfe_bp"] = round(float(rec.mfe_bp), 3)
        except Exception:
            pass
        return m

    def _tick_pair_inner(self, snap: Any, now: float) -> None:
        m = self._extract_metrics(snap, now)
        mid = float(getattr(snap, "mid_price", 0.0) or m.get("mid_price") or 0.0)
        bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        depth_bids, depth_asks = self._read_latest_depth()
        execs = self._poll_execs()

        # Adverse path update (existing agent instrument).
        self._ae_on_tick(mid, now, bid, ask)
        
        # 1. 約定判定
        self._maybe_fill_board(execs, bid, ask, snap, now, m, depth_bids, depth_asks)

        # 2. 保有中ポジションの管理 (エグジット判定)
        if self.wobi3.position_side:
            # Observation: same-direction taker + board agree → MFE research.
            entry = float(self.wobi3.entry_price or 0.0)
            unreal = ((mid / entry) - 1.0) * 1e4 if entry > 0 and mid > 0 else None
            try:
                self.flow_strength.update(
                    now=now,
                    execs=execs,
                    wobi=float(m.get("wobi") or 0.0),
                    ofi=float(m.get("recent_ofi") or 0.0),
                    unrealized_bp=unreal,
                )
            except Exception as exc:
                _log(f"[TF1A_WOBI_HONEST] flow_strength update err: {exc}")
            should_exit, exit_tag, exit_px, exit_reason = self._eval_tf_exit(mid, m, now)
            if should_exit:
                if exit_tag == "HARD_STOP" and bid > 0:
                    px, desc = float(bid), "HIT_BID"
                else:
                    px, desc = calculate_optimal_order_price(
                        depth_bids, depth_asks, "sell", 5, 0.05, 1.0, mid
                    )
                self.pending_exit_tag = exit_tag
                self.pending_exit_reason = f"{exit_reason} [{desc}]"
                self._post_quote("sell", px, f"EXIT {self.pending_exit_tag} {self.pending_exit_reason}")
                if exit_tag == "HARD_STOP":
                    self._maybe_fill_board(
                        execs, bid, ask, snap, now, m, depth_bids, depth_asks
                    )
            elif self.quote_side == "sell":
                self._cancel_quote("cancel_exit_hold")
            elif self.quote_side == "buy":
                self._cancel_quote("cancel_buy_now_long")
            return

        if self.quote_side == "sell":
            self._cancel_quote("flat_drop_exit")

        # 3. 新規エントリー判定 — ENTER v3 (base + micro + agent; fresh5/spread observe)
        spread_bp = float(m.get("spread_bp") or 0.0)
        spread_wide = spread_bp > SPREAD_MAX_BP  # observe only unless TF1A_USE_SPREAD_GATE
        spread_block = bool(spread_wide and TF1A_USE_SPREAD_GATE)
        ret15_bp, ret60_bp = self._update_trend_context(mid, now)
        wobi = float(m.get("wobi") or 0.0)
        ofi = float(m.get("recent_ofi") or 0.0)
        depletion = float(m.get("ask_depletion_vel") or 0.0)
        micro_dev_bp = float(m.get("micro_dev_bp") or 0.0)
        d_wobi_dt = float(m.get("d_wobi_dt") or 0.0)
        z_score = float(m.get("z_score") or 0.0)
        self._update_flow_streaks(wobi, ofi)

        trend_up = (ret15_bp >= 0.5 and ret60_bp >= 0.0) or (ret60_bp >= 1.5)
        book_support = (wobi >= OBS_WOBI_FLOOR and ofi > OBS_OFI_FLOOR)
        breakout = (depletion >= 0.25 and ofi > OBS_OFI_FLOOR)
        agent = self._read_agent_trend(mid, now)
        enter_ok, eflags = eval_tf1a_enter_v2(
            trend_up=trend_up,
            book_support=book_support,
            breakout=breakout,
            ret60_bp=ret60_bp,
            micro_dev_bp=micro_dev_bp,
            d_wobi_dt=d_wobi_dt,
            z_score=z_score,
            agent_direction=str(agent.get("direction") or "neutral"),
            agent_strength=float(agent.get("strength") or 0.0),
            agent_regime=str(agent.get("regime") or "warmup"),
        )
        base_ok = bool(eflags["base"])
        # Rejected ab_v1 — log only, never posts.
        ab_v1 = bool(
            wobi >= OBS_WOBI_FLOOR
            and ofi > OBS_OFI_FLOOR
            and self._ofi_streak >= ENTER_STREAK_MIN
            and self._wobi_streak >= ENTER_STREAK_MIN
            and ret60_bp < ENTER_RET60_MAX_BP
        )
        old_ok = enter_ok

        if spread_block:
            if self.quote_side == "buy":
                self._cancel_quote(f"SPREAD_TOO_WIDE ({spread_bp:.1f}bp)")
        elif enter_ok:
            why = (
                f"TF_TREND_V3_ENTRY (ret60={ret60_bp:+.1f} fresh5={eflags['fresh5']} "
                f"spread={spread_bp:.2f} wide_obs={spread_wide} "
                f"wobi={wobi:+.2f} ofi={ofi:+.2f} "
                f"micro={micro_dev_bp:+.2f} z={z_score:+.2f} dw={d_wobi_dt:+.2f} "
                f"agent={agent.get('direction')}/{agent.get('regime')}"
                f"@{agent.get('strength')} src={agent.get('source')})"
            )
            self._post_bid(depth_bids, depth_asks, mid, why)
        else:
            if self.quote_side == "buy":
                if (
                    ret15_bp < -0.5
                    or wobi < -0.20
                    or str(agent.get("direction")) == "down"
                    or not eflags["micro_ok"]
                ):
                    self._cancel_quote("trend_v3_vanished")

        working_buy = self.quote_side == "buy"
        posted = bool(enter_ok and not spread_block and working_buy)
        self._last_flat_feat = {
            "spread_bp": spread_bp,
            "spread_wide_obs": spread_wide,
            "ret15_bp": ret15_bp,
            "ret60_bp": ret60_bp,
            "wobi": wobi,
            "ofi": ofi,
            "depletion": depletion,
            "micro_dev_bp": micro_dev_bp,
            "d_wobi_dt": d_wobi_dt,
            "z_score": z_score,
            "ofi_streak": int(self._ofi_streak),
            "wobi_streak": int(self._wobi_streak),
            "book_streak": int(self._book_streak),
            "old_ok": old_ok,
            "cand_ab_v1": ab_v1,
            "cand_legacy_trend_up": base_ok,
            "cand_fresh2": bool(ret60_bp < 2.0),
            "cand_fresh5": bool(ret60_bp < ENTER_RET60_MAX_BP),
            "cand_persist3": bool(
                self._ofi_streak >= ENTER_STREAK_MIN
                and self._wobi_streak >= ENTER_STREAK_MIN
                and wobi >= 0.0
            ),
            "enter_v2_base": eflags["base"],
            "enter_v2_fresh5": eflags["fresh5"],
            "enter_v2_micro_ok": eflags["micro_ok"],
            "enter_v2_agent_ok": eflags["agent_ok"],
            "agent_direction": agent.get("direction"),
            "agent_strength": agent.get("strength"),
            "agent_regime": agent.get("regime"),
            "agent_source": agent.get("source"),
            "adverse_score": m.get("adverse_score"),
            "vacuum_score": m.get("vacuum_score"),
            "regime": self._regime_tag(spread_bp, ret60_bp),
        }
        self._log_gate_tick(
            now=now,
            mid=mid,
            spread_bp=spread_bp,
            ret15_bp=ret15_bp,
            ret60_bp=ret60_bp,
            wobi=wobi,
            ofi=ofi,
            depletion=depletion,
            trend_up=trend_up,
            book_support=book_support,
            breakout=breakout,
            old_ok=old_ok,
            posted=posted,
            working_buy=working_buy,
            spread_block=spread_block,
        )

    def metrics(self) -> Dict[str, Any]:
        m = super().metrics()
        m["model"] = "TF1A_WOBI_HONEST"
        m["fill"] = "BOARD_IMITATION"
        tf_stats = m.get("TF1A_WOBI_HONEST") or m.get("TF2B_WOBI5") or {}
        m["TF1A_WOBI_HONEST"] = tf_stats
        m["honest_fill"] = {
            "posts": self.honest_posts,
            "cancels": self.honest_cancels,
            "fills": self.honest_fills,
            "board_fills": self.board_fills,
        }
        m["stack"] = (
            "TF1A_WOBI_HONEST ENTER_v3 (base+micro+agent; fresh5/spread observe-only) + "
            "Target>=5bp + ProfitLock+3 + Trailing + HIT_BID HARD_STOP + "
            "AdverseExcursion path + position_flow_strength→MFE observe + BOARD_IMITATION"
        )
        return m


def main() -> int:
    parser = argparse.ArgumentParser(description="TF1A_WOBI_HONEST DryRun (WIRE=NO · Pure Trend Following · Target >= 5bp)")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    args = parser.parse_args()
    runner = TF1AWobiHonestDryRun(
        interval_sec=args.interval,
        report_interval_sec=args.report_interval,
        market_source=args.source,
    )

    def _sig(signum, _frame):
        _log(f"signal {signum}")
        runner.stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
