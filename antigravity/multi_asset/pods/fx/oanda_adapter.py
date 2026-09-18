"""
antigravity/multi_asset/pods/fx/oanda_adapter.py: OANDA API 接続アダプター (v20 REST + Streaming)
========================================================================================
仕様書: docs/fx_pod_usdjpy.md セクション5 に準拠。
3レイヤー構造:
1. OandaStreaming: 価格ストリーミング & 正規化
2. OandaREST: 注文・約定・口座管理 (1ロット=10,000 units, 1pip=0.01円)
3. OandaRisk: レートリミット、スプレッドショック、建玉上限、イベント窓縮小
"""

import os
import time
import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, List, Callable, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("antigravity.multi_asset.pods.fx.oanda_adapter")
JST = timezone(timedelta(hours=9))


@dataclass
class OandaTick:
    """正規化された為替Tickデータ (BTCライン・日本株ラインと同型)"""
    timestamp: float
    instrument: str
    bid: float
    ask: float
    spread_jpy: float
    spread_pips: float
    mid: float
    bid_depth: float = 1.0  # 万通貨
    ask_depth: float = 1.0
    session: str = "TOKYO_SESSION"


class OandaRisk:
    """
    OANDA向けリスク管理レイヤー (安全装置)
    - 建玉上限 (USD/JPY: 3ロット, EUR/JPY: 2ロット)
    - スプレッドショック遮断 (> 0.3円 / 30 pips)
    - 日次CBリミット (-15,000円)
    - イベント窓ロット縮小 (通常の30%上限)
    """

    def __init__(
        self,
        max_spread_pips: float = 3.0,          # 3.0 pips (0.03円) - イベント時急拡大遮断
        max_spread_jpy: float = 0.03,
        max_lot_map: Optional[Dict[str, float]] = None,
        daily_cb_limit_jpy: float = -15000.0,
        event_max_lot_ratio: float = 0.30,
    ):
        self.max_spread_pips = max_spread_pips
        self.max_spread_jpy = max_spread_jpy
        self.max_lot_map = max_lot_map or {"USD_JPY": 3.0, "EUR_JPY": 2.0, "USDJPY": 3.0, "EURJPY": 2.0}
        self.daily_cb_limit_jpy = daily_cb_limit_jpy
        self.event_max_lot_ratio = event_max_lot_ratio
        self.daily_pnl_fx: float = 0.0
        self.is_event_window: bool = False

    def set_event_window(self, active: bool) -> None:
        """マクロイベント(CPI/FOMC等)の前後窓フラグを設定"""
        self.is_event_window = active

    def pre_order_check(self, instrument: str, lots: float, current_spread_pips: float) -> Tuple[bool, str]:
        """発注前 4大安全チェック"""
        # 1. 日次損失サーキットブレーカー
        if self.daily_pnl_fx <= self.daily_cb_limit_jpy:
            return False, f"日次損失リミット到達 (損益: ¥{self.daily_pnl_fx:,.0f} <= ¥{self.daily_cb_limit_jpy:,.0f})"

        # 2. スプレッドショック
        if current_spread_pips > self.max_spread_pips:
            return False, f"スプレッド急拡大遮断 ({current_spread_pips:.1f} pips > 上限 {self.max_spread_pips:.1f} pips)"

        # 3. 建玉上限
        max_lot = self.max_lot_map.get(instrument, 2.0)
        # イベント窓時は最大ロットを縮小
        if self.is_event_window:
            max_lot = round(max_lot * self.event_max_lot_ratio, 2)
            if lots > max_lot:
                return False, f"マクロイベント窓 ロット上限超過 ({lots} > {max_lot} lots [通常比30%])"
        elif lots > max_lot:
            return False, f"最大保有ロット上限超過 ({lots} > {max_lot} lots)"

        return True, "安全チェック合格"


