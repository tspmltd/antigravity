import pandas as pd
from core.base_strategy import BaseStrategy

class CustomStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(name="BrokenStrat")

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        # signal 列を作成せず返すバグ
        df['signal'] = 0
        return df
