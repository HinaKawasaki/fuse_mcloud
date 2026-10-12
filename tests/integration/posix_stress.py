#!/usr/bin/env python3
"""POSIX 强压/一致性冒烟测试（真实挂载 + 内存假云盘）。

验证在大目录、多目录、频繁列举与读写下的行为：
- ls 结果稳定、无重复、无串台；
- find 递归遍历数量正确；
- 批量新建/删除后保持一致，删除不被复活；
- 记录各阶段耗时。

运行： /home/dsh-ssh/venvs/mcloud/bin/python tests/integration/posix_stress.py
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "basement" / "scripts"))

import trio  # noqa: E402
import pyfuse3  # noqa: E402

from mapping.id_map import InodeMapper  # noqa: E402
from mapping.tree import DirectoryTree  # noqa: E402
from cache.meta_cache import MetaCache  # noqa: E402
from cache.data_cache import DataCache  # noqa: E402
from client.wrapper import CloudItem  # noqa: E402
from fuse.fuse_ops import MCloudFuseOps  # noqa: E402

ROOT_ID = "sandbox_root"
N_DIRS = 20
N_FILES_PER_DIR = 30
N_ROOT_FILES = 200


def build_tree():
    dirs = {ROOT_ID: []}
    content = {}
    for i in range(N_ROOT_FILES):
        fid = f"root_file_{i}"
        dirs[ROOT_ID].append({"id": fid, "name": f"root_{i}.txt", "size": len(f"content-{i}")})
        content[fid] = f"content-{i}".encode()
    for d in range(N_DIRS):
        did = f"dir_{d}"
        dirs[ROOT_ID].append({"id": did, "name": f"目录{d:03d}", "isDir": True})
        dirs[did] = []
        for f in range(N_FILES_PER_DIR):
            fid = f"file_{d}_{f}"
            dirs[did].append({"id": fid, "name": f"f{f:03d}.txt", "size": len(f"{d}-{f}")})
            content[fid] = f"{d}-{f}".encode()
    return dirs, content


class StressCloud:
    def __init__(self):
        self.dirs, self.content = build_tree()
        self.uploads = []
        self.copies = []

    def list_directory(self, dir_file_id, recursive=False, page_limit=200, max_items=50000):
        return [
            CloudItem(i["id"], i["name"], bool(i.get("isDir")), int(i.get("size") or 0), "", dir_file_id)
            for i in self.dirs.get(dir_file_id, [])
        ]

    def download_file(self, file_id, local_save_path):
        path = Path(local_save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.content[file_id])
        return path

    def upload_file(self, local_file_path, target_dir_id, name=None):
        import uuid

        payload = Path(local_file_path).read_bytes()
        file_name = name or Path(local_file_path).name
        new_id = f"up_{uuid.uuid4().hex[:10]}"
        self.content[new_id] = payload
        self.dirs.setdefault(target_dir_id, []).append(
            {"id": new_id, "name": file_name, "size": len(payload)}
        )
        self.uploads.append(file_name)
        return CloudItem(new_id, file_name, False, len(payload), "", target_dir_id)

    def create_folder(self, name, parent_file_id):
        import uuid

        new_id = f"dir_{uuid.uuid4().hex[:10]}"
        self.dirs.setdefault(parent_file_id, []).append({"id": new_id, "name": name, "isDir": True})
        self.dirs.setdefault(new_id, [])
        return new_id

    def batch_copy(self, file_ids, to_parent_file_id):
        import uuid

        results = []
        for file_id in file_ids:
            new_id = f"copy_{uuid.uuid4().hex[:10]}"
            src = next((i for items in self.dirs.values() for i in items if i["id"] == file_id), None)
            self.dirs.setdefault(to_parent_file_id, []).append(
                {"id": new_id, "name": src["name"] if src else file_id}
            )
            if src and file_id in self.content:
                self.content[new_id] = self.content[file_id]
            self.copies.append((file_id, to_parent_file_id))
            results.append({"fileId": file_id, "newFileId": new_id})
        return results


def main() -> int:
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="mcloud-stress-"))
    mountpoint = workdir / "mnt"
    mountpoint.mkdir()

    cloud = StressCloud()
    mapper = InodeMapper(root_file_id=ROOT_ID)
    tree = DirectoryTree(mapper=mapper)
    cache = DataCache(
        client_wrapper=cloud,
        cache_dir=workdir / "cache",
        write_back_dir=workdir / "wb",
        max_size_mb=128,
    )
    ops = MCloudFuseOps(mapper, tree, MetaCache(ttl=5.0), cache, cloud)

    options = set(pyfuse3.default_options)
    options.add("fsname=mcloud_stress")
    state, ready, errors = {}, threading.Event(), []

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
    if not ready.wait(15) or errors:
        print(f"❌ 挂载失败: {errors[:1]}")
        return 1
    for _ in range(50):
        if os.path.ismount(mountpoint):
            break
        time.sleep(0.1)
    time.sleep(0.2)

    failures = []

    def check(label, ok, detail=""):
        print(f"  {'✅' if ok else '❌'} {label} {detail}")
        if not ok:
            failures.append(label)

    def run(cmd, **kw):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)

    try:
        print("🚀 1. 大目录反复列举一致性（20 次）")
        t0 = time.monotonic()
        expected = None
        stable = True
        for _ in range(20):
            names = sorted(n for n in run(["ls", "-1", str(mountpoint)]).stdout.split("\n") if n)
            if len(names) != len(set(names)):
                stable = False
            if expected is None:
                expected = names
            elif names != expected:
                stable = False
        elapsed = time.monotonic() - t0
        check("无重复且 20 次结果一致", stable, f"(expect {len(expected)} 项, {elapsed:.2f}s)")

        print("🚀 2. 深层递归遍历 find")
        t0 = time.monotonic()
        found = run(["find", str(mountpoint), "-type", "f"]).stdout.split("\n")
        found = [f for f in found if f]
        expected_total = N_ROOT_FILES + N_DIRS * N_FILES_PER_DIR
        check("find 文件总数正确", len(found) == expected_total,
              f"({len(found)}/{expected_total}, {time.monotonic()-t0:.2f}s)")

        print("🚀 3. 批量读取校验")
        t0 = time.monotonic()
        ok = True
        for i in range(0, N_ROOT_FILES, 25):
            got = run(["cat", str(mountpoint / f"root_{i}.txt")]).stdout
            if got != f"content-{i}":
                ok = False
                break
        check("随机抽样 cat 内容正确", ok, f"({time.monotonic()-t0:.2f}s)")

        print("🚀 4. 批量新建文件")
        t0 = time.monotonic()
        run(["bash", "-c", f"for i in $(seq 1 40); do echo -n bulk$i > {mountpoint}/bulk_$i.txt; done"])
        after = sorted(n for n in run(["ls", "-1", str(mountpoint)]).stdout.split("\n") if n)
        check("新增 40 个文件", len(after) == len(expected) + 40,
              f"({len(after)} 项, {time.monotonic()-t0:.2f}s)")
        check("新建文件无重名", len(after) == len(set(after)))

        print("🚀 5. 批量删除且不复活")
        run(["bash", "-c", f"rm {mountpoint}/bulk_*.txt"])
        names = sorted(n for n in run(["ls", "-1", str(mountpoint)]).stdout.split("\n") if n)
        check("删除后回到原数量", len(names) == len(expected), f"({len(names)} 项)")
        # 再列举几次，确认墓碑阻止复活
        again = sorted(n for n in run(["ls", "-1", str(mountpoint)]).stdout.split("\n") if n)
        check("再次列举仍不复活", again == names)
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
        print(f"❌ 压测失败: {failures}")
        return 1
    print("🎉 POSIX 强压/一致性冒烟测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
