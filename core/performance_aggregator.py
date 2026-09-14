import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

JST = timezone(timedelta(hours=9))


def get_jst_now() -> datetime:
    """日本標準時 (JST = UTC+9) の現在日時を取得"""
    return datetime.now(JST)


def get_start_of_day_ts(dt: Optional[datetime] = None) -> float:
    """
    指定日時（省略時は現在）の日本時間 00:00:00 (24:00起点) のUNIXタイムスタンプを返す
    """
    if dt is None:
        dt = get_jst_now()
    elif dt.tzinfo is None:
        dt = dt.astimezone(JST)
    sod = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return sod.timestamp()


def aggregate_trades(
    trades: List[Dict[str, Any]],
    start_ts: float,
    end_ts: Optional[float] = None
) -> Dict[str, Any]:
    """
    指定したタイムスタンプ範囲内の約定履歴を集計する。
    """
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
        "trades": selected
    }


def compute_performance_snapshots(
    trades_map: Dict[str, List[Dict[str, Any]]],
    unrealized_pnl_map: Optional[Dict[str, float]] = None,
    positions_map: Optional[Dict[str, float]] = None,
    now_ts: Optional[float] = None
) -> Dict[str, Any]:
    """
    全戦略の約定履歴から、
    1. 直近1時間の成績 (Hourly)
    2. 24時（00:00 JST）起点の本日の累積成績 (Daily Cumulative)
    3. 全期間トータルの累積成績 (All-Time Total)
    を全体および各戦略ごとに一括集計する。
    """
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

        h_stat = aggregate_trades(trades, start_ts=one_hour_ago_ts, end_ts=now_ts)
        d_stat = aggregate_trades(trades, start_ts=sod_ts, end_ts=now_ts)
        t_stat = aggregate_trades(trades, start_ts=0.0, end_ts=now_ts)

        u_pnl = unrealized_pnl_map.get(name, 0.0)
        pos = positions_map.get(name, 0.0)

        strategies_stats.append({
            "name": name,
            "position": pos,
            "unrealized_pnl": u_pnl,
            "hourly": h_stat,
            "daily": d_stat,
            "total": {
                **t_stat,
                "unrealized_pnl": u_pnl,
                "total_pnl": t_stat["realized_pnl"] + u_pnl,
            }
        })

    total_hourly = aggregate_trades(all_trades, start_ts=one_hour_ago_ts, end_ts=now_ts)
    total_daily = aggregate_trades(all_trades, start_ts=sod_ts, end_ts=now_ts)
    total_all_time = aggregate_trades(all_trades, start_ts=0.0, end_ts=now_ts)

    tot_unrealized = sum(unrealized_pnl_map.values())
    total_all_time["unrealized_pnl"] = tot_unrealized
    total_all_time["total_pnl"] = total_all_time["realized_pnl"] + tot_unrealized

    now_jst = get_jst_now()
    start_of_hour = (now_jst - timedelta(hours=1)).strftime("%H:%M")
    end_of_hour = now_jst.strftime("%H:%M")
    today_str = now_jst.strftime("%Y-%m-%d")

    return {
        "timestamp_jst": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "today_str": today_str,
        "hour_range": f"{start_of_hour} 〜 {end_of_hour}",
        "hourly_stats": total_hourly,
        "daily_stats": total_daily,
        "total_stats": total_all_time,
        "strategies_stats": strategies_stats
    }
