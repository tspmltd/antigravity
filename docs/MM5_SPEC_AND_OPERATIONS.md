# MM5 — Spec & Operations (正本)

> **Document:** `docs/MM5_SPEC_AND_OPERATIONS.md`  
> **Status:** CANONICAL  
> **Series:** MM5 / `WOBIMM5_HONEST` (paper) · `WOBIMM5_LIVE` (LIVE chassis)  
> **Locks:** FIX.me CSR-672 (Position-EV), CSR-673 (Adverse observe/cut), CSR-676 (LIVE anti double-exit)  
> **Related:** `docs/honest_adverse_layer_matrix.md` · repo-root `.cursorrules`  
> **Revised:** 2026-11-10 JST

This file is the **source of truth** for MM5 design and Azure/VM operations.
If implementation drifts, update **this doc in the same change**.

---

## 1. Purpose

MM5 is the inventory market-making lane that keeps **Position-EV**:

- Hold inventory only while live **WOBI ∩ OFI** still agrees with the position.
- While EV > 0, **rest opposite maker** (do not dump / do not `instant_harvest`).
- When EV is gone or the adverse path is toxic, flatten at **HIT_TOUCH**.
- Observe adverse path on every fill (`mae` / `mfe` / `ae_*` / `hit_order`).

**MM4 harvest** remains the A/B baseline (`INSTANT_HARVEST=True`). Do not mutate MM4 into MM5 in place.

---

## 2. Design locks (non-negotiable)

| Lock | Value | Why |
|--|--|--|
| Position-EV | ON | `instant_harvest` abandons EV (CSR-672) |
| `INSTANT_HARVEST` | `False` | Harvest = baseline only |
| `REST_EXIT_ON_EV_HOLD` | `True` | EV>0 → rest opposite EXIT quote |
| `ADVERSE_HIT_TOUCH` | `True` | `adverse_cut` / `adverse_fast` / `ev_gone` / `hard_stop` at touch |
| AE kit profile | `inventory_mm` | Fast cut ON; see adverse matrix |
| Fast toxic | MAE ≤ −1.5 **or** ae_1s ≤ −1.0 | Early path cut before latch decay |
| Policy adverse_cut | −1.8 bp unrealized | From WOBI3 LIVE magnitudes |
| Paper fill | `FILL_MODEL=HONEST` + `HONEST_STRICT_OWN_SIZE=True` | No BOARD_TRADE same-tick fiction |
| LIVE trading exit | **maker LIMIT only** | Never auto-MARKET on `ev_gone` / adverse |
| Size / max inv | 0.001 BTC | Venue min lot; do not raise casually |
| Spread gate | **OFF** | CSR-661/662 — width ≠ loss |
| CROSS ENTER | **Forbidden** | One-sided maker |
| Economic_PASS | **NO** until signed CF | No invented promotion |

---

## 3. Architecture

```
shared_feeder (board + prints)
        │
        ▼
┌───────────────────┐     ┌────────────────────────┐
│ wobimm4_policy    │────▶│ decide(): WAIT/ENTER/  │
│ signed WOBI∩OFI   │     │ KEEP/PULL/EXIT         │
└───────────────────┘     └───────────┬────────────┘
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        ▼                             ▼                             ▼
 WOBIMM5_HONEST                HonestAdverseKit              WOBIMM5_LIVE
 (paper dryrun)                inventory_mm                  (clean chassis)
 run_dryrun_wobimm5_honest     path_toxic / close fields     run_wobimm5_live
 HONEST strict queue           ledger mae/mfe/ae_*           OID claim · private WS
                               HIT_TOUCH paper force         maker EXIT · GO gates
```

### 3.1 Paper (`WOBIMM5_HONEST`)

