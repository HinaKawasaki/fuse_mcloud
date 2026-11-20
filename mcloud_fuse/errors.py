"""与后端无关的异常类型，避免 FUSE/重试层依赖某个具体后端。

异常尽量带上三类结构化信息：

- ``status``：HTTP 状态码（401/404/429/5xx …）；
- ``code``：接口返回的业务错误码（字符串，可能是数字）；
- ``errno_hint``：建议映射到的 POSIX errno。

FUSE 层按 ``errno_hint`` 返回真实 errno，而不是把所有失败压成 ``EIO``。
这样 ``cp`` 能看到 ``ENOSPC``、``ls`` 能看到 ``EACCES``、``mv`` 能看到
``EEXIST``，应用不必靠读日志猜发生了什么。
"""

from __future__ import annotations

import errno
from typing import Optional

#: HTTP 状态码 → POSIX errno。只列语义明确的，其余 4xx / 5xx 走兜底。
_STATUS_ERRNO = {
    400: errno.EINVAL,
    401: errno.EACCES,
    403: errno.EACCES,
    404: errno.ENOENT,
    405: errno.ENOSYS,
    408: errno.ETIMEDOUT,
    409: errno.EEXIST,
    410: errno.ENOENT,
    412: errno.ESTALE,
    413: errno.EFBIG,
    416: errno.EINVAL,
    423: errno.EBUSY,
    425: errno.EAGAIN,
    429: errno.EAGAIN,
    503: errno.EBUSY,
    507: errno.ENOSPC,
}

#: 可以安全重试的状态码：限流、超时、服务端临时故障。
_RETRYABLE_STATUS = (408, 425, 429)


def _as_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def errno_for_status(status=None, code=None) -> Optional[int]:
    """把 HTTP 状态码 / 业务码翻译成 errno；翻译不了返回 ``None``。"""
    number = _as_int(status)
    if number is None:
        number = _as_int(code)
    if number is None:
        return None
    if number in _STATUS_ERRNO:
        return _STATUS_ERRNO[number]
    if 500 <= number < 600:
        return errno.EIO
    if 400 <= number < 500:
        return errno.EINVAL
    return None


def retryable_status(status) -> bool:
    """5xx / 408 / 429 值得重试；其余 4xx 是确定性失败。"""
    number = _as_int(status)
    if number is None:
        return True  # 拿不到状态码（连接被重置等）时交给重试策略判断
    return number in _RETRYABLE_STATUS or number >= 500


class MCloudAPIException(Exception):
    """云盘 API 统一异常。"""

    def __init__(
        self,
        message: str = "",
        *,
        code: Optional[str] = None,
        status: Optional[int] = None,
        errno_hint: Optional[int] = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = _as_int(status)
        self.errno_hint = (
            errno_hint
            if errno_hint is not None
            else errno_for_status(self.status, code)
        )

    @property
    def retryable(self) -> bool:
        return retryable_status(self.status)


class MCloudAuthException(MCloudAPIException):
    """登录态缺失、过期或续期失败。"""

    def __init__(self, message: str = "", **kwargs):
        kwargs.setdefault("errno_hint", errno.EACCES)
        super().__init__(message, **kwargs)


class MCloudConflictError(MCloudAPIException):
    """覆盖写冲突：远端在本地打开后已被修改。"""


def api_error_from(
    exc: BaseException, *, message: Optional[str] = None, auth: bool = False
):
    """把后端异常（``Web139Error`` 等）转成带结构化信息的统一异常。

    按鸭子类型读取 ``status`` / ``code``，不 import 具体后端，避免循环依赖。
    """
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    text = str(exc) if message is None else message
    cls = MCloudAuthException if auth else MCloudAPIException
    return cls(text, code=code, status=status)
