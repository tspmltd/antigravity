#!/bin/bash
# ==============================================================================
# DuckDB Quant Analysis Scheduled Reporter (あかり専用)
# ==============================================================================
# 蓄積された板・Fusionログ (Parquet) を集計し、
# Discord「分析・重み更新サーバー」へ自動投稿する。

set -euo pipefail

BASE_DIR="/home/azureuser/antigravity"
LOG_FILE="${BASE_DIR}/logs/duckdb_analysis_cron.log"
PYTHON_BIN="${BASE_DIR}/venv/bin/python3"

mkdir -p "$(dirname "$LOG_FILE")"

echo "======================================================================" >> "$LOG_FILE"
echo "  🦆 DuckDB 定期クオンツ分析実行開始: $(date '+%Y-%m-%d %H:%M:%S JST')" >> "$LOG_FILE"
echo "======================================================================" >> "$LOG_FILE"

cd "$BASE_DIR"

if "$PYTHON_BIN" -m antigravity.quant_pipeline.duckdb_analyzer --post-discord >> "$LOG_FILE" 2>&1; then
    echo "✅ [$(date '+%Y-%m-%d %H:%M:%S JST')] 分析レポートの Discord 送信が完了しました。" >> "$LOG_FILE"
else
    echo "⚠️ [$(date '+%Y-%m-%d %H:%M:%S JST')] 分析レポート送信中にエラーが発生しました。" >> "$LOG_FILE"
fi

echo "" >> "$LOG_FILE"
