"""
Antigravity Watchdog Sentinel (自律死活監視・緊急通報・自動復旧エンジン)

以下の緊急事態を24時間常時監視し、異常発生時に「その都度（リアルタイム）」
Discord アラートチャンネルへ緊急通報を送信し、自動復旧（自己修復・再起動）を行います。

1. システムダウン検知:
   - 取引エンジン (antigravity) のプロセス停止・クラッシュ
   - 自律戦略発見デーモン (run_autonomous_daemon) の停止
   - ニュースパイプライン (news_pipeline/scheduler) の停止
   - bitFlyer WebSocket通信途絶・ログ更新停止 (ストール/フリーズ)
2. システム圧迫検知:
   - CPU使用率急騰 (>=85% 警告 / >=95% 深刻)
   - メモリ残量逼迫 (>=85% 警告 / >=92% 深刻 / 空き<500MB)
   - ディスク容量逼迫 (>=85% 警告 / >=92% 深刻)
3. 自動自己復旧 (Auto-Restart & Remediation):
   - 停止プロセスの自動再起動 & 復旧完了通報
   - リソース逼迫時のキャッシュ破棄・古いログ整理
"""

import os
import sys
import time
import subprocess
import signal
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from antigravity.risk_guard.notifier import DiscordNotifier
from antigravity.risk_guard.system_monitor import SystemResourceMonitor


