"""
PEG Research Data Store
=======================
研究専用。執行には使わない。
- predictions JSONL (生ログ)
- labeled JSONL (正解付け済み)
- daily rollup JSON (日次集計)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

JST = timezone(timedelta(hours=9))
DEFAULT_ROOT = "/home/azureuser/antigravity/data/peg_research"


class PegResearchStore:
    def __init__(self, root: str = DEFAULT_ROOT):
        self.root = root
        self.pred_path = os.path.join(root, "predictions.jsonl")
        self.labeled_path = os.path.join(root, "labeled.jsonl")
        self.state_path = os.path.join(root, "peg_research_state.json")
        self.daily_dir = os.path.join(root, "daily")
        os.makedirs(self.daily_dir, exist_ok=True)
        os.makedirs(root, exist_ok=True)
        self._last_state_write = 0.0

    def _day_key(self, ts: Optional[float] = None) -> str:
        dt = datetime.fromtimestamp(ts or time.time(), JST)
        return dt.strftime("%Y-%m-%d")

    def append_prediction(self, rec: Dict[str, Any]) -> None:
        with open(self.pred_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def append_labeled(self, rec: Dict[str, Any]) -> None:
        with open(self.labeled_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._update_daily(rec)

    def _update_daily(self, rec: Dict[str, Any]) -> None:
        day = self._day_key(rec.get("labeled_ts") or rec.get("ts"))
        path = os.path.join(self.daily_dir, f"{day}.json")
        try:
            data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {
                "date": day,
                "tasks": {},
                "updated_at": None,
            }
        except Exception:
            data = {"date": day, "tasks": {}, "updated_at": None}

        task = rec.get("task", "unknown")
        bucket = data["tasks"].setdefault(task, {
            "n": 0,
            "dir_correct": 0,
            "hit_1_5bp": 0,
            "continue_hit": 0,
            "end_hit": 0,
            "wrong": 0,
            "sum_abs_realized_bp": 0.0,
        })
        bucket["n"] += 1
        outcome = rec.get("outcome") or {}
        if outcome.get("dir_correct"):
            bucket["dir_correct"] += 1
        if outcome.get("hit_1_5bp"):
            bucket["hit_1_5bp"] += 1
        if outcome.get("continue_hit"):
            bucket["continue_hit"] += 1
        if outcome.get("end_hit"):
            bucket["end_hit"] += 1
        if outcome.get("wrong"):
            bucket["wrong"] += 1
        bucket["sum_abs_realized_bp"] += abs(float(rec.get("realized_bp") or 0.0))

        # rates
        n = max(1, bucket["n"])
        bucket["dir_correct_rate"] = round(bucket["dir_correct"] / n, 4)
        bucket["hit_1_5bp_rate"] = round(bucket["hit_1_5bp"] / n, 4)
        bucket["continue_hit_rate"] = round(bucket["continue_hit"] / n, 4)
        bucket["end_hit_rate"] = round(bucket["end_hit"] / n, 4)
        bucket["wrong_rate"] = round(bucket["wrong"] / n, 4)
        bucket["avg_abs_realized_bp"] = round(bucket["sum_abs_realized_bp"] / n, 3)

        data["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def write_state(self, state: Dict[str, Any], min_interval_sec: float = 2.0) -> None:
        now = time.time()
        if now - self._last_state_write < min_interval_sec:
            return
        state = dict(state)
        state["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        state["today"] = self._day_key()
        daily_path = os.path.join(self.daily_dir, f"{state['today']}.json")
        if os.path.exists(daily_path):
            try:
                state["today_rollup"] = json.load(open(daily_path, encoding="utf-8"))
            except Exception:
                pass
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)
        self._last_state_write = now
