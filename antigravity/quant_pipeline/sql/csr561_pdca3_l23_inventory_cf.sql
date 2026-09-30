-- CSR-561 / PDCA3 — CSR-521 L2/L3 Inventory Control CF scorecard
-- WIRE=NO · ENFORCE=0 · Economic PASS=NO · NO fixed 10s/30s adoption
-- Primary research horizon: 20s / 30s (10s comparative only)
-- Population: L2+L3 only for primary PASS; L0 KEEP (do not cut)
--
-- Expected relation mm_trade_cf_log or compatible view with:
--   trade_id, strategy_id, side, ladder (L0..L4), hold_sec,
--   pnl_actual_bp, pnl_cf_10s_bp, pnl_cf_20s_bp, pnl_cf_30s_bp,
--   pnl_cf_45s_bp, pnl_cf_60s_bp, hard_stop, mae_bp, regime, hour_jst

CREATE OR REPLACE VIEW csr561_l23_cf_by_horizon AS
SELECT
  horizon,
  count(*) AS n,
  avg(delta_bp) AS mean_delta_bp,
  median(delta_bp) AS median_delta_bp,
  avg(CASE WHEN delta_bp > 0 THEN 1.0 ELSE 0.0 END) AS cf_win_rate,
  quantile_cont(delta_bp, 0.05) AS p05_delta_bp,
  quantile_cont(delta_bp, 0.95) AS p95_delta_bp,
  avg(pnl_actual_bp) AS mean_actual_bp,
  avg(pnl_virtual_bp) AS mean_virtual_bp
FROM (
  SELECT trade_id, '10s' AS horizon, pnl_actual_bp,
         pnl_cf_10s_bp AS pnl_virtual_bp,
         (pnl_cf_10s_bp - pnl_actual_bp) AS delta_bp
  FROM mm_trade_cf_log WHERE ladder IN ('L2', 'L3') AND pnl_cf_10s_bp IS NOT NULL
  UNION ALL
  SELECT trade_id, '20s', pnl_actual_bp, pnl_cf_20s_bp, (pnl_cf_20s_bp - pnl_actual_bp)
  FROM mm_trade_cf_log WHERE ladder IN ('L2', 'L3') AND pnl_cf_20s_bp IS NOT NULL
  UNION ALL
  SELECT trade_id, '30s', pnl_actual_bp, pnl_cf_30s_bp, (pnl_cf_30s_bp - pnl_actual_bp)
  FROM mm_trade_cf_log WHERE ladder IN ('L2', 'L3') AND pnl_cf_30s_bp IS NOT NULL
  UNION ALL
  SELECT trade_id, '45s', pnl_actual_bp, pnl_cf_45s_bp, (pnl_cf_45s_bp - pnl_actual_bp)
  FROM mm_trade_cf_log WHERE ladder IN ('L2', 'L3') AND pnl_cf_45s_bp IS NOT NULL
  UNION ALL
  SELECT trade_id, '60s', pnl_actual_bp, pnl_cf_60s_bp, (pnl_cf_60s_bp - pnl_actual_bp)
  FROM mm_trade_cf_log WHERE ladder IN ('L2', 'L3') AND pnl_cf_60s_bp IS NOT NULL
) t
GROUP BY 1
ORDER BY CASE horizon WHEN '10s' THEN 1 WHEN '20s' THEN 2 WHEN '30s' THEN 3
                      WHEN '45s' THEN 4 WHEN '60s' THEN 5 END;

-- L2/L3 × side
CREATE OR REPLACE VIEW csr561_l23_cf30_by_side AS
SELECT
  lower(side) AS side,
  count(*) AS n,
  avg(pnl_cf_30s_bp - pnl_actual_bp) AS mean_d30,
  median(pnl_cf_30s_bp - pnl_actual_bp) AS median_d30,
  avg(CASE WHEN (pnl_cf_30s_bp - pnl_actual_bp) > 0 THEN 1.0 ELSE 0.0 END) AS win_rate_d30,
  avg(CASE WHEN hard_stop THEN 1.0 ELSE 0.0 END) AS hs_rate
FROM mm_trade_cf_log
WHERE ladder IN ('L2', 'L3') AND pnl_cf_30s_bp IS NOT NULL
GROUP BY 1;

-- Hourly cohort sign stability (for Discord CTRL windows join)
CREATE OR REPLACE VIEW csr561_l23_cf30_by_hour AS
SELECT
  hour_jst,
  count(*) AS n,
  avg(pnl_cf_30s_bp - pnl_actual_bp) AS mean_d30,
  median(pnl_cf_30s_bp - pnl_actual_bp) AS median_d30,
  avg(CASE WHEN (pnl_cf_30s_bp - pnl_actual_bp) > 0 THEN 1.0 ELSE 0.0 END) AS win_rate_d30
FROM mm_trade_cf_log
WHERE ladder IN ('L2', 'L3') AND pnl_cf_30s_bp IS NOT NULL
GROUP BY 1
ORDER BY 1;

-- L0 must remain positive mass (do not unwind)
CREATE OR REPLACE VIEW csr561_l0_protect_audit AS
SELECT
  count(*) AS n_l0,
  avg(pnl_actual_bp) AS mean_actual_bp,
  avg(CASE WHEN pnl_actual_bp > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
  avg(CASE WHEN hard_stop THEN 1.0 ELSE 0.0 END) AS hs_rate
FROM mm_trade_cf_log
WHERE ladder = 'L0';

-- PASS gate helper (research only — not Economic PASS)
-- Require: median_d30>0 AND cf_win_rate>=0.60 AND multi-hour sign stability
CREATE OR REPLACE VIEW csr561_pass_gate_snapshot AS
SELECT
  (SELECT median_delta_bp FROM csr561_l23_cf_by_horizon WHERE horizon = '30s') AS median_d30,
  (SELECT cf_win_rate FROM csr561_l23_cf_by_horizon WHERE horizon = '30s') AS win_rate_d30,
  (SELECT mean_delta_bp FROM csr561_l23_cf_by_horizon WHERE horizon = '30s') AS mean_d30,
  (SELECT mean_delta_bp FROM csr561_l23_cf_by_horizon WHERE horizon = '10s') AS mean_d10,
  (SELECT count(*) FROM csr561_l23_cf30_by_hour WHERE mean_d30 > 0) AS hours_positive_d30,
  (SELECT count(*) FROM csr561_l23_cf30_by_hour) AS hours_total,
  (SELECT mean_actual_bp FROM csr561_l0_protect_audit) AS l0_mean_actual;
