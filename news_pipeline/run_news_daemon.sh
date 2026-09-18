#!/usr/bin/env bash
# AGY 24h ニュース配信スケジューラー 起動・管理スクリプト

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$DIR")"
VENV_PYTHON="$PARENT_DIR/venv/bin/python3"
SCHEDULER_PY="$DIR/scheduler.py"
LOG_FILE="$DIR/logs/daemon.log"

SEKAI_PY="$DIR/sekai_kabuka_realtime_sentinel.py"
EDINET_PY="$DIR/edinet_sentinel.py"

case "$1" in
    start)
        echo "[INFO] ニュース配信デーモン群を起動します..."
        if ! pgrep -f "news_pipeline/scheduler.py" > /dev/null; then
            nohup "$VENV_PYTHON" -u "$SCHEDULER_PY" --daemon > "$DIR/logs/scheduler_nohup.log" 2>&1 &
            echo "  ✓ scheduler.py 起動完了"
        else
            echo "  • scheduler.py は既に稼働中"
        fi

        if ! pgrep -f "news_pipeline/sekai_kabuka_realtime_sentinel.py" > /dev/null; then
            nohup "$VENV_PYTHON" -u "$SEKAI_PY" --interval 60.0 > "$DIR/logs/sekai_sentinel_nohup.log" 2>&1 &
            echo "  ✓ sekai_kabuka_realtime_sentinel.py 起動完了"
        else
            echo "  • sekai_kabuka_realtime_sentinel.py は既に稼働中"
        fi

        if ! pgrep -f "news_pipeline/edinet_sentinel.py" > /dev/null; then
            nohup "$VENV_PYTHON" -u "$EDINET_PY" > "$DIR/logs/edinet_nohup.log" 2>&1 &
            echo "  ✓ edinet_sentinel.py 起動完了"
        else
            echo "  • edinet_sentinel.py は既に稼働中"
        fi
        ;;
    stop)
        echo "[INFO] ニュース配信デーモン群を停止します..."
        pkill -f "news_pipeline/(scheduler|sekai_kabuka_realtime_sentinel|edinet_sentinel)"
        sleep 1
        echo "[SUCCESS] 全デーモン停止完了。"
        ;;
    status)
        echo "=== ニュース配信デーモン群 稼働状況 ==="
        ps aux | grep -E "news_pipeline/(scheduler|sekai_kabuka_realtime_sentinel|edinet_sentinel)" | grep -v grep
        ;;
    restart)
        $0 stop
        sleep 2
        $0 start
        ;;
    *)
        echo "使用方法: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
