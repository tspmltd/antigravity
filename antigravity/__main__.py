import argparse
import sys
from antigravity.config.settings import Settings
from antigravity.strategies.micro_trend import MicroTrendTickStrategy
from antigravity.strategies.inventory_mm import InventorySkewTickMMStrategy
from antigravity.strategies.order_flow_scalp import OrderFlowScalpTickStrategy
from antigravity.strategies.ema_trend import EmaTrendTickStrategy
from antigravity.runtime.runner import AntigravityRunner


def main():
    parser = argparse.ArgumentParser(description="Antigravity HFT Autonomous Engine")
    parser.add_argument("--symbol", default=Settings.PRODUCT_CODE, help="取引対象シンボル (デフォルト: FX_BTC_JPY)")
    parser.add_argument("--order-size", type=float, default=Settings.ORDER_SIZE_BTC, help="1注文サイズ BTC")
    parser.add_argument("--report-interval", type=float, default=Settings.REPORT_INTERVAL_SEC, help="定期レポート送信間隔（秒）")
    parser.add_argument("--max-dd", type=float, default=Settings.MAX_DRAWDOWN_LIMIT_JPY, help="許容最大ピークドローダウン (円)")
    parser.add_argument("--cooldown", type=float, default=Settings.CIRCUIT_BREAKER_COOLDOWN_SEC, help="冷却待機期間（秒）")
    args = parser.parse_args()

    print("=" * 60)
    print("  🚀 ANTIGRAVITY HFT ENGINE v1.0.0")
    print(f"  Symbol: {args.symbol}")
    print(f"  Order Size: {args.order_size} BTC")
    print(f"  Max Drawdown Limit: {args.max_dd:,.0f} JPY")
    print(f"  Cooldown Seconds: {args.cooldown:.0f} s")
    print(f"  Report Interval: {args.report_interval:.0f} s")
    print("=" * 60)

    # 4大戦略を初期化 (マイクロトレンド + インベントリMM + オーダーフロースキャルプ + 大波EMAトレンド)
    strategies = [
        MicroTrendTickStrategy(),
        InventorySkewTickMMStrategy(),
        OrderFlowScalpTickStrategy(),
        EmaTrendTickStrategy(),
    ]


    runner = AntigravityRunner(
        strategies=strategies,
        product_code=args.symbol,
        order_size=args.order_size,
        max_drawdown_jpy=args.max_dd,
        cooldown_sec=args.cooldown,
        report_interval_sec=args.report_interval,
    )

    runner.run_forever()


if __name__ == "__main__":
    main()
