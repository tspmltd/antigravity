"""
Liquidity Vacuum Agent (流動性蒸発検知エージェント)
=================================================
Head of Agent Research Initiative - Priority 3 (HIGH)

【背景と課題】
Microstructure 定期監査において、毎時 36%〜40% の高頻度で
「cancel_spike_no_taker (約定を伴わないキャンセル急増)」
が発生していることが判明。

従来のブレイクアウト判定ではこれを騙し上げ/騙し下げと誤認していたが、
本質は「流動性の蒸発 (Liquidity Vacuum)」。
買い手/売り手が存在しない真空地帯へ価格が滑落または急騰する現象であり、
エントリの成否を分ける最重要ミクロ構造要因である。

【コア判定条件】
1. cancel_spike       : cancel_rate >= 0.50 (キャンセル急増)
2. taker == 0         : taker_vol <= 0.001 または taker_aggressiveness <= 0.005 (テイカー約定ゼロ)
3. refill弱い          : refill_rate < 0.15 (板の補充が途絶)
➔ 3条件同時成立で「流動性蒸発 (Liquidity Evaporation)」確定 (is_vacuum = True)

【新特徴量】
d(micro_dev)/dt : MicroDev Shift 時間変化率 (微視的歪み速度・加速度)
  - 単位: 円/s および bp/s
  - 平滑化 EMA (tau=3.0s) および 瞬時微分の両方を算出
  - 符号判定:
    - d(micro_dev)/dt < 0 : 買い板蒸発 (Bid Vacuum / 下落真空トラップ)
    - d(micro_dev)/dt > 0 : 売り板蒸発 (Ask Vacuum / 踏み上げ真空トラップ)
  - 二次微分 d^2(micro_dev)/dt^2 : 歪み加速度 (MicroDev Acceleration)

【役割】
- ブレイク判定ではなく「板が消滅したことの検知 (Evaporation Detector)」
- FBV (Fake Breakout Veto) の代替・強化コンポーネント
- 真空地帯へのジャンピング・キャッチ（不利約定）の完全遮断
- 保有玉の不利早期精算 (Early Adverse Cut) シグナル提供
"""

import time
from typing import Dict, Any, Optional
from collections import deque


