#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TF2B_WOBI3 LIVE Execution Engine Entrypoint (CSR-644).

Canonical implementation is located at:
  antigravity.quant_pipeline.run_tf2b_wobi3_live
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

AG = Path(__file__).resolve().parent
if str(AG) not in sys.path:
    sys.path.insert(0, str(AG))

from antigravity.quant_pipeline.run_tf2b_wobi3_live import (
    TF2BWobi3Live,
    TF2BWobi3LiveExecutor,
    main,
)

__all__ = ["TF2BWobi3Live", "TF2BWobi3LiveExecutor", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
