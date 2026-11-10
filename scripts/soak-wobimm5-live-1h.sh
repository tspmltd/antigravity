#!/usr/bin/env bash
# WOBIMM5 LIVE 1h REAL soak — requires GO gates (same as start DRY_RUN=0).
# Does NOT write ENABLE_REAL_TRADING. Operator must set it + CONFIRM_GO + GO flag.
set -euo pipefail
AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
cd "$AG"
export MAX_RUNTIME_SEC=3600
export SIZE="${SIZE:-0.001}"
export DRY_RUN=0
LOG="$AG/logs/wobimm5_live.log"
REPORT_PY="$AG/antigravity/quant_pipeline/report_wobimm5_live_soak.py"
PY="$AG/venv/bin/python3"

rm -f "$AG/data/WOBIMM5_LIVE_STOP.flag"
echo "=== WOBIMM5 LIVE 1h soak start ==="
"$AG/scripts/start-wobimm5-live.sh"

# Wait until process exits (max_runtime flatten) or 3700s wall
for i in $(seq 1 370); do
  if ! pgrep -f 'antigravity.quant_pipeline.run_wobimm5_live' >/dev/null 2>&1; then
    echo "runner exited after ~$((i*10))s"
    break
  fi
  sleep 10
done

if pgrep -f 'antigravity.quant_pipeline.run_wobimm5_live' >/dev/null 2>&1; then
  echo "still running — forcing stop"
  "$AG/scripts/stop-wobimm5-live.sh" || true
fi

"$PY" "$REPORT_PY" || true
echo "=== WOBIMM5 LIVE 1h soak done ==="
echo "log=$LOG state=$AG/data/wobimm5_live_state.json"
