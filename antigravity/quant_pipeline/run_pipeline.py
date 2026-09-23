"""
Antigravity Quant Pipeline Runner
==================================
1本の板データストリームから、
[Microstructure Agent] + [Trend Follow Agent] -> [Signal Fusion Engine] -> [ParquetBatchLogger]
さらに [Dry-run Simulator (影武者)] -> [Discord Quants Dry-run サーバー]
オプションで [SyncExecutor (SafetyGate)] -> [LiveOrderExecutor] -> [Discord LIVE サーバー]
をエンドツーエンドで駆動する統合ランナー。
"""
import sys
import time
import argparse
from datetime import datetime
from dataclasses import asdict

from .schema import OrderbookMicroSnapshot, FusionDecisionLog
from .parquet_logger import ParquetBatchLogger
from .event_bus import EventBus
from .agents.trend_agent import TrendFollowAgent
from .agents.microstructure_agent import MicrostructureAgent
from .agents.adverse_agent import AdverseResearchAgent
from .agents.duckdb_optimizer_agent import DuckDBOptimizerAgent
from .agents.peg_research_agent import PegResearchAgent
from .agents.mm_agent import MMAgent
from .quote_engine import QuoteEngine
from .mm_quote_store import MMQuoteStore
from .council_coordinator import FourAgentsCouncil
from .fusion_engine import SignalFusionEngine
from .ingestion import MarketDataIngestion
from .quant_discord_notifier import QuantDiscordNotifier
from .dryrun_simulator import DryRunSimulator
from .safety_gate import SafetyGate, SafetyGateConfig
from .live_order_executor import LiveOrderExecutor
from .sync_executor import SyncExecutor


