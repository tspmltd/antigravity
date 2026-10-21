#!/usr/bin/env bash
# Stop TF2B_PRD Dry-Run
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$DIR/data/dryrun_tf2b_prd.pid"
STOP_FLAG="$DIR/data/TF2B_PRD_DRYRUN_STOP.flag"
NEEDLE='antigravity.quant_pipeline.run_dryrun_tf2b_prd'

touch "$STOP_FLAG"

is_prd_pid() {
  local p="${1:-}"
  [[ -n "$p" ]] || return 1
  ps -p "$p" >/dev/null 2>&1 || return 1
  tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -E -q "python[0-9.]* .*-m ${NEEDLE}"
}

STOP_PIDS=()
if [[ -f "$PID_FILE" ]]; then
  OLD="$(cat "$PID_FILE" || true)"
  if is_prd_pid "${OLD:-}"; then
    STOP_PIDS+=("$OLD")
  fi
fi
while read -r p; do
  [[ -n "$p" ]] || continue
  is_prd_pid "$p" || continue
  STOP_PIDS+=("$p")
done < <(pgrep -f "python.*-m ${NEEDLE}" || true)

if [[ ${#STOP_PIDS[@]} -gt 0 ]]; then
  mapfile -t STOP_PIDS < <(printf '%s\n' "${STOP_PIDS[@]}" | awk 'NF && !seen[$0]++')
fi

if [[ ${#STOP_PIDS[@]} -gt 0 ]]; then
  for p in "${STOP_PIDS[@]}"; do
    [[ -n "$p" ]] || continue
    echo "Stopping TF2B_PRD dryrun pid=$p"
    kill "$p" 2>/dev/null || true
  done
  sleep 1
  for p in "${STOP_PIDS[@]}"; do
    kill -9 "$p" 2>/dev/null || true
  done
  echo "Stopped TF2B_PRD (${STOP_PIDS[*]})"
else
  echo "TF2B_PRD is not running."
fi

rm -f "$PID_FILE"
