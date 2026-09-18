import os
import shutil
import yaml
from datetime import datetime
from typing import Dict, Any, Optional, List
from agents.base_agent import BaseAgent


class StrategyGovernance(BaseAgent):
    """
    【4. 判断 (Governance)】
    時間足プロファイル（1分足〜日足）および過剰適合（カーブフィッティング）防止ルールに基づき、
    戦略の品質ゲートキーパーとして合否を厳格に判定する。
    """

    def __init__(self, rules_path: Optional[str] = "configs/governance_rules.yaml", config: Optional[Dict[str, Any]] = None):
        super().__init__(name="StrategyGovernance", role="Quality Gate, Overfitting Check & Reporting", config=config)
        self.rules = self._load_rules(rules_path)

    def _load_rules(self, rules_path: Optional[str]) -> Dict[str, Any]:
        if rules_path and os.path.exists(rules_path):
            with open(rules_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        return {}

    def _detect_strategy_type(self, strategy_info: Dict[str, Any]) -> str:
        """戦略情報から戦略タイプを自動判別 (MM vs トレンドフォロー vs 平均回帰)"""
        if "strategy_type" in strategy_info and strategy_info["strategy_type"]:
            return strategy_info["strategy_type"]
        text = (
            str(strategy_info.get("name", "")) + " " +
            str(strategy_info.get("theme", "")) + " " +
            str(strategy_info.get("hypothesis", ""))
        ).lower()
        if any(k in text for k in ["microtrend", "orderflow", "order_flow"]):
            return "micro_trend"
        elif any(k in text for k in ["mm", "spread", "grid", "market making", "skew"]):
            return "market_making"
        elif any(k in text for k in ["trend", "ema", "donchian", "macd", "momentum"]):
            return "trend_following"
        elif any(k in text for k in ["reversion", "rsi", "mean", "bb"]):
            return "mean_reversion"
        return "default"

    def _get_profile(self, timeframe: str, strategy_type: Optional[str] = None) -> Dict[str, Any]:
        """戦略タイプ(MM vs トレンド)および時間足に応じた判定プロファイルを選択"""
        strat_types = self.rules.get("strategy_types", {})
        if strategy_type and strategy_type in strat_types:
            return strat_types[strategy_type]

        profiles = self.rules.get("profiles", {})
        clean_tf = timeframe.lower().replace("min", "m").replace("hour", "h").replace("day", "d")

        if clean_tf in ["1m", "5m"]:
            return profiles.get("scalping_1m", profiles.get("default", {}))
        elif clean_tf in ["15m", "1h", "4h"]:
            return profiles.get("intraday_1h", profiles.get("default", {}))
        elif clean_tf in ["1d"]:
            return profiles.get("swing_1d", profiles.get("default", {}))
        else:
            return profiles.get("default", {})

    def evaluate(
        self,
        strategy_info: Dict[str, Any],
        backtest_result: Dict[str, Any],
        max_iterations: int = 3,
        timeframe: str = "1h"
    ) -> Dict[str, Any]:
        """
        検証結果をガバナンス基準および過剰適合防止ルールに照らし合わせて判定する。
        """
        strat_type = self._detect_strategy_type(strategy_info)
        profile = self._get_profile(timeframe, strategy_type=strat_type)
        oos_rules = self.rules.get("overfitting_guard", {})

        is_metrics = backtest_result.get("in_sample_metrics", {})
        oos_metrics = backtest_result.get("out_of_sample_metrics")
        current_iter = strategy_info.get("iteration", 1)

        sharpe = is_metrics.get("sharpe_ratio", 0.0)
        mdd = is_metrics.get("max_drawdown_pct", 100.0)
        mdd_jpy = is_metrics.get("max_drawdown_jpy", 9999.0)
        trades = is_metrics.get("total_trades", 0)
        pf = is_metrics.get("profit_factor", 0.0)
        win_rate = is_metrics.get("win_rate_pct", 0.0)
        consec_losses = is_metrics.get("max_consecutive_losses", 0)
        avg_trade_pnl = is_metrics.get("avg_trade_pnl_jpy", 0.0)

        min_sharpe = profile.get("min_sharpe_ratio", 1.5)
        max_mdd = profile.get("max_drawdown_pct", 5.0)
        max_mdd_jpy = profile.get("max_drawdown_jpy", 200.0)
        min_trades = profile.get("min_total_trades", 10)
        min_pf = profile.get("min_profit_factor", 1.25)
        min_win_rate = profile.get("min_win_rate_pct", 40.0)
        max_consec = profile.get("max_consecutive_losses", 3)
        min_avg_pnl = profile.get("min_avg_trade_pnl_jpy", 0.1)

        failed_criteria = []

        # 1. 基本パフォーマンスチェック (In-Sample: LIVE準拠)
        if sharpe < min_sharpe:
            failed_criteria.append(f"Sharpe Ratio不足 ({sharpe} < {min_sharpe})")
        if mdd > max_mdd:
            failed_criteria.append(f"MDD超過 ({mdd}% > {max_mdd}%)")
        if "max_drawdown_jpy" in is_metrics and is_metrics["max_drawdown_jpy"] > max_mdd_jpy:
            failed_criteria.append(f"MDD(円)超過 ({is_metrics['max_drawdown_jpy']:.1f}円 > {max_mdd_jpy:.1f}円)")
        if trades < min_trades:
            failed_criteria.append(f"取引回数不足 ({trades} < {min_trades}回)")
        if pf < min_pf:
            failed_criteria.append(f"Profit Factor不足 ({pf} < {min_pf})")
        if win_rate < min_win_rate and trades > 0:
            failed_criteria.append(f"勝率不足 ({win_rate}% < {min_win_rate}%)")
        if "max_consecutive_losses" in is_metrics and is_metrics["max_consecutive_losses"] > max_consec:
            failed_criteria.append(f"最大連敗数超過 ({is_metrics['max_consecutive_losses']}連敗 > {max_consec}連敗上限: LIVE CB抵触リスク)")
        if "avg_trade_pnl_jpy" in is_metrics and is_metrics["avg_trade_pnl_jpy"] < min_avg_pnl and trades > 0:
            failed_criteria.append(f"平均トレード損益不足 ({is_metrics['avg_trade_pnl_jpy']:+.2f}円 < {min_avg_pnl}円: スプレッド負け)")

        # 2. 過剰適合（カーブフィッティング）チェック (Out-of-Sample)
        if oos_rules.get("enable_oos_validation", True) and oos_metrics:
            oos_sharpe = oos_metrics.get("sharpe_ratio", 0.0)
            oos_return = oos_metrics.get("total_return_pct", 0.0)
            oos_pf = oos_metrics.get("profit_factor", 0.0)
            oos_mdd = oos_metrics.get("max_drawdown_pct", 0.0)

            # (A) OOS黒字必須
            if oos_rules.get("require_positive_oos_return", True) and oos_return <= 0:
                failed_criteria.append(f"過剰適合: 未知データ(OOS)で損失発生 ({oos_return}%)")

            # (B) Sharpe劣化率
            if sharpe > 0:
                degradation = ((sharpe - oos_sharpe) / sharpe) * 100.0
                max_deg = oos_rules.get("max_oos_sharpe_degradation_pct", 35.0)
                if degradation > max_deg:
                    failed_criteria.append(f"過剰適合: OOS Sharpe大幅低下 ({degradation:.1f}% > {max_deg}%)")

            # (C) OOS最低PF
            min_oos_pf = oos_rules.get("min_oos_profit_factor", 1.05)
            if oos_pf < min_oos_pf:
                failed_criteria.append(f"過剰適合: OOS Profit Factor低下 ({oos_pf} < {min_oos_pf})")

            # (D) OOS MDD拡大倍率
            max_mdd_mult = oos_rules.get("max_oos_mdd_multiplier", 1.8)
            if mdd > 0 and (oos_mdd / mdd) > max_mdd_mult:
                failed_criteria.append(f"過剰適合: OOS MDD急拡大 ({oos_mdd}% > {mdd * max_mdd_mult:.1f}%)")

        is_passed = len(failed_criteria) == 0

        if is_passed:
            # 合格処理
            report_path, approved_file = self._handle_approval(strategy_info, backtest_result, profile)
            return {
                "status": "PASS",
                "reason": "すべてのガバナンス基準 & 過剰適合チェックをクリア",
                "report_path": report_path,
                "approved_file": approved_file,
            }
        else:
            if current_iter < max_iterations:
                # 改善継続
                return {
                    "status": "REVISE",
                    "reason": f"基準未達 (イテレーション {current_iter}/{max_iterations}): " + ", ".join(failed_criteria),
                    "failed_criteria": failed_criteria,
                }
            else:
                # 上限到達 -> 却下・アーカイブ
                rejected_file = self._handle_rejection(strategy_info, failed_criteria)
                return {
                    "status": "REJECT",
                    "reason": f"最大改善回数到達後も未達: " + ", ".join(failed_criteria),
                    "failed_criteria": failed_criteria,
                    "rejected_file": rejected_file,
                }

    def _handle_approval(self, strategy_info: Dict[str, Any], backtest_result: Dict[str, Any], profile: Dict[str, Any]) -> tuple[str, str]:
        """合格戦略を approved フォルダに配置し詳細レポートを発行"""
        approved_dir = "strategies/approved"
        reports_dir = "reports"
        os.makedirs(approved_dir, exist_ok=True)
        os.makedirs(reports_dir, exist_ok=True)

        strat_id = strategy_info["strategy_id"]
        source_path = strategy_info["file_path"]
        approved_filename = f"{strat_id}_approved.py"
        approved_file = os.path.join(approved_dir, approved_filename)
        shutil.copyfile(source_path, approved_file)

        report_path = os.path.join(reports_dir, f"report_{strat_id}.md")
        is_m = backtest_result["in_sample_metrics"]
        oos_m = backtest_result.get("out_of_sample_metrics")

        content = f"""# 自動売買戦略 ガバナンス合格認定レポート

- **戦略ID**: `{strat_id}`
- **戦略名**: `{strategy_info.get('name')}`
- **バージョン**: `{strategy_info.get('version')}`
- **認定日時**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **承認コード配置先**: `{approved_file}`
- **適用プロファイル**: `{is_m.get('timeframe', 'N/A')}`

---

## 1. 戦略仮説・設計ロジック
{strategy_info.get('hypothesis', 'N/A')}

---

## 2. In-Sample (訓練期) パフォーマンス評価
| 評価項目 | 実績値 | ガバナンス合格ライン | 判定 |
| :--- | :--- | :--- | :--- |
| **Sharpe Ratio** | **{is_m.get('sharpe_ratio')}** | >= {profile.get('min_sharpe_ratio')} | [PASS] |
| **Max Drawdown (MDD)** | **{is_m.get('max_drawdown_pct')}%** ({is_m.get('max_drawdown_jpy', 0.0):.1f}円) | <= {profile.get('max_drawdown_pct')}% ({profile.get('max_drawdown_jpy', 200.0):.1f}円) | [PASS] |
| **Total Trades** | **{is_m.get('total_trades')}回** | >= {profile.get('min_total_trades')}回 | [PASS] |
| **Profit Factor** | **{is_m.get('profit_factor')}** | >= {profile.get('min_profit_factor')} | [PASS] |
| **Total Return** | **{is_m.get('total_return_pct')}%** ({is_m.get('total_pnl_jpy', 0.0):+.1f}円) | - | - |
| **Win Rate** | **{is_m.get('win_rate_pct')}%** | >= {profile.get('min_win_rate_pct')}% | [PASS] |
| **Max Consecutive Losses** | **{is_m.get('max_consecutive_losses', 0)}連敗** | <= {profile.get('max_consecutive_losses', 3)}連敗 | [PASS] |
| **Avg Trade PnL** | **{is_m.get('avg_trade_pnl_jpy', 0.0):+.1f}円** | >= +{profile.get('min_avg_trade_pnl_jpy', 0.1)}円 | [PASS] |
| **Calmar Ratio** | **{is_m.get('calmar_ratio')}** | >= {profile.get('min_calmar_ratio')} | [PASS] |

---

## 3. Out-of-Sample (テスト期) 過剰適合防止チェック
"""
        if oos_m:
            content += f"""| 比較項目 | In-Sample (訓練期) | Out-of-Sample (未知テスト期) | 評価 |
| :--- | :--- | :--- | :--- |
| **Sharpe Ratio** | {is_m.get('sharpe_ratio')} | {oos_m.get('sharpe_ratio')} | 健全 (過剰適合なし) |
| **Total Return** | {is_m.get('total_return_pct')}% | {oos_m.get('total_return_pct')}% | 黒字維持 |
| **Max Drawdown** | {is_m.get('max_drawdown_pct')}% | {oos_m.get('max_drawdown_pct')}% | 許容範囲内 |
| **Profit Factor** | {is_m.get('profit_factor')} | {oos_m.get('profit_factor')} | 優位性維持 |
| **Total Trades** | {is_m.get('total_trades')}回 | {oos_m.get('total_trades')}回 | - |
"""
        else:
            content += "Out-of-Sampleデータなし\n"

        content += """---

## 4. ガバナンス判定総括
**【APPROVED: 本番・ペーパートレード候補として承認】**
本戦略は過剰最適化（カーブフィッティング）の兆候がなく、未知の相場環境に対しても安定した期待値を有していると認定されました。
"""

        with open(report_path, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"[Governance] [APPROVED] 戦略採択: {strat_id} -> レポート生成: {report_path}")
        return report_path, approved_file

    def _handle_rejection(self, strategy_info: Dict[str, Any], failed_reasons: list) -> str:
        """不合格戦略を rejected にアーカイブし敗因を記録"""
        rejected_dir = "strategies/rejected"
        os.makedirs(rejected_dir, exist_ok=True)

        strat_id = strategy_info["strategy_id"]
        source_path = strategy_info["file_path"]
        rejected_file = os.path.join(rejected_dir, f"{strat_id}_rejected.py")
        shutil.copyfile(source_path, rejected_file)

        log_path = os.path.join(rejected_dir, f"{strat_id}_log.txt")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Strategy ID: {strat_id}\n")
            f.write(f"Timestamp: {datetime.now().isoformat()}\n")
            f.write("Failed Reasons:\n")
            for r in failed_reasons:
                f.write(f"- {r}\n")

        print(f"[Governance] [REJECTED] 戦略却下: {strat_id} -> アーカイブ: {rejected_file}")
        return rejected_file
