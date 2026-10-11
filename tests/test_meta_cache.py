import sys
import time
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mapping.id_map import InodeEntry
from cache.meta_cache import MetaCache


def test_meta_cache():
    print("🚀 开始测试 MetaCache 元数据高频缓存与 TTL 机制...")

    # 使用短 TTL (1 秒) 方便测试超时失效
    cache = MetaCache(ttl=1.0)

    mock_entry = InodeEntry(
        inode=10,
        file_id="fid_10",
        path="/docs/file.txt",
        is_dir=False,
        parent_inode=2,
        size=512
    )

    # 1. 测试 getattr 写入与命中
    cache.put_attr(10, mock_entry)
    cached = cache.get_attr(10)
    assert cached is not None
    assert cached.size == 512
    print("✅ Getattr 写入与内存命中测试成功！")

    # 2. 测试 readdir 列表缓存
    mock_children = [mock_entry]
    cache.put_readdir(parent_inode=2, children=mock_children)
    assert len(cache.get_readdir(2)) == 1
    print("✅ Readdir 子节点列表缓存测试成功！")

    # 3. 测试主动失效 (Invalidation)
    cache.invalidate_all_for_inode(inode=10, parent_inode=2)
    assert cache.get_attr(10) is None
    assert cache.get_readdir(2) is None
    print("✅ 缓存主动精准失效逻辑测试成功！")

    # 4. 测试 TTL 超时自动失效
    cache.put_attr(10, mock_entry)
    assert cache.get_attr(10) is not None
    print("⏳ 等待 1.1 秒测试 TTL 自动过期...")
    time.sleep(1.1)
    assert cache.get_attr(10) is None
    print("✅ TTL 超时自动过期失效测试成功！")

    print("🎉 MetaCache 全部测试通过！")


if __name__ == "__main__":
    test_meta_cache()