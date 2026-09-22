"""
UMM Strategy (Unified Market Making v1 - CSR-504 / CSR-458b 確定版)
=====================================================================
bitFlyer Lightning FX (FX_BTC_JPY) 向けマーケットメイク戦略。
FIX.me / CSR-504 正本準拠:
  - SPREAD_MIN_BP: 1.2 (スプレッド下限)
  - GAMMA_HIGH: 0.15 (在庫スキュー係数)
  - MAX_HOLD_SEC: 1800.0 (30分タイムアウト)
  - TAKE_PROFIT_JPY: 35.0 円
  - STOP_LOSS_JPY: 25.0 円 (HardStopBp: 4.0bp相当)
  - ORDER_SIZE_BTC: 0.001 BTC

⚠️ 【運用規則】
  - 本戦略パラメータは「自動調整禁止 (FROZEN / LOCKED)」とする。
  - DuckDB や Evolver による自動最適化は一切適用しない。
  - パラメータの変更はユーザーからの明示的な指示によってのみ実施する。
"""
import time
from typing import Dict, Any, Optional, List


class UMMStrategy:
    """
    Unified Market Making (UMM) v1 確定仕様
    """

    def __init__(self, parameters: Optional[Dict[str, Any]] = None):
        # 確定パラメータ (FIX.me CSR-504 準拠)
        self.strategy_name = "UMM_v1"
        self.frozen_mode = True  # 自動調整禁止フラグ
        self.last_user_directive = "GIT正本 (CSR-504) 初期パラメータ固定稼働"

        self.params = {
            "spread_min_bp": 1.2,          # スプレッド下限 (約1,500円)
            "max_spread_jpy": 3000.0,      # スプレッド上限 (スプレッド負け防止)
            "gamma_high": 0.15,            # 在庫スキュー感度
            "order_size_btc": 0.001,       # 基本ロット
            "max_position_btc": 0.005,     # 最大在庫枠
            "take_profit_jpy": 35.0,       # 利確幅 (円)
            "stop_loss_jpy": 25.0,         # 損切幅 (円)
            "max_hold_sec": 1800.0,        # 最大保有秒数
            "target_spread_markup": 0.5,   # 気配値提示マージン
        }
        if parameters:
            # ユーザーからの明示的な上書き指示のみ反映
            self.params.update(parameters)

        # 内部状態
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
        """ユーザーからの明示的な指示によるパラメータ変更"""
        self.params.update(new_params)
        self.last_user_directive = f"{reason} ({time.strftime('%Y-%m-%d %H:%M:%S')})"
        print(f"[UMMStrategy] 📝 ユーザー指示を適用しました: {new_params} - {reason}")

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
        """
        1 Tick ごとの戦略評価
        戻り値: action ("buy", "sell", "hold", "exit", "cancel"), quotes, reason
        """
        now = time.time()
        spread = best_ask - best_bid
        spread_bp = (spread / mid_price) * 10000.0 if mid_price > 0 else 0.0

        # -------------------------------------------------------------
        # 1. 既存ポジションの防護・エグジット判定 (利確・損切・タイムアウト・逆選択撤退)
        # -------------------------------------------------------------
        if self.position_side:
            elapsed = now - self.entry_time
            eval_price = best_bid if self.position_side == "buy" else best_ask
            price_diff = (eval_price - self.entry_price) if self.position_side == "buy" else (self.entry_price - eval_price)
            current_pnl = price_diff * self.params["order_size_btc"]

            # Adverse は研究フラグのみ。建玉は戦略ルールで閉じる。

            # (B) 利確
            if current_pnl >= self.params["take_profit_jpy"]:
                return {
                    "action": "exit",
                    "reason": f"TAKE_PROFIT (+¥{current_pnl:.1f})",
                    "price": eval_price,
                    "expected_pnl": current_pnl,
                }

            # (C) 損切 (HardStop)
            if current_pnl <= -self.params["stop_loss_jpy"]:
                return {
                    "action": "exit",
                    "reason": f"HARD_STOP (-¥{abs(current_pnl):.1f})",
                    "price": eval_price,
                    "expected_pnl": current_pnl,
                }

            # (D) タイムアウト
            if elapsed >= self.params["max_hold_sec"]:
                return {
                    "action": "exit",
                    "reason": f"TIMEOUT ({elapsed:.0f}s経過)",
                    "price": eval_price,
                    "expected_pnl": current_pnl,
                }

            return {"action": "hold", "reason": "POSITION_MAINTAINED", "current_pnl": current_pnl}

        # -------------------------------------------------------------
        # 2. 新規提示・エントリー判定 (在庫スキュー MM)
        # -------------------------------------------------------------
        # スプレッドチェック (狭すぎ・広すぎを回避)
        if spread_bp < self.params["spread_min_bp"]:
            return {"action": "hold", "reason": f"SPREAD_TOO_NARROW ({spread_bp:.1f}bp < {self.params['spread_min_bp']}bp)"}
        if spread > self.params["max_spread_jpy"]:
            return {"action": "hold", "reason": f"SPREAD_TOO_WIDE (¥{spread:,.0f} > ¥{self.params['max_spread_jpy']:,.0f})"}

        # Adverse は研究フラグのみ。新規提示は止めない。

        # 在庫スキューの計算 (gamma_high)
        # 買い持ちが多い -> 売り指値を引き下げ、買い指値を遠ざける
        skew_offset = -self.inventory_btc * self.params["gamma_high"] * mid_price * 0.001

        # クォート価格の計算
        half_spread = spread * 0.5
        bid_quote = round(mid_price - half_spread + skew_offset)
        ask_quote = round(mid_price + half_spread + skew_offset)

        return {
            "action": "quote",
            "reason": f"UMM_REST (Imb:{imbalance:+.2f}, Skew:{skew_offset:+.0f})",
            "bid_quote": bid_quote,
            "ask_quote": ask_quote,
            "spread_bp": round(spread_bp, 2),
            "skew_offset": round(skew_offset, 1),
        }

    @staticmethod
    def maker_fill(bid_quote, ask_quote, last_sell, last_buy, taker_bid, taker_ask):
        """指値を、その値段まで届いた約定だけが取る。最良気配での成行約定はしない。"""
        if bid_quote and last_sell and taker_bid > 0 and last_sell <= bid_quote:
            return "buy", float(bid_quote)
        if ask_quote and last_buy and taker_ask > 0 and last_buy >= ask_quote:
            return "sell", float(ask_quote)
        return None, None

    def record_trade(self, side: str, fill_price: float, pnl: float, mid_price: float = 0.0):
        """約定および損益の記録 (bp換算対応)"""
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
        """指定ウィンドウ (1h または 24h) の成績を集計"""
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
