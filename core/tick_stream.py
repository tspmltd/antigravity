import time
import json
import urllib.request
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Callable

try:
    import websocket
except ImportError:
    websocket = None


class TickStream:
    """
    bitFlyer FX リアルタイム・約定ストリーム (Tick Stream) 処理エンジン。
    【ミリ秒（ms）完全プッシュ駆動】
    JSON-RPC 2.0 over WebSocket (wss://ws.lightstream.bitflyer.com/json-rpc) を常時接続し、
    取引所で約定が発生した瞬間にミリ秒単位でプッシュ受信・Taker Delta判定・コールバック発火を行う。
    
    【提供メトリクス】
    - 現在価格 (Current Price / LTP)
    - 直近 window 秒間の価格変化 (Price Change in basis points: bp)
    - Taker Buy Volume / Taker Sell Volume / Net Delta / Delta Ratio (-1.0 〜 +1.0)
    - 2bp 初動検知 (is_2bp_momentum_up / is_2bp_momentum_down)
    - フロー同方向継続性 (persistence_score: 0.0 〜 1.0)
    - 逆方向ノイズ比率 (opposite_noise_ratio: 0.0 〜 1.0)
    """

    EXECUTIONS_URL = "https://api.bitflyer.com/v1/executions?product_code="
    WS_URL = "wss://ws.lightstream.bitflyer.com/json-rpc"

    def __init__(
        self,
        product_code: str = "FX_BTC_JPY",
        window_seconds: float = 15.0,
        max_buffer_size: int = 2000,
        on_ticks_callback: Optional[Callable[[List[Dict[str, Any]], Dict[str, Any]], None]] = None,
    ):
        self.product_code = product_code
        self.window_seconds = window_seconds
        self.max_buffer_size = max_buffer_size
        self.on_ticks_callback = on_ticks_callback

        self.ticks: List[Dict[str, Any]] = []
        self.seen_tick_ids = set()
        self.current_price = 0.0
        self.last_fetch_time = 0.0

        self.lock = threading.Lock()
        self.ws_app: Optional[Any] = None
        self.ws_thread: Optional[threading.Thread] = None
        self.is_running = False
        self.is_ws_connected = False
        self.last_push_time = 0.0

    def add_ticks(self, raw_ticks: List[Dict[str, Any]]) -> int:
        """
        新着約定データを重複排除してバッファに追加。
        追加された新規約定件数を返す。
        """
        with self.lock:
            added_count = 0
            new_added_ticks = []
            for t in raw_ticks:
                t_id = t.get("id")
                if t_id and t_id in self.seen_tick_ids:
                    continue

                # 日時パース
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
                    "timestamp": dt.timestamp()
                }

                self.ticks.append(tick_entry)
                new_added_ticks.append(tick_entry)
                if t_id:
                    self.seen_tick_ids.add(t_id)
                added_count += 1

                if price > 0:
                    self.current_price = price

            # バッファ上限ガード
            if len(self.ticks) > self.max_buffer_size:
                self.ticks = self.ticks[-self.max_buffer_size:]
                # 古いIDのガベージコレクション
                if len(self.seen_tick_ids) > self.max_buffer_size * 2:
                    keep_ids = {tk["id"] for tk in self.ticks if tk.get("id")}
                    self.seen_tick_ids = keep_ids

            # 時系列昇順ソート
            if added_count > 0:
                self.ticks.sort(key=lambda x: x["timestamp"])

        # コールバックが登録されていればロック外でミリ秒即座に発火
        if added_count > 0 and self.on_ticks_callback:
            try:
                stats = self.compute_flow_stats()
                self.on_ticks_callback(new_added_ticks, stats)
            except Exception as e:
                print(f"[TickStream] コールバック実行エラー: {e}", flush=True)

        return added_count

    def fetch_latest_ticks(self, count: int = 100) -> int:
        """bitFlyer REST APIから最新の約定ストリームを取得してバッファに追加"""
        url = f"{self.EXECUTIONS_URL}{self.product_code}&count={count}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (GapcorePJ TickStreamEngine)"}
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

    # ------------------------------------------------------------------
    # WebSocket リアルタイム常時接続・ミリ秒プッシュハンドラ
    # ------------------------------------------------------------------
    def start_websocket(self):
        """WebSocket接続を開始（別スレッドで常時受信＆自動再接続）"""
        if websocket is None:
            raise ImportError("websocket-client がインストールされていません。")

        if self.is_running:
            return

        self.is_running = True
        self.ws_thread = threading.Thread(target=self._ws_worker_loop, daemon=True)
        self.ws_thread.start()

    def stop_websocket(self):
        """WebSocket接続を停止"""
        self.is_running = False
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass
        self.is_ws_connected = False

    def _ws_worker_loop(self):
        """切断時に自動再接続を繰り返す常駐ワーカー"""
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
                    except Exception as ex:
                        pass

                def on_open(ws):
                    self.is_ws_connected = True
                    sub = {
                        "method": "subscribe",
                        "params": {"channel": channel}
                    }
                    ws.send(json.dumps(sub))
                    print(f"[WebSocket] ⚡ bitFlyer Lightning FX ({channel}) にミリ秒常時接続完了！", flush=True)

                def on_error(ws, error):
                    pass

                def on_close(ws, close_status_code, close_msg):
                    self.is_ws_connected = False

                self.ws_app = websocket.WebSocketApp(
                    self.WS_URL,
                    on_message=on_message,
                    on_open=on_open,
                    on_error=on_error,
                    on_close=on_close,
                )
                self.ws_app.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                pass

            if self.is_running:
                time.sleep(1.0)  # 再接続ウェイト

    def compute_flow_stats(self, window_sec: Optional[float] = None) -> Dict[str, Any]:
        """
        直近 window_sec 秒間のTaker約定フローと価格変動(bp)を算出
        """
        win = window_sec or self.window_seconds
        with self.lock:
            if not self.ticks:
                return self._empty_stats()

            latest_tick = self.ticks[-1]
            latest_ts = latest_tick["timestamp"]
            cutoff_ts = latest_ts - win

            # スライディングウィンドウ抽出
            window_ticks = [tk for tk in self.ticks if tk["timestamp"] >= cutoff_ts]
            if not window_ticks:
                return self._empty_stats()

            curr_p = latest_tick["price"]
            start_p = window_ticks[0]["price"]

            # 価格変化率 (basis points: 1bp = 0.01% = 0.0001)
            price_diff = curr_p - start_p
            price_change_bp = (price_diff / start_p * 10000.0) if start_p > 0 else 0.0

            # Taker Buy / Sell 集計
            buy_vol = sum(tk["size"] for tk in window_ticks if tk["side"] == "BUY")
            sell_vol = sum(tk["size"] for tk in window_ticks if tk["side"] == "SELL")
            total_vol = buy_vol + sell_vol

            if total_vol <= 1e-9:
                return self._empty_stats()

            net_delta = buy_vol - sell_vol
            delta_ratio = net_delta / total_vol

            # 逆方向ノイズ比率
            if net_delta > 0:
                opposite_noise_ratio = sell_vol / total_vol
            elif net_delta < 0:
                opposite_noise_ratio = buy_vol / total_vol
            else:
                opposite_noise_ratio = 0.5

            # 2bp強モメンタム判定 (従来基準)
            is_2bp_up = (price_change_bp >= 2.0) and (delta_ratio >= 0.5) and (opposite_noise_ratio <= 0.30)
            is_2bp_down = (price_change_bp <= -2.0) and (delta_ratio <= -0.5) and (opposite_noise_ratio <= 0.30)

            # 1.2bp〜1.5bp 微小初動モメンタム判定 (高感度・早期先回りエントリー用)
            is_micro_up = (
                (price_change_bp >= 1.2 and delta_ratio >= 0.35 and opposite_noise_ratio <= 0.35)
                or (price_change_bp >= 0.8 and delta_ratio >= 0.60)  # Taker圧倒先行型
            )
            is_micro_down = (
                (price_change_bp <= -1.2 and delta_ratio <= -0.35 and opposite_noise_ratio <= 0.35)
                or (price_change_bp <= -0.8 and delta_ratio <= -0.60) # Taker圧倒先行型
            )

            should_cancel_bid = (delta_ratio <= -0.6) or (price_change_bp <= -3.0)
            should_cancel_ask = (delta_ratio >= 0.6) or (price_change_bp >= 3.0)

            return {
                "current_price": curr_p,
                "start_price": start_p,
                "price_change_bp": round(price_change_bp, 2),
                "taker_buy_vol": round(buy_vol, 4),
                "taker_sell_vol": round(sell_vol, 4),
                "net_delta": round(net_delta, 4),
                "delta_ratio": round(delta_ratio, 3),
                "opposite_noise_ratio": round(opposite_noise_ratio, 3),
                "is_2bp_momentum_up": is_2bp_up,
                "is_2bp_momentum_down": is_2bp_down,
                "is_micro_momentum_up": is_micro_up,
                "is_micro_momentum_down": is_micro_down,
                "should_cancel_bid": should_cancel_bid,
                "should_cancel_ask": should_cancel_ask,
                "tick_count": len(window_ticks),
                "tick_count_in_window": len(window_ticks),
                "timestamp": latest_tick["dt"].isoformat()
            }

    def is_2bp_momentum_up(self, window_sec: Optional[float] = None, min_delta_ratio: float = 0.5) -> bool:
        stats = self.compute_flow_stats(window_sec=window_sec)
        return (stats["price_change_bp"] >= 2.0) and (stats["delta_ratio"] >= min_delta_ratio)

    def is_2bp_momentum_down(self, window_sec: Optional[float] = None, min_delta_ratio: float = -0.5) -> bool:
        stats = self.compute_flow_stats(window_sec=window_sec)
        return (stats["price_change_bp"] <= -2.0) and (stats["delta_ratio"] <= min_delta_ratio)

    def should_cancel_bid(self, window_sec: Optional[float] = None, reverse_delta_threshold: float = -0.6) -> bool:
        stats = self.compute_flow_stats(window_sec=window_sec)
        return (stats["delta_ratio"] <= reverse_delta_threshold) or (stats["price_change_bp"] <= -3.0)

    def should_cancel_ask(self, window_sec: Optional[float] = None, reverse_delta_threshold: float = 0.6) -> bool:
        stats = self.compute_flow_stats(window_sec=window_sec)
        return (stats["delta_ratio"] >= reverse_delta_threshold) or (stats["price_change_bp"] >= 3.0)

    def _empty_stats(self) -> Dict[str, Any]:
        return {
            "current_price": self.current_price,
            "start_price": self.current_price,
            "price_change_bp": 0.0,
            "taker_buy_vol": 0.0,
            "taker_sell_vol": 0.0,
            "net_delta": 0.0,
            "delta_ratio": 0.0,
            "opposite_noise_ratio": 0.0,
            "is_2bp_momentum_up": False,
            "is_2bp_momentum_down": False,
            "should_cancel_bid": False,
            "should_cancel_ask": False,
            "tick_count": 0,
            "tick_count_in_window": 0,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
