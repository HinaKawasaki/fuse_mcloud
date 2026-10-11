import os
import shutil
import logging
import threading
from pathlib import Path
from typing import Dict, Optional

from config import CACHE_DIR, DISK_FALLBACK_DIR, MAX_CACHE_SIZE_MB
from client.wrapper import DirectClientWrapper

logger = logging.getLogger(__name__)


class DataCache:
    """
    文件内容读写缓存管理器 (Data Cache)
    提供 POSIX open/read/write 操作需要的本地文件缓存与 LRU 淘汰
    """

    def __init__(
        self,
        client_wrapper: DirectClientWrapper,
        cache_dir: Path = CACHE_DIR,
        fallback_dir: Path = DISK_FALLBACK_DIR,
        max_size_mb: Optional[int] = MAX_CACHE_SIZE_MB
    ):
        self.wrapper = client_wrapper
        self.cache_dir = cache_dir
        self.fallback_dir = fallback_dir
        self.max_bytes = (max_size_mb * 1024 * 1024) if max_size_mb else (4096 * 1024 * 1024)
        
        self._lock = threading.RLock()
        # 维护 file_id -> 本地缓存 Path
        self._cache_map: Dict[str, Path] = {}
        # 维护 LRU 访问时间 file_id -> last_access_time
        self._access_time: Dict[str, float] = {}

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fallback_dir.mkdir(parents=True, exist_ok=True)

    def get_cache_path(self, file_id: str) -> Optional[Path]:
        """检查文件是否已缓存在本地 tmpfs 或 Fallback 磁盘"""
        with self._lock:
            path = self._cache_map.get(file_id)
            if path and path.exists():
                import time
                self._access_time[file_id] = time.time()
                return path
            return None

    def prepare_file_for_read(self, file_id: str, file_name: str) -> Path:
        """
        [关键 I/O API] 为读操作准备文件：
        若本地已有缓存则直接复用，否则从移动云盘调用 download_file 下载到本地缓存
        """
        with self._lock:
            # 1. 命中缓存
            existing_path = self.get_cache_path(file_id)
            if existing_path:
                logger.debug(f"命中本地文件缓存: {existing_path}")
                return existing_path

            # 2. 未命中，检查容量并在必要时触发 LRU 淘汰
            self._evict_if_needed()

            # 3. 确定本地暂存路径 (/tmp/cmcloud-fuse/cache/<file_id>_<name>)
            local_path = self.cache_dir / f"{file_id}_{file_name}"

            # 4. 调用真实 API 进行下载
            logger.info(f"正在从云盘下载文件至本地缓存: {file_id} -> {local_path}")
            self.wrapper.download_file(file_id=file_id, local_save_path=local_path)

            # 5. 登记缓存映射
            import time
            self._cache_map[file_id] = local_path
            self._access_time[file_id] = time.time()

            return local_path

    def stage_file_for_upload(self, local_source_path: Path, target_dir_id: str) -> str:
        """
        [关键 I/O API] 将本地修改后的文件上传写回云盘
        """
        logger.info(f"正在上传本地文件到云盘目录: {local_source_path} -> dir_id={target_dir_id}")
        cloud_item = self.wrapper.upload_file(
            local_file_path=local_source_path,
            target_dir_id=target_dir_id
        )
        return cloud_item.file_id

    def invalidate_file_cache(self, file_id: str):
        """主动删除某文件的本地缓存"""
        with self._lock:
            path = self._cache_map.pop(file_id, None)
            self._access_time.pop(file_id, None)
            if path and path.exists():
                try:
                    path.unlink()
                except OSError as e:
                    logger.warning(f"删除本地缓存失败: {e}")

    def _get_current_cache_size(self) -> int:
        """计算当前缓存占用的总字节数"""
        total = 0
        for path in self._cache_map.values():
            if path.exists():
                total += path.stat().st_size
        return total

    def _evict_if_needed(self):
        """根据 LRU 算法剔除最老的文件，保持缓存容量在上限以内"""
        while self._get_current_cache_size() >= self.max_bytes and self._access_time:
            # 找到访问时间最早的 file_id
            oldest_file_id = min(self._access_time, key=self._access_time.get)
            logger.info(f"缓存超出限额，触发 LRU 淘汰旧文件: file_id={oldest_file_id}")
            self.invalidate_file_cache(oldest_file_id)