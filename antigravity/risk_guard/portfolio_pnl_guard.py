"""
PortfolioDailyPnLGuard: GAPCORE Multi-Strategy Portfolio Risk Manager
======================================================================
Coordinates and enforces persistent daily loss limits across multiple
strategies, guaranteeing both individual strategy caps and an overarching
portfolio-level circuit breaker.

Architecture:
- Global state: `data/live-daily-pnl-PORTFOLIO.json`
- Strategy states: `data/live-daily-pnl-{StrategyID}.json`
- Invariant:
  1. If Portfolio total loss <= -PORTFOLIO_LIMIT_JPY:
     ALL strategies are HALTED (Fail-Closed).
  2. If an individual strategy loss <= -STRATEGY_LIMIT_JPY:
     Only that strategy is HALTED, other strategies may continue if portfolio allows.
  3. All states automatically rollover at 00:00 JST atomically.
"""

import os
import json
import time
import threading
from typing import Optional, Dict, Any, List, Tuple, Callable

from .daily_pnl_guard import DailyPnLGuard, get_jst_day_str


class PortfolioDailyPnLGuard:
    """
    Multi-Strategy Portfolio Daily Loss Sentinel.
    Manages global portfolio limit and dispatches to individual DailyPnLGuards.
    """

    def __init__(
        self,
        portfolio_limit_jpy: float = 3000.0,
        strategy_limits: Optional[Dict[str, float]] = None,
        state_dir: Optional[str] = None,
        on_portfolio_breach_callback: Optional[Callable[[str, float, float], None]] = None,
    ):
        self.portfolio_limit_jpy = abs(portfolio_limit_jpy)
        self.on_portfolio_breach_callback = on_portfolio_breach_callback

        self.state_dir = state_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data"
        )
        os.makedirs(self.state_dir, exist_ok=True)
        self.portfolio_state_path = os.path.join(self.state_dir, "live-daily-pnl-PORTFOLIO.json")

        self.lock = threading.RLock()
        self.strategy_guards: Dict[str, DailyPnLGuard] = {}

        # Initialize portfolio state
        self.jst_day: str = get_jst_day_str()
        self.portfolio_realized_jpy: float = 0.0
        self.portfolio_halted: bool = False
        self.halt_reason: str = ""

        # Load or create portfolio state
        self._load_or_init_portfolio_state()

        # Register initial strategies if provided
        if strategy_limits:
            for strat_id, limit in strategy_limits.items():
                self.register_strategy(strat_id, limit)

    def _load_or_init_portfolio_state(self) -> None:
        today_jst = get_jst_day_str()
        with self.lock:
            if os.path.exists(self.portfolio_state_path):
                try:
                    with open(self.portfolio_state_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("jst_day") == today_jst:
                        self.jst_day = today_jst
                        self.portfolio_realized_jpy = float(data.get("realized_jpy", 0.0))
                        self.portfolio_halted = bool(data.get("halted", False))
                        self.halt_reason = str(data.get("halt_reason", ""))
                        return
                except Exception as ex:
                    print(f"[PortfolioDailyPnLGuard] ⚠️ Failed reading state ({ex}). Initializing fresh.", flush=True)

            # Fresh init for new day or new file
            self.jst_day = today_jst
            self.portfolio_realized_jpy = 0.0
            self.portfolio_halted = False
            self.halt_reason = ""
            self._save_portfolio_state()

    def _save_portfolio_state(self) -> None:
        with self.lock:
            data = {
                "jst_day": self.jst_day,
                "realized_jpy": self.portfolio_realized_jpy,
                "limit_jpy": self.portfolio_limit_jpy,
                "halted": self.portfolio_halted,
                "halt_reason": self.halt_reason,
                "strategies": {sid: g.get_status() for sid, g in self.strategy_guards.items()},
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00", time.localtime()),
            }
            tmp_path = f"{self.portfolio_state_path}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.portfolio_state_path)
            except Exception as ex:
                print(f"[PortfolioDailyPnLGuard] ❌ Atomic save failed: {ex}", flush=True)

    def _check_rollover(self) -> None:
        today_jst = get_jst_day_str()
        if self.jst_day != today_jst:
            with self.lock:
                print(f"\n[PortfolioDailyPnLGuard] 🌅 JST Midnight Rollover: {self.jst_day} -> {today_jst}", flush=True)
                self.jst_day = today_jst
                self.portfolio_realized_jpy = 0.0
                self.portfolio_halted = False
                self.halt_reason = ""
                self._save_portfolio_state()

    def register_strategy(
        self,
        strategy_id: str,
        limit_jpy: float,
        on_breach_callback: Optional[Callable[[str, float, float], None]] = None,
    ) -> DailyPnLGuard:
        """Registers an individual strategy with its allocated daily loss budget."""
        with self.lock:
            if strategy_id in self.strategy_guards:
                return self.strategy_guards[strategy_id]

            guard = DailyPnLGuard(
                strategy_id=strategy_id,
                limit_jpy=limit_jpy,
                state_dir=self.state_dir,
                label=f"portfolio_{strategy_id}",
                on_breach_callback=on_breach_callback,
            )
            self.strategy_guards[strategy_id] = guard
            # Sync portfolio total if currently zero and strategies have existing realized pnl
            if abs(self.portfolio_realized_jpy) < 1e-9:
                self.portfolio_realized_jpy = round(sum(g.realized_jpy for g in self.strategy_guards.values()), 2)
            self._save_portfolio_state()
            return guard

    def get_strategy_guard(self, strategy_id: str) -> Optional[DailyPnLGuard]:
        with self.lock:
            return self.strategy_guards.get(strategy_id)

    def can_enter(self, strategy_id: str) -> Tuple[bool, str]:
        """
        Hierarchical entry permission check.
        Returns (allowed: bool, reason: str).
        """
        self._check_rollover()
        with self.lock:
            # 1. Portfolio-level halt check (All strategies blocked)
            if self.portfolio_halted:
                return False, f"ポートフォリオ全体日次リミット到達により全取引停止中: {self.halt_reason}"

            if self.portfolio_realized_jpy <= -self.portfolio_limit_jpy:
                self.portfolio_halted = True
                self.halt_reason = f"ポートフォリオ累計損失到達 ({self.portfolio_realized_jpy:,.1f}円 <= -{self.portfolio_limit_jpy:,.0f}円)"
                self._save_portfolio_state()
                return False, self.halt_reason

            # 2. Strategy-level check
            strat_guard = self.strategy_guards.get(strategy_id)
            if strat_guard is not None:
                if not strat_guard.can_enter():
                    return False, f"戦略({strategy_id})個別日次リミット到達 ({strat_guard.realized_jpy:,.1f}円 <= -{strat_guard.limit_jpy:,.0f}円)"

            return True, "ALLOWED"

    def record_trade_pnl(self, strategy_id: str, pnl_jpy: float) -> Dict[str, Any]:
        """
        Records trade pnl both in strategy guard and in overall portfolio ledger.
        """
        self._check_rollover()
        with self.lock:
            # 1. Update individual strategy guard
            strat_guard = self.strategy_guards.get(strategy_id)
            strat_status = {}
            if strat_guard:
                strat_res = strat_guard.record_trade_pnl(pnl_jpy)
                strat_status = strat_res

            # 2. Update portfolio total
            self.portfolio_realized_jpy = round(self.portfolio_realized_jpy + pnl_jpy, 2)

            # Check portfolio breach
            if self.portfolio_realized_jpy <= -self.portfolio_limit_jpy and not self.portfolio_halted:
                self.portfolio_halted = True
                self.halt_reason = f"ポートフォリオ累計損失リミット到達 (本日累計: {self.portfolio_realized_jpy:,.1f}円 <= 許容: -{self.portfolio_limit_jpy:,.0f}円)"
                print(f"\n[PortfolioDailyPnLGuard] 🚨 {self.halt_reason}", flush=True)
                if self.on_portfolio_breach_callback:
                    try:
                        self.on_portfolio_breach_callback(self.halt_reason, self.portfolio_realized_jpy, self.portfolio_limit_jpy)
                    except Exception as ex:
                        print(f"[PortfolioDailyPnLGuard] ⚠️ Breach callback error: {ex}", flush=True)

            self._save_portfolio_state()

            return {
                "jst_day": self.jst_day,
                "portfolio_realized_jpy": self.portfolio_realized_jpy,
                "portfolio_limit_jpy": self.portfolio_limit_jpy,
                "portfolio_remaining_jpy": max(0.0, self.portfolio_limit_jpy + self.portfolio_realized_jpy),
                "portfolio_halted": self.portfolio_halted,
                "strategy_status": strat_status,
            }

    def get_summary(self) -> Dict[str, Any]:
        """Returns diagnostic snapshot of portfolio risk status."""
        self._check_rollover()
        with self.lock:
            rem = max(0.0, self.portfolio_limit_jpy + self.portfolio_realized_jpy)
            strat_summaries = {}
            for sid, g in self.strategy_guards.items():
                s_rem = max(0.0, g.limit_jpy + g.realized_jpy)
                strat_summaries[sid] = {
                    "realized_jpy": g.realized_jpy,
                    "limit_jpy": g.limit_jpy,
                    "remaining_jpy": s_rem,
                    "halted": g.is_halted,
                }
            return {
                "jst_day": self.jst_day,
                "portfolio_realized_jpy": self.portfolio_realized_jpy,
                "portfolio_limit_jpy": self.portfolio_limit_jpy,
                "portfolio_remaining_jpy": rem,
                "portfolio_halted": self.portfolio_halted,
                "halt_reason": self.halt_reason,
                "strategies": strat_summaries,
            }

    def get_status(self) -> Dict[str, Any]:
        """Alias for get_summary for compatibility."""
        return self.get_summary()

    def can_enter_order(self, strategy_id: str = "portfolio") -> Tuple[bool, str]:
        """Alias for can_enter for compatibility."""
        return self.can_enter(strategy_id)