| Item | Path / value |
|--|--|
| Module | `antigravity/quant_pipeline/run_dryrun_wobimm5_honest.py` |
| Base | `WOBIMM4HonestDryRun` + MM4 hooks (`_ae_path_override`, `_ae_close_path`) |
| Policy | `antigravity/quant_pipeline/wobimm4_policy.py` |
| AE | `honest_adverse_kit.HonestAdverseKit(profile=INVENTORY_MM)` |
| Ledger | `data/mm_research/wobimm5_honest_dryrun/paired_ledger.jsonl` |
| State / PID | `data/dryrun_wobimm5_honest_state.json` · `data/dryrun_wobimm5_honest.pid` |
| Stop flag | `data/WOBIMM5_HONEST_DRYRUN_STOP.flag` |
| Log | `logs/dryrun_wobimm5_honest.log` |
| Start / stop | `scripts/start-dryrun-wobimm5-honest.sh` · `scripts/stop-dryrun-wobimm5-honest.sh` |
| WIRE | **NO** (`ENABLE_REAL_TRADING=false`) |

Paper exit force: same-tick board try, else `HIT_TOUCH_FORCE` when `_flatten_hit_touch(why)`.

### 3.2 LIVE (`WOBIMM5_LIVE`)

| Item | Path / value |
|--|--|
| Module | `antigravity/quant_pipeline/run_wobimm5_live.py` |
| Chassis | **Clean runner** — does **not** subclass paper DryRun |
| Libraries | `wobimm4_policy`, `HonestAdverseKit`, `BitFlyerClient`, `FeederBoardSource`, `WOBIDirectionalEngine3`, private child_order WS |
| Ledger | `data/mm_research/wobimm5_live/paired_ledger.jsonl` |
| State / PID | `data/wobimm5_live_state.json` · `data/wobimm5_live.pid` (also under `mm_research/wobimm5_live/`) |
| GO / STOP | `data/GO_WOBIMM5_LIVE.flag` · `data/WOBIMM5_LIVE_STOP.flag` |
| Log | `logs/wobimm5_live.log` |
| Start / stop | `scripts/start-wobimm5-live.sh` · `scripts/stop-wobimm5-live.sh` |
| Soak | `scripts/soak-wobimm5-live-1h.sh` · `scripts/soak-wobimm5-live-short.sh` |
| Report | `antigravity/quant_pipeline/report_wobimm5_live_soak.py` |

LIVE defaults to **DRY** (no venue POST). REAL is gated (section 5).

LIVE toxic path: `_toxic_maker_exit` — LIMIT at touch, never auto-MARKET.  
Adverse re-entry cooldown: **45s**. Entry stale cancel: **8s**. Toxic adopt threshold: **1.5 bp**.

---

## 4. Decision / exit order

### 4.1 Flat → ENTER

1. Latch signed signal (`CONFIRM_TICKS=3` paper; `LIVE_CONFIRM_TICKS=4` REAL).
2. Live WOBI∩OFI must still agree (no stale latch ENTER).
3. Honest / LIVE enter tightness when enabled (`HONEST_LONG_*` / `LIVE_ENTER_*`).
4. Post **one-sided** maker at frontier / touch. No CROSS ENTER. No spread WAIT.

### 4.2 In position

Priority (high → low):

1. **AE `path_toxic`** (inventory_mm) → `adverse_fast` → HIT_TOUCH  
2. **Policy `adverse_cut`** (unrealized ≤ −1.8 bp) → HIT_TOUCH  
3. Live agrees with position → **`ev_hold`** → rest opposite maker (`REST_EXIT_ON_EV_HOLD`)  
4. Live disagrees / NO_EDGE → **`ev_gone`** → HIT_TOUCH  

Working quote while flat/entering: EDGE_PULL on invert / soft edge loss (LIVE `edge_soft_pull`).

### 4.3 Ledger fields (AE)

On CLOSE, MM5 rows should carry kit path fields when tracking was active:

`mae_bp`, `mfe_bp`, `ae_1s`, `ae_3s`, `ae_10s`, `ae_30s`, `hit_order`, `fill_quality`,  
plus adverse trigger diagnostics when cut: `adverse_trigger_bp`, `adverse_fill_bp`, `adverse_overrun_bp`, `adverse_lag_s`.

---

## 5. LIVE GO gates (Azure VM)

`scripts/start-wobimm5-live.sh` **never** writes `ENABLE_REAL_TRADING=true`.

### 5.1 DRY (default)

```bash
cd /home/azureuser/antigravity
# DRY_RUN=1 is default
./scripts/start-wobimm5-live.sh
```

### 5.2 REAL (all required)

