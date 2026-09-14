import os
import sys
import time
import asyncio
import argparse
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from pipeline.orchestrator import AutonomousPipeline
from core.notifier import DiscordNotifier
from antigravity.risk_guard.git_sync import GitAutoSync



CANDIDATE_THEMES = [
    "Market Making (Inventory Skew & Micro Spread)",
    "Micro Trend Following (Order Flow Delta & Low Reverse Flow)",
    "Spread Capture MM (Cost-Aware Dynamic Spread)",
    "Grid Market Making (Bollinger Bandwidth Range)",
    "Trend Following (EMA Cross + 100SMA Filter)",
    "Volatility Breakout (Donchian Channel Turtle)",
    "Mean Reversion (RSI + Bollinger Oversold/Overbought)",
    "Momentum Spike (MACD + Volume Acceleration)",
]


async def discovery_loop(
    symbol: str = "FX_BTC_JPY",
    timeframe: str = "1m",
    interval_sec: float = 3600.0,
    config_path: str = "configs/pipeline_config.yaml"
):
    """
    バックグラウンドで定期的に自律探索パイプラインを回し、
    合格した新戦略を自動で生成・配置する常駐自律探索デーモン。
    """
    notifier = DiscordNotifier()
    pipeline = AutonomousPipeline(config_path=config_path, commission_rate=0.0, slippage_rate=0.0)
    git_sync = GitAutoSync(notifier=notifier)

    print("\n" + "=" * 70)
    print("      GapcorePJ 常駐型自律戦略探索デーモン (Autonomous Discovery Daemon)      ")
    print("=" * 70)
    print(f"探索市場    : {symbol} (bitFlyer Lightning FX)")
    print(f"基準時間足  : {timeframe}")
    print(f"探索間隔    : {interval_sec / 3600:.1f} 時間ごと")
    print(f"候補テーマ数: {len(CANDIDATE_THEMES)} 種類")
    print("-" * 70)

    notifier.send_message(
        f"🤖 **【常駐自律探索デーモン 起動】**\n"
        f"探索市場: `{symbol}` ({timeframe})\n"
        f"探索間隔: **{interval_sec / 3600:.1f} 時間ごと**\n"
        f"AIが定期的に最新相場で新戦略を自動開発・検証し、合格した戦略をポートフォリオへ自動追加します。",
        target="system"
    )

    cycle = 0
    theme_idx = 0

    while True:
        cycle += 1
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔄 [Cycle {cycle}] 新規戦略の自律探索サイクルを開始します...")

        # 探索するテーマをローテーション選択 (毎回2〜3テーマを探索)
        selected_themes = [
            CANDIDATE_THEMES[(theme_idx + i) % len(CANDIDATE_THEMES)]
            for i in range(3)
        ]
        theme_idx = (theme_idx + 3) % len(CANDIDATE_THEMES)

        print(f"[DiscoveryDaemon] 今回探索するテーマ:")
        for t in selected_themes:
            print(f"  • {t}")

        try:
            results = await pipeline.run_parallel_pipeline(
                themes=selected_themes,
                source="bitflyer",
                symbol=symbol,
                timeframe=timeframe,
                force_refresh=True
            )

            approved = [r for r in results if r.get("final_status") == "APPROVED"]
            if approved:
                print(f"\n[DiscoveryDaemon] 🏆 【新戦略発見！】{len(approved)} 件の戦略がガバナンスを突破しました！")
                print(f"                 稼働中のポートフォリオランナーへ自動追加されます。")
                notifier.send_alert(
                    title=f"🏆 【探索完了・新戦略採択】Cycle {cycle} で新戦略が合格しました！",
                    message=f"**探索市場**: `{symbol}` ({timeframe})\n"
                            f"**採択戦略数**: **{len(approved)} 件**\n"
                            f"稼働中のポートフォリオへ無停止で自動追加（Hot-Add）されます。",
                    level="success",
                    target="system"
                )

                # 未コミットの合格戦略を直ちにGitHubへ自動プッシュ
                try:
                    strat_names = ", ".join([r.get("name", "NewStrategy") for r in approved])
                    git_sync.sync_approved_strategies(strategy_name=strat_names)
                except Exception as ex:
                    print(f"[DiscoveryDaemon] GitAutoSync 例外 (スキップ): {ex}", flush=True)

            else:

                print(f"\n[DiscoveryDaemon] 今回のサイクルでは新規承認戦略はありませんでした (安全ブロック)。")
                notifier.send_alert(
                    title=f"🔍 【探索サイクル完了】Cycle {cycle} 探索結果",
                    message=f"**探索市場**: `{symbol}` ({timeframe})\n"
                            f"今回検証した3テーマは、未知データ過剰適合ガード等により安全にブロック（アーカイブ）されました。\n"
                            f"次回探索サイクル: **{interval_sec / 3600:.1f} 時間後** に最新相場で再探索します。",
                    level="info",
                    target="system"
                )

        except Exception as e:
            print(f"[DiscoveryDaemon] ⚠️ 探索サイクル中にエラー発生: {e}")

        print(f"\n[DiscoveryDaemon] 💤 次回の探索サイクルまで {interval_sec / 3600:.1f} 時間待機します...")
        await asyncio.sleep(interval_sec)


def main():
    parser = argparse.ArgumentParser(description="常駐型 自律戦略探索デーモン")
    parser.add_argument("--symbol", default="FX_BTC_JPY", help="探索銘柄 (デフォルト: FX_BTC_JPY)")
    parser.add_argument("--timeframe", default="1m", help="時間足 (デフォルト: 1m - 高頻度・高サンプル)")
    parser.add_argument("--interval", type=float, default=3600.0, help="探索間隔秒 (デフォルト: 3600秒 = 1時間)")
    parser.add_argument("--config", default="configs/pipeline_config.yaml", help="設定ファイル")

    args = parser.parse_args()

    asyncio.run(discovery_loop(
        symbol=args.symbol,
        timeframe=args.timeframe,
        interval_sec=args.interval,
        config_path=args.config
    ))


if __name__ == "__main__":
    main()
