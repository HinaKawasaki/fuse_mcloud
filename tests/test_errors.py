"""结构化异常：HTTP 状态 / 业务码 → errno，以及「哪些失败值得重试」。"""

from __future__ import annotations

import errno

import pytest

from mcloud_fuse.errors import (
    MCloudAPIException,
    MCloudAuthException,
    MCloudConflictError,
    api_error_from,
    errno_for_status,
    retryable_status,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, errno.EINVAL),
        (401, errno.EACCES),
        (403, errno.EACCES),
        (404, errno.ENOENT),
        (409, errno.EEXIST),
        (412, errno.ESTALE),
        (413, errno.EFBIG),
        (429, errno.EAGAIN),
        (503, errno.EBUSY),
        (507, errno.ENOSPC),
        (500, errno.EIO),
        (599, errno.EIO),
        (422, errno.EINVAL),
        ("404", errno.ENOENT),
        (None, None),
        ("not-a-number", None),
    ],
)
def test_errno_for_status(status, expected):
    assert errno_for_status(status) == expected


def test_errno_falls_back_to_business_code():
    # 有些接口把状态放在业务码里，没有 HTTP status
    assert errno_for_status(None, "404") == errno.ENOENT
    assert errno_for_status(None, "507") == errno.ENOSPC
    assert errno_for_status(None, "abc") is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (500, True),
        (502, True),
        (408, True),
        (429, True),
        (404, False),
        (403, False),
        (None, True),  # 网络层异常，交给重试策略判断
    ],
)
def test_retryable_status(status, expected):
    assert retryable_status(status) is expected


def test_api_error_carries_status_code_and_errno():
    exc = MCloudAPIException("限流", status=429, code="429")
    assert exc.status == 429
    assert exc.code == "429"
    assert exc.errno_hint == errno.EAGAIN
    assert exc.retryable is True

    not_found = MCloudAPIException("不存在", status=404)
    assert not_found.errno_hint == errno.ENOENT
    assert not_found.retryable is False


def test_api_error_without_status_keeps_errno_none():
    exc = MCloudAPIException("普通失败")
    assert exc.status is None
    assert exc.errno_hint is None
    assert exc.retryable is True


def test_auth_exception_defaults_to_eacces():
    exc = MCloudAuthException("token 过期")
    assert exc.errno_hint == errno.EACCES
    already = MCloudAuthException("token 过期", errno_hint=errno.ENOENT)
    assert already.errno_hint == errno.ENOENT


def test_conflict_exception_is_api_exception():
    assert issubclass(MCloudConflictError, MCloudAPIException)


class _BackendError(Exception):
    """模拟 Web139Error：带 status/code 但不是统一异常类型。"""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def test_api_error_from_preserves_backend_details():
    exc = api_error_from(_BackendError("接口失败", status=507))
    assert exc.status == 507
    assert exc.errno_hint == errno.ENOSPC
    assert "接口失败" in str(exc)


def test_api_error_from_can_override_message_and_mark_auth():
    exc = api_error_from(_BackendError("HTTP 401", status=401), auth=True, message="token 失效")
    assert isinstance(exc, MCloudAuthException)
    assert str(exc) == "token 失效"
    assert exc.status == 401
    assert exc.errno_hint == errno.EACCES
