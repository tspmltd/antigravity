-- CSR-567 — Mutation OOS gate · EmaTrend pause · UMM loss shape
-- OBSERVE · WIRE=NO · ENFORCE=0 · Economic PASS=NO
-- Label hygiene: MUTATED / VALIDATED_* — never call code-rewrite "IMPROVED"

-- ---------------------------------------------------------------------------
-- 1. Mutation cycle ledger (Arena autonomous loop)
-- ---------------------------------------------------------------------------
-- Expected: mutation_cycle_log
--   cycle_id, parent_strategy_id, child_strategy_id, parent_version, child_version,
--   fit_start, fit_end, oos_start, oos_end, n_oos, sharpe_parent, sharpe_child,
--   pf_parent, pf_child, worst5_parent, worst5_child, adopted, reject_reason

CREATE OR REPLACE VIEW csr567_mutation_oos_gate AS
SELECT
  cycle_id,
  parent_strategy_id,
  child_strategy_id,
  n_oos,
  sharpe_child - sharpe_parent AS d_sharpe,
  pf_child - pf_parent AS d_pf,
  worst5_child - worst5_parent AS d_worst5,
  CASE
    WHEN coalesce(n_oos, 0) < 30 THEN 'INSUFFICIENT_EVIDENCE'
    WHEN sharpe_child > sharpe_parent
         AND pf_child >= pf_parent
         AND coalesce(worst5_child, 0) >= coalesce(worst5_parent, 0)  -- less negative OK
      THEN 'VALIDATED_OOS_WIN'
    ELSE 'VALIDATED_REJECT'
  END AS mutation_label,
  CASE WHEN adopted THEN TRUE ELSE FALSE END AS was_adopted,
  FALSE AS live_allowed,
  FALSE AS auto_apply_allowed
FROM mutation_cycle_log;

-- Safety audit: rejected bad candidates count as PASS for safety, not IMPROVED
CREATE OR REPLACE VIEW csr567_mutation_cycle_safety AS
SELECT
  cycle_id,
  count(*) AS n_candidates,
  sum(CASE WHEN mutation_label = 'VALIDATED_REJECT' THEN 1 ELSE 0 END) AS n_reject,
  sum(CASE WHEN mutation_label = 'VALIDATED_OOS_WIN' THEN 1 ELSE 0 END) AS n_oos_win,
  sum(CASE WHEN mutation_label = 'INSUFFICIENT_EVIDENCE' THEN 1 ELSE 0 END) AS n_thin,
  sum(CASE WHEN was_adopted THEN 1 ELSE 0 END) AS n_adopted,
  CASE
    WHEN sum(CASE WHEN was_adopted AND mutation_label != 'VALIDATED_OOS_WIN' THEN 1 ELSE 0 END) > 0
      THEN 'SAFETY_FAIL_ADOPTED_NON_WIN'
    WHEN sum(CASE WHEN was_adopted THEN 1 ELSE 0 END) = 0
      THEN 'SAFETY_PASS_NO_ADOPT'
    ELSE 'SAFETY_PASS_OOS_ONLY'
  END AS safety_status,
  'improvement_capability_separate' AS note
FROM csr567_mutation_oos_gate
GROUP BY 1;

-- ---------------------------------------------------------------------------
-- 2. EmaTrend RESEARCH_PAUSE registry
-- ---------------------------------------------------------------------------
-- Manual / config row:
-- strategy_id='EmaTrend_strat_aa70771f' status='RESEARCH_PAUSE'
-- reason='INSUFFICIENT_EVIDENCE+NO_IMPROVEMENT' resume=regime_decomp_first

-- ---------------------------------------------------------------------------
-- 3. UMM loss-shape (not win-rate) — reinforce CSR-566
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW csr567_umm_loss_shape_hour AS
SELECT
  date_trunc('hour', ts) AS hour_jst,
  count(*) AS n,
  round(avg(CASE WHEN pnl_bp > 0 THEN 1.0 ELSE 0.0 END), 4) AS win_rate,
  round(avg(CASE WHEN pnl_bp > 0 THEN pnl_bp END), 4) AS avg_win_bp,
  round(avg(CASE WHEN pnl_bp <= 0 THEN pnl_bp END), 4) AS avg_loss_bp,
  round(quantile_cont(pnl_bp, 0.05), 4) AS p95_loss_bp,
  round(sum(CASE WHEN hard_stop THEN pnl_bp ELSE 0 END), 4) AS hard_stop_bp,
  round(avg(inventory_age_sec), 4) AS mean_inventory_age,
  round(avg(mae_bp), 4) AS mean_mae_bp,
  round(avg(spread_at_entry_bp), 4) AS mean_spread_entry_bp,
  round(sum(pnl_bp), 4) AS sum_bp
FROM mm_trade_cf_log
WHERE strategy_family = 'UMM' OR strategy_id LIKE '%umm%'
GROUP BY 1
ORDER BY 1;

-- FORBIDDEN: declare IMPROVED from in-sample mutation SR alone
-- FORBIDDEN: 4th EMA span search on EmaTrend while RESEARCH_PAUSE
-- FORBIDDEN: PEG promotion without BASE vs BASE+Aging Shadow ablation
-- FORBIDDEN: LIVE / ENFORCE / auto_apply from this SQL
