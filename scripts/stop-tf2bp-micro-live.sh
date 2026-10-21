#!/usr/bin/env bash
# Stop TF2BP micro LIVE.
# Cutover success: soak runner process count == 0.
# Venue flat is recorded and is not a success condition. No compensating order.
set -euo pipefail
AG="${ANTIGRAVITY_ROOT:-/home/azureuser/antigravity}"
STOP="$AG/data/TF2BP_LIVE_STOP.flag"
FLAT="$AG/data/TF2BP_LAST_FLAT.json"
LOG="$AG/logs/tf2bp_micro_live.log"
WAIT_SEC="${TF2BP_STOP_WAIT_SEC:-120}"
mkdir -p "$AG/data" "$AG/logs"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

date "+stop_requested %Y-%m-%dT%H:%M:%S%z" >"$STOP"
echo "wrote $STOP — waiting up to ${WAIT_SEC}s for process exit (venue flat not required)"
LOG_MARK=$(wc -c <"$LOG" 2>/dev/null || echo 0)

# H3 baseline: read-only venue before the stop signal.
cd "$AG"
set -a
# shellcheck disable=SC1091
[ -f .env ] && . ./.env
set +a
ENABLE_REAL_TRADING=true "$AG/venv/bin/python3" - <<'PY'
import json, os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv("/home/azureuser/antigravity/.env")
os.environ["ENABLE_REAL_TRADING"] = "true"
from core.bitflyer_client import BitFlyerClient
c = BitFlyerClient(enable_real_trading=True, max_order_size=0.05)
rows = c.get_positions("FX_BTC_JPY") or []
net = 0.0
for p in rows:
    side = (p.get("side") or "").upper()
    sz = float(p.get("size") or 0)
    net += sz if side == "BUY" else -sz if side == "SELL" else 0.0
Path("/home/azureuser/antigravity/data/tf2bp_cutover_h3_pre.json").write_text(
    json.dumps({"venue_net": net}, indent=2) + "\n"
)
print(f"venue_net_before_stop={net}")
PY

# Ask unit to stop (SIGTERM). A SELL from the old runner is observed, not corrected.
if systemctl --user is-active tf2bp-micro-live.service >/dev/null 2>&1; then
  systemctl --user stop tf2bp-micro-live-monitor.service 2>/dev/null || true
  systemctl --user stop tf2bp-micro-live.service &
  stop_pid=$!
fi

runner_pids() {
  ps -eo pid,args | awk '/antigravity\.quant_pipeline\.run_micro_live_soak_csr574/ && !/awk/ {print $1}'
}

deadline=$((SECONDS + WAIT_SEC))
while (( SECONDS < deadline )); do
  if [[ -z "$(runner_pids)" ]]; then
    echo "process exited"
    break
  fi
  sleep 2
done

# Ensure unit is down
systemctl --user stop tf2bp-micro-live.service 2>/dev/null || true
systemctl --user stop tf2bp-micro-live-monitor.service 2>/dev/null || true
wait "${stop_pid:-}" 2>/dev/null || true

# One more TERM to the runner pids only, after the wait.
still="$(runner_pids)"
if [[ -n "$still" ]]; then
  echo "WARN: still running after ${WAIT_SEC}s — sending TERM once more to: $still"
  # shellcheck disable=SC2086
  kill -TERM $still || true
  sleep 5
fi

# H2: success is process absence. Venue flat is an observation, not success.
still="$(runner_pids)"
if [[ -n "$still" ]]; then
  echo "ERROR: soak runner still active after stop: $still" >&2
  exit 1
fi

ENABLE_REAL_TRADING=true "$AG/venv/bin/python3" - <<'PY'
import json, os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv("/home/azureuser/antigravity/.env")
os.environ["ENABLE_REAL_TRADING"] = "true"
from core.bitflyer_client import BitFlyerClient
c = BitFlyerClient(enable_real_trading=True, max_order_size=0.05)
rows = c.get_positions("FX_BTC_JPY") or []
net = 0.0
for p in rows:
    side = (p.get("side") or "").upper()
    sz = float(p.get("size") or 0)
    net += sz if side == "BUY" else -sz if side == "SELL" else 0.0
Path("/home/azureuser/antigravity/data/TF2BP_STOP_VERIFY.json").write_text(
    json.dumps({"venue_net": net, "process_zero": True, "venue_flat_required": False}, indent=2) + "\n"
)
print(f"venue_net_after_stop={net}")
PY

# H3: record SELL submit / SELL fill / venue change. Do not send a compensating order.
SLICE="$AG/data/tf2bp_cutover_h3_logslice.txt"
if [[ -f "$LOG" ]]; then
  tail -c +"$((LOG_MARK + 1))" "$LOG" >"$SLICE" || true
else
  : >"$SLICE"
fi
"$AG/venv/bin/python3" - <<'PY'
import json
from pathlib import Path
text = Path("/home/azureuser/antigravity/data/tf2bp_cutover_h3_logslice.txt").read_text(errors="replace")
needles = ("SELL", "FLATTEN", "send_order", "exit confirmed")
hits = [ln for ln in text.splitlines() if any(n in ln for n in needles)]
pre = json.loads(Path("/home/azureuser/antigravity/data/tf2bp_cutover_h3_pre.json").read_text())
post = json.loads(Path("/home/azureuser/antigravity/data/TF2BP_STOP_VERIFY.json").read_text())
row = {
    "old_stop_ok": "process_zero",
    "venue_flat_required": False,
    "venue_net_before": pre.get("venue_net"),
    "venue_net_after": post.get("venue_net"),
    "sell_or_flatten_lines": hits[-40:],
    "compensating_buy": "forbidden",
}
Path("/home/azureuser/antigravity/data/tf2bp_cutover_h3_watch.json").write_text(
    json.dumps(row, ensure_ascii=False, indent=2) + "\n"
)
print(
    f"STOP_OK process_zero venue_net_before={row['venue_net_before']} "
    f"venue_net_after={row['venue_net_after']} watch_lines={len(hits)}"
)
PY

echo "LIVE stop complete. Venue flat was not required."
