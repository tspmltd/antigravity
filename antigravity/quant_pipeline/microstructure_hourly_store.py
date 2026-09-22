"""
Microstructure Hourly Board Research Store
==========================================
目的: 人間が直感できない板の癖を細かく蓄積し、1時間ごとに報告する。
      新シグナル候補の判断材料（WIRE=NO / ENFORCE=0）。

集計軸:
  - tip / depth / imbalance / micro_dev / spread
  - taker / cancel / refill / (c−r) / latency
  - pressure / fake_breakout / tip_thin / one_way 時間シェア
  - 同時発生パターン（新シグナル材料）
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

JST = timezone(timedelta(hours=9))
DEFAULT_ROOT = "/home/azureuser/antigravity/data/microstructure"


def _num(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _stats(xs: List[float]) -> Optional[Dict[str, float]]:
    if not xs:
        return None
    xs_s = sorted(xs)
    n = len(xs_s)
    mean = sum(xs_s) / n
    p10 = xs_s[max(0, int(n * 0.10))]
    p50 = xs_s[n // 2]
    p90 = xs_s[min(n - 1, int(n * 0.90))]
    return {
        "n": n,
        "mean": round(mean, 4),
        "p10": round(p10, 4),
        "p50": round(p50, 4),
        "p90": round(p90, 4),
        "min": round(xs_s[0], 4),
        "max": round(xs_s[-1], 4),
    }


class MicrostructureHourlyStore:
    """軽量: ティックはメモリ集計のみ。毎時 JSON 永続化。"""

    FEATURE_KEYS = (
        "spread_bp",
        "imbalance",
        "micro_dev",
        "bid_depth_1",
        "ask_depth_1",
        "total_bid_depth",
        "total_ask_depth",
        "taker_volume_bid",
        "taker_volume_ask",
        "taker_total",
        "taker_aggressiveness",
        "cancel_rate",
        "refill_rate",
        "cancel_minus_refill",
        "latency_ms",
        "tip_ratio_bid_ask",
    )

    def __init__(self, root: str = DEFAULT_ROOT, max_samples_per_hour: int = 2400):
        self.root = root
        self.hourly_dir = os.path.join(root, "hourly")
        self.state_path = os.path.join(root, "microstructure_hourly_state.json")
        self.latest_path = os.path.join(root, "hourly_latest.json")
        os.makedirs(self.hourly_dir, exist_ok=True)
        os.makedirs(root, exist_ok=True)
        self.max_samples = max_samples_per_hour
        self._hour_key: Optional[str] = None
        self._reset_bucket()

    def _hour_key_now(self, ts: Optional[float] = None) -> str:
        dt = datetime.fromtimestamp(ts or time.time(), JST)
        return dt.strftime("%Y-%m-%d_%H")

    def _reset_bucket(self) -> None:
        self._n = 0
        self._feat: Dict[str, List[float]] = {k: [] for k in self.FEATURE_KEYS}
        self._flags = defaultdict(int)
        self._patterns = defaultdict(int)
        self._pressure_buy = 0
        self._pressure_sell = 0
        self._verdicts = defaultdict(int)
        self._started_ts = time.time()
        self._last_mid = 0.0
        self._mid_open = None
        self._mid_close = None

    def ingest(self, snap: Any, micro_state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """板スナップを取り込み。時間が変わったら前時間を flush して返す。"""
        ts_raw = _num(getattr(snap, "timestamp", None) or time.time() * 1000)
        if ts_raw > 1e12:
            ts = ts_raw / 1000.0
        elif ts_raw > 1e9:
            ts = ts_raw
        else:
            ts = time.time()

        hk = self._hour_key_now(ts)
        rolled = None
        if self._hour_key is None:
            self._hour_key = hk
        elif hk != self._hour_key:
            rolled = self._finalize_hour(self._hour_key)
            self._hour_key = hk
            self._reset_bucket()

        if self._n >= self.max_samples:
            # 過密時は間引き（偶数番のみ）
            if self._n % 2 == 1:
                self._n += 1
                return rolled

        mid = _num(getattr(snap, "mid_price", 0.0))
        best_bid = _num(getattr(snap, "best_bid", 0.0))
        best_ask = _num(getattr(snap, "best_ask", 0.0))
        spread = max(0.0, best_ask - best_bid)
        spread_bp = (spread / mid * 10000.0) if mid > 0 else 0.0
        bid1 = _num(getattr(snap, "bid_depth_1", 0.0))
        ask1 = _num(getattr(snap, "ask_depth_1", 0.0))
        tb = _num(getattr(snap, "taker_volume_bid", 0.0))
        ta = _num(getattr(snap, "taker_volume_ask", 0.0))
        cancel = _num(getattr(snap, "cancel_rate", 0.0))
        refill = _num(getattr(snap, "refill_rate", 0.0))
        cr = cancel - refill
        tip_ratio = (bid1 / ask1) if ask1 > 1e-9 else (10.0 if bid1 > 0 else 1.0)

        vals = {
            "spread_bp": spread_bp,
            "imbalance": _num(getattr(snap, "imbalance", 0.0)),
            "micro_dev": _num(getattr(snap, "micro_dev", 0.0)),
            "bid_depth_1": bid1,
            "ask_depth_1": ask1,
            "total_bid_depth": _num(getattr(snap, "total_bid_depth", 0.0)),
            "total_ask_depth": _num(getattr(snap, "total_ask_depth", 0.0)),
            "taker_volume_bid": tb,
            "taker_volume_ask": ta,
            "taker_total": tb + ta,
            "taker_aggressiveness": _num(getattr(snap, "taker_aggressiveness", 0.0)),
            "cancel_rate": cancel,
            "refill_rate": refill,
            "cancel_minus_refill": cr,
            "latency_ms": _num(getattr(snap, "latency_ms", 0.0)),
            "tip_ratio_bid_ask": tip_ratio,
        }
        for k, v in vals.items():
            self._feat[k].append(v)

        ms = micro_state or {}
        p_side = str(ms.get("pressure_side") or "none")
        if p_side == "buy":
            self._pressure_buy += 1
            self._flags["pressure_buy"] += 1
        elif p_side == "sell":
            self._pressure_sell += 1
            self._flags["pressure_sell"] += 1
        if ms.get("fake_breakout_flag") or ms.get("fake_breakout"):
            self._flags["fake_breakout"] += 1
        if ms.get("latency_risk_flag") or ms.get("latency_risk"):
            self._flags["latency_risk"] += 1
        if ms.get("adverse_warning_flag") or ms.get("adverse_warning"):
            self._flags["adverse_warning"] += 1

        tip_thin_bid = bid1 < 0.05
        tip_thin_ask = ask1 < 0.05
        one_way_sell = tb > ta * 2.0 and tb >= 0.01
        one_way_buy = ta > tb * 2.0 and ta >= 0.01
        cancel_spike = cancel >= 0.40 and cr >= 0.15
        imb_extreme = abs(vals["imbalance"]) >= 0.35
        micro_extreme = abs(vals["micro_dev"]) >= 200.0
        taker_confirm = (tb + ta) >= 0.01

        if tip_thin_bid:
            self._flags["tip_thin_bid"] += 1
        if tip_thin_ask:
            self._flags["tip_thin_ask"] += 1
        if one_way_sell:
            self._flags["one_way_sell"] += 1
        if one_way_buy:
            self._flags["one_way_buy"] += 1
        if cancel_spike:
            self._flags["cancel_spike"] += 1
        if imb_extreme and not taker_confirm:
            self._flags["imb_noise_no_taker"] += 1
        if imb_extreme and taker_confirm:
            self._flags["imb_with_taker"] += 1

        # 新シグナル材料: 同時発生パターン
        if tip_thin_bid and one_way_sell:
            self._patterns["tip_thin_bid_x_one_way_sell"] += 1
        if tip_thin_ask and one_way_buy:
            self._patterns["tip_thin_ask_x_one_way_buy"] += 1
        if cancel_spike and not taker_confirm:
            self._patterns["cancel_spike_no_taker"] += 1
        if cancel_spike and taker_confirm:
            self._patterns["cancel_spike_with_taker"] += 1
        if micro_extreme and tip_thin_bid:
            self._patterns["micro_neg_tip_thin"] += 1 if vals["micro_dev"] < 0 else 0
            if vals["micro_dev"] > 0 and tip_thin_ask:
                self._patterns["micro_pos_tip_thin"] += 1
        if p_side != "none" and cancel_spike:
            self._patterns[f"pressure_{p_side}_x_cancel_spike"] += 1
        if imb_extreme and taker_confirm and tip_thin_bid and vals["imbalance"] < 0:
            self._patterns["sell_stack_collapse"] += 1
        if imb_extreme and taker_confirm and tip_thin_ask and vals["imbalance"] > 0:
            self._patterns["buy_stack_collapse"] += 1

        verdict = str(ms.get("verdict") or "")
        if not verdict and p_side != "none":
            verdict = f"{p_side.upper()}_PRESSURE"
        if verdict:
            self._verdicts[verdict] += 1

        if self._mid_open is None and mid > 0:
            self._mid_open = mid
        if mid > 0:
            self._mid_close = mid
            self._last_mid = mid

        self._n += 1
        # 軽量 state は ~30s、部分レポートは ~2min（別プロセス報告用）
        if self._n % 15 == 0:
            self._write_live_state()
        if self._n % 60 == 0:
            partial = self._build_report(self._hour_key or self._hour_key_now(), partial=True)
            tmp2 = self.latest_path + ".tmp"
            with open(tmp2, "w", encoding="utf-8") as f:
                json.dump(partial, f, indent=2, ensure_ascii=False)
            os.replace(tmp2, self.latest_path)
        return rolled

    def flush_partial(self) -> Dict[str, Any]:
        """時間途中でも最新1h窓のスナップショットを返す（定期報告用）。"""
        return self._build_report(self._hour_key or self._hour_key_now(), partial=True)

    def _finalize_hour(self, hour_key: str) -> Dict[str, Any]:
        report = self._build_report(hour_key, partial=False)
        path = os.path.join(self.hourly_dir, f"{hour_key}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        tmp2 = self.latest_path + ".tmp"
        with open(tmp2, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        os.replace(tmp2, self.latest_path)
        self._write_live_state(report)
        return report

    def _build_report(self, hour_key: str, partial: bool) -> Dict[str, Any]:
        n = max(1, self._n)
        feat_stats = {k: _stats(self._feat.get(k) or []) for k in self.FEATURE_KEYS}
        flag_rates = {k: round(v / n, 4) for k, v in sorted(self._flags.items())}
        pattern_rates = {
            k: {"count": v, "rate": round(v / n, 4)}
            for k, v in sorted(self._patterns.items(), key=lambda x: -x[1])
        }
        # 上位パターン（新シグナル材料）
        top_patterns = [
            {"pattern": k, "count": d["count"], "rate": d["rate"]}
            for k, d in list(pattern_rates.items())[:12]
            if d["count"] >= 3
        ]
        mid_move_bp = None
        if self._mid_open and self._mid_close and self._mid_open > 0:
            mid_move_bp = round((self._mid_close - self._mid_open) / self._mid_open * 10000.0, 3)

        signal_hints = self._signal_hints(flag_rates, pattern_rates, feat_stats, n)

        return {
            "hour_key": hour_key,
            "partial": partial,
            "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "purpose": "板状況の細かい解析・新シグナル判断材料",
            "wire": "NO",
            "enforce": 0,
            "n_ticks": self._n,
            "mid_open": self._mid_open,
            "mid_close": self._mid_close,
            "mid_move_bp": mid_move_bp,
            "pressure_share": {
                "buy": round(self._pressure_buy / n, 4),
                "sell": round(self._pressure_sell / n, 4),
                "none": round(1.0 - (self._pressure_buy + self._pressure_sell) / n, 4),
            },
            "flag_rates": flag_rates,
            "feature_stats": feat_stats,
            "top_cooccurrence_patterns": top_patterns,
            "verdict_counts": dict(self._verdicts),
            "signal_candidate_hints": signal_hints,
            "note": "n未達・単時間では採用判定禁止。人間が読めない板癖の定量メモ。",
        }

    def _signal_hints(
        self,
        flag_rates: Dict[str, float],
        pattern_rates: Dict[str, Dict[str, float]],
        feat_stats: Dict[str, Any],
        n: int,
    ) -> List[Dict[str, Any]]:
        hints: List[Dict[str, Any]] = []
        if n < 30:
            hints.append({
                "id": "warmup",
                "priority": "low",
                "text": f"標本 n={n} < 30。この時間はウォームアップのみ。",
            })
            return hints

        def _pat(name: str) -> float:
            return float((pattern_rates.get(name) or {}).get("rate") or 0.0)

        if _pat("tip_thin_bid_x_one_way_sell") >= 0.03:
            hints.append({
                "id": "sig_tip_thin_sell_flow",
                "priority": "high",
                "text": "bid tip薄 + 売り成行偏りが同時多発 → SELL_PRESSURE 系シグナル候補を要検討",
                "rate": _pat("tip_thin_bid_x_one_way_sell"),
            })
        if _pat("tip_thin_ask_x_one_way_buy") >= 0.03:
            hints.append({
                "id": "sig_tip_thin_buy_flow",
                "priority": "high",
                "text": "ask tip薄 + 買い成行偏りが同時多発 → BUY_PRESSURE 系シグナル候補を要検討",
                "rate": _pat("tip_thin_ask_x_one_way_buy"),
            })
        if _pat("cancel_spike_no_taker") >= 0.05 and _pat("cancel_spike_with_taker") < _pat("cancel_spike_no_taker"):
            hints.append({
                "id": "sig_fake_cancel_noise",
                "priority": "medium",
                "text": "cancel急増の多くが成行なし → FAKE_BREAKOUT フィルタ強化候補（単独cancel警報は危険）",
                "rate": _pat("cancel_spike_no_taker"),
            })
        if flag_rates.get("imb_noise_no_taker", 0) >= 0.08:
            hints.append({
                "id": "sig_imb_needs_taker",
                "priority": "medium",
                "text": "極端imbの多くが成行未確認 → imb単独シグナルは棄却、taker確認必須",
                "rate": flag_rates.get("imb_noise_no_taker"),
            })
        if _pat("sell_stack_collapse") >= 0.02 or _pat("buy_stack_collapse") >= 0.02:
            hints.append({
                "id": "sig_stack_collapse",
                "priority": "high",
                "text": "tip枯渇×片方向成行×imb極端の同時発生あり → 崩れ検知シグナル候補",
                "sell_rate": _pat("sell_stack_collapse"),
                "buy_rate": _pat("buy_stack_collapse"),
            })
        cr = feat_stats.get("cancel_minus_refill") or {}
        if (cr.get("p90") or 0) >= 0.25:
            hints.append({
                "id": "sig_cr_tail",
                "priority": "medium",
                "text": f"(c−r) p90={cr.get('p90')} が高尾 → 流動性蒸発テールを特徴に残す価値あり",
            })
        lat = feat_stats.get("latency_ms") or {}
        if (lat.get("p90") or 0) >= 80:
            hints.append({
                "id": "ops_latency",
                "priority": "ops",
                "text": f"latency p90={lat.get('p90')}ms — シグナル評価から高遅延窓を除外推奨",
            })
        if not hints:
            hints.append({
                "id": "quiet",
                "priority": "low",
                "text": "この時間は同時発生パターンが薄い。継続観測。",
            })
        return hints

    def _write_live_state(self, report: Optional[Dict[str, Any]] = None) -> None:
        rep = report or self.flush_partial()
        state = {
            "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "hour_key": rep.get("hour_key"),
            "partial": rep.get("partial", True),
            "n_ticks": rep.get("n_ticks"),
            "pressure_share": rep.get("pressure_share"),
            "top_patterns": (rep.get("top_cooccurrence_patterns") or [])[:5],
            "signal_candidate_hints": rep.get("signal_candidate_hints"),
            "wire": "NO",
            "enforce": 0,
        }
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)
