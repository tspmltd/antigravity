"""
scripts/research/train_teacher_toxic_model.py
CSR-579 / CSR-649 準拠: Phase 2 Teacherモデル学習 & 特徴量寄与度分析
ガバナンス: WIRE=NO, ENFORCE=0, auto_apply=OFF (Read-Only 学習 & 評価)
"""
import duckdb
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, average_precision_score, precision_recall_curve, confusion_matrix
import joblib
import json
import os
from pathlib import Path

def main():
    root = Path(__file__).resolve().parent.parent.parent
    os.chdir(root)
    
    print("[1/5] Loading training dataset from DuckDB / Parquet...")
    dataset_path = "data/ml_spine/training_reservation_labels.parquet"
    if not os.path.exists(dataset_path):
        print(f"ERROR: Dataset not found: {dataset_path}")
        sys.exit(1)
        
    con = duckdb.connect()
    df = con.execute(f"""
        SELECT 
            timestamp,
            spread_bp,
            micro_dev,
            wobi_l1,
            tip_thin_bid,
            tip_thin_ask,
            cushion_bid_vol,
            cushion_ask_vol,
            cancel_rate,
            taker_aggressiveness,
            taker_volume_bid,
            taker_volume_ask,
            -- 複合特徴量 (Microstructure Interactions)
            ROUND((cushion_bid_vol - cushion_ask_vol) / NULLIF(cushion_bid_vol + cushion_ask_vol, 0), 4) AS cushion_skew,
            ROUND((taker_volume_bid - taker_volume_ask) / NULLIF(taker_volume_bid + taker_volume_ask, 0), 4) AS taker_imbalance,
            ROUND(cancel_rate * (1.0 - taker_aggressiveness), 4) AS fake_wall_risk,
            ROUND(spread_bp * ABS(wobi_l1), 4) AS spread_tension,
            -- 正解ラベル
            is_toxic_strike_1s
        FROM read_parquet('{dataset_path}')
        ORDER BY timestamp ASC
    """).df()
    
    print(f" -> Loaded {len(df):,} ticks.")
    
    # 欠損値補完
    df = df.fillna(0.0)
    
    feature_cols = [
        "spread_bp", "micro_dev", "wobi_l1", 
        "tip_thin_bid", "tip_thin_ask", 
        "cushion_bid_vol", "cushion_ask_vol", "cushion_skew",
        "cancel_rate", "taker_aggressiveness", 
        "taker_volume_bid", "taker_volume_ask", "taker_imbalance",
        "fake_wall_risk", "spread_tension"
    ]
    target_col = "is_toxic_strike_1s"
    
    X = df[feature_cols]
    y = df[target_col]
    
    print(f" -> Features: {len(feature_cols)} cols")
    print(f" -> Target Positive Ratio: {y.mean()*100:.2f}% ({y.sum()} / {len(y)})")

    print("\n[2/5] Time-series Split (80% Train, 20% Test, No Future Leakage)...")
    split_idx = int(len(df) * 0.8)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
    
    print(f" -> Train set: {len(X_train):,} rows (Pos: {y_train.sum()})")
    print(f" -> Test set:  {len(X_test):,} rows (Pos: {y_test.sum()})")

    print("\n[3/5] Training LightGBM Teacher Model (Adverse Toxic Strike Predictor)...")
    # 不均衡データ対応（pos_weight）＆蒸留しやすさのため浅い木 (max_depth=4)
    pos_weight = (len(y_train) - y_train.sum()) / max(y_train.sum(), 1)
    
    model = lgb.LGBMClassifier(
        n_estimators=100,
        max_depth=4,
        num_leaves=15,
        learning_rate=0.05,
        scale_pos_weight=min(pos_weight, 10.0), # 過度な重み付けを制限
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=-1,
        verbose=-1
    )
    
    model.fit(X_train, y_train)

    print("\n[4/5] Evaluating Model on Unseen Out-of-Sample Test Set...")
    y_pred_proba = model.predict_proba(X_test)[:, 1]
    roc_auc = roc_auc_score(y_test, y_pred_proba)
    pr_auc = average_precision_score(y_test, y_pred_proba)
    
    # 上位10%のリスク高スコア判定でPULLした場合の精度と再現率
    threshold_p90 = np.percentile(y_pred_proba, 90)
    y_pred_p90 = (y_pred_proba >= threshold_p90).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_test, y_pred_p90).ravel()
    precision_p90 = tp / max(tp + fp, 1)
    recall_p90 = tp / max(tp + fn, 1)
    
    print(f"  ★ ROC-AUC:            {roc_auc:.4f}")
    print(f"  ★ PR-AUC (Avg Prec):   {pr_auc:.4f} (Baseline: {y_test.mean():.4f})")
    print(f"  ★ Top 10% Risk Gate:   Precision = {precision_p90*100:.2f}%, Recall = {recall_p90*100:.2f}%")
    print(f"     (TP: {tp}, FP: {fp}, TN: {tn}, FN: {fn})")

    print("\n[5/5] Extracting Feature Importance & Attribution...")
    importances = model.feature_importances_
    feat_imp = pd.DataFrame({
        "feature": feature_cols,
        "importance_gain": importances
    }).sort_values("importance_gain", ascending=False)
    
    print("\n--- [Feature Importance Ranking (何が毒性直撃を引き起こすか)] ---")
    print(feat_imp.to_string(index=False))

    # 保存
    model_save_path = "data/ml_spine/teacher_toxic_model.pkl"
    eval_save_path = "data/audit/teacher_model_evaluation.json"
    
    joblib.dump(model, model_save_path)
    print(f"\n -> Model saved to: {model_save_path}")
    
    eval_report = {
        "train_rows": len(X_train),
        "test_rows": len(X_test),
        "target_rate_test": float(y_test.mean()),
        "roc_auc": float(roc_auc),
        "pr_auc": float(pr_auc),
        "top_10pct_gate": {
            "threshold": float(threshold_p90),
            "precision": float(precision_p90),
            "recall": float(recall_p90),
            "true_positives": int(tp),
            "false_positives": int(fp),
            "toxic_strikes_prevented_pct": float(recall_p90 * 100)
        },
        "top_5_features": feat_imp.head(5).to_dict("records")
    }
    
    with open(eval_save_path, "w") as f:
        json.dump(eval_report, f, indent=2)
    print(f" -> Evaluation report saved to: {eval_save_path}")
    
    print("\n[ALL COMPLETE] Phase 2 Teacherモデル学習・寄与度分析が正常に完了しました。")

if __name__ == "__main__":
    main()
