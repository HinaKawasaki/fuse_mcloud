import errno
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mcloud_fuse.client.wrapper import CloudItem, DirectClientWrapper
from mcloud_fuse.config import (
    CACHE_DIR,
    EAGER_DOWNLOAD_LIMIT_BYTES,
    LAZY_READ,
    MIN_FREE_MB,
    RANGE_CHUNK_BYTES,
    RESCUE_FILE_MAX_MB,
    RESCUE_MAX_MB,
    RESCUE_MIN_FREE_MB,
    RESCUE_ON_SHUTDOWN,
    RESCUE_TIMEOUT_SEC,
    WRITE_BACK_DIR,
    get_effective_cache_limit_mb,
    get_effective_write_back_limit_mb,
)
from mcloud_fuse.fsutil import is_volatile_writeback
from mcloud_fuse.journal import (
    BUILDING,
    LOST,
    PENDING,
    RESCUED,
    UPLOADED,
    UPLOADING,
    JournalEntry,
    WritebackJournal,
    default_journal_path,
    rescue_dir,
)

logger = logging.getLogger(__name__)


def _safe_component(name: str) -> str:
    """把云端文件名清洗成可安全用作本地缓存文件名的片段。"""
    cleaned = str(name or "").replace("/", "_").replace("\\", "_").strip()
    return cleaned or "unnamed"


@dataclass
class RescueResult:
    """关机抢救的结果，便于日志与测试断言。"""

    rescued: List[tuple] = field(default_factory=list)
    skipped: List[tuple] = field(default_factory=list)
    total_bytes: int = 0
    elapsed: float = 0.0
    reason: str = ""

    @property
    def count(self) -> int:
        return len(self.rescued)


@dataclass
class _PartialFile:
    """按需读取中的「部分文件」：大小已知，内容按块填充。

    只有 :meth:`DataCache.ensure_range` 会读它。所有需要完整内容的地方
    （覆盖写、截断、rename 上传）必须走 :meth:`DataCache.ensure_complete`，
    绝不能把部分文件当完整文件用——否则会静默截断数据。
    """

    path: Path
    name: str
    size: int
    covered: List[Tuple[int, int]] = field(default_factory=list)

    def covered_bytes(self) -> int:
        return sum(stop - begin for begin, stop in self.covered)

    def covers(self, start: int, end: int) -> bool:
        for begin, stop in self.covered:
            if begin <= start and end <= stop:
                return True
        return False

    def add(self, start: int, end: int) -> None:
        if end <= start:
            return
        merged: List[Tuple[int, int]] = []
        for begin, stop in sorted(self.covered + [(start, end)]):
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
            else:
                merged.append((begin, stop))
        self.covered = merged


