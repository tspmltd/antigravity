"""
Spread Gate & Signal Filter Validation Tracker
=============================================
ユーザー指示書準拠: #spread-gate-validation 毎時検証エンジン
1. 総シグナル数 (total_signals)
2. 通過数 (passed_signals)
3. Gate突破率 (pass_rate_pct = passed / total * 100)
4. 平均Spread (avg_spread_jpy, avg_spread_bp)
5. 最大Spread (max_spread_jpy, max_spread_bp)

【判定基準】
• 理想: 突破率 20～40% (良質なMaker/Taker機会のみ厳選)
• 危険: 突破率 1% 以下 (スプレッド条件過多で実質取引不能)
"""
import os
import json
import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
DEFAULT_SAVE_PATH = "/home/azureuser/antigravity/data/spread_gate_stats.json"


class SpreadGateTracker:
    def __init__(self, save_path: str = DEFAULT_SAVE_PATH, max_history_sec: float = 86400.0):
        self.save_path = save_path
        self.max_history_sec = max_history_sec

        # 履歴バッファ: (timestamp, value/event)
        self.spread_history: List[Dict[str, float]] = []  # {"ts": float, "spread": float, "bp": float}
        self.signal_events: List[Dict[str, Any]] = []     # {"ts": float, "strat_id": str, "passed": bool, "block_reason": Optional[str]}

        self._load_existing_state()

    def _load_existing_state(self):
        if os.path.exists(self.save_path):
            try:
                with open(self.save_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 直近の集計データを復元
                now = time.time()
                for item in data.get("recent_spreads", []):
                    if now - item.get("ts", 0) <= self.max_history_sec:
                        self.spread_history.append(item)
                for item in data.get("recent_signals", []):
                    if now - item.get("ts", 0) <= self.max_history_sec:
                        self.signal_events.append(item)
            except Exception:
                pass

    def record_tick_spread(self, spread_jpy: float, ltp: float, timestamp: Optional[float] = None):
        """毎Tickのスプレッド観測値を記録"""
        now = time.time() if timestamp is None else timestamp
        bp = (spread_jpy / ltp * 10000.0) if ltp > 0 else 0.0
        self.spread_history.append({"ts": now, "spread": spread_jpy, "bp": bp})
        self._prune(now)

    def record_signal_decision(self, strat_id: str, passed: bool, block_reason: Optional[str] = None, timestamp: Optional[float] = None):
        """シグナル発生とゲート判定結果を記録"""
        now = time.time() if timestamp is None else timestamp
        self.signal_events.append({
            "ts": now,
            "strat_id": strat_id,
            "passed": passed,
            "block_reason": block_reason,
        })
        self._prune(now)
        self.persist()

    def _prune(self, current_time: float):
        """24時間以上古いレコードを破棄"""
        cutoff = current_time - self.max_history_sec
        if len(self.spread_history) > 1000 and self.spread_history[0]["ts"] < cutoff:
            self.spread_history = [s for s in self.spread_history if s["ts"] >= cutoff]
        if len(self.signal_events) > 1000 and self.signal_events[0]["ts"] < cutoff:
            self.signal_events = [e for e in self.signal_events if e["ts"] >= cutoff]

    def get_window_stats(self, window_sec: float = 3600.0) -> Dict[str, Any]:
        """指定ウィンドウ (デフォルト1時間) の統計を集計"""
        now = time.time()
        cutoff = now - window_sec

        # 1. スプレッド統計
        win_spreads = [s for s in self.spread_history if s["ts"] >= cutoff]
        if win_spreads:
            spread_vals = [s["spread"] for s in win_spreads]
            bp_vals = [s["bp"] for s in win_spreads]
            avg_spread_jpy = round(sum(spread_vals) / len(spread_vals), 1)
            max_spread_jpy = round(max(spread_vals), 1)
            min_spread_jpy = round(min(spread_vals), 1)
            avg_spread_bp = round(sum(bp_vals) / len(bp_vals), 2)
            max_spread_bp = round(max(bp_vals), 2)
        else:
            avg_spread_jpy = 0.0
            max_spread_jpy = 0.0
            min_spread_jpy = 0.0
            avg_spread_bp = 0.0
            max_spread_bp = 0.0

        # 2. シグナル＆ゲート統計
        win_signals = [e for e in self.signal_events if e["ts"] >= cutoff]
        total_signals = len(win_signals)
        passed_signals = sum(1 for e in win_signals if e["passed"])

        pass_rate_pct = round((passed_signals / total_signals * 100.0), 1) if total_signals > 0 else 0.0

        # ゲート別遮断内訳
        gate_blocks: Dict[str, int] = {}
        for e in win_signals:
            if not e["passed"]:
                reason = e.get("block_reason") or "other"
                gate_blocks[reason] = gate_blocks.get(reason, 0) + 1

        # 判定
        # 理想: 20〜40%
        # 危険: 1% 以下
        if total_signals == 0:
            status = "NO_SIGNALS"
            status_desc = "⚪ シグナル未発生 (待機中)"
            color = 0x95A5A6
        elif pass_rate_pct <= 1.0:
            status = "DANGER"
            status_desc = f"🔴 危険: 突破率 {pass_rate_pct:.1f}% (1%以下: スプレッド条件過多で実質取引不能)"
            color = 0xE74C3C
        elif 20.0 <= pass_rate_pct <= 40.0:
            status = "IDEAL"
            status_desc = f"🟢 理想水準: 突破率 {pass_rate_pct:.1f}% (20〜40%: 厳選エントリー)"
            color = 0x2ECC71
        elif pass_rate_pct < 20.0:
            status = "TIGHT"
            status_desc = f"🟡 厳格水準: 突破率 {pass_rate_pct:.1f}% (1〜20%: フィルターやや厳しめ)"
            color = 0xF1C40F
        else:
            status = "LOOSE"
            status_desc = f"🟠 緩和水準: 突破率 {pass_rate_pct:.1f}% (>40%: フィルターやや甘め)"
            color = 0xE67E22

        return {
            "window_sec": window_sec,
            "window_hours": round(window_sec / 3600.0, 1),
            "total_signals": total_signals,
            "passed_signals": passed_signals,
            "pass_rate_pct": pass_rate_pct,
            "avg_spread_jpy": avg_spread_jpy,
            "max_spread_jpy": max_spread_jpy,
            "min_spread_jpy": min_spread_jpy,
            "avg_spread_bp": avg_spread_bp,
            "max_spread_bp": max_spread_bp,
            "gate_blocks": gate_blocks,
            "status": status,
            "status_desc": status_desc,
            "color": color,
        }

    def persist(self):
        """最新状態をJSONファイルへアトミック保存"""
        try:
            stats_1h = self.get_window_stats(3600.0)
            stats_24h = self.get_window_stats(86400.0)

            payload = {
                "timestamp": int(time.time() * 1000),
                "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
                "stats_1h": stats_1h,
                "stats_24h": stats_24h,
                "recent_spreads": self.spread_history[-300:],  # 最新300件を保存
                "recent_signals": self.signal_events[-300:],  # 最新300件を保存
            }

            os.makedirs(os.path.dirname(self.save_path), exist_ok=True)
            tmp_path = self.save_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.save_path)
        except Exception as e:
            pass
