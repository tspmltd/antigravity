"""
Alpha vs Adverse 3-Axis Correlation & Commander Proof Engine
============================================================
【ユーザー設計思想】
「Signal Score × Adverse Score × 実損益 の3軸比較。
 理想形は『高Signal ＆ 低Adverse』だけが勝つこと。
 Adverse Score ↓ 実損益 の相関を統計的に証明し、Adverse Agent を真の司令塔へ昇格させる。」

【3軸マトリクス (4象限)】
  Q1: 高Signal (≥0.55) × 低Adverse (<35) -> 理想勝利圏 (Sweet Spot: フル発注)
  Q2: 高Signal (≥0.55) × 高Adverse (≥35) -> 逆選択被弾圏 (Toxic Trap: 遮断・ロット縮小)
  Q3: 低Signal (<0.55) × 低Adverse (<35) -> ノイズ圏 (Noise: 見送り)
  Q4: 低Signal (<0.55) × 高Adverse (≥35) -> 即死圏 (Suicide: 完全禁止)

【統計的証明】
  • ピアソン相関係数 r (Adverse vs PnL) -> r < -0.30 (有意な負の相関)
  • スピアマン順位相関 rho -> 単調悪化の立証
  • 線形回帰スロープ beta -> 1 Adverse Scoreあたりの損益剥落率 (bp/pt)
  • p値 (有意性検証) -> p < 0.05
"""
import os
import json
import time
import math
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
RECORDS_JSONL_PATH = "/home/azureuser/antigravity/data/adverse_excursion_records.jsonl"
OUTPUT_STATS_PATH = "/home/azureuser/antigravity/data/alpha_vs_adverse_stats.json"


