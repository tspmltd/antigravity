# TF2B_10.15 Shadow Validation Directive — State A Hypothesis

**STATUS:** LANE OPEN · SHADOW ONLY · PROMOTE≠LIVE  
**GOV:** WIRE=NO · ENFORCE=0 · AUTO_APPLY=OFF · LIVE/PAPER/PEG2/TF2BP/UMM=UNTOUCHED  
**Role:** State A 仮説の再現性検証（次世代シグナル探索枠）。PnL最大化ではない。

---

## 0. Thesis (lock)

Confirmed elsewhere:

```
State B → Imbalance が本体 · BidDepth は冗長
```

State A remains under-sampled:

```
State A = High Imbalance + Low Ask Depth + High Bid Depth
Discovery N≈17 · Validation N≈2 · Ret120 attractive but fragile
```

Therefore:

```
TF2B_10.15 = Shadow限定
≠ 有望戦略としての即時採用
```

---

## 1. Absolute constraints

| Forbidden | Rule |
|-----------|------|
| AUTO_APPLY | NO |
| LIVE change | NO |
| PAPER change | NO |
| PEG2 change | NO |
| TF2BP change | NO |
| UMM-P2 change | NO |
| Real orders | NO |
| WIRE | NO |
| ENFORCE | 0 |
| Signal / threshold / state-def retune mid-run | NO |
| Execution / Exit change | NO |
| Mid-run optimization | NO |

Observation only.

---

## 2. Strategy ID

`TF2B_10.15`

### Signal (frozen definition)

**State A** =

- High Imbalance **AND**
- Low Ask Depth **AND**
- High Bid Depth

Cuts = rolling terciles on calibration window (high = top third, low = bottom third).  
**Do not retune thresholds** after lane start except explicit user unlock.

### Execution

Virtual only. No orders.

### Exit (virtual PnL path)

TF2BP Baseline Exit only: Trail + TP15bp (same constants as Baseline).  
Used for `virtual_pnl` attribution — not for promotion of a live package.

---

## 3. Primary goal

Reproduce **State A** in live market data.

Not maximize Shadow PnL.

---

## 4. Coverage arms (always compare)

| Arm | Definition |
|-----|------------|
| **A** | High Imb + Low Ask + High Bid |
| **B** | High Imb only |
| **Baseline** | All / periodic probes |

---

## 5. Stored fields (per signal / labeled row)

`timestamp · symbol · state_id · imbalance · bid_depth · ask_depth · micro_dev · spread · taker_buy · taker_sell · entry_price · future_30s · future_60s · future_120s · future_240s · MFE · MAE · virtual_pnl · regime`

---

## 6. Validation priority metrics

Hit Rate · Ret120 · Ret240 · Median · MFE · MAE  
(+ Ret30/60 for diagnostics)

Also: regime split · spread buckets · State A failure common features.

Compare vs `TF2B_10.10` / `TF2B_11.3` / `TF2B_10.15` on Ret120/Ret240/Hit when reference stats available.

---

## 7. Sample guard

| Validation N (State A labeled) | Action |
|--------------------------------|--------|
| < 20 | 結論禁止 |
| < 50 | PENDING / MORE_DATA |
| ≥ 100 | 初回判定可 |
| ≥ 200 | 昇格審査可 |

Discovery vs Validation = chronological split of completed State A labels (first half / second half) once N allows.

---

## 8. Promotion rules

Labels: `REJECT` · `MORE_DATA` · `PROMOTE_TO_PAPER`

**PROMOTE_TO_PAPER** only if all:

- Validation N ≥ 100  
- Discovery & Validation same sign on Ret120  
- Ret120 > 0  
- Ret240 > 0  
- MFE advantage vs Baseline  
- Reproducibility ok  

`PROMOTE_TO_PAPER` ≠ LIVE. Separate explicit user order required for any further step.

---

## 9. Outputs

```
data/mm_research/tf2b_10_15_shadow/
  LANE_LOCK.json
  tf2b_10_15_shadow_report.md
  tf2b_10_15_daily_summary.json
  tf2b_10_15_signals.parquet
  tf2b_10_15_validation.parquet
  signals.jsonl
  state.json
  health.json
```

Discord: research notify (dryrun multicast / analysis) — health + verdict. Not LIVE alerts.

---

## 10. Portfolio context

```
LIVE        = TF2BP Baseline          維持
PAPER       = PEG2-B                  最優先監視
RESEARCH-1  = UMM+Maker Shadow        最優先研究
RESEARCH-2  = TF2B_10.15 Shadow       並行探索
REJECT      = PEG_v3 / AgingGuard
```

---

## 11. One-line agent contract

> Shadow-validate State A reproducibility. No orders. No retune. No touch LIVE/PAPER/PEG2/TF2BP/UMM. Judge REJECT / MORE_DATA / PROMOTE_TO_PAPER under sample guards.
