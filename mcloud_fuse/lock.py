"""缓存目录互斥锁。

同一个缓存目录只能有一个挂载进程：读缓存按 fileId、写回按 inode 命名，
两个进程共用同一目录会互相覆盖，甚至把对方的在途写回当成自己的恢复。

用 ``flock`` 而不是 PID 文件：进程被 kill -9 时内核会自动释放锁，不会留下
需要手工清理的陈旧锁。锁文件本身可以保留（0 字节，0600）。
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional

LOCK_NAME = ".mcloud-fuse.lock"


class CacheLockError(RuntimeError):
    """缓存目录已被另一个挂载进程占用。"""


class CacheLock:
    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir).expanduser()
        self.path = self.cache_dir / LOCK_NAME
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = ""
            try:
                holder = os.read(fd, 64).decode("utf-8", "replace").strip()
            except OSError:
                pass
            os.close(fd)
            where = f"（pid {holder}）" if holder else ""
            raise CacheLockError(
                f"缓存目录已被另一个 mcloud-fuse 进程占用{where}：{self.cache_dir}\n"
                "请先卸载那个挂载，或给本次挂载换一个 --cache-dir。"
            ) from exc
        except OSError:
            os.close(fd)
            raise
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "CacheLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
