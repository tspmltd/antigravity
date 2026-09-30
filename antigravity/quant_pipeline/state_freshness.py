"""CSR-564 — State Freshness Acceptance Gate (OBSERVE / teacher hygiene).

P0 Acceptance Gate (before any signal-accuracy / Economic PASS):
  1. freshness_ok   — state_age_sec ≤ max_age
  2. no STALE_SCHEMA / missing candidate|decision
  3. duplicate / identical-signature freeze detection
  4. teach blackout for known stale windows (CSR-563 03–07h)

Purpose: stop teaching on frozen snapshots so forward-mid / FakeBO CF /
confidence calibration are not polluted by republished stale state.

WIRE=NO · ENFORCE=0 · LIVE=OFF · does not change execution.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

JST = timezone(timedelta(hours=9))

DEFAULT_MAX_AGE_SEC = 120.0
# Identical signature across ≥ this many consecutive hourly samples → STALE_SNAPSHOT
IDENTICAL_STREAK_FAIL = 3

TEACH_BLACKOUT_WINDOWS_JST = (
    (
        datetime(2026, 10, 1, 3, 0, 0, tzinfo=JST),
        datetime(2026, 10, 1, 8, 0, 0, tzinfo=JST),
    ),
)

# Fields that should move when the market/pipeline is alive
SIGNATURE_FIELDS = (
    "candidate_action",
    "trend_strength",
    "pressure_side",
    "pressure_score",
    "decision_action",
    "micro_state",
    "veto",
)


def state_age_sec(state: Dict[str, Any], now: Optional[datetime] = None) -> Optional[float]:
    """Age of council state timestamp (ms epoch) vs now. None if unreadable."""
    now = now or datetime.now(JST)
    ts = state.get("state_write_ts_ms")
    if ts is None:
        ts = state.get("timestamp")
    if ts is None:
        return None
    try:
        ts_f = float(ts)
        if ts_f > 1e12:
            ts_f /= 1000.0
        return max(0.0, now.timestamp() - ts_f)
    except (TypeError, ValueError):
        return None


def freshness_ok(
    state: Dict[str, Any],
    *,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    now: Optional[datetime] = None,
) -> Tuple[bool, Optional[float], str]:
    age = state_age_sec(state, now=now)
    if age is None:
        return False, None, "MISSING_TIMESTAMP"
    if age > max_age_sec:
        return False, age, f"STALE_STATE age_sec={age:.1f}>{max_age_sec}"
    return True, age, "FRESH"


def signature_fingerprint(state: Dict[str, Any]) -> str:
    """Compact fingerprint of decision-relevant feature values (stale-identical detector)."""
    parts: List[str] = []
    for k in SIGNATURE_FIELDS:
        v = state.get(k)
        if isinstance(v, float):
            parts.append(f"{k}={v:.4f}")
        else:
            parts.append(f"{k}={v}")
    # optional micro metrics often frozen in the 03–07h incident
    conclusions = state.get("conclusions") or {}
    micro = (conclusions.get("MicrostructureAgent") or {}).get("metrics") or {}
    trend = (conclusions.get("TrendFollowAgent") or {}).get("metrics") or {}
    for src, keys in (
        (micro, ("imbalance", "micro_dev")),
        (trend, ("price_delta", "trend_strength")),
    ):
        for k in keys:
            if k in src:
                try:
                    parts.append(f"{k}={float(src[k]):.4f}")
                except (TypeError, ValueError):
                    parts.append(f"{k}={src[k]}")
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def in_teach_blackout(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(JST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=JST)
    else:
        now = now.astimezone(JST)
    for start, end in TEACH_BLACKOUT_WINDOWS_JST:
        if start <= now < end:
            return True
    return False


def identical_streak(fingerprints: Sequence[str]) -> int:
    """Length of trailing identical fingerprints (0 if empty)."""
    if not fingerprints:
        return 0
    last = fingerprints[-1]
    n = 0
    for fp in reversed(fingerprints):
        if fp != last:
            break
        n += 1
    return n


def teacher_valid(
    state: Dict[str, Any],
    *,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Per-row teacher gate. final_action alone is never sufficient."""
    now = now or datetime.now(JST)
    max_age = float(state.get("freshness_max_age_sec") or max_age_sec)
    schema_v = str(state.get("schema_version") or "LEGACY_PRE_CSR531")
    stale_schema = ("candidate_action" not in state) or schema_v.startswith("LEGACY")
    ok, age, reason = freshness_ok(state, max_age_sec=max_age, now=now)
    blackout = in_teach_blackout(now)
    candidate_native = "candidate_action" in state and not stale_schema
    decision_native = "decision_action" in state and not stale_schema
    fp = signature_fingerprint(state)
    valid = (
        ok
        and (not stale_schema)
        and candidate_native
        and decision_native
        and (not blackout)
    )
    fail_reasons: List[str] = []
    if not ok:
        fail_reasons.append(reason)
    if stale_schema:
        fail_reasons.append("STALE_SCHEMA")
    if not candidate_native:
        fail_reasons.append("CANDIDATE_NOT_NATIVE")
    if not decision_native:
        fail_reasons.append("DECISION_NOT_NATIVE")
    if blackout:
        fail_reasons.append("TEACH_BLACKOUT_CSR563")
    return {
        "teacher_valid": valid,
        "freshness_ok": ok,
        "state_age_sec": age,
        "freshness_reason": reason,
        "stale_schema": stale_schema,
        "schema_version": schema_v,
        "teach_blackout": blackout,
        "signature_fp": fp,
        "fail_reasons": fail_reasons,
        "max_age_sec": max_age,
        "final_alone_forbidden": True,
        "observe_mode": True,
        "wire": "NO",
        "enforce": 0,
    }


