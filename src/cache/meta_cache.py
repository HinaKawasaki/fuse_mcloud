import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import META_CACHE_TTL
from mapping.id_map import InodeEntry

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """元数据缓存条目"""
    data: Any
    timestamp: float = field(default_factory=time.time)

    def is_expired(self, ttl: float) -> bool:
        return (time.time() - self.timestamp) > ttl


class MetaCache:
    """
    内存级元数据与目录列举 TTL 缓存器
    提供针对 getattr (InodeEntry) 和 readdir (child inodes/entries) 的秒级内存响应
    """

    def __init__(self, ttl: float = META_CACHE_TTL):
        self.ttl = ttl
        self._lock = threading.RLock()

        # inode -> getattr 元数据缓存 (CacheEntry[InodeEntry])
        self._attr_cache: Dict[int, CacheEntry] = {}

        # parent_inode -> readdir 子节点列举缓存 (CacheEntry[List[InodeEntry]])
        self._dir_cache: Dict[int, CacheEntry] = {}

    # ------------------ 1. Getattr (文件属性) 缓存 ------------------

    def get_attr(self, inode: int) -> Optional[InodeEntry]:
        """获取 inode 的属性缓存，若不存在或已过期则返回 None"""
        with self._lock:
            entry = self._attr_cache.get(inode)
            if not entry:
                return None
            if entry.is_expired(self.ttl):
                self._attr_cache.pop(inode, None)
                return None
            return entry.data

    def put_attr(self, inode: int, entry: InodeEntry):
        """存入/更新 inode 的属性缓存"""
        with self._lock:
            self._attr_cache[inode] = CacheEntry(data=entry)

    def invalidate_attr(self, inode: int):
        """使特定 inode 的属性缓存失效"""
        with self._lock:
            self._attr_cache.pop(inode, None)

    # ------------------ 2. Readdir (目录项列举) 缓存 ------------------

    def get_readdir(self, parent_inode: int) -> Optional[List[InodeEntry]]:
        """获取指定父目录的子项列举缓存"""
        with self._lock:
            entry = self._dir_cache.get(parent_inode)
            if not entry:
                return None
            if entry.is_expired(self.ttl):
                self._dir_cache.pop(parent_inode, None)
                return None
            return entry.data

    def put_readdir(self, parent_inode: int, children: List[InodeEntry]):
        """存入/更新父目录的子项列表缓存"""
        with self._lock:
            self._dir_cache[parent_inode] = CacheEntry(data=children)

    def invalidate_readdir(self, parent_inode: int):
        """使特定父目录的 readdir 缓存失效"""
        with self._lock:
            self._dir_cache.pop(parent_inode, None)

    # ------------------ 3. 通用与级联清理 ------------------

    def invalidate_all_for_inode(self, inode: int, parent_inode: Optional[int] = None):
        """同时清理节点属性缓存与其父目录的 readdir 缓存"""
        with self._lock:
            self.invalidate_attr(inode)
            self.invalidate_readdir(inode)  # 如果是目录，清理其本身的 readdir
            if parent_inode is not None:
                self.invalidate_readdir(parent_inode)
