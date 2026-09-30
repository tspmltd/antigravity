-- CSR-562 / PDCA3.0 P0-A — Conditional Hazard Exit CF (CSR-521)
-- Population: L2+L3 ONLY · L0 KEEP · L1 research · L4 separate
-- NO fixed 10s/30s adopt · primary research horizons 20s/30s
-- Condition axes: ladder × side × mae_bucket × hold_age × regime
-- WIRE=NO · ENFORCE=0 · Economic PASS=NO until gates below

-- mae_bucket: mild (>-5bp) | mid (-5..-15) | tail (≤-15bp)  [MAE more negative = worse]
CREATE OR REPLACE VIEW csr562_l23_base AS
SELECT
  trade_id,
  strategy_id,
  lower(side) AS side,
  ladder,
  hold_sec,
  regime,
  hour_jst,
  hard_stop,
  mae_bp,
  pnl_actual_bp,
  pnl_cf_10s_bp,
  pnl_cf_20s_bp,
  pnl_cf_30s_bp,
  pnl_cf_45s_bp,
  pnl_cf_60s_bp,
  CASE
    WHEN mae_bp IS NULL THEN 'unknown'
    WHEN mae_bp > -5.0 THEN 'mild'
    WHEN mae_bp > -15.0 THEN 'mid'
    ELSE 'tail'
  END AS mae_bucket,
  CASE
    WHEN hold_sec IS NULL THEN 'unknown'
    WHEN hold_sec < 10 THEN 'h0_10'
    WHEN hold_sec < 20 THEN 'h10_20'
    WHEN hold_sec < 30 THEN 'h20_30'
    WHEN hold_sec < 60 THEN 'h30_60'
    ELSE 'h60p'
  END AS hold_bucket
FROM mm_trade_cf_log
WHERE ladder IN ('L2', 'L3');

-- Primary: L2/L3 × horizon scorecard
CREATE OR REPLACE VIEW csr562_p0a_horizon AS
SELECT
  horizon,
  count(*) AS n,
  avg(delta_bp) AS mean_delta_bp,
  median(delta_bp) AS median_delta_bp,
  avg(CASE WHEN delta_bp > 0 THEN 1.0 ELSE 0.0 END) AS cf_win_rate,
  quantile_cont(delta_bp, 0.05) AS p05_delta_bp,
  avg(CASE WHEN hard_stop THEN 1.0 ELSE 0.0 END) AS hs_rate,
  avg(CASE WHEN mae_bp <= -15.0 THEN 1.0 ELSE 0.0 END) AS tail15_rate
FROM (
  SELECT '10s' AS horizon, hard_stop, mae_bp, pnl_actual_bp,
         (pnl_cf_10s_bp - pnl_actual_bp) AS delta_bp
  FROM csr562_l23_base WHERE pnl_cf_10s_bp IS NOT NULL
  UNION ALL
  SELECT '20s', hard_stop, mae_bp, pnl_actual_bp, (pnl_cf_20s_bp - pnl_actual_bp)
  FROM csr562_l23_base WHERE pnl_cf_20s_bp IS NOT NULL
  UNION ALL
  SELECT '30s', hard_stop, mae_bp, pnl_actual_bp, (pnl_cf_30s_bp - pnl_actual_bp)
  FROM csr562_l23_base WHERE pnl_cf_30s_bp IS NOT NULL
) u
GROUP BY 1
ORDER BY CASE horizon WHEN '10s' THEN 1 WHEN '20s' THEN 2 WHEN '30s' THEN 3 END;

-- L2/L3 × side × @30s (short inventory suspect)
CREATE OR REPLACE VIEW csr562_p0a_side_d30 AS
SELECT
  side,
  count(*) AS n,
  avg(pnl_cf_30s_bp - pnl_actual_bp) AS mean_d30,
  median(pnl_cf_30s_bp - pnl_actual_bp) AS median_d30,
  avg(CASE WHEN (pnl_cf_30s_bp - pnl_actual_bp) > 0 THEN 1.0 ELSE 0.0 END) AS win_d30,
  avg(CASE WHEN hard_stop THEN 1.0 ELSE 0.0 END) AS hs_rate,
  avg(CASE WHEN mae_bp <= -15.0 THEN 1.0 ELSE 0.0 END) AS tail15_rate
