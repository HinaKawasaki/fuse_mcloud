import logging
import random
import sys
import time
from pathlib import Path
from typing import Callable, Tuple, Type, TypeVar

# 动态注入 basement/scripts 以加载异常层次
BASEMENT_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "basement" / "scripts"
if str(BASEMENT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(BASEMENT_SCRIPTS_DIR))

from cm_cloud_errors import (  # noqa: E402
    ApiError,
    AuthError,
    CloudError,
    ConfigError,
    NeedAuthError,
    PermissionDeniedError,
    ValidationError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 默认需要重试的异常：网络类 + 接口临时失败
DEFAULT_RETRY_EXCEPTIONS: Tuple[Type[BaseException], ...] = (
    ApiError,
    ConnectionError,
    TimeoutError,
    OSError,
)

# 明确不该重试的异常：入参错误、权限/沙箱拒绝、需要登录
NON_RETRYABLE_EXCEPTIONS: Tuple[Type[BaseException], ...] = (
    PermissionDeniedError,
    ValidationError,
    ConfigError,
    NeedAuthError,
    AuthError,
)


def retry_call(
    func: Callable[..., T],
    *args,
    max_retries: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    retry_exceptions: Tuple[Type[BaseException], ...] = DEFAULT_RETRY_EXCEPTIONS,
    **kwargs,
) -> T:
    """以指数退避重试一个同步调用。

    ``max_retries`` 是**总尝试次数**（含首次）。权限/入参类异常直接抛出，
    不浪费重试次数。

    实现为同步阻塞（内部 ``time.sleep``），因为它总是运行在
    ``trio.to_thread.run_sync`` 派发的工作线程里，不会阻塞事件循环。
    """
    attempt = 0
    delay = initial_delay
    while True:
        attempt += 1
        try:
            return func(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 需要按类型判断是否重试
            if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
                raise
            if not isinstance(exc, retry_exceptions):
                raise
            if attempt >= max_retries:
                logger.error("[Retry] 已达最大尝试次数 %s，放弃: %s", max_retries, exc)
                raise
            sleep_time = delay * (backoff_factor ** (attempt - 1))
            if jitter:
                sleep_time += random.uniform(0, sleep_time * 0.1)
            logger.warning(
                "[Retry] %s 第 %s/%s 次失败(%s)，%.2fs 后重试",
                getattr(func, "__name__", "call"), attempt, max_retries,
                type(exc).__name__, sleep_time,
            )
            time.sleep(sleep_time)
