"""
antigravity/multi_asset/alpha_opportunity_engine.py: 第4階層 Alpha Opportunity Engine
====================================================================================
役割: 「市場がまだ織り込んでいない情報 (Unpriced Alpha)」を探し、自律的に収益化する。

データフロー:
  EDINET / TDnet / PTS / 世界株価 / ニュース / SNS
    ↓
  NEWS Agent (センチネル)
    ↓
  MIS 危険判定 (ブレーキ) & OAS 機会判定 (アクセル)
    ↓
  Alpha Opportunity Engine (第4階層)
    ├── Forecast Agent (未織り込み収束価格・期待リターンbp・時間軸予測)
    └── Strategy Agent (自律執行プラン策定: PEG_v2指値/TWAP/サイズ/TP/SL)
    ↓
  Regime Orchestrator (第3階層 司令塔: ガバナンス・リスク予算調停)
    ↓
  LIVE / DRYRUN / OBSERVATION (第1階層 執行ポッド群)
"""

import time
import uuid
import re
import logging
from typing import Dict, Any, Optional, Tuple, List

from .schemas import (
    MacroImpact,
    AlphaForecast,
    AlphaStrategyPlan,
    ExecutionCommand,
)
from news_pipeline.market_impact_scorer import MarketEvent
from news_pipeline.opportunity_assessor import OpportunityAssessor

logger = logging.getLogger("antigravity.multi_asset.alpha_engine")


