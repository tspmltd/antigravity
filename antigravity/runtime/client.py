import os
import sys
import time
import hmac
import hashlib
import json
import urllib.request
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

load_dotenv()


class BitflyerClient:
    """
    bitFlyer Lightning API クライアント。
    Public API（相場情報・板情報）および HMAC-SHA256 Private API（残高・建玉・発注）を統括します。
    """

    BASE_URL = "https://api.bitflyer.com"

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        enable_real_trading: bool = False,
        max_order_size: float = 0.01,
    ):
        self.api_key = api_key or os.environ.get("BITFLYER_API_KEY", "").strip()
        self.api_secret = api_secret or os.environ.get("BITFLYER_API_SECRET", "").strip()
        env_real = os.environ.get("ENABLE_REAL_TRADING", "false").lower() == "true"
        self.enable_real_trading = enable_real_trading or env_real
        self.max_order_size = float(os.environ.get("MAX_ORDER_SIZE_BTC", max_order_size))

    def _create_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        timestamp = str(int(time.time()))
        text = timestamp + method.upper() + path + body
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            text.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-SIGN": signature,
            "Content-Type": "application/json",
            "User-Agent": "Antigravity/1.0 HFT Engine",
        }

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        if not self.api_key or not self.api_secret:
            raise ValueError("bitFlyer API key and secret are required for private calls.")

        body_str = json.dumps(body) if body else ""
        url = self.BASE_URL + path
        headers = self._create_headers(method, path, body_str)

        req = urllib.request.Request(
            url,
            data=body_str.encode("utf-8") if body_str else None,
            headers=headers,
            method=method.upper(),
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                res_data = response.read().decode("utf-8")
                return json.loads(res_data) if res_data else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            raise RuntimeError(f"bitFlyer API Error ({e.code}): {err_body}")

    def get_ticker(self, product_code: str = "FX_BTC_JPY") -> Dict[str, Any]:
        url = f"{self.BASE_URL}/v1/getticker?product_code={product_code}"
        req = urllib.request.Request(url, headers={"User-Agent": "Antigravity/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_executions(self, product_code: str = "FX_BTC_JPY", count: int = 100) -> List[Dict[str, Any]]:
        url = f"{self.BASE_URL}/v1/getexecutions?product_code={product_code}&count={count}"
        req = urllib.request.Request(url, headers={"User-Agent": "Antigravity/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def send_order(
        self,
        product_code: str,
        side: str,
        size: float,
        price: Optional[float] = None,
        order_type: str = "MARKET",
        minute_to_expire: int = 10000,
    ) -> Dict[str, Any]:
        if size > self.max_order_size:
            raise ValueError(f"Order size ({size}) exceeds max limit ({self.max_order_size})")

        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")

        if not self.enable_real_trading:
            # ペーパートレードシミュレーション
            sim_id = f"JRF{int(time.time() * 1000)}"
            return {
                "status": "simulated",
                "child_order_acceptance_id": sim_id,
                "product_code": product_code,
                "side": side,
                "price": price,
                "size": size,
                "order_type": order_type,
            }

        body: Dict[str, Any] = {
            "product_code": product_code,
            "child_order_type": order_type.upper(),
            "side": side,
            "size": size,
            "minute_to_expire": minute_to_expire,
            "time_in_force": "GTC",
        }
        if order_type.upper() == "LIMIT" and price is not None:
            body["price"] = int(price)

        return self._request("POST", "/v1/me/sendchildorder", body)
