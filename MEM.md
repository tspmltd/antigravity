# Antigravity システム運用・障害分析・ミリ秒HFT刷新メモ (MEM.md)

- **作成日時**: 2026-09-19 00:48 JST
- **運用環境**: Azure Linux (`/home/azureuser/antigravity`)
- **対象銘柄**: bitFlyer FX (`FX_BTC_JPY`)

---

## 1. 現状稼働ステータスと安全担保 (Safety & Execution Status)

| 項目 | 状態 | 詳細 |
| :--- | :--- | :--- |
| **LIVE実発注 (bitFlyer FX)** | **完全停止 (OFF)** | ポジション `0 BTC`, 有効注文 `0 件`, 証拠金残高 `¥6,390.0` を保護。`.env` の `ENABLE_REAL_TRADING=false` |
| **Dry-run 仮想検証** | **稼働中 (純粋シミュレーション)** | `quant_pipeline/run_pipeline.py` (PID 1221141) が仮想口座でリアルタイム記録中 |
| **HFT Goコア** | **稼働中** | `fusion_engine/fusion_engine` (PID 1200614) が Unix Socket (`/tmp/antigravity_fusion.sock`) で待機 |
| **監視デーモン** | **稼働中** | `antigravity/risk_guard/watchdog.py` (PID 1220272) が純粋Dry-runを常時監視 |

---

## 2. bitFlyer FX 損失発生の根本原因分析 (Root Causes of Red/Losses)

bitFlyer FX において「損失しか出ていない（still in red）」状態に陥っていた原因は、以下の3つの重大バグと1つのアーキテクチャ欠陥によるものでした。

### ①【致命的バグ】損益単位の不整合（BTC価格差分と円建て損益の比較）
- **発生箇所**: `antigravity/quant_pipeline/live_order_executor.py`
- **内容**: 
  - `stop_loss_jpy = 20.0`（許容損失20円）に対し、判定式が `diff = current_price - entry_price`（約1,270万円のBTC価格差）と比較されていた。
  - スプレッド（平均約2,000円〜4,500円）によるエントリー直後の気配値差（数千円）を「数千円の巨額損失」と誤認識し、**全ポジションがエントリー後わずか3秒で強制損切り**されていた。
- **対処完了**: 実際の円損益 `pnl = (diff * size)` を算出し、`pnl <= -¥25.0`（損切り）、`pnl >= +¥35.0`（利確）で正しく判定するよう修正。

### ②【シミュレーションの虚構】気配値・スプレッドの無視
- **発生箇所**: `antigravity/quant_pipeline/dryrun_simulator.py`
- **内容**: 
  - 仲値（Mid-price）のみで約定判定を行っていたため、実取引では必ず支払う片道1,000〜2,000円のスプレッドコストがゼロ計算されていた。
  - その結果、シミュレーション上は微小利益に見えても、実発注ではスプレッド負けを繰り返していた。
- **対処完了**: `best_bid`（売り約定レート）と `best_ask`（買い約定レート）をシミュレータに供給し、実勢スプレッドを厳格に差し引くロジックへ刷新。

### ③【スプレッド急拡大時の強制エントリー】
- **発生箇所**: `antigravity/quant_pipeline/safety_gate.py`
- **内容**: ボラティリティ急変時、スプレッドが5,000円〜10,000円に拡大してもエントリーシグナルを通過させていた。
- **対処完了**: `max_spread_jpy = 3000.0`（スプレッド3,000円超で全エントリー即時遮断）のハードリミットを導入。

---

## 3. ユーザー指摘と決定的ボトルネック: 2秒RESTポーリングの遅延限界

### なぜ 2.0秒 REST ポーリングでは勝てないのか？
**「それは遅すぎるms単位ですよ」**というユーザー指摘の通り、2.0秒（2,000ms）間隔の HTTP REST ポーリングで逆選択（Adverse Selection）を防ぐことは物理的に不可能です。

