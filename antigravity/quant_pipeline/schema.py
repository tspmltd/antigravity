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
    """🧩 Signal Fusion Engine 意思決定ログ (学習・重みチューニングの教師データ)"""
    timestamp: int
    trend_direction: str            # up / down / neutral
    trend_strength: float           # 0.0 ~ 1.0
    regime_tag: str                 # trend / range / high_vol / low_vol
    pressure_side: str              # buy / sell / none
    pressure_score: float           # 0.0 ~ 1.0
    fake_breakout_flag: bool
    latency_risk_flag: bool
    final_confidence: float         # 0.0 ~ 1.0
    action: str                     # buy / sell / hold / exit
    size_multiplier: float          # 0.0 ~ 1.5
    realized_pnl: float = 0.0       # 後から紐付ける約定結果
    adverse_warning_flag: bool = False
    adverse_risk_side: str = "none"
    adverse_risk_score: float = 0.0


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
    """👑 4AGENT 合同評議会・総合意思決定＆戦略反映ディレクティブ"""
    timestamp: int
    conclusions: dict = field(default_factory=dict)  # {agent_name: AgentConclusion dict}
    final_action: str = "hold"                       # buy / sell / hold / exit / cancel
    final_confidence: float = 0.0
    size_multiplier: float = 1.0
    active_regime: str = "range"
    adverse_risk_level: str = "SAFE"                 # SAFE / WARNING / CRITICAL
    hard_veto_active: bool = False
    emergency_cancel_active: bool = False
    applied_weights: dict = field(default_factory=dict)
    strategy_directives: list = field(default_factory=list)  # 戦略への有効反映内容サマリー

