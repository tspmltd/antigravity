import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core.notifier import DiscordNotifier
from core.tick_stream import TickStream
from core.tick_strategy import MicroTrendTickStrategy, InventorySkewTickMMStrategy
from execution.tick_live_trader import TickLiveTrader


class TestTickEngine(unittest.TestCase):
    def setUp(self):
        self.stream = TickStream(product_code="FX_BTC_JPY", window_seconds=15.0)

    def test_tick_stream_accumulation_and_delta(self):
        """約定データの蓄積とTaker Delta計算のテスト"""
        now = datetime.now(timezone.utc)
        ticks_data = [
            {"id": 1, "side": "BUY", "price": 10000000.0, "size": 0.5, "exec_date": now.isoformat()},
            {"id": 2, "side": "BUY", "price": 10002000.0, "size": 0.5, "exec_date": (now + timedelta(seconds=1)).isoformat()},
            {"id": 3, "side": "SELL", "price": 10002000.0, "size": 0.2, "exec_date": (now + timedelta(seconds=2)).isoformat()},
        ]

        added = self.stream.add_ticks(ticks_data)
        self.assertEqual(added, 3)

        # 重複IDのテスト（重複して追加されないこと）
        added_duplicate = self.stream.add_ticks(ticks_data)
        self.assertEqual(added_duplicate, 0)

        stats = self.stream.compute_flow_stats(window_sec=15.0)
        self.assertEqual(stats["tick_count"], 3)
        self.assertEqual(stats["taker_buy_vol"], 1.0)
        self.assertEqual(stats["taker_sell_vol"], 0.2)
        self.assertAlmostEqual(stats["net_delta"], 0.8)
        # delta_ratio = (1.0 - 0.2) / 1.2 = 0.8 / 1.2 = 0.666...
        self.assertAlmostEqual(stats["delta_ratio"], 0.8 / 1.2, places=2)
        # 価格変動: 10,000,000 -> 10,002,000 (+2,000円 = +2.0bp)
        self.assertAlmostEqual(stats["price_change_bp"], 2.0, places=4)

    def test_2bp_momentum_detection(self):
        """2bp初動モメンタム検知ロジックのテスト"""
        now = datetime.now(timezone.utc)
        # 10,000,000 から 10,002,500 (+2.5bp) へ上昇、すべて買いTaker
        ticks = [
            {"id": 10, "side": "BUY", "price": 10000000.0, "size": 1.0, "exec_date": now.isoformat()},
            {"id": 11, "side": "BUY", "price": 10002500.0, "size": 1.0, "exec_date": (now + timedelta(seconds=1)).isoformat()},
        ]
        self.stream.add_ticks(ticks)

        is_up = self.stream.is_2bp_momentum_up(min_delta_ratio=0.5)
        is_down = self.stream.is_2bp_momentum_down(min_delta_ratio=-0.5)

        self.assertTrue(is_up, "2.5bp上昇かつ買い過半で2bp初動アップと判定されるべき")
        self.assertFalse(is_down, "下降判定はFalseであるべき")

    def test_cancel_on_reverse_flow(self):
        """逆方向Taker急増による即時Cancel判定のテスト"""
        now = datetime.now(timezone.utc)
        # 売りTakerが圧倒（sell: 2.0, buy: 0.1 -> delta_ratio ~ -0.9）
        ticks = [
            {"id": 20, "side": "SELL", "price": 10000000.0, "size": 1.0, "exec_date": now.isoformat()},
            {"id": 21, "side": "SELL", "price": 9999000.0, "size": 1.0, "exec_date": (now + timedelta(seconds=1)).isoformat()},
            {"id": 22, "side": "BUY", "price": 9999000.0, "size": 0.1, "exec_date": (now + timedelta(seconds=2)).isoformat()},
        ]
        self.stream.add_ticks(ticks)

        # 買い指値/ロングは即時Cancelすべき
        should_cancel_bid = self.stream.should_cancel_bid(reverse_delta_threshold=-0.6)
        should_cancel_ask = self.stream.should_cancel_ask(reverse_delta_threshold=0.6)

        self.assertTrue(should_cancel_bid, "売りTaker急増時は買い指値/ロングを即時Cancelすべき")
        self.assertFalse(should_cancel_ask, "売りTaker急増時は売り指値のCancelは不要")

    def test_micro_trend_strategy_entry_and_exit(self):
        """MicroTrendTickStrategy の2bp初動エントリーと10bp+利確＆逆流Cancelのテスト"""
        strat = MicroTrendTickStrategy(
            trigger_bp=2.0,
            target_trend_bp=12.0,
            cancel_reverse_threshold=-0.5,
        )

        # 1. 2bp初動でBUYエントリー判定
        flow_up = {
            "current_price": 10002500.0,
            "delta_ratio": 0.75,
            "price_change_bp": 2.5,
            "flow_persistence": 0.85,
            "reverse_noise_ratio": 0.15,
        }
        res_entry = strat.on_tick(
            tick={"price": 10002500.0},
            flow_stats=flow_up,
            current_pos=0.0,
            entry_price=0.0,
        )
        self.assertEqual(res_entry["action"], "BUY")
        self.assertIn("2bp初動", res_entry["reason"])

        # 2. ポジション保有中、+12bp（大波）到達でEXIT判定
        flow_target = {
            "current_price": 10015000.0, # +12.5bp
            "delta_ratio": 0.5,
            "price_change_bp": 12.5,
            "flow_persistence": 0.7,
            "reverse_noise_ratio": 0.3,
        }
        res_tp = strat.on_tick(
            tick={"price": 10015000.0},
            flow_stats=flow_target,
            current_pos=0.001,
            entry_price=10002500.0,
        )
        self.assertEqual(res_tp["action"], "EXIT")
        self.assertIn("大波利確", res_tp["reason"])

        # 3. ポジション保有中、逆方向Taker急増で即座にCANCEL判定
        flow_reverse = {
            "current_price": 10002000.0,
            "delta_ratio": -0.65, # 売りTaker殺到
            "price_change_bp": -0.5,
            "flow_persistence": 0.8,
            "reverse_noise_ratio": 0.8,
        }
        res_cancel = strat.on_tick(
            tick={"price": 10002000.0},
            flow_stats=flow_reverse,
            current_pos=0.001,
            entry_price=10002500.0,
        )
        self.assertEqual(res_cancel["action"], "CANCEL")
        self.assertIn("Taker急増", res_cancel["reason"])

    def test_inventory_skew_mm_strategy_actions(self):
        """InventorySkewTickMMStrategy の指値と逆流Cancelのテスト"""
        mm_strat = InventorySkewTickMMStrategy(
            spread_bp=3.0,
            cancel_reverse_threshold=-0.6
        )

        # 逆方向売り急増時はロング/買いをCANCEL
        flow_rev = {
            "current_price": 10000000.0,
            "delta_ratio": -0.75,
            "price_change_bp": -1.0,
        }
        res_cancel = mm_strat.on_tick(
            tick={"price": 10000000.0},
            flow_stats=flow_rev,
            current_pos=0.001,
            entry_price=10000000.0,
        )
        self.assertEqual(res_cancel["action"], "CANCEL")

    def test_websocket_push_callback_execution(self):
        """WebSocketプッシュ受信時にミリ秒でコールバックと戦略判定が発火することのテスト"""
        dummy_notifier = DiscordNotifier(webhook_url="", system_webhook_url="", alert_webhook_url="")
        trader = TickLiveTrader(
            product_code="FX_BTC_JPY",
            order_size_btc=0.001,
            use_websocket=True,
            enable_real_trading=False,
            notifier=dummy_notifier,
        )

        now = datetime.now(timezone.utc)
        # 1. 2bp上昇の買い約定ストリームをプッシュ注入
        ticks_up = [
            {"id": 101, "side": "BUY", "price": 10000000.0, "size": 1.0, "exec_date": now.isoformat()},
            {"id": 102, "side": "BUY", "price": 10002500.0, "size": 1.0, "exec_date": (now + timedelta(seconds=1)).isoformat()},
        ]
        trader.stream.add_ticks(ticks_up)

        # MicroTrendTickStrategy が即座に BUY を執行しているか確認
        strat_info = trader.strategies_map["MicroTrendTick"]
        self.assertEqual(strat_info["position_btc"], 0.001, "2bp初動で即座にロングエントリーされるべき")
        self.assertEqual(strat_info["entry_price"], 10002500.0)

        # 2. 売りTaker殺到（逆流）の約定ストリームをプッシュ注入
        ticks_down = [
            {"id": 103, "side": "SELL", "price": 10001000.0, "size": 3.0, "exec_date": (now + timedelta(seconds=2)).isoformat()},
            {"id": 104, "side": "SELL", "price": 10000000.0, "size": 3.0, "exec_date": (now + timedelta(seconds=3)).isoformat()},
        ]
        trader.stream.add_ticks(ticks_down)

        # 逆方向急増によりミリ秒でCANCEL脱出してポジションがクローズされているか確認
        self.assertEqual(strat_info["position_btc"], 0.0, "売り急増検知で即座にポジションがCANCEL脱出されるべき")
        self.assertEqual(len(strat_info["trades_history"]), 1)
        self.assertIn("Cancel脱出", strat_info["trades_history"][0]["reason"])

    def test_circuit_breaker_emergency_halt(self):
        """サーキットブレーカー発動時に全建玉が強制解消され、新規注文が凍結されることのテスト"""
        dummy_notifier = DiscordNotifier(webhook_url="", system_webhook_url="", alert_webhook_url="")
        trader = TickLiveTrader(
            product_code="FX_BTC_JPY",
            order_size_btc=0.001,
            use_websocket=False,
            enable_real_trading=False,
            notifier=dummy_notifier,
        )

        # 意図的にポジションを付与
        strat_info = trader.strategies_map["MicroTrendTick"]
        strat_info["position_btc"] = 0.005
        strat_info["entry_price"] = 10000000.0

        # サーキットブレーカー発動
        trader.trigger_circuit_breaker("許容ドローダウンリミット超過 (-3,500円)")

        self.assertTrue(trader.is_halted, "is_haltedがTrueになるべき")
        self.assertEqual(strat_info["position_btc"], 0.0, "全建玉が強制解消されて0になるべき")

        # 停止中に新規プッシュが来てもエントリーされないことの検証
        now = datetime.now(timezone.utc)
        ticks_up = [
            {"id": 201, "side": "BUY", "price": 10000000.0, "size": 1.0, "exec_date": now.isoformat()},
            {"id": 202, "side": "BUY", "price": 10005000.0, "size": 1.0, "exec_date": (now + timedelta(seconds=1)).isoformat()},
        ]
        trader._on_websocket_ticks(ticks_up, trader.stream.compute_flow_stats())
        self.assertEqual(strat_info["position_btc"], 0.0, "緊急停止中は新規エントリーが凍結されているべき")

        # 冷却期間完了による自動復帰（resume_trading）のテスト
        trader.resume_trading("冷却期間完了テスト")
        self.assertFalse(trader.is_halted, "resume_trading後はis_haltedがFalseに戻るべき")

        # 復帰後に新規プッシュが来たら正常にエントリーが再開されることの検証
        ticks_resume = [
            {"id": 203, "side": "BUY", "price": 10006000.0, "size": 1.0, "exec_date": (now + timedelta(seconds=2)).isoformat()},
        ]
        trader._on_websocket_ticks(ticks_resume, trader.stream.compute_flow_stats())
        # いずれかの戦略 (InventorySkewTickMM等) が注文を発行してポジションを保有できているか
        has_pos = any(s["position_btc"] != 0.0 for s in trader.strategies_map.values())
        self.assertTrue(has_pos, "復帰後は正常にエントリーが再開されるべき")

    def test_order_flow_scalp_strategy(self):
        """OrderFlowScalpTickStrategy の高頻度スキャルピング判定テスト"""
        from core.tick_strategy import OrderFlowScalpTickStrategy
        strat = OrderFlowScalpTickStrategy(imbalance_threshold=0.40, target_tp_bp=2.0)

        # 1. 買いインバランス (delta_ratio >= 0.40) で瞬時エントリー
        flow_buy = {"delta_ratio": 0.55}
        res_entry = strat.on_tick(tick={"price": 10000000.0}, flow_stats=flow_buy, current_pos=0.0, entry_price=0.0)
        self.assertEqual(res_entry["action"], "BUY")
        self.assertIn("インバランス", res_entry["reason"])

        # 2. +2.0bp到達で瞬時利確
        res_tp = strat.on_tick(tick={"price": 10002100.0}, flow_stats=flow_buy, current_pos=0.001, entry_price=10000000.0)
        self.assertEqual(res_tp["action"], "EXIT")
        self.assertIn("スキャルプ瞬時利確", res_tp["reason"])

        # 3. 逆流発生で即脱出
        flow_rev = {"delta_ratio": -0.45}
        res_cancel = strat.on_tick(tick={"price": 10000500.0}, flow_stats=flow_rev, current_pos=0.001, entry_price=10000000.0)
        self.assertEqual(res_cancel["action"], "CANCEL")
        self.assertIn("逆流即脱出", res_cancel["reason"])


if __name__ == "__main__":
    unittest.main()
