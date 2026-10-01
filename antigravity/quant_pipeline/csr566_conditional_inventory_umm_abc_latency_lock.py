#!/usr/bin/env python3
"""CSR-566 — 21:00–07:00 add-on: conditional Inventory@30s · UMM A/B/C · latency-clean.

Falsifies blanket "always unwind @30s". Keeps @30s as research primary horizon
but redirects P0 to state identification + UMM loss attribution + latency split.

WIRE=NO · ENFORCE=0 · LIVE=BLOCKED · FROZEN · MANUAL-ONLY · Economic PASS=NO
Parquet path: orderbook_micro ⊥ fusion_log ⊥ execution_log → DuckDB → only then LIVE.
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
        "csr": 566,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Conditional Inventory@30s · UMM A/B/C attribution · latency-clean · PEG Aging-only",
        "phase": "OBSERVE_RESEARCH",
        "supersedes_partial": {
            "csr565_blanket_30s_hypothesis": (
                "CSR-565 kept @30s primary; CSR-566 falsifies 'always improve @30s' "
                "and moves target from horizon to inventory state"
            ),
        },
        "window_addon_jst": "2026-10-01T21:00 – 2026-10-02T07:00 (CTRL hourly @30s)",
        "evidence": {
            "ctrl_30s_10obs": {
                "n_hours": 10,
                "sum_delta_bp": 104.90,
                "mean_delta_bp": 10.49,
                "hours_positive": "6/10",
                "hourly_delta_bp": {
                    "23:00": -2.69,
                    "00:00": -23.86,
                    "01:00": 32.30,
                    "02:00": 32.25,
                    "03:00": -5.84,
                    "04:00": -5.05,
                    "05:00": 18.83,
                    "06:00": 35.84,
                    "07:00": 3.73,
                },
                "verdict": "conditional effect — NOT always improve",
                "decisive_worsen": {
                    "00:00": {
                        "actual_bp": -213.84,
                        "virtual30_bp": -237.70,
                        "delta_bp": -23.86,
                    },
                    "23:00": {
                        "actual_bp": -120.85,
                        "virtual30_bp": -123.54,
                        "delta_bp": -2.69,
                    },
                },
                "decisive_improve": {
                    "01:00": {"actual_bp": -73.68, "virtual30_bp": -41.38, "delta_bp": 32.30},
                    "02:00": {"actual_bp": -127.59, "virtual30_bp": -95.34, "delta_bp": 32.25},
                },
                "p0_redirect": (
                    "FROM: unwind at 30s rule  →  TO: which inventory states make "
                    "30s unwind valuable"
                ),
            },
            "umm": {
                "latest_07h_24h_bp": -644.95,
                "trades": 2263,
                "win_rate_pct": 54,
                "mid_window_worst_bp": -1001.10,
                "mid_window_trades": 3988,
                "win_rate_stable_pct": "54-55",
                "interpretation": (
                    "WR maintained while large −EV → first look avg_win/avg_loss/"
                    "HARD_STOP/inventory_age/tail — not directional hit-rate"
                ),
                "l0_collapse_counterexample": {
                    "23:00_L0_bp": -84.91,
                    "00:00_L0_bp": -165.60,
                    "meaning": "NOT inventory-hold-only; entry/quote quality can fail at L0",
                },
                "attribution_split": {
                    "A_entry_quote_quality": "L0 already negative in hour",
                    "B_inventory_holding": "L0≥0 but L1+ deteriorates",
                    "C_hard_stop": "HS rate / HS loss share rises",
                },
            },
            "tf2bp": {
                "07h_24h_bp": 121.77,
                "trades": 213,
                "wr_pct": 51,
                "role": "FROZEN baseline comparator = YES · stable validated alpha = NOT YET",
            },
            "peg_v3": {
                "source": "Discord Observation peg_v3_obs_v1 · 2026-10-01 11:00 → 2026-10-02 07:00",
                "be_exits_all_hours": 0,
                "fake_skip_all_hours": 0,
                "aging_exits_24h_stats_path": [
                    12, 14, 2, 7, 12, 14, 29, 37, 40, 49, 52, 56, 68,
                    113, 144, 155, 168, 187, 2, 6, 9,
                ],
                "note_aging_path": (
                    "rolling 24h AgingExits; dip to 2 at 13:00 and 05:00 is window roll, "
                    "not BE/Fake activation"
                ),
                "delta_24h_vs_baseline_bp_path": [
                    -542.14, -548.75, -558.71, -620.81, -655.08, -644.15, -596.46,
                    -640.88, -626.48, -581.86, -539.03, -381.29, -416.93, -390.77,
                    -448.68, -437.09, -456.49, -498.30, -536.28, -546.30, -546.25,
                ],
                "07h00_lock": {
                    "peg_24h_bp": -424.48,
                    "tf2bp_24h_bp": 121.77,
                    "delta_bp": -546.25,
                    "peg_wr_pct": 10,
                    "tf2bp_wr_pct": 51,
                },
                "ae_vs_pnl_paradox": (
                    "AE_1s/AE_3s often report green vs Baseline while 24h PnL Δ stays "
                    "≈−400..−650bp — do NOT promote on AE alone; friction/capture dominates"
                ),
                "capture_rate_artifact": (
                    "Capture Rate swings to −hundreds/−thousands % — treat as broken KPI "
                    "for this session; not an Economic PASS input"
                ),
                "1h_green_hours_do_not_rescue": (
                    "occasional 1h Δ+ when Baseline also weak (12,16,17,21,22) — "
                    "24h still deeply behind; no promotion"
                ),
                "verdict": (
                    "REJECT PROMOTION. PEG_v3 is AgingGuard-only in practice "
                    "(BE=0 FakeSkip=0 every hour). Score as Aging friction experiment, "
                    "NOT Aging+BE3+FakeLiq composite."
                ),
                "ablation_priority": [
                    "Baseline (TF2BP FROZEN)",
                    "Baseline + AgingGuard only",
                    "Baseline + BE3 only (blocked until fires>0)",
                    "Baseline + FakeLiquidity only (blocked until skips>0)",
                ],
                "be_fake_economic_eval": "blocked until fire counts exist",
                "executive_banner_override": (
                    "Discord still says 検証継続中 — research lock upgrades to "
                    "REJECT PROMOTION / ABLATE Aging first"
                ),
            },
            "council_15h_to_07h": {
                "schema_reported": "csr558_v1",
                "note_schema": (
                    "Hourly Discord still csr558_v1 — CSR-564 freshness fields not yet "
                    "visible in reports; keep CSR-564 gate as infra; do not teach on final"
                ),
                "micro_pressure": "NONE almost every hour (imb≈0)",
                "fakebo_hours": {
                    "effective": ["18:00 BUY", "20:00 SELL"],
                    "redundant": ["19:00 SELL→HOLD", "04:00 BUY→HOLD"],
                },
                "strength_clip_1_00_examples_yen": [
                    -4998, 5762, -3112, -6277, 4165, 10729, 11068, -3781,
                    15001, 13808, 8667, -3701,
                ],
                "confidence_gate_hours": ["15:00", "17:00", "19:00", "04:00"],
                "adverse": "SHADOW_OFF score=NULL valid=False every hour",
                "librarian_2026_10_02": {
                    "portfolio": "MIXED",
                    "TrendFollowResearch": "NOT_USEFUL — tip×threshold/horizon redesign advise",
                    "FactorICResearch": "USEFUL — P0 MM Inventory Risk spread×inventory_proxy",
                    "forbid": "Economic PASS · LIVE · auto weights · frozen param auto change",
                },
            },
            "arena_reset_boundary": {
                "old_run_ended_approx": "2026-10-02T04:17 JST",
                "old_run_stats": {"hours": 70.92, "trades": 3104, "bp": 91.24},
                "new_run_at_07_17": {"hours": 3.00, "trades": 199, "bp": 103.27},
                "forbid": "concatenate old+new as one Arena series",
                "required_keys": ["arena_run_id", "strategy_id", "run_started_at"],
                "new_run_leaders_07_17": {
                    "SpreadCaptureMM": {"bp": 42.46, "trades": 56, "wr_pct": 79},
                    "MicroSpreadMM": {"bp": 32.01, "trades": 45, "wr_pct": 78},
                    "GridMM_strat_1ac224f3": {"bp": 20.32, "trades": 20, "wr_pct": 85},
                },
                "promotion_live": False,
                "promotion_candidate_yes": ["SpreadCaptureMM", "MicroSpreadMM"],
                "grid_note": "new-run observe only — 3h too short for LIVE",
            },
            "micro_latency_04h": {
                "ticks": 1664,
                "latency_mean_ms": 103.45,
                "latency_p90_ms": 155.1,
                "design_threshold_ms": 80,
                "meaning": "04:00 closed hour is high-latency research sample vs design 80ms",
                "tip_density_04h": {
                    "tip_thin_bid_x_one_way_sell_pct": 12.9,
                    "tip_thin_ask_x_one_way_buy_pct": 8.6,
                    "pressure_none_pct": 100,
                },
            },
        },
        "pdca": {
            "P0a": {
                "name": "UMM attribution A/B/C",
                "split": [
                    "A Entry / quote quality (L0 already −)",
                    "B inventory hold loss (L0 ok → L1+ worsen)",
                    "C HARD_STOP loss",
                ],
                "metrics": [
                    "avg_win", "avg_loss", "HARD_STOP loss",
                    "inventory_age", "tail_loss", "HS_rate",
                ],
            },
            "P0b": {
                "name": "Latency-clean research dataset",
                "primary": "latency_ms <= 80",
                "secondary_report": "latency_ms > 80",
                "applies_to": ["Micro tip", "Trend fwd", "FakeBO CF", "Inventory CF joins"],
                "why": "avoid confounding feature edge with data delay",
            },
            "P1a": {
                "name": "Inventory State Classifier",
                "not": "fixed 30s unwind rule",
                "features": [
                    "hold_ladder", "inventory_age", "side", "unrealized_pnl",
                    "spread", "regime", "micro_tip",
                ],
                "label": "Δbp Virtual@30 − Actual (primary horizon still 30s)",
                "forbid": ["adopt best horizon after peeking", "ENFORCE from classifier"],
            },
            "P1b": {
                "name": "PEG ablation",
                "first": "Baseline + AgingGuard only",
                "defer_economic": ["BE3", "FakeLiq"] ,
                "until": "fire counts sufficient",
            },
            "P2": {
                "name": "Arena winner validation",
                "candidates": ["MicroSpreadMM", "SpreadCaptureMM"],
                "observe": ["GridMM strat_1ac224f3"],
                "require_arena_run_id": True,
                "live_now": False,
            },
            "P3": {
                "name": "Tip pressure SHADOW",
                "path": "feature → forward-mid → side-adjusted return → latency-clean",
                "features": [
                    "tip_thin_bid × one_way_sell",
                    "tip_thin_ask × one_way_buy",
                ],
                "keep_existing_pressure": True,
            },
            "P4": {
                "name": "FakeBO decompose",
                "subtypes": [
                    "cancel + no taker",
                    "cancel + with taker",
                    "tip depletion",
                    "subsequent refill",
                ],
            },
        },
        "status_board": {
            "UMM": "INVESTIGATE / HIGH PRIORITY",
            "TF2BP": "FROZEN BASELINE",
            "PEG_v3": "REJECT PROMOTION / ABLATE",
            "Inventory_Control": "RESEARCH CONTINUE (state classifier)",
            "Micro_tip": "SHADOW RESEARCH",
            "Arena_winners": "VALIDATE (per arena_run_id)",
            "Adverse": "SHADOW/OFF",
            "Auto_tuning": "OFF",
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "BLOCKED",
        },
        "absolute_forbid_now": [
            "blanket 30s force unwind because cumulative Δ still +",
            "claim inventory-only root cause (L0 collapse counterexample)",
            "score PEG as Aging+BE+Fake while BE/Fake fire=0",
            "merge Arena pre/post 04:17 into one series",
            "LIVE promote Arena winners on 3h new run",
            "mix latency>80ms into primary tip/FakeBO/Trend CF without split",
            "WIRE=YES / ENFORCE=1 / auto_apply / Adverse NULL→0→SAFE",
        ],
        "architecture": {
            "order": "observe → Parquet (micro/fusion/execution separated) → DuckDB → LIVE only after proof",
            "duckdb_role": "researcher not decider",
            "freshness_prereq": "CSR-564 State Freshness Gate still required before teach",
        },
        "sql": "sql/csr566_conditional_inventory_umm_abc_latency.sql",
        "core": (
            "Biggest change: blanket 30s unwind fixing UMM is falsified. "
            "@30s CF still net+ but worsens 23/00/03/04h — learn STATES not seconds. "
            "UMM needs A/B/C split; PEG is AgingGuard-only until ablation; "
            "Arena needs run_id; Micro/CF primary dataset = latency≤80ms. FROZEN."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build()
    path = OUT / "2026-10-02_csr566_conditional_inventory_umm_abc_latency_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path), "core": lock["core"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
