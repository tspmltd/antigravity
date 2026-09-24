"""
24時間時限実行コントローラー (scheduler.py)
APSchedulerによるJSTタイムスケジュール管理と無人自律稼働パイプライン
07:00 海外市場のまとめ
07:30 海外個別企業ニュース
08:00 日本株個別ニュース
08:30 PTSトップ5/ワースト5 & S高S安
12:00 社会ニュース
16:00 日本株総括
17:00 PTSトップ5/ワースト5
19:00 海外市場まとめ
21:30 海外市場寄り付き概要
"""

import os
import sys

# プロジェクトルートをsys.pathに追加
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# 自作モジュールのインポート
from news_pipeline.scraper import SLOT_SCRAPERS
from news_pipeline.summarizer import Summarizer
from news_pipeline.notifier import send_news_embed, THEME_COLOR_BLUE, THEME_COLOR_NAVY, THEME_COLOR_CYAN
from news_pipeline.x_notifier import (
    default_x_notifier,
    is_x_backbone_slot,
)


# JSTタイムゾーン
JST = pytz.timezone("Asia/Tokyo")

# ログ設定 (ディスク圧迫防止のため RotatingFileHandler: 5MB x 3世代)
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "scheduler.log")

logger = logging.getLogger("news_pipeline")
logger.setLevel(logging.INFO)

# コンソールハンドラー
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch_formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
ch.setFormatter(ch_formatter)
logger.addHandler(ch)

# ファイルハンドラー (最大5MB、3ファイルまでローテーション)
fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
fh.setLevel(logging.INFO)
fh.setFormatter(ch_formatter)
logger.addHandler(fh)


