import logging
import threading
import time

logger = logging.getLogger(__name__)


class RateLimiter:
    """对云盘 API 的并发与速率控制（线程安全，供同步客户端层使用）。

    为什么不用 asyncio/trio 的 Semaphore：所有 API 调用都发生在
    ``trio.to_thread.run_sync`` 派发的工作线程里，且 ``DirectClientWrapper`` 也
    会被脚本直接同步调用。因此这里用 ``threading`` 原语实现，任何线程都能用。

    - 并发限制：同时进行的 API 请求数上限，避免触发风控。
    - 速率限制：两次请求之间的最小间隔，模拟人工节奏。
    """

    def __init__(self, max_concurrent: int = 3, requests_per_second: float = 5.0):
        if max_concurrent < 1:
            raise ValueError("max_concurrent 必须 >= 1")
        self.max_concurrent = int(max_concurrent)
        self._semaphore = threading.BoundedSemaphore(self.max_concurrent)
        self.requests_per_second = float(requests_per_second)
        self.min_interval = 1.0 / self.requests_per_second if self.requests_per_second > 0 else 0.0
        self._pace_lock = threading.Lock()
        self._last_request_at = 0.0

    def acquire(self) -> None:
        """获取一个请求许可（必要时按速率间隔等待）。"""
        self._semaphore.acquire()
        try:
            if self.min_interval > 0:
                with self._pace_lock:
                    now = time.monotonic()
                    wait = self.min_interval - (now - self._last_request_at)
                    if wait > 0:
                        time.sleep(wait)
                    self._last_request_at = time.monotonic()
        except BaseException:
            self._semaphore.release()
            raise

    def release(self) -> None:
        self._semaphore.release()

    def __enter__(self) -> "RateLimiter":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def make_rate_limiter() -> RateLimiter:
    """按 config 中的配置构造限流器。"""
    from mcloud_fuse import config
    return RateLimiter(
        max_concurrent=config.API_MAX_CONCURRENT,
        requests_per_second=config.API_REQUESTS_PER_SECOND,
    )


# 全局单例：所有元数据 / 控制类 API 调用共用
global_rate_limiter = make_rate_limiter()
