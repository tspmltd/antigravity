#!/usr/bin/env bash
# Stop WOBIMM5 LIVE → runner FLAT on signal / stop flag
set -euo pipefail
AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
cd "$AG"
FLAG="$AG/data/WOBIMM5_LIVE_STOP.flag"
PIDF="$AG/data/wobimm5_live.pid"

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

for i in $(seq 1 40); do
  if ! pgrep -f 'antigravity.quant_pipeline.run_wobimm5_live' >/dev/null 2>&1; then
    echo "WOBIMM5 live process gone"
    exit 0
  fi
  sleep 1
done
echo "WARN: still running after 40s — check logs/wobimm5_live.log" >&2
exit 1
