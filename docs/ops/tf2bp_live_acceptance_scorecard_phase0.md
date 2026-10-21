# TF2BP LIVE 受入6条件 採点表 — Phase 0 / Gate 0 LOCK

**STATUS:** LOCK（2026-10-21 21:10 JST · CSR-606 · ロードマップ全体 21:11 JST 確定）  
**SCOPE:** ライフサイクル事故チェーンのみ。Maker/PEG・経済PASS・DeSeva/PRD/CAP・feeder 一本化は対象外。  
**SUCCESS:** コード行数ではない。下表6行が1つも落ちないこと。

## 確定版ロードマップ（LOCKED）

```
Phase 0  統治と採点（LIVE=OFF · 受入6条件LOCK · 既存経路ギャップ監査）※コード禁止
   ↓ Gate 0 （合意・ギャップ表完了）
Phase 1  外枠の安全化（誤認キル禁止 · 再送禁止 · 429はhalt）※I/O再建築はしない
   ↓ Gate 1 （スタブで再送0・誤認キル0）
Phase 2  核心 SM（venue SSOT · BUY 遮断 · 単一プロセス · 人手 flatten 手順書）
   ↓ Gate 2 （残玉あり起動で100% adopt・強制BUY注入でも発注0）
Phase 3  カオス試験（Paper/Sandbox のみ・建玉中 kill -9 復旧・障害注入）
   ↓ Gate 3 （事故チェーン再発 0件）
Phase 4  執行ライフサイクル審査（G0–G4 · GO2 TF2BP · Review Only）
```

**路線 B**（Maker 純化、PEG2-B レース調停、feeder I/O 一本化、往復 0bp）は Phase 4 合格後に独立して開く。  
429 の feeder 一本化は Phase 1 に入れない。Phase 1 は「429 を BUY/STALL に使わず、連続時 ENTRY_HALT」まで。

正本の観測ファイル:

| 役割 | パス |
|------|------|
| SM 監査 | `data/mm_research/tf2bp_live_sm_audit.jsonl` |
| heartbeat | `data/tf2bp_live_heartbeat.json` |
| ENTRY_HALT 旗 | `data/TF2BP_ENTRY_HALT.flag` |
| soak ログ | `logs/tf2bp_micro_live.log` |
| Watchdog ログ | Watchdog が書く TF2BP LIVE 監督ログ |
| 戦略ピン | `antigravity/strategies/tf2bp_strategy.py` |

SM 状態名は実装を正とする: `LONG`（spec 別名 `READY_POSITIONED` も PASS）。  
`-205` の sticky 状態は実装 `ENTRY_HALT`（spec 別名 `BLOCKED_MARGIN` も PASS）。

---

## 受入6条件

| # | 受入条件 | 判定対象 | PASS（観測キー） | FAIL（即時失格） |
|---|----------|----------|------------------|------------------|
| 1 | venue に 0.001 がある状態でプロセスを落としても BUY しない | 起動〜初回シグナル（クラッシュ再起動を含む） | 最初の **取引所 I/O** が `get_positions`。`venue_net==0.001` なら state∈{`LONG`,`READY_POSITIONED`}（adopt）。監査 `buy_submit_count==0` かつ `sendchildorder` BUY=0。照合完了前に判定ループを始めない | 照合前に BUY 送信。ローカル `position=0` で FLAT 初期化して RUN 開始。再起動後の最初の行為が BUY |
| 2 | POSITIONED 中の Strategy BUY は注文ゼロ | 建玉保有中 | 戦略 `action=="buy"` でも SM `can_submit_buy==false` → Drop。`sendchildorder` **BUY** 送信数 0。EXIT の SELL は本条件の対象外 | 取引所への BUY が 1 回でも飛ぶ |
| 3 | 静かな板 + 生きた hb → 殺さない | 無取引ホールド中 | `tf2bp_live_heartbeat.json` の age&lt;10s かつ PID 生存。取引ログ無更新が 120s/300s 続いても Watchdog が SIGTERM / kill / unit restart を出さない。PID 不変 | **発行された** SIGTERM / kill / 再起動。警告文字列だけの `STALL` ログは FAIL にしない |
| 4 | 建玉中の誤 STALL → SAFE_HALT。flatten しない | 異常検知時 | 遷移先 `SAFE_HALT` + アラート。自動 MARKET flatten（反対成行）送信数 0。tn6_be5 の通常 EXIT は本条件の対象外 | 異常検知を理由に成行でポジションを閉じる。Watchdog 起点の flatten |
| 5 | `-205` を何回返しても BUY 再送ゼロ | 証拠金不足時 | 受信直後 `error_class==NON_RETRYABLE_ENTRY`。state∈{`ENTRY_HALT`,`BLOCKED_MARGIN`} sticky。以降 `buy_retry_count==0`。ログに `will retry` が **無い** | `will retry` 出力。BUY 再送が 1 回でも出る |
| 6 | 戦略ピン無改変 | 全ソース | `THR=0.55` · `P2_MIN=0.50` · `tn6_be5` · `buy_only` · soak `size=0.001`。`tf2bp_strategy.py` の git diff **空** | 戦略ロジック / 上記ピンを 1 文字でも変更 |

---

## 原則（この採点表とセット）

- 一本道。7本並列パッチはしない
- 自動 flatten を Watchdog に足さない。Flat は人手手順（Phase 2）
- DeSeva / SEVA / PRD / CAP は LIVE に載せない
- PEG2-B / Maker 転換は路線 B（本採点表の外）
- LIVE=OFF のまま Phase 1 監査へ進む。Gate 0 合格は「文言LOCK」であり実装着手許可ではない

## Gate

| Gate | 意味 |
|------|------|
| **Gate 0** | 本表 LOCK + ギャップ表。通過済み（CSR-606/607） |
| **Gate 1** | スタブ PASS（CSR-608）: 連続429 halt · `-205` BUY再送0 · 静かな板+生きたhbで SIGTERM 0 |
| Gate 2+ | 1行でも FAIL ならその Phase に戻る |
