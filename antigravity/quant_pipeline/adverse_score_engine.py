"""
Adverse Score Engine (統合逆選択スコアリングエンジン)
======================================================
CSR-113 / Adverse Agent 改善指示書 v1.0 最終成果物仕様

【目的】
「どう勝つか」ではなく「どんな時に食われるか」を統計的に解剖し、
全戦略（Trend / MM / MeanRev / Scalp）が通過する最上位ゲートキーパーとして
0〜100 の統合「Adverse Score」を算出する。

【Adverse Score 構成比率】
- 30% : AE (Adverse Excursion - 約定後逆行)
- 25% : Toxic Flow (板崩壊・トキシック成行)
- 20% : Capture Loss (スプレッド取りこぼし率)
- 15% : Latency (取引所RTT・受信遅延)
- 10% : Inventory (在庫滞留リスク・拘束時間)

【判定区分】
- 0 〜 30  : 🟢 安全 (SAFE)       - フル稼働
- 30 〜 60 : 🟡 注意 (CAUTION)    - 厳格スプレッドフィルター適用
- 60 〜 80 : 🟠 危険 (WARNING)    - ロット半減・逆張り指値自重
- 80 〜 100: 🔴 発注禁止 (VETO)   - 新規発注完全遮断 ＆ 指値緊急退避
"""

import os
import json
import time
from typing import Dict, Any, Optional
from datetime import datetime


