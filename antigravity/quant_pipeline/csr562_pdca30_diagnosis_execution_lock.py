#!/usr/bin/env python3
"""CSR-562 — 成績改善PDCA 3.0 確定診断＆実行計画ロック.

Integrates Council / Arena / UMM / PEG_v3 / CSR-521 (2026-09-24〜09-30).
Parent: CSR-561 (PDCA3 theme) · CSR-548/545 (PEG) · CSR-560 (Council) · CSR-539 (Adverse).

Dual P0 focus (user mandate):
  P0-A CSR-521 L2/L3 conditional hazard CF (@20/@30 · NOT fixed adopt)
  P0-B PEG Entry-only vs AgingGuard friction (A/B/C/D orthogonal)

GOV: WIRE=NO · ENFORCE=0 · LIVE=OFF · auto_apply=OFF · NO PARAM CHANGE
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))
SQL_DIR = Path(__file__).resolve().parent / "sql"

DIAGNOSIS = {
    "bottleneck_1_umm_inventory_tail": {
        "phenomenon": "WR 54–60% but 24h PnL −400..−770bp repeated",
        "cause": "Hold Ladder L2/L3 long-hold turns mild 1s AE into −15..−21bp MAE tails",
        "evidence": "Worst10 18–20h; CSR-521 @30s CF often +20bp+ on L2+L3",
        "driver": "CSR-521 conditional unwind = primary return-improvement driver",
        "not": "direction alpha rewrite / UMM kill-all",
    },
    "bottleneck_2_peg_aging_friction": {
        "phenomenon": "PEG_v3 vs Baseline Δ −250..−400bp persistent",
        "false_positive": "AE_3s +1.20bp / capture noise ≠ economic win",
        "true_suspect": "AgingGuard 3s taker cut overfires (35–99); BE3=0 FakeSkip=0",
        "mechanism": "good inventory forced out before +2.5bp mean-revert → spread friction death",
        "required": "A/B/C/D decompose + AgingGuard CF value (actual − hold_cf)",
        "counter_audit": "99→0 exits = provenance (window_id/process_start), not 'fixed'",
    },
    "bottleneck_3_council_distortion": {
        "trend_saturation": "35/56 = 62.5% confidence=1.00 via min(1,diff/3000)",
        "micro_dead": "imbalance ±0.05 vs threshold ±0.25 → pressure NONE 100%",
        "micro_alive": "tip_thin×one_way + FakeBO veto frequent — SHADOW/CF only",
        "adverse": "looks strong but CSR-539 integrity_hold → Economic PASS=NO",
    },
}

ARCHITECTURE = {
    "layers": [
        "market → TrendFollow(raw/clipped) → Candidate → Confidence 0.65 replay",
        "market → Micro tip_thin/taker features (SHADOW) → Decision quality",
        "Decision → FakeBO VETO (avoided-loss CF) + Spread/Adverse Gate (539 audit)",
        "Maker/Taker exec → inventory → CSR-521 Hazard Exit L2/L3@20-30s conditional",
    ],
    "not_model": "4AGENT equal vote / W_PRESSURE auto / Fusion LIVE",
}

PDCA = {
    "Plan": {
        "status": "LOCKED",
        "items": [
            "CSR-521: forbid blanket 10s/30s; L0/L1 keep; L2/L3 + MAE/flow degrade only",
            "PEG A/B/C/D orthogonal (Baseline / Entry-only / Entry+Aging / Full)",
            "Trend strength_raw vs clipped separate logging",
        ],
    },
    "Do": {
        "status": "OBSERVE_LOG_EXTEND_ONLY",
        "safety": ["WIRE=NO", "ENFORCE=0", "LIVE=OFF", "auto_apply=OFF"],
        "log_csr521": [
            "hold_age", "mae_history", "side", "ladder",
            "Virtual_Unwind_PnL_10/20/30", "regime", "trade_id",
        ],
        "log_aging": [
            "process_start_ts", "stats_window_id", "state_reset_count",
            "aging_exits_window", "aging_exits_lifetime",
            "cf_pnl_5/10/20/30s per aging exit",
        ],
        "log_fakebo": [
            "decision IN (BUY,SELL) AND veto=TRUE → fwd 1/3/10/30s CF",
        ],
    },
    "Check": {
        "status": "PENDING_DUCKDB",
        "csr521_pass": [
            "multi-regime (trend/range/high_vol)",
            "L2+L3 cohort Net EV@30s > 0",
            "HardStop rate ↓",
            "worst tail (|MAE|≥15bp) ↓",
            "median Δ>0 AND CF win≥60%",
            "L0 protect audit intact",
            "Discord hour-means alone ≠ PASS",
        ],
        "fakebo_pass": [
            "Effective veto only",
            "CF EV of vetoed entries < 0 (avoided loss)",
            "n≥20 + OOS (CSR-560)",
        ],
        "arena_contrast": [
            "MicroSpread strat_4d3f2c9f vs strat_a5d8ae20",
            "spread_at_entry · hold · MAE · exit_reason",
        ],
    },
    "Act": {
        "status": "CLOSED",
        "rules": [
            "Arena winners FROZEN — no retune",
            "CSR-521: after DuckDB consistency → SHADOW sim → Council approve → UMM exit wire only",
            "Adverse: freeze Economic/Council until CSR-539 integrity_ok",
            "No PEG_v4 · no fixed Aging 3→5 · no imb .25→.10",
        ],
    },
}

PRIORITY: List[Dict[str, Any]] = [
    {
        "pri": "P0-A",
        "item": "CSR-521 conditional Exit CF",
        "work": "L2/L3 × hold × MAE → @20s/@30s CF",
        "done": "tail≤−15bp cut significant · Net EV>0 · multi-regime",
        "sql": "sql/csr562_p0_l23_conditional_hazard_cf.sql",
        "focus": True,
    },
    {
        "pri": "P0-B",
        "item": "PEG Entry-only + Aging friction",
        "work": "A/B/C/D isolate · AgingGuard CF value",
        "done": "Entry-only EV vs Baseline signed · Aging Guard Value signed",
        "sql": "sql/csr562_p0_peg_abcd_aging_cf.sql",
        "focus": True,
    },
    {
        "pri": "P1",
        "item": "FakeBO avoided-loss CF",
        "work": "decision BUY/SELL ∧ veto · fwd CF",
        "done": "Avoided Loss > 0 · n≥20+OOS",
        "sql": "sql/csr560_pdca22_council_direction_fakebo_confidence.sql",
        "focus": False,
    },
    {
        "pri": "P1",
        "item": "TrendFollow raw logging + 0.65 replay",
        "work": "strength_raw/clipped · threshold shadow grid",
        "done": "1.00 saturation diagnosed · threshold curve",
        "sql": "sql/csr560_pdca22_council_direction_fakebo_confidence.sql",
        "focus": False,
    },
    {
        "pri": "P2",
        "item": "Adverse CSR-539 integrity",
        "work": "score missing/default30 · Capture provenance",
        "done": "integrity_hold_csr539 cleared",
        "sql": None,
        "focus": False,
    },
]

FORBIDDEN = [
    "UMM全廃",
    "Arena winners retune",
    "fixed 10s unwind adopt",
    "fixed 30s unwind adopt",
    "Aging 3s→5s immediate",
    "imbalance .25→.10 immediate",
    "PEG_v4",
    "W_PRESSURE auto_apply",
    "Adverse LIVE / Council wire",
    "Capture Rate raw for decision",
    "LIVE ON / WIRE=YES / ENFORCE=1",
    "final_action as teacher",
]


def build() -> Dict[str, Any]:
    return {
        "csr": 562,
        "title": "成績改善PDCA 3.0 確定診断＆実行計画",
        "parent": ["CSR-561", "CSR-560", "CSR-559", "CSR-548", "CSR-545", "CSR-539", "CSR-521"],
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "window": "2026-09-24..2026-09-30",
        "primary_source": "user_pdca30_diagnosis_agenda_20260930",
        "m365_corroboration": False,
        "theme": {
            "from": "アルファを増やす / Council Fusion 優先",
            "to": "勝率維持のまま inventory/exit 摩擦を切る（521 + PEG分解）",
        },
        "diagnosis": DIAGNOSIS,
        "architecture": ARCHITECTURE,
        "pdca": PDCA,
        "priority_matrix": PRIORITY,
        "resource_focus": ["P0-A", "P0-B"],
        "forbidden": FORBIDDEN,
        "relation": {
            "CSR-561": "theme + Arena protect — retained",
            "CSR-562": "definitive PDCA3.0 diagnosis + dual-P0 execution SQL",
            "CSR-560": "Council FakeBO/Trend remains P1 lane",
        },
        "governance": {
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
            "AUTO_APPLY": "OFF",
            "Economic_PASS": "NO",
            "NO_PARAM_CHANGE": True,
            "FROZEN": [
                "Arena SpreadCapture/MicroSpread/InventorySkew",
                "TF2BP Baseline",
                "decision 0.65",
                "Aging 3s",
                "BE3 +3bp",
                "FakeLiq 0.40",
                "W_PRESSURE",
            ],
        },
        "core": (
            "PDCA3.0 P0 = (A) L2/L3 conditional Hazard CF @20/30s + "
            "(B) PEG Entry-only vs AgingGuard friction; Arena FROZEN; Act CLOSED."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build()
    path = OUT / "2026-09-30_csr562_pdca30_diagnosis_execution_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")

    sql_a = SQL_DIR / "csr562_p0_l23_conditional_hazard_cf.sql"
    sql_b = SQL_DIR / "csr562_p0_peg_abcd_aging_cf.sql"
    assert sql_a.exists() and "csr562_p0a_conditional_matrix" in sql_a.read_text(encoding="utf-8")
    assert sql_b.exists() and "csr562_p0b_abcd_deltas" in sql_b.read_text(encoding="utf-8")
    assert lock["pdca"]["Act"]["status"] == "CLOSED"
    assert lock["resource_focus"] == ["P0-A", "P0-B"]

    # Human-readable agenda stub (no DuckDB lake → Check=PENDING)
    agenda = {
        "csr": 562,
        "resource_focus": lock["resource_focus"],
        "next_do": [
            "Ensure mm_trade_cf_log populated with L2/L3 Virtual@10/20/30 + mae + side",
            "Stand up peg_variant_trade_log A/B/C/D OBSERVE streams (ENFORCE=0)",
            "Wire peg_aging_exit_cf_log provenance fields for 99→0 audit",
            "Run csr562 P0-A/P0-B views when lake ready — no param change",
        ],
        "check_status": "PENDING_LAKE",
        "governance": lock["governance"],
    }
    agenda_path = OUT / "2026-09-30_csr562_pdca30_p0_focus_agenda.json"
    agenda_path.write_text(json.dumps(agenda, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "lock": str(path),
        "agenda": str(agenda_path),
        "sql_a": str(sql_a),
        "sql_b": str(sql_b),
        "focus": lock["resource_focus"],
        "core": lock["core"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
