"""
Adverse Advance Research (先回り予測研究 DATA)
==============================================
目的: ADVERSE を先回り予測するエンジン作成のための教師 DATA。
データ源: UMM 約定 + 直前〜数秒前の板特徴。

研究タスク:
  1. adverse_advance — 食われるか (TOXIC vs 非TOXIC)
  2. move_1_5bp      — |ae_1s| が 1〜5bp に入るか
  3. direction       — 逆行方向 (adverse / favorable / flat)
  4. pre_post_delta  — 入口直前 vs 5s前の特徴量変化

WIRE=NO / ENFORCE=0 / research_only
参照: CSR-113 · CSR-231/232 · CSR-512
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Any, Deque, Dict, List, Optional, Tuple

JST = timezone(timedelta(hours=9))
DEFAULT_ROOT = "/home/azureuser/antigravity/data/adverse_research"

FEATURE_KEYS = [
    "mid",
    "spread",
    "spread_bp",
    "bid_depth_1",
    "ask_depth_1",
    "imbalance",
    "micro_dev",
    "taker_volume_bid",
    "taker_volume_ask",
    "taker_total",
    "opp_taker",
    "own_taker",
    "taker_aggressiveness",
    "cancel_rate",
    "refill_rate",
    "cancel_minus_refill",
    "latency_ms",
]


def _num(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _stats(xs: List[float]) -> Optional[Dict[str, float]]:
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    return {"n": n, "mean": round(sum(xs) / n, 4), "median": round(xs[n // 2], 4)}


def _side_takers(asof: Dict[str, Any], side: str) -> Dict[str, Any]:
    out = dict(asof)
    tb = _num(asof.get("taker_volume_bid")) or 0.0
    ta = _num(asof.get("taker_volume_ask")) or 0.0
    if side == "buy":
        out["opp_taker"] = tb
        out["own_taker"] = ta
    else:
        out["opp_taker"] = ta
        out["own_taker"] = tb
    out["taker_total"] = tb + ta
    return out


def _delta(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """a - b for numeric feature keys."""
    if not a or not b:
        return {}
    d = {}
    for k in FEATURE_KEYS:
        va, vb = _num(a.get(k)), _num(b.get(k))
        if va is None or vb is None:
            continue
        d[k] = round(va - vb, 6)
    return d


class BoardFeatureBuffer:
    def __init__(self, maxlen: int = 400):
        self._buf: Deque[Tuple[float, Dict[str, Any]]] = deque(maxlen=maxlen)

    def push(self, ts: float, feat: Dict[str, Any]) -> None:
        self._buf.append((ts, feat))

    def asof(self, ts: float) -> Optional[Dict[str, Any]]:
        if not self._buf:
            return None
        best = None
        for t, f in self._buf:
            if t <= ts + 0.05:
                best = f
            else:
                break
        if best is None:
            best = self._buf[0][1]
        out = dict(best)
        out["asof_lag_sec"] = round(max(0.0, ts - float(best.get("_ts") or ts)), 3)
        return out

    def latest(self) -> Optional[Dict[str, Any]]:
        return dict(self._buf[-1][1]) if self._buf else None


class AdverseAdvanceResearch:
    """UMM 教師付き・先回り予測研究ストア。"""

    def __init__(self, root: str = DEFAULT_ROOT):
        self.root = root
        self.samples_path = os.path.join(root, "advance_samples.jsonl")
        self.state_path = os.path.join(root, "advance_state.json")
        self.daily_dir = os.path.join(root, "daily")
        os.makedirs(self.daily_dir, exist_ok=True)
        os.makedirs(root, exist_ok=True)
        self._counts: Dict[str, int] = defaultdict(int)
        self._day_feat: Dict[str, Dict[str, Dict[str, List[float]]]] = {}
        self._day_task: Dict[str, Dict[str, Dict[str, int]]] = {}
        self._last_state_write = 0.0

    def _day(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d")

    def simple_adverse_score(self, asof: Dict[str, Any], side: str) -> float:
        """研究用の簡易先回りスコア 0-1（執行に使わない）。"""
        f = _side_takers(asof, side)
        tip = _num(f.get("bid_depth_1" if side == "buy" else "ask_depth_1")) or 0.0
        opp = _num(f.get("opp_taker")) or 0.0
        cr = _num(f.get("cancel_minus_refill")) or 0.0
        imb = _num(f.get("imbalance")) or 0.0
        # buy にとって逆行は売り成行・負 imbalance・薄い bid
        adverse_imb = (-imb) if side == "buy" else imb
        s = 0.0
        s += 0.35 * min(1.0, opp / 0.05)
        s += 0.25 * min(1.0, max(0.0, cr) / 0.5)
        s += 0.20 * min(1.0, max(0.0, adverse_imb) / 0.5)
        s += 0.20 * (1.0 if tip < 0.05 else 0.0)
        return round(min(1.0, max(0.0, s)), 4)

    def ingest_umm_completed(self, rec: Dict[str, Any]) -> None:
        strategy = str(rec.get("strategy") or "")
        if strategy != "UMM":
            return
        meta = rec.get("meta") or {}
        pre = meta.get("asof_board")
        if not pre:
            return
        side = str(rec.get("entry_side") or "").lower()
        pre = _side_takers(pre, side)
        pre5 = meta.get("asof_board_pre5s")
        pre10 = meta.get("asof_board_pre10s")
        if pre5:
            pre5 = _side_takers(pre5, side)
        if pre10:
            pre10 = _side_takers(pre10, side)

        fq = str(rec.get("fill_quality") or "NORMAL_FILL")
        ae1 = _num(rec.get("ae_1s"))
        abs_ae = abs(ae1) if ae1 is not None else None
        # 方向: ポジションから見て逆行が adverse
        if ae1 is None:
            direction = "unknown"
        elif abs_ae < 1.0:
            direction = "flat"
        elif ae1 < 0:
            direction = "adverse"  # AE は被害方向を負で定義
        else:
            direction = "favorable"

        labels = {
            "adverse": fq == "TOXIC_FILL",
            "move_1_5bp": bool(abs_ae is not None and 1.0 <= abs_ae <= 5.0),
            "direction": direction,
            "fill_quality": fq,
            "ae_1s": ae1,
            "mae_bp": _num(rec.get("mae_bp")),
            "realized_pnl_bp": _num(rec.get("realized_pnl_bp")),
        }

        pred_score = _num(meta.get("pred_adverse_score"))
        if pred_score is None:
            pred_score = self.simple_adverse_score(pre, side)
        pred_adverse = pred_score >= 0.45
        pred_dir = "adverse" if pred_adverse else "favorable"

        deltas = {
            "pre_minus_pre5s": _delta(pre, pre5),
            "pre_minus_pre10s": _delta(pre, pre10),
        }

        ts = float(rec.get("entry_time") or time.time())
        sample = {
            "ts": ts,
            "ts_jst": datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M:%S"),
            "source": "UMM",
            "trade_id": rec.get("trade_id"),
            "side": side,
            "research_only": True,
            "wire": "NO",
            "tasks": ["adverse_advance", "move_1_5bp", "direction", "pre_post_delta"],
            "pre": pre,
            "pre5s": pre5,
            "pre10s": pre10,
            "deltas": deltas,
            "labels": labels,
            "pred": {
                "adverse_score": pred_score,
                "adverse": pred_adverse,
                "direction": pred_dir,
            },
            "pred_hit": {
                "adverse_advance": pred_adverse == labels["adverse"],
                "direction": (pred_dir == direction) if direction in ("adverse", "favorable") else None,
            },
        }
        with open(self.samples_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")

        self._counts["UMM_total"] += 1
        self._counts[f"UMM_{fq}"] += 1
        if labels["adverse"]:
            self._counts["UMM_adverse"] += 1
        if sample["pred_hit"]["adverse_advance"]:
            self._counts["pred_adverse_hit"] += 1
        self._counts["pred_adverse_n"] += 1

        day = self._day(ts)
        self._accumulate_day(day, fq, pre, deltas.get("pre_minus_pre5s") or {}, labels, sample["pred_hit"])
        self._persist_daily(day)
        self._write_state()

    def _accumulate_day(
        self,
        day: str,
        fq: str,
        pre: Dict[str, Any],
        d5: Dict[str, float],
        labels: Dict[str, Any],
        pred_hit: Dict[str, Any],
    ) -> None:
        if day not in self._day_feat:
            self._day_feat[day] = {
                "TOXIC_FILL": defaultdict(list),
                "NORMAL_FILL": defaultdict(list),
                "GOOD_FILL": defaultdict(list),
            }
            self._day_task[day] = {
                "adverse_advance": {"n": 0, "hit": 0},
                "move_1_5bp": {"n": 0, "pos": 0},
                "direction_adverse": {"n": 0, "pos": 0},
            }
        bucket = self._day_feat[day].setdefault(fq, defaultdict(list))
        for k in FEATURE_KEYS:
            v = _num(pre.get(k))
            if v is not None:
                bucket[k].append(v)
            dv = _num(d5.get(k))
            if dv is not None:
                bucket[f"d5_{k}"].append(dv)

        tasks = self._day_task[day]
        tasks["adverse_advance"]["n"] += 1
        if pred_hit.get("adverse_advance"):
            tasks["adverse_advance"]["hit"] += 1
        tasks["move_1_5bp"]["n"] += 1
        if labels.get("move_1_5bp"):
            tasks["move_1_5bp"]["pos"] += 1
        if labels.get("direction") in ("adverse", "favorable"):
            tasks["direction_adverse"]["n"] += 1
            if labels.get("direction") == "adverse":
                tasks["direction_adverse"]["pos"] += 1

    def _persist_daily(self, day: str) -> None:
        feat = self._day_feat.get(day) or {}
        toxic = feat.get("TOXIC_FILL") or {}
        normal = feat.get("NORMAL_FILL") or {}
        contrasts = {}
        for k in list(FEATURE_KEYS) + [f"d5_{x}" for x in FEATURE_KEYS]:
            st_t = _stats(list(toxic.get(k) or []))
            st_n = _stats(list(normal.get(k) or []))
            delta = None
            if st_t and st_n:
                delta = round(st_t["mean"] - st_n["mean"], 4)
            contrasts[k] = {"toxic": st_t, "normal": st_n, "delta_t_minus_n": delta}

        ranked = []
        for k, c in contrasts.items():
            d = c.get("delta_t_minus_n")
            st_t, st_n = c.get("toxic") or {}, c.get("normal") or {}
            if d is None or (st_t.get("n") or 0) < 10 or (st_n.get("n") or 0) < 10:
                continue
            ranked.append((abs(d), k, d, st_t.get("n"), st_n.get("n")))
        ranked.sort(reverse=True)

        tasks = self._day_task.get(day) or {}
        adv = tasks.get("adverse_advance") or {"n": 0, "hit": 0}
        m15 = tasks.get("move_1_5bp") or {"n": 0, "pos": 0}
        direction = tasks.get("direction_adverse") or {"n": 0, "pos": 0}

        payload = {
            "date": day,
            "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "purpose": "UMM教師・ADVERSE先回り予測研究",
            "wire": "NO",
            "enforce": 0,
            "n": {
                "toxic": len(toxic.get("bid_depth_1") or toxic.get("imbalance") or []),
                "normal": len(normal.get("bid_depth_1") or normal.get("imbalance") or []),
            },
            "tasks": {
                "adverse_advance": {
                    "n": adv["n"],
                    "pred_hit_rate": round(adv["hit"] / adv["n"], 4) if adv["n"] else None,
                },
                "move_1_5bp": {
                    "n": m15["n"],
                    "positive_rate": round(m15["pos"] / m15["n"], 4) if m15["n"] else None,
                },
                "direction_adverse": {
                    "n": direction["n"],
                    "adverse_rate": round(direction["pos"] / direction["n"], 4) if direction["n"] else None,
                },
            },
            "decisive_pre_features": [
                {"feature": k, "delta_mean": d, "toxic_n": nt, "normal_n": nn}
                for _, k, d, nt, nn in ranked[:15]
            ],
            "contrasts": contrasts,
            "note": "n未達では未確定。経済PASS/FAIL禁止。",
        }
        path = os.path.join(self.daily_dir, f"{day}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def _write_state(self) -> None:
        now = time.time()
        if now - self._last_state_write < 2.0:
            return
        day = self._day(now)
        daily_path = os.path.join(self.daily_dir, f"{day}.json")
        today = None
        if os.path.exists(daily_path):
            try:
                today = json.load(open(daily_path, encoding="utf-8"))
            except Exception:
                today = None
        n = max(1, int(self._counts.get("pred_adverse_n") or 0))
        state = {
            "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "mode": "advance_from_umm",
            "wire": "NO",
            "enforce": 0,
            "counts": dict(self._counts),
            "pred_adverse_hit_rate": round(self._counts.get("pred_adverse_hit", 0) / n, 4),
            "today": day,
            "today_tasks": (today or {}).get("tasks"),
            "today_decisive": (today or {}).get("decisive_pre_features", [])[:8],
        }
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)
        self._last_state_write = now


# 後方互換エイリアス
AdverseVictimAnatomy = AdverseAdvanceResearch
