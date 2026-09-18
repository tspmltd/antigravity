"""
Safety Gate (同期の鍵 & 本番安全防護ゲート)
===========================================
Dry-runを「先に走る影武者」とし、同じ意思決定シグナルを用いながら
影武者が十分な成績を出し、本番口座のリスクが許容値内で、
かつ「外部マクロショック」が発生していない場合のみ
シグナルを LIVE 実発注へ通過させる。

【判定条件】
1. 本番口座防護: 当日累計損失 < 300円, 連敗数 < 4回, ポジション重複なし
2. マクロショック連携: 世界の株価・VIX急変時は即座に遮断またはロット半減
3. シグナル品質: final_confidence >= 0.60 (ショック時0.75), fake_breakout == False
4. Dry-run直近成績: Sharpe >= 0.5, 勝率 >= 40%, 平均PnL > 0, 影武者連敗なし
"""
from dataclasses import dataclass, field
from typing import Dict, Any, Tuple, Optional
import time

from .market_shock_sentinel import MarketShockSentinel, MarketShockState


@dataclass
class DryRunStats:
    """Dry-run (影武者) の直近パフォーマンス指標"""
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    consecutive_losses: int = 0
    recent_trend_positive: bool = True


@dataclass
class LiveExecutionState:
    """本番口座のリアルタイム状態"""
    daily_pnl: float = 0.0
    consecutive_losses: int = 0
    has_open_position: bool = False
    open_side: Optional[str] = None
    open_size: float = 0.0
    is_halted: bool = False
    last_trade_ts: float = 0.0


@dataclass
class SafetyGateConfig:
    """安全ゲートの閾値設定"""
    min_dryrun_trades: int = 5              # 影武者の最低サンプル数
    min_dryrun_sharpe: float = 0.50         # 影武者の最低Sharpe
    min_dryrun_win_rate: float = 0.40       # 影武者の最低勝率 (40%以上)
    max_daily_loss: float = 300.0           # 本番許容最大日次損失 (円)
    max_consecutive_losses: int = 4         # 本番最大連続損失回数 (4回で即停止)
    min_confidence: float = 0.60            # LIVE採用最低確信度
    warning_shock_min_confidence: float = 0.75  # マクロ警戒時の確信度引き上げ
    block_on_critical_shock: bool = True    # 致命的ショック時に完全発注遮断
    require_dryrun_avg_profit: bool = True  # 影武者の平均PnLが正であること
    cool_down_seconds: float = 10.0         # 発注インターバル (秒)
    max_spread_jpy: float = 2500.0          # 許容最大スプレッド (円, スプレッド負け防止ガード)


