"""删除（回收站）策略的离线测试。

背景：移动云盘 Skill **没有任何删除接口**。旧实现把 unlink 改写成「云内复制一份进
.TrashBin + 登记墓碑」，结果是原对象既没消失、又白占一份容量；而 .TrashBin 在本挂载
里属于隐藏目录，用户既看不到也删不掉。因此默认改为只登记墓碑。
"""

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent  # 仓库根（mcloud_fuse/ 所在目录）
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcloud_fuse.client.wrapper import CloudItem  # noqa: E402
from mcloud_fuse.mapping.id_map import InodeMapper  # noqa: E402
from mcloud_fuse.mapping.tree import DirectoryTree  # noqa: E402
from mcloud_fuse.utils.trash_bin import TrashBinHandler  # noqa: E402


class MockWrapper:
    def __init__(self):
        self.copied = []
        self.created = []
        self.items = {"root_id": []}

    def list_directory(self, dir_file_id, **kwargs):
        return [
            CloudItem(i["id"], i["name"], i.get("isDir", False), 0, "", dir_file_id)
            for i in self.items.get(dir_file_id, [])
        ]

    def create_folder(self, name, parent_file_id):
        new_id = f"dir_{name}"
        self.created.append((name, parent_file_id))
        self.items.setdefault(parent_file_id, []).append(
            {"id": new_id, "name": name, "isDir": True}
        )
        self.items.setdefault(new_id, [])
        return new_id

    def batch_copy(self, file_ids, to_parent_file_id):
        results = []
        for file_id in file_ids:
            new_id = f"copy_{file_id}"
            self.copied.append((file_id, to_parent_file_id))
            self.items.setdefault(to_parent_file_id, []).append(
                {"id": new_id, "name": file_id}
            )
            results.append({"fileId": file_id, "newFileId": new_id})
        return results


def build(copy_to_trash=None):
    mapper = InodeMapper(root_file_id="root_id")
    tree = DirectoryTree(mapper=mapper)
    wrapper = MockWrapper()
    tree.populate_directory(1, [{"id": "f1", "name": "to_delete.txt", "size": 3}])
    handler = TrashBinHandler(
        wrapper, mapper, tree, copy_to_trash=copy_to_trash
    )
    return mapper, tree, wrapper, handler


def test_default_delete_does_not_touch_cloud():
    """默认策略：只登记墓碑 + 本地摘除，不建目录、不复制。"""
    mapper, tree, wrapper, handler = build()

    assert handler.should_copy() is False
    entry = mapper.get_by_path("/to_delete.txt")
    assert entry is not None

    result = handler.move_to_trash(entry)

    assert result is None, "没有副本就没有副本 fileId"
    assert wrapper.copied == [], "默认不得在云端产生回收站副本"
    assert wrapper.created == [], "默认连 .TrashBin 目录都不该创建"
    assert "f1" in tree.tombstones, "必须登记墓碑，否则下次列举会复活"
    assert mapper.get_by_path("/to_delete.txt") is None


def test_copy_mode_still_available():
    """显式要求复制时（MCLOUD_TRASH_COPY=1）保留旧行为。"""
    mapper, tree, wrapper, handler = build(copy_to_trash=True)

    entry = mapper.get_by_path("/to_delete.txt")
    new_id = handler.move_to_trash(entry)

    assert new_id == "copy_f1"
    assert mapper.get_by_path("/to_delete.txt") is None
    assert wrapper.copied == [("f1", "dir_.TrashBin")]
    assert "f1" in tree.tombstones


def test_trash_dir_created_once():
    mapper, tree, wrapper, handler = build(copy_to_trash=True)
    trash_id = handler.ensure_trash_dir()
    assert trash_id == "dir_.TrashBin"
    assert wrapper.created == [(".TrashBin", "root_id")]
    # 再调用一次应命中缓存，不重复创建
    assert handler.ensure_trash_dir() == trash_id
    assert len(wrapper.created) == 1


def test_trash_bin_hidden_from_root_listing():
    mapper, tree, wrapper, handler = build(copy_to_trash=True)
    handler.ensure_trash_dir()
    # 模拟云端在根目录同时返回 .TrashBin 与普通文件
    tree.populate_directory(1, [
        {"id": "dir_.TrashBin", "name": ".TrashBin", "isDir": True},
        {"id": "f2", "name": "visible.txt"},
    ])
    names = [e.path.rsplit("/", 1)[-1] for e in tree.get_children(1)]
    assert ".TrashBin" not in names
    assert "visible.txt" in names
