"""
MM Agent v0 (連続クォート型 · WIRE=NO)
======================================
OrderbookMicroSnapshot + Micro/Trend/Fusion mode → quote JSON。
執行には接続しない。HardStop mid-bp は QuoteEngine 側 failsafe。

参照: CSR-514 · FIX.me §3 · Go HardStopBp=5.0
"""
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Dict, Optional

from ..event_bus import EventBus
from ..schema import OrderbookMicroSnapshot


class MMAgent:
    AGENT_NAME = "MMAgent"
    AGENT_VERSION = "mm_agent_v0"

    def __init__(
        self,
        bus: Optional[EventBus] = None,
        order_size_btc: float = 0.001,
        max_position_btc: float = 0.005,
        gamma: float = 0.15,
        spread_min_bp: float = 1.2,
        max_spread_jpy: float = 3000.0,
        imb_threshold: float = 0.25,
    ):
        self.bus = bus
        self.order_size_btc = order_size_btc
        self.max_position_btc = max_position_btc
        self.gamma = gamma
        self.spread_min_bp = spread_min_bp
        self.max_spread_jpy = max_spread_jpy
        self.imb_threshold = imb_threshold

        self.inventory_btc: float = 0.0
        self.inventory_pnl: float = 0.0
        self.latest_quote: Dict[str, Any] = {}
        self.fusion_mm_mode: str = "aggressive_mm"
        self.latest_micro: Dict[str, Any] = {}
        self.latest_trend: Dict[str, Any] = {}

        if bus is not None:
            bus.subscribe("orderbook_micro", self.on_orderbook)
            bus.subscribe("micro_state", self._on_micro)
            bus.subscribe("trend_state", self._on_trend)
            bus.subscribe("mm_fusion_mode", self._on_fusion_mode)

    def _on_micro(self, data: Dict[str, Any]) -> None:
        self.latest_micro = data or {}

    def _on_trend(self, data: Dict[str, Any]) -> None:
        self.latest_trend = data or {}

    def _on_fusion_mode(self, data: Dict[str, Any]) -> None:
        if isinstance(data, dict) and data.get("mm_mode"):
            self.fusion_mm_mode = str(data["mm_mode"])

    def set_inventory(self, inventory_btc: float, inventory_pnl: float = 0.0) -> None:
        self.inventory_btc = float(inventory_btc)
        self.inventory_pnl = float(inventory_pnl)

    def on_orderbook(self, snap: OrderbookMicroSnapshot) -> Dict[str, Any]:
        q = self.compute_quote(snap)
        self.latest_quote = q
        if self.bus is not None:
            self.bus.publish("mm_quote", q)
        return q

    def compute_quote(
        self,
        snap: OrderbookMicroSnapshot,
        mm_mode: Optional[str] = None,
        micro: Optional[Dict[str, Any]] = None,
        trend: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        micro = micro if micro is not None else self.latest_micro
        trend = trend if trend is not None else self.latest_trend
        mode = mm_mode or self.fusion_mm_mode or "aggressive_mm"

        mid = float(snap.mid_price)
        best_bid = float(snap.best_bid)
        best_ask = float(snap.best_ask)
        spread = max(0.0, best_ask - best_bid)
        spread_bp = (spread / mid * 10000.0) if mid > 0 else 0.0
        imb = float(snap.imbalance)
        tb = float(snap.taker_volume_bid or 0.0)
        ta = float(snap.taker_volume_ask or 0.0)
        cancel_rate = float(getattr(snap, "cancel_rate", 0.0) or 0.0)
        refill_rate = float(getattr(snap, "refill_rate", 0.0) or 0.0)

        fake_bo = bool(micro.get("fake_breakout_flag") or micro.get("fake_breakout"))
        lat_risk = bool(micro.get("latency_risk_flag") or micro.get("latency_risk"))
        pressure_side = str(micro.get("pressure_side") or "none")
        t_dir = str(trend.get("trend_direction") or "neutral")

        # fair = micro_price 寄り
        micro_px = float(snap.micro_price) if snap.micro_price else mid
        fair = micro_px if micro_px > 0 else mid

        # inventory skew (btc * gamma * mid scale)
        inv_bias = max(-1.0, min(1.0, -self.inventory_btc / max(self.max_position_btc, 1e-9)))
        skew = -self.inventory_btc * self.gamma * mid * 0.001

        # pressure skew (taker-confirmed imbalance only)
        pressure_skew = 0.0
        taker_ok_buy = ta >= 0.01 and imb >= self.imb_threshold
        taker_ok_sell = tb >= 0.01 and imb <= -self.imb_threshold
        if taker_ok_buy:
            pressure_skew = +spread * 0.15  # bid up / ask wider
        elif taker_ok_sell:
            pressure_skew = -spread * 0.15

        # mode adjustments
        size = self.order_size_btc
        half = spread * 0.5
        if mode == "pause" or fake_bo:
            mode = "pause"
            size = 0.0
        elif mode == "inventory_reduce":
            # push quotes to reduce inventory
            if self.inventory_btc > 0:
                skew -= spread * 0.25  # lower ask priority (sell)
            elif self.inventory_btc < 0:
                skew += spread * 0.25
            size = min(size, abs(self.inventory_btc) or size)
        elif mode == "aggressive_mm":
            if lat_risk:
                size *= 0.5
            half = max(half * 0.85, mid * self.spread_min_bp / 10000.0 * 0.5)

        if spread_bp < self.spread_min_bp or spread > self.max_spread_jpy:
            mode = "pause"
            size = 0.0

        bid_quote = round(fair - half + skew + pressure_skew)
        ask_quote = round(fair + half + skew + pressure_skew)
        if bid_quote >= ask_quote and mid > 0:
            bid_quote = round(mid - max(1.0, half * 0.5))
            ask_quote = round(mid + max(1.0, half * 0.5))

        # inventory cap: don't add to same side
        if self.inventory_btc >= self.max_position_btc:
            bid_quote = None  # no more buys
        if self.inventory_btc <= -self.max_position_btc:
            ask_quote = None

        quote = {
            "ts": time.time(),
            "timestamp": int(getattr(snap, "timestamp", time.time() * 1000)),
            "agent": self.AGENT_NAME,
            "version": self.AGENT_VERSION,
            "wire": "NO",
            "enforce": 0,
            "fair_price": round(fair, 1),
            "bid_quote": bid_quote,
            "ask_quote": ask_quote,
            "inventory_bias": round(inv_bias, 4),
            "inventory_btc": round(self.inventory_btc, 6),
            "inventory_pnl": round(self.inventory_pnl, 4),
            "quote_size": round(size, 6),
            "mode": mode,
            "spread_bp": round(spread_bp, 3),
            "imbalance": round(imb, 4),
            "pressure_side": pressure_side,
            "trend_direction": t_dir,
            "cancel_minus_refill": round(cancel_rate - refill_rate, 4),
            "micro_dev": float(snap.micro_dev),
            "taker_aggressiveness": float(getattr(snap, "taker_aggressiveness", 0.0) or 0.0),
            "fake_breakout": fake_bo,
            "latency_risk": lat_risk,
        }
        return quote
