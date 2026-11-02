# src/config.py
"""
MCloud FUSE 中间层配置文件
用户可直接在此修改配置，或通过环境变量覆盖。
"""

import os
from pathlib import Path

# ==================== 1. 缓存与路径配置 ====================
# 读缓存目录（默认 tmpfs，重启即清空；写回日志单独放持久化 state 目录）
CACHE_DIR = Path(os.getenv("MCLOUD_CACHE_DIR", "/tmp/cmcloud-fuse"))

# 写回缓存的本地暂存目录：write() 先落这里，release()/fsync() 再整文件上传
WRITE_BACK_DIR = Path(
    os.getenv("MCLOUD_WRITE_BACK_DIR", "/tmp/cmcloud-fuse/writeback")
)

# 缓存容量最大限制 (单位: MB)
# - 设置为固定数值 (如 2048): 严格限制缓存上限为 2GB
# - 设置为 None: 自动取 min(512, 10% RAM)，至少 128MB（读缓存只保留最近用到的内容）
MAX_CACHE_SIZE_MB = None  # 用户可直接修改此数字

# 写回暂存上限（单位: MB）。超过就返回 ENOSPC，避免 cp 大文件把 tmpfs/内存写满。
# None = 自动取 min(1024, 10% RAM)，至少 256MB。
MAX_WRITE_BACK_MB = None
# 写回/缓存目录至少保留的剩余空间（MB），低于它就拒绝新的写回。
MIN_FREE_MB = int(os.getenv("MCLOUD_MIN_FREE_MB", "64"))

# ==================== 1.1 关机遇失抢救 ====================
# 只对 pending/uploading 的 payload 生效；building 表示半成品，绝不上传。
RESCUE_ON_SHUTDOWN = os.getenv("MCLOUD_RESCUE_ON_SHUTDOWN", "1").strip().lower() not in (
    "0", "false", "no",
)
RESCUE_MAX_MB = int(os.getenv("MCLOUD_RESCUE_MAX_MB", "1024"))
RESCUE_FILE_MAX_MB = int(os.getenv("MCLOUD_RESCUE_FILE_MAX_MB", "512"))
RESCUE_TIMEOUT_SEC = float(os.getenv("MCLOUD_RESCUE_TIMEOUT_SEC", "20"))
RESCUE_MIN_FREE_MB = int(os.getenv("MCLOUD_RESCUE_MIN_FREE_MB", "256"))
# 关机抢救时向 systemd 申请延长停止超时的上限（秒）；只申请一次，绝不无限延长。
STOP_EXTEND_MAX_SEC = float(os.getenv("MCLOUD_STOP_EXTEND_MAX_SEC", "30"))

# ==================== 2. 元数据与 TTL 策略 ====================
META_CACHE_TTL = 60        # getattr / readdir 元数据缓存有效时间 (秒)
# 元数据缓存最多保留多少条（inode 属性 / 目录列表各一份），防止浏览大目录时内存无限涨
META_CACHE_MAX_ENTRIES = int(os.getenv("MCLOUD_META_CACHE_ENTRIES", "4096"))
QUOTA_CACHE_TTL = float(os.getenv("MCLOUD_QUOTA_TTL", "60"))  # statfs 配额缓存（秒）

# ==================== 3. 遗留内部目录 ====================
# 旧版本写入的 .TrashBin / .MCloudHydrate 仍然隐藏，避免历史残留出现在
# 新挂载里。新版本不再创建这两个目录。
TRASH_BIN_DIR_NAME = ".TrashBin"

# 旧版沙箱外文件的懒搬运目录；新版本不再创建，只在列举时继续隐藏。
HYDRATE_DIR_NAME = os.getenv("MCLOUD_HYDRATE_DIR", ".MCloudHydrate")

# ==================== 4. 并发与限流 ====================
API_MAX_CONCURRENT = int(os.getenv("MCLOUD_API_MAX_CONCURRENT", "3"))
API_REQUESTS_PER_SECOND = float(os.getenv("MCLOUD_API_RPS", "5"))

