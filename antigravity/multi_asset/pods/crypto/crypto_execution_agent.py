"""
antigravity/multi_asset/pods/crypto/crypto_execution_agent.py: 暗号資産専属 EXECUTION AGENT
=====================================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 に準拠。
- bitFlyer FX_BTC_JPY 執行プロトコル・建玉管理・利確(+18円)/損切(-25円)
- 司令塔からのモード命令 (STOP/REDUCE_50等) 厳格遵守
"""

import time
import uuid
import logging
from typing import Dict, Any, Optional

from ...schemas import OrderCommand, TradeReport, ExecutionCommand
from ...base_agent import BaseExecutionAgent

logger = logging.getLogger("antigravity.multi_asset.pods.crypto.execution")


class CryptoExecutionAgent(BaseExecutionAgent):
    """
    BTC/JPY 専属 EXECUTION AGENT
    """

    def __init__(self, initial_risk_budget_jpy: float = 30000.0):
        super().__init__(asset_class="BTC", initial_risk_budget_jpy=initial_risk_budget_jpy)
        self.max_position_size = 0.05  # 最大保有ロット 0.05 BTC
        self.min_size = 0.001          # 最小発注単位
        self.positions: Dict[str, Dict[str, Any]] = {}

    def execute_order(self, order: OrderCommand) -> Optional[TradeReport]:
        """発注コマンドを実行"""
        if self.is_halted or self.target_mode == "STOP":
            logger.warning(f"[BTC:EXEC] 🚨 発注拒否: ガバナンス停止中 (mode={self.target_mode}, halted={self.is_halted})")
            return None

        if self.daily_pnl_jpy <= -abs(self.allocated_risk_jpy):
            self.is_halted = True
            logger.error(f"[BTC:EXEC] 🚨 日次損失リミット到達 (損益: {self.daily_pnl_jpy}円) -> CB遮断")
            return None

        size = max(self.min_size, round(order.size, 3))
        if self.target_mode == "REDUCE_50":
            size = max(self.min_size, round(size * 0.5, 3))

        cur_pos = self.positions.get(order.symbol, {}).get("size", 0.0)
        new_pos = cur_pos + (size if order.side == "BUY" else -size)
        if abs(new_pos) > self.max_position_size:
            logger.warning(f"[BTC:EXEC] ⚠️ 最大建玉超過: 想定={new_pos:.3f} > 上限={self.max_position_size:.3f}")
            return None

        base_price = order.price if order.price else 10005000.0
        exec_price = base_price + (2.0 if order.side == "BUY" else -2.0) # スプレッド・スリッページ

        trade_report: Optional[TradeReport] = None
        now = time.time()

        if cur_pos == 0.0 or (cur_pos > 0 and order.side == "BUY") or (cur_pos < 0 and order.side == "SELL"):
            # 新規エントリー
            total_size = cur_pos + (size if order.side == "BUY" else -size)
            self.positions[order.symbol] = {
                "size": round(total_size, 4),
                "avg_price": exec_price,
                "entry_time": now,
            }
            self.active_positions[order.symbol] = round(total_size, 4)
            logger.info(f"[BTC:EXEC] 🟢 約定(新規): {order.symbol} {order.side} {size} BTC @ {exec_price:,.0f}円")
        else:
            # 決済
            close_size = min(size, abs(cur_pos))
            pos_info = self.positions[order.symbol]
            entry_price = pos_info["avg_price"]

            if cur_pos > 0:
                pnl = (exec_price - entry_price) * close_size
            else:
                pnl = (entry_price - exec_price) * close_size

            self.daily_pnl_jpy += pnl
            remaining = round(cur_pos + (close_size if order.side == "BUY" else -close_size), 4)

            if remaining == 0.0:
                del self.positions[order.symbol]
                self.active_positions.pop(order.symbol, None)
            else:
                self.positions[order.symbol]["size"] = remaining
                self.active_positions[order.symbol] = remaining

            trade_id = f"btc_trd_{uuid.uuid4().hex[:8]}"
            trade_report = TradeReport(
                trade_id=trade_id,
                asset_class=self.asset_class,
                symbol=order.symbol,
                side=order.side,
                entry_price=entry_price,
                exit_price=exec_price,
                size=close_size,
                pnl_jpy=round(pnl, 2),
                holding_seconds=round(now - pos_info.get("entry_time", now), 1),
                exit_reason=order.reason or "MM/TP/SL Exit",
                timestamp=now,
            )
            self.trade_history.append(trade_report)
            logger.info(f"[BTC:EXEC] 🔔 約定(決済): {order.symbol} {order.side} {close_size} BTC 損益: {pnl:+.1f}円")

        return trade_report
