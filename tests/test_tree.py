import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mapping.id_map import InodeMapper
from mapping.tree import DirectoryTree


def test_directory_tree():
    print("🚀 开始测试 DirectoryTree 伪装树与动态解析...")

    mapper = InodeMapper(root_file_id="root_id_000")
    tree = DirectoryTree(mapper=mapper, sandbox_root="/AI空间/智能体云盘助手")

    # 1. 验证沙箱路径双向伪装转换
    assert tree.to_sandbox_path("/") == "/AI空间/智能体云盘助手"
    assert tree.to_sandbox_path("/code/py.py") == "/AI空间/智能体云盘助手/code/py.py"
    assert tree.to_local_path("/AI空间/智能体云盘助手") == "/"
    assert tree.to_local_path("/AI空间/智能体云盘助手/code/py.py") == "/code/py.py"
    print("✅ 沙箱路径 ↔ 本地路径伪装双向转换测试成功！")

    # 2. 模拟从云盘 API list_directory 返回的数据
    mock_cloud_items = [
        {
            "id": "fid_dir_1",
            "name": "projects",
            "isDir": True,
            "size": 0,
            "updateTime": "2026-10-04 10:00:00"
        },
        {
            "id": "fid_file_1",
            "name": "README.md",
            "isDir": False,
            "size": 2048,
            "updateTime": "2026-10-04 10:05:00"
        },
        {
            "id": "fid_trash",
            "name": ".TrashBin",  # 应该被自动过滤
            "isDir": True,
            "size": 0,
            "updateTime": "2026-10-04 00:00:00"
        }
    ]

    # 3. 填充根目录 (inode=1)
    entries = tree.populate_directory(parent_inode=1, cloud_items=mock_cloud_items)
    assert len(entries) == 2  # .TrashBin 应该被过滤掉
    assert tree.is_directory_loaded(1) is True

    # 检查映射结果
    proj_entry = mapper.get_by_path("/projects")
    assert proj_entry is not None
    assert proj_entry.file_id == "fid_dir_1"
    assert proj_entry.is_dir is True

    readme_entry = mapper.get_by_path("/README.md")
    assert readme_entry is not None
    assert readme_entry.size == 2048

    children = tree.get_children(1)
    assert len(children) == 2
    print("✅ 云盘目录项动态解析与回收站隔离验证成功！")

    print("🎉 DirectoryTree 全部测试通过！")


if __name__ == "__main__":
    test_directory_tree()