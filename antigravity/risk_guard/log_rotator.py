"""
Log Rotator & Disk Hygiene Sentinel (自動ログローテーション＆ディスク衛生維持)
================================================================================
システム内のログファイルが肥大化してディスクおよびメモリキャッシュを圧迫するのを
未然に防ぐため、定期的にサイズを監視し、10MB を超えたログを自動で安全に切り詰める。
"""
import os
import glob
import time
from typing import List, Dict, Any

MAX_LOG_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
KEEP_LINES = 5000  # 切り詰め時に保持する直近行数


def rotate_log_file(fpath: str, max_bytes: int = MAX_LOG_SIZE_BYTES, keep_lines: int = KEEP_LINES) -> Dict[str, Any]:
    """単一ログファイルの検査と必要に応じた安全な切り詰め"""
    if not os.path.exists(fpath):
        return {"file": fpath, "rotated": False, "reason": "not_found"}

    try:
        size = os.path.getsize(fpath)
        if size <= max_bytes:
            return {"file": fpath, "rotated": False, "size_mb": round(size / (1024 * 1024), 2)}

        # ファイルサイズが上限超過 -> 直近 keep_lines 行を取り出して切り詰め
        tmp_path = f"{fpath}.rot.tmp"
        with open(fpath, "rb") as fin:
            lines = fin.readlines()

        tail_lines = lines[-keep_lines:] if len(lines) > keep_lines else lines
        with open(tmp_path, "wb") as fout:
            fout.writelines(tail_lines)

        os.replace(tmp_path, fpath)
        new_size = os.path.getsize(fpath)

        return {
            "file": fpath,
            "rotated": True,
            "old_size_mb": round(size / (1024 * 1024), 2),
            "new_size_mb": round(new_size / (1024 * 1024), 2),
            "freed_mb": round((size - new_size) / (1024 * 1024), 2),
        }
    except Exception as e:
        return {"file": fpath, "rotated": False, "error": str(e)}


def run_all_log_rotations(base_dir: str = "/home/azureuser/antigravity") -> List[Dict[str, Any]]:
    """システム全体の主要ログファイルを一括検査・ローテーション"""
    target_patterns = [
        os.path.join(base_dir, "*.log"),
        os.path.join(base_dir, "logs", "*.log"),
        os.path.join(base_dir, "news_pipeline", "logs", "*.log"),
        "/tmp/*.log",
    ]

    results = []
    for pattern in target_patterns:
        for fpath in glob.glob(pattern):
            res = rotate_log_file(fpath)
            if res.get("rotated"):
                results.append(res)

    return results


if __name__ == "__main__":
    print("[LogRotator] ログローテーション手動実行開始...")
    res = run_all_log_rotations()
    if res:
        for r in res:
            print(f"  • {r['file']}: {r['old_size_mb']}MB -> {r['new_size_mb']}MB (解放: {r['freed_mb']}MB)")
    else:
        print("  • 全ログファイル健全 (上限10MB以下)")
