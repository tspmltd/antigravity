"""
TF2BP (CURSOR trend_follow_2bp pins, paper MARKET)
==================================================
入口と出口は CURSOR の trend_follow_2bp に合わせる。
  - 判断は 10 秒足の確定時だけ
  - スコアは 10s/60s/180s のテイカーと、最良気配サイズの壁（予測率）
  - 入口: max(ask,bid) >= 0.55、ask != bid、既定は買いのみ、P(next2) >= 0.50
  - P2 は較正点が 40 未満のあいだ素のスコア。較正後は isotonic
  - 約定は成行。紙上の価格は中値
  - 出口: TN6_BE5（MFE が 5bp 以上なら、含みが 0 以下で建値撤退）
  - θ と時間帯セルは入口では使わない（観測タグ）
  - 品質 REJECT は入口を止めない
Adverse の ON は別装置。ON のあいだ新規は出さず、建玉は閉じる。
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional

BAR_SEC = 10.0
BAR_MS = 10_000
LOOK = 18
FWD = 6
THR = 0.55
P2_MIN = 0.50
COOLDOWN_BARS = 6
COST_BP = 0.25
WALL_FLOOR = 0.05
BE_ARM_BP = 5.0
TN6_LB = 6
HOLD_ALIVE = 0.05
HOLD_DEAD = 0.05
HOLD_PRE = 8
HOLD_POST = 2
FADE_TP_BP = 5.0
RIDE_BP = 12.0
TRAIL_GB = 4.0
MAX_HOLD_BARS = 180
SL5_BP = 2.0
EPS = 1e-9
W60, W180, W10 = 0.50, 0.25, 0.25
W60_BURST, W180_BURST = 0.65, 0.35
BURST_RATIO = 3.0
K_PRE = 0.15


def _clip01(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    return 0.0 if x < 0 else 1.0 if x > 1 else float(x)


def _sat01(x: float) -> float:
    if not math.isfinite(x) or x <= 0:
        return 0.0
    return _clip01(1.0 - math.exp(-float(x)))


def _sum_win(xs: List[float], i: int, n: int) -> float:
    return float(sum(xs[max(0, i - n + 1) : i + 1]))


def _pre_boost(side_ask: bool, pre_adv_bp: float) -> float:
    if not math.isfinite(pre_adv_bp) or abs(pre_adv_bp) < 0.25:
        return 1.0
    aligned = pre_adv_bp if side_ask else -pre_adv_bp
    if aligned <= 0:
        return 1.0
    return 1.0 + K_PRE * _clip01(aligned / 2.0)


def _trend(side_ask: bool, wall: float, buy10, sell10, buy60, sell60, buy180, sell180, pre_adv_bp: float) -> float:
    flow10 = max(0.0, buy10 if side_ask else sell10)
    flow60 = max(0.0, buy60 if side_ask else sell60)
    flow180 = max(0.0, buy180 if side_ask else sell180)
    rate10 = flow10 / 10.0
    rate180 = flow180 / 180.0
    if rate180 <= EPS:
        burst = rate10 > EPS
    else:
        burst = rate10 > BURST_RATIO * rate180
    wall_v = max(float(wall), EPS)
    pre_b = _pre_boost(side_ask, pre_adv_bp)
    rates = {
        10: _clip01(_sat01(flow10 / wall_v) * pre_b),
        60: _clip01(_sat01(flow60 / wall_v) * pre_b),
        180: _clip01(_sat01(flow180 / wall_v) * pre_b),
    }
    diff60 = buy60 - sell60
    aligned = diff60 > 0.0 if side_ask else diff60 < 0.0
    if not aligned:
        return 0.0
    if burst:
        rates[10] = 0.0
        trend = W60_BURST * rates[60] + W180_BURST * rates[180]
    else:
        trend = W60 * rates[60] + W180 * rates[180] + W10 * rates[10]
    return _clip01(trend)


def isotonic_fit(xs: List[float], ys: List[float]):
    if not xs:
        return []
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    x_s = [xs[i] for i in order]
    y_s = [float(ys[i]) for i in order]
    stack = []
    for y in y_s:
        stack.append([1.0, y])
        while len(stack) >= 2:
            c0, s0 = stack[-2]
            c1, s1 = stack[-1]
            if (s0 / c0) <= (s1 / c1) + 1e-15:
                break
            stack[-2] = [c0 + c1, s0 + s1]
            stack.pop()
    y_hat = []
    for c, s in stack:
        y_hat.extend([s / c] * int(c))
    return list(zip(x_s, y_hat))


def isotonic_predict(steps, x: float) -> float:
    if not steps:
        return _clip01(float(x))
    y = steps[0][1]
    for xb, yb in steps:
        if x + 1e-15 >= xb:
            y = yb
        else:
            break
    return _clip01(float(y))


class TF2BPStrategy:
    def __init__(self, parameters: Optional[Dict[str, Any]] = None):
        self.strategy_name = "TF2BP_v1"
        self.frozen_mode = True
        self.last_user_directive = "CURSOR TF2BP: 10s bar, thr 0.55, p2 0.50, buy_only, tn6_be5, paper MARKET at mid"
        size = 0.001
        theta_mae = 3.82
        theta_md = 0.12
        if parameters:
            if parameters.get("order_size_btc"):
                size = float(parameters["order_size_btc"])
            if parameters.get("theta_mae") is not None:
                theta_mae = float(parameters["theta_mae"])
            if parameters.get("theta_md") is not None:
                theta_md = float(parameters["theta_md"])
        self.params = {
            "order_size_btc": size,
            "bar_sec": BAR_SEC,
            "thr": THR,
            "p2_min": P2_MIN,
            "side_mode": "buy_only",
            "exit": "tn6_be5",
            "be_arm_bp": BE_ARM_BP,
            "order_type": "MARKET",
            "fill": "mid",
            "cost_bp_accounting": COST_BP,
            "theta_mae_observe": theta_mae,
            "theta_md_observe": theta_md,
        }
        self.bars: List[Dict[str, float]] = []
        self.cur: Optional[Dict[str, float]] = None
        self.iso_p2 = None
        self.cooldown_until_ms = 0
        self.position_side: Optional[str] = None
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.entry_bar_i = -1
        self.entry_bar_ms = 0
        self.mfe_bp = 0.0
        self.peak_bp = 0.0
        self.mom_dead = 0
        self.armed_be = False
        self.score = 0.0
        self.p2 = 0.0
        self.total_trades = 0
        self.win_trades = 0
        self.total_pnl = 0.0
        self.total_pnl_bp = 0.0
        self.trades_history: List[Dict[str, Any]] = []

    def set_user_override(self, new_params: Dict[str, Any], reason: str = "ユーザー指示による調整"):
        if "order_size_btc" in new_params:
            self.params["order_size_btc"] = float(new_params["order_size_btc"])
        self.last_user_directive = f"{reason} ({time.strftime('%Y-%m-%d %H:%M:%S')})"

    def _scores_at(self, i: int):
        row = self.bars[i]
        mid = row["close"]
        if mid <= 0:
            return None
        buys = [b["buy"] for b in self.bars]
        sells = [b["sell"] for b in self.bars]
        buy10, sell10 = _sum_win(buys, i, 1), _sum_win(sells, i, 1)
        buy60, sell60 = _sum_win(buys, i, 6), _sum_win(sells, i, 6)
        buy180, sell180 = _sum_win(buys, i, LOOK), _sum_win(sells, i, LOOK)
        pre = 0.0
        if i >= 2 and self.bars[i - 2]["close"] > 0:
            pre = (mid / self.bars[i - 2]["close"] - 1.0) * 1e4
        ask_w = row["ask_sz"] if row["ask_sz"] > 0 else WALL_FLOOR
        bid_w = row["bid_sz"] if row["bid_sz"] > 0 else WALL_FLOOR
        ask_w = max(WALL_FLOOR, ask_w)
        bid_w = max(WALL_FLOOR, bid_w)
        return {
            "mid": mid,
            "ask": _trend(True, ask_w, buy10, sell10, buy60, sell60, buy180, sell180, pre),
            "bid": _trend(False, bid_w, buy10, sell10, buy60, sell60, buy180, sell180, pre),
            "ms": int(row["start_ms"]),
        }

    def _p2(self, same: float) -> float:
        if self.iso_p2:
            return isotonic_predict(self.iso_p2, same)
        return same

    def _tn6(self, i: int, side_ask: bool) -> float:
        j0 = max(0, i - TN6_LB + 1)
        s = 0.0
        for j in range(j0, i + 1):
            s += self.bars[j]["buy"] - self.bars[j]["sell"]
        return s if side_ask else -s

    def _refit_p2(self) -> None:
        n = len(self.bars)
        if n < LOOK + FWD + 40:
            self.iso_p2 = None
            return
        scores = [self._scores_at(i) if i >= LOOK else None for i in range(n)]
        xs, ys = [], []
        i = LOOK
        while i < n - FWD:
            sc = scores[i]
            if sc is None or i % 2 != 0:
                i += 1
                continue
            ask, bid = sc["ask"], sc["bid"]
            if max(ask, bid) < THR or ask == bid:
                i += 1
                continue
            side_ask = ask > bid
            same = ask if side_ask else bid
            ys.append(self._next2(scores, i, side_ask, sc["mid"]))
            xs.append(same)
            i += 1
        self.iso_p2 = isotonic_fit(xs, ys) if len(xs) >= 40 else None

    def _next2(self, scores, t: int, side_ask: bool, entry_mid: float) -> int:
        end = min(len(self.bars) - 1, t + 24)
        for j in range(t + 1, end + 1):
            sj = scores[j]
            if sj is None:
                continue
            g = self._gross(side_ask, entry_mid, sj["mid"])
            hi, lo = self.bars[j]["high"], self.bars[j]["low"]
            if side_ask:
                up = (hi / entry_mid - 1.0) * 1e4
                dn = (lo / entry_mid - 1.0) * 1e4
            else:
                up = (1.0 - lo / entry_mid) * 1e4
                dn = (1.0 - hi / entry_mid) * 1e4
            if dn <= -SL5_BP:
                return 0
            if up >= 2.0 or g >= 2.0:
                return 1
        sj = scores[end]
        g = self._gross(side_ask, entry_mid, sj["mid"]) if sj else 0.0
        return 1 if g >= 2.0 else 0

    @staticmethod
    def _gross(side_ask: bool, entry: float, mid: float) -> float:
        if entry <= 0 or mid <= 0:
            return 0.0
        ret = (mid / entry - 1.0) * 1e4
        return ret if side_ask else -ret

    def _yen(self, gross_bp: float) -> float:
        return gross_bp * 1e-4 * self.entry_price * float(self.params["order_size_btc"])

    def _roll(self, now: float, mid: float, bid_sz: float, ask_sz: float, sell_v: float, buy_v: float):
        start = int(now // BAR_SEC) * int(BAR_SEC)
        start_ms = start * 1000
        if self.cur is None or int(self.cur["start"]) != start:
            closed = self.cur
            self.cur = {
                "start": float(start),
                "start_ms": float(start_ms),
                "high": mid,
                "low": mid,
                "close": mid,
                "buy": 0.0,
                "sell": 0.0,
                "bid_sz": bid_sz,
                "ask_sz": ask_sz,
            }
            if closed is not None:
                self.bars.append(closed)
                if len(self.bars) > 2500:
                    drop = len(self.bars) - 2500
                    self.bars = self.bars[-2500:]
                    if self.entry_bar_i >= 0:
                        self.entry_bar_i -= drop
            else:
                closed = None
        else:
            closed = None
        b = self.cur
        b["high"] = max(b["high"], mid)
        b["low"] = min(b["low"], mid)
        b["close"] = mid
        b["buy"] += max(0.0, buy_v)
        b["sell"] += max(0.0, sell_v)
        b["bid_sz"] = bid_sz
        b["ask_sz"] = ask_sz
        return closed

    def on_tick(
        self,
        mid_price: float,
        best_bid: float,
        best_ask: float,
        taker_vol_bid: float = 0.0,
        taker_vol_ask: float = 0.0,
        adverse_score: float = 0.0,
        cancel_recommendation: bool = False,
        avoidance_on: bool = False,
        bid_depth_1: float = 0.0,
        ask_depth_1: float = 0.0,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        ts = time.time() if now is None else float(now)
        mid = float(mid_price)
        closed = self._roll(ts, mid, float(bid_depth_1), float(ask_depth_1), float(taker_vol_bid), float(taker_vol_ask))
        # Adverse は研究フラグのみ。建玉・入口は戦略ルールで決める。
        if closed is None:
            return {"action": "hold", "reason": "BAR_OPEN"}
        n = len(self.bars)
        i = n - 1
        if n % 12 == 0 or self.iso_p2 is None:
            self._refit_p2()
        if self.position_side:
            return self._exit_on_bar(i, mid)
        if n < LOOK + 10:
            return {"action": "hold", "reason": "WARMUP", "n_bars": n}
        if self.cooldown_until_ms > 0 and int(self.bars[i]["start_ms"]) <= self.cooldown_until_ms:
            return {"action": "hold", "reason": "COOLDOWN"}
        sc = self._scores_at(i)
        if sc is None:
            return {"action": "hold", "reason": "NO_SCORE"}
        ask, bid = sc["ask"], sc["bid"]
        if max(ask, bid) < THR or ask == bid:
            return {"action": "hold", "reason": "THR", "ask": ask, "bid": bid}
        if ask <= bid:
            return {"action": "hold", "reason": "BUY_ONLY", "ask": ask, "bid": bid}
        p2 = self._p2(ask)
        if p2 < P2_MIN:
            return {"action": "hold", "reason": "P2", "p2": p2, "ask": ask}
        self.score = ask
        self.p2 = p2
        return {
            "action": "buy",
            "reason": f"TF2BP_P2 ask={ask:.3f} p2={p2:.3f}",
            "entry_price": mid,
            "order_type": "MARKET",
            "score": ask,
            "p2": p2,
        }

    def _exit_on_bar(self, i: int, mid: float) -> Dict[str, Any]:
        side_ask = self.position_side == "buy"
        entry = self.entry_price
        bar = self.bars[i]
        hi, lo = bar["high"], bar["low"]
        g = self._gross(side_ask, entry, bar["close"])
        if side_ask:
            fav = (hi - entry) / entry * 1e4 if entry > 0 else 0.0
        else:
            fav = (entry - lo) / entry * 1e4 if entry > 0 else 0.0
        if math.isfinite(fav):
            self.mfe_bp = max(self.mfe_bp, fav)
            self.peak_bp = max(self.peak_bp, fav)
        if self.entry_bar_ms > 0 and int(bar["start_ms"]) >= self.entry_bar_ms:
            age = max(0, int((int(bar["start_ms"]) - self.entry_bar_ms) / BAR_MS))
        else:
            age = max(0, i - self.entry_bar_i)
        mom = self._tn6(i, side_ask)
        if mom >= HOLD_ALIVE:
            self.mom_dead = 0
        elif mom <= -HOLD_DEAD:
            self.mom_dead += 1
        cl = g
        if self.mfe_bp >= BE_ARM_BP:
            self.armed_be = True
        reason = ""
        close_bp = cl
        if self.armed_be and cl <= 0:
            reason, close_bp = "be_stop", max(0.0, cl)
        else:
            post = self.mfe_bp >= RIDE_BP
            need = HOLD_POST if post else HOLD_PRE
            if self.mom_dead >= need:
                reason, close_bp = "mom_dead", cl
            elif cl >= FADE_TP_BP and mom < HOLD_ALIVE and self.mom_dead >= 1:
                reason, close_bp = "fade_tp", cl
            elif post and self.peak_bp - cl >= TRAIL_GB and cl < self.peak_bp:
                reason, close_bp = "trail", max(cl, self.peak_bp - TRAIL_GB)
            elif age >= MAX_HOLD_BARS:
                reason, close_bp = "timeout", cl
        if not reason:
            return {"action": "hold", "reason": "POSITION_RUNNING", "current_pnl": self._yen(cl), "mfe_bp": self.mfe_bp}
        self.cooldown_until_ms = int(bar["start_ms"]) + COOLDOWN_BARS * BAR_MS
        return {
            "action": "exit",
            "reason": reason,
            "pnl": self._yen(close_bp),
            "gross_bp": close_bp,
            "net_bp": close_bp - COST_BP,
            "entry_price": bar["close"],
            "order_type": "MARKET",
        }

    def record_trade(self, side: str, fill_price: float, pnl: float, mid_price: float = 0.0):
        now = time.time()
        eval_price = mid_price if mid_price > 0 else fill_price
        order_val_jpy = self.params["order_size_btc"] * eval_price if eval_price > 0 else 12500.0
        pnl_bp = (pnl / order_val_jpy) * 10000.0 if order_val_jpy > 0 else 0.0
        if side in ("close_buy", "close_sell", "exit", "cancel"):
            self.total_trades += 1
            if pnl > 0:
                self.win_trades += 1
            self.total_pnl += pnl
            self.total_pnl_bp += pnl_bp
            self.trades_history.append({
                "ts": now,
                "side": self.position_side,
                "exit_side": side,
                "fill_price": fill_price,
                "pnl_jpy": round(pnl, 1),
                "pnl_bp": round(pnl_bp, 2),
                "is_win": (pnl > 0),
            })
            self.position_side = None
            self.entry_price = 0.0
            self.mfe_bp = 0.0
            self.peak_bp = 0.0
            self.mom_dead = 0
            self.armed_be = False
            self.entry_bar_i = -1
            self.entry_bar_ms = 0
        elif side in ("buy", "sell"):
            self.position_side = side
            self.entry_price = fill_price
            self.entry_time = now
            self.entry_bar_i = len(self.bars) - 1
            self.entry_bar_ms = int(self.bars[-1]["start_ms"]) if self.bars else 0
            self.mfe_bp = 0.0
            self.peak_bp = 0.0
            self.mom_dead = 0
            self.armed_be = False

    def get_window_stats(self, hours: float = 1.0) -> Dict[str, Any]:
        now = time.time()
        cutoff = now - (hours * 3600.0)
        recent = [t for t in self.trades_history if t["ts"] >= cutoff]
        total_t = len(recent)
        win_t = sum(1 for t in recent if t["is_win"])
        pnl_jpy = sum(t["pnl_jpy"] for t in recent)
        pnl_bp = sum(t["pnl_bp"] for t in recent)
        wr = (win_t / total_t * 100.0) if total_t > 0 else 0.0
        return {
            "window_hours": hours,
            "total_trades": total_t,
            "win_trades": win_t,
            "loss_trades": total_t - win_t,
            "win_rate_pct": round(wr, 1),
            "pnl_jpy": round(pnl_jpy, 1),
            "pnl_bp": round(pnl_bp, 2),
        }
