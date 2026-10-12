"""回收站（.TrashBin）与写回上传的离线测试。"""
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from client.wrapper import CloudItem  # noqa: E402
from mapping.id_map import InodeMapper  # noqa: E402
from mapping.tree import DirectoryTree  # noqa: E402
from utils.trash_bin import TrashBinHandler  # noqa: E402


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
        self.items.setdefault(parent_file_id, []).append({"id": new_id, "name": name, "isDir": True})
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


def build():
    mapper = InodeMapper(root_file_id="root_id")
    tree = DirectoryTree(mapper=mapper)
    wrapper = MockWrapper()
    tree.populate_directory(1, [{"id": "f1", "name": "to_delete.txt", "size": 3}])
    return mapper, tree, wrapper, TrashBinHandler(wrapper, mapper, tree)


def test_trash_dir_created_once():
    mapper, tree, wrapper, handler = build()
    trash_id = handler.ensure_trash_dir()
    assert trash_id == "dir_.TrashBin"
    assert wrapper.created == [(".TrashBin", "root_id")]
    # 再调用一次应命中缓存，不重复创建
    assert handler.ensure_trash_dir() == trash_id
    assert len(wrapper.created) == 1


def test_move_to_trash_removes_local_node():
    mapper, tree, wrapper, handler = build()
    entry = mapper.get_by_path("/to_delete.txt")
    assert entry is not None

    new_id = handler.move_to_trash(entry)

    assert new_id == "copy_f1"
    assert mapper.get_by_path("/to_delete.txt") is None
    assert wrapper.copied == [("f1", "dir_.TrashBin")]


def test_trash_bin_hidden_from_root_listing():
    mapper, tree, wrapper, handler = build()
    handler.ensure_trash_dir()
    # 模拟云端在根目录同时返回 .TrashBin 与普通文件
    tree.populate_directory(1, [
        {"id": "dir_.TrashBin", "name": ".TrashBin", "isDir": True},
        {"id": "f2", "name": "visible.txt"},
    ])
    names = [e.path.rsplit("/", 1)[-1] for e in tree.get_children(1)]
    assert ".TrashBin" not in names
    assert "visible.txt" in names
