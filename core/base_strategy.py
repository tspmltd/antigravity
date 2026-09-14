from abc import ABC, abstractmethod
from typing import Dict, Any
import pandas as pd


class BaseStrategy(ABC):
    """
    全自動売買戦略の基底クラス。
    各サブエージェント（Proposer, Optimizer）はこのインターフェースを満たすコードを生成・改善します。
    """

    def __init__(self, name: str, version: str = "v1.0", parameters: Dict[str, Any] = None):
        self.name = name
        self.version = version
        self.parameters = parameters or {}
        self.hypothesis: str = ""

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        OHLCVデータフレームを受け取り、売買シグナル列 'signal' を追加したDataFrameを返す。
        
        Args:
            df: カラム ['timestamp', 'open', 'high', 'low', 'close', 'volume'] を含むDataFrame
            
        Returns:
            DataFrame: 'signal' 列 (1: 買い, -1: 売り/ドテン売り, 0: ノーポジション/決済) を付与したもの
        """
        pass

    def get_metadata(self) -> Dict[str, Any]:
        """戦略のメタデータを取得"""
        return {
            "name": self.name,
            "version": self.version,
            "parameters": self.parameters,
            "hypothesis": self.hypothesis,
        }
