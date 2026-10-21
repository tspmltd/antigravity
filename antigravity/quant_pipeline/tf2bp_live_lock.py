"""Exclusive lock for TF2BP LIVE single-runner invariant.

Invariant: active TF2BP LIVE runner <= 1
Acquire is atomic via fcntl.LOCK_EX|LOCK_NB (not check-then-act on PID).
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

JST = timezone(timedelta(hours=9))

DEFAULT_LOCK_PATH = Path("/home/azureuser/antigravity/data/tf2bp_live_runner.lock")


class RunnerLockBusy(RuntimeError):
    """Another TF2BP LIVE runner holds the exclusive lock."""


@dataclass
class LockInfo:
    pid: int
    run_id: str
    acquired_ts: float
    ts_jst: str


class ExclusiveRunnerLock:
    """Process-wide exclusive lock (fcntl). Hold fd open for the runner lifetime."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or DEFAULT_LOCK_PATH)
        self._fd: Optional[Any] = None
        self.info: Optional[LockInfo] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, *, run_id: str, blocking: bool = False) -> LockInfo:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(self.path, "a+", encoding="utf-8")
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd.fileno(), flags)
        except BlockingIOError as exc:
            holder = _read_lock_meta(fd)
            fd.close()
            raise RunnerLockBusy(
                f"TF2BP LIVE lock busy path={self.path} holder={holder}"
            ) from exc
        except OSError as exc:
            fd.close()
            raise RunnerLockBusy(f"TF2BP LIVE lock acquire failed: {exc}") from exc

        info = LockInfo(
            pid=os.getpid(),
            run_id=run_id,
            acquired_ts=time.time(),
            ts_jst=datetime.now(JST).isoformat(timespec="seconds"),
        )
        fd.seek(0)
        fd.truncate()
        fd.write(json.dumps(info.__dict__, ensure_ascii=False, indent=2) + "\n")
        fd.flush()
        os.fsync(fd.fileno())
        self._fd = fd
        self.info = info
        return info

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fd.close()
        except Exception:
            pass
        self._fd = None

    def __enter__(self) -> "ExclusiveRunnerLock":
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()


def _read_lock_meta(fd: Any) -> Dict[str, Any]:
    try:
        fd.seek(0)
        raw = fd.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def try_acquire_runner_lock(run_id: str, path: Optional[Path] = None) -> ExclusiveRunnerLock:
    lock = ExclusiveRunnerLock(path=path)
    lock.acquire(run_id=run_id, blocking=False)
    return lock
