#!/usr/bin/env python3
"""CSR-543 / CSR-521 — Hazard Unwind Risk Score (DESIGN ONLY · ENFORCE=0).

Shift from fixed-horizon timers (10/30/60s) to toxic-inventory identification.

Evidence (primary = user overnight log, 9 windows):
  - Full 30s CF: 8/9 positive · ΣΔ ≈ +213.91bp · counterexample 23:00 −9.76bp
  - L2+L3 30s CF: 8/9 positive · ΣΔ ≈ +191.84bp · same 23:00 counterexample
  - 10s CF has 02:00 −5.44bp → fixed early exit also NOT proven

Verdict:
  PROMISING / OBSERVE — NOT proven perfect · NOT Economic PASS · NOT ENFORCE
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

CSR = "CSR-543"
MODE = "DESIGN_ONLY_OBSERVE"
WIRE = "NO"
ENFORCE = 0

UNWIND_RISK_FEATURES: List[str] = [
    "hold_ladder",          # L0..L4
    "mae_bp",
    "inventory_side",       # long/short
    "adverse_markout_bp",   # ACTUAL outcome (CSR-542)
    "spread_bp",
    "tip_thin",
    "taker_aggressiveness",
    "cancel_rate",
    "refill_rate",
    "fake_bo",
    "regime",
]

# Latency is CONDITION only — not a PnL deduction feature in Actual identity
CONDITION_FEATURES: List[str] = [
    "latency_ms",
    "latency_bucket",
    "latency_risk_flag",
]


@dataclass
class HazardUnwindSpec:
    csr: str = CSR
    parent: str = "CSR-521"
    mode: str = MODE
    wire: str = WIRE
    enforce: int = ENFORCE
    hardcode_fixed_horizon: bool = False
    policy: str = (
        "IF (ladder in {L2,L3,L4}) AND (MAE deteriorates) "
        "AND (Adverse deteriorates) AND/OR (Micro liquidity deteriorates) "
        "THEN run Unwind CF — else HOLD (protect L0/L1 edge)"
    )
    features: List[str] = field(default_factory=lambda: list(UNWIND_RISK_FEATURES))
    conditions: List[str] = field(default_factory=lambda: list(CONDITION_FEATURES))
    evidence: Dict[str, Any] = field(
        default_factory=lambda: {
            "source": "user_presented_overnight_log_primary",
            "windows_n": 9,
            "full_30s_cf": {
                "positive_windows": "8/9",
                "sum_delta_bp": 213.91,
                "counterexample": {"window": "23:00", "delta_bp": -9.76},
            },
            "l2_l3_30s_cf": {
                "positive_windows": "8/9",
                "sum_delta_bp": 191.84,
                "counterexample": {"window": "23:00", "delta_bp": -9.76},
            },
            "ten_s_cf_note": "02:00 Δ=-5.44 → fixed 10s also fails; not a timer problem",
            "verdict": "PROMISING_OBSERVE_NOT_PROVEN",
        }
    )
    forbidden: List[str] = field(
        default_factory=lambda: [
            "FIXED_10S_ALL_EXIT",
            "FIXED_30S_ALL_EXIT",
            "FIXED_60S_ALL_EXIT",
            "L2_ALWAYS_CUT",
            "ECONOMIC_PASS",
            "ENFORCE_1",
            "LIVE",
            "LATENCY_COST_IN_ACTUAL_PNL",
            "ADD_CF_DELTA_TO_ACTUAL",
        ]
    )
    gates: Dict[str, Any] = field(
        default_factory=lambda: {
            "economic_pass": False,
            "require_counterexample_retention": True,
            "min_windows_with_mixed_sign": 1,
            "protect_l0_l1_edge": True,
            "p0_is_loss_tail_compression": True,
            "p0_is_not_winrate": True,
        }
    )


def build_lock() -> Dict[str, Any]:
    spec = HazardUnwindSpec()
    return {
        "csr": 543,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "CSR-521 → Hazard Unwind Risk Score",
        "spec": asdict(spec),
        "gov": {
            "UMM": "NOT_PASS",
            "CSR521": "PROMISING_OBSERVE",
            "Arena": "STRONG_RESEARCH_EVIDENCE",
            "TF2BP": "FROZEN",
            "PEG_v3": "REJECT",
            "LIVE": "OFF",
            "WIRE": "NO",
            "ENFORCE": 0,
            "AUTO_APPLY": "OFF",
        },
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr543_hazard_unwind_design_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path), "verdict": lock["spec"]["evidence"]["verdict"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
