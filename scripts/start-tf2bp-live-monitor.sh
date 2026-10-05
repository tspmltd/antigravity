#!/usr/bin/env bash
# TF2BP micro LIVE ops monitor — 1h -100bp Discord alert
set -euo pipefail
AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
cd "$AG"
PY="$AG/venv/bin/python3"
LOG="$AG/logs/tf2bp_micro_live_monitor.log"
PIDF="$AG/data/tf2bp_micro_live_monitor.pid"
mkdir -p "$AG/logs" "$AG/data"

if [[ -f "$PIDF" ]]; then
  old=$(cat "$PIDF" 2>/dev/null || true)
  if [[ -n "${old}" ]] && kill -0 "$old" 2>/dev/null; then
    echo "already running pid=$old"
    exit 0
  fi
  rm -f "$PIDF"
fi

echo "$$" >"$PIDF"
trap 'rm -f "$PIDF"' EXIT
echo "TF2BP LIVE monitor → $LOG (alert: rolling 1h <= -100bp)"
exec "$PY" -u -m antigravity.quant_pipeline.monitor_tf2bp_micro_live \
  --size 0.001 --loss-bp -100 \
  >>"$LOG" 2>&1
