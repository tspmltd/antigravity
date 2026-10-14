# UMM-P2 Shadow Directive — Maker Entry as Execution Alpha

**STATUS:** LANE RUNNING · WIRE=NO · PROMOTE=LOCKED  
**GOV:** WIRE=NO · ENFORCE=0 · AUTO_APPLY=OFF · LIVE=UNTOUCHED  
**Parents:** UMM Profit Decay Investigation · Hold-Time Decay Deep Dive (`CANCER_CONFIRMED`)  
**CSR context:** CSR-504 (UMM) · CSR-582 Maker Entry component · PEG2-B exec constants

---

## 0. Thesis (lock)

UMM is **not directionally broken** (WR 56–58%).  
EV is lost in **hold-time decay / entry AE / never-green 10s+ inventory**.

Counterfactual (buffer⋈AE) suggested Maker Entry Δ≈**+0.32 bp/trade**, ΔPnL≈**+640 bp**, PF 1.00→1.46, DD −102→−48.

UMM-P2 asks only:

> Does Maker Entry function as **Execution Alpha** on UMM in live Shadow,  
> or was the Counterfactual fantasy?

This is **not** UMM redesign. Signal / inventory / skew / sizing / exit / params stay frozen.

---

## 1. Absolute constraints

| Action | Rule |
|--------|------|
| LIVE | **FORBIDDEN** |
| AUTO_APPLY | **FORBIDDEN** |
| Parameter retune | **FORBIDDEN** |
| Signal change | **FORBIDDEN** |
| Inventory policy change | **FORBIDDEN** |
| Skew / gamma change | **FORBIDDEN** |
| Sizing change | **FORBIDDEN** |
| Exit change (TP / HardStop / Timeout) | **FORBIDDEN** |
| UMM Champion unlock (`UMM_CHAMPION_UNLOCK`) | **FORBIDDEN** |
| Shadow wire to venue | **NO** |
| AgingGuard | **REJECT / never reintroduce** |

**Allowed change:** Execution Layer only (how an eligible UMM quote becomes an entry fill).

---

## 2. Arms (paired comparison only)

| Arm | Signal / Quote / Skew / Inventory / Exit / Params | Entry Execution |
|-----|-----------------------------------------------------|-----------------|
| **CTRL** `UMM Baseline` | Identical to current dryrun UMM (CSR-504 frozen) | Current continuous-quote fill (`maker_fill` at quote) |
| **ARM** `UMM+Maker` | **Byte-identical** quote decision as CTRL on same tick | Maker Offset Entry + `CANCEL_AND_WAIT` |

No third arm. No PEG packaging. No exit hybrids.

### 2.1 Execution constants (FROZEN — copy PEG2-B, do not retune)

```
maker_offset_ratio     = 0.97
maker_timeout_sec      = 3.0
fallback_mode          = CANCEL_AND_WAIT
spread_guard_max_bp    = 4.5   # skip post if spread too wide; not a UMM param change
```

Limit price construction (same spirit as PEG2-B):

- Buy:  `limit = mid - (spread/2) * maker_offset_ratio`  toward bid (more passive than crossing)
- Sell: `limit = mid + (spread/2) * maker_offset_ratio`  toward ask

Fill rule (Shadow simulation on shared book):

- Buy fills when `best_bid >= limit` (or equivalent last-trade touch consistent with CTRL feed)
- Sell fills when `best_ask <= limit`
- If not filled by `maker_timeout_sec`: **cancel**, do **not** chase, do **not** enter  
  → counts as `NO_FILL` / opportunity path (must be attributed separately)

### 2.2 What must stay identical tick-to-tick

From `UMMStrategy.on_tick` / dryrun UMM path:

- `spread_min_bp`, `max_spread_jpy`, `gamma_high`
- `order_size_btc`, `max_position_btc`
- `hard_stop_bp`, `take_profit_bp`, `max_hold_sec`
- Skew formula: `-inventory * gamma_high * mid * 0.001`
- Quote eligibility / hold reasons
- Exit reasons and thresholds

ARM may only wrap **entry posting/fill** after CTRL would have been eligible to quote that side.

---

## 3. Runtime topology

```
shared_feeder (existing)
    ├─ CTRL: existing run_dryrun_umm_tf2bp_24h UMM path   [DO NOT MODIFY]
    └─ ARM:  new Shadow twin process/module                [WIRE=NO]
              reads same board
              computes same UMM quote decision
              diverges ONLY at entry execution
```

- CTRL source of truth remains `data/dryrun_umm_tf2bp_state.json` + dryrun logs.
- ARM writes **only** under `data/mm_research/umm_p2_maker_shadow/`.
- Do not mutate `configs/umm_tf2bp_config.json`.
- Do not touch PEG2-B Paper or LIVE TF2BP.

### 3.1 Minimum run length before judgment

| Gate | Requirement |
|------|-------------|
| MORE_DATA floor | ≥ **72h** Shadow wall-clock **or** ≥ **2000** ARM attempted posts |
| Prefer | 72h + fill_rate stability across Asia / EU / US sessions |
| Early abort | Only if integrity failure (feed gap, duplicate accounting, wire leak) |

---

## 4. Metrics (must log per trade / per attempt)

### 4.1 Headline (CTRL vs ARM)

- PnL bp / JPY  
- EV bp  
- PF  
- DD (running)  
- Win rate  
- Trade count / attempted posts / fills / cancel-timeouts  
- Fill rate  

### 4.2 Path