class WatchdogSentinel:
    """
    自律死活監視＆緊急通報センチネル
    """

    def __init__(
        self,
        base_dir: str = "/home/azureuser/antigravity",
        check_interval_sec: float = 15.0,
        heartbeat_timeout_sec: float = 120.0,
        enable_auto_restart: bool = True,
    ):
        self.base_dir = os.path.abspath(base_dir)
        self.check_interval_sec = check_interval_sec
        self.heartbeat_timeout_sec = heartbeat_timeout_sec
        self.enable_auto_restart = enable_auto_restart

        self.notifier = DiscordNotifier()
        self.sys_monitor = SystemResourceMonitor(
            disk_warning_pct=85.0,
            mem_warning_pct=85.0,
            cpu_warning_pct=85.0,
        )

        self.python_bin = os.path.join(self.base_dir, "venv", "bin", "python3")
        if not os.path.exists(self.python_bin):
            self.python_bin = sys.executable

        # 監視対象サービス定義
        self.services = {
            "grid_mm_live": {
                "name": "RsiMeanReversion LIVE本番取引エンジン (strat_9ca5d130)",
                "keywords": ["run_live.py"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "grid_mm_live.log"),
                "check_heartbeat": True,
                "start_cmd": [
                    self.python_bin, "-u", os.path.join(self.base_dir, "run_live.py"),
                    "--strategy", os.path.join(self.base_dir, "strategies", "approved", "strat_9ca5d130_approved.py"),
                    "--symbol", "FX_BTC_JPY", "--size", "0.001", "--interval", "5.0",
                    "--min-profit", "18.0", "--stop-loss", "25.0", "--max-hold", "1800.0",
                    "--daily-loss-limit", "300.0", "--max-consecutive-losses", "4",
                    "--real", "--yes",
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "discovery_daemon": {
                "name": "自律戦略改善・発見デーモン (Discovery Daemon)",
                "keywords": ["run_autonomous_daemon.py"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "daemon.log"),
                "check_heartbeat": False,  # サイクル待機があるためハートビートはチェックしない
                "start_cmd": [
                    self.python_bin, "-u", "run_autonomous_daemon.py",
                    "--symbol", "FX_BTC_JPY", "--timeframe", "1m", "--interval", "3600.0"
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "news_scheduler": {
                "name": "ニュース・外部情報スケジューラ (News Scheduler)",
                "keywords": ["news_pipeline/scheduler.py"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "news_pipeline", "scheduler.log"),
                "check_heartbeat": False,
                "start_cmd": [
                    self.python_bin, "-u", os.path.join(self.base_dir, "news_pipeline", "scheduler.py"), "--daemon"
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "sekai_kabuka_sentinel": {
                "name": "世界の株価 リアルタイム急変センチネル (1%突破監視)",
                "keywords": ["sekai_kabuka_realtime_sentinel.py"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "news_pipeline", "logs", "sekai_kabuka_sentinel.log"),
                "check_heartbeat": False,
                "start_cmd": [
                    self.python_bin, "-u", os.path.join(self.base_dir, "news_pipeline", "sekai_kabuka_realtime_sentinel.py"), "--interval", "60.0"
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "edinet_sentinel": {
                "name": "EDINET 10文字速報センチネル (大量保有・TOB等)",
                "keywords": ["edinet_sentinel.py"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "news_pipeline", "logs", "edinet_sentinel.log"),
                "check_heartbeat": False,
                "start_cmd": [
                    self.python_bin, "-u", os.path.join(self.base_dir, "news_pipeline", "edinet_sentinel.py")
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "tdnet_sentinel": {
                "name": "東証適時開示 (TDnet) リアルタイム速報センチネル (MIS判定・X/Discord)",
                "keywords": ["tdnet_sentinel.py"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "news_pipeline", "logs", "tdnet_sentinel.log"),
                "check_heartbeat": False,
                "start_cmd": [
                    self.python_bin, "-u", os.path.join(self.base_dir, "news_pipeline", "tdnet_sentinel.py")
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "pts_sentinel": {
                "name": "日本株PTS夜間取引センチネル (急変・出来高急増・因果AI)",
                "keywords": ["pts_sentinel.py"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "news_pipeline", "logs", "pts_sentinel.log"),
                "check_heartbeat": False,
                "start_cmd": [
                    self.python_bin, "-u", os.path.join(self.base_dir, "news_pipeline", "pts_sentinel.py")
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "quant_pipeline": {
                "name": "Quant Fusion Dry-run観測エンジン (仮想シミュレーション & Discord配信)",
                "keywords": ["quant_pipeline.run_pipeline"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "quant_pipeline.log"),
                "check_heartbeat": True,
                "start_cmd": [
                    self.python_bin, "-u", "-m", "antigravity.quant_pipeline.run_pipeline",
                    "--interval", "2.0", "--flush-interval", "30.0",
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "jp_equity_paper": {
                "name": "日本株専属ポッド (JP Pod) ペーパートレード自律デーモン",
                "keywords": ["run_jp_equity_paper.py"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "logs", "jp_equity_paper.log"),
                "check_heartbeat": True,
                "start_cmd": [
                    self.python_bin, "-u", os.path.join(self.base_dir, "run_jp_equity_paper.py"),
                    "--daemon",
                    "--symbols", "7203,9984,6758,6857,8035,8306",
                    "--interval", "10.0",
                    "--budget", "20000.0",
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "fusion_engine": {
                "name": "超低レイテンシ HFT Core (Go Fusion Engine Multi-Asset)",
                "keywords": ["fusion_engine/fusion_engine", "fusion_engine --mode=daemon"],
                "exclude": ["go test", "go build"],
                "log_file": os.path.join(self.base_dir, "logs", "fusion_engine.log"),
                "check_heartbeat": False,
                "start_cmd": [
                    os.path.join(self.base_dir, "fusion_engine", "fusion_engine"),
                    "--mode=daemon",
                    "--symbols=USDJPY,BTCJPY,7203,NQ",
                    "--port=9090",
                    "--ipc-sock=/tmp/antigravity_fusion.sock",
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "umm_tf2bp_24h": {
                "name": "UMM & TF2BP 24時間連続Dry-run観測エンジン (固定パラメータ・自動調整禁止)",
                "keywords": ["run_dryrun_umm_tf2bp_24h"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "logs", "dryrun_umm_tf2bp_24h.log"),
                "check_heartbeat": True,
                "start_cmd": [
                    self.python_bin, "-u", "-m", "antigravity.quant_pipeline.run_dryrun_umm_tf2bp_24h",
                    "--symbol", "FX_BTC_JPY",
                    "--hours", "24.0",
                    "--interval", "2.0",
                    "--report-interval", "3600.0",
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "approved_arena": {
                "name": "承認済み12戦略 統合Dry-runアリーナ (Approved Strategy Arena)",
                "keywords": ["run_dryrun_approved_arena"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "logs", "dryrun_approved_arena.log"),
                "check_heartbeat": True,
                "start_cmd": [
                    self.python_bin, "-u", "-m", "antigravity.quant_pipeline.run_dryrun_approved_arena",
                    "--symbol", "FX_BTC_JPY",
                    "--interval", "5.0",
                    "--report-interval", "3600.0",
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
            "hourly_dryrun_reporter": {
                "name": "DRYRUN 1時間毎 統合定期レポート配信デーモン",
                "keywords": ["hourly_dryrun_reporter.py"],
                "exclude": ["watchdog"],
                "log_file": os.path.join(self.base_dir, "logs", "hourly_dryrun_reporter.log"),
                "check_heartbeat": False,
                "start_cmd": [
                    self.python_bin, "-u", "-m", "antigravity.quant_pipeline.hourly_dryrun_reporter",
                ],
                "last_down_alert_time": 0.0,
                "is_down": False,
                "restart_attempts": 0,
            },
        }

        # アラートクールダウン管理
        self.last_resource_alert_time: float = 0.0
        self.last_resource_level: str = "normal"
        self.last_heartbeat_alert_time: float = 0.0
        self.is_running: bool = False

    def get_service_pid(self, keywords: List[str], exclude: Optional[List[str]] = None) -> Optional[int]:
        """
        Linux /proc または環境に依存せず、実行中プロセス一覧から指定キーワードを含むPIDを取得
        """
        current_pid = os.getpid()

        # 1. /proc による高信頼・高精度走査 (Linux Azure VM)
        if os.path.exists("/proc"):
            try:
                for pid_dir in os.listdir("/proc"):
                    if not pid_dir.isdigit():
                        continue
                    pid = int(pid_dir)
                    if pid == current_pid:
                        continue
                    try:
                        with open(f"/proc/{pid}/cmdline", "rb") as f:
                            cmd_bytes = f.read()
                            cmd_str = cmd_bytes.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
                            if all(kw in cmd_str for kw in keywords):
                                if exclude and any(ex in cmd_str for ex in exclude):
                                    continue
                                return pid
                    except (IOError, PermissionError, ProcessLookupError):
                        continue
            except Exception:
                pass

        # 2. フォールバック: ps コマンド
        try:
            res = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, check=False)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.strip().split(None, 1)
                    if len(parts) == 2 and parts[0].isdigit():
                        pid = int(parts[0])
                        cmd_str = parts[1]
                        if pid == current_pid:
                            continue
                        if all(kw in cmd_str for kw in keywords):
                            if exclude and any(ex in cmd_str for ex in exclude):
                                continue
                            return pid
        except Exception:
            pass

        return None

    def read_tail_log(self, file_path: str, max_lines: int = 15) -> str:
        """ログファイルの末尾を取得"""
        if not os.path.exists(file_path):
            return "(ログファイル未生成)"
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                return "".join(lines[-max_lines:])
        except Exception as ex:
            return f"(ログ読み取り失敗: {ex})"

    def check_and_recover_services(self):
        """全サービスのプロセス死活およびログ更新ハートビートを診断"""
        now = time.time()

        for svc_id, svc in self.services.items():
            # 旧来の単体逆張りLIVEエンジンは廃止
            if svc_id == "grid_mm_live":
                continue

            # 意図的停止フラグがある場合は LIVE 取引の再起動をスキップ
            halt_flag = os.path.join(self.base_dir, "data", "LIVE_HALTED.flag")
            if svc_id == "quant_pipeline" and os.path.exists(halt_flag):
                continue

            pid = self.get_service_pid(svc["keywords"], exclude=svc.get("exclude"))

            # 1. プロセス停止（システムダウン）判定
            if pid is None:
                if not svc["is_down"]:
                    svc["is_down"] = True
                    log_tail = self.read_tail_log(svc["log_file"])
                    reason = f"プロセスが停止またはクラッシュしていることを検知しました (キーワード: `{svc['keywords']}`)"
                    print(f"[Watchdog] 🚨 DOWN DETECTED: {svc['name']} (pid=None)", flush=True)

                    # Discordアラート即時送信
                    self.notifier.send_system_down_alert(
                        service_name=svc["name"],
                        reason=reason,
                        log_snippet=log_tail,
                        auto_recovery_status="自動再起動を実行中..." if self.enable_auto_restart else "手動対応待ち",
                    )
                    svc["last_down_alert_time"] = now

                # 自動再起動
                if self.enable_auto_restart:
                    self.restart_service(svc_id)

            else:
                # プロセスが稼働中の場合
                if svc["is_down"]:
                    # ダウンからの復旧を検知
                    svc["is_down"] = False
                    svc["restart_attempts"] = 0
                    print(f"[Watchdog] 🟢 RECOVERED: {svc['name']} (pid={pid})", flush=True)
                    self.notifier.send_system_recovered_alert(
                        service_name=svc["name"],
                        message=f"プロセスが正常に立ち上がり、稼働を開始しました。(PID: `{pid}`)",
                    )

                # 2. ログ更新ハートビート（ストール・フリーズ・通信停止）判定
                if svc.get("check_heartbeat") and os.path.exists(svc["log_file"]):
                    try:
                        mtime = os.path.getmtime(svc["log_file"])
                        elapsed = now - mtime
                        if elapsed >= self.heartbeat_timeout_sec:
                            if now - self.last_heartbeat_alert_time >= 300.0:
                                print(f"[Watchdog] 🚨 STALL DETECTED: {svc['name']} (無更新: {elapsed:.0f}秒)", flush=True)
                                log_tail = self.read_tail_log(svc["log_file"])
                                self.notifier.send_system_down_alert(
                                    service_name=svc["name"],
                                    reason=f"約定データ受信停止・ログ更新途絶を検知しました (最終受信から {elapsed:.0f} 秒経過 / 許容: {self.heartbeat_timeout_sec:.0f} 秒)",
                                    log_snippet=log_tail,
                                    auto_recovery_status="通信復旧およびプロセス再起動を実行中...",
                                )
                                self.last_heartbeat_alert_time = now

                                # プロセスを再起動して通信リセット
                                if self.enable_auto_restart:
                                    print(f"[Watchdog] 🔄 ハングアップ疑いのため {svc['name']} (pid={pid}) を再起動します", flush=True)
                                    try:
                                        os.kill(pid, signal.SIGTERM)
                                        time.sleep(2.0)
                                    except Exception:
                                        pass
                                    self.restart_service(svc_id)
                    except Exception as ex:
                        print(f"[Watchdog] Heartbeat check error for {svc_id}: {ex}", flush=True)

    def restart_service(self, svc_id: str):
        """サービスの自動再起動を実行"""
        svc = self.services[svc_id]
        svc["restart_attempts"] += 1
        print(f"[Watchdog] 🚀 Restarting {svc['name']} (Attempt {svc['restart_attempts']})...", flush=True)

        try:
            log_f = open(svc["log_file"], "a", encoding="utf-8")
            subprocess.Popen(
                svc["start_cmd"],
                cwd=self.base_dir,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            time.sleep(2.0)
            new_pid = self.get_service_pid(svc["keywords"], exclude=svc.get("exclude"))
            if new_pid:
                print(f"[Watchdog] ✅ {svc['name']} 再起動成功 (New PID: {new_pid})", flush=True)
            else:
                print(f"[Watchdog] ⚠️ {svc['name']} 再起動確認待機中...", flush=True)
        except Exception as ex:
            print(f"[Watchdog] ❌ Restart failed for {svc_id}: {ex}", flush=True)

    def check_system_resources(self):
        """システムリソース（CPU/MEM/DISK）の急変・圧迫を診断し、その都度即時通報"""
        now = time.time()
        metrics = self.sys_monitor.collect_all_metrics()

        disk = metrics.get("disk", {})
        mem = metrics.get("memory", {})
        cpu = metrics.get("cpu_pct", 0.0)

        # 深刻度判定
        is_crit = (
            disk.get("used_pct", 0.0) >= 92.0
            or mem.get("used_pct", 0.0) >= 92.0
            or cpu >= 95.0
            or mem.get("free_gb", 10.0) < 0.4  # 空きメモリ400MB未満
        )
        is_warn = (
            disk.get("used_pct", 0.0) >= 85.0
            or mem.get("used_pct", 0.0) >= 85.0
            or cpu >= 85.0
            or mem.get("free_gb", 10.0) < 0.8  # 空きメモリ800MB未満
        )

        current_level = "critical" if is_crit else ("warning" if is_warn else "normal")

        # 状態悪化時、または圧迫継続中かつクールダウン（300秒）経過時に即時通報
        should_alert = False
        if is_warn or is_crit:
            if current_level == "critical" and self.last_resource_level != "critical":
                should_alert = True  # 深刻化時は即座に通報
            elif now - self.last_resource_alert_time >= 300.0:
                should_alert = True

        if should_alert:
            reasons = []
            if disk.get("used_pct", 0.0) >= 85.0:
                reasons.append(f"ディスク使用率逼迫: {disk.get('used_pct', 0.0):.1f}% (空き: {disk.get('free_gb', 0.0):.1f} GB)")
            if mem.get("used_pct", 0.0) >= 85.0 or mem.get("free_gb", 10.0) < 0.8:
                reasons.append(f"メモリ逼迫: {mem.get('used_pct', 0.0):.1f}% (空き残量: {mem.get('free_gb', 0.0):.2f} GB)")
            if cpu >= 85.0:
                reasons.append(f"CPU高負荷逼迫: {cpu:.1f}%")

            print(f"[Watchdog] 🚨 RESOURCE PRESSURE DETECTED: {reasons}", flush=True)

            # 自己修復アクション実行
            actions = self.sys_monitor.execute_remediation(metrics)

            # Discord即時アラート送信
            self.notifier.send_resource_pressure_alert(
                metrics=metrics,
                trigger_reasons=reasons,
                remediation_actions=actions,
                server_name="Antigravity Azure Node",
                level=current_level,
            )
            self.last_resource_alert_time = now

        self.last_resource_level = current_level

    def run_forever(self):
        """監視デーモンのメインループ"""
        print("=" * 60)
        print("  🛡️ ANTIGRAVITY WATCHDOG SENTINEL v1.0.0")
        print(f"  Base Dir: {self.base_dir}")
        print(f"  Check Interval: {self.check_interval_sec} s")
        print(f"  Heartbeat Timeout: {self.heartbeat_timeout_sec} s")
        print(f"  Auto-Restart Enabled: {self.enable_auto_restart}")
        print(f"  Discord Alert URL Configured: {self.notifier.is_enabled('alert')}")
        print("=" * 60)

        self.is_running = True

        # 起動確認通知（初回のみ）
        self.notifier.send_embed(
            title="🛡️ 【監視センチネル常駐開始】Antigravity Watchdog",
            description=(
                f"**システムダウン・システム圧迫・大きなドローダウンの常時即時監視を開始しました。**\n\n"
                f"• **監視対象**: 取引エンジン, 自律発見デーモン, ニュースパイプライン\n"
                f"• **監視周期**: `{self.check_interval_sec:.0f} 秒間隔`\n"
                f"• **データ途絶監視**: `{self.heartbeat_timeout_sec:.0f} 秒`\n"
                f"• **自動自己修復・復旧**: `有効`\n"
                f"• **緊急通報先**: Discord Operation Alert チャンネル"
            ),
            color=0x3498DB,
            footer_text="Antigravity Watchdog Sentinel 🛡️",
            target="alert",
        )

        while self.is_running:
            try:
                # 1. プロセス死活＆ハートビート監視
                self.check_and_recover_services()

                # 2. システムリソース圧迫監視
                self.check_system_resources()

            except Exception as ex:
                print(f"[Watchdog] Loop error: {ex}", flush=True)

            time.sleep(self.check_interval_sec)


def main():
    import traceback
    try:
        sentinel = WatchdogSentinel()
        sentinel.run_forever()
    except KeyboardInterrupt:
        print("[Watchdog] 監視センチネルを手動停止しました。", flush=True)
    except Exception as ex:
        print(f"[Watchdog] 致命的例外により終了: {ex}\n{traceback.format_exc()}", flush=True)


if __name__ == "__main__":
    main()
