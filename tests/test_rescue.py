"""关机遇失抢救：只搬完整 payload、限量、可恢复。"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeCloud

from mcloud_fuse.cache import data_cache as data_cache_module
from mcloud_fuse.cache.data_cache import DataCache
from mcloud_fuse.journal import BUILDING, RESCUED, WritebackJournal, rescue_dir


def make_cache(tmp_path: Path, cloud=None, **kwargs) -> DataCache:
    defaults = dict(
        client_wrapper=cloud or FakeCloud(),
        cache_dir=tmp_path / "cache",
        write_back_dir=tmp_path / "wb",
        max_size_mb=64,
        max_write_back_mb=16,
        min_free_mb=0,
        journal_db=tmp_path / "journal.sqlite3",
        rescue_min_free_mb=0,
        rescue_max_mb=64,
        rescue_file_max_mb=8,
        rescue_timeout_sec=10,
    )
    defaults.update(kwargs)
    return DataCache(**defaults)


def make_pending(cache: DataCache, key: str = "inode-1", payload: bytes = b"hello") -> Path:
    path = cache.create_write_buffer(key, target_dir_id="root", file_name=f"{key}.txt")
    path.write_bytes(payload)
    cache.mark_write_ready(key)
    return path


def test_rescue_copies_pending_payload_to_state_dir(tmp_path: Path, monkeypatch):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(
        data_cache_module,
        "is_volatile_writeback",
        lambda path: Path(path) == cache.write_back_dir,
    )
    source = make_pending(cache, "inode-1", b"payload-1")
    assert "inode-1" in cache.pending_uploads()

    result = cache.rescue_pending_uploads()

    assert result.count == 1
    assert result.total_bytes == len(b"payload-1")
    assert not source.exists(), "抢救后原 tmpfs 文件应移走/由 rename 替换"
    journal = WritebackJournal(tmp_path / "journal.sqlite3")
    entry = journal.get("inode-1")
    journal.close()
    assert entry is not None
    assert entry.state == RESCUED
    assert Path(entry.payload_path).exists()
    assert str(rescue_dir(cache.write_back_dir)) in entry.payload_path
    assert "inode-1" in cache.pending_uploads(), "rescued 仍算待传"
    cache.close()


def test_rescue_skips_when_not_tmpfs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(data_cache_module, "is_volatile_writeback", lambda path: False)
    cache = make_cache(tmp_path)
    make_pending(cache)
    result = cache.rescue_pending_uploads()
    assert result.count == 0
    assert "不在 tmpfs" in result.reason
    cache.close()


def test_rescue_skips_building_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(data_cache_module, "is_volatile_writeback", lambda path: True)
    cache = make_cache(tmp_path)
    buffer = cache.create_write_buffer("inode-2", target_dir_id="root", file_name="half.txt")
    buffer.write_bytes(b"half")
    # 没有 mark_write_ready → 仍是 building
    result = cache.rescue_pending_uploads()
    assert result.count == 0
    assert result.reason == "没有待抢救文件"
    journal = WritebackJournal(tmp_path / "journal.sqlite3")
    assert journal.get("inode-2").state == BUILDING
    journal.close()
    cache.close()


def test_rescue_skips_oversized_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(data_cache_module, "is_volatile_writeback", lambda path: True)
    cache = make_cache(tmp_path, rescue_file_max_mb=1)
    make_pending(cache, "inode-3", b"x" * (2 * 1024 * 1024))
    result = cache.rescue_pending_uploads(force=True)
    assert result.count == 0
    assert any("单文件" in item[3] for item in result.skipped)
    cache.close()


def test_rescue_timeout_skips(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(data_cache_module, "is_volatile_writeback", lambda path: True)
    cache = make_cache(tmp_path, rescue_timeout_sec=0)
    make_pending(cache, "inode-4")
    result = cache.rescue_pending_uploads(force=True)
    assert result.count == 0
    assert any("超时" in item[3] for item in result.skipped)
    cache.close()


def test_rescued_file_is_recovered_on_next_start(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(data_cache_module, "is_volatile_writeback", lambda path: True)
    cloud = FakeCloud()
    first = make_cache(tmp_path, cloud=cloud)
    make_pending(first, "inode-5", b"rescued-data")
    first.rescue_pending_uploads(force=True)
    first.close()

    second = make_cache(tmp_path, cloud=cloud)
    assert second.recover_pending_uploads() == 1
    assert ("inode-5.txt", b"rescued-data") in cloud.uploads
    assert not second.pending_uploads()
    second.close()


def test_recovery_falls_back_to_rescue_path(tmp_path: Path, monkeypatch):
    """日志更新前崩溃：payload 已在 rescue 目录，但日志仍指向 tmpfs。"""
    monkeypatch.setattr(data_cache_module, "is_volatile_writeback", lambda path: True)
    cloud = FakeCloud()
    first = make_cache(tmp_path, cloud=cloud)
    source = make_pending(first, "inode-6", b"orphan-rescue")
    target_dir = rescue_dir(first.write_back_dir)
    target_dir.mkdir(parents=True)
    fallback = target_dir / "inode-6.wb"
    fallback.write_bytes(source.read_bytes())
    source.unlink()
    first.close()

    second = make_cache(tmp_path, cloud=cloud)
    assert second.recover_pending_uploads() == 1
    assert ("inode-6.txt", b"orphan-rescue") in cloud.uploads
    assert not second.pending_uploads()
    second.close()
