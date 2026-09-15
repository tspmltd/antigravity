import time
from typing import Optional, Callable, Dict, Any
from datetime import datetime


class PeakDrawdownCircuitBreaker:
    """
    累積損益の最高値（ピーク・ウォーターマーク）からの下落幅を監視し、
    許容最大ドローダウンを超過した場合に緊急停止を発動、
    一定の冷却期間経過後に安全に自動復帰するサーキットブレーカー。
    """

    def __init__(
        self,
        max_drawdown_limit_jpy: float = 3000.0,
        warning_ratio: float = 0.70,
        cooldown_seconds: float = 300.0,
        on_trip_callback: Optional[Callable[[str, float, float], None]] = None,
        on_resume_callback: Optional[Callable[[str], None]] = None,
        on_warning_callback: Optional[Callable[[str, float, float], None]] = None,
    ):
        self.max_drawdown_limit_jpy = max_drawdown_limit_jpy
        self.warning_ratio = warning_ratio
        self.warning_drawdown_limit_jpy = max_drawdown_limit_jpy * warning_ratio
        self.cooldown_seconds = cooldown_seconds
        self.on_trip_callback = on_trip_callback
        self.on_resume_callback = on_resume_callback
        self.on_warning_callback = on_warning_callback

        self.peak_pnl: float = 0.0
        self.is_halted: bool = False
        self.is_warning_active: bool = False
        self.halt_start_time: Optional[float] = None
        self.trip_count: int = 0
        self.last_trip_reason: str = ""

    def update(self, current_total_pnl: float) -> Dict[str, Any]:
        """
        現在の総損益（確定損益 + 評価損益）を元にピーク更新およびドローダウン判定を実行。
        """
        now = time.time()

        # ピーク損益の更新
        if current_total_pnl > self.peak_pnl:
            self.peak_pnl = current_total_pnl

        current_dd = self.peak_pnl - current_total_pnl

        # 冷却待機中の場合: クールダウン経過チェック
        if self.is_halted:
            if self.halt_start_time and (now - self.halt_start_time >= self.cooldown_seconds):
                self.resume(reason=f"冷却待機期間 ({int(self.cooldown_seconds / 60)}分間) 完了による自動復帰")

        # 正常稼働中の場合: ドローダウン超過チェック
        elif current_dd >= self.max_drawdown_limit_jpy:
            reason = (
                f"直近ピークからの最大許容ドローダウン超過 "
                f"(ピーク損益: {self.peak_pnl:+,.1f} 円 からの下落: {current_dd:,.1f} 円 >= 許容限度: {self.max_drawdown_limit_jpy:,.0f} 円)"
            )
            self.trip(reason=reason, current_dd=current_dd, current_total_pnl=current_total_pnl)

        # 早期警戒チェック（許容上限の70%超過）
        elif current_dd >= self.warning_drawdown_limit_jpy:
            if not self.is_warning_active:
                self.is_warning_active = True
                pct = (current_dd / self.max_drawdown_limit_jpy) * 100.0 if self.max_drawdown_limit_jpy > 0 else 0.0
                reason = (
                    f"許容最大ドローダウンの警戒水準到達 "
                    f"(下落額: {current_dd:,.1f} 円 / 許容限度: {self.max_drawdown_limit_jpy:,.0f} 円 [{pct:.1f}%])"
                )
                print(f"[CircuitBreaker] ⚠️ DRAWDOWN WARNING: {reason}", flush=True)
                if self.on_warning_callback:
                    try:
                        self.on_warning_callback(reason, current_dd, current_total_pnl)
                    except Exception as ex:
                        print(f"[CircuitBreaker] Error in on_warning_callback: {ex}", flush=True)

        # ドローダウンが50%以下まで回復した場合、警戒フラグをリセット
        elif self.is_warning_active and current_dd < (self.warning_drawdown_limit_jpy * 0.7):
            self.is_warning_active = False

        return {
            "is_halted": self.is_halted,
            "peak_pnl": self.peak_pnl,
            "current_dd": current_dd,
            "is_warning_active": self.is_warning_active,
            "trip_count": self.trip_count,
            "halt_start_time": self.halt_start_time,
        }

    def trip(self, reason: str, current_dd: float = 0.0, current_total_pnl: float = 0.0):
        """サーキットブレーカーを手動または自動でトリップ"""
        if self.is_halted:
            return
        self.is_halted = True
        self.halt_start_time = time.time()
        self.trip_count += 1
        self.last_trip_reason = reason

        print(f"[CircuitBreaker] 🚨 TRIP TRIGGERED: {reason}", flush=True)

        if self.on_trip_callback:
            try:
                self.on_trip_callback(reason, current_dd, current_total_pnl)
            except Exception as ex:
                print(f"[CircuitBreaker] Error in on_trip_callback: {ex}", flush=True)

    def resume(self, reason: str = "手動復帰または冷却期間経過"):
        """取引を安全に再開"""
        if not self.is_halted:
            return
        self.is_halted = False
        self.halt_start_time = None

        print(f"[CircuitBreaker] 🟢 TRADING RESUMED: {reason}", flush=True)

        if self.on_resume_callback:
            try:
                self.on_resume_callback(reason)
            except Exception as ex:
                print(f"[CircuitBreaker] Error in on_resume_callback: {ex}", flush=True)
