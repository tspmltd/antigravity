"""
LIVE準備 選抜3戦略 DRYRUN 毎時レポート (CSR-575)
================================================================================
対象:
  1. TF2BP baseline          … data/dryrun_umm_tf2bp_state.json
  2. SpreadCaptureMM         … data/dryrun_approved_arena_state.json (strat_cbcd5aed)
  3. MicroSpreadMM           … data/dryrun_approved_arena_state.json (strat_4d3f2c9f)

毎時ジャスト + 起動時即時に Discord へ送信:
  - 1時間の成績（stats_1h）
  - 24時間の累積（stats_24h）
  - DRYRUN開始からの累積＝台帳の最長累積（total_* / プロセス再起動後も復元分を含む）
  - 直近1時間の運用コメント
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = "/home/azureuser/antigravity"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier

JST = timezone(timedelta(hours=9))
UMM_TF_STATE = os.path.join(BASE_DIR, "data", "dryrun_umm_tf2bp_state.json")
ARENA_STATE = os.path.join(BASE_DIR, "data", "dryrun_approved_arena_state.json")
SESSION_STATE = os.path.join(BASE_DIR, "data", "live_prep_3strat_reporter_session.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "hourly_live_prep_3strat_reporter.log")
PID_PATH = os.path.join(BASE_DIR, "data", "hourly_live_prep_3strat_reporter.pid")

STRATEGIES = (
    {
        "key": "TF2BP_v1",
        "label": "TF2BP baseline",
        "source": "umm_tf",
        "block": "tf2bp",
    },
    {
        "key": "strat_cbcd5aed",
        "label": "SpreadCaptureMM",
        "source": "arena",
        "strat_id": "strat_cbcd5aed",
    },
    {
        "key": "strat_4d3f2c9f",
        "label": "MicroSpreadMM",
        "source": "arena",
        "strat_id": "strat_4d3f2c9f",
    },
)


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _fmt_bp(v: float) -> str:
    return f"{v:+.2f} bp"


def _fmt_jpy(v: float) -> str:
    return f"¥{v:+,.1f}"


def _empty_stats() -> Dict[str, Any]:
    return {
        "total_trades": 0,
        "win_trades": 0,
        "loss_trades": 0,
        "win_rate_pct": 0.0,
        "pnl_jpy": 0.0,
        "pnl_bp": 0.0,
        "position": "UNKNOWN",
    }


def _extract_block(meta: Dict[str, Any], umm_tf: Dict[str, Any], arena: Dict[str, Any]) -> Dict[str, Any]:
    if meta["source"] == "umm_tf":
        block = umm_tf.get(meta["block"]) or {}
        if not isinstance(block, dict):
            return _empty_stats()
        return {
            "total_trades": int(block.get("total_trades") or 0),
            "win_trades": int(block.get("win_trades") or 0),
            "loss_trades": int(block.get("total_trades") or 0) - int(block.get("win_trades") or 0),
            "win_rate_pct": (
                (float(block.get("win_trades") or 0) / float(block.get("total_trades")) * 100.0)
                if float(block.get("total_trades") or 0) > 0
                else 0.0
            ),
            "pnl_jpy": float(block.get("total_pnl") or 0.0),
            "pnl_bp": float(block.get("total_pnl_bp") or 0.0),
            "position": str(block.get("position") or "FLAT"),
            "stats_1h": block.get("stats_1h") or {},
            "stats_24h": block.get("stats_24h") or {},
            "runner_elapsed_h": float(umm_tf.get("elapsed_hours") or 0.0),
        }

    ranking = arena.get("ranking") or []
    row = next((r for r in ranking if r.get("strat_id") == meta["strat_id"]), None)
    if not isinstance(row, dict):
        return _empty_stats()
    return {
        "total_trades": int(row.get("total_trades") or 0),
        "win_trades": int(row.get("win_trades") or 0),
        "loss_trades": int(row.get("loss_trades") or 0),
        "win_rate_pct": float(row.get("win_rate_pct") or 0.0),
        "pnl_jpy": float(row.get("total_pnl_jpy") or 0.0),
        "pnl_bp": float(row.get("total_pnl_bp") or 0.0),
        "position": str(row.get("position") or "FLAT"),
        "stats_1h": row.get("stats_1h") or {},
        "stats_24h": row.get("stats_24h") or {},
        "runner_elapsed_h": None,
        "is_halted": bool(row.get("is_halted")),
        "entry_halted": bool(row.get("entry_halted")),
    }


def _window(stats: Dict[str, Any], key: str) -> Dict[str, Any]:
    w = stats.get(key) or {}
    if not isinstance(w, dict):
        w = {}
    return {
        "total_trades": int(w.get("total_trades") or 0),
        "win_trades": int(w.get("win_trades") or 0),
        "loss_trades": int(w.get("loss_trades") or 0),
        "win_rate_pct": float(w.get("win_rate_pct") or 0.0),
        "pnl_jpy": float(w.get("pnl_jpy") or 0.0),
        "pnl_bp": float(w.get("pnl_bp") or 0.0),
    }


def _all_time(cur: Dict[str, Any]) -> Dict[str, Any]:
    """DRYRUN開始からの最長累積（台帳 total）。"""
    trades = int(cur.get("total_trades") or 0)
    wins = int(cur.get("win_trades") or 0)
    return {
        "total_trades": trades,
        "win_trades": wins,
        "loss_trades": int(cur.get("loss_trades") or max(0, trades - wins)),
        "win_rate_pct": float(cur.get("win_rate_pct") or 0.0),
        "pnl_jpy": float(cur.get("pnl_jpy") or 0.0),
        "pnl_bp": float(cur.get("pnl_bp") or 0.0),
    }


def _proc_alive(pid_path: str) -> Tuple[bool, Optional[int]]:
    if not os.path.exists(pid_path):
        return False, None
    try:
        pid = int(open(pid_path).read().strip())
    except Exception:
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except OSError:
        return False, pid


def _ops_comment(rows: List[Dict[str, Any]]) -> str:
    """直近1時間の運用コメントを日本語で生成。"""
    active = [r for r in rows if r["h1"]["total_trades"] > 0]
    idle = [r for r in rows if r["h1"]["total_trades"] == 0]
    parts: List[str] = []

    if not active:
        parts.append("直近1時間は3戦略とも新規決済なし。板が薄い／シグナル未成立の可能性。")
    else:
        best = max(active, key=lambda r: r["h1"]["pnl_bp"])
        worst = min(active, key=lambda r: r["h1"]["pnl_bp"])
        sum_bp = sum(r["h1"]["pnl_bp"] for r in rows)
        sum_n = sum(r["h1"]["total_trades"] for r in rows)
        if sum_bp >= 5:
            tone = "堅調"
        elif sum_bp >= 0:
            tone = "小幅プラス"
        elif sum_bp > -10:
            tone = "小幅マイナス"
        else:
            tone = "軟調"
        parts.append(
            f"直近1hは合算 {_fmt_bp(sum_bp)} / {sum_n}決済で{tone}。"
            f"先頭は {best['label']}（{_fmt_bp(best['h1']['pnl_bp'])}・勝率{best['h1']['win_rate_pct']:.0f}%）。"
        )
        if worst["key"] != best["key"] and worst["h1"]["pnl_bp"] < 0:
            parts.append(
                f"{worst['label']} が {_fmt_bp(worst['h1']['pnl_bp'])} と足を引っ張った。"
            )

    for r in rows:
        h1 = r["h1"]
        if h1["total_trades"] >= 3 and h1["win_rate_pct"] < 40 and h1["pnl_bp"] < 0:
            parts.append(
                f"{r['label']}: 1h勝率{h1['win_rate_pct']:.0f}%・連敗寄り。エントリー頻度は維持しつつ出口理由の偏りを要監視。"
            )
        if h1["total_trades"] >= 8 and abs(h1["pnl_bp"]) < 2:
            parts.append(
                f"{r['label']}: 回転多いが損益ほぼフラット。手数料感応度が高い時間帯。"
            )
        if r.get("is_halted") or r.get("entry_halted"):
            parts.append(f"{r['label']}: halt/entry_halt 中。再開条件を確認。")

    if idle and active:
        parts.append("休止: " + " / ".join(r["label"] for r in idle) + "。")

    tf = next((r for r in rows if r["key"] == "TF2BP_v1"), None)
    if tf and tf["h24"]["pnl_bp"] <= -50:
        parts.append(
            f"TF2BPの24hは {_fmt_bp(tf['h24']['pnl_bp'])}。LIVE昇格は見送り継続（DRYのみ）。"
        )

    parts.append("方針: パラメータ凍結・実弾フラグOFFのまま観測継続。")
    return " ".join(parts)


class LivePrep3StratHourlyReporter:
    def __init__(self) -> None:
        self.notifier = QuantDiscordNotifier()
        self.last_sent_hour: int = -1
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        os.makedirs(os.path.dirname(SESSION_STATE), exist_ok=True)

    def _log(self, msg: str) -> None:
        # stdout は start script の nohup リダイレクト先と同一ファイル。二重書き込みしない。
        line = f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST')}] {msg}"
        print(line, flush=True)

    def _collect(self) -> List[Dict[str, Any]]:
        umm_tf = _load_json(UMM_TF_STATE)
        arena = _load_json(ARENA_STATE)
        rows: List[Dict[str, Any]] = []
        for meta in STRATEGIES:
            cur = _extract_block(meta, umm_tf, arena)
            rows.append(
                {
                    "key": meta["key"],
                    "label": meta["label"],
                    "position": cur.get("position", "FLAT"),
                    # DRYRUN開始〜 = 台帳最長累積
                    "all": _all_time(cur),
                    "h1": _window(cur, "stats_1h"),
                    "h24": _window(cur, "stats_24h"),
                    "runner_elapsed_h": cur.get("runner_elapsed_h"),
                    "is_halted": cur.get("is_halted", False),
                    "entry_halted": cur.get("entry_halted", False),
                    "alive_source": meta["source"],
                }
            )
        return rows

    def _health_line(self) -> str:
        umm_ok, umm_pid = _proc_alive(os.path.join(BASE_DIR, "data", "dryrun_umm_tf2bp.pid"))
        arena_ok, arena_pid = _proc_alive(os.path.join(BASE_DIR, "data", "dryrun_approved_arena.pid"))
        # PID files can be stale; also check by process name via /proc
        umm_running = umm_ok or self._any_proc("run_dryrun_umm_tf2bp_24h")
        arena_running = arena_ok or self._any_proc("run_dryrun_approved_arena")
        return (
            f"TF2BP runner: `{'🟢 UP' if umm_running else '🔴 DOWN'}`"
            f"{f' (pid {umm_pid})' if umm_pid and umm_ok else ''}\n"
            f"Arena runner: `{'🟢 UP' if arena_running else '🔴 DOWN'}`"
            f"{f' (pid {arena_pid})' if arena_pid and arena_ok else ''}\n"
            f"Mode: `DRYRUN only` · `ENABLE_REAL_TRADING=false` · params FROZEN"
        )

    @staticmethod
    def _any_proc(needle: str) -> bool:
        try:
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    with open(f"/proc/{name}/cmdline", "rb") as f:
                        cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
                    if needle in cmd:
                        return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    def build_and_send(self, trigger_reason: str = "毎時定期報告") -> bool:
        rows = self._collect()
        now_jst = datetime.now(JST)
        now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S JST")

        sum_all_bp = sum(r["all"]["pnl_bp"] for r in rows)
        sum_1h_bp = sum(r["h1"]["pnl_bp"] for r in rows)
        sum_24h_bp = sum(r["h24"]["pnl_bp"] for r in rows)
        sum_all_jpy = sum(r["all"]["pnl_jpy"] for r in rows)
        sum_1h_jpy = sum(r["h1"]["pnl_jpy"] for r in rows)
        sum_24h_jpy = sum(r["h24"]["pnl_jpy"] for r in rows)
        sum_all_n = sum(r["all"]["total_trades"] for r in rows)

        comment = _ops_comment(rows)
        color = 0x2ECC71 if sum_1h_bp >= 0 else 0xE74C3C

        field_lines: List[str] = []
        for i, r in enumerate(rows, 1):
            field_lines.append(
                f"**{i}. {r['label']}** `{r['key']}` `[{r['position']}]`\n"
                f"• 1h成績: **`{_fmt_bp(r['h1']['pnl_bp'])}`** "
                f"({r['h1']['total_trades']}戦/{r['h1']['win_rate_pct']:.0f}% / {_fmt_jpy(r['h1']['pnl_jpy'])})\n"
                f"• 24h累積: **`{_fmt_bp(r['h24']['pnl_bp'])}`** "
                f"({r['h24']['total_trades']}戦/{r['h24']['win_rate_pct']:.0f}% / {_fmt_jpy(r['h24']['pnl_jpy'])})\n"
                f"• DRYRUN開始〜(最長): **`{_fmt_bp(r['all']['pnl_bp'])}`** "
                f"({r['all']['total_trades']}戦/{r['all']['win_rate_pct']:.0f}% / {_fmt_jpy(r['all']['pnl_jpy'])})"
            )

        embed = {
            "title": f"🎯 【LIVE準備3戦略 DRYRUN 毎時レポート】 ({trigger_reason})",
            "description": (
                f"🕒 **集計**: `{now_str}`\n"
                f"Σ **1h成績**: **`{_fmt_bp(sum_1h_bp)}`** (`{_fmt_jpy(sum_1h_jpy)}`)\n"
                f"Σ **24h累積**: **`{_fmt_bp(sum_24h_bp)}`** (`{_fmt_jpy(sum_24h_jpy)}`)\n"
                f"Σ **DRYRUN開始〜(最長)**: **`{_fmt_bp(sum_all_bp)}`** "
                f"(`{_fmt_jpy(sum_all_jpy)}` / {sum_all_n}戦)\n"
                f"🔒 自動調整禁止 / 実弾OFF / 観測専用"
            ),
            "color": color,
            "fields": [
                {
                    "name": "① 戦略別（1h / 24h / DRYRUN開始〜最長）",
                    "value": "\n".join(field_lines)[:1024],
                    "inline": False,
                },
                {
                    "name": "② 直近1時間の運用コメント",
                    "value": comment[:1024],
                    "inline": False,
                },
                {
                    "name": "③ ランナー死活",
                    "value": self._health_line()[:1024],
                    "inline": False,
                },
            ],
            "footer": {"text": "Antigravity LIVE-prep 3-strat DRYRUN · CSR-575"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        ok = self.notifier.post_dryrun_multicast({"embeds": [embed]})
        self._log(
            f"3strat report {'OK' if ok else 'FAIL'} "
            f"(1h={sum_1h_bp:+.2f}bp 24h={sum_24h_bp:+.2f}bp all={sum_all_bp:+.2f}bp)"
        )
        snap_path = os.path.join(BASE_DIR, "data", "live_prep_3strat_latest_report.json")
        _save_json(
            snap_path,
            {
                "ts_jst": now_str,
                "trigger": trigger_reason,
                "sum_1h_bp": sum_1h_bp,
                "sum_24h_bp": sum_24h_bp,
                "sum_all_bp": sum_all_bp,
                "comment": comment,
                "rows": rows,
                "discord_ok": ok,
            },
        )
        return ok

    def run(self) -> None:
        print("=" * 70)
        print("   📢 LIVE準備3戦略 DRYRUN 毎時レポート")
        print("=" * 70)
        print("対象: TF2BP / SpreadCaptureMM / MicroSpreadMM")
        print("送信: 起動時即時 + 毎時ジャスト")
        print("-" * 70)

        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

        self.build_and_send(trigger_reason="起動時即時ステータス報告")
        self.last_sent_hour = datetime.now(JST).hour

        def _sig(sig, _frame):
            self._log(f"signal {sig}; exit")
            sys.exit(0)

        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

        while True:
            try:
                now = datetime.now(JST)
                if now.hour != self.last_sent_hour:
                    self._log(f"hourly tick {now.strftime('%H:%M:%S')}")
                    self.build_and_send(trigger_reason="毎時定期報告")
                    self.last_sent_hour = now.hour
                time.sleep(15.0)
            except Exception as exc:
                self._log(f"loop error: {exc}")
                traceback.print_exc()
                time.sleep(30.0)


def main() -> None:
    LivePrep3StratHourlyReporter().run()


if __name__ == "__main__":
    main()
