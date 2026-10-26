#!/usr/bin/env bash
# Start WOBIMM Dry-Run as FROZEN BENCHMARK (CSR-634 · CHANGE=NO).
# Do not pass strategy param overrides. Does not stop WOBIMM2.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

LOG_FILE="$DIR/logs/dryrun_wobimm.log"
PID_FILE="$DIR/data/dryrun_wobimm.pid"
STOP_FLAG="$DIR/data/WOBIMM_DRYRUN_STOP.flag"
MARKET_SOURCE="${MARKET_SOURCE:-shared_feeder}"
REPORT_INTERVAL="${REPORT_INTERVAL:-3600}"
INTERVAL="${INTERVAL:-1.0}"
NEEDLE='antigravity\.quant_pipeline\.run_dryrun_wobimm([[:space:]]|$)'

mkdir -p "$DIR/logs" "$DIR/data" "$DIR/data/mm_research/wobimm_dryrun"
rm -f "$STOP_FLAG"

is_wobimm_pid() {
  local p="${1:-}"
  [[ -n "$p" ]] || return 1
  ps -p "$p" >/dev/null 2>&1 || return 1
  tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -E -q "python[0-9.]* .*-m ${NEEDLE}"
}

STOP_PIDS=()
if [[ -f "$PID_FILE" ]]; then
  OLD="$(cat "$PID_FILE" || true)"
  if is_wobimm_pid "${OLD:-}"; then
    STOP_PIDS+=("$OLD")
  fi
fi
while read -r p; do
  [[ -n "$p" ]] || continue
  is_wobimm_pid "$p" || continue
  STOP_PIDS+=("$p")
done < <(pgrep -f "python.*-m ${NEEDLE}" || true)

if [[ ${#STOP_PIDS[@]} -gt 0 ]]; then
  mapfile -t STOP_PIDS < <(printf '%s\n' "${STOP_PIDS[@]}" | awk 'NF && !seen[$0]++')
fi

if [[ ${#STOP_PIDS[@]} -gt 0 ]]; then
  for p in "${STOP_PIDS[@]}"; do
    [[ -n "$p" ]] || continue
    echo "Stopping existing WOBIMM dryrun pid=$p"
    kill "$p" 2>/dev/null || true
  done
  sleep 1
  for p in "${STOP_PIDS[@]}"; do
    kill -9 "$p" 2>/dev/null || true
  done
fi

rm -f "$PID_FILE"

export PYTHONPATH="$DIR"
export ENABLE_REAL_TRADING=false

echo "Starting WOBIMM BENCHMARK (FROZEN · CSR-634 · WIRE=NO)..."
echo "  MARKET_SOURCE=$MARKET_SOURCE report=${REPORT_INTERVAL}s CHANGE=NO"

setsid nohup "$DIR/venv/bin/python3" -u -m antigravity.quant_pipeline.run_dryrun_wobimm \
  --interval "$INTERVAL" \
  --report-interval "$REPORT_INTERVAL" \
  --source "$MARKET_SOURCE" \
  >> "$LOG_FILE" 2>&1 < /dev/null &

NEW_PID=$!
disown "$NEW_PID" 2>/dev/null || true
echo "$NEW_PID" > "$PID_FILE"
sleep 2

if ! ps -p "$NEW_PID" >/dev/null 2>&1; then
  echo "ERROR: WOBIMM dryrun failed to start (PID $NEW_PID died). See $LOG_FILE" >&2
  tail -n 30 "$LOG_FILE" >&2 || true
  exit 1
fi

echo "WOBIMM BENCHMARK started (FROZEN)."
echo "PID: $NEW_PID"
echo "Log: $LOG_FILE"
echo "State: $DIR/data/dryrun_wobimm_state.json"
echo "Pin: $DIR/configs/wobimm_benchmark_frozen.json"
echo "Report Interval: ${REPORT_INTERVAL}s (1 hour)"
