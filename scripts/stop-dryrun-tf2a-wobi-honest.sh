#!/usr/bin/env bash
# Stop TF2A_WOBI_HONEST. Does not touch LIVE or TF1A.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$DIR/data/dryrun_tf2a_wobi_honest.pid"
STOP_FLAG="$DIR/data/TF2A_WOBI_HONEST_DRYRUN_STOP.flag"
NEEDLE='antigravity\.quant_pipeline\.run_dryrun_tf2a_wobi_honest([[:space:]]|$)'
touch "$STOP_FLAG"
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" || true)"
  if [[ -n "${PID:-}" ]] && ps -p "$PID" >/dev/null 2>&1; then
    echo "Stopping TF2A_WOBI_HONEST pid=$PID"
    kill "$PID" 2>/dev/null || true
    sleep 1
    kill -9 "$PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi
pkill -f "python.*-m ${NEEDLE}" 2>/dev/null || true
echo "TF2A_WOBI_HONEST stopped"
