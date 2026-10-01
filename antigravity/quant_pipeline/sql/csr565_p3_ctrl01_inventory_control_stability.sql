-- CSR-565 / P3-CTRL-01 — Inventory Control Stability Acceptance Gate
-- OBSERVE only · WIRE=NO · ENFORCE=0 · Economic PASS=NO from this SQL alone
--
-- Primary horizon FIXED = 30s (CTRL design). Comparing 10/20/30/45/60 is allowed
-- for robustness, but FORBIDDEN to adopt argmax(Δ) after peeking (multiple testing).
--
-- Expected source: mm_trade_cf_log (or CTRL observe export) with:
--   trade_id, ts, side, hold_sec, hold_ladder, actual_pnl_bp,
--   cf_10s_bp, cf_20s_bp, cf_30s_bp, cf_45s_bp, cf_60s_bp,
--   mae_bp, hard_stop, regime, hour_jst
--
-- Δbp := Virtual@H − Actual  on the SAME eligible trade_id set.

-- ---------------------------------------------------------------------------
-- 0. Eligibility: inventory events with hold > horizon (would-unwind population)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr565_ctrl_eligible AS
SELECT
  trade_id,
  ts,
  side,
  hold_sec,
  hold_ladder,
  actual_pnl_bp,
  cf_10s_bp,
  cf_20s_bp,
  cf_30s_bp,
  cf_45s_bp,
  cf_60s_bp,
  mae_bp,
  hard_stop,
  regime,
  date_trunc('hour', ts) AS hour_jst
FROM mm_trade_cf_log
WHERE actual_pnl_bp IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 1. Horizon unpivot (research board — primary marker on 30s)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr565_ctrl_horizon_unpivot AS
SELECT trade_id, ts, side, hold_ladder, regime, hour_jst, actual_pnl_bp,
       '10s' AS horizon, 10 AS horizon_sec, cf_10s_bp AS virtual_bp,
       (cf_10s_bp - actual_pnl_bp) AS delta_bp,
       FALSE AS is_primary
FROM csr565_ctrl_eligible
WHERE hold_sec > 10 AND cf_10s_bp IS NOT NULL
UNION ALL
SELECT trade_id, ts, side, hold_ladder, regime, hour_jst, actual_pnl_bp,
       '20s', 20, cf_20s_bp, (cf_20s_bp - actual_pnl_bp), FALSE
FROM csr565_ctrl_eligible
WHERE hold_sec > 20 AND cf_20s_bp IS NOT NULL
UNION ALL
SELECT trade_id, ts, side, hold_ladder, regime, hour_jst, actual_pnl_bp,
       '30s', 30, cf_30s_bp, (cf_30s_bp - actual_pnl_bp), TRUE
FROM csr565_ctrl_eligible
WHERE hold_sec > 30 AND cf_30s_bp IS NOT NULL
UNION ALL
SELECT trade_id, ts, side, hold_ladder, regime, hour_jst, actual_pnl_bp,
       '45s', 45, cf_45s_bp, (cf_45s_bp - actual_pnl_bp), FALSE
FROM csr565_ctrl_eligible
WHERE hold_sec > 45 AND cf_45s_bp IS NOT NULL
UNION ALL
SELECT trade_id, ts, side, hold_ladder, regime, hour_jst, actual_pnl_bp,
       '60s', 60, cf_60s_bp, (cf_60s_bp - actual_pnl_bp), FALSE
FROM csr565_ctrl_eligible
WHERE hold_sec > 60 AND cf_60s_bp IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. Gate scorecard by horizon (report all; decide only on primary=30s)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr565_p3_ctrl01_horizon_scorecard AS
SELECT
  horizon,
  horizon_sec,
  is_primary,
  count(*) AS n,
  round(avg(delta_bp), 4) AS mean_delta_bp,
  round(median(delta_bp), 4) AS median_delta_bp,
  round(sum(delta_bp), 4) AS sum_delta_bp,
  round(avg(CASE WHEN delta_bp > 0 THEN 1.0 ELSE 0.0 END), 4) AS improve_rate,
  round(quantile_cont(delta_bp, 0.05), 4) AS worst_5pct_delta_bp,
  round(avg(CASE WHEN hard_stop THEN 1.0 ELSE 0.0 END), 4) AS hs_rate
FROM csr565_ctrl_horizon_unpivot h
LEFT JOIN csr565_ctrl_eligible e USING (trade_id)
GROUP BY 1, 2, 3
ORDER BY horizon_sec;

