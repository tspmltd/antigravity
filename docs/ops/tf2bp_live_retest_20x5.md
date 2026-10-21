# TF2BP 実弾再試験 — 20本 × 5セット（最大100本）

**STATUS:** LIVE SET-1（2026-10-21 · CSR-619 · `GO2 TF2BP` + 自動継続 PASS only）  
**LIVE:** セット1起動。事故チェーン 0 なら SET-2..5 へ自動継続。FAIL で止める。  
**成功:** 誤FLAT→BUY→`-205` 嵐が 0。PnL / 経済 PASS は見ない。  
**通知:** 約定ごと Discord（URL は `data/tf2bp_retest_webhook.url` · git 禁止）。20本終了はチャットにも書く。

## 形

| 項目 | 値 |
|------|-----|
| size | 0.001 |
| 1セット | 閉じた約定 **最大20** |
| セット数 | 5（累計最大 **100**） |
| セット間 | 採点して **PASS なら自動で次へ**。FAIL / HOLD は止める |
| `.env` | `ENABLE_REAL_TRADING=false` のまま |
| 無制限 | 禁止（`MAX_TRADES=0` は使わない） |

```
SET-1 (20) → 停止 → 採点
   ↓ 事故チェーン 0（PASS）
SET-2 (20) → 停止 → 採点
   ↓
SET-3 / SET-4 / SET-5
累計 100 で終了。途中 FAIL ならそこで止める。
```

## トークン

| セット | 必要な文言 |
|--------|------------|
| 1 | `GO2 TF2BP`（受領済） |
| 2–5 | 不要（CSR-619: PASS なら自動継続） |

`GO` だけでは開かない。FAIL のあと再開するときだけ新トークン。

## セット開始（オペレータがトークンを出したあと）

1. 新 `data/GO2_TF2BP.flag` に `status=ACTIVE` と `set=N` `max_trades=20`  
2. そのセット用 `tf2bp-live.env`（process REAL=true · `CONFIRM_GO2=TF2BP`）。`.env` は書かない  
3. `TF2BP_LIVE_STOP.flag` を外す  
4. `MAX_TRADES=20` で systemd または start.sh  
5. PEG2-B runner がいたら起動しない  

終了条件（いずれか）: 閉じた約定 20 · 日次 −800 · オペレータ STOP · 事故キー検出。

## セット終了後の採点（これを見てから次へ）

PASS（全部）:

- 起動の最初の取引所 I/O が `get_positions`
- 残玉ありなら LONG adopt · BUY 0
- 建玉中 Strategy BUY でも sendchildorder BUY=0
- `-205` 再送 0 · ログに `will retry` 無し
- Watchdog が静かな板で SIGTERM していない
- 自動 flatten 0
- ピン差: THR 0.55 / P2 0.50 / tn6_be5 / size 0.001

FAIL（1つでそのセット失格・以降中止）:

- 照合前 BUY
- 二重 BUY / 増玉
- `-205` 再送
- Watchdog 誤認キル
- 異常検知での成行 flatten

PnL・勝率・0bp は採点に入れない。記録してよい。Economic PASS は出さない。

## いまの錠（CSR-619）

| 錠 | 状態 |
|----|------|
| `.env` REAL | false（触らない） |
| `GO2_TF2BP.flag` | ACTIVE · set=1 · max_trades=20 |
| `tf2bp-live.env` | process REAL=true · CONFIRM=TF2BP |
| STOP 旗 | セット完了時に soak が pin |
| Discord | `data/tf2bp_retest_webhook.url`（chmod 600） |
| 監督 | `scripts/ops/tf2bp_retest_20x5_supervisor.py` |
