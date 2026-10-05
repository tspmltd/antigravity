#!/usr/bin/env bash
# Stop TF2BP micro LIVE (unlimited session).
set -euo pipefail
AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
STOP="$AG/data/TF2BP_LIVE_STOP.flag"
mkdir -p "$AG/data"
date "+stop_requested %Y-%m-%dT%H:%M:%S%z" >"$STOP"
echo "wrote $STOP"
if [[ -f "$AG/data/tf2bp_micro_live.pid" ]]; then
  pid=$(cat "$AG/data/tf2bp_micro_live.pid" 2>/dev/null || true)
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "sending TERM to live pid=$pid"
    kill -TERM "$pid" || true
  fi
fi
echo "LIVE stop requested."
