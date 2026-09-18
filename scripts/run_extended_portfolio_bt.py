"""
scripts/run_extended_portfolio_bt.py: Extended Portfolio Backtest Runner
========================================================================
Validates Step 1 of the Quant Strategy Engineering roadmap:
1. Baseline Single Strategy (EmaTrend alone)
2. Baseline Single Strategy (GridMM alone)
3. 3-Strategy Portfolio (EmaTrend + MeanReversion + OBI)
4. 4-Strategy GAPCORE Portfolio (EmaTrend + MeanReversion + OBI + Adaptive GridMM)

Evaluates:
- Net Profit (JPY)
- Profit Factor (PF)
- Sharpe Ratio
- Max Drawdown (MaxDD)
- Internal Netting Events & Spread Savings
- Regime Breakdown & Dominance Analysis
"""

import os
import sys
import pandas as pd
import numpy as np

# Ensure project root is in path
sys.path.insert(0, "/home/azureuser/antigravity")

from antigravity.backtest.portfolio_backtester import PortfolioBacktester


def run_comparison():
    data_path = "/home/azureuser/antigravity/data/cache/bitflyer_FX_BTC_JPY_1m.csv"
    if not os.path.exists(data_path):
        print(f"Error: Data file not found: {data_path}")
        return

    df = pd.read_csv(data_path)
    print(f"Loaded dataset: {len(df)} 1m bars ({df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]})")

    results = {}

    # 1. Baseline: EmaTrend Single Strategy
    bt_ema = PortfolioBacktester(
        enable_internal_netting=False,
        enable_regime_switch=False,
        order_size=0.001,
        base_spread_bp=2.5,
    )
    results["1. EmaTrend 単体 (順張り)"] = bt_ema.run(df, mode="baseline_ema")

    # 2. Baseline: GridMM Single Strategy
    bt_grid = PortfolioBacktester(
        enable_internal_netting=False,
        enable_regime_switch=False,
        order_size=0.001,
        base_spread_bp=2.5,
    )
    results["2. GridMM 単体 (逆張り)"] = bt_grid.run(df, mode="baseline_gridmm")

    # 3. 4-Strategy Portfolio WITHOUT Internal Netting
    bt_no_net = PortfolioBacktester(
        enable_internal_netting=False,
        enable_regime_switch=True,
        order_size=0.001,
        base_spread_bp=2.5,
    )
    results["3. GAPCORE 4戦略 (ネッティング無)"] = bt_no_net.run(df, mode="portfolio")

    # 4. 4-Strategy GAPCORE Portfolio WITH Internal Netting & Regime Switch
    bt_full = PortfolioBacktester(
        enable_internal_netting=True,
        enable_regime_switch=True,
        order_size=0.001,
        base_spread_bp=2.5,
    )
    results["4. GAPCORE 4戦略 (完全統合版)"] = bt_full.run(df, mode="portfolio")

    print("\n" + "=" * 95)
    print(f"{'戦略構成 / モード':<32} | {'損益 (円)':>10} | {'PF':>6} | {'Sharpe':>7} | {'MaxDD':>9} | {'Netting回数':>10} | {'摩擦削減':>10}")
    print("=" * 95)

    for name, r in results.items():
        pnl = r.get("net_profit_jpy", 0.0)
        pf = r.get("profit_factor", 0.0)
        sr = r.get("sharpe_ratio", 0.0)
        mdd = r.get("max_drawdown_jpy", 0.0)
        net_cnt = r.get("netting_events_count", 0)
        savings = r.get("spread_savings_jpy", 0.0)

        print(f"{name:<30} | {pnl:>+9.1f}円 | {pf:>6.2f} | {sr:>7.2f} | {mdd:>8.1f}円 | {net_cnt:>10d} | {savings:>9.1f}円")

    print("-" * 95)

    # Strategy breakdown for Full Portfolio
    full_r = results["4. GAPCORE 4戦略 (完全統合版)"]
    print("\n【4. GAPCORE 4戦略 完全統合版: 戦略別内訳】")
    for sname, sdata in full_r.get("strategy_breakdown", {}).items():
        print(f"  • {sname:<20}: PnL: {sdata.get('realized_pnl_jpy', 0.0):>+8.1f}円 | 約定数: {sdata.get('fill_count', 0):>4d}")

    print("\n【レジーム出現頻度】")
    for rname, cnt in full_r.get("regime_counts", {}).items():
        pct = cnt / sum(full_r["regime_counts"].values()) * 100.0
        print(f"  • {rname:<10}: {cnt:>5d} ticks ({pct:>5.1f}%)")


if __name__ == "__main__":
    run_comparison()
