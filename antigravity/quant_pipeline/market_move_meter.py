# -*- coding: utf-8 -*-
"""CSR-601: M2P — Market Move Meter (will the market move ~2bp?).

Risk instrument only. Predicts P(|Δmid| >= 2.0bp within 30s) from fact-only
order-book features. Does not trade, size, or gate DeSeva/TF2BP.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import lightgbm as lgb
    import numpy as np
    HAS_ML = True
except ImportError:
    HAS_ML = False
    np = None  # type: ignore


FEATURE_COLS = [
    "feat_spread_bp",
    "feat_depth_ratio",
    "feat_imbalance",
    "feat_micro_dev",
    "feat_trend_strength",
    "feat_cancel_rate",
    "feat_refill_rate",
    "feat_taker_aggressiveness",
    "feat_tip_thin_bid",
    "feat_tip_thin_ask",
    "feat_pressure",
]

MOVE_THRESH_BP = 2.0
HORIZON_SEC = 30.0
QUIET_CUT = 0.40
MOVE_CUT = 0.55


@dataclass
class MoveVerdict:
    timestamp: float
    p_move: float
    predicted_abs_bp: float
    alert: str  # QUIET / WATCH / MOVE
    horizon_sec: float
    move_threshold_bp: float
    inference_ms: float
    source: str  # model / heuristic
    features_snapshot: Optional[Dict[str, float]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MarketMoveMeter:
    """Ahead-of-move risk meter. Binary MOVE vs QUIET, not UP/DOWN."""

    def __init__(
        self,
        model_dir: Optional[str] = None,
        *,
        quiet_cut: float = QUIET_CUT,
        move_cut: float = MOVE_CUT,
        move_threshold_bp: float = MOVE_THRESH_BP,
        horizon_sec: float = HORIZON_SEC,
    ):
        base = Path(model_dir) if model_dir else Path(
            "/home/azureuser/antigravity/data/mm_research/m2p/models"
        )
        self.cls_path = base / "m2p_move_classifier.txt"
        self.reg_path = base / "m2p_abs_regressor.txt"
        self.quiet_cut = float(quiet_cut)
        self.move_cut = float(move_cut)
        self.move_threshold_bp = float(move_threshold_bp)
        self.horizon_sec = float(horizon_sec)
        self.cls_booster = None
        self.reg_booster = None
        self.is_ready = False
        self.load_models()

    def load_models(self) -> bool:
        if not HAS_ML:
            return False
        ok = False
        if self.cls_path.exists():
            try:
                self.cls_booster = lgb.Booster(model_file=str(self.cls_path))
                ok = True
            except Exception as exc:
                print(f"[M2P] classifier load failed: {exc}")
        if self.reg_path.exists():
            try:
                self.reg_booster = lgb.Booster(model_file=str(self.reg_path))
            except Exception as exc:
                print(f"[M2P] regressor load failed: {exc}")
        self.is_ready = ok
        return self.is_ready

    def _vector(self, raw: Dict[str, Any]) -> Dict[str, float]:
        snap: Dict[str, float] = {}
        for col in FEATURE_COLS:
            val = float(raw.get(col, 0.0) or 0.0)
            if val != val or val in (float("inf"), float("-inf")):
                val = 0.0
            snap[col] = val
        return snap

    def _heuristic(self, f: Dict[str, float]) -> tuple[float, float]:
        imb = min(1.0, abs(f.get("feat_imbalance", 0.0)))
        spread = max(0.2, min(20.0, f.get("feat_spread_bp", 2.0)))
        micro = min(8.0, abs(f.get("feat_micro_dev", 0.0)) / spread) / 8.0
        cancel = min(1.0, max(0.0, f.get("feat_cancel_rate", 0.0)))
        thin = min(
            1.0,
            0.5
            * (
                float(f.get("feat_tip_thin_bid", 0.0))
                + float(f.get("feat_tip_thin_ask", 0.0))
            ),
        )
        trend = min(1.0, max(0.0, f.get("feat_trend_strength", 0.0)) / 5.0)
        p = 0.30 * imb + 0.20 * micro + 0.20 * cancel + 0.15 * thin + 0.15 * trend
        pred_abs = 0.8 + 4.0 * p
        return float(min(1.0, max(0.0, p))), float(pred_abs)

    def _alert(self, p_move: float) -> str:
        if p_move >= self.move_cut:
            return "MOVE"
        if p_move >= self.quiet_cut:
            return "WATCH"
        return "QUIET"

    def predict(self, raw_features: Dict[str, Any]) -> MoveVerdict:
        t0 = time.perf_counter()
        now = time.time()
        feat = self._vector(raw_features)
        source = "heuristic"
        p_move, pred_abs = self._heuristic(feat)

        if HAS_ML and self.cls_booster is not None:
            x = np.array([[feat[c] for c in FEATURE_COLS]], dtype=np.float32)
            try:
                p_move = float(self.cls_booster.predict(x)[0])
                source = "model"
            except Exception:
                pass
            if self.reg_booster is not None:
                try:
                    pred_abs = float(self.reg_booster.predict(x)[0])
                except Exception:
                    pass

        p_move = min(1.0, max(0.0, p_move))
        pred_abs = max(0.0, pred_abs)
        return MoveVerdict(
            timestamp=now,
            p_move=round(p_move, 4),
            predicted_abs_bp=round(pred_abs, 3),
            alert=self._alert(p_move),
            horizon_sec=self.horizon_sec,
            move_threshold_bp=self.move_threshold_bp,
            inference_ms=round((time.perf_counter() - t0) * 1000.0, 3),
            source=source,
            features_snapshot={k: round(v, 4) for k, v in feat.items()},
        )
