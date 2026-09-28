#!/usr/bin/env python3
"""CSR-549 — Council 08:00–22:00 teacher timeline (primary = Discord hourly logs).

M365: no extra matching source. Exclude 13:00 STALE_SCHEMA from teacher set.
final_action=HOLD is never a direction teacher. Adverse SHADOW_OFF → NULL.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

# Primary hourly rows 2026-09-28 JST
HOURLY: List[Dict[str, Any]] = [
    {"ts": "08:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "down", "cand": "SELL", "cand_conf": 1.00, "dec": "SELL", "dec_conf": 1.00,
     "veto": False, "veto_reason": None, "regime": "trend", "imb": -0.05, "micro_dev": 1055},
    {"ts": "09:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "down", "cand": "SELL", "cand_conf": 1.00, "dec": "SELL", "dec_conf": 1.00,
     "veto": False, "veto_reason": None, "regime": "trend", "imb": -0.07, "micro_dev": 426},
    {"ts": "10:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "down", "cand": "SELL", "cand_conf": 1.00, "dec": "SELL", "dec_conf": 1.00,
     "veto": False, "veto_reason": None, "regime": "trend", "imb": -0.03, "micro_dev": -533},
    {"ts": "11:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "down", "cand": "SELL", "cand_conf": 1.00, "dec": "SELL", "dec_conf": 1.00,
     "veto": False, "veto_reason": None, "regime": "trend", "imb": 0.02, "micro_dev": 1070},
    {"ts": "12:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "up", "cand": "BUY", "cand_conf": 1.00, "dec": "BUY", "dec_conf": 1.00,
     "veto": True, "veto_reason": "FAKE_BREAKOUT", "regime": "trend", "imb": 0.02, "micro_dev": -186,
     "micro_note": "cancel高・taker弱"},
    {"ts": "13:00", "schema": "LEGACY_PRE_CSR531", "teacher_ok": False,
     "hyp": "unknown", "cand": "HOLD", "cand_conf": 0.00, "dec": "HOLD", "dec_conf": 0.00,
     "veto": False, "veto_reason": None, "regime": "range", "imb": None, "micro_dev": None,
     "exclude_reason": "STALE_SCHEMA — candidate defaulted HOLD · do NOT teach"},
    {"ts": "14:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "up", "cand": "BUY", "cand_conf": 0.63, "dec": "HOLD", "dec_conf": 0.63,
     "veto": False, "veto_reason": None, "regime": "trend", "imb": -0.00, "micro_dev": 311,
     "note": "candidate≠decision · conf<0.65"},
    {"ts": "15:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "down", "cand": "SELL", "cand_conf": 0.59, "dec": "HOLD", "dec_conf": 0.59,
     "veto": False, "veto_reason": None, "regime": "trend", "imb": -0.07, "micro_dev": 372,
     "note": "candidate≠decision · conf<0.65"},
    {"ts": "16:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "up", "cand": "BUY", "cand_conf": 1.00, "dec": "BUY", "dec_conf": 1.00,
     "veto": False, "veto_reason": None, "regime": "trend", "imb": -0.02, "micro_dev": -779},
    {"ts": "17:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "down", "cand": "SELL", "cand_conf": 1.00, "dec": "SELL", "dec_conf": 1.00,
     "veto": False, "veto_reason": None, "regime": "trend", "imb": -0.05, "micro_dev": -408},
    {"ts": "18:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "down", "cand": "SELL", "cand_conf": 1.00, "dec": "SELL", "dec_conf": 1.00,
     "veto": True, "veto_reason": "FAKE_BREAKOUT", "regime": "trend", "imb": -0.03, "micro_dev": -525,
     "micro_note": "cancel高・taker弱"},
    {"ts": "19:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "neutral", "cand": "HOLD", "cand_conf": 0.11, "dec": "EXIT", "dec_conf": 0.11,
     "veto": True, "veto_reason": "FAKE_BREAKOUT", "regime": "high_vol", "imb": 0.05, "micro_dev": -509,
     "note": "direction label weak (HOLD/EXIT) · veto CF on HOLD candidate is low-value",
     "micro_note": "cancel高・taker弱"},
    {"ts": "20:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "down", "cand": "SELL", "cand_conf": 0.45, "dec": "HOLD", "dec_conf": 0.45,
     "veto": True, "veto_reason": "FAKE_BREAKOUT", "regime": "trend", "imb": 0.00, "micro_dev": 821,
     "note": "candidate≠decision · conf<0.65 · veto on SELL candidate",
     "micro_note": "cancel高・taker弱"},
    {"ts": "21:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "down", "cand": "SELL", "cand_conf": 1.00, "dec": "SELL", "dec_conf": 1.00,
     "veto": False, "veto_reason": None, "regime": "trend", "imb": 0.01, "micro_dev": -319},
    {"ts": "22:00", "schema": "csr538_v1", "teacher_ok": True,
     "hyp": "down", "cand": "SELL", "cand_conf": 1.00, "dec": "SELL", "dec_conf": 1.00,
     "veto": False, "veto_reason": None, "regime": "trend", "imb": 0.02, "micro_dev": 380},
]


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [r for r in rows if r.get("teacher_ok")]
    stale = [r for r in rows if not r.get("teacher_ok")]
    veto = [r for r in ok if r.get("veto")]
    split = [r for r in ok if r["cand"] != r["dec"]]
    cand_buy = sum(1 for r in ok if r["cand"] == "BUY")
    cand_sell = sum(1 for r in ok if r["cand"] == "SELL")
    cand_hold = sum(1 for r in ok if r["cand"] == "HOLD")
    conf_1 = sum(1 for r in ok if abs(float(r["cand_conf"]) - 1.0) < 1e-9)
    conf_lt65 = sum(1 for r in ok if float(r["cand_conf"]) < 0.65)
    veto_cf_priority = [
        r for r in veto
        if r["cand"] in ("BUY", "SELL")
    ]
    veto_cf_low = [r for r in veto if r["cand"] == "HOLD"]
    return {
        "hours_presented": len(rows),
        "teacher_ok_n": len(ok),
        "stale_excluded": [r["ts"] for r in stale],
        "candidate_counts": {"BUY": cand_buy, "SELL": cand_sell, "HOLD": cand_hold},
        "candidate_ne_decision": [{"ts": r["ts"], "cand": r["cand"], "dec": r["dec"], "conf": r["cand_conf"]} for r in split],
        "veto_n": len(veto),
        "veto_cf_priority": [
            {"ts": r["ts"], "cand": r["cand"], "dec": r["dec"], "veto": r["veto_reason"],
             "cf_1s": None, "cf_3s": None, "cf_10s": None, "cf_30s": None, "result": "NEED_CF"}
            for r in veto_cf_priority
        ],
        "veto_cf_low_value": [{"ts": r["ts"], "cand": r["cand"], "dec": r["dec"]} for r in veto_cf_low],
        "conf_1_00_n": conf_1,
        "conf_below_0_65_n": conf_lt65,
        "all_final_hold": True,
        "all_execution_allowed_false": True,
        "adverse": "NULL_SHADOW_OFF",
    }


LIBRARIAN_0900 = {
    "portfolio": "MIXED",
    "USEFUL": 4,
    "NEED_MORE": 1,
    "NOT_USEFUL": 2,
    "CONTEXT": 2,
    "lanes": {
        "PegResearch": "NOT_USEFUL",
        "TrendFollowResearch": "NOT_USEFUL",
        "MicrostructureHourly": "NEED_MORE",
        "AdverseAdvance": "USEFUL",
        "FusionParquet": "USEFUL",
        "MMQuoteResearch": "USEFUL",
        "FactorICResearch": "USEFUL",
        "UMM_TF2BP_Context": "CONTEXT",
        "CouncilFusion": "CONTEXT",
    },
    "auto_apply": "OFF",
    "advise_high": [
        "PegResearch peg_diff/mom or horizon (below baseline)",
        "PegResearch trend_end exhaust vs Micro tip depletion",
        "TrendFollow ¥400 / 30s vs Micro tip — RESEARCH ONLY, not Council weight apply",
    ],
}


def build_lock() -> Dict[str, Any]:
    s = summarize(HOURLY)
    return {
        "csr": 549,
        "parent": "CSR-546 / CSR-538",
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Council 08–22h teacher timeline · STALE 13:00 excluded · Veto CF NEED_CF",
        "primary_source": "discord_hourly_council_2026-09-28_0800_2200",
        "m365_corroboration": False,
        "hourly": HOURLY,
        "summary": s,
        "teacher_rules": {
            "direction": "candidate_action (preferred) or decision_action",
            "forbidden": ["final_action", "STALE_SCHEMA rows", "hypothesis=unknown"],
            "observe_hold": "NOT_A_TEACHER",
            "adverse_score": None,
            "adverse_score_valid": False,
        },
        "architecture": {
            "schema": "csr538_v1 except 13:00",
            "candidate_final_separation": "PASS",
            "observe_hold": "PASS",
            "auto_apply": "OFF",
            "economic_pass": False,
            "13:00": "INVALID_TEACHER",
        },
        "librarian_0900": LIBRARIAN_0900,
        "metrics_status": {
            "Direction_Accuracy": "NEEDS_CF (do not score from this Discord dump alone)",
            "Veto_Value": "NEED_CF — priority 12:00 BUY / 18:00 SELL / 20:00 SELL",
            "Confidence_Calibration": "READY_TO_BUCKET after CF join · 1.00 vs <0.65 split observed",
            "Regime_EV": "NEED_CF · almost all trend except 19:00 high_vol",
        },
        "status": {
            "Architecture": "GOOD",
            "Adverse": "INVALID_WHILE_SHADOW_OFF",
            "Librarian": "RESEARCH_ONLY_MIXED",
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
        },
        "core": (
            "Keep candidate vs veto independent. 13:00 HOLD is schema failure, not a market HOLD. "
            "14/15/20 show conf-threshold split (candidate≠decision) — use candidate for direction, "
            "decision for gate. Fake-BO vetoes still NEED mid CF."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build_lock()
    path = OUT / "2026-09-28_csr549_council_0800_2200_teacher_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    s = lock["summary"]
    assert "13:00" in s["stale_excluded"]
    assert s["teacher_ok_n"] == 14
    assert s["veto_n"] == 4
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "teacher_ok": s["teacher_ok_n"],
        "veto_priority": [x["ts"] for x in s["veto_cf_priority"]],
        "splits": s["candidate_ne_decision"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
