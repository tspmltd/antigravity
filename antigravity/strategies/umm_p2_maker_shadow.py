"""
UMM-P2 Shadow — UMM Baseline vs UMM+Maker (Execution Alpha)
============================================================
Directive: docs/ops/umm_p2_maker_shadow_directive.md

- Signal / Inventory policy / Skew / Sizing / Exit / Params: FROZEN (UMMStrategy champion)
- ARM changes Entry Execution only: Maker Offset + CANCEL_AND_WAIT (PEG2-B constants)
- WIRE=NO · LIVE untouched · PROMOTE locked
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .umm_strategy import UMMStrategy

# Frozen PEG2-B execution constants — retune forbidden
MAKER_OFFSET_RATIO = 0.97
MAKER_TIMEOUT_SEC = 3.0
SPREAD_GUARD_MAX_BP = 4.5
FALLBACK_MODE = "CANCEL_AND_WAIT"


def _hold_bucket(hold: float) -> str:
    if hold < 10:
        return "0-10s"
    if hold < 30:
        return "10-30s"
    if hold < 120:
        return "30-120s"
    return "120s+"


def _metrics(pnls: List[float]) -> Dict[str, Any]:
    if not pnls:
        return {"n": 0, "win_rate": None, "ev_bp": None, "pnl_bp": 0.0, "pf": None}
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    gw = sum(wins)
    gl = abs(sum(losses))
    return {
        "n": len(pnls),
        "win_rate": round(len(wins) / len(pnls), 4),
        "ev_bp": round(sum(pnls) / len(pnls), 4),
        "pnl_bp": round(sum(pnls), 2),
        "pf": round(gw / gl, 4) if gl > 0 else None,
        "avg_win_bp": round(sum(wins) / len(wins), 4) if wins else None,
        "avg_loss_bp": round(sum(losses) / len(losses), 4) if losses else None,
    }


@dataclass
class OpenPath:
    side: str
    entry_price: float
    entry_ts: float
    entry_mid: float
    mfe_bp: float = 0.0
    mae_bp: float = 0.0
    ae_100ms: Optional[float] = None
    ae_1s: Optional[float] = None
    ae_3s: Optional[float] = None
    ae_10s: Optional[float] = None
    ae_30s: Optional[float] = None
    samples: List[Tuple[float, float]] = field(default_factory=list)  # (ts, mid)


class UMMArmShadow:
    """One research arm: CTRL (baseline fill) or ARM (maker offset entry)."""

    def __init__(self, arm: str):
        assert arm in ("CTRL", "ARM")
        self.arm = arm
        self.umm = UMMStrategy()  # champion freeze; constructor overrides ignored
        self.pending: Optional[Dict[str, Any]] = None
        self.path: Optional[OpenPath] = None
        self.trades: List[Dict[str, Any]] = []
        self.attempts = 0
        self.fills = 0
        self.timeouts = 0
        self.spread_guard_skips = 0
        self.dd_peak = 0.0
        self.dd_min = 0.0
        self.cum_pnl_bp = 0.0
        self.umm_bid = None
        self.umm_ask = None

    # --- helpers ---
    def _signed_bp(self, side: str, entry: float, mark: float) -> float:
        if entry <= 0 or mark <= 0:
            return 0.0
        raw = (mark - entry) / entry * 10000.0
        return raw if side == "buy" else -raw

    def _limit_from_quote(self, side: str, mid: float, spread: float) -> float:
        half = spread * 0.5
        if side == "buy":
            return round(mid - half * MAKER_OFFSET_RATIO)
        return round(mid + half * MAKER_OFFSET_RATIO)

    def _update_path(self, now: float, mid: float) -> None:
        if not self.path:
            return
        p = self.path
        p.samples.append((now, mid))
        bp = self._signed_bp(p.side, p.entry_price, mid)
        if bp > p.mfe_bp:
            p.mfe_bp = bp
        if bp < p.mae_bp:
            p.mae_bp = bp
        # AE = min signed bp (adverse) over horizon windows from entry
        age = now - p.entry_ts
        for name, horizon, attr in (
            ("ae_100ms", 0.1, "ae_100ms"),
            ("ae_1s", 1.0, "ae_1s"),
            ("ae_3s", 3.0, "ae_3s"),
            ("ae_10s", 10.0, "ae_10s"),
            ("ae_30s", 30.0, "ae_30s"),
        ):
            if age + 1e-9 >= horizon and getattr(p, attr) is None:
                window = [self._signed_bp(p.side, p.entry_price, m) for t, m in p.samples if t <= p.entry_ts + horizon]
                if window:
                    setattr(p, attr, min(window))

    def _enter(self, side: str, px: float, now: float, mid: float, reason: str) -> Dict[str, Any]:
        self.umm.record_trade(side, px, 0.0)
        self.fills += 1
        self.path = OpenPath(side=side, entry_price=px, entry_ts=now, entry_mid=mid)
        self.path.samples.append((now, mid))
        return {"arm": self.arm, "event": "ENTRY", "side": side, "price": px, "reason": reason}

    def _exit(self, now: float, mid: float, tip: float, reason: str, pnl_jpy: float, pnl_bp: float) -> Dict[str, Any]:
        self._update_path(now, mid)
        hold = now - (self.path.entry_ts if self.path else now)
        mfe = self.path.mfe_bp if self.path else 0.0
        mae = self.path.mae_bp if self.path else 0.0
        ae1 = self.path.ae_1s if self.path else None
        ae10 = self.path.ae_10s if self.path else None
        # finalize open AE horizons with available samples
        if self.path:
            for attr, horizon in (("ae_100ms", 0.1), ("ae_1s", 1.0), ("ae_3s", 3.0), ("ae_10s", 10.0), ("ae_30s", 30.0)):
                if getattr(self.path, attr) is None and self.path.samples:
                    window = [
                        self._signed_bp(self.path.side, self.path.entry_price, m)
                        for t, m in self.path.samples
                        if t <= self.path.entry_ts + horizon
                    ]
                    if window:
                        setattr(self.path, attr, min(window))
            ae1 = self.path.ae_1s
            ae10 = self.path.ae_10s

        never_green = pnl_bp <= 0 and (mfe or 0.0) <= 0.5
        ae_worsen = (
            ae1 is not None and ae10 is not None and ae10 < ae1 - 0.5
        )

        side = self.umm.position_side
        self.umm.record_trade("exit", tip, pnl_jpy, mid_price=mid, exit_reason=reason)
        self.cum_pnl_bp += pnl_bp
        if self.cum_pnl_bp > self.dd_peak:
            self.dd_peak = self.cum_pnl_bp
        dd = self.cum_pnl_bp - self.dd_peak
        if dd < self.dd_min:
            self.dd_min = dd

        rec = {
            "arm": self.arm,
            "ts": now,
            "side": side,
            "entry_price": self.path.entry_price if self.path else None,
            "exit_price": tip,
            "hold_sec": round(hold, 3),
            "hold_bucket": _hold_bucket(hold),
            "pnl_bp": round(pnl_bp, 4),
            "pnl_jpy": round(pnl_jpy, 4),
            "mfe_bp": round(mfe, 4),
            "mae_bp": round(mae, 4),
            "ae_100ms": self.path.ae_100ms if self.path else None,
            "ae_1s": ae1,
            "ae_3s": self.path.ae_3s if self.path else None,
            "ae_10s": ae10,
            "ae_30s": self.path.ae_30s if self.path else None,
            "never_green": never_green,
            "ae_worsen": ae_worsen,
            "exit_reason": reason,
            "is_win": pnl_bp > 0,
        }
        self.trades.append(rec)
        self.path = None
        self.pending = None
        return {"arm": self.arm, "event": "EXIT", **rec}

    def step(
        self,
        now: float,
        mid: float,
        best_bid: float,
        best_ask: float,
        imbalance: float,
        last_sell: float,
        last_buy: float,
        taker_bid: float,
        taker_ask: float,
    ) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        if mid <= 0 or best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
            return events

        spread = best_ask - best_bid
        spread_bp = spread / mid * 10000.0

        # Path update while open
        if self.umm.position_side and self.path:
            self._update_path(now, mid)

        # 1) UMM decision (frozen signal/exit/params)
        res = self.umm.on_tick(
            mid_price=mid,
            best_bid=best_bid,
            best_ask=best_ask,
            imbalance=imbalance,
        )
        action = res.get("action", "hold")

        # 2) Hard exits from strategy (TP / HS / TIMEOUT) — identical both arms
        if action == "exit" and self.umm.position_side:
            tip = float(res.get("price") or (best_bid if self.umm.position_side == "buy" else best_ask))
            pnl_jpy = float(res.get("expected_pnl") or 0.0)
            pnl_bp = float(res.get("pnl_bp") or 0.0)
            events.append(self._exit(now, mid, tip, str(res.get("reason") or "EXIT"), pnl_jpy, pnl_bp))
            self.umm_bid = None
            self.umm_ask = None
            return events

        # 3) Quote path
        if action != "quote":
            self.umm_bid = None
            self.umm_ask = None
            self.pending = None
            return events

        bid_q = res.get("bid_quote")
        ask_q = res.get("ask_quote")
        self.umm_bid = bid_q
        self.umm_ask = ask_q

        # Opposite maker-close while in position (baseline exit path — both arms)
        if self.umm.position_side:
            side, px = UMMStrategy.maker_fill(
                bid_q, ask_q, last_sell, last_buy, taker_bid, taker_ask
            )
            if side:
                opposite = (
                    (self.umm.position_side == "buy" and side == "sell")
                    or (self.umm.position_side == "sell" and side == "buy")
                )
                if opposite:
                    tip = float(px)
                    size = float(self.umm.params.get("order_size_btc", 0.001))
                    entry = float(self.umm.entry_price or 0.0)
                    if self.umm.position_side == "buy":
                        pnl_jpy = (tip - entry) * size
                    else:
                        pnl_jpy = (entry - tip) * size
                    pnl_bp = (pnl_jpy / (size * mid) * 10000.0) if mid > 0 else 0.0
                    events.append(
                        self._exit(now, mid, tip, f"MAKER_CLOSE ({side}@{tip:.0f})", pnl_jpy, pnl_bp)
                    )
            return events

        # 4) Flat → entry execution diverges
        if self.arm == "CTRL":
            side, px = UMMStrategy.maker_fill(
                bid_q, ask_q, last_sell, last_buy, taker_bid, taker_ask
            )
            if side:
                self.attempts += 1
                events.append(self._enter(side, float(px), now, mid, "CTRL_BASELINE_FILL"))
            return events

        # ARM: Maker Offset Entry + CANCEL_AND_WAIT
        if spread_bp > SPREAD_GUARD_MAX_BP:
            self.spread_guard_skips += 1
            self.pending = None
            return events

        # Manage pending
        if self.pending:
            side = self.pending["side"]
            lim = self.pending["limit_price"]
            filled = (side == "buy" and best_bid >= lim) or (side == "sell" and best_ask <= lim)
            # also allow last-trade touch consistent with CTRL feed
            if not filled:
                if side == "buy" and last_sell and taker_bid > 0 and last_sell <= lim:
                    filled = True
                if side == "sell" and last_buy and taker_ask > 0 and last_buy >= lim:
                    filled = True
            if filled:
                events.append(self._enter(side, float(lim), now, mid, "ARM_MAKER_FILL"))
                self.pending = None
                return events
            if now >= self.pending["timeout_ts"]:
                self.timeouts += 1
                events.append(
                    {
                        "arm": self.arm,
                        "event": "NO_FILL_CANCEL",
                        "side": side,
                        "limit_price": lim,
                        "waited_sec": round(now - self.pending["created_ts"], 3),
                        "fallback": FALLBACK_MODE,
                    }
                )
                self.pending = None
                # CANCEL_AND_WAIT: do not chase this tick
                return events
            return events

        # New attempt: only if baseline would also see a touch opportunity on that side
        # (eligible quote side with market activity) — then post more passive limit
        base_side, _base_px = UMMStrategy.maker_fill(
            bid_q, ask_q, last_sell, last_buy, taker_bid, taker_ask
        )
        if not base_side:
            return events
        if (base_side == "buy" and bid_q is None) or (base_side == "sell" and ask_q is None):
            return events

        lim = self._limit_from_quote(base_side, mid, spread)
        self.attempts += 1
        self.pending = {
            "side": base_side,
            "limit_price": lim,
            "created_ts": now,
            "timeout_ts": now + MAKER_TIMEOUT_SEC,
            "baseline_quote": bid_q if base_side == "buy" else ask_q,
            "mid": mid,
            "spread_bp": spread_bp,
        }
        events.append(
            {
                "arm": self.arm,
                "event": "ORDER_POSTED",
                "side": base_side,
                "limit_price": lim,
                "baseline_quote": self.pending["baseline_quote"],
                "offset_ratio": MAKER_OFFSET_RATIO,
                "timeout_sec": MAKER_TIMEOUT_SEC,
            }
        )
        # Immediate fill possible if already marketable at passive limit
        if (base_side == "buy" and best_bid >= lim) or (base_side == "sell" and best_ask <= lim):
            events.append(self._enter(base_side, float(lim), now, mid, "ARM_MAKER_FILL_IMMEDIATE"))
            self.pending = None
        return events

    def kpis(self) -> Dict[str, Any]:
        pnls = [t["pnl_bp"] for t in self.trades]
        m = _metrics(pnls)
        losses = [t for t in self.trades if t["pnl_bp"] <= 0]
        by_hold: Dict[str, List[float]] = {}
        never_by: Dict[str, List[bool]] = {}
        worsen_by: Dict[str, List[bool]] = {}
        for t in self.trades:
            b = t["hold_bucket"]
            by_hold.setdefault(b, []).append(t["pnl_bp"])
            never_by.setdefault(b, []).append(bool(t.get("never_green")))
            worsen_by.setdefault(b, []).append(bool(t.get("ae_worsen")))

        def rate(flags: List[bool]) -> Optional[float]:
            return round(sum(1 for x in flags if x) / len(flags), 4) if flags else None

        hold_buckets = {}
        for b in ("0-10s", "10-30s", "30-120s", "120s+"):
            hold_buckets[b] = {
                **_metrics(by_hold.get(b, [])),
                "never_green_rate": rate(never_by.get(b, [])),
                "ae_worsen_rate": rate(worsen_by.get(b, [])),
            }

        ae1 = [t["ae_1s"] for t in self.trades if t.get("ae_1s") is not None]
        ae10 = [t["ae_10s"] for t in self.trades if t.get("ae_10s") is not None]
        return {
            "arm": self.arm,
            "headline": {
                **m,
                "dd_bp": round(self.dd_min, 2),
                "attempts": self.attempts,
                "fills": self.fills,
                "timeouts": self.timeouts,
                "fill_rate": round(self.fills / self.attempts, 4) if self.attempts else None,
                "spread_guard_skips": self.spread_guard_skips,
                "never_green_rate": rate([bool(t.get("never_green")) for t in self.trades]),
                "ae_worsen_rate": rate([bool(t.get("ae_worsen")) for t in self.trades]),
                "avg_ae_1s": round(sum(ae1) / len(ae1), 4) if ae1 else None,
                "avg_ae_10s": round(sum(ae10) / len(ae10), 4) if ae10 else None,
                "avg_mfe": round(sum(t["mfe_bp"] for t in self.trades) / len(self.trades), 4) if self.trades else None,
                "avg_mae": round(sum(t["mae_bp"] for t in self.trades) / len(self.trades), 4) if self.trades else None,
                "open_position": self.umm.position_side,
            },
            "hold_buckets": hold_buckets,
            "n_trades": len(self.trades),
        }


def _bucket_ev_block(kpis: Dict[str, Any]) -> Dict[str, Any]:
    """Per-bucket EV (別EV) + 10s+ sink aggregate."""
    out: Dict[str, Any] = {}
    sink_pnls_n = 0
    sink_pnl = 0.0
    sink_ev_num = 0.0
    for b in ("0-10s", "10-30s", "30-120s", "120s+"):
        hb = (kpis.get("hold_buckets") or {}).get(b) or {}
        out[b] = {
            "n": hb.get("n", 0),
            "ev_bp": hb.get("ev_bp"),
            "pnl_bp": hb.get("pnl_bp", 0.0),
            "never_green_rate": hb.get("never_green_rate"),
            "ae_worsen_rate": hb.get("ae_worsen_rate"),
        }
        if b in ("10-30s", "30-120s", "120s+") and hb.get("n"):
            sink_pnls_n += int(hb["n"])
            sink_pnl += float(hb.get("pnl_bp") or 0.0)
            if hb.get("ev_bp") is not None:
                sink_ev_num += float(hb["ev_bp"]) * int(hb["n"])
    out["10s+_sink"] = {
        "n": sink_pnls_n,
        "pnl_bp": round(sink_pnl, 2),
        "ev_bp": round(sink_ev_num / sink_pnls_n, 4) if sink_pnls_n else None,
    }
    return out


def judge_verdict(
    delta: Dict[str, Any],
    *,
    sample_floor_met: bool,
    arm_n: int,
) -> Dict[str, Any]:
    """User-simplified gate.

    REJECT:     ΔPnL <= 0  (only after sample floor; before floor → MORE_DATA)
    MORE_DATA:  improvement signals but sample insufficient, or mixed
    PROMOTE:    PnL↑ AND PF↑ AND DD↑(less neg) AND never_green↓ AND AE↑(avg_ae_1s less toxic)
                after sample floor — Paper-candidate only
    """
    d_pnl = delta.get("pnl_bp")
    d_pf = delta.get("pf")
    d_dd = delta.get("dd_bp")  # ARM - CTRL; improvement if ARM dd less negative → delta > 0
    d_ng = delta.get("never_green_rate")  # improvement if lower → delta < 0
    d_ae = delta.get("avg_ae_1s")  # improvement if less toxic (higher / closer to 0) → delta > 0
    d_sink = delta.get("sink_10s_plus_pnl_bp")

    improve_flags = {
        "pnl": d_pnl is not None and d_pnl > 0,
        "pf": d_pf is not None and d_pf > 0,
        "dd": d_dd is not None and d_dd > 0,
        "never_green": d_ng is not None and d_ng < 0,
        "ae": d_ae is not None and d_ae > 0,
        "sink_10s_plus": d_sink is not None and d_sink > 0,
    }
    any_improve = any(improve_flags.values())
    promote_core = all(improve_flags[k] for k in ("pnl", "pf", "dd", "never_green", "ae"))

    if not sample_floor_met or arm_n < 30:
        verdict = "MORE_DATA"
        reason = "標本不足（72h or attempts≥2000 and n≥30 未満）"
    elif d_pnl is not None and d_pnl <= 0:
        verdict = "REJECT"
        reason = "ΔPnL <= 0"
    elif promote_core:
        verdict = "PROMOTE"
        reason = "PnL/PF/DD/Never-green/AE が全て改善"
    elif any_improve:
        verdict = "MORE_DATA"
        reason = "改善シグナルありだが揃わず / 安定不足"
    else:
        verdict = "MORE_DATA"
        reason = "判定保留"

    return {
        "verdict": verdict,
        "promote": False,  # never auto; PROMOTE label is advisory Paper-candidate only
        "promote_label": verdict == "PROMOTE",
        "improve_flags": improve_flags,
        "reason": reason,
    }


def compare_kpis(ctrl: UMMArmShadow, arm: UMMArmShadow, *, sample_floor_met: bool = False) -> Dict[str, Any]:
    c, a = ctrl.kpis(), arm.kpis()
    c_ev = _bucket_ev_block(c)
    a_ev = _bucket_ev_block(a)

    def delta(key: str):
        cv = c["headline"].get(key)
        av = a["headline"].get(key)
        if cv is None or av is None:
            return None
        return round(av - cv, 4)

    d_sink_pnl = None
    if c_ev["10s+_sink"]["pnl_bp"] is not None and a_ev["10s+_sink"]["pnl_bp"] is not None:
        # improvement = ARM sink less negative → ARM_pnl - CTRL_pnl > 0
        d_sink_pnl = round(a_ev["10s+_sink"]["pnl_bp"] - c_ev["10s+_sink"]["pnl_bp"], 2)
    d_sink_ev = None
    if c_ev["10s+_sink"]["ev_bp"] is not None and a_ev["10s+_sink"]["ev_bp"] is not None:
        d_sink_ev = round(a_ev["10s+_sink"]["ev_bp"] - c_ev["10s+_sink"]["ev_bp"], 4)

    delta_block = {
        "pnl_bp": delta("pnl_bp"),
        "ev_bp": delta("ev_bp"),
        "pf": delta("pf"),
        "dd_bp": delta("dd_bp"),
        "never_green_rate": delta("never_green_rate"),
        "ae_worsen_rate": delta("ae_worsen_rate"),
        "avg_ae_1s": delta("avg_ae_1s"),
        "sink_10s_plus_pnl_bp": d_sink_pnl,
        "sink_10s_plus_ev_bp": d_sink_ev,
        "fill_rate_arm": a["headline"].get("fill_rate"),
        "bucket_ev": {
            b: {
                "CTRL": c_ev[b].get("ev_bp"),
                "ARM": a_ev[b].get("ev_bp"),
                "delta": (
                    None
                    if c_ev[b].get("ev_bp") is None or a_ev[b].get("ev_bp") is None
                    else round(a_ev[b]["ev_bp"] - c_ev[b]["ev_bp"], 4)
                ),
            }
            for b in ("0-10s", "10-30s", "30-120s", "10s+_sink")
        },
    }
    gate = judge_verdict(delta_block, sample_floor_met=sample_floor_met, arm_n=int(a["headline"].get("n") or 0))

    return {
        "CTRL": c,
        "ARM": a,
        "delta_ARM_minus_CTRL": delta_block,
        "primary_kpis": {
            "never_green_rate": {
                "CTRL": c["headline"].get("never_green_rate"),
                "ARM": a["headline"].get("never_green_rate"),
                "delta": delta("never_green_rate"),
            },
            "ae_worsen_rate": {
                "CTRL": c["headline"].get("ae_worsen_rate"),
                "ARM": a["headline"].get("ae_worsen_rate"),
                "delta": delta("ae_worsen_rate"),
            },
            "bucket_ev": delta_block["bucket_ev"],
            "hold_buckets": {
                b: {"CTRL": c["hold_buckets"].get(b), "ARM": a["hold_buckets"].get(b)}
                for b in ("0-10s", "10-30s", "30-120s")
            },
            "sink_10s_plus": {"CTRL": c_ev["10s+_sink"], "ARM": a_ev["10s+_sink"], "delta_pnl_bp": d_sink_pnl, "delta_ev_bp": d_sink_ev},
        },
        "verdict": gate["verdict"],
        "promote": False,
        "promote_label": gate["promote_label"],
        "gate": gate,
        "note": "Daily primary: never_green / ae_worsen / bucket 別EV / 10s+ sink. PROMOTE≠LIVE.",
    }
