"""写回上传、目录创建、删除进回收站的离线回归测试（不需要真实挂载）。"""
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
import trio

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mapping.id_map import InodeMapper, PENDING_FILE_ID_PREFIX  # noqa: E402
from mapping.tree import DirectoryTree  # noqa: E402
from cache.meta_cache import MetaCache  # noqa: E402
from cache.data_cache import DataCache  # noqa: E402
from client.wrapper import CloudItem  # noqa: E402
from fuse.fuse_ops import MCloudFuseOps  # noqa: E402


class FakeCloud:
    def __init__(self):
        self.dirs = {"root": [{"id": "f_a", "name": "a.txt", "size": 3}]}
        self.content = {"f_a": b"old"}
        self.uploads = []
        self.copies = []

    def list_directory(self, dir_file_id, **kwargs):
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
        payload = Path(local_file_path).read_bytes()
        file_name = name or Path(local_file_path).name
        new_id = f"up_{uuid.uuid4().hex[:8]}"
        self.content[new_id] = payload
        self.dirs.setdefault(target_dir_id, []).append(
            {"id": new_id, "name": file_name, "size": len(payload)}
        )
        self.uploads.append((file_name, payload))
        return CloudItem(new_id, file_name, False, len(payload), "", target_dir_id)

    def create_folder(self, name, parent_file_id):
        new_id = f"dir_{uuid.uuid4().hex[:8]}"
        self.dirs.setdefault(parent_file_id, []).append({"id": new_id, "name": name, "isDir": True})
        self.dirs.setdefault(new_id, [])
        return new_id

    def batch_copy(self, file_ids, to_parent_file_id):
        results = []
        for file_id in file_ids:
            new_id = f"copy_{uuid.uuid4().hex[:8]}"
            src = next(
                (i for items in self.dirs.values() for i in items if i["id"] == file_id), None
            )
            self.dirs.setdefault(to_parent_file_id, []).append(
                {"id": new_id, "name": src["name"] if src else file_id, "size": src.get("size", 0) if src else 0}
            )
            self.copies.append((file_id, to_parent_file_id))
            results.append({"fileId": file_id, "newFileId": new_id})
        return results


@pytest.fixture()
def env():
    tmp = Path(tempfile.mkdtemp(prefix="mcloud-wb-"))
    cloud = FakeCloud()
    mapper = InodeMapper(root_file_id="root")
    tree = DirectoryTree(mapper=mapper)
    cache = DataCache(
        client_wrapper=cloud,
        cache_dir=tmp / "cache",
        write_back_dir=tmp / "wb",
        max_size_mb=64,
    )
    ops = MCloudFuseOps(mapper, tree, MetaCache(ttl=2.0), cache, cloud)
    try:
        yield ops, cloud, mapper, tree
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_create_write_release_uploads(env):
    ops, cloud, mapper, _ = env
    fi, attrs = trio.run(ops.create, 1, b"new.txt", 0o644, os.O_WRONLY | os.O_CREAT, None)
    assert mapper.get_by_inode(attrs.st_ino).file_id.startswith(PENDING_FILE_ID_PREFIX)

    written = trio.run(ops.write, fi.fh, 0, b"brand new")
    assert written == 9
    trio.run(ops.release, fi.fh)

    entry = mapper.get_by_inode(attrs.st_ino)
    assert not entry.file_id.startswith(PENDING_FILE_ID_PREFIX)
    assert entry.size == 9
    assert ("new.txt", b"brand new") in cloud.uploads

    # 读回：release 时已把上传内容登记为本地读缓存
    fi2 = trio.run(ops.open, attrs.st_ino, os.O_RDONLY, None)
    assert trio.run(ops.read, fi2.fh, 0, 100) == b"brand new"
    trio.run(ops.release, fi2.fh)


def test_overwrite_existing_file(env):
    ops, cloud, mapper, _ = env
    entry = mapper.get_by_path("/a.txt") or mapper.allocate_or_update(
        "f_a", "/a.txt", False, parent_inode=1, size=3
    )

    fi = trio.run(ops.open, entry.inode, os.O_WRONLY, None)
    trio.run(ops.write, fi.fh, 0, b"replaced")
    trio.run(ops.release, fi.fh)

    fresh = mapper.get_by_inode(entry.inode)
    assert fresh.file_id != "f_a"
    assert fresh.size == 8
    assert ("a.txt", b"replaced") in cloud.uploads


