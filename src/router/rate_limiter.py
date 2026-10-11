import asyncio
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class RateLimiter:
    """
    针对底层 API 的并发限流与速率控制
    1. 并发限制：基于 asyncio.Semaphore 限制同时发起的 API 请求数 (默认 2~3)
    2. 频率限制：控制单位时间内的最大请求数 (如每秒最多 N 次)
    """
    def __init__(self, max_concurrent: int = 3, requests_per_second: float = 5.0):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0
        self._last_request_time = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        """获取并发与速率许可"""
        await self.semaphore.acquire()
        
        # 速率间隔控制
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self.min_interval:
                sleep_time = self.min_interval - elapsed
                await asyncio.sleep(sleep_time)
            self._last_request_time = time.monotonic()

    def release(self):
        """释放并发信号量"""
        self.semaphore.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()

# 全局单例限流器 (控制在 3 个并发)
global_rate_limiter = RateLimiter(max_concurrent=3, requests_per_second=5.0)