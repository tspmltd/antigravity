import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

JST = timezone(timedelta(hours=9))


def get_jst_now() -> datetime:
    return datetime.now(JST)


def get_start_of_day_ts(dt: Optional[datetime] = None) -> float:
    """日本時間 00:00:00 (24時起点) のタイムスタンプを取得"""
    if dt is None:
        dt = get_jst_now()
    elif dt.tzinfo is None:
        dt = dt.astimezone(JST)
    sod = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return sod.timestamp()


class PerformanceTracker:
    """
    リアルタイム損益・取引パフォーマンス追跡エンジン。
    - 24時（JST 00:00）起点の日次累積成績
    - 直近1時間のスライディング成績
    - 全期間トータル成績
    - 戦略別・全体合算メトリクス
    """

    @staticmethod
    def filter_and_aggregate(
        trades: List[Dict[str, Any]],
        start_ts: float,
        end_ts: Optional[float] = None
    ) -> Dict[str, Any]:
        selected = []
        for t in trades:
            ts = t.get("timestamp")
            if ts is None and "time" in t:
                try:
                    dt_obj = datetime.fromisoformat(t["time"])
                    ts = dt_obj.timestamp()
                except Exception:
                    continue
            if ts is None:
                continue

            if ts >= start_ts and (end_ts is None or ts <= end_ts):
                selected.append(t)

        count = len(selected)
        realized_pnl = sum(t.get("pnl", 0.0) for t in selected)
        wins = [t for t in selected if t.get("pnl", 0.0) > 0]
        losses = [t for t in selected if t.get("pnl", 0.0) < 0]
        win_rate = (len(wins) / count * 100.0) if count > 0 else 0.0

        return {
            "trades_count": count,
            "realized_pnl": realized_pnl,
            "wins_count": len(wins),
            "losses_count": len(losses),
            "win_rate_pct": win_rate,
            "trades": selected,
        }

    def compute_snapshots(
        self,
        trades_map: Dict[str, List[Dict[str, Any]]],
        unrealized_pnl_map: Optional[Dict[str, float]] = None,
        positions_map: Optional[Dict[str, float]] = None,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if now_ts is None:
            now_ts = time.time()

        unrealized_pnl_map = unrealized_pnl_map or {}
        positions_map = positions_map or {}

        sod_ts = get_start_of_day_ts()
        one_hour_ago_ts = now_ts - 3600.0

        all_trades: List[Dict[str, Any]] = []
        strategies_stats = []

        for name, trades in trades_map.items():
            all_trades.extend(trades)

            h_stat = self.filter_and_aggregate(trades, start_ts=one_hour_ago_ts, end_ts=now_ts)
            d_stat = self.filter_and_aggregate(trades, start_ts=sod_ts, end_ts=now_ts)
            t_stat = self.filter_and_aggregate(trades, start_ts=0.0, end_ts=now_ts)

            u_pnl = unrealized_pnl_map.get(name, 0.0)
            pos = positions_map.get(name, 0.0)

            strategies_stats.append({
                "name": name,
                "position_btc": pos,
                "unrealized_pnl": u_pnl,
                "hourly": h_stat,
                "daily": d_stat,
                "total": t_stat,
            })

        overall_hourly = self.filter_and_aggregate(all_trades, start_ts=one_hour_ago_ts, end_ts=now_ts)
        overall_daily = self.filter_and_aggregate(all_trades, start_ts=sod_ts, end_ts=now_ts)
        overall_total = self.filter_and_aggregate(all_trades, start_ts=0.0, end_ts=now_ts)

        total_unrealized = sum(unrealized_pnl_map.values())
        total_position = sum(positions_map.values())

        return {
            "timestamp": now_ts,
            "jst_time_str": get_jst_now().strftime("%Y-%m-%d %H:%M:%S JST"),
            "overall": {
                "position_btc": total_position,
                "unrealized_pnl": total_unrealized,
                "hourly": overall_hourly,
                "daily_cumulative": overall_daily,
                "total_cumulative": overall_total,
                "net_profit_daily": overall_daily["realized_pnl"] + total_unrealized,
                "net_profit_total": overall_total["realized_pnl"] + total_unrealized,
            },
            "strategies": strategies_stats,
        }
