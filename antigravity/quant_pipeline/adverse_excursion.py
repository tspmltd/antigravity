"""
Adverse Excursion (AE) & Execution Quality Suite
================================================
CSR-113 / Adverse Agent 改善指示書 v1.0 完全準拠

【測定項目】
- S1. Adverse Excursion: ae_100ms, ae_500ms, ae_1s, ae_3s, ae_10s, ae_30s
- S3. Capture Rate     : (実現bp / 理論スプレッドbp) * 100% [80%以上:優秀, 50-80%:普通, 50%未満:要改善]
- A1. Fill Quality     : mid_price, micro_price, queue_rank_estimate -> GOOD_FILL / NORMAL_FILL / TOXIC_FILL
- A2. Time-to-Adverse  : 0-250ms, 250-500ms, 500-1000ms, 1-2s, 2-5s, 5s+ / AFTER 1s 回復率 vs 損失拡大率
- A3. 4レジーム分析    : trend_high_vol, trend_low_vol, range_high_vol, range_low_vol
- B1. Latency分析      : latency_ms, spread, ae_1s, realized_pnl -> 80ms以上:サイズ半減, 120ms以上:停止
- B2. Inventory分析    : inventory, holding_time, pnl
- C1. Agent責任分析    : 戦略別 (UMM, SpreadCaptureMM, TF2BP, PEG_v2, MicroTrend, Scalping) 集計
"""

import os
import time
import json
from typing import Dict, Any, List, Optional
from datetime import datetime


CHECKPOINTS_SEC = [
    ("ae_100ms", 0.100),
    ("ae_500ms", 0.500),
    ("ae_1s", 1.000),
    ("ae_3s", 3.000),
    ("ae_10s", 10.000),
    ("ae_30s", 30.000),
]

TIME_TO_ADVERSE_BUCKETS = [
    "0-250ms",
    "250-500ms",
    "500-1000ms",
    "1-2s",
    "2-5s",
    "5s+",
]


