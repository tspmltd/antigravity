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
- When EV is gone / early toxic (`adverse_fast`): **FLATTEN_SOFT** — passive opposite maker LIMIT (SELL@ask / BUY@bid; never cross).
- When `adverse_cut` (≤ −1.8bp) or toxic ADOPT/fill / hard_stop: **FLATTEN_HARD** — MARKET only.
- Observe adverse path on every fill (`mae` / `mfe` / `ae_*` / `hit_order`).

**MM4 harvest** remains the A/B baseline (`INSTANT_HARVEST=True`). Do not mutate MM4 into MM5 in place.

---

## 2. Design locks (non-negotiable)

| Lock | Value | Why |
|--|--|--|
| Position-EV | ON | `instant_harvest` abandons EV (CSR-672) |
| `INSTANT_HARVEST` | `False` | Harvest = baseline only |
| `REST_EXIT_ON_EV_HOLD` | `True` | EV>0 → rest opposite EXIT quote |
| Exit machine | HOLD → FLATTEN_SOFT → FLATTEN_HARD | Irreversible; no Soft←Hard downgrade |
| `FLATTEN_SOFT` | `ev_gone` / `adverse_fast` | Passive opposite maker LIMIT (no cross) |
| `FLATTEN_HARD` | `adverse_cut` / toxic ADOPT|fill / `hard_stop` | MARKET only (+45s re-entry cool) |
| AE kit profile | `inventory_mm` | Fast cut ON; see adverse matrix |
| Fast toxic | MAE ≤ −1.5 **or** ae_1s ≤ −1.0 | Early path → SOFT (not MARKET) |
| Policy adverse_cut | −1.8 bp unrealized | HARD MARKET threshold |
| Paper fill | `FILL_MODEL=HONEST` + `HONEST_STRICT_OWN_SIZE=True` | No BOARD_TRADE same-tick fiction |
| LIVE Soft exit | maker LIMIT @ opposite best | Never cross / never MARKET on Soft |
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

LIVE Soft path: passive opposite maker LIMIT (`soft_maker_exit_px`).  
LIVE Hard path: MARKET (`_is_market_flat_exit`) for `adverse_cut` / toxic ADOPT|fill / `hard_stop`.  
Adverse re-entry cooldown: **45s** (HARD). Entry stale cancel: **8s**. Toxic adopt threshold: **1.5 bp**.  
Spread Gate: ENTER only if best bid/ask spread **≥ 1.2bp** (`MIN_ENTRY_SPREAD_BP`); else `SPREAD_TOO_NARROW` wait.  
ENTRY Passive Offset: ENTER **0.5bp behind** touch (`ENTRY_OFFSET_BP` / `entry_passive_px`) — BUY `floor_tick(bid*(1−0.5/1e4))`, SELL `ceil_tick(ask*(1+0.5/1e4))`; Post-Only.  
TP Maker: best opposite front **clamped ≥ +1.0bp vs entry** (`MIN_HARVEST_BP`). Healthy `ev_gone` deferred **8s** (`TP_GRACE`, keep clamped TP); post-grace Soft uses plain best ask/bid. `adverse_fast`/HARD never deferred.  
Close-settle: **15.0s** after EXIT flat — late same-side FILL echoes blocked (`LATE_CLOSE_ECHO`).  
Late-Fill ADOPT: cancel↔fill race on our OID (`recent_posts` COMPLETED) → adopt into SM (toxic ≤−1.5bp → HARD).  
Unattributed flat≠venue: `GHOST_POSITION_BLOCKED` observe; **3 hits / 3s** → Fail-Closed halt + `flatten()` (no infinite loop).  
Soft adverse_fast escalate → HARD: reprice ≥ **3** or unrealized ≤ **−1.5bp** → pull Soft LIMIT + MARKET (+45s cool).

---

## 4. Decision / exit order

### 4.1 Flat → ENTER

1. Latch signed signal (`CONFIRM_TICKS=3` paper; `LIVE_CONFIRM_TICKS=4` REAL).
2. Live WOBI∩OFI must still agree (no stale latch ENTER).
3. **Spread Gate**: `spread_bp >= 1.2` else skip (`SPREAD_TOO_NARROW`).
4. Honest / LIVE enter tightness when enabled (`HONEST_LONG_*` / `LIVE_ENTER_*`).
5. Post **one-sided** maker **0.5bp behind** touch (`entry_passive_px` / `ENTRY_OFFSET_BP`). No CROSS ENTER.

### 4.2 In position

Priority (high → low):

1. **Policy `adverse_cut`** (unrealized ≤ −1.8 bp) → **FLATTEN_HARD** (MARKET)  
2. **toxic ADOPT/fill / hard_stop** → **FLATTEN_HARD** (MARKET)  
3. **AE `path_toxic`** (inventory_mm) → `adverse_fast` → **FLATTEN_SOFT** (passive LIMIT)  
4. Live agrees with position → **`ev_hold`** / HOLD → rest opposite maker at best ask/bid  
5. Live disagrees / NO_EDGE → **`ev_gone`** → **FLATTEN_SOFT** (passive LIMIT);  
   within **TP_GRACE (8s)** after ENTRY and no adverse → defer Soft, keep `ev_hold` TP

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

### 7.1 Final LIVE harvest decision (Soak8+)

After primary lever **Spread Gate (1.2bp)** and secondary lever **ENTRY Offset (0.5bp)** are both live, further short soaks are **not** iterated for tuning. One **final decision soak** (Soak8 class: REAL · size=0.001 · ~1200s / max_fills=5) closes the WOBIMM standalone-MM question:

| Outcome | Verdict |
|--|--|
| ≥1 gated round closes via **passive `ev_hold` TP at ≥ +1.0bp** (MIN_HARVEST clamp working as intended) | **PASS** — stop short soaks; advance to longer REAL stage / ops |
| No such harvest (no fill, or only Soft→HARD / adverse closes) | **FAIL** — **end** WOBIMM standalone MM pursuit; offline analysis or other strategy only |

Do **not** add a third live lever or run soak9+ seeking Harvest. Safety / infra failures remain separate (investigate; not a “retune” pass).

**Soak8 verdict (2026-11-10 17:19–17:37 JST, `max_fills:5`)**: **FAIL**.  
Closes: −0.73bp HARD (`soft_escalate_reprice`), −2.35bp HARD (`soft_escalate_slip`), −3.54bp `FLATTEN_VENUE` (open `ev_hold` cut by fill cap).  
Passive `ev_hold` ≥+1.0bp harvests: **0**. Safety/infra OK (ghost/desync/match=False=0).  
→ **WOBIMM standalone LIVE MM pursuit closed**; no soak9+ / no third lever. Offline analysis or other strategy only.

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
- Auto-MARKET on Soft exits (`ev_gone` / `adverse_fast`) — Soft is passive LIMIT only  
- Cross-priced Soft exits (SELL@bid / BUY@ask) — that was the friction-death bug  
- Copying MS2 scalp `path_toxic=OFF` or TF Lead vacuum gates into MM5  
- Raising size above 0.001 without a separate GO  
- Mutating MM4 baseline into Position-EV in place  

---

*End of MM5 canonical spec.*
