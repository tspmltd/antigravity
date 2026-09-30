"""
Trend Follow Agent (方向性 ＆ レジーム判定)
- 価格の中期トレンド・ボラティリティ・市場レジーム(trend/range)を判定
- 周期: 1s〜30s
"""
import time
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import asdict
from ..event_bus import EventBus
from ..schema import OrderbookMicroSnapshot, AgentConclusion
from ..trend_research_store import TrendResearchStore


class TrendFollowAgent:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.mid_history: List[float] = []
        self.latest_state: dict = {}
        self.latest_conclusion: Optional[AgentConclusion] = None
        self.research = TrendResearchStore()
        self._pending: List[Dict[str, Any]] = []
        self._last_pred_ts = 0.0
        self.bus.subscribe("orderbook_micro", self.on_micro_update)

    def on_micro_update(self, snap: OrderbookMicroSnapshot):
        self.mid_history.append(snap.mid_price)
        if len(self.mid_history) > 60:
            self.mid_history.pop(0)

        # 研究: 前方 mid で方向ヒットをラベル（WIRE=NO）
        try:
            self._resolve_trend_research(snap)
        except Exception:
            pass

        # 30サンプル以上で方向判定。未満は WARMUP（偽のトレンドを出さない）
        min_samples = 20
        if len(self.mid_history) >= min_samples:
            price_delta = self.mid_history[-1] - self.mid_history[0]
            p_range = max(self.mid_history) - min(self.mid_history)
            # CSR-564 P1 COLLECT: strength_raw uncapped · strength_clipped = decision strength (FROZEN)
            strength_raw = abs(price_delta) / 2500.0
            strength = min(1.0, strength_raw)
            strength_clipped = strength
            ready = True

            if price_delta >= 400.0:
                direction = "up"
            elif price_delta <= -400.0:
                direction = "down"
            else:
                direction = "neutral"

            if strength >= 0.35:
                regime = "trend"
            elif p_range >= 3500.0:
                regime = "high_vol"
            elif p_range <= 800.0:
                regime = "low_vol"
            else:
                regime = "range"
        else:
            direction, strength, regime = "neutral", 0.0, "warmup"
            price_delta = 0.0
            p_range = 0.0
            strength_raw = 0.0
            strength_clipped = 0.0
            ready = False

        trend_state = {
            "timestamp": snap.timestamp,
            "trend_direction": direction,
            "trend_strength": round(strength, 3),
            "strength_raw": round(strength_raw, 4),
            "strength_clipped": round(strength_clipped, 4),
            "raw_move": round(price_delta, 2),
            "regime_tag": regime,
            "sample_ready": ready,
            "n_samples": len(self.mid_history),
        }
        self.latest_state = trend_state
        self.bus.publish("trend_state", trend_state)

        if not ready:
            verdict = "WARMUP"
            primary_action = "hold"
            explanation = f"サンプル不足 ({len(self.mid_history)}/{min_samples}) — トレンド未確定"
            conf = 0.0
        else:
            verdict = f"{regime.upper()}_{direction.upper()}"
            primary_action = "buy" if direction == "up" else ("sell" if direction == "down" else "hold")
            explanation = f"レジーム: {regime}, トレンド: {direction.upper()} (強度: {strength:.2f}, 変動幅: ¥{price_delta:+.0f})"
            conf = round(strength, 3)

        conclusion = AgentConclusion(
            agent_name="TrendFollowAgent",
            timestamp=snap.timestamp,
            verdict=verdict,
            confidence=conf,
            primary_action=primary_action,
            metrics={
                "trend_direction": direction,
                "trend_strength": strength,
                "strength_raw": strength_raw,
                "strength_clipped": strength_clipped,
                "raw_move": price_delta,
                "regime_tag": regime,
                "price_delta": price_delta,
                "price_range": p_range,
                "sample_ready": ready,
                "n_samples": len(self.mid_history),
            },
            parameters={},
            hard_veto=False,
            emergency_cancel=False,
            explanation=explanation,
        )
        self.latest_conclusion = conclusion
        self.bus.publish("trend_conclusion", asdict(conclusion))
        try:
            if ready and direction in ("up", "down"):
                self._maybe_schedule_pred(
                    snap, direction, strength, regime,
                    strength_raw=strength_raw,
                    strength_clipped=strength_clipped,
                    raw_move=price_delta,
                )
        except Exception:
            pass
        return conclusion

    def _snap_ts(self, snap: OrderbookMicroSnapshot) -> float:
        ts = float(snap.timestamp)
        if ts > 1e12:
            return ts / 1000.0
        if ts > 1e9:
            return ts
        return time.time()

    def _maybe_schedule_pred(
        self,
        snap,
        direction: str,
        strength: float,
        regime: str,
        *,
        strength_raw: float = 0.0,
        strength_clipped: float = 0.0,
        raw_move: float = 0.0,
    ) -> None:
        """CSR-564 P1 COLLECT: multi-horizon forward-mid (logic unchanged)."""
        now = self._snap_ts(snap)
        if now - self._last_pred_ts < 15.0:
            return
        self._last_pred_ts = now
        side = "buy" if direction == "up" else "sell"
        self._pending.append({
            "ts": now,
            "mid": float(snap.mid_price),
            "direction": direction,
            "candidate_side": side,
            "strength": strength,
            "strength_raw": strength_raw,
            "strength_clipped": strength_clipped,
            "raw_move": raw_move,
            "regime": regime,
            "horizons_sec": (1.0, 3.0, 10.0, 30.0, 60.0),
            "fwd": {},
            "task": "direction_fwd_multi_csr564",
            "event": "onset" if strength >= 0.35 else "hold",
        })
        if len(self._pending) > 40:
            self._pending = self._pending[-40:]

    def _resolve_trend_research(self, snap: OrderbookMicroSnapshot) -> None:
        now = self._snap_ts(snap)
        mid = float(snap.mid_price)
        still = []
        for p in self._pending:
            entry = float(p["mid"])
            age = now - float(p["ts"])
            fwd_map = p.setdefault("fwd", {})
            for h in p.get("horizons_sec") or (30.0,):
                key = f"fwd_{int(h)}s"
                if key not in fwd_map and age >= float(h):
                    fwd_map[key] = round((mid - entry) / entry * 10000.0, 3) if entry > 0 else 0.0
            max_h = max(p.get("horizons_sec") or (30.0,))
            if age < float(max_h):
                still.append(p)
                continue
            fwd_bp = float(fwd_map.get("fwd_30s") or fwd_map.get("fwd_60s") or 0.0)
            pred = p["direction"]
            actual = "up" if fwd_bp >= 1.0 else ("down" if fwd_bp <= -1.0 else "flat")
            hit = (pred == actual) or (pred == "up" and fwd_bp > 0) or (pred == "down" and fwd_bp < 0)
            if abs(fwd_bp) < 1.0:
                hit = False
            self.research.append_sample({
                "ts": p["ts"],
                "candidate_ts": p["ts"],
                "candidate_side": p.get("candidate_side"),
                "task": p["task"],
                "event": p.get("event", "hold"),
                "pred_dir": pred,
                "actual_dir": actual,
                "fwd_bp": round(fwd_bp, 3),
                "fwd_1s": fwd_map.get("fwd_1s"),
                "fwd_3s": fwd_map.get("fwd_3s"),
                "fwd_10s": fwd_map.get("fwd_10s"),
                "fwd_30s": fwd_map.get("fwd_30s"),
                "fwd_60s": fwd_map.get("fwd_60s"),
                "dir_hit": bool(hit),
                "strength": p.get("strength"),
                "strength_raw": p.get("strength_raw"),
                "strength_clipped": p.get("strength_clipped"),
                "raw_move": p.get("raw_move"),
                "regime": p.get("regime"),
                "source": "TrendFollowAgent",
                "wire": "NO",
                "enforce": 0,
            })
        self._pending = still

    def note_tf2bp_event(self, event: str, side: str, mid: float, reason: str = "") -> None:
        """TF2BP 入口/出口を教師イベントとして記録（dryrun から呼ぶ）。"""
        self.research.append_sample({
            "ts": time.time(),
            "task": "tf2bp_supervised",
            "event": event,
            "pred_dir": "up" if str(side).lower() == "buy" else "down",
            "actual_dir": None,
            "fwd_bp": 0.0,
            "dir_hit": False,
            "strength": None,
            "regime": None,
            "source": "TF2BP",
            "reason": reason,
            "wire": "NO",
            "note": "label later via pipeline mid; onset/end marker",
        })

    def get_latest_conclusion(self) -> Optional[AgentConclusion]:
        return self.latest_conclusion

