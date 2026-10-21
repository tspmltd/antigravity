#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CSR-601: Train M2P (Move-2bp Predictor) — will |Δmid| >= 2.0bp within 30s.

Fact-only 11 features. Time-split 80/20. No LIVE/PAPER/DeSeva touch.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    roc_auc_score,
)

AG = Path("/home/azureuser/antigravity")
DATA_PATH = AG / "data" / "mm_research" / "dev_agent" / "dev_agent_dataset.parquet"
OUT_DIR = AG / "data" / "mm_research" / "m2p" / "models"
REPORT_PATH = AG / "data" / "mm_research" / "m2p" / "train_report.json"
JST = timezone(timedelta(hours=9))

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
HORIZON = "future_return_30s_bp"
THRESH_BP = 2.0


def _heuristic(df: pd.DataFrame) -> np.ndarray:
    imb = df["feat_imbalance"].abs().clip(0, 1)
    spread = df["feat_spread_bp"].clip(0.2, 20)
    micro = (df["feat_micro_dev"].abs() / spread).clip(0, 8) / 8.0
    cancel = df["feat_cancel_rate"].clip(0, 1)
    thin = ((df["feat_tip_thin_bid"] + df["feat_tip_thin_ask"]) / 2.0).clip(0, 1)
    trend = df["feat_trend_strength"].clip(0, 5) / 5.0
    raw = 0.30 * imb + 0.20 * micro + 0.20 * cancel + 0.15 * thin + 0.15 * trend
    return np.clip(raw.to_numpy(dtype=float), 0.0, 1.0)


def main() -> None:
    print("=" * 72)
    print("CSR-601: M2P Move-2bp Predictor training")
    print("=" * 72)
    if not DATA_PATH.exists():
        raise SystemExit(f"dataset missing: {DATA_PATH}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(DATA_PATH)
    df = df.sort_values("timestamp").reset_index(drop=True)
    y_abs = df[HORIZON].abs()
    y = (y_abs >= THRESH_BP).astype(int).to_numpy()
    y_reg = y_abs.clip(0.0, 30.0).to_numpy()
    X = df[FEATURE_COLS].fillna(0.0)

    split = int(len(df) * 0.8)
    X_tr, X_va = X.iloc[:split], X.iloc[split:]
    y_tr, y_va = y[:split], y[split:]
    y_reg_tr, y_reg_va = y_reg[:split], y_reg[split:]
    abs_va = y_abs.iloc[split:].to_numpy()

    print(f"rows={len(df):,} train={len(X_tr):,} val={len(X_va):,}")
    print(f"base_rate |r30|>=2bp train={y_tr.mean():.1%} val={y_va.mean():.1%}")

    cls_data = lgb.Dataset(X_tr, label=y_tr)
    cls_val = lgb.Dataset(X_va, label=y_va, reference=cls_data)
    cls = lgb.train(
        {
            "objective": "binary",
            "metric": "auc",
            "learning_rate": 0.03,
            "num_leaves": 31,
            "max_depth": 6,
            "min_child_samples": 200,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "verbose": -1,
            "random_state": 42,
            "n_jobs": 4,
        },
        cls_data,
        num_boost_round=400,
        valid_sets=[cls_val],
        callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(50)],
    )
    cls_path = OUT_DIR / "m2p_move_classifier.txt"
    cls.save_model(str(cls_path))

    reg_data = lgb.Dataset(X_tr, label=y_reg_tr)
    reg_val = lgb.Dataset(X_va, label=y_reg_va, reference=reg_data)
    reg = lgb.train(
        {
            "objective": "huber",
            "metric": "mae",
            "learning_rate": 0.03,
            "num_leaves": 31,
            "max_depth": 6,
            "min_child_samples": 200,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "verbose": -1,
            "random_state": 42,
            "n_jobs": 4,
        },
        reg_data,
        num_boost_round=300,
        valid_sets=[reg_val],
        callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(50)],
    )
    reg_path = OUT_DIR / "m2p_abs_regressor.txt"
    reg.save_model(str(reg_path))

    p = cls.predict(X_va)
    pred_abs = reg.predict(X_va)
    h = _heuristic(X_va)
    auc = float(roc_auc_score(y_va, p))
    ap = float(average_precision_score(y_va, p))
    brier = float(brier_score_loss(y_va, p))
    auc_h = float(roc_auc_score(y_va, h))

    ladder = []
    for q in (0.80, 0.90, 0.95, 0.99):
        cut = float(np.quantile(p, q))
        mask = p >= cut
        n = int(mask.sum())
        if n == 0:
            continue
        ladder.append(
            {
                "quantile": q,
                "p_cut": round(cut, 4),
                "n": n,
                "move_rate": round(float(y_va[mask].mean()), 4),
                "mean_abs_bp": round(float(abs_va[mask].mean()), 3),
                "lift_vs_base": round(float(y_va[mask].mean() / max(y_va.mean(), 1e-9)), 3),
            }
        )

    quiet = p < 0.40
    watch = (p >= 0.40) & (p < 0.55)
    move = p >= 0.55
    bands = {
        "QUIET": {
            "n": int(quiet.sum()),
            "move_rate": round(float(y_va[quiet].mean()) if quiet.any() else 0.0, 4),
            "mean_abs_bp": round(float(abs_va[quiet].mean()) if quiet.any() else 0.0, 3),
        },
        "WATCH": {
            "n": int(watch.sum()),
            "move_rate": round(float(y_va[watch].mean()) if watch.any() else 0.0, 4),
            "mean_abs_bp": round(float(abs_va[watch].mean()) if watch.any() else 0.0, 3),
        },
        "MOVE": {
            "n": int(move.sum()),
            "move_rate": round(float(y_va[move].mean()) if move.any() else 0.0, 4),
            "mean_abs_bp": round(float(abs_va[move].mean()) if move.any() else 0.0, 3),
        },
    }

    gain = lgb.Booster(model_file=str(cls_path)).feature_importance(importance_type="gain")
    importance = sorted(
        [{"feature": f, "gain": float(g)} for f, g in zip(FEATURE_COLS, gain)],
        key=lambda x: -x["gain"],
    )

    report = {
        "csr": "CSR-601",
        "trained_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "horizon_sec": 30,
        "move_threshold_bp": THRESH_BP,
        "n_train": int(len(X_tr)),
        "n_val": int(len(X_va)),
        "base_rate_val": round(float(y_va.mean()), 4),
        "auc": round(auc, 4),
        "ap": round(ap, 4),
        "brier": round(brier, 4),
        "heuristic_auc": round(auc_h, 4),
        "reg_mae_bp": round(float(np.mean(np.abs(pred_abs - abs_va))), 4),
        "alert_bands": bands,
        "precision_ladder": ladder,
        "feature_importance": importance,
        "models": {"classifier": str(cls_path), "regressor": str(reg_path)},
        "proof": {
            "statement": "30s ahead, |Δmid|>=2.0bp is predictable from fact-only book features",
            "pass_auc_gt_055": auc > 0.55,
            "pass_move_band_lift": bands["MOVE"]["move_rate"] > bands["QUIET"]["move_rate"] + 0.05
            if bands["MOVE"]["n"] and bands["QUIET"]["n"]
            else False,
        },
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["proof"], ensure_ascii=False, indent=2))
    print(f"AUC={auc:.3f} (heuristic {auc_h:.3f}) AP={ap:.3f} Brier={brier:.3f}")
    print("bands", bands)
    print("ladder", ladder)
    print(f"saved {cls_path} {reg_path} {REPORT_PATH}")


if __name__ == "__main__":
    main()