class ForecastAgent:
    """
    第4階層: FORECAST AGENT
    市場がまだ完全に織り込んでいない価格ギャップ（未織り込みアルファ）を定量予測する。
    """

    @classmethod
    def generate_forecast(
        cls,
        event: MarketEvent,
        macro: MacroImpact,
        current_market_price: Optional[float] = None,
    ) -> Optional[AlphaForecast]:
        """
        市場イベントおよびMacroImpactから未織り込みアルファを分析し、予測モデル (AlphaForecast) を生成。
        OAS < 60 の場合は有意なアルファ機会なしと判定して None を返却。
        """
        oas = getattr(macro, "opportunity_score", 0)
        opp_type = getattr(macro, "opportunity_type", "NONE")
        if oas < 60 or opp_type == "NONE":
            return None

        event_id = f"EVT-{int(time.time())}-{getattr(event, 'symbol', 'GEN')}"
        forecast_id = f"FCST-{uuid.uuid4().hex[:8]}"
        symbol = getattr(event, "symbol", "") or "JP_STOCK"
        headline = f"{getattr(event, 'headline_metric', '')} {getattr(event, 'reason', '') or ''}"

        # -------------------------------------------------------------
        # 1. TOB / MBO アービトラージ (公開買付価格への収束)
        # -------------------------------------------------------------
        if opp_type == "TOB_ARBITRAGE":
            # 買付価格またはプレミアム率の抽出
            prem_match = re.search(r"(\d+(?:\.\d+)?)\s*%", headline)
            prem_pct = float(prem_match.group(1)) if prem_match else 20.0

            # 目標価格推計
            curr_price = current_market_price or 2000.0
            target_price = curr_price * (1.0 + prem_pct / 100.0)

            # 価格から買付価格が明示されている場合 (例: 2800円)
            price_match = re.search(r"(\d{3,7})\s*円", headline)
            if price_match:
                explicit_target = float(price_match.group(1))
                if explicit_target > curr_price:
                    target_price = explicit_target
                    prem_pct = ((target_price - curr_price) / curr_price) * 100.0

            # 期待リターン (bp: 1% = 100bp)
            expected_bp = round(prem_pct * 100.0, 1)

            return AlphaForecast(
                forecast_id=forecast_id,
                event_id=event_id,
                asset_class="JP_STOCK",
                symbol=symbol,
                opportunity_type="TOB_ARBITRAGE",
                target_price=target_price,
                current_price=curr_price,
                expected_return_bp=expected_bp,
                confidence=95.0,  # 友好的TOBは最高確度
                time_horizon="SWING",
                unpriced_alpha_rationale=f"TOB公開買付確定価格（{target_price:,.0f}円）への市場価格収束スプレッド（期待 +{expected_bp:.1f} bp）",
                timestamp=time.time(),
            )

        # -------------------------------------------------------------
        # 2. 大量保有報告 / アクティビスト追随 (買増し思惑・株主提案)
        # -------------------------------------------------------------
        if opp_type == "ACTIVIST_FOLLOW":
            curr_price = current_market_price or 2500.0
            # アクティビスト介入時の平均超過リターン (約 +3.5% 〜 +6.0%)
            expected_bp = 450.0  # +450 bp (+4.5%)
            target_price = curr_price * 1.045

            return AlphaForecast(
                forecast_id=forecast_id,
                event_id=event_id,
                asset_class="JP_STOCK",
                symbol=symbol,
                opportunity_type="ACTIVIST_FOLLOW",
                target_price=target_price,
                current_price=curr_price,
                expected_return_bp=expected_bp,
                confidence=85.0,
                time_horizon="SWING",
                unpriced_alpha_rationale=f"アクティビスト/大株主による継続買い需要と企業変革プレミアム（期待 +{expected_bp:.1f} bp）",
                timestamp=time.time(),
            )

        # -------------------------------------------------------------
        # 3. 自社株買い (需給引き締め・下値支持ドリフト)
        # -------------------------------------------------------------
        if opp_type == "BUYBACK_DRIFT":
            curr_price = current_market_price or 3000.0
            # 取得枠から推定期待リターン算出
            pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", headline)
            buyback_ratio = float(pct_match.group(1)) if pct_match else 3.0
            # 自社株買い枠の約60%が株価ドリフトとして顕在化するモデル
            expected_bp = round(buyback_ratio * 0.6 * 100.0, 1)
            target_price = curr_price * (1.0 + expected_bp / 10000.0)

            return AlphaForecast(
                forecast_id=forecast_id,
                event_id=event_id,
                asset_class="JP_STOCK",
                symbol=symbol,
                opportunity_type="BUYBACK_DRIFT",
                target_price=target_price,
                current_price=curr_price,
                expected_return_bp=expected_bp,
                confidence=88.0,
                time_horizon="INTRADAY",
                unpriced_alpha_rationale=f"自社株買い（取得枠 {buyback_ratio:.1f}%）による純粋な買い支え需給ドリフト（期待 +{expected_bp:.1f} bp）",
                timestamp=time.time(),
            )

        # -------------------------------------------------------------
        # 4. 決算サプライズ / 業績上方修正 (PEAD: 決算後ドリフト)
        # -------------------------------------------------------------
        if opp_type == "EARNINGS_SURPRISE":
            curr_price = current_market_price or 1800.0
            op_surp = getattr(event, "op_surprise", None)
            rev_rate = getattr(event, "revision_rate", None)
            delta = op_surp or rev_rate or 15.0
            # PEAD期待値: サプライズの約20%が数日〜数週かけてドリフト
            expected_bp = min(500.0, max(150.0, round(delta * 0.20 * 100.0, 1)))
            target_price = curr_price * (1.0 + expected_bp / 10000.0)

            return AlphaForecast(
                forecast_id=forecast_id,
                event_id=event_id,
                asset_class="JP_STOCK",
                symbol=symbol,
                opportunity_type="EARNINGS_SURPRISE",
                target_price=target_price,
                current_price=curr_price,
                expected_return_bp=expected_bp,
                confidence=78.0,
                time_horizon="INTRADAY",
                unpriced_alpha_rationale=f"業績サプライズ（乖離 {delta:.1f}%）に対する市場の織り込み遅延（PEAD ドリフト期待 +{expected_bp:.1f} bp）",
                timestamp=time.time(),
            )

        # -------------------------------------------------------------
        # 5. PTS夜間急騰モメンタム
        # -------------------------------------------------------------
        if opp_type == "PTS_MOMENTUM":
            curr_price = current_market_price or 4000.0
            p_chg = getattr(event, "price_change", 8.0) or 8.0
            expected_bp = round(p_chg * 0.4 * 100.0, 1) # 翌朝寄付きでの残存ギャップ
            target_price = curr_price * (1.0 + expected_bp / 10000.0)

            return AlphaForecast(
                forecast_id=forecast_id,
                event_id=event_id,
                asset_class="JP_STOCK",
                symbol=symbol,
                opportunity_type="PTS_MOMENTUM",
                target_price=target_price,
                current_price=curr_price,
                expected_return_bp=expected_bp,
                confidence=75.0,
                time_horizon="IMMEDIATE",
                unpriced_alpha_rationale=f"PTS急変（+{p_chg:.1f}%）の翌朝寄付きギャップ捕捉（期待 +{expected_bp:.1f} bp）",
                timestamp=time.time(),
            )

        return None


