#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bitFlyer Lightning Private WS — child_order_events (P0-3).

Auth + subscribe; enqueue EXECUTION events for the LIVE runner to claim/apply
without waiting on the public executions.jsonl feeder (~seconds lag).

P0b fix: subscribe ONLY after auth ACK (id=1). Sending subscribe in on_open
before auth completes yields JSON-RPC -32007 'authentication required' and
zero EXECUTION delivery (LIVE session: exec_n=0, errors=116).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, List, Optional

try:
    import websocket  # websocket-client
except Exception:  # pragma: no cover
    websocket = None  # type: ignore

WS_URL = "wss://ws.lightstream.bitflyer.com/json-rpc"
CHANNEL = "child_order_events"


class PrivateChildOrderStream:
    """Background Lightning private stream for child_order_events."""

    def __init__(
        self,
        *,
        product_code: str = "FX_BTC_JPY",
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        max_queue: int = 500,
    ) -> None:
        self.product_code = str(product_code)
        self.api_key = api_key or os.environ.get("BITFLYER_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("BITFLYER_API_SECRET", "")
        self._q: Deque[Dict[str, Any]] = deque(maxlen=int(max_queue))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws_app: Any = None
        self._active_ws: Any = None
        self.connected = False
        self.authed = False
        self.subscribed = False
        self.recv_n = 0
        self.exec_n = 0
        self.errors = 0
        self.auth_ok_n = 0
        self.subscribe_ok_n = 0
        self.last_event_ts = 0.0
        self.last_error = ""
        self.dropped_non_exec = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_secret and websocket is not None)

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever,
            name="bf-private-child-orders",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        app = self._ws_app
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self.connected = False
        self.authed = False
        self.subscribed = False
        self._active_ws = None

    def drain(self, max_n: int = 64) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        with self._lock:
            while self._q and len(out) < max_n:
                out.append(self._q.popleft())
        return out

    def push_for_test(self, event: Dict[str, Any]) -> None:
        """Unit-test injection (no WS)."""
        with self._lock:
            self._q.append(dict(event))

    def _sign(self, timestamp: str, nonce: str) -> str:
        msg = f"{timestamp}{nonce}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _enqueue(self, ev: Dict[str, Any]) -> None:
        with self._lock:
            self._q.append(ev)
        self.exec_n += 1
        self.last_event_ts = time.time()

    def _send_subscribe(self, ws: Any) -> None:
        sub = {"method": "subscribe", "params": {"channel": CHANNEL}, "id": 2}
        ws.send(json.dumps(sub))

    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        self.recv_n += 1
        # JSON-RPC responses — id 1 auth, id 2 subscribe
        if "result" in msg and msg.get("error") is None:
            rid = msg.get("id")
            if rid == 1 and msg.get("result") is True:
                self.authed = True
                self.auth_ok_n += 1
                # P0b: subscribe only after auth ACK (never in on_open alone).
                ws = self._active_ws
                if ws is not None and not self.subscribed:
                    try:
                        self._send_subscribe(ws)
                    except Exception as exc:
                        self.errors += 1
                        self.last_error = f"subscribe_send:{exc}"
                return
            if rid == 2 and msg.get("result") is True:
                self.subscribed = True
                self.subscribe_ok_n += 1
                return
            return
        if msg.get("error"):
            self.errors += 1
            self.last_error = str(msg.get("error"))
            # Auth failure: do not keep pretending we can subscribe.
            if msg.get("id") == 1:
                self.authed = False
            return
        params = msg.get("params") or {}
        channel = str(params.get("channel") or "")
        if channel != CHANNEL:
            return
        body = params.get("message")
        rows = body if isinstance(body, list) else ([body] if isinstance(body, dict) else [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            pc = str(row.get("product_code") or "")
            if pc and pc != self.product_code:
                continue
            et = str(row.get("event_type") or "").upper()
            if et != "EXECUTION":
                self.dropped_non_exec += 1
                continue
            # Acceptance id — tolerate key variants seen on Lightning.
            oid = str(
                row.get("child_order_acceptance_id")
                or row.get("child_order_acceptanceId")
                or row.get("acceptance_id")
                or ""
            )
            side = str(row.get("side") or "").upper()
            try:
                px = float(row.get("price") or 0.0)
                sz = float(row.get("size") or 0.0)
            except Exception:
                continue
            if not oid or side not in ("BUY", "SELL") or px <= 0 or sz <= 0:
                continue
            self._enqueue(
                {
                    "src": "PRIVATE_WS",
                    "event_type": et,
                    "id": oid,
                    "side": side,
                    "px": px,
                    "size": sz,
                    "product_code": self.product_code,
                    "ts": time.time(),
                    "raw": row,
                }
            )

    def _on_open(self, ws: Any) -> None:
        self.connected = True
        self.authed = False
        self.subscribed = False
        self._active_ws = ws
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        auth = {
            "method": "auth",
            "params": {
                "api_key": self.api_key,
                "timestamp": int(ts),
                "nonce": nonce,
                "signature": self._sign(ts, nonce),
            },
            "id": 1,
        }
        # Auth only — subscribe waits for result id=1 in _handle_message.
        ws.send(json.dumps(auth))

    def _run_forever(self) -> None:
        assert websocket is not None
        while not self._stop.is_set():
            self.connected = False
            self.authed = False
            self.subscribed = False
            self._active_ws = None

            def on_message(_ws: Any, message: str) -> None:
                try:
                    self._handle_message(message)
                except Exception as exc:
                    self.errors += 1
                    self.last_error = str(exc)

            def on_error(_ws: Any, error: Any) -> None:
                self.errors += 1
                self.last_error = str(error)

            def on_close(_ws: Any, *_a: Any) -> None:
                self.connected = False
                self.authed = False
                self.subscribed = False
                self._active_ws = None

            def on_open(ws: Any) -> None:
                try:
                    self._on_open(ws)
                except Exception as exc:
                    self.errors += 1
                    self.last_error = str(exc)

            app = websocket.WebSocketApp(
                WS_URL,
                on_message=on_message,
                on_open=on_open,
                on_error=on_error,
                on_close=on_close,
            )
            self._ws_app = app
            closer = threading.Thread(
                target=self._close_when_stopped,
                args=(app,),
                name="bf-private-ws-stop",
                daemon=True,
            )
            closer.start()
            try:
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self.errors += 1
                self.last_error = str(exc)
            if self._stop.is_set():
                break
            time.sleep(1.0)

    def _close_when_stopped(self, app: Any) -> None:
        while not self._stop.wait(0.2):
            continue
        try:
            app.close()
        except Exception:
            pass
