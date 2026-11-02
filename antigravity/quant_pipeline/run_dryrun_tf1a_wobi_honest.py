#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TF1A_WOBI_HONEST — 本来のトレンドフォロー戦略 (WIRE=NO · HONEST約定).

設計思想:
- 期待値のあるトレンドを捉えて収益を最大化する。
- 保有時間は比較的長め（ノイズを許容しトレンドをホールド）。
- 1回の収益目標は +5.0 bp 以上（損小利大設計）。
- トレイリングストップ:
    * 含み益 < +5.0 bp: ハードストップ -4.0 bp または上位トレンド崩壊までホールド（微小ノイズでの途中撤退を排除）
    * 含み益 >= +5.0 bp: ストップを建値 (+0.5 bp) へ引き上げ（利益確保）
    * 含み益 >= +8.0 bp: ピークから -2.5 bp の動的トレイリングで利益最大化
- 約定: BOARD_IMITATION (public_maker_fill)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

AG = Path(os.environ.get("ANTIGRAVITY_ROOT", "/home/azureuser/antigravity"))
sys.path.insert(0, str(AG))

from antigravity.quant_pipeline.run_dryrun_tf2b_wobi3 import (  # noqa: E402
    calculate_optimal_order_price,
    _iso,
    _log,
)
from antigravity.quant_pipeline.run_dryrun_tf2b_wobi5 import TF2BWobi5DryRun  # noqa: E402
from antigravity.quant_pipeline.run_wobimm_live import public_maker_fill  # noqa: E402
from antigravity.quant_pipeline.wobimm3_policy import (  # noqa: E402
    Wobi3Market,
    entry_gate,
    SPREAD_MAX_BP,
)

OUT_DIR = AG / "data" / "mm_research" / "tf1a_wobi_honest_dryrun"
OUT_DIR.mkdir(parents=True, exist_ok=True)
FEEDER_EXECS = AG / "data" / "market_feeder" / "executions.jsonl"


