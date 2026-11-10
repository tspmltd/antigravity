#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared AdverseExcursion path kit for HONEST paper lanes (WIRE=NO).

Layer separation (Phase 1):
  Observe — mae/mfe/ae_*/hit_order on ledger (always)
  Entry Veto — refuse toxic flat board before ENTER
  Fast Cut (path_toxic) — profile-gated; SCALP bypasses completely
  Hard Stop — fatal line (-4bp) for all profiles; recommend HIT_BID_FORCE
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional, Union

from antigravity.quant_pipeline.adverse_excursion import AdverseExcursionTracker

# Inventory-MM / legacy fast-cut constants (SCALP must not consult these).
ADVERSE_FAST_MAE_BP = -1.5
ADVERSE_AE1S_BP = -1.0
ENTRY_ADVERSE_SCORE_MAX = 55.0
HARD_STOP_BP = -4.0
# SCALP Phase1b: entry noise shield (time-based HS arming).
HS_SHIELD_DURATION_SEC = 5.0
HS_SHIELD_BP = -8.0


class StrategyProfile(str, Enum):
    SCALP = "scalp"
    INVENTORY_MM = "inventory_mm"
    TREND = "trend"


def _as_profile(profile: Optional[Union[StrategyProfile, str]]) -> StrategyProfile:
    if profile is None:
        return StrategyProfile.INVENTORY_MM
    if isinstance(profile, StrategyProfile):
        return profile
    try:
        return StrategyProfile(str(profile).strip().lower())
    except ValueError:
        return StrategyProfile.INVENTORY_MM


