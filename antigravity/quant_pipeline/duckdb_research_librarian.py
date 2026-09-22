"""
DuckDB Research Librarian
=========================
全研究レーンの日次結果を読み、「本当に使えるか」と「次に何を試すか」を判定する。

WIRE=NO / ENFORCE=0 / 重み自動適用なし / 経済 PASS/FAIL 禁止
参照: CSR-499 · CSR-512 · CSR-113 健全性ゲート精神
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

JST = timezone(timedelta(hours=9))
BASE = "/home/azureuser/antigravity"
DEFAULT_OUT = os.path.join(BASE, "data", "research_librarian")

LANE_PATHS = {
    "PegResearch": {
        "daily": os.path.join(BASE, "data", "peg_research", "daily"),
        "state": os.path.join(BASE, "data", "peg_research", "peg_research_state.json"),
        "labeled": os.path.join(BASE, "data", "peg_research", "labeled.jsonl"),
    },
    "AdverseAdvance": {
        "daily": os.path.join(BASE, "data", "adverse_research", "daily"),
        "state": os.path.join(BASE, "data", "adverse_research", "advance_state.json"),
        "samples": os.path.join(BASE, "data", "adverse_research", "advance_samples.jsonl"),
    },
    "MicrostructureHourly": {
        "latest": os.path.join(BASE, "data", "microstructure", "hourly_latest.json"),
        "state": os.path.join(BASE, "data", "microstructure", "microstructure_hourly_state.json"),
        "hourly_dir": os.path.join(BASE, "data", "microstructure", "hourly"),
    },
    "UMM_TF2BP_Context": {
        "state": os.path.join(BASE, "data", "dryrun_umm_tf2bp_state.json"),
    },
    "CouncilFusion": {
        "state": os.path.join(BASE, "configs", "agents_council_state.json"),
    },
    "TrendFollowResearch": {
        "daily": os.path.join(BASE, "data", "trend_research", "daily"),
        "state": os.path.join(BASE, "data", "trend_research", "trend_research_state.json"),
        "samples": os.path.join(BASE, "data", "trend_research", "samples.jsonl"),
    },
}


def _load_json(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _day_key(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), JST).strftime("%Y-%m-%d")


def _count_jsonl(path: str) -> int:
    if not path or not os.path.exists(path):
        return 0
    n = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for _ in f:
                n += 1
    except Exception:
        return 0
    return n


def _duck_count_jsonl(path: str) -> Optional[int]:
    """軽量: DuckDB で JSONL 行数。失敗時 None。"""
    if not path or not os.path.exists(path):
        return 0
    try:
        import duckdb

        conn = duckdb.connect()
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM read_json_auto('{path}', ignore_errors=true)"
        ).fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception:
        return None


def judge_rate(
    n: int,
    rate: Optional[float],
    *,
    baseline: float = 0.5,
    min_n: int = 30,
    useful_edge: float = 0.08,
    harmful_edge: float = 0.05,
) -> Tuple[str, str]:
    """
    戻り値: (USEFUL|NEED_MORE|NOT_USEFUL, reason)
    経済 PASS/FAIL は出さない。予測がベースラインより使えるかのみ。
    """
    if rate is None:
        return "NEED_MORE", "指標なし"
    if n < min_n:
        return "NEED_MORE", f"n={n}<{min_n}（標本不足・判定禁止）"
    if rate >= baseline + useful_edge:
        return "USEFUL", f"n={n} rate={rate:.3f} ≥ baseline+{useful_edge:.2f} ({baseline:.2f})"
    if rate <= baseline - harmful_edge:
        return "NOT_USEFUL", f"n={n} rate={rate:.3f} ≤ baseline-{harmful_edge:.2f}（偶然以下）"
    return "NEED_MORE", f"n={n} rate={rate:.3f} がベースライン近傍（エッジ未確認）"


class DuckDBResearchLibrarian:
    """全研究レーンの日次 Librarian。"""

    def __init__(self, out_root: str = DEFAULT_OUT):
        self.out_root = out_root
        self.daily_dir = os.path.join(out_root, "daily")
        self.state_path = os.path.join(out_root, "librarian_state.json")
        os.makedirs(self.daily_dir, exist_ok=True)
        os.makedirs(out_root, exist_ok=True)

    def review_day(self, day: Optional[str] = None) -> Dict[str, Any]:
        day = day or _day_key()
        lanes: Dict[str, Any] = {}
        lanes["PegResearch"] = self._review_peg(day)
        lanes["AdverseAdvance"] = self._review_adverse(day)
        lanes["MicrostructureHourly"] = self._review_micro(day)
        lanes["UMM_TF2BP_Context"] = self._review_umm_tf(day)
        lanes["CouncilFusion"] = self._review_council(day)
        lanes["TrendFollowResearch"] = self._review_trend(day)
        lanes["FusionParquet"] = self._review_fusion_parquet()

        usable_counts = {"USEFUL": 0, "NEED_MORE": 0, "NOT_USEFUL": 0, "CONTEXT": 0}
        for name, lane in lanes.items():
            u = lane.get("usable", "NEED_MORE")
            if u in usable_counts:
                usable_counts[u] += 1
            else:
                usable_counts["NEED_MORE"] += 1

        # ポートフォリオ判定: 1本でも USEFUL なら部分有用。全部 NEED_MORE なら継続。
        if usable_counts["USEFUL"] >= 1 and usable_counts["NOT_USEFUL"] == 0:
            portfolio = "PARTIAL_USEFUL"
        elif usable_counts["USEFUL"] >= 1:
            portfolio = "MIXED"
        elif usable_counts["NOT_USEFUL"] >= 2:
            portfolio = "MOSTLY_NOT_USEFUL"
        else:
            portfolio = "NEED_MORE"

        next_experiments = self._compose_next_experiments(lanes)
        report = {
            "date": day,
            "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "mode": "research_librarian",
            "agent": "DuckDBOptimizerAgent",
            "role": "Librarian — 全研究レーンの usable 判定と次実験 ADVISE",
            "wire": "NO",
            "enforce": 0,
            "auto_apply_weights": False,
            "portfolio_verdict": portfolio,
            "usable_counts": usable_counts,
            "lanes": lanes,
            "next_experiments": next_experiments,
            "forbidden": [
                "経済 PASS/FAIL",
                "LIVE 配線",
                "approved_weights 自動更新",
                "UMM/TF2BP frozen パラメータ自動変更",
            ],
            "note": "n未達では usable 判定を NEED_MORE に固定。採用は人間承認のみ。",
        }
        self._persist(day, report)
        return report

    def _persist(self, day: str, report: Dict[str, Any]) -> None:
        path = os.path.join(self.daily_dir, f"{day}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        state = {
            "updated_at": report["updated_at"],
            "today": day,
            "portfolio_verdict": report["portfolio_verdict"],
            "usable_counts": report["usable_counts"],
            "next_experiments": report["next_experiments"][:8],
            "wire": "NO",
            "enforce": 0,
            "daily_path": path,
        }
        tmp2 = self.state_path + ".tmp"
        with open(tmp2, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp2, self.state_path)

    def _review_peg(self, day: str) -> Dict[str, Any]:
        paths = LANE_PATHS["PegResearch"]
        daily = _load_json(os.path.join(paths["daily"], f"{day}.json"))
        state = _load_json(paths["state"])
        tasks = (daily.get("tasks") or {}) if daily else {}
        dir_t = tasks.get("direction_1_5bp") or {}
        cont_t = tasks.get("trend_continue") or {}
        end_t = tasks.get("trend_end") or {}

        n_dir = int(dir_t.get("n") or 0)
        rate_dir = dir_t.get("dir_correct_rate")
        rate_15 = dir_t.get("hit_1_5bp_rate")
        n_cont = int(cont_t.get("n") or 0)
        rate_cont = cont_t.get("continue_hit_rate")
        n_end = int(end_t.get("n") or 0)
        rate_end = end_t.get("end_hit_rate")

        u_dir, r_dir = judge_rate(n_dir, rate_dir, baseline=0.5, useful_edge=0.08)
        u_15, r_15 = judge_rate(n_dir, rate_15, baseline=0.25, useful_edge=0.10, harmful_edge=0.05, min_n=30)
        # 1-5bp hit baseline ~ random band occupancy; treat 0.25 as soft baseline
        u_cont, r_cont = judge_rate(n_cont, rate_cont, baseline=0.5, useful_edge=0.08, min_n=30)
        u_end, r_end = judge_rate(n_end, rate_end, baseline=0.5, useful_edge=0.08, min_n=20)

        # レーン総合: 最悪寄りの厳密判定（1本 USEFUL でも他が NOT なら MIXED）
        parts = [u_dir, u_cont, u_end]
        if "NOT_USEFUL" in parts and "USEFUL" not in parts:
            usable = "NOT_USEFUL"
        elif "USEFUL" in parts:
            usable = "USEFUL" if parts.count("USEFUL") >= 2 else "NEED_MORE"
        else:
            usable = "NEED_MORE"

        advise = []
        if u_dir == "NOT_USEFUL":
            advise.append("direction: peg_diff/mom 特徴の再設計か horizon 固定を見直し（現状ベースライン以下）")
        elif u_dir == "NEED_MORE":
            advise.append("direction: 継続蓄積。特徴コントラスト（勝ち/負け）を日次で出す")
        if u_cont in ("NOT_USEFUL", "NEED_MORE") and n_cont < 100:
            advise.append("trend_continue: n増やす（継続ゲートを緩めずラベル品質維持）")
        if u_end == "NOT_USEFUL" or (n_end >= 20 and (rate_end or 0) < 0.4):
            advise.append("trend_end: exhaust/逆行定義を Microstructure tip枯渇パターンと突合")
        if not advise:
            advise.append("Peg: 現状維持観測。Council への執行提案はまだ出さない")

        labeled_n = _duck_count_jsonl(paths["labeled"])
        if labeled_n is None:
            labeled_n = _count_jsonl(paths["labeled"])

        return {
            "lane": "PegResearch",
            "usable": usable,
            "tasks": {
                "direction_1_5bp": {"n": n_dir, "dir_correct_rate": rate_dir, "hit_1_5bp_rate": rate_15, "usable": u_dir, "reason": r_dir},
                "trend_continue": {"n": n_cont, "continue_hit_rate": rate_cont, "usable": u_cont, "reason": r_cont},
                "trend_end": {"n": n_end, "end_hit_rate": rate_end, "usable": u_end, "reason": r_end},
            },
            "labeled_total": labeled_n,
            "state_stats": state.get("stats"),
            "advise": advise,
            "wire": "NO",
        }

    def _review_adverse(self, day: str) -> Dict[str, Any]:
        paths = LANE_PATHS["AdverseAdvance"]
        daily = _load_json(os.path.join(paths["daily"], f"{day}.json"))
        state = _load_json(paths["state"])
        tasks = (daily.get("tasks") or state.get("today_tasks") or {}) if (daily or state) else {}
        adv = tasks.get("adverse_advance") or {}
        m15 = tasks.get("move_1_5bp") or {}
        direction = tasks.get("direction_adverse") or {}

        n = int(adv.get("n") or state.get("counts", {}).get("UMM_total") or 0)
        hit = adv.get("pred_hit_rate")
        if hit is None:
            hit = state.get("pred_adverse_hit_rate")
        u_adv, r_adv = judge_rate(n, hit, baseline=0.5, useful_edge=0.10, min_n=30)

        n_m = int(m15.get("n") or n)
        pos = m15.get("positive_rate")
        u_m, r_m = judge_rate(n_m, pos, baseline=0.2, useful_edge=0.10, min_n=30)

        advise = []
        if n < 30:
            advise.append("AdverseAdvance: UMM 教師サンプルを増やす（pre5/pre10 付き完了約定を優先）")
        if not (daily.get("decisive_pre_features") or state.get("today_decisive")):
            advise.append("decisive_pre_features が空 — toxic vs normal コントラストが出るまで特徴採択禁止")
        if u_adv == "NOT_USEFUL":
            advise.append("先回りスコア閾値/特徴を再設計（現状ヒットが偶然以下）")
        if not advise:
            advise.append("Adverse: 健全性ゲート通過後にのみ経済評価を検討")

        samples_n = _duck_count_jsonl(paths["samples"])
        if samples_n is None:
            samples_n = _count_jsonl(paths["samples"])

        usable = u_adv if n >= 30 else "NEED_MORE"
        return {
            "lane": "AdverseAdvance",
            "usable": usable,
            "n": n,
            "pred_hit_rate": hit,
            "move_1_5bp": {"n": n_m, "positive_rate": pos, "usable": u_m, "reason": r_m},
            "direction_adverse": direction,
            "reason": r_adv,
            "samples_total": samples_n,
            "decisive_pre_features": (daily.get("decisive_pre_features") or state.get("today_decisive") or [])[:5],
            "advise": advise,
            "wire": "NO",
        }

    def _review_micro(self, day: str) -> Dict[str, Any]:
        paths = LANE_PATHS["MicrostructureHourly"]
        latest = _load_json(paths["latest"])
        state = _load_json(paths["state"])
        n = int(latest.get("n_ticks") or state.get("n_ticks") or 0)
        hints = latest.get("signal_candidate_hints") or state.get("signal_candidate_hints") or []
        patterns = latest.get("top_cooccurrence_patterns") or state.get("top_patterns") or []
        high_hints = [h for h in hints if h.get("priority") == "high"]

        if n < 100:
            usable = "NEED_MORE"
            reason = f"hourly ticks n={n}<100"
            advise = ["Microstructure 時間次集計を pipeline で蓄積開始/継続"]
        elif high_hints:
            usable = "USEFUL"
            reason = f"高優先シグナル候補 {len(high_hints)} 件（採用ではなく材料あり）"
            advise = [h.get("text") for h in high_hints[:3]]
            advise.append("候補は Peg/Adverse 特徴に移植してラベル検証（執行しない）")
        elif patterns:
            usable = "NEED_MORE"
            reason = "パターンはあるが high hint なし — 閾値・定義の調整余地"
            advise = ["同時発生 rate≥3% のパターンを PegResearch 特徴に追加試験"]
        else:
            usable = "NEED_MORE"
            reason = "同時発生が薄い時間帯"
            advise = ["継続観測。quiet 時間はシグナル棄却の負例として残す"]

        return {
            "lane": "MicrostructureHourly",
            "usable": usable,
            "n_ticks": n,
            "reason": reason,
            "top_patterns": patterns[:5],
            "hints": hints[:5],
            "advise": advise,
            "wire": "NO",
        }

    def _review_umm_tf(self, day: str) -> Dict[str, Any]:
        st = _load_json(LANE_PATHS["UMM_TF2BP_Context"]["state"])
        umm = st.get("umm") or {}
        tf = st.get("tf2bp") or {}
        peg = st.get("tf2bp_peg_v2") or {}
        # 文脈レーン: 経済判定しない
        return {
            "lane": "UMM_TF2BP_Context",
            "usable": "CONTEXT",
            "reason": "成績レーンは Librarian の usable 対象外（frozen / 観測のみ）",
            "umm_24h_bp": (umm.get("stats_24h") or {}).get("pnl_bp", umm.get("total_pnl_bp")),
            "tf2bp_24h_bp": (tf.get("stats_24h") or {}).get("pnl_bp", tf.get("total_pnl_bp")),
            "peg_v2_24h_bp": (peg.get("stats_24h") or {}).get("pnl_bp", peg.get("total_pnl_bp")),
            "advise": [
                "UMM/TF2BP の bp は Peg/Adverse 研究の背景ノイズ指標としてだけ見る",
                "frozen パラメータは手動指示以外変更禁止",
            ],
            "wire": "NO",
        }

    def _review_council(self, day: str) -> Dict[str, Any]:
        st = _load_json(LANE_PATHS["CouncilFusion"]["state"])
        conclusions = st.get("conclusions") or {}
        duck = conclusions.get("DuckDBOptimizerAgent") or {}
        return {
            "lane": "CouncilFusion",
            "usable": "CONTEXT",
            "reason": "合議は執行メモ。研究 usable は各レーン日次で判定",
            "final_action": st.get("final_action"),
            "active_regime": st.get("active_regime"),
            "duck_verdict_live": duck.get("verdict"),
            "advise": ["Council 日次は Librarian 報告を結論チャンネルへ載せる（執行は変えない）"],
            "wire": "NO",
        }

    def _review_fusion_parquet(self) -> Dict[str, Any]:
        """Fusion ログのラベル健全性（realized_pnl が埋まっているか）。直近日付のみ軽量スキャン。"""
        fusion_root = os.path.join(BASE, "data", "parquet", "fusion_log")
        date_dirs = []
        if os.path.isdir(fusion_root):
            date_dirs = sorted(
                [d for d in os.listdir(fusion_root) if d.startswith("date=")],
                reverse=True,
            )[:3]
        globs = [os.path.join(fusion_root, d, "*.parquet") for d in date_dirs] or [
            os.path.join(fusion_root, "*", "*.parquet")
        ]
        try:
            import duckdb

            conn = duckdb.connect()
            parts = " UNION ALL ".join(
                [f"SELECT realized_pnl, action FROM '{g}'" for g in globs]
            )
            row = conn.execute(
                f"""
                SELECT
                  COUNT(*) AS n,
                  COUNT(CASE WHEN realized_pnl IS NOT NULL AND realized_pnl != 0 THEN 1 END) AS labeled_n,
                  COUNT(CASE WHEN action IN ('buy','sell') THEN 1 END) AS action_n
                FROM ({parts})
                """
            ).fetchone()
            conn.close()
            n, labeled_n, action_n = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        except Exception as e:
            return {
                "lane": "FusionParquet",
                "usable": "NEED_MORE",
                "reason": f"parquet query failed: {e}",
                "advise": ["fusion_log parquet パス/スキーマを確認"],
                "wire": "NO",
            }

        label_rate = (labeled_n / action_n) if action_n else 0.0
        if action_n < 50:
            usable, reason = "NEED_MORE", f"action_n={action_n}<50"
            advise = ["Fusion 意思決定ログの蓄積を継続"]
        elif label_rate < 0.05:
            usable, reason = "NOT_USEFUL", f"realized_pnl ラベル率 {label_rate:.1%} — 重み学習に使えない"
            advise = [
                "FusionDecisionLog.realized_pnl の事後紐付けを修復（学習閉ループの前提）",
                "ラベル修復まで ΔW / approved_weights 更新を禁止（現状維持）",
            ]
        elif label_rate < 0.3:
            usable, reason = "NEED_MORE", f"ラベル率 {label_rate:.1%} — 学習には薄い"
            advise = ["決済時の PnL バックフィルを強化"]
        else:
            usable, reason = "USEFUL", f"ラベル率 {label_rate:.1%} — 研究用に使える母集団"
            advise = ["レジーム別コントラスト分析のみ許可。auto_apply は引き続き OFF"]

        return {
            "lane": "FusionParquet",
            "usable": usable,
            "n": n,
            "action_n": action_n,
            "labeled_n": labeled_n,
            "label_rate": round(label_rate, 4),
            "reason": reason,
            "advise": advise,
            "wire": "NO",
        }


    def _review_trend(self, day: str) -> Dict[str, Any]:
        paths = LANE_PATHS["TrendFollowResearch"]
        daily = _load_json(os.path.join(paths["daily"], f"{day}.json"))
        state = _load_json(paths["state"])
        n = int((daily.get("n") if daily else 0) or state.get("counts", {}).get("n") or 0)
        rate = daily.get("dir_hit_rate") if daily else state.get("dir_hit_rate")
        u, reason = judge_rate(n, rate, baseline=0.5, useful_edge=0.08, min_n=30)
        advise = []
        if n < 30:
            advise.append("TrendFollow: forward-mid ラベル蓄積（pipeline 稼働で自動）")
        elif u == "NOT_USEFUL":
            advise.append("方向閾値(¥400)/horizon30s を Micro tipパターンと突合して再設計")
        elif u == "NEED_MORE":
            advise.append("regime別 hit_rate を分けて見る（trend vs range）")
        else:
            advise.append("USEFUL側: TF2BP onset と突合しエントリーフィルタ候補を作る（執行しない）")
        samples_n = _duck_count_jsonl(paths["samples"])
        if samples_n is None:
            samples_n = _count_jsonl(paths["samples"])
        return {
            "lane": "TrendFollowResearch",
            "usable": u,
            "n": n,
            "dir_hit_rate": rate,
            "reason": reason,
            "samples_total": samples_n,
            "onset_n": (daily or {}).get("onset_n") or state.get("counts", {}).get("onset_n"),
            "advise": advise,
            "wire": "NO",
        }

    def _compose_next_experiments(self, lanes: Dict[str, Any]) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for name, lane in lanes.items():
            for a in (lane.get("advise") or [])[:2]:
                out.append({
                    "lane": name,
                    "usable": str(lane.get("usable")),
                    "advise": a,
                    "priority": "high" if lane.get("usable") == "NOT_USEFUL" else "normal",
                })
        # 優先: NOT_USEFUL → NEED_MORE → その他
        rank = {"NOT_USEFUL": 0, "NEED_MORE": 1, "MIXED": 2, "USEFUL": 3, "CONTEXT": 4}
        out.sort(key=lambda x: rank.get(x.get("usable", ""), 9))
        # 重複テキスト除去
        seen = set()
        uniq = []
        for item in out:
            t = item["advise"]
            if t in seen:
                continue
            seen.add(t)
            uniq.append(item)
        return uniq[:12]


def run_daily_review(day: Optional[str] = None) -> Dict[str, Any]:
    return DuckDBResearchLibrarian().review_day(day)


if __name__ == "__main__":
    import pprint

    rep = run_daily_review()
    print(json.dumps({
        "date": rep["date"],
        "portfolio_verdict": rep["portfolio_verdict"],
        "usable_counts": rep["usable_counts"],
        "next_experiments": rep["next_experiments"][:6],
        "lanes": {k: {"usable": v.get("usable"), "advise0": (v.get("advise") or [""])[0]} for k, v in rep["lanes"].items()},
    }, indent=2, ensure_ascii=False))
