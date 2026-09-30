#!/usr/bin/env python3
"""CSR-561 — 成績改善PDCA3 lock.

Theme shift (user BizChat 2026-09-30):
  NOT "add alpha" → "keep win-rate, cut UMM inventory tail loss"

Primary evidence (user-presented; M365 not re-fetched):
  - Arena FROZEN winners: SpreadCapture / MicroSpread / InventorySkew
  - CSR-521 @30s CF: 11/11 hourly + (mean ~+21bp); @10s high mean but unstable
  - L0 positive / L2+L3 negative pattern repeats
  - PEG still Baseline-behind; AgingGuard first suspect; BE/FakeLiq unobserved
  - Micro pressure=none 100%; tip×one_way SHADOW only
  - Council: Trend 1.00 saturation + FakeBO effective CF (CSR-560)
  - Adverse: strong-looking but CSR-539 integrity HOLD

Governance: WIRE=NO · ENFORCE=0 · LIVE=OFF · auto_apply=OFF · NO PARAM CHANGE
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))
SQL = Path(__file__).resolve().parent / "sql" / "csr561_pdca3_l23_inventory_cf.sql"

TIERS: List[Dict[str, Any]] = [
    {
        "id": "TIER_A_PROTECT",
        "items": [
            "Arena winners FROZEN (SpreadCaptureMM / MicroSpreadMM / InventorySkewMM)",
            "TF2BP Baseline FROZEN",
            "compare by strategy_id + parameter_hash (not family name)",
        ],
    },
    {
        "id": "TIER_B_P0_RESEARCH",
        "items": [
            "CSR-521 Inventory Control — L2/L3 conditional unwind CF",
            "primary horizons 20s/30s; 10s comparative only (NOT adopt fixed)",
            "L0 KEEP; L1 research; L4 separate hard-risk",
            "PASS needs medianΔ>0 · CF win-rate≥60% · tail MAE improve · multi-regime sign",
        ],
    },
    {
        "id": "TIER_C_PARALLEL",
        "items": [
            "P1 Arena winner/loser contrast (e.g. MicroSpread 4d3f2c9f vs a5d8ae20)",
            "P2 Adverse CSR-539 integrity (blocks Economic PASS)",
            "P3 Council Trend raw/clipped + FakeBO effective CF (CSR-560)",
            "P4 PEG Entry-only / AgingGuard CF / counter provenance (OBSERVE)",
            "P5 Micro tip_thin×one_way SHADOW feature only (no ENFORCE)",
        ],
    },
]

FORBIDDEN = [
    "UMM全廃",
    "Arena winners retune",
    "fixed 10s unwind adopt",
    "fixed 30s unwind adopt",
    "imbalance .25→.10 immediate",
    "PEG_v4",
    "W_PRESSURE auto-opt",
    "Adverse LIVE",
    "auto_apply",
    "LIVE ON / WIRE=YES / ENFORCE=1",
]

CSR521_PASS = {
    "population": "ladder IN (L2,L3) · paired Actual vs Virtual",
    "primary_horizon": ["20s", "30s"],
    "comparative_horizon": ["10s", "45s", "60s"],
    "require": [
        "median Δ > 0",
        "CF win-rate > 60%",
        "worst-tail / HS rate improve vs Actual",
        "paired Actual vs CF on same trade_id",
        "sign stable across ≥2 regimes and multiple independent hours",
        "L0 mean actual remains non-destroyed (protect audit)",
    ],
    "not_pass": [
        "mean-only on 4–11 Discord hours",
        "blanket age>=30 exit",
        "mix Direction Alpha into Inventory lane",
    ],
}

PEG_DECOMP = {
    "variants": ["A Baseline", "B PEG Entry-only", "C Entry+Aging", "D Full"],
    "aging_cf_fields": [
        "actual_exit_pnl", "cf_hold_1s", "cf_hold_3s", "cf_hold_5s",
        "cf_hold_10s", "cf_hold_20s", "cf_hold_30s", "stats_window_id",
        "aging_exits_window", "aging_exits_lifetime", "state_reset_count",
    ],
    "note": "99→0 exits discontinuity = provenance audit, not 'fixed'",
}


def build_lock() -> Dict[str, Any]:
    return {
        "csr": 561,
        "parent": ["CSR-521", "CSR-548", "CSR-556", "CSR-559", "CSR-560"],
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "成績改善PDCA3 · Inventory tail cut · Arena protect",
        "primary_source": "user_bizchat_pdca3_logs_20260930",
        "m365_corroboration": False,
        "theme": {
            "from": "アルファを増やす",
            "to": "勝率を維持したまま UMM inventory tail loss を切る",
        },
        "classification": {
            "Arena_top_MM": "PROTECT_FROZEN",
            "CSR521_Inventory": "P0_RESEARCH",
            "TF2BP_Baseline": "FROZEN_CONTROL",
            "PEG_v3": "OBSERVE_DECOMPOSE_NOT_LIVE",
            "UMM": "LOSS_SOURCE_INVENTORY_NOT_DIRECTION",
            "Micro_pressure": "DEAD_LEGACY_THRESHOLD_NO_KNOB",
            "Micro_tip_one_way": "SHADOW_FEATURE",
            "FakeBO": "CF_CONTINUE_EFFECTIVE_ONLY",
            "Council_Adverse": "RESEARCH_NO_WIRE",
        },
        "tiers": TIERS,
        "csr521_pass_gates": CSR521_PASS,
        "peg_decomposition": PEG_DECOMP,
        "priority_order": [
            "P0 CSR-521 L2/L3 CF (20/30s primary)",
            "P1 Arena strategy_id winner/loser contrast",
            "P2 Adverse CSR-539 integrity",
            "P3 Council Trend calibration + FakeBO CF (CSR-560)",
            "P4 PEG Entry-only + Aging CF + provenance",
            "P5 Micro tip×one_way SHADOW",
            "LAST Fusion weights / W_PRESSURE",
        ],
        "forbidden": FORBIDDEN,
        "sql_path": "antigravity/quant_pipeline/sql/csr561_pdca3_l23_inventory_cf.sql",
        "relation_to_pdca2": {
            "CSR559": "architecture Plan remains; Do still not LIVE",
            "CSR560": "Council Direction/FakeBO SQL remains P3 lane",
            "CSR561": "promotes Inventory Control to PDCA3 P0 research",
        },
        "pdca3": {
            "Plan": "LOCKED",
            "Do": "OBSERVE_CF_COLLECT_ONLY",
            "Check": "PENDING_L23_COHORT",
            "Act": "CLOSED",
        },
        "governance": {
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
            "AUTO_APPLY": "OFF",
            "NO_PARAM_CHANGE": True,
            "Economic_PASS": "NO",
            "FROZEN": ["Arena winners", "TF2BP Baseline", "decision 0.65", "FakeBO thresholds"],
        },
        "core": (
            "PDCA3 P0 = cut degraded inventory (L2/L3) via conditional CF, "
            "protect Arena + Baseline, never adopt fixed 10s/30s from Discord means alone."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-30_csr561_pdca3_inventory_tail_arena_protect_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    assert SQL.exists()
    text = SQL.read_text(encoding="utf-8")
    assert "csr561_l23_cf_by_horizon" in text
    assert lock["pdca3"]["Act"] == "CLOSED"
    assert "fixed 30s unwind adopt" in FORBIDDEN
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "P0": lock["priority_order"][0],
        "sql": str(SQL),
        "gov": lock["governance"]["WIRE"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
