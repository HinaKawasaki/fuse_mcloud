import shutil
import sys
import time
from pathlib import Path

# 确保 src 目录在 sys.path 中
SRC_DIR = Path(__file__).resolve().parent.parent  # 仓库根（mcloud_fuse/ 所在目录）
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcloud_fuse.cache.data_cache import DataCache


class MockClientWrapper:
    """Mock 的 Client Wrapper，用于模拟云端文件的下载与上传网络 IO"""

    def __init__(self):
        self.download_calls = 0
        self.upload_calls = 0

    def download_file(self, file_id: str, local_save_path: Path) -> Path:
        self.download_calls += 1
        local_save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_save_path, "wb") as f:
            f.write(f"Cloud Content for {file_id}".encode("utf-8"))
        return local_save_path

    def upload_file(self, local_file_path: Path, target_dir_id: str, name=None):
        self.upload_calls += 1

        class DummyCloudItem:
            file_id = f"uploaded_id_{int(time.time())}"

        return DummyCloudItem()


def test_data_cache():
    print("🚀 开始测试 DataCache 读缓存、写回上传与 LRU 淘汰机制...")

    test_root = Path("/tmp/cmcloud-fuse-test")
    test_cache_dir = test_root / "cache"

    if test_root.exists():
        shutil.rmtree(test_root)

    mock_wrapper = MockClientWrapper()

    # 初始化 DataCache，设置较小的 max_size_mb (例如 1MB) 方便测试 LRU 淘汰
    cache = DataCache(
        client_wrapper=mock_wrapper,
        cache_dir=test_cache_dir,
        write_back_dir=test_root / "wb",
        max_size_mb=1,  # 限额 1MB
    )

    # ------------------ 1. 测试未命中与 API 下载 ------------------
    file1_path = cache.prepare_file_for_read(file_id="fid_001", file_name="test1.txt")
    assert file1_path.exists()
    assert mock_wrapper.download_calls == 1
    with open(file1_path, "r", encoding="utf-8") as f:
        assert f.read() == "Cloud Content for fid_001"
    print("✅ 首次读取未命中 & 自动触发网络下载测试成功！")

    # ------------------ 2. 测试缓存命中 (Zero IO) ------------------
    file1_path_again = cache.prepare_file_for_read(file_id="fid_001", file_name="test1.txt")
    assert file1_path_again == file1_path
    assert mock_wrapper.download_calls == 1
    print("✅ 二次读取精准命中本地缓存测试成功！")

    # ------------------ 3. 测试写回暂存 → 上传 → 零拷贝入缓存 ------------------
    buffer_path = cache.create_write_buffer(
        "wb_test", target_dir_id="dir_root", file_name="to_upload.txt"
    )
    buffer_path.write_bytes(b"Local new file content")

    item = cache.upload_write_buffer(
        "wb_test", target_dir_id="dir_root", file_name="to_upload.txt"
    )
    # 上传后暂存必须仍在：文件可能还开着，fsync 之后还要继续写
    assert buffer_path.exists()
    assert mock_wrapper.upload_calls == 1

    cache_path = cache.finalize_write_buffer("wb_test", item.file_id, "to_upload.txt")
    # 收尾是「改名」而不是「拷贝」：暂存消失、缓存出现、内容一致、不额外占一份磁盘
    assert not buffer_path.exists()
    assert cache_path is not None and cache_path.exists()
    assert cache_path.read_bytes() == b"Local new file content"
    assert cache.get_cache_path(item.file_id) == cache_path
    print("✅ 写回暂存 → 上传 → 零拷贝入缓存测试成功！")

    # ------------------ 4. 测试 LRU 容量溢出与淘汰 ------------------
    large_file_id = "fid_large"
    large_cache_file = test_cache_dir / f"{large_file_id}_large.bin"

    time.sleep(0.05)
    _ = cache.prepare_file_for_read(file_id="fid_002", file_name="test2.txt")

    with open(large_cache_file, "wb") as f:
        f.write(b"X" * (1024 * 1024 + 512 * 1024))

    cache._cache_map[large_file_id] = large_cache_file
    cache._access_time[large_file_id] = time.time()

    cache._evict_if_needed()

    assert cache.get_cache_path("fid_001") is None
    print("✅ LRU 容量限额与旧缓存淘汰机制测试成功！")

    shutil.rmtree(test_root, ignore_errors=True)
    print("🎉 DataCache 全部测试通过！")


def test_finalize_can_discard_local_copy(tmp_path):
    """UPLOAD_KEEP_CACHE=0 场景：上传后不保留本地副本。"""
    wrapper = MockClientWrapper()
    cache = DataCache(
        client_wrapper=wrapper,
        cache_dir=tmp_path / "cache",
        write_back_dir=tmp_path / "wb",
        max_size_mb=1,
    )
    buffer_path = cache.create_write_buffer("k1", target_dir_id="d", file_name="x.bin")
    buffer_path.write_bytes(b"abc")
    item = cache.upload_write_buffer("k1", target_dir_id="d", file_name="x.bin")

    result = cache.finalize_write_buffer(
        "k1", item.file_id, "x.bin", keep_local_for_read=False
    )

    assert result is None
    assert not buffer_path.exists()
    assert cache.get_cache_path(item.file_id) is None


if __name__ == "__main__":
    test_data_cache()
