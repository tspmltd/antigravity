-- CSR-560 / PDCA2.2 — Council research DuckDB aggregations
-- Governance: WIRE=NO · ENFORCE=0 · LIVE=OFF · auto_apply=OFF · Economic PASS=NO
-- Teacher labels: candidate_action / decision_action ONLY (final_action FORBIDDEN)
-- Adverse population: adverse_score_valid = true ONLY (SHADOW_OFF NULL ≠ safe 0)
--
-- Expected base relation (view or table) — align to FusionDecisionLog + mid join:
--   council_teacher_rows(
--     ts_ms BIGINT,
--     episode_id VARCHAR,
--     regime VARCHAR,
--     hypothesis_direction VARCHAR,
--     candidate_action VARCHAR,          -- buy|sell|hold
--     candidate_confidence DOUBLE,
--     strength_raw DOUBLE,               -- optional: pre-clip |diff|/3000
--     strength_clipped DOUBLE,           -- optional: min(1, strength_raw)
--     decision_action VARCHAR,           -- buy|sell|hold|exit
--     decision_confidence DOUBLE,
--     decision_gate_reason VARCHAR,
--     veto BOOLEAN,
--     veto_reason VARCHAR,
--     veto_effective BOOLEAN,            -- CSR-558: veto AND decision IN (buy,sell)
--     fake_bo BOOLEAN,
--     imbalance DOUBLE,
--     micro_dev DOUBLE,
--     pressure_side VARCHAR,
--     latency_ms DOUBLE,
--     adverse_score DOUBLE,
--     adverse_score_valid BOOLEAN,
--     mid_t0 DOUBLE,
--     mid_1s DOUBLE,
--     mid_3s DOUBLE,
--     mid_10s DOUBLE,
--     mid_30s DOUBLE,
--     mid_60s DOUBLE
--   )
--
-- directional_forward_bp(side, mid0, mid_h):
--   BUY  -> (mid_h - mid0) / mid0 * 1e4
--   SELL -> (mid0 - mid_h) / mid0 * 1e4

-- ---------------------------------------------------------------------------
-- Helpers (DuckDB macros)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE MACRO dir_fwd_bp(side, mid0, mid_h) AS (
  CASE
    WHEN mid0 IS NULL OR mid0 <= 0 OR mid_h IS NULL THEN NULL
    WHEN lower(side) = 'buy'  THEN (mid_h - mid0) / mid0 * 10000.0
    WHEN lower(side) = 'sell' THEN (mid0 - mid_h) / mid0 * 10000.0
    ELSE NULL
  END
);

CREATE OR REPLACE MACRO conf_bucket(c) AS (
  CASE
    WHEN c IS NULL THEN 'NA'
    WHEN c < 0.25 THEN '0-.25'
    WHEN c < 0.45 THEN '.25-.45'
    WHEN c < 0.65 THEN '.45-.65'
    WHEN c < 0.80 THEN '.65-.80'
    WHEN c < 1.00 THEN '.80-.99'
    ELSE '1.00'
  END
);

-- ---------------------------------------------------------------------------
-- P0: Trend candidate Direction Alpha (forward mid teacher)
-- Population: candidate IN (buy,sell) · latency split optional
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr560_p0_direction_by_conf AS
SELECT
  conf_bucket(candidate_confidence) AS conf_bucket,
  count(*) AS n,
  avg(dir_fwd_bp(candidate_action, mid_t0, mid_1s))  AS ev_1s_bp,
  avg(dir_fwd_bp(candidate_action, mid_t0, mid_3s))  AS ev_3s_bp,
  avg(dir_fwd_bp(candidate_action, mid_t0, mid_10s)) AS ev_10s_bp,
  avg(dir_fwd_bp(candidate_action, mid_t0, mid_30s)) AS ev_30s_bp,
  avg(dir_fwd_bp(candidate_action, mid_t0, mid_60s)) AS ev_60s_bp,
  avg(CASE WHEN dir_fwd_bp(candidate_action, mid_t0, mid_30s) > 0 THEN 1.0 ELSE 0.0 END) AS hit_30s,
  -- calibration diagnostics
  avg(candidate_confidence) AS mean_conf,
  avg(strength_raw) AS mean_strength_raw,
  avg(strength_clipped) AS mean_strength_clipped
FROM council_teacher_rows
WHERE lower(candidate_action) IN ('buy', 'sell')
  AND mid_t0 IS NOT NULL
GROUP BY 1
ORDER BY
  CASE conf_bucket(candidate_confidence)
    WHEN '0-.25' THEN 1 WHEN '.25-.45' THEN 2 WHEN '.45-.65' THEN 3
    WHEN '.65-.80' THEN 4 WHEN '.80-.99' THEN 5 WHEN '1.00' THEN 6 ELSE 9
  END;

-- P0 regime slice
CREATE OR REPLACE VIEW csr560_p0_direction_by_regime_conf AS
SELECT
  coalesce(regime, 'NA') AS regime,
  conf_bucket(candidate_confidence) AS conf_bucket,
  count(*) AS n,
  avg(dir_fwd_bp(candidate_action, mid_t0, mid_30s)) AS ev_30s_bp,
  avg(CASE WHEN dir_fwd_bp(candidate_action, mid_t0, mid_30s) > 0 THEN 1.0 ELSE 0.0 END) AS hit_30s
