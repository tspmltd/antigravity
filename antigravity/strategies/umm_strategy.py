"""
UMM Strategy (Unified Market Making v1 - CSR-504 / CSR-458b 確定版)
=====================================================================
GIT正本: gapcore-platform/internal/strategy/unified_mm_v1.go
  - SPREAD_MIN_BP: 1.2
  - GAMMA_HIGH: 0.15
  - HardStopBp: 5.0 （mid 建値損益 bp）
  - MAX_HOLD_SEC: 1800.0 (failsafe_age)
  - ORDER_SIZE_BTC: 0.001 BTC

旧 take_profit_jpy/stop_loss_jpy × size は実質 ~18–26bp になり Go HardStop と乖離。
イグジットは bp 正本に統一。

⚠️ 自動調整禁止 (FROZEN)。パラメータ変更はユーザー明示指示のみ。
"""
import time
from typing import Dict, Any, Optional, List


class UMMStrategy:
    def __init__(self, parameters: Optional[Dict[str, Any]] = None):
        self.strategy_name = "UMM_v1"
        self.frozen_mode = True
        self.last_user_directive = "連続クォート型 (CSR-514) · HardStopBp=5.0 mid-bp failsafe"

        self.params = {
            "spread_min_bp": 1.2,
            "max_spread_jpy": 3000.0,
            "gamma_high": 0.15,
            "order_size_btc": 0.001,
            "max_position_btc": 0.005,
            "hard_stop_bp": 5.0,
            "take_profit_bp": 2.5,
            "max_hold_sec": 1800.0,
            "target_spread_markup": 0.5,
        }
        if parameters:
            self.params.update(parameters)
        # 旧 jpy キーは誤移植のため破棄（bp 正本を優先）
        self.params.pop("take_profit_jpy", None)
        self.params.pop("stop_loss_jpy", None)

        self.inventory_btc: float = 0.0
        self.position_side: Optional[str] = None
        self.entry_price: float = 0.0
        self.entry_time: float = 0.0
        self.total_trades: int = 0
        self.win_trades: int = 0
        self.total_pnl: float = 0.0
        self.total_pnl_bp: float = 0.0
        self.trades_history: List[Dict[str, Any]] = []

    def set_user_override(self, new_params: Dict[str, Any], reason: str = "ユーザー指示による調整"):
        self.params.update(new_params)
        self.params.pop("take_profit_jpy", None)
        self.params.pop("stop_loss_jpy", None)
        self.last_user_directive = f"{reason} ({time.strftime('%Y-%m-%d %H:%M:%S')})"
        print(f"[UMMStrategy] 📝 ユーザー指示を適用しました: {new_params} - {reason}")

    def _pnl_bp(self, mark: float) -> float:
        if self.entry_price <= 0 or mark <= 0 or not self.position_side:
            return 0.0
        raw = (mark - self.entry_price) / self.entry_price * 10000.0
        return raw if self.position_side == "buy" else -raw

    def on_tick(
        self,
        mid_price: float,
        best_bid: float,
        best_ask: float,
        imbalance: float = 0.0,
        adverse_score: float = 0.0,
        cancel_recommendation: bool = False,
        avoidance_on: bool = False,
    ) -> Dict[str, Any]:
        now = time.time()
        spread = best_ask - best_bid
        spread_bp = (spread / mid_price) * 10000.0 if mid_price > 0 else 0.0

        if self.position_side:
            elapsed = now - self.entry_time
            tip_mark = best_bid if self.position_side == "buy" else best_ask
            pnl_bp_mid = self._pnl_bp(mid_price)
            pnl_bp_tip = self._pnl_bp(tip_mark)
            size = float(self.params["order_size_btc"])
            price_diff = (tip_mark - self.entry_price) if self.position_side == "buy" else (self.entry_price - tip_mark)
            current_pnl_jpy = price_diff * size

            hard_stop = float(self.params.get("hard_stop_bp", 5.0))
            take_profit = float(self.params.get("take_profit_bp", 2.5))

            if pnl_bp_tip >= take_profit:
                return {
                    "action": "exit",
                    "reason": f"TAKE_PROFIT (+{pnl_bp_tip:.2f}bp tip)",
                    "price": tip_mark,
                    "expected_pnl": current_pnl_jpy,
                    "pnl_bp": pnl_bp_tip,
                }

            if pnl_bp_mid <= -hard_stop:
                mid_diff = (mid_price - self.entry_price) if self.position_side == "buy" else (self.entry_price - mid_price)
                return {
                    "action": "exit",
                    "reason": f"HARD_STOP ({pnl_bp_mid:.2f}bp mid≤-{hard_stop:.1f})",
                    "price": tip_mark,
                    "expected_pnl": mid_diff * size,
                    "pnl_bp": pnl_bp_mid,
                }

            if elapsed >= self.params["max_hold_sec"]:
                return {
                    "action": "exit",
                    "reason": f"TIMEOUT ({elapsed:.0f}s経過, {pnl_bp_mid:+.2f}bp mid)",
                    "price": tip_mark,
                    "expected_pnl": current_pnl_jpy,
                    "pnl_bp": pnl_bp_mid,
                }
            # 連続クォート: 建玉中も hold-only にせず両面を出し続ける（反対約定で解消）

        if spread_bp < self.params["spread_min_bp"]:
            return {"action": "hold", "reason": f"SPREAD_TOO_NARROW ({spread_bp:.1f}bp < {self.params['spread_min_bp']}bp)"}
        if spread > self.params["max_spread_jpy"]:
            return {"action": "hold", "reason": f"SPREAD_TOO_WIDE (¥{spread:,.0f} > ¥{self.params['max_spread_jpy']:,.0f})"}

        skew_offset = -self.inventory_btc * self.params["gamma_high"] * mid_price * 0.001
        half_spread = spread * 0.5
        bid_quote = round(mid_price - half_spread + skew_offset)
        ask_quote = round(mid_price + half_spread + skew_offset)

        # 在庫キャップ: 同方向の追加は出さない
        if self.inventory_btc >= self.params["max_position_btc"]:
            bid_quote = None
        if self.inventory_btc <= -self.params["max_position_btc"]:
            ask_quote = None

        return {
            "action": "quote",
            "reason": f"UMM_CONT (Imb:{imbalance:+.2f}, Skew:{skew_offset:+.0f}, Inv:{self.inventory_btc:+.4f})",
            "bid_quote": bid_quote,
            "ask_quote": ask_quote,
            "spread_bp": round(spread_bp, 2),
            "skew_offset": round(skew_offset, 1),
            "continuous_quote": True,
        }

    @staticmethod
    def maker_fill(bid_quote, ask_quote, last_sell, last_buy, taker_bid, taker_ask):
        if bid_quote and last_sell and taker_bid > 0 and last_sell <= bid_quote:
            return "buy", float(bid_quote)
        if ask_quote and last_buy and taker_ask > 0 and last_buy >= ask_quote:
            return "sell", float(ask_quote)
        return None, None

    def record_trade(self, side: str, fill_price: float, pnl: float, mid_price: float = 0.0):
        now = time.time()
        eval_price = mid_price if mid_price > 0 else fill_price
        order_val_jpy = self.params["order_size_btc"] * eval_price if eval_price > 0 else 12500.0
        pnl_bp = (pnl / order_val_jpy) * 10000.0 if order_val_jpy > 0 else 0.0

        if side in ("close_buy", "close_sell", "exit", "cancel"):
            self.total_trades += 1
            if pnl > 0:
                self.win_trades += 1
            self.total_pnl += pnl
            self.total_pnl_bp += pnl_bp
            self.trades_history.append({
                "ts": now,
                "side": self.position_side,
                "exit_side": side,
                "fill_price": fill_price,
                "pnl_jpy": round(pnl, 1),
                "pnl_bp": round(pnl_bp, 2),
                "is_win": (pnl > 0),
            })
            self.position_side = None
            self.entry_price = 0.0
            self.inventory_btc = 0.0

        elif side == "buy":
            self.inventory_btc += self.params["order_size_btc"]
            self.position_side = "buy"
            self.entry_price = fill_price
            self.entry_time = now
        elif side == "sell":
            self.inventory_btc -= self.params["order_size_btc"]
            self.position_side = "sell"
            self.entry_price = fill_price
            self.entry_time = now

    def get_window_stats(self, hours: float = 1.0) -> Dict[str, Any]:
        now = time.time()
        cutoff = now - (hours * 3600.0)
        recent_trades = [t for t in self.trades_history if t["ts"] >= cutoff]
        total_t = len(recent_trades)
        win_t = sum(1 for t in recent_trades if t["is_win"])
        loss_t = total_t - win_t
        pnl_jpy = sum(t["pnl_jpy"] for t in recent_trades)
        pnl_bp = sum(t["pnl_bp"] for t in recent_trades)
        wr = (win_t / total_t * 100.0) if total_t > 0 else 0.0
        return {
            "window_hours": hours,
            "total_trades": total_t,
            "win_trades": win_t,
            "loss_trades": loss_t,
            "win_rate_pct": round(wr, 1),
            "pnl_jpy": round(pnl_jpy, 1),
            "pnl_bp": round(pnl_bp, 2),
        }
