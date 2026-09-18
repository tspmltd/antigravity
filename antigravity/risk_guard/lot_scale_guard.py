"""
LotScaleGuard: GAPCORE-compliant Dynamic Lot Scaling & Promotion/Demotion Guard
=============================================================================
Enables autonomous scaling of strategy order size (e.g. 0.001 -> 0.002 BTC)
based on real-time empirical trading edge (Profit Factor, MaxDD, Win Rate, Sample Size).

Fail-Closed Philosophy:
- Promotions require STRICT, STATISTICALLY SIGNIFICANT evidence across multiple dimensions (AND condition).
- Demotions trigger RAPIDLY and UNFORGIVINGLY on ANY sign of edge decay or excessive risk (OR condition).
- State is persisted atomically to JSON to survive process restarts without resetting progression.
"""

import os
import json
import time
import tempfile
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, Dict, Any, List, Tuple

JST = timezone(timedelta(hours=9))


def get_jst_now_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


class LotScaleGuard:
    """
    GAPCORE-compliant Dynamic Lot Scaling Engine.
    """

    def __init__(
        self,
        strategy_id: str = "EmaTrend",
        tiers: Optional[List[float]] = None,
        initial_tier_index: int = 0,
        # Promotion (昇格) 条件 - 全てANDで満たす必要あり
        min_trades_count: int = 20,
        min_profit_factor: float = 1.30,
        max_allowed_drawdown: float = 750.0,
        min_win_rate_pct: float = 45.0,
        min_collateral_jpy: float = 6000.0,
        min_hours_between_promotions: float = 2.0,
        # Demotion (降格 / Fail-Closed) 条件 - いずれか1つでも該当すれば即降格
        max_dd_demote_threshold: float = 1000.0,
        min_pf_demote_threshold: float = 1.05,
        max_consecutive_losses: int = 5,
        max_daily_loss_ratio: float = 0.60,
        min_collateral_demote: float = 5000.0,
        # State management
        state_dir: Optional[str] = None,
        on_tier_changed_callback: Optional[Callable[[str, float, float, str, str], None]] = None,
    ):
        self.strategy_id = strategy_id
        self.tiers = tiers or [0.001, 0.002, 0.003]
        self.current_tier_idx = max(0, min(initial_tier_index, len(self.tiers) - 1))

        # Promotion parameters
        self.min_trades_count = min_trades_count
        self.min_profit_factor = min_profit_factor
        self.max_allowed_drawdown = max_allowed_drawdown
        self.min_win_rate_pct = min_win_rate_pct
        self.min_collateral_jpy = min_collateral_jpy
        self.min_hours_between_promotions = min_hours_between_promotions

        # Demotion parameters
        self.max_dd_demote_threshold = max_dd_demote_threshold
        self.min_pf_demote_threshold = min_pf_demote_threshold
        self.max_consecutive_losses = max_consecutive_losses
        self.max_daily_loss_ratio = max_daily_loss_ratio
        self.min_collateral_demote = min_collateral_demote

        self.on_tier_changed_callback = on_tier_changed_callback

        # State storage
        if state_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            state_dir = os.path.join(base_dir, "data")
        self.state_dir = state_dir
        os.makedirs(self.state_dir, exist_ok=True)
        self.state_path = os.path.join(self.state_dir, f"live-lot-scale-{self.strategy_id}.json")

        self.lock = threading.RLock()

        # Tracking state
        self.last_scale_ts: float = 0.0
        self.consecutive_losses: int = 0
        self.history_events: List[Dict[str, Any]] = []

        # Load persisted state if exists, otherwise save initial state
        self._load_state()
        self._save_state()

    @property
    def current_lot_size(self) -> float:
        with self.lock:
            return self.tiers[self.current_tier_idx]

    def record_trade_result(self, pnl: float):
        """トレード損益結果を記録し、連敗カウンタを更新"""
        with self.lock:
            if pnl < 0:
                self.consecutive_losses += 1
            elif pnl > 0:
                self.consecutive_losses = 0
            self._save_state()

    def evaluate(
        self,
        metrics: Dict[str, Any],
        collateral: float,
        daily_loss_used: float = 0.0,
        daily_loss_limit: float = 1500.0,
        now_ts: Optional[float] = None,
    ) -> Tuple[bool, float, str, str]:
        """
        現在のエッジ、ドローダウン、証拠金状態を総合評価し、ロットの昇格・降格を判定。
        
        Returns:
            Tuple[changed: bool, new_lot: float, reason: str, event_type: str]
            event_type: 'PROMOTION' | 'DEMOTION' | 'NONE'
        """
        now = now_ts if now_ts is not None else time.time()

        with self.lock:
            trades_cnt = metrics.get("trades_count", 0)
            pf = metrics.get("profit_factor", 0.0)
            max_dd = metrics.get("max_dd_24h", 0.0)
            wr = metrics.get("win_rate_pct", 0.0)

            old_tier = self.current_tier_idx
            old_lot = self.tiers[old_tier]

            # =========================================================
            # 1. 降格判定 (Demotion - Fail Closed)
            # Tier > 0 の場合、いかなるリスクシグナルでも最優先で即座に縮小
            # =========================================================
            if self.current_tier_idx > 0:
                demote_reasons = []

                if max_dd >= self.max_dd_demote_threshold:
                    demote_reasons.append(f"24h MaxDD悪化 ({max_dd:,.1f}円 >= {self.max_dd_demote_threshold:,.0f}円)")

                if trades_cnt >= 10 and pf < self.min_pf_demote_threshold:
                    demote_reasons.append(f"PF低下・エッジ減衰 (PF: {pf:.2f} < {self.min_pf_demote_threshold:.2f})")

                if self.consecutive_losses >= self.max_consecutive_losses:
                    demote_reasons.append(f"連敗閾値超過 ({self.consecutive_losses}連敗 >= {self.max_consecutive_losses}回)")

                if daily_loss_limit > 0 and (daily_loss_used / daily_loss_limit) >= self.max_daily_loss_ratio:
                    used_pct = (daily_loss_used / daily_loss_limit) * 100.0
                    demote_reasons.append(f"日次損失枠警戒水準到達 ({daily_loss_used:,.0f}円/{daily_loss_limit:,.0f}円, {used_pct:.1f}%)")

                if collateral < self.min_collateral_demote:
                    demote_reasons.append(f"証拠金残高不足 ({collateral:,.1f}円 < {self.min_collateral_demote:,.0f}円)")

                if demote_reasons:
                    self.current_tier_idx = max(0, self.current_tier_idx - 1)
                    new_lot = self.tiers[self.current_tier_idx]
                    reason_str = " / ".join(demote_reasons)
                    self.last_scale_ts = now
                    self._record_history(event="DEMOTION", old_lot=old_lot, new_lot=new_lot, reason=reason_str)
                    self._save_state()

                    if self.on_tier_changed_callback:
                        try:
                            self.on_tier_changed_callback(self.strategy_id, old_lot, new_lot, reason_str, "DEMOTION")
                        except Exception:
                            pass

                    return True, new_lot, reason_str, "DEMOTION"

            # =========================================================
            # 2. 昇格判定 (Promotion - Statistically Proven Edge)
            # 最高Tier未満の場合、厳格な全条件を満たせば自律ロット拡大
            # =========================================================
            if self.current_tier_idx < len(self.tiers) - 1:
                elapsed_hours = (now - self.last_scale_ts) / 3600.0 if self.last_scale_ts > 0 else 999.0

                conditions = {
                    "サンプル数十分": (trades_cnt >= self.min_trades_count, f"{trades_cnt}/{self.min_trades_count}取引"),
                    "高PF維持": (pf >= self.min_profit_factor, f"PF: {pf:.2f} >= {self.min_profit_factor:.2f}"),
                    "低DD維持": (max_dd <= self.max_allowed_drawdown, f"MaxDD: {max_dd:,.1f}円 <= {self.max_allowed_drawdown:,.0f}円"),
                    "勝率基準充足": (wr >= self.min_win_rate_pct, f"勝率: {wr:.1f}% >= {self.min_win_rate_pct:.1f}%"),
                    "証拠金余力十分": (collateral >= self.min_collateral_jpy, f"証拠金: {collateral:,.1f}円 >= {self.min_collateral_jpy:,.0f}円"),
                    "滞在冷却時間充足": (elapsed_hours >= self.min_hours_between_promotions, f"冷却: {elapsed_hours:.1f}h >= {self.min_hours_between_promotions:.1f}h"),
                    "連敗なし": (self.consecutive_losses < 3, f"連敗: {self.consecutive_losses}回 < 3回"),
                }

                all_passed = all(cond[0] for cond in conditions.values())
                if all_passed:
                    self.current_tier_idx += 1
                    new_lot = self.tiers[self.current_tier_idx]
                    reasons = [f"{k}({v[1]})" for k, v in conditions.items()]
                    reason_str = "全昇格基準クリア: " + ", ".join(reasons)
                    self.last_scale_ts = now
                    self._record_history(event="PROMOTION", old_lot=old_lot, new_lot=new_lot, reason=reason_str)
                    self._save_state()

                    if self.on_tier_changed_callback:
                        try:
                            self.on_tier_changed_callback(self.strategy_id, old_lot, new_lot, reason_str, "PROMOTION")
                        except Exception:
                            pass

                    return True, new_lot, reason_str, "PROMOTION"

            return False, old_lot, "変更なし (基準未達または現状維持)", "NONE"

    def get_promotion_progress(self, metrics: Dict[str, Any], collateral: float) -> Dict[str, Any]:
        """次期昇格に向けた各条件の達成状況・進捗を返す"""
        with self.lock:
            trades_cnt = metrics.get("trades_count", 0)
            pf = metrics.get("profit_factor", 0.0)
            max_dd = metrics.get("max_dd_24h", 0.0)
            wr = metrics.get("win_rate_pct", 0.0)

            is_max_tier = (self.current_tier_idx >= len(self.tiers) - 1)
            target_lot = self.tiers[min(len(self.tiers) - 1, self.current_tier_idx + 1)]

            items = [
                {
                    "name": "取引サンプル数",
                    "current": f"{trades_cnt} 回",
                    "target": f"{self.min_trades_count} 回以上",
                    "passed": trades_cnt >= self.min_trades_count,
                },
                {
                    "name": "Profit Factor",
                    "current": f"{pf:.2f}",
                    "target": f"{self.min_profit_factor:.2f} 以上",
                    "passed": pf >= self.min_profit_factor,
                },
                {
                    "name": "24h MaxDD",
                    "current": f"{max_dd:,.1f} 円",
                    "target": f"{self.max_allowed_drawdown:,.0f} 円以下",
                    "passed": max_dd <= self.max_allowed_drawdown,
                },
                {
                    "name": "勝率",
                    "current": f"{wr:.1f}%",
                    "target": f"{self.min_win_rate_pct:.1f}% 以上",
                    "passed": wr >= self.min_win_rate_pct,
                },
                {
                    "name": "証拠金純資産",
                    "current": f"{collateral:,.1f} 円",
                    "target": f"{self.min_collateral_jpy:,.0f} 円以上",
                    "passed": collateral >= self.min_collateral_jpy,
                },
            ]
            passed_count = sum(1 for it in items if it["passed"])
            total_count = len(items)

            return {
                "is_max_tier": is_max_tier,
                "current_tier": self.current_tier_idx,
                "current_lot": self.current_lot_size,
                "target_lot": target_lot,
                "items": items,
                "passed_count": passed_count,
                "total_count": total_count,
                "progress_pct": round(passed_count / total_count * 100.0, 1),
            }

    def _record_history(self, event: str, old_lot: float, new_lot: float, reason: str):
        self.history_events.append({
            "timestamp": time.time(),
            "jst_time": get_jst_now_str(),
            "event": event,
            "old_lot": old_lot,
            "new_lot": new_lot,
            "reason": reason,
        })
        if len(self.history_events) > 50:
            self.history_events = self.history_events[-50:]

    def _save_state(self):
        """原子的 (Atomic) なファイル書き込みで状態を永続化"""
        data = {
            "strategy_id": self.strategy_id,
            "current_tier_idx": self.current_tier_idx,
            "current_lot_size": self.current_lot_size,
            "tiers": self.tiers,
            "last_scale_ts": self.last_scale_ts,
            "consecutive_losses": self.consecutive_losses,
            "history_events": self.history_events,
            "updated_at": get_jst_now_str(),
        }
        dir_name = os.path.dirname(self.state_path)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".lot_scale_", suffix=".tmp")
        try:
            with open(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.state_path)
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            print(f"[LotScaleGuard] State save failed: {e}", flush=True)

    def _load_state(self):
        """保存された状態を復元"""
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.current_tier_idx = data.get("current_tier_idx", self.current_tier_idx)
            self.last_scale_ts = data.get("last_scale_ts", self.last_scale_ts)
            self.consecutive_losses = data.get("consecutive_losses", self.consecutive_losses)
            self.history_events = data.get("history_events", [])
            print(
                f"[LotScaleGuard] 復元完了: {self.strategy_id} Tier={self.current_tier_idx} "
                f"Lot={self.current_lot_size} BTC (更新: {data.get('updated_at')})",
                flush=True
            )
        except Exception as e:
            print(f"[LotScaleGuard] State load failed: {e}", flush=True)
