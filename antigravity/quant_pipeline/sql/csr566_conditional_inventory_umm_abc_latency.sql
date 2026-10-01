-- CSR-566 — Conditional Inventory@30s · UMM A/B/C · latency-clean · arena_run_id
-- OBSERVE · WIRE=NO · ENFORCE=0 · Economic PASS=NO
-- Primary horizon remains 30s for labels; FORBIDDEN to adopt argmax(horizon).
-- Primary Micro/Trend/tip/FakeBO research set: latency_ms <= 80

-- ---------------------------------------------------------------------------
-- 0. Latency-clean gate (design threshold from MicrostructureAgent = 80ms)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr566_latency_split AS
SELECT
  *,
  CASE WHEN latency_ms <= 80.0 THEN TRUE ELSE FALSE END AS latency_clean,
  CASE
    WHEN latency_ms IS NULL THEN 'UNKNOWN'
    WHEN latency_ms <= 80.0 THEN 'LE80'
    ELSE 'GT80'
  END AS latency_bucket
FROM orderbook_micro_log;

-- Use latency_clean=TRUE as PRIMARY dataset for tip / FakeBO / Trend fwd joins

-- ---------------------------------------------------------------------------
-- 1. Inventory state features for classifier (label = Δ@30s)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr566_inventory_state_rows AS
SELECT
  e.trade_id,
  e.ts,
  e.side,
  e.hold_sec,
  e.hold_ladder,
  e.actual_pnl_bp,
  e.cf_30s_bp,
  (e.cf_30s_bp - e.actual_pnl_bp) AS delta30_bp,
  e.mae_bp,
  e.hard_stop,
  e.regime,
  e.unrealized_pnl_bp,
  e.spread_bp,
  e.tip_thin_bid,
  e.tip_thin_ask,
  e.one_way_buy,
  e.one_way_sell,
  CASE
    WHEN coalesce(e.tip_thin_bid, FALSE) AND coalesce(e.one_way_sell, FALSE)
      THEN 'TIP_THIN_BID_x_ONE_WAY_SELL'
    WHEN coalesce(e.tip_thin_ask, FALSE) AND coalesce(e.one_way_buy, FALSE)
      THEN 'TIP_THIN_ASK_x_ONE_WAY_BUY'
    ELSE 'NO_TIP_COMBO'
  END AS tip_combo,
  date_trunc('hour', e.ts) AS hour_jst,
  coalesce(m.latency_clean, TRUE) AS latency_clean
FROM mm_trade_cf_log e
LEFT JOIN (
  SELECT trade_id, bool_and(latency_ms <= 80.0) AS latency_clean
  FROM trade_latency_join  -- populate via mid/tick join; placeholder name
  GROUP BY 1
) m USING (trade_id)
WHERE e.cf_30s_bp IS NOT NULL AND e.hold_sec > 30;

-- State slices: where does @30s help vs hurt?
CREATE OR REPLACE VIEW csr566_delta30_by_state AS
SELECT
  hold_ladder,
  side,
  tip_combo,
  coalesce(regime, 'UNKNOWN') AS regime,
  CASE
    WHEN unrealized_pnl_bp IS NULL THEN 'U_UNK'
    WHEN unrealized_pnl_bp >= 0 THEN 'U_POS'
    WHEN unrealized_pnl_bp >= -5 THEN 'U_SMALL_NEG'
    ELSE 'U_DEEP_NEG'
  END AS upnl_bucket,
  latency_clean,
  count(*) AS n,
  round(avg(delta30_bp), 4) AS mean_delta30_bp,
  round(median(delta30_bp), 4) AS median_delta30_bp,
  round(sum(delta30_bp), 4) AS sum_delta30_bp,
  round(avg(CASE WHEN delta30_bp > 0 THEN 1.0 ELSE 0.0 END), 4) AS improve_rate,
  round(quantile_cont(delta30_bp, 0.05), 4) AS worst_5pct_delta30
