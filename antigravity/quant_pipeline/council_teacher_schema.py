#!/usr/bin/env python3
"""CSR-546 — Council Learning / Veto CF schema (OBSERVE · ENFORCE=0).

Rules:
  - Direction Accuracy uses candidate_action or decision_action — NEVER final_action
  - OBSERVE final_action=HOLD is not a teacher label
  - adverse_score_valid=False → store NULL (SHADOW_OFF 0 ≠ safe 0)
  - Veto Value = CF_without_veto − CF_with_veto (independent of Direction Accuracy)
  - Librarian RESEARCH ONLY · no auto weight apply to execution
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

LEARNING_COLUMNS: List[str] = [
    "timestamp",
    # hypothesis
    "hypothesis_direction",
    "hypothesis_strength",
    "regime",
    # candidate
    "candidate_action",
    "candidate_confidence",
    # decision
    "decision_action",
    "decision_confidence",
    "decision_gate_reason",
    # veto (CSR-558: effective ≠ redundant)
    "micro_veto",
    "veto_reason",
    "veto_effective",
    # execution
    "execution_allowed",
    "final_action",
    # adverse
    "adverse_state",
    "adverse_score",
    "adverse_score_valid",
    # micro
    "imbalance",
    "micro_dev",
    "fake_breakout",
    # labels (forward mid — Direction Accuracy only)
    "forward_mid_1s_bp",
    "forward_mid_3s_bp",
    "forward_mid_10s_bp",
    "forward_mid_30s_bp",
    "forward_mid_60s_bp",
    # CSR-564 P0 freshness / P1 collect / P2 shadow
    "state_age_sec",
    "freshness_ok",
    "teacher_valid",
    "signature_fp",
    "strength_raw",
    "strength_clipped",
    "raw_move",
    "buy_pressure_v2",
    "sell_pressure_v2",
    # research CF layers
    "candidate_cf_30s_bp",
    "decision_cf_30s_bp",
    "veto_cf_30s_bp",
    # legacy aliases kept for older joins
    "cf_1s_bp",
    "cf_3s_bp",
    "cf_10s_bp",
    "cf_30s_bp",
    "realized_pnl_bp",
]

TEACHER_RULES = {
    "direction_accuracy_label": ["candidate_action", "decision_action"],
    "forbidden_direction_label": ["final_action"],
    "observe_final_hold": "NOT_A_TEACHER",
    "adverse_shadow_off": "NULL_NOT_ZERO",
}

METRICS = {
    "direction_accuracy": {
        "def": "candidate BUY → mid↑@30s? · candidate SELL → mid↓@30s?",
        "label_source": "candidate_action|decision_action",
        "not": "final_action",
    },
    "veto_value": {
        "def": "CF_PnL_without_veto − CF_PnL_with_veto",
        "horizons_sec": [1, 3, 10, 30],
        "population": "veto_effective == true ONLY",
        "note": "Effective Veto only · redundant (decision HOLD + veto) excluded from Veto economics",
    },
    "confidence_gate_value": {
        "def": "candidate BUY/SELL → decision HOLD via conf threshold · EV vs if traded",
        "label_source": "candidate_action ≠ decision_action",
    },
    "confidence_calibration": {
        "def": "bucket conf 1.00 / 0.70–0.79 / … vs realized EV",
        "label_source": "candidate_confidence|decision_confidence",
    },
    "regime_conditional_ev": {
        "def": "Trend×BUY / Trend×SELL / Range×BUY / Range×SELL",
    },
    "council_value_decomposition": {
        "formula": "Candidate Alpha + Confidence Gate Value + Micro Veto Value = Council Value",
        "micro_veto_value": "Effective Veto only",
    },
}


def veto_effective(*, veto: bool, decision_action: str) -> bool:
    """CSR-558: real CF target iff decision was still BUY/SELL when veto fired."""
    d = str(decision_action or "hold").lower()
    return bool(veto) and d in ("buy", "sell")

# Primary evidence Fake-BO windows from user log (CF cells empty until mid join)
FAKE_BO_VETO_CASES = [
    {"ts": "00:00", "candidate": "SELL", "veto": "FAKE_BREAKOUT",
     "cf_1s": None, "cf_3s": None, "cf_10s": None, "cf_30s": None, "result": "NEED_CF"},
    {"ts": "04:00", "candidate": "BUY", "veto": "FAKE_BREAKOUT",
     "cf_1s": None, "cf_3s": None, "cf_10s": None, "cf_30s": None, "result": "NEED_CF"},
    {"ts": "06:00", "candidate": "SELL", "veto": "FAKE_BREAKOUT",
     "cf_1s": None, "cf_3s": None, "cf_10s": None, "cf_30s": None, "result": "NEED_CF"},
]

COUNCIL_TIMELINE_PRIMARY = [
    {"ts": "22:29", "direction": "UP", "candidate": "BUY", "conf": 1.00, "micro_veto": "NO"},
    {"ts": "23:00", "direction": "UP", "candidate": "BUY", "conf": 1.00, "micro_veto": "NO"},
    {"ts": "00:00", "direction": "DOWN", "candidate": "SELL", "conf": 1.00, "micro_veto": "FAKE_BREAKOUT"},
    {"ts": "01:00", "direction": "UP", "candidate": "BUY", "conf": 1.00, "micro_veto": "NO"},
    {"ts": "02:00", "direction": "DOWN", "candidate": "SELL", "conf": 1.00, "micro_veto": "NO"},
    {"ts": "03:00", "direction": "DOWN", "candidate": "SELL", "conf": 0.70, "micro_veto": "NO"},
    {"ts": "04:00", "direction": "UP", "candidate": "BUY", "conf": 1.00, "micro_veto": "FAKE_BREAKOUT"},
    {"ts": "05:00", "direction": "DOWN", "candidate": "SELL", "conf": 0.76, "micro_veto": "NO"},
    {"ts": "06:00", "direction": "DOWN", "candidate": "SELL", "conf": 0.74, "micro_veto": "FAKE_BREAKOUT"},
    {"ts": "07:00", "direction": "UP", "candidate": "BUY", "conf": 1.00, "micro_veto": "NO"},
]


def normalize_adverse_for_db(
    *,
    adverse_score: Optional[float],
    adverse_score_valid: bool,
    adverse_risk_level: str = "SHADOW_OFF",
) -> Dict[str, Any]:
    if not adverse_score_valid or adverse_risk_level == "SHADOW_OFF":
        return {"adverse_score": None, "adverse_score_valid": False}
    return {"adverse_score": adverse_score, "adverse_score_valid": True}


def direction_label_ok(field: str) -> bool:
    return field in TEACHER_RULES["direction_accuracy_label"]


def build_lock() -> Dict[str, Any]:
    return {
        "csr": 546,
        "parent": "CSR-538",
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Council teacher schema · Adverse NULL · Veto CF independent scoring",
        "primary_source": "user_presented_council_timeline",
        "m365_corroboration": False,
        "architecture": {
            "candidate_decision_final_separation": "PASS",
            "observe_hold_not_teacher": "PASS",
            "auto_apply": "OFF",
            "economic_pass": False,
        },
        "learning_columns": LEARNING_COLUMNS,
        "teacher_rules": TEACHER_RULES,
        "metrics": METRICS,
        "council_timeline_primary": COUNCIL_TIMELINE_PRIMARY,
        "fake_bo_veto_cf_table": FAKE_BO_VETO_CASES,
        "librarian_path": [
            "Observation", "Parquet", "DuckDB", "Librarian", "Research conclusion", "Human judgment"
        ],
        "librarian_lanes": [
            "PegResearch", "TrendFollowResearch", "MicrostructureHourly",
            "AdverseAdvance", "FusionParquet", "MMQuoteResearch", "FactorICResearch",
        ],
        "status": {
            "CSR538_Architecture": "GOOD",
            "Candidate_Final_separation": "PASS",
            "OBSERVE_HOLD_semantics": "PASS",
            "Auto_apply": "OFF",
            "Economic_PASS": "NO",
            "Direction_model": "NEEDS_CF_VALIDATION",
            "Micro_Veto": "PROMISING_NEEDS_CF",
            "Adverse": "INVALID_WHILE_SHADOW_OFF",
            "Librarian": "RESEARCH_ONLY",
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
        },
        "csr558_extension": {
            "veto_effective": "veto AND decision_action IN (buy,sell)",
            "redundant_veto_excluded_from_economics": True,
            "adverse_population": "WHERE adverse_score_valid = true",
        },
        "core": "Score candidate and Veto independently — never collapse into final_action",
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr546_council_teacher_schema_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    # smoke normalize
    assert normalize_adverse_for_db(
        adverse_score=0.0, adverse_score_valid=False
    )["adverse_score"] is None
    assert direction_label_ok("candidate_action")
    assert not direction_label_ok("final_action")
    print(json.dumps({"ok": True, "path": str(path), "veto_cases": len(FAKE_BO_VETO_CASES)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
