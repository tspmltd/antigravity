#!/usr/bin/env bash
# Stop WOBIMM4_HONEST Dry-Run
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$DIR/data/dryrun_wobimm4_honest.pid"
STOP_FLAG="$DIR/data/WOBIMM4_HONEST_DRYRUN_STOP.flag"
NEEDLE='antigravity\.quant_pipeline\.run_dryrun_wobimm4_honest([[:space:]]|$)'
touch "$STOP_FLAG"
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
  for p in "${STOP_PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
  sleep 1
  for p in "${STOP_PIDS[@]}"; do kill -9 "$p" 2>/dev/null || true; done
  echo "Stopped WOBIMM4_HONEST (${STOP_PIDS[*]})"
else
  echo "WOBIMM4_HONEST is not running."
fi
rm -f "$PID_FILE"