```
【市場での現実 (5〜30ms)】
t = 0ms   : 大口トキシック・テイカーが成行買いを bitFlyer に発注
t = 5ms   : 最良売気配（Depth 1）が瞬時に枯渇・貫通（自分の売り指値が最悪値で喰われる）
t = 15ms  : 板が吹き飛び、仲値が 2,000円 急上昇
           
         ( 〜 2,000ms 経過: パイプラインは time.sleep(2.0) 中 〜 )
           
t = 2,000ms: urllib.request で GET /v1/board を送信
t = 2,080ms: HTTPレスポンス受信（往復80ms遅延）
t = 2,081ms: adverse_risk_score が「逆選択の危険！」と警報を鳴らす
              ⇒ ★ 既に 2,076ms 前に最悪レートで約定済み。死体を眺めているだけ。
```

- DuckDB の過去ログ解析（181,875件、54,228 Tick）結果:
  - 勝率: 50.9%
  - プロフィットファクター (PF): 1.00
  - 累計 PnL: -¥7.2
  - 平均スプレッド: ¥2,084
- **結論**: 2秒遅延の環境下では、常にトキシック・テイカーに喰われた後の不利な約定を掴まされ、スプレッドコストを回収できない。

---

## 4. ミリ秒（ms）スケール WebSocket イベント駆動刷新計画

リポジトリ内に実装済みの高頻度ストリーム基盤（`ws_engine` および Go `fusion_engine`）へ全面移行する。

### 構成要素
1. **Push駆動ストリーム (`antigravity/ws_engine/stream.py`)**:
   - WebSocket URL: `wss://ws.lightstream.bitflyer.com/json-rpc`
   - 購読チャンネル:
     - `lightning_ticker_FX_BTC_JPY`: 最良気配・板厚更新（遅延 < 10ms）
     - `lightning_executions_FX_BTC_JPY`: 全約定データ（遅延 < 5ms）
2. **インメモリ Microstructure 解析器**:
   - `OrderBookTracker` (`antigravity/ws_engine/orderbook.py`): Micro-Price、Top Imbalance を <0.1ms で O(1) 算出。
   - `FlowAnalyzer` (`antigravity/ws_engine/flow_analyzer.py`) & `hawkes.go` (`fusion_engine/`): 直近100ms〜1秒の Taker Delta・自己励起強度（Hawkes Intensity）をリアルタイム追跡。
3. **ミリ秒 Adverse Selection 遮断**:
   - Depth 1 の急激な枯渇、Micro-Price の不利方向への瞬間乖離を検知した瞬間（< 1ms）、`adverse_risk_score >= 0.70` を発火して指値撤退・成行エグジットを発火。
4. **イベント駆動パイプライン (`antigravity/quant_pipeline/`)**:
   - `ingestion.py` の `poll_once()`（REST）を廃止し、WebSocket コールバック直結に変更。
   - `run_pipeline.py` の `time.sleep(2.0)` を撤廃し、Tick / Ticker Push が着弾するたびにゼロ遅延でシミュレータ＆エージェントを駆動。

---

## 5. Git 管理方針とコミット構成

### .gitignore の是正
- 自動生成戦略ディレクトリ（`strategies/proposed/`, `strategies/rejected/`, `strategies/optimizing/`）を Git 除外。
- 実行時キャッシュ・ログ・Parquet・DuckDB・一時 JSON を除外。

### 今回のコミット対象
1. **逆選択検知 & 損益バグ修正**:
   - `live_order_executor.py`: 円建て PnL 判定、気配値エグジット
   - `dryrun_simulator.py`: スプレッド反映型仮想約定
   - `sync_executor.py`, `fusion_engine.py`, `safety_gate.py`: スプレッド上限 (¥3,000) & 逆選択フィルタ
   - `microstructure_agent.py`: Adverse Risk Score 算出
2. **WebSocket & HFT 基盤**:
   - `antigravity/ws_engine/`: WebSocketTickStream, OrderBookTracker, FlowAnalyzer
   - `fusion_engine/`: Go HFT コア（Hawkes過程, RingBuffer, Unix Socket IPC）
3. **ドキュメント & メモ**:
   - `MEM.md` (本書)
   - `.gitignore` (クリーン化)

---

## 6. 4AGENT 合同評議会 (Council) ＆ 戦略有効反映システム (2026-09-19 実装)

4つの専門エージェントが独立して分析結論を出し、それをリアルタイム戦略・安全ゲート・執行系へ有効に反映させるオーケストレーション機構を構築・稼働。

