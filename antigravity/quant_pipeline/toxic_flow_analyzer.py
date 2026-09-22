"""
Toxic Flow Analyzer (トキシック・フロー分析器)
==============================================
CSR-113 / Adverse Agent 改善指示書 v1.0 準拠 (Priority S2)

【目的】
取引所（bitFlyer Lightning）において、トキシック・テイカーに指値が喰われる
板崩壊局面（Toxic Flow）をリアルタイムに検知し、0〜100 の Toxic Score を算出する。

【取得・観測項目】
- imbalance   : 最良板厚不均衡 (Bid - Ask) / (Bid + Ask) [-1.0, 1.0]
- ofi         : Order Flow Imbalance (板変動と約定を統合したフロー不均衡)
- taker_buy   : 直近時間窓の買い成行ボリューム (BTC)
- taker_sell  : 直近時間窓の売り成行ボリューム (BTC)
- cancel_rate : 直近キャンセル率 (0.0 〜 1.0)
- refill_rate : 直近板補充・リフィル率 (0.0 〜 1.0)
- depth_1     : 最良気配板厚 (BTC)
- depth_3     : 上位3本累積板厚 (BTC)
- depth_5     : 上位5本累積板厚 (BTC)

【出力】
- toxic_score : 0 〜 100（研究表示。執行ゲートには使わない）
  - 反対成行 (opp_taker) が無い限り上限 35（cancel/refill 単独で WARNING 以上にしない）
  - 85以上    : TOXIC_CRITICAL
  - 65〜84    : TOXIC_WARNING
  - 40〜64    : TOXIC_MODERATE
  - 40未満    : SAFE_FLOW
"""

import math
from typing import Dict, Any, Optional


class ToxicFlowAnalyzer:
    """
    リアルタイム Toxic Flow 採点エンジン v2
    主信号は反対成行。cancel/refill/depth は補助のみ。
    """

    def __init__(
        self,
        critical_threshold: float = 85.0,
        warning_threshold: float = 65.0,
        min_opp_taker_for_alert: float = 0.01,
        no_taker_score_cap: float = 35.0,
    ):
        self.critical_threshold = critical_threshold
        self.warning_threshold = warning_threshold
        self.min_opp_taker_for_alert = min_opp_taker_for_alert
        self.no_taker_score_cap = no_taker_score_cap
        self.analyzer_version = "toxic_v2"

    def calculate_toxic_score(
        self,
        side: str,  # 評価する指値の向き ("buy" = 買い指値への急落リスク, "sell" = 売り指値への急騰リスク)
        imbalance: float,
        taker_buy: float,
        taker_sell: float,
        cancel_rate: float,
        refill_rate: float,
        depth_1: float,
        depth_3: float = 0.0,
        depth_5: float = 0.0,
        ofi: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        指定された指値サイドに対する Toxic Score (0〜100) を精密算出
        """
        is_buy = (side.lower() == "buy")

        # 1. 不均衡評価 (0〜15点) — 補助
        adverse_imb = -imbalance if is_buy else imbalance
        imb_factor = max(0.0, min(1.0, (adverse_imb + 0.20) / 1.00))
        score_imb = imb_factor * 15.0

        # 2. Taker Flow 急増評価 (0〜55点) — 主信号
        opp_taker = taker_sell if is_buy else taker_buy
        own_taker = taker_buy if is_buy else taker_sell
        tot_taker = opp_taker + own_taker

        if tot_taker > 0:
            taker_ratio = opp_taker / (tot_taker + 1e-6)
        else:
            taker_ratio = 0.0
        # 0.015 BTC ≈ 弱い印、0.08 BTC ≈ 強い印（窓成行の実スケール）
        taker_vol_intensity = min(1.0, opp_taker / 0.08)
        taker_factor = (taker_ratio * 0.45) + (taker_vol_intensity * 0.55)
        if ofi is not None and tot_taker > 0:
            adverse_ofi = -ofi if is_buy else ofi
            ofi_factor = max(0.0, min(1.0, (adverse_ofi + 1.0) / 2.0))
            taker_factor = taker_factor * 0.75 + ofi_factor * 0.25

        score_taker = min(55.0, max(0.0, taker_factor * 55.0))

        # 3. キャンセル急増評価 (0〜12点) — 補助・単独で警報不可
        cancel_factor = max(0.0, min(1.0, (cancel_rate - 0.25) / 0.55))
        score_cancel = cancel_factor * 12.0

        # 4. リフィル消失評価 (0〜10点) — 補助
        refill_factor = max(0.0, min(1.0, (0.25 - refill_rate) / 0.25))
        score_refill = refill_factor * 10.0

        # 5. 板厚・Depth枯渇ペナルティ (0〜8点) — 補助
        depth_penalty = 0.0
        if depth_1 < 0.02:
            depth_penalty = 5.0
        elif depth_1 < 0.05:
            depth_penalty = 2.5
        if depth_3 > 0 and depth_3 < 0.10:
            depth_penalty += 2.5
        depth_penalty = min(8.0, depth_penalty)

        raw_total = score_imb + score_taker + score_cancel + score_refill + depth_penalty
        toxic_score = round(max(0.0, min(100.0, raw_total)), 1)
        capped = False
        if opp_taker < self.min_opp_taker_for_alert and toxic_score > self.no_taker_score_cap:
            toxic_score = self.no_taker_score_cap
            capped = True

        # レベル判定（研究表示。action は執行に接続しない）
        if toxic_score >= self.critical_threshold:
            level = "TOXIC_CRITICAL"
            action = "research_alert"
            description = f"危険水準 (Toxic:{toxic_score}) - 反対成行優勢"
        elif toxic_score >= self.warning_threshold:
            level = "TOXIC_WARNING"
            action = "research_alert"
            description = f"警戒水準 (Toxic:{toxic_score}) - 反対成行増加"
        elif toxic_score >= 40.0:
            level = "TOXIC_MODERATE"
            action = "observe"
            description = f"中立水準 (Toxic:{toxic_score})"
        else:
            level = "SAFE_FLOW"
            action = "observe"
            description = (
                f"安全/未確認 (Toxic:{toxic_score})"
                + (" - 成行なしのため上限キャップ" if capped else "")
            )

        return {
            "side": side,
            "toxic_score": toxic_score,
            "level": level,
            "action": action,
            "description": description,
            "analyzer_version": self.analyzer_version,
            "taker_capped": capped,
            "breakdown": {
                "score_imbalance": round(score_imb, 1),
                "score_taker_flow": round(score_taker, 1),
                "score_cancel": round(score_cancel, 1),
                "score_refill": round(score_refill, 1),
                "score_depth_penalty": round(depth_penalty, 1),
            },
            "metrics": {
                "imbalance": imbalance,
                "adverse_imbalance": adverse_imb,
                "taker_buy": taker_buy,
                "taker_sell": taker_sell,
                "opp_taker": opp_taker,
                "cancel_rate": cancel_rate,
                "refill_rate": refill_rate,
                "depth_1": depth_1,
                "depth_3": depth_3,
                "depth_5": depth_5,
            }
        }
