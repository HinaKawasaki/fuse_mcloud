import sys
import asyncio
import pytest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

BASEMENT_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "basement" / "scripts"
if str(BASEMENT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(BASEMENT_SCRIPTS_DIR))

from mapping.id_map import InodeMapper
from mapping.tree import DirectoryTree
from cache.meta_cache import MetaCache
from cache.data_cache import DataCache
from fuse.fuse_ops import MCloudFuseOps


class DummyWrapper:
    def search_or_list_files(self, **kwargs):
        return {"items": []}


async def main_test():
    print("🚀 开始测试 MCloudFuseOps 只读回调与属性解析...")

    mapper = InodeMapper(root_file_id="root_123")
    tree = DirectoryTree(mapper=mapper)
    meta_cache = MetaCache()
    wrapper = DummyWrapper()
    data_cache = DataCache(client_wrapper=wrapper, cache_dir=Path("/tmp/fuse_ops_test"))

    ops = MCloudFuseOps(
        mapper=mapper,
        tree=tree,
        meta_cache=meta_cache,
        data_cache=data_cache,
        wrapper=wrapper
    )

    # 1. 测试根节点 getattr
    root_attr = await ops.getattr(1)
    assert root_attr.st_ino == 1
    assert root_attr.st_mode & 0o40000  # 确认是目录 S_IFDIR
    print("✅ getattr 根节点属性解析成功！")

    # 2. 分配测试文件，测试 lookup
    entry = mapper.allocate_or_update(
        file_id="fid_test",
        path="/test.txt",
        is_dir=False,
        size=128
    )
    file_attr = await ops.lookup(1, b"test.txt")
    assert file_attr.st_ino == entry.inode
    assert file_attr.st_size == 128
    print("✅ lookup 子节点搜索与属性返回成功！")

    print("🎉 MCloudFuseOps 核心逻辑测试通过！")


if __name__ == "__main__":
    asyncio.run(main_test())