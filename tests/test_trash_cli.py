"""`mcloud-fuse trash` 子命令的离线测试。"""

from __future__ import annotations

import json

from mcloud_fuse.client.web139 import RecycleItem
from mcloud_fuse.trash_cli import human_size, main


class FakeClient:
    def __init__(self):
        self.items = [
            RecycleItem("r1", "old.txt", False, 2048, "2026-10-01T12:00:00+08:00", 30, {}),
            RecycleItem("r2", "old-dir", True, 1024 * 1024, "2026-10-02T12:00:00+08:00", 20, {}),
        ]
        self.restored = []
        self.cleared = False

    def list_recycle_bin(self, page_size=100):
        return list(self.items)

    def restore_recycle(self, file_ids):
        self.restored.append(list(file_ids))

    def clear_recycle_bin(self):
        self.cleared = True


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(2048) == "2.0 KiB"
    assert human_size(1024 * 1024) == "1.0 MiB"


def test_list_prints_items(capsys):
    client = FakeClient()
    assert main(["list"], client=client) == 0
    out = capsys.readouterr().out
    assert "old.txt" in out and "old-dir" in out
    assert "2.0 KiB" in out and "1.0 MiB" in out
    assert "30天" in out and "r1" in out


def test_list_json(capsys):
    client = FakeClient()
    assert main(["list", "--json"], client=client) == 0
    data = json.loads(capsys.readouterr().out)
    assert [item["file_id"] for item in data] == ["r1", "r2"]
    assert "raw" not in data[0]


def test_restore(capsys):
    client = FakeClient()
    assert main(["restore", "r1", "r2"], client=client) == 0
    assert client.restored == [["r1", "r2"]]
    assert "已恢复 2 项" in capsys.readouterr().out


def test_empty_requires_confirmation(capsys):
    client = FakeClient()
    assert main(["empty"], client=client) == 1
    assert client.cleared is False
    assert "需要 --yes" in capsys.readouterr().err


def test_empty_with_yes(capsys):
    client = FakeClient()
    assert main(["empty", "--yes"], client=client) == 0
    assert client.cleared is True
    assert "已清空" in capsys.readouterr().out
