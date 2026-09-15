#!/usr/bin/env bash
# Antigravity 全プロセス統合管理スクリプト
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

VENV_PYTHON="$DIR/venv/bin/python3"
if [ ! -f "$VENV_PYTHON" ]; then
    VENV_PYTHON="python3"
fi

status() {
    echo "=========================================================="
    echo "  🚀 Antigravity Autonomous Fleet 稼働状況"
    echo "=========================================================="
    
    echo -n "1. 取引エンジン (-m antigravity)        : "
    PID_ENG=$(pgrep -f "antigravity.runtime.runner|-m antigravity" | grep -v "$$" | head -n 1 || true)
    if [ -n "$PID_ENG" ]; then
        echo "🟢 RUNNING (PID: $PID_ENG)"
    else
        echo "🔴 STOPPED"
    fi

    echo -n "2. 自律改善・発見 (run_autonomous_daemon) : "
    PID_DISC=$(pgrep -f "run_autonomous_daemon.py" | grep -v "$$" | head -n 1 || true)
    if [ -n "$PID_DISC" ]; then
        echo "🟢 RUNNING (PID: $PID_DISC)"
    else
        echo "🔴 STOPPED"
    fi

    echo -n "3. ニュースパイプライン (news_pipeline) : "
    PID_NEWS=$(pgrep -f "news_pipeline/scheduler.py" | grep -v "$$" | head -n 1 || true)
    if [ -n "$PID_NEWS" ]; then
        echo "🟢 RUNNING (PID: $PID_NEWS)"
    else
        echo "🔴 STOPPED"
    fi

    echo -n "4. 死活監視・緊急警報 (watchdog sentinel) : "
    if systemctl --user is-active --quiet antigravity-watchdog.service 2>/dev/null; then
        WD_PID=$(systemctl --user show -p MainPID --value antigravity-watchdog.service)
        echo "🟢 RUNNING (systemd PID: $WD_PID)"
    else
        PID_WD=$(pgrep -f "antigravity.risk_guard.watchdog" | grep -v "$$" | head -n 1 || true)
        if [ -n "$PID_WD" ]; then
            echo "🟢 RUNNING (PID: $PID_WD)"
        else
            echo "🔴 STOPPED"
        fi
    fi
    echo "=========================================================="
}

start() {
    echo "[Fleet] 全サービス起動シーケンスを開始します..."
    
    # 1. 取引エンジン
    PID_ENG=$(pgrep -f "antigravity.runtime.runner|-m antigravity" | grep -v "$$" | head -n 1 || true)
    if [ -z "$PID_ENG" ]; then
        echo "[Fleet] 取引エンジンを起動中..."
        nohup "$VENV_PYTHON" -u -m antigravity --symbol FX_BTC_JPY >> "$DIR/antigravity.log" 2>&1 &
        sleep 2
    else
        echo "[Fleet] 取引エンジンは既に稼働中です (PID: $PID_ENG)"
    fi

    # 2. 自律改善デーモン
    PID_DISC=$(pgrep -f "run_autonomous_daemon.py" | grep -v "$$" | head -n 1 || true)
    if [ -z "$PID_DISC" ]; then
        echo "[Fleet] 自律発見デーモンを起動中..."
        nohup "$VENV_PYTHON" -u run_autonomous_daemon.py --symbol FX_BTC_JPY --timeframe 1m --interval 3600.0 >> "$DIR/daemon.log" 2>&1 &
        sleep 2
    else
        echo "[Fleet] 自律発見デーモンは既に稼働中です (PID: $PID_DISC)"
    fi

    # 3. ニュースパイプライン
    PID_NEWS=$(pgrep -f "news_pipeline/scheduler.py" | grep -v "$$" | head -n 1 || true)
    if [ -z "$PID_NEWS" ]; then
        echo "[Fleet] ニューススケジューラを起動中..."
        nohup "$VENV_PYTHON" -u "$DIR/news_pipeline/scheduler.py" --daemon >> "$DIR/news_pipeline/scheduler.log" 2>&1 &
        sleep 2
    else
        echo "[Fleet] ニューススケジューラは既に稼働中です (PID: $PID_NEWS)"
    fi

    # 4. Watchdog Sentinel
    "$DIR/scripts/run_watchdog.sh" start

    status
}

stop() {
    echo "[Fleet] 全サービス停止中..."
    "$DIR/scripts/run_watchdog.sh" stop || true
    pkill -f "antigravity.runtime.runner|-m antigravity" || true
    pkill -f "run_autonomous_daemon.py" || true
    pkill -f "news_pipeline/scheduler.py" || true
    sleep 2
    echo "[Fleet] 全サービス停止完了"
    status
}

restart() {
    stop
    sleep 2
    start
}

case "$1" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
