"""
scripts/research/build_reservation_training_dataset.py
CSR-579 / CSR-649 準拠: 教師ラベル生成 & 損失アトリビューション集計スクリプト (Phase 1)
ガバナンス: WIRE=NO, ENFORCE=0, auto_apply=OFF (Read-Only 抽出・集計)
"""
import duckdb
import os
import sys
import json
from pathlib import Path

def main():
    root = Path(__file__).resolve().parent.parent.parent
    os.chdir(root)
    
    con = duckdb.connect(database=':memory:')
    
    print("[1/4] Checking source parquet and audit files...")
    # 2026-10-28 (上昇/揉み合い) と 2026-10-29 (下降波) の健全なParquetを両方統合
    ob_path_28 = "data/mm_research/wobimm_honest_dryrun/ticks/orderbook_micro/date=2026-10-28/*.parquet"
    ob_path_29 = "data/parquet/orderbook_micro/date=2026-10-29/*.parquet"
    exec_audit_path = "data/adverse_excursion_records.jsonl"
    output_dir = Path("data/ml_spine")
    audit_dir = Path("data/audit")
    
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    
    output_path = output_dir / "training_reservation_labels.parquet"
    summary_path = audit_dir / "loss_cause_summary.json"

    print("[2/4] Building Multi-Horizon & Reservation Microstructure features via DuckDB...")
    query = f"""
    CREATE OR REPLACE TABLE raw_spine AS
    WITH combined_raw AS (
        SELECT * FROM read_parquet('{ob_path_28}')
        UNION ALL
        SELECT * FROM read_parquet('{ob_path_29}')
    )
    SELECT 
        timestamp,
        mid_price,
        best_bid,
        best_ask,
        -- スプレッド (bp)
        ROUND((best_ask - best_bid) / NULLIF(mid_price, 0) * 10000.0, 2) AS spread_bp,
        micro_dev,
        imbalance AS wobi_l1,
        -- 板薄判定 (0.015 BTC 未満)
        CASE WHEN bid_depth_1 < 0.015 THEN 1 ELSE 0 END AS tip_thin_bid,
        CASE WHEN ask_depth_1 < 0.015 THEN 1 ELSE 0 END AS tip_thin_ask,
        bid_depth_1 AS cushion_bid_vol,
        ask_depth_1 AS cushion_ask_vol,
        cancel_rate,
        taker_aggressiveness,
        taker_volume_bid,
        taker_volume_ask,
        -- μ予測用: 未来Mid差分 (Horizon: lead 2 ticks, 10 ticks, 50 ticks)
        LEAD(mid_price, 2) OVER (ORDER BY timestamp) - mid_price AS delta_mid_200ms,
        LEAD(mid_price, 10) OVER (ORDER BY timestamp) - mid_price AS delta_mid_1s,
        LEAD(mid_price, 50) OVER (ORDER BY timestamp) - mid_price AS delta_mid_5s,
        -- m_adv予測用: 毒性急落フラグ (1秒以内に -1.5bp 以上急落したか)
        CASE 
            WHEN (LEAD(mid_price, 10) OVER (ORDER BY timestamp) - mid_price) / NULLIF(mid_price, 0) * 10000.0 <= -1.5 
            THEN 1 ELSE 0 
        END AS is_toxic_strike_1s,
        -- 在庫死フラグ (50ティック以内に -5.0bp に達したか)
        CASE 
            WHEN (LEAD(mid_price, 50) OVER (ORDER BY timestamp) - mid_price) / NULLIF(mid_price, 0) * 10000.0 <= -5.0 
            THEN 1 ELSE 0 
        END AS is_hard_stop_50t
    FROM combined_raw
    WHERE mid_price IS NOT NULL AND mid_price > 0;
    """
    con.execute(query)

    print("[3/4] Exporting training dataset to Parquet...")
    con.execute(f"COPY raw_spine TO '{output_path}' (FORMAT PARQUET);")
    
    row_count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{output_path}')").fetchone()[0]
    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f" -> Successfully saved: {output_path}")
    print(f" -> Total Rows: {row_count:,}")
    print(f" -> File Size:  {file_size_mb:.2f} MB")
    
    print("\n--- [Sample Data: Head 5 Rows] ---")
    df_sample = con.execute(f"SELECT timestamp, mid_price, spread_bp, wobi_l1, delta_mid_1s, is_toxic_strike_1s, is_hard_stop_50t FROM read_parquet('{output_path}') LIMIT 5").df()
    print(df_sample.to_string(index=False))

    print("\n[4/4] Generating Loss Attribution Summary (Waterfall Engine)...")
    if os.path.exists(exec_audit_path):
        loss_summary_query = f"""
        SELECT 
            CASE 
                WHEN COALESCE(ae_1s, 0.0) <= -1.5 THEN 'ADVERSE_TOXIC_STRIKE'
                WHEN COALESCE(holding_time_sec, 0.0) >= 30.0 AND COALESCE(ae_1s, 0.0) > -1.0 THEN 'HOLD_LADDER_BLEED'
                WHEN ABS(COALESCE(mae_bp, 0.0)) <= 1.0 AND ABS(COALESCE(mfe_bp, 0.0)) <= 1.0 THEN 'EXIT_FRICTION_CHOP'
                ELSE 'UNCLASSIFIED_LOSS'
            END AS loss_cause,
            COUNT(*) AS loss_trades,
            ROUND(SUM(realized_pnl_bp), 2) AS total_loss_bp,
            ROUND(AVG(realized_pnl_bp), 2) AS avg_loss_bp,
            ROUND(AVG(COALESCE(ae_1s, 0.0)), 2) AS avg_ae_1s
        FROM read_json_auto('{exec_audit_path}', maximum_object_size=10485760)
        WHERE realized_pnl_bp < 0
        GROUP BY loss_cause
        ORDER BY total_loss_bp ASC;
        """
        df_summary = con.execute(loss_summary_query).df()
        print("\n--- 損失内訳ランキング (Loss Cause Attribution Summary) ---")
        print(df_summary.to_string(index=False))
        df_summary.to_json(summary_path, orient="records", indent=2)
        print(f"\n -> Loss attribution summary saved to: {summary_path}")
    else:
        print(f" -> Audit file not found: {exec_audit_path}")

    print("\n[ALL COMPLETE] Phase 1 教師データセット抽出・損失アトリビューション集計が完了しました。")

if __name__ == "__main__":
    main()