class LiquidityVacuumAgent:
    def __init__(
        self,
        history_len: int = 120,
        cancel_threshold: float = 0.50,
        taker_threshold: float = 0.001,
        refill_threshold: float = 0.15,
        ema_alpha: float = 0.35,
        bus: Optional[Any] = None,
    ):
        self.history_len = history_len
        self.cancel_threshold = cancel_threshold
        self.taker_threshold = taker_threshold
        self.refill_threshold = refill_threshold
        self.ema_alpha = ema_alpha
        self.bus = bus

        self._history: deque = deque(maxlen=history_len)
        self._ema_d_micro_dev: Optional[float] = None
        self.total_vacuum_events = 0
        self.total_ticks = 0
        self.latest_result: Dict[str, Any] = {}

        if self.bus:
            try:
                self.bus.subscribe("orderbook_micro", self._on_bus_snap)
            except Exception:
                pass

    def _on_bus_snap(self, snap: Any) -> None:
        self.on_tick(snap)

    def on_tick(self, snap: Any, now: Optional[float] = None) -> Dict[str, Any]:
        t = now or time.time()
        self.total_ticks += 1

        # 各種スナップショット指標の抽出
        cancel = float(getattr(snap, "cancel_rate", 0.0) or 0.0)
        refill = float(getattr(snap, "refill_rate", 0.0) or 0.0)
        taker_agg = float(getattr(snap, "taker_aggressiveness", 0.0) or 0.0)
        taker_buy = float(getattr(snap, "taker_volume_ask", 0.0) or 0.0)
        taker_sell = float(getattr(snap, "taker_volume_bid", 0.0) or 0.0)
        taker_vol = taker_buy + taker_sell

        micro_dev = float(getattr(snap, "micro_dev", 0.0) or 0.0)
        mid_price = float(getattr(snap, "mid_price", 0.0) or 0.0)

        bid_d = float(getattr(snap, "total_bid_depth", 0.0) or getattr(snap, "bid_depth_1", 0.0) or 0.0)
        ask_d = float(getattr(snap, "total_ask_depth", 0.0) or getattr(snap, "ask_depth_1", 0.0) or 0.0)
        cushion = bid_d + ask_d

        # 新特徴量: d(micro_dev)/dt (MicroDev Shift 速度・加速度)
        d_micro_dev_dt_raw = 0.0
        micro_dev_accel = 0.0
        cushion_velocity = 0.0

        if self._history:
            prev = self._history[-1]
            dt = max(0.01, t - prev["t"])
            d_micro_dev_dt_raw = (micro_dev - prev["micro_dev"]) / dt
            cushion_velocity = (cushion - prev["cushion"]) / dt

            # EMA 平滑化 (高周波ジッター・マイクロサージ抑制)
            if self._ema_d_micro_dev is None:
                self._ema_d_micro_dev = d_micro_dev_dt_raw
            else:
                self._ema_d_micro_dev = (
                    self.ema_alpha * d_micro_dev_dt_raw + (1.0 - self.ema_alpha) * self._ema_d_micro_dev
                )

            # 2次微分 (加速度 d^2(micro_dev)/dt^2)
            prev_d = prev.get("d_micro_dev_dt", 0.0)
            micro_dev_accel = (d_micro_dev_dt_raw - prev_d) / dt
        else:
            self._ema_d_micro_dev = 0.0

        d_micro_dev_dt = float(self._ema_d_micro_dev or 0.0)
        d_micro_dev_dt_bp = (d_micro_dev_dt / mid_price * 10000.0) if mid_price > 0 else 0.0

        # 履歴バッファへ格納
        self._history.append({
            "t": t,
            "micro_dev": micro_dev,
            "d_micro_dev_dt": d_micro_dev_dt,
            "cushion": cushion,
            "cancel": cancel,
            "refill": refill,
            "taker_vol": taker_vol,
        })

        # =========================================================================
        # 1. コア判定条件 (Liquidity Vacuum)
        # cancel_spike AND taker == 0 AND refill弱い ➔ 流動性蒸発検知
        # =========================================================================
        cancel_spike = (cancel >= self.cancel_threshold)
        taker_zero = (taker_vol <= self.taker_threshold) or (taker_agg <= 0.005)
        refill_weak = (refill < self.refill_threshold)
        cushion_thin = (cushion < 15.0)

        # 真空確定フラグ (Evaporation)
        is_vacuum = bool(cancel_spike and taker_zero and refill_weak)
        if is_vacuum:
            self.total_vacuum_events += 1

        # 蒸発の方向性 (Vacuum Side) の特定
        if is_vacuum:
            if d_micro_dev_dt < -30.0 or micro_dev < -50.0:
                vacuum_side = "bid"  # 買い板が蒸発 (Bid Vacuum: 下落崖っぷち)
            elif d_micro_dev_dt > 30.0 or micro_dev > 50.0:
                vacuum_side = "ask"  # 売り板が蒸発 (Ask Vacuum: 踏み上げ空間)
            else:
                vacuum_side = "bilateral"  # 両側蒸発 (スプレッド急拡)
        else:
            vacuum_side = "none"

        # =========================================================================
        # 2. 連続的真空度スコア (vacuum_score: 0.0 〜 1.0)
        # =========================================================================
        s_cancel = min(1.0, cancel / self.cancel_threshold)
        s_taker = max(0.0, 1.0 - taker_vol / 0.02)
        s_refill = max(0.0, 1.0 - refill / (self.refill_threshold * 1.5))
        s_cushion = max(0.0, 1.0 - cushion / 30.0)
        s_accel = min(1.0, abs(d_micro_dev_dt) / 150.0)

        base_score = 0.35 * s_cancel + 0.25 * s_taker + 0.25 * s_refill + 0.15 * s_cushion
        if is_vacuum:
            # 確定時は最低 0.60 を保証し、加速度と板薄度で 1.0 までスケール
            vacuum_score = round(min(1.0, 0.60 + 0.25 * s_accel + 0.15 * s_cushion), 3)
        else:
            # 通常時は最大 0.55 以下に抑制
            vacuum_score = round(min(0.55, base_score * 0.85), 3)

        # =========================================================================
        # 3. レベル判定 (Risk Level & Action)
        # =========================================================================
        accel_severe = (abs(d_micro_dev_dt) >= 150.0 or abs(d_micro_dev_dt_bp) >= 0.15)

        if is_vacuum and (cushion_thin or accel_severe):
            risk_level = "CRITICAL_FLASH_EVAPORATION"
            action = "VETO_ENTRY_AND_STANDBY"
        elif is_vacuum:
            risk_level = "VACUUM_WARNING"
            action = "VETO_BREAKOUT_BYPASS"
        elif cancel_spike and not refill_weak:
            risk_level = "REFILL_BUFFERED"
            action = "ALLOW_NORMAL_EXECUTION"
        else:
            risk_level = "NORMAL_DEPTH"
            action = "NORMAL"

        result = {
            "timestamp": t,
            "is_vacuum": is_vacuum,
            "vacuum_score": vacuum_score,
            "risk_level": risk_level,
            "action": action,
            "vacuum_side": vacuum_side,
            "metrics": {
                "cancel_rate": round(cancel, 3),
                "refill_rate": round(refill, 3),
                "taker_volume": round(taker_vol, 4),
                "taker_aggressiveness": round(taker_agg, 3),
                "taker_is_zero": taker_zero,
                "micro_dev": round(micro_dev, 1),
                "d_micro_dev_dt": round(d_micro_dev_dt, 2),
                "d_micro_dev_dt_bp": round(d_micro_dev_dt_bp, 4),
                "micro_dev_acceleration": round(d_micro_dev_dt, 2),  # 後方互換キー
                "micro_dev_jerk": round(micro_dev_accel, 2),
                "cushion_btc": round(cushion, 2),
                "cushion_velocity_btc_s": round(cushion_velocity, 2),
            },
            "events_detected": self.total_vacuum_events,
        }

        self.latest_result = result
        if self.bus:
            try:
                self.bus.publish("vacuum_state", result)
            except Exception:
                pass

        return result

    def get_latest_state(self) -> Dict[str, Any]:
        return self.latest_result