def acceptance_gate(
    samples: Sequence[Dict[str, Any]],
    *,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    identical_fail: int = IDENTICAL_STREAK_FAIL,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """CSR-564 Acceptance Gate over a window of council/hourly samples.

    PASS only if:
      - every sample freshness_ok (or empty → FAIL EMPTY)
      - teacher_valid rate == 1.0 for native schema rows
      - trailing identical signature streak < identical_fail
      - distinct signature count ≥ 2 when n ≥ identical_fail
    Signal accuracy / Economic PASS are OUT OF SCOPE until this passes.
    """
    now = now or datetime.now(JST)
    if not samples:
        return {
            "gate": "FAIL",
            "reason": "EMPTY",
            "n": 0,
            "freshness_rate": 0.0,
            "teacher_valid_rate": 0.0,
            "identical_streak": 0,
            "distinct_sig_n": 0,
            "economic_pass_allowed": False,
            "signal_accuracy_allowed": False,
        }

    tvs = [teacher_valid(s, max_age_sec=max_age_sec, now=now) for s in samples]
    fps = [t["signature_fp"] for t in tvs]
    n = len(tvs)
    fresh_n = sum(1 for t in tvs if t["freshness_ok"])
    teach_n = sum(1 for t in tvs if t["teacher_valid"])
    streak = identical_streak(fps)
    distinct = len(set(fps))
    fail: List[str] = []
    if fresh_n < n:
        fail.append(f"FRESHNESS_RATE={fresh_n}/{n}")
    if teach_n < n:
        fail.append(f"TEACHER_VALID_RATE={teach_n}/{n}")
    if streak >= identical_fail:
        fail.append(f"IDENTICAL_STREAK={streak}>={identical_fail}")
    if n >= identical_fail and distinct < 2:
        fail.append(f"DISTINCT_SIG={distinct}<2")

    gate = "PASS" if not fail else "FAIL"
    return {
        "gate": gate,
        "reason": "OK" if gate == "PASS" else ";".join(fail),
        "n": n,
        "freshness_rate": round(fresh_n / n, 4),
        "teacher_valid_rate": round(teach_n / n, 4),
        "identical_streak": streak,
        "distinct_sig_n": distinct,
        "fail_reasons": fail,
        "economic_pass_allowed": False,  # always False until explicit later CSR
        "signal_accuracy_allowed": gate == "PASS",
        "observe_mode": True,
        "wire": "NO",
        "enforce": 0,
        "core": (
            "Acceptance Gate = freshness + duplicate + stale-snapshot only; "
            "not signal accuracy; not LIVE/WIRE."
        ),
    }