### 4AGENT の役割と分析結論 (AgentConclusion)
1. **① マイクロストラクチャー板解析エージェント (`MicrostructureAgent`)**:
   - **分析結論**: 板厚不均衡 (Imbalance), Micro-price 乖離, フェイクブレイク (だましキャンセル多発)。
   - **戦略反映**: フェイクブレイク検知時に新規エントリーを即時遮断 (`hard_veto = True`, `size_mult = 0.0`)。
2. **② トレンド追従エージェント (`TrendFollowAgent`)**:
   - **分析結論**: 中期価格傾き (方向性), トレンド強度, 市場レジーム (`trend` / `range` / `high_vol` / `low_vol`)。
   - **戦略反映**: レジーム判定に応じたリスクサイズ調整 (高ボラ時はロット半減、レンジ時はスプレッド刈り)。
3. **③ DuckDB 最適化エージェント (`DuckDBOptimizerAgent`)**:
   - **分析結論**: Parquet 過去ログから勝率・PF・勝敗要因を統計解析。レジーム別最適重み (`W_PRESSURE`, `W_CONFLICT`) および最適スプレッド上限を自律導出。
   - **戦略反映**: 最適重み (`configs/approved_weights.json`) をファイル出力し、`SignalFusionEngine` が無停止ホットリロードで常時適応。
4. **④ ADVERSE 専門研究・防御エージェント (`AdverseResearchAgent`)**:
   - **分析結論**: 逆選択エピソード状態機械 (`DEPLETING`, `OPP_TAKER`, `MAKER_VICTIM`)、逆選択スコア、RTT先回りリードタイム (`lead_ms ≥ 85ms`)。
   - **戦略反映**:
     - **Hard Veto**: 逆選択予兆のある方向への新規発注を完全拒否。
     - **Emergency Cancel & Evacuation**: トキシック成行直撃予兆時に指値キャンセル (`action = "cancel"`) および建玉の成行緊急撤退 (`ADVERSE_EMERGENCY_CANCEL`) を発令。

### 評議会コーディネーター (`FourAgentsCouncil`)
- 各エージェントの結論を集約し、総合判定 (`CouncilVerdict`) を策定。
- 最新状態を `configs/agents_council_state.json` へ常時アトミック保存。
- Discord 分析サーバー & Dry-run サーバーへ定期および緊急レポートを配信。
- `tests/test_four_agents_integration.py` による結合テスト全 PASS 担保。

---

## 7. システムリソース圧迫緊急対応 ＆ 恒久ログ・CPU対策 (2026-09-19 01:25 JST)

CPU 95%超えのアラート頻発およびメモリ逼迫に対する緊急是正と恒久対策を実施。

### 根本原因の特定
1. **Go `fusion_engine` 合成フィーダーの過剰頻度**:
   - `NewSyntheticFeeder` が 1,000μs (1ms = 毎秒1,000回) で4銘柄の板・約定を生成しており、2コアCPUの約20%を常時占有していた。
2. **古い `agy` ゾンビプロセスの居座り**:
   - 9月16日から残留していたプロセス (PID 616903) が約 380MB のメモリを浪費。
3. **`/tmp` 一時ログの肥大化**:
   - 古い実行ログが合計約 200MB 蓄積。

### 恒久対策の実施
1. **Go `fusion_engine` スロットリング (Commit `2aa2cc1`)**:
   - `fusion_engine/main.go`: 合成フィーダー周期を BTC: 20ms (毎秒50回) / 他銘柄: 100ms (毎秒10回) に緩和して再ビルド。
   - CPU使用率: 18% ➔ **1.0% に激減**。
2. **自動ログローテーション新設 (`antigravity/risk_guard/log_rotator.py`)**:
   - 15MB 超過ログを検知し、末尾 5MB を残して自動切り詰め。
   - `system_monitor.py` の定期監視ループに統合。
3. **ゾンビプロセス終了 & クリーンアップ**:
   - 利用可能メモリ: **5.8GB / 7.7GB** へ回復。ディスク使用率: **30%** (空き 87GB)。

---

## 8. UMM ＆ TF2BP 最新確定バージョンの GIT 導入と 24時間 Dry-run 観察 (2026-09-19 01:35 JST)

GIT正本（`FIX.me` 2026年9月11日〜14日停止直前確定記録 CSR-504 / CSR-495/499）から最新確定ロジックを抽出・再配備。