class AlphaVsAdverseAnalyzer:
    def __init__(self, records_path: str = RECORDS_JSONL_PATH, output_path: str = OUTPUT_STATS_PATH):
        self.records_path = records_path
        self.output_path = output_path

    def load_completed_trades(self) -> List[Dict[str, Any]]:
        """決済済み（損益確定）トレードを全件ロード"""
        trades = []
        if not os.path.exists(self.records_path):
            return trades

        try:
            with open(self.records_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        # realized_pnl_bp がある決済済みレコードのみ対象
                        if record.get("realized_pnl_bp") is not None:
                            trades.append(record)
                    except Exception:
                        continue
        except Exception as e:
            print(f"[AlphaVsAdverse] Load error: {e}")

        return trades

    def analyze(self) -> Dict[str, Any]:
        trades = self.load_completed_trades()
        n = len(trades)

        if n < 3:
            return self._generate_insufficient_data_result(n)

        # 3軸データの抽出: (signal_score, adverse_score, realized_pnl_bp)
        data_points = []
        adv_scores = []
        pnls = []

        for t in trades:
            pnl = float(t.get("realized_pnl_bp", 0.0))
            meta = t.get("meta") or {}

            # Adverse Score — missing ≠ 30.0（CSR-539: default30が20-40崩壊の主因）
            if "adverse_score" in meta and meta.get("adverse_score") is not None:
                adv = float(meta["adverse_score"])
            elif t.get("fill_quality") == "TOXIC_FILL":
                adv = 75.0
            elif t.get("fill_quality") == "GOOD_FILL":
                adv = 15.0
            else:
                continue  # skip unscored from correlation / certification population
            # Signal Score (合議確信度または初期値0.65)
            sig = float(meta.get("signal_confidence") or meta.get("confidence") or 0.65)

            data_points.append({
                "trade_id": t.get("trade_id"),
                "strategy": t.get("strategy"),
                "signal_score": sig,
                "adverse_score": adv,
                "pnl_bp": pnl,
                "ae_1s": t.get("ae_1s"),
                "capture_rate": t.get("capture_rate"),
            })
            adv_scores.append(adv)
            pnls.append(pnl)

        n = len(data_points)
        n_skipped_unscored = len(trades) - n
        if n < 3:
            return self._generate_insufficient_data_result(n)

        # 1. 統計的相関の算出 (Adverse Score vs PnL bp)
        corr_r, p_value, slope, intercept = self._calculate_linear_regression(adv_scores, pnls)
        spearman_rho = self._calculate_spearman(adv_scores, pnls)

        # 2. 3軸 4象限マトリクス (Signal × Adverse)
        quadrants = self._calculate_quadrants(data_points)

        # 3. 司令塔認定判定 — 統計ゲート必須。CSR-539: Economic PASSはintegrity別監査まで禁止
        q1_pnl = quadrants["Q1_SweetSpot"]["expected_pnl_bp"]
        q2_pnl = quadrants["Q2_ToxicTrap"]["expected_pnl_bp"]
        q1_n = quadrants["Q1_SweetSpot"]["count"]
        q2_n = quadrants["Q2_ToxicTrap"]["count"]
        min_n = 30
        gate_fails = []
        if n < min_n:
            gate_fails.append(f"n<{min_n}")
        if not (corr_r < -0.25):
            gate_fails.append("r_not_lt_-0.25")
        if p_value >= 0.05:
            gate_fails.append("p_ge_0.05")
        if q1_n < 10 or q2_n < 10:
            gate_fails.append("quadrant_n_low")
        if not (q1_pnl > q2_pnl):
            gate_fails.append("q1_not_gt_q2")
        # CSR-539: statistical gates may pass after Fisher-z fix, but Council Economic PASS stays blocked
        integrity_note = (
            "CSR-539: integrity audit required (score missing/default30 collapse · Capture provenance). "
            "Adverse Economic PASS=NO until integrity_ok."
        )
        is_commander_certified = False  # hard block until CSR-539 clears
        if len(gate_fails) == 0:
            gate_fails.append("integrity_hold_csr539")

        result = {
            "timestamp": int(time.time() * 1000),
            "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "total_completed_trades": n,
            "n_skipped_unscored": n_skipped_unscored,
            "correlation_analysis": {
                "pearson_r": round(corr_r, 3),
                "spearman_rho": round(spearman_rho, 3),
                "r_squared": round(corr_r ** 2, 3),
                "slope_beta_bp_per_score": round(slope, 3),
                "intercept": round(intercept, 2),
                "p_value_estimate": round(p_value, 12) if p_value >= 1e-12 else 0.0,
                "p_value_method": "fisher_z_two_sided_erfc (CSR-535; replaces bogus 1/(1+t^2/df))",
                "p_value_legacy_note": "pre-CSR-535 pseudo-p was INVALID for certification",
                "relationship": "NEGATIVE_CORRELATION (Score上昇で損益悪化)" if corr_r < -0.25 and p_value < 0.05 else "WEAK_OR_UNPROVEN",
            },
            "quadrant_matrix": quadrants,
            "commander_certification": {
                "is_certified": is_commander_certified,
                "gate_fails": gate_fails,
                "economic_pass": False,
                "title": "【未認定 / 整合性監査中 CSR-539】",
                "summary": (
                    f"統計ゲート: {gate_fails}. r={corr_r:.2f}, p={p_value:.4g}, n={n}. "
                    + integrity_note
                ),
            },
            "adverse_economic_pass": False,
        }

        self._persist(result)
        return result

    def _calculate_quadrants(self, points: List[Dict[str, Any]]) -> Dict[str, Any]:
        """4象限集計 (高/低Signal × 高/低Adverse)"""
        # 閾値: Signal 0.55, Adverse 35.0
        q_bins = {
            "Q1_SweetSpot": {"name": "Q1: 理想勝利圏 (高Sig × 低Adv)", "filter": lambda p: p["signal_score"] >= 0.55 and p["adverse_score"] < 35.0},
            "Q2_ToxicTrap": {"name": "Q2: 逆選択被弾圏 (高Sig × 高Adv)", "filter": lambda p: p["signal_score"] >= 0.55 and p["adverse_score"] >= 35.0},
            "Q3_Noise":     {"name": "Q3: ノイズ圏 (低Sig × 低Adv)", "filter": lambda p: p["signal_score"] < 0.55 and p["adverse_score"] < 35.0},
            "Q4_Suicide":   {"name": "Q4: 即死圏 (低Sig × 高Adv)", "filter": lambda p: p["signal_score"] < 0.55 and p["adverse_score"] >= 35.0},
        }

        res = {}
        for q_id, q_info in q_bins.items():
            matched = [p for p in points if q_info["filter"](p)]
            cnt = len(matched)
            if cnt > 0:
                pnl_vals = [p["pnl_bp"] for p in matched]
                win_cnt = sum(1 for p in pnl_vals if p > 0)
                win_rate = round(win_cnt / cnt * 100.0, 1)
                exp_pnl = round(sum(pnl_vals) / cnt, 2)
            else:
                win_rate = 0.0
                exp_pnl = 0.0

            res[q_id] = {
                "name": q_info["name"],
                "count": cnt,
                "win_rate_pct": win_rate,
                "expected_pnl_bp": exp_pnl,
            }

        return res

    def _calculate_linear_regression(self, x: List[float], y: List[float]) -> Tuple[float, float, float, float]:
        """Pearson r, **two-sided** p-value, slope, intercept.

        CSR-535: 旧「擬似p」`1/(1+t^2/df)` は正規Pearson pではない（r≈-0.28 で p≈0.92 になりうる誤表示）。
        両側対立仮説 H1: r ≠ 0。大標本は Fisher z → 正規近似。
        """
        n = len(x)
        if n < 3:
            return 0.0, 1.0, 0.0, 0.0

        mean_x = sum(x) / n
        mean_y = sum(y) / n

        num = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
        den_x = sum((x[i] - mean_x) ** 2 for i in range(n))
        den_y = sum((y[i] - mean_y) ** 2 for i in range(n))

        if den_x == 0 or den_y == 0:
            return 0.0, 1.0, 0.0, mean_y

        r = num / math.sqrt(den_x * den_y)
        # numerical clamp
        r = max(-0.999999, min(0.999999, r))
        slope = num / den_x
        intercept = mean_y - slope * mean_x

        # Fisher z transform → two-sided normal p (valid for moderate |r|, n>=10)
        z = 0.5 * math.log((1.0 + r) / (1.0 - r))
        se = 1.0 / math.sqrt(max(1.0, n - 3))
        z_stat = abs(z) / se
        # erfc tail: P(|Z| > z_stat) = erfc(z_stat / sqrt(2))
        p_val = math.erfc(z_stat / math.sqrt(2.0))
        # floor for JSON readability; true tiny p stays very small
        if p_val < 1e-300:
            p_val = 0.0

        return r, p_val, slope, intercept

    def _calculate_spearman(self, x: List[float], y: List[float]) -> float:
        """Spearman rho 順位相関係数"""
        n = len(x)
        if n < 2:
            return 0.0

        def rank_data(arr):
            indexed = sorted(enumerate(arr), key=lambda item: item[1])
            ranks = [0] * len(arr)
            for r, (idx, _) in enumerate(indexed):
                ranks[idx] = r + 1
            return ranks

        rx = rank_data(x)
        ry = rank_data(y)

        d_sq = sum((rx[i] - ry[i]) ** 2 for i in range(n))
        rho = 1.0 - (6.0 * d_sq) / (n * (n ** 2 - 1))
        return rho

    def _generate_insufficient_data_result(self, count: int) -> Dict[str, Any]:
        return {
            "timestamp": int(time.time() * 1000),
            "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "total_completed_trades": count,
            "correlation_analysis": {
                "pearson_r": 0.0,
                "spearman_rho": 0.0,
                "r_squared": 0.0,
                "slope_beta_bp_per_score": 0.0,
                "p_value_estimate": 1.0,
                "relationship": "INSUFFICIENT_DATA (サンプル収集中)",
            },
            "quadrant_matrix": {},
            "commander_certification": {
                "is_certified": False,
                "title": "⚖️ 【司令塔検証中 (サンプル蓄積フェーズ)】",
                "summary": f"決済済みトレードが現在 {count} 件です。統計的相関の厳密な検定には最低10件以上の完了が必要です。"
            }
        }

    def _persist(self, data: Dict[str, Any]):
        try:
            os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
            tmp = self.output_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.output_path)
        except Exception:
            pass
