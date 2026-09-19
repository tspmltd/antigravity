"""
TF2BP with PEG_v2 Strategy (Model 3: EffectiveReach + Model 1: DynamicRatio)
=============================================================================
TF2BP (CSR-499) の微小モメンタム判定に、
バックテストで最高改善 (+54bp) を実証した【PEG_v2 指値執行エンジン】を搭載した
観測検証（OBSERVATION）専用戦略。

【PEG_v2 の構成要素】:
  1. Model 1 (Dynamic Ratio):
     - 対向板厚（Depth 1）が薄ければ深く差し込み（0.975〜0.980）、
       厚い壁があれば手前（0.915〜0.920）で先頭キューを奪取。
  2. Model 3 (Effective Reach):
     - テイカー攻撃性（Taker Aggressiveness）が高い瞬間は板が抜けるため、
       さらに +0.015〜+0.020 ブーストして最速約定を刈り取る。
  3. TF2BP 厳格エグジット規律:
     - 建値防衛 (BE5: MFE >= 5.0bp 後、利益ゼロ反落で be_stop 即時脱出)
     - 利益目標利確 (Target 15.0bp)
     - トレーリングストップ (Peak から 4.0bp ドローダウン)
     - 逆選択先回り退避 (Adverse Score >= 0.70)
"""
import time
import math
from typing import Dict, Any, Optional, List


