#!/usr/bin/env bash
# systemd ExecStart wrapper for TF2BP micro LIVE (CSR-581 continuous).
# Auto-restart is handled by systemd (Restart=on-failure).
# Intentional stop: scripts/stop-tf2bp-micro-live.sh → exit 0 (no restart).
set -euo pipefail

AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
cd "$AG"

GO2_MARK="$AG/data/GO2_TF2BP.flag"
STOP="$AG/data/TF2BP_LIVE_STOP.flag"
FLEET="$AG/data/FLEET_STOPPED.flag"
PIDF="$AG/data/tf2bp_micro_live.pid"
LOG="$AG/logs/tf2bp_micro_live.log"
PY="$AG/venv/bin/python3"
MEM_MIN_MB="${TF2BP_LIVE_MEM_MIN_MB:-1200}"

mkdir -p "$AG/logs" "$AG/data"

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

if [[ -f "$STOP" ]]; then
  echo "[$(ts)] REFUSE: STOP flag present ($STOP) — intentional halt" | tee -a "$LOG"
  exit 0
fi
if [[ -f "$FLEET" ]]; then
  echo "[$(ts)] REFUSE: FLEET_STOPPED present — waiting for clear" | tee -a "$LOG"
  exit 0
fi
if [[ ! -f "$GO2_MARK" ]]; then
  echo "[$(ts)] REFUSE: missing $GO2_MARK" | tee -a "$LOG"
  exit 2
fi
if [[ "${CONFIRM_GO2:-}" != "TF2BP" ]]; then
  echo "[$(ts)] REFUSE: CONFIRM_GO2 != TF2BP" | tee -a "$LOG"
  exit 2
fi
if [[ "${ENABLE_REAL_TRADING:-false}" != "true" ]]; then
  echo "[$(ts)] REFUSE: ENABLE_REAL_TRADING is not true (process env only; .env unchanged)" | tee -a "$LOG"
  exit 2
fi
if [[ ! -x "$PY" ]]; then
  echo "[$(ts)] REFUSE: missing python $PY" | tee -a "$LOG"
  exit 1
fi

avail_mb=$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
if (( avail_mb < MEM_MIN_MB )); then
  echo "[$(ts)] REFUSE: MemAvailable=${avail_mb}MB < ${MEM_MIN_MB}MB (avoid hang)" | tee -a "$LOG"
  exit 75
fi

# H1: pre-v1.1 runners never take the flock. Refuse while any soak runner exists.
runners=$(ps -eo args | awk '/antigravity\.quant_pipeline\.run_micro_live_soak_csr574/ && !/awk/ {c++} END{print c+0}')
if [[ "$runners" != "0" ]]; then
  echo "[$(ts)] REFUSE: soak runner already active count=$runners (flock is not sufficient)" | tee -a "$LOG"
  exit 0
fi

# This cutover's live test is 0.001 BTC x at most 20 closed trades.
if ! awk -v m="${MAX_TRADES:-0}" 'BEGIN{ exit (m+0>0 && m+0<=20)?0:1 }'; then
  echo "[$(ts)] REFUSE: MAX_TRADES=${MAX_TRADES:-} must be 1..20 for the micro LIVE soak" | tee -a "$LOG"
  exit 2
fi

# Clear stale pid if previous unit died hard
if [[ -f "$PIDF" ]]; then
  old=$(cat "$PIDF" 2>/dev/null || true)
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    echo "[$(ts)] REFUSE: already running pid=$old" | tee -a "$LOG"
    exit 0
  fi
  rm -f "$PIDF"
fi

echo $$ >"$PIDF"
cleanup() { rm -f "$PIDF"; }
trap cleanup EXIT

SIZE="${SIZE:-0.001}"
MAX_TRADES="${MAX_TRADES:-0}"
MAX_RUNTIME_SEC="${MAX_RUNTIME_SEC:-0}"
INTERVAL="${INTERVAL:-2.0}"

echo "[$(ts)] TF2BP micro LIVE start mem_avail=${avail_mb}MB size=$SIZE max_trades=$MAX_TRADES runtime=$MAX_RUNTIME_SEC" | tee -a "$LOG"

exec "$PY" -u -m antigravity.quant_pipeline.run_micro_live_soak_csr574 \
  --only TF2BP_v1 \
  --size "$SIZE" \
  --max-trades "$MAX_TRADES" \
  --max-runtime-sec "$MAX_RUNTIME_SEC" \
  --interval "$INTERVAL" \
  --real --confirm-go MICRO574 \
  >>"$LOG" 2>&1