class ActiveEntryRecord:
    """追跡中の単一約定レコード (全指標統合)"""

    def __init__(
        self,
        trade_id: str,
        side: str,
        entry_price: float,
        entry_time: float,
        strategy_name: str = "default",
        mid_price: float = 0.0,
        micro_price: float = 0.0,
        queue_rank_estimate: int = 1,
        regime: str = "range_low_vol",
        latency_ms: float = 0.0,
        spread_bp: float = 0.0,
        inventory: float = 0.0,
        meta: Optional[Dict[str, Any]] = None,
    ):
        self.trade_id = trade_id
        self.side = side.lower()  # "buy" or "sell"
        self.entry_price = float(entry_price)
        self.entry_time = float(entry_time)
        self.strategy_name = strategy_name

        # A1. Fill Quality 用
        self.mid_price = float(mid_price) if mid_price > 0 else self.entry_price
        self.micro_price = float(micro_price) if micro_price > 0 else self.entry_price
        self.queue_rank_estimate = int(queue_rank_estimate)
        self.fill_quality: str = "NORMAL_FILL"  # GOOD_FILL / NORMAL_FILL / TOXIC_FILL

        # A3. 4レジーム分類
        # trend_high_vol / trend_low_vol / range_high_vol / range_low_vol
        self.regime = self._normalize_regime(regime)

        # B1. Latency & B2. Inventory 用
        self.latency_ms = float(latency_ms)
        self.spread_bp = float(spread_bp)
        self.inventory = float(inventory)

        self.meta = meta or {}

        # S1. 測定結果
        self.excursions: Dict[str, Optional[float]] = {
            name: None for name, _ in CHECKPOINTS_SEC
        }
        self.is_completed = False
        self.completed_time: Optional[float] = None
        self.mae_bp: float = 0.0  # 最大逆行 (Maximum Adverse Excursion)
        self.mfe_bp: float = 0.0  # 最大順行 (Maximum Favorable Excursion)

        # A2. Time-to-Adverse 用 (逆行ピーク到達時間)
        self.peak_adverse_time: Optional[float] = None
        self.time_to_adverse_bucket: str = "5s+"

        # S3. エグジット情報 (Capture Rate)
        self.exit_price: Optional[float] = None
        self.exit_time: Optional[float] = None
        self.holding_time_sec: float = 0.0
        self.realized_pnl_bp: Optional[float] = None
        self.theory_spread_bp: float = self.spread_bp if self.spread_bp > 0 else 1.5
        self.capture_rate: Optional[float] = None
        self.capture_grade: Optional[str] = None  # 優秀 / 普通 / 要改善

    def _normalize_regime(self, raw_regime: str) -> str:
        """4レジームへの正規化"""
        r = raw_regime.lower()
        if "trend" in r:
            return "trend_high_vol" if "high" in r else "trend_low_vol"
        else:
            return "range_high_vol" if "high" in r else "range_low_vol"

    def update_price(
        self,
        current_price: float,
        current_time: float,
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
    ):
        if self.is_completed:
            return

        elapsed = current_time - self.entry_time
        if elapsed < 0:
            return

        # 評価価格
        if self.side == "buy":
            eval_px = best_bid if best_bid is not None and best_bid > 0 else current_price
            diff_bp = ((eval_px - self.entry_price) / self.entry_price) * 10000.0
        else:
            eval_px = best_ask if best_ask is not None and best_ask > 0 else current_price
            diff_bp = ((self.entry_price - eval_px) / self.entry_price) * 10000.0

        # MAE / MFE の追跡
        if diff_bp < self.mae_bp:
            self.mae_bp = diff_bp
            self.peak_adverse_time = elapsed
            self.time_to_adverse_bucket = self._get_time_bucket(elapsed)

        if diff_bp > self.mfe_bp:
            self.mfe_bp = diff_bp

        # チェックポイント
        all_filled = True
        for name, sec in CHECKPOINTS_SEC:
            if self.excursions[name] is None:
                if elapsed >= sec:
                    self.excursions[name] = round(diff_bp, 1)
                else:
                    all_filled = False

        # A1. Fill Quality の分類 (1秒到達時)
        if self.excursions["ae_1s"] is not None:
            ae_1 = self.excursions["ae_1s"]
            if ae_1 > 0.0:
                self.fill_quality = "GOOD_FILL"
            elif ae_1 < -1.5:
                self.fill_quality = "TOXIC_FILL"
            else:
                self.fill_quality = "NORMAL_FILL"

        if all_filled:
            self.is_completed = True
            self.completed_time = current_time

    def _get_time_bucket(self, sec: float) -> str:
        if sec < 0.250:
            return "0-250ms"
        elif sec < 0.500:
            return "250-500ms"
        elif sec < 1.000:
            return "500-1000ms"
        elif sec < 2.000:
            return "1-2s"
        elif sec < 5.000:
            return "2-5s"
        else:
            return "5s+"

    def record_exit(self, exit_price: float, exit_time: float, realized_pnl_bp: float, theory_spread_bp: Optional[float] = None):
        """エグジット時の決済・Capture Rate 確定"""
        self.exit_price = float(exit_price)
        self.exit_time = float(exit_time)
        self.holding_time_sec = round(self.exit_time - self.entry_time, 1)
        self.realized_pnl_bp = round(realized_pnl_bp, 2)
        if theory_spread_bp and theory_spread_bp > 0:
            self.theory_spread_bp = theory_spread_bp

        # S3. Capture Rate の計算: 実現bp / 理論スプレッドbp
        if self.theory_spread_bp > 0:
            cr_pct = (self.realized_pnl_bp / self.theory_spread_bp) * 100.0
            self.capture_rate = round(cr_pct, 1)

            # 判定: 80%以上: 優秀 / 50-80%: 普通 / 50%未満: 要改善
            if cr_pct >= 80.0:
                self.capture_grade = "優秀"
            elif cr_pct >= 50.0:
                self.capture_grade = "普通"
            else:
                self.capture_grade = "要改善"

    def to_standard_dict(self) -> Dict[str, Any]:
        """指示書 v1.0 準拠の保存フォーマット"""
        res: Dict[str, Any] = {
            "entry_price": int(self.entry_price) if self.entry_price.is_integer() else self.entry_price,
        }
        for name, _ in CHECKPOINTS_SEC:
            if self.excursions[name] is not None:
                res[name] = self.excursions[name]
        return res

    def to_full_dict(self) -> Dict[str, Any]:
        """完全メタデータ付きレコード (全分析項目)"""
        data = self.to_standard_dict()
        data.update({
            "trade_id": self.trade_id,
            "strategy": self.strategy_name,
            "entry_side": self.side,
            "mid_price": self.mid_price,
            "micro_price": self.micro_price,
            "queue_rank_estimate": self.queue_rank_estimate,
            "fill_quality": self.fill_quality,
            "regime": self.regime,
            "latency_ms": self.latency_ms,
            "spread_bp": self.spread_bp,
            "inventory": self.inventory,
            "entry_time": self.entry_time,
            "entry_time_jst": datetime.fromtimestamp(self.entry_time).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "mae_bp": round(self.mae_bp, 1),
            "mfe_bp": round(self.mfe_bp, 1),
            "time_to_adverse": self.time_to_adverse_bucket,
            "is_completed": self.is_completed,
            "exit_price": self.exit_price,
            "holding_time_sec": self.holding_time_sec,
            "realized_pnl_bp": self.realized_pnl_bp,
            "theory_spread_bp": self.theory_spread_bp,
            "capture_rate": self.capture_rate,
            "capture_grade": self.capture_grade,
        })
        if self.meta:
            data["meta"] = self.meta
        return data


