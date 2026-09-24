"""
Arena signal helpers: closed-bar evaluation, UTC minute alignment, last-signal read.

The dry-run arena polls every few seconds while strategies are 1-minute OHLCV.
Using the in-progress bar as `iloc[-1]` poisons indicators (high==low doji →
fake max-sell delta, ATR collapse) and makes ffill/exit wipe a real ±1 that
already printed on the last *closed* bar.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence, Tuple

import pandas as pd

OHLCV_COLS: Tuple[str, ...] = ("open", "high", "low", "close")
HISTORY_MAX_BARS = 300


def to_utc_ts(ts: Any) -> pd.Timestamp:
    """Normalize any timestamp to UTC-aware. Naive values are treated as UTC (bitFlyer)."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def same_utc_minute(ts_a: Any, ts_b: Any) -> bool:
    return to_utc_ts(ts_a).floor("min") == to_utc_ts(ts_b).floor("min")


def missing_ohlcv_columns(df: Optional[pd.DataFrame], extra: Sequence[str] = ()) -> list:
    if df is None or not isinstance(df, pd.DataFrame):
        return list(OHLCV_COLS)
    need = list(OHLCV_COLS) + list(extra)
    return [c for c in need if c not in df.columns]


def bars_for_signal_eval(df: Optional[pd.DataFrame], now: Any = None) -> pd.DataFrame:
    """
    Drop the in-progress 1-minute bar when it belongs to the current UTC minute.

    Strategies and the arena both read only the last row. Evaluating the forming
    candle makes ±1 on the last closed bar invisible.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df
    if "timestamp" not in df.columns or len(df) < 2:
        return df
    now_ts = to_utc_ts(now) if now is not None else pd.Timestamp.now(tz="UTC")
    last_ts = to_utc_ts(df["timestamp"].iloc[-1])
    if last_ts.floor("min") == now_ts.floor("min"):
        return df.iloc[:-1]
    return df


def read_last_signal(df_sig: Any) -> Tuple[int, str]:
    """
    Read the last `signal` value as -1/0/1.

    Returns (sig, err). err is empty on success. Missing column / NaN / bad dtype
    are errors so the caller can log strat_id instead of silently using 0.
    """
    if df_sig is None or not isinstance(df_sig, pd.DataFrame) or df_sig.empty:
        return 0, "empty_signal_df"
    if "signal" not in df_sig.columns:
        return 0, "missing_signal_column"
    val = df_sig["signal"].iloc[-1]
    if pd.isna(val):
        return 0, "nan_signal"
    try:
        sig = int(val)
    except (TypeError, ValueError):
        return 0, f"invalid_signal:{val!r}"
    if sig not in (-1, 0, 1):
        return 0, f"out_of_range_signal:{sig}"
    return sig, ""


def upsert_minute_bar(
    df: Optional[pd.DataFrame],
    ltp: float,
    now: Any,
    volume: float = 0.01,
    max_bars: int = HISTORY_MAX_BARS,
) -> pd.DataFrame:
    """Update the current UTC 1-minute bar, or append a new one. Timestamps are UTC-aware."""
    bar_ts = to_utc_ts(now).floor("min")
    if df is not None and isinstance(df, pd.DataFrame) and len(df) > 0 and "timestamp" in df.columns:
        if same_utc_minute(df["timestamp"].iloc[-1], bar_ts):
            idx = df.index[-1]
            df.loc[idx, "close"] = ltp
            df.loc[idx, "high"] = max(float(df.loc[idx, "high"]), ltp)
            df.loc[idx, "low"] = min(float(df.loc[idx, "low"]), ltp)
            return df

    new_row = pd.DataFrame([{
        "timestamp": bar_ts,
        "open": ltp,
        "high": ltp,
        "low": ltp,
        "close": ltp,
        "volume": volume,
    }])
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        out = new_row
    else:
        out = pd.concat([df, new_row], ignore_index=True)
    if len(out) > max_bars:
        out = out.iloc[-max_bars:].reset_index(drop=True)
    return out


def candle_close_position(high: Iterable, low: Iterable, close: Iterable):
    """
    Close location in the bar, mapped later to delta in [-1, 1].

    Zero-range bars (doji / forming minute) must return 0.5 so delta is 0.
    The old `.replace(0, 1e-9)` path made close_pos=0 → delta=-1 (fake max sell).
    """
    import numpy as np

    high_a = pd.to_numeric(pd.Series(high), errors="coerce")
    low_a = pd.to_numeric(pd.Series(low), errors="coerce")
    close_a = pd.to_numeric(pd.Series(close), errors="coerce")
    candle_range = (high_a - low_a).to_numpy(dtype=float)
    denom = np.where(candle_range > 0, candle_range, np.nan)
    close_pos = (close_a.to_numpy(dtype=float) - low_a.to_numpy(dtype=float)) / denom
    close_pos = np.where(np.isfinite(close_pos), close_pos, 0.5)
    return close_pos
