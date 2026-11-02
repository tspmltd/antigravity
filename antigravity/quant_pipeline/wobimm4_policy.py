#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WOBIMM4 policy — UKI 2018 three laws + WOBIMM LIVE lessons.

Source: https://note.com/uki_profit/n/n24a110c0a609
  1. Maximize trade count (always one-sided maker, not both sides of the book)
  2. Strip EV inhibitors (limit only, no TP/SL, no invented fill, no 5s timer)
  3. Signed indicator = WOBI ∩ OFI (board + prints). No unpublished IC, no p_fill.

LIVE WOBIMM (n=345, −421bp) taught the rest:
  - Two-sided quoting and stacking are gone (already).
  - Hold inventory only while live WOBI∩OFI still agrees with the position.
    A latched signal is not EV. NO_EDGE or the other side → flatten now.
  - Tails hit −46bp with no mark cap. ADVERSE_CUT at −1.8bp from WOBI3 LIVE.
  - Working quotes die on EDGE_PULL (invert / left-behind), not on a clock.
    Latch is for ENTER confirm, not for holding a dead position.
  - ENTER needs live WOBI∩OFI agreeing with the confirmed side. A stale latch
    with ofi=0 is not an edge (paper: ENTER NO_EDGE after every flatten).
  - No spread gate. Maker sits on the board (ENTER=touch, EXIT=ask−1/bid+1).
    Width is not a reason to WAIT or PULL. In position: EV hold or flatten.

Paper / WIRE=NO. This module does not post and does not read .env.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SIZE_BTC = 0.001
MAX_INV_BTC = 0.001
SIGNAL_WOBI = 0.20
SIGNAL_OFI = 0.02
# HONEST longs were −1.14 bp vs shorts +0.91. Tighten only BUY under honest_fill.
HONEST_LONG_WOBI = 0.35
HONEST_LONG_OFI = 0.06
# CSR-662: SPREAD_MAX removed. TF taker wide-stop is inverted for this maker.
# CSR-661 measured 2.8 ≈ p99; that is a distribution, not EV. No replacement gate.
CONFIRM_TICKS = 3

# WOBI3 LIVE EDGE_PULL / ADVERSE_CUT. Magnitudes, not clocks.
ADVERSE_CUT_BP = 1.8
INVERT_WOBI = 0.35
INVERT_OFI = 0.15

BUY = 1
SELL = -1
FLAT = 0

WAIT = "WAIT"
ENTER = "ENTER"
KEEP = "KEEP"
PULL = "PULL"
EXIT = "EXIT"


@dataclass(frozen=True)
class Wobimm4State:
    net_lots: int = 0
    working_side: Optional[str] = None
    wobi: float = 0.0
    recent_ofi: float = 0.0
    spread_bp: float = 0.0
    signal: Optional[int] = None
    signal_why: str = ""
    unrealized_bp: float = 0.0
    drift_bp: float = 0.0
    honest_fill: bool = False
    long_tight: bool = False
    instant_harvest: bool = False


def signed_signal(wobi: float, ofi: float) -> tuple[int, str]:
    """+1 BUY, -1 SELL, 0 no edge. Both wobi and ofi must agree."""
    if wobi >= SIGNAL_WOBI and ofi >= SIGNAL_OFI:
        return BUY, f"BUY_PRESSURE (wobi={wobi:+.2f} ofi={ofi:+.2f})"
    if wobi <= -SIGNAL_WOBI and ofi <= -SIGNAL_OFI:
        return SELL, f"SELL_PRESSURE (wobi={wobi:+.2f} ofi={ofi:+.2f})"
    return FLAT, f"NO_EDGE (wobi={wobi:+.2f} ofi={ofi:+.2f})"


def invert_against(side: Optional[str], wobi: float, ofi: float) -> bool:
    """Confirmed invert vs the quote. Both board and prints."""
    if side == "BUY":
        return wobi < -INVERT_WOBI and ofi < -INVERT_OFI
    if side == "SELL":
        return wobi > INVERT_WOBI and ofi > INVERT_OFI
    return False


def step_confirm(
    pending: int,
    streak: int,
    confirmed: int,
    raw: int,
    need: int = CONFIRM_TICKS,
) -> tuple[int, int, int]:
    """Latch the signed signal. Zeros break the pending streak only."""
    if raw == 0:
        return 0, 0, confirmed
    if raw == pending:
        streak += 1
    else:
        pending = raw
        streak = 1
    if streak >= need:
        confirmed = raw
    return pending, streak, confirmed


def side_name(sig: int) -> Optional[str]:
    if sig > 0:
        return "BUY"
    if sig < 0:
        return "SELL"
    return None


def decide(s: Wobimm4State) -> tuple[str, Optional[str], str]:
    """One tick. Returns (action, quote_side, reason).

    quote_side is BUY/SELL for ENTER/EXIT/KEEP-working, else None.
    """
    work = (s.working_side or "").upper() or None
    live, live_why = signed_signal(s.wobi, s.recent_ofi)
    if s.signal is None:
        sig, why = live, live_why
    else:
        sig = int(s.signal)
        why = s.signal_why or live_why
    want = side_name(sig)

    if s.net_lots != 0:
        flatten = "SELL" if s.net_lots > 0 else "BUY"
        if s.unrealized_bp <= -ADVERSE_CUT_BP:
            return EXIT, flatten, f"adverse_cut ({s.unrealized_bp:+.2f}bp)"
        pos = BUY if s.net_lots > 0 else SELL
        if s.instant_harvest:
            return EXIT, flatten, f"harvest {live_why}"
        # Live tape only. Latch is not EV — that is how the 8h short sat.
        if live == pos:
            if work:
                return PULL, work, f"ev_hold {live_why}"
            return KEEP, None, f"ev_hold {live_why}"
        return EXIT, flatten, f"ev_gone {live_why}"

    if work:
        if invert_against(work, s.wobi, s.recent_ofi):
            return PULL, work, "edge_invert"
        if want is not None and work != want:
            return PULL, work, f"signal_flip {why}"
        return KEEP, work, "working_quote"

    if want is None:
        return WAIT, None, why
    if live != sig:
        return WAIT, None, f"live_disagree {why} {live_why}"
    if (s.long_tight or s.honest_fill) and want == "BUY":
        if s.wobi < HONEST_LONG_WOBI or s.recent_ofi < HONEST_LONG_OFI:
            return WAIT, None, (
                f"long_tight (wobi={s.wobi:+.2f}<{HONEST_LONG_WOBI:+.2f} "
                f"ofi={s.recent_ofi:+.2f}<{HONEST_LONG_OFI:+.2f})"
            )
    return ENTER, want, why
