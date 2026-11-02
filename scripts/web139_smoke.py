#!/usr/bin/env python3
"""yun.139.com Web API 真机冒烟（L3，默认不参与挂载）。

用法::

    export MCLOUD_WEB_TOKEN='<Basic 后面的值>'
    # 或写入 ~/.config/mcloud-fuse/web_token
    python3 scripts/web139_smoke.py --list-only
    python3 scripts/web139_smoke.py            # 全链路，会在云盘临时建目录

会在云盘根目录（或 --root 指定目录）下创建 ``_mcloud_fuse_web_probe_*``，
结束时送进回收站。若不想留痕，加 ``--keep`` 保留现场，或手工在 App 清空回收站。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcloud_fuse.client.web139 import (  # noqa: E402
    Web139Auth,
    Web139Client,
    Web139Error,
)


def _print_token(token) -> None:
    expiry = token.expires_at_ms
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry / 1000)) if expiry else "未知"
    print(f"账号：{token.account}   token 到期：{when}")


def _check_range(url: str) -> None:
    try:
        resp = requests.get(url, headers={"Range": "bytes=0-9"}, stream=True, timeout=30)
    except requests.RequestException as exc:
        print(f"  ! 直链请求失败：{exc}")
        return
    try:
        if resp.status_code == 206:
            body = resp.raw.read(10, decode_content=True)
            print(f"  Range：支持（206，{len(body)} 字节）")
        elif resp.status_code == 200:
            print("  Range：不支持（200，服务端忽略 Range）")
        else:
            print(f"  Range：HTTP {resp.status_code}")
    finally:
        resp.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="yun.139.com Web API 冒烟")
    parser.add_argument("--root", default="/", help="探测用的父目录 fileId，默认根目录 /")
    parser.add_argument("--list-only", action="store_true", help="只列目录并取一个下载直链")
    parser.add_argument("--keep", action="store_true", help="结束时不要删除探测对象")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    try:
        auth = Web139Auth.load()
    except Web139Error as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    _print_token(auth.token)
    client = Web139Client(auth, timeout=args.timeout)

    print(f"\n1. 列目录 {args.root} ...")
    items = client.list_files(args.root)
    print(f"   共 {len(items)} 项：")
    for item in items[:10]:
        kind = "DIR " if item.is_dir else "FILE"
        print(f"   [{kind}] {item.name}  id={item.file_id}  size={item.size}")

    if args.list_only:
        first_file = next((i for i in items if not i.is_dir and i.size > 0), None)
        if first_file:
            print(f"\n2. 取下载直链：{first_file.name}")
            url = client.get_download_url(first_file.file_id)
            print(f"   {url[:120]}...")
            _check_range(url)
        return 0

    stamp = int(time.time())
    dir_name = f"_mcloud_fuse_web_probe_{stamp}"
    print(f"\n2. 建目录 {dir_name} ...")
    dir_id = client.create_folder(args.root, dir_name)
    print(f"   dirId={dir_id}")

    payload = (b"mcloud-fuse web139 probe\n" * 64)
    tmp = tempfile.NamedTemporaryFile(prefix="web139-probe-", suffix=".txt", delete=False)
    tmp.write(payload)
    tmp.close()
    tmp_path = Path(tmp.name)
    file_id = ""
    try:
        print("\n3. 上传 hello.txt ...")
        file_id = client.upload_file(tmp_path, dir_id, "hello.txt")
        print(f"   fileId={file_id}")

        print("\n4. 列新目录，确认可见 ...")
        children = client.list_files(dir_id)
        names = [c.name for c in children]
        print(f"   {names}")

        print("\n5. 取直链并测 Range ...")
        url = client.get_download_url(file_id)
        _check_range(url)

        print("\n6. 重命名 hello.txt -> hello-renamed.txt ...")
        client.rename(file_id, "hello-renamed.txt")

        print("\n7. 移动到根目录 ...")
        client.move([file_id], args.root)

        print("\n8. 清理（真删除）...")
        if args.keep:
            print("   --keep：保留现场")
        else:
            client.delete([file_id])
            file_id = ""
            client.delete([dir_id])
        print("\n✅ 全链路完成")
    except Web139Error as exc:
        print(f"\n❌ 失败：{exc}")
        if file_id and not args.keep:
            try:
                client.delete([file_id])
            except Web139Error:
                pass
        return 1
    finally:
        tmp_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
