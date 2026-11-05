#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TF1A entry-gate validation (observation only). WIRE=NO. Does not change gates.

Answers: does each block (spread / ENTER v2 parts) improve forward mid EV
and/or realized close EV? Writes JSON report under mm_research.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

AG = Path(__file__).resolve().parents[2]
OUT_DIR = AG / "data" / "mm_research" / "tf1a_wobi_honest_dryrun"
DEFAULT_GATE = OUT_DIR / "gate_ticks.jsonl"
DEFAULT_LEDGER = OUT_DIR / "paired_ledger.jsonl"
DEFAULT_AE = AG / "data" / "adverse_excursion_records.jsonl"
DEFAULT_OUT = OUT_DIR / "gate_validation_report.json"
JST = timezone(timedelta(hours=9))

SPREAD_MAX_BP = 2.0
HORIZONS_S = (15, 30, 60, 120, 300)
SPREAD_BUCKETS = (
    ("<1.5", 0.0, 1.5),
    ("1.5-2.0", 1.5, 2.0),
    ("2.0-2.5", 2.0, 2.5),
    ("2.5-3.0", 2.5, 3.0),
    (">=3.0", 3.0, 1e9),
)


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _bucket(spread_bp: float) -> str:
    for name, lo, hi in SPREAD_BUCKETS:
        if lo <= spread_bp < hi:
            return name
    return ">=3.0"


def _mean_se(xs: Sequence[float]) -> Tuple[float, float, int]:
    n = len(xs)
    if n == 0:
        return float("nan"), float("nan"), 0
    mean = sum(xs) / n
    se = statistics.stdev(xs) / math.sqrt(n) if n > 1 else 0.0
    return mean, se, n


