"""
antigravity/multi_asset/pods/japan_equity/jp_execution_agent.py: 日本株専属 EXECUTION AGENT
========================================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 に準拠。
- 東証単元株制 (100株単位) および 呼値刻み (Tick Size) ルール厳守
- 日次損失リミット (CB) & 司令塔からの動作モード (HFT/TREND/HYBRID/REDUCE_50/STOP) 厳格適用
- 高精度ペーパートレードシミュレーション & ポジション・損益管理
"""

import time
import uuid
import logging
from typing import Dict, Any, Optional, List

from ...schemas import OrderCommand, TradeReport, ExecutionCommand
from ...base_agent import BaseExecutionAgent

logger = logging.getLogger("antigravity.multi_asset.pods.japan_equity.execution")


class JpExecutionAgent(BaseExecutionAgent):
    """
    日本株専属 EXECUTION AGENT
    """

    def __init__(self, initial_risk_budget_jpy: float = 20000.0):
        super().__init__(asset_class="JP_STOCK", initial_risk_budget_jpy=initial_risk_budget_jpy)
        self.max_position_size = 1000.0  # 最大保有株数 (10単元 = 1,000株)
        self.unit_shares = 100           # 東証単元株単位 (100株)
        self.max_spread_pct = 0.008      # スプレッドショック防護 (0.8%)
        self.slippage_bps = 2.0          # スリッページ (2bp = 0.02%)
        self.commission_rate = 0.0000    # 手数料 (ゼロ手数料モデル)
        self.open_orders: Dict[str, OrderCommand] = {}
        self.positions: Dict[str, Dict[str, Any]] = {} # symbol -> {"shares": int, "avg_price": float}

    @staticmethod
    def round_to_tse_tick(price: float) -> float:
        """東証呼値（呼値刻み）テーブル厳守"""
        if price <= 3000:
            return float(round(price))  # 1円刻み (1,000円〜3,000円)
        elif price <= 5000:
            return float(round(price / 5.0) * 5.0)  # 5円刻み (3,000円〜5,000円)
        elif price <= 30000:
            return float(round(price / 10.0) * 10.0)  # 10円刻み (5,000円〜30,000円)
        elif price <= 50000:
            return float(round(price / 50.0) * 50.0)  # 50円刻み (30,000円〜50,000円)
        else:
            return float(round(price / 100.0) * 100.0) # 100円刻み (50,000円超)

    def enforce_unit_shares(self, size: float) -> int:
        """東証単元株制 (100株単位) 強制 (四捨五入)"""
        shares = int((size + self.unit_shares / 2.0) // self.unit_shares) * self.unit_shares
        return max(self.unit_shares, shares)

    def execute_order(self, order: OrderCommand) -> Optional[TradeReport]:
        """
        発注コマンドを実行
        """
        # 1. 司令塔STOP・CB遮断チェック (Regime Orchestrator STOP絶対優先)
        if self.is_halted or self.target_mode == "STOP":
            logger.warning(f"[JP:EXEC] 🚨 発注拒否: ガバナンス停止中 (mode={self.target_mode}, halted={self.is_halted})")
            return None

        # 2. 日次CB (Circuit Breaker) チェック (日次損失 > -20,000円 で自動停止)
        if self.daily_pnl_jpy <= -abs(self.allocated_risk_jpy):
            self.is_halted = True
            logger.error(
                f"[JP:EXEC] 🚨 日次CB発動 (本日損失: {self.daily_pnl_jpy:+,.1f}円 <= リミット: {-abs(self.allocated_risk_jpy):+,.1f}円) -> 自動停止"
            )
            return None

        # 3. 単元株 (100株単位) 補正 & モード別サイズ制限
        shares = self.enforce_unit_shares(order.size)
        if self.target_mode == "REDUCE_50":
            shares = max(self.unit_shares, int(shares * 0.5 // self.unit_shares * self.unit_shares))

        # 現在ポジション取得
        cur_pos = self.positions.get(order.symbol, {}).get("shares", 0)
        is_closing = (cur_pos > 0 and order.side == "SELL") or (cur_pos < 0 and order.side == "BUY")

        # 4. スプレッドショック防護 (スプレッド > 0.8% の場合、新規発注禁止)
        spread_pct = 0.0
        if hasattr(order, "extra") and isinstance(order.extra, dict):
            spread_pct = float(order.extra.get("spread_pct", 0.0))
        if not is_closing and spread_pct > self.max_spread_pct:
            logger.warning(
                f"[JP:EXEC] 🚨 スプレッドショック防護発動: {order.symbol} spread_pct={spread_pct*100:.2f}% > 0.8% -> 新規発注禁止"
            )
            return None

        # 最大ポジション上限チェック
        new_pos = cur_pos + (shares if order.side == "BUY" else -shares)
        if abs(new_pos) > self.max_position_size:
            logger.warning(f"[JP:EXEC] ⚠️ 保有上限超過エラー: 想定建玉={new_pos}株 > 上限={self.max_position_size}株")
            return None

        # 5. 約定シミュレーション (呼値刻み丸め & スリッページ適用)
        base_price = order.price if order.price else 2500.0
        base_price = self.round_to_tse_tick(base_price)

        slip_factor = (1.0 + self.slippage_bps / 10000.0) if order.side == "BUY" else (1.0 - self.slippage_bps / 10000.0)
        exec_price = self.round_to_tse_tick(base_price * slip_factor)

        # 5. 建玉更新 & 損益計算
        trade_report: Optional[TradeReport] = None
        now = time.time()

        if cur_pos == 0 or (cur_pos > 0 and order.side == "BUY") or (cur_pos < 0 and order.side == "SELL"):
            # 新規 / 増玉
            total_shares = cur_pos + (shares if order.side == "BUY" else -shares)
            existing_cost = self.positions.get(order.symbol, {}).get("avg_price", 0.0) * abs(cur_pos)
            new_cost = exec_price * shares
            new_avg = (existing_cost + new_cost) / abs(total_shares)

            self.positions[order.symbol] = {
                "shares": total_shares,
                "avg_price": new_avg,
                "entry_time": now,
            }
            self.active_positions[order.symbol] = float(total_shares)
            logger.info(f"[JP:EXEC] 🟢 約定(新規): {order.symbol} {order.side} {shares}株 @ {exec_price}円 (保有計: {total_shares}株)")

        else:
            # 返済・決済
            close_shares = min(shares, abs(cur_pos))
            pos_info = self.positions[order.symbol]
            entry_price = pos_info["avg_price"]

            if cur_pos > 0:  # 買い建玉の返済売り
                pnl = (exec_price - entry_price) * close_shares
            else:            # 売り建玉の買戻し
                pnl = (entry_price - exec_price) * close_shares

            self.daily_pnl_jpy += pnl
            remaining = cur_pos + (close_shares if order.side == "BUY" else -close_shares)

            if remaining == 0:
                del self.positions[order.symbol]
                self.active_positions.pop(order.symbol, None)
            else:
                self.positions[order.symbol]["shares"] = remaining
                self.active_positions[order.symbol] = float(remaining)

            trade_id = f"jp_trd_{uuid.uuid4().hex[:8]}"
            trade_report = TradeReport(
                trade_id=trade_id,
                asset_class=self.asset_class,
                symbol=order.symbol,
                side=order.side,
                entry_price=entry_price,
                exit_price=exec_price,
                size=float(close_shares),
                pnl_jpy=round(pnl, 2),
                holding_seconds=round(now - pos_info.get("entry_time", now), 1),
                exit_reason=order.reason or "Target/SL Exit",
                timestamp=now,
            )
            self.trade_history.append(trade_report)
            logger.info(
                f"[JP:EXEC] 🔔 約定(決済): {order.symbol} {order.side} {close_shares}株 損益: {pnl:+.1f}円 (本日累計: {self.daily_pnl_jpy:+.1f}円)"
            )

        return trade_report