class SafetyGate:
    """Dry-runとLIVEをつなぐ安全防護ゲート (外部情報収集連携ハブ対応)"""

    def __init__(
        self,
        config: Optional[SafetyGateConfig] = None,
        shock_sentinel: Optional[MarketShockSentinel] = None,
    ):
        self.config = config or SafetyGateConfig()
        self.shock_sentinel = shock_sentinel or MarketShockSentinel()

    def allow(
        self,
        signal: Dict[str, Any],
        live_state: LiveExecutionState,
        stats: DryRunStats,
        spread_jpy: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        シグナルをLIVE実取引に渡して良いかを判定。
        戻り値: (allowed: bool, reason: str)
        """
        action = signal.get("action", "").lower()
        confidence = float(signal.get("final_confidence", 0.0))
        size_multiplier = float(signal.get("size_multiplier", 1.0))
        fake_bo = bool(signal.get("fake_breakout_flag", False))

        # 0. EXIT / CANCEL はポジションがある場合常に安全弁として許可
        if action in ("exit", "cancel"):
            if live_state.has_open_position:
                return True, f"ポジション解消のためのエグジット/キャンセル要求 ({action})"
            return False, "ポジションなしのためエグジット不要"


        # エントリー(buy/sell)の判定
        if action not in ("buy", "sell"):
            return False, f"未発注アクション: {action}"

        # 1. 本番ハードストップ／リスク防護チェック (最優先)
        if live_state.is_halted:
            return False, "LIVE停止中 (手動停止またはキルスイッチ作動)"

        if live_state.daily_pnl <= -self.config.max_daily_loss:
            return False, f"日次損失制限超過 (累計損益: ¥{live_state.daily_pnl:.1f} <= -¥{self.config.max_daily_loss})"

        if live_state.consecutive_losses >= self.config.max_consecutive_losses:
            return False, f"最大連敗数到達 ({live_state.consecutive_losses} >= {self.config.max_consecutive_losses}回)"

        if live_state.has_open_position:
            # 既存ポジションと同方向のナンピンは禁止
            return False, f"既存ポジション保有中 ({live_state.open_side} {live_state.open_size} BTC)"

        # 2. マクロ・市場急変ショックチェック (情報収集自動化連携)
        shock = self.shock_sentinel.get_current_shock()
        if shock.shock_active:
            if shock.shock_level == "critical" and self.config.block_on_critical_shock:
                return False, f"マクロ市場急変遮断 (Critical: {shock.event_name}) - LIVE発注を安全保護停止"
            elif shock.shock_level == "warning":
                if confidence < self.config.warning_shock_min_confidence:
                    return False, f"マクロ警戒発動中・確信度不足 ({confidence:.2f} < 警戒基準{self.config.warning_shock_min_confidence:.2f} [{shock.event_name}])"

        # 3. 発注クールダウンチェック
        now = time.time()
        if now - live_state.last_trade_ts < self.config.cool_down_seconds:
            return False, f"発注クールダウン中 (残り {self.config.cool_down_seconds - (now - live_state.last_trade_ts):.1f}秒)"

        # 4. シグナル品質チェック
        if fake_bo:
            return False, "板のフェイクブレイク検知 (Fake Breakout Guard)"

        # 4.1. Adverse Selection (逆選択・急激な逆行予兆) 先回り遮断
        adverse_flag = bool(signal.get("adverse_warning_flag", False))
        adverse_side = signal.get("adverse_risk_side", "none")
        adverse_score = float(signal.get("adverse_risk_score", 0.0))
        if action == adverse_side and (adverse_flag or adverse_score >= 0.60):
            return False, f"Adverse Selection 逆行先回り遮断 (スコア: {adverse_score:.2f}, Micro-Price/板厚崩落検知)"

        required_conf = self.config.warning_shock_min_confidence if shock.shock_active else self.config.min_confidence
        if confidence < required_conf:
            return False, f"確信度不足 ({confidence:.2f} < 閾値{required_conf:.2f})"

        if size_multiplier <= 0.0:
            return False, "サイズ乗数ゼロ (リスク回避)"

        # 4.5. スプレッド過大チェック (スプレッド負け防止ガード)
        chk_spread = spread_jpy if spread_jpy is not None else signal.get("spread_jpy")
        if chk_spread is not None and chk_spread > self.config.max_spread_jpy:
            return False, f"スプレッド過大遮断 (¥{chk_spread:,.0f} > 上限¥{self.config.max_spread_jpy:,.0f} スプレッド負け防止)"

        # 5. Dry-run (影武者) 成績連動チェック
        # サンプル数が十分にある場合、影武者が勝てている時だけ本番を動かす
        if stats.total_trades >= self.config.min_dryrun_trades:
            if stats.sharpe_ratio < self.config.min_dryrun_sharpe:
                return False, f"Dry-run Sharpe不足 ({stats.sharpe_ratio:.2f} < 基準{self.config.min_dryrun_sharpe:.2f})"

            if stats.win_rate < self.config.min_dryrun_win_rate:
                return False, f"Dry-run 勝率不足 ({stats.win_rate*100:.1f}% < 基準{self.config.min_dryrun_win_rate*100:.1f}%)"

            if self.config.require_dryrun_avg_profit and stats.avg_pnl <= 0:
                return False, f"Dry-run 平均PnLマイナス (¥{stats.avg_pnl:.1f} <= 0)"

            if stats.consecutive_losses >= 3:
                return False, "Dry-run 影武者が3連敗中 (市場環境不一致の疑い)"

        # 全ての安全ゲートをクリア
        shock_note = f" (マクロ警戒防護中: {shock.event_name})" if shock.shock_active else ""
        return True, f"全安全ゲート合格 (Dry-run成績・本番リスク・シグナル確信度 OK{shock_note})"
