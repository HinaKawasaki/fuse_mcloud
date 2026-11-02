"""``mcloud-fuse journal``：查看 / 清理 / 重试写回日志。

日志本身在 ``~/.local/state/mcloud-fuse``，payload 在缓存目录的 ``writeback/``。
``forget`` 默认只删数据库记录（保留 payload 方便人工抢救），加 ``--purge`` 才删文件。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from mcloud_fuse import userconfig
from mcloud_fuse.journal import LOST, WritebackJournal, default_journal_path, rescue_dir
from mcloud_fuse.lock import CacheLock, CacheLockError


def _cache_dir(args: argparse.Namespace) -> Path:
    if args.cache_dir:
        return Path(args.cache_dir).expanduser()
    try:
        saved = userconfig.load()
    except userconfig.ConfigError:
        saved = None
    return Path(saved.cache_dir) if saved else Path("/tmp/cmcloud-fuse")


def _write_back_dir(args: argparse.Namespace) -> Path:
    override = os.getenv("MCLOUD_WRITE_BACK_DIR")
    if override:
        return Path(override).expanduser()
    return _cache_dir(args) / "writeback"


def _journal(args: argparse.Namespace) -> WritebackJournal:
    if args.journal:
        return WritebackJournal(Path(args.journal).expanduser())
    return WritebackJournal(default_journal_path(_write_back_dir(args)))


def _fmt_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def cmd_list(args: argparse.Namespace) -> int:
    journal = _journal(args)
    try:
        entries = journal.entries(args.state)
    finally:
        journal.close()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "key": item.key,
                        "state": item.state,
                        "file_name": item.file_name,
                        "target_dir_id": item.target_dir_id,
                        "size": item.size,
                        "payload_path": item.payload_path,
                        "updated_at": item.updated_at,
                        "last_error": item.last_error,
                    }
                    for item in entries
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not entries:
        print("写回日志为空。")
        return 0
    print(f"{'KEY':<18} {'STATE':<9} {'SIZE':>9}  FILE")
    for item in entries:
        payload = "" if Path(item.payload_path).exists() else " [payload 不存在]"
        print(
            f"{item.key:<18} {item.state:<9} {_fmt_size(item.size):>9}  "
            f"{item.file_name}{payload}"
        )
        if item.last_error:
            print(f"{'':<18} last_error: {item.last_error}")
    return 0


def _ensure_not_mounted(args: argparse.Namespace) -> bool:
    """forget/gc 可能删在途记录；挂载进程持锁时默认拒绝，--force 才放行。"""
    if args.force:
        return True
    lock = CacheLock(_cache_dir(args))
    try:
        lock.acquire()
    except CacheLockError as exc:
        print(f"❌ {exc}\n   挂载运行中，确认要操作请加 --force。", file=sys.stderr)
        return False
    finally:
        lock.release()
    return True


def cmd_forget(args: argparse.Namespace) -> int:
    if not args.keys and not args.lost and not args.all:
        print("需要指定 key，或加 --lost / --all。", file=sys.stderr)
        return 2
    if not _ensure_not_mounted(args):
        return 1
    journal = _journal(args)
    try:
        if args.all:
            entries = journal.entries()
        elif args.lost:
            entries = journal.entries(LOST)
        else:
            entries = [item for item in (journal.get(key) for key in args.keys) if item]
        removed = 0
        for item in entries:
            journal.remove(item.key)
            removed += 1
            if args.purge:
                try:
                    Path(item.payload_path).unlink()
                except OSError:
                    pass
        print(f"已移除 {removed} 条日志记录" + ("，并删除 payload" if args.purge else "（payload 保留）"))
    finally:
        journal.close()
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    """清理孤儿 payload 和过期的 building/lost 记录。"""
    if not _ensure_not_mounted(args):
        return 1
    write_back = _write_back_dir(args)
    journal = _journal(args)
    try:
        entries = journal.entries()
        referenced = {str(Path(item.payload_path)) for item in entries}
        now = time.time()
        removed_files = 0
        for directory, pattern in (
            (write_back, "*.wb"),
            (rescue_dir(write_back), "*.wb"),
            (rescue_dir(write_back), "*.part"),
        ):
            if not directory.exists():
                continue
            for path in directory.glob(pattern):
                if str(path) in referenced and not path.name.endswith(".part"):
                    continue
                try:
                    age_h = (now - path.stat().st_mtime) / 3600
                except OSError:
                    continue
                if age_h >= args.orphan_hours:
                    try:
                        path.unlink()
                        removed_files += 1
                    except OSError:
                        pass
        removed_rows = 0
        for item in entries:
            if item.state not in ("building", LOST):
                continue
            age_h = (now - item.updated_at) / 3600 if item.updated_at else 0
            if age_h >= args.stale_hours:
                journal.remove(item.key)
                if args.purge:
                    try:
                        Path(item.payload_path).unlink()
                    except OSError:
                        pass
                removed_rows += 1
        print(f"清理孤儿 payload {removed_files} 个，过期日志 {removed_rows} 条")
    finally:
        journal.close()
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    """在未挂载时手动补传所有 pending 记录。"""
    from mcloud_fuse.cache.data_cache import DataCache
    from mcloud_fuse.client.wrapper import DirectClientWrapper
    from mcloud_fuse.lock import CacheLock, CacheLockError

    cache = _cache_dir(args)
    lock = CacheLock(cache)
    try:
        lock.acquire()
    except CacheLockError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    try:
        data_cache = DataCache(
            client_wrapper=DirectClientWrapper(),
            cache_dir=cache,
            write_back_dir=_write_back_dir(args),
            journal_db=Path(args.journal).expanduser() if args.journal else None,
        )
        try:
            recovered = data_cache.recover_pending_uploads()
        finally:
            data_cache.close()
        print(f"补传完成：{recovered} 个文件")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 补传失败：{exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mcloud-fuse journal", description="写回日志管理")
    parser.add_argument("--cache-dir", help="挂载使用的缓存目录（默认读配置）")
    parser.add_argument("--journal", help="直接指定 SQLite 日志路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出日志")
    p_list.add_argument("--state", choices=("building", "uploading", "pending", "rescued", "uploaded", "lost"))
    p_list.add_argument("--lost", action="store_const", const="lost", dest="state")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_forget = sub.add_parser("forget", help="删除日志记录（默认保留 payload）")
    p_forget.add_argument("keys", nargs="*")
    p_forget.add_argument("--lost", action="store_true", help="删除所有 lost 记录")
    p_forget.add_argument("--all", action="store_true", help="删除所有记录")
    p_forget.add_argument("--purge", action="store_true", help="同时删除 payload 文件")
    p_forget.add_argument("--force", action="store_true", help="挂载运行中也允许操作")
    p_forget.set_defaults(func=cmd_forget)

    p_gc = sub.add_parser("gc", help="清理孤儿 payload / 过期 building、lost 记录")
    p_gc.add_argument("--orphan-hours", type=float, default=24.0)
    p_gc.add_argument("--stale-hours", type=float, default=24.0)
    p_gc.add_argument("--force", action="store_true", help="挂载运行中也允许操作")
    p_gc.add_argument("--purge", action="store_true", help="同时删除过期记录的 payload")
    p_gc.set_defaults(func=cmd_gc)

    p_retry = sub.add_parser("retry", help="未挂载时手动补传 pending 文件")
    p_retry.set_defaults(func=cmd_retry)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
