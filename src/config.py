# src/config.py
"""
MCloud FUSE 中间层配置文件
用户可直接在此修改配置，或通过环境变量覆盖。
"""

import os
from pathlib import Path

# ==================== 1. 缓存与路径配置 ====================
# 内存/临时缓存目录 (默认放在 /tmp 内存文件系统中)
CACHE_DIR = Path(os.getenv("MCLOUD_CACHE_DIR", "/tmp/cmcloud-fuse/cache"))

# 写回缓存的本地暂存目录：write() 先落这里，release()/fsync() 再整文件上传
WRITE_BACK_DIR = Path(os.getenv("MCLOUD_WRITE_BACK_DIR", "/tmp/cmcloud-fuse/writeback"))

# 缓存容量最大限制 (单位: MB)
# - 设置为固定数值 (如 2048): 严格限制缓存上限为 2GB
# - 设置为 None: 由程序自动探测系统可用内存并动态设定 (默认分配 25% RAM)
MAX_CACHE_SIZE_MB = None  # 用户可直接修改此数字

# ==================== 2. 元数据与 TTL 策略 ====================
META_CACHE_TTL = 60        # getattr / readdir 元数据缓存有效时间 (秒)

# ==================== 3. 沙箱与目录映射 ====================
SANDBOX_ROOT = "/AI空间/智能体云盘助手"
TRASH_BIN_DIR_NAME = ".TrashBin"

# 沙箱外文件的懒搬运暂存目录（位于沙箱根下）。
# 官方接口只允许下载沙箱内文件，因此读取沙箱外文件时先云内复制到这里的
# 每文件子目录，再走普通下载；该目录与其子项在目录列举中隐藏。
HYDRATE_DIR_NAME = os.getenv("MCLOUD_HYDRATE_DIR", ".MCloudHydrate")

# 挂载根：
# - sandbox（默认）：只暴露沙箱 /AI空间/智能体云盘助手，行为与以前一致；
# - cloud：暴露整个云盘根目录，沙箱外文件读取时按需搬进沙箱暂存目录；
#   写操作仍只允许落在沙箱内，越界由官方 Skill 拒绝（表现为 EACCES）。
ROOT_MODE = os.getenv("MCLOUD_ROOT", "sandbox").strip().lower()
ROOT_MODE_SANDBOX = "sandbox"
ROOT_MODE_CLOUD = "cloud"
CLOUD_ROOT_FILE_ID = "root"

# ==================== 4. 并发与限流 ====================
API_MAX_CONCURRENT = int(os.getenv("MCLOUD_API_MAX_CONCURRENT", "3"))
API_REQUESTS_PER_SECOND = float(os.getenv("MCLOUD_API_RPS", "5"))

# ==================== 5. 大文件多段上传 ====================
# 超过该大小改用多段并发上传（单段也能传，多段是为了提速与分片重试）
MULTIPART_THRESHOLD_BYTES = int(
    os.getenv("MCLOUD_MULTIPART_THRESHOLD", str(16 * 1024 * 1024))
)
# 云盘接口强制的最小分片大小（实测 < 5MiB 会被判“请求参数不合法”）
MIN_PART_SIZE_BYTES = int(os.getenv("MCLOUD_MIN_PART_SIZE", str(5 * 1024 * 1024)))
# 分片数量上限（分片大小据此向上取整，保证总片数不超过它）
MAX_PARTS = int(os.getenv("MCLOUD_MAX_PARTS", "64"))
# 单个分片上传的尝试次数
PART_RETRIES = int(os.getenv("MCLOUD_PART_RETRIES", "3"))
# 注：该后端要求分片严格按序上传（InvalidPartOrder），因此不提供并发分片。

# ==================== 6. 写回收尾策略 ====================
# 上传完成后是否把写回文件保留为本地读缓存（改名进缓存目录，零拷贝）。
# 置 0 则上传后直接删除，适合本地磁盘紧张、且不在意刚写完立刻重读还要下次下载的场景。
UPLOAD_KEEP_CACHE = os.getenv("MCLOUD_UPLOAD_KEEP_CACHE", "1").strip().lower() not in (
    "0", "false", "no",
)


def get_effective_cache_limit_mb() -> int:
    """获取实际生效的缓存容量限额 (MB)。

    用户在 config.MAX_CACHE_SIZE_MB 显式配置时直接使用；
    为 None 时读取 /proc/meminfo，分配物理内存的 25%（至少 1GB，兜底 2GB）。
    """
    if MAX_CACHE_SIZE_MB is not None and MAX_CACHE_SIZE_MB > 0:
        return int(MAX_CACHE_SIZE_MB)

    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                    return max(1024, total_kb // 1024 // 4)
    except (OSError, ValueError, IndexError):
        pass
    return 2048  # 保底 2GB
