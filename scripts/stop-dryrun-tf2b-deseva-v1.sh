#!/usr/bin/env bash
# Stop TF2B_DeSeva_v1 Dry-Run only.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$DIR/data/dryrun_tf2b_deseva_v1.pid"
STOP_FLAG="$DIR/data/TF2B_DESEVA_V1_DRYRUN_STOP.flag"
NEEDLE='antigravity.quant_pipeline.run_dryrun_tf2b_deseva_v1'

mkdir -p "$DIR/data"
touch "$STOP_FLAG"

STOP_PIDS=()
if [[ -f "$PID_FILE" ]]; then
  OLD="$(cat "$PID_FILE" || true)"
  if [[ -n "${OLD:-}" ]] && ps -p "$OLD" >/dev/null 2>&1; then
    if tr '\0' ' ' < "/proc/$OLD/cmdline" 2>/dev/null | grep -E -q "python[0-9.]* .*-m ${NEEDLE}"; then
      STOP_PIDS+=("$OLD")
    fi
  fi
fi
while read -r p; do
  [[ -n "$p" ]] || continue
  tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -E -q "python[0-9.]* .*-m ${NEEDLE}" || continue
  STOP_PIDS+=("$p")
done < <(pgrep -f "python.*-m ${NEEDLE}" || true)

if [[ ${#STOP_PIDS[@]} -gt 0 ]]; then
  mapfile -t STOP_PIDS < <(printf '%s\n' "${STOP_PIDS[@]}" | awk 'NF && !seen[$0]++')
fi

if [[ ${#STOP_PIDS[@]} -eq 0 ]]; then
  echo "TF2B_DeSeva_v1 dryrun not running"
  rm -f "$STOP_FLAG"
  exit 0
fi

for p in "${STOP_PIDS[@]}"; do
  echo "Stopping TF2B_DeSeva_v1 dryrun pid=$p"
  kill "$p" 2>/dev/null || true
done
sleep 2
for p in "${STOP_PIDS[@]}"; do
  if ps -p "$p" >/dev/null 2>&1; then
    kill -9 "$p" 2>/dev/null || true
  fi
done

rm -f "$PID_FILE" "$STOP_FLAG"
echo "✅ TF2B_DeSeva_v1 dryrun stopped"
