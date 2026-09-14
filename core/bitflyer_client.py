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

# .envファイルの自動ロード
load_dotenv()


class BitFlyerClient:
    """
    bitFlyer Private API クライアント (HMAC-SHA256認証 + 安全ガード機能付き)。
    残高照会、証拠金情報、建玉取得、およびペーパートレード/本番発注に対応します。
    """

    BASE_URL = "https://api.bitflyer.com"

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        enable_real_trading: bool = False,
        max_order_size: float = 0.01
    ):
        self.api_key = api_key or os.environ.get("BITFLYER_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("BITFLYER_API_SECRET", "")
        
        # 安全ガード設定 (.envの値も参照)
        env_real = os.environ.get("ENABLE_REAL_TRADING", "false").lower() == "true"
        self.enable_real_trading = enable_real_trading or env_real
        self.max_order_size = float(os.environ.get("MAX_ORDER_SIZE_BTC", max_order_size))

        if not self.api_key or not self.api_secret:
            print("[BitFlyerClient] ⚠️ APIキーまたはシークレットが設定されていません。Private APIは制限されます。")

    def _create_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """bitFlyer Private APIの認証ヘッダーを生成"""
        timestamp = str(int(time.time()))
        text = timestamp + method.upper() + path + body
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            text.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-SIGN": signature,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (GapcorePJ AlgoTrader)"
        }

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        """Private APIリクエストを実行"""
        if not self.api_key or not self.api_secret:
            raise ValueError("APIキーとシークレットを設定してください。")

        body_str = json.dumps(body) if body else ""
        url = self.BASE_URL + path
        headers = self._create_headers(method, path, body_str)

        req = urllib.request.Request(
            url,
            data=body_str.encode("utf-8") if body_str else None,
            headers=headers,
            method=method.upper()
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                res_data = response.read().decode("utf-8")
                return json.loads(res_data) if res_data else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            raise RuntimeError(f"bitFlyer APIエラー ({e.code}): {err_body}")

    def get_balance(self) -> List[Dict[str, Any]]:
        """口座残高を取得 (/v1/me/getbalance)"""
        return self._request("GET", "/v1/me/getbalance")

    def get_collateral(self) -> Dict[str, Any]:
        """証拠金状態・評価損益・維持率を取得 (/v1/me/getcollateral)"""
        return self._request("GET", "/v1/me/getcollateral")

    def get_positions(self, product_code: str = "FX_BTC_JPY") -> List[Dict[str, Any]]:
        """保有建玉一覧を取得 (/v1/me/getpositions)"""
        return self._request("GET", f"/v1/me/getpositions?product_code={product_code}")

    def send_order(
        self,
        product_code: str = "BTC_JPY",
        side: str = "BUY",
        size: float = 0.001,
        order_type: str = "MARKET",
        price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        新規注文発注 (/v1/me/sendchildorder)
        安全ガード:
        - 1回あたりの最大数量リミッター (max_order_size) を超過した場合はエラー
        - enable_real_trading が False の場合はペーパートレード (Dry Run) としてログ出力
        """
        # 1. 数量制限ガード
        if size > self.max_order_size:
            raise ValueError(
                f"[安全ガード作動] 発注数量 {size} BTC が上限 ({self.max_order_size} BTC) を超えています。"
            )

        side_upper = side.upper()
        if side_upper not in ["BUY", "SELL"]:
            raise ValueError("side は 'BUY' または 'SELL' を指定してください。")

        body = {
            "product_code": product_code,
            "child_order_type": order_type.upper(),
            "side": side_upper,
            "size": size,
        }
        if order_type.upper() == "LIMIT" and price is not None:
            body["price"] = int(price)

        # 2. Dry Run / ペーパートレード判定
        if not self.enable_real_trading:
            print(f"[BitFlyerClient] 🛡️ [DRY RUN / ペーパートレード] 仮想注文実行:")
            print(f"                銘柄: {product_code}, 区分: {side_upper}, 数量: {size}, 種別: {order_type}")
            return {
                "status": "SIMULATED",
                "message": "Real trading is disabled. Order was simulated safely.",
                "order_details": body
            }

        # 3. 本番発注
        print(f"[BitFlyerClient] 🚨 [REAL ORDER] 本番注文を送信します: {body}")
        res = self._request("POST", "/v1/me/sendchildorder", body)
        return {
            "status": "SUBMITTED",
            "child_order_acceptance_id": res.get("child_order_acceptance_id"),
            "order_details": body
        }
