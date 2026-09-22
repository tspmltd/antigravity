"""
DuckDB Optimizer Agent → Research Librarian
==========================================
旧: Fusion 重みの自動最適化（auto_apply）
新: 全研究レーン結果の日次 usable 判定 + 次実験 ADVISE（Librarian）

WIRE=NO / ENFORCE=0 / approved_weights 自動更新なし
"""
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from dataclasses import asdict

from ..event_bus import EventBus
from ..schema import AgentConclusion
from ..duckdb_research_librarian import DuckDBResearchLibrarian

JST = timezone(timedelta(hours=9))


class DuckDBOptimizerAgent:
    """Research Librarian（互換名 DuckDBOptimizerAgent）。重みは読取のみ。"""

    def __init__(
        self,
        bus: EventBus,
        base_dir: str = "/home/azureuser/antigravity/data/parquet",
        weights_path: str = "/home/azureuser/antigravity/configs/approved_weights.json",
        min_samples_to_optimize: int = 50,
        auto_apply: bool = False,
    ):
        self.bus = bus
        self.base_dir = base_dir
        self.weights_path = weights_path
        self.min_samples_to_optimize = min_samples_to_optimize
        self.auto_apply = False  # 強制 OFF

        self.last_analysis_ts: float = 0.0
        self.last_daily_review_day: str = ""
        self.latest_conclusion: Optional[AgentConclusion] = None
        self.latest_librarian_report: Optional[Dict[str, Any]] = None
        self.current_weights: Dict[str, Any] = {}
        self.librarian = DuckDBResearchLibrarian()
        self._load_current_weights()

        self.bus.subscribe("orderbook_micro", self._on_tick_sample)
        self._sample_counter = 0

    def _load_current_weights(self):
        import json, os
        if os.path.exists(self.weights_path):
            try:
                with open(self.weights_path, "r", encoding="utf-8") as f:
                    self.current_weights = json.load(f)
            except Exception as e:
                print(f"[DuckDBOptimizerAgent] 重み読み込み例外: {e}")

    def _on_tick_sample(self, snap):
        self._sample_counter += 1
        if self._sample_counter % 300 == 0:
            self.analyze_and_conclude()

    def run_daily_librarian(self, day: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        day = day or datetime.now(JST).strftime("%Y-%m-%d")
        if not force and self.last_daily_review_day == day and self.latest_librarian_report:
            return self.latest_librarian_report
        report = self.librarian.review_day(day)
        self.latest_librarian_report = report
        self.last_daily_review_day = day
        self.bus.publish("librarian_daily_report", report)
        self.analyze_and_conclude(librarian_report=report)
        return report

    def analyze_and_conclude(
        self,
        force_apply: Optional[bool] = None,
        librarian_report: Optional[Dict[str, Any]] = None,
    ) -> AgentConclusion:
        now_ms = int(time.time() * 1000)
        if force_apply:
            print("[DuckDBOptimizerAgent] auto_apply 拒否（Librarian / ENFORCE=0）", flush=True)

        report = librarian_report or self.latest_librarian_report
        if report is None:
            try:
                report = self.librarian.review_day()
                self.latest_librarian_report = report
                self.last_daily_review_day = report.get("date", "")
            except Exception as e:
                report = {
                    "portfolio_verdict": "NEED_MORE",
                    "usable_counts": {},
                    "next_experiments": [{"advise": f"librarian error: {e}"}],
                    "lanes": {},
                }

        portfolio = report.get("portfolio_verdict", "NEED_MORE")
        counts = report.get("usable_counts") or {}
        experiments = report.get("next_experiments") or []
        top_adv = [e.get("advise") for e in experiments[:3] if e.get("advise")]

        verdict = f"LIBRARIAN_{portfolio}"
        explanation = (
            f"Librarian: portfolio={portfolio} "
            f"USEFUL={counts.get('USEFUL', 0)} NEED_MORE={counts.get('NEED_MORE', 0)} "
            f"NOT_USEFUL={counts.get('NOT_USEFUL', 0)}. "
            f"次: {'; '.join(top_adv) if top_adv else '継続観測'}。重み自動適用=OFF。"
        )

        w_pressure = dict(self.current_weights.get("W_PRESSURE", {
            "trend": 0.60, "range": 0.40, "high_vol": 0.70, "low_vol": 0.30,
        }))
        w_conflict = dict(self.current_weights.get("W_CONFLICT", {
            "trend": 0.50, "range": 0.80, "high_vol": 0.60, "low_vol": 0.40,
        }))
        max_spread = float(self.current_weights.get("max_spread_jpy", 2500.0))
        lane_usable = {
            name: (lane.get("usable") if isinstance(lane, dict) else None)
            for name, lane in (report.get("lanes") or {}).items()
        }

        conclusion = AgentConclusion(
            agent_name="DuckDBOptimizerAgent",
            timestamp=now_ms,
            verdict=verdict,
            confidence=0.0 if portfolio == "NEED_MORE" else 0.4,
            primary_action="hold",
            metrics={
                "role": "research_librarian",
                "portfolio_verdict": portfolio,
                "usable_counts": counts,
                "lane_usable": lane_usable,
                "next_experiments": experiments[:5],
                "wire": "NO",
                "enforce": 0,
                "auto_apply_weights": False,
            },
            parameters={
                "W_PRESSURE": w_pressure,
                "W_CONFLICT": w_conflict,
                "max_spread_jpy": max_spread,
                "confidence_threshold": 0.65,
                "librarian_note": "weights are frozen snapshot; not optimized today",
            },
            hard_veto=False,
            emergency_cancel=False,
            explanation=explanation[:500],
        )
        self.latest_conclusion = conclusion
        self.last_analysis_ts = time.time()
        self.bus.publish("duckdb_conclusion", asdict(conclusion))
        return conclusion

    def get_latest_conclusion(self) -> AgentConclusion:
        if self.latest_conclusion is None:
            return self.analyze_and_conclude()
        return self.latest_conclusion

    def get_latest_librarian_report(self) -> Optional[Dict[str, Any]]:
        return self.latest_librarian_report
