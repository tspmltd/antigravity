-- CSR-562 / PDCA3.0 P0-B — PEG A/B/C/D orthogonal decomposition + AgingGuard CF
-- DO NOT change Aging 3s / BE3 / FakeLiq thresholds
-- Capture Rate: EXCLUDED from decision (MASKED / CSR-545)
-- AE improve ≠ Economic PASS
-- WIRE=NO · ENFORCE=0 · LIVE=OFF
--
-- Expected relations (or compatible views):
--   peg_variant_trade_log:
--     trade_id, variant ('A'|'B'|'C'|'D'), pnl_bp, ae_1s, ae_3s, exit_reason,
--     hold_sec, regime, hour_jst, mfe_bp, mae_bp
--   peg_aging_exit_cf_log: (CSR-545 schema)
--     trade_id, actual_pnl_bp, cf_5s_bp, cf_10s_bp, cf_20s_bp, cf_30s_bp,
--     exit_reason, aging_trigger_ts, stats_window_id, process_start_ts,
--     aging_exits_window, aging_exits_lifetime, state_reset_count

-- A/B/C/D 24h EV summary
CREATE OR REPLACE VIEW csr562_p0b_abcd_summary AS
SELECT
  variant,
  count(*) AS n,
  avg(pnl_bp) AS mean_pnl_bp,
  sum(pnl_bp) AS sum_pnl_bp,
  avg(CASE WHEN pnl_bp > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
  avg(ae_1s) AS mean_ae_1s,
  avg(ae_3s) AS mean_ae_3s,
  avg(hold_sec) AS mean_hold_sec,
  avg(CASE WHEN exit_reason = 'AGING_GUARD' THEN 1.0 ELSE 0.0 END) AS aging_exit_rate,
  avg(CASE WHEN exit_reason = 'BE3' THEN 1.0 ELSE 0.0 END) AS be3_exit_rate,
  avg(CASE WHEN exit_reason = 'FAKE_LIQ_SKIP' THEN 1.0 ELSE 0.0 END) AS fake_skip_rate
FROM peg_variant_trade_log
WHERE variant IN ('A', 'B', 'C', 'D')
GROUP BY 1
ORDER BY variant;

-- Incremental value isolation
-- B−A = Maker Entry pure value
-- C−B = AgingGuard incremental
-- D−C = BE3+FakeLiq+Adaptive residual
CREATE OR REPLACE VIEW csr562_p0b_abcd_deltas AS
WITH s AS (SELECT * FROM csr562_p0b_abcd_summary)
SELECT
  (SELECT mean_pnl_bp FROM s WHERE variant = 'B')
    - (SELECT mean_pnl_bp FROM s WHERE variant = 'A') AS delta_entry_only_vs_base,
  (SELECT sum_pnl_bp FROM s WHERE variant = 'B')
    - (SELECT sum_pnl_bp FROM s WHERE variant = 'A') AS sum_delta_entry_only_vs_base,
  (SELECT mean_pnl_bp FROM s WHERE variant = 'C')
    - (SELECT mean_pnl_bp FROM s WHERE variant = 'B') AS delta_aging_vs_entry,
  (SELECT sum_pnl_bp FROM s WHERE variant = 'C')
    - (SELECT sum_pnl_bp FROM s WHERE variant = 'B') AS sum_delta_aging_vs_entry,
  (SELECT mean_pnl_bp FROM s WHERE variant = 'D')
    - (SELECT mean_pnl_bp FROM s WHERE variant = 'C') AS delta_full_residual,
  (SELECT mean_ae_3s FROM s WHERE variant = 'B')
    - (SELECT mean_ae_3s FROM s WHERE variant = 'A') AS delta_ae3_entry_vs_base;

-- AgingGuard CF value: actual_exit − hold_cf (negative Guard Value = Guard hurt)
CREATE OR REPLACE VIEW csr562_p0b_aging_cf_value AS
SELECT
  horizon,
  count(*) AS n,
  avg(guard_value_bp) AS mean_guard_value_bp,
  median(guard_value_bp) AS median_guard_value_bp,
  avg(CASE WHEN guard_value_bp > 0 THEN 1.0 ELSE 0.0 END) AS guard_win_rate,
  avg(actual_pnl_bp) AS mean_actual,
  avg(cf_pnl_bp) AS mean_cf_hold
FROM (
  SELECT '5s' AS horizon, actual_pnl_bp, cf_5s_bp AS cf_pnl_bp,
         (actual_pnl_bp - cf_5s_bp) AS guard_value_bp
  FROM peg_aging_exit_cf_log WHERE cf_5s_bp IS NOT NULL AND exit_reason = 'AGING_GUARD'
  UNION ALL
  SELECT '10s', actual_pnl_bp, cf_10s_bp, (actual_pnl_bp - cf_10s_bp)
  FROM peg_aging_exit_cf_log WHERE cf_10s_bp IS NOT NULL AND exit_reason = 'AGING_GUARD'
  UNION ALL
  SELECT '20s', actual_pnl_bp, cf_20s_bp, (actual_pnl_bp - cf_20s_bp)
  FROM peg_aging_exit_cf_log WHERE cf_20s_bp IS NOT NULL AND exit_reason = 'AGING_GUARD'
  UNION ALL
  SELECT '30s', actual_pnl_bp, cf_30s_bp, (actual_pnl_bp - cf_30s_bp)
  FROM peg_aging_exit_cf_log WHERE cf_30s_bp IS NOT NULL AND exit_reason = 'AGING_GUARD'
) t
GROUP BY 1
ORDER BY CASE horizon WHEN '5s' THEN 1 WHEN '10s' THEN 2 WHEN '20s' THEN 3 WHEN '30s' THEN 4 END;

-- Counter provenance audit (99→0 discontinuity)
CREATE OR REPLACE VIEW csr562_p0b_aging_provenance AS
SELECT
  stats_window_id,
  process_start_ts,
  min(aging_trigger_ts) AS window_first_exit,
  max(aging_trigger_ts) AS window_last_exit,
  count(*) AS exits_in_rows,
  max(aging_exits_window) AS max_exits_window,
  max(aging_exits_lifetime) AS max_exits_lifetime,
  max(state_reset_count) AS max_reset_count
FROM peg_aging_exit_cf_log
WHERE exit_reason = 'AGING_GUARD'
GROUP BY 1, 2
ORDER BY process_start_ts;

-- Done criteria helpers
-- Entry-only Done: sum_delta_entry_only_vs_base > 0 AND n_B,n_A sufficient
-- Aging convict: mean_guard_value @10s and @30s both < 0 with n>=20
CREATE OR REPLACE VIEW csr562_p0b_done_gate AS
SELECT
  (SELECT sum_delta_entry_only_vs_base FROM csr562_p0b_abcd_deltas) AS entry_sum_delta_vs_base,
  (SELECT delta_entry_only_vs_base FROM csr562_p0b_abcd_deltas) AS entry_mean_delta_vs_base,
  (SELECT delta_aging_vs_entry FROM csr562_p0b_abcd_deltas) AS aging_mean_delta_vs_entry,
  (SELECT mean_guard_value_bp FROM csr562_p0b_aging_cf_value WHERE horizon = '10s') AS aging_gv_10s,
  (SELECT mean_guard_value_bp FROM csr562_p0b_aging_cf_value WHERE horizon = '30s') AS aging_gv_30s,
  (SELECT n FROM csr562_p0b_aging_cf_value WHERE horizon = '10s') AS aging_n_10s,
  (SELECT n FROM csr562_p0b_abcd_summary WHERE variant = 'B') AS n_entry_only,
  (SELECT n FROM csr562_p0b_abcd_summary WHERE variant = 'A') AS n_baseline;
