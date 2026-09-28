-- CSR-554 / CSR-521 — Conditional Unwind CF DuckDB queries
-- RULE: CF Δ = Virtual − Actual · NEVER rewrite Actual · NEVER report Actual+CF as realized
-- Latency = CONDITION (GROUP BY only) · not a PnL debit
-- economic_pass=NO · enforce=0 · wire=NO · auto_apply=OFF

-- ---------------------------------------------------------------------------
-- 0. Schema note (proposed ADD-ON for CSR-521 research; not legacy required cols)
-- Table/view: mm_trade_cf_log
-- ---------------------------------------------------------------------------

-- 2. CF basic aggregate (blanket horizons + CONDITIONAL)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_csr521_cf_policy_unpivot AS
WITH base AS (
  SELECT
    trade_id,
    actual_pnl_bp,
    cf_10s_bp, cf_20s_bp, cf_30s_bp, cf_45s_bp,
    cf_60s_bp, cf_90s_bp, cf_120s_bp,
    cf_conditional_bp
  FROM mm_trade_cf_log
),
unpivoted AS (
  SELECT trade_id, actual_pnl_bp, '10s' AS policy, cf_10s_bp AS cf_pnl_bp FROM base
  UNION ALL SELECT trade_id, actual_pnl_bp, '20s', cf_20s_bp FROM base
  UNION ALL SELECT trade_id, actual_pnl_bp, '30s', cf_30s_bp FROM base
  UNION ALL SELECT trade_id, actual_pnl_bp, '45s', cf_45s_bp FROM base
  UNION ALL SELECT trade_id, actual_pnl_bp, '60s', cf_60s_bp FROM base
  UNION ALL SELECT trade_id, actual_pnl_bp, '90s', cf_90s_bp FROM base
  UNION ALL SELECT trade_id, actual_pnl_bp, '120s', cf_120s_bp FROM base
  UNION ALL SELECT trade_id, actual_pnl_bp, 'CONDITIONAL', cf_conditional_bp FROM base
)
SELECT * FROM unpivoted;

-- Query: Q2 policy scorecard
SELECT
  policy,
  COUNT(*) AS n,
  ROUND(SUM(actual_pnl_bp), 2) AS actual_bp,
  ROUND(SUM(cf_pnl_bp), 2) AS virtual_bp,
  ROUND(SUM(cf_pnl_bp) - SUM(actual_pnl_bp), 2) AS delta_bp,
  ROUND(AVG(cf_pnl_bp - actual_pnl_bp), 3) AS delta_per_trade_bp,
  ROUND(AVG(CASE WHEN cf_pnl_bp > actual_pnl_bp THEN 1.0 ELSE 0.0 END) * 100, 1)
    AS improve_rate_pct
FROM v_csr521_cf_policy_unpivot
WHERE cf_pnl_bp IS NOT NULL
GROUP BY policy
ORDER BY delta_bp DESC;

-- ---------------------------------------------------------------------------
-- 3. Conditional Unwind P0 body: Ladder × Adverse
-- ---------------------------------------------------------------------------
SELECT
  hold_ladder,
  CASE
    WHEN adverse_score IS NULL THEN 'UNKNOWN'
    WHEN adverse_score < 40 THEN 'LOW'
    WHEN adverse_score < 60 THEN 'MID'
    ELSE 'HIGH'
  END AS adverse_slice,
  COUNT(*) AS n,
  ROUND(AVG(hold_time_s), 1) AS hold_avg_s,
  ROUND(AVG(mae_bp), 3) AS avg_mae_bp,
  ROUND(AVG(mfe_bp), 3) AS avg_mfe_bp,
  ROUND(SUM(actual_pnl_bp), 2) AS actual_bp,
  ROUND(SUM(cf_conditional_bp), 2) AS conditional_bp,
  ROUND(SUM(cf_conditional_bp) - SUM(actual_pnl_bp), 2) AS conditional_delta_bp,
  ROUND(AVG(cf_conditional_bp - actual_pnl_bp), 3) AS delta_per_trade,
  ROUND(AVG(CASE WHEN actual_exit_reason = 'HARD_STOP' THEN 1.0 ELSE 0.0 END) * 100, 1)
    AS hs_rate_pct
FROM mm_trade_cf_log
WHERE conditional_trigger = TRUE
GROUP BY 1, 2
ORDER BY 1, 2;

