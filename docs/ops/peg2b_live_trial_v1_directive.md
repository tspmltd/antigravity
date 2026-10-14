# PEG2-B LIVE Trial v1 — Paper→LIVE 乖離確認（CSR-588）

**STATUS:** PHASE-1 RESET · REAL · lot=0.001  
**目的:** Paper と LIVE の乖離確認。**利益追求ではない。** 実約定品質検証。  
**終了:** 自動 FLAT · **Review Only** · LIVE昇格は別審査（auto promote 禁止）

### Run-1（18:27–19:14）判定ロック

- **分類: TECHNICAL_ABORT**（FAIL ではない）
- 停止: `E:position_mismatch_oversize` = timeout/cancel vs COMPLETED レース → 保護停止
- 戦略負けではない。参考値: Live PnL +14.4bp · Fill 50% · Timeout 50% · WR 100%（FORCE_FLAT込み）
- 内部評価メモ: 研究 A- · 実装 C · 運用候補 A-（ロジックは生きている）
- Run-2 は **race修正 + venue_net==0 ゲート後** に Phase-1 1h リセット

---

## 戦略

| Item | Value |
|------|-------|
| Strategy | **PEG2-B のみ** |
| real | `true`（process env · `.env` 不変） |
| lot | `0.001` BTC |
| offset | `0.97` |
| timeout | `3s CANCEL_AND_WAIT` |
| Signal / Exit / Threshold | **完全 FROZEN** |

---

## 非接触（OBSERVE のみ · WIRE 禁止）

- CSR-521
- Alpha vs Adverse
- PEG_v3
- TF2B_10.15
- 4AGENT

---

## Phase-1（開始後 1 時間）

### 収集項目

- Live PnL · Live EV · Paper EV
- Fill Rate · Timeout Rate · Maker Fill %
- Slippage · Win Rate · Paper差分

### Phase-1 → Phase-2 継続条件（全て満たす）

1. Live EV ≥ Paper EV − 0.50bp  
2. Fill Rate ≥ 20%  
3. Timeout Rate ≤ 70%  
4. 累積損益 > −30bp  
5. Position mismatch なし  

---

## 停止条件 → 即 FLAT

| ID | Condition |
|----|-----------|
| A | PnL ≤ −30bp |
| B | 4連敗 |
| C | Timeout Rate > 80% |
| D | Live EV < Paper EV − 0.50bp |
| E | Execution異常（order reject / position mismatch / API異常） |

---

## Phase-2 / Phase-3

- Phase-2: +2h（累積 3h）— Phase-1 継続条件クリア時のみ  
- Phase-3: +3h（累積 6h）— さらに良好な場合のみ  
- Hard cap: **6h**

---

## レポート

`30min` / `1h` / `3h` / `6h`

出力必須:

- Paper EV · Live EV · ΔEV  
- Good Fill · Bad Fill · Timeout Attribution  
- Capture · Maker Rate  

Artifacts:

```
data/mm_research/peg2b_live_trial/
  trial_1h_report.md
  trial_3h_report.md
  trial_6h_report.md
  reports/30m_*.md
  stop_report.md
  LIVE_PROMOTE_REVIEW.md   # Review Only（別審査用メモ）
```

---

## Start gates

```bash
touch data/GO_PEG2B_LIVE_TRIAL.flag
CONFIRM_GO=PEG2B_LIVE_TRIAL_V1 ENABLE_REAL_TRADING=true DRY_RUN=0 \
  bash scripts/start-peg2b-live-trial.sh
```

Stop:

```bash
bash scripts/stop-peg2b-live-trial.sh   # → auto FLAT
```

### Run-2 判断（CSR-588c）

- cancel/fill レースは低レイテンシで十分あり得る部類。
- **問題はレース後の例外処理クラッシュ**（戦略ロジックではない）。
- 戦略評価は無傷。Run-1=TECHNICAL_ABORT 参考値。
- **Run-2 = 実質的な初回 LIVE 試験**（Phase-1 1h リセット）。

