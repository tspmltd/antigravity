"""
TrendFollow Research Store (TF2BP 教師付き方向研究)
================================================
onset / hold / end を日次集計。WIRE=NO / ENFORCE=0。
"""
from __future__ import annotations
import json, os, time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

JST = timezone(timedelta(hours=9))
DEFAULT_ROOT = "/home/azureuser/antigravity/data/trend_research"


class TrendResearchStore:
    def __init__(self, root: str = DEFAULT_ROOT):
        self.root = root
        self.samples = os.path.join(root, "samples.jsonl")
        self.state_path = os.path.join(root, "trend_research_state.json")
        self.daily_dir = os.path.join(root, "daily")
        os.makedirs(self.daily_dir, exist_ok=True)
        os.makedirs(root, exist_ok=True)
        self._counts = {"n": 0, "dir_hit": 0, "onset_n": 0, "end_n": 0}
        self._last_write = 0.0

    def _day(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d")

    def append_sample(self, rec: Dict[str, Any]) -> None:
        with open(self.samples, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._counts["n"] += 1
        if rec.get("dir_hit"):
            self._counts["dir_hit"] += 1
        if rec.get("event") == "onset":
            self._counts["onset_n"] += 1
        if rec.get("event") == "end":
            self._counts["end_n"] += 1
        self._update_daily(rec)
        self._write_state()

    def _update_daily(self, rec: Dict[str, Any]) -> None:
        day = self._day(float(rec.get("ts") or time.time()))
        path = os.path.join(self.daily_dir, f"{day}.json")
        try:
            data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {
                "date": day, "n": 0, "dir_hit": 0, "onset_n": 0, "end_n": 0,
                "sum_fwd_bp": 0.0, "tasks": {},
            }
        except Exception:
            data = {"date": day, "n": 0, "dir_hit": 0, "onset_n": 0, "end_n": 0, "sum_fwd_bp": 0.0, "tasks": {}}
        data["n"] += 1
        if rec.get("dir_hit"):
            data["dir_hit"] += 1
        if rec.get("event") == "onset":
            data["onset_n"] += 1
        if rec.get("event") == "end":
            data["end_n"] += 1
        data["sum_fwd_bp"] += abs(float(rec.get("fwd_bp") or 0.0))
        n = max(1, data["n"])
        data["dir_hit_rate"] = round(data["dir_hit"] / n, 4)
        data["avg_abs_fwd_bp"] = round(data["sum_fwd_bp"] / n, 3)
        data["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        task = rec.get("task") or "direction_fwd"
        tb = data["tasks"].setdefault(task, {"n": 0, "hit": 0})
        tb["n"] += 1
        if rec.get("dir_hit"):
            tb["hit"] += 1
        tb["hit_rate"] = round(tb["hit"] / max(1, tb["n"]), 4)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def _write_state(self) -> None:
        now = time.time()
        if now - self._last_write < 2.0:
            return
        n = max(1, self._counts["n"])
        state = {
            "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "wire": "NO",
            "enforce": 0,
            "counts": dict(self._counts),
            "dir_hit_rate": round(self._counts["dir_hit"] / n, 4),
            "today": self._day(now),
        }
        day_path = os.path.join(self.daily_dir, f"{state['today']}.json")
        if os.path.exists(day_path):
            try:
                state["today_rollup"] = json.load(open(day_path, encoding="utf-8"))
            except Exception:
                pass
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)
        self._last_write = now
