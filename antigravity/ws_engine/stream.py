import time
import json
import urllib.request
import threading
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Callable

try:
    import websocket
except ImportError:
    websocket = None

from .flow_analyzer import FlowAnalyzer
from .orderbook import OrderBookTracker


class WebSocketTickStream:
    """
    bitFlyer Lightning リアルタイム約定 ＆ 板情報(Ticker) WebSocket ストリーム処理エンジン。
    ミリ秒単位のプッシュ駆動で約定データおよび気配値を受信し、
    仲値（Mid）、板厚不均衡（Imbalance）、Taker Delta を高速算出します。
    """

    DEFAULT_WS_URL = "wss://ws.lightstream.bitflyer.com/json-rpc"
    DEFAULT_REST_URL = "https://api.bitflyer.com/v1/executions?product_code="
    DEFAULT_TICKER_URL = "https://api.bitflyer.com/v1/ticker?product_code="

    def __init__(
        self,
        product_code: str = "FX_BTC_JPY",
        ws_url: str = DEFAULT_WS_URL,
        window_seconds: float = 15.0,
        max_buffer_size: int = 2000,
        on_ticks_callback: Optional[Callable[[List[Dict[str, Any]], Dict[str, Any]], None]] = None,
        on_connect_callback: Optional[Callable[[], None]] = None,
        on_disconnect_callback: Optional[Callable[[str], None]] = None,
        deadzone_theta: float = 0.10,
    ):
        self.product_code = product_code
        self.ws_url = ws_url
        self.window_seconds = window_seconds
        self.max_buffer_size = max_buffer_size
        self.on_ticks_callback = on_ticks_callback
        self.on_connect_callback = on_connect_callback
        self.on_disconnect_callback = on_disconnect_callback

        self.analyzer = FlowAnalyzer(window_seconds=window_seconds)
        self.orderbook = OrderBookTracker(deadzone_theta=deadzone_theta)
        self.ticks: List[Dict[str, Any]] = []
        self.seen_tick_ids = set()
        self.current_price = 0.0
        self.current_mid = 0.0
        self.last_fetch_time = 0.0
        self.last_push_time = 0.0

        self.lock = threading.Lock()
        self.ws_app: Optional[Any] = None
        self.ws_thread: Optional[threading.Thread] = None
        self.is_running = False
        self.is_ws_connected = False

    def add_ticks(self, raw_ticks: List[Dict[str, Any]]) -> int:
        with self.lock:
            added_count = 0
            new_added_ticks = []
            book_snap = self.orderbook.get_snapshot()
            mid = book_snap["mid_price"] if book_snap["is_valid"] else self.current_price

            for t in raw_ticks:
                t_id = t.get("id")
                if t_id and t_id in self.seen_tick_ids:
                    continue

                dt_raw = t.get("exec_date")
                if isinstance(dt_raw, str):
                    try:
                        dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))
                    except Exception:
                        dt = datetime.now(timezone.utc)
                elif isinstance(dt_raw, datetime):
                    dt = dt_raw
                else:
                    dt = datetime.now(timezone.utc)

                price = float(t.get("price", 0.0))
                size = float(t.get("size", 0.0))
                side = str(t.get("side", "")).upper()
                tick_mid = mid if mid > 0 else price

                tick_entry = {
                    "id": t_id,
                    "price": price,
                    "mid": tick_mid,
                    "size": size,
                    "side": side,
                    "dt": dt,
                    "timestamp": dt.timestamp(),
                }

                self.ticks.append(tick_entry)
                new_added_ticks.append(tick_entry)
                if t_id:
                    self.seen_tick_ids.add(t_id)
                added_count += 1

                if price > 0:
                    self.current_price = price
                if tick_mid > 0:
                    self.current_mid = tick_mid

            if len(self.ticks) > self.max_buffer_size:
                self.ticks = self.ticks[-self.max_buffer_size:]
                if len(self.seen_tick_ids) > self.max_buffer_size * 2:
                    self.seen_tick_ids = {tk["id"] for tk in self.ticks if tk.get("id")}

            if added_count > 0:
                self.ticks.sort(key=lambda x: x["timestamp"])

        if added_count > 0 and self.on_ticks_callback:
            try:
                stats = self.compute_flow_stats()
                self.on_ticks_callback(new_added_ticks, stats)
            except Exception as e:
                print(f"[WebSocketTickStream] Callback error: {e}", flush=True)

        return added_count


    def fetch_latest_ticks(self, count: int = 100) -> int:
        # 1. Ticker情報をRESTから初期取得してMid気配値をセット
        try:
            ticker_url = f"{self.DEFAULT_TICKER_URL}{self.product_code}"
            req_t = urllib.request.Request(
                ticker_url,
                headers={"User-Agent": "Mozilla/5.0 (Antigravity HFT Engine)"}
            )
            with urllib.request.urlopen(req_t, timeout=5) as resp_t:
                t_data = json.loads(resp_t.read().decode("utf-8"))
                if isinstance(t_data, dict):
                    self.orderbook.update_from_ticker(t_data)
                    self.current_mid = self.orderbook.mid_price
        except Exception:
            pass

        # 2. 直近約定データをRESTから取得
        url = f"{self.DEFAULT_REST_URL}{self.product_code}&count={count}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Antigravity HFT Engine)"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    added = self.add_ticks(data)
                    self.last_fetch_time = time.time()
                    return added
        except Exception:
            pass
        return 0

    def start(self):
        if websocket is None:
            raise ImportError("websocket-client is required. Run pip install websocket-client.")
        if self.is_running:
            return
        self.is_running = True
        self.ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self.ws_thread.start()

    def stop(self):
        self.is_running = False
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass
        self.is_ws_connected = False

    def _ws_loop(self):
        while self.is_running:
            try:
                exec_channel = f"lightning_executions_{self.product_code}"
                ticker_channel = f"lightning_ticker_{self.product_code}"

                def on_message(ws, msg_str):
                    self.last_push_time = time.time()
                    try:
                        data = json.loads(msg_str)
                        if data.get("method") == "channelMessage":
                            params = data.get("params", {})
                            ch = params.get("channel")
                            if ch == exec_channel:
                                ticks_data = params.get("message", [])
                                if isinstance(ticks_data, list) and ticks_data:
                                    self.add_ticks(ticks_data)
                            elif ch == ticker_channel:
                                ticker_data = params.get("message", {})
                                if isinstance(ticker_data, dict) and ticker_data:
                                    self.orderbook.update_from_ticker(ticker_data)
                                    if self.orderbook.mid_price > 0:
                                        self.current_mid = self.orderbook.mid_price
                    except Exception:
                        pass

                def on_open(ws):
                    self.is_ws_connected = True
                    for ch in [exec_channel, ticker_channel]:
                        sub = {
                            "method": "subscribe",
                            "params": {"channel": ch}
                        }
                        ws.send(json.dumps(sub))
                    print(f"[WebSocket] Connected to {exec_channel} & {ticker_channel} in real-time mode.", flush=True)
                    if self.on_connect_callback:
                        try:
                            self.on_connect_callback()
                        except Exception as ex:
                            print(f"[WebSocket] on_connect_callback error: {ex}", flush=True)

                def on_error(ws, error):
                    if error:
                        print(f"[WebSocket] Error: {error}", flush=True)

                def on_close(ws, close_status_code, close_msg):
                    was_connected = self.is_ws_connected
                    self.is_ws_connected = False
                    reason = f"code={close_status_code}, msg={close_msg}"
                    print(f"[WebSocket] Closed ({reason})", flush=True)
                    if was_connected and self.on_disconnect_callback and self.is_running:
                        try:
                            self.on_disconnect_callback(reason)
                        except Exception as ex:
                            print(f"[WebSocket] on_disconnect_callback error: {ex}", flush=True)

                self.ws_app = websocket.WebSocketApp(
                    self.ws_url,
                    on_message=on_message,
                    on_open=on_open,
                    on_error=on_error,
                    on_close=on_close,
                )
                self.ws_app.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                pass

            if self.is_running:
                time.sleep(1.0)

    def compute_flow_stats(self, window_sec: Optional[float] = None) -> Dict[str, Any]:
        with self.lock:
            ticks_copy = list(self.ticks)
            curr_p = self.current_price
            curr_m = self.current_mid if self.current_mid > 0 else curr_p

        stats = self.analyzer.compute_flow_stats(
            ticks_copy,
            current_price=curr_m,
            window_sec=window_sec
        )

        # 板情報（Mid, Microprice, Imbalance, Spread）をマージ
        book_snap = self.orderbook.get_snapshot()
        stats.update({
            "mid_price": book_snap["mid_price"] if book_snap["mid_price"] > 0 else curr_m,
            "best_bid": book_snap["best_bid"],
            "best_ask": book_snap["best_ask"],
            "spread": book_snap["spread"],
            "spread_bp": book_snap["spread_bp"],
            "book_imbalance": book_snap["book_imbalance"],
            "top_imbalance": book_snap["top_imbalance"],
            "microprice": book_snap["microprice"] if book_snap["microprice"] > 0 else curr_m,
            "book_is_valid": book_snap["is_valid"],
        })
        return stats

