"""pytest 公共配置：注入 sys.path，并隔离真实的用户配置。

如果不隔离，``mcloud_fuse`` 包导入时会读取开发机上的
``~/.config/mcloud-fuse/config.json``，测试结果就会随本机配置漂移。
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (ROOT, ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# 指向一个不存在的文件：显式设置了 MCLOUD_CONFIG_FILE 的用例仍可覆盖。
os.environ.setdefault(
    "MCLOUD_CONFIG_FILE",
    str(Path(tempfile.gettempdir()) / "mcloud-fuse-pytest" / "no-config.json"),
)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_journal(tmp_path, monkeypatch):
    """每个用例一份独立的 SQLite 写回日志，避免测试之间串数据。"""
    monkeypatch.setenv("MCLOUD_JOURNAL_DB", str(tmp_path / "writeback.sqlite3"))
    monkeypatch.setenv("MCLOUD_STATE_DIR", str(tmp_path / "state"))