class StrategyAgent:
    """
    第4階層: STRATEGY AGENT
    収束予測 (AlphaForecast) を受け、最適な執行スタイル (PEG_v2/TWAP/成行) と
    厳格なリスク管理パラメータ (利確・損切りライン) を含めた執行計画 (AlphaStrategyPlan) を起草する。
    """

    @classmethod
    def formulate_plan(
        cls,
        forecast: AlphaForecast,
        current_price: Optional[float] = None,
    ) -> AlphaStrategyPlan:
        """
        予測モデルから自律執行計画 (AlphaStrategyPlan) を策定
        """
        plan_id = f"PLAN-{uuid.uuid4().hex[:8]}"
        opp_type = forecast.opportunity_type
        exp_bp = forecast.expected_return_bp
        conf = forecast.confidence

        # 1. 執行スタイル (Order Style) と 目標モード (Target Mode) の決定
        if opp_type == "TOB_ARBITRAGE":
            # TOBアービトラージ: 最良買気配+1tickに指値を追従させ、スプレッドを最大化して鞘取り
            order_style = "PEG_v2"
            target_mode = "SPECIAL_EVENT"
            # TOBは不成立リスクが極めて低いため、タイトな撤回SLと目標TPを設定
            take_profit_bp = round(exp_bp * 0.95, 1)  # 買付価格直前で手仕舞い
            stop_loss_bp = 150.0                      # 1.5%下落でTOB破談リスクヘッジ
            target_size = 500.0                       # 単元株制で500株 (5単元)
            reason = f"TOB確定的鞘取りアービトラージ: PEG_v2指値で最良気配追従 (目標 +{take_profit_bp} bp)"

        elif opp_type in ("ACTIVIST_FOLLOW", "BUYBACK_DRIFT"):
            # 大量保有・自社株買い: インパクトを抑えて滑らかに指値/TWAPで集める
            order_style = "TWAP" if opp_type == "ACTIVIST_FOLLOW" else "LIMIT"
            target_mode = "ALPHA_ACCUMULATE"
            take_profit_bp = round(exp_bp * 1.1, 1)
            stop_loss_bp = round(exp_bp * 0.4, 1)     # 期待値の40%逆行で損切り
            target_size = 300.0                       # 300株 (3単元)
            reason = f"{opp_type} 需給ドリフト追従: {order_style}で有利な押し目買付 (目標 +{take_profit_bp} bp)"

        elif opp_type in ("EARNINGS_SURPRISE", "PTS_MOMENTUM"):
            # 決算・PTS急騰: 初動モメンタムを即座に捉えるためアグレッシブに参入
            order_style = "AGGRESSIVE_MARKET" if opp_type == "PTS_MOMENTUM" else "PEG_v2"
            target_mode = "SPECIAL_EVENT" if conf >= 80.0 else "ALPHA_ACCUMULATE"
            take_profit_bp = round(exp_bp * 1.0, 1)
            stop_loss_bp = round(exp_bp * 0.5, 1)
            target_size = 200.0                       # 200株 (2単元)
            reason = f"{opp_type} モメンタム捕捉: {order_style}執行 (目標 +{take_profit_bp} bp)"

        else:
            order_style = "LIMIT"
            target_mode = "ALPHA_ACCUMULATE"
            take_profit_bp = 200.0
            stop_loss_bp = 100.0
            target_size = 100.0
            reason = "一般アルファ執行"

        return AlphaStrategyPlan(
            plan_id=plan_id,
            forecast_id=forecast.forecast_id,
            asset_class=forecast.asset_class,
            symbol=forecast.symbol,
            action="BUY" if exp_bp > 0 else "HOLD",
            order_style=order_style,
            target_size=target_size,
            target_mode=target_mode,
            max_slippage_bp=5.0,
            take_profit_bp=take_profit_bp,
            stop_loss_bp=stop_loss_bp,
            reason=reason,
            timestamp=time.time(),
        )


class AlphaOpportunityEngine:
    """
    第4階層: Alpha Opportunity Engine 統合マネージャー
    外部センチネル (TDnet, EDINET, PTS, ニュース) からのイベントを受信し、
    Forecast Agent (未織り込み度予測) -> Strategy Agent (執行プラン策定) をパイプライン実行。
    起草されたプランを Regime Orchestrator (第3階層) へ即時供給する。
    """

    def __init__(self):
        self.forecast_agent = ForecastAgent()
        self.strategy_agent = StrategyAgent()
        self.last_forecast: Optional[AlphaForecast] = None
        self.last_plan: Optional[AlphaStrategyPlan] = None
        self.history: List[Dict[str, Any]] = []

    def evaluate_opportunity(
        self,
        event: MarketEvent,
        macro: MacroImpact,
        current_market_price: Optional[float] = None,
    ) -> Tuple[Optional[AlphaForecast], Optional[AlphaStrategyPlan]]:
        """
        市場イベントから未織り込みアルファを評価し、Forecast & StrategyPlan を生成
        """
        oas = getattr(macro, "opportunity_score", 0)
        opp_type = getattr(macro, "opportunity_type", "NONE")

        if oas < 60 or opp_type == "NONE":
            return None, None

        logger.info(
            f"[ALPHA_ENGINE] 収益機会を検知: {event.name}({event.symbol}) type={opp_type}, OAS={oas}, MIS={macro.impact_score}"
        )

        # 1. 未織り込みアルファ予測 (Forecast Agent)
        forecast = self.forecast_agent.generate_forecast(
            event=event,
            macro=macro,
            current_market_price=current_market_price,
        )
        if not forecast:
            return None, None

        # 2. 自律執行プラン策定 (Strategy Agent)
        plan = self.strategy_agent.formulate_plan(
            forecast=forecast,
            current_price=current_market_price,
        )

        self.last_forecast = forecast
        self.last_plan = plan

        record = {
            "timestamp": time.time(),
            "symbol": forecast.symbol,
            "opportunity_type": opp_type,
            "oas": oas,
            "mis": macro.impact_score,
            "expected_bp": forecast.expected_return_bp,
            "order_style": plan.order_style,
            "target_mode": plan.target_mode,
            "target_size": plan.target_size,
        }
        self.history.append(record)

        logger.info(
            f"[ALPHA_ENGINE] 執行計画生成完了: plan_id={plan.plan_id}, style={plan.order_style}, mode={plan.target_mode}, size={plan.target_size}株, 期待=+{forecast.expected_return_bp}bp"
        )
        return forecast, plan
