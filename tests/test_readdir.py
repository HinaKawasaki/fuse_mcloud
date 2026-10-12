"""目录列举回归测试。

针对 debug/log_repeat_location.log 暴露的问题：
- ls 出现大量重复行（同名条目被重复吐出）
- 列表内容来自全盘搜索而非当前目录，导致任意目录都能看到无关文件
- 分页 next_id 不稳定导致重复/遗漏
"""
import sys
from pathlib import Path

import pytest
import trio

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cache.meta_cache import MetaCache  # noqa: E402
from client.wrapper import CloudItem, DirectClientWrapper  # noqa: E402
from fuse.fuse_ops import MCloudFuseOps  # noqa: E402
from mapping.id_map import InodeMapper  # noqa: E402
from mapping.tree import DirectoryTree  # noqa: E402

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class FakeWrapper:
    """按目录返回子项的假 wrapper，并记录每次列举使用的 dir_file_id。"""

    def __init__(self, directories):
        self.directories = directories
        self.list_calls = []

    def list_directory(self, dir_file_id, recursive=False, page_limit=200, max_items=50000):
        self.list_calls.append(dir_file_id)
        return [
            CloudItem(
                file_id=item["id"],
                name=item["name"],
                is_dir=bool(item.get("isDir")),
                size=int(item.get("size") or 0),
                updated_at=str(item.get("updateTime") or ""),
                parent_id=dir_file_id,
            )
            for item in self.directories.get(dir_file_id, [])
        ]


class StubDataCache:
    def prepare_file_for_read(self, file_id, file_name):
        return Path("/tmp/stub-cache") / f"{file_id}_{file_name}"


def make_ops(wrapper):
    mapper = InodeMapper(root_file_id="sandbox_root")
    tree = DirectoryTree(mapper=mapper)
    return MCloudFuseOps(
        mapper=mapper,
        tree=tree,
        meta_cache=MetaCache(),
        data_cache=StubDataCache(),
        wrapper=wrapper,
    )


class ReaddirCollector:
    """把 pyfuse3.readdir_reply 的调用收集起来，模拟内核侧读取。"""

    def __init__(self, stop_after=None):
        self.rows = []
        self.stop_after = stop_after

    def __call__(self, token, name, attrs, next_id):
        self.rows.append((name.decode("utf-8"), next_id, attrs.st_ino))
        if self.stop_after is not None and len(self.rows) >= self.stop_after:
            return False
        return True


# ---------------------------------------------------------------------------
# 直接测试 DirectoryTree 的去重与对账
# ---------------------------------------------------------------------------

def test_populate_deduplicates_same_name():
    mapper = InodeMapper(root_file_id="sandbox_root")
    tree = DirectoryTree(mapper=mapper)
    items = [
        {"id": "id_10", "name": "16", "isDir": True},
        {"id": "id_11", "name": "16", "isDir": True},
        {"id": "id_20", "name": "zotero", "isDir": True},
        {"id": "id_21", "name": "zotero", "isDir": True},
        {"id": "id_30", "name": "mozilla", "isDir": True},
        {"id": "id_40", "name": "readme.md", "isDir": False, "size": 10},
    ]
    entries = tree.populate_directory(1, items)
    names = [e.path.rsplit("/", 1)[-1] for e in entries]
    assert names == sorted(set(names), key=names.index)  # 无重复
    assert len(names) == len(set(names)) == 4
    assert len(tree.get_children(1)) == 4
    # 同一路径只允许一个 inode 占用
    assert len(mapper._path_to_inode) == 5  # 根 + 4 个子项


def test_populate_removes_stale_children():
    mapper = InodeMapper(root_file_id="sandbox_root")
    tree = DirectoryTree(mapper=mapper)
    tree.populate_directory(1, [
        {"id": "a", "name": "keep.txt"},
        {"id": "b", "name": "ghost.txt"},
    ])
    assert mapper.get_by_path("/ghost.txt") is not None

    # 云端删除 ghost.txt 后重新列举，本地必须同步消失
    tree.populate_directory(1, [{"id": "a", "name": "keep.txt"}])
    assert mapper.get_by_path("/keep.txt") is not None
    assert mapper.get_by_path("/ghost.txt") is None


def test_children_sorted_by_inode_and_reparenting():
    mapper = InodeMapper(root_file_id="root")
    tree = DirectoryTree(mapper=mapper)
    tree.populate_directory(1, [{"id": "d", "name": "docs", "isDir": True}])
    docs = mapper.get_by_path("/docs")

    tree.populate_directory(docs.inode, [
        {"id": "f2", "name": "b.txt"},
        {"id": "f1", "name": "a.txt"},
    ])
    inodes = mapper.get_children_inodes(docs.inode)
    assert inodes == sorted(inodes)
    assert len(inodes) == 2