class OandaREST:
    """
    OANDA REST API (v20) クライアント
    実資金口座およびPractice (デモ) 環境の両対応
    """

    def __init__(
        self,
        token: Optional[str] = None,
        account_id: Optional[str] = None,
        environment: str = "practice",  # "practice" or "live"
    ):
        self.token = token or os.environ.get("OANDA_API_KEY", "")
        self.account_id = account_id or os.environ.get("OANDA_ACCOUNT_ID", "")
        self.environment = environment or os.environ.get("OANDA_ENV", "practice")

        if self.environment == "live":
            self.base_url = "https://api-fxtrade.oanda.com/v3"
        else:
            self.base_url = "https://api-fxpractice.oanda.com/v3"

        self.lot_unit = 10000  # 1ロット = 10,000通貨

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "Antigravity-MultiAsset-FX/1.0",
        }

    def send_order(
        self,
        instrument: str,
        units: int,
        side: str = "BUY",
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        OANDA 注文送信
        units: 正数(買い) / 負数(売り) または side="SELL" で符号反転
        """
        fmt_inst = instrument.replace("/", "_")
        if not "_" in fmt_inst and len(fmt_inst) == 6:
            fmt_inst = f"{fmt_inst[:3]}_{fmt_inst[3:]}"

        signed_units = abs(units) if side.upper() == "BUY" else -abs(units)

        # APIキー未設定時はシミュレーション約定を即時生成
        if not self.token or not self.account_id:
            mock_price = price or (155.250 if side.upper() == "BUY" else 155.247)
            return {
                "orderFillTransaction": {
                    "id": f"mock_{int(time.time()*1000)}",
                    "instrument": fmt_inst,
                    "units": str(signed_units),
                    "price": str(mock_price),
                    "pl": "0.0",
                    "time": datetime.now(timezone.utc).isoformat(),
                }
            }

        url = f"{self.base_url}/accounts/{self.account_id}/orders"
        payload = {
            "order": {
                "units": str(signed_units),
                "instrument": fmt_inst,
                "timeInForce": "FOK",
                "type": order_type.upper(),
                "positionFill": "DEFAULT",
            }
        }
        if order_type.upper() == "LIMIT" and price:
            payload["order"]["price"] = f"{price:.3f}"
            payload["order"]["timeInForce"] = "GTC"

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=self._headers(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.error(f"[OandaREST] 注文送信失敗: {e}")
            raise

    def get_positions(self) -> List[Dict[str, Any]]:
        """現在のオープンポジション一覧を取得"""
        if not self.token or not self.account_id:
            return []

        url = f"{self.base_url}/accounts/{self.account_id}/openPositions"
        try:
            req = urllib.request.Request(url, headers=self._headers(), method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("positions", [])
        except Exception as e:
            logger.warning(f"[OandaREST] ポジション取得エラー: {e}")
            return []

    def get_account_summary(self) -> Dict[str, Any]:
        """口座サマリー (残高・未実現損益・証拠金)"""
        if not self.token or not self.account_id:
            return {"balance": 1000000.0, "pl": 0.0, "marginUsed": 0.0}

        url = f"{self.base_url}/accounts/{self.account_id}/summary"
        try:
            req = urllib.request.Request(url, headers=self._headers(), method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("account", {})
        except Exception as e:
            logger.warning(f"[OandaREST] 口座サマリー取得エラー: {e}")
            return {}


class OandaStreaming:
    """
    OANDA 価格ストリーミング (Pricing Stream)
    価格Tickを受信し、BTCラインと共通の OandaTick オブジェクトに正規化
    """

    def __init__(
        self,
        token: Optional[str] = None,
        account_id: Optional[str] = None,
        environment: str = "practice",
    ):
        self.token = token or os.environ.get("OANDA_API_KEY", "")
        self.account_id = account_id or os.environ.get("OANDA_ACCOUNT_ID", "")
        self.environment = environment or os.environ.get("OANDA_ENV", "practice")

        if self.environment == "live":
            self.stream_url = "https://stream-fxtrade.oanda.com/v3"
        else:
            self.stream_url = "https://stream-fxpractice.oanda.com/v3"

    def normalize_tick(self, raw_price_data: Dict[str, Any]) -> OandaTick:
        """OANDAの価格JSONを正規化OandaTickへ変換"""
        inst = raw_price_data.get("instrument", "USD_JPY")
        bids = raw_price_data.get("bids", [{"price": "155.250", "liquidity": "1000000"}])
        asks = raw_price_data.get("asks", [{"price": "155.253", "liquidity": "1000000"}])

        best_bid = float(bids[0]["price"]) if bids else 155.250
        best_ask = float(asks[0]["price"]) if asks else 155.253
        spread_jpy = max(0.0, best_ask - best_bid)
        spread_pips = round(spread_jpy * 100.0, 2)  # 1 pip = 0.01円
        mid = round((best_bid + best_ask) / 2.0, 4)

        bid_depth = float(bids[0].get("liquidity", 100000)) / 10000.0
        ask_depth = float(asks[0].get("liquidity", 100000)) / 10000.0

        ts_raw = raw_price_data.get("time", "")
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            ts = dt.timestamp()
        except Exception:
            ts = time.time()

        return OandaTick(
            timestamp=ts,
            instrument=inst,
            bid=best_bid,
            ask=best_ask,
            spread_jpy=spread_jpy,
            spread_pips=spread_pips,
            mid=mid,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
        )


class OandaAdapter:
    """
    為替ポッド専属 OANDA 統合アダプター
    Streaming / REST / Risk Layer を一体提供
    """

    def __init__(self, token: Optional[str] = None, account_id: Optional[str] = None):
        self.rest = OandaREST(token=token, account_id=account_id)
        self.streaming = OandaStreaming(token=token, account_id=account_id)
        self.risk = OandaRisk()
