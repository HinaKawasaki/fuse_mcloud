"""文件系统小工具：判断目录落在 tmpfs 还是持久盘。"""

from __future__ import annotations

from pathlib import Path


def filesystem_type(path: Path) -> str:
    """从 /proc/mounts 找最长前缀，返回 fstype；读不到时返回「未知」。"""
    try:
        target = str(Path(path).resolve())
        best_mount = ""
        best_type = ""
        with open("/proc/mounts", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mountpoint, fstype = parts[1], parts[2]
                if target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/"):
                    if len(mountpoint) >= len(best_mount):
                        best_mount, best_type = mountpoint, fstype
        return best_type or "未知"
    except OSError:
        return "未知"


def is_volatile_writeback(path: Path) -> bool:
    """写回目录是否在易失文件系统上（需要关机抢救）。"""
    return filesystem_type(path) in ("tmpfs", "ramfs")
