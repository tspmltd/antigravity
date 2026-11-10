#!/usr/bin/env bash
# WOBIMM5 LIVE starter — MM5 clean chassis
# Default DRY_RUN=1 (no venue POST).
# Real orders require ALL of:
#   1) data/GO_WOBIMM5_LIVE.flag with status=ACTIVE
#   2) CONFIRM_GO=WOBIMM5_LIVE in this shell
#   3) ENABLE_REAL_TRADING=true already set by operator (never written by this script)
#   4) runner --real --confirm-go WOBIMM5
#   5) SIZE <= 0.001
#   6) WOBIMM5_LIVE_STOP.flag absent
set -euo pipefail

AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
cd "$AG"
DRY_RUN="${DRY_RUN:-1}"
SIZE="${SIZE:-0.001}"
INTERVAL="${INTERVAL:-1.0}"
REPORT_INTERVAL="${REPORT_INTERVAL:-3600}"
MAX_RUNTIME_SEC="${MAX_RUNTIME_SEC:-0}"
MAX_FILLS="${MAX_FILLS:-0}"
MARKET_SOURCE="${MARKET_SOURCE:-shared_feeder}"
GO_FLAG="$AG/data/GO_WOBIMM5_LIVE.flag"
STOP_FLAG="$AG/data/WOBIMM5_LIVE_STOP.flag"
LOG="$AG/logs/wobimm5_live.log"
PIDF="$AG/data/wobimm5_live.pid"
PY="$AG/venv/bin/python3"
NEEDLE='antigravity\.quant_pipeline\.run_wobimm5_live([[:space:]]|$)'

mkdir -p "$AG/logs" "$AG/data" "$AG/data/mm_research/wobimm5_live"

if [[ ! -x "$PY" ]]; then
  echo "ERROR: missing venv python $PY" >&2
  exit 1
fi

if pgrep -f "python.*-m ${NEEDLE}" >/dev/null 2>&1; then
  echo "REFUSE: WOBIMM5 live already active" >&2
  exit 1
fi

awk -v s="$SIZE" 'BEGIN{
  if (s+0 > 0.001+1e-12) { print "REFUSE: SIZE must be <= 0.001" > "/dev/stderr"; exit 2 }
}'

ARGS=(
  -u -m antigravity.quant_pipeline.run_wobimm5_live
  --size "$SIZE"
  --interval "$INTERVAL"
  --report-interval "$REPORT_INTERVAL"
  --source "$MARKET_SOURCE"
)
if [[ "${MAX_RUNTIME_SEC}" != "0" && -n "${MAX_RUNTIME_SEC}" ]]; then
  ARGS+=(--max-runtime-sec "$MAX_RUNTIME_SEC")
fi
if [[ "${MAX_FILLS}" != "0" && -n "${MAX_FILLS}" ]]; then
  ARGS+=(--max-fills "$MAX_FILLS")
fi

if [[ "$DRY_RUN" != "0" ]]; then
  export ENABLE_REAL_TRADING=false
  echo "WOBIMM5 LIVE DRY_RUN=1 → $LOG (size=$SIZE max_runtime=$MAX_RUNTIME_SEC max_fills=$MAX_FILLS)"
  setsid nohup "$PY" "${ARGS[@]}" >>"$LOG" 2>&1 < /dev/null &
  echo $! >"$PIDF"
  echo "pid=$(cat "$PIDF") log=$LOG"
  exit 0
fi

if [[ -f "$STOP_FLAG" ]]; then
  echo "REFUSE: STOP flag present ($STOP_FLAG)" >&2
  exit 2
fi
if [[ ! -f "$GO_FLAG" ]]; then
  echo "REFUSE: $GO_FLAG missing" >&2
  exit 2
fi
if ! grep -q 'status=ACTIVE' "$GO_FLAG"; then
  echo "REFUSE: $GO_FLAG is not status=ACTIVE" >&2
  exit 2
fi
if [[ "${CONFIRM_GO:-}" != "WOBIMM5_LIVE" ]]; then
  echo "REFUSE: set CONFIRM_GO=WOBIMM5_LIVE" >&2
  exit 2
fi
if [[ "${ENABLE_REAL_TRADING:-false}" != "true" ]]; then
  echo "REFUSE: ENABLE_REAL_TRADING is not true (do not set it from this script)." >&2
  exit 2
fi

ARGS+=(--real --confirm-go WOBIMM5)
echo "WOBIMM5 LIVE REAL → $LOG (GO gates passed · size=$SIZE)"
setsid nohup env ENABLE_REAL_TRADING=true CONFIRM_GO=WOBIMM5_LIVE \
  "$PY" "${ARGS[@]}" >>"$LOG" 2>&1 < /dev/null &
echo $! >"$PIDF"
sleep 2
if ! ps -p "$(cat "$PIDF")" >/dev/null 2>&1; then
  echo "ERROR: WOBIMM5 live failed to start. See $LOG" >&2
  tail -n 40 "$LOG" >&2 || true
  exit 1
fi
echo "pid=$(cat "$PIDF") log=$LOG state=$AG/data/wobimm5_live_state.json"
