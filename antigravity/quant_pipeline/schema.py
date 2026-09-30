"""
Antigravity Quant Schema Definitions (GAPCORE Compliant)
- 全エージェント共通のイベント・スナップショット・シグナル・約定・PnLスキーマ
"""
from dataclasses import dataclass, asdict, field
from typing import List, Optional


@dataclass
class MarketSnapshot:
    """📊 基本マーケットスナップショット"""
    timestamp: int                  # Unix ms
    mid_price: float
    best_bid: float
    best_ask: float
    spread: float
    bid_size_1: float
    ask_size_1: float
    orderbook_imbalance: float      # (B - A) / (B + A) [-1.0, 1.0]
    micro_price: float              # 板厚加重平均価格
    last_trade_price: float = 0.0
    last_trade_size: float = 0.0
    volatility_5s: float = 0.0


@dataclass
class OrderbookMicroSnapshot:
    """🧱 板マイクロ構造専用スナップショット (depth 1-5, taker, cancel/refill)"""
    timestamp: int                  # Unix ms
    latency_ms: float               # WebSocket受信レイテンシ
    best_bid: float
    best_ask: float
    mid_price: float
    micro_price: float
    micro_dev: float                # micro_price - mid_price
    bid_depth_1: float
    ask_depth_1: float
    total_bid_depth: float          # 1〜5レベル合計
    total_ask_depth: float
    imbalance: float
    taker_volume_bid: float         # 直近窓でbidを削った成行量
    taker_volume_ask: float         # 直近窓でaskを削った成行量
    taker_aggressiveness: float     # taker_vol / depth
    cancel_rate: float = 0.0        # 直近キャンセル率
    refill_rate: float = 0.0        # 直近再配置率
    last_sell_price: float = 0.0    # この更新で買い板を削った最安の約定
    last_buy_price: float = 0.0     # この更新で売り板を削った最高の約定


@dataclass
class CoreEvent:
    """🧱 全エージェント共通イベント"""
    timestamp: int                  # Unix ms
    agent_type: str                 # trend / mean_rev / adverse / mm / hft / scalping
    event_type: str                 # signal / entry / exit / fill / cancel / error
    market_state: str               # trend / range / high_vol / low_vol
    symbol: str = "FX_BTC_JPY"
    position_size: float = 0.0
    inventory_risk: float = 0.0
    latency_ms: float = 0.0
    message: str = ""


@dataclass
class SignalMetrics:
    """🎯 シグナル強度・確信度"""
    timestamp: int
    signal_id: str
    agent_type: str
    signal_strength: float          # 0.0 ~ 1.0
    confidence_score: float
    expected_return: float
    regime_tag: str
    features_used: str = ""


@dataclass
class FusionDecisionLog:
    """🧩 Signal Fusion Engine 意思決定ログ (v1 · **現行本番経路** · P0境界まで維持)

    CSR-532: V2 切替は P0(CSR-521 24h) 完了後。今夜は PREPARE のみ。
    realized_pnl は後から紐付ける教師ラベル（CSR-529）。
    """
    timestamp: int
    trend_direction: str            # up / down / neutral
    trend_strength: float           # 0.0 ~ 1.0
    regime_tag: str                 # trend / range / high_vol / low_vol
    pressure_side: str              # buy / sell / none
    pressure_score: float           # 0.0 ~ 1.0
    fake_breakout_flag: bool
    latency_risk_flag: bool
    final_confidence: float         # 0.0 ~ 1.0
    action: str                     # buy / sell / hold / exit  ※ legacy = decision OR final 混在しうる
    size_multiplier: float          # 0.0 ~ 1.5
    realized_pnl: float = 0.0       # 後から紐付ける約定結果
    adverse_warning_flag: bool = False
    adverse_risk_side: str = "none"
    adverse_risk_score: float = 0.0


# execution_gate_reason 許容値（Hands 責務）
EXECUTION_GATE_REASONS = (
    "OBSERVE_MODE",
    "HARD_VETO",
    "LOW_CONFIDENCE",
    "NO_POSITION_TO_EXIT",
    "LIVE_DISABLED",
    "WIRE_DISABLED",
    "ALLOWED",
)


