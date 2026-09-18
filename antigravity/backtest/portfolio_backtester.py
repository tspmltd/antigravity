"""
PortfolioBacktester: GAPCORE Multi-Strategy Portfolio Backtest Simulator
========================================================================
Rigorous event-driven simulator supporting:
1. Shadow Accounting (Independent PositionManager per strategy)
2. Target Aggregation & Internal Netting (Zero-cost offsetting)
3. Priority-based Order Rate Limiting (Simulating 429 skips)
4. Regime-Adaptive Weighting (Trend vs Range vs High-Vol)
5. Hierarchical Daily Loss Caps (Fail-Closed portfolio / strategy bounds)
"""

import time
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
import numpy as np

from antigravity.execution.position_manager import PositionManager
from antigravity.risk_guard.order_rate_guard import OrderRateGuard
from antigravity.risk_guard.portfolio_pnl_guard import PortfolioDailyPnLGuard
from antigravity.strategies.ema_trend import EmaTrendTickStrategy
from antigravity.strategies.mean_reversion import MeanReversionStrategy
from antigravity.strategies.orderbook_imbalance import OrderBookImbalanceStrategy
from antigravity.strategies.grid_mm import GridMmStrategy
from .regime_detector import RegimeDetector


class PortfolioBacktester:
    def __init__(
        self,
        enable_internal_netting: bool = True,
        enable_rate_limit: bool = True,
        enable_regime_switch: bool = True,
        enable_daily_cap: bool = True,
        order_size: float = 0.001,
        initial_capital_jpy: float = 10000.0,
        portfolio_daily_limit_jpy: float = 3000.0,
        strategy_daily_limits: Optional[Dict[str, float]] = None,
        max_orders_per_min: int = 30,
        taker_fee_ratio: float = 0.0,  # bitFlyer Lightning FX maker/taker fee = 0.0%
        base_spread_bp: float = 2.5,  # bitFlyer FX 実勢スプレッド (2.5bp ≒ 約3,000円幅)
        aging_timeout_sec: float = 300.0,
    ):
        self.enable_internal_netting = enable_internal_netting
        self.enable_rate_limit = enable_rate_limit
        self.enable_regime_switch = enable_regime_switch
        self.enable_daily_cap = enable_daily_cap

        self.order_size = order_size
        self.initial_capital_jpy = initial_capital_jpy
        self.portfolio_daily_limit_jpy = portfolio_daily_limit_jpy
        self.strategy_daily_limits = strategy_daily_limits or {
            "EmaTrend": 1500.0,
            "MeanReversion": 1000.0,
            "OrderBookImbalance": 500.0,
            "GridMM": 1000.0,
        }
        self.max_orders_per_min = max_orders_per_min
        self.taker_fee_ratio = taker_fee_ratio
        self.base_spread_bp = base_spread_bp
        self.aging_timeout_sec = aging_timeout_sec

        self.regime_detector = RegimeDetector()

    def run(
        self,
        df: pd.DataFrame,
        mode: str = "portfolio",  # "baseline_ema", "baseline_gridmm", "portfolio_no_netting", "portfolio"
    ) -> Dict[str, Any]:
        """
        Executes backtest over historical price dataframe.
        """
        # 1. Initialize Strategies
        strategies: Dict[str, Any] = {}
        if mode == "baseline_ema":
            strategies["EmaTrend"] = EmaTrendTickStrategy(order_size=self.order_size)
        elif mode == "baseline_gridmm":
            strategies["GridMM"] = GridMmStrategy(order_size=self.order_size)
        else:
            strategies["EmaTrend"] = EmaTrendTickStrategy(order_size=self.order_size)
            strategies["MeanReversion"] = MeanReversionStrategy(order_size=self.order_size)
            strategies["OrderBookImbalance"] = OrderBookImbalanceStrategy(order_size=self.order_size)
            strategies["GridMM"] = GridMmStrategy(order_size=self.order_size)

        # 2. Initialize Shadow Accounting (Independent PositionManagers)
        pos_managers: Dict[str, PositionManager] = {
            strat_id: PositionManager(strategy_id=strat_id, lot_size=self.order_size)
            for strat_id in strategies.keys()
        }

        # 3. Exchange-level Physical State
        exchange_actual_qty: float = 0.0
        exchange_avg_price: float = 0.0
        exchange_realized_pnl: float = 0.0
        exchange_total_fees_and_slippage: float = 0.0

        # 4. Rate Guard & Daily Guard
        rate_guard = OrderRateGuard(max_orders_per_min=self.max_orders_per_min)
        daily_cap_records: Dict[str, float] = {strat_id: 0.0 for strat_id in strategies.keys()}
        portfolio_daily_realized: float = 0.0
        current_jst_day: str = ""

        # Performance Tracking
        timestamps: List[Any] = []
        equity_series: List[float] = []
        physical_orders_count: int = 0
        netting_events_count: int = 0
        spread_savings_jpy: float = 0.0
        rate_limit_skips_count: int = 0
        regime_counts: Dict[str, int] = {"TREND": 0, "RANGE": 0, "HIGH_VOL": 0, "NORMAL": 0}

        # Iteration through DataFrame bars
        # For high-fidelity microstructure, we expand each 1m bar into 4 sub-tick phases:
        # Open -> High (or Low) -> Low (or High) -> Close
        for idx, row in df.iterrows():
            bar_ts = row.get("timestamp")
            if isinstance(bar_ts, str):
                try:
                    ts_dt = datetime.strptime(bar_ts[:19], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    ts_dt = datetime.now()
            elif isinstance(bar_ts, pd.Timestamp):
                ts_dt = bar_ts.to_pydatetime()
            else:
                ts_dt = datetime.now()

            bar_epoch = ts_dt.timestamp()

            # JST Day Rollover Check (00:00 JST = 15:00 UTC previous day)
            jst_dt = ts_dt + timedelta(hours=9)
            jst_day_str = jst_dt.strftime("%Y-%m-%d")
            if jst_day_str != current_jst_day:
                current_jst_day = jst_day_str
                # Rollover daily counters
                portfolio_daily_realized = 0.0
                daily_cap_records = {sid: 0.0 for sid in strategies.keys()}

            open_p = float(row["open"])
            high_p = float(row["high"])
            low_p = float(row["low"])
            close_p = float(row["close"])
            vol = float(row.get("volume", 1.0))

            # Determine intra-bar path: if Close >= Open, path is Open -> Low -> High -> Close
            if close_p >= open_p:
                sub_ticks = [
                    (open_p, bar_epoch),
                    (low_p, bar_epoch + 15.0),
                    (high_p, bar_epoch + 35.0),
                    (close_p, bar_epoch + 55.0),
                ]
            else:
                sub_ticks = [
                    (open_p, bar_epoch),
                    (high_p, bar_epoch + 15.0),
                    (low_p, bar_epoch + 35.0),
                    (close_p, bar_epoch + 55.0),
                ]

            bar_range_bp = (high_p - low_p) / open_p * 10000.0 if open_p > 0 else 0.0

            for p, tick_epoch in sub_ticks:
                mid_p = p
                # Synthetic realistic micro-spread and imbalance
                spread_bp = max(0.5, min(8.0, self.base_spread_bp + (bar_range_bp * 0.1)))
                spread_half_jpy = (mid_p * spread_bp / 10000.0) / 2.0

                # Micro Taker Delta & Book Imbalance derived from sub-step velocity
                delta_ratio = max(-1.0, min(1.0, (close_p - open_p) / max(1.0, (high_p - low_p + 1e-9))))
                book_imbalance = max(-0.8, min(0.8, delta_ratio * 0.8))

                tick_data = {"price": mid_p, "timestamp": tick_epoch, "mid": mid_p}
                flow_stats = {
                    "mid_price": mid_p,
                    "spread_bp": spread_bp,
                    "book_imbalance": book_imbalance,
                    "delta_ratio": delta_ratio,
                }

                # ----------------------------------------------------
                # A. Regime Detection & Weights
                # ----------------------------------------------------
                ema_strat = strategies.get("EmaTrend")
                fast_e = (ema_strat.fast_ema if ema_strat else None) or mid_p
                slow_e = (ema_strat.slow_ema if ema_strat else None) or mid_p

                if self.enable_regime_switch and mode != "baseline_ema":
                    regime, weights = self.regime_detector.detect(
                        mid_price=mid_p,
                        fast_ema=fast_e,
                        slow_ema=slow_e,
                        spread_bp=spread_bp,
                        bar_range_bp=bar_range_bp,
                    )
                else:
                    regime, weights = "NORMAL", {sid: 1.0 for sid in strategies.keys()}

                regime_counts[regime] = regime_counts.get(regime, 0) + 1

                # ----------------------------------------------------
                # B. Strategy Decision Phase (Target Qty from Brains)
                # ----------------------------------------------------
                strat_targets: Dict[str, float] = {}
                strat_reasons: Dict[str, str] = {}

                # Check Portfolio Daily Cap
                portfolio_halted = (
                    self.enable_daily_cap and portfolio_daily_realized <= -self.portfolio_daily_limit_jpy
                )

                for strat_id, strat in strategies.items():
                    pm = pos_managers[strat_id]
                    curr_pos = pm.actual_qty
                    avg_p = pm.avg_price

                    # Check strategy-level daily cap
                    strat_halted = (
                        self.enable_daily_cap
                        and daily_cap_records.get(strat_id, 0.0) <= -self.strategy_daily_limits.get(strat_id, 9999.0)
                    )

                    # Aging Guard Check (300s timeout forced exit)
                    if curr_pos != 0 and pm.oldest_fill_time:
                        elapsed_hold = tick_epoch - pm.oldest_fill_time
                        if elapsed_hold >= self.aging_timeout_sec:
                            strat_targets[strat_id] = 0.0
                            strat_reasons[strat_id] = f"AgingGuard満期強制手仕舞い ({elapsed_hold:.0f}s)"
                            continue

                    # Evaluate Brain signal
                    tgt = strat.decide_target_qty(tick_data, flow_stats, curr_pos, avg_p)

                    # Apply Daily Loss Caps: if halted, prevent new entry (only allow exit to 0)
                    if (portfolio_halted or strat_halted) and abs(tgt) > 1e-9:
                        if abs(curr_pos) < 1e-9:
                            tgt = 0.0  # Block new entry
                        elif (curr_pos > 0 and tgt < 0) or (curr_pos < 0 and tgt > 0):
                            tgt = 0.0  # Force flat instead of flip

                    # Apply Regime Weight: if weight is 0, cannot open new position
                    strat_weight = weights.get(strat_id, 1.0)
                    if strat_weight <= 0.0 and abs(curr_pos) < 1e-9:
                        tgt = 0.0  # Prevent entry during adverse regime

                    strat_targets[strat_id] = round(tgt, 6)

                # ----------------------------------------------------
                # C. Execution & Aggregation Phase
                # ----------------------------------------------------
                if mode == "portfolio" and self.enable_internal_netting:
                    # GAPCORE Core: Target Aggregation & Internal Netting
                    portfolio_target = round(sum(strat_targets.values()), 6)
                    required_exchange = round(portfolio_target - exchange_actual_qty, 6)

                    # Calculate gross intended trade volume across strategies before updating PMs
                    gross_trade_qty = 0.0
                    for strat_id, tgt in strat_targets.items():
                        diff = round(tgt - pos_managers[strat_id].actual_qty, 6)
                        if abs(diff) >= self.order_size:
                            gross_trade_qty += abs(diff)

                    # Netted Volume: gross desired volume minus net physical exchange volume
                    phys_needed_qty = abs(required_exchange) if abs(required_exchange) >= self.order_size else 0.0
                    netted_qty = max(0.0, round(gross_trade_qty - phys_needed_qty, 6))

                    if netted_qty > 0:
                        netting_events_count += 1
                        # Each netted unit saves a full round of crossing the spread (2 * spread_half_jpy)
                        savings = netted_qty * (spread_half_jpy * 2.0)
                        spread_savings_jpy += savings

                    # 1. Update Shadow Accounting for all strategies (Mid-Price valuation)
                    for strat_id, tgt in strat_targets.items():
                        pm = pos_managers[strat_id]
                        diff = round(tgt - pm.actual_qty, 6)
                        if abs(diff) >= self.order_size:
                            fill_side = "BUY" if diff > 0 else "SELL"
                            fill_qty = abs(diff)
                            pnl = pm.on_fill(side=fill_side, size=fill_qty, price=mid_p, timestamp=tick_epoch)
                            if abs(pnl) > 1e-9:
                                daily_cap_records[strat_id] += pnl
                                portfolio_daily_realized += pnl

                    # 2. Physical Exchange Order Dispatch
                    if abs(required_exchange) >= self.order_size:
                        # Rate limit check
                        is_exit = (
                            (exchange_actual_qty > 0 and required_exchange < 0)
                            or (exchange_actual_qty < 0 and required_exchange > 0)
                        )
                        order_allowed = True
                        if self.enable_rate_limit and not is_exit:
                            # Entry order subject to rate limit
                            order_allowed = rate_guard.can_send_order("portfolio", now_ts=tick_epoch)
                            if not order_allowed:
                                rate_limit_skips_count += 1

                        if order_allowed:
                            phys_side = "BUY" if required_exchange > 0 else "SELL"
                            phys_qty = abs(required_exchange)
                            # Execution price incorporates physical spread crossing
                            fill_px = (
                                mid_p + spread_half_jpy if phys_side == "BUY" else mid_p - spread_half_jpy
                            )
                            # Account for physical slippage / spread cost
                            spread_cost = spread_half_jpy * phys_qty
                            exchange_total_fees_and_slippage += spread_cost

                            # Update physical exchange state
                            if abs(exchange_actual_qty) < 1e-9:
                                exchange_actual_qty = phys_qty if phys_side == "BUY" else -phys_qty
                                exchange_avg_price = fill_px
                            elif (exchange_actual_qty > 0 and phys_side == "BUY") or (
                                exchange_actual_qty < 0 and phys_side == "SELL"
                            ):
                                new_q = exchange_actual_qty + (phys_qty if phys_side == "BUY" else -phys_qty)
                                exchange_avg_price = (
                                    abs(exchange_actual_qty) * exchange_avg_price + phys_qty * fill_px
                                ) / abs(new_q)
                                exchange_actual_qty = round(new_q, 6)
                            else:
                                close_q = min(abs(exchange_actual_qty), phys_qty)
                                pnl = (
                                    (fill_px - exchange_avg_price) * close_q
                                    if exchange_actual_qty > 0
                                    else (exchange_avg_price - fill_px) * close_q
                                )
                                exchange_realized_pnl += pnl
                                rem_q = phys_qty - close_q
                                if rem_q > 1e-9:
                                    exchange_actual_qty = round(rem_q if phys_side == "BUY" else -rem_q, 6)
                                    exchange_avg_price = fill_px
                                else:
                                    exchange_actual_qty = round(
                                        exchange_actual_qty + (phys_qty if phys_side == "BUY" else -phys_qty), 6
                                    )
                                    if abs(exchange_actual_qty) < 1e-9:
                                        exchange_actual_qty = 0.0
                                        exchange_avg_price = 0.0

                            physical_orders_count += 1
                            if self.enable_rate_limit:
                                rate_guard.record_order("portfolio", now_ts=tick_epoch)

                else:
                    # Non-Netting Mode: Each strategy executes physical orders independently
                    for strat_id, tgt in strat_targets.items():
                        pm = pos_managers[strat_id]
                        diff = round(tgt - pm.actual_qty, 6)
                        if abs(diff) >= self.order_size:
                            is_exit = (pm.actual_qty > 0 and diff < 0) or (pm.actual_qty < 0 and diff > 0)
                            order_allowed = True
                            if self.enable_rate_limit and not is_exit:
                                order_allowed = rate_guard.can_send_order(strat_id, now_ts=tick_epoch)
                                if not order_allowed:
                                    rate_limit_skips_count += 1

                            if order_allowed:
                                side = "BUY" if diff > 0 else "SELL"
                                size = abs(diff)
                                fill_px = mid_p + spread_half_jpy if side == "BUY" else mid_p - spread_half_jpy
                                spread_cost = spread_half_jpy * size
                                exchange_total_fees_and_slippage += spread_cost

                                pnl = pm.on_fill(side=side, size=size, price=fill_px, timestamp=tick_epoch)
                                if abs(pnl) > 1e-9:
                                    daily_cap_records[strat_id] += pnl
                                    portfolio_daily_realized += pnl

                                physical_orders_count += 1
                                if self.enable_rate_limit:
                                    rate_guard.record_order(strat_id, now_ts=tick_epoch)

            # Record end-of-bar equity
            total_strat_realized = sum(pm.realized_pnl for pm in pos_managers.values())
            total_strat_unrealized = sum(pm.get_unrealized_pnl(close_p) for pm in pos_managers.values())
            current_equity = (
                self.initial_capital_jpy
                + total_strat_realized
                + total_strat_unrealized
                - exchange_total_fees_and_slippage
            )

            timestamps.append(bar_ts)
            equity_series.append(current_equity)

        # ----------------------------------------------------
        # D. Compile Comprehensive Results & KPI
        # ----------------------------------------------------
        eq_s = pd.Series(equity_series, index=timestamps)
        initial_cap = self.initial_capital_jpy
        final_cap = equity_series[-1] if equity_series else initial_cap
        net_profit = final_cap - initial_cap
        return_pct = (net_profit / initial_cap) * 100.0

        # Drawdown calculation
        cummax = eq_s.cummax()
        drawdown = eq_s - cummax
        drawdown_pct = (drawdown / cummax) * 100.0
        max_dd_jpy = abs(float(drawdown.min())) if not drawdown.empty else 0.0
        max_dd_pct = abs(float(drawdown_pct.min())) if not drawdown_pct.empty else 0.0

        # Sharpe ratio calculation (1m bars annualized: 525,600)
        returns = eq_s.pct_change().dropna()
        if len(returns) > 1 and returns.std() > 0:
            sharpe = float((returns.mean() / returns.std()) * np.sqrt(525600.0))
        else:
            sharpe = 0.0

        # Strategy breakdown
        breakdown: Dict[str, Any] = {}
        for sid, pm in pos_managers.items():
            breakdown[sid] = {
                "realized_pnl": round(pm.realized_pnl, 2),
                "final_position": pm.actual_qty,
            }

        return {
            "mode": mode,
            "initial_capital": initial_cap,
            "final_capital": round(final_cap, 2),
            "net_profit_jpy": round(net_profit, 2),
            "return_pct": round(return_pct, 2),
            "max_drawdown_jpy": round(max_dd_jpy, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "sharpe_ratio": round(sharpe, 2),
            "physical_orders_count": physical_orders_count,
            "netting_events_count": netting_events_count,
            "spread_savings_jpy": round(spread_savings_jpy, 2),
            "rate_limit_skips_count": rate_limit_skips_count,
            "regime_counts": regime_counts,
            "strategy_breakdown": breakdown,
            "equity_series": eq_s,
        }
