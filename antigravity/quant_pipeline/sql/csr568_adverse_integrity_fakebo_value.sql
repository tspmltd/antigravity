-- CSR-568 — Adverse integrity · FakeBO veto value · Spread Gate CF · EXIT semantics
-- OBSERVE · WIRE=NO · ENFORCE=0 · Economic PASS=NO until integrity PASS
-- FORBIDDEN: WIRE Adverse on Low/High EV split alone
-- FORBIDDEN: interpret FakeBO snapshot fire-rate as market FakeBO share
-- FORBIDDEN: optimize Spread Gate pass-rate as primary KPI

-- ---------------------------------------------------------------------------
-- 0. Adverse score provenance audit (CSR-539 hold)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr568_adverse_score_provenance AS
SELECT
  trade_id,
  ts,
  strategy_id,
  run_id,
  adverse_score,
  score_source,
  score_version,
  CASE
    WHEN adverse_score IS NULL THEN TRUE ELSE FALSE
  END AS missing,
  CASE
    WHEN defaulted OR (adverse_score = 30 AND score_source IN ('default', 'fallback', 'MISSING_FILL'))
      THEN TRUE ELSE FALSE
  END AS defaulted_or_collapse30,
  CASE
    WHEN adverse_score IS NULL THEN 'MISSING'
    WHEN adverse_score < 20 THEN '0-20'
    WHEN adverse_score < 40 THEN '20-40'
    WHEN adverse_score < 60 THEN '40-60'
    WHEN adverse_score < 80 THEN '60-80'
    ELSE '80-100'
  END AS score_bucket
FROM mm_trade_cf_log;

CREATE OR REPLACE VIEW csr568_adverse_integrity_hourly AS
SELECT
  date_trunc('hour', ts) AS hour_jst,
  count(*) AS n,
  count(DISTINCT score_source) AS n_sources,
  count(DISTINCT score_version) AS n_versions,
  count(DISTINCT run_id) AS n_runs,
  avg(CASE WHEN missing THEN 1.0 ELSE 0.0 END) AS missing_rate,
  avg(CASE WHEN defaulted_or_collapse30 THEN 1.0 ELSE 0.0 END) AS default30_rate,
  count(DISTINCT score_bucket) AS n_buckets,
  -- single-bucket collapse detector (e.g. 05:00 all in 20-40)
  max(bucket_n) * 1.0 / nullif(count(*), 0) AS max_bucket_share
FROM (
  SELECT
    *,
    count(*) OVER (PARTITION BY date_trunc('hour', ts), score_bucket) AS bucket_n
  FROM csr568_adverse_score_provenance
) x
GROUP BY 1
ORDER BY 1;

-- Economic slice ONLY after integrity hour PASS (scripted filter)
CREATE OR REPLACE VIEW csr568_adverse_ev_by_bucket AS
SELECT
  CASE WHEN adverse_score < 40 THEN 'LOW' WHEN adverse_score >= 60 THEN 'HIGH' ELSE 'MID' END AS adv_band,
  count(*) AS n,
  round(avg(CASE WHEN actual_pnl_bp > 0 THEN 1.0 ELSE 0.0 END), 4) AS win_rate,
  round(avg(actual_pnl_bp), 4) AS mean_ev_bp,
  round(median(actual_pnl_bp), 4) AS median_ev_bp
FROM mm_trade_cf_log
WHERE adverse_score_valid = TRUE
  AND coalesce(defaulted, FALSE) = FALSE
  AND adverse_score IS NOT NULL
  AND integrity_hold = FALSE  -- column/flag from CSR-539 repair
GROUP BY 1;

-- ---------------------------------------------------------------------------
-- 1. FakeBO veto value: effective / redundant / harmful
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr568_veto_value_classes AS
SELECT
  decision_id,
  ts,
  candidate_action,
  decision_action,
  veto,
  veto_effective,
  veto_reason,
  CASE
    WHEN veto AND veto_effective THEN 'EFFECTIVE'
    WHEN veto AND NOT veto_effective THEN 'REDUNDANT'
    WHEN NOT veto THEN 'NO_VETO'
    ELSE 'OTHER'
  END AS veto_class,
  cf_pnl_without_veto_bp,
  cf_pnl_with_veto_bp,
  (cf_pnl_without_veto_bp - cf_pnl_with_veto_bp) AS veto_saved_bp