@dataclass
class FusionDecisionLogV2:
    """🧩 Fusion 意思決定ログ v2（CSR-531/532 PREPARE）— Brain / Gate / Hands 分離

    **切替条件:** P0 CSR-521 24h 完走後のみ live writer へ接続。
    DuckDB 教師: candidate/decision + realized_pnl_bp（final 単独禁止）。
    Counterfactual は本レコードへネストせず `CounterfactualLog` 別テーブル（decision_id JOIN）。
    """
    timestamp: int
    # 0. Join keys（誤結合防止）
    decision_id: str
    signal_id: str
    strategy_id: str
    strategy_version: str
    parameter_hash: str
    # 1. TrendFollow
    trend_direction: str
    trend_strength: float
    trend_regime: str
    # 2. Candidate
    candidate_action: str
    candidate_confidence: float
    # 3. Microstructure
    micro_state: str
    pressure_side: str
    pressure_score: float
    # 4. Risk / Veto
    veto: bool
    veto_reason: str
    adverse_score: float
    # 5. Brain decision
    decision_action: str
    decision_confidence: float
    # 6. Hands / Gate
    execution_allowed: bool
    execution_gate_reason: str  # see EXECUTION_GATE_REASONS
    final_action: str
    # 7. Teacher label (async JOIN → same decision_id)
    realized_pnl_bp: Optional[float] = None
    # meta / legacy bridges
    latency_risk_flag: bool = False
    size_multiplier: float = 1.0
    fake_breakout_flag: bool = False
    schema_version: str = "fusion_v2_csr532"
    wire: str = "NO"
    enforce: int = 0
    live_writer: bool = False  # True のみ parquet fusion_log_v2 へ flush

    def to_legacy(self) -> FusionDecisionLog:
        """旧 DuckDB クエリ互換行（action=decision_action · 潰さない）。"""
        return FusionDecisionLog(
            timestamp=self.timestamp,
            trend_direction=self.trend_direction,
            trend_strength=self.trend_strength,
            regime_tag=self.trend_regime,
            pressure_side=self.pressure_side,
            pressure_score=self.pressure_score,
            fake_breakout_flag=self.fake_breakout_flag or self.veto,
            latency_risk_flag=self.latency_risk_flag,
            final_confidence=self.decision_confidence,
            action=self.decision_action,
            size_multiplier=self.size_multiplier,
            realized_pnl=float(self.realized_pnl_bp or 0.0),
            adverse_warning_flag=self.adverse_score > 0,
            adverse_risk_side="none",
            adverse_risk_score=self.adverse_score,
        )


@dataclass
class CounterfactualLog:
    """↗ Counterfactual 別テーブル（CSR-532）— fusion_log_v2.decision_id で JOIN

    例示PnLを保存しない。市場から計算できた値のみ。
      cf_status: NULL_PENDING | INVALID | VALID
      verdict: 結果から生成（先決め禁止）
        SAVED_CAPITAL_CANDIDATE | OPPORTUNITY_COST_CANDIDATE | FLAT | NEED_MORE
    """
    decision_id: str
    timestamp: int
    event: str  # HARD_VETO_AUDIT | EXIT_AUDIT | TREND_CANDIDATE_AUDIT
    candidate_action: str
    entry_ref_price: float
    veto_reason: str = ""
    trend_strength: float = 0.0
    # horizons — None = 未計算
    cf_pnl_1s_bp: Optional[float] = None
    cf_pnl_3s_bp: Optional[float] = None
    cf_pnl_10s_bp: Optional[float] = None
    cf_pnl_30s_bp: Optional[float] = None
    cf_mae_bp: Optional[float] = None
    cf_mfe_bp: Optional[float] = None
    cf_status: str = "NULL_PENDING"  # NULL_PENDING | INVALID | VALID
    verdict: Optional[str] = None
    schema_version: str = "cf_log_csr532"
    wire: str = "NO"
    enforce: int = 0

    @staticmethod
    def verdict_from_pnl30(pnl30: Optional[float]) -> Optional[str]:
        if pnl30 is None:
            return None
        if pnl30 < 0:
            return "SAVED_CAPITAL_CANDIDATE"
        if pnl30 > 0:
            return "OPPORTUNITY_COST_CANDIDATE"
        return "FLAT"