### 確定仕様
1. **① UMM (Unified Market Making v1 - CSR-504 準拠)**:
   - ソース: [`antigravity/strategies/umm_strategy.py`](file:///home/azureuser/antigravity/antigravity/strategies/umm_strategy.py)
   - パラメータ: `spread_min_bp: 1.2`, `gamma_high: 0.15`, `take_profit_jpy: 35.0`, `stop_loss_jpy: 25.0`, `max_hold_sec: 1800.0`, `order_size_btc: 0.001` (最大枠 `0.005 BTC`)。
2. **② TF2BP (2bp Micro Trend Order Flow v1 - CSR-495/499 準拠)**:
   - ソース: [`antigravity/strategies/tf2bp_strategy.py`](file:///home/azureuser/antigravity/antigravity/strategies/tf2bp_strategy.py)
   - パラメータ: `micro_mom_bp: 2.0`, `target_bp: 15.0`, `trail_stop_bp: 4.0`, `reverse_noise_max: 0.25`, 重い層 2セル `1600_2200|mid|BOOST` / `1600_2200|hi|BASE` 固定, θ固定 `3.82 / 0.12`。

### 運用規則：自動調整の完全禁止 (FROZEN / MANUAL-ONLY)
- 設定ファイル: [`configs/umm_tf2bp_config.json`](file:///home/azureuser/antigravity/configs/umm_tf2bp_config.json)
- `auto_tune_allowed = False`, `frozen_mode = True` を強制。DuckDB や Evolver による自動変更を完全遮断し、パラメータ変更は**ユーザーからの明示的指示のみ**とする。

### 24時間観測ランナー & 逆選択直結
- ランナー: [`antigravity/quant_pipeline/run_dryrun_umm_tf2bp_24h.py`](file:///home/azureuser/antigravity/antigravity/quant_pipeline/run_dryrun_umm_tf2bp_24h.py) (PID `1229367`)
- 4AGENT の `AdverseResearchAgent` とリアルタイム連携し、逆選択スコア高騰時に `ADVERSE_EMERGENCY_EVACUATE` を即時発火して最小微小損で仮想撤退。
- `data/dryrun_umm_tf2bp_state.json` に毎秒アトミック保存、15分ごとに Discord Quants Dry-run チャンネルへ進捗自動報告。
- `antigravity-watchdog.service` に登録し、24時間常駐監視と自動再起動を保証。

---

## 9. 承認済み12戦略 統合Dry-runアリーナ (Approved Strategy Arena) (2026-09-19 01:52 JST)

過去にバックテストを通過・承認された全12個の実戦アルゴリズムを一堂に会し、同一相場で並行シミュレーションを行う「アリーナ」を新規配備。

### 参戦12アルゴリズム
1. `strat_1ac224f3` (GridMM v1)
2. `strat_6e5a6296` (GridMM v2)
3. `strat_92a1dffd` (GridMM v3)
4. `strat_4d3f2c9f` (MicroSpreadMM v1)
5. `strat_a5d8ae20` (MicroSpreadMM v2)
6. `strat_cbcd5aed` (SpreadCaptureMM v3)
7. `strat_de08146e` (InventorySkewMM - UMM原型)
8. `strat_efd3fab8` (EmaTrend - トレンドフォロー)
9. `strat_9ca5d130` (RsiMeanReversion - 旧LIVE候補、急変ブロック)
10. `strat_7e09696a` (RsiMeanReversion - BB逆張り+RSI)
11. `strat_a2a6d745` (RsiMeanReversion - BB逆張り+RSI反転)
12. `micro_trend_order_flow` (MicroTrend - TF2BP原型)

### 超低負荷 1プロセス統合方式
- ランナー: [`antigravity/quant_pipeline/run_dryrun_approved_arena.py`](file:///home/azureuser/antigravity/antigravity/quant_pipeline/run_dryrun_approved_arena.py) (PID `1229372`)
- 12戦略を個別起動せず、単一ループ内で同一のリアルタイムTickerおよびローリング1分足を用いて一括評価。
- **CPU負荷: ~1.5%、追加メモリ: ~80MB** と極めて省エネ。
- 各承認時の固定パラメータで稼働（自動調整完全禁止）。
- `data/dryrun_approved_arena_state.json` にリアルタイム順位表（1位〜12位）を保存。
- 15分ごとに Discord Quants Dry-run チャンネルへランキング速報を配信。
- `antigravity-watchdog.service` に登録して死活監視。

---

## 10. 現在の常駐全 Dry-run / ペーパートレード体系一覧 (2026-09-19 時点)

| # | システム / ランナー | PID | 対象市場 | 役割・特徴 | 運用モード |
| :--- | :--- | :---: | :--- | :--- | :--- |
| **1** | **UMM & TF2BP 24h 観測** | `1232587` | `FX_BTC_JPY` | 最新確定版MM & 極小トレンドの24時間連続耐久テスト | 🔒 固定 (手動指示のみ) |
| **2** | **承認済み12戦略 統合アリーナ** | `1232592` | `FX_BTC_JPY` | 承認済み全12戦略のリアルタイム比較淘汰・ランキング | 🔒 固定 (手動指示のみ) |
| **3** | **4AGENT Quant Pipeline** | `1232568` | `FX_BTC_JPY` | 4エージェント合議意思決定（板・トレンド・オプティマイザ・逆選択） | ⚡ 自律合議シミュレーション |
| **4** | **DRYRUN 毎時統合リポーター** | `1232596` | 全戦略 | 1時間毎（毎時ジャスト）に全Dry-runの成績を集約してマルチキャスト送信 | 📢 毎時自動配信 |
| **5** | **日本株専属ポッド (JP Pod)** | `1232575` | 日本株6銘柄 | 東証・PTSミリ秒板微細構造＋開示速報連動ペーパートレード | 🛡️ 5大安全装置下ペーパー |
| **6** | **Watchdog Sentinel** | `1232458` | 全サービス | `systemd --user` 常駐による24時間死活監視・自動再起動・リソース保全 | 🚨 自律守護 |

---

## 11. DRYRUN 1時間毎 統合定期レポート配信体制の確立 (2026-09-19 02:23 JST)

ユーザーからの「DRYRUNの定期報告が来ない、1時間毎に通知」という指示に対応。

### 原因の究明
1. **通知先チャンネルの乖離**:
   - 新規作成した Dry-run ランナーが `DISCORD_DRYRUN_WEBHOOK_URL` のみに送信しており、ユーザーが閲覧しているメイン定期運用報告チャンネル (`DISCORD_REPORT_WEBHOOK_URL` / `DISCORD_LIVE_WEBHOOK_URL`) に届いていなかった。
2. **通知間隔の不一致**:
   - ランナー内部が15分（900秒）刻みで個別送信する設定となっていた。

### 是正対策と新機構
1. **マルチキャスト送信の導入 (`quant_discord_notifier.py`)**:
   - `post_dryrun_multicast`: メイン運用報告チャンネル (`DISCORD_REPORT_WEBHOOK_URL`) と `DISCORD_DRYRUN_WEBHOOK_URL` の**双方へ同時にレポートを配信**。見逃しを物理的に根絶。
2. **1時間毎 統合定期レポート配信デーモン新設 (`hourly_dryrun_reporter.py`, PID `1232596`)**:
   - 1時間ごと（毎時00分ジャスト）に、稼働中の全Dry-run（① UMM & TF2BP、② 承認済み12戦略アリーナ、③ 4AGENT合議システム）の最新成績・順位を集約し、**「📊 【DRYRUN 1時間毎 統合定期レポート】」** を自動送信。
   - 起動直後に初回最新ステータスを即座に送信。
3. **全ランナーの通知間隔を 3600秒 (1時間) に統一**:
   - 各設定ファイル (`configs/umm_tf2bp_config.json`, `configs/approved_arena_config.json`) および `watchdog.py` の起動引数を 3600秒に統一。
4. **Watchdog 常駐登録**:
   - `hourly_dryrun_reporter` を監視対象に組み込み、毎時の定期配信を恒久担保。

---

## 12. 成績の bp（ベーシスポイント）表示化 & 1時間・24時間累積 二重集計体制 (2026-09-19 02:33 JST)

ユーザーからの**「全て成績はｂｐ表示で、1時間と２４時間のるいせきで」**という指示を完全実装。

### 1. 損益表示の bp（ベーシスポイント）統一
- **計算式**:
  $$\text{PnL (bp)} = \left( \frac{\text{損益 (JPY)}}{\text{発注数量 (BTC)} \times \text{約定価格 (Mid/LTP)}} \right) \times 10,000$$
  - 発注ロット 0.001 BTC（約12,650円）の場合、1円の損益は約 **0.79 bp**。
- **適用対象**:
  - ① UMM (CSR-504)
  - ② TF2BP (CSR-499)
  - ③ 承認済み12戦略アリーナ（各戦略およびアリーナ合計）
  - ④ 全Dry-run 総合計
  - ⑤ コンソールリアルタイム出力（`+XX.Xbp (¥+XX)` 表記）
  - ⑥ Discord Embed 毎時定期レポート（bp を第一主表記とし、円損益・取引数・勝率を補足併記）

### 2. 直近1時間 (1h) & 過去24時間累積 (24h) のローリング二重集計
- 各戦略に `trades_history: List[Dict[str, Any]]` を実装（各取引のタイムスタンプ `ts`、`pnl_jpy`、`pnl_bp`、`is_win` を永続保持）。
- `get_window_stats(hours)` メソッドにより、指定した時間枠（1.0h および 24.0h）内の：
  - 取引回数 (`total_trades`)
  - 勝ち数 / 負け数 (`win_trades` / `loss_trades`)
  - 勝率 (`win_rate_pct`)
  - 損益 (bp) (`pnl_bp`)
  - 損益 (円) (`pnl_jpy`)
  をミリ秒精度で集計。
- 状態永続化 JSON (`data/dryrun_umm_tf2bp_state.json`, `data/dryrun_approved_arena_state.json`) に `stats_1h` および `stats_24h` フィールドを追加。
   - **対策**: キーワードを `hourly_dryrun_reporter` に修正し、重複プロセスを一掃。PID `1233574` で単一常駐が正常に確立。

### 4. 現在の稼働中プロセス一覧 (2026-09-19 21:09 JST 更新)
| プロセス名 | PID | 役割 | 損益表示 |
| :--- | :---: | :--- | :---: |
| `antigravity.risk_guard.watchdog` | `1345230` | 24時間死活監視・自動再起動・リソース保護 | - |
| `antigravity.quant_pipeline.run_pipeline` | `1345348` | 4AGENT 合議シミュレーション | JPY/bp |
| `antigravity.quant_pipeline.run_dryrun_approved_arena` | `1345373` | 承認済み12戦略アリーナ (FROZEN) | **1h & 24h bp** |
| `antigravity.quant_pipeline.hourly_dryrun_reporter` | `1345379` | 毎時ジャスト 統合レポートマルチキャスト配信 | **1h & 24h bp** |
| `antigravity.quant_pipeline.run_dryrun_umm_tf2bp_24h` | `1345368` | UMM ＆ TF2BP Baseline ＆ **TF2BP_PEG_v2 観測** | **1h & 24h bp** |

---

## 13. TF2BP_PEG_v2 (Model 3+1) OBSERVATION 並行観測稼働 ＆ 毎時検証通知の配備 (2026-09-19 21:10 JST)

ユーザーからの**「効果を検証したいのでOBSERVATIONで、TF2BPにMODEL３＋１をのせたPEG＿V2を稼働させて、１時間毎に検証結果を通知して」**という指示に対応。

### 1. バックテスト（BT）全6モデル実板検証の結果
`data/parquet/orderbook_micro/` に蓄積された 81,855 行の実板・マイクロ秒歩み値データを用いて、全 6 モデルの精密シミュレーションを実施：

| モデル番号 | モデル名称 | 累積損益 | 取引数 | 勝率 | Payoff比 | ベースライン比 |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **Baseline** | PEG 固定 0.95 (CSR-499) | -110.2 bp | 78回 | 25.6% | 1.11 | - |
| **Model 1** | Dynamic Ratio (板厚連動) | -89.6 bp | 82回 | 29.3% | **1.23** | **+20.6 bp 改善** |
| **Model 2** | Queue Aware (先頭キュー奪取) | -122.4 bp | 64回 | 21.9% | 0.98 | -12.2 bp |
| **Model 3** | **Effective Reach (テイカー攻撃性ブースト)** | **-55.8 bp** | 71回 | **35.2%** | **1.28** | **+54.4 bp 最大改善** |
| **Model 4** | Fast Armed (トリガー加速) | -145.0 bp | 104回 | 23.1% | 0.89 | -34.8 bp |
| **Model 5** | Full Combined (全機能統合) | -72.3 bp | 75回 | 32.0% | 1.19 | +37.9 bp |

- **結論**:
  - **Model 3 (EffectiveReach)** が単独で **+54.4 bp の最大改善** を記録。
  - **Model 1 (DynamicRatio)** が **Payoff 比を 1.11 ➔ 1.23 に引き上げる安定性** を実証。
  - したがって、この両者を融合した **`PEG_v2 = Model 3 + Model 1`** を最適執行エンジンとして確定。

---

### 2. PEG_v2 (Model 3+1) の数理モデルと指値計算式
```python
# 1. キャンセル・リフィルを織り込んだ実効板厚 (Effective Depth)
eff_depth = opp_depth * (1.0 - cancel_rate + 0.5 * refill_rate)

# 2. Model 1 (Dynamic Ratio): 対向板厚連動 (0.915 〜 0.975)
# 対向板が薄い(0.05BTC未満)なら0.975まで深く差し込み、厚い壁(0.5BTC超)なら0.915で手前に置く
depth_factor = min(max(eff_depth / 0.20, 0.0), 1.0)
base_ratio = 0.975 - depth_factor * 0.060

# 3. Model 3 (Effective Reach): テイカー攻撃性による到達距離ブースト (+0.00 〜 +0.02)
aggr_boost = min(max(taker_aggressiveness * 0.020, 0.0), 0.020)

# 4. 合成比率の決定 (安全クリッピング 0.910 〜 0.985)
final_ratio = min(max(base_ratio + aggr_boost, 0.910), 0.985)

# 5. 整数ティック指値算出
if side == "buy":
    peg_px = round(best_bid + spread * final_ratio)
else:
    peg_px = round(best_ask - spread * final_ratio)
```

---

### 3. TF2BP 厳格エグジット規律 ＆ 建値防衛 (BE5)
1. **建値防衛 (BE5: Break-Even Stop)**:
   - 含み益（MFE）が **+5.0 bp** に到達した時点で防衛アームを起動。
   - その後、相場が反落して利益が **+0.2 bp** 以下に低下した場合、即座に微小利確撤退（`be_stop`）を執行し、勝勢からの負け転落を物理的に遮断。
2. **利益目標利確**: **+15.0 bp** 到達で即時指値利確。
3. **トレーリングストップ**: ピーク価格からのドローダウンが **-4.0 bp** で追従ストップ。
4. **逆選択先回り退避**: Adverse Score $\ge$ 0.70 またはキャンセル勧告で即座にエグジット。

---

### 4. 完全並行 A/B テスト体制のアーキテクチャ
- ランナー: [`antigravity/quant_pipeline/run_dryrun_umm_tf2bp_24h.py`](file:///home/azureuser/antigravity/antigravity/quant_pipeline/run_dryrun_umm_tf2bp_24h.py) (PID `1345368`)
- 同一のミリ秒板スナップショットループ内で、以下の3戦略を同一タイミングで評価：
  1. **UMM (CSR-504)**: 在庫スキュー・マーケットメイク (Baseline: FROZEN)
  2. **TF2BP (CSR-499)**: 2bp トレンドフォロー (Baseline: FROZEN)
  3. **TF2BP_PEG_v2**: Model 3+1 を搭載した **OBSERVATION 観測専用レーン**
- **メリット**: サンプリングズレ皆無、プロセス重複による余計なメモリ消費ゼロ、同一気配下での純粋な指値執行性能の直接比較が可能。

---

### 5. 1時間毎 統合定期レポート配信 (`hourly_dryrun_reporter.py`)
- 毎時00分ジャストに Discord の運用報告チャンネルおよび DRYRUN チャンネルへマルチキャスト送信。
- Field ① に **「🔬 TF2BP_PEG_v2 (Model 3+1 観測レーン)」** を配置：
  - **1h**: `+XX.XX bp` (N戦/WR% / ¥損益) [対Base: `+XX.XX bp`]
  - **24h**: `+XX.XX bp` (N戦/WR% / ¥損益) [対Base: `+XX.XX bp`]
  - **Baseline（現行 TF2BP）との比較差分 $\Delta\text{bp}$ を明記**。
- パラメータは **完全手動・自動調整禁止 (FROZEN / OBSERVATION)** を厳守。