class TF2BP_PEG_v2_Strategy:
    """
    TF2BP + PEG_v2 (Model 3+1) 観測検証用戦略
    """

    def __init__(self, parameters: Optional[Dict[str, Any]] = None):
        self.strategy_name = "TF2BP_PEG_v2"
        self.frozen_mode = True
        self.last_user_directive = "OBSERVATION: TF2BP + PEG_v2 (Model 3+1) 並行検証稼働"

        # 確定パラメータ
        self.params = {
            "micro_mom_bp": 2.0,            # 2bp初動モメンタム
            "target_bp": 15.0,              # 利益目標 (15bp)
            "trail_stop_bp": 4.0,           # トレーリングストップ (4bp)
            "be_arm_bp": 5.0,               # 建値防衛アーム (MFE 5bp到達で発動)
            "flow_window": 5,               # フロー観測窓
            "reverse_noise_max": 0.25,      # 逆方向ノイズ上限
            "order_size_btc": 0.001,        # 基本ロット
            "max_hold_sec": 1200.0,         # 最大保有秒数 (20分)
            "max_wait_ticks": 15,           # PEG指値の最長待ち時間 (約30秒)
        }
        if parameters:
            self.params.update(parameters)

        # 履歴バッファ
        self.price_history: List[float] = []
        self.flow_history: List[float] = []

        # PEG 指値待機管理
        self.pending_order: Optional[Dict[str, Any]] = None

        # 内部建玉状態
        self.position_side: Optional[str] = None
        self.entry_price: float = 0.0
        self.entry_time: float = 0.0
        self.peak_price: float = 0.0
        self.peak_mfe_bp: float = 0.0
        self.be_armed: bool = False

        # 成績記録
        self.total_trades: int = 0
        self.win_trades: int = 0
        self.total_pnl: float = 0.0
        self.total_pnl_bp: float = 0.0
        self.trades_history: List[Dict[str, Any]] = []

    def calculate_peg_v2_price(
        self,
        side: str,
        best_bid: float,
        best_ask: float,
        opp_depth: float = 0.1,
        taker_aggressiveness: float = 0.5,
        cancel_rate: float = 0.0,
        refill_rate: float = 0.0,
    ) -> float:
        """
        【PEG_v2 計算式】Model 1 (Dynamic Ratio) + Model 3 (Effective Reach)
        """
        spread = best_ask - best_bid
        if spread <= 0:
            return best_ask if side == "buy" else best_bid

        # 1. キャンセル・リフィルを考慮した実効板厚 (Effective Depth)
        eff_depth = opp_depth * (1.0 - cancel_rate + 0.5 * refill_rate)
        eff_depth = max(eff_depth, 0.001)

        # 2. Model 1 (Dynamic Ratio): 板厚連動 (0.915 〜 0.975)
        # 対向板が薄い(0.05BTC未満)なら 0.975、厚い(0.5BTC超)なら 0.915
        depth_factor = min(max(eff_depth / 0.20, 0.0), 1.0)
        base_ratio = 0.975 - depth_factor * 0.060

        # 3. Model 3 (Effective Reach): テイカー攻撃性による到達距離ブースト (+0.00 〜 +0.02)
        aggr_boost = min(max(taker_aggressiveness * 0.020, 0.0), 0.020)

        # 合成比率 (最小 0.910, 最大 0.985)
        final_ratio = min(max(base_ratio + aggr_boost, 0.910), 0.985)

        # 4. 指値価格算出 (四捨五入整数ティック)
        if side == "buy":
            price = best_bid + spread * final_ratio
        else:
            price = best_ask - spread * final_ratio

        return round(price)

    def on_tick(
        self,
        mid_price: float,
        best_bid: float,
        best_ask: float,
        taker_vol_bid: float = 0.0,
        taker_vol_ask: float = 0.0,
        ask_depth_1: float = 0.1,
        bid_depth_1: float = 0.1,
        taker_aggressiveness: float = 0.5,
        cancel_rate: float = 0.0,
        refill_rate: float = 0.0,
        adverse_score: float = 0.0,
        cancel_recommendation: bool = False,
    ) -> Dict[str, Any]:
        """
        1 Tick ごとの戦略評価 & 約定・エグジット判定
        """
        now = time.time()
        self.price_history.append(mid_price)
        if len(self.price_history) > 30:
            self.price_history.pop(0)

        net_taker = taker_vol_ask - taker_vol_bid
        flow_dir = 1.0 if net_taker > 0.05 else (-1.0 if net_taker < -0.05 else 0.0)
        self.flow_history.append(flow_dir)
        if len(self.flow_history) > self.params["flow_window"]:
            self.flow_history.pop(0)

        # -------------------------------------------------------------
        # 1. 指値待機中 (PENDING) の約定・キャンセル判定
        # -------------------------------------------------------------
        if self.pending_order:
            self.pending_order["wait_ticks"] += 1
            p_side = self.pending_order["side"]
            p_price = self.pending_order["price"]

            # 約定判定 (相手気配が自分の指値にタッチしたか)
            filled = False
            if p_side == "buy" and (p_price >= best_ask or best_bid >= p_price):
                filled = True
            elif p_side == "sell" and (p_price <= best_bid or best_ask <= p_price):
                filled = True

            if filled:
                # 約定成功！
                self.position_side = p_side
                self.entry_price = p_price
                self.entry_time = now
                self.peak_price = p_price
                self.peak_mfe_bp = 0.0
                self.be_armed = False
                self.pending_order = None
                return {
                    "action": "fill",
                    "side": p_side,
                    "fill_price": p_price,
                    "reason": f"PEG_V2_FILLED (@¥{p_price:,.0f})",
                }

            # 逆選択スコア急騰または待機タイムアウトによる注文キャンセル
            if adverse_score >= 0.65 or self.pending_order["wait_ticks"] >= self.params["max_wait_ticks"]:
                reason = f"PEG_V2_CANCEL (Wait:{self.pending_order['wait_ticks']}t, Adv:{adverse_score:.2f})"
                self.pending_order = None
                return {"action": "cancel_pending", "reason": reason}

            return {"action": "wait_fill", "reason": f"PEG_V2_WAITING (@¥{p_price:,.0f})"}

        # -------------------------------------------------------------
        # 2. 既存ポジションの防護 ＆ BE5 建値防衛 ＆ エグジット
        # -------------------------------------------------------------
        if self.position_side:
            elapsed = now - self.entry_time
            eval_price = best_bid if self.position_side == "buy" else best_ask

            if self.position_side == "buy":
                if eval_price > self.peak_price:
                    self.peak_price = eval_price
                
                # MFE (含み益 bp)
                mfe_bp = ((self.peak_price - self.entry_price) / self.entry_price) * 10000.0
                self.peak_mfe_bp = max(self.peak_mfe_bp, mfe_bp)

                # BE5 建値防衛アーム発動 (MFE >= 5.0bp)
                if self.peak_mfe_bp >= self.params["be_arm_bp"]:
                    self.be_armed = True

                pnl = (eval_price - self.entry_price) * self.params["order_size_btc"]
                pnl_bp = (pnl / (self.params["order_size_btc"] * self.entry_price)) * 10000.0
                target_price = self.entry_price * (1.0 + self.params["target_bp"] * 0.0001)
                trail_stop_price = self.peak_price * (1.0 - self.params["trail_stop_bp"] * 0.0001)

                # (A) 逆選択退避
                if cancel_recommendation or adverse_score >= 0.70:
                    return {"action": "cancel", "reason": f"ADVERSE_EVACUATE (Adv:{adverse_score:.2f})", "pnl": pnl}

                # (B) BE5 建値防衛エグジット (MFE 5bp到達後、利益が0.2bp以下に反落したら即時微小利確撤退)
                if self.be_armed and pnl_bp <= 0.2:
                    return {"action": "exit", "reason": f"BE5_STOP (MFE:{self.peak_mfe_bp:.1f}bp後 建値防衛)", "pnl": max(0.0, pnl)}

                # (C) ターゲット到達利確 (15bp)
                if eval_price >= target_price:
                    return {"action": "exit", "reason": f"TARGET_15BP_REACHED (+¥{pnl:.1f})", "pnl": pnl}

                # (D) トレーリングストップ (4bp反落)
                if eval_price <= trail_stop_price:
                    return {"action": "exit", "reason": f"TRAIL_STOP_HIT (Peak:¥{self.peak_price:,.0f}, PnL:{pnl_bp:+.1f}bp)", "pnl": pnl}

            else:  # sell ポジション
                if eval_price < self.peak_price:
                    self.peak_price = eval_price

                mfe_bp = ((self.entry_price - self.peak_price) / self.entry_price) * 10000.0
                self.peak_mfe_bp = max(self.peak_mfe_bp, mfe_bp)

                if self.peak_mfe_bp >= self.params["be_arm_bp"]:
                    self.be_armed = True

                pnl = (self.entry_price - eval_price) * self.params["order_size_btc"]
                pnl_bp = (pnl / (self.params["order_size_btc"] * self.entry_price)) * 10000.0
                target_price = self.entry_price * (1.0 - self.params["target_bp"] * 0.0001)
                trail_stop_price = self.peak_price * (1.0 + self.params["trail_stop_bp"] * 0.0001)

                if cancel_recommendation or adverse_score >= 0.70:
                    return {"action": "cancel", "reason": f"ADVERSE_EVACUATE (Adv:{adverse_score:.2f})", "pnl": pnl}

                if self.be_armed and pnl_bp <= 0.2:
                    return {"action": "exit", "reason": f"BE5_STOP (MFE:{self.peak_mfe_bp:.1f}bp後 建値防衛)", "pnl": max(0.0, pnl)}

                if eval_price <= target_price:
                    return {"action": "exit", "reason": f"TARGET_15BP_REACHED (+¥{pnl:.1f})", "pnl": pnl}

                if eval_price >= trail_stop_price:
                    return {"action": "exit", "reason": f"TRAIL_STOP_HIT (Peak:¥{self.peak_price:,.0f}, PnL:{pnl_bp:+.1f}bp)", "pnl": pnl}

            # タイムアウト
            if elapsed >= self.params["max_hold_sec"]:
                return {"action": "exit", "reason": f"TIMEOUT ({elapsed:.0f}s経過)", "pnl": pnl}

            return {"action": "hold", "reason": "POSITION_RUNNING", "current_pnl": pnl}

        # -------------------------------------------------------------
        # 3. 新規エントリー判定 (2bpモメンタム ➔ PEG_v2 指値発注)
        # -------------------------------------------------------------
        if len(self.price_history) < 5 or len(self.flow_history) < 3:
            return {"action": "hold", "reason": "WARMING_UP"}

        start_p = self.price_history[0]
        curr_p = self.price_history[-1]
        mom_bp = ((curr_p - start_p) / start_p) * 10000.0

        flow_sum = sum(self.flow_history)
        noise_ratio = len([f for f in self.flow_history if (f < 0 if mom_bp > 0 else f > 0)]) / len(self.flow_history)

        if adverse_score >= 0.60:
            return {"action": "hold", "reason": f"ADVERSE_VETO (Adv:{adverse_score:.2f})"}

        # 買いシグナル検知 ➔ PEG_v2 指値算出
        if mom_bp >= self.params["micro_mom_bp"] and flow_sum >= 2.0 and noise_ratio <= self.params["reverse_noise_max"]:
            peg_px = self.calculate_peg_v2_price(
                side="buy",
                best_bid=best_bid,
                best_ask=best_ask,
                opp_depth=ask_depth_1,
                taker_aggressiveness=taker_aggressiveness,
                cancel_rate=cancel_rate,
                refill_rate=refill_rate,
            )
            self.pending_order = {
                "side": "buy",
                "price": peg_px,
                "wait_ticks": 0,
                "created_at": now,
            }
            return {
                "action": "post_peg",
                "side": "buy",
                "price": peg_px,
                "reason": f"PEG_V2_BUY_ARM (Mom:{mom_bp:+.1f}bp, PEG_Px:¥{peg_px:,.0f})",
            }

        # 売りシグナル検知 ➔ PEG_v2 指値算出
        elif mom_bp <= -self.params["micro_mom_bp"] and flow_sum <= -2.0 and noise_ratio <= self.params["reverse_noise_max"]:
            peg_px = self.calculate_peg_v2_price(
                side="sell",
                best_bid=best_bid,
                best_ask=best_ask,
                opp_depth=bid_depth_1,
                taker_aggressiveness=taker_aggressiveness,
                cancel_rate=cancel_rate,
                refill_rate=refill_rate,
            )
            self.pending_order = {
                "side": "sell",
                "price": peg_px,
                "wait_ticks": 0,
                "created_at": now,
            }
            return {
                "action": "post_peg",
                "side": "sell",
                "price": peg_px,
                "reason": f"PEG_V2_SELL_ARM (Mom:{mom_bp:+.1f}bp, PEG_Px:¥{peg_px:,.0f})",
            }

        return {"action": "hold", "reason": "WAIT_MOMENTUM_2BP"}

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
            self.peak_price = 0.0
            self.peak_mfe_bp = 0.0
            self.be_armed = False

        elif side in ("buy", "sell"):
            self.position_side = side
            self.entry_price = fill_price
            self.peak_price = fill_price
            self.peak_mfe_bp = 0.0
            self.be_armed = False
            self.entry_time = now

    def get_window_stats(self, hours: float = 1.0) -> Dict[str, Any]:
        """指定ウィンドウ (1h または 24h) の成績を集計"""
        now = time.time()
        cutoff = now - (hours * 3600.0)
        recent = [t for t in self.trades_history if t["ts"] >= cutoff]

        total_t = len(recent)
        win_t = sum(1 for t in recent if t["is_win"])
        loss_t = total_t - win_t
        pnl_jpy = sum(t["pnl_jpy"] for t in recent)
        pnl_bp = sum(t["pnl_bp"] for t in recent)
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
