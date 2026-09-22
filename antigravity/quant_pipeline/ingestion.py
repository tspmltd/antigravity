"""
Market Data Ingestion Engine (1本化ストリーム & 特徴量算出)
- bitFlyerから板・約定・Tickerを受信
- depth 1-5, mid, micro-price, imbalance, taker volume, latency を計算
- orderbook_micro を Parquet へ非同期投入 & EventBus へブロードキャスト
"""
import time
import json
import queue
import threading
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
        self.exec_url = f"https://api.bitflyer.com/v1/executions?product_code={product_code}&count=100"
        self.prev_poll_time = 0.0
        self.last_exec_id = None
        self.prev_bids = None
        self.prev_asks = None

    def process_board_data(self, board_dict: Dict[str, Any], latency_ms: float = 15.0, persist: bool = True) -> OrderbookMicroSnapshot:
        """板情報と約定からMicrostructure特徴量を生成"""
        ts = int(time.time() * 1000)
        
        bids = board_dict.get("bids", [])
        asks = board_dict.get("asks", [])
        mid = float(board_dict.get("mid_price", 0.0))

        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0
        if mid == 0.0:
            if best_bid > 0.0 and best_ask > 0.0:
                mid = (best_bid + best_ask) / 2.0
            else:
                mid = best_bid or best_ask

        # depth 1〜5
        b_depths = [float(b["size"]) for b in bids[:5]]
        a_depths = [float(a["size"]) for a in asks[:5]]
        
        # パディング (5本未満の場合)
        while len(b_depths) < 5:
            b_depths.append(0.0)
        while len(a_depths) < 5:
            a_depths.append(0.0)

        # 合計は届いた板の全段。上位5枚だけに切らない。
        tot_b = sum(float(b.get("size", 0.0)) for b in bids)
        tot_a = sum(float(a.get("size", 0.0)) for a in asks)
        denom = tot_b + tot_a

        # Imbalance: (B - A) / (B + A)
        imbalance = (tot_b - tot_a) / denom if denom > 0 else 0.0

        # Micro-price: (Bid * Ask_size + Ask * Bid_size) / (Bid_size + Ask_size)
        if b_depths[0] > 0 and a_depths[0] > 0:
            micro_price = (best_bid * a_depths[0] + best_ask * b_depths[0]) / (b_depths[0] + a_depths[0])
        elif b_depths[0] > 0:
            micro_price = best_bid
        elif a_depths[0] > 0:
            micro_price = best_ask
        else:
            micro_price = mid

        # 成行は約定だけ。板厚からは作らない。
        taker_vol_bid = float(board_dict.get("taker_volume_bid", 0.0) or 0.0)
        taker_vol_ask = float(board_dict.get("taker_volume_ask", 0.0) or 0.0)
        taker_agg = round((taker_vol_bid + taker_vol_ask) / denom, 3) if denom > 0 else 0.0
        cancel_rate = float(board_dict.get("cancel_rate", 0.0) or 0.0)
        refill_rate = float(board_dict.get("refill_rate", 0.0) or 0.0)
        last_sell_price = float(board_dict.get("last_sell_price", 0.0) or 0.0)
        last_buy_price = float(board_dict.get("last_buy_price", 0.0) or 0.0)

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
            cancel_rate=round(cancel_rate, 4),
            refill_rate=round(refill_rate, 4),
            last_sell_price=last_sell_price,
            last_buy_price=last_buy_price,
        )

        if persist:
            self.logger.log("orderbook_micro", snap)

        # 2. EventBusへブロードキャスト (全エージェントが受動)
        self.bus.publish("orderbook_micro", snap)

        return snap

    def _fetch_json(self, url: str) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": "Antigravity Quant Ingest"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _book_map(levels: List[Dict[str, Any]]) -> Dict[float, float]:
        book: Dict[float, float] = {}
        for lv in levels or []:
            price = round(float(lv.get("price") or 0.0), 1)
            size = float(lv.get("size") or 0.0)
            if price > 0 and size > 0:
                book[price] = size
        return book

    @staticmethod
    def _side_flow(prev: Dict[float, float], curr: Dict[float, float], traded: Dict[float, float]) -> tuple:
        """前回板と今回板と、その価格の約定量からキャンセルと補充を分ける。"""
        cancel_qty = 0.0
        refill_qty = 0.0
        trade_qty = 0.0
        for price in set(prev) | set(curr) | set(traded):
            before = prev.get(price, 0.0)
            after = curr.get(price, 0.0)
            hit = traded.get(price, 0.0)
            residual = after - before + hit
            trade_qty += hit
            if residual >= 0:
                refill_qty += residual
            else:
                cancel_qty += -residual
        return cancel_qty, refill_qty, trade_qty

    def _new_taker_volumes(self) -> tuple:
        """前回以降の約定。SELL は買い板、BUY は売り板。価格別の量も返す。"""
        rows = self._fetch_json(self.exec_url)
        empty = (0.0, 0.0, {}, {})
        if not isinstance(rows, list) or not rows:
            return empty
        ids = [int(r.get("id") or 0) for r in rows]
        max_id = max(ids)
        if self.last_exec_id is None:
            self.last_exec_id = max_id
            return empty
        bid_v = 0.0
        ask_v = 0.0
        sell_at: Dict[float, float] = {}
        buy_at: Dict[float, float] = {}
        for row in rows:
            eid = int(row.get("id") or 0)
            if eid <= self.last_exec_id:
                continue
            size = float(row.get("size") or 0.0)
            price = round(float(row.get("price") or 0.0), 1)
            side = str(row.get("side") or "").upper()
            if side == "BUY":
                ask_v += size
                if price > 0:
                    buy_at[price] = buy_at.get(price, 0.0) + size
            elif side == "SELL":
                bid_v += size
                if price > 0:
                    sell_at[price] = sell_at.get(price, 0.0) + size
        self.last_exec_id = max_id
        return bid_v, ask_v, sell_at, buy_at

    def _rates_from_book_change(self, bids: List[Dict[str, Any]], asks: List[Dict[str, Any]], sell_at: Dict[float, float], buy_at: Dict[float, float]) -> tuple:
        curr_bids = self._book_map(bids)
        curr_asks = self._book_map(asks)
        if self.prev_bids is None or self.prev_asks is None:
            self.prev_bids = curr_bids
            self.prev_asks = curr_asks
            return 0.0, 0.0
        c1, r1, t1 = self._side_flow(self.prev_bids, curr_bids, sell_at)
        c2, r2, t2 = self._side_flow(self.prev_asks, curr_asks, buy_at)
        self.prev_bids = curr_bids
        self.prev_asks = curr_asks
        cancel_qty = c1 + c2
        refill_qty = r1 + r2
        trade_qty = t1 + t2
        denom = cancel_qty + refill_qty + trade_qty
        if denom <= 0:
            return 0.0, 0.0
        return cancel_qty / denom, refill_qty / denom

    def poll_once(self) -> Optional[OrderbookMicroSnapshot]:
        """板 REST と約定 REST を1回取り、成行量は約定だけを載せる。"""
        t0 = time.time()
        try:
            data = self._fetch_json(self.board_url)
            if not isinstance(data, dict):
                return None
            try:
                bid_v, ask_v, sell_at, buy_at = self._new_taker_volumes()
            except Exception as ex:
                print(f"[Ingestion] Executions error: {ex}")
                bid_v, ask_v, sell_at, buy_at = 0.0, 0.0, {}, {}
            data["taker_volume_bid"] = bid_v
            data["taker_volume_ask"] = ask_v
            cancel_rate, refill_rate = self._rates_from_book_change(
                data.get("bids") or [], data.get("asks") or [], sell_at, buy_at
            )
            data["cancel_rate"] = cancel_rate
            data["refill_rate"] = refill_rate
            latency_ms = (time.time() - t0) * 1000.0
            return self.process_board_data(data, latency_ms=latency_ms)
        except Exception as ex:
            print(f"[Ingestion] Poll error: {ex}")
            return None