def format_slot_content(slot: str, raw_data: dict) -> dict:
    """
    収集したraw_dataを、Discord Embed用のタイトル・説明・フィールド群に成形・要約する。
    ハルシネーションを防止し、各項目を3行以内に構造化。
    """
    title = raw_data.get("title", f"市場ニュース速報 ({slot} JST)")
    description = ""
    fields = []
    color = THEME_COLOR_BLUE

    if slot == "07:00":
        color = THEME_COLOR_BLUE
        # 指標
        indicators = raw_data.get("indicators", {})
        ind_text = Summarizer.structure_market_indicators(indicators)
        fields.append({"name": "📈 主要市場指標 (オーバーナイト終値)", "value": ind_text, "inline": False})

        # ニュース要約
        news_items = raw_data.get("news", [])
        if news_items:
            news_text = "\n".join([f"• {item['title']}" for item in news_items])
            summary = Summarizer.summarize_text(news_text, max_lines=3, topic_hint="米株・市況ヘッドライン")
            fields.append({"name": "📰 市況ヘッドライン要約", "value": summary, "inline": False})

    elif slot == "07:30":
        color = THEME_COLOR_CYAN
        # 指標
        indicators = raw_data.get("indicators", {})
        ind_text = Summarizer.structure_market_indicators(indicators)
        fields.append({"name": "💻 注目テック・半導体株価", "value": ind_text, "inline": False})

        # 個別ニュース要約
        news_items = raw_data.get("news", [])
        if news_items:
            news_text = "\n".join([f"• {item['title']}" for item in news_items])
            summary = Summarizer.summarize_text(news_text, max_lines=3, topic_hint="海外テック・半導体企業動向")
            fields.append({"name": "🚀 注目企業・決算ヘッドライン", "value": summary, "inline": False})

    elif slot == "08:00":
        color = THEME_COLOR_BLUE
        # 適時開示
        disclosures = raw_data.get("disclosures", [])
        if disclosures:
            disc_lines = []
            for d in disclosures:
                disc_lines.append(f"▫️ **{d['name']} ({d['code']})**: {d['disclosure']} ({d['change_pct']})")
            fields.append({"name": "📢 注目適時開示・材料銘柄", "value": "\n".join(disc_lines), "inline": False})
        else:
            fields.append({"name": "📢 注目適時開示", "value": "情報なし (開示待ち)", "inline": False})

        # 国内ニュース要約
        news_items = raw_data.get("news", [])
        if news_items:
            news_text = "\n".join([f"• {item['title']}" for item in news_items])
            summary = Summarizer.summarize_text(news_text, max_lines=3, topic_hint="日本企業・経済ニュース")
            fields.append({"name": "🗞 国内企業トピックス", "value": summary, "inline": False})

    elif slot == "08:30":
        color = THEME_COLOR_NAVY
        pts_top = raw_data.get("pts_top", [])
        pts_worst = raw_data.get("pts_worst", [])
        stop_high = raw_data.get("stop_high", [])
        stop_low = raw_data.get("stop_low", [])

        fields.append({"name": "🟢 PTS上昇率 トップ5", "value": "\n".join(pts_top[:5]), "inline": True})
        fields.append({"name": "🔴 PTS下落率 ワースト5", "value": "\n".join(pts_worst[:5]), "inline": True})
        fields.append({"name": "🚀 前営業日 ストップ高", "value": "\n".join(stop_high[:5]), "inline": False})
        fields.append({"name": "⚠️ 前営業日 ストップ安", "value": "\n".join(stop_low[:5]), "inline": False})

    elif slot == "12:00":
        color = THEME_COLOR_BLUE
        nhk_news = raw_data.get("nhk_news", [])
        yahoo_topics = raw_data.get("yahoo_topics", [])

        if nhk_news:
            nhk_text = "\n".join([f"• {n['title']}" for n in nhk_news])
            summary_nhk = Summarizer.summarize_text(nhk_text, max_lines=3, topic_hint="社会・総合速報")
            fields.append({"name": "🇯🇵 NHK ニュース速報まとめ", "value": summary_nhk, "inline": False})

        if yahoo_topics:
            yahoo_text = "\n".join([f"• {y['title']}" for y in yahoo_topics])
            summary_yahoo = Summarizer.summarize_text(yahoo_text, max_lines=3, topic_hint="主要トピックス")
            fields.append({"name": "📌 昼の主要トピックス", "value": summary_yahoo, "inline": False})

    elif slot == "16:00":
        color = THEME_COLOR_BLUE
        # 大引け指数
        indicators = raw_data.get("indicators", {})
        ind_text = Summarizer.structure_market_indicators(indicators)
        fields.append({"name": "📊 主要指数 大引け結果", "value": ind_text, "inline": False})

        # セクター動向
        top_sec = raw_data.get("top_sectors", [])
        worst_sec = raw_data.get("worst_sectors", [])
        sec_text = "**【上昇上位】**\n" + "\n".join(top_sec) + "\n\n**【下落上位】**\n" + "\n".join(worst_sec)
        fields.append({"name": "🏭 東証33業種 セクター動向", "value": sec_text, "inline": False})

        # 大引け市況ヘッドライン
        news_items = raw_data.get("news", [])
        if news_items:
            news_text = "\n".join([f"• {item['title']}" for item in news_items])
            summary = Summarizer.summarize_text(news_text, max_lines=3, topic_hint="大引け市況")
            fields.append({"name": "📰 大引け総括ヘッドライン", "value": summary, "inline": False})

    elif slot == "17:00":
        color = THEME_COLOR_NAVY
        pts_top = raw_data.get("pts_top", [])
        pts_worst = raw_data.get("pts_worst", [])
        fields.append({"name": "🟢 夜間PTS 上昇率トップ5", "value": "\n".join(pts_top[:5]), "inline": True})
        fields.append({"name": "🔴 夜間PTS 下落率ワースト5", "value": "\n".join(pts_worst[:5]), "inline": True})

    elif slot == "19:00":
        color = THEME_COLOR_BLUE
        indicators = raw_data.get("indicators", {})
        ind_text = Summarizer.structure_market_indicators(indicators)
        fields.append({"name": "🌍 欧州寄り付き & アジア引け値", "value": ind_text, "inline": False})

        news_items = raw_data.get("news", [])
        if news_items:
            news_text = "\n".join([f"• {item['title']}" for item in news_items])
            summary = Summarizer.summarize_text(news_text, max_lines=3, topic_hint="欧州・国際市場")
            fields.append({"name": "📰 欧州寄り付き・海外市場ヘッドライン", "value": summary, "inline": False})

    elif slot == "21:30":
        color = THEME_COLOR_CYAN
        indicators = raw_data.get("indicators", {})
        ind_text = Summarizer.structure_market_indicators(indicators)
        fields.append({"name": "🔔 NY市場寄り付き動向", "value": ind_text, "inline": False})

        news_items = raw_data.get("news", [])
        if news_items:
            news_text = "\n".join([f"• {item['title']}" for item in news_items])
            summary = Summarizer.summarize_text(news_text, max_lines=3, topic_hint="NY寄り付き・指標速報")
            fields.append({"name": "📋 今夜の米経済指標・ヘッドライン", "value": summary, "inline": False})

    return {
        "title": title,
        "description": description,
        "fields": fields,
        "color": color
    }


