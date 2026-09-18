# AGY 24時間ニュース配信プロジェクト (news_pipeline)

金融・マクロ経済・個別株・PTS・社会ニュースを、定時スケジュール（JST）に沿って自律収集・要約し、Discord Webhookへ青色テーマのEmbed形式で自動配信する完全無人パイプラインです。

---

## 1. ディレクトリ構成

```
news_pipeline/
├── __init__.py
├── scraper.py       # 9つのタイムスケジュールに対応したデータ収集ロジック
├── summarizer.py    # LLM (Gemini API) 3行要約・構造化 (ルールベース自動フォールバック)
├── notifier.py      # Discord Webhook送信モジュール (リトライ・レートリミット制御)
├── scheduler.py     # APSchedulerによる24時間JST時限実行コントローラー
├── run_news_daemon.sh # バックグラウンド常駐起動スクリプト
├── README.md        # 本運用ドキュメント
└── logs/            # 実行・エラーログ (5MB×3世代ローテーションでDISK保護)
    └── scheduler.log
```

---

## 2. 配信タイムスケジュール (JST)

| 時刻 (JST) | 配信タイトル | 収集・巡回対象データ | カテゴリ |
| :--- | :--- | :--- | :---: |
| **07:00** | 🌅 海外市場のまとめ | 米株主要指数 (S&P500, Nasdaq)、米10年債利回り、ドル円、原油、ゴールド、主要市況ヘッドライン | マクロ |
| **07:30** | 🌐 海外個別企業ニュース | 注目テック・半導体 (NVDA, AAPL, MSFT, TSLA, TSM) の決算・動向 | マクロ |
| **08:00** | 🇯🇵 日本株個別ニュース | 株探インパクト開示情報 (TDnet等)、国内企業トピックス | 個別株 |
| **08:30** | ⚡ PTSトップ5/ワースト5 & S高S安 | 前夜PTS値上がり/値下がり上位、前営業日ストップ高/安銘柄 | 個別株 |
| **12:00** | 🏛 社会ニュース | 昼時点の国内主要ニュース (NHK・Yahoo!速報)、政治経済 | マクロ |
| **16:00** | 📊 日本株総括 | 日経平均、TOPIX、グロース250大引け結果、東証33業種騰落ランキング | 個別株 |
| **17:00** | 🌙 PTSトップ5/ワースト5 | 夕方時点（夜間取引開始直後）のPTSランキング | 個別株 |
| **19:00** | 🌍 海外市場まとめ | 欧州市場寄り付き (FTSE, DAX, CAC)、アジア市場大引け振り返り | マクロ |
| **21:30** | 🔔 海外市場寄り付き概要 | NY市場寄り付き動向 (ダウ, S&P, Nasdaq)、米経済指標・ヘッドライン | マクロ |

---

## 3. 運用・実行コマンド

### A. バックグラウンド常駐稼働 (推奨)
```bash
# 起動
bash news_pipeline/run_news_daemon.sh

# または nohup で直接起動
nohup /home/azureuser/antigravity/venv/bin/python -u news_pipeline/scheduler.py > news_pipeline/logs/daemon.log 2>&1 &
```

### B. 動作確認・即時テスト実行
指定した時間スロットの収集〜Discord送信を即座にテストできます。
```bash
# 07:00 (海外市場) を即時テスト
/home/azureuser/antigravity/venv/bin/python news_pipeline/scheduler.py --test 07:00

# 08:30 (PTS & ストップ高安) を即時テスト
/home/azureuser/antigravity/venv/bin/python news_pipeline/scheduler.py --test 08:30

# 全スロットを順次テスト
/home/azureuser/antigravity/venv/bin/python news_pipeline/scheduler.py --test all
```

### C. Discord送信なしでのコンソール確認 (Dry-Run)
```bash
/home/azureuser/antigravity/venv/bin/python news_pipeline/scheduler.py --dry-run 16:00
```

---

## 4. 設計・制約への準拠事項
1. **青色テーマ Embed**: Discordの公式Embed仕様（タイムスタンプ付き・青系統カラー）を採用。
2. **耐障害性 (指数バックオフ & リトライ)**: 送信失敗時は最大3回自動リトライ。Discordレートリミット(429)検知時は `retry_after` 秒自動スリープ。
3. **安全なスクレイピング**: 各データ取得は個別例外ハンドリングされ、万一タイムアウトや取得元エラーが生じてもプロセス全体は落ちず「データ取得スキップ」として継続。
4. **DISK・PC負荷の極小化**: ログファイルは `RotatingFileHandler`（最大5MB×3世代）により容量肥大化を完全防止。常時CPU負荷はほぼ0%で、既存の自動売買プロセスに影響を与えません。
5. **ハルシネーションの禁止**: ソースにない数値や事実を創作しないプロンプト・抽出ロジックを徹底。
