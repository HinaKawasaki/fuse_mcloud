"""FUSE/缓存层到 Web 后端的适配测试（不需要 pyfuse3 / trio / 网络）。"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, List

import pytest

from mcloud_fuse.client.web139 import WebFile
from mcloud_fuse.client.wrapper import DirectClientWrapper


class FakeAuth:
    class Token:
        @staticmethod
        def is_expired() -> bool:
            return False

    token = Token()


class FakeClient:
    def __init__(self):
        self.auth = FakeAuth()
        self.tree = {
            "/": [
                {"fileId": "d1", "name": "docs", "type": "folder", "size": 0},
                {"fileId": "f1", "name": "a.txt", "type": "file", "size": 3},
            ],
            "d1": [{"fileId": "f2", "name": "b.txt", "type": "file", "size": 5}],
        }
        self.calls: List[Any] = []
        self.updated_at = "2026-10-01T00:00:00+08:00"
        self.upload_kwargs: List[dict] = []

    def iter_files(self, parent_id: str = "/", page_size: int = 100):
        for item in self.tree.get(parent_id, []):
            yield WebFile.from_api(item, parent_id)

    def find_child(self, parent_id: str, name: str):
        for item in self.tree.get(parent_id, []):
            if item["name"] == name:
                return WebFile.from_api(item, parent_id)
        return None

    def create_folder(self, parent_id, name):
        self.calls.append(("mkdir", parent_id, name))
        return "d2"

    def rename(self, file_id, new_name):
        self.calls.append(("rename", file_id, new_name))

    def move(self, file_ids, to_parent_id):
        self.calls.append(("move", list(file_ids), to_parent_id))

    def delete(self, file_ids, *, permanent=True):
        self.calls.append(("delete", list(file_ids), permanent))

    def get_file(self, file_id):
        return WebFile.from_api(
            {
                "fileId": file_id,
                "name": "x.txt",
                "type": "file",
                "size": 5,
                "updatedAt": self.updated_at,
            },
            "d1",
        )

    def upload_file(self, path, parent_id, name, **kwargs):
        self.calls.append(("upload", Path(path).name, parent_id, name))
        self.upload_kwargs.append(kwargs)
        return "f_new"

    def get_download_url(self, file_id):
        return f"https://cdn/{file_id}"


def make_wrapper() -> DirectClientWrapper:
    return DirectClientWrapper(client=FakeClient())


def test_list_directory_maps_cloud_items():
    wrapper = make_wrapper()
    items = wrapper.list_directory("/")
    assert [(i.file_id, i.name, i.is_dir, i.size) for i in items] == [
        ("d1", "docs", True, 0),
        ("f1", "a.txt", False, 3),
    ]


def test_list_directory_recursive_walks_children():
    wrapper = make_wrapper()
    items = wrapper.list_directory("/", recursive=True)
    assert [i.file_id for i in items] == ["d1", "f1", "f2"]


def test_find_child_maps_cloud_item():
    wrapper = make_wrapper()
    item = wrapper.find_child("/", "docs")
    assert item is not None and item.file_id == "d1" and item.is_dir
    assert wrapper.find_child("/", "missing") is None


def test_rename_move_delete_delegate_to_web_client():
    wrapper = make_wrapper()
    wrapper.rename("f1", "b.txt")
    wrapper.move(["f1"], "d1")
    wrapper.delete(["f1"])
    assert wrapper.flush_deletes(timeout=5) is True
    assert wrapper.client.calls == [
        ("rename", "f1", "b.txt"),
        ("move", ["f1"], "d1"),
        ("delete", ["f1"], True),
    ]


def test_upload_returns_cloud_item(tmp_path: Path):
    src = tmp_path / "x.bin"
    src.write_bytes(b"hello")
    wrapper = make_wrapper()
    item = wrapper.upload_file(src, "d1", "renamed.bin")
    assert (item.file_id, item.name, item.size, item.parent_id) == (
        "f_new",
        "renamed.bin",
        5,
        "d1",
    )
    assert wrapper.client.calls[-1] == ("upload", "x.bin", "d1", "renamed.bin")


def test_root_is_cloud_root():
    wrapper = make_wrapper()
    assert wrapper.root_file_id == "/"


def test_token_parse_smoke():
    # 保证 wrapper 依赖的 token 解析在测试环境中可用
    from mcloud_fuse.client.web139 import Web139Token

    raw = base64.b64encode(
        f"0:13800000000:p|a|y|{int(time.time() * 1000) + 100000}".encode()
    ).decode()
    token = Web139Token.parse(raw)
    assert token.account == "13800000000"


def test_upload_conflict_copy_renames_local_version(tmp_path: Path):
    src = tmp_path / "x.txt"
    src.write_bytes(b"local")
    wrapper = make_wrapper()
    wrapper.client.updated_at = "2026-10-02T00:00:00+08:00"

    item = wrapper.upload_file(
        src,
        "d1",
        "x.txt",
        expected_file_id="f1",
        expected_updated_at="2026-10-01T00:00:00+08:00",
    )

    assert item.name.startswith("x.txt.conflict-")
    assert wrapper.client.calls[-1][3] == item.name


def test_upload_conflict_error_mode(tmp_path: Path, monkeypatch):
    import mcloud_fuse.client.wrapper as wrapper_mod
    from mcloud_fuse.errors import MCloudConflictError

    src = tmp_path / "x.txt"
    src.write_bytes(b"local")
    wrapper = make_wrapper()
    wrapper.client.updated_at = "2026-10-02T00:00:00+08:00"
    monkeypatch.setattr(wrapper_mod, "CONFLICT_MODE", "error")

    with pytest.raises(MCloudConflictError):
        wrapper.upload_file(
            src,
            "d1",
            "x.txt",
            expected_file_id="f1",
            expected_updated_at="2026-10-01T00:00:00+08:00",
        )


def test_upload_without_conflict_keeps_name(tmp_path: Path):
    src = tmp_path / "x.txt"
    src.write_bytes(b"local")
    wrapper = make_wrapper()
    wrapper.client.updated_at = "2026-10-01T00:00:00+08:00"

    item = wrapper.upload_file(
        src,
        "d1",
        "x.txt",
        expected_file_id="f1",
        expected_updated_at="2026-10-01T00:00:00+08:00",
    )
    assert item.name == "x.txt"


def test_delete_batches_and_hides_immediately():
    wrapper = make_wrapper()
    wrapper.delete(["f1", "f2"])
    # 不等后台：列举必须立刻看不到
    assert all(item.file_id not in ("f1", "f2") for item in wrapper.list_directory("/"))
    assert wrapper.flush_deletes(timeout=5) is True
    delete_calls = [call for call in wrapper.client.calls if call[0] == "delete"]
    assert delete_calls == [("delete", ["f1", "f2"], True)]
