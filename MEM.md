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
