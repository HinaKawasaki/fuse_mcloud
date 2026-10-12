"""多段上传与统一调用封装（限流+重试）的离线测试。"""
import functools
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import client.wrapper as wrapper_mod
from client.wrapper import DirectClientWrapper
from cm_cloud_errors import ApiError
from router.retry import retry_call


class DummyAuth:
    def get_valid_token(self, issue_login=False):
        return "token"


def make_wrapper() -> DirectClientWrapper:
    return DirectClientWrapper(DummyAuth())


def force_multipart(monkeypatch, *, threshold=1024, min_part=4096, max_parts=8):
    monkeypatch.setattr(wrapper_mod, "MULTIPART_THRESHOLD_BYTES", threshold)
    monkeypatch.setattr(wrapper_mod, "MIN_PART_SIZE_BYTES", min_part)
    monkeypatch.setattr(wrapper_mod, "MAX_PARTS", max_parts)
    monkeypatch.setattr(wrapper_mod.sandbox, "ensure_files_in_sandbox", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# 分片规划
# ---------------------------------------------------------------------------

def test_plan_parts_small_file_uses_single(monkeypatch):
    force_multipart(monkeypatch)
    assert DirectClientWrapper._plan_parts(512) is None
    assert DirectClientWrapper._plan_parts(1024) is None


def test_plan_parts_invariants(monkeypatch):
    force_multipart(monkeypatch)
    for size in (5000, 20000, 100000, 1_000_000):
        parts = DirectClientWrapper._plan_parts(size)
        assert parts is not None, size
        assert sum(parts) == size
        assert len(parts) >= 2
        assert len(parts) <= 8
        assert all(p >= 4096 for p in parts[:-1]), f"非末段小于最小分片: {parts}"
        assert parts[-1] > 0


def test_plan_parts_under_real_config_for_756mib_iso():
    """用真实配置规划 756MiB 的 Debian ISO：片数受控、非末段不小于 5MiB。"""
    size = 792723456
    parts = DirectClientWrapper._plan_parts(size)
    assert parts is not None
    assert sum(parts) == size
    assert 2 <= len(parts) <= wrapper_mod.MAX_PARTS
    assert all(p >= wrapper_mod.MIN_PART_SIZE_BYTES for p in parts[:-1])
    assert parts[-1] > 0


# ---------------------------------------------------------------------------
# 多段上传
# ---------------------------------------------------------------------------

def test_multipart_upload_reassembles_bytes(monkeypatch, tmp_path):
    force_multipart(monkeypatch)
    content = bytes(range(256)) * 200  # 51200 字节
    src = tmp_path / "big.bin"
    src.write_bytes(content)

    def fake_post_json(path, payload, **kwargs):
        parts = payload["partInfos"]
        return {
            "code": "0",
            "data": {
                "uploadId": "U1",
                "fileId": "F1",
                "partInfos": [
                    {
                        "partNumber": p["partNumber"],
                        "uploadUrl": f"http://up/{p['partNumber']}",
                        "partSize": p["partSize"],
                    }
                    for p in parts
                ],
            },
        }

    monkeypatch.setattr(wrapper_mod.transport, "post_json", fake_post_json)

    slices = {}

    def fake_put(url, path):
        slices[url] = Path(path).read_bytes()

    monkeypatch.setattr(wrapper_mod.transport, "put_file_content", fake_put)

    completed = []
    monkeypatch.setattr(
        wrapper_mod.client, "api_file_complete",
        lambda uid, fid, digest: completed.append((uid, fid, digest)),
    )

    item = make_wrapper().upload_file(src, "rootdir")

    assert item.file_id == "F1"
    assert item.size == len(content)
    assert len(slices) >= 2
    # 后端要求严格按 partNumber 顺序上传，这里断言调用顺序
    assert list(slices.keys()) == [f"http://up/{i + 1}" for i in range(len(slices))]
    rebuilt = b"".join(slices[f"http://up/{i + 1}"] for i in range(len(slices)))
    assert rebuilt == content
    assert completed == [("U1", "F1", hashlib.sha256(content).hexdigest())]


def test_multipart_falls_back_to_single(monkeypatch, tmp_path):
    force_multipart(monkeypatch, threshold=1, min_part=1, max_parts=4)
    content = b"hello world" * 1000
    src = tmp_path / "f.bin"
    src.write_bytes(content)

    # 多段槽位返回空 parts -> _create_multipart_task 抛 ApiError -> 回退
    monkeypatch.setattr(
        wrapper_mod.transport, "post_json",
        lambda *a, **k: {"code": "0", "data": {"uploadId": "U", "fileId": "F", "partInfos": []}},
    )

    slot = SimpleNamespace(
        upload_id="U2", file_id="F2", parent_file_id="rootdir", upload_url="http://up/single"
    )
    monkeypatch.setattr(wrapper_mod.client, "api_file_create", lambda *a, **k: slot)
    put_calls = []
    monkeypatch.setattr(
        wrapper_mod.transport, "put_file_content",
        lambda url, path: put_calls.append((url, Path(path).read_bytes())),
    )
    completed = []
    monkeypatch.setattr(
        wrapper_mod.client, "api_file_complete",
        lambda uid, fid, digest: completed.append((uid, fid)),
    )

    item = make_wrapper().upload_file(src, "rootdir")

    assert item.file_id == "F2"
    assert put_calls == [("http://up/single", content)]
    assert completed == [("U2", "F2")]


# ---------------------------------------------------------------------------
# 统一调用封装
# ---------------------------------------------------------------------------

def test_api_retries_and_returns(monkeypatch):
    monkeypatch.setattr(
        wrapper_mod, "retry_call",
        functools.partial(retry_call, initial_delay=0.01, jitter=False),
    )
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ApiError("临时故障")
        return "OK"

    assert make_wrapper()._api(flaky) == "OK"
    assert calls["n"] == 3


def test_api_does_not_retry_permission_error(monkeypatch):
    from cm_cloud_errors import PermissionDeniedError

    monkeypatch.setattr(
        wrapper_mod, "retry_call",
        functools.partial(retry_call, initial_delay=0.01, jitter=False),
    )
    calls = {"n": 0}

    def denied():
        calls["n"] += 1
        raise PermissionDeniedError("不在沙箱")

    with pytest.raises(PermissionDeniedError):
        make_wrapper()._api(denied)
    assert calls["n"] == 1
