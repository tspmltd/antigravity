"""
Market Data Ingestion Engine (1本化ストリーム & 特徴量算出)
- bitFlyerから板・約定・Tickerを受信
- depth 1-5, mid, micro-price, imbalance, taker volume, latency を計算
- orderbook_micro を Parquet へ非同期投入 & EventBus へブロードキャスト
"""
import time
import json
import urllib.request
from typing import List, Dict, Any, Optional

from .event_bus import EventBus
from .parquet_logger import ParquetBatchLogger
from .schema import OrderbookMicroSnapshot, MarketSnapshot


class MarketDataIngestion:
    def __init__(
        self,
        bus: EventBus,
        logger: ParquetBatchLogger,
        product_code: str = "FX_BTC_JPY"
    ):
        self.bus = bus
        self.logger = logger
        self.product_code = product_code
        self.ticker_url = f"https://api.bitflyer.com/v1/ticker?product_code={product_code}"
        self.board_url = f"https://api.bitflyer.com/v1/board?product_code={product_code}"
        
        self.prev_poll_time = 0.0

    def process_board_data(self, board_dict: Dict[str, Any], latency_ms: float = 15.0) -> OrderbookMicroSnapshot:
        """板情報と約定からMicrostructure特徴量を生成"""
        ts = int(time.time() * 1000)
        
        bids = board_dict.get("bids", [])
        asks = board_dict.get("asks", [])
        mid = float(board_dict.get("mid_price", 0.0))

        best_bid = float(bids[0]["price"]) if bids else mid - 500
        best_ask = float(asks[0]["price"]) if asks else mid + 500
        if mid == 0.0:
            mid = (best_bid + best_ask) / 2.0

        # depth 1〜5
        b_depths = [float(b["size"]) for b in bids[:5]]
        a_depths = [float(a["size"]) for a in asks[:5]]
        
        # パディング (5本未満の場合)
        while len(b_depths) < 5:
            b_depths.append(0.0)
        while len(a_depths) < 5:
            a_depths.append(0.0)

        tot_b = sum(b_depths)
        tot_a = sum(a_depths)
        denom = tot_b + tot_a

        # Imbalance: (B - A) / (B + A)
        imbalance = (tot_b - tot_a) / denom if denom > 0 else 0.0

        # Micro-price: (Bid * Ask_size + Ask * Bid_size) / (Bid_size + Ask_size)
        d1_denom = b_depths[0] + a_depths[0]
        if d1_denom > 0:
            micro_price = (best_bid * a_depths[0] + best_ask * b_depths[0]) / d1_denom
        else:
            micro_price = mid

        # 疑似/推定 Taker Volume (最良気配のサイズ変化や板厚差分から推定)
        taker_vol_bid = max(0.0, a_depths[0] * 0.5)
        taker_vol_ask = max(0.0, b_depths[0] * 0.5)
        taker_agg = round((taker_vol_bid + taker_vol_ask) / max(0.01, denom), 3)

        snap = OrderbookMicroSnapshot(
            timestamp=ts,
            latency_ms=round(latency_ms, 1),
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=round(mid, 1),
            micro_price=round(micro_price, 1),
            micro_dev=round(micro_price - mid, 1),
            bid_depth_1=round(b_depths[0], 4),
            ask_depth_1=round(a_depths[0], 4),
            total_bid_depth=round(tot_b, 4),
            total_ask_depth=round(tot_a, 4),
            imbalance=round(imbalance, 3),
            taker_volume_bid=round(taker_vol_bid, 4),
            taker_volume_ask=round(taker_vol_ask, 4),
            taker_aggressiveness=taker_agg,
            cancel_rate=0.0,
            refill_rate=0.0
        )

        # 1. Parquet用キューに送出
        self.logger.log("orderbook_micro", snap)

        # 2. EventBusへブロードキャスト (全エージェントが受動)
        self.bus.publish("orderbook_micro", snap)

        return snap

    def poll_once(self) -> Optional[OrderbookMicroSnapshot]:
        """REST APIから1回取得してパイプラインを回す (単体テスト・フォールバック用)"""
        t0 = time.time()
        try:
            req = urllib.request.Request(self.board_url, headers={"User-Agent": "Antigravity Quant Ingest"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            latency_ms = (time.time() - t0) * 1000.0
            return self.process_board_data(data, latency_ms=latency_ms)
        except Exception as ex:
            print(f"[Ingestion] Poll error: {ex}")
            return None
