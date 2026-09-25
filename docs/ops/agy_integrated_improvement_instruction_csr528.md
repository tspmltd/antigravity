# Antigravity Quant System 統合改善指示書（CSR-528）

**基準時点:** 2026-09-25 20:00 JST  
**正本:** 本ファイル · MEM §36 · CSR.md CSR-528  
**WIRE=NO · ENFORCE=0 · LIVE=OFF · Economic PASS前 ENFORCE禁止**

## 0. 最上位運用原則（即時ロック）

| 項目 | 値 |
|------|-----|
| LIVE | OFF |
| WIRE変更 | 禁止 |
| 自動パラメータ更新 | OFF |
| Economic PASS前 ENFORCE | 禁止 |
| Baseline変更 | 禁止 |
| 改善対象 | OBSERVATION / SHADOW のみ |

順序（崩さない）:  
`観測 → Counterfactual → Economic PASS → ENFORCE候補 → 再観測`

## 1. 戦略ステータス（正式固定）

| 戦略 | STATUS | MODE | ACTION |
|------|--------|------|--------|
| **TF2BP CSR-499** | VALIDATED | FROZEN | KEEP · **CONTROL BASELINE** |
| **UMM CSR-504** | CONDITIONAL EDGE | FROZEN + CONTROL OBSERVATION | Inventory Control 研究（Entry/spread 不用意に変えぬ） |
| **TF2BP_PEG_v2** | REJECTED_FOR_PROMOTION | — | ARCHIVE |
| **TF2BP_PEG_v3** | REJECTED_FOR_PROMOTION | RESEARCH ONLY · ENFORCE=0 | `PEG strategy=FAIL` / `PEG execution IQ=KEEP` |

全改善効果は原則 **TF2BP との差**で判定。

## 2–3. S0 監査結果（2026-09-25 実施）

### PEG_v3 正本 vs execution → **REPORT_BUG**（EXPERIMENT_INVALID ではない）

- `exec_audit` / live params: `peg_v3_obs_v1` · `be_arm_bp=3.0` · AgingGuard 主因（exit AGING_GUARD n≈173）
- 監査ログに BE5 文字列 **0件**
- 旧定期レポートの BE5 表記は文言汚染（CSR-527 同期済）
- schema 欠落あり（trade_id / AE_* / be_arm_threshold 未記録）→ 今後の audit 拡張対象
- **PEG_v3 PnL は設計v3の評価として使用可**（昇格は引き続き禁止）

### CSR-521 Δ accounting → **VALID_COMPARISON=true**（matched population）

定義ロック: `delta = virtual_matched − actual_matched`（同一 trade_id 集合）

| 閾値 | matched n | actual_matched | virtual | **Δ** | VALID |
|------|-----------|----------------|---------|-------|-------|
| @30s | 380 | −271.89 | +15.41 | **+287.30** | true |
| @60s | 99 | — | — | **+104.98** | true |

母集団不一致時は `VALID_COMPARISON=false` とし Economic PASS から除外。

## 4–6. CSR-521 発展（次フェーズ · 未ENFORCE）

- **禁止:** `age>=30 → 全決済`
- **CTRL-2B OBSERVE:** Actual vs Virtual30_ALL / L1PLUS / L2PLUS / L3PLUS  
  第一候補: `age>=30 AND ladder>=L2` のみ Virtual Unwind
- Hold Ladder × Spread（&lt;1.2 / 1.2–2.0 / ≥2.0）
- Long / Short Inventory 分離（方向Alpha禁止 · Inventory Control限定）

## 7–10. ゲート層ロール

- Microstructure = **EXECUTION RISK / HARD VETO**（方向予測器から外す）
- Fake Breakout Hard Veto = Shadow CF（saved / false_veto）
- 4AGENT: `candidate_*` と `final_*` 分離（同一フィールド禁止）
- Adverse = Shadow Gate 先（帯別 EV）→ 確証後のみ Soft Gate 検討

## 11–12. FROZEN / PEG研究縮小

- TF2BP: threshold/exit/size/spread/execution/4AGENT結合 **一切変更禁止**（ログのみ増やす）
- PEG: v4/v5/v6 フル戦略禁止 · **PEG ENTRY QUALITY** のみ（ExitはBaseline CFで分離）

## 13–15. Arena / Discovery

- GREEN FROZEN OBSERVE · GREY 0戦は失格にしない · RED NO PROMOTION
- Discovery 3レーン A/B/C · Family再探索制限
- Unknown/0%/+0 フォールバック禁止 → INVALID_RESULT

## 16–18. ログ正本・毎時簡略・Economic PASS順序

正本ログ: market_snapshot / orderbook_micro / fusion_log / execution_log / inventory_control_log / counterfactual_log  
共通ID: trade_id · signal_id · decision_id · strategy_id · strategy_version · parameter_hash

Economic PASS順序:  
`DESIGN → OBSERVE → CF → ACCOUNTING CHECK → ECONOMIC PASS → SHADOW → SOFT ENFORCE → FULL ENFORCE`

## 最終優先順位

| 級 | 内容 |
|----|------|
| **S0** | PEG_v3 正本↔execution監査 · CSR-521 Δ accounting · trade_id照合 |
| **S1** | Virtual30_L2PLUS · Hold×Spread · Hold×Long/Short |
| **A** | Micro Veto CF · Adverse Shadow · candidate/final分離 |
| **B** | Arena GREEN FROZEN · Discovery 3レーン · Unknown map修正 |
| **REJECT** | PEG_v2/v3 promotion · 下位 InventorySkew/MM promotion |
| **ABSOLUTE FROZEN** | TF2BP CSR-499 |

## 最終指令（一言）

現フェーズは Alpha追加ではない。  
目的は既存Alphaを壊す Execution / Inventory / Adverse 要因の除去。  
最重要Control=TF2BP · 最大改善対象=UMM Inventory Control ·  
最重要研究=CSR-521 Conditional Unwind / Micro Veto CF / Adverse Shadow。
