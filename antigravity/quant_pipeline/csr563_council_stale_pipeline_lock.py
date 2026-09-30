#!/usr/bin/env python3
"""CSR-563 — Council STALE freeze (03–07h) root cause + observe pipeline restore.

NOT a test fixed-feed. agents_council_state.json froze at 2026-10-01 02:01 JST
because quant_pipeline.run_pipeline died after MicrostructureAgent was overwritten
by MicrostructureResearchAgent (batch-only). Watchdog crash-looped ImportError
(~1151×). Hourly Discord reporters kept republishing the stale JSON.

Restore: live MicrostructureAgent from git HEAD; batch agent moved to
microstructure_research_agent.py. WIRE=NO · ENFORCE=0 · LIVE=OFF unchanged.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "mm_research" / "daily"
JST = timezone(timedelta(hours=9))


def build() -> dict:
    return {
        "csr": 563,
        "ts_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": "Council STALE freeze · run_pipeline ImportError · observe restore",
        "diagnosis": {
            "not": "test fixed feed",
            "is": "STALE agents_council_state.json after run_pipeline crash",
            "frozen_payload": {
                "price_delta": -2872.0,
                "micro_dev": -532.0,
                "imbalance": -0.005,
                "trend_strength": 1.0,
                "state_last_write_jst": "2026-10-01T02:01:04+09:00",
            },
            "root_cause": (
                "microstructure_agent.py replaced with batch MicrostructureResearchAgent; "
                "run_pipeline still imports MicrostructureAgent → ImportError; "
                "watchdog auto-restart loop; reporters read stale state"
            ),
            "evidence": [
                "quant_pipeline.log last live ticks ~01:59 then ImportError storm",
                "ImportError count ~1151",
                "state mtime stuck 02:01 while micro hourly / dryrun logs kept moving",
                "run_pipeline NOT in process list until restore",
            ],
        },
        "governance_status": {
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
            "auto_apply": "OFF",
            "SpreadCaptureMM": "FROZEN_CONTROL (Arena lead)",
            "UMM": "INVESTIGATE/QUARANTINE (CSR-521 inventory)",
            "TF2BP_PEG_v3": "INVESTIGATE/QUARANTINE (AgingGuard friction · Entry-only CF)",
            "batch_research_agents": "OBSERVE_ACCUMULATE_24_48h",
        },
        "peg_night": {
            "aging_chop_death": "3s AgingGuard taker cut on wide spread = friction death",
            "0400_0600_relative_plus": "Baseline DD not PEG alpha — reject as PEG improve",
            "verdict": "PEG_v3 remains UNQUALIFIED · INVESTIGATE Entry-only + Aging CF",
        },
        "restore": {
            "action": "restore MicrostructureAgent from git HEAD; move ResearchAgent to microstructure_research_agent.py",
            "run_pipeline": "watchdog restarted OK after import fix",
            "param_change": False,
            "LIVE_change": False,
        },
        "forbid_teach_on": [
            "Council rows 03:00–07:00 JST 2026-10-01 (STALE_SCHEMA / stale state republish)",
            "final_action alone",
        ],
        "governance": {
            "WIRE": "NO",
            "ENFORCE": 0,
            "LIVE": "OFF",
            "AUTO_APPLY": "OFF",
            "Economic_PASS": "NO",
        },
        "core": (
            "03–07h Council identical values = stale state after run_pipeline ImportError, "
            "not fixed test feed; observe pipeline restored; LIVE/ENFORCE untouched."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = build()
    path = OUT / "2026-10-01_csr563_council_stale_pipeline_import_error_lock.json"
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path), "core": lock["core"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
