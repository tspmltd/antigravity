from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class BaseTickStrategy(ABC):
    """
    ティック駆動型（Tick-Driven / Event-Driven）戦略の基底クラス。
    ローソク足を待たず、リアルタイム約定イベント（Tick）とTaker差分量に基づいて即座にアクションを返却する。
    """

    def __init__(self, name: str, version: str = "v1.0", parameters: Optional[Dict[str, Any]] = None):
        self.name = name
        self.version = version
        self.parameters = parameters or {}
        self.strategy_type = "tick"

    @abstractmethod
    def on_tick(
        self,
        tick: Dict[str, Any],
        flow_stats: Dict[str, Any],
        current_pos: float,
        entry_price: float,
    ) -> Dict[str, Any]:
        """
        最新ティックおよびTakerフロー統計を受け取り、実行アクションを判定。
        
        Returns:
            Dict containing:
                - action: 'BUY', 'SELL', 'EXIT', 'CANCEL', 'REFILL', 'HOLD'
                - reason: アクションの発生要因
                - target_price: 指値・目標価格 (成行の場合は None)
        """
        pass


class MicroTrendTickStrategy(BaseTickStrategy):
    """
    【マイクロトレンド・ティック戦略 (高回転・早期先回り対応版)】
    ユーザーの洞察：
    - 「2bp程度の小さなトレンドフォローの積み重ねが10〜20bpのトレンドになる」
    - 「taker差分量が一定同方向に継続してあるか逆方向のうごｋがすくないか、cancel,refillもだいｊ」
    - 取引頻度向上：1.2bp〜1.5bpの微小初動や先行大口Takerフローを敏感に検知して早期順張り。
    - 多段階利確：初動モメンタム失速時は+3.5bpで即利確して資金回転、強いトレンド時は12〜20bpの大波を追随。
    """

    def __init__(self, parameters: Optional[Dict[str, Any]] = None, **kwargs):
        default_params = {
            "initial_momentum_bp": 1.2,      # 1.2bp 初動トリガー (従来の2.0bpから高感度化)
            "min_delta_ratio": 0.35,         # Taker買い優勢基準 (過半数・35%以上の差分)
            "target_trend_bp": 12.0,         # 12bp〜20bp 大波利確目標
            "micro_tp_bp": 3.5,              # 3.5bp 初動後の失速時早期利確
            "trail_step_bp": 3.0,            # トレーリングステップ
            "stop_loss_bp": 4.5,             # 逆行防衛ストップ (4.5bp)
            "reverse_flow_exit_ratio": 0.35  # 逆方向Deltaが35%を超えたら即エグジット
        }
        if parameters:
            default_params.update(parameters)
        if kwargs:
            if "trigger_bp" in kwargs:
                default_params["initial_momentum_bp"] = kwargs["trigger_bp"]
            if "cancel_reverse_threshold" in kwargs:
                default_params["reverse_flow_exit_ratio"] = abs(kwargs["cancel_reverse_threshold"])
            default_params.update(kwargs)
        super().__init__(name="MicroTrendTick", version="v2.0", parameters=default_params)
        self.strategy_type = "micro_trend"

    def on_tick(
        self,
        tick: Dict[str, Any],
        flow_stats: Dict[str, Any],
        current_pos: float,
        entry_price: float,
    ) -> Dict[str, Any]:
        curr_p = tick["price"]
        delta_ratio = flow_stats.get("delta_ratio", 0.0)

        # --------------------------------------------------------
        # 1. ポジション保有中の管理 (大波追随 ＆ モメンタム失速早期利確 ＆ 逆流Cancel)
        # --------------------------------------------------------
        if current_pos != 0 and entry_price > 0:
            is_long = current_pos > 0
            ret_bp = ((curr_p - entry_price) / entry_price * 10000.0) * (1.0 if is_long else -1.0)

            # ① 10bp〜20bp 大波到達利確 (大トレンド獲得)
            if ret_bp >= self.parameters["target_trend_bp"]:
                return {
                    "action": "EXIT",
                    "reason": f"10-20bp大波利確達成 (+{ret_bp:.1f}bp)",
                    "target_price": None
                }

            # ② 初動モメンタム失速時の早期マイクロ利確 (+3.5bp以上 かつ フロー沈静化)
            if ret_bp >= self.parameters["micro_tp_bp"]:
                is_stalled = (is_long and delta_ratio <= 0.10) or (not is_long and delta_ratio >= -0.10)
                if is_stalled:
                    return {
                        "action": "EXIT",
                        "reason": f"初動モメンタム失速・早期マイクロ利確 (+{ret_bp:.1f}bp, Delta:{delta_ratio:+.2f})",
                        "target_price": None
                    }

            # ③ 逆方向Taker急増による即時Cancel / 脱出 (Adverse Selection防止)
            rev_exit = self.parameters["reverse_flow_exit_ratio"]
            if is_long:
                if flow_stats.get("should_cancel_bid") or delta_ratio <= -rev_exit:
                    return {
                        "action": "CANCEL",
                        "reason": f"売りTaker急増・即時Cancel脱出 (Delta:{delta_ratio:.2f}, PnL:{ret_bp:+.1f}bp)",
                        "target_price": None
                    }
            else:
                if flow_stats.get("should_cancel_ask") or delta_ratio >= rev_exit:
                    return {
                        "action": "CANCEL",
                        "reason": f"買いTaker急増・即時Cancel脱出 (Delta:{delta_ratio:.2f}, PnL:{ret_bp:+.1f}bp)",
                        "target_price": None
                    }

            # ④ 防衛ストップ
            if ret_bp <= -self.parameters["stop_loss_bp"]:
                return {
                    "action": "EXIT",
                    "reason": f"防衛ストップ損切 ({ret_bp:.1f}bp)",
                    "target_price": None
                }

            return {"action": "HOLD", "reason": f"トレンド追随中 ({ret_bp:+.1f}bp)", "target_price": None}

        # --------------------------------------------------------
        # 2. ノーポジション時の高感度・早期初動エントリー
        # --------------------------------------------------------
        if current_pos == 0:
            min_bp = self.parameters["initial_momentum_bp"]
            min_delta = self.parameters["min_delta_ratio"]
            p_change = flow_stats.get("price_change_bp", 0.0)

            # 買い初動条件:
            # A: 微小初動 (価格+1.2bp & Delta+0.35)
            # B: Taker圧倒先行型 (価格+0.8bp & Delta+0.60)
            # C: 従来2bp強モメンタム
            is_up = (
                flow_stats.get("is_micro_momentum_up")
                or flow_stats.get("is_2bp_momentum_up")
                or (p_change >= min_bp and delta_ratio >= min_delta)
                or (p_change >= 0.8 and delta_ratio >= 0.60)
            )

            # 売り初動条件:
            is_down = (
                flow_stats.get("is_micro_momentum_down")
                or flow_stats.get("is_2bp_momentum_down")
                or (p_change <= -min_bp and delta_ratio <= -min_delta)
                or (p_change <= -0.8 and delta_ratio <= -0.60)
            )

            if is_up:
                return {
                    "action": "BUY",
                    "reason": f"2bp初動順張り (Change:{p_change:+.1f}bp, Delta:{delta_ratio:+.2f})",
                    "target_price": None
                }
            elif is_down:
                return {
                    "action": "SELL",
                    "reason": f"2bp初動順張り (Change:{p_change:+.1f}bp, Delta:{delta_ratio:+.2f})",
                    "target_price": None
                }

        return {"action": "HOLD", "reason": "待機中 (初動シグナル待ち)", "target_price": None}


