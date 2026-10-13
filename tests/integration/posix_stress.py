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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
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
N_DIRS = 20
N_FILES_PER_DIR = 30
N_ROOT_FILES = 200

# 把每个文件的体积填充到该字节数（默认 0 = 保持原始的小内容）。
# 配合 MCLOUD_STRESS_CACHE_MB=1 使用，可以强制在读的过程中反复触发 LRU 淘汰，
# 用来回归「淘汰把正在读的文件删掉」这类竞态。
PAD_BYTES = int(os.environ.get("MCLOUD_STRESS_FILE_BYTES", "0"))


def file_text(base: str) -> str:
    if PAD_BYTES <= len(base):
        return base
    return (base * (PAD_BYTES // len(base) + 1))[:PAD_BYTES]


def build_tree():
    dirs = {ROOT_ID: []}
    content = {}
    for i in range(N_ROOT_FILES):
        fid = f"root_file_{i}"
        text = file_text(f"content-{i}")
        dirs[ROOT_ID].append({"id": fid, "name": f"root_{i}.txt", "size": len(text)})
        content[fid] = text.encode()
    for d in range(N_DIRS):
        did = f"dir_{d}"
        dirs[ROOT_ID].append({"id": did, "name": f"目录{d:03d}", "isDir": True})
        dirs[did] = []
        for f in range(N_FILES_PER_DIR):
            fid = f"file_{d}_{f}"
            text = file_text(f"{d}-{f}")
            dirs[did].append({"id": fid, "name": f"f{f:03d}.txt", "size": len(text)})
            content[fid] = text.encode()
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

    def download_range(self, file_id, local_save_path, offset, length, expected_total=None):
        """模拟支持 HTTP Range 的下载直链（按需读取路径会用到这里）。"""
        payload = self.content[file_id]
        chunk = payload[offset : offset + length]
        path = Path(local_save_path)
        with open(path, "r+b") as handle:
            handle.seek(offset)
            handle.write(chunk)
        return len(chunk)

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
    # 缓存限额可用环境变量调小（如 MCLOUD_STRESS_CACHE_MB=1）来强制在读的过程中
    # 触发 LRU 淘汰，用来复现「淘汰把正在读的文件删掉」这类竞态。
    cache_mb = int(os.environ.get("MCLOUD_STRESS_CACHE_MB", "128"))
    # 按需读阈值同样可用环境变量压小（如 MCLOUD_STRESS_EAGER_LIMIT=1024），
    # 让所有文件都走 Range 分片读取，从而真实内核下覆盖按需读路径。
    eager_raw = os.environ.get("MCLOUD_STRESS_EAGER_LIMIT", "").strip()
    eager_limit = int(eager_raw) if eager_raw else None
    cache = DataCache(
        client_wrapper=cloud,
        cache_dir=workdir / "cache",
        write_back_dir=workdir / "wb",
        max_size_mb=cache_mb,
        eager_download_limit_bytes=eager_limit,
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
        # 逐个读完全部根文件：配合小缓存限额时，会在读的过程中反复触发 LRU 淘汰
        for i in range(N_ROOT_FILES):
            got = run(["cat", str(mountpoint / f"root_{i}.txt")]).stdout
            if got != file_text(f"content-{i}"):
                ok = False
                break
        check("顺序 cat 全部根文件内容正确", ok, f"({time.monotonic()-t0:.2f}s)")

        print("🚀 3b. 并发读取（小缓存下会边读边淘汰）")
        t0 = time.monotonic()
        total_expected = sum(len(file_text(f"content-{i}")) for i in range(N_ROOT_FILES))
        proc = run(
            [
                "bash",
                "-c",
                f"ls {mountpoint}/root_*.txt | xargs -P 16 -n 1 cat | wc -c",
            ]
        )
        got_bytes = int(proc.stdout.strip() or 0)
        # 并发 cat 的输出顺序不确定，但字节总数是确定的：
        # 只要有一个读被 LRU 淘汰打断（旧实现会报 EBADF），总数就会少。
        check(
            "并发 cat 全部根文件字节数一致",
            proc.returncode == 0 and got_bytes == total_expected,
            f"({got_bytes}/{total_expected} bytes, {time.monotonic()-t0:.2f}s)",
        )

        print("🚀 3c. 打开后延迟读取（复现淘汰竞态）")
        t0 = time.monotonic()
        # 真实应用常常是 open() 之后先干点别的再 read()。此时若缓存被其它下载挤爆，
        # 旧实现（read 时按路径重新 open）会撞上 ENOENT/EBADF；持有 fd 的实现不受影响。
        reader_code = (
            "import sys, time\n"
            "with open(sys.argv[1], 'rb') as fh:\n"
            "    time.sleep(float(sys.argv[2]))\n"
            "    data = fh.read()\n"
            "print(len(data))\n"
        )
        readers = [
            subprocess.Popen(
                [sys.executable, "-c", reader_code, str(mountpoint / f"root_{i}.txt"), "2.0"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(4)
        ]
        time.sleep(0.4)
        # 淘汰压力：只读 root_100~root_199（1.6MiB > 1MiB 限额），刻意避开上面 4 个
        # 读者持有的 root_0~root_3——否则它们被淘汰后又会被压力重新下载回同一路径，
        # 旧实现反而「侥幸」通过，复现不出问题。
        run([
            "bash",
            "-c",
            f"ls {mountpoint}/root_1[0-9][0-9].txt | xargs -P 16 -n 1 cat > /dev/null",
        ])

        reader_ok = True
        detail = []
        for index, proc in enumerate(readers):
            out, err = proc.communicate(timeout=60)
            expected_len = len(file_text(f"content-{index}"))
            if proc.returncode != 0 or out.strip() != str(expected_len):
                reader_ok = False
                detail.append(f"#{index} rc={proc.returncode} out={out.strip()!r} {err.strip()[:80]}")
        check(
            "延迟读取的 4 个句柄仍能读完",
            reader_ok,
            detail[0] if detail else f"({time.monotonic()-t0:.2f}s)",
        )

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
