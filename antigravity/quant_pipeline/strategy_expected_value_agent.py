# -*- coding: utf-8 -*-
"""
CSR-EV002 / CSR-597: Strategy Expected Value Agent (SEVA)
=========================================================
Mission:
    現在の市場状態 X において、各取引戦略（TF2BP, SpreadCaptureMM, InventorySkewMM, UMM）が
    獲得し得る「未来純期待値（Expected Value bp）」および「信頼度（Confidence）」を同時推定し、
    戦略別EVランキングを出力する中核クオンツエンジン。

CSR-597 改定（Head of Agent CONDITIONAL GO 指示反映）:
    1. UMM 過剰許可 (74.7%) の厳格是正:
       - 滞留摩擦・スリッページ・Toxic Flow ペナルティを注入。
       - UMM の最低EVハードルを厳格化し、Allowed Rate を「3〜15%」の厳選帯へ抑制。
    2. SpreadCaptureMM の適正評価:
       - Arena上位実績 (+71.9bp / 勝率66%) を反映し、リフィル安定局面での過小評価（0%遮断）を是正。
    3. 3大交差KPI（Conditional Sharpe, False Pass, Coverage）の基盤化。
"""

from __future__ import annotations

import os
import json
import time
import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path

try:
    import lightgbm as lgb
    import numpy as np
    HAS_ML = True
except ImportError:
    HAS_ML = False
    np = None


# 対象戦略一覧
SUPPORTED_STRATEGIES = [
    "TF2BP",
    "SpreadCaptureMM",
    "InventorySkewMM",
    "UMM",
]

# 共通特徴量リスト
FEATURE_COLUMNS = [
    "spread_bp",
    "imbalance",
    "micro_dev",
    "depth_ratio",
    "bid_depth_1",
    "ask_depth_1",
    "total_bid_depth",
    "total_ask_depth",
    "taker_volume_bid",
    "taker_volume_ask",
    "taker_aggressiveness",
    "pressure_score",
    "micro_accel",
    "cancel_rate",
    "refill_rate",
    "vacuum_score",
    "latency_ms",
    "toxic_score",
    "adverse_score",
    "news_impact_score",
    "news_risk_level",
    "news_alpha_flag",
]


@dataclass
class StrategyEVEstimate:
    strategy_name: str
    ev_bp: float                 # 期待リターン (bp, コスト控除後Net)
    confidence: float            # 信頼度 (0.0 ~ 1.0)
    entry_allowed: bool          # 執行許可 (EV > min_ev_threshold)
    downside_q10_bp: float       # 下振れ10%点 (VaR)
    rank: int = 0
    rejection_reason: Optional[str] = None
    predicted_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SEVARankingVerdict:
    timestamp: float
    verdict_iso: str
    ranked_strategies: List[StrategyEVEstimate]
    top_strategy: Optional[str]
    max_ev_bp: float
    allowed_count: int
    raw_features: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "verdict_iso": self.verdict_iso,
            "ranked_strategies": [s.to_dict() for s in self.ranked_strategies],
            "top_strategy": self.top_strategy,
            "max_ev_bp": self.max_ev_bp,
            "allowed_count": self.allowed_count,
            "feature_snapshot": {
                k: round(self.raw_features.get(k, 0.0), 4)
                for k in ["spread_bp", "imbalance", "adverse_score", "vacuum_score", "cancel_rate", "refill_rate"]
                if k in self.raw_features
            }
        }


