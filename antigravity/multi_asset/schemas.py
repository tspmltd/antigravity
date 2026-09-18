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
    level: str                # "NORMAL", "WARNING", "CRITICAL", "WIDE"
    primary_event: str        # "米CPI上振れ", "トヨタ決算サプライズ", "PTS異常急変"
    global_regime: str        # "RISK_ON", "RISK_OFF", "STAGFLATION", "NEUTRAL"
    asset_impact_map: Dict[str, str] = field(default_factory=dict) # {"JP_STOCK": "BULL", "FX": "BEAR_JPY", "BTC": "NEUTRAL"}
    horizon: str = "INTRADAY" # "IMMEDIATE" (〜15分), "INTRADAY" (当日), "SWING" (数日〜週)
    timestamp: float = field(default_factory=time.time)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MacroImpact":
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
