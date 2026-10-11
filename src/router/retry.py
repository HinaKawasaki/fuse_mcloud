import sys
import asyncio
import random
import logging
from functools import wraps
from pathlib import Path

# 动态注入 basement/scripts 路径以加载 ApiError / CloudError
BASEMENT_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "basement" / "scripts"
if str(BASEMENT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(BASEMENT_SCRIPTS_DIR))

from cm_cloud_errors import CloudError, ApiError

logger = logging.getLogger(__name__)

def with_retry(
    max_retries: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    retry_exceptions: tuple = (CloudError, TimeoutError, ConnectionError, OSError)
):
    """
    异步函数的指数退避重试装饰器
    :param max_retries: 最大重试次数
    :param initial_delay: 初始重试延迟 (秒)
    :param backoff_factor: 退避倍数
    :param jitter: 是否引入随机抖动，防止惊群效应
    :param retry_exceptions: 需要捕获并重试的异常类型
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retry_exceptions as e:
                    # 如果遇到 ApiError 且是不可恢复状态（如 401/403 凭据过期/拒绝访问），直接抛出不重试
                    if isinstance(e, ApiError) and getattr(e, "code", None) in [401, 403, 10001]:
                        logger.error(f"[Retry] 遇到不可恢复错误 (code={e.code}), 放弃重试: {e}")
                        raise e

                    if attempt == max_retries:
                        logger.error(f"[Retry] 已达到最大重试次数 ({max_retries}), 最终失败: {e}")
                        raise e

                    # 计算等待时间：delay * (backoff_factor ^ (attempt - 1))
                    sleep_time = delay * (backoff_factor ** (attempt - 1))
                    if jitter:
                        sleep_time += random.uniform(0, sleep_time * 0.1)

                    logger.warning(
                        f"[Retry] 捕获可恢复异常 ({type(e).__name__}: {e})，"
                        f"即将进行第 {attempt}/{max_retries} 次重试，等待 {sleep_time:.2f}s..."
                    )
                    await asyncio.sleep(sleep_time)

        return wrapper
    return decorator