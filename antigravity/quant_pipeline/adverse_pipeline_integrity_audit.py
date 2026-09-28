"""CSR-539 P0-A — Adverse statistical pipeline integrity audit (OBSERVE · ENFORCE=0).

Checks:
  1) p-value path (legacy pseudo-p vs Fisher-z)
  2) adverse_score presence / scale / constant collapse
  3) score-bin mass in 20-40 via default=30
  4) Capture raw vs robust across 21:00 JST boundary
  5) Economic PASS remains forbidden until integrity_ok

Does NOT restart dryrun. Does NOT claim market regime change.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

JST = timezone(timedelta(hours=9))
BASE = "/home/azureuser/antigravity"
RECORDS = f"{BASE}/data/adverse_excursion_records.jsonl"
ALPHA_STATS = f"{BASE}/data/alpha_vs_adverse_stats.json"
AE_SUMMARY = f"{BASE}/data/adverse_excursion_summary.json"
OUT = f"{BASE}/data/mm_research/daily"


def _pct(xs: List[float]) -> Optional[Dict[str, float]]:
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)

    def q(p: float) -> float:
        idx = (n - 1) * p
        lo, hi = int(idx), min(n - 1, int(idx) + 1)
        return xs[lo] if lo == hi else xs[lo] * (hi - idx) + xs[hi] * (idx - lo)

    return {
        "n": n,
        "mean": round(sum(xs) / n, 2),
        "median": round(q(0.5), 2),
        "p10": round(q(0.1), 2),
        "p90": round(q(0.9), 2),
    }


def _bin(score: float) -> str:
    if score < 20:
        return "0-20"
    if score < 40:
        return "20-40"
    if score < 60:
        return "40-60"
    if score < 80:
        return "60-80"
    return "80-100"


def audit(boundary_jst: str = "2026-09-27 21:00:00") -> Dict[str, Any]:
    bound = datetime.strptime(boundary_jst, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST).timestamp()
    pre_bins, post_bins = Counter(), Counter()
    pre_miss = post_miss = 0
    pre_n = post_n = 0
    pre_vals: List[float] = []
    post_vals: List[float] = []
    pre_cap: List[float] = []
    post_cap: List[float] = []
    scale_flags = Counter()

    if os.path.isfile(RECORDS):
        with open(RECORDS, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("realized_pnl_bp") is None:
                    continue
                et = r.get("entry_time") or r.get("exit_time")
                if et is None:
                    continue
                et = float(et)
                meta = r.get("meta") or {}
                has = "adverse_score" in meta
                bucket_pre = et < bound
                if has:
                    sc = float(meta["adverse_score"])
                    if 0.0 <= sc <= 1.5:
                        scale_flags["score_looks_0_1"] += 1
                    elif sc > 1.5:
                        scale_flags["score_looks_0_100"] += 1
                    (pre_vals if bucket_pre else post_vals).append(sc)
                    b = _bin(sc)
                else:
                    if bucket_pre:
                        pre_miss += 1
                    else:
                        post_miss += 1
                    b = _bin(30.0)  # legacy default used by analyzer/summary
                    scale_flags["defaulted_to_30"] += 1
                if bucket_pre:
                    pre_n += 1
                    pre_bins[b] += 1
                    if r.get("capture_rate") is not None:
                        pre_cap.append(float(r["capture_rate"]))
                else:
                    post_n += 1
                    post_bins[b] += 1
                    if r.get("capture_rate") is not None:
                        post_cap.append(float(r["capture_rate"]))

    alpha = {}
    if os.path.isfile(ALPHA_STATS):
        with open(ALPHA_STATS, "r", encoding="utf-8") as f:
            alpha = json.load(f)
    corr = alpha.get("correlation_analysis") or {}
    p_method = corr.get("p_value_method")
    p_val = corr.get("p_value_estimate")
    legacy_p_path = p_method is None and isinstance(p_val, (int, float)) and p_val > 0.5

    # Recompute with current on-disk analyzer (does not touch LIVE dryrun memory)
    recompute = None
    try:
        from antigravity.quant_pipeline.alpha_vs_adverse_analyzer import AlphaVsAdverseAnalyzer

        recompute = AlphaVsAdverseAnalyzer().analyze()
    except Exception as e:
        recompute = {"error": str(e)}

    ae_sum = {}
    if os.path.isfile(AE_SUMMARY):
        with open(AE_SUMMARY, "r", encoding="utf-8") as f:
            ae_sum = json.load(f)

    miss_rate_pre = round(pre_miss / pre_n, 4) if pre_n else None
    miss_rate_post = round(post_miss / post_n, 4) if post_n else None
    post_collapse = (
        post_n > 50
        and post_bins.get("20-40", 0) / max(1, post_n) >= 0.95
    ) or (
        post_n > 0
        and len(post_vals) > 5
        and len(set(round(v, 1) for v in post_vals)) <= 2
    )

    integrity_fails = []
    if legacy_p_path:
        integrity_fails.append("persisted_stats_still_legacy_pseudo_p")
    if miss_rate_pre is not None and miss_rate_pre > 0.5:
        integrity_fails.append("pre_boundary_adverse_score_missing_gt_50pct")
    if miss_rate_post is not None and miss_rate_post > 0.5:
        integrity_fails.append("post_boundary_adverse_score_missing_gt_50pct")
    if post_collapse:
        integrity_fails.append("post_boundary_score_bin_collapse_or_constant")
    if scale_flags.get("score_looks_0_1", 0) > 0 and scale_flags.get("score_looks_0_100", 0) > 0:
        integrity_fails.append("mixed_0_1_and_0_100_score_scales")

    # Even if Fisher-z p≪0.05, Economic PASS stays NO until integrity_ok
    recom_corr = (recompute or {}).get("correlation_analysis") or {}
    out = {
        "csr": "CSR-539",
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "boundary_jst": boundary_jst,
        "wire": "NO",
        "enforce": 0,
        "adverse_economic_pass": False,
        "integrity_ok": len(integrity_fails) == 0,
        "integrity_fails": integrity_fails,
        "p_value_audit": {
            "persisted_file": ALPHA_STATS,
            "persisted_p": p_val,
            "persisted_method": p_method,
            "legacy_pseudo_p_still_in_file": legacy_p_path,
            "on_disk_recompute_p": recom_corr.get("p_value_estimate"),
            "on_disk_recompute_method": recom_corr.get("p_value_method"),
            "on_disk_recompute_r": recom_corr.get("pearson_r"),
            "note_ja": (
                "r≈-0.28 で旧擬似p≈0.92は計算バグ。ディスク上再計算はFisher-zでp≈0になりうるが、"
                "score欠落・default=30・スケール問題が残る限り Economic PASS 禁止。"
                "hourly_dryrun_reporter 長期プロセスは旧モジュールをキャッシュしうる → 再起動でDiscord整合。"
            ),
        },
        "score_provenance": {
            "pre": {
                "n": pre_n,
                "bins_with_default30_for_missing": dict(pre_bins),
                "miss_adverse_score": pre_miss,
                "miss_rate": miss_rate_pre,
                "score_when_present": _pct(pre_vals),
            },
            "post": {
                "n": post_n,
                "bins_with_default30_for_missing": dict(post_bins),
                "miss_adverse_score": post_miss,
                "miss_rate": miss_rate_post,
                "score_when_present": _pct(post_vals),
                "unique_rounded_scores": sorted(set(round(v, 1) for v in post_vals))[:20],
            },
            "scale_flags": dict(scale_flags),
            "root_cause_candidate_ja": (
                "meta.adverse_score 欠落時に analyzer/summary が score=30.0 を代入 → 20-40 帯に崩壊。"
                "21時以降の『全件20-40』は相場変化断定不可。入力欠落/書込停止を優先疑義。"
            ),
        },
        "capture_provenance": {
            "pre": _pct(pre_cap),
            "post": _pct(post_cap),
            "summary_now": ae_sum.get("capture_rate_stats"),
            "rule": "raw mean INVALID for gating · use median/winsorized (CSR-535)",
        },
        "verdict": {
            "Adverse_Economic_PASS": "NO",
            "commander_cert_usable_for_council": False,
            "next": "fix score write path · restart hourly reporter · keep dryrun 24h untouched",
        },
    }

    os.makedirs(OUT, exist_ok=True)
    path = f"{OUT}/{datetime.now(JST).strftime('%Y-%m-%d')}_csr539_adverse_integrity_audit.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    out["path"] = path
    return out


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2, ensure_ascii=False))
