import json
import os
from enum import Enum
from datetime import datetime
from typing import Dict, Any, List


class StrategyStatus(str, Enum):
    PROPOSED = "PROPOSED"
    BACKTESTING = "BACKTESTING"
    OPTIMIZING = "OPTIMIZING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PipelineStateManager:
    """
    パイプラインを通過する全戦略のライフサイクルとメトリクス履歴を追跡・永続化する。
    """

    def __init__(self, state_file: str = "pipeline_state.json"):
        self.state_file = state_file
        self.state: Dict[str, Any] = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"strategies": {}, "history": []}

    def save_state(self):
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def record_transition(self, strat_id: str, status: StrategyStatus, details: Dict[str, Any] = None):
        """状態遷移を記録"""
        now = datetime.now().isoformat()
        if strat_id not in self.state["strategies"]:
            self.state["strategies"][strat_id] = {
                "created_at": now,
                "history": []
            }

        entry = {
            "timestamp": now,
            "status": status.value,
            "details": details or {}
        }
        self.state["strategies"][strat_id]["current_status"] = status.value
        self.state["strategies"][strat_id]["history"].append(entry)
        self.state["history"].append({"strat_id": strat_id, **entry})
        self.save_state()

    def get_summary(self) -> Dict[str, int]:
        """現在のサマリー集計"""
        counts = {status.value: 0 for status in StrategyStatus}
        for s in self.state["strategies"].values():
            curr = s.get("current_status")
            if curr in counts:
                counts[curr] += 1
        return counts
