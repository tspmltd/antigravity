-- CSR-555 — Hazard calibration support queries (on inventory snapshots)
-- Teacher: exit_reason_final == 'HARD_STOP' (never pnl ≤ -4.8 inference)
-- Latency = CONDITION only

-- Phase1 bucket: Ladder × MAE band × Adverse → forward HS rate vs Hold EV vs CF-Unwind EV
WITH snap AS (
  SELECT
    *,
    CASE
      WHEN mae_bp IS NULL THEN 'UNKNOWN'
      WHEN mae_bp > -1.0 THEN 'GT_M1'
      WHEN mae_bp > -2.0 THEN 'M1_M2'
      ELSE 'LE_M2'
    END AS mae_band,
    CASE
      WHEN adverse_valid = FALSE OR adverse_score IS NULL THEN 'UNKNOWN'
      WHEN adverse_score < 40 THEN 'LOW'
      WHEN adverse_score < 60 THEN 'MID'
      ELSE 'HIGH'
    END AS adverse_slice
  FROM mm_inventory_hazard_snapshot
)
SELECT
  hold_ladder,
  mae_band,
  adverse_slice,
  COUNT(*) AS n,
  ROUND(AVG(CASE WHEN hazard_label = 1 THEN 1.0 ELSE 0.0 END) * 100, 1) AS forward_hs_rate_pct,
  ROUND(AVG(CASE WHEN hs_within_30s = 1 THEN 1.0 ELSE 0.0 END) * 100, 1) AS hs_within_30s_pct,
  ROUND(AVG(current_pnl_bp), 3) AS hold_mark_ev,  -- path mark; not final Actual rewrite
  ROUND(AVG(cf_unwind_now_bp), 3) AS cf_unwind_ev,
  ROUND(AVG(cf_unwind_now_bp - current_pnl_bp), 3) AS cf_vs_hold_mark_delta
FROM snap
GROUP BY 1, 2, 3
HAVING COUNT(*) >= 20
ORDER BY 1, 2, 3;

-- Threshold sweep skeleton (requires precomputed hazard_score on snapshots)
-- SELECT threshold, trigger_n, hs_avoided, false_cut, cf_delta_bp, ev_per_trigger ...
-- filled by Python calibration runner after score join.
