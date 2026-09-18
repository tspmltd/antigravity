"""
Market Shock Sentinel (市場急変・マクロショック連携ハブ)
=========================================================
世界の株価急変・VIX恐怖指数急騰・マクロショック等の外部情報を
リアルタイムに集約し、SignalFusionEngine および SafetyGate へ伝播。

【防護レベル】
- critical: 主要市場 ±2.0%超急変 / VIX急騰 ➔ 本番発注の即時完全遮断 (HALT)
- warning:  主要市場 ±1.0%突破 ➔ ロット50%半減 & 確信度閾値引き上げ (DEFENSE)
- none:     通常運用
"""
import os
import json
import time
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
DEFAULT_SHOCK_FILE = "/home/azureuser/antigravity/data/market_shock_state.json"


@dataclass
class MarketShockState:
    shock_active: bool = False
    shock_level: str = "none"  # "none", "warning", "critical"
    event_name: str = "市場正常"
    triggered_at: float = 0.0
    expires_at: float = 0.0
    recommended_action: str = "NORMAL"  # "NORMAL", "REDUCE_SIZE_50", "HALT_LIVE"
    updated_at_str: str = ""


class MarketShockSentinel:
    def __init__(self, state_file: str = DEFAULT_SHOCK_FILE):
        self.state_file = state_file

    def get_current_shock(self) -> MarketShockState:
        """現在有効な市場ショック状態を取得 (期限切れなら自動復帰)"""
        if not os.path.exists(self.state_file):
            return MarketShockState()

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            now = time.time()
            expires_at = float(data.get("expires_at", 0.0))

            if expires_at > now and data.get("shock_active", False):
                return MarketShockState(
                    shock_active=True,
                    shock_level=data.get("shock_level", "warning"),
                    event_name=data.get("event_name", "外部市場急変"),
                    triggered_at=data.get("triggered_at", now),
                    expires_at=expires_at,
                    recommended_action=data.get("recommended_action", "REDUCE_SIZE_50"),
                    updated_at_str=data.get("updated_at_str", ""),
                )
            else:
                # 期限切れ
                return MarketShockState()
        except Exception as e:
            print(f"[MarketShockSentinel] ⚠️ 状態ファイル読込エラー: {e}")
            return MarketShockState()

    def publish_shock(
        self,
        event_name: str,
        level: str = "warning",
        duration_sec: float = 1800.0,  # デフォルト30分間有効
    ) -> MarketShockState:
        """外部センチネルから市場ショックを発令"""
        now = time.time()
        level_clean = level.lower()
        if level_clean not in ("warning", "critical"):
            level_clean = "warning"

        action = "HALT_LIVE" if level_clean == "critical" else "REDUCE_SIZE_50"
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

        state = MarketShockState(
            shock_active=True,
            shock_level=level_clean,
            event_name=event_name,
            triggered_at=now,
            expires_at=now + duration_sec,
            recommended_action=action,
            updated_at_str=now_str,
        )

        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=2, ensure_ascii=False)

        print(f"[MarketShockSentinel] 🚨 【市場ショック発令】 レベル: {level_clean.upper()} | 事象: {event_name} | 防護策: {action} (有効期限: {duration_sec/60:.0f}分)")
        return state

    def clear_shock(self):
        """ショック状態を即時クリア (平常復帰)"""
        if os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except Exception:
                pass
        print("[MarketShockSentinel] 🟢 市場ショック解除: 平常運転に復帰しました。")
