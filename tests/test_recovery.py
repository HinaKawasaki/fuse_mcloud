"""写回崩溃恢复测试：进程异常退出后，未上传的暂存文件应在下次启动时补传。"""
import sys
import time
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
        journal_db=tmp_path / "journal.sqlite3",
    )


def test_recover_pending_upload(tmp_path):
    cloud = FakeCloud()

    # 第一次进程：创建写回暂存并登记日志（模拟写完但还没 release 就崩溃）
    first = make_cache(tmp_path, cloud)
    buffer_path = first.create_write_buffer(
        "inode-99", target_dir_id="root", file_name="recovered.txt"
    )
    buffer_path.write_bytes(b"recovered data")
    # 模拟 fsync / release 把日志从 building 推到 pending，然后进程崩溃
    first.mark_write_ready("inode-99")
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
    first.mark_write_ready("inode-1")
    (tmp_path / "wb" / "inode-1.wb").unlink()

    second = make_cache(tmp_path, cloud)
    assert second.recover_pending_uploads() == 0
    assert not second.pending_uploads()
    assert "inode-1" in second.lost_uploads(), "payload 丢失应标记 lost 而不是静默删除"


def _uploading_entry(cache, key="k1", name="note.txt", payload=b"FRESH"):
    path = cache.create_write_buffer(key, target_dir_id="root", file_name=name)
    path.write_bytes(payload)
    cache.mark_write_ready(key)
    cache.mark_write_uploading(key)
    return path


def test_recovery_does_not_trust_same_name_same_size(tmp_path):
    """P0 回归：远端已有同名同大小的旧文件时，绝不能把没传成功的写回当成已上传。"""
    cloud = FakeCloud()
    cloud.dirs["root"].append(
        {"id": "f_old", "name": "note.txt", "size": 5, "updated_at": "2020-01-01 00:00:00"}
    )
    cloud.content["f_old"] = b"OLD!!"

    first = make_cache(tmp_path, cloud)
    _uploading_entry(first, payload=b"FRESH")
    first.close()

    second = make_cache(tmp_path, cloud)
    assert second.recover_pending_uploads() == 1, "无法证明上传成功时必须重传"
    assert ("note.txt", b"FRESH") in cloud.uploads
    assert not second.pending_uploads()
    assert not second.lost_uploads()


def test_recovery_trusts_recent_remote_object(tmp_path):
    """远端确实有本次上传的对象（同名同大小、时间不早于 payload）时，不重复上传。"""
    cloud = FakeCloud()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    cloud.dirs["root"].append(
        {"id": "f_new", "name": "note.txt", "size": 5, "updated_at": stamp}
    )
    cloud.content["f_new"] = b"FRESH"

    first = make_cache(tmp_path, cloud)
    _uploading_entry(first, payload=b"FRESH")
    first.close()

    second = make_cache(tmp_path, cloud)
    assert second.recover_pending_uploads() == 0
    assert not cloud.uploads, "已确认上传成功的条目不应重复上传"
    assert not second.pending_uploads()
    assert not second.lost_uploads()


def test_recovery_proof_for_overwrite_requires_changed_updated_at(tmp_path):
    """覆盖写：远端还是打开时那个对象、updatedAt 也没变，就不能认为上传成功。"""
    cloud = FakeCloud()
    cloud.dirs["root"].append(
        {"id": "f_a", "name": "a.txt", "size": 5, "updated_at": "2026-01-01 00:00:00"}
    )
    cloud.content["f_a"] = b"OLD!!"

    first = make_cache(tmp_path, cloud)
    path = first.create_write_buffer(
        "k2",
        target_dir_id="root",
        file_name="a.txt",
        base_file_id="f_a",
        base_updated_at="2026-01-01 00:00:00",
    )
    path.write_bytes(b"NEW!!")
    first.mark_write_ready("k2")
    first.mark_write_uploading("k2")
    first.close()

    second = make_cache(tmp_path, cloud)
    assert second.recover_pending_uploads() == 1
    assert ("a.txt", b"NEW!!") in cloud.uploads