# ---------------------------------------------------------------------------
# readdir / lookup 行为
# ---------------------------------------------------------------------------

def test_readdir_uses_directory_id_not_global_search(monkeypatch):
    """根目录只应列出沙箱根的子项，子目录只列自己的子项。"""
    wrapper = FakeWrapper({
        "sandbox_root": [
            {"id": "dir_docs", "name": "docs", "isDir": True},
            {"id": "file_a", "name": "a.txt", "size": 1},
        ],
        "dir_docs": [
            {"id": "file_b", "name": "b.txt", "size": 2},
            {"id": "file_c", "name": "c.txt", "size": 3},
        ],
    })
    ops = make_ops(wrapper)

    collected = ReaddirCollector()
    monkeypatch.setattr("pyfuse3.readdir_reply", collected)

    trio.run(ops.readdir, 1, 0, object())
    root_names = [name for name, _, _ in collected.rows]
    assert root_names == ["docs", "a.txt"]
    assert wrapper.list_calls == ["sandbox_root"]

    # 进入 /docs 再列举，只能看到 docs 的子项
    docs_inode = ops.mapper.get_by_path("/docs").inode
    collected2 = ReaddirCollector()
    monkeypatch.setattr("pyfuse3.readdir_reply", collected2)
    trio.run(ops.readdir, docs_inode, 0, object())

    assert [name for name, _, _ in collected2.rows] == ["b.txt", "c.txt"]
    assert wrapper.list_calls == ["sandbox_root", "dir_docs"]


def test_readdir_pagination_reports_each_entry_once(monkeypatch):
    wrapper = FakeWrapper({
        "sandbox_root": [
            {"id": f"file_{i}", "name": f"f{i}.txt"} for i in range(5)
        ],
    })
    ops = make_ops(wrapper)

    # 第一次：内核缓冲区在第 3 条后满
    first = ReaddirCollector(stop_after=3)
    monkeypatch.setattr("pyfuse3.readdir_reply", first)
    trio.run(ops.readdir, 1, 0, object())
    assert len(first.rows) == 3

    # 内核带着最后一条的 next_id 再次调用
    last_id = first.rows[-1][1]
    second = ReaddirCollector()
    monkeypatch.setattr("pyfuse3.readdir_reply", second)
    trio.run(ops.readdir, 1, last_id, object())

    emitted = [n for n, _, _ in first.rows] + [n for n, _, _ in second.rows]
    assert emitted == [f"f{i}.txt" for i in range(5)]
    assert len(emitted) == len(set(emitted)) == 5


def test_readdir_never_emits_duplicate_names(monkeypatch):
    wrapper = FakeWrapper({
        "sandbox_root": [
            {"id": "id_1", "name": "zotero", "isDir": True},
            {"id": "id_2", "name": "zotero", "isDir": True},
            {"id": "id_3", "name": "zotero", "isDir": True},
        ],
    })
    ops = make_ops(wrapper)
    collected = ReaddirCollector()
    monkeypatch.setattr("pyfuse3.readdir_reply", collected)
    trio.run(ops.readdir, 1, 0, object())
    names = [n for n, _, _ in collected.rows]
    assert names == ["zotero"]


def test_lookup_populates_correct_parent(monkeypatch):
    wrapper = FakeWrapper({
        "sandbox_root": [{"id": "dir_docs", "name": "docs", "isDir": True}],
        "dir_docs": [{"id": "file_a", "name": "a.txt", "size": 7}],
    })
    ops = make_ops(wrapper)

    root_attr = trio.run(ops.lookup, 1, b"docs")
    docs_inode = root_attr.st_ino

    child_attr = trio.run(ops.lookup, docs_inode, b"a.txt")
    assert child_attr.st_size == 7
    assert wrapper.list_calls == ["sandbox_root", "dir_docs"]

    with pytest.raises(Exception):
        trio.run(ops.lookup, docs_inode, b"nope.txt")


# ---------------------------------------------------------------------------
# wrapper 解析
# ---------------------------------------------------------------------------

def test_parse_raw_file_detects_directories():
    parse = DirectClientWrapper._parse_raw_file
    assert parse({"fileId": "1", "name": "d", "type": "folder"}).is_dir is True
    assert parse({"fileId": "2", "name": "d", "category": "folder"}).is_dir is True
    assert parse({"fileId": "3", "name": "f", "type": "file"}).is_dir is False
    assert parse({"fileId": "4", "name": "f", "isDir": False, "size": "12"}).size == 12
