"""
AgingGuard: GAPCORE-compliant Inventory Aging / Hold Timeout Risk Guard
======================================================================
Monitors how long a position has been held (FIFO oldest fill age).
Forces an emergency exit if an inventory remains open beyond `max_hold_seconds`
to eliminate salt-pickling (塩漬け) and runaway trend drift risks.
"""

import time
import threading
from typing import Optional, Callable, Dict, Any


class AgingGuard:
    """
    GAPCORE-compliant inventory aging guard.
    
    Tracks the timestamp of the oldest open fill. If the position is held
    longer than `max_hold_seconds`, triggers a timeout exit signal and callback.
    """

    def __init__(
        self,
        max_hold_seconds: float = 300.0,      # Default: 5 minutes max hold
        warning_ratio: float = 0.80,          # Warn at 80% of max hold (e.g., 4 mins)
        on_timeout_callback: Optional[Callable[[str, float, float], None]] = None,
        on_warning_callback: Optional[Callable[[str, float, float], None]] = None,
    ):
        if max_hold_seconds <= 0:
            raise ValueError("max_hold_seconds must be positive")

        self.max_hold_seconds = float(max_hold_seconds)
        self.warning_ratio = float(warning_ratio)
        self.warning_seconds = self.max_hold_seconds * self.warning_ratio

        self.on_timeout_callback = on_timeout_callback
        self.on_warning_callback = on_warning_callback

        self.lock = threading.RLock()

        # State fields
        self.oldest_fill_time: Optional[float] = None
        self.entry_price: float = 0.0
        self.current_pos: float = 0.0
        self.is_warning_fired: bool = False
        self.is_timed_out_fired: bool = False

    @property
    def has_position(self) -> bool:
        with self.lock:
            return abs(self.current_pos) > 1e-9 and self.oldest_fill_time is not None

    def on_position_update(
        self,
        new_position: float,
        price: float = 0.0,
        timestamp: Optional[float] = None,
    ) -> None:
        """
        Called whenever position quantity changes (fills, partial closes, or flips).
        Adheres to FIFO aging:
        - 0 -> pos: records initial oldest fill timestamp.
        - pos -> 0: resets aging counter.
        - flip (long -> short or short -> long): resets oldest fill timestamp to current flip.
        - same-sign change (partial close or pyramid): retains oldest fill timestamp.
        """
        now = timestamp if timestamp is not None else time.time()

        with self.lock:
            prev_pos = self.current_pos
            self.current_pos = new_position

            # Position completely closed
            if abs(new_position) < 1e-9:
                self.oldest_fill_time = None
                self.entry_price = 0.0
                self.is_warning_fired = False
                self.is_timed_out_fired = False
                return

            # Initial position opened from zero
            if abs(prev_pos) < 1e-9:
                self.oldest_fill_time = now
                self.entry_price = price
                self.is_warning_fired = False
                self.is_timed_out_fired = False
                return

            # Doten / Position flipped sign
            if (prev_pos > 0 and new_position < 0) or (prev_pos < 0 and new_position > 0):
                self.oldest_fill_time = now
                self.entry_price = price
                self.is_warning_fired = False
                self.is_timed_out_fired = False
                return

            # Same sign (increase or partial reduction): retain oldest_fill_time
            if price > 0:
                self.entry_price = price

    def get_age_seconds(self, now: Optional[float] = None) -> float:
        """Returns age of the oldest open fill in seconds. Returns 0 if flat."""
        with self.lock:
            if not self.has_position or self.oldest_fill_time is None:
                return 0.0
            ts = now if now is not None else time.time()
            return max(0.0, ts - self.oldest_fill_time)

    def check(
        self,
        current_position: float,
        current_price: float = 0.0,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Periodic heartbeat check for inventory timeout.
        Returns status dict with `action` ("EXIT" or "HOLD").
        """
        now_ts = now if now is not None else time.time()

        with self.lock:
            # Sync position if provided
            if abs(self.current_pos - current_position) > 1e-9:
                self.on_position_update(current_position, price=current_price, timestamp=now_ts)

            if not self.has_position:
                return {
                    "is_active": False,
                    "current_position": 0.0,
                    "age_seconds": 0.0,
                    "max_hold_seconds": self.max_hold_seconds,
                    "is_warning": False,
                    "is_timed_out": False,
                    "action": "HOLD",
                    "reason": "No open position",
                }

            age_sec = self.get_age_seconds(now=now_ts)

            # Check timeout
            if age_sec >= self.max_hold_seconds:
                reason = (
                    f"在庫滞留タイムアウト検知 (保有時間: {age_sec:.1f}秒 >= 許容上限: {self.max_hold_seconds:.0f}秒) "
                    f"[建玉: {self.current_pos:+.4f} BTC]"
                )
                if not self.is_timed_out_fired:
                    self.is_timed_out_fired = True
                    print(f"[AgingGuard] 🚨 AGING TIMEOUT: {reason}", flush=True)
                    if self.on_timeout_callback:
                        try:
                            self.on_timeout_callback(reason, age_sec, self.current_pos)
                        except Exception as ex:
                            print(f"[AgingGuard] Error in on_timeout_callback: {ex}", flush=True)

                return {
                    "is_active": True,
                    "current_position": self.current_pos,
                    "age_seconds": age_sec,
                    "max_hold_seconds": self.max_hold_seconds,
                    "is_warning": True,
                    "is_timed_out": True,
                    "action": "EXIT",
                    "reason": reason,
                }

            # Check early warning (e.g. 80% elapsed)
            if age_sec >= self.warning_seconds:
                if not self.is_warning_fired:
                    self.is_warning_fired = True
                    reason = (
                        f"在庫滞留警告水準到達 (保有時間: {age_sec:.1f}秒 >= 警告閾値: {self.warning_seconds:.0f}秒 / 上限: {self.max_hold_seconds:.0f}秒) "
                        f"[建玉: {self.current_pos:+.4f} BTC]"
                    )
                    print(f"[AgingGuard] ⚠️ AGING WARNING: {reason}", flush=True)
                    if self.on_warning_callback:
                        try:
                            self.on_warning_callback(reason, age_sec, self.current_pos)
                        except Exception as ex:
                            print(f"[AgingGuard] Error in on_warning_callback: {ex}", flush=True)

                return {
                    "is_active": True,
                    "current_position": self.current_pos,
                    "age_seconds": age_sec,
                    "max_hold_seconds": self.max_hold_seconds,
                    "is_warning": True,
                    "is_timed_out": False,
                    "action": "HOLD",
                    "reason": "Aging warning active",
                }

            return {
                "is_active": True,
                "current_position": self.current_pos,
                "age_seconds": age_sec,
                "max_hold_seconds": self.max_hold_seconds,
                "is_warning": False,
                "is_timed_out": False,
                "action": "HOLD",
                "reason": "Within hold threshold",
            }