1. `data/GO_WOBIMM5_LIVE.flag` exists and contains `status=ACTIVE`
2. Shell: `CONFIRM_GO=WOBIMM5_LIVE`
3. Shell: `ENABLE_REAL_TRADING=true` (**operator-set only**)
4. Runner args: `--real --confirm-go WOBIMM5`
5. `SIZE <= 0.001`
6. `data/WOBIMM5_LIVE_STOP.flag` **absent**

```bash
cd /home/azureuser/antigravity
# Operator arms GO flag + env first, then:
DRY_RUN=0 CONFIRM_GO=WOBIMM5_LIVE ENABLE_REAL_TRADING=true \
  ./scripts/start-wobimm5-live.sh
```

### 5.3 Stop / flatten intent

```bash
./scripts/stop-wobimm5-live.sh   # writes STOP flag + SIGTERM
```

Do not `kill -9` as first resort; allow runner flatten / reconcile paths.

### 5.4 Paper A/B (does not touch LIVE)

```bash
./scripts/start-dryrun-wobimm5-honest.sh
./scripts/stop-dryrun-wobimm5-honest.sh
```

Hourly fleet Discord: `scripts/start-hourly-honest-reporter.sh`  
(includes MM4→MM5 Δ). Webhook is dedicated HONEST channel — not LIVE trade alerts.

---

## 6. Operational checklist (Azure)

| Check | Command / path |
|--|--|
| Paper alive | `pgrep -af run_dryrun_wobimm5_honest` |
| LIVE alive | `pgrep -af run_wobimm5_live` |
| Feeder | `data/market_feeder/latest.json` fresh |
| Paper log | `tail -f logs/dryrun_wobimm5_honest.log` |
| LIVE log | `tail -f logs/wobimm5_live.log` |
| Paper state | `data/dryrun_wobimm5_honest_state.json` → `rest_exit_on_ev_hold`, `adverse_fast_cuts` |
| LIVE state | `data/wobimm5_live_state.json` → `mode`, `halt_trading`, `private_ws` |
| GO flag | `data/GO_WOBIMM5_LIVE.flag` |
| AE on ledger | latest CLOSE has `mae_bp` / `hit_order` |
| Desync | LIVE: prefer adopt-on-pull / force-sync (CSR-676); hard halt only beyond `HARD_HALT_BTC` |

**Do not** restart LIVE in a loop to “fix” DESYNC without reading adopt / ghost / claimed OID logs.

---

## 7. A/B & Discord

| Lane | Role |
|--|--|
| `WOBIMM4_HONEST` | Harvest baseline |
| `WOBIMM5_HONEST` | Position-EV + AE repair |
| `WOBIMM4X2_HONEST` | MM scale-in (inherits MM4 Phase2 HIT_TOUCH) |

Reporter: `antigravity/quant_pipeline/honest_discord.py` · Δ line `MM4→MM5`.

Promotion rule: **paper CF first**. LIVE soak reports do not alone set Economic_PASS.

---

## 8. Tests (smoke)

```bash
cd /home/azureuser/antigravity
./venv/bin/python3 -m pytest \
  tests/test_wobimm4_policy.py \
  tests/test_honest_adverse_kit.py \
  tests/test_wobimm5_live_fill.py \
  tests/test_wobimm_honest_fill.py \
  -q
```

Policy test covers `rest_exit_on_ev_hold`. LIVE fill tests cover predict vs commit (no venue POST).

---

## 9. Change control

1. Paper change → new CSR note in `FIX.me` + update **this doc**.  
2. LIVE behavior change → CSR + dual review of GO gates / MARKET prohibition.  
3. Threshold changes (`ADVERSE_CUT_BP`, fast MAE, confirm ticks) need CF n + regime split — no silent retune.  
4. Repo agent rules: root `.cursorrules` must stay aligned with this doc.

---

## 10. Explicit non-goals

- Reintroducing `instant_harvest` on MM5  
- Auto-MARKET trading exits on adverse / ev_gone  
- Copying MS2 scalp `path_toxic=OFF` or TF Lead vacuum gates into MM5  
- Raising size above 0.001 without a separate GO  
- Mutating MM4 baseline into Position-EV in place  

---

*End of MM5 canonical spec.*
