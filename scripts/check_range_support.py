#!/usr/bin/env python3
"""探测「下载直链是否支持 HTTP Range」——按需读取大文件的前提。

用法（需要有效的登录态）：

    python3 scripts/check_range_support.py            # 自动挑沙箱里最小的文件
    python3 scripts/check_range_support.py <file_id>  # 指定文件

判读：
- 两次都返回 206 且 Content-Range 起始位置分别对应 0 / 1024 → 支持按需读；
- 返回 200（并带上整个 Content-Length）→ 服务端忽略 Range，本层的按需读会自动
  降级为「首次读取整文件下载」，功能不受影响但拿不到按需读的收益。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from mcloud_fuse.client.auth_manager import AuthManager  # noqa: E402
from mcloud_fuse.client.wrapper import DirectClientWrapper  # noqa: E402


def pick_smallest_file(wrapper):
    root_id = wrapper.sandbox_root_file_id()
    items = [item for item in wrapper.list_directory(root_id) if not item.is_dir]
    if not items:
        return None
    item = min(items, key=lambda entry: entry.size)
    print(f"自动选中（沙箱内最小的文件）: {item.name}  {item.size} bytes  id={item.file_id}")
    return item.file_id


def main() -> int:
    wrapper = DirectClientWrapper(AuthManager())
    file_id = sys.argv[1] if len(sys.argv) > 1 else pick_smallest_file(wrapper)
    if not file_id:
        print("沙箱根目录里没有文件可探测；请显式传入 file_id")
        return 2

    url = wrapper._resolve_download_url(file_id)
    print(f"下载地址: {url[:120]}{'...' if len(url) > 120 else ''}")

    verdict = "未知"
    for start in (0, 1024):
        headers = {"Range": f"bytes={start}-{start + 1023}"}
        resp = requests.get(url, headers=headers, stream=True, timeout=30)
        print(
            f"Range bytes={start}-{start + 1023}: "
            f"HTTP {resp.status_code}  "
            f"Content-Range={resp.headers.get('Content-Range')!r}  "
            f"Accept-Ranges={resp.headers.get('Accept-Ranges')!r}  "
            f"Content-Length={resp.headers.get('Content-Length')!r}"
        )
        resp.close()
        if resp.status_code == 206:
            verdict = "支持 Range（按需读可用）"
        elif resp.status_code == 200 and verdict == "未知":
            verdict = "不支持 Range（会自动降级为整文件下载）"

    print(f"\n结论: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
