#!/usr/bin/env python3
"""真实云端 + 真实 FUSE 挂载端到端验证。

默认只读：
- 官方 CLI `list` 与 wrapper.list_directory 结果一致；
- FUSE `ls` 与 API 列举一致、无重复、不串台；
- FUSE `cat` 的字节与 wrapper.download_file 完全一致（sha256）。

设置 ``MCLOUD_LIVE_WRITE=1`` 追加写入验证（会在云盘留下一个探测目录与回收站副本）：
- FUSE `mkdir` 立刻在挂载点可见；
- FUSE 新建文件后立刻可读回，且稍后能在云端索引中查到、内容一致；
- `rm` 后 FUSE 不可见。

FUSE 循环跑在主线程，检查跑在工作线程，结束时给自身发 SIGINT 触发干净卸载
（避免在非主线程调用 pyfuse3.close 导致的段错误）。

运行： MCLOUD_LIVE_WRITE=1 python3 tests/integration/real_mount_smoke.py
"""
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
if os.getenv("MCLOUD_LOG", "").upper() == "DEBUG":
    for _name in ("fuse.fuse_ops", "client.wrapper", "cache.data_cache", "mapping.tree"):
        logging.getLogger(_name).setLevel(logging.DEBUG)

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "basement" / "scripts"))

import pyfuse3  # noqa: E402
import trio  # noqa: E402

from mcloud_fuse.cache.data_cache import DataCache  # noqa: E402
from mcloud_fuse.cache.meta_cache import MetaCache  # noqa: E402
from mcloud_fuse.client.auth_manager import AuthManager  # noqa: E402
from mcloud_fuse.client.wrapper import DirectClientWrapper  # noqa: E402
from mcloud_fuse.fuse.fuse_ops import MCloudFuseOps  # noqa: E402
from mcloud_fuse.mapping.id_map import InodeMapper  # noqa: E402
from mcloud_fuse.mapping.tombstones import TombstoneStore  # noqa: E402
from mcloud_fuse.mapping.tree import DirectoryTree  # noqa: E402

TRASH = ".TrashBin"
PROBE_DIR = "_mcloud_fuse_probe"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cli_list(root_id: str):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "basement" / "scripts" / "main.py"),
         "list", root_id, "--limit", "200"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    names = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("record") == "file":
            names.append(row.get("name", ""))
    return sorted(n for n in names if n and n != TRASH), proc.returncode


def wait_for(predicate, timeout=90.0, interval=2.0):
    """轮询等待云盘搜索索引追上写入（最终一致）。"""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        value = predicate()
        if value:
            return value, time.monotonic() - start
        time.sleep(interval)
    return None, time.monotonic() - start


def _is_keyboard_interrupt(exc: BaseException) -> bool:
    """判断异常是否为 KeyboardInterrupt（含被 trio 包进 BaseExceptionGroup 的情况）。

    用 duck typing 判断 ``.exceptions``，避免在 Python 3.10 上引用 BaseExceptionGroup。
    """
    if isinstance(exc, KeyboardInterrupt):
        return True
    children = getattr(exc, "exceptions", None)
    if children:
        return all(_is_keyboard_interrupt(child) for child in children)
    return False


