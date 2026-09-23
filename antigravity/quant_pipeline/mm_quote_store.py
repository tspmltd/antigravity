"""
MM Quote Research Store
=======================
連続クォート研究ログ（WIRE=NO）。執行・重み自動適用には使わない。
- quotes.jsonl / fills.jsonl
- daily rollup（fill_rate · spread_capture proxy · inventory）
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

JST = timezone(timedelta(hours=9))
DEFAULT_ROOT = "/home/azureuser/antigravity/data/mm_research"


class MMQuoteStore:
    def __init__(self, root: str = DEFAULT_ROOT, quote_log_every: int = 5):
        self.root = root
        self.quotes_path = os.path.join(root, "quotes.jsonl")
        self.fills_path = os.path.join(root, "fills.jsonl")
        self.state_path = os.path.join(root, "mm_quote_state.json")
        self.daily_dir = os.path.join(root, "daily")
        os.makedirs(self.daily_dir, exist_ok=True)
        os.makedirs(root, exist_ok=True)
        self.quote_log_every = max(1, int(quote_log_every))
        self._quote_n = 0
        self._last_state_write = 0.0

    def _day_key(self, ts: Optional[float] = None) -> str:
        return datetime.fromtimestamp(ts or time.time(), JST).strftime("%Y-%m-%d")

    def append_quote(self, rec: Dict[str, Any]) -> None:
        self._quote_n += 1
        if self._quote_n % self.quote_log_every != 0:
            # still count for daily quote_ticks
            self._bump_daily_quote(rec)
            return
        with open(self.quotes_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._bump_daily_quote(rec)
        self._maybe_write_state()

    def append_fill(self, rec: Dict[str, Any]) -> None:
        with open(self.fills_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._bump_daily_fill(rec)
        self._maybe_write_state(force=True)

    def _bump_daily_quote(self, rec: Dict[str, Any]) -> None:
        day = self._day_key(rec.get("ts"))
        data = self._load_daily(day)
        t = data["tasks"].setdefault("mm_quote", {
            "n_quote_ticks": 0,
            "n_logged_quotes": 0,
            "sum_spread_bp": 0.0,
            "sum_quote_distance": 0.0,
            "sum_inventory_pnl_bp": 0.0,
            "sum_adverse_selection_bp": 0.0,
            "mode_counts": {},
            "pause_n": 0,
        })
        t["n_quote_ticks"] += 1
        if self._quote_n % self.quote_log_every == 0:
            t["n_logged_quotes"] += 1
        sp = float(rec.get("spread_bp") or 0.0)
        t["sum_spread_bp"] += sp
        qd_b = rec.get("quote_distance_bid")
        qd_a = rec.get("quote_distance_ask")
        if qd_b is not None or qd_a is not None:
            dist = abs(float(qd_b or 0.0)) + abs(float(qd_a or 0.0))
            t["sum_quote_distance"] += dist
        t["sum_inventory_pnl_bp"] += float(rec.get("inventory_pnl_bp") or rec.get("inventory_pnl") or 0.0)
        t["sum_adverse_selection_bp"] += float(rec.get("adverse_selection_bp") or rec.get("adverse_selection") or 0.0)
        mode = str(rec.get("mode") or "unknown")
        t["mode_counts"][mode] = int(t["mode_counts"].get(mode, 0)) + 1
        if mode == "pause":
            t["pause_n"] += 1
        n = max(1, t["n_quote_ticks"])
        t["avg_spread_bp"] = round(t["sum_spread_bp"] / n, 3)
        t["avg_quote_distance"] = round(t["sum_quote_distance"] / n, 3)
        t["avg_inventory_pnl_bp"] = round(t["sum_inventory_pnl_bp"] / n, 3)
        t["avg_adverse_selection_bp"] = round(t["sum_adverse_selection_bp"] / n, 3)
        t["pause_rate"] = round(t["pause_n"] / n, 4)
        if rec.get("spread_capture") is not None or rec.get("spread_capture_bp_avg") is not None:
            t["spread_capture"] = rec.get("spread_capture") if rec.get("spread_capture") is not None else rec.get("spread_capture_bp_avg")
        t["fill_rate"] = rec.get("fill_rate")
        data["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        self._atomic_json_write(os.path.join(self.daily_dir, f"{day}.json"), data)

    def _bump_daily_fill(self, rec: Dict[str, Any]) -> None:
        day = self._day_key(rec.get("ts"))
        data = self._load_daily(day)
        t = data["tasks"].setdefault("mm_fill", {
            "n": 0,
            "opens": 0,
            "reduces": 0,
            "closes": 0,
            "flips": 0,
            "hard_stops": 0,
            "sum_realized_jpy": 0.0,
            "pos_closes": 0,
            "sum_abs_inventory": 0.0,
        })
        t["n"] += 1
        ev = str(rec.get("event") or "")
        if ev == "open_or_add":
            t["opens"] += 1
        elif ev == "reduce":
            t["reduces"] += 1
        elif ev == "close":
            t["closes"] += 1
            if float(rec.get("realized_pnl_jpy") or 0.0) > 0:
                t["pos_closes"] += 1
        elif ev == "flip":
            t["flips"] += 1
        if "HARD_STOP" in str(rec.get("reason") or ""):
            t["hard_stops"] += 1
        t["sum_realized_jpy"] += float(rec.get("realized_pnl_jpy") or 0.0)
        t["sum_abs_inventory"] += abs(float(rec.get("inventory_btc") or 0.0))
        n = max(1, t["n"])
        t["pos_close_rate"] = round(t["pos_closes"] / max(1, t["closes"]), 4) if t["closes"] else None
        t["avg_abs_inventory"] = round(t["sum_abs_inventory"] / n, 6)
        # fill_rate vs quote ticks
        q = data["tasks"].get("mm_quote") or {}
        qt = int(q.get("n_quote_ticks") or 0)
        t["fill_rate"] = round(t["n"] / qt, 4) if qt > 0 else None
        data["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        self._atomic_json_write(os.path.join(self.daily_dir, f"{day}.json"), data)

    def _load_daily(self, day: str) -> Dict[str, Any]:
        path = os.path.join(self.daily_dir, f"{day}.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"date": day, "tasks": {}, "updated_at": None, "wire": "NO", "enforce": 0}

    def _atomic_json_write(self, path: str, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _maybe_write_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_state_write < 5.0:
            return
        self._last_state_write = now
        day = self._day_key()
        daily = self._load_daily(day)
        state = {
            "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "today": day,
            "tasks": daily.get("tasks"),
            "quotes_path": self.quotes_path,
            "fills_path": self.fills_path,
            "wire": "NO",
            "enforce": 0,
            "auto_apply": False,
        }
        self._atomic_json_write(self.state_path, state)