def execute_slot(slot: str, dry_run: bool = False) -> bool:
    """
    指定スロットのデータ収集・要約・Discord送信を実行する安全ラッパー関数。
    トップレベル例外キャッチにより、いかなるエラーでもプロセス停止を防止。
    """
    logger.info(f"[Scheduler] >>> スロット実行開始: {slot} (dry_run={dry_run})")
    try:
        scraper_func = SLOT_SCRAPERS.get(slot)
        if not scraper_func:
            logger.error(f"[Scheduler] 未知のスロット: {slot}")
            return False

        # 1. スクレイピング & データ収集
        raw_data = scraper_func()

        # 2. 要約 & Discord Embed成形
        embed_data = format_slot_content(slot, raw_data)

        if dry_run:
            logger.info(f"[DryRun] Title: {embed_data['title']}")
            for f in embed_data["fields"]:
                logger.info(f"[DryRun] Field [{f['name']}]:\n{f['value']}")
            x_text = compose_slot_x_text(slot, embed_data)
            if x_text:
                logger.info(f"[DryRun] 🐦 X Tweet Preview ({len(x_text)}文字):\n{x_text}")
            else:
                logger.info(f"[DryRun] 🐦 スロット {slot} は空本文のため X 欠送")
            return True

        # 3. Discord送信
        # カテゴリ振り分け (個別株系 08:00, 08:30, 16:00, 17:00 は report、他は macro)
        category = "stock" if slot in ("08:00", "08:30", "16:00", "17:00") else "macro"
        success = send_news_embed(
            title=embed_data["title"],
            description=embed_data["description"],
            fields=embed_data["fields"],
            color=embed_data["color"],
            category=category
        )

        # 4. 定時 10 枠はすべて X へ。07:00 は海外終値、08:00 だけ朝サマリー。空本文は欠送。
        #    急変 overlay は定時の穴埋めに使わない。
        try:
            x_text = compose_slot_x_text(slot, embed_data)
            if not x_text:
                logger.info(f"[Scheduler] 🐦 スロット {slot} は本文なしのため X 欠送")
            else:
                tweet_id = default_x_notifier.post_tweet(text=x_text)
                if tweet_id:
                    logger.info(f"[Scheduler] 🐦 X投稿完了 slot={slot} (Tweet ID: {tweet_id})")
        except Exception as ex:
            logger.warning(f"[Scheduler] X投稿スキップ/エラー slot={slot}: {ex}")

        if success:
            logger.info(f"[Scheduler] <<< スロット実行完了 (成功): {slot}")
        else:
            logger.error(f"[Scheduler] <<< スロット実行完了 (送信失敗): {slot}")
        return success


    except Exception as e:
        logger.exception(f"[Scheduler] [CRITICAL] スロット {slot} の実行中に予期せぬ例外が発生しました (自動復帰): {e}")
        return False


def compose_slot_x_text(slot: str, embed_data: dict) -> str:
    """背骨スロットの X 本文。スロットごとに身元が違い、空なら欠送。"""
    if not is_x_backbone_slot(slot):
        return ""
    if slot == "08:00":
        from news_pipeline.morning_summary import MorningSummaryGenerator
        return MorningSummaryGenerator().build_summary(compact_for_x=True)
    return default_x_notifier.format_news_for_x(
        slot,
        embed_data.get("title", ""),
        embed_data.get("fields") or [],
    )


def check_sekai_kabuka_movers():
    """毎時実行: 世界の株価 (sekai-kabuka.com) で1%以上変動した市場を検知しグラフ付き通知"""
    try:
        from news_pipeline.sekai_kabuka_monitor import SekaiKabukaMonitor
        logger.info("[Scheduler] 🔍 世界の株価 1%急変チェックを実行中...")
        monitor = SekaiKabukaMonitor()
        monitor.check_and_notify(threshold_pct=1.0)
    except Exception as ex:
        logger.warning(f"[Scheduler] 世界の株価急変監視エラー: {ex}")


