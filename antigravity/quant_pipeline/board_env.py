"""
Board environment flags (research · CSR-521)
============================================
cancel_spike / fake_breakout / cancel_spike_no_taker
定義は microstructure_hourly_store と揃える。WIRE=NO · ENFORCE は呼び出し側。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def board_env_flags(
    *,
    cancel_rate: float = 0.0,
    refill_rate: float = 0.0,
    taker_volume_bid: float = 0.0,
    taker_volume_ask: float = 0.0,
    taker_aggressiveness: float = 0.0,
    fake_breakout_flag: Optional[bool] = None,
) -> Dict[str, Any]:
    cancel = float(cancel_rate or 0.0)
    refill = float(refill_rate or 0.0)
    cr = cancel - refill
    tb = float(taker_volume_bid or 0.0)
    ta = float(taker_volume_ask or 0.0)
    tagg = float(taker_aggressiveness or 0.0)
    taker_confirm = (tb + ta) >= 0.01
    cancel_spike = cancel >= 0.40 and cr >= 0.15
    fake_bo = (
        bool(fake_breakout_flag)
        if fake_breakout_flag is not None
        else (cancel >= 0.55 and tagg < 0.15)
    )
    csnt = bool(cancel_spike and not taker_confirm)
    return {
        "cancel_spike": bool(cancel_spike),
        "fake_breakout": bool(fake_bo),
        "cancel_spike_no_taker": csnt,
        "cancel_spike_with_taker": bool(cancel_spike and taker_confirm),
        "taker_confirm": bool(taker_confirm),
        "cancel_minus_refill": round(cr, 4),
        # OBSERVE only — would_pause は ENFORCE=0 では実行しない
        "observe_would_pause_csnt": csnt,
        "observe_would_pause_fake_bo": bool(fake_bo),
        "wire": "NO",
        "enforce": 0,
    }
