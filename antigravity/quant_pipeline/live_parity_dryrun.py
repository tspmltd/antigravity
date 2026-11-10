"""LIVE-parity DRYRUN fill + verdict (CSR-680). WIRE=NO.

Soft paper (BOARD_TRADE / BOOK_CROSS instant commit, HIT_BID_FORCE,
fee-free PnL, n=0 = "設計どおり") is banned on LIVE-parity lanes.

Paper proxy for REAL commit rules:
  - PUBLIC_OID → commit
  - BOARD_TRADE / BOOK_CROSS → commit only if opposing print size ≥ lot
    through our price in the same exec batch (BOOK_CROSS alone = park)
  - HIT_BID_FORCE → never commit (rest Soft; escalate to MARKET_SIM)

Fees (CSR-617 B4 class): maker 0 · taker exit 0.75bp · MARKET slip 0.50bp.
Verdict is Soak8-hard: short window / n=0 / no net≥1.0bp → not success.
Economic_PASS is always NO here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

OPTIMISTIC_FILL_SOURCES = frozenset({"BOOK_CROSS", "BOARD_TRADE"})
FORCE_FILL_SOURCES = frozenset({"HIT_BID_FORCE", "HIT_TOUCH_FORCE"})

LOT_BTC = 0.001
MAKER_FEE_BP = 0.0
TAKER_EXIT_FEE_BP = 0.75  # ~half of observed ~1.45bp taker RT
MARKET_SLIP_BP = 0.50

# Soak8-class evaluation (MM5 §7.1)
MIN_WINDOW_SEC = 1200.0
MIN_SAMPLE_TRADES = 5
MIN_HARVEST_BP = 1.0

# Soft rest then Hard escalate (LIVE Soft→HARD shape)
EXIT_REST_MAX_SEC = 3.0


def opposing_through_size(
    side: str, price: float, execs: Optional[List[Dict[str, Any]]]
) -> float:
    """Sum opposing public prints that trade through our resting price."""
    side_u = str(side or "").upper()
    px = float(price or 0.0)
    if px <= 0:
        return 0.0
    total = 0.0
    for ex in execs or []:
        try:
            sz = float(ex.get("size") or 0.0)
            ep = float(ex.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if sz <= 0 or ep <= 0:
            continue
        ex_side = str(ex.get("side") or "").upper()
        if side_u == "BUY" and ex_side == "SELL" and ep <= px + 1e-12:
            total += sz
        elif side_u == "SELL" and ex_side == "BUY" and ep >= px - 1e-12:
            total += sz
    return total


def live_parity_commit(
    src: str,
    side: str,
    price: float,
    execs: Optional[List[Dict[str, Any]]],
    *,
    lot_btc: float = LOT_BTC,
) -> Tuple[bool, str]:
    """Return (commit?, why). Mirrors LIVE REAL: optimistic alone does not fill."""
    s = str(src or "")
    if s in FORCE_FILL_SOURCES:
        return False, "force_fill_banned"
    if s == "PUBLIC_OID":
        return True, "public_oid"
    if s == "MARKET_SIM":
        return True, "market_sim"
    if s == "QUEUE_TRADE" or s == "HEAD_CROSS":
        return True, s.lower()
    if s in OPTIMISTIC_FILL_SOURCES:
        thru = opposing_through_size(side, price, execs)
        if thru + 1e-12 >= float(lot_btc):
            return True, f"size_through={thru:.4f}"
        return False, f"optimistic_parked thru={thru:.4f}<{lot_btc}"
    return False, f"unknown_src={s}"


def fee_haircut_bp(exit_src: str) -> float:
    """Round-trip cost haircut vs gross mid PnL."""
    s = str(exit_src or "")
    if s == "MARKET_SIM" or s in FORCE_FILL_SOURCES or "MARKET" in s.upper():
        return float(MAKER_FEE_BP + TAKER_EXIT_FEE_BP + MARKET_SLIP_BP)
    # Maker entry + maker exit (size-through board)
    return float(MAKER_FEE_BP + MAKER_FEE_BP)


def net_pnl_bp(gross_bp: float, exit_src: str) -> Tuple[float, float]:
    """Return (net_bp, fee_bp)."""
    fee = fee_haircut_bp(exit_src)
    return float(gross_bp) - fee, fee


def lane_verdict(
    *,
    n: int,
    harvest_ge_min: int,
    window_sec: float,
    net_pnl_bp_sum: float = 0.0,
    min_window_sec: float = MIN_WINDOW_SEC,
    min_sample: int = MIN_SAMPLE_TRADES,
    min_harvest_bp: float = MIN_HARVEST_BP,
) -> Dict[str, Any]:
    """Soak8-hard verdict. Never invents Economic_PASS=YES."""
    n_i = int(n or 0)
    h = int(harvest_ge_min or 0)
    w = float(window_sec or 0.0)
    out: Dict[str, Any] = {
        "economic_pass": "NO",
        "min_window_sec": float(min_window_sec),
        "min_sample": int(min_sample),
        "min_harvest_bp": float(min_harvest_bp),
        "n": n_i,
        "harvest_ge_min": h,
        "window_sec": round(w, 1),
        "net_pnl_bp": round(float(net_pnl_bp_sum), 2),
    }
    if w + 1e-9 < float(min_window_sec):
        out["verdict"] = "INSUFFICIENT"
        out["why"] = f"window={w:.0f}s<{min_window_sec:.0f}s (Soak8-class)"
        return out
    if n_i <= 0:
        out["verdict"] = "FAIL"
        out["why"] = "n=0 after soak window (absence ≠ PASS)"
        return out
    if n_i < int(min_sample):
        out["verdict"] = "INSUFFICIENT"
        out["why"] = f"n={n_i}<{min_sample} (Soak8 max_fills class)"
        return out
    if h <= 0:
        out["verdict"] = "FAIL"
        out["why"] = f"no close with net≥{min_harvest_bp:.1f}bp (Soak8 harvest rule)"
        return out
    out["verdict"] = "OBSERVE"
    out["why"] = (
        f"harvests={h} net_sum={float(net_pnl_bp_sum):+.2f}bp · "
        "Economic_PASS=NO until signed CF"
    )
    return out


def count_harvests(rows: List[Dict[str, Any]], min_bp: float = MIN_HARVEST_BP) -> int:
    n = 0
    for r in rows or []:
        try:
            pnl = float(r.get("pnl_bp_net", r.get("pnl_bp")) or 0.0)
        except (TypeError, ValueError):
            continue
        if pnl + 1e-12 >= float(min_bp):
            n += 1
    return n


def fmt_verdict_line(lane: str, v: Dict[str, Any]) -> str:
    return (
        f"**{lane}** `{v.get('verdict')}` · n={v.get('n')} · "
        f"harvest≥{v.get('min_harvest_bp')}={v.get('harvest_ge_min')} · "
        f"Economic_PASS={v.get('economic_pass')} · {v.get('why')}"
    )


class LiveParityScalpMixin:
    """Mixin for MS1/MS2-stack scalp dryruns: LIVE-hard fill + fee + Soft→HARD.

    Requires host attrs: quote_side, quote_px, working_id, wobi3, honest_fills,
    board_fills, _on_honest_fill, _execute_exit_wobi3 (super), _post_quote,
    _yen via wobi3, ledger via parent exit path.
    """

    LIVE_PARITY = True
    HS_FORCE_FILL = False
    LOT_BTC = LOT_BTC
    EXIT_REST_MAX_SEC = EXIT_REST_MAX_SEC
    FILL_MODEL = "LIVE_PARITY_SIZE_THROUGH"

    def _parity_init(self) -> None:
        self.parity_parked = 0
        self.parity_commits = 0
        self.parity_market_sim = 0
        self.exit_posted_ts = 0.0
        self._lane_started_ts = float(__import__("time").time())
        self._last_execs: List[Dict[str, Any]] = []

    def _maybe_fill_board(
        self,
        execs: List[Dict[str, Any]],
        bid: float,
        ask: float,
        snap: Any,
        now: float,
        m: Dict[str, Any],
        depth_bids: List,
        depth_asks: List,
    ) -> None:
        from antigravity.quant_pipeline.run_wobimm_live import public_maker_fill

        self._last_execs = list(execs or [])
        if not self.quote_side or self.quote_px <= 0:
            return
        hit = public_maker_fill(
            side=self.quote_side.upper(),
            price=self.quote_px,
            acceptance_id=self.working_id,
            public_execs=execs,
            best_bid=bid,
            best_ask=ask,
        )
        if not hit:
            return
        src, _sz = hit
        ok, why = live_parity_commit(
            src, self.quote_side, self.quote_px, execs, lot_btc=float(self.LOT_BTC)
        )
        if not ok:
            self.parity_parked = int(getattr(self, "parity_parked", 0)) + 1
            return
        self.parity_commits = int(getattr(self, "parity_commits", 0)) + 1
        self._on_honest_fill(
            self.quote_side,
            self.quote_px,
            f"{src}|{why}",
            snap,
            now,
            m,
            depth_bids,
            depth_asks,
        )

    def _parity_maybe_hard_exit(
        self,
        *,
        bid: float,
        snap: Any,
        now: float,
        m: Dict[str, Any],
        depth_bids: List,
        depth_asks: List,
        tag: str,
        reason: str,
    ) -> bool:
        """After Soft rest TTL, MARKET_SIM with taker+slip (LIVE Soft→HARD)."""
        if not self.wobi3.position_side or bid <= 0:
            return False
        posted = float(getattr(self, "exit_posted_ts", 0.0) or 0.0)
        if posted <= 0:
            return False
        if float(now) - posted < float(getattr(self, "EXIT_REST_MAX_SEC", EXIT_REST_MAX_SEC)):
            return False
        self.parity_market_sim = int(getattr(self, "parity_market_sim", 0)) + 1
        self.pending_exit_tag = tag
        self.pending_exit_reason = f"{reason} [MARKET_SIM soft→hard]"
        self._on_honest_fill(
            "sell",
            float(bid),
            "MARKET_SIM",
            snap,
            now,
            m,
            depth_bids,
            depth_asks,
        )
        return True

    def _parity_mark_exit_posted(self, now: float) -> None:
        if float(getattr(self, "exit_posted_ts", 0.0) or 0.0) <= 0:
            self.exit_posted_ts = float(now)

    def parity_metrics(self) -> Dict[str, Any]:
        import time as _t

        started = float(getattr(self, "_lane_started_ts", 0.0) or _t.time())
        window = max(0.0, _t.time() - started)
        rows = list(getattr(self, "hist_wobi3", []) or [])
        n = len(rows)
        harvest = count_harvests(rows, MIN_HARVEST_BP)
        net_sum = 0.0
        for r in rows:
            try:
                net_sum += float(r.get("pnl_bp_net", r.get("pnl_bp")) or 0.0)
            except (TypeError, ValueError):
                pass
        verd = lane_verdict(
            n=n,
            harvest_ge_min=harvest,
            window_sec=window,
            net_pnl_bp_sum=net_sum,
        )
        return {
            "live_parity": True,
            "fill_model": str(getattr(self, "FILL_MODEL", "LIVE_PARITY")),
            "hs_force_fill": bool(getattr(self, "HS_FORCE_FILL", False)),
            "parity_parked": int(getattr(self, "parity_parked", 0)),
            "parity_commits": int(getattr(self, "parity_commits", 0)),
            "parity_market_sim": int(getattr(self, "parity_market_sim", 0)),
            "exit_rest_max_sec": float(getattr(self, "EXIT_REST_MAX_SEC", EXIT_REST_MAX_SEC)),
            "verdict": verd,
        }