def test_mkdir_creates_cloud_folder(env):
    ops, cloud, mapper, _ = env
    attrs = trio.run(ops.mkdir, 1, b"docs", 0o755, None)
    entry = mapper.get_by_inode(attrs.st_ino)
    assert entry.is_dir
    assert any(i["name"] == "docs" and i.get("isDir") for i in cloud.dirs["root"])
    assert mapper.get_by_path("/docs") is entry


def test_unlink_moves_to_trash(env):
    ops, cloud, mapper, _ = env
    mapper.get_by_path("/a.txt") or mapper.allocate_or_update(
        "f_a", "/a.txt", False, parent_inode=1, size=3
    )
    trio.run(ops.unlink, 1, b"a.txt", None)
    assert mapper.get_by_path("/a.txt") is None
    assert cloud.copies == [("f_a", "dir_.TrashBin")] or (
        len(cloud.copies) == 1 and cloud.copies[0][0] == "f_a"
    )


def test_deleted_file_does_not_resurrect(env):
    """云盘没有删除 API：删除后重新列举，墓碑必须阻止原文件复活。"""
    ops, cloud, mapper, tree = env
    trio.run(ops._load_directory, 1)
    assert mapper.get_by_path("/a.txt") is not None

    trio.run(ops.unlink, 1, b"a.txt", None)
    assert mapper.get_by_path("/a.txt") is None

    # 强制重新从（仍然含有 a.txt 的）云端列举
    tree.mark_directory_stale(1)
    ops.meta_cache.invalidate_readdir(1)
    trio.run(ops._load_directory, 1)
    assert mapper.get_by_path("/a.txt") is None


def test_tombstone_persists_to_disk(tmp_path):
    from mapping.tombstones import TombstoneStore

    path = tmp_path / ".tombstones"
    first = TombstoneStore(path)
    first.add("f_deleted")
    assert "f_deleted" in TombstoneStore(path)
    assert "f_deleted" in first.snapshot()


def test_cloud_index_lag_does_not_drop_new_file(env):
    """云盘搜索索引最终一致：刚上传的文件短期内列举不到，也不能被对账清理掉。"""
    ops, cloud, mapper, tree = env
    fi, attrs = trio.run(ops.create, 1, b"lag.txt", 0o644, os.O_WRONLY | os.O_CREAT, None)
    trio.run(ops.write, fi.fh, 0, b"data")
    trio.run(ops.release, fi.fh)
    assert mapper.get_by_path("/lag.txt") is not None

    # 模拟索引尚未收录：把新文件从云端列举结果里拿掉
    uploaded = next(i for i in cloud.dirs["root"] if i["name"] == "lag.txt")
    cloud.dirs["root"] = [i for i in cloud.dirs["root"] if i["name"] != "lag.txt"]
    tree.mark_directory_stale(1)
    ops.meta_cache.invalidate_readdir(1)
    trio.run(ops._load_directory, 1)
    assert mapper.get_by_path("/lag.txt") is not None, "索引宽限期内不应被清理"

    # 索引追上后恢复正常：再次列举采用云端结果并撤销宽限期
    cloud.dirs["root"].append(uploaded)
    tree.mark_directory_stale(1)
    ops.meta_cache.invalidate_readdir(1)
    trio.run(ops._load_directory, 1)
    entry = mapper.get_by_path("/lag.txt")
    assert entry is not None
    assert entry.file_id == uploaded["id"]
    assert not tree.is_recent(entry.inode)


def test_truncate_via_setattr(env):
    import pyfuse3
    from types import SimpleNamespace

    ops, cloud, mapper, _ = env
    entry = mapper.get_by_path("/a.txt") or mapper.allocate_or_update(
        "f_a", "/a.txt", False, parent_inode=1, size=3
    )
    # pyfuse3.SetattrFields 的属性只读，测试里用等价的轻量对象替代
    fields = SimpleNamespace(update_size=True, update_mode=False)
    attr = pyfuse3.EntryAttributes()
    attr.st_size = 1

    result = trio.run(ops.setattr, entry.inode, attr, fields, None, None)
    assert result.st_size == 1
    fresh = mapper.get_by_inode(entry.inode)
    assert fresh.size == 1
    assert any(name == "a.txt" and payload == b"o" for name, payload in cloud.uploads)
