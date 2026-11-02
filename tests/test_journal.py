"""SQLite 写回日志：持久化、状态机、损坏重建与旧 JSON 迁移。"""

from __future__ import annotations

import json
from pathlib import Path

from fakes import FakeCloud

from mcloud_fuse.cache.data_cache import DataCache
from mcloud_fuse.journal import (
    BUILDING,
    LOST,
    PENDING,
    JournalEntry,
    WritebackJournal,
    default_journal_path,
)


def entry(key: str = "k1", state: str = PENDING) -> JournalEntry:
    return JournalEntry(
        key=key,
        target_dir_id="root",
        file_name="a.txt",
        payload_path=f"/tmp/wb/{key}.wb",
        state=state,
    )


def test_upsert_get_and_counts(tmp_path: Path):
    journal = WritebackJournal(tmp_path / "journal.sqlite3")
    journal.upsert(entry())
    journal.upsert(entry("k2", BUILDING))

    loaded = journal.get("k1")
    assert loaded is not None
    assert loaded.file_name == "a.txt"
    assert journal.pending() == {
        "k1": {
            "target_dir_id": "root",
            "file_name": "a.txt",
            "payload_path": "/tmp/wb/k1.wb",
        }
    }
    assert journal.counts() == {PENDING: 1, BUILDING: 1}
    assert len(journal) == 2
    journal.close()


def test_state_transitions_and_remove(tmp_path: Path):
    path = tmp_path / "journal.sqlite3"
    journal = WritebackJournal(path)
    journal.upsert(entry())
    journal.set_state("k1", LOST, last_error="payload missing", size=12)
    loaded = journal.get("k1")
    assert loaded is not None
    assert loaded.state == LOST
    assert loaded.last_error == "payload missing"
    assert loaded.size == 12
    assert journal.lost()["k1"]["file_name"] == "a.txt"
    assert journal.remove("k1") is True
    assert journal.remove("k1") is False
    journal.close()


def test_persists_across_connections(tmp_path: Path):
    path = tmp_path / "journal.sqlite3"
    first = WritebackJournal(path)
    first.upsert(entry())
    first.close()

    second = WritebackJournal(path)
    assert "k1" in second.pending()
    second.close()

    assert path.stat().st_mode & 0o777 == 0o600


def test_corrupt_database_is_rebuilt(tmp_path: Path):
    path = tmp_path / "journal.sqlite3"
    path.write_bytes(b"this is not sqlite")
    journal = WritebackJournal(path)
    journal.upsert(entry())
    assert "k1" in journal.pending()
    journal.close()
    assert list(tmp_path.glob("journal.sqlite3.corrupt-*"))


def test_default_path_follows_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MCLOUD_JOURNAL_DB", str(tmp_path / "custom.sqlite3"))
    assert default_journal_path() == tmp_path / "custom.sqlite3"


