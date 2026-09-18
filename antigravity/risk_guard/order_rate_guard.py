"""
OrderRateGuard: GAPCORE-compliant Order Rate Limiter & Flood Guard
===================================================================
Enforces bitFlyer Private API rate limits at the mathematical level,
preventing HTTP 429 Too Many Requests and account API bans across
single or multi-strategy portfolio engines.

Guarantees:
1. MaxOrdersPerMinute (Global/Portfolio):
   Strict sliding-window deque ensuring total private order calls never exceed limit.
2. Per-Strategy Cooldown:
   Enforces MIN_ORDER_INTERVAL_SEC (default 3.0s) per individual strategy.
3. Global Burst Spacing:
   Enforces minimum spacing between ANY consecutive orders (default 0.5s).
4. Cancellation Rate Guard:
   Protects cancel order endpoints from spamming.
5. Fail-Closed Design:
   Rejects orders by default if limits are violated and optionally triggers notifications.
"""

import time
import threading
from collections import deque
from typing import Optional, Dict, Any, List, Tuple, Callable


class OrderRateGuard:
    """
    Thread-safe, high-precision rate limit guard for exchange order dispatch.
    """

    def __init__(
        self,
        max_orders_per_min: int = 60,
        min_order_interval_sec: float = 3.0,
        global_min_interval_sec: float = 0.5,
        max_cancels_per_min: int = 30,
        min_cancel_interval_sec: float = 1.0,
        window_seconds: float = 60.0,
        on_rate_limit_callback: Optional[Callable[[str, int, int], None]] = None,
    ):
        self.max_orders_per_min = max_orders_per_min
        self.min_order_interval_sec = min_order_interval_sec
        self.global_min_interval_sec = global_min_interval_sec
        self.max_cancels_per_min = max_cancels_per_min
        self.min_cancel_interval_sec = min_cancel_interval_sec
        self.window_seconds = window_seconds
        self.on_rate_limit_callback = on_rate_limit_callback

        self.lock = threading.RLock()

        # Sliding window history of timestamps
        self.order_history: deque = deque()
        self.cancel_history: deque = deque()

        # Last order timestamps
        self.last_global_order_ts: float = 0.0
        self.last_global_cancel_ts: float = 0.0
        self.last_strategy_order_ts: Dict[str, float] = {}

        # Violation counters
        self.order_reject_count: int = 0
        self.cancel_reject_count: int = 0

    @property
    def last_order_ts(self) -> float:
        return self.last_global_order_ts

    @last_order_ts.setter
    def last_order_ts(self, val: float) -> None:
        with self.lock:
            self.last_global_order_ts = val
            if val == 0.0:
                self.last_strategy_order_ts.clear()
                self.order_history.clear()
            else:
                for strat in list(self.last_strategy_order_ts.keys()):
                    self.last_strategy_order_ts[strat] = val

    def reset_limits(self) -> None:
        """Reset all rate counters (useful for testing or state resets)."""
        with self.lock:
            self.last_global_order_ts = 0.0
            self.last_global_cancel_ts = 0.0
            self.last_strategy_order_ts.clear()
            self.order_history.clear()
            self.cancel_history.clear()
            self.order_reject_count = 0
            self.cancel_reject_count = 0

    def _cleanup_window(self, history: deque, now_ts: float) -> None:
        cutoff = now_ts - self.window_seconds
        while history and history[0] <= cutoff:
            history.popleft()

    def check_order_allowed(
        self,
        strategy_id: str = "default",
        now_ts: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Evaluates whether an order can be sent under current rate constraints.
        Returns (allowed: bool, reason: str).
        """
        now = now_ts if now_ts is not None else time.time()

        with self.lock:
            # 1. Global consecutive burst spacing
            elapsed_global = now - self.last_global_order_ts
            if elapsed_global < self.global_min_interval_sec:
                rem = self.global_min_interval_sec - elapsed_global
                return False, f"グローバルバースト間隔制限 ({elapsed_global:.2f}s < {self.global_min_interval_sec:.1f}s, 残余待機: {rem:.2f}s)"

            # 2. Per-strategy cooldown
            last_strat_ts = self.last_strategy_order_ts.get(strategy_id, 0.0)
            elapsed_strat = now - last_strat_ts
            if elapsed_strat < self.min_order_interval_sec:
                rem = self.min_order_interval_sec - elapsed_strat
                return False, f"戦略({strategy_id})クールダウン待機中 ({elapsed_strat:.2f}s < {self.min_order_interval_sec:.1f}s, 残余待機: {rem:.2f}s)"

            # 3. Sliding-window 1-minute order volume limit
            self._cleanup_window(self.order_history, now)
            current_count = len(self.order_history)
            if current_count >= self.max_orders_per_min:
                oldest = self.order_history[0] if self.order_history else now
                wait_time = max(0.0, oldest + self.window_seconds - now)
                return False, f"1分間発注上限到達 ({current_count}/{self.max_orders_per_min}回, 枠開放まで: {wait_time:.1f}s)"

            return True, "ALLOWED"

    def can_send_order(
        self,
        strategy_id: str = "default",
        now_ts: Optional[float] = None,
    ) -> bool:
        """
        Boolean interface for rapid checking.
        Fires callback if rate limit is breached.
        """
        allowed, reason = self.check_order_allowed(strategy_id=strategy_id, now_ts=now_ts)
        if not allowed:
            with self.lock:
                self.order_reject_count += 1
                cnt = len(self.order_history)
            if self.on_rate_limit_callback:
                try:
                    self.on_rate_limit_callback(reason, cnt, self.max_orders_per_min)
                except Exception:
                    pass
        return allowed

    def record_order(
        self,
        strategy_id: str = "default",
        now_ts: Optional[float] = None,
    ) -> None:
        """
        Records an accepted order event into history and updates timestamps.
        """
        now = now_ts if now_ts is not None else time.time()
        with self.lock:
            self._cleanup_window(self.order_history, now)
            self.order_history.append(now)
            self.last_global_order_ts = now
            self.last_strategy_order_ts[strategy_id] = now

    def check_cancel_allowed(self, now_ts: Optional[float] = None) -> Tuple[bool, str]:
        """Evaluates whether a cancel request can be sent."""
        now = now_ts if now_ts is not None else time.time()
        with self.lock:
            elapsed = now - self.last_global_cancel_ts
            if elapsed < self.min_cancel_interval_sec:
                return False, f"キャンセル間隔制限 ({elapsed:.2f}s < {self.min_cancel_interval_sec:.1f}s)"

            self._cleanup_window(self.cancel_history, now)
            current_count = len(self.cancel_history)
            if current_count >= self.max_cancels_per_min:
                return False, f"1分間キャンセル上限到達 ({current_count}/{self.max_cancels_per_min}回)"

            return True, "ALLOWED"

    def can_cancel_order(self, now_ts: Optional[float] = None) -> bool:
        allowed, _ = self.check_cancel_allowed(now_ts=now_ts)
        if not allowed:
            with self.lock:
                self.cancel_reject_count += 1
        return allowed

    def record_cancel(self, now_ts: Optional[float] = None) -> None:
        now = now_ts if now_ts is not None else time.time()
        with self.lock:
            self._cleanup_window(self.cancel_history, now)
            self.cancel_history.append(now)
            self.last_global_cancel_ts = now

    def get_status(self, now_ts: Optional[float] = None) -> Dict[str, Any]:
        """Returns comprehensive diagnostic metrics of current rate state."""
        now = now_ts if now_ts is not None else time.time()
        with self.lock:
            self._cleanup_window(self.order_history, now)
            self._cleanup_window(self.cancel_history, now)
            orders_1m = len(self.order_history)
            cancels_1m = len(self.cancel_history)
            return {
                "orders_last_1m": orders_1m,
                "orders_limit_1m": self.max_orders_per_min,
                "orders_remaining_1m": max(0, self.max_orders_per_min - orders_1m),
                "cancels_last_1m": cancels_1m,
                "cancels_limit_1m": self.max_cancels_per_min,
                "order_reject_count": self.order_reject_count,
                "cancel_reject_count": self.cancel_reject_count,
                "sec_since_last_order": (now - self.last_global_order_ts) if self.last_global_order_ts > 0 else 9999.0,
            }
