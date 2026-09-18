"""
scripts/run_multi_period_bt.py: Multi-Period Cross-Regime Portfolio Stress Tester
================================================================================
Stress tests GAPCORE 4-Strategy Portfolio across diverse market regimes:
1. 2026-07-10 (Calm Range / Summer Regime)
2. 2026-08-03 (High-Frequency Microstructure / Flow Shock)
3. 2026-09-15~17 (Extreme Trend / Flash Whipsaw Regime)
"""

import os
import sys
import pandas as pd
import numpy as np

sys.path.insert(0, "/home/azureuser/antigravity")

from antigravity.backtest.portfolio_backtester import PortfolioBacktester


def test_dataset(name: str, df: pd.DataFrame, spread_bp: float = 2.5):
    print("\n" + "=" * 95)
    print(f"  📂 データセット: {name} ({len(df)} bars)")
    print("=" * 95)

    # 1. Baseline: EmaTrend alone
    bt_ema = PortfolioBacktester(
        enable_internal_netting=False,
        enable_regime_switch=False,
        order_size=0.001,
        base_spread_bp=spread_bp,
    )
    r_ema = bt_ema.run(df, mode="baseline_ema")

    # 2. Baseline: GridMM alone
    bt_grid = PortfolioBacktester(
        enable_internal_netting=False,
        enable_regime_switch=False,
        order_size=0.001,
        base_spread_bp=spread_bp,
    )
    r_grid = bt_grid.run(df, mode="baseline_gridmm")

    # 3. 4-Strategy Portfolio WITHOUT Netting
    bt_no_net = PortfolioBacktester(
        enable_internal_netting=False,
        enable_regime_switch=True,
        order_size=0.001,
        base_spread_bp=spread_bp,
    )
    r_no_net = bt_no_net.run(df, mode="portfolio")

    # 4. GAPCORE 4-Strategy Portfolio WITH Netting & Regime Switch
    bt_full = PortfolioBacktester(
        enable_internal_netting=True,
        enable_regime_switch=True,
        order_size=0.001,
        base_spread_bp=spread_bp,
    )
    r_full = bt_full.run(df, mode="portfolio")

    modes = [
        ("EmaTrend 単体 (順張り)", r_ema),
        ("GridMM 単体 (逆張り)", r_grid),
        ("GAPCORE 4戦略 (ネッティング無)", r_no_net),
        ("GAPCORE 4戦略 (完全統合版)", r_full),
    ]

    print(f"{'戦略構成 / モード':<30} | {'損益 (円)':>10} | {'PF':>6} | {'Sharpe':>7} | {'MaxDD':>9} | {'Netting':>8} | {'摩擦削減':>9}")
    print("-" * 95)
    for mname, res in modes:
        pnl = res.get("net_profit_jpy", 0.0)
        pf = res.get("profit_factor", 0.0)
        sr = res.get("sharpe_ratio", 0.0)
        mdd = res.get("max_drawdown_jpy", 0.0)
        net_cnt = res.get("netting_events_count", 0)
        savings = res.get("spread_savings_jpy", 0.0)
        print(f"{mname:<28} | {pnl:>+9.1f}円 | {pf:>6.2f} | {sr:>7.2f} | {mdd:>8.1f}円 | {net_cnt:>8d} | {savings:>8.1f}円")

    # Regime stats for full
    print("\n  [レジーム比率]: " + ", ".join(f"{k}: {v} ticks ({v/sum(r_full['regime_counts'].values())*100:.1f}%)" for k, v in r_full.get("regime_counts", {}).items()))


def main():
    # 1. 9月データ (最新・高ボラトレンド)
    sep_path = "/home/azureuser/antigravity/data/cache/bitflyer_FX_BTC_JPY_1m.csv"
    if os.path.exists(sep_path):
        df_sep = pd.read_csv(sep_path)
        test_dataset("2026年9月 (直近高ボラ急変・トレンド相場)", df_sep, spread_bp=2.5)

    # 2. 7月データ (5分足レンジ相場)
    jul_path = "/home/azureuser/gapcore-platform/logs/signalverify/bars_5m/FX_BTC_JPY.20260710.csv"
    if os.path.exists(jul_path):
        df_jul = pd.read_csv(jul_path)
        # 必要なカラムをマッピング
        df_jul_std = pd.DataFrame({
            "timestamp": df_jul["bar_start_utc"],
            "open": df_jul["open"],
            "high": df_jul["high"],
            "low": df_jul["low"],
            "close": df_jul["close"],
            "volume": df_jul["volume"],
        })
        test_dataset("2026年7月 (ボックスレンジ相場)", df_jul_std, spread_bp=2.0)

    # 3. 8月データ (10秒足高頻度マイクロストラクチャー)
    aug_path = "/home/azureuser/gapcore-platform/logs/signalverify/bars_5m/FX_BTC_JPY.20260803.w1_10s.pressure.csv"
    if os.path.exists(aug_path):
        df_aug = pd.read_csv(aug_path)
        df_aug_std = pd.DataFrame({
            "timestamp": df_aug["bar_start_utc"],
            "open": df_aug["open"],
            "high": df_aug["high"],
            "low": df_aug["low"],
            "close": df_aug["close"],
            "volume": df_aug["volume"],
        })
        test_dataset("2026年8月 (高頻度マイクロストラクチャー・板圧力相場)", df_aug_std, spread_bp=1.8)


if __name__ == "__main__":
    main()
