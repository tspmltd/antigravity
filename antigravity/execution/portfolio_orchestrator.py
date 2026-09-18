"""
PortfolioOrchestrator: GAPCORE Multi-Strategy Portfolio Execution Engine
========================================================================
Coordinates multiple StrategyBrains, Shadow Accounting (PositionManager per strategy),
Target Aggregation, Internal Netting, Regime-Adaptive Sizing, and Order Rate Guarantees.

Key Invariants:
1. Target_PF = sum(Target_i)
2. Required_PF = Target_PF - Actual_Exchange - Pending_Exchange
3. Internal Netting: When signals offset, no physical order is sent;
   positions are virtually matched at Mid-Price saving full 2bp spread costs.
4. Fail-Closed Protection: Portfolio Daily Loss Cap and Order Rate Limit are strictly enforced.
"""

import time
import threading
from typing import Dict, Any, List, Optional, Tuple, Callable

from antigravity.execution.position_manager import PositionManager
from antigravity.risk_guard.order_rate_guard import OrderRateGuard
from antigravity.risk_guard.portfolio_pnl_guard import PortfolioDailyPnLGuard
from antigravity.strategies.ema_trend import EmaTrendTickStrategy
from antigravity.strategies.mean_reversion import MeanReversionStrategy
from antigravity.strategies.orderbook_imbalance import OrderBookImbalanceStrategy
from antigravity.strategies.grid_mm import GridMmStrategy
from antigravity.backtest.regime_detector import RegimeDetector


