"""
TF2BP with PEG_v2 Strategy (Model 3: EffectiveReach + Model 1: DynamicRatio)
=============================================================================
TF2BP (CSR-499) の微小モメンタム判定に、
【PEG_v2 指値執行エンジン】を搭載した OBSERVATION 観測専用戦略。

CSR-521-PEGFIX（2026-09-24）:
  悪化主因だった「スプレッド奥刺し → tip評価即含み損 → trail 4bp 即死」を修正。
  PEG (Model 1+3) は維持し、**真 maker 領域**で最大限活用する。

【PEG_v2 の構成要素】:
  1. Model 1 (Dynamic Ratio): 対向板厚で improve 幅を可変（薄い→深く / 厚い→手前）
  2. Model 3 (Effective Reach): テイカー攻撃性で improve を小幅ブースト（クロス禁止）
  3. Maker clamp: 対向 tip の 1tick 手前でクリップ
  4. 出口: trail は MFE 武装後のみ · tip 評価に half-spread バッファ
"""
import time
from typing import Dict, Any, Optional, List


class TF2BP_PEG_v2_Strategy:
    """TF2BP + PEG_v2 (Model 3+1) 観測検証用戦略（maker-safe）。"""

    def __init__(self, parameters: Optional[Dict[str, Any]] = None):
        self.strategy_name = "TF2BP_PEG_v2"
        self.frozen_mode = True
        self.last_user_directive = (
            "OBSERVATION: TF2BP + PEG_v2 (Model 3+1) maker-safe · CSR-521-PEGFIX"
        )
        self.peg_version = "peg_v2_maker_safe_v1"

        self.params = {
            "micro_mom_bp": 2.0,
            "target_bp": 15.0,
            "trail_stop_bp": 4.0,
            "trail_arm_mfe_bp": 4.0,
            "min_hold_before_trail_sec": 8.0,
            "be_arm_bp": 5.0,
            "flow_window": 5,
            "reverse_noise_max": 0.25,
            "order_size_btc": 0.001,
            "max_hold_sec": 1200.0,
            "max_wait_sec": 45.0,
            "peg_improve_min": 0.12,
            "peg_improve_max": 0.48,
            "peg_depth_ref_btc": 0.20,
            "peg_aggr_boost_max": 0.08,
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
        self.be_armed: bool = False
        self.trail_armed: bool = False

        self.total_trades: int = 0
        self.win_trades: int = 0
        self.total_pnl: float = 0.0
        self.total_pnl_bp: float = 0.0
        self.trades_history: List[Dict[str, Any]] = []

    def calculate_peg_v2_price(
        self,
        side: str,
        best_bid: float,
        best_ask: float,
        opp_depth: float = 0.1,
        taker_aggressiveness: float = 0.5,
        cancel_rate: float = 0.0,
        refill_rate: float = 0.0,
    ) -> float:
        """Model 1 + Model 3 · maker-safe improve from own tip."""
        spread = best_ask - best_bid
        tick = 1.0
        if spread <= tick:
            return round(best_bid if side == "buy" else best_ask)

        eff_depth = opp_depth * (1.0 - cancel_rate + 0.5 * refill_rate)
        eff_depth = max(eff_depth, 0.001)

        depth_ref = float(self.params["peg_depth_ref_btc"])
        depth_factor = min(max(eff_depth / depth_ref, 0.0), 1.0)
        improve_min = float(self.params["peg_improve_min"])
        improve_max = float(self.params["peg_improve_max"])
        # thick → improve_min (手前) / thin → improve_max (深く)
        base_improve = improve_max - depth_factor * (improve_max - improve_min)

        aggr_boost = min(
            max(float(taker_aggressiveness) * float(self.params["peg_aggr_boost_max"]), 0.0),
            float(self.params["peg_aggr_boost_max"]),
        )
        improve = min(max(base_improve + aggr_boost, improve_min), improve_max)

        if side == "buy":
            raw = best_bid + spread * improve
            price = min(raw, best_ask - tick)
            price = max(price, best_bid)
        else:
            raw = best_ask - spread * improve
            price = max(raw, best_bid + tick)
            price = min(price, best_ask)
        return round(price)

    @staticmethod
    def maker_fill_hit(
        side: str,
        peg_price: float,
        best_bid: float,
        best_ask: float,
        last_sell: float = 0.0,
        last_buy: float = 0.0,
    ) -> bool:
        """真 maker。クロス自己約定は禁止。"""
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
    ) -> Dict[str, Any]:
        now = time.time()
        self.price_history.append(mid_price)
        if len(self.price_history) > 30:
            self.price_history.pop(0)

        net_taker = taker_vol_ask - taker_vol_bid
        flow_dir = 1.0 if net_taker > 0.05 else (-1.0 if net_taker < -0.05 else 0.0)
        self.flow_history.append(flow_dir)
        if len(self.flow_history) > self.params["flow_window"]:
            self.flow_history.pop(0)

        half_spread_bp = 0.0
        if mid_price > 0 and best_ask > best_bid:
            half_spread_bp = ((best_ask - best_bid) / mid_price) * 5000.0

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
                new_px = self.calculate_peg_v2_price(
                    side=p_side,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    opp_depth=opp,
                    taker_aggressiveness=taker_aggressiveness,
                    cancel_rate=cancel_rate,
                    refill_rate=refill_rate,
                )
                self.pending_order["price"] = new_px
                self.pending_order["last_reprice_at"] = now
                p_price = new_px

            if self.maker_fill_hit(
                p_side, p_price, best_bid, best_ask, last_sell_price, last_buy_price
            ):
                self.position_side = p_side
                self.entry_price = p_price
                self.entry_time = now
                self.peak_price = p_price
                self.peak_mfe_bp = 0.0
                self.be_armed = False
                self.trail_armed = False
                self.pending_order = None
                return {
                    "action": "fill",
                    "side": p_side,
                    "fill_price": p_price,
                    "reason": f"PEG_V2_MAKER_FILL (@¥{p_price:,.0f} · {self.peg_version})",
                }

            if waited >= float(self.params["max_wait_sec"]):
                self.pending_order = None
                return {"action": "cancel_pending", "reason": f"PEG_V2_CANCEL (Wait:{waited:.1f}s)"}

            if len(self.price_history) >= 5 and self.price_history[0] > 0:
                mom_bp = (
                    (self.price_history[-1] - self.price_history[0]) / self.price_history[0]
                ) * 10000.0
                if p_side == "buy" and mom_bp <= -self.params["micro_mom_bp"]:
                    self.pending_order = None
                    return {"action": "cancel_pending", "reason": "PEG_V2_CANCEL (mom reverse)"}
                if p_side == "sell" and mom_bp >= self.params["micro_mom_bp"]:
                    self.pending_order = None
                    return {"action": "cancel_pending", "reason": "PEG_V2_CANCEL (mom reverse)"}

            return {"action": "wait_fill", "reason": f"PEG_V2_WAITING (@¥{p_price:,.0f})"}

        # --- POSITION EXITS ---
        if self.position_side:
            elapsed = now - self.entry_time
            tip = best_bid if self.position_side == "buy" else best_ask
            eval_mid = mid_price if mid_price > 0 else tip
            trail_dd = float(self.params["trail_stop_bp"]) + max(0.0, half_spread_bp)

            if self.position_side == "buy":
                if tip > self.peak_price:
                    self.peak_price = tip
                mfe_bp = ((self.peak_price - self.entry_price) / self.entry_price) * 10000.0
                self.peak_mfe_bp = max(self.peak_mfe_bp, mfe_bp)
                if self.peak_mfe_bp >= self.params["be_arm_bp"]:
                    self.be_armed = True
                if (
                    self.peak_mfe_bp >= float(self.params["trail_arm_mfe_bp"])
                    and elapsed >= float(self.params["min_hold_before_trail_sec"])
                ):
                    self.trail_armed = True

                pnl = (tip - self.entry_price) * self.params["order_size_btc"]
                pnl_bp = (
                    (pnl / (self.params["order_size_btc"] * self.entry_price)) * 10000.0
                    if self.entry_price
                    else 0.0
                )
                mid_pnl_bp = (
                    ((eval_mid - self.entry_price) / self.entry_price) * 10000.0
                    if self.entry_price
                    else 0.0
                )
                peak_mfe_mid = max(self.peak_mfe_bp, mid_pnl_bp)

                if self.be_armed and pnl_bp <= 0.2:
                    return {
                        "action": "exit",
                        "reason": f"BE5_STOP (MFE:{self.peak_mfe_bp:.1f}bp後 建値防衛)",
                        "pnl": max(0.0, pnl),
                        "price": tip,
                    }
                target_price = self.entry_price * (1.0 + self.params["target_bp"] * 0.0001)
                if tip >= target_price:
                    return {
                        "action": "exit",
                        "reason": f"TARGET_15BP_REACHED (+¥{pnl:.1f})",
                        "pnl": pnl,
                        "price": tip,
                    }
                if self.trail_armed and peak_mfe_mid - mid_pnl_bp >= trail_dd:
                    return {
                        "action": "exit",
                        "reason": (
                            f"TRAIL_STOP_HIT (PeakMFE:{self.peak_mfe_bp:.1f}bp, "
                            f"PnL:{pnl_bp:+.1f}bp, buf:{trail_dd:.1f}bp)"
                        ),
                        "pnl": pnl,
                        "price": tip,
                    }
            else:
                if tip < self.peak_price or self.peak_price <= 0:
                    self.peak_price = tip
                mfe_bp = ((self.entry_price - self.peak_price) / self.entry_price) * 10000.0
                self.peak_mfe_bp = max(self.peak_mfe_bp, mfe_bp)
                if self.peak_mfe_bp >= self.params["be_arm_bp"]:
                    self.be_armed = True
                if (
                    self.peak_mfe_bp >= float(self.params["trail_arm_mfe_bp"])
                    and elapsed >= float(self.params["min_hold_before_trail_sec"])
                ):
                    self.trail_armed = True

                pnl = (self.entry_price - tip) * self.params["order_size_btc"]
                pnl_bp = (
                    (pnl / (self.params["order_size_btc"] * self.entry_price)) * 10000.0
                    if self.entry_price
                    else 0.0
                )
                mid_pnl_bp = (
                    ((self.entry_price - eval_mid) / self.entry_price) * 10000.0
                    if self.entry_price
                    else 0.0
                )
                peak_mfe_mid = max(self.peak_mfe_bp, mid_pnl_bp)

                if self.be_armed and pnl_bp <= 0.2:
                    return {
                        "action": "exit",
                        "reason": f"BE5_STOP (MFE:{self.peak_mfe_bp:.1f}bp後 建値防衛)",
                        "pnl": max(0.0, pnl),
                        "price": tip,
                    }
                target_price = self.entry_price * (1.0 - self.params["target_bp"] * 0.0001)
                if tip <= target_price:
                    return {
                        "action": "exit",
                        "reason": f"TARGET_15BP_REACHED (+¥{pnl:.1f})",
                        "pnl": pnl,
                        "price": tip,
                    }
                if self.trail_armed and peak_mfe_mid - mid_pnl_bp >= trail_dd:
                    return {
                        "action": "exit",
                        "reason": (
                            f"TRAIL_STOP_HIT (PeakMFE:{self.peak_mfe_bp:.1f}bp, "
                            f"PnL:{pnl_bp:+.1f}bp, buf:{trail_dd:.1f}bp)"
                        ),
                        "pnl": pnl,
                        "price": tip,
                    }

            if elapsed >= self.params["max_hold_sec"]:
                return {
                    "action": "exit",
                    "reason": f"TIMEOUT ({elapsed:.0f}s経過)",
                    "pnl": pnl,
                    "price": tip,
                }
            return {"action": "hold", "reason": "POSITION_RUNNING", "current_pnl": pnl}

        # --- NEW PEG ARM ---
        if len(self.price_history) < 5 or len(self.flow_history) < 3:
            return {"action": "hold", "reason": "WARMING_UP"}

        start_p = self.price_history[0]
        curr_p = self.price_history[-1]
        if start_p <= 0:
            return {"action": "hold", "reason": "WARMING_UP"}
        mom_bp = ((curr_p - start_p) / start_p) * 10000.0
        flow_sum = sum(self.flow_history)
        noise_ratio = len(
            [f for f in self.flow_history if (f < 0 if mom_bp > 0 else f > 0)]
        ) / len(self.flow_history)

        if (
            mom_bp >= self.params["micro_mom_bp"]
            and flow_sum >= 2.0
            and noise_ratio <= self.params["reverse_noise_max"]
        ):
            peg_px = self.calculate_peg_v2_price(
                side="buy",
                best_bid=best_bid,
                best_ask=best_ask,
                opp_depth=ask_depth_1,
                taker_aggressiveness=taker_aggressiveness,
                cancel_rate=cancel_rate,
                refill_rate=refill_rate,
            )
            self.pending_order = {
                "side": "buy",
                "price": peg_px,
                "created_at": now,
                "last_reprice_at": now,
            }
            return {
                "action": "post_peg",
                "side": "buy",
                "price": peg_px,
                "reason": (
                    f"PEG_V2_BUY_ARM (Mom:{mom_bp:+.1f}bp, PEG_Px:¥{peg_px:,.0f}, "
                    f"{self.peg_version})"
                ),
            }

        if (
            mom_bp <= -self.params["micro_mom_bp"]
            and flow_sum <= -2.0
            and noise_ratio <= self.params["reverse_noise_max"]
        ):
            peg_px = self.calculate_peg_v2_price(
                side="sell",
                best_bid=best_bid,
                best_ask=best_ask,
                opp_depth=bid_depth_1,
                taker_aggressiveness=taker_aggressiveness,
                cancel_rate=cancel_rate,
                refill_rate=refill_rate,
            )
            self.pending_order = {
                "side": "sell",
                "price": peg_px,
                "created_at": now,
                "last_reprice_at": now,
            }
            return {
                "action": "post_peg",
                "side": "sell",
                "price": peg_px,
                "reason": (
                    f"PEG_V2_SELL_ARM (Mom:{mom_bp:+.1f}bp, PEG_Px:¥{peg_px:,.0f}, "
                    f"{self.peg_version})"
                ),
            }

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
                "pnl_jpy": round(pnl, 1),
                "pnl_bp": round(pnl_bp, 2),
                "is_win": (pnl > 0),
                "peg_version": self.peg_version,
            })
            self.position_side = None
            self.entry_price = 0.0
            self.entry_time = 0.0
            self.peak_price = 0.0
            self.peak_mfe_bp = 0.0
            self.be_armed = False
            self.trail_armed = False
        elif side in ("buy", "sell"):
            self.position_side = side
            self.entry_price = fill_price
            self.peak_price = fill_price
            self.peak_mfe_bp = 0.0
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
        }
