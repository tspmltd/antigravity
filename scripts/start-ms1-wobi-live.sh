#!/usr/bin/env bash
# MS1_WOBI LIVE starter (CSR-694) — greenfield micro-scalp chassis.
# Default DRY_RUN=1 (no venue POST).
# Real orders require ALL of:
#   1) data/GO_MS1_WOBI_LIVE.flag with status=ACTIVE
#   2) CONFIRM_GO=MS1_LIVE in this shell
#   3) ENABLE_REAL_TRADING=true already set by operator (never written by this script)
#   4) runner --real --confirm-go MS1
#   5) SIZE <= 0.001
#   6) MS1_WOBI_LIVE_STOP.flag absent
#   7) no concurrent other LIVE lanes
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
GO_FLAG="$AG/data/GO_MS1_WOBI_LIVE.flag"
STOP_FLAG="$AG/data/MS1_WOBI_LIVE_STOP.flag"
LOG="$AG/logs/ms1_wobi_live.log"
PIDF="$AG/data/ms1_wobi_live.pid"
PY="$AG/venv/bin/python3"
NEEDLE='antigravity\.quant_pipeline\.run_ms1_wobi_live([[:space:]]|$)'

mkdir -p "$AG/logs" "$AG/data" "$AG/data/mm_research/ms1_wobi_live"

if [[ ! -x "$PY" ]]; then
  echo "ERROR: missing venv python $PY" >&2
  exit 1
fi

if pgrep -f "python.*-m ${NEEDLE}" >/dev/null 2>&1; then
  echo "REFUSE: MS1 live already active" >&2
  exit 1
fi
if pgrep -f 'antigravity\.quant_pipeline\.run_wobimm_live' >/dev/null 2>&1; then
  echo "REFUSE: WOBIMM live active" >&2
  exit 1
fi
if pgrep -f 'antigravity\.quant_pipeline\.run_wobimm5_live' >/dev/null 2>&1; then
  echo "REFUSE: WOBIMM5 live active" >&2
  exit 1
fi
if pgrep -f 'antigravity\.quant_pipeline\.run_tf2b_wobi3_live' >/dev/null 2>&1; then
  echo "REFUSE: TF2B_WOBI3 live active" >&2
  exit 1
fi
if pgrep -f 'antigravity\.quant_pipeline\.run_peg2b_live_trial' >/dev/null 2>&1; then
  echo "REFUSE: PEG2-B live trial active" >&2
  exit 1
fi
if pgrep -f 'antigravity\.quant_pipeline\.run_micro_live_soak_csr574' >/dev/null 2>&1; then
  echo "REFUSE: TF2BP soak runner active" >&2
  exit 1
fi

awk -v s="$SIZE" 'BEGIN{
  if (s+0 > 0.001+1e-12) { print "REFUSE: SIZE must be <= 0.001" > "/dev/stderr"; exit 2 }
}'

ARGS=(
  -u -m antigravity.quant_pipeline.run_ms1_wobi_live
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

# Never write ENABLE_REAL_TRADING=true into .env from this script.
if [[ "$DRY_RUN" != "0" ]]; then
  export ENABLE_REAL_TRADING=false
  echo "MS1_WOBI LIVE DRY_RUN=1 → $LOG (size=$SIZE max_runtime=$MAX_RUNTIME_SEC max_fills=$MAX_FILLS)"
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
if [[ "${CONFIRM_GO:-}" != "MS1_LIVE" ]]; then
  echo "REFUSE: set CONFIRM_GO=MS1_LIVE" >&2
  exit 2
fi
if [[ "${ENABLE_REAL_TRADING:-false}" != "true" ]]; then
  echo "REFUSE: ENABLE_REAL_TRADING is not true (do not set it from this script)." >&2
  exit 2
fi

ARGS+=(--real --confirm-go MS1)
echo "MS1_WOBI LIVE REAL → $LOG (GO gates passed · size=$SIZE · Maker EV_GONE)"
setsid nohup env ENABLE_REAL_TRADING=true CONFIRM_GO=MS1_LIVE \
  "$PY" "${ARGS[@]}" >>"$LOG" 2>&1 < /dev/null &
echo $! >"$PIDF"
sleep 2
if ! ps -p "$(cat "$PIDF")" >/dev/null 2>&1; then
  echo "ERROR: MS1 live failed to start. See $LOG" >&2
  tail -n 40 "$LOG" >&2 || true
  exit 1
fi
echo "pid=$(cat "$PIDF") log=$LOG state=$AG/data/ms1_wobi_live_state.json"