class AdverseExcursionTracker:
    """
    Adverse Excursion & Execution Quality 総合分析エンジン
    """

    def __init__(
        self,
        save_dir: str = "/home/azureuser/antigravity/data",
        max_active_records: int = 150,
        max_history_records: int = 1000,
    ):
        self.save_dir = save_dir
        self.max_active_records = max_active_records
        self.max_history_records = max_history_records

        self.active_records: List[ActiveEntryRecord] = []
        self.history_records: List[ActiveEntryRecord] = []

        self.jsonl_path = os.path.join(self.save_dir, "adverse_excursion_records.jsonl")
        self.latest_json_path = os.path.join(self.save_dir, "adverse_excursion_latest.json")
        self.summary_json_path = os.path.join(self.save_dir, "adverse_excursion_summary.json")

        os.makedirs(self.save_dir, exist_ok=True)

    def track_entry(
        self,
        trade_id: str,
        side: str,
        entry_price: float,
        entry_time: Optional[float] = None,
        strategy_name: str = "default",
        mid_price: float = 0.0,
        micro_price: float = 0.0,
        queue_rank_estimate: int = 1,
        regime: str = "range_low_vol",
        latency_ms: float = 0.0,
        spread_bp: float = 0.0,
        inventory: float = 0.0,
        meta: Optional[Dict[str, Any]] = None,
    ) -> ActiveEntryRecord:
        """新規約定のAE追跡を開始"""
        now = time.time() if entry_time is None else entry_time
        record = ActiveEntryRecord(
            trade_id=trade_id,
            side=side,
            entry_price=entry_price,
            entry_time=now,
            strategy_name=strategy_name,
            mid_price=mid_price,
            micro_price=micro_price,
            queue_rank_estimate=queue_rank_estimate,
            regime=regime,
            latency_ms=latency_ms,
            spread_bp=spread_bp,
            inventory=inventory,
            meta=meta,
        )
        self.active_records.append(record)

        if len(self.active_records) > self.max_active_records:
            oldest = self.active_records.pop(0)
            self.history_records.append(oldest)

        return record

    def record_exit(
        self,
        trade_id: str,
        exit_price: float,
        exit_time: Optional[float] = None,
        realized_pnl_bp: float = 0.0,
        theory_spread_bp: Optional[float] = None,
    ):
        """トレード手仕舞い時の記録 (Capture Rate 確定)"""
        now = time.time() if exit_time is None else exit_time
        for r in list(self.active_records) + list(self.history_records):
            if r.trade_id == trade_id:
                r.record_exit(exit_price, now, realized_pnl_bp, theory_spread_bp)
                break

    def on_close(
        self,
        trade_id: str,
        exit_price: float,
        pnl_bp: float = 0.0,
        exit_reason: str = "",
        theory_spread_bp: Optional[float] = None,
    ):
        """トレード決済時のフック (Capture Rate 計算とサマリー永続化)"""
        self.record_exit(
            trade_id=trade_id,
            exit_price=exit_price,
            exit_time=time.time(),
            realized_pnl_bp=pnl_bp,
            theory_spread_bp=theory_spread_bp,
        )
        self._persist_latest_and_summary()

    def on_tick(
        self,
        current_price: float,
        timestamp: Optional[float] = None,
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        now = time.time() if timestamp is None else timestamp
        just_completed = []
        still_active = []

        for r in self.active_records:
            r.update_price(
                current_price=current_price,
                current_time=now,
                best_bid=best_bid,
                best_ask=best_ask,
            )
            if r.is_completed:
                just_completed.append(r)
                self.history_records.append(r)
                self._persist_single_record(r)
            else:
                if now - r.entry_time > 35.0:
                    r.is_completed = True
                    just_completed.append(r)
                    self.history_records.append(r)
                    self._persist_single_record(r)
                else:
                    still_active.append(r)

        self.active_records = still_active

        if len(self.history_records) > self.max_history_records:
            self.history_records = self.history_records[-self.max_history_records:]

        if just_completed:
            self._persist_latest_and_summary()

        return [r.to_standard_dict() for r in just_completed]

    def _persist_single_record(self, record: ActiveEntryRecord):
        try:
            full_data = record.to_full_dict()
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(full_data, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[AETracker] ⚠️ JSONL保存エラー: {e}")

    def _persist_latest_and_summary(self):
        try:
            if not self.history_records:
                return

            latest = self.history_records[-1]
            latest_data = latest.to_standard_dict()

            tmp_latest = self.latest_json_path + ".tmp"
            with open(tmp_latest, "w", encoding="utf-8") as f:
                json.dump(latest_data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_latest, self.latest_json_path)

            summary_data = self.get_summary_stats()
            tmp_summary = self.summary_json_path + ".tmp"
            with open(tmp_summary, "w", encoding="utf-8") as f:
                json.dump(summary_data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_summary, self.summary_json_path)

        except Exception as e:
            print(f"[AETracker] ⚠️ 最新サマリー保存エラー: {e}")

    def get_latest_standard_record(self) -> Dict[str, Any]:
        if self.history_records:
            return self.history_records[-1].to_standard_dict()
        if self.active_records:
            return self.active_records[-1].to_standard_dict()
        return {}

    def get_worst_10_trades(self) -> List[Dict[str, Any]]:
        """最悪逆行トレード (MAE最小 / 逆行最大) 上位10件"""
        all_records = self.history_records + self.active_records
        sorted_records = sorted(all_records, key=lambda r: r.mae_bp)
        return [r.to_full_dict() for r in sorted_records[:10]]

    def get_summary_stats(self) -> Dict[str, Any]:
        """Adverse Agent 改善指示書 v1.0 完全集計サマリー"""
        records = self.history_records
        if not records:
            return {
                "total_tracked_trades": 0,
                "avg_ae": {},
                "worst_10": [],
                "fill_quality": {},
                "after_1s_recovery": {},
                "regime_stats": {},
                "latency_stats": {},
                "capture_rate_stats": {},
                "agent_attribution": {},
            }

        n = len(records)

        # 1. 各チェックポイント平均 AE (S1)
        avg_ae = {}
        for name, _ in CHECKPOINTS_SEC:
            vals = [r.excursions[name] for r in records if r.excursions.get(name) is not None]
            avg_ae[name] = round(sum(vals) / len(vals), 2) if vals else 0.0

        # 2. Worst 10 トレード (Discord 投稿用)
        worst_10 = self.get_worst_10_trades()

        # 3. Fill Quality 分類 (A1)
        fq_counts = {"GOOD_FILL": 0, "NORMAL_FILL": 0, "TOXIC_FILL": 0}
        for r in records:
            fq_counts[r.fill_quality] = fq_counts.get(r.fill_quality, 0) + 1

        # 4. Time-to-Adverse & AFTER 1s 回復率分析 (A2)
        # 1秒後に損失が出ていたトレードのうち、3秒・10秒で回復した割合
        loss_at_1s = [r for r in records if r.excursions.get("ae_1s") is not None and r.excursions["ae_1s"] < 0]
        recovered_at_3s = sum(1 for r in loss_at_1s if r.excursions.get("ae_3s") is not None and r.excursions["ae_3s"] > r.excursions["ae_1s"])
        expanded_at_3s = len(loss_at_1s) - recovered_at_3s
        recovery_rate_pct = round((recovered_at_3s / len(loss_at_1s) * 100.0), 1) if loss_at_1s else 0.0
        expansion_rate_pct = round((expanded_at_3s / len(loss_at_1s) * 100.0), 1) if loss_at_1s else 0.0

        # 5. 4レジーム別分析 (A3)
        # trend_high_vol / trend_low_vol / range_high_vol / range_low_vol
        regimes = ["trend_high_vol", "trend_low_vol", "range_high_vol", "range_low_vol"]
        regime_stats = {}
        for reg in regimes:
            reg_records = [r for r in records if r.regime == reg]
            if reg_records:
                reg_n = len(reg_records)
                avg_mae = round(sum(r.mae_bp for r in reg_records) / reg_n, 2)
                avg_mfe = round(sum(r.mfe_bp for r in reg_records) / reg_n, 2)
                pnl_list = [r.realized_pnl_bp for r in reg_records if r.realized_pnl_bp is not None]
                if pnl_list:
                    win_cnt = sum(1 for p in pnl_list if p > 0)
                    wr = round(win_cnt / len(pnl_list) * 100.0, 1)
                    ev = round(sum(pnl_list) / len(pnl_list), 2)
                else:
                    wr = 0.0
                    ev = 0.0
                regime_stats[reg] = {
                    "count": reg_n,
                    "mae": avg_mae,
                    "mfe": avg_mfe,
                    "win_rate_pct": wr,
                    "expected_bp": ev,
                }
            else:
                regime_stats[reg] = {"count": 0, "mae": 0.0, "mfe": 0.0, "win_rate_pct": 0.0, "expected_bp": 0.0}

        # 6. Latency 分析 (B1)
        # <50ms, 50-80ms, 80-120ms, >=120ms
        lat_buckets = {"under_50ms": [], "50_80ms": [], "80_120ms": [], "over_120ms": []}
        for r in records:
            if r.latency_ms < 50.0:
                lat_buckets["under_50ms"].append(r)
            elif r.latency_ms < 80.0:
                lat_buckets["50_80ms"].append(r)
            elif r.latency_ms < 120.0:
                lat_buckets["80_120ms"].append(r)
            else:
                lat_buckets["over_120ms"].append(r)

        latency_stats = {}
        for k, v in lat_buckets.items():
            if v:
                avg_ae1 = round(sum(r.excursions.get("ae_1s", 0) or 0 for r in v) / len(v), 2)
                latency_stats[k] = {"count": len(v), "avg_ae_1s": avg_ae1}
            else:
                latency_stats[k] = {"count": 0, "avg_ae_1s": 0.0}

        # 7. Capture Rate 分析 (S3)
        cr_records = [r for r in records if r.capture_rate is not None]
        if cr_records:
            avg_cr = round(sum(r.capture_rate for r in cr_records) / len(cr_records), 1)
            cr_grade = "優秀" if avg_cr >= 80.0 else ("普通" if avg_cr >= 50.0 else "要改善")
            cr_stats = {
                "total_completed": len(cr_records),
                "avg_capture_rate_pct": avg_cr,
                "grade": cr_grade,
                "excellent_count": sum(1 for r in cr_records if r.capture_grade == "優秀"),
                "normal_count": sum(1 for r in cr_records if r.capture_grade == "普通"),
                "poor_count": sum(1 for r in cr_records if r.capture_grade == "要改善"),
            }
        else:
            cr_stats = {"total_completed": 0, "avg_capture_rate_pct": 0.0, "grade": "判定不能"}

        # 8. Agent 責任分析 (C1)
        agents = ["UMM", "SpreadCaptureMM", "TF2BP", "TF2BP_PEG_v2", "MicroTrend", "Scalping"]
        agent_attribution = {}
        for ag in agents:
            ag_records = [r for r in records if ag.lower() in r.strategy_name.lower()]
            if ag_records:
                ag_n = len(ag_records)
                ag_mae = round(sum(r.mae_bp for r in ag_records) / ag_n, 2)
                ag_ae1 = round(sum(r.excursions.get("ae_1s", 0) or 0 for r in ag_records) / ag_n, 2)
                toxic_cnt = sum(1 for r in ag_records if r.fill_quality == "TOXIC_FILL")
                agent_attribution[ag] = {
                    "trades": ag_n,
                    "avg_mae": ag_mae,
                    "avg_ae_1s": ag_ae1,
                    "toxic_fill_rate": round(toxic_cnt / ag_n * 100.0, 1),
                }

        # 9. Adverse Score 帯別 妥当性分析テーブル (ユーザー指示書要求)
        # Score帯: 0-20, 20-40, 40-60, 60-80, 80-100
        # 測定項目: 件数, 勝率(%), 期待値(bp), AE_1s(bp), CaptureRate(%)
        # 成功条件: Score上昇 -> 期待値悪化 (単調性判定)
        score_bins = [
            ("0-20", 0.0, 20.0),
            ("20-40", 20.0, 40.0),
            ("40-60", 40.0, 60.0),
            ("60-80", 60.0, 80.0),
            ("80-100", 80.0, 100.0),
        ]
        score_validation_table = {}
        expected_pnls = []

        for label, low, high in score_bins:
            bin_records = []
            for r in records:
                score = 30.0
                if r.meta and "adverse_score" in r.meta:
                    score = float(r.meta["adverse_score"])
                elif r.fill_quality == "TOXIC_FILL":
                    score = 75.0
                elif r.fill_quality == "GOOD_FILL":
                    score = 15.0

                if (low <= score < high) or (high == 100.0 and score >= high):
                    bin_records.append(r)

            b_count = len(bin_records)
            if b_count > 0:
                completed = [r for r in bin_records if r.realized_pnl_bp is not None]
                win_cnt = sum(1 for r in completed if r.realized_pnl_bp > 0)
                win_rate = round(win_cnt / len(completed) * 100.0, 1) if completed else 0.0
                exp_pnl = round(sum(r.realized_pnl_bp for r in completed) / len(completed), 2) if completed else 0.0
                ae_1s_vals = [r.excursions["ae_1s"] for r in bin_records if r.excursions.get("ae_1s") is not None]
                avg_ae1 = round(sum(ae_1s_vals) / len(ae_1s_vals), 2) if ae_1s_vals else 0.0
                cr_vals = [r.capture_rate for r in bin_records if r.capture_rate is not None]
                avg_cr = round(sum(cr_vals) / len(cr_vals), 1) if cr_vals else 0.0

                expected_pnls.append(exp_pnl)
                score_validation_table[label] = {
                    "count": b_count,
                    "completed_count": len(completed),
                    "win_rate_pct": win_rate,
                    "expected_pnl_bp": exp_pnl,
                    "avg_ae_1s": avg_ae1,
                    "avg_capture_rate": avg_cr,
                }
            else:
                score_validation_table[label] = {
                    "count": 0,
                    "completed_count": 0,
                    "win_rate_pct": 0.0,
                    "expected_pnl_bp": 0.0,
                    "avg_ae_1s": 0.0,
                    "avg_capture_rate": 0.0,
                }

        # 単調性判定: 有効なbinで期待値が単調減少しているか
        valid_exp = [score_validation_table[k]["expected_pnl_bp"] for k, _, _ in score_bins if score_validation_table[k]["completed_count"] >= 3]
        is_monotonic = True
        if len(valid_exp) >= 2:
            for i in range(len(valid_exp) - 1):
                if valid_exp[i] < valid_exp[i + 1]:
                    is_monotonic = False
                    break
        else:
            is_monotonic = None  # データ蓄積中

        return {
            "total_tracked_trades": n,
            "avg_ae": avg_ae,
            "worst_10": worst_10,
            "fill_quality": fq_counts,
            "after_1s_recovery": {
                "recovery_rate_pct": recovery_rate_pct,
                "loss_expansion_pct": expansion_rate_pct,
            },
            "regime_stats": regime_stats,
            "latency_stats": latency_stats,
            "capture_rate_stats": cr_stats,
            "agent_attribution": agent_attribution,
            "score_validation": {
                "table": score_validation_table,
                "is_monotonic_decline": is_monotonic,
                "status": "VALIDATED" if is_monotonic is True else ("ACCUMULATING" if is_monotonic is None else "CHECK_REQUIRED"),
            },
            "latest_record": records[-1].to_standard_dict() if records else {},
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