class TF1AWobiHonestDryRun(TF2BWobi5DryRun):
    ENTRY_POLICY = "tf1a_wobi"
    LANE_NAME = "TF1A_WOBI_HONEST"
    STATE_FILE = AG / "data" / "dryrun_tf1a_wobi_honest_state.json"
    PID_FILE = AG / "data" / "dryrun_tf1a_wobi_honest.pid"
    LEDGER_FILE = OUT_DIR / "paired_ledger.jsonl"
    AUDIT_FILE = OUT_DIR / "audit_events.jsonl"
    REPORT_MINUTE = 12

    # TF パラメータ
    TARGET_MIN_PROFIT_BP = 5.0     # 最低目標利益: +5bp
    TRAILING_ACTIVATION_BP = 8.0   # トレイリング開始利益: +8bp
    TRAILING_STEP_BP = 2.5         # トレイリング許容後退幅: 2.5bp
    HARD_STOP_BP = -4.0            # ノイズを耐えるハードストップ: -4.0bp
    BREAKEVEN_LOCK_BP = 0.5        # +5bp到達時に建値ロックする水準

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.quote_side: Optional[str] = None
        self.quote_px = 0.0
        self.working_id = ""
        self.pending_exit_tag = ""
        self.pending_exit_reason = ""
        self.exec_fp: Optional[Any] = None

        # トレンドフォロー管理ステート
        self.peak_pnl_bp = 0.0
        self.entry_ts = 0.0
        self.breakeven_locked = False

        # カウンター
        self.honest_posts = 0
        self.honest_cancels = 0
        self.honest_fills = 0
        self.gate_pulls = 0
        self.board_fills = 0
        self._last_hb = 0.0
        self._price_history: List[tuple[float, float]] = []  # (ts, mid)
        self._init_exec_stream()

    def _init_exec_stream(self) -> None:
        if FEEDER_EXECS.exists():
            try:
                self.exec_fp = open(FEEDER_EXECS, "r", encoding="utf-8")
                self.exec_fp.seek(0, 2)
            except Exception as exc:
                _log(f"exec stream: {exc}")

    def _poll_execs(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.exec_fp:
            self._init_exec_stream()
        if not self.exec_fp:
            return out
        try:
            while True:
                line = self.exec_fp.readline()
                if not line:
                    break
                row = json.loads(line)
                out.extend(row.get("rows") or [])
        except Exception:
            pass
        return out

    def _update_trend_context(self, mid: float, now: float) -> tuple[float, float]:
        """直近の価格推移から短期(15s)と中期(60s)のモメンタムを算出"""
        self._price_history.append((now, mid))
        cutoff = now - 180.0
        self._price_history = [(t, p) for t, p in self._price_history if t >= cutoff]
        
        p15 = [p for t, p in self._price_history if t >= now - 15.0]
        p60 = [p for t, p in self._price_history if t >= now - 60.0]
        
        ret15_bp = ((mid / p15[0] - 1.0) * 1e4) if p15 else 0.0
        ret60_bp = ((mid / p60[0] - 1.0) * 1e4) if p60 else 0.0
        return ret15_bp, ret60_bp

    def _post_quote(self, side: str, px: float, why: str) -> None:
        side = side.lower()
        if self.quote_side == side and abs(self.quote_px - px) <= 1e-9:
            return
        if side == "buy" and self.quote_side == "buy":
            return
        if self.quote_side and self.quote_side != side:
            self.honest_cancels += 1
        self.quote_side = side
        self.quote_px = float(px)
        self.working_id = f"SIM-{side.upper()}-{int(time.time() * 1000)}"
        self.honest_posts += 1
        _log(f"[TF1A_WOBI_HONEST] POST {side.upper()} @ {px:,.0f} ({why})")

    def _cancel_quote(self, why: str) -> None:
        if not self.quote_side:
            return
        _log(f"[TF1A_WOBI_HONEST] CANCEL {self.quote_side.upper()} ({why})")
        self.honest_cancels += 1
        self.quote_side = None
        self.quote_px = 0.0
        self.working_id = ""
        self.pending_exit_tag = ""
        self.pending_exit_reason = ""

    def _on_honest_fill(
        self,
        side: str,
        px: float,
        src: str,
        snap: Any,
        now: float,
        m: Dict[str, Any],
        depth_bids: List[Dict[str, float]],
        depth_asks: List[Dict[str, float]],
    ) -> None:
        self.honest_fills += 1
        self.board_fills += 1
        self.quote_side = None
        self.quote_px = 0.0
        self.working_id = ""
        _log(f"[TF1A_WOBI_HONEST] FILL {side.upper()} @ {px:,.0f} src={src}")
        
        if side == "buy" and not self.wobi3.position_side:
            self.entry_ts = now
            self.peak_pnl_bp = 0.0
            self.breakeven_locked = False
            self.apply_wobi3_entry_state("buy", px, now, depth_asks, mid_price=float(getattr(snap, "mid_price", px) or px))
            return
            
        if side == "sell" and self.wobi3.position_side:
            entry = float(self.wobi3.entry_price or px)
            pnl_bp = (px / entry - 1.0) * 1e4 if entry > 0 else 0.0
            pnl_j = self.wobi3._yen(pnl_bp)
            hold_sec = now - self.entry_ts if self.entry_ts > 0 else 0.0
            tag = self.pending_exit_tag or "TF_EXIT"
            reason = self.pending_exit_reason or src
            self.pending_exit_tag = ""
            self.pending_exit_reason = ""
            _log(f"[TF1A_WOBI_HONEST] TRADE CLOSED: PnL={pnl_bp:+.2f}bp ({pnl_j:+.1f}円) Hold={hold_sec:.1f}s Reason={reason}")
            self._execute_exit_wobi3(px, pnl_bp, pnl_j, reason, tag, now)

    def _eval_tf_exit(self, current_mid: float, m: Dict[str, Any], now: float) -> tuple[bool, str, float, str]:
        """トレンドフォロー向けダイナミック・エグジット
        
        マイクロノイズ(WOBI/OFI一時ブレ)では切らず、目標達成・トレイリング・構造崩壊でエグジットする。
        """
        entry = float(self.wobi3.entry_price or 0.0)
        if entry <= 0 or current_mid <= 0:
            return False, "HOLD", 0.0, ""

        unrealized_bp = (current_mid / entry - 1.0) * 1e4
        hold_time = now - self.entry_ts if self.entry_ts > 0 else 0.0
        if unrealized_bp > self.peak_pnl_bp:
            self.peak_pnl_bp = unrealized_bp

        # 1. ハードストップロス (-4.0bp)
        if unrealized_bp <= self.HARD_STOP_BP:
            return True, "HARD_STOP", current_mid, f"stop_loss ({unrealized_bp:+.2f}bp <= {self.HARD_STOP_BP}bp)"

        # 2. +5bp 突破後の建値ロック (Breakeven Protection)
        if self.peak_pnl_bp >= self.TARGET_MIN_PROFIT_BP:
            self.breakeven_locked = True
            # 建値+0.5bpを割り込んだら利確保護
            if unrealized_bp <= self.BREAKEVEN_LOCK_BP:
                return True, "BREAKEVEN_LOCK", current_mid, f"lock_breakeven (+5bp hit, locked at {unrealized_bp:+.2f}bp)"

        # 3. +8bp 突破後の動的トレイリングストップ (Trailing Stop)
        if self.peak_pnl_bp >= self.TRAILING_ACTIVATION_BP:
            trailing_floor = self.peak_pnl_bp - self.TRAILING_STEP_BP
            if unrealized_bp <= trailing_floor:
                return True, "TRAILING_STOP", current_mid, f"trailing_exit (peak={self.peak_pnl_bp:+.2f}bp cur={unrealized_bp:+.2f}bp <= {trailing_floor:+.2f}bp)"

        # 4. 上位トレンド構造崩壊判定 (長期反転)
        ret15_bp, ret60_bp = self._update_trend_context(current_mid, now)
        wobi = float(m.get("wobi") or 0.0)
        ofi = float(m.get("recent_ofi") or 0.0)
        
        # 60秒モメンタムが大幅逆行かつ板も強く売り転換した場合のみトレンド崩壊とみなす
        if ret60_bp <= -3.0 and wobi <= -0.40 and ofi <= -0.15:
            return True, "TREND_COLLAPSE", current_mid, f"trend_collapsed (ret60={ret60_bp:+.1f}bp wobi={wobi:+.2f})"

        return False, "HOLD", 0.0, f"holding_trend (unrealized={unrealized_bp:+.2f}bp peak={self.peak_pnl_bp:+.2f}bp hold={hold_time:.0f}s)"

    def _maybe_fill_board(
        self,
        execs: List[Dict[str, Any]],
        bid: float,
        ask: float,
        snap: Any,
        now: float,
        m: Dict[str, Any],
        depth_bids: List,
        depth_asks: List,
    ) -> None:
        if not self.quote_side or self.quote_px <= 0:
            return
        hit = public_maker_fill(
            side=self.quote_side.upper(),
            price=self.quote_px,
            acceptance_id=self.working_id,
            public_execs=execs,
            best_bid=bid,
            best_ask=ask,
        )
        if not hit:
            return
        src, _sz = hit
        self._on_honest_fill(self.quote_side, self.quote_px, src, snap, now, m, depth_bids, depth_asks)

    def _post_bid(
        self,
        depth_bids: List,
        depth_asks: List,
        mid: float,
        why: str,
    ) -> None:
        pref, desc = calculate_optimal_order_price(
            depth_bids, depth_asks, "buy", 5, 0.05, 1.0, mid
        )
        if pref <= 0:
            return
        self._post_quote("buy", pref, f"{why} [{desc}]")

    def _tick_pair(self, snap: Any, now: float) -> None:
        try:
            self._tick_pair_inner(snap, now)
        except Exception as exc:
            _log(f"[TF1A_WOBI_HONEST] tick error: {exc}")
        if now - self._last_hb >= 60.0:
            self._last_hb = now
            pos = "LONG" if self.wobi3.position_side else "FLAT"
            q = self.quote_side or "FLAT"
            px = f"{self.quote_px:,.0f}" if self.quote_px else "-"
            _log(
                f"[TF1A_WOBI_HONEST] hb pos={pos} quote={q} @{px} "
                f"posts={self.honest_posts} fills={self.honest_fills}"
            )

    def _tick_pair_inner(self, snap: Any, now: float) -> None:
        m = self._extract_metrics(snap, now)
        mid = float(getattr(snap, "mid_price", 0.0) or m.get("mid_price") or 0.0)
        bid = float(getattr(snap, "best_bid", 0.0) or 0.0)
        ask = float(getattr(snap, "best_ask", 0.0) or 0.0)
        depth_bids, depth_asks = self._read_latest_depth()
        execs = self._poll_execs()
        
        # 1. 約定判定
        self._maybe_fill_board(execs, bid, ask, snap, now, m, depth_bids, depth_asks)

        # 2. 保有中ポジションの管理 (エグジット判定)
        if self.wobi3.position_side:
            should_exit, exit_tag, exit_px, exit_reason = self._eval_tf_exit(mid, m, now)
            if should_exit:
                px, desc = calculate_optimal_order_price(
                    depth_bids, depth_asks, "sell", 5, 0.05, 1.0, mid
                )
                self.pending_exit_tag = exit_tag
                self.pending_exit_reason = f"{exit_reason} [{desc}]"
                self._post_quote("sell", px, f"EXIT {self.pending_exit_tag} {self.pending_exit_reason}")
            elif self.quote_side == "sell":
                self._cancel_quote("cancel_exit_hold")
            elif self.quote_side == "buy":
                self._cancel_quote("cancel_buy_now_long")
            return

        if self.quote_side == "sell":
            self._cancel_quote("flat_drop_exit")

        # 3. 新規エントリー判定 (本物のトレンドを検知)
        spread_bp = float(m.get("spread_bp") or 0.0)
        if spread_bp > SPREAD_MAX_BP:
            if self.quote_side == "buy":
                self._cancel_quote(f"SPREAD_TOO_WIDE ({spread_bp:.1f}bp)")
            return

        # トレンド指標
        ret15_bp, ret60_bp = self._update_trend_context(mid, now)
        wobi = float(m.get("wobi") or 0.0)
        ofi = float(m.get("recent_ofi") or 0.0)
        depletion = float(m.get("ask_depletion_vel") or 0.0)

        # エントリーゲート:
        # (A) 短期・中期の価格モメンタムが上向き (ret15 > 0 かつ ret60 >= 0)
        # (B) 板が買い優勢 (wobi >= 0.15) かつ 約定フローも買い優勢 (ofi > 0)
        # (C) または 売板食い尽くしブレイクアウト (depletion >= 0.25)
        trend_up = (ret15_bp >= 0.5 and ret60_bp >= 0.0) or (ret60_bp >= 1.5)
        book_support = (wobi >= 0.15 and ofi > 0.0)
        breakout = (depletion >= 0.25 and ofi > 0.0)

        if trend_up and (book_support or breakout):
            why = f"TF_TREND_ENTRY (ret60={ret60_bp:+.1f}bp wobi={wobi:+.2f} ofi={ofi:+.2f})"
            self._post_bid(depth_bids, depth_asks, mid, why)
        else:
            if self.quote_side == "buy":
                # トレンドが消えた場合は指値を引き下げ
                if ret15_bp < -0.5 or wobi < -0.20:
                    self._cancel_quote("trend_vanished")

    def metrics(self) -> Dict[str, Any]:
        m = super().metrics()
        m["model"] = "TF1A_WOBI_HONEST"
        m["fill"] = "BOARD_IMITATION"
        tf_stats = m.get("TF1A_WOBI_HONEST") or m.get("TF2B_WOBI5") or {}
        m["TF1A_WOBI_HONEST"] = tf_stats
        m["honest_fill"] = {
            "posts": self.honest_posts,
            "cancels": self.honest_cancels,
            "fills": self.honest_fills,
            "board_fills": self.board_fills,
        }
        m["stack"] = (
            "TF1A_WOBI_HONEST Pure Trend Following + Target>=5bp + "
            "Breakeven Lock & Dynamic Trailing Stop + BOARD_IMITATION fill"
        )
        return m


def main() -> int:
    parser = argparse.ArgumentParser(description="TF1A_WOBI_HONEST DryRun (WIRE=NO · Pure Trend Following · Target >= 5bp)")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--source", type=str, default="shared_feeder")
    args = parser.parse_args()
    runner = TF1AWobiHonestDryRun(
        interval_sec=args.interval,
        report_interval_sec=args.report_interval,
        market_source=args.source,
    )

    def _sig(signum, _frame):
        _log(f"signal {signum}")
        runner.stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