class InventorySkewTickMMStrategy(BaseTickStrategy):
    """
    【ティック駆動型 在庫スキューMM戦略 (高回転スプレッド回収版)】
    - Makerとしてタイトなスプレッドで指値を提示
    - 2.5bp反発でサクサク利確して資金回転を最大化
    - 逆方向Taker急増時にミリ秒単位で指値をCANCELして貫通防止
    """

    def __init__(self, parameters: Optional[Dict[str, Any]] = None, **kwargs):
        default_params = {
            "half_spread_bp": 1.2,       # 1.2bp (片側1.2bp / 約1,400円幅で約定しやすく設定)
            "micro_tp_bp": 2.5,          # 2.5bp 反発で即利確 (従来の5.0bpから回転率倍増)
            "stop_loss_bp": 5.0,         # 5.0bp タイト損切
            "skew_factor": 1.5           # Takerフロー方向へのスキュー偏向度
        }
        if parameters:
            default_params.update(parameters)
        if kwargs:
            if "spread_bp" in kwargs:
                default_params["half_spread_bp"] = kwargs["spread_bp"] / 2.0
            if "cancel_reverse_threshold" in kwargs:
                default_params["reverse_flow_exit_ratio"] = abs(kwargs["cancel_reverse_threshold"])
            default_params.update(kwargs)
        super().__init__(name="InventorySkewTickMM", version="v2.0", parameters=default_params)
        self.strategy_type = "market_making"

    def on_tick(
        self,
        tick: Dict[str, Any],
        flow_stats: Dict[str, Any],
        current_pos: float,
        entry_price: float,
    ) -> Dict[str, Any]:
        curr_p = tick["price"]
        delta_ratio = flow_stats.get("delta_ratio", 0.0)

        # ポジション保有中の決済
        if current_pos != 0 and entry_price > 0:
            is_long = current_pos > 0
            ret_bp = ((curr_p - entry_price) / entry_price * 10000.0) * (1.0 if is_long else -1.0)

            # 逆方向Taker殺到時は即時CANCEL脱出
            rev_exit = self.parameters.get("reverse_flow_exit_ratio", 0.55)
            if is_long and (flow_stats.get("should_cancel_bid") or delta_ratio <= -rev_exit):
                return {"action": "CANCEL", "reason": f"MM逆流Cancel脱出 (Delta:{delta_ratio:.2f})", "target_price": None}
            if not is_long and (flow_stats.get("should_cancel_ask") or delta_ratio >= rev_exit):
                return {"action": "CANCEL", "reason": f"MM逆流Cancel脱出 (Delta:{delta_ratio:.2f})", "target_price": None}

            # 微小反発利確 (2.5bpで素早く回転)
            if ret_bp >= self.parameters["micro_tp_bp"]:
                return {"action": "EXIT", "reason": f"MM高回転利確 (+{ret_bp:.1f}bp)", "target_price": None}

            # タイト損切
            if ret_bp <= -self.parameters["stop_loss_bp"]:
                return {"action": "EXIT", "reason": f"MMタイト損切 ({ret_bp:.1f}bp)", "target_price": None}

            return {"action": "HOLD", "reason": f"MMスプレッド獲得待機 ({ret_bp:+.1f}bp)", "target_price": None}

        # ノーポジション時: フロー安定ならREFILL（指値提示）
        if current_pos == 0:
            # 売り圧殺到でなければ買い指値REFILL
            if not flow_stats.get("should_cancel_bid") and delta_ratio > -0.25:
                return {
                    "action": "BUY",
                    "reason": "MM買い指値REFILL (スプレッド確保)",
                    "target_price": curr_p * (1 - self.parameters["half_spread_bp"] / 10000.0)
                }
            elif not flow_stats.get("should_cancel_ask") and delta_ratio < 0.25:
                return {
                    "action": "SELL",
                    "reason": "MM売り指値REFILL (スプレッド確保)",
                    "target_price": curr_p * (1 + self.parameters["half_spread_bp"] / 10000.0)
                }

        return {"action": "HOLD", "reason": "MM待機", "target_price": None}


