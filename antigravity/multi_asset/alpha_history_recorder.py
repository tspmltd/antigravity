"""
antigravity/multi_asset/alpha_history_recorder.py: 約定実績データレイク自動記録エンジン
========================================================================================
役割: 日本株ペーパートレード、暗号資産DRYRUN、開示主導ポジションのイグジット（手仕舞い）時に、
約定結果（損益bp、拘束日数、勝敗、事前EVS）を Parquet ファイルへ自動追記する。
これにより、DuckDB を介して日々の実測値で事前確率 (Prior) を永続的に閉ループ自己更新する。
"""

import os
import sys
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

logger = logging.getLogger("antigravity.multi_asset.history_recorder")
JST = timezone(timedelta(hours=9))

HISTORY_DIR = os.path.join(BASE_DIR, "data", "alpha_history")
DAILY_LOG_PARQUET = os.path.join(HISTORY_DIR, "live_paper_trades.parquet")


class AlphaTradeHistoryRecorder:
    """開示・アルファ取引の実績レコーダー"""

    @classmethod
    def record_completed_trade(
        cls,
        symbol: str,
        event_type: str,
        tier: str,
        entry_price: float,
        exit_price: float,
        holding_days: float,
        pnl_bp: float,
        evs_at_entry: float,
        source: str = "paper_trade",
        trade_id: Optional[str] = None,
    ) -> bool:
        """
        手仕舞い完了した取引レコードを Parquet へ追記保存
        """
        try:
            import pandas as pd
            os.makedirs(HISTORY_DIR, exist_ok=True)

            tid = trade_id or f"trade_{uuid.uuid4().hex[:8]}"
            now_iso = datetime.now(JST).isoformat()
            win = bool(pnl_bp > 0)

            new_record = {
                "trade_id": tid,
                "timestamp": now_iso,
                "symbol": str(symbol),
                "event_type": str(event_type),
                "tier": str(tier),
                "entry_price": float(entry_price),
                "exit_price": float(exit_price),
                "holding_days": round(float(holding_days), 2),
                "pnl_bp": round(float(pnl_bp), 2),
                "win": win,
                "evs_at_entry": round(float(evs_at_entry), 1),
                "source": str(source),
            }

            df_new = pd.DataFrame([new_record])

            if os.path.exists(DAILY_LOG_PARQUET):
                df_existing = pd.read_parquet(DAILY_LOG_PARQUET)
                df_combined = pd.concat([df_existing, df_new], ignore_index=True)
            else:
                df_combined = df_new

            df_combined.to_parquet(DAILY_LOG_PARQUET, index=False)
            logger.info(f"[HistoryRecorder] 💾 取引実績保存成功: {symbol} ({tier}) PnL: {pnl_bp:+.1f}bp, 拘束: {holding_days:.1f}日")
            return True
        except Exception as e:
            logger.error(f"[HistoryRecorder] 取引実績保存エラー: {e}")
            return False
