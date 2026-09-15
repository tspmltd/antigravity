import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal
import numpy as np
import pandas as pd


class DataLoader:
    """
    ヒストリカル価格データの読み込み・取引所APIダウンロード・キャッシュ・合成データ生成モジュール。
    """

    CACHE_DIR = "data/cache"

    @classmethod
    def load_or_generate_data(
        cls,
        file_path: Optional[str] = None,
        source: Optional[Literal["binance", "bitflyer", "gmo", "synthetic"]] = None,
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
        limit: int = 1500,
        cache_ttl_hours: float = 1.0,
        force_refresh: bool = False,
        n_bars: int = 5000,
        seed: int = 42
    ) -> pd.DataFrame:
        """
        価格データを取得して整形済みDataFrameを返す。
        
        優先順位:
        1. file_path が指定されていれば指定CSVからロード
        2. source が指定されていれば取引所APIからフェッチ＆キャッシュ
        3. 上記がなければ既存の最新キャッシュを検索
        4. 全てなければ合成市場データを自動生成
        """
        # 1. 指定ファイルからのロード
        if file_path and os.path.exists(file_path):
            print(f"[DataLoader] 指定CSVファイルを読み込みます: {file_path}")
            return cls.load_from_csv(file_path)

        # 2. 取引所APIからのダウンロード & キャッシュ
        if source and source != "synthetic":
            try:
                print(f"[DataLoader] 取引所 ({source.upper()}) から {symbol} ({timeframe}) の実データを取得します...")
                return cls.fetch_and_cache(
                    source=source,
                    symbol=symbol,
                    timeframe=timeframe,
                    limit=limit,
                    cache_ttl_hours=cache_ttl_hours,
                    force_refresh=force_refresh,
                )
            except Exception as e:
                print(f"[DataLoader]  API取得エラー: {e} -> キャッシュまたはフォールバックを試みます")

        # 3. 既存のキャッシュファイルを探索
        cache_file = os.path.join(cls.CACHE_DIR, f"{source or 'binance'}_{symbol}_{timeframe}.csv")
        if os.path.exists(cache_file):
            print(f"[DataLoader] ローカルキャッシュからデータを読み込みます: {cache_file}")
            return cls.load_from_csv(cache_file)

        # 4. 合成データ生成 (フォールバック)
        print("[DataLoader] 実データソースが指定されていないため、合成市場データを生成します。")
        return cls.generate_synthetic_data(n_bars=n_bars, seed=seed)

    @classmethod
    def fetch_and_cache(
        cls,
        source: str = "binance",
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
        limit: int = 1000,
        cache_ttl_hours: float = 1.0,
        force_refresh: bool = False
    ) -> pd.DataFrame:
        """
        取引所APIから実ローソク足データを取得し、CSVとしてローカルにキャッシュする。
        """
        os.makedirs(cls.CACHE_DIR, exist_ok=True)
        cache_path = os.path.join(cls.CACHE_DIR, f"{source}_{symbol}_{timeframe}.csv")

        # キャッシュ有効性チェック
        if not force_refresh and os.path.exists(cache_path):
            file_mtime = datetime.fromtimestamp(os.path.getmtime(cache_path))
            age_hours = (datetime.now() - file_mtime).total_seconds() / 3600.0
            if age_hours < cache_ttl_hours:
                print(f"[DataLoader] 有効なキャッシュを使用します (作成から {age_hours:.1f} 時間経過): {cache_path}")
                return cls.load_from_csv(cache_path)

        # APIからデータダウンロード
        if source == "binance":
            df = cls._fetch_binance_klines(symbol=symbol, interval=timeframe, limit=limit)
        elif source == "bitflyer":
            df = cls._fetch_bitflyer_executions_as_ohlcv(symbol=symbol, timeframe=timeframe, max_executions=max(limit * 20, 30000))
        elif source == "gmo":
            df = cls._fetch_gmo_klines(symbol=symbol, interval=timeframe)
        else:
            raise ValueError(f"未対応のデータソース: {source}")

        # 既存キャッシュとのマージ（過去ローソク足の継続蓄積）
        if os.path.exists(cache_path):
            try:
                old_df = cls.load_from_csv(cache_path)
                if not old_df.empty and "timestamp" in old_df.columns:
                    merged = pd.concat([old_df, df], ignore_index=True)
                    merged["timestamp"] = pd.to_datetime(merged["timestamp"])
                    merged = merged.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                    df = merged
            except Exception as ex:
                print(f"[DataLoader] キャッシュマージ例外 (新規保存にフォールバック): {ex}")

        # キャッシュに保存
        df.to_csv(cache_path, index=False)
        print(f"[DataLoader] 実相場データをキャッシュに保存しました: {cache_path} ({len(df)} 本)")
        return df

    @staticmethod
    def _fetch_binance_klines(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 1000) -> pd.DataFrame:
        """BinanceパブリックAPIからKlines（ローソク足）を取得"""
        # Binanceのインターバル形式にマッピング
        interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
        binance_interval = interval_map.get(interval, "1h")
        clean_symbol = symbol.replace("_", "").replace("/", "").upper()

        url = f"https://api.binance.com/api/v3/klines?symbol={clean_symbol}&interval={binance_interval}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (GapcorePJ AlgoTrader)"})
        
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        rows = []
        for item in data:
            # item: [open_time, open, high, low, close, volume, close_time, ...]
            rows.append({
                "timestamp": pd.to_datetime(item[0], unit="ms"),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5])
            })
        return pd.DataFrame(rows)

    @staticmethod
    def _fetch_bitflyer_executions_as_ohlcv(
        symbol: str = "BTC_JPY",
        timeframe: str = "1m",
        max_executions: int = 15000
    ) -> pd.DataFrame:
        """
        bitFlyerパブリックAPIから約定履歴（Executions）を取得し、指定時間足のOHLCVにリサンプリング集計する。
        """
        clean_symbol = symbol.replace("/", "_").upper()
        if clean_symbol in ["BTCJPY", "BTC"]:
            clean_symbol = "BTC_JPY"
        elif clean_symbol in ["FXBTCJPY", "FX", "FXBTC", "BITFLYER_FX"]:
            clean_symbol = "FX_BTC_JPY"

        all_execs = []
        before_id = None

        # 複数回ページング取得 (最新から順に遡る)
        fetch_rounds = max(1, max_executions // 500)
        for _ in range(fetch_rounds):
            url = f"https://api.bitflyer.com/v1/executions?product_code={clean_symbol}&count=500"
            if before_id:
                url += f"&before={before_id}"

            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (GapcorePJ AlgoTrader)"})
            with urllib.request.urlopen(req, timeout=10) as response:
                execs = json.loads(response.read().decode("utf-8"))

            if not execs:
                break

            all_execs.extend(execs)
            before_id = execs[-1]["id"]
            if len(all_execs) >= max_executions:
                break
            time.sleep(0.2) # APIレートリミット対策

        if not all_execs:
            raise ValueError(f"bitFlyerから約定データを取得できませんでした ({clean_symbol})")

        # DataFrame化してリサンプリング
        df_exec = pd.DataFrame(all_execs)
        df_exec["timestamp"] = pd.to_datetime(df_exec["exec_date"], format="mixed")
        df_exec = df_exec.sort_values("timestamp").reset_index(drop=True)

        # タイムフレーム変換
        rule_map = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h", "1d": "1D"}
        freq = rule_map.get(timeframe, "1min")

        ohlcv = df_exec.set_index("timestamp")["price"].resample(freq).ohlc()
        volume = df_exec.set_index("timestamp")["size"].resample(freq).sum()

        df_resampled = ohlcv.copy()
        df_resampled["volume"] = volume
        df_resampled = df_resampled.dropna().reset_index()

        # カラム名の標準化
        df_resampled.columns = ["timestamp", "open", "high", "low", "close", "volume"]
        return df_resampled

    @staticmethod
    def _fetch_gmo_klines(symbol: str = "BTC_JPY", interval: str = "1h") -> pd.DataFrame:
        """GMOコインパブリックAPIから指定銘柄の直近ローソク足を取得"""
        clean_symbol = symbol.replace("/", "_").upper()
        if clean_symbol in ["BTCJPY", "BTC"]:
            clean_symbol = "BTC"

        # GMOのインターバル形式
        interval_map = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1hour", "1d": "1day"}
        gmo_interval = interval_map.get(interval, "1hour")

        # GMOは日付指定 (本日および前日)
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        url = f"https://api.coin.z.com/public/v1/klines?symbol={clean_symbol}&interval={gmo_interval}&date={today_str}"
        
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (GapcorePJ AlgoTrader)"})
        with urllib.request.urlopen(req, timeout=10) as response:
            res_json = json.loads(response.read().decode("utf-8"))

        if res_json.get("status") != 0 or not res_json.get("data"):
            raise ValueError(f"GMOコインAPIエラー: {res_json}")

        rows = []
        for item in res_json["data"]:
            # item: {"openTime": "...", "open": "...", "high": "...", "low": "...", "close": "...", "volume": "..."}
            rows.append({
                "timestamp": pd.to_datetime(int(item["openTime"]), unit="ms"),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item["volume"])
            })
        return pd.DataFrame(rows)

    @staticmethod
    def load_from_csv(file_path: str) -> pd.DataFrame:
        """
        ローカルCSVファイルを読み込み、カラム名を標準形式 ['timestamp', 'open', 'high', 'low', 'close', 'volume'] に統一。
        """
        df = pd.read_csv(file_path)

        # カラム名の小文字化と揺らぎ吸収
        rename_map = {}
        for col in df.columns:
            lower = col.lower().strip()
            if lower in ["time", "datetime", "date", "timestamp"]:
                rename_map[col] = "timestamp"
            elif lower in ["open", "o"]:
                rename_map[col] = "open"
            elif lower in ["high", "h"]:
                rename_map[col] = "high"
            elif lower in ["low", "l"]:
                rename_map[col] = "low"
            elif lower in ["close", "c"]:
                rename_map[col] = "close"
            elif lower in ["volume", "vol", "v", "size"]:
                rename_map[col] = "volume"

        df = df.rename(columns=rename_map)

        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 欠損値補完
        df = df.ffill().bfill()
        return df

    @staticmethod
    def generate_synthetic_data(
        n_bars: int = 5000,
        start_date: str = "2024-01-01",
        freq: str = "1h",
        seed: int = 42
    ) -> pd.DataFrame:
        """合成OHLCVデータ生成"""
        np.random.seed(seed)
        dates = pd.date_range(start=start_date, periods=n_bars, freq=freq)
        returns = np.random.normal(0.0001, 0.008, n_bars)
        trend_mask = np.sin(np.linspace(0, 10, n_bars)) * 0.003
        returns += trend_mask
        price = 50000.0 * np.exp(np.cumsum(returns))

        high_noise = np.abs(np.random.normal(0, 0.004, n_bars))
        low_noise = np.abs(np.random.normal(0, 0.004, n_bars))
        volume = np.random.lognormal(mean=5.0, sigma=1.0, size=n_bars) * 10.0

        close = price
        high = price * (1 + high_noise)
        low = price * (1 - low_noise)
        open_price = np.roll(close, 1)
        open_price[0] = price[0]

        return pd.DataFrame({
            "timestamp": dates,
            "open": open_price,
            "high": np.maximum(high, np.maximum(open_price, close)),
            "low": np.minimum(low, np.minimum(open_price, close)),
            "close": close,
            "volume": volume
        })

    @staticmethod
    def split_train_test(
        df: pd.DataFrame,
        train_ratio: float = 0.7
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """In-Sample (訓練期) と Out-of-Sample (テスト期) に時系列順で分割"""
        split_idx = int(len(df) * train_ratio)
        train_df = df.iloc[:split_idx].copy().reset_index(drop=True)
        test_df = df.iloc[split_idx:].copy().reset_index(drop=True)
        return train_df, test_df
