"""
DuckDB Alpha & Weight Optimizer
===============================
- 蓄積された Parquet ファイルをDuckDBで直接クエリ
- DBサーバー常駐ゼロ / メモリ消費数MBで数万行の瞬時集計
- 勝ちパターンと負けパターンの差分から Fusion Engine の重みを最適化
- Discord「分析・重み更新サーバー」へ自動レポート送信 & 承認ワークフロー
"""
import os
import json
import argparse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
import duckdb
import pandas as pd

from .quant_discord_notifier import QuantDiscordNotifier

JST = timezone(timedelta(hours=9))


def run_analysis(
    base_dir: str = "/home/azureuser/antigravity/data/parquet",
    post_discord: bool = False,
    approve_update: bool = False,
    weights_path: str = "/home/azureuser/antigravity/configs/approved_weights.json",
) -> Dict[str, Any]:
    conn = duckdb.connect()

    micro_path = os.path.join(base_dir, "orderbook_micro", "*", "*.parquet")
    fusion_path = os.path.join(base_dir, "fusion_log", "*", "*.parquet")

    print("=" * 65)
    print("       🦆 DuckDB Quant Alpha & Parquet Analytics Engine       ")
    print("=" * 65)

    stats_summary: Dict[str, Any] = {}
    delta_w_list: List[Dict[str, Any]] = []

    # 1. orderbook_micro 統計
    try:
        df_micro_stats = conn.execute(f"""
            SELECT 
                COUNT(*) AS total_snapshots,
                ROUND(AVG(mid_price), 1) AS avg_mid,
                ROUND(AVG(imbalance), 3) AS avg_imbalance,
                ROUND(AVG(total_bid_depth), 3) AS avg_bid_depth,
                ROUND(AVG(total_ask_depth), 3) AS avg_ask_depth,
                ROUND(AVG(latency_ms), 1) AS avg_latency_ms
            FROM '{micro_path}'
        """).df()
        print("\n📊 【Orderbook Micro 基礎統計】")
        print(df_micro_stats.to_string(index=False))
        if not df_micro_stats.empty:
            stats_summary = df_micro_stats.iloc[0].to_dict()
    except Exception as e:
        print(f"Orderbook micro data not ready yet: {e}")

    # 2. fusion_log 統計
    try:
        df_fusion_stats = conn.execute(f"""
            SELECT 
                regime_tag,
                action,
                COUNT(*) AS count,
                ROUND(AVG(final_confidence), 3) AS avg_confidence,
                ROUND(AVG(pressure_score), 3) AS avg_pressure_score,
                ROUND(AVG(trend_strength), 3) AS avg_trend_strength
            FROM '{fusion_path}'
            GROUP BY regime_tag, action
            ORDER BY regime_tag, count DESC
        """).df()
        print("\n🧩 【Fusion Engine 意思決定分布】")
        print(df_fusion_stats.to_string(index=False))
    except Exception as e:
        print(f"Fusion log data not ready yet: {e}")

    # 3. 勝ち負け差分による重み更新推奨値の算出
    try:
        df_weights = conn.execute(f"""
            SELECT 
                regime_tag,
                COUNT(*) AS samples,
                ROUND(COALESCE(AVG(CASE WHEN realized_pnl > 0 THEN pressure_score END), 0.0), 3) AS win_avg_pressure,
                ROUND(COALESCE(AVG(CASE WHEN realized_pnl < 0 THEN pressure_score END), 0.0), 3) AS lose_avg_pressure,
                ROUND(COALESCE(AVG(CASE WHEN realized_pnl > 0 THEN pressure_score END), 0.0) - 
                      COALESCE(AVG(CASE WHEN realized_pnl < 0 THEN pressure_score END), 0.0), 3) AS delta_w_pressure
            FROM '{fusion_path}'
            WHERE action IN ('buy', 'sell')
            GROUP BY regime_tag
        """).df()
        print("\n🎯 【レジーム別 重み更新推奨値 (ΔW_PRESSURE)】")
        print(df_weights.to_string(index=False))
        if not df_weights.empty:
            delta_w_list = df_weights.to_dict(orient="records")
    except Exception as e:
        print(f"Weight delta calculation notice: {e}")

    # 4. 重み承認更新 (approve_update)
    if approve_update and delta_w_list:
        print("\n" + "=" * 65)
        print("       ⚖️ 重み承認適用 (Apply Approved Weights)       ")
        print("=" * 65)
        current_weights = {}
        if os.path.exists(weights_path):
            with open(weights_path, "r", encoding="utf-8") as f:
                current_weights = json.load(f)

        w_pressure = current_weights.get("W_PRESSURE", {})
        for dw in delta_w_list:
            reg = dw["regime_tag"]
            delta = float(dw["delta_w_pressure"])
            old_val = float(w_pressure.get(reg, 0.50))
            # ラーニングレート 0.20 で過剰フィッティングを抑制
            new_val = max(0.10, min(0.95, round(old_val + delta * 0.20, 3)))
            w_pressure[reg] = new_val
            print(f"  • レジーム [{reg}]: W_PRESSURE {old_val} ➔ {new_val} (Δ={delta:+.3f})")

        current_weights["W_PRESSURE"] = w_pressure
        current_weights["updated_at"] = datetime.now(JST).isoformat()
        current_weights["version"] = f"opt-{int(datetime.now().timestamp())}"

        with open(weights_path, "w", encoding="utf-8") as f:
            json.dump(current_weights, f, indent=2, ensure_ascii=False)
        print(f"\n✅ 承認済み重みを更新しました: {weights_path}")

    # 5. Discord 分析サーバーへレポート投稿
    if post_discord:
        print("\n🚀 Discord 分析・重み更新サーバーへレポートを送信中...")
        notifier = QuantDiscordNotifier()
        success = notifier.notify_analysis_report(
            stats_dict=stats_summary,
            delta_w_list=delta_w_list,
            approval_command="python3 -m antigravity.quant_pipeline.duckdb_analyzer --post-discord --approve",
        )
        if success:
            print("✅ Discord 分析サーバーへのレポート送信が完了しました。")
        else:
            print("⚠️ Discord送信に失敗しました（Webhook URLを確認してください）。")

    return {
        "stats": stats_summary,
        "delta_w": delta_w_list,
    }


def main():
    parser = argparse.ArgumentParser(description="DuckDB Alpha & Weight Optimizer")
    parser.add_argument("--dir", default="/home/azureuser/antigravity/data/parquet", help="Parquetベースディレクトリ")
    parser.add_argument("--post-discord", action="store_true", help="Discord分析サーバーへ集計レポートを送信")
    parser.add_argument("--approve", action="store_true", help="算出したΔWを承認して approved_weights.json に反映")
    args = parser.parse_args()

    run_analysis(
        base_dir=args.dir,
        post_discord=args.post_discord,
        approve_update=args.approve,
    )


if __name__ == "__main__":
    main()