FROM council_teacher_rows
WHERE lower(candidate_action) IN ('buy', 'sell')
GROUP BY 1, 2;

-- Latency separation (signal ≠ execution quality)
CREATE OR REPLACE VIEW csr560_p0_direction_latency_split AS
SELECT
  CASE
    WHEN latency_ms IS NULL THEN 'latency_unknown'
    WHEN latency_ms <= 80 THEN 'latency_le_80'
    ELSE 'latency_gt_80'
  END AS latency_bucket,
  conf_bucket(candidate_confidence) AS conf_bucket,
  count(*) AS n,
  avg(dir_fwd_bp(candidate_action, mid_t0, mid_30s)) AS ev_30s_bp
FROM council_teacher_rows
WHERE lower(candidate_action) IN ('buy', 'sell')
GROUP BY 1, 2;

-- ---------------------------------------------------------------------------
-- P2: Decision gate CF (candidate BUY/SELL → decision HOLD)
-- "もし candidate 方向で執行していたら"
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr560_p2_decision_gate_cf AS
SELECT
  conf_bucket(candidate_confidence) AS conf_bucket,
  count(*) AS n_gated_hold,
  avg(dir_fwd_bp(candidate_action, mid_t0, mid_30s)) AS cf_if_traded_30s_bp,
  avg(CASE WHEN dir_fwd_bp(candidate_action, mid_t0, mid_30s) < 0 THEN 1.0 ELSE 0.0 END) AS gate_saved_rate_30s,
  -- positive CF means gate blocked a winner (opportunity cost)
  avg(CASE WHEN dir_fwd_bp(candidate_action, mid_t0, mid_30s) > 0 THEN 1.0 ELSE 0.0 END) AS gate_missed_win_rate_30s
FROM council_teacher_rows
WHERE lower(candidate_action) IN ('buy', 'sell')
  AND lower(decision_action) = 'hold'
GROUP BY 1
ORDER BY 1;

-- Pass-through decisions (candidate == decision BUY/SELL)
CREATE OR REPLACE VIEW csr560_p2_decision_passthrough_ev AS
SELECT
  conf_bucket(decision_confidence) AS conf_bucket,
  lower(decision_action) AS decision_action,
  count(*) AS n,
  avg(dir_fwd_bp(decision_action, mid_t0, mid_30s)) AS ev_30s_bp
FROM council_teacher_rows
WHERE lower(decision_action) IN ('buy', 'sell')
  AND coalesce(veto, false) = false
GROUP BY 1, 2;

-- ---------------------------------------------------------------------------
-- P3 / FakeBO: Effective Veto CF ONLY (CSR-558)
-- Population MUST be:
--   decision IN (buy,sell) AND (fake_bo OR veto_reason='FAKE_BREAKOUT')
-- Low-confidence HOLD+FakeBO = REDUNDANT → EXCLUDE from Veto economics
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr560_fakebo_effective_veto_rows AS
SELECT
  *,
  dir_fwd_bp(decision_action, mid_t0, mid_1s)  AS cf_1s_bp,
  dir_fwd_bp(decision_action, mid_t0, mid_3s)  AS cf_3s_bp,
  dir_fwd_bp(decision_action, mid_t0, mid_10s) AS cf_10s_bp,
  dir_fwd_bp(decision_action, mid_t0, mid_30s) AS cf_30s_bp,
  dir_fwd_bp(decision_action, mid_t0, mid_60s) AS cf_60s_bp,
  -- avoided_loss proxy: if CF negative, veto avoided that loss
  CASE
    WHEN dir_fwd_bp(decision_action, mid_t0, mid_30s) IS NULL THEN NULL
    ELSE greatest(0.0, -dir_fwd_bp(decision_action, mid_t0, mid_30s))
  END AS avoided_loss_30s_bp,
  CASE
    WHEN dir_fwd_bp(decision_action, mid_t0, mid_30s) IS NULL THEN NULL
    ELSE greatest(0.0, dir_fwd_bp(decision_action, mid_t0, mid_30s))
  END AS missed_profit_30s_bp,
  -- net_veto_value ≈ avoided_loss - missed_profit = -CF
  CASE
    WHEN dir_fwd_bp(decision_action, mid_t0, mid_30s) IS NULL THEN NULL
    ELSE -dir_fwd_bp(decision_action, mid_t0, mid_30s)
  END AS net_veto_value_30s_bp
FROM council_teacher_rows
WHERE lower(decision_action) IN ('buy', 'sell')
  AND (
    coalesce(veto_effective, false) = true
    OR (
      coalesce(veto, false) = true
      AND coalesce(fake_bo, false) = true
    )
    OR upper(coalesce(veto_reason, '')) IN ('FAKE_BREAKOUT', 'FAKE_BO')
  );

