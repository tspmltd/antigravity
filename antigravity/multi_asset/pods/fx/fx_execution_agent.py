"""
antigravity/multi_asset/pods/fx/fx_execution_agent.py: 為替 (FX) 専属 EXECUTION AGENT
=============================================================================
仕様書: docs/multi_asset_os_architecture.md セクション3 & 5 に準拠。
- USD/JPY ロット単位 (1ロット = 1万通貨, 1pip = 100円/lot)
- ピップスベースの損益計算・スリッページモデル (0.2 pips)
- 司令塔ガバナンス (STOP / REDUCE_50) 厳守 & 日次損失CB
"""

import time
import uuid
import logging
from typing import Dict, Any, Optional

from ...schemas import OrderCommand, TradeReport, ExecutionCommand
from ...base_agent import BaseExecutionAgent
from .oanda_adapter import OandaAdapter, OandaRisk, OandaREST

logger = logging.getLogger("antigravity.multi_asset.pods.fx.execution")


class FxExecutionAgent(BaseExecutionAgent):
    """
    為替 (FX) 専属 EXECUTION AGENT
    OANDA REST API 実発注 ＆ ペーパートレード両対応
    """

    def __init__(
        self,
        initial_risk_budget_jpy: float = 30000.0,
        enable_oanda_real: bool = False,
        oanda_adapter: Optional[OandaAdapter] = None,
    ):
        super().__init__(asset_class="FX", initial_risk_budget_jpy=initial_risk_budget_jpy)
        self.max_position_size = 5.0   # 最大保有ロット (5ロット = 5万通貨)
        self.min_lot = 0.1             # 最小発注単位 (1,000通貨 = 0.1ロット)
        self.units_per_lot = 10000.0   # 1ロットあたりの通貨数 (1万通貨)
        self.slippage_pips = 0.2       # 0.2 pips スリッページ
        self.positions: Dict[str, Dict[str, Any]] = {}
        
        # OANDA アダプター & リスク管理
        self.enable_oanda_real = enable_oanda_real
        self.adapter = oanda_adapter or OandaAdapter()
        self.risk = self.adapter.risk
        self.rest = self.adapter.rest

    def execute_order(self, order: OrderCommand) -> Optional[TradeReport]:
        """発注コマンドを実行"""
        if self.is_halted or self.target_mode == "STOP":
            logger.warning(f"[FX:EXEC] 🚨 発注拒否: ガバナンス停止中 (mode={self.target_mode}, halted={self.is_halted})")
            return None

        if self.daily_pnl_jpy <= -abs(self.allocated_risk_jpy):
            self.is_halted = True
            logger.error(f"[FX:EXEC] 🚨 日次損失リミット到達 (損益: {self.daily_pnl_jpy}円) -> CB遮断")
            return None

        lot_size = max(self.min_lot, round(order.size, 2))
        if self.target_mode == "REDUCE_50":
            lot_size = max(self.min_lot, round(lot_size * 0.5, 2))

        # OANDA 4大安全装置チェック (CB、スプレッドショック、建玉上限、イベント窓)
        spread_pips = getattr(order, "spread_pips", 0.3)
        check_ok, reason = self.risk.pre_order_check(
            instrument=order.symbol,
            lots=lot_size,
            current_spread_pips=spread_pips,
        )
        if not check_ok:
            logger.warning(f"[FX:EXEC] 🛡️ リスクゲート遮断: {reason}")
            return None

        cur_lots = self.positions.get(order.symbol, {}).get("lots", 0.0)
        new_lots = cur_lots + (lot_size if order.side == "BUY" else -lot_size)
        if abs(new_lots) > self.max_position_size:
            logger.warning(f"[FX:EXEC] ⚠️ 最大建玉超過: 想定={new_lots:.2f} > 上限={self.max_position_size:.2f} lots")
            return None

        # OANDA API 実発注 (実資金モード時)
        if self.enable_oanda_real:
            try:
                units = int(lot_size * self.units_per_lot)
                oanda_resp = self.rest.send_order(
                    instrument=order.symbol,
                    units=units,
                    side=order.side,
                    order_type="MARKET",
                )
                logger.info(f"[FX:EXEC] 🚨 [OANDA REAL ORDER] 送信成功: {oanda_resp.get('orderFillTransaction', {}).get('id')}")
            except Exception as e:
                logger.error(f"[FX:EXEC] ❌ OANDA実発注エラー: {e}")
                return None

        base_price = order.price if order.price else 150.000
        slip_jpy = (self.slippage_pips / 100.0) # 1 pip = 0.01 JPY
        exec_price = round(base_price + (slip_jpy if order.side == "BUY" else -slip_jpy), 3)

        trade_report: Optional[TradeReport] = None
        now = time.time()

        if cur_lots == 0.0 or (cur_lots > 0 and order.side == "BUY") or (cur_lots < 0 and order.side == "SELL"):
            # 新規エントリー / 増玉
            total_lots = round(cur_lots + (lot_size if order.side == "BUY" else -lot_size), 2)
            existing_cost = self.positions.get(order.symbol, {}).get("avg_price", 0.0) * abs(cur_lots)
            new_cost = exec_price * lot_size
            new_avg = (existing_cost + new_cost) / abs(total_lots)

            self.positions[order.symbol] = {
                "lots": total_lots,
                "avg_price": round(new_avg, 4),
                "entry_time": now,
            }
            self.active_positions[order.symbol] = total_lots
            logger.info(f"[FX:EXEC] 🟢 約定(新規): {order.symbol} {order.side} {lot_size} lots @ {exec_price:.3f}円 (保有: {total_lots} lots)")

        else:
            # 決済 / 途転
            close_lots = min(lot_size, abs(cur_lots))
            pos_info = self.positions[order.symbol]
            entry_price = pos_info["avg_price"]

            # 損益計算: (価格差 JPY) * (決済通貨量)
            currency_units = close_lots * self.units_per_lot
            if cur_lots > 0: # 買い玉決済
                pnl = (exec_price - entry_price) * currency_units
            else:           # 売り玉決済
                pnl = (entry_price - exec_price) * currency_units

            self.daily_pnl_jpy += pnl
            self.risk.daily_pnl_fx = self.daily_pnl_jpy
            remaining = round(cur_lots + (close_lots if order.side == "BUY" else -close_lots), 2)

            if remaining == 0.0:
                del self.positions[order.symbol]
                self.active_positions.pop(order.symbol, None)
            else:
                self.positions[order.symbol]["lots"] = remaining
                self.active_positions[order.symbol] = remaining

            trade_id = f"fx_trd_{uuid.uuid4().hex[:8]}"
            trade_report = TradeReport(
                trade_id=trade_id,
                asset_class=self.asset_class,
                symbol=order.symbol,
                side=order.side,
                entry_price=entry_price,
                exit_price=exec_price,
                size=close_lots,
                pnl_jpy=round(pnl, 2),
                holding_seconds=round(now - pos_info.get("entry_time", now), 1),
                exit_reason=order.reason or "FX TakeProfit/StopLoss Exit",
                timestamp=now,
            )
            self.trade_history.append(trade_report)
            logger.info(f"[FX:EXEC] 🔔 約定(決済): {order.symbol} {order.side} {close_lots} lots 損益: {pnl:+.1f}円 (本日累計: {self.daily_pnl_jpy:+.1f}円)")

        return trade_report
