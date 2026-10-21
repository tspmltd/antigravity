#!/usr/bin/env bash
# TF2BP micro LIVE starter — CSR-569/573/581
# Default: DRY_RUN=1 (paper / soak runner · TF2BP only).
# Real orders require ALL of:
#   1) data/GO2_TF2BP.flag present (operator after checklist)
#   2) CONFIRM_GO2=TF2BP in this shell
#   3) ENABLE_REAL_TRADING=true already set by operator (never written by this script)
#   4) runner --confirm-go MICRO574 (internal CSR-574 gate)
set -euo pipefail

AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
cd "$AG"
DRY_RUN="${DRY_RUN:-1}"
GO2_MARK="$AG/data/GO2_TF2BP.flag"
CAPS="$AG/configs/live_prep_caps_csr573.json"
LOG="$AG/logs/tf2bp_micro_live.log"
PIDF="$AG/data/tf2bp_micro_live.pid"
PY="$AG/venv/bin/python3"
SIZE="${SIZE:-0.001}"
# 0 = unlimited trades (continuous until stop / daily loss / runtime)
MAX_TRADES="${MAX_TRADES:-0}"
# 0 = no wall-clock cap
MAX_RUNTIME_SEC="${MAX_RUNTIME_SEC:-0}"
INTERVAL="${INTERVAL:-2.0}"
SMOKE="${SMOKE:-0}"

# H1: do not start a second soak runner. flock does not cover a pre-v1.1 process.
if [[ -n "$(ps -eo args | awk '/antigravity\.quant_pipeline\.run_micro_live_soak_csr574/ && !/awk/ {print 1; exit}')" ]]; then
  echo "REFUSE: soak runner already active (flock is not sufficient)" >&2
  exit 1
fi

mkdir -p "$AG/logs" "$AG/data"

if [[ ! -f "$CAPS" ]]; then
  echo "ERROR: missing caps $CAPS" >&2
  exit 1
fi
if [[ ! -x "$PY" ]]; then
  echo "ERROR: missing venv python $PY" >&2
  exit 1
fi

# Hard caps: size only (trade-count ceiling removed for continuous LIVE)
awk -v s="$SIZE" -v m="$MAX_TRADES" 'BEGIN{
  if (s+0 > 0.001+1e-12) { print "REFUSE: SIZE must be <= 0.001" > "/dev/stderr"; exit 2 }
  if (m+0 < 0) { print "REFUSE: MAX_TRADES must be >= 0 (0=unlimited)" > "/dev/stderr"; exit 2 }
}'

ARGS=(
  -u -m antigravity.quant_pipeline.run_micro_live_soak_csr574
  --only TF2BP_v1
  --size "$SIZE"
  --max-trades "$MAX_TRADES"
  --max-runtime-sec "$MAX_RUNTIME_SEC"
  --interval "$INTERVAL"
)
if [[ "$SMOKE" == "1" ]]; then
  ARGS+=( --smoke )
fi

# Never write ENABLE_REAL_TRADING=true into .env from this script.
if [[ "$DRY_RUN" != "0" ]]; then
  export ENABLE_REAL_TRADING=false
  echo "TF2BP micro DRY_RUN=1 → $LOG (only=TF2BP_v1 size=$SIZE max_trades=$MAX_TRADES)"
  echo "$$" >"$PIDF"
  # shellcheck disable=SC2064
  trap 'rm -f "$PIDF"' EXIT
  exec "$PY" "${ARGS[@]}" >>"$LOG" 2>&1
fi

# DRY_RUN=0 — hard gates (operator LIVE)
if [[ ! -f "$GO2_MARK" ]]; then
  echo "REFUSE: $GO2_MARK missing. Complete checklist + user message GO2 TF2BP first." >&2
  exit 2
fi
if [[ "${CONFIRM_GO2:-}" != "TF2BP" ]]; then
  echo "REFUSE: set CONFIRM_GO2=TF2BP for this shell only after GO2." >&2
  exit 2
fi
if [[ "${ENABLE_REAL_TRADING:-false}" != "true" ]]; then
  echo "REFUSE: ENABLE_REAL_TRADING is not true (do not set it from this script)." >&2
  exit 2
fi

echo "TF2BP micro REAL → $LOG (GO2 gates passed · runner confirm MICRO574)"
echo "$$" >"$PIDF"
trap 'rm -f "$PIDF"' EXIT
exec "$PY" "${ARGS[@]}" --real --confirm-go MICRO574 >>"$LOG" 2>&1