-- ---------------------------------------------------------------------------
-- 4. Adverse × Latency × Inventory (Latency = CONDITION only)
-- ---------------------------------------------------------------------------
WITH sliced AS (
  SELECT
    *,
    CASE
      WHEN hold_time_s < 10 THEN 'L0'
      WHEN hold_time_s < 30 THEN 'L1'
      WHEN hold_time_s < 60 THEN 'L2'
      WHEN hold_time_s < 120 THEN 'L3'
      ELSE 'L4'
    END AS ladder,
    CASE
      WHEN adverse_score IS NULL THEN 'UNKNOWN'
      WHEN adverse_score < 40 THEN 'LOW'
      WHEN adverse_score < 60 THEN 'MID'
      ELSE 'HIGH'
    END AS adverse_slice,
    CASE
      WHEN latency_ms IS NULL THEN 'UNKNOWN'
      WHEN latency_ms < 40 THEN 'LT_40'
      WHEN latency_ms < 80 THEN '40_80'
      WHEN latency_ms < 150 THEN '80_150'
      WHEN latency_ms < 250 THEN '150_250'
      ELSE 'GT_250'
    END AS latency_slice
  FROM mm_trade_cf_log
)
SELECT
  latency_slice,
  adverse_slice,
  ladder,
  COUNT(*) AS n,
  ROUND(AVG(gross_spread_bp), 3) AS gross_spread_ev,
  ROUND(AVG(ae_1s_bp), 3) AS ae_1s,
  ROUND(AVG(ae_3s_bp), 3) AS ae_3s,
  ROUND(AVG(mae_bp), 3) AS mae,
  ROUND(AVG(actual_pnl_bp), 3) AS actual_ev,
  ROUND(SUM(actual_pnl_bp), 2) AS actual_total,
  ROUND(AVG(CASE WHEN actual_exit_reason = 'HARD_STOP' THEN 1.0 ELSE 0.0 END) * 100, 1)
    AS hs_pct,
  ROUND(AVG(cf_conditional_bp - actual_pnl_bp), 3) AS conditional_cf_delta_ev
FROM sliced
GROUP BY 1, 2, 3
HAVING COUNT(*) >= 5
ORDER BY 1, 2, 3;

-- ---------------------------------------------------------------------------
-- 5. Aggressive Maker wait CF (on conditional triggers only)
-- ---------------------------------------------------------------------------
WITH x AS (
  SELECT trade_id, actual_pnl_bp,
         cf_maker_05s_bp, cf_maker_10s_bp, cf_maker_20s_bp, cf_maker_30s_bp
  FROM mm_trade_cf_log
  WHERE conditional_trigger = TRUE
),
policies AS (
  SELECT trade_id, actual_pnl_bp, '0.5s' AS policy, cf_maker_05s_bp AS cf_bp FROM x
  UNION ALL SELECT trade_id, actual_pnl_bp, '1.0s', cf_maker_10s_bp FROM x
  UNION ALL SELECT trade_id, actual_pnl_bp, '2.0s', cf_maker_20s_bp FROM x
  UNION ALL SELECT trade_id, actual_pnl_bp, '3.0s', cf_maker_30s_bp FROM x
)
SELECT
  policy,
  COUNT(cf_bp) AS n,
  ROUND(SUM(actual_pnl_bp), 2) AS actual_bp,
  ROUND(SUM(cf_bp), 2) AS maker_cf_bp,
  ROUND(SUM(cf_bp) - SUM(actual_pnl_bp), 2) AS delta_bp,
  ROUND(AVG(cf_bp - actual_pnl_bp), 3) AS delta_ev
FROM policies
WHERE cf_bp IS NOT NULL
GROUP BY policy
ORDER BY delta_bp DESC;

-- ---------------------------------------------------------------------------
-- 6. BIG_SAVE / BIG_DAMAGE outcomes (gate Economic PASS)
-- ---------------------------------------------------------------------------
SELECT
  CASE
    WHEN cf_conditional_bp - actual_pnl_bp >= 2.0 THEN 'BIG_SAVE'
    WHEN cf_conditional_bp > actual_pnl_bp THEN 'SMALL_SAVE'
    WHEN cf_conditional_bp = actual_pnl_bp THEN 'UNCHANGED'
    WHEN cf_conditional_bp - actual_pnl_bp <= -2.0 THEN 'BIG_DAMAGE'
    ELSE 'SMALL_DAMAGE'
  END AS outcome,
  COUNT(*) AS n,
  ROUND(SUM(cf_conditional_bp - actual_pnl_bp), 2) AS total_delta_bp,
  ROUND(AVG(cf_conditional_bp - actual_pnl_bp), 3) AS avg_delta_bp
FROM mm_trade_cf_log
WHERE conditional_trigger = TRUE
  AND cf_conditional_bp IS NOT NULL
GROUP BY 1
ORDER BY total_delta_bp DESC;

-- ---------------------------------------------------------------------------
-- 7. HardStop rescue rate
-- ---------------------------------------------------------------------------
WITH hs AS (
  SELECT *
  FROM mm_trade_cf_log
  WHERE actual_exit_reason = 'HARD_STOP'
)
SELECT
  COUNT(*) AS actual_hs_n,
  ROUND(SUM(actual_pnl_bp), 2) AS actual_hs_pnl,
  ROUND(SUM(cf_conditional_bp), 2) AS cf_hs_pnl,
  ROUND(SUM(cf_conditional_bp) - SUM(actual_pnl_bp), 2) AS hs_saved_bp,
  ROUND(AVG(CASE WHEN cf_conditional_bp > actual_pnl_bp THEN 1.0 ELSE 0.0 END) * 100, 1)
    AS hs_rescue_rate_pct
FROM hs
WHERE cf_conditional_bp IS NOT NULL;