def main():
    parser = argparse.ArgumentParser(description="Antigravity 4AGENT Quant Pipeline Runner")
    parser.add_argument("--symbol", default="FX_BTC_JPY", help="対象銘柄")
    parser.add_argument("--interval", type=float, default=2.0, help="観測間隔 (秒)")
    parser.add_argument("--steps", type=int, default=None, help="最大ステップ数 (テスト用)")
    parser.add_argument("--flush-interval", type=float, default=20.0, help="Parquetフラッシュ間隔 (秒)")
    parser.add_argument("--no-discord", action="store_true", help="Discord通知を無効化")
    parser.add_argument("--live-sync", action="store_true", help="SafetyGateによるLIVE同期を有効化")
    parser.add_argument("--real", action="store_true", help="【実資金】bitFlyer API実発注を有効化")
    parser.add_argument("--size", type=float, default=0.001, help="発注ロット (BTC, デフォルト: 0.001)")
    args = parser.parse_args()

    print("=" * 85)
    print("   🏛️  Antigravity 4AGENT Autonomous Strategy & Governance Pipeline  🏛️")
    print("   [1]Microstructure  [2]TrendFollow  [3]DuckDBOptimizer  [4]AdverseResearch")
    print("   [5]PegResearch (WIRE=NO · CSR-022/025/210o/231/499 · DATA only)")
    print("   [6]MMAgent+QuoteEngine (WIRE=NO · CSR-514 · continuous quote)")
    print("=" * 85)
    print(f"対象銘柄          : {args.symbol}")
    print(f"観測間隔          : {args.interval} 秒")
    print(f"Parquet出力先     : data/parquet/")
    print(f"Parquetフラッシュ : {args.flush_interval} 秒ごと")
    print(f"Discord 連携      : {'無効' if args.no_discord else '有効 (4AGENT合同評議会 & Dry-run サーバー)'}")
    
    live_mode_str = "無効 (Dry-run観測のみ)"
    if args.live_sync:
        live_mode_str = f"有効 ({'🚨 実資金' if args.real else '🛡️ シミュレーション'} ロット: {args.size} BTC)"
    print(f"LIVE 本番連動     : {live_mode_str}")
    print("-" * 85)
    print(" [時刻]    | Mid価格 (円) | Imb     | トレンド | 板圧力     | Adverse(スコア) | 確信度 | アクション | 仮想PnL  | LIVE")
    print("-" * 85)

    # 1. コンポーネント初期化
    logger = ParquetBatchLogger(flush_interval_sec=args.flush_interval, batch_size=100)
    bus = EventBus()
    notifier = None if args.no_discord else QuantDiscordNotifier()

    # 2. 影武者シミュレータ & 同期オーケストレーター初期化
    simulator = DryRunSimulator(notifier=notifier)
    sync_executor = None

    if args.live_sync:
        gate_config = SafetyGateConfig(
            min_dryrun_trades=5,
            min_dryrun_sharpe=0.50,
            min_confidence=0.65,
            max_daily_loss=300.0,
            max_consecutive_losses=4,
            max_spread_jpy=3000.0,
        )
        safety_gate = SafetyGate(gate_config)
        live_exec = LiveOrderExecutor(
            notifier=notifier,
            symbol=args.symbol,
            order_size_btc=args.size,
            enable_real_trading=args.real,
            take_profit_jpy=35.0,
            stop_loss_jpy=25.0,
            max_hold_sec=1800.0,
        )
        sync_executor = SyncExecutor(
            notifier=notifier,
            safety_gate=safety_gate,
            dryrun_sim=simulator,
            live_executor=live_exec,
            symbol=args.symbol,
            live_order_size=args.size,
            enable_real_live=args.real,
        )

    # 3. 4AGENT 登録 + PEG 研究（評議会外・執行非接続）
    trend_agent = TrendFollowAgent(bus)
    micro_agent = MicrostructureAgent(bus)
    adverse_agent = AdverseResearchAgent(bus)
    duckdb_agent = DuckDBOptimizerAgent(bus)
    peg_research_agent = PegResearchAgent(bus)  # CSR-022/025/210o · WIRE=NO
    mm_store = MMQuoteStore()
    mm_agent = MMAgent(bus)  # CSR-514 · continuous quote · WIRE=NO
    quote_engine = QuoteEngine(store=mm_store, bus=bus, logger=logger)

    # 4. 4AGENT 評議会コーディネーター登録
    council = FourAgentsCouncil(
        bus=bus,
        micro_agent=micro_agent,
        trend_agent=trend_agent,
        duckdb_agent=duckdb_agent,
        adverse_agent=adverse_agent,
        notifier=notifier,
    )

    # 5. 意思決定エンジン登録
    fusion_engine = SignalFusionEngine(bus, logger)

    # 6. Ingestionエンジン登録
    ingestion = MarketDataIngestion(bus, logger, product_code=args.symbol)

    step = 0
    last_discord_summary_ts = time.time()

    try:
        while True:
            step += 1
            snap = ingestion.poll_once()
            if snap:
                now_str = datetime.now().strftime("%H:%M:%S")

                # 4AGENT の合議判定を策定
                verdict = council.deliberate()

                dec = fusion_engine.latest_micro
                t_dir = fusion_engine.latest_trend["trend_direction"]
                p_side = dec.get("pressure_side", "none")
                p_score = dec.get("pressure_score", 0.0)

                adv_state = adverse_agent.latest_state
                adv_side = adv_state.get("adverse_side", "none")
                adv_score = adv_state.get("adverse_score", 0.0)
                adv_str = f"{adv_side[:1].upper()}:{adv_score:.2f}"

                # 最新意思決定を取得
                latest_dec = fusion_engine.evaluate()
                act_str = latest_dec.action.upper() if latest_dec else "HOLD"
                conf_val = latest_dec.final_confidence if latest_dec else 0.0

                # MM 連続クォート（WIRE=NO）— Trend dryrun と分離
                q = mm_agent.latest_quote or mm_agent.compute_quote(snap)
                quote_engine.step(q, snap)
                mm_agent.set_inventory(
                    quote_engine.inventory_btc,
                    quote_engine._inventory_pnl_jpy(snap.mid_price),
                )

                live_status = "OFF"

                if sync_executor:
                    # 価格更新（利確・損切・タイムアウト：気配値・スプレッド反映）
                    sync_executor.on_market_tick(snap.mid_price, best_bid=snap.best_bid, best_ask=snap.best_ask)
                    # シグナル同期処理
                    if latest_dec:
                        sig_dict = asdict(latest_dec)
                        spread_jpy = snap.best_ask - snap.best_bid
                        sync_executor.process_signal(
                            sig_dict,
                            snap.mid_price,
                            spread_jpy=spread_jpy,
                            best_bid=snap.best_bid,
                            best_ask=snap.best_ask,
                        )

                    l_state = sync_executor.live_state
                    if l_state.has_open_position:
                        live_status = f"{l_state.open_side[:1].upper()}:{l_state.open_size:.3f}"
                    else:
                        live_status = f"P:{l_state.daily_pnl:+.0f}"
                else:
                    # Dry-run 単独モード
                    simulator.feed_market_price(snap.mid_price, best_bid=snap.best_bid, best_ask=snap.best_ask)
                    if latest_dec:
                        sig_dict = asdict(latest_dec)
                        simulator.feed_signal(sig_dict, snap.mid_price, best_bid=snap.best_bid, best_ask=snap.best_ask)

                stats = simulator.get_stats()
                pnl_str = f"{stats.total_pnl:+7.1f}"

                print(
                    f" {now_str} | {snap.mid_price:12,.0f} | {snap.imbalance:+7.2f} | "
                    f"{t_dir:8} | {p_side:4}({p_score:.1f}) | {adv_str:15} | {conf_val:6.2f} | {act_str:6} | {pnl_str}円 | {live_status:>7}",
                    flush=True
                )

                # 60秒に1回、定常状態（HOLD）でもDiscord Dry-run サーバーへ健全性配信
                now = time.time()
                if notifier and (now - last_discord_summary_ts >= 60.0) and latest_dec:
                    notifier.notify_dryrun_fusion_decision(
                        decision=asdict(latest_dec),
                        mid_price=snap.mid_price,
                        virtual_pnl=stats.total_pnl,
                        virtual_win_rate=stats.win_rate,
                        force=True
                    )
                    last_discord_summary_ts = now


            if args.steps and step >= args.steps:
                print(f"\n[Pipeline] 指定ステップ数 ({args.steps}) に到達しました。")
                break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n[Pipeline] ユーザー中断を受信しました。")
    finally:
        print("[Pipeline] Parquetキューをフラッシュ中...")
        logger.stop()
        print("✅ 全ログの Parquet 保存が完了しました。")


if __name__ == "__main__":
    main()