FROM csr566_inventory_state_rows
GROUP BY 1, 2, 3, 4, 5, 6
HAVING count(*) >= 20
ORDER BY mean_delta30_bp DESC;

-- Hourly (report only — do not treat hour as the classifier target)
CREATE OR REPLACE VIEW csr566_delta30_hourly AS
SELECT
  hour_jst,
  count(*) AS n,
  round(avg(delta30_bp), 4) AS mean_delta30_bp,
  round(sum(delta30_bp), 4) AS sum_delta30_bp,
  CASE WHEN avg(delta30_bp) > 0 THEN 1 ELSE 0 END AS hour_positive
FROM csr566_inventory_state_rows
GROUP BY 1
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 2. UMM attribution A / B / C
-- A: hour where L0 mean actual < 0  → entry/quote
-- B: hour L0≥0 and (L1+ mean < L0) → holding
-- C: HARD_STOP loss share
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr566_umm_ladder_hour AS
SELECT
  date_trunc('hour', ts) AS hour_jst,
  hold_ladder,
  count(*) AS n,
  round(avg(actual_pnl_bp), 4) AS mean_actual_bp,
  round(sum(actual_pnl_bp), 4) AS sum_actual_bp,
  round(avg(CASE WHEN actual_pnl_bp > 0 THEN 1.0 ELSE 0.0 END), 4) AS win_rate,
  round(avg(CASE WHEN actual_pnl_bp > 0 THEN actual_pnl_bp END), 4) AS avg_win_bp,
  round(avg(CASE WHEN actual_pnl_bp <= 0 THEN actual_pnl_bp END), 4) AS avg_loss_bp,
  round(quantile_cont(actual_pnl_bp, 0.05), 4) AS worst_5pct_bp,
  round(avg(CASE WHEN hard_stop THEN 1.0 ELSE 0.0 END), 4) AS hs_rate,
  round(sum(CASE WHEN hard_stop THEN actual_pnl_bp ELSE 0 END), 4) AS hs_loss_bp
FROM mm_trade_cf_log
WHERE strategy_id LIKE '%umm%' OR strategy_family = 'UMM'
GROUP BY 1, 2;

CREATE OR REPLACE VIEW csr566_umm_abc_hour AS
WITH piv AS (
  SELECT
    hour_jst,
    max(CASE WHEN hold_ladder = 'L0' THEN mean_actual_bp END) AS l0_mean,
    max(CASE WHEN hold_ladder = 'L1' THEN mean_actual_bp END) AS l1_mean,
    max(CASE WHEN hold_ladder = 'L2' THEN mean_actual_bp END) AS l2_mean,
    max(CASE WHEN hold_ladder = 'L3' THEN mean_actual_bp END) AS l3_mean,
    sum(n) AS n_all,
    sum(hs_loss_bp) AS hs_loss_bp,
    sum(sum_actual_bp) AS sum_actual_bp
  FROM csr566_umm_ladder_hour
  GROUP BY 1
)
SELECT
  hour_jst,
  l0_mean,
  l1_mean,
  l2_mean,
  l3_mean,
  n_all,
  hs_loss_bp,
  sum_actual_bp,
  CASE
    WHEN l0_mean IS NOT NULL AND l0_mean < 0 THEN 'A_ENTRY_QUOTE'
    WHEN l0_mean IS NOT NULL AND l0_mean >= 0
         AND coalesce(l1_mean, 0) < l0_mean THEN 'B_HOLDING'
    ELSE 'MIXED_OR_THIN'
  END AS primary_bucket,
  CASE
    WHEN abs(coalesce(hs_loss_bp, 0)) >= 0.30 * abs(nullif(sum_actual_bp, 0))
      THEN TRUE ELSE FALSE
  END AS c_hard_stop_dominant
