#!/usr/bin/env python3
"""CSR-551 — Council CF Scorecard · CSR-531 LEGACY vs CSR-538 CANONICAL.

Teacher rules (primary = user-presented Council timeline):
  - CSR-531 LEGACY: TRAINING NO (final≠candidate contamination)
  - CSR-538 CANONICAL: layered labels
  - STALE_SCHEMA → training_eligible=false
  - SHADOW_OFF score=0 PROHIBITED → NULL / valid=false
  - final_action NEVER direction teacher
  - Direction CF from future_mid@1/3/10/30s — NOT log-internal 変動幅
  - Confidence 0.65 = CF TEST only (do not retune live to 0.60)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

# CSR-531 contamination examples (user primary)
CSR531_CONTAMINATION = [
    {"ts": "09:00", "cand": "HOLD", "dec": "HOLD", "final": "BUY", "problem": "final≠cand"},
    {"ts": "13:00", "cand": "HOLD", "dec": "HOLD", "final": "SELL", "problem": "final≠cand"},
    {"ts": "10:00", "cand": "HOLD", "dec": "HOLD", "final": "EXIT", "problem": "final≠cand"},
]

# CSR-538 Fake-BO veto events (union of prior + day logs)
FAKE_BO_EVENTS = [
    {"ts": "00:00", "cand": "SELL", "dec": "SELL", "veto": "FAKE_BREAKOUT"},
    {"ts": "04:00", "cand": "BUY", "dec": "BUY", "veto": "FAKE_BREAKOUT"},
    {"ts": "06:00", "cand": "SELL", "dec": "SELL", "veto": "FAKE_BREAKOUT"},
    {"ts": "07:30", "cand": "SELL", "dec": "SELL", "veto": "FAKE_BREAKOUT"},
    {"ts": "12:00", "cand": "BUY", "dec": "BUY", "veto": "FAKE_BREAKOUT"},
    {"ts": "18:00", "cand": "SELL", "dec": "SELL", "veto": "FAKE_BREAKOUT"},
    {"ts": "19:00", "cand": "HOLD", "dec": "EXIT", "veto": "FAKE_BREAKOUT", "regime": "high_vol"},
    {"ts": "20:00", "cand": "SELL", "dec": "HOLD", "veto": "FAKE_BREAKOUT", "cand_conf": 0.45},
]

CONF_GATE_CF_CASES = [
    {"ts": "14:00", "cand": "BUY", "cand_conf": 0.63, "dec": "HOLD"},
    {"ts": "15:00", "cand": "SELL", "cand_conf": 0.59, "dec": "HOLD"},
    {"ts": "20:00", "cand": "SELL", "cand_conf": 0.45, "dec": "HOLD"},
]

VETO_EVAL_TAXONOMY = [
    {"cand": "BUY", "dec": "BUY", "veto": "NO", "eval": "Direction CF"},
    {"cand": "BUY", "dec": "BUY", "veto": "YES", "eval": "Veto CF"},
    {"cand": "SELL", "dec": "SELL", "veto": "YES", "eval": "Veto CF"},
    {"cand": "BUY|SELL", "dec": "HOLD", "veto": "NO", "eval": "Confidence gate"},
    {"cand": "BUY|SELL", "dec": "HOLD", "veto": "YES", "eval": "Gate + Veto separation"},
    {"cand": "HOLD", "dec": "EXIT", "veto": "YES", "eval": "Exit research"},
]


def training_eligible(
    *,
    schema_version: str,
    candidate_action: str,
    final_action: str,
    hypothesis_direction: Optional[str] = None,
) -> Dict[str, Any]:
    if str(schema_version).startswith("LEGACY") or schema_version == "LEGACY_PRE_CSR531":
        return {"training_eligible": False, "reason": "STALE_SCHEMA"}
    if not str(schema_version).startswith("csr538") and not str(schema_version).startswith("csr531"):
        # pre-layered
        if candidate_action.upper() == "HOLD" and final_action.upper() in ("BUY", "SELL", "EXIT"):
            return {"training_eligible": False, "reason": "CSR531_FINAL_CONTAMINATION"}
    if hypothesis_direction in ("unknown", None) and candidate_action.upper() == "HOLD":
        # only exclude if also stale-like; csr538 genuine HOLD ok
        pass
    if str(schema_version).startswith("csr531") and not str(schema_version).startswith("csr538"):
        if candidate_action.upper() == "HOLD" and final_action.upper() != "HOLD":
            return {"training_eligible": False, "reason": "CSR531_FINAL_CONTAMINATION"}
    return {"training_eligible": True, "reason": "OK"}


def empty_scorecard() -> Dict[str, Any]:
    def horz():
        return {"n": None, "cf_1s_bp": None, "cf_3s_bp": None, "cf_10s_bp": None, "cf_30s_bp": None}

    return {
        "title": "CSR-538 COUNCIL COUNTERFACTUAL SCORECARD",
        "A_DIRECTION": {
            "candidate_BUY": horz(),
            "candidate_SELL": horz(),
            "label_source": "candidate_action",
            "forbidden_label": "final_action",
            "future_mid_only": True,
            "log_internal_price_move": "NOT_VALID_TEACHER",
        },
        "B_CONFIDENCE": {
            "0.40-0.64": {"n": None, "ev_bp": None},
            "0.65-0.79": {"n": None, "ev_bp": None},
            "0.80-0.99": {"n": None, "ev_bp": None},
            "1.00": {"n": None, "ev_bp": None},
            "gate_0_65": "CF_TEST_ONLY_DO_NOT_RETUNE_LIVE",
            "cf_cases_below_gate": CONF_GATE_CF_CASES,
        },
        "C_MICRO_VETO": {
            "veto_candidates_n": None,
            "cf_without_veto_bp": None,
            "veto_benefit_bp": None,
            "FAKE_BREAKOUT": {
                "BUY_veto": {"n": None, "cf_bp": None},
                "SELL_veto": {"n": None, "cf_bp": None},
            },
            "known_events": FAKE_BO_EVENTS,
            "taxonomy": VETO_EVAL_TAXONOMY,
        },
        "D_REGIME": {
            "trend_BUY": {"n": None, "ev_bp": None},
            "trend_SELL": {"n": None, "ev_bp": None},
            "high_vol": {"n": None, "ev_bp": None},
            "low_vol": {"n": None, "ev_bp": None},
            "range": {"n": None, "ev_bp": None},
        },
        "E_DATA_QUALITY": {
            "csr538_valid_pct": None,
            "stale_schema_n": None,
            "adverse_valid_pct": None,
            "final_as_teacher": 0,
            "targets": {
                "final_as_teacher": 0,
                "stale_in_training": 0,
                "adverse_score_zero_when_shadow_off": 0,
            },
        },
        "cf_join": {
            "candidate_timestamp": True,
            "future_mid_1s": True,
            "future_mid_3s": True,
            "future_mid_10s": True,
            "future_mid_30s": True,
        },
    }


def build_lock() -> Dict[str, Any]:
    # smoke eligibility
    stale = training_eligible(
        schema_version="LEGACY_PRE_CSR531",
        candidate_action="HOLD",
        final_action="HOLD",
        hypothesis_direction="unknown",
    )
    contam = training_eligible(
        schema_version="csr531_v1",
        candidate_action="HOLD",
        final_action="BUY",
    )
    ok = training_eligible(
        schema_version="csr538_v1",
        candidate_action="BUY",
        final_action="HOLD",
    )
    return {
        "csr": 551,
        "parent": "CSR-538",
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Council CF Scorecard · CSR-531 TRAINING NO · CSR-538 CANONICAL",
        "primary_source": "user_presented_council_timeline",
        "m365_corroboration": False,
        "csr531": {
            "status": "LEGACY",
            "training": "NO",
            "contamination_examples": CSR531_CONTAMINATION,
            "problem": "final_action alone yields HOLD→BUY/SELL/EXIT mixed teachers",
        },
        "csr538": {
            "status": "CANONICAL",
            "layers": {
                "hypothesis": "VALID RESEARCH FIELD",
                "candidate": "PRIMARY DIRECTION LABEL",
                "decision": "COUNCIL LABEL",
                "veto": "INDEPENDENT RISK LABEL",
                "final": "EXECUTION STATE ONLY",
                "execution_allowed": "GATE STATE",
            },
            "pipeline": [
                "Research Hypothesis", "Candidate", "Decision",
                "Micro Veto", "Execution Gate", "Final=HOLD because OBSERVE",
            ],
        },
        "prohibited": [
            "final teacher",
            "SHADOW_OFF score=0",
            "STALE_SCHEMA in training",
            "direction accuracy from log-internal 変動幅",
            "live retune confidence 0.65→0.60 without CF",
        ],
        "eligibility_smoke": {"stale": stale, "contam": contam, "ok": ok},
        "scorecard_template": empty_scorecard(),
        "adverse_db": {
            "adverse_score": None,
            "adverse_valid": False,
            "when": "SHADOW_OFF",
        },
        "status": {
            "CSR531": "LEGACY · TRAINING NO",
            "CSR538": "CANONICAL",
            "final_teacher": "PROHIBITED",
            "SHADOW_OFF_score0": "PROHIBITED",
            "STALE_SCHEMA": "TRAINING EXCLUDE",
            "Confidence_0_65": "CF TEST",
            "FAKE_BREAKOUT_Veto": "P0 CF TEST",
            "Adverse": "WAIT FOR valid=True",
            "WIRE": "NO",
            "ENFORCE": 0,
            "AUTO_APPLY": "OFF",
            "LIVE": "OFF",
        },
        "core": (
            "Measure which layer created value: Direction Alpha (candidate) · "
            "Confidence gate (decision) · Micro Veto — each with independent CF PnL. "
            "Never 'SELL said so → correct' from co-logged price move."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr551_council_cf_scorecard_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    # also dump bare scorecard for Discord/report paste
    sc_path = OUT / "2026-09-28_csr551_council_cf_scorecard_template.json"
    sc_path.write_text(json.dumps(lock["scorecard_template"], ensure_ascii=False, indent=2), encoding="utf-8")
    assert lock["eligibility_smoke"]["stale"]["training_eligible"] is False
    assert lock["eligibility_smoke"]["contam"]["training_eligible"] is False
    assert lock["eligibility_smoke"]["ok"]["training_eligible"] is True
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "scorecard": str(sc_path),
        "fake_bo_n": len(FAKE_BO_EVENTS),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
