#!/usr/bin/env bash
# Start TF2A_WOBI_HONEST (TF1A + HS HIT_BID_FORCE A/B · WIRE=NO). Does not touch LIVE or TF1A.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
LOG_FILE="$DIR/logs/dryrun_tf2a_wobi_honest.log"
PID_FILE="$DIR/data/dryrun_tf2a_wobi_honest.pid"
STOP_FLAG="$DIR/data/TF2A_WOBI_HONEST_DRYRUN_STOP.flag"
MARKET_SOURCE="${MARKET_SOURCE:-shared_feeder}"
REPORT_INTERVAL="${REPORT_INTERVAL:-3600}"
INTERVAL="${INTERVAL:-1.0}"
NEEDLE='antigravity\.quant_pipeline\.run_dryrun_tf2a_wobi_honest([[:space:]]|$)'
mkdir -p "$DIR/logs" "$DIR/data" "$DIR/data/mm_research/tf2a_wobi_honest_dryrun"
rm -f "$STOP_FLAG"
is_pid() {
  local p="${1:-}"
  [[ -n "$p" ]] || return 1
  ps -p "$p" >/dev/null 2>&1 || return 1
  tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -E -q "python[0-9.]* .*-m ${NEEDLE}"
}
STOP_PIDS=()
if [[ -f "$PID_FILE" ]]; then
  OLD="$(cat "$PID_FILE" || true)"
  if is_pid "${OLD:-}"; then STOP_PIDS+=("$OLD"); fi
fi
while read -r p; do
  [[ -n "$p" ]] || continue
  is_pid "$p" || continue
  STOP_PIDS+=("$p")
done < <(pgrep -f "python.*-m ${NEEDLE}" || true)
if [[ ${#STOP_PIDS[@]} -gt 0 ]]; then
  mapfile -t STOP_PIDS < <(printf '%s\n' "${STOP_PIDS[@]}" | awk 'NF && !seen[$0]++')
  for p in "${STOP_PIDS[@]}"; do echo "Stopping existing TF2A_WOBI_HONEST pid=$p"; kill "$p" 2>/dev/null || true; done
  sleep 1
  for p in "${STOP_PIDS[@]}"; do kill -9 "$p" 2>/dev/null || true; done
fi
rm -f "$PID_FILE"
export PYTHONPATH="$DIR"
export ENABLE_REAL_TRADING=false
echo "Starting TF2A_WOBI_HONEST (WIRE=NO · HS HIT_BID_FORCE · A/B vs TF1A)..."
setsid nohup "$DIR/venv/bin/python3" -u -m antigravity.quant_pipeline.run_dryrun_tf2a_wobi_honest \
  --interval "$INTERVAL" \
  --report-interval "$REPORT_INTERVAL" \
  --source "$MARKET_SOURCE" \
  >> "$LOG_FILE" 2>&1 < /dev/null &
NEW_PID=$!
disown "$NEW_PID" 2>/dev/null || true
echo "$NEW_PID" > "$PID_FILE"
sleep 2
if ! ps -p "$NEW_PID" >/dev/null 2>&1; then
  echo "ERROR: TF2A_WOBI_HONEST failed. See $LOG_FILE" >&2
  tail -n 30 "$LOG_FILE" >&2 || true
  exit 1
fi
echo "TF2A_WOBI_HONEST started PID=$NEW_PID"
echo "Log: $LOG_FILE"
echo "State: $DIR/data/dryrun_tf2a_wobi_honest_state.json"
