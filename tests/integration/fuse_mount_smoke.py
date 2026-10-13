#!/usr/bin/env python3
"""真实 FUSE 挂载冒烟测试（内存假云盘，不需要云盘登录态）。

覆盖：
1. 只读：ls 无重复、子目录不串台、cat 内容正确（对应 debug/log_repeat_location.log）；
2. 写回：create/write/release 后整文件上传，内容可读；
3. 目录：mkdir 后 ls 可见；
4. 删除：unlink 改写为复制到 .TrashBin，本地消失；
5. 覆盖写：改写已有文件后内容更新。

运行： /home/dsh-ssh/venvs/mcloud/bin/python tests/integration/fuse_mount_smoke.py
"""
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "basement" / "scripts"))

import pyfuse3  # noqa: E402
import trio  # noqa: E402

from mcloud_fuse.cache.data_cache import DataCache  # noqa: E402
from mcloud_fuse.cache.meta_cache import MetaCache  # noqa: E402
from mcloud_fuse.client.wrapper import CloudItem  # noqa: E402
from mcloud_fuse.fuse.fuse_ops import MCloudFuseOps  # noqa: E402
from mcloud_fuse.mapping.id_map import InodeMapper  # noqa: E402
from mcloud_fuse.mapping.tree import DirectoryTree  # noqa: E402

ROOT_ID = "sandbox_root"


def initial_tree():
    return {
        ROOT_ID: [
            {"id": "d_docs", "name": "docs", "isDir": True},
            {"id": "f_a", "name": "a.txt", "size": len(b"hello a")},
            {"id": "f_b", "name": "b.txt", "size": len(b"hello b")},
            # 云端允许同名；POSIX 目录不允许，ls 必须只看到一个 zotero
            {"id": "f_dup1", "name": "zotero", "isDir": True},
            {"id": "f_dup2", "name": "zotero", "isDir": True},
        ],
        "d_docs": [
            {"id": "f_c", "name": "c.txt", "size": len(b"hello c")},
            {"id": "d_sub", "name": "sub", "isDir": True},
            {"id": "f_a2", "name": "a.txt", "size": len(b"inner a")},
        ],
        "d_sub": [{"id": "f_d", "name": "d.txt", "size": len(b"hello d")}],
    }


class FakeCloud:
    """可变的内存云盘：支持列举、下载、上传、建目录、复制。"""

    def __init__(self):
        self.dirs = initial_tree()
        self.content = {
            "f_a": b"hello a",
            "f_b": b"hello b",
            "f_c": b"hello c",
            "f_d": b"hello d",
            "f_a2": b"inner a",
        }
        self.list_calls = []
        self.uploads = []

    # --- 列举 ---
    def list_directory(self, dir_file_id, recursive=False, page_limit=200, max_items=50000):
        self.list_calls.append(dir_file_id)
        return [
            CloudItem(
                file_id=item["id"],
                name=item["name"],
                is_dir=bool(item.get("isDir")),
                size=int(item.get("size") or 0),
                updated_at=str(item.get("updateTime") or ""),
                parent_id=dir_file_id,
            )
            for item in self.dirs.get(dir_file_id, [])
        ]

    # --- 读 ---
    def download_file(self, file_id, local_save_path):
        path = Path(local_save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.content[file_id])
        return path

    # --- 写 ---
    def upload_file(self, local_file_path, target_dir_id, name=None):
        path = Path(local_file_path)
        payload = path.read_bytes()
        file_name = name or path.name
        new_id = f"up_{uuid.uuid4().hex[:8]}"
        self.content[new_id] = payload
        self.dirs.setdefault(target_dir_id, []).append(
            {"id": new_id, "name": file_name, "size": len(payload)}
        )
        self.uploads.append((file_name, len(payload)))
        return CloudItem(new_id, file_name, False, len(payload), "", target_dir_id)

    def create_folder(self, name, parent_file_id):
        new_id = f"dir_{uuid.uuid4().hex[:8]}"
        self.dirs.setdefault(parent_file_id, []).append(
            {"id": new_id, "name": name, "isDir": True}
        )
        self.dirs.setdefault(new_id, [])
        return new_id

    def batch_copy(self, file_ids, to_parent_file_id):
        results = []
        for file_id in file_ids:
            source = None
            for items in self.dirs.values():
                for item in items:
                    if item["id"] == file_id:
                        source = item
                        break
                if source:
                    break
            new_id = f"copy_{uuid.uuid4().hex[:8]}"
            name = source["name"] if source else file_id
            self.dirs.setdefault(to_parent_file_id, []).append(
                {"id": new_id, "name": name, "size": source.get("size", 0) if source else 0}
            )
            if source and file_id in self.content:
                self.content[new_id] = self.content[file_id]
            results.append({"fileId": file_id, "newFileId": new_id})
        return results


