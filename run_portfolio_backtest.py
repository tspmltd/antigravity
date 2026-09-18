#!/usr/bin/env python3
"""
run_portfolio_backtest.py: GAPCORE Multi-Strategy vs Baseline Benchmark Driver
==============================================================================
Runs rigorous head-to-head backtests comparing:
1. Baseline: EmaTrend Single-Strategy
2. Target A: Multi-Strategy without Netting (Raw Physical Orders)
3. Target B: Multi-Strategy with Internal Netting, Rate Quota & Regime Switching

Evaluates:
- Net Profit (JPY) & Return (%)
- Max Drawdown (JPY & %)
- Sharpe Ratio (Annualized)
- Physical Order Load vs 429 Skips
- Netting Events & Spread Friction Savings (JPY)
- Strategy-level PnL Contributions
"""

import os
import sys
import time
import pandas as pd
import numpy as np

# Ensure antigravity package is on path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.backtest.portfolio_backtester import PortfolioBacktester


def load_dataset(data_path: str) -> pd.DataFrame:
    if not os.path.exists(data_path):
        raise FileNotFoundError(f"Data file not found: {data_path}")
    df = pd.read_csv(data_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def print_comparison_table(results: dict):
    print("\n" + "=" * 92)
    print(" 🏆 GAPCORE バックテスト検証結果: 単一戦略 vs ポートフォリオ（内部ネッティング＆発注枠）")
    print("=" * 92)

    headers = [
        "評価指標 (KPI)",
        "① Baseline (EmaTrend単体)",
        "② Multi (ネッティングなし)",
        "③ Target (GAPCORE完全版)",
    ]

    r1 = results["baseline"]
    r2 = results["no_netting"]
    r3 = results["target"]

    rows = [
        ("初期資本金 (Capital)", f"{r1['initial_capital']:,.0f} 円", f"{r2['initial_capital']:,.0f} 円", f"{r3['initial_capital']:,.0f} 円"),
        ("最終資産 (Final Equity)", f"{r1['final_capital']:,.1f} 円", f"{r2['final_capital']:,.1f} 円", f"{r3['final_capital']:,.1f} 円"),
        ("純損益 (Net Profit)", f"{r1['net_profit_jpy']:+,.1f} 円", f"{r2['net_profit_jpy']:+,.1f} 円", f"{r3['net_profit_jpy']:+,.1f} 円"),
        ("収益率 (Return %)", f"{r1['return_pct']:+.2f} %", f"{r2['return_pct']:+.2f} %", f"{r3['return_pct']:+.2f} %"),
        ("最大ドローダウン (Max DD 円)", f"{r1['max_drawdown_jpy']:,.1f} 円", f"{r2['max_drawdown_jpy']:,.1f} 円", f"{r3['max_drawdown_jpy']:,.1f} 円"),
        ("最大ドローダウン率 (Max DD %)", f"{r1['max_drawdown_pct']:.2f} %", f"{r2['max_drawdown_pct']:.2f} %", f"{r3['max_drawdown_pct']:.2f} %"),
        ("シャープレシオ (Sharpe Ratio)", f"{r1['sharpe_ratio']:.2f}", f"{r2['sharpe_ratio']:.2f}", f"{r3['sharpe_ratio']:.2f}"),
        ("物理発注回数 (Physical Orders)", f"{r1['physical_orders_count']:,} 回", f"{r2['physical_orders_count']:,} 回", f"{r3['physical_orders_count']:,} 回"),
        ("内部ネッティング回数 (Netting)", "0 回 (非対応)", "0 回 (非対応)", f"{r3['netting_events_count']:,} 回 🚀"),
        ("スプレッド節約額 (Savings)", "0.0 円", "0.0 円", f"+{r3['spread_savings_jpy']:,.1f} 円 💰"),
        ("429レート枠スキップ数 (Skips)", f"{r1['rate_limit_skips_count']} 回", f"{r2['rate_limit_skips_count']} 回", f"{r3['rate_limit_skips_count']} 回 (防衛)"),
    ]

    col_widths = [32, 18, 18, 20]
    header_str = " | ".join(f"{h:<{w}}" for h, w in zip(headers, col_widths))
    print(header_str)
    print("-" * 92)

    for row in rows:
        row_str = " | ".join(f"{col:<{w}}" for col, w in zip(row, col_widths))
        print(row_str)

    print("=" * 92)

    # Strategy breakdown for Target
    print("\n📊 【③ Target (GAPCORE完全版) の戦略別 損益内訳】")
    for sid, data in r3["strategy_breakdown"].items():
        print(f"  • {sid:<20}: 確定損益 {data['realized_pnl']:+,.1f} 円 | 最終建玉: {data['final_position']:+.4f} BTC")

    print("\n🌐 【相場レジーム分布 (バー内サブステップ累計)】")
    for reg, count in r3["regime_counts"].items():
        ratio = count / max(1, sum(r3["regime_counts"].values())) * 100.0
        print(f"  • {reg:<10}: {count:,} ticks ({ratio:.1f}%)")

    print("\n" + "=" * 92)


def main():
    data_path = os.path.join(BASE_DIR, "data", "cache", "bitflyer_BTC_JPY_1m.csv")
    print(f"[Driver] ヒストリカルデータをロード中: {data_path}")
    df = load_dataset(data_path)
    print(f"[Driver] ロード完了: {len(df):,} 本の1分足バー ({df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]})")

    results = {}

    # 1. Baseline: EmaTrend 単体
    print("\n▶ [1/3] Baseline (EmaTrend単体) バックテスト実行中...")
    bt_base = PortfolioBacktester(
        enable_internal_netting=False,
        enable_rate_limit=True,
        enable_regime_switch=False,
        enable_daily_cap=True,
    )
    t0 = time.time()
    results["baseline"] = bt_base.run(df, mode="baseline_ema")
    print(f"   完了 ({time.time() - t0:.2f}s) | 損益: {results['baseline']['net_profit_jpy']:+,.1f} 円")

    # 2. Multi-Strategy without Netting
    print("\n▶ [2/3] Multi-Strategy (ネッティングなし・物理両建て) バックテスト実行中...")
    bt_raw = PortfolioBacktester(
        enable_internal_netting=False,
        enable_rate_limit=True,
        enable_regime_switch=False,
        enable_daily_cap=True,
    )
    t0 = time.time()
    results["no_netting"] = bt_raw.run(df, mode="portfolio_no_netting")
    print(f"   完了 ({time.time() - t0:.2f}s) | 損益: {results['no_netting']['net_profit_jpy']:+,.1f} 円")

    # 3. Target: Multi-Strategy with Internal Netting, Rate Quota, and Regime Switching
    print("\n▶ [3/3] Target (GAPCORE完全版: 内部ネッティング＋発注枠＋レジーム切替) バックテスト実行中...")
    bt_target = PortfolioBacktester(
        enable_internal_netting=True,
        enable_rate_limit=True,
        enable_regime_switch=True,
        enable_daily_cap=True,
    )
    t0 = time.time()
    results["target"] = bt_target.run(df, mode="portfolio")
    print(f"   完了 ({time.time() - t0:.2f}s) | 損益: {results['target']['net_profit_jpy']:+,.1f} 円")

    # Display comparison
    print_comparison_table(results)


if __name__ == "__main__":
    main()
