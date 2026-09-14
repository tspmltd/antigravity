import asyncio
import argparse
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
from pipeline.orchestrator import AutonomousPipeline


async def main():
    parser = argparse.ArgumentParser(description="自動売買システム 自律改善マルチエージェント・パイプライン")
    parser.add_argument(
        "--themes",
        nargs="+",
        default=[
            "Trend Following (EMA Cross + ATR Filter)",
            "Mean Reversion (RSI + Bollinger Bands)",
            "Volatility Breakout (Donchian Channel / Turtle)",
            "Momentum (MACD + Volume Spike)",
        ],
        help="検証・改善を実施する戦略テーマのリスト",
    )
    parser.add_argument(
        "--config",
        default="configs/pipeline_config.yaml",
        help="パイプライン設定ファイルパス",
    )
    parser.add_argument(
        "--data",
        default=None,
        help="バックテスト用CSVデータパス (指定した場合は最優先)",
    )
    parser.add_argument(
        "--source",
        choices=["binance", "bitflyer", "gmo", "synthetic"],
        default=None,
        help="取引所APIデータソース ('binance', 'bitflyer', 'gmo', 'synthetic')",
    )
    parser.add_argument(
        "--symbol",
        default="BTCUSDT",
        help="対象銘柄シンボル (例: 'BTCUSDT', 'BTC_JPY')",
    )
    parser.add_argument(
        "--timeframe",
        default="1h",
        help="時間足 (例: '1m', '5m', '15m', '1h', '1d')",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="キャッシュを破棄して最新相場データを再取得",
    )
    parser.add_argument(
        "--commission",
        type=float,
        default=None,
        help="バックテスト片道手数料率 (例: 0.0 でMaker無料, 0.0005 で0.05%)",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=None,
        help="バックテストスリッページ率 (例: 0.0 でスリッページなし)",
    )

    args = parser.parse_args()

    print("=" * 65)
    print("      Autonomous Strategy Discovery & Optimization Pipeline      ")
    print("    [Proposer] -> [Runner (Self-Healing)] -> [Optimizer] -> [Governance]")
    print("=" * 65)

    pipeline = AutonomousPipeline(
        config_path=args.config,
        commission_rate=args.commission,
        slippage_rate=args.slippage
    )
    results = await pipeline.run_parallel_pipeline(
        themes=args.themes,
        data_file=args.data,
        source=args.source,
        symbol=args.symbol,
        timeframe=args.timeframe,
        force_refresh=args.refresh
    )

    print("\n[Done] すべてのタスクが完了しました。レポートは reports/ ディレクトリをご確認ください。")


if __name__ == "__main__":
    asyncio.run(main())
