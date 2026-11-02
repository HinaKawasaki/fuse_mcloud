"""`mcloud-fuse trash` 子命令：查看 / 恢复 / 清空云盘回收站。

`rm` 默认是永久删除，不经过回收站；这里是给 `MCLOUD_RM_MODE=trash` 以及
网页端删除的对象准备的恢复入口。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import List, Optional, Sequence

from mcloud_fuse.client.web139 import RecycleItem
from mcloud_fuse.client.wrapper import DirectClientWrapper

_SIZE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_size(size: int) -> str:
    value = float(size)
    for unit in _SIZE_UNITS:
        if value < 1024 or unit == _SIZE_UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcloud-fuse trash",
        description="查看 / 恢复 / 清空中国移动云盘回收站",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ls_parser = sub.add_parser("list", aliases=["ls"], help="列出回收站")
    ls_parser.add_argument("--json", action="store_true", help="输出 JSON")
    ls_parser.add_argument("--limit", type=int, default=50, help="最多显示几条（默认 50）")
    ls_parser.add_argument("--page-size", type=int, default=100, help=argparse.SUPPRESS)

    restore_parser = sub.add_parser("restore", help="按 fileId 恢复")
    restore_parser.add_argument("file_ids", nargs="+", help="一个或多个 fileId")

    empty_parser = sub.add_parser("empty", aliases=["clear"], help="清空回收站（不可恢复）")
    empty_parser.add_argument("--yes", "-y", action="store_true", help="跳过确认")

    return parser


def _print_items(items: Sequence[RecycleItem]) -> None:
    if not items:
        print("回收站为空")
        return
    header = f"{'类型':<4} {'大小':>10} {'剩余':>6} {'删除时间':<20} {'名称'}"
    print(header)
    print("-" * 80)
    for item in items:
        kind = "目录" if item.is_dir else "文件"
        print(
            f"{kind:<4} {human_size(item.size):>10} {str(item.remaining_days) + '天':>6} "
            f"{item.trashed_at[:19]:<20} {item.name}"
        )
        print(f"     fileId: {item.file_id}")


def _cmd_list(client, args) -> int:
    items: List[RecycleItem] = []
    for item in client.list_recycle_bin(page_size=max(1, args.page_size)):
        items.append(item)
        if len(items) >= args.limit:
            break
    if args.json:
        print(json.dumps([{k: v for k, v in asdict(item).items() if k != "raw"} for item in items], ensure_ascii=False, indent=2))
    else:
        _print_items(items)
    return 0


def _cmd_restore(client, args) -> int:
    client.restore_recycle(args.file_ids)
    print(f"✅ 已恢复 {len(args.file_ids)} 项：")
    for file_id in args.file_ids:
        print(f"   {file_id}")
    return 0


def _cmd_empty(client, args) -> int:
    if not args.yes:
        if not sys.stdin.isatty():
            print("拒绝执行：清空回收站需要 --yes（非交互环境）", file=sys.stderr)
            return 1
        answer = input("将清空回收站中的全部对象，且不可恢复。输入 yes 继续：").strip()
        if answer != "yes":
            print("已取消")
            return 1
    client.clear_recycle_bin()
    print("✅ 回收站已清空")
    return 0


def main(argv: Optional[Sequence[str]] = None, client=None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if client is None:
        try:
            client = DirectClientWrapper()
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 初始化失败：{exc}", file=sys.stderr)
            print("   请先运行 scripts/import_web_token.py 导入 token。", file=sys.stderr)
            return 1

    try:
        if args.command in ("list", "ls"):
            return _cmd_list(client, args)
        if args.command == "restore":
            return _cmd_restore(client, args)
        if args.command in ("empty", "clear"):
            return _cmd_empty(client, args)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    parser.error(f"未知子命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
