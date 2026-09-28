#!/usr/bin/env python3
"""CSR-550 — Research priority lock (primary = user log through 22:00).

P0 axes:
  CSR-521 Conditional Unwind = L2+ × MAE trajectory × Adverse (not fixed 30s)
  CSR-539 Integrity = score provenance / default30 / Capture — Economic PASS blocked
  UMM Tail = Worst-MAE → Ladder → HS (AE1–3s alone insufficient)

P1: Council Fake-BO Veto CF · Spread Gate blocked/passed EV
P2: PEG_v3 Economic REJECT · Entry/AE research asset only

Latency = CONDITION only · never Actual PnL deduct.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

PRIMARY = {
    "source": "user_presented_log_primary",
    "m365_corroboration": False,
    "council_50snap_2017_2217": {
        "n": 50,
        "cand_buy": 23, "cand_sell": 25, "cand_hold": 2,
        "dec_buy": 22, "dec_sell": 23, "dec_hold": 3, "dec_exit": 2,
        "fake_bo_veto": 12, "veto_rate_pct": 24.0,
        "regime_trend": 47, "high_vol": 2, "warmup": 1,
        "candidate_direction_switches": 23,
        "decompose": ["Direction Alpha", "Confidence Gate", "FakeBreakout Veto",
                      "→ Decision Economic Value"],
    },
    "adverse_council": {
        "level": "SHADOW_OFF", "score": None, "valid": False,
        "note": "NULL better than score=0.00 SAFE conflation",
    },
    "adverse_research_2200": {
        "ae_100ms": -0.69, "ae_1s": -0.92, "ae_3s": -0.97, "ae_10s": -1.06, "n": 1000,
        "score_0_20": {"n": 84, "win_pct": 83.3, "ev_bp": 1.87, "ae1s": 0.6},
        "score_60_80": {"n": 237, "win_pct": 26.6, "ev_bp": -1.27, "ae1s": -2.4},
        "alpha_x_adverse": {
            "hi_sig_lo_adv": {"n": 6339, "win_pct": 65.3, "ev_bp": 0.31},
            "hi_sig_hi_adv": {"n": 4945, "win_pct": 23.1, "ev_bp": -1.57},
            "r": -0.338, "beta_bp_per_pt": -0.041, "r2": 0.114, "n": 11284,
            "p_display": 0,  # presented as p=0; still not Economic PASS
        },
        "integrity_hold_csr539": [
            "score missing/default30 collapse",
            "Capture provenance",
        ],
        "economic_pass": False,
        "verdict": "STRONG_SIGNAL_HOLD_UNTIL_CSR539_INTEGRITY",
    },
    "csr521_2200": {
        "L0": {"bp": 5.98, "hs_pct": 7.1},
        "L1": {"bp": -17.97, "hs_pct": 12.8},
        "L2": {"bp": -17.68, "hs_pct": 25.0},
        "actual_bp": -29.67,
        "virtual_30_bp": 12.73,
        "delta_30_bp": 42.40,
        "l2_l3_delta_bp": 42.40,  # exact match → CU hypothesis strong sample
        "cf": {"10s": 64.58, "20s": 95.31, "30s": 42.40, "45s": -7.35},
        "note": "20s best · 45s harmful · NOT L2→fixed 30s cut",
        "policy": "IF ladder>=L2 AND adverse deteriorating AND MAE deteriorating "
                  "AND recovery_prob low THEN conditional unwind",
    },
    "umm_worst10_2200": {
        "all_umm": True,
        "mae_examples_bp": [-17.0, -16.6, -16.4, -16.1],
        "ae_masked_example": {"mae_bp": -15.2, "ae1s": 0.3, "ae3s": 0.1},
        "implication": "short AE cannot catch inventory tail metamorphosis",
    },
    "spread_gate_2200": {
        "pass_1h_pct": 5.0, "pass_1h": "94/1887",
        "pass_24h_pct": 8.9, "pass_24h": "4208/47393",
        "blocks": {"toxic": 587, "entry_halt": 973, "spread_limit": 134,
                   "confidence": 46, "observe_only": 53},
        "do_not_loosen": True,
        "next": "Blocked CF EV vs Passed Actual EV per gateway",
    },
    "peg_v3_2200": {
        "1h_bp": -40.91, "24h_bp": -252.50, "vs_base_bp": -303.00,
        "economic": "REJECT",
        "save": "Entry/AE research asset",
        "research": "Aging Exit CF only",
    },
}


def build_lock() -> Dict[str, Any]:
    return {
        "csr": 550,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Adverse×Ladder×MAE P0 · integrity HOLD · Council Veto stage",
        "primary": PRIMARY,
        "actual_pnl_identity": [
            "Gross Spread", "+ Adverse/Markout", "+ Inventory/Holding",
            "+ Exit/HS", "+ Residual", "= Realized PnL",
        ],
        "latency_role": "CONDITION_ONLY_NO_DEDUCT",
        "p0_axis": "Adverse × Inventory Ladder × MAE trajectory",
        "priorities": {
            "P0": [
                "CSR-521 Conditional Unwind (L2+ + MAE traj + Adverse)",
                "CSR-539 Integrity Audit (score/default30/Capture)",
                "UMM Tail Attribution (Worst-MAE → Ladder → HS)",
            ],
            "P1": [
                "Council Veto CF (FAKE_BREAKOUT ×12 non-veto virtual PnL)",
                "Spread Gate CF (blocked vs passed EV)",
            ],
            "P2": [
                "PEG_v3 Economic REJECT · Entry/AE save · Aging CF only",
            ],
        },
        "status": {
            "LIVE": "OFF",
            "WIRE": "NO",
            "ENFORCE": 0,
            "UMM": "NOT PASS",
            "CSR521": "STRONGLY PROMISING / OBSERVE",
            "Adverse_Economic": "HOLD / CSR-539",
            "4AGENT": "CF VALIDATION",
            "PEG_v3": "REJECT",
            "AUTO_APPLY": "OFF",
        },
        "forbidden": [
            "ADVERSE_ECONOMIC_PASS_BEFORE_CSR539",
            "FIXED_L2_30S_CUT",
            "LOOSEN_SPREAD_GATE_WITHOUT_CF",
            "LATENCY_COST_IN_ACTUAL",
            "PEG_V4",
            "ENFORCE_1",
            "LIVE",
        ],
        "core": (
            "Adverse Score looks like a real research variable (score bands / Alpha×Adverse) "
            "but Economic PASS waits on CSR-539. UMM tail = positions that look clean at 1–3s "
            "then inventoriize — catch with Adverse×Ladder×MAE, not AE alone."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr550_research_priority_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    assert lock["primary"]["csr521_2200"]["delta_30_bp"] == lock["primary"]["csr521_2200"]["l2_l3_delta_bp"]
    assert lock["primary"]["adverse_research_2200"]["economic_pass"] is False
    print(json.dumps({"ok": True, "path": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