class OrderFlowScalpTickStrategy(BaseTickStrategy):
    """
    【オーダーフロー・インバランス 高頻度スキャルピング戦略】
    - 直近のTaker差分量（Delta）の瞬間的な歪み・インバランスに即座に飛び乗る
    - +1.8bp〜+2.5bpの微小利益を毎分単位で高回転に抜き取る超高頻度戦略
    - 逆行時はミリ秒で即座に脱出
    """

    def __init__(self, parameters: Optional[Dict[str, Any]] = None, **kwargs):
        default_params = {
            "imbalance_threshold": 0.45,   # Taker Delta比率 45%以上の不均衡で発火
            "target_tp_bp": 2.0,           # 2.0bp 瞬時利確
            "stop_loss_bp": 3.5,           # 3.5bp 超タイト損切
            "cancel_reverse_ratio": 0.35   # 逆流35%で即脱出
        }
        if parameters:
            default_params.update(parameters)
        if kwargs:
            default_params.update(kwargs)
        super().__init__(name="OrderFlowScalpTick", version="v1.0", parameters=default_params)
        self.strategy_type = "scalping"

    def on_tick(
        self,
        tick: Dict[str, Any],
        flow_stats: Dict[str, Any],
        current_pos: float,
        entry_price: float,
    ) -> Dict[str, Any]:
        curr_p = tick["price"]
        delta_ratio = flow_stats.get("delta_ratio", 0.0)

        # ポジション保有中
        if current_pos != 0 and entry_price > 0:
            is_long = current_pos > 0
            ret_bp = ((curr_p - entry_price) / entry_price * 10000.0) * (1.0 if is_long else -1.0)

            # 瞬時スキャルプ利確 (+2.0bp)
            if ret_bp >= self.parameters["target_tp_bp"]:
                return {"action": "EXIT", "reason": f"スキャルプ瞬時利確 (+{ret_bp:.1f}bp)", "target_price": None}

            # 逆流脱出
            rev_exit = self.parameters["cancel_reverse_ratio"]
            if is_long and delta_ratio <= -rev_exit:
                return {"action": "CANCEL", "reason": f"スキャルプ逆流即脱出 (Delta:{delta_ratio:+.2f})", "target_price": None}
            if not is_long and delta_ratio >= rev_exit:
                return {"action": "CANCEL", "reason": f"スキャルプ逆流即脱出 (Delta:{delta_ratio:+.2f})", "target_price": None}

            # タイトストップ
            if ret_bp <= -self.parameters["stop_loss_bp"]:
                return {"action": "EXIT", "reason": f"スキャルプ防衛損切 ({ret_bp:.1f}bp)", "target_price": None}

            return {"action": "HOLD", "reason": f"スキャルプ保有中 ({ret_bp:+.1f}bp)", "target_price": None}

        # ノーポジション時: Taker不均衡検知で瞬時エントリー
        if current_pos == 0:
            imb = self.parameters["imbalance_threshold"]
            if delta_ratio >= imb:
                return {
                    "action": "BUY",
                    "reason": f"Taker買いインバランス検知 (Delta:{delta_ratio:+.2f})",
                    "target_price": None
                }
            elif delta_ratio <= -imb:
                return {
                    "action": "SELL",
                    "reason": f"Taker売りインバランス検知 (Delta:{delta_ratio:+.2f})",
                    "target_price": None
                }

        return {"action": "HOLD", "reason": "スキャルプ待機", "target_price": None}