class StrategyExpectedValueAgent:
    """Strategy Expected Value Agent (SEVA) - CSR-597 Calibrated"""

    def __init__(
        self,
        model_dir: Optional[str] = None,
        min_ev_bp: float = 0.20,
        min_confidence: float = 0.65,
        default_latency_penalty_bp: float = 0.10,
    ):
        self.model_dir = Path(model_dir) if model_dir else Path("/home/azureuser/antigravity/data/mm_research/seva/models")
        self.min_ev_bp = float(min_ev_bp)
        self.min_confidence = float(min_confidence)
        self.latency_penalty = float(default_latency_penalty_bp)

        # 戦略別最低EV閾値（CSR-597: UMMは厳格化、TF2BP/SpreadCaptureは標準）
        self.strat_min_ev = {
            "TF2BP": 0.15,
            "SpreadCaptureMM": 0.10,
            "InventorySkewMM": 0.20,
            "UMM": 1.40,  # UMMは長期摩擦リスクを考慮し厳格化 (74.7% -> 3~15%へ抑制)
        }

        self.models: Dict[str, Any] = {}
        self.is_warmed_up = False
        self.last_inference_time_ms = 0.0

        self.load_models()

    def load_models(self) -> None:
        if not HAS_ML or not self.model_dir.exists():
            return

        for strat in SUPPORTED_STRATEGIES:
            model_path = self.model_dir / f"{strat}_ev_regressor.txt"
            if model_path.exists():
                try:
                    self.models[strat] = lgb.Booster(model_file=str(model_path))
                except Exception as e:
                    self.models.pop(strat, None)
                    print(f"[SEVA] Warning: failed to load model for {strat}: {e}")

    def extract_features(self, raw_input: Dict[str, Any]) -> Tuple[List[float], Dict[str, float]]:
        feat_dict = {}
        feat_vector = []

        for col in FEATURE_COLUMNS:
            v = float(raw_input.get(col, 0.0))
            if math.isnan(v) or math.isinf(v):
                v = 0.0
            feat_dict[col] = v
            feat_vector.append(v)

        return feat_vector, feat_dict

    def _prior_ev_heuristics(self, strategy: str, f: Dict[str, float]) -> Tuple[float, float, float]:
        spread = f.get("spread_bp", 2.0)
        imb = f.get("imbalance", 0.0)
        adverse = f.get("adverse_score", 30.0)
        vac = f.get("vacuum_score", 0.0)
        cancel = f.get("cancel_rate", 0.1)
        refill = f.get("refill_rate", 0.5)

        risk_penalty = (adverse / 100.0) * 1.5 + vac * 1.2

        if strategy == "TF2BP":
            ev = (abs(imb) * 1.8) - risk_penalty - (0.5 if cancel > 0.4 else 0.0)
            if spread > 5.0:
                ev -= 0.6
            conf = max(0.4, min(0.95, 0.60 + abs(imb) * 0.3 - adverse * 0.003))
            q10 = ev - 2.5

        elif strategy == "SpreadCaptureMM":
            capture_potential = spread * 0.45
            ev = capture_potential * (refill / max(refill + cancel, 0.01)) - risk_penalty
            if abs(imb) > 0.6 or vac > 0.5:
                ev -= 1.8
            conf = max(0.4, min(0.92, 0.70 - abs(imb) * 0.25 - vac * 0.3))
            q10 = ev - 3.2

        elif strategy == "InventorySkewMM":
            ev = (spread * 0.35) - (risk_penalty * 0.9) - abs(f.get("micro_dev", 0.0)) * 0.03
            conf = max(0.4, min(0.90, 0.65 - adverse * 0.002))
            q10 = ev - 2.8

        elif strategy == "UMM":
            # CSR-597: UMM 滞留摩擦・長期ボラティリティペナルティ
            ev = (spread * 0.25) - (risk_penalty * 1.5) - 0.80
            conf = 0.75
            q10 = ev - 4.5

        else:
            ev = -0.5
            conf = 0.5
            q10 = -5.0

        return round(ev, 4), round(conf, 4), round(q10, 4)

    def predict_strategy_ev(self, strategy: str, feat_vector: List[float], feat_dict: Dict[str, float]) -> StrategyEVEstimate:
        spread = feat_dict.get("spread_bp", 2.0)
        adverse = feat_dict.get("adverse_score", 30.0)
        vac = feat_dict.get("vacuum_score", 0.0)
        cancel = feat_dict.get("cancel_rate", 0.1)
        refill = feat_dict.get("refill_rate", 0.5)

        # 1. 基本推論
        if HAS_ML and strategy in self.models:
            try:
                x_arr = np.array([feat_vector], dtype=np.float32)
                ev_pred = float(self.models[strategy].predict(x_arr)[0])
                conf = 0.80
                q10 = ev_pred - 2.2
            except Exception:
                ev_pred, conf, q10 = self._prior_ev_heuristics(strategy, feat_dict)
        else:
            ev_pred, conf, q10 = self._prior_ev_heuristics(strategy, feat_dict)

        # 2. CSR-597 キャリブレーション補正（負例注入 & サンプル偏重是正）
        if strategy == "UMM":
            # UMM 負例正則化: キャンセル多発や板薄局面での過大評価を抑制
            if cancel > 0.30 or vac > 0.30 or adverse > 45.0:
                ev_pred -= 1.0  # 逆選択ペナルティ
            # スプレッドが広すぎる時（> 2.5bp）の滞留損失ペナルティ
            if spread > 2.5:
                ev_pred -= 0.8
            conf = 0.75

        elif strategy == "SpreadCaptureMM":
            # SpreadCaptureMM 適正化: リフィルが健全で不均衡が小さい安定レンジを適正評価
            if refill > 0.50 and abs(feat_dict.get("imbalance", 0.0)) < 0.35 and adverse < 40.0:
                ev_pred = max(ev_pred, (spread * 0.40) - (adverse * 0.01) - 0.20)
                conf = 0.85

        # 3. 執行許可ゲート判定
        entry_allowed = True
        rejection_reason = None
        min_ev = self.strat_min_ev.get(strategy, self.min_ev_bp)

        # CSR-597 / CSR-598: UMM は負例教材として完全隔離（執行許可禁止）
        if strategy == "UMM":
            entry_allowed = False
            rejection_reason = "STRATEGY_QUARANTINED (CSR-598: UMM sealed as negative learning specimen)"
        elif ev_pred <= 0.0:
            entry_allowed = False
            rejection_reason = f"NEGATIVE_OR_ZERO_EV ({ev_pred:+.2f}bp <= 0bp)"
        elif ev_pred < min_ev:
            entry_allowed = False
            rejection_reason = f"SUB_THRESHOLD_EV ({ev_pred:+.2f}bp < +{min_ev:.2f}bp)"
        elif conf < self.min_confidence:
            entry_allowed = False
            rejection_reason = f"LOW_CONFIDENCE ({conf*100:.1f}% < {self.min_confidence*100:.0f}%)"

        return StrategyEVEstimate(
            strategy_name=strategy,
            ev_bp=round(ev_pred, 4),
            confidence=round(conf, 4),
            entry_allowed=entry_allowed,
            downside_q10_bp=round(q10, 4),
            rejection_reason=rejection_reason,
        )

    def evaluate_and_rank(self, raw_input: Dict[str, Any]) -> SEVARankingVerdict:
        t0 = time.perf_counter()
        now = time.time()

        feat_vector, feat_dict = self.extract_features(raw_input)

        estimates = []
        for strat in SUPPORTED_STRATEGIES:
            est = self.predict_strategy_ev(strat, feat_vector, feat_dict)
            estimates.append(est)

        # EV降順ソート
        estimates.sort(key=lambda s: s.ev_bp, reverse=True)
        for i, est in enumerate(estimates, 1):
            est.rank = i

        allowed_estimates = [s for s in estimates if s.entry_allowed]
        top_strat = allowed_estimates[0].strategy_name if allowed_estimates else None
        max_ev = allowed_estimates[0].ev_bp if allowed_estimates else (estimates[0].ev_bp if estimates else 0.0)

        inference_ms = (time.perf_counter() - t0) * 1000.0
        self.last_inference_time_ms = inference_ms

        verdict_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

        return SEVARankingVerdict(
            timestamp=now,
            verdict_iso=verdict_iso,
            ranked_strategies=estimates,
            top_strategy=top_strat,
            max_ev_bp=max_ev,
            allowed_count=len(allowed_estimates),
            raw_features=feat_dict,
        )
