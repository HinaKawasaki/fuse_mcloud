"""MCloudFuseOps 基础回调 smoke test（getattr / lookup / readdir / 读句柄）。"""
import os
import sys
from pathlib import Path

import trio

SRC_DIR = Path(__file__).resolve().parent.parent  # 仓库根（mcloud_fuse/ 所在目录）
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcloud_fuse.cache.data_cache import DataCache  # noqa: E402
from mcloud_fuse.cache.meta_cache import MetaCache  # noqa: E402
from mcloud_fuse.client.wrapper import CloudItem  # noqa: E402
from mcloud_fuse.fuse.fuse_ops import MCloudFuseOps  # noqa: E402
from mcloud_fuse.mapping.id_map import InodeMapper  # noqa: E402
from mcloud_fuse.mapping.tree import DirectoryTree  # noqa: E402


class DummyWrapper:
    """只提供目录列举能力的替身。"""

    def list_directory(self, dir_file_id, **kwargs):
        if dir_file_id == "root_123":
            return [CloudItem("fid_doc", "doc", True, 0, "", dir_file_id)]
        if dir_file_id == "fid_doc":
            return [CloudItem("fid_test", "test.txt", False, 128, "", dir_file_id)]
        return []


class DownloadingWrapper(DummyWrapper):
    """在 DummyWrapper 之上补一个 download_file，供真实 DataCache 的读路径使用。"""

    def __init__(self, contents):
        self.contents = contents

    def download_file(self, file_id, local_save_path):
        local_save_path.parent.mkdir(parents=True, exist_ok=True)
        local_save_path.write_bytes(self.contents[file_id])
        return local_save_path


class DummyDataCache:
    def prepare_file_for_read(self, file_id, file_name):
        return Path("/tmp/fuse_ops_test") / file_name


def build_ops(data_cache=None, wrapper=None):
    mapper = InodeMapper(root_file_id="root_123")
    tree = DirectoryTree(mapper=mapper)
    return MCloudFuseOps(
        mapper=mapper,
        tree=tree,
        meta_cache=MetaCache(),
        data_cache=data_cache if data_cache is not None else DummyDataCache(),
        wrapper=wrapper if wrapper is not None else DummyWrapper(),
    )


def test_getattr_root():
    ops = build_ops()
    root_attr = trio.run(ops.getattr, 1)
    assert root_attr.st_ino == 1
    assert root_attr.st_mode & 0o40000  # S_IFDIR


def test_lookup_and_getattr_child():
    ops = build_ops()
    doc_attr = trio.run(ops.lookup, 1, b"doc")
    file_attr = trio.run(ops.lookup, doc_attr.st_ino, b"test.txt")
    assert file_attr.st_size == 128
    assert file_attr.st_mode & 0o100000  # S_IFREG


def test_missing_lookup_raises():
    ops = build_ops()
    trio.run(ops.lookup, 1, b"doc")
    doc_inode = ops.mapper.get_by_path("/doc").inode
    try:
        trio.run(ops.lookup, doc_inode, b"missing")
    except Exception:
        pass
    else:  # pragma: no cover
        raise AssertionError("missing entry should raise")


def _make_read_ops(tmp_path, payload=b"x" * 128):
    wrapper = DownloadingWrapper({"fid_test": payload})
    cache = DataCache(
        client_wrapper=wrapper,
        cache_dir=tmp_path / "cache",
        write_back_dir=tmp_path / "wb",
        max_size_mb=1,
    )
    ops = build_ops(data_cache=cache, wrapper=wrapper)
    doc_attr = trio.run(ops.lookup, 1, b"doc")
    file_attr = trio.run(ops.lookup, doc_attr.st_ino, b"test.txt")
    return ops, cache, file_attr.st_ino


def test_read_survives_cache_invalidation(tmp_path):
    """回归：读句柄打开期间缓存条目被淘汰/作废，read() 仍须返回正确内容。

    修复前 ``read()`` 每次都按路径重新 ``open``，LRU 淘汰把该路径 unlink 掉之后
    正在读的文件会突然报 EBADF（下载另一个文件就可能触发）。
    """
    payload = b"x" * 128
    ops, cache, inode = _make_read_ops(tmp_path, payload)

    finfo = trio.run(ops.open, inode, os.O_RDONLY)
    cache_path = cache.get_cache_path("fid_test")
    assert cache_path is not None
    assert cache.pinned_count("fid_test") == 1

    # 模拟并发淘汰 / 覆盖写把缓存条目作废：句柄还开着
    cache.invalidate_file_cache("fid_test")
    assert cache_path.exists(), "仍有句柄占用时只能延迟删除"

    assert trio.run(ops.read, finfo.fh, 0, len(payload)) == payload
    assert trio.run(ops.read, finfo.fh, 4, 8) == payload[4:12]

    trio.run(ops.release, finfo.fh)
    assert cache.pinned_count("fid_test") == 0
    assert not cache_path.exists(), "最后一个句柄关闭后应真正删除"


def test_concurrent_read_opens_get_independent_handles(tmp_path):
    """同一 inode 打开两次必须拿到不同 fh，否则 release 会错关别人的 fd。"""
    payload = b"x" * 128
    ops, cache, inode = _make_read_ops(tmp_path, payload)

    first = trio.run(ops.open, inode, os.O_RDONLY)
    second = trio.run(ops.open, inode, os.O_RDONLY)

    assert first.fh != second.fh
    assert cache.pinned_count("fid_test") == 2

    trio.run(ops.release, first.fh)
    assert cache.pinned_count("fid_test") == 1
    # 第一个句柄关闭后，第二个仍必须可读
    assert trio.run(ops.read, second.fh, 0, len(payload)) == payload

    trio.run(ops.release, second.fh)
    assert cache.pinned_count("fid_test") == 0