class PortfolioOrchestrator:
    """
    Production-ready Multi-Strategy Portfolio Orchestrator.
    """

    def __init__(
        self,
        symbol: str = "FX_BTC_JPY",
        order_size: float = 0.001,
        portfolio_daily_limit_jpy: float = 3000.0,
        strategy_daily_limits: Optional[Dict[str, float]] = None,
        max_orders_per_min: int = 30,
        aging_timeout_sec: float = 300.0,
        enable_internal_netting: bool = True,
        enable_regime_switch: bool = True,
        portfolio_guard: Optional[PortfolioDailyPnLGuard] = None,
        order_rate_guard: Optional[OrderRateGuard] = None,
        on_order_required_callback: Optional[Callable[[str, float, float, str], None]] = None,
    ):
        self.symbol = symbol
        self.order_size = order_size
        self.aging_timeout_sec = aging_timeout_sec
        self.enable_internal_netting = enable_internal_netting
        self.enable_regime_switch = enable_regime_switch
        self.on_order_required_callback = on_order_required_callback

        self.lock = threading.RLock()

        # 1. Guards
        self.strategy_daily_limits = strategy_daily_limits or {
            "EmaTrend": 1500.0,
            "MeanReversion": 1000.0,
            "OrderBookImbalance": 500.0,
            "GridMM": 1000.0,
        }
        self.portfolio_guard = portfolio_guard or PortfolioDailyPnLGuard(
            portfolio_limit_jpy=portfolio_daily_limit_jpy,
            strategy_limits=self.strategy_daily_limits,
        )
        self.order_rate_guard = order_rate_guard or OrderRateGuard(
            max_orders_per_min=max_orders_per_min,
            min_order_interval_sec=1.5,
            global_min_interval_sec=0.5,
        )

        # 2. Strategies
        self.strategies: Dict[str, Any] = {
            "EmaTrend": EmaTrendTickStrategy(order_size=self.order_size),
            "MeanReversion": MeanReversionStrategy(order_size=self.order_size),
            "OrderBookImbalance": OrderBookImbalanceStrategy(order_size=self.order_size),
            "GridMM": GridMmStrategy(order_size=self.order_size),
        }

        # 3. Shadow Accounting: Independent PositionManagers
        self.pos_managers: Dict[str, PositionManager] = {
            sid: PositionManager(strategy_id=sid, lot_size=self.order_size)
            for sid in self.strategies.keys()
        }

        # 4. Regime Detector
        self.regime_detector = RegimeDetector()
        self.current_regime: str = "NORMAL"
        self.current_weights: Dict[str, float] = {sid: 1.0 for sid in self.strategies.keys()}

        # 5. Exchange Physical State
        self.exchange_actual_qty: float = 0.0
        self.exchange_avg_price: float = 0.0
        self.exchange_pending_qty: float = 0.0

        # 6. Performance & Netting Metrics
        self.netting_events_count: int = 0
        self.spread_savings_jpy: float = 0.0
        self.physical_orders_dispatched: int = 0
        self.rate_limit_skips_count: int = 0

    @property
    def portfolio_target_qty(self) -> float:
        with self.lock:
            val = sum(pm.target_qty for pm in self.pos_managers.values())
            return round(val, 6)

    @property
    def portfolio_actual_qty(self) -> float:
        with self.lock:
            val = sum(pm.actual_qty for pm in self.pos_managers.values())
            return round(val, 6)

    @property
    def required_exchange_qty(self) -> float:
        """
        GAPCORE Portfolio Invariant:
        RequiredQty = PortfolioTarget - ExchangeActual - ExchangePending
        """
        with self.lock:
            val = self.portfolio_target_qty - self.exchange_actual_qty - self.exchange_pending_qty
            return round(val, 6)

    def on_tick(
        self,
        tick: Dict[str, Any],
        flow_stats: Dict[str, Any],
        now_ts: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Processes real-time market tick across all strategies.
        Computes targets, performs internal netting, and returns order instruction if needed.

        Returns:
            Dict containing order instruction if physical execution is required, else None.
        """
        ts = now_ts if now_ts is not None else tick.get("timestamp", time.time())
        mid_p = flow_stats.get("mid_price", tick.get("mid", tick.get("price", 0.0)))
        spread_bp = flow_stats.get("spread_bp", 1.0)
        spread_half_jpy = (mid_p * spread_bp / 10000.0) / 2.0

        if mid_p <= 0:
            return None

        with self.lock:
            # ----------------------------------------------------
            # 1. Update Market Regime
            # ----------------------------------------------------
            fast_e = self.strategies["EmaTrend"].fast_ema or mid_p
            slow_e = self.strategies["EmaTrend"].slow_ema or mid_p

            if self.enable_regime_switch:
                self.current_regime, self.current_weights = self.regime_detector.detect(
                    mid_price=mid_p,
                    fast_ema=fast_e,
                    slow_ema=slow_e,
                    spread_bp=spread_bp,
                )
            else:
                self.current_regime = "NORMAL"
                self.current_weights = {sid: 1.0 for sid in self.strategies.keys()}

            # ----------------------------------------------------
            # 2. Check Daily Loss Limits (Fail-Closed)
            # ----------------------------------------------------
            is_portfolio_allowed, pf_reason = self.portfolio_guard.can_enter_order("portfolio")

            # ----------------------------------------------------
            # 3. Strategy Target Generation & Aging Check
            # ----------------------------------------------------
            desired_targets: Dict[str, float] = {}

            for strat_id, strat in self.strategies.items():
                pm = self.pos_managers[strat_id]
                curr_pos = pm.actual_qty
                avg_p = pm.avg_price

                # A. Aging Guard (300s timeout forced exit)
                if abs(curr_pos) > 1e-9 and pm.oldest_fill_time:
                    elapsed_hold = ts - pm.oldest_fill_time
                    if elapsed_hold >= self.aging_timeout_sec:
                        desired_targets[strat_id] = 0.0
                        continue

                # B. Brain Decision
                tgt = strat.decide_target_qty(tick, flow_stats, curr_pos, avg_p)

                # C. Check Strategy-Level Daily Loss Guard
                strat_guard = self.portfolio_guard.get_strategy_guard(strat_id)
                strat_allowed = strat_guard.can_enter() if strat_guard else True

                # Block entry if portfolio or strategy breached limit
                if (not is_portfolio_allowed or not strat_allowed) and abs(tgt) > 1e-9:
                    if abs(curr_pos) < 1e-9:
                        tgt = 0.0
                    elif (curr_pos > 0 and tgt < 0) or (curr_pos < 0 and tgt > 0):
                        tgt = 0.0

                # D. Regime Sizing Filter
                weight = self.current_weights.get(strat_id, 1.0)
                if weight <= 0.0 and abs(curr_pos) < 1e-9:
                    tgt = 0.0

                desired_targets[strat_id] = round(tgt, 6)

            # ----------------------------------------------------
            # 4. Target Aggregation & Internal Netting
            # ----------------------------------------------------
            gross_trade_qty = 0.0
            for strat_id, tgt in desired_targets.items():
                diff = round(tgt - self.pos_managers[strat_id].actual_qty, 6)
                if abs(diff) >= self.order_size:
                    gross_trade_qty += abs(diff)

            # Set new targets in Shadow Managers
            for strat_id, tgt in desired_targets.items():
                self.pos_managers[strat_id].set_target(tgt, reason="Brain Decision")

            portfolio_tgt = self.portfolio_target_qty
            req_qty = self.required_exchange_qty

            phys_needed_qty = abs(req_qty) if abs(req_qty) >= self.order_size else 0.0
            netted_qty = max(0.0, round(gross_trade_qty - phys_needed_qty, 6))

            if netted_qty > 0 and self.enable_internal_netting:
                self.netting_events_count += 1
                # Spread savings: 2 * half_spread * netted_qty
                savings = netted_qty * (spread_half_jpy * 2.0)
                self.spread_savings_jpy += savings

                # Virtually fill the netted quantity at Mid-Price for strategies
                for strat_id, tgt in desired_targets.items():
                    pm = self.pos_managers[strat_id]
                    diff = round(tgt - pm.actual_qty, 6)
                    if abs(diff) >= self.order_size and abs(req_qty) < self.order_size:
                        side = "BUY" if diff > 0 else "SELL"
                        pnl = pm.on_fill(side=side, size=abs(diff), price=mid_p, timestamp=ts)
                        if abs(pnl) > 1e-9:
                            self.portfolio_guard.record_trade_pnl(strat_id, pnl)

            # ----------------------------------------------------
            # 5. Physical Order Requirement Check
            # ----------------------------------------------------
            if abs(req_qty) >= self.order_size:
                is_exit = (
                    (self.exchange_actual_qty > 0 and req_qty < 0)
                    or (self.exchange_actual_qty < 0 and req_qty > 0)
                )

                # Order rate guard check
                allowed, rate_reason = self.order_rate_guard.check_order_allowed(
                    strategy_id="portfolio",
                    now_ts=ts,
                )
                if not allowed and not is_exit:
                    self.rate_limit_skips_count += 1
                    return None

                side = "BUY" if req_qty > 0 else "SELL"
                size = abs(req_qty)

                return {
                    "action": "ORDER",
                    "side": side,
                    "size": size,
                    "price": mid_p,
                    "reason": f"PF Target差分執行 (Tgt:{portfolio_tgt:+.4f}, Act:{self.exchange_actual_qty:+.4f})",
                    "is_exit": is_exit,
                    "timestamp": ts,
                }

            return None

    def on_order_sent(self, side: str, size: float):
        with self.lock:
            delta = size if side.upper() == "BUY" else -size
            self.exchange_pending_qty = round(self.exchange_pending_qty + delta, 6)
            self.physical_orders_dispatched += 1
            self.order_rate_guard.record_order("portfolio")

    def on_order_failed(self, side: str, size: float):
        with self.lock:
            delta = size if side.upper() == "BUY" else -size
            self.exchange_pending_qty = round(self.exchange_pending_qty - delta, 6)

    def on_exchange_fill(
        self,
        side: str,
        size: float,
        price: float,
        timestamp: Optional[float] = None,
    ) -> float:
        """
        Called when physical order execution is verified from exchange.
        Updates physical exchange position and distributes fill to Shadow Account Managers.
        """
        ts = timestamp if timestamp is not None else time.time()
        with self.lock:
            fill_delta = size if side.upper() == "BUY" else -size

            # Deduct pending
            if self.exchange_pending_qty > 0 and fill_delta > 0:
                self.exchange_pending_qty = max(0.0, round(self.exchange_pending_qty - fill_delta, 6))
            elif self.exchange_pending_qty < 0 and fill_delta < 0:
                self.exchange_pending_qty = min(0.0, round(self.exchange_pending_qty - fill_delta, 6))
            else:
                self.exchange_pending_qty = round(self.exchange_pending_qty - fill_delta, 6)

            # Update Exchange Actual Qty
            prev_act = self.exchange_actual_qty
            next_act = round(prev_act + fill_delta, 6)

            trade_pnl = 0.0
            if abs(prev_act) < 1e-9:
                self.exchange_actual_qty = next_act
                self.exchange_avg_price = price
            elif (prev_act > 0 and fill_delta > 0) or (prev_act < 0 and fill_delta < 0):
                tot = abs(prev_act) + size
                self.exchange_avg_price = (abs(prev_act) * self.exchange_avg_price + size * price) / tot
                self.exchange_actual_qty = next_act
            else:
                close_q = min(abs(prev_act), size)
                trade_pnl = (
                    (price - self.exchange_avg_price) * close_q
                    if prev_act > 0
                    else (self.exchange_avg_price - price) * close_q
                )
                self.exchange_actual_qty = next_act
                rem_q = size - close_q
                if rem_q > 1e-9:
                    self.exchange_avg_price = price
                elif abs(next_act) < 1e-9:
                    self.exchange_actual_qty = 0.0
                    self.exchange_avg_price = 0.0

            # Distribute fill to strategy Shadow Position Managers that have required difference
            remaining_fill = size
            for strat_id, pm in self.pos_managers.items():
                if remaining_fill <= 1e-9:
                    break
                diff = round(pm.target_qty - pm.actual_qty, 6)
                if (side.upper() == "BUY" and diff > 0) or (side.upper() == "SELL" and diff < 0):
                    alloc_size = min(abs(diff), remaining_fill)
                    strat_pnl = pm.on_fill(side=side, size=alloc_size, price=price, timestamp=ts)
                    if abs(strat_pnl) > 1e-9:
                        self.portfolio_guard.record_trade_pnl(strat_id, strat_pnl)
                    remaining_fill = round(remaining_fill - alloc_size, 6)

            # If remaining_fill > 0 (e.g. direct order dispatch or target unassigned), allocate to primary strategy
            if remaining_fill > 1e-9:
                primary_sid = "EmaTrend" if "EmaTrend" in self.pos_managers else next(iter(self.pos_managers.keys()))
                pm = self.pos_managers[primary_sid]
                fill_delta = remaining_fill if side.upper() == "BUY" else -remaining_fill
                pm.target_qty = round(pm.actual_qty + fill_delta, 6)
                strat_pnl = pm.on_fill(side=side, size=remaining_fill, price=price, timestamp=ts)
                if abs(strat_pnl) > 1e-9:
                    self.portfolio_guard.record_trade_pnl(primary_sid, strat_pnl)

            return trade_pnl

    def get_snapshot(self, mid_price: float = 0.0) -> Dict[str, Any]:
        with self.lock:
            strat_snaps = {sid: pm.get_snapshot(mid_price) for sid, pm in self.pos_managers.items()}
            total_realized = sum(s["realized_pnl"] for s in strat_snaps.values())
            total_unrealized = sum(s["unrealized_pnl"] for s in strat_snaps.values())
            pf_status = self.portfolio_guard.get_status()

            return {
                "portfolio_target_qty": self.portfolio_target_qty,
                "portfolio_actual_qty": self.portfolio_actual_qty,
                "exchange_actual_qty": self.exchange_actual_qty,
                "exchange_pending_qty": self.exchange_pending_qty,
                "required_exchange_qty": self.required_exchange_qty,
                "exchange_avg_price": self.exchange_avg_price,
                "total_realized_pnl": total_realized,
                "total_unrealized_pnl": total_unrealized,
                "total_pnl": total_realized + total_unrealized,
                "netting_events_count": self.netting_events_count,
                "spread_savings_jpy": self.spread_savings_jpy,
                "physical_orders_dispatched": self.physical_orders_dispatched,
                "rate_limit_skips_count": self.rate_limit_skips_count,
                "current_regime": self.current_regime,
                "current_weights": self.current_weights,
                "portfolio_guard_status": pf_status,
                "strategy_snapshots": strat_snaps,
            }
