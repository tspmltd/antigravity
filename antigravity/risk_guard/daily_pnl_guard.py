"""
DailyPnLGuard: GAPCORE-compliant Persistent Daily Loss Risk Guard
================================================================
Tracks JST calendar-day (00:00:00 JST) realized PnL across process restarts.
Persists state atomically to a JSON file so that crashes, watchdog restarts,
or unexpected exceptions NEVER reset the drawdown counter.
"""

import os
import json
import time
import tempfile
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, Dict, Any

JST = timezone(timedelta(hours=9))


def get_jst_day_str(dt: Optional[datetime] = None) -> str:
    """Get YYYY-MM-DD string in JST."""
    if dt is None:
        dt = datetime.now(JST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(JST)
    else:
        dt = dt.astimezone(JST)
    return dt.strftime("%Y-%m-%d")


class DailyPnLGuard:
    """
    GAPCORE-compliant daily loss guard.
    
    Key invariant:
    - If accumulated realized loss for the current JST calendar day
      exceeds `limit_jpy`, `halted` is set to True and persisted.
    - Subsequent restarts on the same JST day will immediately load
      the halted state and prevent any new orders (Fail-Closed).
    - At 00:00 JST, the guard automatically rolls over to a fresh day.
    """

    def __init__(
        self,
        strategy_id: str,
        limit_jpy: float,
        state_dir: Optional[str] = None,
        label: str = "daily_pnl_guard",
        on_breach_callback: Optional[Callable[[str, float, float], None]] = None,
    ):
        if not strategy_id:
            raise ValueError("strategy_id is required")
        if limit_jpy <= 0:
            raise ValueError("limit_jpy must be positive")

        self.strategy_id = strategy_id
        self.limit_jpy = float(limit_jpy)
        self.label = label
        self.on_breach_callback = on_breach_callback

        if state_dir is None:
            # Default to <repo_root>/data or fallback
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            state_dir = os.path.join(base_dir, "data")
        self.state_dir = state_dir
        os.makedirs(self.state_dir, exist_ok=True)

        self.state_path = os.path.join(self.state_dir, f"live-daily-pnl-{self.strategy_id}.json")
        self.lock = threading.RLock()

        # State fields
        self.jst_day: str = get_jst_day_str()
        self.realized_jpy: float = 0.0
        self.halted: bool = False
        self.last_updated_iso: str = ""

        # Session tracking
        self.session_start_realized: float = 0.0
        self.session_initialized: bool = False

        # Load persisted state on startup
        self._load()

    @property
    def is_halted(self) -> bool:
        with self.lock:
            self._check_day_rollover()
            return self.halted

    def can_enter(self) -> bool:
        """Returns True only if trading is allowed (not halted)."""
        return not self.is_halted

    def can_enter_order(self, *args, **kwargs) -> bool:
        """Alias for can_enter for compatibility."""
        return self.can_enter()

    def observe(self, session_realized_jpy: float) -> Dict[str, Any]:
        """
        Observe session-level cumulative realized PnL (from engine start).
        Calculates the delta and updates the calendar-day total.
        Returns state dictionary with `breached` flag.
        """
        with self.lock:
            self._check_day_rollover()

            if not self.session_initialized:
                self.session_start_realized = session_realized_jpy
                self.session_initialized = True

            delta = session_realized_jpy - self.session_start_realized
            daily_jpy = self.realized_jpy + delta

            breached_now = False

            if not self.halted and daily_jpy <= -self.limit_jpy:
                self.halted = True
                breached_now = True
                reason = (
                    f"JST暦日({self.jst_day}) 累積実現損失リミット到達 "
                    f"(本日累計: {daily_jpy:+,.1f} 円 <= 許容上限: -{self.limit_jpy:,.0f} 円)"
                )
                print(f"[DailyPnLGuard] 🚨 LIMIT BREACH: {reason}", flush=True)

                self._checkpoint(session_realized_jpy, daily_jpy)

                if self.on_breach_callback:
                    try:
                        self.on_breach_callback(reason, daily_jpy, self.limit_jpy)
                    except Exception as ex:
                        print(f"[DailyPnLGuard] Error in on_breach_callback: {ex}", flush=True)
            else:
                self._checkpoint(session_realized_jpy, daily_jpy)

            return {
                "jst_day": self.jst_day,
                "realized_jpy": self.realized_jpy,
                "limit_jpy": self.limit_jpy,
                "halted": self.halted,
                "breached": breached_now,
                "can_enter": not self.halted,
            }

    def record_trade_pnl(self, trade_pnl: float) -> Dict[str, Any]:
        """
        Directly record a completed trade's realized PnL.
        Convenience wrapper around observe().
        """
        with self.lock:
            self._check_day_rollover()
            new_daily_jpy = self.realized_jpy + trade_pnl
            breached_now = False

            if not self.halted and new_daily_jpy <= -self.limit_jpy:
                self.halted = True
                breached_now = True
                reason = (
                    f"JST暦日({self.jst_day}) 累積実現損失リミット到達 "
                    f"(本日累計: {new_daily_jpy:+,.1f} 円 <= 許容上限: -{self.limit_jpy:,.0f} 円)"
                )
                print(f"[DailyPnLGuard] 🚨 LIMIT BREACH: {reason}", flush=True)

                self.realized_jpy = new_daily_jpy
                self._save()

                if self.on_breach_callback:
                    try:
                        self.on_breach_callback(reason, new_daily_jpy, self.limit_jpy)
                    except Exception as ex:
                        print(f"[DailyPnLGuard] Error in on_breach_callback: {ex}", flush=True)
            else:
                self.realized_jpy = new_daily_jpy
                self._save()

            return {
                "jst_day": self.jst_day,
                "realized_jpy": self.realized_jpy,
                "limit_jpy": self.limit_jpy,
                "halted": self.halted,
                "breached": breached_now,
                "can_enter": not self.halted,
            }

    def manual_resume(self, reason: str = "手動復帰指示") -> None:
        """Manually un-halt trading (requires explicit operator action)."""
        with self.lock:
            self.halted = False
            self._save()
            print(f"[DailyPnLGuard] 🟢 TRADING MANUALLY RESUMED: {reason} (本日損益: {self.realized_jpy:+,.1f} 円)", flush=True)

    def startup_log_line(self) -> str:
        """Returns startup diagnostics string formatted similarly to GAPCORE."""
        with self.lock:
            self._check_day_rollover()
            status = "HALTED" if self.halted else "ACTIVE"
            return (
                f"LIVE_DAILY_LOSS_CONFIG strategy={self.strategy_id} limit_jpy={self.limit_jpy:.0f} "
                f"label={self.label} jst_day={self.jst_day} accumulated_jpy={self.realized_jpy:+.2f} "
                f"halted={self.halted} status={status} state_file={self.state_path}"
            )

    def _check_day_rollover(self) -> None:
        """Checks if JST calendar day has changed; rolls over if so."""
        current_day = get_jst_day_str()
        if self.jst_day != current_day:
            print(
                f"[DailyPnLGuard] 🌅 JST Day Rollover detected ({self.jst_day} -> {current_day}). "
                f"Resetting daily counter (Previous Day: {self.realized_jpy:+,.1f} 円, Halted: {self.halted}).",
                flush=True
            )
            self.jst_day = current_day
            self.realized_jpy = 0.0
            self.halted = False
            self.session_initialized = False
            self._save()

    def _checkpoint(self, session_realized_jpy: float, daily_jpy: float) -> None:
        self.realized_jpy = daily_jpy
        self.session_start_realized = session_realized_jpy
        self._save()

    def get_status(self) -> Dict[str, Any]:
        """Returns snapshot of current guard state."""
        with self.lock:
            self._check_day_rollover()
            return {
                "strategy_id": self.strategy_id,
                "jst_day": self.jst_day,
                "realized_jpy": self.realized_jpy,
                "limit_jpy": self.limit_jpy,
                "remaining_jpy": max(0.0, self.limit_jpy + self.realized_jpy),
                "halted": self.halted,
                "can_enter": not self.halted,
                "state_path": self.state_path,
            }

    def _load(self) -> None:
        """Load state from JSON file if available."""
        current_day = get_jst_day_str()
        if not os.path.exists(self.state_path):
            self.jst_day = current_day
            self.realized_jpy = 0.0
            self.halted = False
            self._save()
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            saved_day = data.get("jst_day", "")
            if saved_day != current_day:
                print(
                    f"[DailyPnLGuard] 🌅 Stored state is from past day ({saved_day} vs current {current_day}). "
                    f"Resetting daily counter to 0.",
                    flush=True
                )
                self.jst_day = current_day
                self.realized_jpy = 0.0
                self.halted = False
            else:
                self.jst_day = saved_day
                self.realized_jpy = float(data.get("realized_jpy", 0.0))
                self.halted = bool(data.get("halted", False))
                if self.halted:
                    print(
                        f"[DailyPnLGuard] ⚠️ [LOAD] RESTORED HALTED STATE from {self.state_path}. "
                        f"Today's loss: {self.realized_jpy:+,.1f} 円 <= -{self.limit_jpy:,.0f} 円. "
                        f"New orders will remain BLOCKED until next JST day or operator manual resume.",
                        flush=True
                    )
                else:
                    print(
                        f"[DailyPnLGuard] ℹ️ [LOAD] Restored daily state: jst_day={self.jst_day}, "
                        f"accumulated={self.realized_jpy:+,.1f} 円, halted={self.halted}",
                        flush=True
                    )
            self._save()
        except Exception as ex:
            print(f"[DailyPnLGuard] ⚠️ Failed to load {self.state_path}: {ex}. Starting fresh for {current_day}.", flush=True)
            self.jst_day = current_day
            self.realized_jpy = 0.0
            self.halted = False
            self._save()

    def _save(self) -> None:
        """Atomically persist state to JSON."""
        self.last_updated_iso = datetime.now(JST).isoformat()
        payload = {
            "jst_day": self.jst_day,
            "realized_jpy": round(self.realized_jpy, 4),
            "halted": self.halted,
            "limit_jpy": self.limit_jpy,
            "strategy_id": self.strategy_id,
            "updated_at": self.last_updated_iso,
        }

        # Write to temporary file in the same directory, then atomic rename
        dir_name = os.path.dirname(self.state_path)
        tmp_file = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                tmp_file = tf.name
                json.dump(payload, tf, ensure_ascii=False, indent=2)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp_file, self.state_path)
        except Exception as ex:
            print(f"[DailyPnLGuard] ⚠️ Error writing state to {self.state_path}: {ex}", flush=True)
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                except Exception:
                    pass