def build_ops(cache_root: Path):
    wrapper = FakeCloud()
    mapper = InodeMapper(root_file_id=ROOT_ID)
    tree = DirectoryTree(mapper=mapper)
    data_cache = DataCache(
        client_wrapper=wrapper,
        cache_dir=cache_root / "cache",
        write_back_dir=cache_root / "writeback",
        max_size_mb=64,
    )
    ops = MCloudFuseOps(
        mapper=mapper,
        tree=tree,
        meta_cache=MetaCache(ttl=5.0),
        data_cache=data_cache,
        wrapper=wrapper,
    )
    return ops, wrapper


def main() -> int:
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="mcloud-smoke-"))
    mountpoint = workdir / "mnt"
    mountpoint.mkdir()

    ops, cloud = build_ops(workdir)
    options = set(pyfuse3.default_options)
    options.add("fsname=mcloud_smoke")
    state = {}
    ready = threading.Event()
    errors = []

    def fuse_thread():
        try:
            pyfuse3.init(ops, str(mountpoint), options)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            ready.set()
            return

        async def _serve():
            state["token"] = trio.lowlevel.current_trio_token()
            ready.set()
            await pyfuse3.main()

        try:
            trio.run(_serve)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            try:
                pyfuse3.close()
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=fuse_thread, daemon=True)
    thread.start()
    if not ready.wait(15):
        print("❌ 挂载未就绪（init 超时）")
        return 1
    if errors:
        print(f"❌ 挂载失败: {errors[0]}")
        return 1
    for _ in range(50):
        if os.path.ismount(mountpoint):
            break
        time.sleep(0.1)
    time.sleep(0.2)

    failures = []

    def check(label, actual, expected):
        ok = actual == expected
        print(f"  {'✅' if ok else '❌'} {label}: {actual!r}")
        if not ok:
            failures.append(label)

    def run(cmd, **kwargs):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, **kwargs)

    def ls(path):
        out = run(["ls", "-1", str(path)]).stdout
        return sorted(n for n in out.split("\n") if n)

    try:
        print("🚀 1. 只读：目录列举与读取")
        root_names = ls(mountpoint)
        check("根目录无重复项", len(root_names), len(set(root_names)))
        check("根目录子项集合", root_names, sorted(["a.txt", "b.txt", "docs", "zotero"]))
        check("子目录不串台", ls(mountpoint / "docs"), sorted(["a.txt", "c.txt", "sub"]))
        check("cat 根目录文件", run(["cat", str(mountpoint / "a.txt")]).stdout, "hello a")
        check("同名文件按目录区分", run(["cat", str(mountpoint / "docs" / "a.txt")]).stdout, "inner a")
        check("深层文件读取", run(["cat", str(mountpoint / "docs" / "sub" / "d.txt")]).stdout, "hello d")
        check("stat 大小", run(["stat", "-c", "%s", str(mountpoint / "b.txt")]).stdout.strip(),
              str(len(b"hello b")))

        print("🚀 2. 写回：新建文件上传")
        run(["bash", "-c", f"echo -n 'brand new' > {mountpoint}/new.txt"])
        check("新文件出现在 ls", "new.txt" in ls(mountpoint), True)
        check("新文件内容", run(["cat", str(mountpoint / "new.txt")]).stdout, "brand new")
        check("云端已收到上传", any(name == "new.txt" for name, _ in cloud.uploads), True)

        print("🚀 3. 目录：mkdir")
        run(["mkdir", str(mountpoint / "newdir")])
        check("新目录出现在 ls", "newdir" in ls(mountpoint), True)
        check("新目录可写", run(["bash", "-c", f"echo x > {mountpoint}/newdir/f.txt"]).returncode, 0)

        print("🚀 4. 覆盖写已有文件")
        run(["bash", "-c", f"echo -n 'replaced' > {mountpoint}/a.txt"])
        check("覆盖写后内容", run(["cat", str(mountpoint / "a.txt")]).stdout, "replaced")

        print("🚀 5. 删除：移入回收站")
        run(["rm", str(mountpoint / "b.txt")])
        check("删除后本地消失", "b.txt" in ls(mountpoint), False)
        trash_ids = [i["id"] for i in cloud.dirs.get(ROOT_ID, []) if i["name"] == ".TrashBin"]
        check("云端创建了 .TrashBin", len(trash_ids) == 1, True)
        trash_children = cloud.dirs.get(trash_ids[0], []) if trash_ids else []
        check("原文件已复制进回收站", any(i["name"] == "b.txt" for i in trash_children), True)
    finally:
        token = state.get("token")
        try:
            if token is not None:
                token.run_sync_soon(pyfuse3.close)
        except Exception:  # noqa: BLE001
            pass
        thread.join(timeout=10)
        if thread.is_alive():
            subprocess.run(["fusermount3", "-u", str(mountpoint)], capture_output=True)
            thread.join(timeout=5)

    if failures:
        print(f"❌ 冒烟测试失败: {failures}")
        return 1
    print("🎉 FUSE 挂载冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
