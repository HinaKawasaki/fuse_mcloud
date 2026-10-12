"""MCloudFuseOps 基础回调 smoke test（getattr / lookup / readdir）。"""
import sys
from pathlib import Path

import trio

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cache.meta_cache import MetaCache  # noqa: E402
from client.wrapper import CloudItem  # noqa: E402
from fuse.fuse_ops import MCloudFuseOps  # noqa: E402
from mapping.id_map import InodeMapper  # noqa: E402
from mapping.tree import DirectoryTree  # noqa: E402


class DummyWrapper:
    """只提供目录列举能力的替身。"""

    def list_directory(self, dir_file_id, **kwargs):
        if dir_file_id == "root_123":
            return [CloudItem("fid_doc", "doc", True, 0, "", dir_file_id)]
        if dir_file_id == "fid_doc":
            return [CloudItem("fid_test", "test.txt", False, 128, "", dir_file_id)]
        return []


class DummyDataCache:
    def prepare_file_for_read(self, file_id, file_name):
        return Path("/tmp/fuse_ops_test") / file_name


def build_ops():
    mapper = InodeMapper(root_file_id="root_123")
    tree = DirectoryTree(mapper=mapper)
    return MCloudFuseOps(
        mapper=mapper,
        tree=tree,
        meta_cache=MetaCache(),
        data_cache=DummyDataCache(),
        wrapper=DummyWrapper(),
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
