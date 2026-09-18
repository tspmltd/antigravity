"""
news_pipeline/pts_causal_engine.py: PTS x TDnet 因果AI (特徴量テーブル x LightGBM推論 x ルールベースCIS2)
===================================================================================================
- 特徴量テーブル設計 (6カテゴリ・25特徴量):
    1. 開示特徴量 (種別, 方向, 強度, 不祥事, 決算)
    2. PTS特徴量 (変動率, 出来高比, 継続時間, 板厚比, 約定回数比, スプレッド比, 方向一貫性)
    3. 時系列特徴量 (時間差 delta_minutes, 時間帯)
    4. テーマ特徴量 (半導体, AI, 自動車, 銀行, セクター波及)
    5. メタ情報 (時価総額順位, 大型株フラグ, ボラティリティ)
    6. 教師ラベル (DIRECT, PARTIAL, NONE)
- ハイブリッド推論:
    ・LightGBMモデル存在時: P(DIRECT), P(PARTIAL), P(NONE) 確率推定
    ・フォールバック時: Causal Impact Score 2.0 (CIS2) によるルールベース高精度判定
- 学習データ自動蓄積 (data/pts_causal_dataset.jsonl) & 自動再学習 (train_model)
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List
from dataclasses import dataclass, asdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

logger = logging.getLogger("news_pipeline.pts_causal_engine")
JST = timezone(timedelta(hours=9))


@dataclass
class PTSFeatureRecord:
    """PTS x 開示 特徴量レコード"""
    symbol: str
    name: str

    # 1. 開示特徴量
    disclosure_type: str = "その他"       # 決算 / 業績修正 / 不祥事 / M&A / 資金調達 / その他
    disclosure_direction: str = "neutral"  # positive / negative / neutral
    disclosure_strength: float = 0.0       # 乖離率・修正率など
    disclosure_is_critical: int = 0        # 不祥事・内部統制不備
    disclosure_is_financial: int = 0       # 決算・業績修正
    disclosure_time_minutes: int = 0       # 開示時刻 (0〜1439分)

    # 2. PTS特徴量
    pts_change_pct: float = 0.0            # 変動率 (%)
    pts_volume_ratio: float = 1.0          # 出来高 / 30日平均
    pts_sustained_minutes: int = 5         # 急変継続時間 (分)
    pts_board_thickness_ratio: float = 1.0 # 板厚比
    pts_trade_count_ratio: float = 1.0     # 約定回数比
    pts_spread_ratio: float = 1.0          # スプレッド比 (1.0以下で縮小)
    pts_direction_consistency: float = 0.8 # 方向一貫性 (0〜1)
    pts_volume_derivative: float = 0.0     # 出来高加速度
    pts_time_minutes: int = 0              # PTS急変時刻 (0〜1439分)

    # 3. 時系列特徴量
    delta_minutes: int = 0                 # 開示→PTS急変までの時間差 (分)
    time_band: str = "17-18"               # "17-18", "18-21", "21-24"
    is_after_hours_peak: int = 1           # 17〜18時なら1

    # 4. テーマ特徴量
    sector: str = "一般"
    theme_ai: int = 0
    theme_china: int = 0
    theme_macro: int = 0
    theme_sector_moving: int = 0

    # 5. メタ情報
    market_cap_rank: int = 500
    is_large_cap: int = 0
    volatility_30d: float = 0.2
    avg_pts_volume_30d: float = 5000.0

    # 6. 教師ラベル
    causal_label: str = "NONE"             # DIRECT / PARTIAL / NONE
    causal_score: float = 0.0              # CIS2 スコア


class PTSCausalEngine:
    """PTS x TDnet 因果判定エンジン (LightGBM & CIS2)"""

    def __init__(self, model_path: Optional[str] = None, dataset_path: Optional[str] = None):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.model_path = model_path or os.path.join(base_dir, "data", "pts_causal_lgb_model.txt")
        self.dataset_path = dataset_path or os.path.join(base_dir, "data", "pts_causal_dataset.jsonl")
        os.makedirs(os.path.dirname(self.dataset_path), exist_ok=True)

        self.model = None
        self._load_model_if_exists()

    def _load_model_if_exists(self):
        """学習済みLightGBMモデルをロード"""
        if os.path.exists(self.model_path):
            try:
                import lightgbm as lgb
                self.model = lgb.Booster(model_file=self.model_path)
                logger.info(f"[PTSCausalEngine] 🤖 LightGBM因果判定モデルをロードしました: {self.model_path}")
            except Exception as e:
                logger.warning(f"[PTSCausalEngine] LightGBMモデル読込失敗 (ルールベースへフォールバック): {e}")

    def calculate_cis2(self, r: PTSFeatureRecord) -> float:
        """
        Causal Impact Score 2.0 (CIS2: ルールベース因果スコア 0〜200)
        """
        score = 0.0

        # 1. 開示の意味 (NLP)
        if r.disclosure_is_critical:
            score += 50.0
        elif r.disclosure_is_financial:
            score += 40.0
        elif r.disclosure_type in ("M&A", "買収"):
            score += 35.0
        elif r.disclosure_type in ("資金調達", "自社株買い"):
            score += 25.0
        elif r.disclosure_type != "その他":
            score += 15.0

        # 強度加算 (乖離率・修正率)
        if r.disclosure_strength >= 20.0:
            score += 20.0
        elif r.disclosure_strength >= 10.0:
            score += 10.0

        # 2. PTSの質 (時系列・板)
        if abs(r.pts_change_pct) >= 5.0:
            score += 25.0
        elif abs(r.pts_change_pct) >= 3.0:
            score += 15.0

        if r.pts_volume_ratio >= 10.0:
            score += 30.0
        elif r.pts_volume_ratio >= 5.0:
            score += 20.0
        elif r.pts_volume_ratio >= 3.0:
            score += 10.0

        if r.pts_sustained_minutes >= 15:
            score += 15.0
        if r.pts_board_thickness_ratio >= 2.0:
            score += 10.0
        if r.pts_spread_ratio <= 0.8:
            score += 10.0

        # 3. 時間的因果
        if r.delta_minutes <= 30:
            score += 40.0
        elif r.delta_minutes <= 120:
            score += 25.0
        elif r.delta_minutes <= 240:
            score += 10.0
        else:
            score += 5.0

        # 4. 方向一致
        is_pos_match = (r.disclosure_direction == "positive" and r.pts_change_pct > 0)
        is_neg_match = (r.disclosure_direction == "negative" and r.pts_change_pct < 0)
        if is_pos_match or is_neg_match:
            score += 30.0
        elif r.disclosure_direction != "neutral":
            score -= 20.0

        # 5. テーマ・メタ一致
        if r.theme_sector_moving:
            score += 15.0
        if r.is_large_cap:
            score += 10.0

        return max(0.0, score)

    def predict_causality(self, record: PTSFeatureRecord) -> Tuple[str, float, Dict[str, float]]:
        """
        因果関係を判定 (DIRECT / PARTIAL / NONE)
        Returns:
            (label: str, confidence_or_score: float, probs: Dict[str, float])
        """
        # ルールベースCIS2スコア計算
        cis2 = self.calculate_cis2(record)
        record.causal_score = cis2

        # LightGBMモデルによるML推論 (利用可能な場合)
        if self.model is not None:
            try:
                features = [
                    record.disclosure_strength,
                    record.disclosure_is_critical,
                    record.disclosure_is_financial,
                    record.disclosure_time_minutes,
                    record.pts_change_pct,
                    record.pts_volume_ratio,
                    record.pts_sustained_minutes,
                    record.pts_board_thickness_ratio,
                    record.pts_trade_count_ratio,
                    record.pts_spread_ratio,
                    record.pts_direction_consistency,
                    record.delta_minutes,
                    record.is_after_hours_peak,
                    record.theme_ai,
                    record.theme_sector_moving,
                    record.is_large_cap,
                    record.volatility_30d,
                ]
                probs = self.model.predict([features])[0]
                # classes: 0: DIRECT, 1: PARTIAL, 2: NONE
                p_direct, p_partial, p_none = float(probs[0]), float(probs[1]), float(probs[2])
                prob_dict = {"DIRECT": p_direct, "PARTIAL": p_partial, "NONE": p_none}

                if p_direct >= 0.70:
                    label = "DIRECT"
                    conf = p_direct
                elif (p_direct + p_partial) >= 0.70:
                    label = "PARTIAL"
                    conf = p_partial
                else:
                    label = "NONE"
                    conf = p_none

                record.causal_label = label
                return label, conf, prob_dict
            except Exception as e:
                logger.warning(f"[PTSCausalEngine] LightGBM推論エラー (CIS2へフォールバック): {e}")

        # フォールバック: CIS2 ルールベース判定
        if cis2 >= 120.0:
            label = "DIRECT"
        elif cis2 >= 80.0:
            label = "PARTIAL"
        elif cis2 >= 50.0:
            label = "WEAK"
        else:
            label = "NONE"

        record.causal_label = label
        prob_dict = {"CIS2_SCORE": cis2}
        return label, cis2, prob_dict

    def save_sample_for_training(self, record: PTSFeatureRecord):
        """学習データセットに1レコード追記保存"""
        try:
            row = asdict(record)
            row["saved_at"] = datetime.now(JST).isoformat()
            with open(self.dataset_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[PTSCausalEngine] 教師データ保存失敗: {e}")

    def format_causal_reason(self, label: str, disclosure_title: Optional[str] = None) -> str:
        """判定結果から投稿用の要因テキストを生成"""
        disc_str = f"（{disclosure_title[:20]}）" if disclosure_title else ""
        if label == "DIRECT":
            return f"TDnet開示連動{disc_str}が主因"
        elif label == "PARTIAL":
            return f"TDnet開示{disc_str}および市場思惑"
        elif label == "WEAK":
            return f"材料観測・思惑（開示との連動は軽微）"
        else:
            return "市場需給・材料観測（開示連動なし）"


# シングルトンインスタンス
default_pts_causal_engine = PTSCausalEngine()
