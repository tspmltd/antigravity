"""
DuckDB Optimizer Agent → Research Librarian
==========================================
旧: Fusion 重みの自動最適化（auto_apply）
新: 全研究レーン結果の日次 usable 判定 + 次実験 ADVISE（Librarian）

WIRE=NO / ENFORCE=0 / approved_weights 自動更新なし

CSR-TF1A: review_day() は同期呼び出し禁止（council deliberate を固める）。
バックグラウンドスレッドで走らせ、結論はキャッシュを返す。
"""
import threading
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
        self._review_lock = threading.Lock()
        self._review_inflight = False
        self._load_current_weights()

        # Never block init/tick on review_day — publish pending then kick async.
        self.analyze_and_conclude(allow_sync_review=False)
        self.schedule_daily_review(force=False)

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

    def _pending_report(self, note: str) -> Dict[str, Any]:
        return {
            "date": datetime.now(JST).strftime("%Y-%m-%d"),
            "portfolio_verdict": "NEED_MORE",
            "usable_counts": {},
            "next_experiments": [{"advise": note}],
            "lanes": {},
            "async_pending": True,
        }

    def _on_tick_sample(self, snap):
        self._sample_counter += 1
        # 生存心拍更新: 30 tick (約60s) ごとに結論タイムスタンプを更新しBusへ通知
        if self._sample_counter % 30 == 0:
            self.analyze_and_conclude(allow_sync_review=False)
        # 日次レビュー再確認: 300 tick (約10分) ごと
        if self._sample_counter % 300 == 0:
            self.schedule_daily_review(force=False)

    def schedule_daily_review(self, day: Optional[str] = None, force: bool = False) -> None:
        """Kick review_day in a daemon thread. Safe to call from tick/deliberate."""
        day = day or datetime.now(JST).strftime("%Y-%m-%d")
        if not force and self.last_daily_review_day == day and self.latest_librarian_report:
            if not self.latest_librarian_report.get("async_pending"):
                return
        with self._review_lock:
            if self._review_inflight:
                return
            self._review_inflight = True

        def _worker():
            try:
                report = self.librarian.review_day(day)
                self.latest_librarian_report = report
                self.last_daily_review_day = day
                self.bus.publish("librarian_daily_report", report)
                self.analyze_and_conclude(librarian_report=report, allow_sync_review=False)
                print(
                    f"[DuckDBOptimizerAgent] async review_day done day={day} "
                    f"portfolio={report.get('portfolio_verdict')}",
                    flush=True,
                )
            except Exception as e:
                print(f"[DuckDBOptimizerAgent] async review_day error: {e}", flush=True)
                try:
                    from antigravity.risk_guard.notifier import DiscordNotifier
                    DiscordNotifier().send_embed(
                        title="🚨 【DuckDBOptimizerAgent エラー】日次分析処理例外",
                        description=(
                            f"**DuckDBOptimizerAgent の非同期日次レビューで例外が発生しました。**\n\n"
                            f"• **エラー内容**: `{e}`\n"
                            f"• **対象日**: `{day}`\n"
                            f"• **状態**: pending レポートにフォールバックしました。"
                        ),
                        color=0xE74C3C,
                        target="alert",
                    )
                except Exception as notify_err:
                    print(f"[DuckDBOptimizerAgent] Discord通知例外: {notify_err}", flush=True)
                self.analyze_and_conclude(
                    librarian_report=self._pending_report(f"librarian error: {e}"),
                    allow_sync_review=False,
                )
            finally:
                with self._review_lock:
                    self._review_inflight = False

        threading.Thread(target=_worker, name="duckdb-librarian-review", daemon=True).start()

    def run_daily_librarian(self, day: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        day = day or datetime.now(JST).strftime("%Y-%m-%d")
        if not force and self.last_daily_review_day == day and self.latest_librarian_report:
            if not self.latest_librarian_report.get("async_pending"):
                return self.latest_librarian_report
        self.schedule_daily_review(day=day, force=force)
        return self.latest_librarian_report or self._pending_report("async review scheduled")

    def analyze_and_conclude(
        self,
        force_apply: Optional[bool] = None,
        librarian_report: Optional[Dict[str, Any]] = None,
        allow_sync_review: bool = False,
    ) -> AgentConclusion:
        now_ms = int(time.time() * 1000)
        if force_apply:
            print("[DuckDBOptimizerAgent] auto_apply 拒否（Librarian / ENFORCE=0）", flush=True)

        report = librarian_report or self.latest_librarian_report
        if report is None:
            if allow_sync_review:
                try:
                    report = self.librarian.review_day()
                    self.latest_librarian_report = report
                    self.last_daily_review_day = report.get("date", "")
                except Exception as e:
                    report = self._pending_report(f"librarian error: {e}")
            else:
                report = self._pending_report("async review pending (non-blocking)")
                self.schedule_daily_review(force=False)

        portfolio = report.get("portfolio_verdict", "NEED_MORE")
        counts = report.get("usable_counts") or {}
        experiments = report.get("next_experiments") or []
        top_adv = [e.get("advise") for e in experiments[:3] if e.get("advise")]
        pending = bool(report.get("async_pending"))

        verdict = f"LIBRARIAN_{portfolio}"
        explanation = (
            f"Librarian{' [ASYNC_PENDING]' if pending else ''}: portfolio={portfolio} "
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
                "async_pending": pending,
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
            return self.analyze_and_conclude(allow_sync_review=False)
        return self.latest_conclusion

    def get_latest_librarian_report(self) -> Optional[Dict[str, Any]]:
        return self.latest_librarian_report
