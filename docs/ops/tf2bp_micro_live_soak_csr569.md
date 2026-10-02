# TF2BP micro LIVE soak — CSR-569

**Status:** PREPARE / DESIGN only · `ENABLE_REAL_TRADING=false` · LIVE=OFF  
**GO meaning:** design+gates locked · **not** real-order enable  
**GO2 required** before any LIVE flag flip.

## In scope
- TF2BP baseline only (`buy_only`, `tn6_be5`, FROZEN entry pins)
- Caps: size `0.001` BTC · max pos `0.001` BTC
- Daily loss stop `800` JPY / `60` bp
- Session loss stop `25` bp

## Out of scope
- PEG_v2 / PEG_v3
- UMM LIVE (separate process forever for soak)
- Council / Adverse WIRE
- Parameter retune during soak

## Gates before GO2
| ID | Check |
|----|--------|
| G0 | Governance + alerts ready · flag still false |
| G1 | ≥7d dryrun stability (costed) · no promote on 1 snapshot |
| G2 | hour / spread cell scorecard |
| G3 | latency-clean + fee stress |
| G4 | TF2BP-only process · flatten · deadman · run_id |

## Soak phases (after GO2)
1. **A** 2h supervised · max 20 trades · ops success ≠ Economic PASS  
2. **B** 24h micro · shadow twin compare  
3. **C** scale only with **GO3**

## Kill immediately
- equity < 5000 JPY remaining buffer breach
- API / latency_ms p90 > 200 for 10 min
- unhandled exception storm
- position != flat after flatten command > 60s

## Commands (placeholders — implement before GO2)
```bash
# still dry
# bash scripts/ops/start-tf2bp-micro-live.sh DRY_RUN=1

# only after GO2 checklist PASS + explicit user GO2
# ENABLE_REAL_TRADING=true bash scripts/ops/start-tf2bp-micro-live.sh DRY_RUN=0
```

## Forbidden
- Flip `.env` ENABLE_REAL_TRADING without GO2
- Attach PEG or UMM to the soak process
- Call Economic PASS from design lock alone
