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
    RANGE_CHUNK_BYTES,
    WRITE_BACK_DIR,
    get_effective_cache_limit_mb,
)

logger = logging.getLogger(__name__)


def _safe_component(name: str) -> str:
    """把云端文件名清洗成可安全用作本地缓存文件名的片段。"""
    cleaned = str(name or "").replace("/", "_").replace("\\", "_").strip()
    return cleaned or "unnamed"


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
    ):
        self.wrapper = client_wrapper
        self.cache_dir = Path(cache_dir)
        self.write_back_dir = Path(write_back_dir)
        limit_mb = max_size_mb if max_size_mb is not None else get_effective_cache_limit_mb()
        self.max_bytes = max(1, int(limit_mb)) * 1024 * 1024
        # 大于该大小的文件走按需（Range）读取；0 表示关闭该特性
        self.eager_download_limit_bytes = (
            EAGER_DOWNLOAD_LIMIT_BYTES
            if eager_download_limit_bytes is None
            else int(eager_download_limit_bytes)
        )
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
        # 写回日志：崩溃恢复时用来把未上传的暂存文件补传回云盘
        self._journal_path = self.write_back_dir / "writeback_journal.json"
        self._journal: Dict[str, dict] = {}

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.write_back_dir.mkdir(parents=True, exist_ok=True)
        self._load_journal()

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

    def _get_current_cache_size(self) -> int:
        """计算当前缓存占用的总字节数（完整文件 + 按需读的部分文件）"""
        with self._lock:
            paths = list(self._cache_map.values())
            paths.extend(partial.path for partial in self._partial_paths.values())
        total = 0
        for path in paths:
            try:
                if path.exists():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

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
        """该大小的文件是否值得走按需读取（``eager_download_limit_bytes`` 为 0 时关闭）。"""
        return (
            self.eager_download_limit_bytes > 0
            and int(size) > self.eager_download_limit_bytes
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

    def _load_journal(self) -> None:
        try:
            if self._journal_path.exists():
                with open(self._journal_path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    self._journal = data
                    logger.info("发现 %s 条未完成的写回记录", len(self._journal))
        except (OSError, ValueError) as exc:
            logger.warning("加载写回日志失败: %s", exc)

    def _save_journal_nolock(self) -> None:
        try:
            tmp = self._journal_path.with_name(self._journal_path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._journal, handle, ensure_ascii=False)
            os.replace(tmp, self._journal_path)
        except OSError as exc:
            logger.warning("保存写回日志失败: %s", exc)

    def register_write_meta(self, key: str, target_dir_id: str, file_name: str) -> None:
        """登记一条待上传的写回记录。"""
        with self._lock:
            self._journal[key] = {
                "target_dir_id": str(target_dir_id),
                "file_name": str(file_name),
            }
            self._save_journal_nolock()

    def clear_write_meta(self, key: str) -> None:
        with self._lock:
            if self._journal.pop(key, None) is not None:
                self._save_journal_nolock()

    def pending_uploads(self) -> Dict[str, dict]:
        with self._lock:
            return dict(self._journal)

    def recover_pending_uploads(self) -> int:
        """启动时把上次崩溃遗留的写回文件补传回云盘。

        :return: 成功补传的数量
        """
        recovered = 0
        for key, meta in self.pending_uploads().items():
            path = self.write_back_dir / f"{_safe_component(key)}.wb"
            target_dir_id = str(meta.get("target_dir_id") or "")
            file_name = str(meta.get("file_name") or "")
            if not path.exists() or not target_dir_id or not file_name:
                self.clear_write_meta(key)
                continue
            try:
                item = self.wrapper.upload_file(
                    local_file_path=path, target_dir_id=target_dir_id, name=file_name
                )
            except Exception as exc:  # noqa: BLE001 - 恢复失败保留现场
                logger.warning("补传写回文件失败 key=%s: %s", key, exc)
                continue
            self._promote_to_cache(path, item.file_id, file_name)
            self.clear_write_meta(key)
            recovered += 1
            logger.info("崩溃恢复：已补传 %s -> fileId=%s", file_name, item.file_id)
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
            shutil.copyfile(initial_path, target)
        else:
            target.touch()
        with self._lock:
            self._write_paths[key] = target
        if target_dir_id and file_name:
            self.register_write_meta(key, target_dir_id, file_name)
        return target

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

    def upload_write_buffer(self, key: str, target_dir_id: str, file_name: str) -> CloudItem:
        """把写回暂存文件整文件上传到云盘，**保留**暂存文件。

        保留是必要的：``fsync`` 之后文件可能仍处于打开状态，后续 ``write`` 还要继续写它。
        真正的收尾（把暂存变成读缓存）由 ``finalize_write_buffer`` 在关闭文件时完成。
        """
        with self._lock:
            path = self._write_paths.get(key)
        if path is None or not path.exists():
            raise FileNotFoundError(f"写回暂存文件不存在: {key}")
        return self.wrapper.upload_file(
            local_file_path=path,
            target_dir_id=target_dir_id,
            name=file_name,
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
