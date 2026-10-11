# 判断用户是否设置了缓存大小限制，如果没有则自动计算
import psutil
from src import config

def get_effective_cache_limit_mb() -> int:
    """获取实际生效的缓存容量限额 (MB)"""
    # 1. 如果用户在 config.py 中显式指定了数值 (如 2048)，直接使用
    if config.MAX_CACHE_SIZE_MB is not None and config.MAX_CACHE_SIZE_MB > 0:
        return config.MAX_CACHE_SIZE_MB

    # 2. 用户写了 None，由程序动态计算 (默认分配物理内存的 25%)
    try:
        total_ram_mb = psutil.virtual_memory().total // (1024 * 1024)
        return max(1024, total_ram_mb // 4)
    except Exception:
        return 2048  # 保底 2GB