"""
Quote Engine (連続クォート · maker_fill シミュレーション · WIRE=NO)
================================================================
MM Agent の quote JSON を受け、板プリントで maker 約定をシミュレートする。
fill-and-hold しない。在庫は両面クォートで増減し、HardStop mid −5bp は failsafe。

参照: CSR-514 · Go HardStopBp=5.0 · UMMStrategy.maker_fill
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from .schema import OrderbookMicroSnapshot


class QuoteEngine:
    """連続クォート執行シミュレータ（研究専用・LIVE非接続）。"""

    def __init__(
        self,
        order_size_btc: float = 0.001,
        max_position_btc: float = 0.005,
        hard_stop_bp: float = 5.0,
        max_hold_sec: float = 1800.0,
        store: Any = None,
        bus: Any = None,
        logger: Any = None,
    ):
        self.order_size_btc = float(order_size_btc)
        self.max_position_btc = float(max_position_btc)
        self.hard_stop_bp = float(hard_stop_bp)
        self.max_hold_sec = float(max_hold_sec)
        self.store = store
        self.bus = bus
        self.logger = logger  # optional ParquetBatchLogger → mm_quote_log

        self.inventory_btc: float = 0.0
        self.avg_entry: float = 0.0
        self.entry_time: float = 0.0
        self.realized_pnl_jpy: float = 0.0
        self.realized_pnl_bp: float = 0.0
        self.total_fills: int = 0
        self.win_closes: int = 0
        self.close_count: int = 0
        self.latest_quote: Dict[str, Any] = {}
        self.latest_events: List[Dict[str, Any]] = []
        self._quote_ticks: int = 0
        self._fill_ticks: int = 0
        self._sum_spread_capture_bp: float = 0.0
        self._spread_capture_n: int = 0
        self._last_adverse_selection_bp: float = 0.0

    @staticmethod
    def maker_fill(
        bid_quote: Optional[float],
        ask_quote: Optional[float],
        last_sell: float,
        last_buy: float,
        taker_bid: float,
        taker_ask: float,
    ) -> Tuple[Optional[str], Optional[float]]:
        """指値まで届いた約定だけが取る（最良気配成行はしない）。"""
        if bid_quote and last_sell and taker_bid > 0 and last_sell <= bid_quote:
            return "buy", float(bid_quote)
        if ask_quote and last_buy and taker_ask > 0 and last_buy >= ask_quote:
            return "sell", float(ask_quote)
        return None, None

    def _mid_pnl_bp(self, mid: float) -> float:
        if self.inventory_btc == 0.0 or self.avg_entry <= 0 or mid <= 0:
            return 0.0
        raw = (mid - self.avg_entry) / self.avg_entry * 10000.0
        return raw if self.inventory_btc > 0 else -raw

    def _inventory_pnl_jpy(self, mark: float) -> float:
        if self.inventory_btc == 0.0 or self.avg_entry <= 0:
            return 0.0
        return (mark - self.avg_entry) * self.inventory_btc

    def set_inventory(self, inventory_btc: float, avg_entry: float = 0.0) -> None:
        self.inventory_btc = float(inventory_btc)
        if avg_entry > 0:
            self.avg_entry = float(avg_entry)
        if abs(self.inventory_btc) < 1e-12:
            self.inventory_btc = 0.0
            self.avg_entry = 0.0
            self.entry_time = 0.0

    def step(self, quote: Dict[str, Any], snap: OrderbookMicroSnapshot) -> List[Dict[str, Any]]:
        """1板更新: failsafe → maker_fill → 在庫更新 → ログ。"""
        events: List[Dict[str, Any]] = []
        now = time.time()
        mid = float(snap.mid_price)
        self.latest_quote = dict(quote or {})
        self._quote_ticks += 1

        mode = str(quote.get("mode") or "aggressive_mm")
        bid_q = quote.get("bid_quote")
        ask_q = quote.get("ask_quote")
        size = float(quote.get("quote_size") or self.order_size_btc)

        # --- HardStop / age failsafe (flatten at tip) ---
        if abs(self.inventory_btc) > 1e-12:
            pnl_bp_mid = self._mid_pnl_bp(mid)
            age = now - self.entry_time if self.entry_time > 0 else 0.0
            tip = float(snap.best_bid) if self.inventory_btc > 0 else float(snap.best_ask)
            if pnl_bp_mid <= -self.hard_stop_bp:
                ev = self._flatten(tip, mid, reason=f"HARD_STOP ({pnl_bp_mid:.2f}bp mid)", now=now)
                events.append(ev)
            elif age >= self.max_hold_sec:
                ev = self._flatten(tip, mid, reason=f"TIMEOUT ({age:.0f}s)", now=now)
                events.append(ev)

        # --- continuous quote + maker fill (pause / size0 → no fill) ---
        if mode != "pause" and size > 0 and abs(self.inventory_btc) < self.max_position_btc + 1e-12:
            # inventory cap: drop aggressive side
            if self.inventory_btc >= self.max_position_btc:
                bid_q = None
            if self.inventory_btc <= -self.max_position_btc:
                ask_q = None

            side, px = self.maker_fill(
                bid_q,
                ask_q,
                float(getattr(snap, "last_sell_price", 0.0) or 0.0),
                float(getattr(snap, "last_buy_price", 0.0) or 0.0),
                float(snap.taker_volume_bid or 0.0),
                float(snap.taker_volume_ask or 0.0),
            )
            if side and px:
                fill_size = min(size, self.order_size_btc)
                ev = self._apply_fill(side, float(px), fill_size, mid, now, reason=f"maker_fill/{mode}")
                events.append(ev)
                self._fill_ticks += 1

        # quote log — plan learning fields (quote_distance / fill_rate / inventory / capture / AE)
        best_bid = float(snap.best_bid)
        best_ask = float(snap.best_ask)
        quote_distance_bid = (best_bid - float(bid_q)) if bid_q else None
        quote_distance_ask = (float(ask_q) - best_ask) if ask_q else None
        fill_rate = (self._fill_ticks / self._quote_ticks) if self._quote_ticks > 0 else 0.0
        inv_pnl_bp = self._mid_pnl_bp(mid)
        adverse_selection_bp = -inv_pnl_bp if abs(self.inventory_btc) > 1e-12 else 0.0
        self._last_adverse_selection_bp = adverse_selection_bp
        avg_sc = (
            self._sum_spread_capture_bp / self._spread_capture_n
            if self._spread_capture_n > 0
            else None
        )

        log_rec = {
            **self.latest_quote,
            "inventory_btc": round(self.inventory_btc, 6),
            "inventory": round(self.inventory_btc, 6),
            "avg_entry": round(self.avg_entry, 1),
            "inventory_pnl_jpy": round(self._inventory_pnl_jpy(mid), 2),
            "inventory_pnl": round(inv_pnl_bp, 3),
            "inventory_pnl_bp": round(inv_pnl_bp, 3),
            "quote_distance_bid": round(quote_distance_bid, 1) if quote_distance_bid is not None else None,
            "quote_distance_ask": round(quote_distance_ask, 1) if quote_distance_ask is not None else None,
            "fill_rate": round(fill_rate, 4),
            "spread_capture": round(avg_sc, 3) if avg_sc is not None else None,
            "spread_capture_bp_avg": round(avg_sc, 3) if avg_sc is not None else None,
            "adverse_selection": round(adverse_selection_bp, 3),
            "adverse_selection_bp": round(adverse_selection_bp, 3),
            "mid": mid,
            "wire": "NO",
            "enforce": 0,
        }
        if self.store is not None:
            try:
                self.store.append_quote(log_rec)
            except Exception:
                pass
        if self.logger is not None:
            try:
                self.logger.log("mm_quote_log", log_rec)
            except Exception:
                pass

        for ev in events:
            if self.store is not None:
                try:
                    self.store.append_fill(ev)
                except Exception:
                    pass
            if self.bus is not None:
                try:
                    self.bus.publish("mm_fill", ev)
                except Exception:
                    pass
            if self.logger is not None:
                try:
                    self.logger.log("mm_fill_log", ev)
                except Exception:
                    pass

        self.latest_events = events
        if self.bus is not None:
            try:
                self.bus.publish("mm_inventory", {
                    "inventory_btc": self.inventory_btc,
                    "avg_entry": self.avg_entry,
                    "inventory_pnl_bp": inv_pnl_bp,
                    "realized_pnl_bp": self.realized_pnl_bp,
                    "adverse_selection_bp": adverse_selection_bp,
                    "total_fills": self.total_fills,
                    "wire": "NO",
                })
            except Exception:
                pass
        return events

    def _apply_fill(
        self,
        side: str,
        price: float,
        size: float,
        mid: float,
        now: float,
        reason: str,
    ) -> Dict[str, Any]:
        signed = size if side == "buy" else -size
        prev = self.inventory_btc
        realized = 0.0
        closed = 0.0
        event_type = "open_or_add"

        # closing / reducing opposite inventory
        if prev != 0.0 and (prev > 0) != (signed > 0):
            close_qty = min(abs(prev), abs(signed))
            if prev > 0:  # long reduced by sell
                realized = (price - self.avg_entry) * close_qty
            else:  # short reduced by buy
                realized = (self.avg_entry - price) * close_qty
            closed = close_qty
            event_type = "reduce" if close_qty < abs(prev) - 1e-12 else "close"
            self.realized_pnl_jpy += realized
            notional = close_qty * (mid if mid > 0 else price)
            bp = (realized / notional) * 10000.0 if notional > 0 else 0.0
            self.realized_pnl_bp += bp
            self.close_count += 1
            if realized > 0:
                self.win_closes += 1
            # spread_capture: realized bp on reduce/close (positive = captured)
            self._sum_spread_capture_bp += bp
            self._spread_capture_n += 1

        new_inv = prev + signed
        # residual opens new avg
        if abs(new_inv) < 1e-12:
            self.inventory_btc = 0.0
            self.avg_entry = 0.0
            self.entry_time = 0.0
        elif prev == 0.0 or (prev > 0) == (signed > 0):
            # add same direction
            old_abs = abs(prev)
            new_abs = abs(new_inv)
            if old_abs < 1e-12:
                self.avg_entry = price
                self.entry_time = now
            else:
                self.avg_entry = (self.avg_entry * old_abs + price * abs(signed)) / new_abs
            self.inventory_btc = new_inv
        else:
            # flipped or residual after close
            residual = abs(signed) - closed
            self.inventory_btc = new_inv
            if residual > 1e-12:
                self.avg_entry = price
                self.entry_time = now
                event_type = "flip"
            # else fully closed — avg cleared above if near zero

        self.total_fills += 1
        return {
            "ts": now,
            "event": event_type,
            "side": side,
            "price": price,
            "size": size,
            "inventory_btc": round(self.inventory_btc, 6),
            "avg_entry": round(self.avg_entry, 1),
            "realized_pnl_jpy": round(realized, 2),
            "mid": mid,
            "reason": reason,
            "wire": "NO",
            "enforce": 0,
        }

    def _flatten(self, tip: float, mid: float, reason: str, now: float) -> Dict[str, Any]:
        side = "sell" if self.inventory_btc > 0 else "buy"
        size = abs(self.inventory_btc)
        return self._apply_fill(side, tip, size, mid, now, reason=reason)

    def stats(self) -> Dict[str, Any]:
        wr = (self.win_closes / self.close_count * 100.0) if self.close_count > 0 else 0.0
        fill_rate = (self._fill_ticks / self._quote_ticks) if self._quote_ticks > 0 else 0.0
        return {
            "total_fills": self.total_fills,
            "close_count": self.close_count,
            "win_closes": self.win_closes,
            "win_rate_pct": round(wr, 1),
            "realized_pnl_jpy": round(self.realized_pnl_jpy, 1),
            "realized_pnl_bp": round(self.realized_pnl_bp, 2),
            "inventory_btc": round(self.inventory_btc, 6),
            "fill_rate": round(fill_rate, 4),
            "quote_ticks": self._quote_ticks,
            "wire": "NO",
        }