@dataclass
class ExecutionLog:
    """💱 注文・約定ログ"""
    timestamp: int
    order_id: str
    side: str                       # BUY / SELL
    order_type: str                 # MARKET / LIMIT
    order_price: float
    order_size: float
    fill_price: float
    fill_size: float
    slippage: float
    fee: float = 0.0
    execution_latency_ms: float = 0.0


@dataclass
class PnLLog:
    """📈 損益・ドローダウンログ"""
    timestamp: int
    realized_pnl: float
    unrealized_pnl: float
    drawdown: float
    max_drawdown: float
    sharpe_rolling: float = 0.0
    win_rate_rolling: float = 0.0


@dataclass
class AgentConclusion:
    """🏛️ 各専門エージェントの分析結論スキーマ (4AGENT 統一)"""
    agent_name: str                 # MicrostructureAgent / TrendFollowAgent / DuckDBOptimizerAgent / AdverseResearchAgent
    timestamp: int                  # Unix ms
    verdict: str                    # エージェントの判定要約 (例: BUY_PRESSURE, NORMAL_RANGE, ADVERSE_DEPLETION_EVACUATE)
    confidence: float               # 0.0 ~ 1.0
    primary_action: str             # buy / sell / hold / exit / cancel / veto
    metrics: dict = field(default_factory=dict)      # 固有の計算指標 (Imbalance, Sharpe, LeadTime etc.)
    parameters: dict = field(default_factory=dict)   # 推奨パラメータ (重み, スプレッド上限 etc.)
    hard_veto: bool = False         # 新規エントリーの絶対遮断権
    emergency_cancel: bool = False  # 保有指値・ポジションの即時撤退要求
    explanation: str = ""           # 人間可読の分析サマリー


@dataclass
class CouncilVerdict:
    """👑 4AGENT 合同評議会・総合意思決定＆戦略反映ディレクティブ

    CSR-531 Action 意味論（潰さない）:
      candidate_*  = Trend 方向候補
      decision_*   = Council 経済判断（Veto前の意図含む）
      veto         = Micro Hard Veto
      execution_allowed = 執行許可（OBSERVE期間は false）
      final_action = 実際に許可された行動（未許可なら hold）
    HOLD ≠ EXIT。candidate と final を同一フィールドへ書かない。
    """
    timestamp: int
    conclusions: dict = field(default_factory=dict)  # {agent_name: AgentConclusion dict}

    # --- layered action schema (CSR-531) ---
    trend_direction: str = "neutral"
    trend_strength: float = 0.0
    trend_regime: str = "range"
    candidate_action: str = "hold"
    candidate_confidence: float = 0.0
    micro_state: str = "NEUTRAL"
    pressure_side: str = "none"
    pressure_score: float = 0.0
    veto: bool = False
    veto_reason: Optional[str] = None
    # CSR-558: Effective Veto = veto ∧ decision∈{buy,sell}; redundant (dec HOLD) excluded from Veto CF economics
    veto_effective: bool = False
    # CSR-546: SHADOW_OFF の 0.0 は「安全」ではなく無効。valid=False なら NULL 扱い。
    adverse_score: Optional[float] = None
    adverse_score_valid: bool = False
    decision_action: str = "hold"
    decision_confidence: float = 0.0
    decision_gate_reason: str = "PASS_THROUGH"  # CSR-558: CONFIDENCE_GATE / PASS_THROUGH / …
    execution_allowed: bool = False
    execution_gate_reason: str = "OBSERVE_MODE"  # CSR-532 Hands 理由
    final_action: str = "hold"
    final_confidence: float = 0.0

    size_multiplier: float = 1.0
    active_regime: str = "range"
    adverse_risk_level: str = "SHADOW_OFF"  # SHADOW_OFF / SHADOW_ON · SAFE legacy≠Economic PASS
    hard_veto_active: bool = False
    emergency_cancel_active: bool = False
    applied_weights: dict = field(default_factory=dict)
    strategy_directives: list = field(default_factory=list)
    schema_version: str = "csr564_v1"
    wire: str = "NO"
    enforce: int = 0
    # CSR-564 P0 State Freshness (reader computes age/ok; writer stamps write time)
    state_write_ts_ms: Optional[int] = None
    freshness_max_age_sec: float = 120.0
    # CSR-564 P2 Micro Pressure v2 SHADOW features (not wired to veto/decision)
    buy_pressure_v2: float = 0.0
    sell_pressure_v2: float = 0.0