CREATE OR REPLACE VIEW csr560_fakebo_cf_summary AS
SELECT
  count(*) AS n_effective_veto,
  avg(cf_1s_bp) AS mean_cf_1s,
  avg(cf_3s_bp) AS mean_cf_3s,
  avg(cf_10s_bp) AS mean_cf_10s,
  avg(cf_30s_bp) AS mean_cf_30s,
  avg(cf_60s_bp) AS mean_cf_60s,
  avg(avoided_loss_30s_bp) AS mean_avoided_loss_30s,
  avg(missed_profit_30s_bp) AS mean_missed_profit_30s,
  avg(net_veto_value_30s_bp) AS mean_net_veto_value_30s,
  avg(CASE WHEN cf_30s_bp < 0 THEN 1.0 ELSE 0.0 END) AS pct_cf_negative_30s
FROM csr560_fakebo_effective_veto_rows;

-- Contrast: decision BUY/SELL without veto vs effective FakeBO veto
CREATE OR REPLACE VIEW csr560_fakebo_vs_noveto AS
SELECT
  CASE
    WHEN coalesce(veto_effective, false)
      OR (coalesce(veto, false) AND coalesce(fake_bo, false))
      THEN 'decision_BUYSELL_FakeBO_veto'
    ELSE 'decision_BUYSELL_no_veto'
  END AS cohort,
  count(*) AS n,
  avg(dir_fwd_bp(decision_action, mid_t0, mid_1s))  AS ev_1s_bp,
  avg(dir_fwd_bp(decision_action, mid_t0, mid_3s))  AS ev_3s_bp,
  avg(dir_fwd_bp(decision_action, mid_t0, mid_10s)) AS ev_10s_bp,
  avg(dir_fwd_bp(decision_action, mid_t0, mid_30s)) AS ev_30s_bp,
  avg(dir_fwd_bp(decision_action, mid_t0, mid_60s)) AS ev_60s_bp
FROM council_teacher_rows
WHERE lower(decision_action) IN ('buy', 'sell')
GROUP BY 1;

-- Redundant veto audit (must NOT enter economics)
CREATE OR REPLACE VIEW csr560_fakebo_redundant_audit AS
SELECT
  count(*) AS n_redundant,
  avg(candidate_confidence) AS mean_cand_conf,
  avg(dir_fwd_bp(candidate_action, mid_t0, mid_30s)) AS cf_if_candidate_traded_30s
FROM council_teacher_rows
WHERE coalesce(fake_bo, false) = true
  AND lower(decision_action) = 'hold'
  AND lower(candidate_action) IN ('buy', 'sell');

-- ---------------------------------------------------------------------------
-- Confidence calibration PASS / FAIL gates (read with csr560_p0_direction_by_conf)
-- ---------------------------------------------------------------------------
-- See lock JSON for numeric thresholds. SQL emits monotonicity score:
CREATE OR REPLACE VIEW csr560_confidence_calibration_score AS
WITH b AS (
  SELECT * FROM csr560_p0_direction_by_conf
  WHERE conf_bucket IN ('.45-.65', '.65-.80', '.80-.99', '1.00')
),
ord AS (
  SELECT
    conf_bucket,
    n,
    ev_30s_bp,
    hit_30s,
    row_number() OVER (ORDER BY
      CASE conf_bucket
        WHEN '.45-.65' THEN 1 WHEN '.65-.80' THEN 2
        WHEN '.80-.99' THEN 3 WHEN '1.00' THEN 4
      END
    ) AS rk
  FROM b
),
pairs AS (
  SELECT
    a.conf_bucket AS lo_bucket,
    b.conf_bucket AS hi_bucket,
    a.ev_30s_bp AS lo_ev,
    b.ev_30s_bp AS hi_ev,
    CASE WHEN b.ev_30s_bp >= a.ev_30s_bp THEN 1 ELSE 0 END AS mono_ok
  FROM ord a
  JOIN ord b ON b.rk = a.rk + 1
)
SELECT
  (SELECT count(*) FROM pairs) AS n_adjacent_pairs,
  (SELECT sum(mono_ok) FROM pairs) AS n_mono_ok,
  (SELECT avg(mono_ok) FROM pairs) AS mono_rate,
  (SELECT ev_30s_bp FROM ord WHERE conf_bucket = '1.00') AS ev_100,
  (SELECT ev_30s_bp FROM ord WHERE conf_bucket = '.65-.80') AS ev_65_80,
  (SELECT n FROM ord WHERE conf_bucket = '1.00') AS n_100,
  (SELECT avg(mean_strength_raw) FROM csr560_p0_direction_by_conf WHERE conf_bucket = '1.00') AS mean_raw_at_100;

-- ---------------------------------------------------------------------------
-- Smoke queries (run after council_teacher_rows is populated)
-- ---------------------------------------------------------------------------
-- SELECT * FROM csr560_p0_direction_by_conf;
-- SELECT * FROM csr560_fakebo_cf_summary;
-- SELECT * FROM csr560_fakebo_vs_noveto;
-- SELECT * FROM csr560_fakebo_redundant_audit;
-- SELECT * FROM csr560_confidence_calibration_score;
-- SELECT * FROM csr560_p2_decision_gate_cf;
