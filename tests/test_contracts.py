"""端点契约：仓库里保存的真实响应样例，必须还能被当前解析器读懂。

这些用例不是「请求上游」，而是锁定**我们与上游之间约定的字段**：
- 样例来自真实响应（已脱敏）；
- 任何字段被删/改名，样例与 schema 对不上，CI 变红；
- 上游真实漂移由 `scripts/check_contracts.py --live` 检测（见契约 workflow）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcloud_fuse.client.web139 import DiskQuota, RecycleItem, Web139Client, WebFile

CONTRACT_DIR = Path(__file__).parent / "contracts"
SCHEMA = json.loads((CONTRACT_DIR / "schema.json").read_text(encoding="utf-8"))


def load(name: str):
    return json.loads((CONTRACT_DIR / f"{name}.json").read_text(encoding="utf-8"))


def resolve(doc, path: str):
    current = doc
    for part in path.split("."):
        if part.isdigit():
            current = current[int(part)]
        else:
            if not isinstance(current, dict) or part not in current:
                raise KeyError(f"缺少字段 {path!r}（在 {part!r} 处断掉）")
            current = current[part]
    return current


def test_every_fixture_has_schema():
    fixtures = {path.stem for path in CONTRACT_DIR.glob("*.json")} - {"schema"}
    assert fixtures == set(SCHEMA), "新增/删除样例时必须同步 schema.json"


@pytest.mark.parametrize("name", sorted(SCHEMA))
def test_fixture_satisfies_schema(name):
    doc = load(name)
    for path in SCHEMA[name]:
        resolve(doc, path)


def test_file_list_parses_to_webfile():
    items = load("file_list")["data"]["items"]
    first, second = (WebFile.from_api(item, "/") for item in items)
    assert first.is_dir and first.name == "docs"
    assert not second.is_dir and second.size == 123


def test_quota_parses_to_diskquota():
    quota = DiskQuota.from_api(load("quota_detail")["data"])
    assert quota.total_mb == 8547328
    assert quota.free_mb == 8334382
    assert quota.used_mb == quota.total_mb - quota.free_mb


def test_recyclebin_parses_to_recycleitem():
    item = RecycleItem.from_api(load("recyclebin_list")["data"]["items"][0])
    assert item.file_id.startswith("Ftrash")
    assert item.remaining_days == 43
    assert not item.is_dir


def test_file_create_has_upload_slot():
    data = load("file_create")["data"]
    assert data["fileId"] and data["uploadId"]
    assert data["partInfos"][0]["uploadUrl"].startswith("https://")


def test_download_url_prefers_url_field():
    # 真实响应用的是 data.url；客户端也兼容 data.cdnUrl
    client = Web139Client.__new__(Web139Client)
    doc = load("download_url")
    data = client._data(doc)
    assert (data.get("cdnUrl") or data.get("url")).startswith("https://")
