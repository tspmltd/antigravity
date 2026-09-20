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
- toxic_score : 0 〜 100
  - 85以上    : 🚨 TOXIC_CRITICAL (即時指値退避 / 発注完全拒否)
  - 65〜84    : ⚠️ TOXIC_WARNING  (警戒・サイズ半減 / 逆張り指値見送り)
  - 40〜64    : ⚖️ TOXIC_MODERATE (通常流動性・ノイズ)
  - 40未満    : 🟢 SAFE_FLOW      (安全・Maker優位)

【危険例の再現】
imbalance: -0.8, taker_buy急増 (Sell指値直撃時) or taker_sell急増 (Buy指値直撃時),
cancel急増, refill消失 ➔ Toxic Score: 92前後
"""

import math
from typing import Dict, Any, Optional


class ToxicFlowAnalyzer:
    """
    リアルタイム Toxic Flow 採点エンジン
    """

    def __init__(
        self,
        critical_threshold: float = 85.0,
        warning_threshold: float = 65.0,
    ):
        self.critical_threshold = critical_threshold
        self.warning_threshold = warning_threshold

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
        
        # 1. 不均衡評価 (0〜22点)
        adverse_imb = -imbalance if is_buy else imbalance
        imb_factor = max(0.0, min(1.0, (adverse_imb + 0.20) / 1.00))
        score_imb = imb_factor * 22.0

        # 2. Taker Flow 急増評価 (0〜28点)
        opp_taker = taker_sell if is_buy else taker_buy
        own_taker = taker_buy if is_buy else taker_sell
        tot_taker = opp_taker + own_taker

        taker_ratio = (opp_taker / (tot_taker + 1e-6)) if tot_taker > 0 else 0.5
        taker_vol_intensity = min(1.0, opp_taker / 0.08)
        taker_factor = (taker_ratio * 0.6) + (taker_vol_intensity * 0.4)
        if ofi is not None:
            adverse_ofi = -ofi if is_buy else ofi
            ofi_factor = max(0.0, min(1.0, (adverse_ofi + 1.0) / 2.0))
            taker_factor = taker_factor * 0.7 + ofi_factor * 0.3

        score_taker = min(28.0, max(0.0, (taker_factor - 0.20) / 0.70 * 28.0))

        # 3. キャンセル急増評価 (0〜22点)
        cancel_factor = max(0.0, min(1.0, (cancel_rate - 0.15) / 0.50))
        score_cancel = cancel_factor * 22.0

        # 4. リフィル消失評価 (0〜18点)
        refill_factor = max(0.0, min(1.0, (0.35 - refill_rate) / 0.30))
        score_refill = refill_factor * 18.0

        # 5. 板厚・Depth枯渇ペナルティ (0〜6点)
        depth_penalty = 0.0
        if depth_1 < 0.02:
            depth_penalty = 4.0
        elif depth_1 < 0.05:
            depth_penalty = 2.0
        if depth_3 > 0 and depth_3 < 0.10:
            depth_penalty += 2.0

        # 合計スコア (0〜100にクリッピング)
        raw_total = score_imb + score_taker + score_cancel + score_refill + depth_penalty
        toxic_score = round(max(0.0, min(100.0, raw_total)), 1)

        # レベル判定
        if toxic_score >= self.critical_threshold:
            level = "TOXIC_CRITICAL"
            action = "cancel"
            description = f"🚨 危険水準 (Toxic:{toxic_score}) - トキシック直撃・即時退避"
        elif toxic_score >= self.warning_threshold:
            level = "TOXIC_WARNING"
            action = "veto"
            description = f"⚠️ 警戒水準 (Toxic:{toxic_score}) - 逆選択リスク高・発注自重"
        elif toxic_score >= 40.0:
            level = "TOXIC_MODERATE"
            action = "caution"
            description = f"⚖️ 中立水準 (Toxic:{toxic_score}) - 通常ボラティリティ"
        else:
            level = "SAFE_FLOW"
            action = "allow"
            description = f"🟢 安全水準 (Toxic:{toxic_score}) - メーカー指値有利"

        return {
            "side": side,
            "toxic_score": toxic_score,
            "level": level,
            "action": action,
            "description": description,
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
