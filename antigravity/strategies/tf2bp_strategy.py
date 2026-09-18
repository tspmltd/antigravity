"""
TF2BP Strategy (2bp Micro Trend Order Flow - CSR-495 / CSR-499 確定版)
========================================================================
bitFlyer Lightning FX (FX_BTC_JPY) 向け 2bp マイクロトレンド追従戦略。
FIX.me / CSR-495 / CSR-499 正本準拠:
  - TARGET_BP: 2.0 〜 15.0 bp (2bp初動モメンタム)
  - FLOW_PERSISTENCE_BARS: 3 (Takerフロー継続本数)
  - REVERSE_NOISE_MAX: 0.25 (逆方向ノイズ上限 25%)
  - TRAIL_STOP_BP: 4.0 bp (トレーリングストップ)
  - HEAVY_LAYER_LOCK: 1600_2200|mid|BOOST & 1600_2200|hi|BASE
  - TH_MAE: 3.82 / TH_MD: 0.12 (θ固定)
  - ORDER_SIZE_BTC: 0.001 BTC

⚠️ 【運用規則】
  - 本戦略パラメータは「自動調整禁止 (FROZEN / LOCKED)」とする。
  - DuckDB や Evolver による自動最適化は一切適用しない。
  - パラメータの変更はユーザーからの明示的な指示によってのみ実施する。
"""
import time
from typing import Dict, Any, Optional, List


