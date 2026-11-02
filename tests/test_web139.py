"""yun.139.com Web 适配层（L3）的离线协议测试。

这些用例不访问网络：所有 HTTP 交互都由 FakeSession 接管，用来锁定
请求体、签名头、分页、分片上传与 token 续期的行为。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from mcloud_fuse.client import web139
from mcloud_fuse.client.web139 import (
    Web139Auth,
    Web139AuthError,
    Web139Client,
    Web139Error,
    Web139Token,
    mcloud_sign,
)


def make_token(account: str = "13800000000", expires_in_ms: int = 30 * 24 * 3600 * 1000) -> str:
    expiry = int(time.time() * 1000) + expires_in_ms
    payload = f"v1|sig|opaque|{expiry}"
    return base64.b64encode(f"0:{account}:{payload}".encode()).decode()


class FakeResponse:
    def __init__(self, status_code: int = 200, doc: Any = None, content: bytes = b""):
        self.status_code = status_code
        self._doc = doc if doc is not None else {"success": True, "data": {}}
        self.content = content or json.dumps(self._doc).encode()
        self.text = self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return self._doc


class FakeSession:
    """按 (method, path) 分派的假会话，记录全部调用。"""

    def __init__(self, routes: Dict[Tuple[str, str], Any]):
        self.routes = routes
        self.calls: List[Dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        parsed = urllib.parse.urlparse(url)
        handler = self.routes.get((method, url)) or self.routes.get((method, parsed.path))
        if handler is None:
            raise AssertionError(f"未预期的请求：{method} {url}")
        if callable(handler):
            return handler(kwargs)
        return handler

    def calls_to(self, method: str, path: str) -> List[Dict[str, Any]]:
        return [
            call
            for call in self.calls
            if call["method"] == method and call["url"].endswith(path)
        ]


def make_client(
    routes: Dict[Tuple[str, str], Any],
    *,
    token: Optional[str] = None,
    part_size: int = 100 * 1024 * 1024,
) -> Tuple[Web139Client, FakeSession]:
    session = FakeSession(routes)
    auth = Web139Auth(Web139Token.parse(token or make_token()))
    client = Web139Client(
        auth,
        session=session,  # type: ignore[arg-type]
        base_url="https://yun.139.com",
        part_size=part_size,
        rate_limiter=None,
    )
    return client, session


def body_of(call: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(call["data"].decode())


# --------------------------------------------------------------------- token


def test_token_parse_accepts_basic_prefix():
    raw = make_token("13912345678")
    token = Web139Token.parse("Basic " + raw)
    assert token.account == "13912345678"
    assert token.to_basic() == raw
    assert token.needs_refresh() is False


def test_token_parse_rejects_garbage():
    with pytest.raises(Web139AuthError):
        Web139Token.parse("not-base64!!")
    with pytest.raises(Web139AuthError):
        Web139Token.parse(base64.b64encode(b"only:two").decode())


def test_token_expiry_and_refresh_window():
    expired = Web139Token.parse(make_token(expires_in_ms=-1000))
    assert expired.is_expired() is True
    near = Web139Token.parse(make_token(expires_in_ms=10 * 24 * 3600 * 1000))
    assert near.needs_refresh() is True


def test_auth_refreshes_and_saves_private_file(tmp_path: Path):
    new_payload = f"v2|sig|opaque|{int(time.time() * 1000) + 40 * 24 * 3600 * 1000}"
    xml = (
        f"<root><return>0</return><token>{new_payload}</token>"
        "<expiretime>3456000</expiretime><desc>ok</desc></root>"
    ).encode()
    session = FakeSession(
        {("POST", "/tellin/authTokenRefresh.do"): FakeResponse(200, content=xml)}
    )
    path = tmp_path / "web_token"
    auth = Web139Auth(Web139Token.parse(make_token(expires_in_ms=24 * 3600 * 1000)), path)

    basic = auth.authorization(session)  # type: ignore[arg-type]

    parsed = Web139Token.parse(basic)
    assert parsed.payload == new_payload
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text().strip() == basic
    refresh_body = session.calls[0]["data"].decode()
    assert "<clienttype>656</clienttype>" in refresh_body


def test_auth_refresh_rejected_raises(tmp_path: Path):
    xml = b"<root><return>-1</return><desc>expired</desc></root>"
    session = FakeSession(
        {("POST", "/tellin/authTokenRefresh.do"): FakeResponse(200, content=xml)}
    )
    auth = Web139Auth(Web139Token.parse(make_token(expires_in_ms=1000)), tmp_path / "t")
    with pytest.raises(Web139AuthError, match="expired"):
        auth.authorization(session)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- sign


def test_sign_is_deterministic_uppercase_md5():
    body = '{"a":1,"b":"中文 空格!"}'
    first = mcloud_sign(body, "2026-10-28 12:00:00", "abcdef")
    assert first == mcloud_sign(body, "2026-10-28 12:00:00", "abcdef")
    assert len(first) == 32 and first == first.upper()
    assert first != mcloud_sign(body, "2026-10-28 12:00:01", "abcdef")


def test_sign_uses_encode_uri_component_semantics():
    # 空格应编码为 %20，! ' ( ) * 保持原样
    encoded = web139.urllib.parse.quote("a b!'()*~", safe=web139._UNRESERVED)
    assert encoded == "a%20b!'()*~"


# ---------------------------------------------------------------------- list


def test_list_paginates_and_maps_fields():
    def page_one(_kwargs):
        return FakeResponse(
            200,
            {
                "success": True,
                "data": {
                    "items": [
                        {
                            "fileId": "f1",
                            "name": "docs",
                            "type": "folder",
                            "size": 0,
                            "createdAt": "2026-01-01T00:00:00+08:00",
                            "updatedAt": "2026-01-02T00:00:00+08:00",
                        }
                    ],
                    "nextPageCursor": "c1",
                },
            },
        )

    def page_two(_kwargs):
        return FakeResponse(
            200,
            {
                "success": True,
                "data": {
                    "items": [
                        {
                            "fileId": "f2",
                            "name": "a.bin",
                            "type": "file",
                            "size": 123,
                            "createdAt": "x",
                            "updatedAt": "y",
                        }
                    ]
                },
            },
        )

    client, session = make_client(
        {
            ("POST", "/file/list"): lambda kw: page_one(kw)
            if body_of(kw)["pageInfo"]["pageCursor"] == ""
            else page_two(kw)
        }
    )
    items = client.list_files("parent-1")

    assert [(i.file_id, i.name, i.is_dir, i.size) for i in items] == [
        ("f1", "docs", True, 0),
        ("f2", "a.bin", False, 123),
    ]
    bodies = [body_of(c) for c in session.calls_to("POST", "/file/list")]
    assert [b["pageInfo"]["pageCursor"] for b in bodies] == ["", "c1"]
    assert bodies[0]["parentFileId"] == "parent-1"
    assert bodies[0]["pageInfo"]["pageSize"] == 500


def test_get_download_url_prefers_cdn():
    client, _ = make_client(
        {
            ("POST", "/file/getDownloadUrl"): FakeResponse(
                200, {"success": True, "data": {"cdnUrl": "https://cdn/x", "url": "https://u/x"}}
            )
        }
    )
    assert client.get_download_url("f1") == "https://cdn/x"


def test_api_error_message_surfaces():
    client, _ = make_client(
        {
            ("POST", "/file/list"): FakeResponse(
                200, {"success": False, "code": "500", "message": "boom"}
            )
        }
    )
    with pytest.raises(Web139Error, match="boom"):
        client.list_files()


# ----------------------------------------------------------------------- ops


def test_create_folder_uses_refuse_mode():
    client, session = make_client(
        {("POST", "/file/create"): FakeResponse(200, {"success": True, "data": {"fileId": "d1"}})}
    )
    assert client.create_folder("parent", "newdir") == "d1"
    body = body_of(session.calls[0])
    assert body == {
        "parentFileId": "parent",
        "name": "newdir",
        "description": "",
        "type": "folder",
        "fileRenameMode": "refuse",
    }


def test_rename_move_copy_trash_payloads():
    routes = {
        ("POST", "/file/update"): FakeResponse(200, {"success": True}),
        ("POST", "/file/batchMove"): FakeResponse(200, {"success": True}),
        ("POST", "/file/batchCopy"): FakeResponse(200, {"success": True}),
        ("POST", "/file/batchDelete"): FakeResponse(200, {"success": True}),
        ("POST", "/recyclebin/batchTrash"): FakeResponse(200, {"success": True}),
    }
    client, session = make_client(routes)
    client.rename("f1", "renamed")
    client.move(["f1", "f2"], "dst")
    client.copy(["f3"], "dst")
    client.trash(["f4"])

    assert body_of(session.calls_to("POST", "/file/update")[0])["name"] == "renamed"
    assert body_of(session.calls_to("POST", "/file/batchMove")[0]) == {
        "fileIds": ["f1", "f2"],
        "toParentFileId": "dst",
    }
    assert body_of(session.calls_to("POST", "/file/batchCopy")[0])["fileIds"] == ["f3"]
    assert body_of(session.calls_to("POST", "/recyclebin/batchTrash")[0])["fileIds"] == ["f4"]


# -------------------------------------------------------------------- upload


def test_upload_single_part_then_complete(tmp_path: Path):
    payload = b"hello world"
    src = tmp_path / "hello.txt"
    src.write_bytes(payload)

    uploaded = {}

    def put(kwargs):
        uploaded["bytes"] = kwargs["data"].read()
        return FakeResponse(200, content=b"")

    create = FakeResponse(
        200,
        {
            "success": True,
            "data": {
                "fileId": "new1",
                "uploadId": "up1",
                "fileName": "hello.txt",
                "partInfos": [{"partNumber": 1, "uploadUrl": "https://eos/part1"}],
            },
        },
    )
    complete = FakeResponse(200, {"success": True})
    client, session = make_client(
        {
            ("POST", "/file/create"): create,
            ("PUT", "https://eos/part1"): put,
            ("POST", "/file/complete"): complete,
        }
    )

    file_id = client.upload_file(src, "parent")

    assert file_id == "new1"
    create_body = body_of(session.calls_to("POST", "/file/create")[0])
    assert create_body["size"] == len(payload)
    assert create_body["contentHashAlgorithm"] == "SHA256"
    assert create_body["partInfos"] == [
        {"partNumber": 1, "partSize": len(payload), "parallelHashCtx": {"partOffset": 0}}
    ]
    assert uploaded["bytes"] == payload
    done = body_of(session.calls_to("POST", "/file/complete")[0])
    assert done["fileId"] == "new1" and done["uploadId"] == "up1"


def test_upload_more_than_100_parts_fetches_more_urls(tmp_path: Path):
    src = tmp_path / "blob.bin"
    src.write_bytes(b"x" * 150)

    def create_handler(_kwargs):
        return FakeResponse(
            200,
            {
                "success": True,
                "data": {
                    "fileId": "big1",
                    "uploadId": "upbig",
                    "fileName": "blob.bin",
                    "partInfos": [
                        {"partNumber": i, "uploadUrl": f"https://eos/{i}"}
                        for i in range(1, 101)
                    ],
                },
            },
        )

    def more_handler(kwargs):
        body = body_of(kwargs)
        assert body["uploadId"] == "upbig"
        return FakeResponse(
            200,
            {
                "success": True,
                "data": {
                    "partInfos": [
                        {
                            "partNumber": p["partNumber"],
                            "uploadUrl": f"https://eos/{p['partNumber']}",
                        }
                        for p in body["partInfos"]
                    ]
                },
            },
        )

    routes = {
        ("POST", "/file/create"): create_handler,
        ("POST", "/file/getUploadUrl"): more_handler,
        ("POST", "/file/complete"): FakeResponse(200, {"success": True}),
    }
    for i in range(1, 151):
        routes[("PUT", f"https://eos/{i}")] = FakeResponse(200, content=b"")
    client, session = make_client(routes, part_size=1)

    assert client.upload_file(src, "parent") == "big1"
    more = session.calls_to("POST", "/file/getUploadUrl")
    assert len(more) == 1
    assert [p["partNumber"] for p in body_of(more[0])["partInfos"]] == list(range(101, 151))
    assert len([c for c in session.calls if c["method"] == "PUT"]) == 150


def test_upload_existing_content_skips_parts(tmp_path: Path):
    src = tmp_path / "dup.bin"
    src.write_bytes(b"same")
    client, session = make_client(
        {
            ("POST", "/file/create"): FakeResponse(
                200,
                {"success": True, "data": {"fileId": "dup1", "fileName": "dup.bin", "exist": True}},
            )
        }
    )
    assert client.upload_file(src, "parent") == "dup1"
    assert [c for c in session.calls if c["method"] == "PUT"] == []
    assert session.calls_to("POST", "/file/complete") == []


def test_upload_conflict_replaces_old_object(tmp_path: Path):
    src = tmp_path / "x.bin"
    src.write_bytes(b"new")

    list_calls = {"n": 0}

    def list_handler(_kwargs):
        list_calls["n"] += 1
        return FakeResponse(
            200,
            {
                "success": True,
                "data": {
                    "items": [
                        {"fileId": "old", "name": "x.bin", "type": "file", "size": 3},
                        {"fileId": "new", "name": "x.bin(1)", "type": "file", "size": 3},
                    ]
                },
            },
        )

    routes = {
        ("POST", "/file/list"): list_handler,
        ("POST", "/file/create"): FakeResponse(
            200,
            {
                "success": True,
                "data": {
                    "fileId": "new",
                    "uploadId": "u",
                    "fileName": "x.bin(1)",
                    "partInfos": [{"partNumber": 1, "uploadUrl": "https://eos/p"}],
                },
            },
        ),
        ("PUT", "https://eos/p"): FakeResponse(200, content=b""),
        ("POST", "/file/complete"): FakeResponse(200, {"success": True}),
        ("POST", "/file/update"): FakeResponse(200, {"success": True}),
        ("POST", "/file/batchDelete"): FakeResponse(200, {"success": True}),
    }
    client, session = make_client(routes)
    assert client.upload_file(src, "parent") == "new"

    renames = [body_of(c)["name"] for c in session.calls_to("POST", "/file/update")]
    assert renames[0].startswith("x.bin.mcloud-old-")
    assert renames[1] == "x.bin"
    assert body_of(session.calls_to("POST", "/file/batchDelete")[0])["fileIds"] == ["old"]


# --------------------------------------------------------------------- route


def test_host_resolved_from_route_policy():
    route = FakeResponse(
        200,
        {
            "success": True,
            "data": {
                "routePolicyList": [
                    {"modName": "other", "httpsUrl": "https://other.example"},
                    {"modName": "personal", "httpsUrl": "https://p.example/"},
                ]
            },
        },
    )
    client, session = make_client(
        {
            ("POST", "/user/route/qryRoutePolicy"): route,
            ("POST", "/file/list"): FakeResponse(200, {"success": True, "data": {"items": []}}),
        }
    )
    client._base_url = None
    client._host = None
    client.list_files("/")
    assert session.calls[0]["url"] == web139.ROUTE_URL
    assert session.calls[-1]["url"] == "https://p.example/file/list"


def test_delete_permanent_uses_batch_delete():
    client, session = make_client(
        {("POST", "/file/batchDelete"): FakeResponse(200, {"success": True})}
    )
    client.delete(["f1", "f2"])
    assert body_of(session.calls_to("POST", "/file/batchDelete")[0]) == {
        "fileIds": ["f1", "f2"]
    }


def test_delete_soft_uses_recyclebin():
    client, session = make_client(
        {("POST", "/recyclebin/batchTrash"): FakeResponse(200, {"success": True})}
    )
    client.delete(["f1"], permanent=False)
    assert body_of(session.calls_to("POST", "/recyclebin/batchTrash")[0])["fileIds"] == ["f1"]


def test_clear_recycle_bin():
    client, session = make_client(
        {("POST", "/recyclebin/clear"): FakeResponse(200, {"success": True})}
    )
    client.clear_recycle_bin()
    assert session.calls_to("POST", "/recyclebin/clear")


def test_get_file_maps_metadata():
    client, _ = make_client(
        {
            ("POST", "/file/get"): FakeResponse(
                200,
                {
                    "success": True,
                    "data": {
                        "fileId": "f9",
                        "name": "x.bin",
                        "type": "file",
                        "size": 42,
                        "createdAt": "c",
                        "updatedAt": "u",
                        "parentFileId": "p",
                    },
                },
            )
        }
    )
    item = client.get_file("f9")
    assert (item.file_id, item.name, item.size, item.is_dir) == ("f9", "x.bin", 42, False)


def test_delete_waits_until_async_task_succeeds():
    seen = {"n": 0}

    def task_status(_kwargs):
        seen["n"] += 1
        status = "Running" if seen["n"] == 1 else "Succeed"
        return FakeResponse(
            200, {"success": True, "data": {"taskInfo": {"taskId": "t1", "status": status}}}
        )

    client, session = make_client(
        {
            ("POST", "/file/batchDelete"): FakeResponse(
                200, {"success": True, "data": {"taskId": "t1"}}
            ),
            ("POST", "/task/get"): task_status,
        }
    )
    old = web139.TASK_POLL_INTERVAL
    web139.TASK_POLL_INTERVAL = 0.01
    try:
        client.delete(["f1"])
    finally:
        web139.TASK_POLL_INTERVAL = old
    assert seen["n"] == 2, "必须轮询到 Succeed 才能返回"
    assert body_of(session.calls_to("POST", "/task/get")[0])["taskId"] == "t1"


def test_delete_raises_when_async_task_fails():
    client, _ = make_client(
        {
            ("POST", "/file/batchDelete"): FakeResponse(
                200, {"success": True, "data": {"taskId": "t2"}}
            ),
            ("POST", "/task/get"): FakeResponse(
                200,
                {
                    "success": True,
                    "data": {
                        "taskInfo": {"taskId": "t2", "status": "Failed", "message": "denied"}
                    },
                },
            ),
        }
    )
    with pytest.raises(Web139Error, match="denied"):
        client.delete(["f1"])


def test_quota_parses_mb_and_used():
    client, _ = make_client(
        {
            ("POST", web139.QUOTA_URL): FakeResponse(
                200,
                {
                    "success": True,
                    "data": {
                        "diskSize": 1000,
                        "freeDiskSize": 400,
                        "quotaList": [{"driveType": 1, "usedSize": 600}],
                    },
                },
            )
        }
    )
    quota = client.quota()
    assert (quota.total_mb, quota.free_mb, quota.used_mb) == (1000, 400, 600)


def test_recyclebin_list_paginates():
    def page(_kwargs):
        cursor = body_of(_kwargs)["pageInfo"]["pageCursor"]
        if cursor == "":
            return FakeResponse(
                200,
                {
                    "success": True,
                    "data": {
                        "items": [
                            {
                                "fileId": "r1",
                                "name": "old.txt",
                                "type": "file",
                                "size": 12,
                                "trashedAt": "2026-10-01T00:00:00+08:00",
                                "remainingTime": 30,
                            }
                        ],
                        "nextPageCursor": "c1",
                    },
                },
            )
        return FakeResponse(
            200,
            {
                "success": True,
                "data": {
                    "items": [
                        {
                            "fileId": "r2",
                            "name": "dir",
                            "type": "folder",
                            "size": 0,
                            "trashedAt": "2026-10-02T00:00:00+08:00",
                            "remainingTime": 20,
                            "trashedDirStatistics": {"totalSize": 1024},
                        }
                    ]
                },
            },
        )

    client, session = make_client({("POST", "/recyclebin/list"): page})
    items = client.list_recycle_bin()
    assert [(i.file_id, i.is_dir, i.size, i.remaining_days) for i in items] == [
        ("r1", False, 12, 30),
        ("r2", True, 1024, 20),
    ]
    assert [body_of(c)["pageInfo"]["pageCursor"] for c in session.calls_to(
        "POST", "/recyclebin/list"
    )] == ["", "c1"]


def test_restore_recycle_waits_for_task():
    client, session = make_client(
        {
            ("POST", "/recyclebin/batchRestore"): FakeResponse(
                200, {"success": True, "data": {"taskId": "t9"}}
            ),
            ("POST", "/task/get"): FakeResponse(
                200,
                {"success": True, "data": {"taskInfo": {"taskId": "t9", "status": "Succeed"}}},
            ),
        }
    )
    client.restore_recycle(["r1", "r2"])
    assert body_of(session.calls_to("POST", "/recyclebin/batchRestore")[0]) == {
        "fileIds": ["r1", "r2"]
    }
    assert session.calls_to("POST", "/task/get")
