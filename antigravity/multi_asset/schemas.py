"""
antigravity/multi_asset/schemas.py: 標準データ契約 (Typed Data Schemas)
===================================================================
仕様書: docs/multi_asset_os_architecture.md セクション4 に準拠。
全資産クラス (BTC, 日本株, FX, 先物, 米株) のエージェント間通信・ガバナンスを型付け。
"""

import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional, List


@dataclass
class MicroSignal:
    """
    第1階層 MICRO担当AGENT 出力データモデル
    秒〜分単位の短期方向性・流動性レジーム
    """
    asset_class: str          # "BTC", "JP_STOCK", "FX", "FUTURES", "US_STOCK"
    symbol: str               # "FX_BTC_JPY", "7203", "USDJPY"
    direction: str            # "LONG", "SHORT", "NEUTRAL"
    confidence: float         # 0.0 〜 100.0
    regime: str               # "TREND", "MEAN_REVERT", "HIGH_VOL", "ILLIQUID"
    spread_jpy: float
    imbalance_ratio: float    # 買い板 / 売り板 比率 (1.0 = 均衡, >1.0 = 買い優勢)
    timestamp: float = field(default_factory=time.time)
    extra_metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MicroSignal":
        return cls(**data)


@dataclass
class MacroImpact:
    """
    第2階層 MACRO Impact AGENT 出力データモデル
    マクロ指標・東証TDnet・EDINET・PTS夜間急変・世界主要市場の統合解析
    """
    impact_score: int         # 0 〜 100 (MIS: Market Impact Score)
    level: str = "NORMAL"     # "NORMAL", "WARNING", "CRITICAL", "WIDE"
    primary_event: str = ""   # "米CPI上振れ", "トヨタ決算サプライズ", "PTS異常急変"
    global_regime: str = "NEUTRAL" # "RISK_ON", "RISK_OFF", "STAGFLATION", "NEUTRAL"
    asset_impact_map: Dict[str, str] = field(default_factory=dict) # {"JP_STOCK": "BULL", "FX": "BEAR_JPY", "BTC": "NEUTRAL"}
    horizon: str = "INTRADAY" # "IMMEDIATE" (〜15分), "INTRADAY" (当日), "SWING" (数日〜週)
    opportunity_score: int = 0         # 0 〜 100 (OAS: Opportunity Assessment Score: 市場機会スコア)
    opportunity_type: str = "NONE"     # "TOB_ARBITRAGE", "ACTIVIST_FOLLOW", "BUYBACK_DRIFT", "EARNINGS_SURPRISE", "PTS_MOMENTUM", "NONE"
    is_special_event: bool = False     # OAS >= 80 かつ特異アルファイベント (TOB/MBO等)
    rationale: str = ""                # 判定根拠
    timestamp: float = field(default_factory=time.time)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MacroImpact":
        return cls(**data)


@dataclass
class AlphaForecast:
    """
    第4階層 FORECAST AGENT 出力データモデル
    市場未織り込みアルファ (Unpriced Alpha) の価格収束予測
    """
    forecast_id: str
    event_id: str
    asset_class: str          # "JP_STOCK", "BTC", "FX", "FUTURES"
    symbol: str               # "7203", "FX_BTC_JPY", "USDJPY"
    opportunity_type: str     # "TOB_ARBITRAGE", "ACTIVIST_FOLLOW", "BUYBACK_DRIFT", "EARNINGS_SURPRISE", "PTS_MOMENTUM"
    target_price: Optional[float] = None     # 予測目標価格 (TOB買付価格・理論フェアバリュー)
    current_price: Optional[float] = None    # 開示直後 / 現在価格
    expected_return_bp: float = 0.0          # 単発トレード期待値 (bp: (P_win * R_win) - (P_loss * R_loss))
    win_probability: float = 0.50            # 成功確率 (0.0 〜 1.0, 例: 0.98)
    win_return_bp: float = 0.0               # 成功時利益 (bp, 例: +1500.0)
    loss_return_bp: float = 0.0              # 失敗時損失 (bp, 例: -2000.0)
    holding_days: float = 1.0                # 想定拘束期間 (日数, 例: 60.0, 3.0, 0.5)
    daily_expectancy_bp: float = 0.0         # 1日あたり資金効率 (bp/日 = expected_return_bp / holding_days)
    annualized_return_pct: float = 0.0       # 年率換算期待利回り (%, Capital Velocity = daily_bp * 365 / 100)
    tier: str = "TIER1"                      # "TIER1" (大量保有), "TIER2" (自社株買い), "TIER3" (上方修正), "TIER4" (TOB)
    evs_score: float = 0.0                   # 第5階層 EVS (Expected Value Score: 期待bp x 勝率 x 資金効率 x 流動性)
    liquidity_factor: float = 1.0            # 流動性係数 (小型株でアルゴ不在なら 1.3, 超大型株は 0.8)
    capital_requirement_jpy: float = 200000.0# 推奨必要資金 (円: 1単元の想定拘束資金)
    sample_size: int = 0                     # DuckDB/Parquet 過去同条件母集団件数 (例: 148件)
    empirical_win_rate: Optional[float] = None # 過去実績勝率 (例: 0.71)
    confidence: float = 0.0                  # 予測確信度 (0.0 〜 100.0)
    time_horizon: str = "INTRADAY"           # "IMMEDIATE", "INTRADAY", "SWING"
    unpriced_alpha_rationale: str = ""       # 市場未織り込み根拠
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AlphaForecast":
        return cls(**data)


