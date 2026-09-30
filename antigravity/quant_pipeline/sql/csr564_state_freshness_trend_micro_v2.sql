-- CSR-564 P0 — Council State Freshness audit
-- Reject STALE rows from teacher / CF populations
-- WIRE=NO · ENFORCE=0

-- Expected: council_verdict_log or parquet with
--   ts, schema_version, candidate_action, decision_action,
--   state_write_ts_ms, mid, trend_strength, pressure_side

CREATE OR REPLACE VIEW csr564_council_freshness AS
SELECT
  ts,
  schema_version,
  candidate_action,
  decision_action,
  final_action,
  trend_strength,
  pressure_side,
  state_write_ts_ms,
  -- age at ingest if wall_ts present; else NULL
  CASE
    WHEN state_write_ts_ms IS NULL THEN NULL
    WHEN wall_ts_ms IS NULL THEN NULL
    ELSE (wall_ts_ms - state_write_ts_ms) / 1000.0
  END AS state_age_sec,
  CASE
    WHEN state_write_ts_ms IS NULL THEN FALSE
    WHEN wall_ts_ms IS NULL THEN FALSE
    WHEN (wall_ts_ms - state_write_ts_ms) / 1000.0 <= 120.0 THEN TRUE
    ELSE FALSE
  END AS freshness_ok,
  CASE
    WHEN schema_version LIKE 'LEGACY%' THEN FALSE
    WHEN candidate_action IS NULL OR decision_action IS NULL THEN FALSE
    WHEN state_write_ts_ms IS NOT NULL
         AND wall_ts_ms IS NOT NULL
         AND (wall_ts_ms - state_write_ts_ms) / 1000.0 <= 120.0
         AND NOT (
           ts >= TIMESTAMP '2026-10-01 03:00:00+09'
           AND ts < TIMESTAMP '2026-10-01 08:00:00+09'
         )
    THEN TRUE
    ELSE FALSE
  END AS teacher_valid
FROM council_verdict_log;

CREATE OR REPLACE VIEW csr564_freshness_hourly AS
SELECT
  date_trunc('hour', ts) AS hour_jst,
  count(*) AS n,
  avg(CASE WHEN freshness_ok THEN 1.0 ELSE 0.0 END) AS freshness_rate,
  avg(CASE WHEN teacher_valid THEN 1.0 ELSE 0.0 END) AS teacher_valid_rate,
  avg(state_age_sec) AS mean_age_sec,
  max(state_age_sec) AS max_age_sec,
  count(DISTINCT round(trend_strength, 4) || '|' || coalesce(candidate_action, '')) AS distinct_sig_n
FROM csr564_council_freshness
GROUP BY 1
ORDER BY 1;

-- Identical-signature freeze detector (same candidate+strength for ≥N hours)
CREATE OR REPLACE VIEW csr564_identical_signature_hours AS
SELECT
  candidate_action,
  round(trend_strength, 4) AS strength_r4,
  coalesce(pressure_side, 'none') AS pressure_side,
  count(DISTINCT date_trunc('hour', ts)) AS hours_with_sig,
  min(ts) AS first_ts,
  max(ts) AS last_ts
FROM csr564_council_freshness
GROUP BY 1, 2, 3
HAVING count(DISTINCT date_trunc('hour', ts)) >= 3;

-- Hourly Acceptance Gate rollup (PASS only when freshness+diversity OK)
CREATE OR REPLACE VIEW csr564_acceptance_gate_hourly AS
SELECT
  hour_jst,
  n,
  freshness_rate,
  teacher_valid_rate,
  mean_age_sec,
  max_age_sec,
  distinct_sig_n,
  CASE
    WHEN n = 0 THEN 'FAIL_EMPTY'
    WHEN freshness_rate < 1.0 THEN 'FAIL_STALE'
    WHEN teacher_valid_rate < 1.0 THEN 'FAIL_TEACHER'
    WHEN distinct_sig_n < 2 AND n >= 3 THEN 'FAIL_IDENTICAL'
    ELSE 'PASS'
  END AS acceptance_gate,
  FALSE AS economic_pass_allowed  -- never from this view alone
FROM csr564_freshness_hourly;

-- P1: Trend forward-mid collect (FusionDecisionLog / trend_research join)
-- Populate table trend_forward_mid_log then:
CREATE OR REPLACE VIEW csr564_trend_forward_mid AS
SELECT
  candidate_ts,
  candidate_side,
  raw_move,
  strength_raw,
  strength_clipped,
  fwd_1s,
  fwd_3s,
  fwd_10s,
  fwd_30s,
  fwd_60s,
  freshness_ok,
  teacher_valid
FROM trend_forward_mid_log
WHERE teacher_valid = TRUE AND freshness_ok = TRUE;

-- P2: Micro Pressure v2 × Trend 4-quadrant forward EV (SHADOW · after P0 PASS)
CREATE OR REPLACE VIEW csr564_micro_v2_quadrant_ev AS
SELECT
  CASE
    WHEN upper(candidate_side) = 'BUY' AND buy_pressure_v2 >= sell_pressure_v2
      THEN 'TREND_BUY_x_BUY_PRESSURE'
    WHEN upper(candidate_side) = 'BUY' AND buy_pressure_v2 < sell_pressure_v2
      THEN 'TREND_BUY_x_SELL_PRESSURE'
    WHEN upper(candidate_side) = 'SELL' AND sell_pressure_v2 >= buy_pressure_v2
      THEN 'TREND_SELL_x_SELL_PRESSURE'
    WHEN upper(candidate_side) = 'SELL' AND sell_pressure_v2 < buy_pressure_v2
      THEN 'TREND_SELL_x_BUY_PRESSURE'
    ELSE 'OTHER'
  END AS quadrant,
  count(*) AS n,
  avg(fwd_30s) AS mean_fwd_30s_bp,
  avg(fwd_60s) AS mean_fwd_60s_bp
FROM trend_forward_mid_log t
JOIN micro_pressure_v2_shadow m USING (candidate_ts)
WHERE t.teacher_valid = TRUE AND t.freshness_ok = TRUE
GROUP BY 1
ORDER BY 1;
