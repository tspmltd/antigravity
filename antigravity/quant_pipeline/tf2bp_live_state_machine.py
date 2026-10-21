"""TF2BP LIVE State Machine v1.1 — lifecycle / safety boundary only.

Strategy alpha pins (THR/P2/tn6_be5/buy_only/size) are NOT owned here.
Exchange position is the fact of exposure; local memory is never FLAT proof.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

JST = timezone(timedelta(hours=9))
EPS = 1e-12

DEFAULT_HB_PATH = Path("/home/azureuser/antigravity/data/tf2bp_live_heartbeat.json")
DEFAULT_AUDIT_PATH = Path("/home/azureuser/antigravity/data/mm_research/tf2bp_live_sm_audit.jsonl")
DEFAULT_ENTRY_HALT_FLAG = Path("/home/azureuser/antigravity/data/TF2BP_ENTRY_HALT.flag")


class LiveState(str, Enum):
    BOOT = "BOOT"
    RECONCILING = "RECONCILING"
    READY_FLAT = "READY_FLAT"
    ENTRY_PENDING = "ENTRY_PENDING"
    LONG = "LONG"
    EXIT_PENDING = "EXIT_PENDING"
    ENTRY_HALT = "ENTRY_HALT"
    SAFE_HALT = "SAFE_HALT"
    STOPPED = "STOPPED"


class OrderErrorClass(str, Enum):
    NON_RETRYABLE_ENTRY = "NON_RETRYABLE_ENTRY"
    STATE_FAULT = "STATE_FAULT"
    TRANSIENT = "TRANSIENT"
    ORDER_UNKNOWN = "ORDER_UNKNOWN"


class InvalidTransition(RuntimeError):
    pass


# Spec v1.1 allowed transitions (+ STOPPED / ENTRY_HALT from RECONCILING)
ALLOWED_TRANSITIONS: Dict[LiveState, Set[LiveState]] = {
    LiveState.BOOT: {LiveState.RECONCILING},
    LiveState.RECONCILING: {
        LiveState.READY_FLAT,
        LiveState.LONG,
        LiveState.SAFE_HALT,
        LiveState.ENTRY_HALT,
    },
    LiveState.READY_FLAT: {
        LiveState.ENTRY_PENDING,
        LiveState.ENTRY_HALT,
        LiveState.RECONCILING,
        LiveState.SAFE_HALT,
        LiveState.STOPPED,
    },
    LiveState.ENTRY_PENDING: {
        LiveState.LONG,
        LiveState.RECONCILING,
        LiveState.ENTRY_HALT,
        LiveState.SAFE_HALT,
    },
    LiveState.LONG: {
        LiveState.EXIT_PENDING,
        LiveState.ENTRY_HALT,
        LiveState.RECONCILING,
        LiveState.SAFE_HALT,
    },
    LiveState.EXIT_PENDING: {
        LiveState.READY_FLAT,
        LiveState.LONG,
        LiveState.RECONCILING,
        LiveState.SAFE_HALT,
        LiveState.ENTRY_HALT,
    },
    LiveState.ENTRY_HALT: {
        LiveState.RECONCILING,
        LiveState.LONG,
        LiveState.SAFE_HALT,
        LiveState.STOPPED,
    },
    LiveState.SAFE_HALT: {LiveState.STOPPED},  # operator stop only; no auto-trade recovery
    LiveState.STOPPED: set(),
}

_NO_KILL_STATES = frozenset(
    {
        LiveState.LONG,
        LiveState.EXIT_PENDING,
        LiveState.ENTRY_PENDING,
        LiveState.RECONCILING,
        LiveState.SAFE_HALT,
        LiveState.ENTRY_HALT,
    }
)


def _jst() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def classify_order_error(exc: BaseException) -> OrderErrorClass:
    """Classify exchange/order exceptions from observed client formats."""
    # Observed LIVE log form:
    # bitFlyer APIエラー (400): {"status":-205,"error_message":"Margin amount is insufficient for this order.","data":null}
    msg = str(exc)
    low = msg.lower()
    name = type(exc).__name__

    if name in ("OrderSubmitUnknownError",) or "ORDER_UNKNOWN" in msg or "order_submit_unknown" in low:
        return OrderErrorClass.ORDER_UNKNOWN

    if (
        "Margin amount is insufficient" in msg
        or '"status":-205' in msg
        or "status\":-205" in msg
        or ("-205" in msg and "margin" in low)
    ):
        return OrderErrorClass.NON_RETRYABLE_ENTRY
    if "exposure" in low and ("violation" in low or "exceed" in low):
        return OrderErrorClass.NON_RETRYABLE_ENTRY
    if "timeout" in low or "timed out" in low or isinstance(exc, TimeoutError):
        return OrderErrorClass.ORDER_UNKNOWN
    if "position" in low and ("unknown" in low or "mismatch" in low or "reconcile" in low):
        return OrderErrorClass.STATE_FAULT
    return OrderErrorClass.TRANSIENT


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hb_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class VenueSnapshot:
    net: Optional[float]
    avg_entry: float = 0.0
    open_orders_known: bool = False
    open_order_count: int = 0
    ok: bool = False
    error: str = ""

    def is_known_flat(self) -> bool:
        return self.ok and self.net is not None and abs(self.net) <= EPS

    def is_known_long(self) -> bool:
        return self.ok and self.net is not None and self.net > EPS

    def is_unknown(self) -> bool:
        return (not self.ok) or self.net is None


@dataclass
class OrderLookupResult:
    """Result of child-order / open-order inquiry after ORDER_UNKNOWN."""

    known: bool
    filled: bool = False
    rejected_or_absent: bool = False
    acceptance_id: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TransitionRecord:
    ts_jst: str
    run_id: str
    session_id: str
    state_before: str
    event: str
    state_after: str
    local_position: float
    venue_position: Optional[float]
    detail: Dict[str, Any] = field(default_factory=dict)


class TF2BPLiveStateMachine:
    def __init__(
        self,
        *,
        max_size: float = 0.001,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        heartbeat_path: Optional[Path] = None,
        audit_path: Optional[Path] = None,
        entry_halt_flag_path: Optional[Path] = None,
        process_alive: bool = True,
        pid: Optional[int] = None,
    ) -> None:
        self.max_size = float(max_size)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.session_id = session_id or datetime.now(JST).strftime("%Y%m%dT%H%M%S")
        self.heartbeat_path = Path(heartbeat_path or DEFAULT_HB_PATH)
        self.audit_path = Path(audit_path or DEFAULT_AUDIT_PATH)
        self.entry_halt_flag_path = Path(entry_halt_flag_path or DEFAULT_ENTRY_HALT_FLAG)
        self.pid = int(pid if pid is not None else os.getpid())

        self.state = LiveState.BOOT
        self.entry_allowed = False
        self.entry_halt = False
        self.reconcile_completed = False
        self.local_position = 0.0
        self.venue_position: Optional[float] = None
        self.entry_price = 0.0
        self.last_order_id: Optional[str] = None
        self.last_order_side: Optional[str] = None
        self.process_alive = process_alive
        self.audit_log: List[TransitionRecord] = []
        self.max_transient_attempts = 3
        self.pending_since: Optional[float] = None
        self.hb_write_ok = True
        self.critical_persist_ok = True

        self._audit("BOOT", detail={"entry_allowed": False, "pid": self.pid})
        self.write_heartbeat(fail_closed=False)

    # ----- transition / audit -----

    def _allowed(self, new_state: LiveState) -> bool:
        if new_state == self.state:
            return True
        return new_state in ALLOWED_TRANSITIONS.get(self.state, set())

    def _transition(
        self,
        event: str,
        new_state: LiveState,
        detail: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> None:
        if not self._allowed(new_state):
            bad = {
                "reason": "invalid_transition",
                "from": self.state.value,
                "to": new_state.value,
                "event": event,
                **(detail or {}),
            }
            self._audit("INVALID_TRANSITION", detail=bad)
            raise InvalidTransition(
                f"invalid transition {self.state.value} -> {new_state.value} on {event}"
            )
        before = self.state
        self.state = new_state
        self._audit(
            event,
            state_before=before,
            state_after=new_state,
            detail=detail,
            **extra,
        )
        if new_state in (LiveState.ENTRY_PENDING, LiveState.EXIT_PENDING):
            self.pending_since = time.time()
        elif before in (LiveState.ENTRY_PENDING, LiveState.EXIT_PENDING):
            self.pending_since = None
        ok = self.write_heartbeat(fail_closed=True)
        if not ok and self.state not in (LiveState.SAFE_HALT, LiveState.STOPPED):
            # fail-closed: cannot prove liveness identity → SAFE_HALT if allowed
            if self._allowed(LiveState.SAFE_HALT):
                self.state = LiveState.SAFE_HALT
                self.entry_allowed = False
                self._audit("SAFE_HALT", detail={"reason": "heartbeat_write_failed"})

    def _audit(
        self,
        event: str,
        *,
        state_before: Optional[LiveState] = None,
        state_after: Optional[LiveState] = None,
        detail: Optional[Dict[str, Any]] = None,
        strategy_signal: Optional[str] = None,
        order_side: Optional[str] = None,
        order_qty: Optional[float] = None,
        order_id: Optional[str] = None,
        order_result: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        before = state_before if state_before is not None else self.state
        after = state_after if state_after is not None else self.state
        row = TransitionRecord(
            ts_jst=_jst(),
            run_id=self.run_id,
            session_id=self.session_id,
            state_before=before.value if isinstance(before, LiveState) else str(before),
            event=event,
            state_after=after.value if isinstance(after, LiveState) else str(after),
            local_position=float(self.local_position),
            venue_position=self.venue_position,
            detail=dict(detail or {}),
        )
        row.detail.setdefault("pid", self.pid)
        if strategy_signal is not None:
            row.detail["strategy_signal"] = strategy_signal
        if order_side is not None:
            row.detail["order_side"] = order_side
        if order_qty is not None:
            row.detail["order_qty"] = order_qty
        if order_id is not None:
            row.detail["order_id"] = order_id
        if order_result is not None:
            row.detail["order_result"] = order_result
        if error_code is not None:
            row.detail["error_code"] = error_code
        self.audit_log.append(row)
        self._append_audit(row)

    def _append_audit(self, row: TransitionRecord) -> None:
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts_jst": row.ts_jst,
                "run_id": row.run_id,
                "session_id": row.session_id,
                "pid": self.pid,
                "state_before": row.state_before,
                "event": row.event,
                "state_after": row.state_after,
                "local_position": row.local_position,
                "venue_position": row.venue_position,
                **row.detail,
            }
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:
            self.critical_persist_ok = False
            print(f"[TF2BP-SM] CRITICAL audit write failed: {exc}", flush=True)

    def write_heartbeat(self, *, fail_closed: bool = True) -> bool:
        payload = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "pid": self.pid,
            "timestamp": time.time(),
            "ts_jst": _jst(),
            "state": self.state.value,
            "process_alive": bool(self.process_alive),
            "entry_halt": bool(self.entry_halt),
            "entry_allowed": bool(self.entry_allowed),
            "local_position": float(self.local_position),
            "venue_position": self.venue_position,
            "reconcile_completed": bool(self.reconcile_completed),
        }
        try:
            atomic_write_json(self.heartbeat_path, payload)
            self.hb_write_ok = True
            return True
        except Exception as exc:
            self.hb_write_ok = False
            print(f"[TF2BP-SM] CRITICAL heartbeat write failed: {exc}", flush=True)
            if fail_closed:
                self.entry_allowed = False
            return False

    # ----- boot / reconcile -----

    def begin_reconcile(self) -> None:
        self.entry_allowed = False
        self.reconcile_completed = False
        self._transition("RECONCILE", LiveState.RECONCILING, {"entry_allowed": False})

    def apply_reconcile(self, snap: VenueSnapshot) -> LiveState:
        if self.state != LiveState.RECONCILING:
            self.begin_reconcile()

        self.venue_position = snap.net if snap.ok else None

        if snap.is_unknown() or not snap.open_orders_known:
            self.entry_allowed = False
            self.reconcile_completed = False
            self._transition(
                "SAFE_HALT",
                LiveState.SAFE_HALT,
                {
                    "reason": "reconcile_failure_or_unknown",
                    "error": snap.error,
                    "open_orders_known": snap.open_orders_known,
                },
            )
            return self.state

        assert snap.net is not None
        net = float(snap.net)

        if net < -EPS or net > self.max_size + EPS:
            self.entry_allowed = False
            self.reconcile_completed = False
            self.local_position = net
            reason = "unexpected_short" if net < -EPS else "unexpected_exposure_oversize"
            self._transition(
                "SAFE_HALT",
                LiveState.SAFE_HALT,
                {"reason": reason, "venue_net": net, "max_size": self.max_size},
            )
            return self.state

        if snap.open_order_count > 0:
            self.entry_allowed = False
            self.reconcile_completed = False
            self._transition(
                "SAFE_HALT",
                LiveState.SAFE_HALT,
                {"reason": "open_orders_present", "open_order_count": snap.open_order_count},
            )
            return self.state

        if abs(net) <= EPS:
            self.local_position = 0.0
            self.venue_position = 0.0
            self.entry_price = 0.0
            self.reconcile_completed = True
            if self.entry_halt:
                self.entry_allowed = False
                self._transition("RECONCILE", LiveState.ENTRY_HALT, {"reason": "flat_but_entry_halt"})
            else:
                self.entry_allowed = True
                self._transition("RECONCILE", LiveState.READY_FLAT, {"reason": "venue_flat"})
            return self.state

        self.local_position = net
        self.venue_position = net
        self.entry_price = float(snap.avg_entry or 0.0)
        self.reconcile_completed = True
        self.entry_allowed = False
        self._transition(
            "RECONCILE",
            LiveState.LONG,
            {"reason": "adopt_venue_long", "additional_buy": "BLOCK"},
        )
        return self.state

    # ----- ENTRY gate -----

    def can_submit_buy(
        self,
        *,
        strategy_action: str,
        venue_recheck: VenueSnapshot,
        risk_pass: bool = True,
        conflicting_open_orders: bool = False,
    ) -> Tuple[bool, str]:
        if strategy_action != "buy":
            return False, "not_buy_signal"

        if self.entry_halt_flag_path.exists():
            self.entry_halt = True

        if self.entry_halt:
            self._audit("ENTRY_BLOCK", detail={"reason": "entry_halt"}, strategy_signal="buy")
            if self.state == LiveState.READY_FLAT:
                self.entry_allowed = False
                try:
                    self._transition("ENTRY_HALT", LiveState.ENTRY_HALT, {"reason": "entry_halt"})
                except InvalidTransition:
                    pass
            return False, "entry_halt"

        if self.state != LiveState.READY_FLAT:
            self._audit(
                "ENTRY_BLOCK",
                detail={"reason": f"state_{self.state.value}"},
                strategy_signal="buy",
            )
            return False, f"state_{self.state.value}"

        if not self.reconcile_completed:
            self._audit("ENTRY_BLOCK", detail={"reason": "reconcile_incomplete"}, strategy_signal="buy")
            return False, "reconcile_incomplete"

        if self.venue_position is None or abs(float(self.venue_position)) > EPS:
            self._audit(
                "ENTRY_BLOCK",
                detail={"reason": "sm_venue_not_flat", "venue_position": self.venue_position},
                strategy_signal="buy",
            )
            return False, "sm_venue_not_flat"

        if conflicting_open_orders:
            self._audit("ENTRY_BLOCK", detail={"reason": "open_orders"}, strategy_signal="buy")
            return False, "open_orders"

        if not risk_pass:
            self._audit("ENTRY_BLOCK", detail={"reason": "risk_gate"}, strategy_signal="buy")
            return False, "risk_gate"

        if venue_recheck.is_unknown():
            self.entry_allowed = False
            self.reconcile_completed = False
            self._transition(
                "STATE_FAULT",
                LiveState.SAFE_HALT,
                {"reason": "venue_recheck_unknown", "error": venue_recheck.error},
            )
            return False, "venue_recheck_unknown"

        if not venue_recheck.is_known_flat():
            # No READY_FLAT→LONG shortcut: re-enter RECONCILING
            self.entry_allowed = False
            self._audit(
                "ENTRY_BLOCK",
                detail={"reason": "venue_non_flat_reconciliation", "venue_net": venue_recheck.net},
                strategy_signal="buy",
            )
            self.begin_reconcile()
            self.apply_reconcile(venue_recheck)
            return False, "venue_non_flat"

        return True, "ok"

    def mark_entry_pending(self, *, order_qty: float, order_id: Optional[str] = None) -> None:
        self.entry_allowed = False
        self.last_order_id = order_id
        self.last_order_side = "BUY"
        self._transition(
            "ORDER_SUBMIT",
            LiveState.ENTRY_PENDING,
            {"phase": "buy"},
            strategy_signal="buy",
            order_side="BUY",
            order_qty=order_qty,
            order_id=order_id,
        )

    def on_entry_confirmed(self, venue: VenueSnapshot, *, order_id: Optional[str] = None) -> LiveState:
        if venue.is_unknown():
            return self.enter_order_unknown(detail={"reason": "post_buy_venue_unknown"}, order_id=order_id)
        assert venue.net is not None
        if venue.net > self.max_size + EPS:
            self.local_position = venue.net
            self.venue_position = venue.net
            self._transition(
                "SAFE_HALT",
                LiveState.SAFE_HALT,
                {"reason": "oversize_after_buy", "venue_net": venue.net},
                order_id=order_id,
                order_result="oversize",
            )
            return self.state
        if venue.net > EPS:
            self.local_position = venue.net
            self.venue_position = venue.net
            self.entry_price = float(venue.avg_entry or self.entry_price)
            self._transition(
                "FILL",
                LiveState.LONG,
                {"side": "BUY"},
                order_side="BUY",
                order_id=order_id,
                order_result="filled_long",
            )
            return self.state
        # still flat — treat as unknown (may be lag); recovery path, no re-BUY assumption
        return self.enter_order_unknown(
            detail={"reason": "buy_submitted_venue_still_flat"},
            order_id=order_id,
        )

    # ----- ORDER_UNKNOWN recovery -----

    def enter_order_unknown(
        self,
        *,
        detail: Optional[Dict[str, Any]] = None,
        order_id: Optional[str] = None,
    ) -> LiveState:
        """Mark unknown; stay in ENTRY_PENDING/EXIT_PENDING when possible for recovery."""
        self.entry_allowed = False
        d = dict(detail or {"reason": "order_result_unknown"})
        if order_id:
            self.last_order_id = order_id
            d["order_id"] = order_id
        # Prefer staying pending so recover_order_unknown can run
        if self.state == LiveState.ENTRY_PENDING:
            self._audit("ORDER_UNKNOWN", detail=d, order_id=order_id)
            return self.state
        if self.state == LiveState.EXIT_PENDING:
            self._audit("ORDER_UNKNOWN", detail=d, order_id=order_id)
            return self.state
        # From other states → SAFE_HALT
        if self._allowed(LiveState.SAFE_HALT) or self.state == LiveState.SAFE_HALT:
            if self.state != LiveState.SAFE_HALT:
                self._transition("ORDER_UNKNOWN", LiveState.SAFE_HALT, d, order_id=order_id)
            else:
                self._audit("ORDER_UNKNOWN", detail=d, order_id=order_id)
        return self.state

    def recover_order_unknown(
        self,
        *,
        venue: VenueSnapshot,
        order_lookup: OrderLookupResult,
        expected_side: str = "BUY",
    ) -> LiveState:
        """Resolve ORDER_UNKNOWN via order + open-orders + venue. Never re-POST."""
        self.entry_allowed = False
        self._audit(
            "ORDER_UNKNOWN_RECOVERY",
            detail={
                "order_known": order_lookup.known,
                "filled": order_lookup.filled,
                "rejected_or_absent": order_lookup.rejected_or_absent,
                "venue_ok": venue.ok,
                "venue_net": venue.net,
                "open_orders_known": venue.open_orders_known,
                "open_order_count": venue.open_order_count,
                "expected_side": expected_side,
            },
        )

        # Case A — venue shows expected LONG after BUY
        if expected_side == "BUY" and venue.is_known_long() and venue.open_orders_known:
            if 0 < float(venue.net or 0) <= self.max_size + EPS and venue.open_order_count == 0:
                self.local_position = float(venue.net or 0)
                self.venue_position = self.local_position
                self.entry_price = float(venue.avg_entry or self.entry_price)
                if self.state == LiveState.ENTRY_PENDING:
                    self._transition("FILL", LiveState.LONG, {"reason": "unknown_recovery_filled"})
                else:
                    self.begin_reconcile()
                    self.apply_reconcile(venue)
                return self.state

        # Case A' — EXIT: venue flat
        if expected_side == "SELL" and venue.is_known_flat() and venue.open_orders_known:
            if venue.open_order_count == 0:
                self.local_position = 0.0
                self.venue_position = 0.0
                if self.entry_halt:
                    self._transition("FILL", LiveState.ENTRY_HALT, {"reason": "exit_recovery_flat"})
                else:
                    self.entry_allowed = True
                    self.reconcile_completed = True
                    self._transition("FILL", LiveState.READY_FLAT, {"reason": "exit_recovery_flat"})
                return self.state

        # Case B — clearly not filled, venue flat, no open orders
        if (
            order_lookup.known
            and order_lookup.rejected_or_absent
            and not order_lookup.filled
            and venue.is_known_flat()
            and venue.open_orders_known
            and venue.open_order_count == 0
        ):
            self.begin_reconcile()
            self.apply_reconcile(venue)
            return self.state

        # Case B' — EXIT residual long with no open orders → LONG
        if (
            expected_side == "SELL"
            and venue.is_known_long()
            and venue.open_orders_known
            and venue.open_order_count == 0
        ):
            self.local_position = float(venue.net or 0)
            self.venue_position = self.local_position
            if self.state == LiveState.EXIT_PENDING:
                self._transition("RECONCILE", LiveState.LONG, {"reason": "exit_incomplete_adopt"})
            else:
                self.begin_reconcile()
                self.apply_reconcile(venue)
            return self.state

        # Case C — cannot decide uniquely
        self._transition(
            "SAFE_HALT",
            LiveState.SAFE_HALT,
            {"reason": "order_unknown_unresolved", **(order_lookup.detail or {})},
        )
        return self.state

    # ----- EXIT -----

    def can_submit_exit(self, *, strategy_action: str) -> Tuple[bool, str]:
        if strategy_action != "exit":
            return False, "not_exit_signal"
        if self.state == LiveState.LONG:
            if self.venue_position is not None and self.venue_position <= EPS and self.local_position <= EPS:
                return False, "not_long"
            return True, "ok"
        if self.state == LiveState.ENTRY_HALT and (
            (self.venue_position is not None and self.venue_position > EPS) or self.local_position > EPS
        ):
            return True, "ok_halt_exit"
        return False, f"state_{self.state.value}"

    def mark_exit_pending(self, *, order_qty: float, order_id: Optional[str] = None) -> None:
        self.last_order_id = order_id
        self.last_order_side = "SELL"
        # ENTRY_HALT → EXIT_PENDING not in table; go LONG first if halt+long then exit
        if self.state == LiveState.ENTRY_HALT:
            self._transition("RECONCILE", LiveState.LONG, {"reason": "halt_to_long_for_exit"})
        self._transition(
            "ORDER_SUBMIT",
            LiveState.EXIT_PENDING,
            {"phase": "sell"},
            strategy_signal="exit",
            order_side="SELL",
            order_qty=order_qty,
            order_id=order_id,
        )

    def on_exit_venue_check(self, venue: VenueSnapshot) -> LiveState:
        if venue.is_unknown():
            return self.enter_order_unknown(detail={"reason": "post_exit_venue_unknown"})
        assert venue.net is not None
        self.venue_position = venue.net
        if abs(venue.net) <= EPS:
            self.local_position = 0.0
            self.venue_position = 0.0
            self.entry_price = 0.0
            if self.entry_halt:
                self.entry_allowed = False
                self._transition("FILL", LiveState.ENTRY_HALT, {"side": "SELL", "flat": True})
            else:
                self.entry_allowed = True
                self.reconcile_completed = True
                self._transition("FILL", LiveState.READY_FLAT, {"side": "SELL", "flat": True})
            return self.state
        # residual long — back to LONG (not FLAT)
        self.local_position = venue.net
        self._transition(
            "RECONCILE",
            LiveState.LONG,
            {"reason": "exit_incomplete_residual", "venue_net": venue.net},
        )
        return self.state

    def pending_needs_recovery(self, *, timeout_sec: float = 15.0) -> bool:
        if self.state not in (LiveState.ENTRY_PENDING, LiveState.EXIT_PENDING):
            return False
        if self.pending_since is None:
            return True
        return (time.time() - self.pending_since) >= timeout_sec

    # ----- halt / stop -----

    def on_margin_insufficient(self, *, venue: Optional[VenueSnapshot] = None) -> LiveState:
        self.entry_halt = True
        self.entry_allowed = False
        wrote = self._write_entry_halt_flag(reason="-205")
        if not wrote:
            # fail-closed: do not claim halt; force SAFE_HALT
            self._transition(
                "SAFE_HALT",
                LiveState.SAFE_HALT,
                {"reason": "entry_halt_flag_write_failed", "error_code": "-205"},
                error_code="-205",
            )
            return self.state
        if venue is not None and venue.is_known_long():
            self.local_position = float(venue.net or 0.0)
            self.venue_position = self.local_position
            # sticky halt while LONG: entry_halt flag; state stays LONG (ENTRY_HALT event as audit)
            if self.state == LiveState.READY_FLAT:
                self._transition("ENTRY_HALT", LiveState.ENTRY_HALT, {"reason": "margin_-205"}, error_code="-205")
            elif self.state == LiveState.ENTRY_PENDING:
                self._transition("ENTRY_HALT", LiveState.ENTRY_HALT, {"reason": "margin_-205"}, error_code="-205")
            else:
                self._audit("ENTRY_HALT", detail={"reason": "margin_-205", "sticky": True}, error_code="-205")
        else:
            if self.state == LiveState.ENTRY_PENDING:
                self._transition("ENTRY_HALT", LiveState.ENTRY_HALT, {"reason": "margin_-205"}, error_code="-205")
            elif self.state == LiveState.READY_FLAT:
                self._transition("ENTRY_HALT", LiveState.ENTRY_HALT, {"reason": "margin_-205"}, error_code="-205")
            elif self.state == LiveState.RECONCILING:
                self._transition("ENTRY_HALT", LiveState.ENTRY_HALT, {"reason": "margin_-205"}, error_code="-205")
            else:
                self._audit("ENTRY_HALT", detail={"reason": "margin_-205"}, error_code="-205")
        return self.state

    def _write_entry_halt_flag(self, *, reason: str) -> bool:
        try:
            self.entry_halt_flag_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                self.entry_halt_flag_path,
                {
                    "ts_jst": _jst(),
                    "run_id": self.run_id,
                    "pid": self.pid,
                    "reason": reason,
                },
            )
            self.critical_persist_ok = True
            return True
        except Exception as exc:
            self.critical_persist_ok = False
            print(f"[TF2BP-SM] CRITICAL entry_halt flag write failed: {exc}", flush=True)
            return False

    def on_state_fault(self, reason: str) -> LiveState:
        self.entry_allowed = False
        self.reconcile_completed = False
        if self.state != LiveState.SAFE_HALT:
            self._transition("STATE_FAULT", LiveState.SAFE_HALT, {"reason": reason})
        return self.state

    def on_entry_halt(self, reason: str = "operator") -> LiveState:
        """Phase 1: sticky ENTRY_HALT (e.g. consecutive_429). Does not flatten."""
        self.entry_halt = True
        self.entry_allowed = False
        wrote = self._write_entry_halt_flag(reason=reason)
        if not wrote:
            if self._allowed(LiveState.SAFE_HALT) and self.state != LiveState.SAFE_HALT:
                self._transition(
                    "SAFE_HALT",
                    LiveState.SAFE_HALT,
                    {"reason": "entry_halt_flag_write_failed", "source": reason},
                )
            return self.state
        if self.state == LiveState.READY_FLAT:
            self._transition("ENTRY_HALT", LiveState.ENTRY_HALT, {"reason": reason})
        elif self.state == LiveState.RECONCILING:
            self._transition("ENTRY_HALT", LiveState.ENTRY_HALT, {"reason": reason})
        else:
            self._audit("ENTRY_HALT", detail={"reason": reason, "sticky": True})
        return self.state

    def request_entry_block_from_watchdog(self, reason: str = "heartbeat_abnormal") -> bool:
        """Returns True only if halt flag persisted. Caller must not assume halt otherwise."""
        self.entry_halt = False  # not sticky until flag write succeeds
        wrote = self._write_entry_halt_flag(reason=reason)
        if not wrote:
            self.entry_allowed = False
            if self._allowed(LiveState.SAFE_HALT):
                self._transition(
                    "SAFE_HALT",
                    LiveState.SAFE_HALT,
                    {"reason": "watchdog_halt_flag_write_failed", "source": "watchdog"},
                )
            return False
        self.entry_halt = True
        self.entry_allowed = False
        if self.state == LiveState.READY_FLAT:
            self._transition("ENTRY_HALT", LiveState.ENTRY_HALT, {"reason": reason, "source": "watchdog"})
        else:
            self._audit("ENTRY_HALT", detail={"reason": reason, "source": "watchdog", "sticky": True})
        return True

    def stop(self, reason: str = "operator") -> LiveState:
        self.entry_allowed = False
        if self.venue_position is not None and abs(self.venue_position) > EPS:
            if self.state != LiveState.SAFE_HALT:
                if self._allowed(LiveState.SAFE_HALT):
                    self._transition(
                        "SAFE_HALT",
                        LiveState.SAFE_HALT,
                        {"reason": f"stop_while_positioned:{reason}"},
                    )
            return self.state
        if self.state != LiveState.STOPPED and self._allowed(LiveState.STOPPED):
            self._transition("STOP", LiveState.STOPPED, {"reason": reason})
        elif self.state == LiveState.SAFE_HALT:
            self._transition("STOP", LiveState.STOPPED, {"reason": reason})
        return self.state


def read_heartbeat(path: Path | str) -> Optional[Dict[str, Any]]:
    try:
        p = Path(path)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def heartbeat_belongs_to_runner(
    hb: Optional[Dict[str, Any]],
    *,
    expected_pid: Optional[int] = None,
    expected_run_id: Optional[str] = None,
) -> bool:
    if not hb:
        return False
    if expected_pid is not None and int(hb.get("pid") or -1) != int(expected_pid):
        return False
    if expected_run_id is not None and str(hb.get("run_id") or "") != str(expected_run_id):
        return False
    return True


def watchdog_should_sigterm_on_hb_stall(hb: Optional[Dict[str, Any]] = None) -> bool:
    return False


def watchdog_hb_implies_positioned(hb: Optional[Dict[str, Any]]) -> bool:
    if not hb:
        return False
    state = str(hb.get("state") or "")
    if state in {s.value for s in _NO_KILL_STATES}:
        return True
    try:
        vp = hb.get("venue_position")
        if vp is not None and abs(float(vp)) > EPS:
            return True
        lp = hb.get("local_position")
        if lp is not None and abs(float(lp)) > EPS:
            return True
    except (TypeError, ValueError):
        pass
    return False
