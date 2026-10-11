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

# 磁盘落盘/溢出缓存路径 (当内存缓存不足或大文件溢出时使用)
DISK_FALLBACK_DIR = Path(
    os.getenv("MCLOUD_FALLBACK_DIR", Path.home() / ".cache" / "cmcloud-fuse" / "fallback")
)

# 缓存容量最大限制 (单位: MB)
# - 设置为固定数值 (如 2048): 严格限制缓存上限为 2GB
# - 设置为 None: 由程序自动探测系统可用内存并动态设定 (默认分配 25% RAM)
MAX_CACHE_SIZE_MB = None  # 用户可直接修改此数字

# ==================== 2. 元数据与 TTL 策略 ====================
META_CACHE_TTL = 60        # getattr / readdir 元数据缓存有效时间 (秒)
PREFETCH_DIR_META = True   # 是否在 readdir 时预加载子目录元数据

# ==================== 3. 沙箱与目录映射 ====================
SANDBOX_ROOT = "/AI空间/智能体云盘助手"
TRASH_BIN_DIR_NAME = ".TrashBin"