import sys
import asyncio
import time
from pathlib import Path

# 添加 src 到 PYTHONPATH
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from router.rate_limiter import global_rate_limiter
from router.retry import with_retry

# 记录全局并发数
active_tasks = 0
max_observed_concurrent = 0

async def simulated_api_call(task_id: int):
    global active_tasks, max_observed_concurrent
    
    # 模拟经过限流器
    async with global_rate_limiter:
        active_tasks += 1
        max_observed_concurrent = max(max_observed_concurrent, active_tasks)
        print(f"  [Task {task_id}] 进入 API 调用, 当前并发数: {active_tasks}")
        
        # 模拟耗时网络请求
        await asyncio.sleep(0.2)
        
        active_tasks -= 1
        print(f"  [Task {task_id}] 完成 API 调用, 剩余并发数: {active_tasks}")

mock_fail_count = 0

@with_retry(max_retries=3, initial_delay=0.1)
async def simulated_flaky_api():
    """模拟前 2 次失败、第 3 次成功的不稳定 API"""
    global mock_fail_count
    mock_fail_count += 1
    if mock_fail_count < 3:
        print(f"  [Flaky API] 模拟网络异常 (尝试第 {mock_fail_count} 次)")
        raise ConnectionError("模拟的临时网络中断")
    print(f"  [Flaky API] 成功返回数据！")
    return "OK"

async def main():
    print("🚀 1. 测试并发限制 (目标并发上限 <= 3)...")
    tasks = [simulated_api_call(i) for i in range(10)]
    start_time = time.monotonic()
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start_time
    
    print(f"✅ 并发测试完成! 耗时: {elapsed:.2f}s, 观测到的最高并发数: {max_observed_concurrent}")
    assert max_observed_concurrent <= 3, "并发数量超过了预设限制！"

    print("\n🚀 2. 测试指数退避重试...")
    res = await simulated_flaky_api()
    print(f"✅ 重试机制测试完成! 最终结果: {res}")

if __name__ == "__main__":
    asyncio.run(main())