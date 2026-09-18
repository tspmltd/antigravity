import os
import sys
import glob
import argparse

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from execution.paper_trader import LivePaperTrader


def find_default_strategy() -> str:
    """有効なCustomStrategyを含む戦略ファイルを最新順に探索"""
    candidates = (
        glob.glob("strategies/approved/*.py") +
        glob.glob("strategies/optimizing/*.py") +
        glob.glob("strategies/proposed/*.py")
    )
    # 更新日時が新しい順にソート
    candidates = sorted(candidates, key=os.path.getmtime, reverse=True)

    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if "class CustomStrategy" in content and "BaseStrategy" in content:
                return path
        except Exception:
            continue

    raise FileNotFoundError("有効な戦略ファイル (CustomStrategy) が見つかりません。")


def main():
    parser = argparse.ArgumentParser(description="bitFlyer リアルタイム自動売買・ペーパートレードランナー")
    parser.add_argument(
        "--strategy",
        default=None,
        help="実行する戦略ファイルパス (省略時は approved から自動選択)",
    )
    parser.add_argument(
        "--symbol",
        default="FX_BTC_JPY",
        help="銘柄コード (デフォルト: FX_BTC_JPY - bitFlyer Lightning FX)",
    )
    parser.add_argument(
        "--timeframe",
        default="1m",
        help="足種 (デフォルト: 1m)",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=0.001,
        help="発注ロット (BTC, デフォルト: 0.001)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="価格監視インターバル秒 (デフォルト: 5.0秒)",
    )
    parser.add_argument(
        "--report-interval",
        type=float,
        default=3600.0,
        help="Discord定期レポート送信間隔秒 (デフォルト: 3600秒 = 1時間)",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=None,
        help="最大実行ステップ数 (テスト用、省略時は継続監視)",
    )
    parser.add_argument(
        "--min-profit",
        type=float,
        default=18.0,
        help="スプレッド負け防止ガードの最低利幅目標 (円, デフォルト: 18.0円 [第1段階: +15~+20円])",
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        default=25.0,
        help="ハードストップロス緊急損切閾値 (円, デフォルト: 25.0円 [第1段階: -20~-30円])",
    )
    parser.add_argument(
        "--max-hold",
        type=float,
        default=1800.0,
        help="最大保有時間秒 (デフォルト: 1800秒 = 30分)",
    )
    parser.add_argument(
        "--daily-loss-limit",
        type=float,
        default=300.0,
        help="日次最大許容損失 (円, 到達で当日エントリー遮断 & 緊急全決済, デフォルト: 300.0円)",
    )
    parser.add_argument(
        "--max-consecutive-losses",
        type=int,
        default=4,
        help="最大連続損失回数 (連敗到達で当日エントリー遮断 & 緊急全決済, デフォルト: 4回)",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="【危険】本番実資金トレードモードを有効化 (自己責任で実行)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="本番確認プロンプトをスキップして即時起動",
    )

    args = parser.parse_args()

    # 戦略ファイルの特定
    strat_path = args.strategy or find_default_strategy()

    # 本番トレード二重確認ガード
    is_real = False
    if args.real:
        if not args.yes:
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
        else:
            print("\n" + "!" * 60)
            print("     🚨 【本番実資金取引モード (Non-Interactive: --yes)】 🚨     ")
            print("  実資金を用いた注文が bitFlyer API に送信されます。")
            print("!" * 60 + "\n")
            is_real = True

    trader = LivePaperTrader(
        strategy_path=strat_path,
        product_code=args.symbol,
        timeframe=args.timeframe,
        order_size_btc=args.size,
        poll_interval_sec=args.interval,
        enable_real_trading=is_real,
        min_profit_jpy=args.min_profit,
        stop_loss_jpy=args.stop_loss,
        max_hold_sec=args.max_hold,
        daily_loss_limit_jpy=args.daily_loss_limit,
        max_consecutive_losses=args.max_consecutive_losses
    )
    trader.hourly_interval_sec = args.report_interval

    trader.start(max_steps=args.steps)


if __name__ == "__main__":
    main()
