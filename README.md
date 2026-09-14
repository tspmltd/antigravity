# 自動売買システム 自律改善パイプライン (GapcorePJ)

本リポジトリは、4つの特化型エージェント（提案・検証・改善・判断）が並行・協調稼働し、アルゴリズム取引戦略の「仮説立案 → バックテスト検証 → ボトルネック改善 → ガバナンス判定（選別）」を自律的に繰り返すクオンツ研究パイプラインです。

---

## 1. システム構成図

```
[1. 提案 (Strategy Proposer)]
             │  戦略コード生成
             ▼
[2. 検証 (Backtest Runner)] ◄──────┐
   │ (エラー時: 自己修復ループ)           │
   │ バックテスト結果・メトリクス抽出           │
   ▼                               │ 改善コード
[4. 判断 (Governance)]             │
   │                               │
   ├─► 合格 (Pass)  ──► [strategies/approved/] & レポート出力
   ├─► 改善 (Revise) ──► [3. 改善 (Optimizer)] ──┘
   └─► 却下 (Reject) ──► [strategies/rejected/] (アーカイブ)
```

---

## 2. ディレクトリ構成

```
GapcorePJ/
├── .agents/                      # Antigravity Subagent定義
│   ├── strategy-proposer.md      # 提案エージェント仕様
│   ├── backtest-runner.md        # 検証エージェント仕様
│   ├── strategy-optimizer.md     # 改善エージェント仕様
│   └── strategy-governance.md    # 判断エージェント仕様
│
├── configs/                      # パイプライン・ガバナンス設定
│   ├── pipeline_config.yaml      # 並行数、最大改善回数、手数料・スリッページ
│   └── governance_rules.yaml     # 採択基準 (Sharpe > 1.8, MDD < 5%等)
│
├── core/                         # コア基盤
│   ├── base_strategy.py          # 戦略基底クラス (BaseStrategy)
│   ├── engine.py                 # バックテスト実行エンジン
│   ├── metrics.py                # 評価指標 (Sharpe, MDD, WinRate, PF, etc.)
│   └── dataloader.py             # 価格データローダー & 合成市場データ生成
│
├── agents/                       # 4エージェントのPython実装モジュール
│   ├── base_agent.py             # LLM/モック連携基底クラス
│   ├── strategy_proposer.py      # 【1. 提案】仮説立案 & コード生成
│   ├── backtest_runner.py        # 【2. 検証】テスト実行 & エラー自己修復
│   ├── optimizer.py              # 【3. 改善】ボトルネック分析 & フィルタ追加
│   └── governance.py             # 【4. 判断】採択・却下判定 & レポート生成
│
├── pipeline/                     # パイプライン制御層
│   ├── state_manager.py          # 戦略ライフサイクル状態追跡
│   └── orchestrator.py           # 非同期並行オーケストレーター
│
├── strategies/                   # 戦略コードの配置場所
│   ├── proposed/                 # 生成された初期戦略
│   ├── optimizing/               # 改善イテレーション中の戦略
│   ├── approved/                 # ガバナンス合格戦略 (本番候補)
│   └── rejected/                 # 基準未達アーカイブ
│
├── reports/                      # 合格戦略のMarkdownレポート
├── requirements.txt              # 必要パッケージ
├── main.py                       # パイプライン起動スクリプト
└── README.md
```

---

## 3. クイックスタート

### 依存パッケージのインストール
```powershell
pip install -r requirements.txt
```

### パイプラインの実行
```powershell
# 1. 取引所から実相場データを直接取得して実行 (推奨・キャッシュ自動保存)
python main.py --source binance --symbol BTCUSDT --timeframe 1h

# 2. bitFlyer (BTC/JPY) の直近約定データを1分足に変換して実行
python main.py --source bitflyer --symbol BTC_JPY --timeframe 1m

# 3. 手元の実相場CSVファイルを指定して実行
python main.py --data data/your_crypto_1h.csv

# 4. オフライン検証 (合成市場データを自動生成)
python main.py
```

### リアルタイム自動売買・ペーパートレードの実行 (`run_live.py`)
```powershell
# 仮想資金でリアルタイム監視・ペーパートレードを開始 (安全・推奨)
python run_live.py --interval 5.0 --size 0.001

# 特定の戦略ファイルを指定して実行
python run_live.py --strategy strategies/approved/test_golden_001_approved.py

# 【注意】実資金での本番注文 (二重確認プロンプトあり)
python run_live.py --real --size 0.001
```

---

## 4. ガバナンス基準の設定 (`configs/governance_rules.yaml`)

```yaml
governance_criteria:
  min_sharpe_ratio: 1.8      # 最低シャープレシオ
  max_drawdown_pct: 5.0      # 最大許容ドローダウン (%)
  min_total_trades: 30       # 最低取引回数
  min_profit_factor: 1.5     # 最低プロフィットファクター

overfitting_guard:
  enable_oos_validation: true # In-Sample/Out-of-Sample過剰適合検証
  max_oos_degradation_pct: 25.0
```
