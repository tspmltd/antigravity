#!/usr/bin/env bash
# PEG2-B LIVE Trial v1 (CSR-588)
# Default DRY_RUN=1. Real orders require ALL of:
#   1) data/GO_PEG2B_LIVE_TRIAL.flag
#   2) CONFIRM_GO=PEG2B_LIVE_TRIAL_V1
#   3) ENABLE_REAL_TRADING=true in this shell (never written to .env by this script)
#   4) DRY_RUN=0
set -euo pipefail

AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
cd "$AG"
DRY_RUN="${DRY_RUN:-1}"
SIZE="${SIZE:-0.001}"
INTERVAL="${INTERVAL:-1.0}"
LOG="$AG/logs/peg2b_live_trial.log"
PIDF="$AG/data/peg2b_live_trial.pid"
PY="$AG/venv/bin/python3"
GO_FLAG="$AG/data/GO_PEG2B_LIVE_TRIAL.flag"

mkdir -p "$AG/logs" "$AG/data" "$AG/data/mm_research/peg2b_live_trial/reports"

if [[ -n "$(ps -eo args | awk '/antigravity\.quant_pipeline\.run_peg2b_live_trial/ && !/awk/ {print 1; exit}')" ]]; then
  echo "REFUSE: peg2b live trial already active" >&2
  exit 1
fi
if [[ -n "$(ps -eo args | awk '/antigravity\.quant_pipeline\.run_micro_live_soak_csr574/ && !/awk/ {print 1; exit}')" ]]; then
  echo "REFUSE: TF2BP soak runner active" >&2
  exit 1
fi

awk -v s="$SIZE" 'BEGIN{
  if (s+0 > 0.001+1e-12) { print "REFUSE: SIZE must be <= 0.001" > "/dev/stderr"; exit 2 }
}'

ARGS=(
  -u -m antigravity.quant_pipeline.run_peg2b_live_trial
  --size "$SIZE"
  --interval "$INTERVAL"
)

if [[ "$DRY_RUN" != "0" ]]; then
  export ENABLE_REAL_TRADING=false
  echo "PEG2-B LIVE Trial DRY_RUN=1 → $LOG (size=$SIZE)"
  nohup "$PY" "${ARGS[@]}" >>"$LOG" 2>&1 &
  echo $! >"$PIDF"
  echo "pid=$(cat "$PIDF")"
  exit 0
fi

if [[ ! -f "$GO_FLAG" ]]; then
  echo "REFUSE: $GO_FLAG missing" >&2
  exit 2
fi
if [[ "${CONFIRM_GO:-}" != "PEG2B_LIVE_TRIAL_V1" ]]; then
  echo "REFUSE: set CONFIRM_GO=PEG2B_LIVE_TRIAL_V1" >&2
  exit 2
fi
if [[ "${ENABLE_REAL_TRADING:-false}" != "true" ]]; then
  echo "REFUSE: ENABLE_REAL_TRADING is not true (do not set it from this script into .env)" >&2
  exit 2
fi

# Never write ENABLE_REAL_TRADING=true into .env
ARGS+=(--real --confirm-go PEG2B588)
echo "PEG2-B LIVE Trial REAL → $LOG (GO gates passed · size=$SIZE)"
nohup env ENABLE_REAL_TRADING=true CONFIRM_GO=PEG2B_LIVE_TRIAL_V1 \
  "$PY" "${ARGS[@]}" >>"$LOG" 2>&1 &
echo $! >"$PIDF"
echo "pid=$(cat "$PIDF") log=$LOG"
