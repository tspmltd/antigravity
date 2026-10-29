"""
data/ml_spine/distilled_axe.py

Shadow scorer. Returns numbers in bp, never verbs.
The on-disk LUT is only a coarse mean-drift lookup (yen). This module
converts that one teacher number to bp. p_fill and m_adv are unlearned.
Do not regenerate this file from build_distilled_axe_lut.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


def yen_to_bp(delta_yen: float, mid: float) -> Optional[float]:
    if mid is None or mid <= 0:
        return None
    return (float(delta_yen) / float(mid)) * 10000.0


class DistilledAxe:
    def __init__(self, lut_path: str = None):
        if lut_path is None:
            lut_path = str(Path(__file__).parent / "distilled_axe_lut.json")
        with open(lut_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.matrix = data.get("matrix", {})

    def _lut_key(self, wobi: float, spread_bp: float, cancel_rate: float, micro_dev: float) -> str:
        if wobi >= 0.30:
            w = "STRONG_BID"
        elif wobi >= 0.05:
            w = "WEAK_BID"
        elif wobi >= -0.15:
            w = "NEUTRAL"
        elif wobi >= -0.35:
            w = "WEAK_ASK"
        else:
            w = "STRONG_ASK"
        tension = spread_bp * abs(wobi)
        if tension < 0.30:
            t = "TENSION_LOW"
        elif tension < 0.80:
            t = "TENSION_MID"
        else:
            t = "TENSION_HIGH"
        c = "SPOOF_RISK_HIGH" if cancel_rate >= 0.35 else "NORMAL"
        m = "MICRO_FAVORABLE" if micro_dev >= 0 else "MICRO_ADVERSE"
        return f"{w}_{t}_{c}_{m}"

    def evaluate(
        self,
        wobi: float,
        spread_bp: float,
        cancel_rate: float,
        micro_dev: float,
        mid: float = 0.0,
        capture_bp: Optional[float] = None,
        fee_bp: float = 0.0,
        venue_net: float = 0.0,
    ) -> Dict[str, Any]:
        """Return quote numbers. Missing teachers stay null. No action/grade."""
        key = self._lut_key(wobi, spread_bp, cancel_rate, micro_dev)
        cell = self.matrix.get(key) or {}
        drift_yen = cell.get("expected_drift_1s")
        try:
            drift_yen_f = float(drift_yen) if drift_yen is not None else None
        except (TypeError, ValueError):
            drift_yen_f = None
        mu_1s_bp = yen_to_bp(drift_yen_f, mid) if drift_yen_f is not None else None
        if capture_bp is None:
            capture_bp = float(spread_bp) / 2.0 if spread_bp is not None else None
        return {
            "mu_1s_bp": None if mu_1s_bp is None else round(mu_1s_bp, 4),
            "p_fill": None,
            "m_adv_1s_bp": None,
            "inv_penalty_bp": 0.0,
            "capture_bp": None if capture_bp is None else round(float(capture_bp), 4),
            "fee_bp": round(float(fee_bp), 4),
            "ev_quote_bp": None,
            "lut_key": key,
            "lut_drift_yen": drift_yen_f,
            "venue_net": venue_net,
        }
