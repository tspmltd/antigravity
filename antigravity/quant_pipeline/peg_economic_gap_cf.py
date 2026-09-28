"""CSR-537 — PEG_v3 Economic Gap attribution (OBSERVE · no param change · ENFORCE=0).

結論固定:
  Maker化で AE/Toxic は改善しても PnL を大幅破壊 → OBSERVATION ONLY · PROMOTION=NO
  Adverse では敗因説明不可。次は Entry ではなく Exit/Selection 帰属。

PEG ECONOMIC GAP
  ├── Entry Selection Cost (baseline_only / peg_only / both)
  ├── Execution Quality (entry improvement / AE_1s / AE_3s)
  └── Exit Friction (AgingGuard / BE3 / other)

最重要CF（各PEG約定）:
  actual_peg_pnl, cf_no_aging_pnl, cf_baseline_exit_pnl,
  delta_aging, delta_baseline_exit, exit_reason

このモジュールは exec_audit JSONL の軽量集計のみ。戦略 params は変更しない。
P0 CSR-521 完走より下位。WIRE=NO · LIVE=OFF。
"""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

JST = timezone(timedelta(hours=9))
BASE = "/home/azureuser/antigravity"
AUDIT = f"{BASE}/data/mm_research/exec_audit_peg_v3.jsonl"
OUT_DIR = f"{BASE}/data/mm_research/daily"

CSR = "CSR-537"
WIRE = "NO"
ENFORCE = 0
PROMOTION = False
PARAM_CHANGE = False

# CF columns required on each PEG fill (DuckDB / Parquet later)
CF_COLUMNS = [
    "trade_id",
    "side",
    "entry_ts",
    "exit_ts",
    "exit_reason",
    "actual_peg_pnl_bp",
    "cf_no_aging_pnl_bp",       # hold past AgingGuard to 10s/30s (existing pnl_if_hold_*)
    "cf_baseline_exit_pnl_bp",  # if held to Baseline-equivalent exit (NULL until join)
    "delta_aging_bp",           # cf_no_aging − actual  (+ = aging hurt)
    "delta_baseline_exit_bp",   # cf_baseline_exit − actual
    "mfe_bp",
    "mae_bp",
    "hold_duration_sec",
    "be_armed",
    "aging_exit_triggered",
    "cf_status",  # NULL_PENDING | VALID | INVALID
]


@dataclass
class PegGapVerdict:
    csr: str = CSR
    status: str = "OBSERVATION_ONLY"
    promotion: bool = PROMOTION
    param_change: bool = PARAM_CHANGE
    wire: str = WIRE
    enforce: int = ENFORCE
    live: str = "OFF"
    primary_suspect: str = "AgingGuard"
    convicted: bool = False  # suspect ≠ convicted until CF attribution
    be3: str = "INACTIVE_NEED_ATTRIBUTION"
    fake_liquidity: str = "ZERO_FIRE_NEED_CONDITION_AUDIT"
    capture_raw_mean: str = "INVALID_FOR_GATING"
    architecture: str = "CSR-535 KEEP — do not make PEG a Direction Agent"
    next_cf: List[str] = field(
        default_factory=lambda: [
            "Entry Selection Cost (baseline_only/peg_only/both)",
            "Execution Quality (AE improved but PnL destroyed)",
            "Exit Friction (Aging primary CF: hold to 10s/30s vs actual)",
        ]
    )
    evidence_22jst: Dict[str, Any] = field(
        default_factory=lambda: {
            "baseline_bp": 87.13,
            "peg_v3_bp": -24.15,
            "delta_bp": -111.28,
            "ae_1s_delta_bp": 1.30,
            "ae_3s_delta_bp": 1.20,
            "baseline_trades": 105,
            "peg_trades": 17,
            "note": "AE↑ ∧ PnL↓ ∧ n_peg≪n_base → selection+exit mix; not toxic-fill story",
        }
    )


def _load_exits(path: str = AUDIT, max_lines: int = 20_000) -> List[Dict[str, Any]]:
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= max_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            life = obj.get("position_lifecycle")
            if not isinstance(life, dict):
                continue
            if life.get("exit_reason") is None and life.get("realized_pnl_bp") is None:
                continue
            rows.append(obj)
    return rows


def summarize_exec_audit(path: str = AUDIT) -> Dict[str, Any]:
    """Light attribution from existing EXEC_AUDIT — no strategy restart."""
    exits = _load_exits(path)
    reasons = Counter()
    aging_deltas_10: List[float] = []
    aging_deltas_30: List[float] = []
    be_armed_n = 0
    mfe_ge_3 = 0
    aging_n = 0
    for obj in exits:
        life = obj["position_lifecycle"]
        reason = str(life.get("exit_reason") or "UNKNOWN")
        reasons[reason] += 1
        actual = life.get("realized_pnl_bp")
        mfe = life.get("mfe_bp")
        if life.get("be_armed"):
            be_armed_n += 1
        if isinstance(mfe, (int, float)) and float(mfe) >= 3.0:
            mfe_ge_3 += 1
        if reason == "AGING_GUARD" or life.get("aging_exit_triggered"):
            aging_n += 1
            cf = obj.get("counterfactual") or {}
            if isinstance(actual, (int, float)):
                if cf.get("pnl_if_hold_10s") is not None:
                    aging_deltas_10.append(float(cf["pnl_if_hold_10s"]) - float(actual))
                if cf.get("pnl_if_hold_30s") is not None:
                    aging_deltas_30.append(float(cf["pnl_if_hold_30s"]) - float(actual))

    def _avg(xs: List[float]) -> Optional[float]:
        return round(sum(xs) / len(xs), 3) if xs else None

    return {
        "csr": CSR,
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "n_exits_audited": len(exits),
        "exit_reason_counts": dict(reasons),
        "aging_exit_count": aging_n,
        "be_attribution_shadow": {
            "be_arm_count": be_armed_n,  # armed at exit time
            "be_trigger_count": int(reasons.get("BE_TRIGGERED", 0)),
            "mfe_ge_3bp_count": mfe_ge_3,
            "armed_then_exit_other_count": be_armed_n - int(reasons.get("BE_TRIGGERED", 0)),
            "note": "arm@exit snapshot · live arm-then-other needs tick path; SHADOW only",
        },
        "aging_cf_preview": {
            "n_with_hold10": len(aging_deltas_10),
            "mean_delta_aging_vs_hold10_bp": _avg(aging_deltas_10),
            "n_with_hold30": len(aging_deltas_30),
            "mean_delta_aging_vs_hold30_bp": _avg(aging_deltas_30),
            "definition": "delta = cf_hold − actual_peg (+ = aging cut left money on table OR avoided worse)",
            "status": "PREVIEW_NOT_CONVICTION",
        },
        "cf_schema_required": CF_COLUMNS,
        "verdict": asdict(PegGapVerdict()),
        "wire": WIRE,
        "enforce": ENFORCE,
        "param_change": PARAM_CHANGE,
        "promotion": PROMOTION,
        "p0_priority": "CSR-521 24h完走 remains TOP — this module is P7 subordinate",
    }


def run() -> Dict[str, Any]:
    out = summarize_exec_audit()
    os.makedirs(OUT_DIR, exist_ok=True)
    path = f"{OUT_DIR}/{datetime.now(JST).strftime('%Y-%m-%d')}_csr537_peg_economic_gap.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    out["path"] = path
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))