FROM csr562_l23_base
WHERE pnl_cf_30s_bp IS NOT NULL
GROUP BY 1;

-- Conditional: mae_bucket × hold_bucket × @20/@30 (NOT blanket age cut)
CREATE OR REPLACE VIEW csr562_p0a_conditional_matrix AS
SELECT
  mae_bucket,
  hold_bucket,
  count(*) AS n,
  avg(pnl_cf_20s_bp - pnl_actual_bp) AS mean_d20,
  avg(pnl_cf_30s_bp - pnl_actual_bp) AS mean_d30,
  median(pnl_cf_30s_bp - pnl_actual_bp) AS median_d30,
  avg(CASE WHEN (pnl_cf_30s_bp - pnl_actual_bp) > 0 THEN 1.0 ELSE 0.0 END) AS win_d30,
  avg(CASE WHEN hard_stop THEN 1.0 ELSE 0.0 END) AS hs_rate
FROM csr562_l23_base
WHERE pnl_cf_20s_bp IS NOT NULL AND pnl_cf_30s_bp IS NOT NULL
GROUP BY 1, 2
ORDER BY 1, 2;

-- Regime stability for PASS
CREATE OR REPLACE VIEW csr562_p0a_regime_d30 AS
SELECT
  coalesce(regime, 'unknown') AS regime,
  count(*) AS n,
  avg(pnl_cf_30s_bp - pnl_actual_bp) AS mean_d30,
  median(pnl_cf_30s_bp - pnl_actual_bp) AS median_d30,
  avg(CASE WHEN (pnl_cf_30s_bp - pnl_actual_bp) > 0 THEN 1.0 ELSE 0.0 END) AS win_d30
FROM csr562_l23_base
WHERE pnl_cf_30s_bp IS NOT NULL
GROUP BY 1;

-- L0 protect audit (must remain healthy — do not unwind)
CREATE OR REPLACE VIEW csr562_p0a_l0_protect AS
SELECT
  count(*) AS n_l0,
  avg(pnl_actual_bp) AS mean_actual,
  avg(CASE WHEN pnl_actual_bp > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
  avg(CASE WHEN hard_stop THEN 1.0 ELSE 0.0 END) AS hs_rate
FROM mm_trade_cf_log
WHERE ladder = 'L0';

-- PASS gate snapshot (research — NOT Economic PASS / NOT ENFORCE)
-- Require ALL: median_d30>0 · win_d30>=0.60 · ≥2 regimes median>0 · L0 mean not destroyed
CREATE OR REPLACE VIEW csr562_p0a_pass_gate AS
SELECT
  (SELECT median_delta_bp FROM csr562_p0a_horizon WHERE horizon = '30s') AS median_d30,
  (SELECT cf_win_rate FROM csr562_p0a_horizon WHERE horizon = '30s') AS win_d30,
  (SELECT mean_delta_bp FROM csr562_p0a_horizon WHERE horizon = '20s') AS mean_d20,
  (SELECT mean_delta_bp FROM csr562_p0a_horizon WHERE horizon = '10s') AS mean_d10,
  (SELECT count(*) FROM csr562_p0a_regime_d30 WHERE median_d30 > 0 AND n >= 5) AS regimes_ok,
  (SELECT count(*) FROM csr562_p0a_regime_d30 WHERE n >= 5) AS regimes_eligible,
  (SELECT mean_actual FROM csr562_p0a_l0_protect) AS l0_mean_actual,
  (SELECT tail15_rate FROM csr562_p0a_horizon WHERE horizon = '30s') AS cf_cohort_tail15;
