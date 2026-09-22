"""
Signal Fusion Engine (意思決定コア)
- トレンド係 (方向性・レジーム) と 板の癖係 (瞬間圧力・リスク) を統合
- レジーム別重みパラメータにより最終確信度 (final_confidence) を算出
- 意思決定ログ (FusionDecisionLog) を Parquet へ非同期蓄積
"""
import time
from typing import Dict, Any, Optional
from dataclasses import asdict

from .event_bus import EventBus
from .parquet_logger import ParquetBatchLogger
from .schema import FusionDecisionLog


class SignalFusionEngine:
    def __init__(
        self,
        bus: EventBus,
        logger: ParquetBatchLogger,
        confidence_threshold: float = 0.65,
    ):
        self.bus = bus
        self.logger = logger
        self.confidence_threshold = confidence_threshold

        self.latest_trend: Dict[str, Any] = {
            "trend_direction": "neutral",
            "trend_strength": 0.0,
            "regime_tag": "range",
        }
        self.latest_micro: Dict[str, Any] = {
            "pressure_side": "none",
            "pressure_score": 0.0,
            "fake_breakout_flag": False,
            "latency_risk_flag": False,
        }
        self.latest_adverse: Dict[str, Any] = {
            "adverse_side": "none",
            "adverse_score": 0.0,
            "cancel_recommendation": False,
            "lead_ms_estimated": 0.0,
        }

        self.weights_file = "/home/azureuser/antigravity/configs/approved_weights.json"
        self._last_weights_mtime: float = 0.0
        # レジーム別重み設定 (過去ログのDuckDB分析から動的更新可能)
        self.W_PRESSURE = {
            "trend": 0.60,
            "range": 0.40,
            "high_vol": 0.70,
            "low_vol": 0.30,
        }
        self.W_CONFLICT = {
            "trend": 0.50,
            "range": 0.80,
            "high_vol": 0.60,
            "low_vol": 0.40,
        }
        self.load_weights(self.weights_file)

        self.bus.subscribe("trend_state", self._on_trend_update)
        self.bus.subscribe("micro_state", self._on_micro_update)
        self.bus.subscribe("adverse_research_state", self._on_adverse_update)

    def _check_and_reload_weights(self):
        """承認済み重みファイルの変更を検知して無停止ホットリロード"""
        import os
        if os.path.exists(self.weights_file):
            try:
                mtime = os.path.getmtime(self.weights_file)
                if mtime > self._last_weights_mtime:
                    self.load_weights(self.weights_file)
                    self._last_weights_mtime = mtime
            except Exception:
                pass

    def load_weights(self, filepath: str):
        """外部JSONファイルから承認済み重みをロード"""
        import json
        import os
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "W_PRESSURE" in data:
                    self.W_PRESSURE.update(data["W_PRESSURE"])
                if "W_CONFLICT" in data:
                    self.W_CONFLICT.update(data["W_CONFLICT"])
                if "confidence_threshold" in data:
                    self.confidence_threshold = float(data["confidence_threshold"])
                self._last_weights_mtime = os.path.getmtime(filepath)
                # print(f"[SignalFusionEngine] ✅ 承認済み重みをホットリロードしました: {filepath}")
            except Exception as e:
                print(f"[SignalFusionEngine] ⚠️ 重みロード失敗: {e}")

    def _on_trend_update(self, data: Dict[str, Any]):
        self.latest_trend = data
        self.evaluate()

    def _on_micro_update(self, data: Dict[str, Any]):
        self.latest_micro = data
        self.evaluate()

    def _on_adverse_update(self, data: Dict[str, Any]):
        self.latest_adverse = data
        self.evaluate()

    def evaluate(self) -> Optional[FusionDecisionLog]:
        self._check_and_reload_weights()
        t_dir = self.latest_trend["trend_direction"]
        t_str = self.latest_trend["trend_strength"]
        regime = self.latest_trend["regime_tag"]

        p_side = self.latest_micro["pressure_side"]
        p_score = self.latest_micro["pressure_score"]
        fake_bo = self.latest_micro["fake_breakout_flag"]
        lat_risk = self.latest_micro["latency_risk_flag"]
        adverse_side = self.latest_micro.get("adverse_risk_side", "none")
        adverse_score = float(self.latest_micro.get("adverse_risk_score", 0.0))
        adverse_warning = bool(self.latest_micro.get("adverse_warning_flag", False))

        # 1. 方向整合性チェック
        alignment = (t_dir == p_side and t_dir in ["up", "down"])

        # 2. スコア統合 (重み付き)
        base = t_str
        if alignment:
            micro_boost = p_score * self.W_PRESSURE.get(regime, 0.50)
        else:
            micro_boost = -p_score * self.W_CONFLICT.get(regime, 0.60)

        final_confidence = max(0.0, min(1.0, base + micro_boost))

        # 3. リスク補正 & Adverse Selection (逆選択) 先回り防御
        size_mult = 1.0

        intended_action = "buy" if t_dir == "up" or (t_dir == "neutral" and p_side == "buy") else "sell"

        # 微細構造の逆選択スコアは確信度の補正だけに使う。数量ゼロにはしない。
        if intended_action == adverse_side and adverse_score > 0.0:
            penalty = max(0.0, 1.0 - (adverse_score * 0.8))
            final_confidence *= penalty

        # Adverse は研究メモのみ。融合の数量・アクションは動かさない。

        # マクロ急変ショック連携 (外部センチネルからの急変状態)
        try:
            from .market_shock_sentinel import MarketShockSentinel
            shock = MarketShockSentinel().get_current_shock()
            if shock.shock_active:
                regime = "high_vol"  # 強制的にボラティリティ警戒レジーム
                if shock.shock_level == "critical":
                    size_mult = 0.0  # 致命的ショック時は完全見送り
                    final_confidence *= 0.20
                else:
                    size_mult *= 0.50  # 警戒ショック時はロット半減
        except Exception:
            pass

        if fake_bo:
            final_confidence *= 0.30   # フェイクブレイク検知時は確信度を急減
            size_mult = 0.0            # 発注サイズゼロ (見送り)
        if lat_risk:
            size_mult *= 0.50          # 通信遅延時はロット半減

        # 4. 最終アクション決定
        if final_confidence >= self.confidence_threshold and size_mult > 0.0:
            action = intended_action
        elif final_confidence >= 0.20:
            action = "hold"
        else:
            action = "exit"

        # 5. 意思決定ログを生成 & Parquetへ非同期投入
        decision = FusionDecisionLog(
            timestamp=int(time.time() * 1000),
            trend_direction=t_dir,
            trend_strength=round(t_str, 3),
            regime_tag=regime,
            pressure_side=p_side,
            pressure_score=round(p_score, 3),
            fake_breakout_flag=fake_bo,
            latency_risk_flag=lat_risk,
            final_confidence=round(final_confidence, 3),
            action=action,
            size_multiplier=round(size_mult, 2),
            realized_pnl=0.0,
            adverse_warning_flag=adverse_warning,
            adverse_risk_side=adverse_side,
            adverse_risk_score=round(adverse_score, 3)
        )
        self.logger.log("fusion_log", decision)

        # 6. シグナルを配信 (buy/sell/exit/cancel)
        if action in ["buy", "sell", "exit", "cancel"]:
            self.bus.publish("final_signal", asdict(decision))

        return decision