class TF2BPStrategy:
    """
    2bp Micro Trend Order Flow (TF2BP) v1 確定仕様
    """

    def __init__(self, parameters: Optional[Dict[str, Any]] = None):
        self.strategy_name = "TF2BP_v1"
        self.frozen_mode = True  # 自動調整禁止フラグ
        self.last_user_directive = "GIT正本 (CSR-495/499) 初期パラメータ固定稼働"

        # 確定パラメータ
        self.params = {
            "micro_mom_bp": 2.0,            # 2bp初動 (0.02% = 0.0002)
            "target_bp": 15.0,              # 利益目標 (15bp = 0.15%)
            "trail_stop_bp": 4.0,           # トレーリングストップ幅 (4bp = 0.04%)
            "flow_window": 5,               # フロー観測窓 (サンプル数)
            "reverse_noise_max": 0.25,      # 逆方向ノイズ上限
            "order_size_btc": 0.001,        # 基本ロット
            "max_hold_sec": 1200.0,         # 最大保有秒数 (20分)
            "theta_mae": 3.82,              # CSR-499 θ固定
            "theta_md": 0.12,               # CSR-499 θ固定
        }
        if parameters:
            self.params.update(parameters)

        # 履歴バッファ
        self.price_history: List[float] = []
        self.flow_history: List[float] = []  # +1 (Taker買優勢), -1 (Taker売優勢)

        # 内部建玉状態
        self.position_side: Optional[str] = None
        self.entry_price: float = 0.0
        self.entry_time: float = 0.0
        self.peak_price: float = 0.0
        self.total_trades: int = 0
        self.win_trades: int = 0
        self.total_pnl: float = 0.0

    def set_user_override(self, new_params: Dict[str, Any], reason: str = "ユーザー指示による調整"):
        """ユーザーからの明示的な指示によるパラメータ変更"""
        self.params.update(new_params)
        self.last_user_directive = f"{reason} ({time.strftime('%Y-%m-%d %H:%M:%S')})"
        print(f"[TF2BPStrategy] 📝 ユーザー指示を適用しました: {new_params} - {reason}")

    def on_tick(
        self,
        mid_price: float,
        best_bid: float,
        best_ask: float,
        taker_vol_bid: float = 0.0,
        taker_vol_ask: float = 0.0,
        adverse_score: float = 0.0,
        cancel_recommendation: bool = False,
    ) -> Dict[str, Any]:
        """
        1 Tick ごとの戦略評価
        戻り値: action ("buy", "sell", "hold", "exit", "cancel"), reason
        """
        now = time.time()
        self.price_history.append(mid_price)
        if len(self.price_history) > 30:
            self.price_history.pop(0)

        # Taker Delta の記録
        net_taker = taker_vol_ask - taker_vol_bid
        flow_dir = 1.0 if net_taker > 0.05 else (-1.0 if net_taker < -0.05 else 0.0)
        self.flow_history.append(flow_dir)
        if len(self.flow_history) > self.params["flow_window"]:
            self.flow_history.pop(0)

        # -------------------------------------------------------------
        # 1. 既存ポジションの防護＆トレーリングストップ (TF2BP)
        # -------------------------------------------------------------
        if self.position_side:
            elapsed = now - self.entry_time
            eval_price = best_bid if self.position_side == "buy" else best_ask

            if self.position_side == "buy":
                if eval_price > self.peak_price:
                    self.peak_price = eval_price
                trail_stop_price = self.peak_price * (1.0 - self.params["trail_stop_bp"] * 0.0001)
                pnl = (eval_price - self.entry_price) * self.params["order_size_btc"]
                target_price = self.entry_price * (1.0 + self.params["target_bp"] * 0.0001)

                # (A) 逆選択キャンセル
                if cancel_recommendation or adverse_score >= 0.70:
                    return {"action": "cancel", "reason": f"ADVERSE_EVACUATE (逆選択スコア: {adverse_score:.2f})", "pnl": pnl}

                # (B) ターゲット到達利確 (15bp)
                if eval_price >= target_price:
                    return {"action": "exit", "reason": f"TARGET_BP_REACHED (+¥{pnl:.1f})", "pnl": pnl}

                # (C) トレーリングストップ発火 (4bpドローダウン)
                if eval_price <= trail_stop_price:
                    return {"action": "exit", "reason": f"TRAIL_STOP_HIT (Peak:¥{self.peak_price:,.0f}, PnL:{pnl:+.1f}円)", "pnl": pnl}

                # (D) 反対方向の強いTaker成行流入 (素早い撤退)
                if len(self.flow_history) >= 3 and sum(self.flow_history[-3:]) <= -2.5:
                    return {"action": "exit", "reason": "OPPOSITE_TAKER_FLIP_EXIT", "pnl": pnl}

            else:  # sell ポジション
                if eval_price < self.peak_price:
                    self.peak_price = eval_price
                trail_stop_price = self.peak_price * (1.0 + self.params["trail_stop_bp"] * 0.0001)
                pnl = (self.entry_price - eval_price) * self.params["order_size_btc"]
                target_price = self.entry_price * (1.0 - self.params["target_bp"] * 0.0001)

                if cancel_recommendation or adverse_score >= 0.70:
                    return {"action": "cancel", "reason": f"ADVERSE_EVACUATE (逆選択スコア: {adverse_score:.2f})", "pnl": pnl}

                if eval_price <= target_price:
                    return {"action": "exit", "reason": f"TARGET_BP_REACHED (+¥{pnl:.1f})", "pnl": pnl}

                if eval_price >= trail_stop_price:
                    return {"action": "exit", "reason": f"TRAIL_STOP_HIT (Peak:¥{self.peak_price:,.0f}, PnL:{pnl:+.1f}円)", "pnl": pnl}

                if len(self.flow_history) >= 3 and sum(self.flow_history[-3:]) >= 2.5:
                    return {"action": "exit", "reason": "OPPOSITE_TAKER_FLIP_EXIT", "pnl": pnl}

            # タイムアウト
            if elapsed >= self.params["max_hold_sec"]:
                return {"action": "exit", "reason": f"TIMEOUT ({elapsed:.0f}s経過)", "pnl": pnl}

            return {"action": "hold", "reason": "POSITION_RUNNING", "current_pnl": pnl}

        # -------------------------------------------------------------
        # 2. 新規エントリー判定 (2bp初動モメンタム ＋ Takerフロー継続)
        # -------------------------------------------------------------
        if len(self.price_history) < 5 or len(self.flow_history) < 3:
            return {"action": "hold", "reason": "WARMING_UP"}

        # 直近 N サンプルのリターン (bp)
        start_p = self.price_history[0]
        curr_p = self.price_history[-1]
        mom_bp = ((curr_p - start_p) / start_p) * 10000.0

        # フロー継続度
        flow_sum = sum(self.flow_history)
        noise_ratio = len([f for f in self.flow_history if (f < 0 if mom_bp > 0 else f > 0)]) / len(self.flow_history)

        # 逆選択警戒時は見送り
        if adverse_score >= 0.60:
            return {"action": "hold", "reason": f"ADVERSE_VETO (逆選択スコア: {adverse_score:.2f})"}

        # 2bp以上の初動 ＋ Takerフロー継続 ＋ 逆方向ノイズ25%以下
        if mom_bp >= self.params["micro_mom_bp"] and flow_sum >= 2.0 and noise_ratio <= self.params["reverse_noise_max"]:
            return {
                "action": "buy",
                "reason": f"TF2BP_LONG (Mom:{mom_bp:+.1f}bp, Flow:{flow_sum:+.1f}, Noise:{noise_ratio:.2f})",
                "entry_price": best_ask,
            }
        elif mom_bp <= -self.params["micro_mom_bp"] and flow_sum <= -2.0 and noise_ratio <= self.params["reverse_noise_max"]:
            return {
                "action": "sell",
                "reason": f"TF2BP_SHORT (Mom:{mom_bp:+.1f}bp, Flow:{flow_sum:+.1f}, Noise:{noise_ratio:.2f})",
                "entry_price": best_bid,
            }

        return {"action": "hold", "reason": "WAIT_MOMENTUM_2BP"}

    def record_trade(self, side: str, fill_price: float, pnl: float):
        """約定および損益の記録"""
        self.total_trades += 1
        if pnl > 0:
            self.win_trades += 1
        self.total_pnl += pnl

        if side in ("buy", "sell"):
            self.position_side = side
            self.entry_price = fill_price
            self.peak_price = fill_price
            self.entry_time = time.time()
        elif side in ("close_buy", "close_sell", "exit", "cancel"):
            self.position_side = None
            self.entry_price = 0.0
            self.peak_price = 0.0
