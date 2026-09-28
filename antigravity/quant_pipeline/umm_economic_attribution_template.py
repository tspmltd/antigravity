#!/usr/bin/env python3
"""CSR-542 — UMM Economic Attribution template (Adverse Actual / Latency Condition).

RULES (locked):
  - Adverse / Post-fill Markout = ACTUAL outcome → may enter PnL identity (once)
  - Latency = CONDITION / explanatory variable → NEVER deducted as latency_cost_bp
  - Latency CF / CSR-521 Unwind CF = RESEARCH ONLY → never added to Actual Net
  - AE diagnostic KPIs ≠ markout component used in identity (pick one markout horizon)
  - Do not invent Gross Spread / AE / Latency fills without UMM trade-level join

Identity (Actual only):
  GrossSpread + AdverseMarkout + Inventory + Exit + Residual = RealizedNet
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))

SCHEMA_VERSION = "csr542_attr_v2"

# --- Primary evidence: user-presented integrated log @ 2026-09-27 22:00 JST ---
PRIMARY_2200 = {
    "asof_jst": "2026-09-27T22:00:00+09:00",
    "source": "user_presented_integrated_log_primary",
    "umm_24h_net_bp": 50.74,
    "umm_1h_net_bp": 46.48,
    "umm_trades_24h": 1176,
    "umm_win_rate_pct": 61.0,
    "umm_24h_bottom_09h_bp": -106.47,
    "recovery_09_to_22_bp": 157.21,
    "hold_ladder_1h": [
        {"level": "L0", "n": 27, "pnl_bp": 26.66, "hs_rate_pct": 0.0},
        {"level": "L1", "n": 14, "pnl_bp": 11.36, "hs_rate_pct": 0.0},
        {"level": "L2", "n": 2, "pnl_bp": 1.63, "hs_rate_pct": 0.0},
        {"level": "L3", "n": 10, "pnl_bp": 0.46, "hs_rate_pct": 10.0},
        {"level": "L4", "n": 5, "pnl_bp": 6.37, "hs_rate_pct": 0.0},
    ],
    "cf_research_only": {
        "actual_1h_bp": 46.48,
        "virtual_30s_bp": 58.84,
        "delta_30s_bp": 12.36,
        "delta_60s_bp": 7.22,
        "delta_120s_bp": 0.50,
        "l2_l3_only_delta_bp": 13.29,
        "id_ok": True,
        "note": "CF must not be added to Actual Net; L2+L3 Δ is independent CF",
    },
}

TRADE_LEVEL_SCHEMA = [
    "trade_id", "timestamp", "strategy", "side", "size",
    "fill_price", "mid_at_fill", "spread_bp", "gross_spread_bp",
    "ae_100ms_bp", "ae_1s_bp", "ae_3s_bp", "ae_10s_bp", "adverse_score",
    "adverse_markout_bp",  # single horizon used in Actual identity
    "latency_ms", "latency_bucket", "latency_risk_flag",
    "hold_seconds", "hold_ladder", "hard_stop", "exit_reason",
    "inventory_pnl_bp", "exit_pnl_bp", "realized_pnl_bp",
    "latency_cf_bp", "latency_cf_valid", "unwind_cf_bp",
]

LATENCY_BUCKETS = ["≤20ms", "20–40ms", "40–80ms", "80–150ms", "150–250ms", ">250ms"]
ADVERSE_SCORE_BANDS = ["0–20", "20–40", "40–60", "60–80", "80–100"]
ADVERSE_CROSS = ["Low", "Mid", "High"]
LATENCY_CROSS = ["≤40ms", "40–80ms", "80–150ms", ">150ms"]


def _blank_cell() -> Dict[str, Any]:
    return {"value": None, "status": "NEED_UMM_TRADE_JOIN", "in_actual_pnl": False}


def empty_latency_condition_table() -> List[Dict[str, Any]]:
    rows = []
    for b in LATENCY_BUCKETS:
        rows.append({
            "latency_bucket": b,
            "n": None,
            "share": None,
            "gross_spread_per_trade": None,
            "ae_1s": None,
            "ae_3s": None,
            "win_rate_pct": None,
            "ev_per_trade": None,
            "net_pnl_bp": None,
            "latency_cost_bp": "FORBIDDEN",  # structural double-count guard
            "role": "CONDITION",
        })
    return rows


def empty_adverse_window_table() -> List[Dict[str, Any]]:
    out = []
    for w in ("1h", "6h", "24h"):
        out.append({
            "window": w,
            "n": None,
            "ae_100ms": None,
            "ae_1s": None,
            "ae_3s": None,
            "ae_10s": None,
            "toxic_rate": None,
            "net_pnl_bp": None,
            "role": "ACTUAL_OUTCOME_DIAGNOSTIC",
            "note": "AE KPIs diagnostic; identity uses adverse_markout_bp once",
        })
    return out


def empty_adverse_score_table() -> List[Dict[str, Any]]:
    return [
        {
            "score_band": b,
            "n": None,
            "win_rate_pct": None,
            "ev_per_trade": None,
            "ae_1s": None,
            "ae_3s": None,
            "net_pnl_bp": None,
        }
        for b in ADVERSE_SCORE_BANDS
    ]


def empty_adverse_x_latency() -> List[Dict[str, Any]]:
    rows = []
    for lat in LATENCY_CROSS:
        for adv in ADVERSE_CROSS:
            rows.append({
                "latency": lat,
                "adverse": adv,
                "n": None,
                "ae_1s": None,
                "ev_per_trade": None,
                "net_pnl_bp": None,
                "rule": "do_not_re_deduct_as_latency_cost",
            })
    return rows


def empty_actual_attribution(windows: Optional[List[str]] = None) -> Dict[str, Any]:
    windows = windows or ["1h", "6h", "24h"]
    components = [
        ("gross_spread_capture_bp", True),
        ("adverse_markout_bp", True),
        ("inventory_holding_bp", True),
        ("exit_hard_stop_bp", True),
        ("other_residual_bp", True),
        ("latency_cost_bp", False),  # FORBIDDEN in Actual
    ]
    table: Dict[str, Any] = {"windows": {}}
    for w in windows:
        cells = {}
        for name, allowed in components:
            if not allowed:
                cells[name] = {
                    "value": None,
                    "status": "FORBIDDEN_IN_ACTUAL",
                    "in_actual_pnl": False,
                    "reason": "Latency is CONDITION not Actual cost",
                }
            else:
                cells[name] = {
                    "value": None,
                    "status": "NEED_JOIN" if name != "inventory_holding_bp" else "PENDING",
                    "in_actual_pnl": True,
                }
        cells["net_umm_pnl_bp"] = {"value": None, "status": "PENDING", "in_actual_pnl": True}
        cells["identity_residual_bp"] = {"value": None, "status": "PENDING"}
        cells["identity_ok"] = False
        table["windows"][w] = cells
    table["identity_formula"] = (
        "gross_spread + adverse_markout + inventory + exit + residual = realized_net"
    )
    table["forbidden_in_identity"] = ["latency_cost_bp", "latency_cf_bp", "unwind_cf_bp"]
    return table


def check_identity(
    gross: Optional[float],
    adverse: Optional[float],
    inventory: Optional[float],
    exit_eff: Optional[float],
    residual: Optional[float],
    realized: float,
    tol: float = 1e-6,
) -> Dict[str, Any]:
    parts = [gross, adverse, inventory, exit_eff, residual]
    if any(p is None for p in parts):
        # Partial identity: inventory-only path (Hold Ladder) when orthogonal layers unknown
        return {
            "mode": "partial_or_incomplete",
            "identity_ok": False,
            "residual": None,
            "note": "full orthogonal identity requires all Actual components",
        }
    s = float(gross) + float(adverse) + float(inventory) + float(exit_eff) + float(residual)
    r = s - float(realized)
    return {
        "mode": "full_orthogonal",
        "sum_bp": round(s, 6),
        "realized_bp": float(realized),
        "identity_residual_bp": round(r, 6),
        "identity_ok": abs(r) < tol,
    }


def check_hold_ladder_identity(ladder: List[Dict[str, Any]], realized_1h: float, tol: float = 0.02) -> Dict[str, Any]:
    s = sum(float(x["pnl_bp"]) for x in ladder)
    n = sum(int(x["n"]) for x in ladder)
    r = s - float(realized_1h)
    return {
        "mode": "hold_ladder_partition",
        "sum_bp": round(s, 6),
        "realized_1h_bp": float(realized_1h),
        "identity_residual_bp": round(r, 6),
        "identity_ok": abs(r) < tol,
        "n_trades": n,
        "note": "Inventory Hold Ladder partitions Actual 1h Net; not orthogonal to Gross/Adverse",
    }


def build_template_v2(fill_primary_2200: bool = True) -> Dict[str, Any]:
    actual = empty_actual_attribution()
    report: Dict[str, Any] = {
        "csr": 542,
        "schema_version": SCHEMA_VERSION,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "champion": "UMM_v1_CSR504_CHAMPION_CANDIDATE",
        "rules": {
            "adverse": "ACTUAL_PRICE_OUTCOME → may enter PnL identity once",
            "latency": "CONDITION → never latency_cost_bp in Actual",
            "latency_cf": "RESEARCH_ONLY → never add to Actual Net",
            "csr521_cf": "RESEARCH_ONLY → never add to Actual Net",
            "double_count_guard": "same markout loss must not be deducted as Adverse AND Latency",
            "ae_vs_markout": "AE_* are diagnostic KPIs; identity uses one adverse_markout_bp horizon",
        },
        "trade_level_schema": TRADE_LEVEL_SCHEMA,
        "A_actual_pnl_attribution": actual,
        "B_adverse_actual_outcome": {
            "by_window": empty_adverse_window_table(),
            "by_score": empty_adverse_score_table(),
            "umm_only_required": True,
            "status": "NEED_UMM_TRADE_JOIN",
        },
        "C_latency_condition": {
            "by_bucket": empty_latency_condition_table(),
            "kpi": {
                "1h": {"p50": None, "p75": None, "p90": None, "p95": None, "p99": None,
                       "gt80_rate": None, "gt150_rate": None, "gt250_rate": None},
                "24h": {"p50": None, "p75": None, "p90": None, "p95": None, "p99": None,
                        "gt80_rate": None, "gt150_rate": None, "gt250_rate": None},
            },
            "status": "NEED_UMM_TRADE_JOIN",
            "latency_cost_in_actual": False,
        },
        "D_adverse_x_latency_cross": empty_adverse_x_latency(),
        "E_latency_counterfactual": {
            "role": "RESEARCH_ONLY",
            "1h": {
                "matched_high_n": None,
                "matched_low_n": None,
                "high_ev_per_trade": None,
                "matched_low_ev_per_trade": None,
                "latency_cf_delta_bp": None,
                "cf_valid": None,
            },
            "24h": {
                "matched_high_n": None,
                "matched_low_n": None,
                "high_ev_per_trade": None,
                "matched_low_ev_per_trade": None,
                "latency_cf_delta_bp": None,
                "cf_valid": None,
            },
            "forbidden": "ActualNet + LatencyCFΔ = improved_pnl",
        },
        "F_dashboard": {
            "ACTUAL": {},
            "CONDITIONS": {},
            "ADVERSE_DIAG": {},
            "RESEARCH_CF": {},
            "RULE": {
                "latency_in_actual": False,
                "adverse_in_actual": True,
                "cf_in_actual": False,
            },
        },
        "hold_ladder_partition": None,
        "primary_topline": None,
        "gov": {"LIVE": "OFF", "WIRE": "NO", "ENFORCE": 0, "champion_freeze": True},
    }

    if fill_primary_2200:
        p = PRIMARY_2200
        ladder_id = check_hold_ladder_identity(p["hold_ladder_1h"], p["umm_1h_net_bp"])
        report["primary_topline"] = {
            "asof_jst": p["asof_jst"],
            "source": p["source"],
            "umm_24h_net_bp": p["umm_24h_net_bp"],
            "umm_1h_net_bp": p["umm_1h_net_bp"],
            "trades_24h": p["umm_trades_24h"],
            "win_rate_pct": p["umm_win_rate_pct"],
            "bottom_09h_bp": p["umm_24h_bottom_09h_bp"],
            "recovery_bp": p["recovery_09_to_22_bp"],
        }
        report["hold_ladder_partition"] = {
            "window": "1h@22:00",
            "rows": p["hold_ladder_1h"],
            "identity": ladder_id,
            "interpretation": {
                "L0_L1_edge_bp": round(26.66 + 11.36, 2),
                "L3_note": "thin + HS10% — not 'hold longer = more profit'",
                "csr521_aim": "protect L0/L1; selectively unwind toxic L2/L3/L4 only",
            },
        }
        # Inventory path fills Actual 1h inventory component; orthogonal layers remain NEED_JOIN
        w1 = report["A_actual_pnl_attribution"]["windows"]["1h"]
        w1["inventory_holding_bp"] = {
            "value": p["umm_1h_net_bp"],
            "status": "MEASURED_HOLD_LADDER_PARTITION",
            "in_actual_pnl": True,
            "note": "Full 1h Net is partitioned by Hold Ladder; not yet orthogonalized vs Gross/Adverse/Exit",
        }
        w1["gross_spread_capture_bp"] = {
            "value": None,
            "status": "NEED_UMM_FILL_MID_JOIN",
            "in_actual_pnl": True,
        }
        w1["adverse_markout_bp"] = {
            "value": None,
            "status": "NEED_UMM_ONLY_AE_JOIN",
            "in_actual_pnl": True,
        }
        w1["exit_hard_stop_bp"] = {
            "value": None,
            "status": "NEED_EXIT_REASON_JOIN",
            "in_actual_pnl": True,
        }
        w1["other_residual_bp"] = {
            "value": None,
            "status": "PENDING_ORTHOGONAL_DECOMP",
            "in_actual_pnl": True,
        }
        w1["net_umm_pnl_bp"] = {
            "value": p["umm_1h_net_bp"],
            "status": "MEASURED",
            "in_actual_pnl": True,
        }
        # Hold-ladder identity (partition), not full orthogonal
        w1["identity_residual_bp"] = {
            "value": ladder_id["identity_residual_bp"],
            "status": "HOLD_LADDER_PARTITION",
            "mode": "hold_ladder_partition",
        }
        w1["identity_ok"] = ladder_id["identity_ok"]
        w1["orthogonal_identity_ok"] = False
        w1["orthogonal_identity_note"] = (
            "Gross/Adverse/Exit still NEED_JOIN; do not force orthogonal residual=0 by invention"
        )

        w24 = report["A_actual_pnl_attribution"]["windows"]["24h"]
        w24["net_umm_pnl_bp"] = {
            "value": p["umm_24h_net_bp"],
            "status": "MEASURED",
            "in_actual_pnl": True,
        }
        w24["inventory_holding_bp"] = {
            "value": None,
            "status": "NEED_24H_HOLD_LADDER",
            "in_actual_pnl": True,
        }

        report["F_dashboard"]["ACTUAL"] = {
            "gross_spread_bp": None,
            "adverse_markout_bp": None,
            "inventory_hold_ladder_1h_bp": p["umm_1h_net_bp"],
            "exit_hard_stop_bp": None,
            "residual_bp": None,
            "net_1h_bp": p["umm_1h_net_bp"],
            "net_24h_bp": p["umm_24h_net_bp"],
            "hold_ladder_identity_ok": ladder_id["identity_ok"],
            "orthogonal_identity_ok": False,
        }
        report["F_dashboard"]["CONDITIONS"] = {
            "latency_p50_ms": None,
            "latency_p90_ms": None,
            "gt80_pct": None,
            "gt150_pct": None,
            "status": "NEED_UMM_TRADE_JOIN",
        }
        report["F_dashboard"]["ADVERSE_DIAG"] = {
            "ae_100ms": None,
            "ae_1s": None,
            "ae_3s": None,
            "ae_10s": None,
            "status": "NEED_UMM_ONLY_EXTRACT",
        }
        cf = p["cf_research_only"]
        report["F_dashboard"]["RESEARCH_CF"] = {
            "csr521_actual_1h_bp": cf["actual_1h_bp"],
            "csr521_virtual_30s_bp": cf["virtual_30s_bp"],
            "csr521_delta_30s_bp": cf["delta_30s_bp"],
            "csr521_delta_60s_bp": cf["delta_60s_bp"],
            "csr521_delta_120s_bp": cf["delta_120s_bp"],
            "csr521_l2_l3_delta_bp": cf["l2_l3_only_delta_bp"],
            "csr521_id_ok": cf["id_ok"],
            "latency_cf_delta_bp": None,
            "added_to_actual": False,
        }
        report["csr521_cf_block"] = cf

    return report


def render_markdown(report: Dict[str, Any]) -> str:
    p = report.get("primary_topline") or {}
    hl = report.get("hold_ladder_partition") or {}
    rows = hl.get("rows") or []
    ident = hl.get("identity") or {}
    lines = [
        "# UMM Economic Attribution Template v2 (CSR-542)",
        "",
        f"- asof: `{p.get('asof_jst')}`",
        f"- source: `{p.get('source')}`",
        f"- schema: `{report.get('schema_version')}`",
        "",
        "## Rules",
        "- Adverse = ACTUAL outcome → in identity once",
        "- Latency = CONDITION → **not** in Actual PnL",
        "- Latency CF / CSR-521 CF = RESEARCH ONLY → **not** added to Actual",
        "",
        "## A. Topline (measured)",
        f"| metric | value |",
        f"|---|---|",
        f"| UMM 24h Net | **{p.get('umm_24h_net_bp')} bp** |",
        f"| UMM 1h Net | **{p.get('umm_1h_net_bp')} bp** |",
        f"| Trades 24h | {p.get('trades_24h')} |",
        f"| Win rate | {p.get('win_rate_pct')}% |",
        f"| 09:00 bottom → 22:00 recovery | {p.get('bottom_09h_bp')} → Δ {p.get('recovery_bp')} bp |",
        "",
        "## B. Hold Ladder partition (1h @22:00) — identity",
        "| Level | n | PnL bp | HS% |",
        "|---|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['level']} | {r['n']} | {r['pnl_bp']:+.2f} | {r['hs_rate_pct']:.1f} |"
        )
    lines += [
        "",
        f"- sum = **{ident.get('sum_bp')} bp** · realized = **{ident.get('realized_1h_bp')} bp**",
        f"- residual = **{ident.get('identity_residual_bp')} bp** · identity_ok = **{ident.get('identity_ok')}**",
        "",
        "## C. Actual orthogonal layers (NEED JOIN — not invented)",
        "| Component | 1h | in Actual |",
        "|---|---|---|",
        "| Gross Spread Capture | NEED_UMM_FILL_MID_JOIN | YES |",
        "| Adverse Markout | NEED_UMM_ONLY_AE_JOIN | YES |",
        "| Inventory (Hold Ladder partition) | +46.48 bp (full net) | YES* |",
        "| Exit / Hard Stop | NEED_EXIT_REASON_JOIN | YES |",
        "| Latency Cost | **FORBIDDEN** | NO |",
        "",
        "\\* Until orthogonal join exists, Hold Ladder is the closed Actual partition; "
        "Gross/Adverse/Exit remain null (not zero).",
        "",
        "## D. Latency Condition (empty until join)",
        "| Bucket | n | AE_1s | EV/trade | latency_cost_bp |",
        "|---|---|---|---|---|",
    ]
    for row in report["C_latency_condition"]["by_bucket"]:
        lines.append(
            f"| {row['latency_bucket']} | — | — | — | FORBIDDEN |"
        )
    cf = report.get("csr521_cf_block") or {}
    lines += [
        "",
        "## E. Research CF (not added to Actual)",
        f"| Actual 1h | {cf.get('actual_1h_bp')} bp |",
        f"| Virtual @30s | {cf.get('virtual_30s_bp')} bp |",
        f"| Δ30s | {cf.get('delta_30s_bp')} bp |",
        f"| Δ60s | {cf.get('delta_60s_bp')} bp |",
        f"| Δ120s | {cf.get('delta_120s_bp')} bp |",
        f"| L2+L3 Δ (independent CF) | {cf.get('l2_l3_only_delta_bp')} bp |",
        f"| id_ok | {cf.get('id_ok')} |",
        "",
        "FORBIDDEN: `46.48 + Δ = improved production PnL`",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report = build_template_v2(fill_primary_2200=True)
    json_path = OUT / "2026-09-27_csr542_umm_economic_attribution_template_v2.json"
    md_path = OUT / "2026-09-27_csr542_umm_economic_attribution_template_v2.md"
    lock_path = OUT / "2026-09-27_csr542_adverse_latency_no_double_count_lock.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    lock = {
        "csr": 542,
        "ts_jst": report["ts_jst"],
        "schema_version": SCHEMA_VERSION,
        "rules": report["rules"],
        "hold_ladder_identity_ok": (report.get("hold_ladder_partition") or {}).get("identity", {}).get("identity_ok"),
        "orthogonal_layers_filled": False,
        "latency_in_actual": False,
        "cf_in_actual": False,
        "files": [str(json_path.name), str(md_path.name)],
        "gov": report["gov"],
    }
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    ident = (report.get("hold_ladder_partition") or {}).get("identity") or {}
    print(json.dumps({
        "ok": True,
        "json": str(json_path),
        "md": str(md_path),
        "lock": str(lock_path),
        "hold_ladder_identity_ok": ident.get("identity_ok"),
        "residual_bp": ident.get("identity_residual_bp"),
        "latency_in_actual": False,
        "orthogonal_ready": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
