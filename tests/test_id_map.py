import sys
from pathlib import Path

# 确保导入 src 模块
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mapping.id_map import InodeMapper


def test_inode_mapper():
    print("🚀 开始测试 InodeMapper 三向映射与分配...")

    mapper = InodeMapper(root_file_id="cloud_root_123")

    # 1. 验证 Root 节点
    root = mapper.get_by_inode(1)
    assert root is not None
    assert root.file_id == "cloud_root_123"
    assert mapper.get_by_path("/").inode == 1
    print("✅ 根节点映射验证成功！")

    # 2. 模拟分配目录 /docs
    doc_entry = mapper.allocate_or_update(
        file_id="fid_docs",
        path="/docs",
        is_dir=True,
        parent_inode=1
    )
    assert doc_entry.inode == 2
    assert mapper.get_by_file_id("fid_docs").path == "/docs"
    assert mapper.get_by_path("/docs").inode == 2
    print("✅ 目录 /docs 分配及三向查找测试成功！")

    # 3. 模拟在 /docs 下分配文件 /docs/hello.txt
    file_entry = mapper.allocate_or_update(
        file_id="fid_hello",
        path="/docs/hello.txt",
        is_dir=False,
        parent_inode=doc_entry.inode,
        size=1024
    )
    assert file_entry.inode == 3
    assert file_entry.parent_inode == 2
    assert 3 in mapper.get_by_inode(2).child_inodes
    print("✅ 文件 /docs/hello.txt 关联与父子节点结构验证成功！")

    # 4. 删除节点验证
    mapper.remove_by_inode(2)  # 删除 /docs，应连带移除子节点 3
    assert mapper.get_by_path("/docs") is None
    assert mapper.get_by_path("/docs/hello.txt") is None
    assert mapper.get_by_inode(3) is None
    print("✅ 节点删除与级联清理验证成功！")

    print("🎉 InodeMapper 全部测试通过！")


if __name__ == "__main__":
    test_inode_mapper()