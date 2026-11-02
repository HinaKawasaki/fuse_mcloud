"""资源上限、懒加载与并发锁。"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest
from fakes import FakeCloud

from mcloud_fuse.cache.data_cache import DataCache
from mcloud_fuse.cache.meta_cache import MetaCache
from mcloud_fuse.lock import CacheLock, CacheLockError
from mcloud_fuse.mapping.id_map import InodeEntry


def make_cache(tmp_path: Path, **kwargs) -> DataCache:
    defaults = dict(
        client_wrapper=FakeCloud(),
        cache_dir=tmp_path / "cache",
        write_back_dir=tmp_path / "wb",
        max_size_mb=64,
        max_write_back_mb=1,
        min_free_mb=0,
        journal_db=tmp_path / "journal.sqlite3",
    )
    defaults.update(kwargs)
    return DataCache(**defaults)


def test_write_back_limit_returns_enospc(tmp_path: Path):
    cache = make_cache(tmp_path)
    with pytest.raises(OSError) as excinfo:
        cache.ensure_write_back_capacity(2 * 1024 * 1024)
    assert excinfo.value.errno == errno.ENOSPC
    assert "MCLOUD_MAX_WRITE_BACK_MB" in str(excinfo.value)
    cache.close()


def test_create_write_buffer_checks_initial_size(tmp_path: Path):
    cache = make_cache(tmp_path)
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(OSError) as excinfo:
        cache.create_write_buffer(
            "inode-big", initial_path=source, target_dir_id="root", file_name="big.bin"
        )
    assert excinfo.value.errno == errno.ENOSPC
    assert not cache.pending_uploads()
    cache.close()


def test_sparse_partial_uses_allocated_blocks(tmp_path: Path):
    cache = make_cache(tmp_path, max_size_mb=64)
    cache.open_lazy("fid_big", "big.bin", 1024 * 1024 * 1024)
    assert cache.cache_usage_bytes() < 1024 * 1024, "稀疏壳不能按逻辑大小计入缓存"
    cache.release_read("fid_big")
    cache.close()


def test_meta_cache_is_lru_bounded():
    cache = MetaCache(ttl=60, max_entries=2)

    def entry(inode: int) -> InodeEntry:
        return InodeEntry(inode=inode, file_id=f"f{inode}", path=f"/f{inode}", is_dir=False)

    cache.put_attr(1, entry(1))
    cache.put_attr(2, entry(2))
    assert cache.get_attr(1) is not None  # 1 变成最近使用
    cache.put_attr(3, entry(3))
    assert cache.get_attr(2) is None, "最久未使用的 2 应被淘汰"
    assert cache.get_attr(1) is not None
    assert cache.get_attr(3) is not None


def test_directory_cache_is_bounded():
    cache = MetaCache(ttl=60, max_entries=2)
    cache.put_readdir(1, [])
    cache.put_readdir(2, [])
    cache.put_readdir(3, [])
    assert cache.get_readdir(1) is None
    assert cache.get_readdir(3) is not None


def test_cache_lock_rejects_second_mount(tmp_path: Path):
    first = CacheLock(tmp_path)
    second = CacheLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(CacheLockError, match="另一个 mcloud-fuse"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
