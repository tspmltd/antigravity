import os
from typing import Dict, Any
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Antigravity HFT システム全体の設定定数"""

    PRODUCT_CODE: str = os.environ.get("ANTIGRAVITY_SYMBOL", "FX_BTC_JPY")
    ORDER_SIZE_BTC: float = float(os.environ.get("ANTIGRAVITY_ORDER_SIZE", "0.001"))
    MAKER_FEE_PCT: float = 0.0
    TAKER_FEE_PCT: float = 0.0

    # リスク管理設定
    MAX_DRAWDOWN_LIMIT_JPY: float = float(os.environ.get("ANTIGRAVITY_MAX_DD", "3000.0"))
    CIRCUIT_BREAKER_COOLDOWN_SEC: float = 300.0  # 5分間

    # 監視・レポート間隔
    POLL_INTERVAL_SEC: float = 1.0
    REPORT_INTERVAL_SEC: float = 3600.0  # 1時間

    # WebSocketエンドポイント
    WS_URL: str = "wss://ws.lightstream.bitflyer.com/json-rpc"

    # Discord 3系統 Webhook URL
    DISCORD_REPORT_URL: str = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    DISCORD_SYSTEM_URL: str = os.environ.get("DISCORD_SYSTEM_WEBHOOK_URL", "").strip() or DISCORD_REPORT_URL
    DISCORD_ALERT_URL: str = os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip() or DISCORD_REPORT_URL
