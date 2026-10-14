# PEG2-B LIVE Trial v1 — Paper→LIVE 乖離確認

**STATUS:** PHASE-1 ARMED · REAL ORDERS · lot=0.001  
**CSR:** CSR-588  
**GOV:** ENABLE_REAL_TRADING=process-true · `.env` 不変 · AUTO_APPLY=OFF · ENFORCE=0  
**目的:** 利益最大化ではない。**実約定品質**と Paper との乖離確認。

---

## 0. Scope

| Item | Rule |
|------|------|
| Strategy | **PEG2-B のみ** |
| Size | `0.001` BTC hard cap |
| Max wall | **6h** (Phase-1 1h → Phase-2 +2h → Phase-3 +3h) |
| Paper twin | 既存 `run_paper_tf2bp_peg2` を **monitor-only**（改変禁止） |

---

## 1. Absolute FROZEN

変更禁止（完全 FROZEN）:

- Signal
- Threshold
- Exit
- Inventory
- Sizing（lot は trial cap `0.001` のみ）
- Skew

Execution constants（PEG2-B 正本コピー）:

```
maker_offset_ratio   = 0.97
maker_timeout_sec    = 3.0
fallback_mode        = CANCEL_AND_WAIT
spread_guard_max_bp  = 4.5
target_bp            = 15.0
trail_stop_bp        = 4.0
trail_arm_mfe_bp     = 4.0
min_hold_before_trail_sec = 8.0
max_hold_sec         = 1200.0
imbalance_high_threshold  = 0.20
```

---

## 2. Forbidden（実装禁止 · OBSERVE ONLY）

- CSR-521
- Alpha vs Adverse
- PEG_v3
- TF2B_10.15
- 4AGENT Candidate

これらへの WIRE / 改変 / 併用発注は禁止。Shadow レーンは放置（触らない）。

---

## 3. Phases

| Phase | Duration | Enter when |
|-------|----------|------------|
| 1 | 1h | GO accepted + venue FLAT |
| 2 | +2h | Phase-1 成功条件 |
| 3 | +3h | Phase-2 も良好 |
| Cap | ≤6h | hard stop |

### Phase-1 → Phase-2 継続条件（1h 終了時）

1. Live EV ≈ Paper EV（\|ΔEV\| ≤ 0.25bp）
2. Fill Rate > 20%
3. Timeout Rate < 70%

### 6h 成功 → LIVE昇格レビュー提出

Live EV ≥ Paper EV − 0.10bp（同等以上）  
※ 昇格レビュー提出 ≠ 自動 LIVE 常駐化

---

## 4. Immediate stop → FLAT

| ID | Condition |
|----|-----------|
| A | 累計損益 ≤ −30bp |
| B | 4連敗 |
| C | Timeout Rate > 80%（attempts≥10） |
| D | Live EV < Paper EV − 0.50bp（閉じてた trades≥3） |
| E | 異常（API error / order reject / position mismatch） |

停止時: 全指値 CANCEL → 成行 FLAT → 原因レポート生成。

---

## 5. 30分レポート KPI

1. LIVE PnL  
2. Paper PnL 差分  
3. Fill Rate  
4. Timeout Rate  
5. Maker Fill %  
6. Average Slippage  
7. Win Rate  
8. EV  

---

## 6. Artifacts

```
data/mm_research/peg2b_live_trial/
  LANE_LOCK.json
  state.json
  audit.jsonl
  reports/30m_*.md
  trial_1h_report.md
  trial_3h_report.md
  trial_6h_report.md
  stop_report.md          # 停止時のみ
```

各 trial_* に: Paper vs Live 差分 / Good Fill / Bad Fill / Timeout Attribution。

---

## 7. Start gates（全て必須）

1. `data/GO_PEG2B_LIVE_TRIAL.flag` 存在  
2. `CONFIRM_GO=PEG2B_LIVE_TRIAL_V1`  
3. プロセス env `ENABLE_REAL_TRADING=true`（**.env へ書かない**）  
4. `--real --confirm-go PEG2B588`  
5. venue position = 0 · ACTIVE orders = 0  
6. size ≤ 0.001  
7. 他 PEG2B LIVE trial / TF2BP soak runner 非稼働  

```bash
# operator
touch data/GO_PEG2B_LIVE_TRIAL.flag
CONFIRM_GO=PEG2B_LIVE_TRIAL_V1 ENABLE_REAL_TRADING=true DRY_RUN=0 \
  bash scripts/start-peg2b-live-trial.sh
```

---

## 8. Non-goals

- パラメータ探索
- PEG_v3 / TF2B_10.15 / UMM LIVE 同居
- `.env` の恒久 `ENABLE_REAL_TRADING=true`
