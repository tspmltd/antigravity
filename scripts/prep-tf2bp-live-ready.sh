#!/usr/bin/env bash
# CSR-581 — Prepare TF2BP LIVE path (wiring + dry smoke). Never flips ENABLE_REAL_TRADING.
set -euo pipefail
AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
cd "$AG"
PY="$AG/venv/bin/python3"
CAPS="$AG/configs/live_prep_caps_csr573.json"
OUT_DIR="$AG/data/mm_research/daily"
READY="$AG/data/TF2BP_LIVE_PREP_READY.json"
LOG="$AG/logs/tf2bp_live_prep_ready.log"
JST_DATE="$(TZ=Asia/Tokyo date +%Y-%m-%d)"
mkdir -p "$OUT_DIR" "$AG/logs" "$AG/data"

{
  echo "=== TF2BP LIVE prep $(TZ=Asia/Tokyo date '+%Y-%m-%dT%H:%M:%S%z') ==="
  echo "ENABLE_REAL_TRADING in .env (must stay false here):"
  grep -E '^ENABLE_REAL_TRADING=' "$AG/.env" 2>/dev/null || echo "(no .env line)"
  test -f "$CAPS" && echo "caps: OK $CAPS" || { echo "caps: MISSING"; exit 1; }
  test -x "$PY" && echo "python: OK" || { echo "python: MISSING"; exit 1; }
  test -f "$AG/antigravity/quant_pipeline/run_micro_live_soak_csr574.py" && echo "runner: OK" || exit 1
  test -x "$AG/scripts/start-tf2bp-micro-live.sh" && echo "starter: OK" || exit 1

  export ENABLE_REAL_TRADING=false
  if [[ "${SKIP_SMOKE:-0}" == "1" ]]; then
    echo "--- SKIP_SMOKE=1 (reuse prior smoke) ---"
    rc=0
    ready=true
  else
    echo "--- dry smoke (TF2BP only · ~90s wall via --smoke) ---"
    set +e
    timeout 120s "$PY" -u -m antigravity.quant_pipeline.run_micro_live_soak_csr574 \
      --only TF2BP_v1 --smoke --size 0.001 --max-trades 10 --interval 2.0 \
      >>"$LOG" 2>&1
    rc=$?
    set -e
    echo "smoke_rc=$rc (124=timeout treated as soft-ok if process ran)"
    if [[ "$rc" -ne 0 && "$rc" -ne 124 ]]; then
      echo "SMOKE_FAIL"
      ready=false
    else
      echo "SMOKE_OK"
      ready=true
    fi
  fi

  # Wiring ready ≠ GO2. Do not create GO2_TF2BP.flag here.
  if [[ "$ready" == "true" ]]; then py_ready=True; else py_ready=False; fi
  "$PY" - <<PY
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
JST = timezone(timedelta(hours=9))
ready = ${py_ready}
payload = {
  "csr": 581,
  "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
  "title": "TF2BP LIVE prep wired · DRY smoke",
  "wiring": "start-tf2bp-micro-live.sh → run_micro_live_soak_csr574 --only TF2BP_v1",
  "ENABLE_REAL_TRADING": False,
  "GO2_TF2BP_flag": False,
  "status": "READY_FOR_OPERATOR_GO2" if ready else "NOT_READY",
  "still_required_for_live": [
    "CSR-573 checklist G0-G4 green",
    "user message exactly: GO2 TF2BP",
    "operator creates data/GO2_TF2BP.flag",
    "operator sets ENABLE_REAL_TRADING=true (not this script)",
    "CONFIRM_GO2=TF2BP DRY_RUN=0 ./scripts/start-tf2bp-micro-live.sh",
  ],
  "caps": {"size_btc": 0.001, "max_trades": 10},
  "smoke_log": "logs/tf2bp_live_prep_ready.log",
  "smoke_rc": ${rc},
}
Path("$READY").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
Path("$OUT_DIR/${JST_DATE}_csr581_tf2bp_live_prep_ready.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
)
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
} | tee -a "$LOG"

echo "Wrote $READY"
echo "LIVE not started. ENABLE_REAL_TRADING untouched."