class AdverseScoreEngine:
    """
    5大要素統合 Adverse Score 算出エンジン
    """

    def __init__(
        self,
        save_dir: str = "/home/azureuser/antigravity/data",
    ):
        self.save_dir = save_dir
        self.state_file = os.path.join(self.save_dir, "adverse_score_state.json")
        self._last_state_write = 0.0
        os.makedirs(self.save_dir, exist_ok=True)

    def calculate_adverse_score(
        self,
        ae_1s: Optional[float],
        ae_3s: Optional[float],
        mae_bp: Optional[float],
        toxic_score: float,
        capture_rate_pct: Optional[float],
        latency_ms: float,
        inventory_btc: float,
        holding_time_sec: float,
        micro_dev: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Adverse Score (0〜100) を5大要素から合成算出
        """
        # -------------------------------------------------------------
        # 1. AE スコア (30%)
        # -------------------------------------------------------------
        # ae_1s / ae_3s / mae_bp の悪化度合
        if ae_1s is not None:
            # 1秒後逆行: 0bp以上=0点, -1.5bp=50点, -3.0bp=80点, -5.0bp以下=100点
            raw_ae_loss = -ae_1s if ae_1s < 0 else 0.0
            score_ae = min(100.0, raw_ae_loss * 25.0)
            if ae_3s is not None and ae_3s < ae_1s:
                # 3秒後も損失拡大している場合は加点
                score_ae = min(100.0, score_ae + 15.0)
        elif abs(micro_dev) > 0:
            # 約定履歴がない場合は micro_price 乖離から暫定推定
            score_ae = min(80.0, abs(micro_dev) / 20.0)
        else:
            score_ae = 15.0  # デフォルト平常ノイズ

        # -------------------------------------------------------------
        # 2. Toxic Flow スコア (25%)
        # -------------------------------------------------------------
        # ToxicFlowAnalyzer の出力 (0〜100)
        score_toxic = max(0.0, min(100.0, float(toxic_score)))

        # -------------------------------------------------------------
        # 3. Capture Loss スコア (20%)
        # -------------------------------------------------------------
        # capture_rate (80%以上=0点, 50-80%=35点, 0-50%=70点, <0%=100点)
        if capture_rate_pct is not None:
            if capture_rate_pct >= 80.0:
                score_capture = 0.0
            elif capture_rate_pct >= 50.0:
                score_capture = (80.0 - capture_rate_pct) / 30.0 * 40.0  # 0〜40点
            elif capture_rate_pct >= 0.0:
                score_capture = 40.0 + (50.0 - capture_rate_pct) / 50.0 * 35.0  # 40〜75点
            else:
                score_capture = 100.0  # スプレッド負け
        else:
            score_capture = 20.0  # 初期値

        # -------------------------------------------------------------
        # 4. Latency スコア (15%)
        # -------------------------------------------------------------
        # <50ms=0点, 50-80ms=0〜40点, 80-120ms=40〜80点, >=120ms=100点
        if latency_ms < 50.0:
            score_latency = 0.0
        elif latency_ms < 80.0:
            score_latency = (latency_ms - 50.0) / 30.0 * 40.0
        elif latency_ms < 120.0:
            score_latency = 40.0 + (latency_ms - 80.0) / 40.0 * 40.0
        else:
            score_latency = 100.0

        # -------------------------------------------------------------
        # 5. Inventory スコア (10%)
        # -------------------------------------------------------------
        # 在庫あり ＋ 拘束時間で評価
        if abs(inventory_btc) < 1e-5:
            score_inventory = 0.0  # FLAT
        else:
            if holding_time_sec < 30.0:
                score_inventory = 20.0
            elif holding_time_sec < 120.0:
                score_inventory = 45.0
            elif holding_time_sec < 300.0:
                score_inventory = 70.0
            else:
                score_inventory = 100.0  # 5分以上スタック

        # -------------------------------------------------------------
        # 統合 Adverse Score (0〜100)
        # -------------------------------------------------------------
        total_score = (
            score_ae * 0.30
            + score_toxic * 0.25
            + score_capture * 0.20
            + score_latency * 0.15
            + score_inventory * 0.10
        )
        total_score = round(max(0.0, min(100.0, total_score)), 1)

        # 判定区分
        if total_score >= 80.0:
            tier = "発注禁止"
            tier_code = "HARD_VETO"
            action = "cancel_and_veto"
            color = 0xE74C3C  # 赤
            directive = "🔴 [発注禁止] 逆選択リスク極大 - 新規エントリー完全遮断＆指値緊急退避"
        elif total_score >= 60.0:
            tier = "危険"
            tier_code = "WARNING"
            action = "halve_size_and_caution"
            color = 0xE67E22  # オレンジ
            directive = "🟠 [危険] 逆選択高警戒 - ロット半減・逆張り指値見送り"
        elif total_score >= 30.0:
            tier = "注意"
            tier_code = "CAUTION"
            action = "strict_filter"
            color = 0xF1C40F  # 黄
            directive = "🟡 [注意] 逆選択兆候あり - スプレッド厳格フィルター適用"
        else:
            tier = "安全"
            tier_code = "SAFE"
            action = "allow"
            color = 0x2ECC71  # 緑
            directive = "🟢 [安全] 逆選択リスク極小 - 通常稼働許可"

        result = {
            "timestamp": int(time.time() * 1000),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "adverse_score": total_score,
            "tier": tier,
            "tier_code": tier_code,
            "action": action,
            "directive": directive,
            "color": color,
            "weights": {
                "ae_weight": 0.30,
                "toxic_weight": 0.25,
                "capture_weight": 0.20,
                "latency_weight": 0.15,
                "inventory_weight": 0.10,
            },
            "breakdown": {
                "score_ae": round(score_ae, 1),
                "weighted_ae": round(score_ae * 0.30, 1),
                "score_toxic": round(score_toxic, 1),
                "weighted_toxic": round(score_toxic * 0.25, 1),
                "score_capture": round(score_capture, 1),
                "weighted_capture": round(score_capture * 0.20, 1),
                "score_latency": round(score_latency, 1),
                "weighted_latency": round(score_latency * 0.15, 1),
                "score_inventory": round(score_inventory, 1),
                "weighted_inventory": round(score_inventory * 0.10, 1),
            },
            "metrics": {
                "ae_1s": ae_1s,
                "ae_3s": ae_3s,
                "mae_bp": mae_bp,
                "toxic_score": toxic_score,
                "capture_rate_pct": capture_rate_pct,
                "latency_ms": latency_ms,
                "inventory_btc": inventory_btc,
                "holding_time_sec": holding_time_sec,
            },
        }

        self._persist_state(result)
        return result

    def _persist_state(self, state_data: Dict[str, Any]):
        now = time.time()
        if now - self._last_state_write < 1.0:
            return
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.state_file)
            self._last_state_write = now
        except Exception:
            pass
