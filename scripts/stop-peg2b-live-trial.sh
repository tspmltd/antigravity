#!/usr/bin/env bash
# Stop PEG2-B LIVE Trial → runner FLAT on signal / stop flag
set -euo pipefail
AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
cd "$AG"
FLAG="$AG/data/PEG2B_LIVE_TRIAL_STOP.flag"
PIDF="$AG/data/peg2b_live_trial.pid"

mkdir -p "$AG/data"
echo "STOP $(date '+%Y-%m-%dT%H:%M:%S%z')" >"$FLAG"
echo "wrote $FLAG"

if [[ -f "$PIDF" ]]; then
  pid="$(cat "$PIDF" || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" || true
    echo "SIGTERM → $pid"
  fi
fi

# wait up to 30s for clean FLAT
for i in $(seq 1 30); do
  if ! pgrep -f 'antigravity.quant_pipeline.run_peg2b_live_trial' >/dev/null 2>&1; then
    echo "trial process gone"
    exit 0
  fi
  sleep 1
done
echo "WARN: still running after 30s — check logs/peg2b_live_trial.log" >&2
exit 1
