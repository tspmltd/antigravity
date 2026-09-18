"""
DuckDB Optimizer Agent (4AGENT - 第3のブレイン)
==============================================
Parquet に蓄積された板情報・意思決定・約定データを DuckDB で高速クエリし、
勝敗要因からレジーム別の最適重み (W_PRESSURE, W_CONFLICT) や安全パラメータを算出して
戦略へ動的にフィードバック（ホットリロード）する自律最適化エージェント。
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
import duckdb
from dataclasses import asdict

from ..event_bus import EventBus
from ..schema import AgentConclusion

JST = timezone(timedelta(hours=9))


class DuckDBOptimizerAgent:
    """
    DuckDB データ駆動型最適化エージェント
    """

    def __init__(
        self,
        bus: EventBus,
        base_dir: str = "/home/azureuser/antigravity/data/parquet",
        weights_path: str = "/home/azureuser/antigravity/configs/approved_weights.json",
        min_samples_to_optimize: int = 50,
        auto_apply: bool = True,
    ):
        self.bus = bus
        self.base_dir = base_dir
        self.weights_path = weights_path
        self.min_samples_to_optimize = min_samples_to_optimize
        self.auto_apply = auto_apply

        self.last_analysis_ts: float = 0.0
        self.latest_conclusion: Optional[AgentConclusion] = None
        self.current_weights: Dict[str, Any] = {}
        self._load_current_weights()

        # EventBus に自分自身を登録
        self.bus.subscribe("orderbook_micro", self._on_tick_sample)
        self._sample_counter = 0

    def _load_current_weights(self):
        if os.path.exists(self.weights_path):
            try:
                with open(self.weights_path, "r", encoding="utf-8") as f:
                    self.current_weights = json.load(f)
            except Exception as e:
                print(f"[DuckDBOptimizerAgent] 重み読み込み例外: {e}")

    def _on_tick_sample(self, snap):
        # 100 Tick ごとにバックグラウンド分析をトリガー検討 (または外部呼び出し)
        self._sample_counter += 1
        if self._sample_counter % 120 == 0:  # 約2分〜4分ごと
            self.analyze_and_conclude()

    def analyze_and_conclude(self, force_apply: Optional[bool] = None) -> AgentConclusion:
        """
        DuckDB で蓄積 Parquet を高速解析し、分析結論 (AgentConclusion) を算出
        """
        now_ms = int(time.time() * 1000)
        should_apply = self.auto_apply if force_apply is None else force_apply

        micro_path = os.path.join(self.base_dir, "orderbook_micro", "*", "*.parquet")
        fusion_path = os.path.join(self.base_dir, "fusion_log", "*", "*.parquet")

        stats = {
            "total_snapshots": 0,
            "avg_mid": 0.0,
            "avg_imbalance": 0.0,
            "avg_spread": 0.0,
            "avg_latency_ms": 0.0,
            "total_decisions": 0,
            "win_rate": 0.50,
            "profit_factor": 1.0,
            "delta_w_summary": {},
        }
        delta_w_list = []

        conn = duckdb.connect()
        try:
            # 1. マイクロ板スナップショット集計
            df_micro = conn.execute(f"""
                SELECT 
                    COUNT(*) AS count,
                    ROUND(AVG(mid_price), 1) AS avg_mid,
                    ROUND(AVG(imbalance), 3) AS avg_imb,
                    ROUND(AVG(best_ask - best_bid), 1) AS avg_spread,
                    ROUND(AVG(latency_ms), 1) AS avg_lat
                FROM '{micro_path}'
            """).df()
            if not df_micro.empty and int(df_micro.iloc[0]["count"]) > 0:
                row = df_micro.iloc[0]
                stats["total_snapshots"] = int(row["count"])
                stats["avg_mid"] = float(row["avg_mid"])
                stats["avg_imbalance"] = float(row["avg_imb"])
                stats["avg_spread"] = float(row["avg_spread"])
                stats["avg_latency_ms"] = float(row["avg_lat"])
        except Exception:
            pass

        try:
            # 2. 意思決定ログおよび勝敗要因クエリ
            df_fusion = conn.execute(f"""
                SELECT 
                    COUNT(*) AS total_dec,
                    COUNT(CASE WHEN realized_pnl > 0 THEN 1 END) AS win_cnt,
                    COUNT(CASE WHEN realized_pnl < 0 THEN 1 END) AS lose_cnt,
                    ROUND(COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 0.0), 1) AS total_gain,
                    ROUND(COALESCE(ABS(SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl END)), 0.0), 1) AS total_loss
                FROM '{fusion_path}'
                WHERE action IN ('buy', 'sell')
            """).df()
            if not df_fusion.empty and int(df_fusion.iloc[0]["total_dec"]) > 0:
                frow = df_fusion.iloc[0]
                tot_trades = int(frow["win_cnt"]) + int(frow["lose_cnt"])
                stats["total_decisions"] = int(frow["total_dec"])
                if tot_trades > 0:
                    stats["win_rate"] = round(int(frow["win_cnt"]) / tot_trades, 3)
                    loss_sum = float(frow["total_loss"])
                    gain_sum = float(frow["total_gain"])
                    stats["profit_factor"] = round(gain_sum / max(1.0, loss_sum), 2)
        except Exception:
            pass

        try:
            # 3. レジーム別重み差分 (勝ちトレードの圧力スコア vs 負けトレードの圧力スコア)
            df_w = conn.execute(f"""
                SELECT 
                    regime_tag,
                    COUNT(*) AS samples,
                    ROUND(COALESCE(AVG(CASE WHEN realized_pnl > 0 THEN pressure_score END), 0.0), 3) AS win_avg_p,
                    ROUND(COALESCE(AVG(CASE WHEN realized_pnl < 0 THEN pressure_score END), 0.0), 3) AS lose_avg_p,
                    ROUND(COALESCE(AVG(CASE WHEN realized_pnl > 0 THEN pressure_score END), 0.0) - 
                          COALESCE(AVG(CASE WHEN realized_pnl < 0 THEN pressure_score END), 0.0), 3) AS delta_w
                FROM '{fusion_path}'
                WHERE action IN ('buy', 'sell')
                GROUP BY regime_tag
            """).df()
            if not df_w.empty:
                delta_w_list = df_w.to_dict(orient="records")
                for item in delta_w_list:
                    stats["delta_w_summary"][item["regime_tag"]] = float(item["delta_w"])
        except Exception:
            pass

        # 最適化推奨重み＆スプレッド上限の計算
        new_w_pressure = dict(self.current_weights.get("W_PRESSURE", {
            "trend": 0.60,
            "range": 0.40,
            "high_vol": 0.70,
            "low_vol": 0.30,
        }))
        new_w_conflict = dict(self.current_weights.get("W_CONFLICT", {
            "trend": 0.50,
            "range": 0.80,
            "high_vol": 0.60,
            "low_vol": 0.40,
        }))

        updated = False
        if delta_w_list and len(delta_w_list) > 0:
            for dw in delta_w_list:
                reg = dw["regime_tag"]
                delta = float(dw["delta_w"])
                old_val = float(new_w_pressure.get(reg, 0.50))
                # ラーニングレート 0.15 で安全に更新
                target_val = max(0.15, min(0.90, round(old_val + delta * 0.15, 3)))
                if abs(target_val - old_val) >= 0.01:
                    new_w_pressure[reg] = target_val
                    updated = True

        # スプレッド上限の動的推奨 (平均スプレッド + 1,000円、最大3,000円)
        rec_max_spread = 2500.0
        if stats["avg_spread"] > 500.0:
            rec_max_spread = min(3000.0, max(2000.0, round(stats["avg_spread"] + 800.0, -2)))

        # 結論の構築
        verdict = "WEIGHTS_OPTIMAL" if not updated else "WEIGHTS_ADAPTED"
        confidence = min(1.0, max(0.5, stats["win_rate"]))
        explanation = (
            f"DuckDB解析完了: スナップショット {stats['total_snapshots']:,}件, "
            f"意思決定 {stats['total_decisions']:,}件, 勝率 {stats['win_rate']*100:.1f}%, "
            f"平均スプレッド ¥{stats['avg_spread']:,.0f}。最適スプレッド上限 ¥{rec_max_spread:,.0f}。"
        )
        if updated:
            explanation += f" レジーム別重みを更新適用: {new_w_pressure}"

        conclusion = AgentConclusion(
            agent_name="DuckDBOptimizerAgent",
            timestamp=now_ms,
            verdict=verdict,
            confidence=confidence,
            primary_action="hold",
            metrics=stats,
            parameters={
                "W_PRESSURE": new_w_pressure,
                "W_CONFLICT": new_w_conflict,
                "max_spread_jpy": rec_max_spread,
                "confidence_threshold": 0.65,
            },
            hard_veto=False,
            emergency_cancel=False,
            explanation=explanation,
        )

        # ファイルへ保存（ホットリロード対象）
        if should_apply and updated:
            self._save_weights(new_w_pressure, new_w_conflict, rec_max_spread)

        self.latest_conclusion = conclusion
        self.last_analysis_ts = time.time()
        self.bus.publish("duckdb_conclusion", asdict(conclusion))
        return conclusion

    def _save_weights(self, w_pressure: Dict[str, float], w_conflict: Dict[str, float], max_spread: float):
        try:
            os.makedirs(os.path.dirname(self.weights_path), exist_ok=True)
            data = {
                "W_PRESSURE": w_pressure,
                "W_CONFLICT": w_conflict,
                "max_spread_jpy": max_spread,
                "confidence_threshold": 0.65,
                "updated_at": datetime.now(JST).isoformat(),
                "version": f"opt-{int(time.time())}",
                "updated_by": "DuckDBOptimizerAgent",
            }
            with open(self.weights_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self.current_weights = data
            print(f"[DuckDBOptimizerAgent] ✅ 最新の最適化重みを反映しました: {self.weights_path}")
        except Exception as e:
            print(f"[DuckDBOptimizerAgent] ⚠️ 重み保存エラー: {e}")

    def get_latest_conclusion(self) -> AgentConclusion:
        if self.latest_conclusion is None:
            return self.analyze_and_conclude()
        return self.latest_conclusion