class LiveBoardFeed:
    """板スナップショット、板差分、約定をプッシュで受け、2秒待ちなしでスナップショットを出す。"""

    def __init__(self, ingestion: MarketDataIngestion):
        self.ingestion = ingestion
        self.out: "queue.Queue" = queue.Queue(maxsize=500)
        self.running = False
        self.connected = False
        self.message_count = 0
        self.last_error = ""
        self._lock = threading.Lock()
        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}
        self._prev_bids = None
        self._prev_asks = None
        self._sell_at: Dict[float, float] = {}
        self._buy_at: Dict[float, float] = {}
        self._bid_v = 0.0
        self._ask_v = 0.0
        self._last_persist = 0.0
        self._thread = None

    def start(self) -> None:
        import websocket
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="live-board-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False

    def get(self, timeout: float = 0.5):
        try:
            return self.out.get(timeout=timeout)
        except queue.Empty:
            return None

    def note_execution(self, row: Dict[str, Any]) -> None:
        size = float(row.get("size") or 0.0)
        price = round(float(row.get("price") or 0.0), 1)
        side = str(row.get("side") or "").upper()
        if size <= 0 or price <= 0:
            return
        with self._lock:
            if side == "BUY":
                self._ask_v += size
                self._buy_at[price] = self._buy_at.get(price, 0.0) + size
            elif side == "SELL":
                self._bid_v += size
                self._sell_at[price] = self._sell_at.get(price, 0.0) + size

    def replace_book(self, bids, asks) -> None:
        with self._lock:
            self._bids = MarketDataIngestion._book_map(bids)
            self._asks = MarketDataIngestion._book_map(asks)
            self._prev_bids = None
            self._prev_asks = None

    def apply_diff(self, bids, asks) -> None:
        with self._lock:
            for lv in bids or []:
                self._apply_level(self._bids, lv)
            for lv in asks or []:
                self._apply_level(self._asks, lv)

    @staticmethod
    def _apply_level(book: Dict[float, float], lv: Dict[str, Any]) -> None:
        price = round(float(lv.get("price") or 0.0), 1)
        size = float(lv.get("size") or 0.0)
        if price <= 0:
            return
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    def emit(self, latency_ms: float = 1.0):
        with self._lock:
            curr_b = dict(self._bids)
            curr_a = dict(self._asks)
            if not curr_b and not curr_a:
                self._sell_at = {}
                self._buy_at = {}
                self._bid_v = 0.0
                self._ask_v = 0.0
                self._prev_bids = curr_b
                self._prev_asks = curr_a
                return None
            sell_at = dict(self._sell_at)
            buy_at = dict(self._buy_at)
            bid_v = self._bid_v
            ask_v = self._ask_v
            prev_b = None if self._prev_bids is None else dict(self._prev_bids)
            prev_a = None if self._prev_asks is None else dict(self._prev_asks)
            self._sell_at = {}
            self._buy_at = {}
            self._bid_v = 0.0
            self._ask_v = 0.0
            self._prev_bids = curr_b
            self._prev_asks = curr_a
        if prev_b is None or prev_a is None:
            cancel_rate, refill_rate = 0.0, 0.0
        else:
            c1, r1, t1 = MarketDataIngestion._side_flow(prev_b, curr_b, sell_at)
            c2, r2, t2 = MarketDataIngestion._side_flow(prev_a, curr_a, buy_at)
            denom = c1 + c2 + r1 + r2 + t1 + t2
            if denom <= 0:
                cancel_rate, refill_rate = 0.0, 0.0
            else:
                cancel_rate = (c1 + c2) / denom
                refill_rate = (r1 + r2) / denom
        best_bid = max(curr_b) if curr_b else 0.0
        best_ask = min(curr_a) if curr_a else 0.0
        if best_bid > 0.0 and best_ask > 0.0:
            mid = (best_bid + best_ask) / 2.0
        else:
            mid = best_bid or best_ask
        board = {
            "mid_price": mid,
            "bids": [{"price": px, "size": sz} for px, sz in sorted(curr_b.items(), reverse=True)],
            "asks": [{"price": px, "size": sz} for px, sz in sorted(curr_a.items())],
            "taker_volume_bid": bid_v,
            "taker_volume_ask": ask_v,
            "cancel_rate": cancel_rate,
            "refill_rate": refill_rate,
            "last_sell_price": min(sell_at) if sell_at else 0.0,
            "last_buy_price": max(buy_at) if buy_at else 0.0,
        }
        now = time.time()
        persist = (now - self._last_persist) >= 0.25
        snap = self.ingestion.process_board_data(board, latency_ms=latency_ms, persist=persist)
        if persist:
            self._last_persist = now
        try:
            self.out.put_nowait(snap)
        except queue.Full:
            try:
                self.out.get_nowait()
            except queue.Empty:
                pass
            try:
                self.out.put_nowait(snap)
            except queue.Full:
                pass
        return snap

    def _loop(self) -> None:
        import websocket
        product = self.ingestion.product_code
        snap_ch = f"lightning_board_snapshot_{product}"
        diff_ch = f"lightning_board_{product}"
        exec_ch = f"lightning_executions_{product}"
        url = "wss://ws.lightstream.bitflyer.com/json-rpc"
        while self.running:
            try:
                def on_message(ws, msg_str):
                    self.message_count += 1
                    try:
                        data = json.loads(msg_str)
                    except Exception:
                        return
                    if data.get("method") != "channelMessage":
                        return
                    params = data.get("params") or {}
                    ch = params.get("channel")
                    message = params.get("message")
                    if ch == snap_ch and isinstance(message, dict):
                        self.replace_book(message.get("bids") or [], message.get("asks") or [])
                        self.emit(1.0)
                    elif ch == diff_ch and isinstance(message, dict):
                        self.apply_diff(message.get("bids") or [], message.get("asks") or [])
                        self.emit(1.0)
                    elif ch == exec_ch and isinstance(message, list):
                        for row in message:
                            self.note_execution(row)
                        self.emit(1.0)

                def on_open(ws):
                    self.connected = True
                    for ch in (snap_ch, diff_ch, exec_ch):
                        ws.send(json.dumps({"method": "subscribe", "params": {"channel": ch}}))

                def on_error(ws, error):
                    if error:
                        self.last_error = str(error)

                def on_close(ws, status, msg):
                    self.connected = False

                app = websocket.WebSocketApp(url, on_message=on_message, on_open=on_open, on_error=on_error, on_close=on_close)
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as ex:
                self.last_error = str(ex)
            self.connected = False
            if self.running:
                time.sleep(1.0)