FROM council_veto_cf_log;

CREATE OR REPLACE VIEW csr568_veto_value_scorecard AS
SELECT
  veto_class,
  count(*) AS n,
  round(avg(veto_saved_bp), 4) AS mean_saved_bp,
  round(sum(veto_saved_bp), 4) AS sum_saved_bp,
  round(avg(CASE WHEN veto_saved_bp > 0 THEN 1.0 ELSE 0.0 END), 4) AS help_rate,
  round(avg(CASE WHEN veto_saved_bp < 0 THEN 1.0 ELSE 0.0 END), 4) AS harm_rate
FROM csr568_veto_value_classes
WHERE veto_class IN ('EFFECTIVE', 'REDUNDANT')
GROUP BY 1;

-- harmful ⊂ EFFECTIVE with veto_saved_bp < 0 (blocked a good trade)

-- ---------------------------------------------------------------------------
-- 2. Spread Gate rejected-EV (pass-rate is diagnostic only)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr568_spread_gate_cf AS
SELECT
  date_trunc('hour', ts) AS hour_jst,
  reject_reason,  -- Toxic / Spread / entry_halt / confidence / PASS
  count(*) AS n,
  round(avg(cf_pnl_if_taken_bp), 4) AS mean_rejected_or_accepted_cf_bp,
  round(avg(actual_pnl_bp), 4) AS mean_actual_bp
FROM spread_gate_decision_log
GROUP BY 1, 2
ORDER BY 1, 2;

-- Gate value ≈ EV(accept path) vs EV(if rejected had traded) — compute in script

-- ---------------------------------------------------------------------------
-- 3. UMM adverse × inventory attribution
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr568_umm_adverse_inventory AS
SELECT
  CASE
    WHEN adverse_score IS NULL OR adverse_score_valid = FALSE THEN 'ADV_NULL'
    WHEN adverse_score < 40 THEN 'ADV_LOW'
    WHEN adverse_score < 60 THEN 'ADV_MID'
    ELSE 'ADV_HIGH'
  END AS adverse_bucket,
  hold_ladder,
  side,
  CASE
    WHEN hold_sec < 10 THEN 'AGE_<10'
    WHEN hold_sec < 30 THEN 'AGE_10_30'
    WHEN hold_sec < 60 THEN 'AGE_30_60'
    WHEN hold_sec < 120 THEN 'AGE_60_120'
    ELSE 'AGE_120p'
  END AS inventory_age_bucket,
  CASE
    WHEN spread_at_entry_bp < 2 THEN 'SPR_<2'
    WHEN spread_at_entry_bp < 5 THEN 'SPR_2_5'
    ELSE 'SPR_5p'
  END AS spread_bucket,
  count(*) AS n,
  round(avg(actual_pnl_bp), 4) AS avg_pnl,
  round(median(actual_pnl_bp), 4) AS median_pnl,
  round(avg(CASE WHEN actual_pnl_bp > 0 THEN 1.0 ELSE 0.0 END), 4) AS win_rate,
  round(avg(mae_bp), 4) AS mean_mae,
  round(avg(CASE WHEN hard_stop THEN 1.0 ELSE 0.0 END), 4) AS hs_rate
FROM mm_trade_cf_log
WHERE strategy_family = 'UMM' OR strategy_id LIKE '%umm%'
GROUP BY 1, 2, 3, 4, 5
HAVING count(*) >= 20
ORDER BY avg_pnl ASC;

-- ---------------------------------------------------------------------------
-- 4. EXIT semantics PREPARE (documentation view — no enforce)
-- ---------------------------------------------------------------------------
-- Target fields on future council_verdict_log:
--   position_state ∈ {FLAT,LONG,SHORT}
--   candidate_intent / decision_intent ∈ {NO_ENTRY, ENTER_BUY, ENTER_SELL, EXIT_LONG, EXIT_SHORT, HOLD}
-- Map legacy: FLAT+decision=EXIT → decision_intent=NO_ENTRY

-- FORBIDDEN: Economic PASS / WIRE Adverse before csr568_adverse_integrity_hourly PASS
-- FORBIDDEN: FakeBO threshold retune before EFFECTIVE CF scorecard
-- FORBIDDEN: Spread Gate pass-rate optimization as primary
