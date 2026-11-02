#!/usr/bin/env python3
"""探测下载直链是否支持 HTTP Range —— 按需读取大文件的前提。

用法::

    python3 scripts/check_range_support.py            # 自动挑根目录里最小的文件
    python3 scripts/check_range_support.py <file_id>  # 指定文件

判读：

- 两次都返回 206 且 Content-Range 起始位置分别对应 0 / 1024 → 支持按需读；
- 返回 200（并带上整个 Content-Length）→ 服务端忽略 Range，本层按需读会自动
  降级为整文件下载，功能不受影响，只是拿不到按需读的收益。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from mcloud_fuse.client.wrapper import DirectClientWrapper  # noqa: E402


def pick_smallest_file(wrapper):
    items = [item for item in wrapper.list_directory("/") if not item.is_dir and item.size > 0]
    if not items:
        return None
    item = min(items, key=lambda entry: entry.size)
    print(f"自动选中（根目录最小的文件）: {item.name}  {item.size} bytes  id={item.file_id}")
    return item.file_id


def probe(url: str, offset: int, length: int = 16) -> None:
    headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
    resp = requests.get(url, headers=headers, stream=True, timeout=30)
    try:
        print(f"  offset={offset}: HTTP {resp.status_code}", end="")
        if resp.status_code == 206:
            print(f"  Content-Range={resp.headers.get('Content-Range', '')!r}")
        else:
            print()
    finally:
        resp.close()


def main() -> int:
    wrapper = DirectClientWrapper()
    file_id = sys.argv[1] if len(sys.argv) > 1 else pick_smallest_file(wrapper)
    if not file_id:
        print("根目录没有可探测的文件", file=sys.stderr)
        return 1

    url = wrapper.get_download_urls([file_id]).get(file_id)
    if not url:
        print(f"未取到 {file_id} 的下载直链", file=sys.stderr)
        return 1
    print(f"直链：{url[:120]}...")
    probe(url, 0)
    probe(url, 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
