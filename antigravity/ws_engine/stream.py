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


class WebSocketTickStream:
    """
    bitFlyer Lightning リアルタイム約定 WebSocket ストリーム処理エンジン。
    ミリ秒単位のプッシュ駆動で約定データを受信し、Taker Delta と指標を高速算出します。
    """

    DEFAULT_WS_URL = "wss://ws.lightstream.bitflyer.com/json-rpc"
    DEFAULT_REST_URL = "https://api.bitflyer.com/v1/executions?product_code="

    def __init__(
        self,
        product_code: str = "FX_BTC_JPY",
        ws_url: str = DEFAULT_WS_URL,
        window_seconds: float = 15.0,
        max_buffer_size: int = 2000,
        on_ticks_callback: Optional[Callable[[List[Dict[str, Any]], Dict[str, Any]], None]] = None,
    ):
        self.product_code = product_code
        self.ws_url = ws_url
        self.window_seconds = window_seconds
        self.max_buffer_size = max_buffer_size
        self.on_ticks_callback = on_ticks_callback

        self.analyzer = FlowAnalyzer(window_seconds=window_seconds)
        self.ticks: List[Dict[str, Any]] = []
        self.seen_tick_ids = set()
        self.current_price = 0.0
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

                tick_entry = {
                    "id": t_id,
                    "price": price,
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
                channel = f"lightning_executions_{self.product_code}"

                def on_message(ws, msg_str):
                    self.last_push_time = time.time()
                    try:
                        data = json.loads(msg_str)
                        if data.get("method") == "channelMessage":
                            params = data.get("params", {})
                            if params.get("channel") == channel:
                                ticks_data = params.get("message", [])
                                if isinstance(ticks_data, list) and ticks_data:
                                    self.add_ticks(ticks_data)
                    except Exception:
                        pass

                def on_open(ws):
                    self.is_ws_connected = True
                    sub = {
                        "method": "subscribe",
                        "params": {"channel": channel}
                    }
                    ws.send(json.dumps(sub))
                    print(f"[WebSocket] Connected to {channel} in real-time mode.", flush=True)

                def on_error(ws, error):
                    pass

                def on_close(ws, close_status_code, close_msg):
                    self.is_ws_connected = False

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
        return self.analyzer.compute_flow_stats(ticks_copy, current_price=curr_p, window_sec=window_sec)
