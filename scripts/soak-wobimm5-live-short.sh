#!/usr/bin/env bash
# WOBIMM5 LIVE short REAL soak (safe restart after fill-model fix).
# Defaults: 15 min OR 4 committed fills (whichever first).
# Requires operator GO gates — does NOT write ENABLE_REAL_TRADING.
#
# Watch during soak:
#   PREDICT_FILL / PREDICT_MISS / CANCEL
#   FILL src=PUBLIC_OID only (REAL ledger)
#   RECONCILE ... match=True
#
# Example:
#   CONFIRM_GO=WOBIMM5_LIVE ENABLE_REAL_TRADING=true \
#     MAX_RUNTIME_SEC=900 MAX_FILLS=4 \
#     ./scripts/soak-wobimm5-live-short.sh
set -euo pipefail
AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
cd "$AG"
export MAX_RUNTIME_SEC="${MAX_RUNTIME_SEC:-900}"
export MAX_FILLS="${MAX_FILLS:-4}"
export SIZE="${SIZE:-0.001}"
export DRY_RUN=0
LOG="$AG/logs/wobimm5_live.log"
WATCH="$AG/logs/wobimm5_live_soak_watch.log"
REPORT_PY="$AG/antigravity/quant_pipeline/report_wobimm5_live_soak.py"
PY="$AG/venv/bin/python3"
GO_FLAG="$AG/data/GO_WOBIMM5_LIVE.flag"

# Preflight: venue must be flat
"$PY" - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv("/home/azureuser/antigravity/.env")
os.environ["ENABLE_REAL_TRADING"] = "true"
from core.bitflyer_client import BitFlyerClient
from antigravity.quant_pipeline.run_wobimm5_live import parse_venue_net
c = BitFlyerClient(enable_real_trading=True, max_order_size=0.01)
vnet = parse_venue_net(c.get_positions("FX_BTC_JPY") or [])
act = c.get_child_orders("FX_BTC_JPY", child_order_state="ACTIVE") or []
print(f"preflight venue_net={vnet} active={len(act)}")
if abs(vnet) >= 1e-8 or act:
    raise SystemExit("REFUSE: venue not flat before short soak")
PY

# Arm GO if operator already set CONFIRM + ENABLE (script still won't write ENABLE)
if [[ ! -f "$GO_FLAG" ]] || ! grep -q 'status=ACTIVE' "$GO_FLAG"; then
  echo "REFUSE: write $GO_FLAG with status=ACTIVE before short soak" >&2
  exit 2
fi

rm -f "$AG/data/WOBIMM5_LIVE_STOP.flag"
: >"$WATCH"
echo "=== WOBIMM5 LIVE SHORT soak start max_runtime=${MAX_RUNTIME_SEC}s max_fills=${MAX_FILLS} ==="
"$AG/scripts/start-wobimm5-live.sh"

# Parallel watch: extract soak KPIs into watch log
(
  tail -n 0 -F "$LOG" 2>/dev/null | while IFS= read -r line; do
    case "$line" in
      *PREDICT*|*PREDICT_MISS*|*FILL*|*RECONCILE*|*DESYNC*|*PURGE_ORPHAN*|*FLATTEN*|*STOPPED*|*GHOST_POSITION*|*LATE_CLOSE_ECHO*|*SOFT_TO_HARD*|*VENUE_FORCE_SYNC*)
        echo "$(date -Iseconds) $line" | tee -a "$WATCH"
        ;;
    esac
  done
) &
WATCH_PID=$!

wall=$(( MAX_RUNTIME_SEC + 120 ))
loops=$(( (wall + 9) / 10 ))
for i in $(seq 1 "$loops"); do
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
kill "$WATCH_PID" 2>/dev/null || true

"$PY" "$REPORT_PY" || true
echo "=== SHORT soak done ==="
echo "log=$LOG watch=$WATCH"
echo "--- soak KPI grep ---"
grep -E 'PREDICT|FILL src=|RECONCILE|DESYNC|STOPPED' "$LOG" | tail -40 || true