class HonestAdverseKit:
    def __init__(
        self,
        *,
        lane: str,
        save_dir: str,
        hard_stop_bp: float = HARD_STOP_BP,
        target_mfe_bp: float = 5.0,
        profile: Optional[Union[StrategyProfile, str]] = None,
        veto_score_threshold: float = ENTRY_ADVERSE_SCORE_MAX,
        fast_cut_mae_bp: float = ADVERSE_FAST_MAE_BP,
        fast_cut_ae1s_bp: float = ADVERSE_AE1S_BP,
        hs_shield_duration_sec: Optional[float] = None,
        hs_shield_bp: Optional[float] = None,
        log_fn=None,
    ) -> None:
        self.lane = lane
        self.profile = _as_profile(profile)
        self.hard_stop_bp = float(hard_stop_bp)
        self.target_mfe_bp = float(target_mfe_bp)
        self.veto_score_threshold = float(veto_score_threshold)
        self.fast_cut_mae_bp = float(fast_cut_mae_bp)
        self.fast_cut_ae1s_bp = float(fast_cut_ae1s_bp)
        # SCALP default: 5s shield at -8bp; other profiles: no shield.
        if hs_shield_duration_sec is None:
            self.hs_shield_duration_sec = (
                float(HS_SHIELD_DURATION_SEC)
                if self.profile == StrategyProfile.SCALP
                else 0.0
            )
        else:
            self.hs_shield_duration_sec = float(hs_shield_duration_sec)
        if hs_shield_bp is None:
            self.hs_shield_bp = (
                float(HS_SHIELD_BP)
                if self.profile == StrategyProfile.SCALP
                else float(hard_stop_bp)
            )
        else:
            self.hs_shield_bp = float(hs_shield_bp)
        self._log = log_fn or (lambda _m: None)
        self.tracker = AdverseExcursionTracker(save_dir=save_dir)
        self.current_trade_id: Optional[str] = None

    def _find(self):
        tid = self.current_trade_id
        if not tid:
            return None
        for r in self.tracker.active_records:
            if r.trade_id == tid:
                return r
        for r in self.tracker.history_records:
            if r.trade_id == tid:
                return r
        return None

    def track_entry(
        self,
        *,
        px: float,
        now: float,
        side: str,
        mid: float,
        spread_bp: float,
        meta: Optional[Dict[str, Any]] = None,
        regime: str = "range_low_vol",
    ) -> None:
        trade_id = f"{self.lane}_{int(now * 1000)}"
        self.current_trade_id = trade_id
        base_meta = {
            "lane": self.lane,
            "wire": "NO",
            "adverse_profile": self.profile.value,
        }
        if meta:
            base_meta.update(meta)
        try:
            self.tracker.track_entry(
                trade_id=trade_id,
                side=side.lower(),
                entry_price=float(px),
                entry_time=now,
                strategy_name=self.lane,
                mid_price=float(mid) if mid > 0 else float(px),
                micro_price=float(mid) if mid > 0 else float(px),
                regime=regime,
                spread_bp=float(spread_bp or 0.0),
                inventory=0.001,
                meta=base_meta,
            )
        except Exception as exc:
            self._log(f"[{self.lane}] ae track_entry err: {exc}")
            self.current_trade_id = None

    def _extend(self, rec: Any, mid: float, now: float, mark_px: float) -> None:
        if mid <= 0 or float(rec.entry_price or 0.0) <= 0:
            return
        elapsed = now - float(rec.entry_time)
        if elapsed < 0:
            return
        eval_px = mark_px if mark_px > 0 else mid
        if str(rec.side).lower() == "buy":
            diff_bp = ((eval_px - float(rec.entry_price)) / float(rec.entry_price)) * 1e4
        else:
            diff_bp = ((float(rec.entry_price) - eval_px) / float(rec.entry_price)) * 1e4
        if diff_bp < float(rec.mae_bp):
            rec.mae_bp = float(diff_bp)
            rec.peak_adverse_time = elapsed
            if float(diff_bp) <= self.hard_stop_bp and getattr(rec, "t_mae4", None) is None:
                rec.t_mae4 = elapsed
        if diff_bp > float(rec.mfe_bp):
            rec.mfe_bp = float(diff_bp)
            if float(diff_bp) >= self.target_mfe_bp and getattr(rec, "t_mfe5", None) is None:
                rec.t_mfe5 = elapsed

    def on_tick(self, *, mid: float, now: float, bid: float, ask: float, side: str = "buy") -> None:
        if not self.current_trade_id or mid <= 0:
            return
        try:
            self.tracker.on_tick(
                current_price=mid,
                timestamp=now,
                best_bid=bid if bid > 0 else None,
                best_ask=ask if ask > 0 else None,
            )
        except Exception as exc:
            self._log(f"[{self.lane}] ae on_tick err: {exc}")
            return
        rec = self._find()
        if rec is None:
            return
        mark = bid if str(side).lower() == "buy" else ask
        self._extend(rec, mid, now, mark)

    def should_veto_entry(self, adverse_score: Optional[float]) -> tuple[bool, str]:
        """Layer 1: refuse ENTER on elevated flat-tick adverse_score.

        NOTE: TF must NOT use this (≥55 failed CF). Use TfAdverseLead vacuum/path
        lead instead (see tf_adverse_lead.py · Phase3).
        """
        if self.profile == StrategyProfile.TREND:
            return False, ""
        if adverse_score is None:
            return False, ""
        try:
            s = float(adverse_score)
        except (TypeError, ValueError):
            return False, ""
        if s >= self.veto_score_threshold:
            return True, (
                f"ADVERSE_ENTRY_VETO (score={s:.1f}>={self.veto_score_threshold})"
            )
        return False, ""

    def is_path_toxic_values(self, mae_bp: float, ae1s_bp: Optional[float]) -> tuple[bool, str]:
        """Layer 2: early path cut. SCALP / TREND always bypass."""
        if self.profile in (StrategyProfile.SCALP, StrategyProfile.TREND):
            return False, ""
        mae = float(mae_bp or 0.0)
        if mae <= self.fast_cut_mae_bp:
            return True, f"adverse_fast_mae ({mae:+.2f}bp <= {self.fast_cut_mae_bp})"
        if ae1s_bp is not None and float(ae1s_bp) <= self.fast_cut_ae1s_bp:
            return True, (
                f"adverse_fast_ae1s ({float(ae1s_bp):+.2f}bp <= {self.fast_cut_ae1s_bp})"
            )
        return False, ""

    def path_toxic(self) -> tuple[bool, str]:
        """True when early adverse path says fill was toxic — cut now."""
        if self.profile in (StrategyProfile.SCALP, StrategyProfile.TREND):
            return False, ""
        rec = self._find()
        if rec is None:
            return False, ""
        mae = float(rec.mae_bp or 0.0)
        ae1 = rec.excursions.get("ae_1s")
        return self.is_path_toxic_values(mae, None if ae1 is None else float(ae1))

    def is_hard_stop(
        self,
        current_pnl_bp: float,
        holding_time_sec: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Layer 3: fatal line — force flatten.

        SCALP Phase1b: time-based HS shield — for the first
        ``hs_shield_duration_sec`` after entry, only extreme drawdown
        (``hs_shield_bp``, default -8) fires; after that, normal
        ``hard_stop_bp`` (-4) applies. Inventory/trend unchanged when
        holding_time_sec is omitted or shield duration is 0.
        """
        pnl = float(current_pnl_bp)
        hold = None if holding_time_sec is None else float(holding_time_sec)
        if (
            hold is not None
            and self.hs_shield_duration_sec > 0
            and hold <= self.hs_shield_duration_sec
        ):
            if pnl <= self.hs_shield_bp:
                return True, (
                    f"hard_stop_shield ({pnl:+.2f}bp <= {self.hs_shield_bp}bp "
                    f"hold={hold:.2f}s<={self.hs_shield_duration_sec:.1f}s)"
                )
            return False, ""
        if pnl <= self.hard_stop_bp:
            why = f"hard_stop ({pnl:+.2f}bp <= {self.hard_stop_bp}bp)"
            if hold is not None and self.hs_shield_duration_sec > 0:
                why += f" hold={hold:.2f}s>shield"
            return True, why
        return False, ""

    def get_recommended_execution(self, *, is_hard_stop_triggered: bool = False) -> str:
        """Layer 4: how to cut when exiting on adverse/hard-stop."""
        if is_hard_stop_triggered:
            return "HIT_BID_FORCE"
        if self.profile == StrategyProfile.INVENTORY_MM:
            return "HIT_TOUCH"
        return "HIT_BID"

    def path_fields(self) -> Dict[str, Any]:
        rec = self._find()
        if rec is None:
            return {"adverse_profile": self.profile.value}
        hit5 = bool(rec.mfe_bp >= self.target_mfe_bp)
        hit_stop = bool(rec.mae_bp <= self.hard_stop_bp)
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
            "adverse_profile": self.profile.value,
            "mfe_bp": round(float(rec.mfe_bp), 3),
            "mae_bp": round(float(rec.mae_bp), 3),
            "ae_1s": rec.excursions.get("ae_1s"),
            "ae_3s": rec.excursions.get("ae_3s"),
            "ae_10s": rec.excursions.get("ae_10s"),
            "ae_30s": rec.excursions.get("ae_30s"),
            "time_to_adverse": rec.time_to_adverse_bucket,
            "fill_quality": rec.fill_quality,
            "regime": rec.regime,
            "hit_order": hit_order,
            "t_mfe5_s": None if t5 is None else round(float(t5), 2),
            "t_mae4_s": None if t4 is None else round(float(t4), 2),
            # Legacy: entry_* == FILL-time (historical alias). Prefer post_*/fill_*.
            "entry_wobi": (rec.meta or {}).get("wobi"),
            "entry_ofi": (rec.meta or {}).get("ofi"),
            "entry_adverse_score": (rec.meta or {}).get("adverse_score"),
            "entry_spread_bp": (rec.meta or {}).get("spread_bp"),
            "post_ofi": (rec.meta or {}).get("post_ofi"),
            "post_wobi": (rec.meta or {}).get("post_wobi"),
            "fill_ofi": (rec.meta or {}).get("fill_ofi"),
            "fill_wobi": (rec.meta or {}).get("fill_wobi"),
            "d_ofi": (rec.meta or {}).get("d_ofi"),
            "d_wobi": (rec.meta or {}).get("d_wobi"),
            "queue_wait_s": (rec.meta or {}).get("queue_wait_s"),
            "shadow_pulled": (rec.meta or {}).get("shadow_pulled"),
            "shadow_pull_ofi": (rec.meta or {}).get("shadow_pull_ofi"),
            "shadow_pull_wait_s": (rec.meta or {}).get("shadow_pull_wait_s"),
        }

    def close(self, *, exit_px: float, pnl_bp: float, reason: str) -> Dict[str, Any]:
        path = self.path_fields()
        tid = self.current_trade_id
        if tid:
            try:
                self.tracker.on_close(
                    trade_id=tid,
                    exit_price=float(exit_px),
                    pnl_bp=float(pnl_bp),
                    exit_reason=reason,
                )
            except Exception as exc:
                self._log(f"[{self.lane}] ae on_close err: {exc}")
        self.current_trade_id = None
        return path

    @staticmethod
    def entry_veto_adverse_score(score: Optional[float]) -> tuple[bool, str]:
        """Backward-compatible static veto (default threshold)."""
        if score is None:
            return False, ""
        try:
            s = float(score)
        except (TypeError, ValueError):
            return False, ""
        if s >= ENTRY_ADVERSE_SCORE_MAX:
            return True, f"ADVERSE_ENTRY_VETO (score={s:.1f}>={ENTRY_ADVERSE_SCORE_MAX})"
        return False, ""
