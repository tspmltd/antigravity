# TF2B_DeSeva_v1 Dry-Run Directive

**Model:** `TF2B_DeSeva_v1`  
**Status:** LOCK · WIRE=NO · ENFORCE=0 · DRYRUN only  
**Stack:** TF2BP baseline signal/exit × SEVA `entry_allowed` filter

## Hypothesis

SEVA が高評価した局面だけ TF2BP を執行すると、Baseline に対して **Sharpe↑ / Max Drawdown 改善**する。

## Lanes

| Lane | Entry | Exit |
|------|-------|------|
| `TF2BP_CTRL` | TF2BP raw | TF2BP frozen exit |
| `TF2B_DeSeva_v1` | TF2BP ∧ SEVA.TF2BP.`entry_allowed` | same TF2BP exit |

## Primary KPIs (vs CTRL)

- ΔSharpe (1h / 24h / all)
- ΔMaxDD_bp (higher = less drawdown)
- ΔPnL / ΔEV (secondary)
- SEVA allow_rate

## Ops

- Start: `scripts/start-dryrun-tf2b-deseva-v1.sh`
- Stop: `scripts/stop-dryrun-tf2b-deseva-v1.sh`
- State: `data/dryrun_tf2b_deseva_v1_state.json`
- Artifacts: `data/mm_research/tf2b_deseva_v1_dryrun/`
- Discord: `post_tf2b_deseva_dryrun` + hourly integrated reporter

## Forbidden

- LIVE/PAPER touch
- TF2BP pin retune
- SEVA model retune mid-run
- Auto promote without 24h Sharpe/DD proof
