"""
TF2BP_PEG_v3 — OBSERVATION 実験レーン（CSR-522）
================================================
判定ロック（2026-09-24）:
  baseline = validated · peg_v2 = rejected_for_promotion / structurally_broken
  病因 = Adverse Fill（Fake Liquidity）+ AgingGuardなし + BE遅延
  Execution Alpha はあるが Signal Alpha を破壊 → 昇格禁止

S級:
  1. AgingGuard — age>=3s かつ unrealized_pnl_bp < 0.5 → 即脱出
  2. BE3 — arm +3.0bp · trigger +0.2bp
A級:
  3. FakeLiquidityFilter — |imb|>0.40 ∧ cancel>0.30 ∧ taker_total==0 → skip
  4. Adaptive DynamicRatio — noise 0.915–0.975 / trend 0.985–0.995（maker clamp）

監査: EXEC_AUDIT JSONL（反事実 hold10/30 含む）
WIRE=NO · ENFORCE=0 · Baseline(CSR-499) 非改変
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

JST = timezone(timedelta(hours=9))
AUDIT_PATH = "/home/azureuser/antigravity/data/mm_research/exec_audit_peg_v3.jsonl"


class TF2BP_PEG_v3_Strategy:
    """TF2BP + PEG_v3 観測検証用戦略。"""

    def __init__(self, parameters: Optional[Dict[str, Any]] = None):
        self.strategy_name = "TF2BP_PEG_v3"
        self.frozen_mode = True
        self.last_user_directive = (
            "OBSERVATION: PEG_v3 = AgingGuard+BE3+FakeLiq+AdaptiveRatio · CSR-522 · ENFORCE=0"
        )
        self.peg_version = "peg_v3_obs_v1"

        self.params = {
            "micro_mom_bp": 2.0,
            "target_bp": 15.0,
            "trail_stop_bp": 4.0,
            "trail_arm_mfe_bp": 4.0,
            "min_hold_before_trail_sec": 8.0,
            # S級 BE3
            "be_arm_bp": 3.0,
            "be_trigger_bp": 0.2,
            # S級 AgingGuard
            "aging_guard_sec": 3.0,
            "aging_guard_min_pnl_bp": 0.5,
            "flow_window": 5,
            "reverse_noise_max": 0.25,
            "order_size_btc": 0.001,
            "max_hold_sec": 1200.0,
            "max_wait_sec": 45.0,
            # Adaptive DynamicRatio（旧比率意味 · maker clamp）
            "ratio_noise_min": 0.915,
            "ratio_noise_max": 0.975,
            "ratio_trend_min": 0.985,
            "ratio_trend_max": 0.995,
            "peg_depth_ref_btc": 0.20,
            "peg_aggr_boost_max": 0.010,
            # FakeLiquidity
            "fake_liq_imbalance": 0.40,
            "fake_liq_cancel_rate": 0.30,
            "trend_imbalance": 0.25,
        }
        if parameters:
            self.params.update(parameters)

        self.price_history: List[float] = []
        self.flow_history: List[float] = []
        self.pending_order: Optional[Dict[str, Any]] = None

        self.position_side: Optional[str] = None
        self.entry_price: float = 0.0
        self.entry_time: float = 0.0
        self.peak_price: float = 0.0
        self.peak_mfe_bp: float = 0.0
        self.peak_mae_bp: float = 0.0
        self.be_armed: bool = False
        self.trail_armed: bool = False
        self._mid_path: List[Tuple[float, float]] = []
        self._entry_meta: Dict[str, Any] = {}
        self._last_ratio: float = 0.0
        self._last_regime: str = "noise"

        self.total_trades: int = 0
        self.win_trades: int = 0
        self.total_pnl: float = 0.0
        self.total_pnl_bp: float = 0.0
        self.trades_history: List[Dict[str, Any]] = []
        self.fake_liq_skips: int = 0
        self.aging_exits: int = 0
        self.be_exits: int = 0

        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)

    # ------------------------------------------------------------------ peg
    def _is_trend_regime(
        self,
        imbalance: float,
        flow_sum: float,
        taker_total: float,
    ) -> bool:
        return (
            abs(imbalance) >= float(self.params["trend_imbalance"])
            and abs(flow_sum) >= 2.0
            and taker_total > 1e-9
        )

    def calculate_peg_v3_price(
        self,
        side: str,
        best_bid: float,
        best_ask: float,
        opp_depth: float = 0.1,
        taker_aggressiveness: float = 0.5,
        cancel_rate: float = 0.0,
        refill_rate: float = 0.0,
        imbalance: float = 0.0,
        flow_sum: float = 0.0,
        taker_total: float = 0.0,
    ) -> Tuple[float, float, str]:
        """Adaptive DynamicRatio · maker-safe clamp. returns (price, ratio, regime)."""
        spread = best_ask - best_bid
        tick = 1.0
        if spread <= tick:
            px = round(best_bid if side == "buy" else best_ask)
            return px, 0.0, "flat_spread"

        eff_depth = opp_depth * (1.0 - cancel_rate + 0.5 * refill_rate)
        eff_depth = max(eff_depth, 0.001)
        depth_ref = float(self.params["peg_depth_ref_btc"])
        depth_factor = min(max(eff_depth / depth_ref, 0.0), 1.0)

        trend = self._is_trend_regime(imbalance, flow_sum, taker_total)
        if trend:
            rmin = float(self.params["ratio_trend_min"])
            rmax = float(self.params["ratio_trend_max"])
            regime = "trend"
        else:
            rmin = float(self.params["ratio_noise_min"])
            rmax = float(self.params["ratio_noise_max"])
            regime = "noise"

        # 薄い→深く(rmax) / 厚い→手前(rmin)
        base_ratio = rmax - depth_factor * (rmax - rmin)
        aggr_boost = min(
            max(float(taker_aggressiveness) * float(self.params["peg_aggr_boost_max"]), 0.0),
            float(self.params["peg_aggr_boost_max"]),
        )
        ratio = min(max(base_ratio + aggr_boost, rmin), rmax)

        if side == "buy":
            raw = best_bid + spread * ratio
            price = min(raw, best_ask - tick)
            price = max(price, best_bid)
        else:
            raw = best_ask - spread * ratio
            price = max(raw, best_bid + tick)
            price = min(price, best_ask)
        return round(price), float(ratio), regime

    @staticmethod
    def maker_fill_hit(
        side: str,
        peg_price: float,
        best_bid: float,
        best_ask: float,
        last_sell: float = 0.0,
        last_buy: float = 0.0,
    ) -> bool:
        if side == "buy":
            if last_sell > 0 and last_sell <= peg_price:
                return True
            if best_ask > 0 and best_ask <= peg_price:
                return True
            return False
        if last_buy > 0 and last_buy >= peg_price:
            return True
        if best_bid > 0 and best_bid >= peg_price:
            return True
        return False

    def fake_liquidity_block(
        self,
        side: str,
        imbalance: float,
        cancel_rate: float,
        taker_total: float,
    ) -> bool:
        """FakeLiquidityFilter — 厚い板 + 高cancel + taker不在。"""
        if cancel_rate < float(self.params["fake_liq_cancel_rate"]):
            return False
        if taker_total > 1e-9:
            return False
        thr = float(self.params["fake_liq_imbalance"])
        if side == "buy" and imbalance > thr:
            return True
        if side == "sell" and imbalance < -thr:
            return True
        return False

    # ------------------------------------------------------------------ audit
    def _audit(self, payload: Dict[str, Any]) -> None:
        try:
            payload = {
                "event_type": "EXEC_AUDIT",
                "strategy_id": "TF2BP_PEG_v3",
                "peg_version": self.peg_version,
                "timestamp": datetime.now(JST).isoformat(timespec="milliseconds"),
                **payload,
            }
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _counterfactual(self, side: str, entry: float, size: float) -> Dict[str, float]:
        out = {"pnl_if_hold_10s": None, "pnl_if_hold_30s": None}
        if entry <= 0 or not self._mid_path:
            return out
        t0 = self.entry_time
        for hold, key in ((10.0, "pnl_if_hold_10s"), (30.0, "pnl_if_hold_30s")):
            target = t0 + hold
            mid = None
            for ts, m in self._mid_path:
                if ts >= target:
                    mid = m
                    break
            if mid is None:
                mid = self._mid_path[-1][1]
            if side == "buy":
                pnl = (mid - entry) * size
            else:
                pnl = (entry - mid) * size
            notional = size * entry
            out[key] = round((pnl / notional) * 10000.0, 2) if notional else 0.0
        return out

    # ------------------------------------------------------------------ tick
    def on_tick(
        self,
        mid_price: float,
        best_bid: float,
        best_ask: float,
        taker_vol_bid: float = 0.0,
        taker_vol_ask: float = 0.0,
        ask_depth_1: float = 0.1,
        bid_depth_1: float = 0.1,
        taker_aggressiveness: float = 0.5,
        cancel_rate: float = 0.0,
        refill_rate: float = 0.0,
        adverse_score: float = 0.0,
        cancel_recommendation: bool = False,
        avoidance_on: bool = False,
        last_sell_price: float = 0.0,
        last_buy_price: float = 0.0,
        imbalance: float = 0.0,
    ) -> Dict[str, Any]:
        now = time.time()
        self.price_history.append(mid_price)
        if len(self.price_history) > 30:
            self.price_history.pop(0)

        net_taker = taker_vol_ask - taker_vol_bid
        taker_total = float(taker_vol_bid) + float(taker_vol_ask)
        flow_dir = 1.0 if net_taker > 0.05 else (-1.0 if net_taker < -0.05 else 0.0)
        self.flow_history.append(flow_dir)
        if len(self.flow_history) > self.params["flow_window"]:
            self.flow_history.pop(0)
        flow_sum = sum(self.flow_history)

        half_spread_bp = 0.0
        spread_bp = 0.0
        if mid_price > 0 and best_ask > best_bid:
            spread_bp = ((best_ask - best_bid) / mid_price) * 10000.0
            half_spread_bp = spread_bp * 0.5

        # --- PENDING PEG ---
        if self.pending_order:
            p_side = self.pending_order["side"]
            p_price = float(self.pending_order["price"])
            created = float(self.pending_order.get("created_at") or now)
            if "created_at" not in self.pending_order:
                self.pending_order["created_at"] = now
            waited = now - created

            last_re = float(self.pending_order.get("last_reprice_at") or created)
            if now - last_re >= 2.0:
                opp = ask_depth_1 if p_side == "buy" else bid_depth_1
                new_px, ratio, regime = self.calculate_peg_v3_price(
                    side=p_side,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    opp_depth=opp,
                    taker_aggressiveness=taker_aggressiveness,
                    cancel_rate=cancel_rate,
                    refill_rate=refill_rate,
                    imbalance=imbalance,
                    flow_sum=flow_sum,
                    taker_total=taker_total,
                )
                self.pending_order["price"] = new_px
                self.pending_order["last_reprice_at"] = now
                self.pending_order["ratio"] = ratio
                self.pending_order["regime"] = regime
                self._last_ratio = ratio
                self._last_regime = regime
                p_price = new_px

            if self.maker_fill_hit(
                p_side, p_price, best_bid, best_ask, last_sell_price, last_buy_price
            ):
                fill_delay_ms = (now - created) * 1000.0
                self.position_side = p_side
                self.entry_price = p_price
                self.entry_time = now
                self.peak_price = p_price
                self.peak_mfe_bp = 0.0
                self.peak_mae_bp = 0.0
                self.be_armed = False
                self.trail_armed = False
                self._mid_path = [(now, mid_price)]
                self._entry_meta = {
                    "side": p_side,
                    "trigger_imbalance": imbalance,
                    "spread_bp": round(spread_bp, 3),
                    "cancel_rate": cancel_rate,
                    "taker_total": taker_total,
                    "maker_offset_ratio": self.pending_order.get("ratio", self._last_ratio),
                    "regime": self.pending_order.get("regime", self._last_regime),
                    "fill_delay_ms": round(fill_delay_ms, 1),
                }
                self.pending_order = None
                self._audit({
                    "signal": {
                        "side": p_side.upper(),
                        "trigger_imbalance": round(imbalance, 4),
                        "spread_bp": round(spread_bp, 3),
                    },
                    "execution": {
                        "fill_status": "FILLED",
                        "fill_delay_ms": round(fill_delay_ms, 1),
                        "maker_offset_ratio": self._entry_meta["maker_offset_ratio"],
                        "adverse_fill_detected": False,
                        "regime": self._entry_meta["regime"],
                    },
                    "position_lifecycle": None,
                    "counterfactual": None,
                })
                return {
                    "action": "fill",
                    "side": p_side,
                    "fill_price": p_price,
                    "reason": f"PEG_V3_MAKER_FILL (@¥{p_price:,.0f} · {self.peg_version})",
                }

            if waited >= float(self.params["max_wait_sec"]):
                self._audit({
                    "signal": {
                        "side": p_side.upper(),
                        "trigger_imbalance": round(imbalance, 4),
                        "spread_bp": round(spread_bp, 3),
                    },
                    "execution": {
                        "fill_status": "MISSED_TIMEOUT",
                        "fill_delay_ms": round(waited * 1000.0, 1),
                        "maker_offset_ratio": self.pending_order.get("ratio", self._last_ratio),
                        "adverse_fill_detected": False,
                    },
                    "position_lifecycle": None,
                    "counterfactual": None,
                })
                self.pending_order = None
                return {"action": "cancel_pending", "reason": f"PEG_V3_CANCEL (Wait:{waited:.1f}s)"}

            if len(self.price_history) >= 5 and self.price_history[0] > 0:
                mom_bp = (
                    (self.price_history[-1] - self.price_history[0]) / self.price_history[0]
                ) * 10000.0
                if p_side == "buy" and mom_bp <= -self.params["micro_mom_bp"]:
                    self.pending_order = None
                    return {"action": "cancel_pending", "reason": "PEG_V3_CANCEL (mom reverse)"}
                if p_side == "sell" and mom_bp >= self.params["micro_mom_bp"]:
                    self.pending_order = None
                    return {"action": "cancel_pending", "reason": "PEG_V3_CANCEL (mom reverse)"}

            return {"action": "wait_fill", "reason": f"PEG_V3_WAITING (@¥{p_price:,.0f})"}

        # --- POSITION EXITS ---
        if self.position_side:
            self._mid_path.append((now, mid_price))
            if len(self._mid_path) > 200:
                self._mid_path = self._mid_path[-200:]

            elapsed = now - self.entry_time
            tip = best_bid if self.position_side == "buy" else best_ask
            eval_mid = mid_price if mid_price > 0 else tip
            trail_dd = float(self.params["trail_stop_bp"]) + max(0.0, half_spread_bp)
            size = float(self.params["order_size_btc"])
            be_trig = float(self.params["be_trigger_bp"])

            if self.position_side == "buy":
                if tip > self.peak_price:
                    self.peak_price = tip
                mfe_bp = ((self.peak_price - self.entry_price) / self.entry_price) * 10000.0
                mid_pnl_bp = ((eval_mid - self.entry_price) / self.entry_price) * 10000.0
                pnl = (tip - self.entry_price) * size
            else:
                if tip < self.peak_price or self.peak_price <= 0:
                    self.peak_price = tip
                mfe_bp = ((self.entry_price - self.peak_price) / self.entry_price) * 10000.0
                mid_pnl_bp = ((self.entry_price - eval_mid) / self.entry_price) * 10000.0
                pnl = (self.entry_price - tip) * size

            pnl_bp = (
                (pnl / (size * self.entry_price)) * 10000.0 if self.entry_price else 0.0
            )
            self.peak_mfe_bp = max(self.peak_mfe_bp, mfe_bp, mid_pnl_bp)
            self.peak_mae_bp = min(self.peak_mae_bp, mid_pnl_bp, pnl_bp)
            peak_mfe_mid = max(self.peak_mfe_bp, mid_pnl_bp)

            if self.peak_mfe_bp >= float(self.params["be_arm_bp"]):
                self.be_armed = True
            if (
                self.peak_mfe_bp >= float(self.params["trail_arm_mfe_bp"])
                and elapsed >= float(self.params["min_hold_before_trail_sec"])
            ):
                self.trail_armed = True

            def _exit(reason_code: str, reason: str, exit_pnl: float) -> Dict[str, Any]:
                cf = self._counterfactual(self.position_side, self.entry_price, size)
                if reason_code == "AGING_GUARD":
                    self.aging_exits += 1
                if reason_code == "BE_TRIGGERED":
                    self.be_exits += 1
                adverse = self.peak_mae_bp <= -2.0 and self.peak_mfe_bp < 1.0
                self._audit({
                    "signal": {
                        "side": (self.position_side or "").upper(),
                        "trigger_imbalance": self._entry_meta.get("trigger_imbalance"),
                        "spread_bp": self._entry_meta.get("spread_bp"),
                    },
                    "execution": {
                        "fill_status": "FILLED",
                        "fill_delay_ms": self._entry_meta.get("fill_delay_ms"),
                        "maker_offset_ratio": self._entry_meta.get("maker_offset_ratio"),
                        "adverse_fill_detected": adverse,
                        "regime": self._entry_meta.get("regime"),
                    },
                    "position_lifecycle": {
                        "mfe_bp": round(self.peak_mfe_bp, 2),
                        "mae_bp": round(self.peak_mae_bp, 2),
                        "hold_duration_sec": round(elapsed, 2),
                        "be_armed": self.be_armed,
                        "aging_exit_triggered": reason_code == "AGING_GUARD",
                        "exit_reason": reason_code,
                        "realized_pnl_bp": round(
                            (exit_pnl / (size * self.entry_price)) * 10000.0, 2
                        ) if self.entry_price else 0.0,
                    },
                    "counterfactual": cf,
                })
                return {
                    "action": "exit",
                    "reason": reason,
                    "pnl": exit_pnl,
                    "price": tip,
                    "exit_reason_code": reason_code,
                }

            # S級 AgingGuard
            if (
                elapsed >= float(self.params["aging_guard_sec"])
                and pnl_bp < float(self.params["aging_guard_min_pnl_bp"])
            ):
                return _exit(
                    "AGING_GUARD",
                    f"AGING_GUARD (age:{elapsed:.1f}s pnl:{pnl_bp:+.2f}bp)",
                    pnl,
                )

            # S級 BE3
            if self.be_armed and pnl_bp <= be_trig:
                return _exit(
                    "BE_TRIGGERED",
                    f"BE3_STOP (MFE:{self.peak_mfe_bp:.1f}bp後 建値防衛≤{be_trig}bp)",
                    max(0.0, pnl),
                )

            # TP
            if self.position_side == "buy":
                target_price = self.entry_price * (1.0 + self.params["target_bp"] * 0.0001)
                hit_tp = tip >= target_price
            else:
                target_price = self.entry_price * (1.0 - self.params["target_bp"] * 0.0001)
                hit_tp = tip <= target_price
            if hit_tp:
                return _exit("TP", f"TARGET_15BP_REACHED (+¥{pnl:.1f})", pnl)

            if self.trail_armed and peak_mfe_mid - mid_pnl_bp >= trail_dd:
                return _exit(
                    "TRAIL",
                    (
                        f"TRAIL_STOP_HIT (PeakMFE:{self.peak_mfe_bp:.1f}bp, "
                        f"PnL:{pnl_bp:+.1f}bp, buf:{trail_dd:.1f}bp)"
                    ),
                    pnl,
                )

            if elapsed >= self.params["max_hold_sec"]:
                return _exit("TIMEOUT", f"TIMEOUT ({elapsed:.0f}s経過)", pnl)

            return {"action": "hold", "reason": "POSITION_RUNNING", "current_pnl": pnl}

        # --- NEW PEG ARM ---
        if len(self.price_history) < 5 or len(self.flow_history) < 3:
            return {"action": "hold", "reason": "WARMING_UP"}

        start_p = self.price_history[0]
        curr_p = self.price_history[-1]
        if start_p <= 0:
            return {"action": "hold", "reason": "WARMING_UP"}
        mom_bp = ((curr_p - start_p) / start_p) * 10000.0
        noise_ratio = len(
            [f for f in self.flow_history if (f < 0 if mom_bp > 0 else f > 0)]
        ) / len(self.flow_history)

        def _arm(side: str) -> Dict[str, Any]:
            if self.fake_liquidity_block(side, imbalance, cancel_rate, taker_total):
                self.fake_liq_skips += 1
                self._audit({
                    "signal": {
                        "side": side.upper(),
                        "trigger_imbalance": round(imbalance, 4),
                        "spread_bp": round(spread_bp, 3),
                    },
                    "execution": {
                        "fill_status": "SKIP_FAKE_LIQUIDITY",
                        "fill_delay_ms": None,
                        "maker_offset_ratio": None,
                        "adverse_fill_detected": False,
                        "cancel_rate": cancel_rate,
                        "taker_total": taker_total,
                    },
                    "position_lifecycle": None,
                    "counterfactual": None,
                })
                return {
                    "action": "hold",
                    "reason": (
                        f"PEG_V3_SKIP_FAKE_LIQ "
                        f"(imb:{imbalance:+.2f} cxl:{cancel_rate:.2f} taker:{taker_total:.3f})"
                    ),
                }
            opp = ask_depth_1 if side == "buy" else bid_depth_1
            peg_px, ratio, regime = self.calculate_peg_v3_price(
                side=side,
                best_bid=best_bid,
                best_ask=best_ask,
                opp_depth=opp,
                taker_aggressiveness=taker_aggressiveness,
                cancel_rate=cancel_rate,
                refill_rate=refill_rate,
                imbalance=imbalance,
                flow_sum=flow_sum,
                taker_total=taker_total,
            )
            self._last_ratio = ratio
            self._last_regime = regime
            self.pending_order = {
                "side": side,
                "price": peg_px,
                "created_at": now,
                "last_reprice_at": now,
                "ratio": ratio,
                "regime": regime,
            }
            return {
                "action": "post_peg",
                "side": side,
                "price": peg_px,
                "reason": (
                    f"PEG_V3_{side.upper()}_ARM (Mom:{mom_bp:+.1f}bp, "
                    f"ratio:{ratio:.3f}/{regime}, PEG:¥{peg_px:,.0f})"
                ),
            }

        if (
            mom_bp >= self.params["micro_mom_bp"]
            and flow_sum >= 2.0
            and noise_ratio <= self.params["reverse_noise_max"]
        ):
            return _arm("buy")

        if (
            mom_bp <= -self.params["micro_mom_bp"]
            and flow_sum <= -2.0
            and noise_ratio <= self.params["reverse_noise_max"]
        ):
            return _arm("sell")

        return {"action": "hold", "reason": "WAIT_MOMENTUM_2BP"}

    def record_trade(self, side: str, fill_price: float, pnl: float, mid_price: float = 0.0):
        now = time.time()
        eval_price = mid_price if mid_price > 0 else fill_price
        order_val_jpy = self.params["order_size_btc"] * eval_price if eval_price > 0 else 12500.0
        pnl_bp = (pnl / order_val_jpy) * 10000.0 if order_val_jpy > 0 else 0.0

        if side in ("close_buy", "close_sell", "exit", "cancel"):
            self.total_trades += 1
            if pnl > 0:
                self.win_trades += 1
            self.total_pnl += pnl
            self.total_pnl_bp += pnl_bp
            hold_sec = round(now - self.entry_time, 3) if self.entry_time > 0 else None
            self.trades_history.append({
                "ts": now,
                "entry_ts": self.entry_time if self.entry_time > 0 else None,
                "hold_sec": hold_sec,
                "side": self.position_side,
                "exit_side": side,
                "fill_price": fill_price,
                "entry_price": self.entry_price if self.entry_price > 0 else None,
                "pnl_jpy": round(pnl, 1),
                "pnl_bp": round(pnl_bp, 2),
                "is_win": (pnl > 0),
                "peg_version": self.peg_version,
                "mfe_bp": round(self.peak_mfe_bp, 2),
                "mae_bp": round(self.peak_mae_bp, 2),
                "be_armed": self.be_armed,
            })
            self.position_side = None
            self.entry_price = 0.0
            self.entry_time = 0.0
            self.peak_price = 0.0
            self.peak_mfe_bp = 0.0
            self.peak_mae_bp = 0.0
            self.be_armed = False
            self.trail_armed = False
            self._mid_path = []
            self._entry_meta = {}
        elif side in ("buy", "sell"):
            self.position_side = side
            self.entry_price = fill_price
            self.peak_price = fill_price
            self.peak_mfe_bp = 0.0
            self.peak_mae_bp = 0.0
            self.be_armed = False
            self.trail_armed = False
            self.entry_time = now

    def get_window_stats(self, hours: float = 1.0) -> Dict[str, Any]:
        now = time.time()
        cutoff = now - (hours * 3600.0)
        recent = [t for t in self.trades_history if t["ts"] >= cutoff]
        total_t = len(recent)
        win_t = sum(1 for t in recent if t["is_win"])
        pnl_jpy = sum(t["pnl_jpy"] for t in recent)
        pnl_bp = sum(t["pnl_bp"] for t in recent)
        wr = (win_t / total_t * 100.0) if total_t > 0 else 0.0
        return {
            "window_hours": hours,
            "total_trades": total_t,
            "win_trades": win_t,
            "loss_trades": total_t - win_t,
            "win_rate_pct": round(wr, 1),
            "pnl_jpy": round(pnl_jpy, 1),
            "pnl_bp": round(pnl_bp, 2),
            "peg_version": self.peg_version,
            "fake_liq_skips": self.fake_liq_skips,
            "aging_exits": self.aging_exits,
            "be_exits": self.be_exits,
        }
