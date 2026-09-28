"""CSR-536 P0-C — Conditional Unwind classifier **DESIGN ONLY** (OBSERVE · ENFORCE=0).

目的: 「30秒で全部切れ」ではなく、悪化在庫だけを切る判定器の設計固定。
  Healthy inventory → HOLD
  Deteriorating inventory → Conditional Unwind (@~30s情報帯)

禁止:
  - 30s hard-code 本番化
  - Economic PASS / ENFORCE / WIRE / LIVE
  - 新Alpha・weights変更

ラベル（反事実・同一 trade set）:
  y_unwind_better = 1 if (virtual_pnl@H − actual_pnl) > 0 for hold>H
  特に H=30 を第一候補。10時の反例（Δ=-12）は「切らないクラス」教師。

特徴（既存 OBSERVE 列のみ・新規フィード禁止）:
  hold_sec, ladder(L0–L4), ips, mae_bp, entry_spread_bp, side,
  csnt / fake_bo flags (secondary), adverse_score (SHADOW)

このモジュールはスキーマとヘルパのみ。学習・執行はしない。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

CSR = "CSR-536"
MODE = "DESIGN_ONLY"
WIRE = "NO"
ENFORCE = 0
HARDCODE_30S = False
HORIZON_PRIMARY_SEC = 30.0
HORIZONS_RESEARCH = (10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0)

FEATURE_COLUMNS: List[str] = [
    "hold_sec",
    "ladder_level",  # L0..L4
    "ips",
    "mae_bp",
    "entry_spread_bp",
    "side",  # long/short
    "csnt_any",
    "fake_bo_any",
    "adverse_score_shadow",  # optional · never BUY/SELL
]

LABEL_COLUMNS: List[str] = [
    "y_unwind_better_30",  # 1 if Virtual@30 > Actual on eligible
    "delta_bp_30",
    "actual_pnl_bp",
    "virtual_pnl_bp_30",
    "is_hard_stop",
]


@dataclass
class ConditionalUnwindSpec:
    """Frozen design contract — not an executable strategy."""

    csr: str = CSR
    mode: str = MODE
    wire: str = WIRE
    enforce: int = ENFORCE
    hardcode_30s_exit: bool = HARDCODE_30S
    primary_horizon_sec: float = HORIZON_PRIMARY_SEC
    research_horizons_sec: tuple = HORIZONS_RESEARCH
    policy: str = "healthy→HOLD · deteriorating→Conditional Unwind"
    next_question: str = "WHEN TO UNWIND?"
    features: List[str] = field(default_factory=lambda: list(FEATURE_COLUMNS))
    labels: List[str] = field(default_factory=lambda: list(LABEL_COLUMNS))
    gates: Dict[str, Any] = field(
        default_factory=lambda: {
            "economic_pass": False,
            "csr521_pass": False,
            "min_independent_episodes": 20,
            "require_counterexample_class": True,  # must retain 10:00-class losers
            "spread_x_inventory_proxy": "RESEARCH_AFTER_P0",  # CSR-535 P3 inventory interaction
        }
    )
    forbidden: List[str] = field(
        default_factory=lambda: [
            "blanket_exit_at_30s",
            "ENFORCE_before_PASS",
            "LIVE_ON",
            "WIRE_YES",
            "new_alpha_weights",
            "peg_v4",
        ]
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def label_unwind_better(
    virtual_pnl_bp: Optional[float], actual_pnl_bp: Optional[float]
) -> Optional[int]:
    """Binary teacher from SAME-SET CF. None if either side missing."""
    if virtual_pnl_bp is None or actual_pnl_bp is None:
        return None
    return 1 if float(virtual_pnl_bp) > float(actual_pnl_bp) else 0


def policy_hint_from_delta(delta_bp: Optional[float], actual_full_bp: Optional[float]) -> str:
    """Human-readable OBSERVE hint — never an execution order."""
    if delta_bp is None:
        return "INSUFFICIENT"
    # Counterexample class first (e.g. 10:00 Δ=-12): cutting would hurt
    if delta_bp < 0:
        return "CUT_WOULD_HURT"
    # Good inventory windows: small positive Δ while actual already green → hold
    if actual_full_bp is not None and actual_full_bp > 5.0 and delta_bp < 2.0:
        return "LIKELY_HOLD_HEALTHY"
    if delta_bp >= 10.0:
        return "LIKELY_UNWIND_CANDIDATE"
    return "MARGINAL_OBSERVE"


SPEC = ConditionalUnwindSpec()
