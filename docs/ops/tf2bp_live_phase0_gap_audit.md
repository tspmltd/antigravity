# TF2BP LIVE Phase 0 — 既存経路ギャップ表

**STATUS:** READ-ONLY AUDIT（2026-10-21 21:11 JST · CSR-607）  
**CODE:** 変更なし  
**採点表:** `docs/ops/tf2bp_live_acceptance_scorecard_phase0.md`（CSR-606 LOCK）

分類:

- **① 既に閉じている** — 現行コードがその条件を満たす経路になっている
- **② 未証明** — 経路はある／部分的だが、受入キーでの機械試験が無い、または状態名が採点表とズレる
- **③ 残存 will retry / 迂回リスク** — 古い文字列・別クライアント・自動再起動など、事故チェーンに戻り得る穴

監査対象: `run_micro_live_soak_csr574.py` · `tf2bp_live_state_machine.py` · `watchdog.py` · `runtime/client.py` · `core/bitflyer_client.py`（soak の実クライアント）· `start-tf2bp-micro-live.sh`

---

## 経路マップ

| 役割 | 実際に LIVE soak が使うもの |
|------|------------------------------|
| ランナー | `run_micro_live_soak_csr574.py` → `core.bitflyer_client.BitFlyerClient` |
| SM | `tf2bp_live_state_machine.py` |
| Watchdog | `tf2bp_micro_live` · hb ファイル · `kill_on_heartbeat_stall=False` |
| 起動 | `start-tf2bp-micro-live.sh`（pgrep 先行 + GO2） |
| 停止 | `stop-tf2bp-micro-live.sh`（process zero。flat は成功条件にしない） |
| **使わない** | `antigravity/runtime/client.py`（soak 非参照） |

---

## 受入6条件 × 分類

| # | 条件 | 判定 | 根拠（読み取り） | Phase で閉じる穴 |
|---|------|------|------------------|------------------|
| 1 | 残玉あり再起動で BUY しない | **① + ②** | soak は lock 後 `begin_reconcile` → `get_positions` → `apply_reconcile`。`LONG` なら `additional_buy=BLOCK`。`can_submit_buy` は `READY_FLAT` 以外拒否。SIGTERM は flatten せずループ離脱 | **②** クラッシュ再起動30回の機械試験なし。systemd/Watchdog が pid=None で unit start する経路は、復旧としては正しいが「Gate 3 で証明」が未実施 |
| 2 | POSITIONED 中 BUY ゼロ | **① + ②** | `can_submit_buy`: state≠READY_FLAT / venue_net≠0 / entry_halt → Drop。送信はゲート後だけ | **②** 強制 BUY 注入の自動試験なし（単体 SM テストは別ファイルにあり、soak 結合は未証明） |
| 3 | 静かな板 + 生きた hb → 殺さない | **① + ②** | TF2BP は log mtime を見ない。`explicit_file`。`kill_on_heartbeat_stall=False` で `allow_kill` を再強制 False。ログ静止でも SIGTERM しない | **②** 120s/300s 無取引の実測ログなし。他サービスは今も mtime→SIGTERM（TF2BP 以外。混同注意） |
| 4 | 誤 STALL → SAFE_HALT。flatten しない | **①部分 + ②** | soak 停止: `no_auto_flatten`。SIGTERM ハンドラは shutdown のみ。Watchdog stall は ENTRY_HALT **旗** + アラート。venue 不明の再チェックは `SAFE_HALT` | **②** 建玉中 stall の遷移先が `SAFE_HALT` ではなく `LONG` のまま + BUY ブロック、になり得る。自動 flatten 0 は①、状態名は未一致 |
| 5 | `-205` 再送ゼロ | **① + ③注意** | soak は `classify_order_error` → `NON_RETRYABLE` → `on_margin_insufficient` → ENTRY_HALT。`will retry` 文字列は soak / strategy に **無い**。`BitFlyerClient` POST は `_request_post_once`（再POSTしない）。TRANSIENT も再BUYせず ORDER_UNKNOWN | **③** 旧ログ `tf2bp_micro_live.log` に事故当時の `will retry` が残存（現行コードではない）。`runtime/client.py` は POST-once / -205 分類なし（soak 非使用だが迂回資産） |
| 6 | 戦略ピン無改変 | **①** | `tf2bp_strategy.py`: `THR=0.55` `P2_MIN=0.50` `buy_only` `tn6_be5`。soak 起動 SIZE≤0.001。本監査で戦略ファイルは未編集 | 以後の Phase でも diff 空を維持 |

---

## ③ 残存リスク（Phase 1–2 の入力）

| ID | 場所 | 内容 | 事故層 |
|----|------|------|--------|
| R1 | `runtime/client.py` | soak 非使用の第二クライアント。POST 再送防止も `-205` 分類も無い | 5 迂回 |
| R2 | 板 429 | Phase 1: 連続5回で `on_entry_halt(consecutive_429)`。BUY/STALL に使わない。GET 429 再試行は読み取りのみ | **閉じた（CSR-608）**。feeder 一本化は路線 B |
| R3 | Watchdog `pid is None` + `enable_auto_restart` | プロセス死亡後に `systemctl --user start`。Popen フォールバックは TF2BP では拒否 | 1 の復旧経路。殺す機械ではないが、自動再起動は Gate 3 で証明が要る |
| R4 | `max_order_size=max(size, 0.05)` | soak は 0.001 を送る。クライアント上限は 0.05 | 6 の脇。呼び出しサイズが漏れても 0.05 まで通る |
| R5 | start.sh `MAX_TRADES` 既定 0 | 無制限。GO2 ゲートは①。本数上限は採点表外 | 統治。Phase 4 審査で見る |
| R6 | 旧 `will retry` ログ | 現行コードに無し。採点表 FAIL キーが旧ログに当たると誤失格 | 観測手順で「現行ラン」だけを見る |

---

## 429（路線 B に出したもの）

| いま | Phase 1 に必要な最小 | 路線 B |
|------|----------------------|--------|
| 板 429 → BUY しない・hb 継続 | 連続 429 を BUY/STALL に使わず **ENTRY_HALT** | REST 全廃 / feeder 一本化 / 429=0.0% |

現状は「BUY に使っていない」（①に近い）が、「連続時 halt」は②。

---

## Gate 0 実務

- 文言 LOCK: CSR-606 済み
- ギャップ表: 本ファイル
- **実装着手はまだしない。** 次は Phase 1（外枠）で R2 の連続 429 halt と、条件 3/5 のスタブ試験から。
