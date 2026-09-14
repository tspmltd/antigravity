import os
import sys
import glob
import argparse

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from execution.multi_paper_trader import MultiStrategyLiveTrader
from execution.tick_live_trader import TickLiveTrader
from core.tick_strategy import (
    MicroTrendTickStrategy,
    InventorySkewTickMMStrategy,
    OrderFlowScalpTickStrategy,
)


def find_strategies(custom_paths=None):
    """稼働させる戦略リストを取得"""
    if custom_paths:
        return custom_paths

    # 1. 承認済み戦略
    approved_raw = glob.glob("strategies/approved/*.py")
    approved = []
    for p in approved_raw:
        try:
            with open(p, "r", encoding="utf-8") as f:
                if "class CustomStrategy" in f.read():
                    approved.append(p)
        except Exception:
            pass

    # 2. 代表的な主要戦略（最新のoptimizingまたはproposedから補完）
    proposed_or_optimizing = (
        glob.glob("strategies/optimizing/*.py") +
        glob.glob("strategies/proposed/*.py")
    )
    # 重複しないように代表的な戦略を抽出
    selected = list(approved)
    seen_types = {"InventorySkewMM"} if approved else set()

    for path in sorted(proposed_or_optimizing, key=os.path.getmtime, reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if "class CustomStrategy" not in content:
                continue
            
            # タイプ判定
            strat_type = None
            if "MicroSpreadMM" in content or "micro_spread" in content:
                strat_type = "MicroSpreadMM"
            elif "EmaTrend" in content or "ema_fast" in content:
                strat_type = "EmaTrend"
            elif "GridMM" in content or "grid_spacing" in content:
                strat_type = "GridMM"

            if strat_type and strat_type not in seen_types:
                seen_types.add(strat_type)
                selected.append(path)
        except Exception:
            continue

    if not selected:
        raise FileNotFoundError("稼働可能な戦略ファイルが見つかりません。")

    return selected


def main():
    parser = argparse.ArgumentParser(description="bitFlyer FX 複数戦略同時並行ペーパートレード＆Discord通知ランナー")
    parser.add_argument(
        "--mode",
        choices=["tick", "candle"],
        default="tick",
        help="実行モード: tick (1分足撤廃・完全ティック駆動/即時Cancel/Refill, デフォルト) または candle (古典的ローソク足)",
    )
    parser.add_argument(
        "--symbol",
        default="FX_BTC_JPY",
        help="銘柄コード (デフォルト: FX_BTC_JPY - bitFlyer Lightning FX)",
    )
    parser.add_argument(
        "--timeframe",
        default="1m",
        help="足種 (デフォルト: 1m - candleモード時のみ使用)",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=0.001,
        help="1戦略あたりの発注ロット (BTC, デフォルト: 0.001)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="約定・価格監視インターバル秒 (デフォルト: 1.0秒)",
    )
    parser.add_argument(
        "--report-interval",
        type=float,
        default=3600.0,
        help="Discord定期レポート送信間隔秒 (デフォルト: 3600秒 = 1時間)",
    )
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=None,
        help="稼働する戦略ファイルパスのリスト (candleモード用。省略時は自動選定)",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=None,
        help="最大実行ステップ数 (テスト用)",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="【危険】本番実資金トレードモードを有効化 (自己責任で実行)",
    )

    args = parser.parse_args()

    is_real = False
    if args.real:
        print("\n" + "!" * 60)
        print("                 【警告: 本番実資金取引モード】                 ")
        print("  実資金を用いた注文が bitFlyer API に送信されます。")
        print("!" * 60)
        confirm = input("本当に本番発注を実行しますか？ (実行する場合は大文字で 'YES' と入力): ")
        if confirm.strip() != "YES":
            print("[中止] 本番モードはキャンセルされました。ペーパートレードとして起動します。")
            is_real = False
        else:
            is_real = True

    if args.mode == "tick":
        print(f"\n[起動] 完全ティック駆動（Tick-Driven / Order Flow Event-Driven）モードで起動します。")
        print(f"       1分足を完全撤廃し、直近15秒約定ストリーム・2bp初動・Taker差分・即時Cancel/Refillで高速稼働します。")
        tick_trader = TickLiveTrader(
            strategies=[
                MicroTrendTickStrategy(initial_momentum_bp=1.2, min_delta_ratio=0.35, micro_tp_bp=3.5, target_trend_bp=12.0),
                InventorySkewTickMMStrategy(half_spread_bp=1.2, micro_tp_bp=2.5, stop_loss_bp=5.0),
                OrderFlowScalpTickStrategy(imbalance_threshold=0.40, target_tp_bp=2.0, stop_loss_bp=3.5),
            ],
            product_code=args.symbol,
            order_size_btc=args.size,
            poll_interval_sec=args.interval,
            report_interval_sec=args.report_interval,
            enable_real_trading=is_real,
        )
        tick_trader.start(max_steps=args.steps)
    else:
        strat_paths = find_strategies(args.strategies)
        trader = MultiStrategyLiveTrader(
            strategy_paths=strat_paths,
            product_code=args.symbol,
            timeframe=args.timeframe,
            order_size_btc=args.size,
            poll_interval_sec=args.interval,
            report_interval_sec=args.report_interval,
            enable_real_trading=is_real,
        )
        trader.start(max_steps=args.steps)


if __name__ == "__main__":
    main()
