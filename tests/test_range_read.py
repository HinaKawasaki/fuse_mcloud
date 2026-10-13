"""按需（Range）读取大文件的离线测试。

覆盖三件事：
1. 只下载被读到的分片，重复读不重复下载；
2. 服务端忽略 Range（HTTP 200）时自动降级为整文件，且不反复下载；
3. 部分文件**绝不会**被当成完整文件交给写路径 / rename 上传。
"""

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent  # 仓库根（mcloud_fuse/ 所在目录）
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcloud_fuse.cache.data_cache import DataCache  # noqa: E402

CONTENT = bytes(range(256)) * 4  # 1024 字节，便于按偏移断言


class RangeWrapper:
    """模拟支持 Range 的下载直链：按 offset 写入切片。"""

    def __init__(self, content: bytes, supports_range: bool = True):
        self.content = content
        self.supports_range = supports_range
        self.range_calls = []
        self.full_calls = []

    def download_file(self, file_id, local_save_path):
        self.full_calls.append(file_id)
        Path(local_save_path).write_bytes(self.content)
        return Path(local_save_path)

    def download_range(self, file_id, local_save_path, offset, length, expected_total=None):
        self.range_calls.append((offset, length))
        path = Path(local_save_path)
        if not self.supports_range:
            # 服务端忽略 Range：回 200，整个响应体从头写起
            path.write_bytes(self.content)
            return None
        chunk = self.content[offset : offset + length]
        with open(path, "r+b") as handle:
            handle.seek(offset)
            handle.write(chunk)
        return len(chunk)


def make_cache(tmp_path, wrapper, limit=64, chunk=64):
    return DataCache(
        client_wrapper=wrapper,
        cache_dir=tmp_path / "cache",
        write_back_dir=tmp_path / "wb",
        max_size_mb=64,
        eager_download_limit_bytes=limit,
        range_chunk_bytes=chunk,
    )


def test_lazy_read_fetches_only_requested_chunks(tmp_path):
    wrapper = RangeWrapper(CONTENT)
    cache = make_cache(tmp_path, wrapper)

    assert cache.should_read_lazily(len(CONTENT)) is True
    assert cache.should_read_lazily(8) is False

    path = cache.open_lazy("fid_big", "big.bin", len(CONTENT))
    assert path.exists() and path.stat().st_size == len(CONTENT)
    assert cache.is_partial("fid_big") is True
    # 部分文件不能出现在「完整缓存」视图里，否则写路径会拿到半截文件
    assert cache.get_cache_path("fid_big") is None
    assert wrapper.range_calls == []

    cache.ensure_range("fid_big", 0, 10)
    assert wrapper.range_calls == [(0, 64)], "首次读应对齐到分片起点并只取一片"
    with open(path, "rb") as handle:
        assert handle.read(10) == CONTENT[:10]

    cache.ensure_range("fid_big", 100, 10)
    assert wrapper.range_calls == [(0, 64), (64, 64)], "第二次读只补上缺的那一片"
    with open(path, "rb") as handle:
        handle.seek(100)
        assert handle.read(10) == CONTENT[100:110]

    cache.ensure_range("fid_big", 0, 10)
    assert len(wrapper.range_calls) == 2, "已落盘的分片不应重复下载"
    assert wrapper.full_calls == [], "按需读期间不应触发整文件下载"

    cache.release_read("fid_big")


def test_ensure_complete_fills_partial_before_write_path(tmp_path):
    """覆盖写 / truncate / rename 需要完整内容时必须先补齐。"""
    wrapper = RangeWrapper(CONTENT)
    cache = make_cache(tmp_path, wrapper)

    cache.open_lazy("fid_big", "big.bin", len(CONTENT))
    cache.ensure_range("fid_big", 0, 10)
    cache.release_read("fid_big")

    # acquire_for_read 是写路径 / rename 的入口：必须拿到完整文件
    path = cache.acquire_for_read("fid_big", "big.bin")
    cache.release_read("fid_big")

    assert wrapper.full_calls == ["fid_big"]
    assert cache.is_partial("fid_big") is False
    assert cache.get_cache_path("fid_big") == path
    assert path.read_bytes() == CONTENT


def test_server_without_range_falls_back_and_stays_complete(tmp_path):
    wrapper = RangeWrapper(CONTENT, supports_range=False)
    cache = make_cache(tmp_path, wrapper)

    cache.open_lazy("fid_big", "big.bin", len(CONTENT))
    path = cache.ensure_range("fid_big", 500, 10)

    assert wrapper.range_calls == [(448, 64)], "按分片对齐地只请求缺口"
    assert cache.is_partial("fid_big") is False, "服务端不支持 Range 时应直接转为完整缓存"
    assert path.read_bytes() == CONTENT

    cache.release_read("fid_big")
    # 后续读取直接命中完整缓存，不再发起任何下载
    again = cache.acquire_for_read("fid_big", "big.bin")
    cache.release_read("fid_big")
    assert again == path
    assert len(wrapper.range_calls) == 1
    assert wrapper.full_calls == []


def test_eager_limit_zero_disables_lazy_reads(tmp_path):
    """``MCLOUD_EAGER_DOWNLOAD_LIMIT=0`` 时必须退回 v0.3.0 的整文件下载行为。"""
    wrapper = RangeWrapper(CONTENT)
    cache = make_cache(tmp_path, wrapper, limit=0)
    assert cache.should_read_lazily(10_000) is False

    path = cache.acquire_for_read("fid_big", "big.bin")
    cache.release_read("fid_big")
    assert wrapper.full_calls == ["fid_big"]
    assert wrapper.range_calls == []
    assert path.read_bytes() == CONTENT


def test_short_range_read_only_requests_to_end_of_file(tmp_path):
    """请求越界时只取到文件末尾，并把这段记为已覆盖。"""
    wrapper = RangeWrapper(CONTENT[:100])
    cache = make_cache(tmp_path, wrapper)

    cache.open_lazy("fid_small", "small.bin", 100)
    cache.ensure_range("fid_small", 90, 10)
    cache.release_read("fid_small")

    assert wrapper.range_calls == [(64, 36)], "越界部分只请求到文件末尾"
    assert wrapper.full_calls == []