FROM piv
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 3. Arena run boundary (DO NOT concat across run_started_at)
-- ---------------------------------------------------------------------------
-- Expected columns on arena_trade_log / dryrun_approved_arena exports:
--   arena_run_id, strategy_id, run_started_at, ts, pnl_bp, ...
CREATE OR REPLACE VIEW csr566_arena_by_run AS
SELECT
  arena_run_id,
  strategy_id,
  run_started_at,
  count(*) AS n,
  round(sum(pnl_bp), 4) AS sum_bp,
  round(avg(pnl_bp), 4) AS mean_bp,
  round(avg(CASE WHEN pnl_bp > 0 THEN 1.0 ELSE 0.0 END), 4) AS win_rate,
  min(ts) AS first_ts,
  max(ts) AS last_ts
FROM arena_trade_log
GROUP BY 1, 2, 3
ORDER BY run_started_at, sum_bp DESC;

-- ---------------------------------------------------------------------------
-- 4. Tip SHADOW vs pressure (latency-clean primary)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr566_tip_vs_pressure_fwd AS
SELECT
  latency_bucket,
  tip_combo,
  pressure_side,
  count(*) AS n,
  round(avg(fwd_30s_bp), 4) AS mean_fwd30_bp,
  round(avg(side_adj_fwd_30s_bp), 4) AS mean_side_adj_fwd30_bp
FROM tip_pressure_forward_mid_log  -- feature→forward-mid join table
GROUP BY 1, 2, 3
HAVING count(*) >= 30
ORDER BY 1, 4 DESC;

-- ---------------------------------------------------------------------------
-- 5. Research gates (not Economic PASS / not ENFORCE)
-- ---------------------------------------------------------------------------
-- P0a: csr566_umm_abc_hour populated · A vs B vs C hours counted
-- P0b: tip/FakeBO/Trend reports show LE80 vs GT80 split
-- P1a: csr566_delta30_by_state has slices with n≥20 both improve_rate>0.55 and <0.45
-- P1b: PEG ablation fire counts: Aging / BE / Fake separately before economics
-- FORBIDDEN: SELECT horizon ORDER BY mean_delta LIMIT 1 → adopt
-- FORBIDDEN: pool arena_run_id across 04:17 reset
-- FORBIDDEN: promote PEG on AE_1s/AE_3s green while 24h Δ≪0
-- FORBIDDEN: score PEG as BE3/FakeLiq composite while exits=0 / skips=0

-- ---------------------------------------------------------------------------
-- 6. PEG fire audit (Aging vs BE vs Fake) — economics only after fires
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr566_peg_component_fires AS
SELECT
  date_trunc('hour', ts) AS hour_jst,
  sum(CASE WHEN exit_reason = 'AGING_GUARD' THEN 1 ELSE 0 END) AS aging_exits,
  sum(CASE WHEN exit_reason = 'BE3' THEN 1 ELSE 0 END) AS be_exits,
  sum(CASE WHEN skip_reason = 'FAKE_LIQUIDITY' THEN 1 ELSE 0 END) AS fake_skips,
  round(sum(pnl_bp), 4) AS peg_sum_bp
FROM peg_v3_trade_log
GROUP BY 1
ORDER BY 1;

CREATE OR REPLACE VIEW csr566_peg_vs_baseline_hourly AS
SELECT
  h.hour_jst,
  h.aging_exits,
  h.be_exits,
  h.fake_skips,
  h.peg_sum_bp,
  b.baseline_sum_bp,
  (h.peg_sum_bp - b.baseline_sum_bp) AS delta_bp,
  CASE
    WHEN h.be_exits = 0 AND h.fake_skips = 0 AND h.aging_exits > 0
      THEN 'AGING_ONLY_ACTIVE'
    WHEN h.be_exits = 0 AND h.fake_skips = 0 AND h.aging_exits = 0
      THEN 'NO_COMPONENT_FIRE'
    ELSE 'MULTI_OR_OTHER'
  END AS active_components
FROM csr566_peg_component_fires h
LEFT JOIN (
  SELECT date_trunc('hour', ts) AS hour_jst, round(sum(pnl_bp), 4) AS baseline_sum_bp
  FROM tf2bp_baseline_trade_log
  GROUP BY 1
) b USING (hour_jst);