def run_checks(run, mountpoint, wrapper, root_id, api_items, results, live_write):
    failures = []

    def check(label, ok, detail=""):
        print(f"  {'✅' if ok else '❌'} {label} {detail}", flush=True)
        if not ok:
            failures.append(label)

    try:
        api_names = sorted(i.name for i in api_items if i.name != TRASH)
        print("🚀 2. FUSE ls 与 API 一致性", flush=True)
        ls_names = sorted(n for n in os.listdir(mountpoint) if n != TRASH)
        check("FUSE 列举与 API 一致", ls_names == api_names, f"{ls_names}")
        check("无重复项", len(ls_names) == len(set(ls_names)))
        check(".TrashBin 已隐藏", TRASH not in os.listdir(mountpoint))

        print("🚀 3. 逐文件读取字节一致性（sha256）", flush=True)
        files = [i for i in api_items if not i.is_dir and i.name != TRASH]
        for item in files:
            fuse_bytes = run(["cat", str(mountpoint / item.name)], binary=True).stdout
            api_path = Path(tempfile.gettempdir()) / f"{item.file_id}.api"
            wrapper.download_file(item.file_id, api_path)
            check(f"内容一致: {item.name}",
                  sha256_bytes(fuse_bytes) == sha256_bytes(api_path.read_bytes()),
                  f"({len(fuse_bytes)}B)")

        for d in [i for i in api_items if i.is_dir and i.name != TRASH][:5]:
            sub_api = sorted(i.name for i in wrapper.list_directory(d.file_id) if i.name != TRASH)
            sub_fuse = sorted(n for n in os.listdir(mountpoint / d.name) if n != TRASH)
            check(f"子目录一致: {d.name}", sub_api == sub_fuse, f"{sub_fuse}")

        if live_write:
            print("🚀 4. 写入验证（真实云盘）", flush=True)
            probe = mountpoint / PROBE_DIR
            if not probe.exists():
                run(["mkdir", str(probe)])
            check("mkdir 后 FUSE 立即可见", probe.is_dir())

            # 云盘搜索索引最终一致：轮询确认它出现在 API 列举里
            item, waited = wait_for(lambda: next(
                (i for i in wrapper.list_directory(root_id) if i.name == PROBE_DIR), None))
            check("mkdir 稍后云端索引可见", item is not None, f"({waited:.1f}s)")
            if item is None:
                item = next(
                    (i for i in wrapper.list_directory(root_id) if i.name == PROBE_DIR), None)

            content = "hello from mcloud_fuse real mount"
            payload = (content + "\n").encode()
            fname = f"hello_{int(time.time())}.txt"
            target = probe / fname
            proc = run(["bash", "-c", f"printf '%s\\n' '{content}' > '{target}'"])
            check("写入命令成功", proc.returncode == 0,
                  f"(rc={proc.returncode} err={proc.stderr!r})")

            # close() 触发的上传在后台进行：open/read 会等它完成，这里应立刻能读回
            read_back = run(["cat", str(target)], binary=True).stdout
            check("新建文件立刻可读回", read_back == payload, f"({len(read_back)}B)")

            if item is not None:
                uploaded, waited2 = wait_for(lambda: next(
                    (i for i in wrapper.list_directory(item.file_id) if i.name == fname), None))
                check("新文件稍后云端索引可见", uploaded is not None, f"({waited2:.1f}s)")
                if uploaded is not None:
                    got = Path(tempfile.gettempdir()) / "mcloud_roundtrip.txt"
                    wrapper.download_file(uploaded.file_id, got)
                    check("云端内容一致", got.read_bytes() == payload,
                          f"({got.stat().st_size}B)")

            run(["rm", str(target)])
            check("删除后 FUSE 不可见", not os.path.exists(str(target)))
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        failures.append(f"检查异常: {exc}")
    finally:
        results.extend(failures)
        os.kill(os.getpid(), signal.SIGINT)


def main() -> int:
    live_write = bool(os.getenv("MCLOUD_LIVE_WRITE"))

    auth = AuthManager()
    wrapper = DirectClientWrapper(auth)
    root_id = wrapper.ensure_workspace()
    print(f"🚀 真实沙箱根 fileId = {root_id}", flush=True)

    api_items = wrapper.list_directory(root_id, recursive=False)
    api_names = sorted(i.name for i in api_items if i.name != TRASH)
    print(f"   API 直接子项: {api_names}", flush=True)

    print("🚀 1. 官方 CLI 与 wrapper 列举一致性", flush=True)
    cli_names, code = cli_list(root_id)
    ok_cli = code == 0 and cli_names == api_names
    print(f"  {'✅' if ok_cli else '❌'} CLI 与 wrapper 列举一致 "
          f"cli={cli_names} api={api_names}", flush=True)

    workdir = Path(tempfile.mkdtemp(prefix="mcloud-real-"))
    mountpoint = workdir / "mnt"
    mountpoint.mkdir()

    mapper = InodeMapper(root_file_id=root_id)
    tombstones = TombstoneStore(workdir / "cache" / ".tombstones")
    tree = DirectoryTree(mapper=mapper, tombstones=tombstones)
    cache = DataCache(
        client_wrapper=wrapper,
        cache_dir=workdir / "cache",
        write_back_dir=workdir / "wb",
        max_size_mb=256,
    )
    ops = MCloudFuseOps(mapper, tree, MetaCache(ttl=5.0), cache, wrapper)

    options = set(pyfuse3.default_options)
    options.add("fsname=mcloud_real")

    try:
        pyfuse3.init(ops, str(mountpoint), options)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 挂载失败: {exc}")
        return 1

    def run(cmd, binary=False):
        return subprocess.run(cmd, capture_output=True, text=not binary, timeout=180)

    results = []
    worker = threading.Thread(
        target=run_checks,
        args=(run, mountpoint, wrapper, root_id, api_items, results, live_write),
        daemon=True,
    )
    worker.start()

    try:
        trio.run(pyfuse3.main)
    except BaseException as exc:  # noqa: BLE001
        # 工作线程用 SIGINT 触发干净卸载；trio 会把它包进 BaseExceptionGroup。
        if not _is_keyboard_interrupt(exc):
            raise
    finally:
        try:
            pyfuse3.close()
        except Exception:  # noqa: BLE001
            pass
    worker.join(timeout=120)

    if not ok_cli:
        results.append("CLI 一致性")
    if results:
        print(f"❌ 真实挂载验证失败: {results}")
        return 1
    print("🎉 真实云端 FUSE 端到端验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
