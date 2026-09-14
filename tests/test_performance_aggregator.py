import sys
import unittest
import time
from datetime import datetime, timezone, timedelta

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
from core.performance_aggregator import (
    get_jst_now,
    get_start_of_day_ts,
    aggregate_trades,
    compute_performance_snapshots,
    JST
)
from core.notifier import DiscordNotifier


class TestPerformanceAggregator(unittest.TestCase):
    def test_start_of_day_ts(self):
        """24時起点 (00:00 JST) のタイムスタンプ計算テスト"""
        fixed_dt = datetime(2026, 9, 14, 15, 30, 45, tzinfo=JST)
        sod_ts = get_start_of_day_ts(fixed_dt)
        sod_dt = datetime.fromtimestamp(sod_ts, tz=JST)

        self.assertEqual(sod_dt.year, 2026)
        self.assertEqual(sod_dt.month, 9)
        self.assertEqual(sod_dt.day, 14)
        self.assertEqual(sod_dt.hour, 0)
        self.assertEqual(sod_dt.minute, 0)
        self.assertEqual(sod_dt.second, 0)

    def test_aggregate_trades_filtering(self):
        """直近1時間および本日累計の取引フィルタリングテスト"""
        now = time.time()
        # 30分前（1時間以内＆本日）
        t1 = {"timestamp": now - 1800, "pnl": 50.0, "reason": "MM利確"}
        # 45分前（1時間以内＆本日）
        t2 = {"timestamp": now - 2700, "pnl": -20.0, "reason": "Cancel脱出"}
        # 2時間前（1時間外だが本日）
        t3 = {"timestamp": now - 7200, "pnl": 100.0, "reason": "トレンド利確"}
        # 昨日（26時間前）
        t4 = {"timestamp": now - 26 * 3600, "pnl": 30.0, "reason": "昨日トレード"}

        trades = [t1, t2, t3, t4]

        # 1. 直近1時間
        hourly = aggregate_trades(trades, start_ts=now - 3600, end_ts=now)
        self.assertEqual(hourly["trades_count"], 2)
        self.assertAlmostEqual(hourly["realized_pnl"], 30.0)  # 50 - 20
        self.assertEqual(hourly["wins_count"], 1)
        self.assertEqual(hourly["losses_count"], 1)
        self.assertAlmostEqual(hourly["win_rate_pct"], 50.0)

        # 2. 本日 (仮に4時間前が本日0時以降とする)
        daily = aggregate_trades(trades, start_ts=now - 10000, end_ts=now)
        self.assertEqual(daily["trades_count"], 3)
        self.assertAlmostEqual(daily["realized_pnl"], 130.0)  # 50 - 20 + 100
        self.assertEqual(daily["wins_count"], 2)

        # 3. 全期間
        total = aggregate_trades(trades, start_ts=0.0, end_ts=now)
        self.assertEqual(total["trades_count"], 4)
        self.assertAlmostEqual(total["realized_pnl"], 160.0)

    def test_compute_performance_snapshots(self):
        """compute_performance_snapshots の一括集計テスト"""
        now = time.time()
        trades_map = {
            "StratA": [
                {"timestamp": now - 600, "pnl": 40.0},
                {"timestamp": now - 1200, "pnl": 60.0},
            ],
            "StratB": [
                {"timestamp": now - 300, "pnl": -15.0},
            ]
        }
        unrealized = {"StratA": 10.0, "StratB": -5.0}
        positions = {"StratA": 0.001, "StratB": -0.001}

        res = compute_performance_snapshots(
            trades_map=trades_map,
            unrealized_pnl_map=unrealized,
            positions_map=positions,
            now_ts=now
        )

        self.assertIn("hourly_stats", res)
        self.assertIn("daily_stats", res)
        self.assertIn("total_stats", res)
        self.assertIn("strategies_stats", res)

        # 合計直近1時間
        self.assertEqual(res["hourly_stats"]["trades_count"], 3)
        self.assertAlmostEqual(res["hourly_stats"]["realized_pnl"], 85.0)  # 40+60-15

        # 全期間トータル
        self.assertAlmostEqual(res["total_stats"]["unrealized_pnl"], 5.0)  # 10 - 5
        self.assertAlmostEqual(res["total_stats"]["total_pnl"], 90.0)     # 85 + 5

    def test_notifier_send_periodic_performance_report_mock(self):
        """DiscordNotifier の send_periodic_performance_report メソッド呼出テスト"""
        notifier = DiscordNotifier(webhook_url="", system_webhook_url="", alert_webhook_url="")
        now = time.time()
        res = compute_performance_snapshots(
            trades_map={"TestStrat": [{"timestamp": now - 300, "pnl": 50.0}]},
            unrealized_pnl_map={"TestStrat": 0.0},
            positions_map={"TestStrat": 0.0},
            now_ts=now
        )

        # 例外なく実行でき、未設定時はFalseを返すこと
        success = notifier.send_periodic_performance_report(
            symbol="FX_BTC_JPY",
            current_price=11800000.0,
            hourly_stats=res["hourly_stats"],
            daily_stats=res["daily_stats"],
            total_stats=res["total_stats"],
            strategies_stats=res["strategies_stats"],
            hour_range=res["hour_range"],
            today_str=res["today_str"]
        )
        self.assertFalse(success)


if __name__ == "__main__":
    unittest.main()
