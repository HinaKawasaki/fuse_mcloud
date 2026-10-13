"""写回崩溃恢复测试：进程异常退出后，未上传的暂存文件应在下次启动时补传。"""
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent  # 仓库根（mcloud_fuse/ 所在目录）
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeCloud  # noqa: E402

from mcloud_fuse.cache.data_cache import DataCache  # noqa: E402


def make_cache(tmp_path, cloud):
    return DataCache(
        client_wrapper=cloud,
        cache_dir=tmp_path / "cache",
        write_back_dir=tmp_path / "wb",
        max_size_mb=64,
    )


def test_recover_pending_upload(tmp_path):
    cloud = FakeCloud()

    # 第一次进程：创建写回暂存并登记日志（模拟写完但还没 release 就崩溃）
    first = make_cache(tmp_path, cloud)
    buffer_path = first.create_write_buffer(
        "inode-99", target_dir_id="root", file_name="recovered.txt"
    )
    buffer_path.write_bytes(b"recovered data")
    assert "inode-99" in first.pending_uploads()

    # 第二次进程：新建 DataCache，读日志并补传
    second = make_cache(tmp_path, cloud)
    assert second.pending_uploads(), "写回日志应跨进程保留"
    recovered = second.recover_pending_uploads()

    assert recovered == 1
    assert ("recovered.txt", b"recovered data") in cloud.uploads
    assert not second.pending_uploads()

    uploaded = next(i for i in cloud.dirs["root"] if i["name"] == "recovered.txt")
    assert second.prepare_file_for_read(uploaded["id"], "recovered.txt").read_bytes() == b"recovered data"


def test_recover_skips_missing_buffer(tmp_path):
    cloud = FakeCloud()
    first = make_cache(tmp_path, cloud)
    first.create_write_buffer("inode-1", target_dir_id="root", file_name="ghost.txt")
    (tmp_path / "wb" / "inode-1.wb").unlink()

    second = make_cache(tmp_path, cloud)
    assert second.recover_pending_uploads() == 0
    assert not second.pending_uploads()