def test_default_path_is_scoped_by_writeback_dir(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MCLOUD_JOURNAL_DB", raising=False)
    one = default_journal_path(tmp_path / "a" / "writeback")
    two = default_journal_path(tmp_path / "b" / "writeback")
    assert one != two
    assert one.parent == two.parent


def test_data_cache_migrates_legacy_json(tmp_path: Path):
    write_back = tmp_path / "wb"
    write_back.mkdir()
    (write_back / "inode-1.wb").write_bytes(b"legacy")
    (write_back / "writeback_journal.json").write_text(
        json.dumps(
            {
                "inode-1": {
                    "target_dir_id": "root",
                    "file_name": "legacy.txt",
                    "base_file_id": "old",
                    "base_updated_at": "2026-01-01T00:00:00Z",
                }
            }
        ),
        encoding="utf-8",
    )

    cache = DataCache(
        client_wrapper=FakeCloud(),
        cache_dir=tmp_path / "cache",
        write_back_dir=write_back,
        max_size_mb=64,
        journal_db=tmp_path / "journal.sqlite3",
    )
    assert "inode-1" in cache.pending_uploads()
    assert (write_back / "writeback_journal.json.migrated").exists()
    assert not (write_back / "writeback_journal.json").exists()
    cache.close()


def test_uncommitted_write_is_not_uploaded_after_crash(tmp_path: Path):
    """只创建、没 fsync/release 的写回文件是半成品，崩溃恢复不能上传。"""
    cloud = FakeCloud()
    kwargs = dict(
        client_wrapper=cloud,
        cache_dir=tmp_path / "cache",
        write_back_dir=tmp_path / "wb",
        max_size_mb=64,
        journal_db=tmp_path / "journal.sqlite3",
    )
    first = DataCache(**kwargs)
    first.create_write_buffer("inode-7", target_dir_id="root", file_name="half.txt")
    assert not first.pending_uploads(), "创建阶段不应进入 pending"
    first.close()

    second = DataCache(**kwargs)
    assert second.recover_pending_uploads() == 0
    assert not second.pending_uploads()
    assert "inode-7" in second.lost_uploads()
    assert not cloud.uploads, "半成品绝不能上传"
    second.close()


def test_missing_payload_is_marked_lost(tmp_path: Path):
    cloud = FakeCloud()
    kwargs = dict(
        client_wrapper=cloud,
        cache_dir=tmp_path / "cache",
        write_back_dir=tmp_path / "wb",
        max_size_mb=64,
        journal_db=tmp_path / "journal.sqlite3",
    )
    first = DataCache(**kwargs)
    buffer = first.create_write_buffer("inode-9", target_dir_id="root", file_name="gone.txt")
    first.mark_write_ready("inode-9")
    assert "inode-9" in first.pending_uploads()
    buffer.unlink()
    first.close()

    second = DataCache(**kwargs)
    assert second.recover_pending_uploads() == 0
    assert not second.pending_uploads()
    assert "inode-9" in second.lost_uploads()
    second.close()


def test_journal_cli_list_and_forget(tmp_path: Path, capsys):
    from mcloud_fuse import journal_cli

    db = tmp_path / "journal.sqlite3"
    payload = tmp_path / "wb" / "k1.wb"
    payload.parent.mkdir()
    payload.write_bytes(b"x")
    journal = WritebackJournal(db)
    journal.upsert(
        JournalEntry(
            key="k1",
            target_dir_id="root",
            file_name="a.txt",
            payload_path=str(payload),
            state=LOST,
            size=1,
        )
    )
    journal.close()

    assert (
        journal_cli.main(
            ["--journal", str(db), "--cache-dir", str(tmp_path / "cache"), "list", "--json"]
        )
        == 0
    )
    assert '"key": "k1"' in capsys.readouterr().out

    assert (
        journal_cli.main(
            [
                "--journal",
                str(db),
                "--cache-dir",
                str(tmp_path / "cache"),
                "forget",
                "--lost",
                "--purge",
            ]
        )
        == 0
    )
    assert not payload.exists()
    check = WritebackJournal(db)
    assert check.entries() == []
    check.close()


def test_journal_cli_gc_removes_orphan_and_stale(tmp_path: Path):
    import os
    import time

    from mcloud_fuse import journal_cli

    db = tmp_path / "journal.sqlite3"
    write_back = tmp_path / "cache" / "writeback"
    write_back.mkdir(parents=True)
    orphan = write_back / "orphan.wb"
    orphan.write_bytes(b"x")
    old = time.time() - 48 * 3600
    os.utime(orphan, (old, old))

    journal = WritebackJournal(db)
    journal.upsert(
        JournalEntry(
            key="old-building",
            target_dir_id="root",
            file_name="old.txt",
            payload_path=str(write_back / "old.wb"),
            state=BUILDING,
            updated_at=old,
        )
    )
    journal.close()

    assert (
        journal_cli.main(
            [
                "--journal",
                str(db),
                "--cache-dir",
                str(tmp_path / "cache"),
                "gc",
                "--orphan-hours",
                "1",
                "--stale-hours",
                "1",
            ]
        )
        == 0
    )
    assert not orphan.exists()
    check = WritebackJournal(db)
    assert check.entries() == []
    check.close()


def test_uploading_entry_is_deduped_when_remote_matches(tmp_path: Path):
    """上传中崩溃：远端已有同名同大小文件时不再重传。"""
    cloud = FakeCloud()
    kwargs = dict(
        client_wrapper=cloud,
        cache_dir=tmp_path / "cache",
        write_back_dir=tmp_path / "wb",
        max_size_mb=64,
        journal_db=tmp_path / "journal.sqlite3",
    )
    first = DataCache(**kwargs)
    buffer = first.create_write_buffer("inode-8", target_dir_id="root", file_name="same.txt")
    buffer.write_bytes(b"payload")
    first.mark_write_ready("inode-8")
    first.mark_write_uploading("inode-8")
    first.close()

    # 模拟服务端其实已经存好，只是进程没来得及清日志
    cloud.dirs["root"].append({"id": "remote_1", "name": "same.txt", "size": len(b"payload")})
    second = DataCache(**kwargs)
    assert second.recover_pending_uploads() == 0
    assert not second.pending_uploads()
    assert not cloud.uploads
    second.close()


def test_uploading_entry_without_remote_match_is_retried(tmp_path: Path):
    cloud = FakeCloud()
    kwargs = dict(
        client_wrapper=cloud,
        cache_dir=tmp_path / "cache",
        write_back_dir=tmp_path / "wb",
        max_size_mb=64,
        journal_db=tmp_path / "journal.sqlite3",
    )
    first = DataCache(**kwargs)
    buffer = first.create_write_buffer("inode-9", target_dir_id="root", file_name="new.txt")
    buffer.write_bytes(b"payload")
    first.mark_write_ready("inode-9")
    first.mark_write_uploading("inode-9")
    first.close()

    second = DataCache(**kwargs)
    assert second.recover_pending_uploads() == 1
    assert ("new.txt", b"payload") in cloud.uploads
    assert not second.pending_uploads()
    second.close()