def _stat(xs: Sequence[float]) -> Dict[str, Any]:
    mean, se, n = _mean_se(xs)
    if n == 0:
        return {"n": 0, "ev_bp": None, "se_bp": None, "hit_pos": None, "p25": None, "p75": None}
    ys = sorted(xs)
    return {
        "n": n,
        "ev_bp": round(mean, 3),
        "se_bp": round(se, 3),
        "hit_pos": round(sum(1 for x in xs if x > 0) / n, 3),
        "p25": round(ys[max(0, n // 4)], 3),
        "p75": round(ys[min(n - 1, (3 * n) // 4)], 3),
    }


def _enter_v2(g: Dict[str, Any]) -> bool:
    """Reconstruct ENTER v2 from logged flags when present; else from features."""
    flags = (
        g.get("enter_v2_base"),
        g.get("enter_v2_fresh5"),
        g.get("enter_v2_micro_ok"),
        g.get("enter_v2_agent_ok"),
    )
    if all(x is not None for x in flags):
        return bool(flags[0] and flags[1] and flags[2] and flags[3])
    # Fallback: old_ok was enter_v2 after CSR enter-v2 wire
    return bool(g.get("old_ok"))


def _precompute_fwd(
    ts: List[float], mids: List[float], horizons: Sequence[int] = HORIZONS_S
) -> Dict[int, Dict[str, Optional[float]]]:
    """O(n log n) forward mid returns via bisect."""
    n = len(ts)
    out: Dict[int, Dict[str, Optional[float]]] = {i: {} for i in range(n)}
    for h in horizons:
        key = f"fwd_{h}s"
        grace = float(h) + 15.0
        for i in range(n):
            m0 = mids[i]
            if m0 <= 0:
                out[i][key] = None
                continue
            j = bisect.bisect_left(ts, ts[i] + float(h), lo=i + 1)
            if j >= n or (ts[j] - ts[i]) > grace:
                out[i][key] = None
                continue
            m1 = mids[j]
            out[i][key] = (m1 / m0 - 1.0) * 1e4 if m1 > 0 else None
    return out


def _onset_mask(rows: List[Dict[str, Any]], pred, min_gap_s: float = 30.0) -> List[bool]:
    out = [False] * len(rows)
    last_ts = -1e18
    for i, g in enumerate(rows):
        if not pred(g):
            continue
        ts = float(g.get("ts") or 0.0)
        if ts - last_ts >= min_gap_s:
            out[i] = True
            last_ts = ts
    return out


def validate_forward(gates: List[Dict[str, Any]]) -> Dict[str, Any]:
    ts = [float(g.get("ts") or 0.0) for g in gates]
    mids = [float(g.get("mid") or 0.0) for g in gates]
    fwd_map = _precompute_fwd(ts, mids)

    def collect(indices: List[int], horizon: str) -> List[float]:
        out: List[float] = []
        for i in indices:
            v = fwd_map[i].get(horizon)
            if v is not None and not math.isnan(v):
                out.append(float(v))
        return out

    enter_idx = [i for i, g in enumerate(gates) if _enter_v2(g)]
    onset = _onset_mask(gates, _enter_v2, 30.0)
    enter_onset = [i for i, ok in enumerate(onset) if ok]

    # Spread block counterfactual on ENTER v2 onsets
    by_spread: Dict[str, Any] = {}
    for name, lo, hi in SPREAD_BUCKETS:
        idx = [
            i
            for i in enter_onset
            if lo <= float(gates[i].get("spread_bp") or 0.0) < hi
        ]
        cell: Dict[str, Any] = {"n_signals": len(idx)}
        for h in HORIZONS_S:
            key = f"fwd_{h}s"
            cell[key] = _stat(collect(idx, key))
        by_spread[name] = cell

    # Policy A: current (enter_v2 AND NOT spread_block)
    # Policy B: enter_v2 ignore spread
    # Policy C: enter_v2 AND spread<=2.5
    # Policy D: enter_v2 AND spread<=3.0
    policies: Dict[str, Any] = {}
    defs = {
        "current_spread_le_2.0": lambda g: _enter_v2(g) and float(g.get("spread_bp") or 0) <= SPREAD_MAX_BP,
        "ignore_spread": lambda g: _enter_v2(g),
        "spread_le_2.5": lambda g: _enter_v2(g) and float(g.get("spread_bp") or 0) <= 2.5,
        "spread_le_3.0": lambda g: _enter_v2(g) and float(g.get("spread_bp") or 0) <= 3.0,
        "blocked_only_spread_gt_2": lambda g: _enter_v2(g) and float(g.get("spread_bp") or 0) > SPREAD_MAX_BP,
    }
    for pname, pred in defs.items():
        mask = _onset_mask(gates, pred, 30.0)
        idx = [i for i, ok in enumerate(mask) if ok]
        cell: Dict[str, Any] = {"n_signals": len(idx)}
        for h in HORIZONS_S:
            key = f"fwd_{h}s"
            cell[key] = _stat(collect(idx, key))
        policies[pname] = cell

    # ENTER v2 component ablation (onset of base, then add filters)
    ablations: Dict[str, Any] = {}
    ab_defs = {
        "base_only": lambda g: bool(g.get("enter_v2_base")),
        "base+fresh5": lambda g: bool(g.get("enter_v2_base") and g.get("enter_v2_fresh5")),
        "base+fresh5+micro": lambda g: bool(
            g.get("enter_v2_base") and g.get("enter_v2_fresh5") and g.get("enter_v2_micro_ok")
        ),
        "enter_v2_full": _enter_v2,
        "cand_ab_v1_observe": lambda g: bool(g.get("cand_ab_v1")),
        "drop_fresh5": lambda g: bool(
            g.get("enter_v2_base") and g.get("enter_v2_micro_ok") and g.get("enter_v2_agent_ok")
        ),
        "drop_micro": lambda g: bool(
            g.get("enter_v2_base") and g.get("enter_v2_fresh5") and g.get("enter_v2_agent_ok")
        ),
        "drop_agent": lambda g: bool(
            g.get("enter_v2_base") and g.get("enter_v2_fresh5") and g.get("enter_v2_micro_ok")
        ),
    }
    # Only rows that have enter_v2_* fields
    has_v2 = [g for g in gates if g.get("enter_v2_base") is not None]
    if has_v2:
        # Ablations use same clock as full series: map has_v2 row -> gates index by ts
        # Faster: compute on has_v2 mid path (flat-only already).
        ts2 = [float(g.get("ts") or 0.0) for g in has_v2]
        mid2 = [float(g.get("mid") or 0.0) for g in has_v2]
        fwd2_map = _precompute_fwd(ts2, mid2)

        def collect2(indices: List[int], horizon: str) -> List[float]:
            out: List[float] = []
            for i in indices:
                v = fwd2_map[i].get(horizon)
                if v is not None:
                    out.append(float(v))
            return out

        for pname, pred in ab_defs.items():
            mask = _onset_mask(has_v2, pred, 30.0)
            idx = [i for i, ok in enumerate(mask) if ok]
            cell = {"n_signals": len(idx)}
            for h in HORIZONS_S:
                key = f"fwd_{h}s"
                cell[key] = _stat(collect2(idx, key))
            ablations[pname] = cell

    # Coverage
    n = len(gates)
    coverage = {
        "n_ticks": n,
        "spread_block_pct": round(100.0 * sum(1 for g in gates if g.get("spread_block")) / max(n, 1), 2),
        "enter_v2_pct": round(100.0 * len(enter_idx) / max(n, 1), 2),
        "enter_v2_and_spread_ok_pct": round(
            100.0
            * sum(1 for g in gates if _enter_v2(g) and not g.get("spread_block"))
            / max(n, 1),
            2,
        ),
        "posted_pct": round(100.0 * sum(1 for g in gates if g.get("posted")) / max(n, 1), 2),
        "spread_gt_2_pct": round(
            100.0 * sum(1 for g in gates if float(g.get("spread_bp") or 0) > 2.0) / max(n, 1), 2
        ),
    }

    return {
        "coverage": coverage,
        "enter_v2_onset_by_spread": by_spread,
        "spread_policies": policies,
        "enter_v2_ablations": ablations,
        "n_enter_v2_ticks": len(enter_idx),
        "n_enter_v2_onset_30s": len(enter_onset),
        "n_with_enter_v2_flags": len(has_v2),
    }


def validate_realized(gates: List[Dict[str, Any]], ledger: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Join closes to nearest prior posted/enter gate for entry spread."""
    if not ledger or not gates:
        return {"n": 0}

    # Prefer fills that have AE fields (newer schema)
    joined: List[Dict[str, Any]] = []
    gate_ts = [float(g.get("ts") or 0.0) for g in gates]

    for row in ledger:
        ts = float(row.get("ts") or 0.0)
        if ts <= 0:
            continue
        # nearest gate at or before close ts within hold window; prefer posted
        # Use entry ≈ close_ts - hold_s
        hold = float(row.get("hold_s") or 0.0)
        fill_ts = ts - hold if hold > 0 else ts
        best = None
        best_dt = 1e9
        # linear scan near end — gates sorted
        # binary-ish: walk from end
        lo, hi = 0, len(gates) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if gate_ts[mid] <= fill_ts + 0.5:
                lo = mid
            else:
                hi = mid - 1
        for j in range(max(0, lo - 40), lo + 1):
            g = gates[j]
            dt = fill_ts - gate_ts[j]
            if dt < -0.5 or dt > 120.0:
                continue
            score = dt
            if g.get("posted") or g.get("working_buy"):
                score -= 50.0
            elif _enter_v2(g):
                score -= 20.0
            if score < best_dt:
                best_dt = score
                best = g
        if not best:
            continue
        joined.append(
            {
                "pnl_bp": float(row.get("pnl_bp") or 0.0),
                "hold_s": hold,
                "exit_reason": str(row.get("exit_reason") or row.get("exit_side") or ""),
                "mfe_bp": row.get("mfe_bp"),
                "mae_bp": row.get("mae_bp"),
                "is_win": bool(row.get("is_win")),
                "entry_spread_bp": float(best.get("spread_bp") or 0.0),
                "spread_block_would": float(best.get("spread_bp") or 0.0) > SPREAD_MAX_BP,
                "enter_v2": _enter_v2(best),
                "posted": bool(best.get("posted")),
                "regime": best.get("regime"),
                "ret60_bp": best.get("ret60_bp"),
                "wobi": best.get("wobi"),
                "ofi": best.get("ofi"),
            }
        )

    by_spread: Dict[str, Any] = {}
    for name, lo, hi in SPREAD_BUCKETS:
        xs = [r["pnl_bp"] for r in joined if lo <= r["entry_spread_bp"] < hi]
        mfes = [float(r["mfe_bp"]) for r in joined if r.get("mfe_bp") is not None and lo <= r["entry_spread_bp"] < hi]
        maes = [float(r["mae_bp"]) for r in joined if r.get("mae_bp") is not None and lo <= r["entry_spread_bp"] < hi]
        cell = _stat(xs)
        cell["mfe_mean"] = round(sum(mfes) / len(mfes), 3) if mfes else None
        cell["mae_mean"] = round(sum(maes) / len(maes), 3) if maes else None
        cell["n_with_ae"] = min(len(mfes), len(maes))
        by_spread[name] = cell

    current = [r for r in joined if not r["spread_block_would"]]
    blocked_cf = [r for r in joined if r["spread_block_would"]]  # should be rare under current policy

    return {
        "n_joined": len(joined),
        "n_ledger": len(ledger),
        "all": _stat([r["pnl_bp"] for r in joined]),
        "realized_by_entry_spread": by_spread,
        "current_spread_ok": _stat([r["pnl_bp"] for r in current]),
        "would_have_been_blocked": _stat([r["pnl_bp"] for r in blocked_cf]),
        "note": "Under current policy almost all fills have entry_spread<=2.0; blocked_cf is near-empty by construction.",
    }


def validate_ae_tf1a(ae_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    tf = [
        r
        for r in ae_rows
        if "TF1A" in str(r.get("strategy") or r.get("lane") or r.get("trade_id") or "")
        or str(r.get("strategy_id") or "").upper().startswith("TF1A")
    ]
    if not tf:
        # try any recent with tags
        sample_keys = sorted(ae_rows[-1].keys()) if ae_rows else []
        return {"n_tf1a": 0, "ae_sample_keys": sample_keys, "n_ae_total": len(ae_rows)}
    mfes = [float(r["mfe_bp"]) for r in tf if r.get("mfe_bp") is not None]
    maes = [float(r["mae_bp"]) for r in tf if r.get("mae_bp") is not None]
    return {
        "n_tf1a": len(tf),
        "mfe": _stat(mfes),
        "mae": _stat(maes),
    }


def verdict(fwd: Dict[str, Any], realized: Dict[str, Any]) -> Dict[str, Any]:
    """GO/NO-GO style calls. Conservative: need clear EV lift to keep a block."""
    pol = fwd.get("spread_policies") or {}
    cur = (pol.get("current_spread_le_2.0") or {}).get("fwd_60s") or {}
    ign = (pol.get("ignore_spread") or {}).get("fwd_60s") or {}
    blk = (pol.get("blocked_only_spread_gt_2") or {}).get("fwd_60s") or {}

    calls: List[Dict[str, Any]] = []

    # Spread gate
    if cur.get("n", 0) >= 20 and blk.get("n", 0) >= 10:
        # Keep block if blocked slice is clearly worse than current
        keep = (blk.get("ev_bp") is not None and cur.get("ev_bp") is not None
                and blk["ev_bp"] + 1.0 * (blk.get("se_bp") or 0) < cur["ev_bp"])
        drop = (blk.get("ev_bp") is not None and cur.get("ev_bp") is not None
                and blk["ev_bp"] >= cur["ev_bp"])
        if keep:
            decision = "KEEP_SPREAD_2.0"
            why = f"blocked slice fwd60 EV={blk['ev_bp']:+.2f} worse than current {cur['ev_bp']:+.2f}"
        elif drop:
            decision = "RELAX_OR_DROP_SPREAD_2.0"
            why = f"blocked slice fwd60 EV={blk['ev_bp']:+.2f} not worse than current {cur['ev_bp']:+.2f}"
        else:
            decision = "INCONCLUSIVE_SPREAD"
            why = "overlap within noise / insufficient separation"
    elif blk.get("n", 0) < 10:
        decision = "INCONCLUSIVE_SPREAD"
        why = f"blocked enter_v2 onsets n={blk.get('n', 0)} too small for EV call"
    else:
        decision = "INCONCLUSIVE_SPREAD"
        why = "insufficient current-policy sample"

    calls.append({"gate": "SPREAD_MAX_BP=2.0", "decision": decision, "why": why, "fwd60_current": cur, "fwd60_blocked": blk, "fwd60_ignore": ign})

    # Component ablations
    abl = fwd.get("enter_v2_ablations") or {}
    full = (abl.get("enter_v2_full") or {}).get("fwd_60s") or {}
    for drop_name, key in (
        ("fresh5", "drop_fresh5"),
        ("micro", "drop_micro"),
        ("agent", "drop_agent"),
    ):
        d = (abl.get(key) or {}).get("fwd_60s") or {}
        if full.get("n", 0) >= 20 and d.get("n", 0) >= 20 and full.get("ev_bp") is not None and d.get("ev_bp") is not None:
            if d["ev_bp"] + (d.get("se_bp") or 0) < full["ev_bp"]:
                dec = f"KEEP_{drop_name.upper()}"
                w = f"dropping {drop_name} hurts fwd60 ({d['ev_bp']:+.2f} vs full {full['ev_bp']:+.2f})"
            elif d["ev_bp"] > full["ev_bp"] + (full.get("se_bp") or 0):
                dec = f"QUESTION_{drop_name.upper()}"
                w = f"dropping {drop_name} improves/equal fwd60 ({d['ev_bp']:+.2f} vs full {full['ev_bp']:+.2f})"
            else:
                dec = f"INCONCLUSIVE_{drop_name.upper()}"
                w = "within noise"
        else:
            dec = f"INCONCLUSIVE_{drop_name.upper()}"
            w = "n too small or flags missing"
        calls.append({"gate": f"enter_v2.{drop_name}", "decision": dec, "why": w, "fwd60_full": full, "fwd60_drop": d})

    improve = any(c["decision"].startswith("RELAX") or c["decision"].startswith("QUESTION") for c in calls)
    return {
        "improve_candidate": improve,
        "action_now": "DO_NOT_CHANGE_GATES" if not improve else "CANDIDATE_CHANGE_EXISTS_BUT_WAIT_FOR_REVIEW",
        "calls": calls,
        "realized_note": realized.get("note"),
        "realized_all": realized.get("all"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate TF1A gates (observation)")
    ap.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ap.add_argument("--ae", type=Path, default=DEFAULT_AE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--since-hours", type=float, default=0.0)
    args = ap.parse_args()

    t0 = time.time()
    gates = _load_jsonl(args.gate)
    if args.since_hours > 0:
        cut = datetime.now(tz=JST).timestamp() - args.since_hours * 3600.0
        gates = [g for g in gates if float(g.get("ts") or 0.0) >= cut]
    ledger = _load_jsonl(args.ledger)
    ae = _load_jsonl(args.ae)

    print(f"loaded gates={len(gates)} ledger={len(ledger)} ae={len(ae)}")
    fwd = validate_forward(gates)
    realized = validate_realized(gates, ledger)
    ae_s = validate_ae_tf1a(ae)
    verd = verdict(fwd, realized)

    report = {
        "ts_jst": datetime.now(tz=JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "elapsed_s": round(time.time() - t0, 2),
        "inputs": {
            "gate": str(args.gate),
            "ledger": str(args.ledger),
            "ae": str(args.ae),
            "n_gates": len(gates),
            "n_ledger": len(ledger),
        },
        "method": {
            "forward": "mid-to-mid bp from gate_ticks series; ENTER onset spaced ≥30s",
            "realized": "ledger close joined to nearest prior posted/enter gate for entry_spread",
            "caveat": "forward is not maker-fill EV; relative ranking across buckets/policies is the point",
        },
        "forward": fwd,
        "realized": realized,
        "ae_tf1a": ae_s,
        "verdict": verd,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # Human summary
    print("\n=== coverage ===")
    for k, v in (fwd.get("coverage") or {}).items():
        print(f"  {k}: {v}")

    print("\n=== spread policies (fwd 60s, onset≥30s) ===")
    for name, cell in (fwd.get("spread_policies") or {}).items():
        s = cell.get("fwd_60s") or {}
        print(
            f"  {name:28s} n={s.get('n', 0):4d} "
            f"EV={s.get('ev_bp')} se={s.get('se_bp')} hit+={s.get('hit_pos')}"
        )

    print("\n=== enter_v2 by spread bucket (fwd 60s) ===")
    for name, cell in (fwd.get("enter_v2_onset_by_spread") or {}).items():
        s = cell.get("fwd_60s") or {}
        print(
            f"  {name:8s} n={s.get('n', 0):4d} "
            f"EV={s.get('ev_bp')} se={s.get('se_bp')} hit+={s.get('hit_pos')}"
        )

    print("\n=== enter_v2 ablations (fwd 60s) ===")
    for name, cell in (fwd.get("enter_v2_ablations") or {}).items():
        s = cell.get("fwd_60s") or {}
        print(
            f"  {name:22s} n={s.get('n', 0):4d} "
            f"EV={s.get('ev_bp')} se={s.get('se_bp')} hit+={s.get('hit_pos')}"
        )

    print("\n=== realized by entry spread ===")
    print(f"  joined={realized.get('n_joined')} all={realized.get('all')}")
    for name, cell in (realized.get("realized_by_entry_spread") or {}).items():
        print(f"  {name:8s} {cell}")

    print("\n=== verdict ===")
    print(f"  action_now: {verd.get('action_now')}")
    for c in verd.get("calls") or []:
        print(f"  [{c['decision']}] {c['gate']}: {c['why']}")

    print(f"\nreport -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
