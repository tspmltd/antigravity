#!/usr/bin/env python3
"""CSR-541 — UMM Frozen Champion Candidate snapshot (params NO CHANGE).

Saves code_hash / parameter_hash / policy bundle. Does not touch dryrun / LIVE.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
UMM_SRC = ROOT / "antigravity" / "strategies" / "umm_strategy.py"
OUT_DIR = ROOT / "data" / "mm_research" / "champions"
DAILY = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

# Primary evidence from user-presented integrated log (22:00 JST 2026-09-27)
PRIMARY_EVIDENCE = {
    "source": "user_presented_integrated_log_primary",
    "asof_jst": "2026-09-27T22:00:00+09:00",
    "umm_24h_bp_09h": -106.47,
    "umm_1h_bp_22h": 46.48,
    "umm_24h_bp_22h": 50.74,
    "umm_delta_09_to_22_bp": 157.21,
    "umm_trades_24h": 1176,
    "umm_win_rate_pct": 61.0,
    "portfolio_24h_bp": 229.11,
    "tf2bp_24h_bp": 87.13,
    "peg_v3_24h_bp": -24.15,
    "arena_24h_bp": 91.24,
    "arena_top3_note": {
        "SpreadCaptureMM": 69.19,
        "MicroSpreadMM": 15.61,
        "InventorySkewMM": 13.94,
        "simple_sum": 98.74,
        "disclaimer": "simple_sum != Arena total decomposition",
    },
}


def _sha16_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _sha16_text(text: str) -> str:
    return _sha16_bytes(text.encode("utf-8"))


def parameter_hash(params: Dict[str, Any]) -> str:
    return _sha16_text(json.dumps(params, sort_keys=True, default=str))


def build_snapshot() -> Dict[str, Any]:
    from antigravity.strategies.umm_strategy import UMMStrategy

    code = UMM_SRC.read_text(encoding="utf-8")
    umm = UMMStrategy()
    params = dict(umm.params)
    policy = {
        "spread_policy": {
            "spread_min_bp": params["spread_min_bp"],
            "max_spread_jpy": params["max_spread_jpy"],
            "target_spread_markup": params.get("target_spread_markup"),
        },
        "inventory_policy": {
            "gamma_high": params["gamma_high"],
            "max_position_btc": params["max_position_btc"],
            "order_size_btc": params["order_size_btc"],
            "continuous_quote": True,
        },
        "exit_policy": {
            "take_profit_bp": params["take_profit_bp"],
            "hard_stop_bp": params["hard_stop_bp"],
            "max_hold_sec": params["max_hold_sec"],
            "conditional_unwind": "OBSERVE_ONLY_CSR521",
            "all_exit_30s": False,
        },
        "risk_limits": {
            "hard_stop_bp": params["hard_stop_bp"],
            "max_position_btc": params["max_position_btc"],
            "LIVE": "OFF",
            "WIRE": "NO",
            "ENFORCE": 0,
        },
    }
    # Rolling state (secondary; primary is presented log)
    rolling = {}
    state_path = ROOT / "data" / "dryrun_umm_tf2bp_state.json"
    if state_path.exists():
        st = json.loads(state_path.read_text(encoding="utf-8"))
        u = st.get("umm") or {}
        rolling = {
            "asof_unix": st.get("timestamp"),
            "umm_stats_1h": u.get("stats_1h"),
            "umm_stats_24h": u.get("stats_24h"),
            "umm_params_runtime": u.get("params"),
            "tf2bp_stats_24h": (st.get("tf2bp") or {}).get("stats_24h"),
            "peg_v3_stats_24h": (st.get("tf2bp_peg_v3") or {}).get("stats_24h"),
        }

    snap = {
        "csr": 541,
        "status": "CHAMPION_CANDIDATE",
        "change": "FREEZE_FIRST",
        "live": "NO",
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "strategy_id": "UMM_v1_CSR504",
        "source_file": str(UMM_SRC.relative_to(ROOT)),
        "code_hash": _sha16_text(code),
        "parameter_hash": parameter_hash(params),
        "policy_hash": parameter_hash(policy),
        "schema_version": "csr541_champion_v1",
        "params": params,
        "policy": policy,
        "primary_evidence": PRIMARY_EVIDENCE,
        "rolling_state_secondary": rolling,
        "research_lanes_allowed": [
            "CF_L3_ONLY_UNWIND",
            "CF_L4_ONLY_UNWIND",
            "CF_L3L4_PLUS_MAE",
            "CF_L3L4_PLUS_ADVERSE_MICRO",
            "PNL_ATTRIBUTION",
        ],
        "forbidden": [
            "PARAM_OPTIMIZATION",
            "ALL_EXIT_30S_HARDCODE",
            "LIVE_PROMOTION",
            "ENFORCE_1",
            "PEG_PROMOTION",
        ],
        "peer_status": {
            "TF2BP_CSR499": "VALIDATED_FROZEN",
            "PEG_v3": "OBSERVATION_REJECT",
            "Arena_MM": "PROMISING_RESEARCH_NO_AUTO_PROMOTE",
            "CSR521": "HIGH_PRIORITY_CF_CONDITIONAL_UNWIND",
        },
    }
    return snap


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DAILY.mkdir(parents=True, exist_ok=True)
    snap = build_snapshot()
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    champ_dir = OUT_DIR / f"umm_csr504_{ts}"
    champ_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(UMM_SRC, champ_dir / "umm_strategy.py")
    (champ_dir / "champion.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Stable pointer + daily lock
    (OUT_DIR / "umm_csr504_CURRENT.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lock_path = DAILY / "2026-09-27_csr541_umm_champion_freeze_lock.json"
    lock_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "status": snap["status"],
        "code_hash": snap["code_hash"],
        "parameter_hash": snap["parameter_hash"],
        "policy_hash": snap["policy_hash"],
        "champion_dir": str(champ_dir),
        "lock": str(lock_path),
        "primary_umm_24h_bp": snap["primary_evidence"]["umm_24h_bp_22h"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
