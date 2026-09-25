# Antigravity 改善実行手順書（CSR-528-EXEC）

**基準:** 2026-09-25 20:00 JST 統合改善指示書  
**対象:** TF2BP / UMM / PEG / 4AGENT / Microstructure / Adverse / Arena  
**WIRE=NO · ENFORCE=0 · LIVE=OFF · Economic PASS前 ENFORCE禁止**

---

## PHASE 0｜安全ロック確認

| 項目 | 要求値 |
|------|--------|
| LIVE | OFF |
| WIRE | NO |
| AUTO_TUNE | OFF |
| ENFORCE | 0 |
| TF2BP CSR-499 | FROZEN |
| UMM CSR-504 | FROZEN（本体 Entry/spread 非改変） |
| PEG_v2 | ARCHIVE |
| PEG_v3 | RESEARCH ONLY |

**完了条件:** Baseline に変更が 1 件もないこと。作業開始時点の config/hash を保存。

## PHASE 1｜PEG_v3 実装監査（S0）

`exec_audit_peg_v3.jsonl` を正本に trade 単位監査。  
期待: AgingGuard@3s · BE3 · FakeLiquidity · AdaptiveRatio。

| 判定 | 条件 |
|------|------|
| REPORT_BUG | Execution=BE3 · レポートのみ BE5 |
| PEG_V3_EXPERIMENT_INVALID | Execution も BE5 |

**完了:** `CONFIG_MATCH = TRUE/FALSE` 一意確定。

## PHASE 2｜PEG Entry/Exit 分解（A）

同一 signal で A/B/C/D 比較。PEG_v4 禁止。  
完了: `PEG failure source = ENTRY / EXIT / BOTH`。

## PHASE 3｜CSR-521 Accounting（S0）

`delta = virtual_matched − actual_matched`（同一 trade_id）。  
母集団不一致 → `VALID_COMPARISON=false` · `ECONOMIC_PASS=false`。

## PHASE 4｜UMM Hold Ladder（S1）

L0–L4 個別: N / PnL / WR / HS / MAE / MFE / hold p50/p90。  
仮説（未確定）: L0=Alpha source · L1–L3=leakage。

## PHASE 5｜Conditional Unwind CF（S1）

**禁止:** age≥30 全決済。  
V0=Actual · V1=ALL@30 · V2=L1+@30 · V3=L2+@30 · V4=L3+@30。  
第一候補研究: `age≥30 AND ladder≥L2`（≠ ENFORCE）。

## PHASE 6｜Spread × Hold（S1）

S0 &lt;1.2 · S1 1.2–2.0 · S2 ≥2.0 × L0–L3。

## PHASE 7｜Long / Short Inventory

Side × Spread × Ladder。方向 Alpha 転用禁止。Inventory Control のみ。

## PHASE 8｜Micro Hard Veto CF（A）

ROLE=EXECUTION RISK / HARD VETO。veto_saved / false_veto → PASS/FAIL/NEED_MORE。

## PHASE 9｜4AGENT Action Schema

`candidate_*` と `final_*` 分離。HOLD と BUY/SELL 同時表示禁止。

## PHASE 10｜Adverse Shadow Gate（A）

Score bucket 0–100。Hard Gate は Economic PASS 後のみ。

## PHASE 11｜Arena GREEN/GREY/RED

GREEN FROZEN · GREY 0戦≠Fail · RED NO PROMOTION。

## PHASE 12–13｜Discovery 3レーン + Unknown→INVALID_RESULT

## PHASE 14｜Parquet 正本化

共通 ID · 6 テーブル · DuckDB JOIN。

## PHASE 15｜Economic PASS Gate

`DESIGN→OBSERVE→CF→ACCOUNTING→PASS→SHADOW→SOFT→FULL`

---

## 今日の実行順

1. PEG_v3 Config 監査 → CONFIG_MATCH  
2. CSR-521 Accounting → VALID_COMPARISON  
3. Virtual30_L2PLUS KPIs  
4. Hold × Spread × Side  
5. Fake-BO Veto CF  
6. 4AGENT schema  
7. Adverse Shadow  
8. Arena GREEN FROZEN  
9. Discovery（後）

## 完了判定ボード

| 項目 | 状態 |
|------|------|
| TF2BP Baseline | FROZEN |
| UMM Entry | FROZEN |
| UMM Inventory Control | RESEARCH |
| CSR-521 Accounting | BLOCKER→クリア後 PASS 経路 |
| PEG_v2 | ARCHIVED |
| PEG_v3 | AUDIT / REJECT PROMOTION |
| Micro Hard Veto | SHADOW VALIDATION |
| Adverse Gate | SHADOW |
| 4AGENT Schema | FIX REQUIRED |
| Arena GREEN | FROZEN |
| Discovery | LOWER PRIORITY |
| LIVE | OFF |

**本命一本:** CSR-521 Accounting → Virtual30_L2PLUS → Economic PASS


## 実行ステータス（2026-09-25 20:58:00 JST）

| PHASE | 状態 | 証拠 |
|-------|------|------|
| 0 安全ロック | **PASS** | `2026-09-25_csr528_phase0_lock.json` · LIVE/WIRE/ENFORCE clean · hashes保存 |
| 1 PEG_v3監査 | **DONE · CONFIG_MATCH=TRUE** | REPORT_BUG（実行BE3·Aging · 非INVALID）`csr528_s0_audits.json` |
| 2 PEG Entry/Exit分解 | PENDING (A) | — |
| 3 CSR-521 Accounting | **DONE · VALID_COMPARISON=true** | matched@30 n=380 Δ=+287.3（S0全日） |
| 4 Hold Ladder | **OBSERVE** | session n=975 · L0=+217 / L1=−92 / L2=−67 / L3=−10 |
| 5 Conditional Unwind | **OBSERVE** | L2PLUS Δ=+65.8 · HS avoided=11 · win_trunc=45 · MDD+31 · PF+0.41 |
| 6 Spread×Hold | **OBSERVE** | 全spread bucketで L0+ / L1–L2− |
| 7 Long/Short | **OBSERVE** | BUY/SELLとも L0+・L1+−（対称的左テール） |
| 8–15 | PENDING | ENFORCE=0 維持 |

**Economic PASS:** 未実施（OBSERVEのみ）。ENFORCE禁止。
