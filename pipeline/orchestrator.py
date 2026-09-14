import os
import asyncio
import yaml
from typing import List, Dict, Any, Optional
import pandas as pd

from agents.strategy_proposer import StrategyProposer
from agents.backtest_runner import BacktestRunner
from agents.optimizer import StrategyOptimizer
from agents.governance import StrategyGovernance
from core.dataloader import DataLoader
from core.engine import BacktestEngine
from core.notifier import DiscordNotifier
from pipeline.state_manager import PipelineStateManager, StrategyStatus


class AutonomousPipeline:
    """
    自動売買システムの自律改善パイプライン・オーケストレーター。
    4つのサブエージェントを非同期イベントループで並行稼働・協調制御します。
    """

    def __init__(
        self,
        config_path: str = "configs/pipeline_config.yaml",
        commission_rate: Optional[float] = None,
        slippage_rate: Optional[float] = None
    ):
        self.config = self._load_config(config_path)
        self.max_parallel = self.config.get("pipeline", {}).get("max_parallel_tasks", 2)
        self.max_iterations = self.config.get("pipeline", {}).get("max_optimization_iterations", 3)
        self.notifier = DiscordNotifier()

        # バックテストエンジン初期化
        bt_conf = self.config.get("backtest", {})
        comm = commission_rate if commission_rate is not None else bt_conf.get("commission_rate", 0.0005)
        slip = slippage_rate if slippage_rate is not None else bt_conf.get("slippage_rate", 0.0002)

        self.engine = BacktestEngine(
            initial_capital=bt_conf.get("initial_capital", 1000000.0),
            commission_rate=comm,
            slippage_rate=slip,
        )

        # 4つのエージェントのインスタンス化
        self.proposer = StrategyProposer(config=self.config)
        self.runner = BacktestRunner(engine=self.engine, config=self.config)
        self.optimizer = StrategyOptimizer(config=self.config)
        self.governance = StrategyGovernance(config=self.config)

        # 状態追跡マネージャ
        self.state_manager = PipelineStateManager()

    def _load_config(self, path: str) -> Dict[str, Any]:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        return {}

    async def run_single_theme_lifecycle(
        self,
        theme: str,
        train_df: pd.DataFrame,
        test_df: Optional[pd.DataFrame] = None,
        timeframe: str = "1h"
    ) -> Dict[str, Any]:
        """
        単一のテーマ/仮説に対する自律改善サイクルを実行する。
        【提案】 -> 【検証 (自己修復)】 -> [【改善】 <-> 【検証】] -> 【判断】
        """
        print(f"\n{'='*60}")
        print(f"[Pipeline] 新規戦略サイクル開始: テーマ='{theme}'")
        print(f"{'='*60}")

        # 1. 【提案 (Strategy Proposer)】
        strat_info = self.proposer.propose_strategy(theme=theme)
        strat_id = strat_info["strategy_id"]
        self.state_manager.record_transition(
            strat_id,
            StrategyStatus.PROPOSED,
            {"name": strat_info["name"], "file": strat_info["file_path"], "hypothesis": strat_info["hypothesis"]}
        )
        print(f"[Proposer] 戦略コード生成完了: {strat_info['name']} ({strat_info['file_path']})")

        iteration = 1
        current_strat_info = strat_info

        while iteration <= self.max_iterations:
            print(f"\n--- [Iteration {iteration}/{self.max_iterations}] 検証・自己修復フェーズ: {current_strat_info['name']} ---")
            
            # 2. 【検証 (Backtest Runner)】
            self.state_manager.record_transition(
                strat_id,
                StrategyStatus.BACKTESTING,
                {"iteration": iteration, "file": current_strat_info["file_path"]}
            )

            # バックテスト実行（エラー時はRunner内で最大N回自己修復）
            bt_result = self.runner.run_backtest_with_healing(current_strat_info, train_df, test_df, timeframe=timeframe)

            if not bt_result["is_success"]:
                print(f"[Runner] [FAIL] 自己修復失敗・バックテスト中断: {bt_result.get('error')}")
                self.state_manager.record_transition(
                    strat_id,
                    StrategyStatus.REJECTED,
                    {"reason": "Backtest execution failure", "error": bt_result.get("error")}
                )
                return {"strategy_id": strat_id, "final_status": "REJECTED", "reason": bt_result.get("error")}

            is_metrics = bt_result["in_sample_metrics"]
            print(f"[Runner] [SUCCESS] バックテスト成功! メトリクス:")
            print(f"         Sharpe: {is_metrics.get('sharpe_ratio')} | MDD: {is_metrics.get('max_drawdown_pct')}% | "
                  f"Trades: {is_metrics.get('total_trades')} | WinRate: {is_metrics.get('win_rate_pct')}% | PF: {is_metrics.get('profit_factor')}")

            # 4. 【判断 (Governance)】
            decision = self.governance.evaluate(
                current_strat_info,
                bt_result,
                max_iterations=self.max_iterations,
                timeframe=timeframe
            )
            print(f"[Governance] 判定結果: {decision['status']} ({decision['reason']})")

            if decision["status"] == "PASS":
                self.state_manager.record_transition(
                    strat_id,
                    StrategyStatus.APPROVED,
                    {"report": decision["report_path"], "approved_file": decision["approved_file"], "metrics": is_metrics}
                )
                print(f"[Pipeline] [APPROVED] おめでとうございます！戦略 '{strat_id}' がガバナンス基準をクリアし、採択されました！")
                
                # Discord採択通知
                self.notifier.send_alert(
                    title="🏆 【戦略採択 APPROVED】新戦略がガバナンス基準をクリア！",
                    message=f"**戦略名**: `{current_strat_info['name']}`\n"
                            f"**時間足**: `{timeframe}`\n"
                            f"**Sharpe Ratio**: **{is_metrics.get('sharpe_ratio')}** | **MDD**: **{is_metrics.get('max_drawdown_pct')}%**\n"
                            f"**勝率**: **{is_metrics.get('win_rate_pct')}%** | **PF**: **{is_metrics.get('profit_factor')}**\n"
                            f"承認レポート: `{decision['report_path']}`",
                    level="success"
                )
                return {
                    "strategy_id": strat_id,
                    "final_status": "APPROVED",
                    "report_path": decision["report_path"],
                    "approved_file": decision["approved_file"],
                    "metrics": is_metrics
                }

            elif decision["status"] == "REJECT":
                self.state_manager.record_transition(
                    strat_id,
                    StrategyStatus.REJECTED,
                    {"failed_criteria": decision.get("failed_criteria"), "archive_file": decision.get("rejected_file")}
                )
                print(f"[Pipeline] [ARCHIVED] 戦略 '{strat_id}' は基準未達のためアーカイブされました。")
                return {
                    "strategy_id": strat_id,
                    "final_status": "REJECTED",
                    "reason": decision["reason"],
                    "metrics": is_metrics
                }

            # 3. 【改善 (Optimizer)】 -> 再度検証へ
            print(f"\n--- [Iteration {iteration}] 改善フェーズ (Optimizer) ---")
            self.state_manager.record_transition(
                strat_id,
                StrategyStatus.OPTIMIZING,
                {"iteration": iteration, "failed_criteria": decision.get("failed_criteria")}
            )

            current_strat_info = self.optimizer.optimize_strategy(current_strat_info, bt_result)
            
            # Discord戦略改善通知
            change_note = current_strat_info.get("hypothesis", "").split("->")[-1].strip() if "->" in current_strat_info.get("hypothesis", "") else "パラメータ調整 & フィルター追加"
            self.notifier.send_alert(
                title=f"💡 【戦略自律改善】{current_strat_info['name']} を改善しました！",
                message=f"**改善バージョン**: `{current_strat_info.get('version', 'v2.0')}` (イテレーション {iteration}/{self.max_iterations})\n"
                        f"**前回の課題**: {decision.get('reason', '')[:80]}\n"
                        f"**主な改善点**: **{change_note}**\n"
                        f"改善コード: `{current_strat_info['file_path']}`",
                level="info"
            )
            iteration += 1

        return {"strategy_id": strat_id, "final_status": "REJECTED", "reason": "Max iterations reached"}

    async def run_parallel_pipeline(
        self,
        themes: List[str],
        data_file: Optional[str] = None,
        source: Optional[str] = None,
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
        force_refresh: bool = False
    ):
        """
        複数のテーマ仮説を並行（Semaphore制御）で投入・実行する。
        """
        print(f"[Pipeline] データセット準備中...")
        full_df = DataLoader.load_or_generate_data(
            file_path=data_file,
            source=source,
            symbol=symbol,
            timeframe=timeframe,
            force_refresh=force_refresh,
            n_bars=4000
        )
        train_df, test_df = DataLoader.split_train_test(full_df, train_ratio=0.7)
        print(f"[Pipeline] データ準備完了 (訓練バー数: {len(train_df)}, テストバー数: {len(test_df)})")

        sem = asyncio.Semaphore(self.max_parallel)

        async def worker(theme: str):
            async with sem:
                return await self.run_single_theme_lifecycle(theme, train_df, test_df, timeframe=timeframe)

        print(f"[Pipeline] {len(themes)} 件のテーマについて並行処理 (並行数: {self.max_parallel}) を開始します。")
        tasks = [worker(theme) for theme in themes]
        results = await asyncio.gather(*tasks)

        print(f"\n{'='*60}")
        print(f"[Pipeline] 全パイプライン実行完了 サマリー")
        print(f"{'='*60}")
        summary = self.state_manager.get_summary()
        for k, v in summary.items():
            print(f"  {k:15}: {v} 件")

        # Discordへ全戦略検証結果サマリーを一括送信
        discord_summary = []
        for theme, res in zip(themes, results):
            m = res.get("metrics", {})
            discord_summary.append({
                "theme": theme,
                "name": res.get("strategy_id", "Strategy"),
                "status": res.get("final_status", "REJECT"),
                "trades": m.get("total_trades", 0),
                "win_rate": m.get("win_rate_pct", 0.0),
                "pf": m.get("profit_factor", 0.0),
                "mdd": m.get("max_drawdown_pct", 0.0),
                "sharpe": m.get("sharpe_ratio", 0.0),
                "reason": res.get("reason", "")
            })
        self.notifier.send_pipeline_discovery_summary(
            results_summary=discord_summary,
            symbol=symbol,
            timeframe=timeframe
        )
        return results