- AE_100ms · AE_1s · AE_3s · AE_10s · AE_30s  
- MFE · MAE · realized  
- `never_green` rate (loss & MFE ≤ 0.5)  
- `ae_worsen` rate (AE_10s < AE_1s − 0.5)  

### 4.3 Hold buckets (mandatory)

```
0-10s | 10-30s | 30-120s | 120s+
```

Report per bucket: n · WR · **別EV** · PnL · never_green% · ae_worsen%

### 4.3b Daily monitor card (mandatory — every JST day)

Write / refresh:

- `data/mm_research/umm_p2_maker_shadow/daily_monitor_latest.md`
- `data/mm_research/umm_p2_maker_shadow/daily/YYYY-MM-DD.md`
- `data/mm_research/umm_p2_maker_shadow/daily/YYYY-MM-DD.json`

**Must include every day:**

| KPI | Why |
|-----|-----|
| Never-green率 (CTRL / ARM / Δ) | toxic hold proxy |
| AE worsen率 (CTRL / ARM / Δ) | path decay proxy |
| 別EV: 0-10s / 10-30s / 30-120s | where edge lives |
| **10s+ sink** PnL & EV (CTRL / ARM / Δ) | primary repair target |

Focus question each day: *Did Maker shrink the 10s+ sink?*

### 4.4 Attribution (do not mix)

| Class | Definition |
|-------|------------|
| `FILLED_REALIZED` | ARM filled · compare realized vs CTRL paired when both trade |
| `NO_FILL_CANCEL` | ARM timeout cancel · CTRL may have traded (opportunity) |
| `BOTH_SKIP` | Neither entered |
| `CTRL_ONLY` | CTRL filled · ARM no fill |
| `ARM_ONLY` | ARM filled · CTRL did not (should be rare; flag if common = bug) |

Opportunity loss and realized loss **must not** be summed into one vanity metric.

### 4.5 Primary success hypotheses (pre-registered)

1. ARM reduces **avg AE_1s** vs CTRL (less toxic entry).  
2. ARM reduces **never_green%** and/or **10s+ loss mass**.  
3. ARM improves EV/PF/DD without WR collapse.  
4. Fill rate remains usable (PEG2-B reference ~high-70% was strategy-specific; UMM MM may differ — log, don’t retune).

---

## 5. Decision gate (simplified — user lock)

Apply only after sample floor (§3.1). Before floor → always **MORE_DATA**.

| Verdict | Rule |
|---------|------|
| **REJECT** | `ΔPnL <= 0` |
| **MORE_DATA** | 改善シグナルありだが標本不足、または指標が揃わない |
| **PROMOTE** | **全て揃う:** PnL改善 · PF改善 · DD改善 · Never-green率低下 · AE改善 |

Notes:

- DD改善 = ARM の DD が CTRL より浅い（ΔDD > 0）
- Never-green低下 = Δ(never_green) < 0
- AE改善 = avg AE_1s が CTRL よりマシ（ΔAE_1s > 0；毒性減少）
- 10s+ sink ΔPnL/ΔEV は **必須監視**（PROMOTE 必須条件には当面含めないが、改善なしなら PROMOTE を疑う）
- `PROMOTE` = Paper-candidate ラベルのみ。**LIVE 自動昇格なし**

---

## 6. Deliverables

```
data/mm_research/umm_p2_maker_shadow/
  LANE_LOCK.json          # this lane's frozen constants + bans
  state_arm.json          # ARM runtime state
  audit_arm.jsonl         # post/fill/cancel/exit events
  paired_summary.json     # CTRL vs ARM rollups
  report_latest.md        # human verdict stub
```

Periodic (≤1h): refresh `paired_summary.json` with windows `1h / 6h / 24h / 72h`.

### 6.1 Discord notify (mandatory)

Post verification results via `QuantDiscordNotifier.post_umm_p2_maker_shadow`:

| Event | kind |
|-------|------|
| Shadow start | `start` |
| Every `UMM_P2_DISCORD_INTERVAL_SEC` (default 3600s) | `periodic` |
| JST day roll (daily monitor) | `daily` |
| Verdict → REJECT / PROMOTE | `verdict` |
| Process stop | `final` |

Destination: analysis webhook + dryrun multicast (same research path as other observe reports). **Do not** use as LIVE trade alerts.

---

## 7. Implementation checklist (next coding agent)

1. Read this directive + `LANE_LOCK.json`.  
2. Implement ARM Shadow module **without** editing `UMMStrategy` champion params.  
   Prefer wrapper/adapter over forking strategy math.  
3. Pair on shared feeder timestamps with CTRL dryrun.  
4. Start ARM with WIRE=NO; verify PID + audit heartbeats.  
5. Leave CTRL dryrun, PEG2 Paper, LIVE untouched.  
6. Do **not** declare PROMOTE before §3.1 floor.

---

## 8. Portfolio context (do not disturb)

```
LIVE     = TF2BP Baseline          ✅ maintain
PAPER    = PEG2-B                  ✅ monitor only
RESEARCH = UMM-P2 Maker Shadow     ← this lane
FROZEN   = PEG_v3
REJECT   = AgingGuard
P2-design (later) = Maker Entry Universalization Study (no experiments until UMM-P2 resolves)
```

---

## 9. One-line agent contract

> Build and run **UMM Baseline vs UMM+Maker** Shadow.  
> Change **execution only**. Freeze everything else.  
> Judge **REJECT / MORE_DATA / PROMOTE**. Never touch LIVE.