# ==================== 5. 写回收尾策略 ====================
# 上传完成后是否把写回文件保留为本地读缓存（改名进缓存目录，零拷贝）。
# 置 0 则上传后直接删除，适合本地磁盘紧张、且不在意刚写完立刻重读还要下次下载的场景。
UPLOAD_KEEP_CACHE = os.getenv("MCLOUD_UPLOAD_KEEP_CACHE", "1").strip().lower() not in (
    "0", "false", "no",
)

# ==================== 6. 懒加载 / 按需（Range）读取 ====================
# 默认所有文件都按需读：open() 不下载内容，read() 才按 4MiB 分片拉取。
# MCLOUD_LAZY_READ=0 可退回旧的整文件下载；MCLOUD_EAGER_DOWNLOAD_LIMIT>0
# 则只对超过该大小的文件懒加载。
LAZY_READ = os.getenv("MCLOUD_LAZY_READ", "1").strip().lower() not in (
    "0", "false", "no",
)
EAGER_DOWNLOAD_LIMIT_BYTES = int(
    os.getenv("MCLOUD_EAGER_DOWNLOAD_LIMIT", "0")
)
# 按需读的单次预取粒度（实际请求会向上取整到该大小的整数倍）
RANGE_CHUNK_BYTES = int(os.getenv("MCLOUD_RANGE_CHUNK", str(4 * 1024 * 1024)))
# 单个分片请求的超时（与 SDK 的整文件下载超时分开：分片更小、应当更快返回）
RANGE_TIMEOUT_SEC = float(os.getenv("MCLOUD_RANGE_TIMEOUT", "60"))

# ==================== 7. yun.139.com Web API ====================
# 使用前提是显式导入 token（scripts/import_web_token.py）；没有 token 时
# 挂载会直接报错，不会静默改走其他通道。启用风险见 ADR-0002。
# 浏览器里 hcy/file/list 请求头 Authorization: Basic 后面的值
WEB_TOKEN_FILE = Path(
    os.getenv("MCLOUD_WEB_TOKEN_FILE", "~/.config/mcloud-fuse/web_token")
).expanduser()
# Web 端单文件上传上限比 Skill 宽松：13=PC（500GB）、9=网页（50GB）
WEB_CLIENT_TYPE = os.getenv("MCLOUD_WEB_CLIENT_TYPE", "13").strip()
WEB_PART_SIZE_BYTES = int(os.getenv("MCLOUD_WEB_PART_SIZE", str(100 * 1024 * 1024)))

# rm 语义：delete=调用 /file/batchDelete（不进回收站）；
# trash=调用 /recyclebin/batchTrash（可在 App 回收站里恢复）。
RM_PERMANENT = os.getenv("MCLOUD_RM_MODE", "delete").strip().lower() != "trash"

# 覆盖写冲突策略：
# - copy：把本地版本另存为 name.conflict-<host>-<ts>，远端原样保留（不丢数据）
# - error：提交失败（ESTALE），保留写回缓冲，由调用方决定重试
CONFLICT_MODE = os.getenv("MCLOUD_CONFLICT", "copy").strip().lower()
if CONFLICT_MODE not in ("copy", "error"):
    CONFLICT_MODE = "copy"


def _total_ram_mb() -> int:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 2048


def get_effective_cache_limit_mb() -> int:
    """读缓存上限：显式配置 > MCLOUD_MAX_CACHE_MB > 自动（min(512, 10% RAM)）。"""
    explicit = os.getenv("MCLOUD_MAX_CACHE_MB")
    if explicit:
        return max(32, int(explicit))
    if MAX_CACHE_SIZE_MB is not None and MAX_CACHE_SIZE_MB > 0:
        return int(MAX_CACHE_SIZE_MB)
    return max(128, min(512, _total_ram_mb() // 10))


def get_effective_write_back_limit_mb() -> int:
    """写回暂存上限：显式配置 > MCLOUD_MAX_WRITE_BACK_MB > 自动（min(1024, 10% RAM)）。"""
    explicit = os.getenv("MCLOUD_MAX_WRITE_BACK_MB")
    if explicit:
        return max(32, int(explicit))
    if MAX_WRITE_BACK_MB is not None and MAX_WRITE_BACK_MB > 0:
        return int(MAX_WRITE_BACK_MB)
    return max(256, min(1024, _total_ram_mb() // 10))
