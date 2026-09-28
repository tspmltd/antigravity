#!/usr/bin/env python3
"""CSR-544 P1 — Latency × Adverse × Inventory DuckDB SQL templates.

Rules:
  - HardStop = exit_reason == 'HARD_STOP' (NEVER infer from realized_pnl <= -4.8)
  - Adverse bands: LOW <40 / MID <60 / HIGH >=60 (aligns with P0 Adverse>=60)
  - Latency = CONDITION slice only — no latency_cost_bp in Actual identity
  - Actual: Gross Spread + Adverse/Markout + Inventory + Exit + Residual = Realized
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

# SQL is intentional template — bind table names when UMM trade join exists
SQL_TRADE_ENRICH = r"""
-- CSR-544: require exit_reason from strategy log; do not invent HardStop from PnL
SELECT
  t.trade_id,
  t.ts,
  t.strategy,
  t.side,
  t.hold_ladder,
  t.hold_sec,
  t.current_pnl_bp,
  t.mae_bp,
  t.mfe_bp,
  t.pnl_slope_5s,
  t.gross_spread_bp,
  t.adverse_markout_bp,
  t.ae_1s_bp,
  t.ae_3s_bp,
  t.adverse_score,
  t.latency_ms,
  t.realized_pnl_bp,
  t.exit_reason,
  CASE WHEN t.exit_reason = 'HARD_STOP' THEN 1 ELSE 0 END AS is_hard_stop,
  CASE
    WHEN t.adverse_score < 40 THEN 'LOW'
    WHEN t.adverse_score < 60 THEN 'MID'
    ELSE 'HIGH'
  END AS adverse_slice,
  CASE
    WHEN t.latency_ms <= 40 THEN 'LOW'
    WHEN t.latency_ms <= 80 THEN 'MID'
    WHEN t.latency_ms <= 150 THEN 'HIGH'
    ELSE 'VERY_HIGH'
  END AS latency_slice
FROM umm_trades_enriched AS t
WHERE t.strategy = 'UMM'
"""

SQL_3AXIS = r"""
-- Condition slices only; NO latency_cost_bp column
SELECT
  latency_slice,
  adverse_slice,
  hold_ladder AS ladder,
  COUNT(*) AS n,
  AVG(gross_spread_bp) AS spread_per_trade,
  AVG(ae_1s_bp) AS ae_1s,
  AVG(realized_pnl_bp) AS pnl_per_trade,
  SUM(realized_pnl_bp) AS pnl_sum_bp,
  AVG(is_hard_stop) * 100.0 AS hs_rate_pct,
  AVG(mae_bp) AS mae_mean,
  AVG(mfe_bp) AS mfe_mean
FROM umm_trades_enriched_view
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""

SQL_ACTUAL_IDENTITY = r"""
-- Actual PnL identity (Latency NOT included)
SELECT
  SUM(gross_spread_bp) AS gross_spread,
  SUM(adverse_markout_bp) AS adverse_markout,
  SUM(inventory_holding_bp) AS inventory_holding,
  SUM(exit_effect_bp) AS exit_effect,
  SUM(residual_bp) AS residual,
  SUM(realized_pnl_bp) AS realized,
  SUM(gross_spread_bp) + SUM(adverse_markout_bp) + SUM(inventory_holding_bp)
    + SUM(exit_effect_bp) + SUM(residual_bp) - SUM(realized_pnl_bp) AS identity_residual,
  ABS(
    SUM(gross_spread_bp) + SUM(adverse_markout_bp) + SUM(inventory_holding_bp)
    + SUM(exit_effect_bp) + SUM(residual_bp) - SUM(realized_pnl_bp)
  ) < 1e-6 AS identity_ok
FROM umm_trades_enriched_view
"""

FORBIDDEN_SQL_PATTERNS = [
    "realized_pnl_bp <= -4.8",
    "latency_cost_bp",
    "WHEN realized_pnl_bp <= -4.8 THEN 1",
]


def build_lock() -> Dict[str, Any]:
    return {
        "csr": 544,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "P1 3-axis Attribution SQL templates",
        "hard_stop_rule": "exit_reason = 'HARD_STOP' ONLY",
        "adverse_bands": {"LOW": "<40", "MID": "40–60", "HIGH": ">=60"},
        "latency_role": "CONDITION_SLICE_ONLY",
        "actual_identity": "Gross+AdverseMarkout+Inventory+Exit+Residual=Realized",
        "sql": {
            "enrich": SQL_TRADE_ENRICH,
            "three_axis": SQL_3AXIS,
            "actual_identity": SQL_ACTUAL_IDENTITY,
        },
        "forbidden_sql_patterns": FORBIDDEN_SQL_PATTERNS,
        "status": "TEMPLATE_READY_NEED_UMM_TRADE_JOIN",
        "gov": {"LIVE": "OFF", "WIRE": "NO", "ENFORCE": 0},
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr544_3axis_attribution_sql_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    sql_path = OUT / "2026-09-28_csr544_3axis_attribution.sql"
    sql_path.write_text(
        "-- CSR-544 P1 templates\n\n"
        + "-- === ENRICH ===\n"
        + SQL_TRADE_ENRICH
        + "\n\n-- === 3AXIS ===\n"
        + SQL_3AXIS
        + "\n\n-- === IDENTITY ===\n"
        + SQL_ACTUAL_IDENTITY
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "json": str(path), "sql": str(sql_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
