#!/usr/bin/env bash
# Stop TF1A_WOBI_HONEST Dry-Run
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$DIR/data/dryrun_tf1a_wobi_honest.pid"
STOP_FLAG="$DIR/data/TF1A_WOBI_HONEST_DRYRUN_STOP.flag"
NEEDLE='antigravity\.quant_pipeline\.run_dryrun_tf1a_wobi_honest([[:space:]]|$)'

touch "$STOP_FLAG"
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" || true)"
  if [[ -n "$PID" ]] && ps -p "$PID" >/dev/null 2>&1; then
    kill "$PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

for p in $(pgrep -f "python.*-m ${NEEDLE}" || true); do
  kill "$p" 2>/dev/null || true
done
echo "TF1A_WOBI_HONEST stopped."
