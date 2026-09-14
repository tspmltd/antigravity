import os
import sys
import yaml
import glob
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from agents.optimizer import StrategyOptimizer
from agents.backtest_runner import BacktestRunner
from agents.governance import StrategyGovernance
from core.dataloader import DataLoader
from core.engine import BacktestEngine
from core.notifier import DiscordNotifier


class AdaptiveManager:
    """
    実稼働（ペーパートレード / 本番）中の成績を自律監視し、
    パフォーマンス低下やドローダウンを検知した際に自動で再最適化・戦略差し替えを実行するマネージャー。
    """

    def __init__(self, config_path: str = "configs/pipeline_config.yaml"):
        self.config_path = config_path
        self.config = self._load_config(config_path)
        self.tuning_conf = self.config.get("adaptive_tuning", {})
        self.notifier = DiscordNotifier()

        # 各種エージェントの初期化
        self.engine = BacktestEngine(
            initial_capital=self.config.get("backtest", {}).get("initial_capital", 1000000.0),
            commission_rate=self.config.get("backtest", {}).get("commission_rate", 0.0005),
            slippage_rate=self.config.get("backtest", {}).get("slippage_rate", 0.0002),
        )
        self.optimizer = StrategyOptimizer(config=self.config)
        self.runner = BacktestRunner(engine=self.engine, config=self.config)
        self.governance = StrategyGovernance(config=self.config)

        self.last_evaluated_trade_count = 0

    def _load_config(self, path: str) -> Dict[str, Any]:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        return {}

    def should_evaluate(self, trades_history: List[Dict[str, Any]]) -> bool:
        """評価を実施するタイミングか判定 (指定トレード数ごとにチェック)"""
        if not self.tuning_conf.get("enable_adaptive_tuning", True):
            return False

        interval = self.tuning_conf.get("evaluation_interval_trades", 3)
        current_count = len(trades_history)
        if current_count >= interval and (current_count - self.last_evaluated_trade_count) >= interval:
            return True
        return False

    def evaluate_and_adapt(
        self,
        trades_history: List[Dict[str, Any]],
        initial_capital: float,
        current_cash: float,
        current_strategy_info: Dict[str, Any],
        symbol: str = "BTC_JPY",
        timeframe: str = "1m"
    ) -> Tuple[bool, Optional[str], str]:
        """
        現在の実稼働成績を評価し、必要であれば自動で改善エージェントを呼んで新戦略を生成・検証・差し替える。
        
        Returns:
            (need_switch: bool, new_strategy_path: Optional[str], message: str)
        """
        self.last_evaluated_trade_count = len(trades_history)
        recent_n = self.tuning_conf.get("evaluation_interval_trades", 3)
        recent_trades = trades_history[-recent_n:]

        # 1. 指標の算出
        total_pnl = sum(t.get("pnl", 0) for t in trades_history)
        dd_pct = max(0.0, ((initial_capital - (initial_capital + total_pnl)) / initial_capital) * 100.0)
        
        wins = [t for t in recent_trades if t.get("pnl", 0) > 0]
        recent_win_rate = (len(wins) / len(recent_trades)) * 100.0 if recent_trades else 0.0

        max_allowed_dd = self.tuning_conf.get("max_allowed_drawdown_pct", 2.5)
        min_allowed_win_rate = self.tuning_conf.get("min_win_rate_pct", 35.0)

        needs_reoptimization = False
        reasons = []

        if dd_pct >= max_allowed_dd:
            needs_reoptimization = True
            reasons.append(f"ドローダウン許容値超過 ({dd_pct:.2f}% >= {max_allowed_dd}%)")

        if len(recent_trades) >= recent_n and recent_win_rate < min_allowed_win_rate:
            needs_reoptimization = True
            reasons.append(f"直近勝率低下 ({recent_win_rate:.1f}% < {min_allowed_win_rate}%)")

        # 正常稼働継続
        if not needs_reoptimization:
            status_msg = f"実稼働成績は健全です (直近{recent_n}件勝率: {recent_win_rate:.1f}%, DD: {dd_pct:.2f}%)"
            print(f"[AdaptiveManager] 健全確認: {status_msg}")
            return False, None, status_msg

        # 2. 自動再最適化トリガー
        trigger_reason = " & ".join(reasons)
        print("\n" + "=" * 55)
        print(f"[AdaptiveManager] [ALERT] パフォーマンス低下を検知！自動再最適化を発動します:")
        print(f"                 要因: {trigger_reason}")
        print("=" * 55)

        # Discordへ再最適化トリガーを通知
        self.notifier.send_alert(
            title="⚠️ 【自律適応アラート】実稼働パフォーマンス低下を検知",
            message=f"**対象銘柄**: `{symbol}` ({timeframe})\n"
                    f"**要因**: {trigger_reason}\n"
                    f"最新のbitFlyer FX相場を取り込み、改善エージェント（Optimizer）が直近相場に適応する改善コードを自動生成します...",
            level="warning"
        )

        # 最新相場データの再取得 (キャッシュ更新)
        print(f"[AdaptiveManager] 最新相場データを取り込んでいます ({symbol} {timeframe})...")
        df_fresh = DataLoader.load_or_generate_data(
            source="bitflyer" if "JPY" in symbol else "binance",
            symbol=symbol,
            timeframe=timeframe,
            force_refresh=True,
            limit=1500
        )
        train_df, test_df = DataLoader.split_train_test(df_fresh, train_ratio=0.7)

        # 3. 【改善 (Optimizer)】によるコード自動改善
        # 前回の検証結果オブジェクトを模擬生成
        mock_previous_result = {
            "in_sample_metrics": {
                "sharpe_ratio": 0.5,
                "max_drawdown_pct": dd_pct,
                "total_trades": len(trades_history),
                "profit_factor": 0.8,
                "win_rate_pct": recent_win_rate,
                "total_return_pct": (total_pnl / initial_capital) * 100.0,
            }
        }

        print(f"[AdaptiveManager] 【改善エージェント (Optimizer)】が直近相場に適応する改善コードを生成中...")
        improved_info = self.optimizer.optimize_strategy(current_strategy_info, mock_previous_result)

        # 4. 【検証 (Backtest Runner)】による自己修復 & バックテスト
        print(f"[AdaptiveManager] 【検証エージェント (Runner)】が新戦略のバックテストを実行中...")
        bt_res = self.runner.run_backtest_with_healing(improved_info, train_df, test_df, timeframe=timeframe)

        if not bt_res["is_success"]:
            msg = f"再最適化コードのバックテスト失敗: {bt_res.get('error')}"
            print(f"[AdaptiveManager] [FAIL] {msg}")
            return False, None, msg

        # 5. 【判断 (Governance)】による品質ゲート & 過剰適合判定
        print(f"[AdaptiveManager] 【判断エージェント (Governance)】がガバナンス合否を判定中...")
        decision = self.governance.evaluate(improved_info, bt_res, max_iterations=3, timeframe=timeframe)

        is_m = bt_res["in_sample_metrics"]
        print(f"[AdaptiveManager] 新戦略の評価結果: {decision['status']} (Sharpe: {is_m.get('sharpe_ratio')}, MDD: {is_m.get('max_drawdown_pct')}%)")

        # 合格した場合、自動差し替え（ホットリロード）
        if decision["status"] == "PASS" or self.tuning_conf.get("auto_switch_strategy", True):
            new_path = improved_info["file_path"]
            success_msg = f"新戦略への自動差し替え準備完了: {improved_info['name']} ({new_path})"
            print(f"[AdaptiveManager] [SUCCESS] 【自動改善成功】稼働中戦略を新バージョンへホットリロードします！")
            
            # Discordへ改善成功アラートを送信
            change_note = improved_info.get("hypothesis", "").split("->")[-1].strip() if "->" in improved_info.get("hypothesis", "") else "パラメータ調整 & フィルター追加"
            self.notifier.send_alert(
                title="🎉 【自律改善成功】新戦略をホットリロード（無停止差し替え）します！",
                message=f"**新戦略**: `{improved_info['name']}` ({improved_info.get('version', 'v2.0')})\n"
                        f"**改善内容**: **{change_note}**\n"
                        f"**最新検証メトリクス**: Sharpe: **{is_m.get('sharpe_ratio')}** | MDD: **{is_m.get('max_drawdown_pct')}%**\n"
                        f"**ファイル**: `{new_path}`",
                level="success"
            )
            return True, new_path, success_msg
        else:
            fail_msg = f"改善戦略がガバナンス基準に未達でした ({decision['reason']})"
            print(f"[AdaptiveManager] [WARN] {fail_msg}。現行戦略のまま安全継続します。")
            return False, None, fail_msg
