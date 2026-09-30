#!/usr/bin/env python3
"""CSR-558 — Council Accuracy Report · Effective vs Redundant Veto (CSR-538 healthy).

Primary = user-presented hourly council log (Adverse NULL confirmed).
M365: no extra matching source.

Rules:
  - adverse_score_valid=false → exclude from Adverse effect population
  - candidate ≠ decision is a feature (confidence gate), not a bug
  - veto_effective = veto AND decision IN (buy, sell)  — CF target
  - redundant veto (decision HOLD + veto) ≠ Veto economic value
  - final HOLD (OBSERVE) never direction teacher
  - forward_mid_* only for Direction Accuracy (not log-internal 変動幅)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

# Primary hourly rows from user message
HOURLY: List[Dict[str, Any]] = [
    {"ts": "23:00", "regime": "trend", "cand": "SELL", "cand_conf": 0.84,
     "dec": "SELL", "dec_conf": 0.84, "veto": False, "veto_reason": None},
    {"ts": "00:00", "regime": "trend", "cand": "BUY", "cand_conf": 1.00,
     "dec": "BUY", "dec_conf": 1.00, "veto": False, "veto_reason": None},
    {"ts": "01:00", "regime": "high_vol", "cand": "BUY", "cand_conf": 0.30,
     "dec": "HOLD", "dec_conf": 0.30, "veto": False, "veto_reason": None,
     "note": "confidence gate · candidate≠decision · NOT same as final OBSERVE HOLD"},
    {"ts": "02:00", "regime": "trend", "cand": "BUY", "cand_conf": 1.00,
     "dec": "BUY", "dec_conf": 1.00, "veto": True, "veto_reason": "FAKE_BREAKOUT"},
    {"ts": "03:00", "regime": "trend", "cand": "SELL", "cand_conf": 1.00,
     "dec": "SELL", "dec_conf": 1.00, "veto": False, "veto_reason": None},
    {"ts": "04:00", "regime": "range", "cand": "BUY", "cand_conf": 0.29,
     "dec": "HOLD", "dec_conf": 0.29, "veto": True, "veto_reason": "FAKE_BREAKOUT",
     "note": "redundant veto · decision already HOLD"},
    {"ts": "05:00", "regime": "trend", "cand": "BUY", "cand_conf": 1.00,
     "dec": "BUY", "dec_conf": 1.00, "veto": False, "veto_reason": None},
    {"ts": "06:00", "regime": "trend", "cand": "BUY", "cand_conf": 1.00,
     "dec": "BUY", "dec_conf": 1.00, "veto": False, "veto_reason": None},
    {"ts": "07:00", "regime": "trend", "cand": "SELL", "cand_conf": 1.00,
     "dec": "SELL", "dec_conf": 1.00, "veto": True, "veto_reason": "FAKE_BREAKOUT"},
]

RESEARCH_COLUMNS: List[str] = [
    "timestamp",
    "hypothesis_direction",
    "hypothesis_strength",
    "regime",
    "candidate_action",
    "candidate_confidence",
    "decision_action",
    "decision_confidence",
    "decision_gate_reason",
    "veto",
    "veto_reason",
    "veto_effective",
    "execution_allowed",
    "final_action",
    "adverse_state",
    "adverse_score",
    "adverse_score_valid",
    "imbalance",
    "micro_dev",
    "fake_breakout",
    "forward_mid_1s_bp",
    "forward_mid_3s_bp",
    "forward_mid_10s_bp",
    "forward_mid_30s_bp",
    "candidate_cf_30s_bp",
    "decision_cf_30s_bp",
    "veto_cf_30s_bp",
]

ACCURACY_LAYERS = [
    "Candidate BUY",
    "Candidate SELL",
    "Decision BUY",
    "Decision SELL",
    "Decision HOLD",
    "Effective Veto",
    "No Veto",
]


def veto_effective(*, veto: bool, decision_action: str) -> bool:
    d = str(decision_action or "hold").lower()
    return bool(veto) and d in ("buy", "sell")


def decision_gate_reason(cand: str, dec: str, conf: float, veto: bool) -> str:
    c, d = cand.upper(), dec.upper()
    if c in ("BUY", "SELL") and d == "HOLD" and conf < 0.65:
        base = "CONFIDENCE_GATE"
    elif c == d:
        base = "PASS_THROUGH"
    else:
        base = "OTHER_GATE"
    if veto and d == "HOLD":
        return f"{base}+VETO_REDUNDANT"
    if veto and d in ("BUY", "SELL"):
        return f"{base}+VETO_EFFECTIVE_PENDING"  # decision still buy/sell before gate
    return base


def classify_hours(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    effective, redundant, conf_gate = [], [], []
    for r in rows:
        ve = veto_effective(veto=bool(r["veto"]), decision_action=r["dec"])
        r2 = {**r, "veto_effective": ve,
              "decision_gate_reason": decision_gate_reason(
                  r["cand"], r["dec"], float(r["cand_conf"]), bool(r["veto"]))}
        if ve:
            effective.append(r2)
        elif r["veto"] and r["dec"].upper() == "HOLD":
            redundant.append(r2)
        if r["cand"].upper() != r["dec"].upper():
            conf_gate.append(r2)
    return {
        "n": len(rows),
        "effective_veto": [{"ts": x["ts"], "cand": x["cand"], "dec": x["dec"],
                            "veto": x["veto_reason"]} for x in effective],
        "redundant_veto": [{"ts": x["ts"], "cand": x["cand"], "dec": x["dec"],
                            "veto": x["veto_reason"]} for x in redundant],
        "confidence_gate_splits": [
            {"ts": x["ts"], "cand": x["cand"], "dec": x["dec"], "conf": x["cand_conf"]}
            for x in conf_gate
        ],
        "annotated": [
            {**r, "veto_effective": veto_effective(veto=bool(r["veto"]), decision_action=r["dec"])}
            for r in rows
        ],
    }


def empty_accuracy_report() -> Dict[str, Any]:
    layers = {}
    for name in ACCURACY_LAYERS:
        layers[name] = {
            "n": None,
            "direction_hit": None,
            "ev_1s_bp": None,
            "ev_3s_bp": None,
            "ev_10s_bp": None,
            "ev_30s_bp": None,
        }
    return {
        "title": "Council Accuracy Report",
        "layers": layers,
        "decomposition": {
            "Candidate_Alpha": None,
            "Confidence_Gate_Value": None,
            "Micro_Veto_Value": None,  # Effective Veto only
            "Council_Value": None,
            "formula": "Candidate Alpha + Confidence Gate Value + Micro Veto Value = Council Value",
        },
        "adverse_population": "WHERE adverse_score_valid = true ONLY",
        "gov": {"WIRE": "NO", "ENFORCE": 0, "AUTO_APPLY": "OFF", "LIVE": "OFF"},
    }


def build_lock() -> Dict[str, Any]:
    cls = classify_hours(HOURLY)
    return {
        "csr": 558,
        "parent": "CSR-538 / CSR-551",
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Council Accuracy · veto_effective · Adverse valid filter",
        "primary_source": "user_presented_council_hourly",
        "m365_corroboration": False,
        "adverse": {
            "state": "SHADOW_OFF",
            "score": None,
            "valid": False,
            "duckdb": "WHERE adverse_score_valid = true",
            "status": "HEALTHY_NULL_SEMANTICS",
        },
        "hourly": HOURLY,
        "classification": cls,
        "test_cases": {
            "01:00": "confidence gate BUY→HOLD · distinct from OBSERVE final HOLD",
            "04:00": "redundant veto · do NOT credit Veto economics",
            "02:00_07:00": "effective Fake-BO veto · real Veto CF targets",
        },
        "research_columns": RESEARCH_COLUMNS,
        "accuracy_report_template": empty_accuracy_report(),
        "librarian_0000": {
            "portfolio": "PARTIAL_USEFUL",
            "USEFUL": 3,
            "NEED_MORE": 4,
            "NOT_USEFUL": 0,
            "CONTEXT": 2,
            "note": "not all lanes forced USEFUL — governance OK",
        },
        "p0_next": "Council Accuracy Report (layer EV with forward_mid join)",
        "status": {
            "CSR538": "HEALTHY / RESEARCH PIPELINE",
            "Adverse_NULL": "PASS",
            "candidate_decision_split": "PASS",
            "veto_effective_taxonomy": "LOCKED",
            "WIRE": "NO",
            "ENFORCE": 0,
            "AUTO_APPLY": "OFF",
            "LIVE": "OFF",
        },
        "core": (
            "Council is Hypothesis → Confidence Gate → Micro Veto → Execution Gate. "
            "Score layers independently; only Effective Veto enters Veto Value."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-29_csr558_council_accuracy_veto_effective_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    rep = OUT / "2026-09-29_csr558_council_accuracy_report_template.json"
    rep.write_text(json.dumps(empty_accuracy_report(), ensure_ascii=False, indent=2), encoding="utf-8")
    assert [x["ts"] for x in lock["classification"]["effective_veto"]] == ["02:00", "07:00"]
    assert [x["ts"] for x in lock["classification"]["redundant_veto"]] == ["04:00"]
    assert veto_effective(veto=True, decision_action="hold") is False
    assert veto_effective(veto=True, decision_action="buy") is True
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "effective": [x["ts"] for x in lock["classification"]["effective_veto"]],
        "redundant": [x["ts"] for x in lock["classification"]["redundant_veto"]],
        "conf_gates": [x["ts"] for x in lock["classification"]["confidence_gate_splits"]],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
