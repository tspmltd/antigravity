#!/usr/bin/env bash
# Start TF2B_DeSeva_v1 Dry-Run (TF2BP × SEVA · WIRE=NO).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

LOG_FILE="$DIR/logs/dryrun_tf2b_deseva_v1.log"
PID_FILE="$DIR/data/dryrun_tf2b_deseva_v1.pid"
STOP_FLAG="$DIR/data/TF2B_DESEVA_V1_DRYRUN_STOP.flag"
MARKET_SOURCE="${MARKET_SOURCE:-shared_feeder}"
REPORT_INTERVAL="${REPORT_INTERVAL:-900}"
INTERVAL="${INTERVAL:-1.0}"
NEEDLE='antigravity.quant_pipeline.run_dryrun_tf2b_deseva_v1'

mkdir -p "$DIR/logs" "$DIR/data" "$DIR/data/mm_research/tf2b_deseva_v1_dryrun"
rm -f "$STOP_FLAG"

is_deseva_pid() {
  local p="${1:-}"
  [[ -n "$p" ]] || return 1
  ps -p "$p" >/dev/null 2>&1 || return 1
  tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -E -q "python[0-9.]* .*-m ${NEEDLE}"
}

STOP_PIDS=()
if [[ -f "$PID_FILE" ]]; then
  OLD="$(cat "$PID_FILE" || true)"
  if is_deseva_pid "${OLD:-}"; then
    STOP_PIDS+=("$OLD")
  fi
fi
while read -r p; do
  [[ -n "$p" ]] || continue
  is_deseva_pid "$p" || continue
  STOP_PIDS+=("$p")
done < <(pgrep -f "python.*-m ${NEEDLE}" || true)

if [[ ${#STOP_PIDS[@]} -gt 0 ]]; then
  mapfile -t STOP_PIDS < <(printf '%s\n' "${STOP_PIDS[@]}" | awk 'NF && !seen[$0]++')
fi

if [[ ${#STOP_PIDS[@]} -gt 0 ]]; then
  for p in "${STOP_PIDS[@]}"; do
    [[ -n "$p" ]] || continue
    echo "Stopping TF2B_DeSeva_v1 dryrun pid=$p"
    kill "$p" 2>/dev/null || true
  done
  sleep 1
  for p in "${STOP_PIDS[@]}"; do
    [[ -n "$p" ]] || continue
    if ps -p "$p" >/dev/null 2>&1; then
      kill -9 "$p" 2>/dev/null || true
    fi
  done
  sleep 1
fi

if pgrep -f "python.*-m ${NEEDLE}" >/dev/null; then
  echo "WARNING: TF2B_DeSeva_v1 still alive; aborting"
  pgrep -af "python.*-m ${NEEDLE}" || true
  exit 1
fi

export ENABLE_REAL_TRADING=false
export MARKET_SOURCE

if [[ -f "$LOG_FILE" ]] && [[ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 50000000 ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
fi

echo "Starting TF2B_DeSeva_v1 Dry-Run (TF2BP × SEVA)..."
echo "  MARKET_SOURCE=$MARKET_SOURCE report=${REPORT_INTERVAL}s WIRE=NO"

nohup "$DIR/venv/bin/python3" -u -m antigravity.quant_pipeline.run_dryrun_tf2b_deseva_v1 \
  --symbol FX_BTC_JPY \
  --interval "$INTERVAL" \
  --report-interval "$REPORT_INTERVAL" \
  --market-source "$MARKET_SOURCE" \
  >> "$LOG_FILE" 2>&1 &

PID=$!
disown "$PID" || true
echo "$PID" > "$PID_FILE"
echo "✅ TF2B_DeSeva_v1 dryrun started PID=$PID"
echo "  Discord every ${REPORT_INTERVAL}s · Stop: $DIR/scripts/stop-dryrun-tf2b-deseva-v1.sh"
