#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

LOG_FILE="$DIR/logs/dryrun_umm_tf2bp_24h.log"
PID_FILE="$DIR/data/dryrun_umm_tf2bp.pid"

# 既存プロセスがあれば停止
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo "Existing dryrun process $OLD_PID is running. Stopping..."
        kill "$OLD_PID" || true
        sleep 2
    fi
fi

echo "Starting 24-hour UMM & TF2BP Dry-run observation..."
mkdir -p "$DIR/logs" "$DIR/data"

nohup "$DIR/venv/bin/python3" -u -m antigravity.quant_pipeline.run_dryrun_umm_tf2bp_24h \
    --symbol FX_BTC_JPY \
    --hours 24.0 \
    --interval 2.0 \
    --report-interval 900.0 \
    >> "$LOG_FILE" 2>&1 &

PID=$!
disown $PID
echo "$PID" > "$PID_FILE"
echo "✅ Started successfully with PID $PID"
