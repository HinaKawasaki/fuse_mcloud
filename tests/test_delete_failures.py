"""删除失败记录：落盘、去重、上限、清理。"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from mcloud_fuse import delete_failures


@pytest.fixture(autouse=True)
def _isolated_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MCLOUD_DELETE_FAILURES_FILE", str(tmp_path / "failures.json"))
    yield


def test_record_and_load_roundtrip():
    delete_failures.record(
        [{"file_id": "f1", "name": "/a.txt", "error": "boom", "status": 503}]
    )
    items = delete_failures.load()
    assert len(items) == 1
    assert items[0]["file_id"] == "f1"
    assert items[0]["status"] == 503
    assert items[0]["at"] > 0


def test_record_dedupes_by_file_id():
    delete_failures.record([{"file_id": "f1", "name": "/a.txt", "error": "old"}])
    delete_failures.record([{"file_id": "f1", "name": "/a.txt", "error": "new"}])
    items = delete_failures.load()
    assert len(items) == 1 and items[0]["error"] == "new"


def test_record_keeps_only_latest_entries():
    delete_failures.record(
        {"file_id": f"f{i}", "name": f"/{i}.txt", "error": "x"}
        for i in range(delete_failures.MAX_ENTRIES + 20)
    )
    items = delete_failures.load()
    assert len(items) == delete_failures.MAX_ENTRIES
    assert items[-1]["file_id"] == f"f{delete_failures.MAX_ENTRIES + 19}"


def test_forget_removes_only_listed_ids():
    delete_failures.record(
        [{"file_id": "f1", "error": "x"}, {"file_id": "f2", "error": "y"}]
    )
    delete_failures.forget(["f1"])
    assert [item["file_id"] for item in delete_failures.load()] == ["f2"]


def test_file_is_private_and_valid_json():
    delete_failures.record([{"file_id": "f1", "error": "x"}])
    path = delete_failures.failures_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))[0]["file_id"] == "f1"


def test_corrupt_file_is_ignored():
    path = delete_failures.failures_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert delete_failures.load() == []
    # 记录新失败时要能修复文件
    delete_failures.record([{"file_id": "f1", "error": "x"}])
    assert len(delete_failures.load()) == 1


def test_clear_removes_file():
    delete_failures.record([{"file_id": "f1", "error": "x"}])
    delete_failures.clear()
    assert delete_failures.load() == []
    delete_failures.clear()  # 幂等
