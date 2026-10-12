"""限流器与退避重试的离线测试（同步/线程安全实现）。"""
import threading
import time

import pytest
from cm_cloud_errors import ApiError, PermissionDeniedError

from router.rate_limiter import RateLimiter, global_rate_limiter
from router.retry import retry_call


def test_concurrency_limit():
    """并发上限必须被严格遵守。"""
    limit = 3
    limiter = RateLimiter(max_concurrent=limit, requests_per_second=0)
    active = 0
    max_seen = 0
    lock = threading.Lock()

    def worker():
        nonlocal active, max_seen
        with limiter:
            with lock:
                active += 1
                max_seen = max(max_seen, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_seen <= limit, f"并发超过上限: {max_seen}"
    assert max_seen >= 2, "并发没有真正并行，限流器可能有 bug"


def test_rate_pacing():
    """两次请求之间应满足最小间隔。"""
    limiter = RateLimiter(max_concurrent=10, requests_per_second=20)  # 间隔 50ms
    start = time.monotonic()
    for _ in range(5):
        with limiter:
            pass
    elapsed = time.monotonic() - start
    # 5 次请求至少 4 个间隔 => >= 0.2s（留一点调度余量）
    assert elapsed >= 0.16, f"速率限制未生效，耗时仅 {elapsed:.3f}s"


def test_retry_succeeds_after_transient_failures():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ApiError("临时网络故障")
        return "OK"

    result = retry_call(flaky, max_retries=3, initial_delay=0.01, jitter=False)
    assert result == "OK"
    assert attempts["n"] == 3


def test_retry_gives_up():
    attempts = {"n": 0}

    def always_fail():
        attempts["n"] += 1
        raise ApiError("一直失败")

    with pytest.raises(ApiError):
        retry_call(always_fail, max_retries=2, initial_delay=0.01, jitter=False)
    assert attempts["n"] == 2


def test_non_retryable_not_retried():
    """权限类异常应立即抛出，不浪费重试次数。"""
    attempts = {"n": 0}

    def denied():
        attempts["n"] += 1
        raise PermissionDeniedError("不在沙箱内")

    with pytest.raises(PermissionDeniedError):
        retry_call(denied, max_retries=5, initial_delay=0.01, jitter=False)
    assert attempts["n"] == 1


def test_global_limiter_matches_config():
    import config

    assert global_rate_limiter.max_concurrent == config.API_MAX_CONCURRENT