class DataCache:
    """
    文件内容读写缓存管理器 (Data Cache)
    提供 POSIX open/read 需要的本地文件缓存、LRU 淘汰，以及 write 阶段的写回暂存。
    """

    def __init__(
        self,
        client_wrapper: DirectClientWrapper,
        cache_dir: Path = CACHE_DIR,
        write_back_dir: Path = WRITE_BACK_DIR,
        max_size_mb: Optional[int] = None,
        eager_download_limit_bytes: Optional[int] = None,
        range_chunk_bytes: Optional[int] = None,
        journal_db: Optional[Path] = None,
        lazy_read: Optional[bool] = None,
        max_write_back_mb: Optional[int] = None,
        min_free_mb: Optional[int] = None,
        rescue_on_shutdown: Optional[bool] = None,
        rescue_max_mb: Optional[int] = None,
        rescue_file_max_mb: Optional[int] = None,
        rescue_timeout_sec: Optional[float] = None,
        rescue_min_free_mb: Optional[int] = None,
    ):
        self.wrapper = client_wrapper
        self.cache_dir = Path(cache_dir)
        self.write_back_dir = Path(write_back_dir)
        limit_mb = max_size_mb if max_size_mb is not None else get_effective_cache_limit_mb()
        self.max_bytes = max(1, int(limit_mb)) * 1024 * 1024
        # 默认所有文件懒加载：open 不下载，read 才拉分片
        self.lazy_read = LAZY_READ if lazy_read is None else bool(lazy_read)
        # >0 时只对该大小以上的文件懒加载；0 表示全部懒加载
        self.eager_download_limit_bytes = (
            EAGER_DOWNLOAD_LIMIT_BYTES
            if eager_download_limit_bytes is None
            else int(eager_download_limit_bytes)
        )
        write_back_mb = (
            get_effective_write_back_limit_mb()
            if max_write_back_mb is None
            else max(1, int(max_write_back_mb))
        )
        self.max_write_back_bytes = write_back_mb * 1024 * 1024
        self.min_free_bytes = (
            max(0, int(min_free_mb)) * 1024 * 1024
            if min_free_mb is not None
            else MIN_FREE_MB * 1024 * 1024
        )
        self.rescue_on_shutdown = (
            RESCUE_ON_SHUTDOWN if rescue_on_shutdown is None else bool(rescue_on_shutdown)
        )
        self.rescue_max_bytes = (
            (RESCUE_MAX_MB if rescue_max_mb is None else int(rescue_max_mb))
            * 1024
            * 1024
        )
        self.rescue_file_max_bytes = (
            (RESCUE_FILE_MAX_MB if rescue_file_max_mb is None else int(rescue_file_max_mb))
            * 1024
            * 1024
        )
        self.rescue_timeout_sec = (
            RESCUE_TIMEOUT_SEC if rescue_timeout_sec is None else float(rescue_timeout_sec)
        )
        self.rescue_min_free_bytes = (
            (RESCUE_MIN_FREE_MB if rescue_min_free_mb is None else int(rescue_min_free_mb))
            * 1024
            * 1024
        )
        self._stopping = threading.Event()
        self.range_chunk_bytes = max(
            1, RANGE_CHUNK_BYTES if range_chunk_bytes is None else int(range_chunk_bytes)
        )

        self._lock = threading.RLock()
        # 维护 file_id -> 本地缓存 Path
        self._cache_map: Dict[str, Path] = {}
        # 维护 LRU 访问时间 file_id -> last_access_time
        self._access_time: Dict[str, float] = {}
        # 每个 file_id 一把锁，避免同一文件被并发重复下载
        self._download_locks: Dict[str, threading.Lock] = {}
        # 正在被打开的读句柄占用的 file_id 计数。
        # LRU 淘汰必须绕开它们：把文件从正在 read() 的句柄底下删掉会让读操作报 EBADF。
        self._pinned: Dict[str, int] = {}
        # 被淘汰/作废、但因仍有句柄占用而推迟删除的本地文件
        self._deferred_deletes: Dict[str, list] = {}
        # 按需读取中的部分文件 file_id -> _PartialFile
        self._partial_paths: Dict[str, _PartialFile] = {}
        # 写回暂存 key -> 本地路径
        self._write_paths: Dict[str, Path] = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.write_back_dir.mkdir(parents=True, exist_ok=True)

        # 写回日志：SQLite 元数据放持久化的 state 目录，payload 仍在 tmpfs。
        # 两者故意分开：日志很小且必须跨重启，payload 很大且可以丢（代价见 journal.py）。
        try:
            self._journal = WritebackJournal(
                journal_db or default_journal_path(self.write_back_dir)
            )
        except Exception as exc:  # noqa: BLE001 - 日志坏了不应该阻止挂载
            logger.warning("写回日志不可用，降级为内存日志（崩溃恢复失效）: %s", exc)
            self._journal = WritebackJournal(Path(":memory:"))
        self._migrate_legacy_journal()

    # ------------------------------------------------------------------
    # 读缓存
    # ------------------------------------------------------------------

    def get_cache_path(self, file_id: str) -> Optional[Path]:
        """检查文件是否已缓存在本地 tmpfs 或 Fallback 磁盘"""
        with self._lock:
            path = self._cached_path_nolock(file_id)
            if path is not None:
                self._access_time[file_id] = time.time()
            return path

    def _cached_path_nolock(self, file_id: str) -> Optional[Path]:
        """取已落盘且仍存在的缓存路径（调用方需持有 ``self._lock``）。"""
        path = self._cache_map.get(file_id)
        if path and path.exists():
            return path
        return None

    def _download_lock(self, file_id: str) -> threading.Lock:
        with self._lock:
            return self._download_locks.setdefault(file_id, threading.Lock())

    def prepare_file_for_read(self, file_id: str, file_name: str) -> Path:
        """
        [关键 I/O API] 为读操作准备文件：
        若本地已有缓存则直接复用，否则从移动云盘下载到本地缓存。
        下载过程不持有全局锁，避免阻塞其他文件的缓存命中。

        注意：本方法**不**保护返回的路径不被 LRU 淘汰。凡是「拿到路径后还要
        继续用它」的调用方（打开句柄、复制进写回暂存、上传走 rename），都应改用
        :meth:`acquire_for_read` + :meth:`release_read` 把文件钉住。
        """
        return self._acquire_read_path(file_id, file_name, pin=False)

    def acquire_for_read(self, file_id: str, file_name: str) -> Path:
        """获取读路径并钉住它，直到调用 :meth:`release_read` 为止。

        与 :meth:`prepare_file_for_read` 的区别是：钉住期间 LRU 淘汰会跳过该条目，
        即使缓存已超限也只会清掉其它可淘汰项，绝不会把这个文件从使用者的脚下删掉。
        """
        return self._acquire_read_path(file_id, file_name, pin=True)

    def release_read(self, file_id: str) -> None:
        """释放一次 :meth:`acquire_for_read`；最后一个句柄关闭时执行延迟删除。"""
        with self._lock:
            count = self._pinned.get(file_id, 0)
            if count > 1:
                self._pinned[file_id] = count - 1
                return
            self._pinned.pop(file_id, None)
            pending = self._deferred_deletes.pop(file_id, [])

        for path in pending:
            self._unlink_quietly(path)

    def pinned_count(self, file_id: str) -> int:
        """当前占用该缓存条目的读句柄数量（诊断 / 测试用）。"""
        with self._lock:
            return self._pinned.get(file_id, 0)

    def _acquire_read_path(self, file_id: str, file_name: str, *, pin: bool) -> Path:
        with self._lock:
            is_partial = file_id in self._partial_paths
        if is_partial:
            # 需要完整内容（覆盖写 / 截断 / rename 上传）→ 先把部分文件补齐
            return self.ensure_complete(file_id, pin=pin)

        with self._lock:
            existing = self._cached_path_nolock(file_id)
            if existing is not None:
                self._access_time[file_id] = time.time()
                if pin:
                    self._pinned[file_id] = self._pinned.get(file_id, 0) + 1
                return existing

        with self._download_lock(file_id):
            # 双重检查：等待下载锁期间可能已被其他线程下载完成
            with self._lock:
                existing = self._cached_path_nolock(file_id)
                if existing is not None:
                    self._access_time[file_id] = time.time()
                    if pin:
                        self._pinned[file_id] = self._pinned.get(file_id, 0) + 1
                    return existing

            self._evict_if_needed()

            local_path = self.cache_dir / f"{file_id}_{_safe_component(file_name)}"
            logger.info("正在从云盘下载文件至本地缓存: %s -> %s", file_id, local_path)
            self.wrapper.download_file(file_id=file_id, local_save_path=local_path)

            with self._lock:
                self._cache_map[file_id] = local_path
                self._access_time[file_id] = time.time()
                if pin:
                    self._pinned[file_id] = self._pinned.get(file_id, 0) + 1
            return local_path

    def register_cache_file(self, file_id: str, local_path: Path) -> None:
        """把外部已经落盘的文件登记进缓存（例如新建文件上传后）。"""
        with self._lock:
            self._cache_map[file_id] = Path(local_path)
            self._access_time[file_id] = time.time()

    def invalidate_file_cache(self, file_id: str):
        """主动删除某文件的本地缓存（完整文件或按需读的部分文件）。

        若该条目仍被打开的读句柄占用，则只摘掉缓存映射、把删除推迟到最后一个
        句柄 :meth:`release_read` 之后执行（POSIX 语义：已打开的文件不会凭空消失）。
        """
        with self._lock:
            path = self._cache_map.pop(file_id, None)
            partial = self._partial_paths.pop(file_id, None)
            self._access_time.pop(file_id, None)
            doomed = [item for item in (path, partial.path if partial else None) if item]
            if doomed and self._pinned.get(file_id, 0) > 0:
                self._deferred_deletes.setdefault(file_id, []).extend(doomed)
                logger.debug(
                    "缓存条目仍被 %s 个句柄占用，延迟删除: %s",
                    self._pinned.get(file_id, 0),
                    doomed,
                )
                return
        for item in doomed:
            self._unlink_quietly(item)

    @staticmethod
    def _unlink_quietly(path: Optional[Path]) -> None:
        if path is None:
            return
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning("删除本地缓存失败: %s", exc)

    @staticmethod
    def _allocated_bytes(path: Path) -> int:
        """实际占用（稀疏文件按 st_blocks 算），避免把 100GB 稀疏壳算成 100GB 缓存。"""
        try:
            stat = path.stat()
        except OSError:
            return 0
        blocks = getattr(stat, "st_blocks", 0) * 512
        return blocks if blocks > 0 else stat.st_size

    def _get_current_cache_size(self) -> int:
        """计算当前缓存实际占用的磁盘/内存字节数（完整文件 + 按需读部分文件）。"""
        with self._lock:
            paths = list(self._cache_map.values())
            partial_bytes = sum(
                partial.covered_bytes() for partial in self._partial_paths.values()
            )
        return sum(self._allocated_bytes(path) for path in paths) + partial_bytes

    def _get_write_back_size(self) -> int:
        with self._lock:
            paths = list(self._write_paths.values())
        return sum(self._allocated_bytes(path) for path in paths)

    def cache_usage_bytes(self) -> int:
        return self._get_current_cache_size()

    def write_back_usage_bytes(self) -> int:
        return self._get_write_back_size()

    def ensure_write_back_capacity(self, additional_bytes: int) -> None:
        """在写入前检查写回上限与剩余空间；不够就抛 ENOSPC，而不是把 tmpfs 写满。"""
        additional = max(0, int(additional_bytes))
        if additional == 0:
            return
        used = self._get_write_back_size()
        limit = self.max_write_back_bytes
        if used + additional > limit:
            raise OSError(
                errno.ENOSPC,
                f"写回暂存将超过上限 {limit // (1024 * 1024)} MiB "
                f"(已用 {used // (1024 * 1024)} MiB, 本次 +{additional // (1024 * 1024)} MiB)；"
                "调大 MCLOUD_MAX_WRITE_BACK_MB 或把 MCLOUD_WRITE_BACK_DIR 指到持久盘",
            )
        try:
            free = shutil.disk_usage(self.write_back_dir).free
        except OSError:
            free = 0
        if free < self.min_free_bytes + additional:
            raise OSError(
                errno.ENOSPC,
                f"缓存所在文件系统剩余空间不足（free={free // (1024 * 1024)} MiB, "
                f"需要 {self.min_free_bytes // (1024 * 1024)} MiB 保底）",
            )

    def _evict_if_needed(self):
        """根据 LRU 算法剔除最老的文件，保持缓存容量在上限以内。

        被读句柄钉住的条目一律跳过：宁可暂时超出上限，也不把正在读的文件删掉。
        若全部条目都被占用，直接返回（下次有句柄释放时会再次触发淘汰）。
        """
        while self._get_current_cache_size() >= self.max_bytes:
            with self._lock:
                candidates = [
                    file_id
                    for file_id in self._access_time
                    if self._pinned.get(file_id, 0) == 0
                ]
                if not candidates:
                    logger.warning(
                        "缓存已达上限，但所有条目都被打开的句柄占用，暂缓淘汰"
                    )
                    return
                oldest_file_id = min(candidates, key=self._access_time.get)
            logger.info("缓存超出限额，触发 LRU 淘汰旧文件: file_id=%s", oldest_file_id)
            self.invalidate_file_cache(oldest_file_id)

    # ------------------------------------------------------------------
    # 按需（Range）读取
    # ------------------------------------------------------------------

    def should_read_lazily(self, size: int) -> bool:
        """是否走按需读取。

        默认全部懒加载（``lazy_read=True``）；``EAGER_DOWNLOAD_LIMIT>0`` 时只对大文件；
        ``lazy_read=False`` 时退回 v0.3.0 的整文件下载。
        """
        if not self.lazy_read:
            return False
        return (
            self.eager_download_limit_bytes <= 0
            or int(size) > self.eager_download_limit_bytes
        )

    def is_partial(self, file_id: str) -> bool:
        """该 file_id 当前是否为「只落了一部分」的按需读文件。"""
        with self._lock:
            return file_id in self._partial_paths

    def open_lazy(self, file_id: str, file_name: str, size: int) -> Path:
        """为按需读准备一个「大小已知、内容待填充」的本地文件，并钉住该条目。

        与 :meth:`acquire_for_read` 一样，句柄关闭时必须调用 :meth:`release_read`。
        返回的路径**只保证**在句柄存续期间可用；需要完整内容请用 :meth:`ensure_complete`。
        """
        size = max(0, int(size))
        with self._lock:
            partial = self._partial_paths.get(file_id)
            if partial is not None:
                partial.size = max(partial.size, size)
                self._pinned[file_id] = self._pinned.get(file_id, 0) + 1
                self._access_time[file_id] = time.time()
                return partial.path

        path = self.cache_dir / f"{file_id}_{_safe_component(file_name)}.partial"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.truncate(size)

        with self._lock:
            self._partial_paths[file_id] = _PartialFile(path=path, name=file_name, size=size)
            self._pinned[file_id] = self._pinned.get(file_id, 0) + 1
            self._access_time[file_id] = time.time()
        logger.info("文件 %s 启用按需读取（%s bytes，不再整文件下载）", file_id, size)
        return path

    def ensure_range(self, file_id: str, offset: int, length: int) -> Path:
        """确保 ``[offset, offset+length)`` 已落盘，返回当前可读路径。

        返回值可能是被升级后的完整缓存文件（服务端不支持 Range 时），因此调用方
        每次都应使用新返回的路径，而不是自己缓存 open 时的路径。
        """
        offset = max(0, int(offset))
        length = max(0, int(length))

        with self._lock:
            complete = self._cached_path_nolock(file_id)
            if complete is not None:
                return complete
            partial = self._partial_paths.get(file_id)

        if partial is None:
            raise FileNotFoundError(f"按需读文件不存在: {file_id}")
        if length == 0 or offset >= partial.size:
            return partial.path

        end = min(partial.size, offset + length)
        chunk = self.range_chunk_bytes
        cursor = (offset // chunk) * chunk
        while cursor < end:
            block_end = min(partial.size, cursor + chunk)
            if not partial.covers(cursor, block_end):
                promoted = self._fetch_block(file_id, partial, cursor, block_end)
                if promoted is not None:
                    return promoted
            cursor = block_end
        return partial.path

    def _fetch_block(
        self, file_id: str, partial: "_PartialFile", start: int, end: int
    ) -> Optional[Path]:
        """拉取一个分片。返回 ``None`` = 仍是部分文件；返回 Path = 已升级为完整文件。"""
        with self._download_lock(file_id):
            if partial.covers(start, end):
                return None
            logger.debug("按需读取分片: %s [%s, %s)", file_id, start, end)
            written = self.wrapper.download_range(
                file_id, partial.path, start, end - start, partial.size
            )
            if written is None:
                # 服务端忽略了 Range（HTTP 200）：整个文件已经从头写进本地
                logger.info(
                    "文件 %s 的下载直链不支持 Range，已整文件落盘并转为完整缓存", file_id
                )
                return self._promote_partial(file_id, partial)
            partial.add(start, start + max(0, int(written)))
            self._evict_if_needed()
            if partial.covers(0, partial.size):
                return self._promote_partial(file_id, partial)
            return None

    def _promote_partial(self, file_id: str, partial: "_PartialFile") -> Path:
        """把部分文件升级为完整读缓存条目（保留原有的钉住计数）。"""
        final = self.cache_dir / f"{file_id}_{_safe_component(partial.name)}"
        try:
            os.replace(partial.path, final)
        except OSError as exc:
            logger.warning("部分文件转完整缓存失败（继续使用原路径）: %s", exc)
            final = partial.path
        with self._lock:
            self._partial_paths.pop(file_id, None)
            self._cache_map[file_id] = final
            self._access_time[file_id] = time.time()
        return final

    def ensure_complete(self, file_id: str, *, pin: bool = False) -> Path:
        """把按需读的部分文件补齐为完整本地缓存。

        覆盖写 / 截断 / rename 上传等需要完整内容的地方都必须经过这里，
        否则会把只有前几 MB 的部分文件当真文件上传，静默截断数据。
        """
        with self._lock:
            complete = self._cached_path_nolock(file_id)
            if complete is not None:
                self._access_time[file_id] = time.time()
                if pin:
                    self._pinned[file_id] = self._pinned.get(file_id, 0) + 1
                return complete
            partial = self._partial_paths.get(file_id)
            name = partial.name if partial is not None else file_id

        with self._download_lock(file_id):
            with self._lock:
                complete = self._cached_path_nolock(file_id)
                if complete is not None:
                    self._access_time[file_id] = time.time()
                    if pin:
                        self._pinned[file_id] = self._pinned.get(file_id, 0) + 1
                    return complete
                partial = self._partial_paths.get(file_id)
                if partial is not None:
                    name = partial.name

            final = self.cache_dir / f"{file_id}_{_safe_component(name)}"
            logger.info("按需读文件需要完整内容，改为整文件下载: %s", file_id)
            self.wrapper.download_file(file_id=file_id, local_save_path=final)

            with self._lock:
                self._partial_paths.pop(file_id, None)
                self._cache_map[file_id] = final
                self._access_time[file_id] = time.time()
                if pin:
                    self._pinned[file_id] = self._pinned.get(file_id, 0) + 1
            if partial is not None:
                self._unlink_quietly(partial.path)
            return final

    # ------------------------------------------------------------------
    # 写回暂存 (write-back)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 写回日志（崩溃恢复）
    # ------------------------------------------------------------------

    def _migrate_legacy_journal(self) -> None:
        """把旧版 ``writeback_journal.json`` 导入 SQLite 后改名留档。"""
        legacy = self.write_back_dir / "writeback_journal.json"
        if not legacy.exists():
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("旧写回日志无法读取，跳过迁移: %s", exc)
            return
        if not isinstance(data, dict):
            return
        imported = 0
        for key, meta in data.items():
            if not isinstance(meta, dict):
                continue
            target_dir_id = str(meta.get("target_dir_id") or "")
            file_name = str(meta.get("file_name") or "")
            if not target_dir_id or not file_name:
                continue
            self._journal.upsert(
                JournalEntry(
                    key=str(key),
                    target_dir_id=target_dir_id,
                    file_name=file_name,
                    payload_path=str(self.write_back_dir / f"{_safe_component(key)}.wb"),
                    base_file_id=meta.get("base_file_id"),
                    base_updated_at=meta.get("base_updated_at"),
                    state=PENDING,
                )
            )
            imported += 1
        try:
            legacy.replace(legacy.with_suffix(".json.migrated"))
        except OSError as exc:
            logger.warning("旧写回日志迁移后改名失败: %s", exc)
        if imported:
            logger.info("已迁移 %s 条旧写回日志到 SQLite", imported)

    @property
    def journal_path(self) -> Path:
        return self._journal.path

    def register_write_meta(
        self,
        key: str,
        target_dir_id: str,
        file_name: str,
        base_file_id: Optional[str] = None,
        base_updated_at: Optional[str] = None,
        payload_path: Optional[Path] = None,
        state: str = BUILDING,
    ) -> None:
        """登记一条待上传的写回记录（含冲突检测基线）。

        先以 ``building`` 状态登记、payload 写完后改为 ``pending``，
        避免崩溃恢复时把只写了一半的文件当成完整文件上传。
        """
        target = Path(payload_path) if payload_path else (
            self.write_back_dir / f"{_safe_component(key)}.wb"
        )
        with self._lock:
            self._journal.upsert(
                JournalEntry(
                    key=str(key),
                    target_dir_id=str(target_dir_id),
                    file_name=str(file_name),
                    payload_path=str(target),
                    base_file_id=str(base_file_id) if base_file_id else None,
                    base_updated_at=str(base_updated_at) if base_updated_at else None,
                    state=state,
                )
            )

    def clear_write_meta(self, key: str) -> None:
        with self._lock:
            self._journal.remove(key)

    def begin_shutdown(self) -> None:
        """通知后台补传尽快停下，给关机抢救让路。"""
        self._stopping.set()

    def pending_uploads(self) -> Dict[str, dict]:
        with self._lock:
            pending = self._journal.pending()
            pending.update({item.key: item.meta() for item in self._journal.entries(RESCUED)})
            return pending

    def lost_uploads(self) -> Dict[str, dict]:
        """payload 已丢失（常见于 tmpfs 重启）的写回记录，供 status/doctor 展示。"""
        with self._lock:
            return self._journal.lost()

    def rescuable_uploads(self) -> Dict[str, dict]:
        """真正会在关机抢救里被搬运的条目（pending/uploading 且 payload 存在）。"""
        with self._lock:
            entries = self._journal.entries(PENDING) + self._journal.entries(UPLOADING)
        result = {}
        for entry in entries:
            if Path(entry.payload_path).exists():
                result[entry.key] = entry.meta()
        return result

    def _rescue_path(self, key: str) -> Path:
        return rescue_dir(self.write_back_dir) / f"{_safe_component(key)}.wb"

    @staticmethod
    def _copy_with_deadline(source: Path, target: Path, deadline: float) -> None:
        with open(source, "rb") as src, open(target, "wb") as dst:
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("关机抢救超时")
                chunk = src.read(4 * 1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())

    def rescue_pending_uploads(self, *, force: bool = False) -> RescueResult:
        """关机时把待上传 payload 从 tmpfs 复制到持久盘。

        - 只处理 ``pending`` / ``uploading``；``building`` 是半成品，绝不上传；
        - 有总大小、单文件大小、剩余空间、总超时四道闸门；
        - 先写 ``.part``，fsync 后原子改名，再更新日志路径；
        - 默认只在写回目录位于 tmpfs/ramfs 时执行。
        """
        started = time.monotonic()
        result = RescueResult()
        if not force and not self.rescue_on_shutdown:
            result.reason = "MCLOUD_RESCUE_ON_SHUTDOWN=0"
            return result
        if not force and not is_volatile_writeback(self.write_back_dir):
            result.reason = "写回目录不在 tmpfs，无需抢救"
            return result

        with self._lock:
            entries = self._journal.entries(PENDING) + self._journal.entries(UPLOADING)
        candidates = []
        for entry in entries:
            path = Path(entry.payload_path)
            if not path.exists():
                self._journal.set_state(
                    entry.key, LOST, last_error="关机抢救时 payload 已不存在"
                )
                result.skipped.append((entry.key, entry.file_name, 0, "payload 不存在"))
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            candidates.append((size, entry, path))
        if not candidates:
            result.reason = "没有待抢救文件"
            result.elapsed = time.monotonic() - started
            return result

        target_dir = rescue_dir(self.write_back_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            target_dir.chmod(0o700)
        except OSError:
            pass
        if not force and is_volatile_writeback(target_dir):
            result.reason = "状态目录也在 tmpfs，抢救没有意义"
            result.elapsed = time.monotonic() - started
            return result
        deadline = started + max(0.0, self.rescue_timeout_sec)
        logger.info(
            "关机抢救：%s 个待上传文件，预算 %.0f 秒 / %s MiB",
            len(candidates),
            self.rescue_timeout_sec,
            self.rescue_max_bytes // (1024 * 1024),
        )

        for size, entry, source in sorted(candidates, key=lambda item: item[0]):
            if time.monotonic() >= deadline:
                result.skipped.append((entry.key, entry.file_name, size, "超时"))
                break
            if size > self.rescue_file_max_bytes:
                result.skipped.append(
                    (
                        entry.key,
                        entry.file_name,
                        size,
                        f"单文件超过 {self.rescue_file_max_bytes // (1024 * 1024)} MiB",
                    )
                )
                continue
            if result.total_bytes + size > self.rescue_max_bytes:
                result.skipped.append((entry.key, entry.file_name, size, "超过总预算"))
                break
            try:
                free = shutil.disk_usage(target_dir).free
            except OSError:
                free = 0
            if free < self.rescue_min_free_bytes + size:
                result.skipped.append((entry.key, entry.file_name, size, "目标盘空间不足"))
                break
            destination = target_dir / f"{_safe_component(entry.key)}.wb"
            part = destination.with_name(destination.name + ".part")
            try:
                self._copy_with_deadline(source, part, deadline)
                os.replace(part, destination)
                try:
                    os.chmod(destination, 0o600)
                except OSError:
                    pass
                try:
                    dir_fd = os.open(target_dir, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
                # uploading 的歧义状态要保留：下次启动先核对远端，避免重复上传。
                rescued_state = UPLOADING if entry.state == UPLOADING else RESCUED
                self._journal.relocate(
                    entry.key, str(destination), state=rescued_state, size=size
                )
                # 日志已经指向持久盘，tmpfs 原文件可以删掉（同路径时不要删目标）
                if source != destination:
                    try:
                        source.unlink()
                    except OSError:
                        pass
            except TimeoutError:
                try:
                    part.unlink()
                except OSError:
                    pass
                result.skipped.append((entry.key, entry.file_name, size, "复制超时"))
                break
            except OSError as exc:
                try:
                    part.unlink()
                except OSError:
                    pass
                result.skipped.append((entry.key, entry.file_name, size, f"复制失败：{exc}"))
                continue
            result.rescued.append((entry.key, entry.file_name, size))
            result.total_bytes += size
            logger.info("关机抢救：%s -> %s", entry.file_name, destination)

        result.elapsed = time.monotonic() - started
        if result.skipped:
            logger.warning("关机抢救跳过 %s 个文件（见 journal）", len(result.skipped))
        logger.info(
            "关机抢救完成：%s 个 / %.1f MiB / %.1f 秒",
            result.count,
            result.total_bytes / (1024 * 1024),
            result.elapsed,
        )
        return result

    def recover_pending_uploads(self) -> int:
        """启动时把上次崩溃遗留的写回文件补传回云盘。

        - ``building``：崩溃在 payload 写入阶段，标记丢失，绝不上传半成品；
        - ``pending`` 但 payload 不存在：tmpfs 重启后内容没了，标记 ``lost`` 留档；
        - 上传成功：转读缓存并删除日志条目。

        :return: 成功补传的数量
        """
        recovered = 0
        # 先处理「上传中崩溃」：可能服务端已成功、只是响应/清理没回来。
        # 核对远端同名同大小文件，能确认就清日志，避免重复上传占容量。
        for entry in self._journal.entries(UPLOADING):
            if self._looks_uploaded(entry):
                logger.info("恢复：%s 已在云端存在，清除上传中日志", entry.file_name)
                self.clear_write_meta(entry.key)
                continue
            self._journal.set_state(entry.key, PENDING, last_error="上传状态未知，已转为待传")

        for entry in self._journal.entries(BUILDING):
            self._journal.set_state(
                entry.key, LOST, last_error="payload 写入阶段被中断（不会补传半成品）"
            )
            logger.warning("写回文件在创建阶段中断: %s (%s)", entry.file_name, entry.key)

        for entry in self._journal.entries(PENDING) + self._journal.entries(RESCUED):
            if self._stopping.is_set():
                logger.info("收到关机信号，停止后台补传")
                break
            path = Path(entry.payload_path)
            if not path.exists():
                rescued_path = self._rescue_path(entry.key)
                if rescued_path.exists():
                    path = rescued_path
                    self._journal.relocate(
                        entry.key, str(path), state=entry.state, size=path.stat().st_size
                    )
            if not path.exists():
                self._journal.set_state(
                    entry.key, LOST, last_error="payload 不存在（tmpfs 重启或已被清理）"
                )
                logger.warning(
                    "写回文件已丢失，无法补传: %s（%s）；记录已标记 lost",
                    entry.file_name,
                    entry.key,
                )
                continue
            extra = {}
            if entry.base_file_id and entry.base_updated_at:
                extra["expected_file_id"] = entry.base_file_id
                extra["expected_updated_at"] = entry.base_updated_at
            try:
                item = self.wrapper.upload_file(
                    local_file_path=path,
                    target_dir_id=entry.target_dir_id,
                    name=entry.file_name,
                    **extra,
                )
            except Exception as exc:  # noqa: BLE001 - 恢复失败保留现场
                self._journal.set_state(entry.key, PENDING, last_error=str(exc))
                logger.warning("补传写回文件失败 key=%s: %s", entry.key, exc)
                continue
            if item.name != entry.file_name:
                logger.warning("崩溃恢复发现远端修改，已将本地版本另存为 %s", item.name)
            self._journal.set_state(entry.key, UPLOADED)
            self._promote_to_cache(path, item.file_id, item.name)
            self.clear_write_meta(entry.key)
            recovered += 1
            logger.info("崩溃恢复：已补传 %s -> fileId=%s", entry.file_name, item.file_id)
        return recovered

    # ------------------------------------------------------------------
    # 写回暂存 (write-back)
    # ------------------------------------------------------------------

    def create_write_buffer(
        self,
        key: str,
        initial_path: Optional[Path] = None,
        target_dir_id: Optional[str] = None,
        file_name: Optional[str] = None,
        base_file_id: Optional[str] = None,
        base_updated_at: Optional[str] = None,
    ) -> Path:
        """
        为写操作创建本地暂存文件。
        - 若 ``initial_path`` 给定（修改已有文件），先把原内容复制过来；
        - 否则创建空的截断文件（新建文件）。
        - 若给定 ``target_dir_id`` / ``file_name``，同时登记写回日志，
          进程崩溃后可据此补传。
        """
        target = self.write_back_dir / f"{_safe_component(key)}.wb"
        if initial_path is not None and Path(initial_path).exists():
            self.ensure_write_back_capacity(Path(initial_path).stat().st_size)
        tracked = bool(target_dir_id and file_name)
        if tracked:
            self.register_write_meta(
                key,
                str(target_dir_id),
                str(file_name),
                base_file_id=base_file_id,
                base_updated_at=base_updated_at,
                payload_path=target,
                state=BUILDING,
            )
        try:
            if initial_path is not None and Path(initial_path).exists():
                shutil.copyfile(initial_path, target)
            else:
                target.touch()
            # 让日志里的「pending」在数据真正落盘之后才出现
            with open(target, "rb") as handle:
                os.fsync(handle.fileno())
        except BaseException:
            if tracked:
                self.clear_write_meta(key)
            try:
                target.unlink()
            except OSError:
                pass
            raise
        with self._lock:
            self._write_paths[key] = target
        if tracked:
            # 仍保持 building：只有 fsync/release 真正提交时才转为 pending，
            # 否则崩溃恢复会把写了一半的文件当成完整文件上传。
            self._journal.set_state(key, BUILDING, size=target.stat().st_size)
        return target

    def mark_write_uploading(self, key: str) -> None:
        """上传请求发出前标记；崩溃恢复时可据此核对远端，避免重复上传。"""
        self._journal.set_state(key, UPLOADING)

    def mark_write_pending(self, key: str, error: str = "") -> None:
        self._journal.set_state(key, PENDING, last_error=error or None)

    def mark_write_ready(self, key: str) -> None:
        """把写回记录从 building 转为 pending（调用方准备上传/已 fsync）。

        这一步之后崩溃，恢复逻辑才会补传；之前崩溃只会把条目标记 lost，
        不会把半成品上传到云端。
        """
        with self._lock:
            path = self._write_paths.get(key)
        if path is None or not path.exists():
            raise FileNotFoundError(f"写回暂存文件不存在: {key}")
        self._journal.set_state(key, PENDING, size=path.stat().st_size)

    def discard_write_buffer(self, key: str) -> None:
        """放弃某次写操作（如上传失败后清理）。"""
        with self._lock:
            path = self._write_paths.pop(key, None)
        self.clear_write_meta(key)
        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    def upload_write_buffer(
        self,
        key: str,
        target_dir_id: str,
        file_name: str,
        base_file_id: Optional[str] = None,
        base_updated_at: Optional[str] = None,
    ) -> CloudItem:
        """把写回暂存文件整文件上传到云盘，**保留**暂存文件。

        保留是必要的：``fsync`` 之后文件可能仍处于打开状态，后续 ``write`` 还要继续写它。
        真正的收尾（把暂存变成读缓存）由 ``finalize_write_buffer`` 在关闭文件时完成。
        """
        with self._lock:
            path = self._write_paths.get(key)
        if path is None or not path.exists():
            raise FileNotFoundError(f"写回暂存文件不存在: {key}")
        extra = {}
        if base_file_id and base_updated_at:
            extra["expected_file_id"] = base_file_id
            extra["expected_updated_at"] = base_updated_at
        return self.wrapper.upload_file(
            local_file_path=path,
            target_dir_id=target_dir_id,
            name=file_name,
            **extra,
        )

    def _promote_to_cache(self, source: Path, file_id: str, file_name: str) -> Optional[Path]:
        """把已上传的本地文件变成读缓存：优先同文件系统改名（零拷贝），否则回退拷贝。"""
        cache_path = self.cache_dir / f"{file_id}_{_safe_component(file_name)}"
        try:
            os.replace(source, cache_path)
        except OSError:
            try:
                shutil.copyfile(source, cache_path)
                os.unlink(source)
            except OSError as exc:
                logger.warning("写回文件转读缓存失败: %s", exc)
                return None
        self.register_cache_file(file_id, cache_path)
        return cache_path

    def _looks_uploaded(self, entry: JournalEntry) -> bool:
        """保守核对：目标目录里存在同名、同大小、且不是旧基线的文件。"""
        path = Path(entry.payload_path)
        if not path.exists():
            return False
        try:
            size = path.stat().st_size
            items = self.wrapper.list_directory(entry.target_dir_id, page_limit=200)
        except Exception:  # noqa: BLE001 - 核对失败就退回重传，宁可多传不丢
            return False
        for item in items:
            same_name = item.name == entry.file_name or item.name.startswith(
                entry.file_name + ".conflict-"
            )
            if not same_name or int(item.size or 0) != size:
                continue
            if entry.base_file_id and item.file_id == entry.base_file_id:
                if not entry.base_updated_at or item.updated_at == entry.base_updated_at:
                    continue  # 还是上传前的旧对象
            return True
        return False

    def close(self) -> None:
        with self._lock:
            self._journal.close()

    def finalize_write_buffer(
        self,
        key: str,
        file_id: str,
        file_name: str,
        *,
        keep_local_for_read: bool = True,
    ) -> Optional[Path]:
        """收尾写回：清日志、移除暂存登记，并把内容转为读缓存（或直接删除）。

        用 ``os.replace`` 改名而不是 ``copyfile``，因此不会为同一个文件再占一份磁盘。
        """
        with self._lock:
            path = self._write_paths.pop(key, None)
        self.clear_write_meta(key)
        if path is None or not path.exists():
            return None
        if not keep_local_for_read:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return self._promote_to_cache(path, file_id, file_name)
