# MS1_WOBI LIVE prep checklist (CSR-694)

**Status:** CSR-694b invariant in code · canary REAL armed via GO=`ACTIVE` (CSR-694c) · Economic_PASS=**NO** · `.env` ENABLE_REAL stays false

Greenfield runner: `antigravity/quant_pipeline/run_ms1_wobi_live.py`  
(Does **not** subclass TF2B_WOBI3 LIVE or paper DryRun.)

## Preflight

- [ ] Venue flat (`FX_BTC_JPY` net ≈ 0) · open orders = 0
- [ ] Shared feeder healthy (`data/market_feeder/latest.json` fresh)
- [ ] Paper MS1 (`run_dryrun_ms1_wobi_honest`) still green / observed
- [ ] No other LIVE: WOBIMM / WOBIMM5 / WOBI3 / PEG2B / TF2BP soak
- [ ] `data/GO_MS1_WOBI_LIVE.flag` exists with `status=PREP` (not ACTIVE yet)
- [ ] `.env` keeps `ENABLE_REAL_TRADING=false`

## DRY_RUN soak (default)

```bash
DRY_RUN=1 ./scripts/start-ms1-wobi-live.sh
# optional canary bounds:
# MAX_RUNTIME_SEC=3600 MAX_FILLS=20 DRY_RUN=1 ./scripts/start-ms1-wobi-live.sh
tail -f logs/ms1_wobi_live.log
./scripts/stop-ms1-wobi-live.sh
```

Accept when: posts/cancels/fills coherent · Maker EXIT on EV_GONE · no short · no double SELL · fee≈0 maker path · ledger rows sane.

## REAL arming (human only)

Exact tokens — all required:

1. Edit flag → `status=ACTIVE`
2. Shell: `CONFIRM_GO=MS1_LIVE`
3. Shell: `ENABLE_REAL_TRADING=true` (**operator-set**; scripts never write `.env`)
4. `DRY_RUN=0 SIZE=0.001 ./scripts/start-ms1-wobi-live.sh`
5. Prefer `MAX_FILLS=5..20` / `MAX_RUNTIME_SEC` for first canary

## Abort

```bash
./scripts/stop-ms1-wobi-live.sh
# STOP flag → runner flatten (Maker SELL if long) → SIGTERM
```

Leftover: confirm venue flat via API; do not open a second LIVE lane to “clean up”.

## Success metrics (canary)

| Metric | Target |
|--|--|
| size | 0.001 only |
| side | BUY enter only |
| exit | EV_GONE wall-anchor Maker |
| short | never |
| Economic_PASS | NO until separate CSR |