@dataclass
class AlphaStrategyPlan:
    """
    第4階層 STRATEGY AGENT 出力データモデル
    収束予測に基づく具体的な自律執行プラン (PEG_v2指値・TWAP・ロット・TP/SL)
    """
    plan_id: str
    forecast_id: str
    asset_class: str
    symbol: str
    action: str               # "BUY", "SELL", "HOLD", "ARBITRAGE"
    order_style: str          # "PEG_v2", "LIMIT", "AGGRESSIVE_MARKET", "TWAP"
    target_size: float = 0.0  # 推奨発注株数 / ロット
    target_mode: str = "SPECIAL_EVENT" # "SPECIAL_EVENT", "ALPHA_ACCUMULATE", "HYBRID"
    max_slippage_bp: float = 5.0
    take_profit_bp: Optional[float] = None
    stop_loss_bp: Optional[float] = None
    reason: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AlphaStrategyPlan":
        return cls(**data)


@dataclass
class ExecutionCommand:
    """
    第3階層 司令塔 (Regime Orchestrator) -> 第1階層 EXECUTION AGENT ガバナンス命令
    上位命令の絶対優先（Top-Down Precedence）を担保する。
    """
    asset_class: str          # "BTC", "JP_STOCK", "FX", "ALL"
    target_mode: str          # "HFT", "TREND", "HYBRID", "REDUCE_50", "STOP"
    allocated_risk_jpy: float # 許容日次損失リミット (円)
    max_position_size: float  # 最大ロット / 株式なら最大株数
    is_halted: bool           # サーキットブレーカー強制遮断フラグ (True時は新規発注即停止)
    reason: str               # 発令理由 (マクロ急変、CB到達、通常リバランス等)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionCommand":
        return cls(**data)


@dataclass
class StrategyDraft:
    """
    第1階層 ALPHA分析AGENT 出力データモデル
    資産クラス固有のアルファ源探索・戦略起草
    """
    strategy_id: str
    asset_class: str
    name: str
    code: str
    metrics: Dict[str, float] = field(default_factory=dict) # Sharpe, PF, WinRate, MDD, MaxConsecLoss
    status: str = "DRAFT"     # "DRAFT", "PROPOSED", "APPROVED", "REJECTED"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StrategyDraft":
        return cls(**data)


@dataclass
class OrderCommand:
    """
    ポッド内売買発注コマンド (内部または実証券API向け)
    """
    order_id: str
    asset_class: str
    symbol: str
    side: str                 # "BUY", "SELL"
    order_type: str = "MARKET"# "MARKET", "LIMIT"
    price: Optional[float] = None
    size: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OrderCommand":
        return cls(**data)


@dataclass
class TradeReport:
    """
    売買約定・決済レポート
    """
    trade_id: str
    asset_class: str
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    size: float
    pnl_jpy: float
    holding_seconds: float
    exit_reason: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradeReport":
        return cls(**data)


@dataclass
class AssetPodState:
    """
    各ポッドのテレメトリ・ステータス
    """
    asset_class: str
    current_mode: str = "HYBRID"
    active_positions: Dict[str, float] = field(default_factory=dict) # symbol -> position size
    daily_pnl_jpy: float = 0.0
    allocated_budget_jpy: float = 0.0
    is_halted: bool = False
    last_signal: Optional[MicroSignal] = None
    last_command: Optional[ExecutionCommand] = None
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        res = asdict(self)
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AssetPodState":
        data_copy = dict(data)
        if data_copy.get("last_signal") and isinstance(data_copy["last_signal"], dict):
            data_copy["last_signal"] = MicroSignal.from_dict(data_copy["last_signal"])
        if data_copy.get("last_command") and isinstance(data_copy["last_command"], dict):
            data_copy["last_command"] = ExecutionCommand.from_dict(data_copy["last_command"])
        return cls(**data_copy)