def start_scheduler():
    """APSchedulerを起動し、24時間常駐稼働を開始する"""
    scheduler = BlockingScheduler(timezone=JST)

    # タイムスケジュール登録 (JST)
    schedule_jobs = [
        ("07:00", 7, 0),
        ("07:30", 7, 30),
        ("08:00", 8, 0),
        ("08:30", 8, 30),
        ("12:00", 12, 0),
        ("16:00", 16, 0),
        ("17:00", 17, 0),
        ("19:00", 19, 0),
        ("21:30", 21, 30),
    ]

    logger.info("==================================================")
    logger.info("  AGY 24時間ニュース配信スケジューラー 起動")
    logger.info("==================================================")
    logger.info(f"タイムゾーン: {JST}")

    for slot_name, hour, minute in schedule_jobs:
        scheduler.add_job(
            execute_slot,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=JST),
            args=[slot_name, False],
            id=f"job_{slot_name.replace(':', '')}",
            name=f"NewsDelivery_{slot_name}",
            misfire_grace_time=300  # 5分遅延まで許容
        )
        logger.info(f"  - 登録スケジュール: {slot_name} JST (Job ID: job_{slot_name.replace(':', '')})")

    # 毎時00分: 世界の株価 1%以上急変検知・グラフ通知ジョブ
    scheduler.add_job(
        check_sekai_kabuka_movers,
        trigger=CronTrigger(minute=0, timezone=JST),
        id="job_sekai_kabuka_hourly",
        name="SekaiKabuka_HourlyCheck",
        misfire_grace_time=300
    )
    logger.info("  - 登録スケジュール: 毎時00分 JST (世界の株価 1%急変グラフ通知)")

    # 毎日 17:30 JST: 本日の重要開示 TOP5 自動集計・サムネイル画像付きX/Discord投稿
    def run_daily_top5_job():
        try:
            from news_pipeline.daily_top5_reporter import DailyTop5Reporter
            DailyTop5Reporter().run_report(mock_if_empty=False)
        except Exception as ex:
            logger.warning(f"[Scheduler] DailyTop5 実行例外: {ex}")

    scheduler.add_job(
        run_daily_top5_job,
        trigger=CronTrigger(hour=17, minute=30, timezone=JST),
        id="job_daily_top5_1730",
        name="DailyTop5_DisclosureReport",
        misfire_grace_time=300
    )
    logger.info("  - 登録スケジュール: 毎日 17:30 JST (重要開示 TOP5 サムネイル画像付きX/Discord配信)")


    logger.info("常駐待機を開始します (Ctrl+Cで停止)...")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit) as sig:
        logger.info(f"[Scheduler] 停止シグナルを受信しました: {type(sig).__name__}。正常終了します。")
    except Exception as e:
        logger.exception(f"[Scheduler] スケジューラー例外: {e}")
    finally:
        logger.info("[Scheduler] scheduler.start() を抜けました。")


def daemonize():
    """UNIX標準のダブルフォークで端末から完全デタッチされた常駐デーモンを生成"""
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"fork #1 failed: {e}\n")
        sys.exit(1)

    os.setsid()
    os.umask(0)

    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"fork #2 failed: {e}\n")
        sys.exit(1)

    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "r") as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())
    with open(LOG_FILE, "a") as logf:
        os.dup2(logf.fileno(), sys.stdout.fileno())
        os.dup2(logf.fileno(), sys.stderr.fileno())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AGY 24h News Pipeline Scheduler")
    parser.add_argument("--test", type=str, help="指定スロットの即時テスト実行 (例: 07:00, 08:30, all)")
    parser.add_argument("--dry-run", type=str, help="Discord送信なしのコンソール確認 (例: 07:00, all)")
    parser.add_argument("--daemon", action="store_true", help="バックグラウンドデーモンとして起動")

    args = parser.parse_args()

    if args.test:
        target = args.test.strip()
        if target == "all":
            for s in SLOT_SCRAPERS.keys():
                execute_slot(s, dry_run=False)
                time.sleep(2)
        else:
            execute_slot(target, dry_run=False)
    elif args.dry_run:
        target = args.dry_run.strip()
        if target == "all":
            for s in SLOT_SCRAPERS.keys():
                execute_slot(s, dry_run=True)
        else:
            execute_slot(target, dry_run=True)
    else:
        if args.daemon:
            daemonize()
        start_scheduler()
