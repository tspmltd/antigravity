import os
import subprocess
import time
from typing import Dict, Any, List, Optional
from datetime import datetime
from .notifier import DiscordNotifier


class GitAutoSync:
    """
    自律戦略探索パイプラインおよびクラウドVM用 Git自動同期エンジン。
    新戦略が合格・承認された際、自動でGitコミット＆GitHubプッシュを行い、
    Discordへ通知してローカルPCとの完全同期を維持します。
    """

    def __init__(self, notifier: Optional[DiscordNotifier] = None, branch: str = "main"):
        self.notifier = notifier or DiscordNotifier()
        self.branch = branch

    def _run_git(self, args: List[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
        """Gitコマンドを安全に実行"""
        cmd = ["git"] + args
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )

    def is_git_repo(self) -> bool:
        """Gitリポジトリ内であるか確認"""
        res = self._run_git(["rev-parse", "--is-inside-work-tree"])
        return res.returncode == 0 and res.stdout.strip() == "true"

    def has_uncommitted_approved(self) -> bool:
        """strategies/approved/ または reports/ に未コミット差分があるか確認"""
        res = self._run_git(["status", "--porcelain", "strategies/approved", "reports"])
        return res.returncode == 0 and bool(res.stdout.strip())

    def sync_approved_strategies(
        self,
        strategy_name: str = "AutomatedStrategy",
        approved_file: Optional[str] = None,
        report_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        新合格戦略およびレポートを自動コミット＆GitHubへプッシュ。
        """
        if not self.is_git_repo():
            return {"success": False, "reason": "Not a git repository"}

        if not self.has_uncommitted_approved():
            return {"success": True, "reason": "No uncommitted approved strategies"}

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[GitAutoSync] 🔄 新規合格戦略 '{strategy_name}' のGitHub自動プッシュを開始します...", flush=True)

        try:
            # 1. ステージング
            add_res = self._run_git(["add", "strategies/approved", "reports"])
            if add_res.returncode != 0:
                print(f"[GitAutoSync] git add 失敗: {add_res.stderr}", flush=True)
                return {"success": False, "reason": add_res.stderr}

            # 2. コミット
            commit_msg = (
                f"feat(discovery): auto-sync newly approved strategy '{strategy_name}' ({timestamp}) [skip ci]"
            )
            commit_res = self._run_git(["commit", "-m", commit_msg])
            if commit_res.returncode != 0:
                print(f"[GitAutoSync] git commit 失敗: {commit_res.stderr}", flush=True)
                return {"success": False, "reason": commit_res.stderr}

            print(f"[GitAutoSync] ✅ コミット作成成功: {commit_msg}", flush=True)

            # 3. プッシュ
            push_res = self._run_git(["push", "origin", self.branch], timeout=60.0)
            if push_res.returncode != 0:
                print(f"[GitAutoSync] ⚠️ git push 失敗 (認証またはリモート競合の可能性): {push_res.stderr}", flush=True)
                return {
                    "success": False,
                    "reason": f"Push failed: {push_res.stderr.strip()}",
                    "committed": True,
                }

            print(f"[GitAutoSync] 🚀 GitHubへの自動プッシュ完了！ (origin/{self.branch})", flush=True)

            # 4. Discord通知
            file_info = f"`{os.path.basename(approved_file)}`" if approved_file else "新戦略ファイル"
            report_info = f"`{os.path.basename(report_file)}`" if report_file else "検証レポート"
            self.notifier.send_system_update(
                title="🌟 【自律Git同期】新戦略をGitHubへ自動プッシュしました",
                description=(
                    f"Azure VMの自律探索デーモンが合格させた新戦略が、**GitHubリポジトリへ自動同期**されました！\n\n"
                    f"• **採択戦略名**: `{strategy_name}`\n"
                    f"• **戦略ファイル**: {file_info}\n"
                    f"• **検証レポート**: {report_info}\n\n"
                    f"💻 **ローカルPCでの同期手順**:\n"
                    f"手元のターミナルで `git pull origin main` を実行すると、最新の戦略ファイルを直ちに取り込めます。"
                ),
            )

            return {"success": True, "committed": True, "pushed": True}

        except Exception as ex:
            print(f"[GitAutoSync] 例外発生: {ex}", flush=True)
            return {"success": False, "reason": str(ex)}
