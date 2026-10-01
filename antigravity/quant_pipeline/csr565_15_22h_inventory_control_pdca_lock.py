#!/usr/bin/env python3
"""CSR-565 — 15:00–22:11 OBSERVE diagnosis · PDCA reorder to Inventory Control P0.

Window evidence (user Discord/CTRL 2026-10-01 ~15:00–22:11 JST):
  - CTRL Would-Unwind @30s: mean +12.28 / med +12.96 / 7/8h+ / ΣΔ +98.24 bp
  - CTRL @10s: mean +46.28 / med +39.72 / 8/8+ but high variance → NOT adopt 10s
  - UMM 24h −770 bp @ WR 54% → inventory exposure not entry hit-rate
  - TF2BP 1h sum −55.92 · PEG_v3 0/8h+ · AgingExits↑ BE=0 FakeSkip=0
  - Arena: SpreadCaptureMM / MicroSpreadMM relative strong; InventorySkew WR+/EV−

PDCA (research priority; CSR-564 freshness gate remains infra prerequisite):
  P0 Inventory Control (CTRL @30s primary · multi-horizon OBSERVE)
  P1 UMM Inventory Attribution (age×ladder×side×DD×horizon)
  P2 PEG Ablation (Aging / BE3 / FakeLiq separate)
  P3 SpreadCaptureMM / MicroSpreadMM promotion candidates (NOT LIVE)
  P4 Micro tip×one_way SHADOW (do not retune pressure thr)
  P5 FakeBO subtype redesign research

Acceptance Gate: P3-CTRL-01 Inventory Control Stability
WIRE=NO · ENFORCE=0 · LIVE=OFF · FROZEN · MANUAL-ONLY · Economic PASS=NO
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))


def build() -> dict:
    return {
        "csr": 565,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "15–22h OBSERVE diagnosis · Inventory Control P0 · PEG ablation · Arena candidates",
        "phase": "OBSERVE_RESEARCH",
        "window_jst": "2026-10-01T15:00–22:11 (approx; hourly Discord/CTRL)",
        "infra_prerequisite": {
            "csr564_freshness_gate": "STILL REQUIRED before teaching any CF labels",
            "note": "CSR-564 freshness ≠ superseded; economic P0 is now Inventory Control",
        },
        "portfolio_diagnosis": {
            "strong": ["SpreadCaptureMM", "MicroSpreadMM"],
            "caution": ["TF2BP baseline (frozen comparator ≠ profitable truth)"],
            "problem_dense": ["UMM", "InventorySkewMM", "TF2BP_PEG_v3"],
            "most_promising_research": "Inventory unwind control (CTRL Would-Unwind)",
            "new_alpha_candidate": "tip_thin × one_way taker (SHADOW)",
            "do_not_use": ["Adverse LIVE", "auto weights", "auto parameter optimization"],
        },
        "evidence": {
            "ctrl_would_unwind_30s_8h": {
                "mean_delta_bp": 12.28,
                "median_delta_bp": 12.96,
                "hours_positive": "7/8",
                "sum_delta_bp": 98.24,
                "primary_horizon": 30,
                "reason_primary_30": "CTRL design priority; avoid cherry-picking max of 10/20/30/45/60",
            },
            "ctrl_would_unwind_10s_8h": {
                "mean_delta_bp": 46.28,
                "median_delta_bp": 39.72,
                "hours_positive": "8/8",
                "hourly_deltas_bp": [26.38, 30.54, 90.21, 14.07, 48.90, 65.69, 79.33, 15.08],
                "adopt_10s_now": False,
                "why_not": "high variance + multiple-testing if pick best horizon; 30s remains research primary",
            },
            "umm": {
                "latest_24h_bp": -770.25,
                "trades_24h": 3669,
                "win_rate_pct": 54.0,
                "umm_1h_15_22_sum_bp": -159.34,
                "umm_1h_mean_bp": -19.92,
                "umm_1h_positive_hours": "1/8",
                "interpretation": (
                    "WR 54% with large negative EV → inventory exposure / hold-tail, "
                    "not necessarily entry hit-rate failure"
                ),
                "investigate_before_spread_threshold": [
                    "inventory_age",
                    "ladder",
                    "side",
                    "drawdown",
                    "unwind_horizon",
                ],
            },
            "tf2bp": {
                "1h_series_bp": [193.04, 218.22, 131.33, 132.80, 118.13, 69.54, 47.75, -71.70],
                "note": "series mixes rolling 24h-at-hour vs 1h — do not treat as independent cum PnL without window audit",
                "1h_sum_bp_user": -55.92,
                "1h_mean_bp_user": -6.99,
                "1h_positive_hours": "3/8",
                "baseline_role": "frozen comparator ONLY — not profitable truth",
            },
            "peg_v3": {
                "1h_bp": [-5.87, -3.72, -36.45, -11.64, -16.22, -6.47, -0.02, -9.29],
                "mean_bp_h": -11.21,
                "positive_hours": "0/8",
                "latest_24h_peg_bp": -412.12,
                "latest_24h_tf2bp_bp": -71.70,
                "delta_vs_tf2bp_bp": -340.42,
                "aging_exits_path": [13, 14, 29, 37, 45, 51, 52, 57],
                "be_exits": 0,
                "fake_skip": 0,
                "verdict": "do not improve PEG_v3 as monolith — ablate AgingGuard / BE3 / FakeLiq",
            },
            "arena": {
                "MicroSpreadMM_24h": {"bp": 89.71, "trades": 156, "wr_pct": 69},
                "SpreadCaptureMM_24h": {"bp": 83.06, "trades": 361, "wr_pct": 64},
                "InventorySkewMM_24h_approx": {"bp": -63.07, "trades": 411, "wr_pct": 59},
                "promotion_live_now": False,
                "p1_promotion_candidates": ["SpreadCaptureMM", "MicroSpreadMM"],
                "inventory_skew_needs": [
                    "avg_win", "avg_loss", "tail_loss", "inventory_age",
                    "inventory_side", "MAE", "HARD_STOP_rate",
                ],
            },
            "microstructure": {
                "fake_bo_share_pct_range": [32, 48],
                "cancel_spike_share_pct_range": [31, 48],
                "pressure_none_share_pct_range": [94, 100],
                "tip_events_observed": [
                    "tip_thin_bid × one_way_sell",
                    "tip_thin_ask × one_way_buy",
                ],
                "imb_taker_confirm_often_zero": True,
                "next": "existing_pressure vs tip_thin×one_way forward-mid (NOT threshold tweak)",
                "fakebo_decompose": [
                    "cancel only",
                    "cancel + no taker",
                    "cancel + micro reversal",
                    "tip depletion + cancel",
                    "liquidity recovery / no recovery",
                ],
            },
        },
        "pdca": {
            "P0": {
                "name": "Inventory Control",
                "status": "OBSERVE · ENFORCE=0",
                "hypothesis": "L2/L3 long inventory → PnL worsen → virtual unwind improves",
                "primary_horizon_sec": 30,
                "compare_horizons_sec": [10, 20, 30, 45, 60],
                "forbid": ["adopt best horizon after peeking", "10s force unwind now"],
            },
            "P1": {
                "name": "UMM Inventory Attribution",
                "axes": ["inventory_age", "ladder", "side", "drawdown", "unwind_horizon"],
                "question": "bad entry vs bad holding?",
                "forbid": ["spread entry threshold tweak first"],
            },
            "P2": {
                "name": "PEG Ablation",
                "arms": [
                    "Baseline",
                    "Baseline+AgingGuard",
                    "Baseline+BE3",
                    "Baseline+FakeLiq",
                    "Baseline+AgingGuard+BE3",
                    "full PEG_v3",
                ],
                "forbid": ["monolithic PEG_v3 retune"],
            },
            "P3": {
                "name": "SpreadCaptureMM / MicroSpreadMM promotion candidates",
                "live_now": False,
                "checks": [
                    "regime×PnL", "spread_bucket×PnL", "hour×PnL", "side×PnL",
                    "MAE/MFE", "worst_1pct/5pct", "max_losing_streak", "turnover",
                ],
            },
            "P4": {
                "name": "Micro tip-pressure SHADOW",
                "features": [
                    "tip_thin_bid × one_way_sell",
                    "tip_thin_ask × one_way_buy",
                ],
                "keep_existing_pressure": True,
            },
            "P5": {
                "name": "FakeBO redesign research",
                "goal": "escape cancel-only dependence via subtype CF",
            },
        },
        "acceptance_gate": {
            "id": "P3-CTRL-01",
            "name": "Inventory Control Stability",
            "min_n_inventory_events": 1000,
            "requires": ["multiple hours", "multiple regimes"],
            "compare": ["Actual", "Virtual@10", "Virtual@20", "Virtual@30", "Virtual@45", "Virtual@60"],
            "primary": "Δbp at fixed primary horizon=30s",
            "secondary": ["worst_5pct", "HS_rate", "turnover_increase", "side_asymmetry"],
            "forbid": ["pick best horizon after seeing all", "ENFORCE from this gate alone"],
            "sql": "sql/csr565_p3_ctrl01_inventory_control_stability.sql",
            "economic_pass_from_gate": False,
        },
        "absolute_forbid_now": [
            "change to 10s forced unwind because mean@10s looks best",
            "LIVE SpreadCaptureMM / MicroSpreadMM because +80–90bp",
            "WIRE=YES / ENFORCE=1 / auto_apply",
            "Adverse NULL→0→SAFE",
            "treat TF2BP as profitable truth",
            "treat overlapping 1h windows as independent cumulative PnL without audit",
        ],
        "architecture": {
            "teacher_path": (
                "fusion_log + execution_log Parquet separate → realized_pnl teacher → "
                "DuckDB compare → only then consider execution wire"
            ),
            "duckdb_role": "researcher not decider",
            "gov": {
                "WIRE": "NO",
                "ENFORCE": 0,
                "LIVE": "OFF",
                "FROZEN": True,
                "MANUAL_ONLY": True,
                "auto_apply": "OFF",
                "Economic_PASS": "NO",
            },
        },
        "core": (
            "15–22h: Inventory Control Would-Unwind repeatedly improves (esp @30s primary); "
            "UMM WR+ with large −EV points to hold/inventory; PEG_v3 fails as monolith "
            "(Aging↑ BE/Fake silent) → ablate; Arena winners are SpreadCapture/MicroSpread "
            "as research promotion candidates only. No ENFORCE / no LIVE / no 10s adopt."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build()
    path = OUT / "2026-10-01_csr565_15_22h_inventory_control_pdca_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path), "core": lock["core"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
