#!/usr/bin/env bash
# Antigravity Watchdog Sentinel ランチャースクリプト (systemd連携)
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

SERVICE_NAME="antigravity-watchdog.service"

case "$1" in
    start)
        echo "[Watchdog] systemd ユーザーサービスを開始します..."
        systemctl --user daemon-reload
        systemctl --user start "$SERVICE_NAME"
        systemctl --user status "$SERVICE_NAME" --no-pager
        ;;
    stop)
        echo "[Watchdog] 停止中..."
        systemctl --user stop "$SERVICE_NAME" || true
        pkill -f "antigravity.risk_guard.watchdog" || true
        echo "[Watchdog] 停止完了"
        ;;
    status)
        systemctl --user status "$SERVICE_NAME" --no-pager || true
        ;;
    restart)
        echo "[Watchdog] 再起動中..."
        systemctl --user daemon-reload
        systemctl --user restart "$SERVICE_NAME"
        systemctl --user status "$SERVICE_NAME" --no-pager
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
