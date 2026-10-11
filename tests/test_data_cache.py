import sys
import os
import time
import shutil
from pathlib import Path

# 确保 src 目录在 sys.path 中
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cache.data_cache import DataCache


class MockClientWrapper:
    """Mock 的 Client Wrapper，用于模拟云端文件的下载与上传网络 IO"""

    def __init__(self):
        self.download_calls = 0
        self.upload_calls = 0

    def download_file(self, file_id: str, local_save_path: Path) -> Path:
        self.download_calls += 1
        # 模拟从云端下载数据写到本地 Path
        local_save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_save_path, "wb") as f:
            f.write(f"Cloud Content for {file_id}".encode("utf-8"))
        return local_save_path

    def upload_file(self, local_file_path: Path, target_dir_id: str):
        self.upload_calls += 1

        class DummyCloudItem:
            file_id = f"uploaded_id_{int(time.time())}"

        return DummyCloudItem()


def test_data_cache():
    print("🚀 开始测试 DataCache 读缓存、上传同步与 LRU 淘汰机制...")

    # 使用独立的测试专用临时缓存目录
    test_cache_dir = Path("/tmp/cmcloud-fuse-test/cache")
    test_fallback_dir = Path("/tmp/cmcloud-fuse-test/fallback")

    # 清理上一次测试留下来的残留数据
    if test_cache_dir.exists():
        shutil.rmtree(test_cache_dir)
    if test_fallback_dir.exists():
        shutil.rmtree(test_fallback_dir)

    mock_wrapper = MockClientWrapper()

    # 初始化 DataCache，设置较小的 max_size_mb (例如 1MB) 方便测试 LRU 淘汰
    cache = DataCache(
        client_wrapper=mock_wrapper,
        cache_dir=test_cache_dir,
        fallback_dir=test_fallback_dir,
        max_size_mb=1  # 限额 1MB
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
    assert mock_wrapper.download_calls == 1  # 下载调用次数不应该增加
    print("✅ 二次读取精准命中本地缓存测试成功！")

    # ------------------ 3. 测试上传暂存与同步 ------------------
    dummy_upload_src = test_cache_dir / "to_upload.txt"
    with open(dummy_upload_src, "w", encoding="utf-8") as f:
        f.write("Local new file content")

    uploaded_id = cache.stage_file_for_upload(dummy_upload_src, target_dir_id="dir_root")
    assert uploaded_id.startswith("uploaded_id_")
    assert mock_wrapper.upload_calls == 1
    print("✅ 文件上传回写同步测试成功！")

    # ------------------ 4. 测试 LRU 容量溢出与淘汰 ------------------
    # 手动写入较大文件来触发 LRU
    large_file_id = "fid_large"
    large_cache_file = test_cache_dir / f"{large_file_id}_large.bin"
    
    # 稍微拉开访问时间戳
    time.sleep(0.05)
    _ = cache.prepare_file_for_read(file_id="fid_002", file_name="test2.txt")
    
    # 手动生成一个超过 1MB (1.5MB) 的文件装填进缓存
    with open(large_cache_file, "wb") as f:
        f.write(b"X" * (1024 * 1024 + 512 * 1024))

    cache._cache_map[large_file_id] = large_cache_file
    cache._access_time[large_file_id] = time.time()

    # 触发 LRU 检查
    cache._evict_if_needed()

    # 验证最老的文件 (fid_001) 是否被 LRU 剔除
    assert cache.get_cache_path("fid_001") is None
    print("✅ LRU 容量限额与旧缓存淘汰机制测试成功！")

    # 清理测试现场
    if test_cache_dir.parent.exists():
        shutil.rmtree(test_cache_dir.parent)

    print("🎉 DataCache 全部测试通过！")


if __name__ == "__main__":
    test_data_cache()