-- ---------------------------------------------------------------------------
-- 3. Primary=30s stability: hour × regime × ladder × side
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr565_p3_ctrl01_primary30_slices AS
SELECT
  hour_jst,
  coalesce(regime, 'UNKNOWN') AS regime,
  coalesce(hold_ladder, 'UNK') AS hold_ladder,
  coalesce(side, 'UNK') AS side,
  count(*) AS n,
  round(avg(delta_bp), 4) AS mean_delta_bp,
  round(median(delta_bp), 4) AS median_delta_bp,
  round(sum(delta_bp), 4) AS sum_delta_bp,
  round(avg(CASE WHEN delta_bp > 0 THEN 1.0 ELSE 0.0 END), 4) AS improve_rate,
  round(quantile_cont(delta_bp, 0.05), 4) AS worst_5pct_delta_bp
FROM csr565_ctrl_horizon_unpivot
WHERE is_primary = TRUE
GROUP BY 1, 2, 3, 4
ORDER BY 1, 2, 3, 4;

-- Hourly sign stability (mirrors 15–22 Discord CTRL windows)
CREATE OR REPLACE VIEW csr565_p3_ctrl01_primary30_hourly AS
SELECT
  hour_jst,
  count(*) AS n,
  round(avg(delta_bp), 4) AS mean_delta_bp,
  round(median(delta_bp), 4) AS median_delta_bp,
  round(sum(delta_bp), 4) AS sum_delta_bp,
  CASE WHEN avg(delta_bp) > 0 THEN 1 ELSE 0 END AS hour_positive
FROM csr565_ctrl_horizon_unpivot
WHERE is_primary = TRUE
GROUP BY 1
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 4. P1 UMM attribution stub (entry vs holding)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr565_umm_inventory_attribution AS
SELECT
  hold_ladder,
  side,
  CASE
    WHEN hold_sec < 10 THEN 'H0_<10'
    WHEN hold_sec < 30 THEN 'H1_10_30'
    WHEN hold_sec < 60 THEN 'H2_30_60'
    WHEN hold_sec < 120 THEN 'H3_60_120'
    ELSE 'H4_120p'
  END AS age_bucket,
  count(*) AS n,
  round(avg(actual_pnl_bp), 4) AS mean_actual_bp,
  round(avg(CASE WHEN actual_pnl_bp > 0 THEN 1.0 ELSE 0.0 END), 4) AS win_rate,
  round(avg(CASE WHEN actual_pnl_bp > 0 THEN actual_pnl_bp END), 4) AS avg_win_bp,
  round(avg(CASE WHEN actual_pnl_bp <= 0 THEN actual_pnl_bp END), 4) AS avg_loss_bp,
  round(quantile_cont(actual_pnl_bp, 0.05), 4) AS worst_5pct_bp,
  round(avg(CASE WHEN hard_stop THEN 1.0 ELSE 0.0 END), 4) AS hs_rate,
  round(avg(mae_bp), 4) AS mean_mae_bp
FROM csr565_ctrl_eligible
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- ---------------------------------------------------------------------------
-- 5. Acceptance Gate rollup (PASS criteria — research only, not ENFORCE)
-- ---------------------------------------------------------------------------
-- Manual / scripted check against:
--   n_primary30 >= 1000
--   distinct hour_jst >= 3
--   distinct regime >= 2 (when regime populated)
--   primary30 mean_delta_bp and hour_positive rate reported
--   secondary horizons reported but NOT used for adopt
-- FORBIDDEN: SELECT horizon ORDER BY mean_delta_bp DESC LIMIT 1 → adopt

CREATE OR REPLACE VIEW csr565_p3_ctrl01_gate_status AS
WITH p AS (
  SELECT * FROM csr565_p3_ctrl01_horizon_scorecard WHERE is_primary = TRUE
),
h AS (
  SELECT
    count(*) AS n_hours,
    sum(hour_positive) AS hours_positive,
    count(DISTINCT hour_jst) AS distinct_hours
  FROM csr565_p3_ctrl01_primary30_hourly
),
r AS (
  SELECT count(DISTINCT coalesce(regime, 'UNKNOWN')) AS distinct_regimes
  FROM csr565_ctrl_horizon_unpivot
  WHERE is_primary = TRUE
)
SELECT
  p.n AS n_inventory_events,
  h.distinct_hours,
  h.hours_positive,
  h.n_hours,
  r.distinct_regimes,
  p.mean_delta_bp AS primary30_mean_delta_bp,
  p.median_delta_bp AS primary30_median_delta_bp,
  p.worst_5pct_delta_bp,
  p.hs_rate,
  CASE
    WHEN p.n IS NULL OR p.n < 1000 THEN 'FAIL_N'
    WHEN h.distinct_hours < 3 THEN 'FAIL_HOURS'
    WHEN r.distinct_regimes < 2 THEN 'FAIL_REGIME_OR_UNKNOWN'
    ELSE 'PASS_COLLECT'  -- research collect OK · NOT Economic PASS · NOT ENFORCE
  END AS acceptance_gate,
  FALSE AS economic_pass_allowed,
  FALSE AS enforce_allowed,
  'primary_horizon_fixed_30s' AS primary_rule,
  'do_not_argmax_horizons' AS multiple_testing_ban
FROM p, h, r;
