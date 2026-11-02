import errno
import logging
import os
import stat
import threading
import time
import uuid
from typing import Dict, Optional

import pyfuse3
import trio

from mcloud_fuse.cache.data_cache import DataCache
from mcloud_fuse.cache.meta_cache import MetaCache
from mcloud_fuse.client.wrapper import (
    CloudItem,
    MCloudAPIException,
    MCloudAuthException,
    MCloudConflictError,
)
from mcloud_fuse.config import QUOTA_CACHE_TTL, UPLOAD_KEEP_CACHE
from mcloud_fuse.mapping.id_map import PENDING_FILE_ID_PREFIX, InodeEntry, InodeMapper
from mcloud_fuse.mapping.tree import DirectoryTree

logger = logging.getLogger(__name__)


class MCloudFuseOps(pyfuse3.Operations):
    """
    移动云盘 FUSE 操作回调实现 (pyfuse3.Operations)

    设计要点：
    - readdir / lookup 只使用当前目录自身的 fileId 调 ``list_directory``，
      绝不使用全盘搜索，否则会把整个云盘的条目拍平到当前目录（历史 bug）。
    - 所有阻塞式网络 / 磁盘 I/O 都通过 ``trio.to_thread.run_sync`` 放到工作线程，
      避免阻塞 pyfuse3 的 trio 事件循环。
    - 写入采用写回缓存：write 先落本地暂存文件，release/fsync 时整文件上传。
    - 删除/重命名/移动直接调用 Web API 原语（/file/batchDelete、/file/update、
      /file/batchMove），不再做墓碑或「下载 + 重传」近似。
    """

    def __init__(
        self,
        mapper: InodeMapper,
        tree: DirectoryTree,
        meta_cache: MetaCache,
        data_cache: DataCache,
        wrapper,
    ):
        super().__init__()
        self.mapper = mapper
        self.tree = tree
        self.meta_cache = meta_cache
        self.data_cache = data_cache
        self.wrapper = wrapper

        # 已打开文件句柄 inode -> 本地读缓存 Path（仅写句柄使用，读句柄见 _read_fds）
        self._fh_map: Dict[int, "os.PathLike"] = {}
        # 读句柄 fh -> 已打开的本地 fd。
        # 为什么 open 时就持有 fd：read() 原先每次按路径重新 open，一旦 LRU 淘汰
        # 把该路径删掉（下载另一个文件就可能触发），正在读的文件会突然报 EBADF。
        # 持有 fd 后即使路径被 unlink，数据仍随句柄存活，符合 POSIX 语义。
        # fh 从 2**32 起另行分配：同一 inode 被并发打开多次时，若复用 inode 当 fh，
        # 句柄会互相覆盖、release 还可能关掉别人的 fd。
        self._read_fds: Dict[int, int] = {}
        # 按需读句柄 fh -> file_id（大文件不持有 fd，靠钉住 + 每次按 Range 补齐）
        self._read_lazy: Dict[int, str] = {}
        # 读句柄 fh -> 被钉住的缓存 file_id
        self._read_pins: Dict[int, str] = {}
        self._next_read_fh = 1 << 32
        self._fh_lock = threading.Lock()
        # 写回状态：inode -> {buffer, dirty, size, base_file_id, target_dir_inode, name}
        self._write_state: Dict[int, dict] = {}
        # 正在进行的写回上传：inode -> Event。
        # FUSE 的 release 是异步的，close() 返回时上传可能仍在后台进行；
        # 紧随其后的 open/read/unlink 必须等待它完成，否则会读到占位 fileId。
        self._commit_events: Dict[int, threading.Event] = {}
        # statfs 配额缓存：(到期时间, DiskQuota | None)
        self._quota_cache: Optional[tuple] = None
        self._quota_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 写回在途同步
    # ------------------------------------------------------------------

    async def _wait_inflight_commit(self, inode: int) -> None:
        event = self._commit_events.get(inode)
        if event is not None:
            await trio.to_thread.run_sync(event.wait)

    # ==================================================================
    # 属性转换
    # ==================================================================

    _TIME_FORMATS = (
        ("%Y%m%d%H%M%S", 14),
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%dT%H:%M:%S", 19),
    )

    @classmethod
    def _parse_timestamp_ns(cls, raw: str) -> Optional[int]:
        """把云端时间戳解析为纳秒；无法解析时返回 None。"""
        text = str(raw or "").strip()
        if not text:
            return None
        for fmt, width in cls._TIME_FORMATS:
            try:
                moment = time.strptime(text[:width], fmt)
            except (ValueError, OverflowError):
                continue
            try:
                return int(time.mktime(moment) * 1e9)
            except (ValueError, OverflowError):
                return None
        return None

    def _to_attrs(self, entry: InodeEntry) -> pyfuse3.EntryAttributes:
        """助手函数：将内部 InodeEntry 转为 pyfuse3 规范的 EntryAttributes"""
        attrs = pyfuse3.EntryAttributes()
        attrs.st_ino = entry.inode
        attrs.generation = 1
        attrs.entry_timeout = self.meta_cache.ttl
        attrs.attr_timeout = self.meta_cache.ttl

        if entry.is_dir:
            attrs.st_mode = stat.S_IFDIR | 0o755
            attrs.st_nlink = 2
            attrs.st_size = 4096
            attrs.st_blksize = 4096
            attrs.st_blocks = 0
        else:
            attrs.st_mode = stat.S_IFREG | 0o644
            attrs.st_nlink = 1
            attrs.st_size = entry.size
            attrs.st_blksize = 4096
            attrs.st_blocks = (entry.size + 511) // 512
        attrs.st_uid = os.getuid()
        attrs.st_gid = os.getgid()

        now_ns = int(time.time() * 1e9)
        parsed_ns = self._parse_timestamp_ns(entry.updated_at)
        attrs.st_ctime_ns = parsed_ns or now_ns
        attrs.st_mtime_ns = parsed_ns or now_ns
        attrs.st_atime_ns = now_ns

        return attrs

    @staticmethod
    def _entry_name(entry: InodeEntry) -> bytes:
        return entry.path.rstrip("/").rsplit("/", 1)[-1].encode("utf-8")

    @staticmethod
    def _decode_name(name: bytes) -> str:
        return name.decode("utf-8", "surrogateescape")

    # ==================================================================
    # 错误映射
    # ==================================================================

    @staticmethod
    def _to_fuse_error(exc: BaseException) -> pyfuse3.FUSEError:
        """把内部异常映射为合适的 errno，供内核返回给用户态。"""
        if isinstance(exc, pyfuse3.FUSEError):
            return exc
        if isinstance(exc, MCloudAuthException):
            return pyfuse3.FUSEError(errno.EACCES)
        if isinstance(exc, MCloudConflictError):
            return pyfuse3.FUSEError(errno.ESTALE)
        if isinstance(exc, FileNotFoundError):
            return pyfuse3.FUSEError(errno.ENOENT)
        if isinstance(exc, (PermissionError, IsADirectoryError)):
            return pyfuse3.FUSEError(errno.EACCES)
        if isinstance(exc, NotADirectoryError):
            return pyfuse3.FUSEError(errno.ENOTDIR)
        if isinstance(exc, MCloudAPIException):
            message = str(exc)
            if "权限拒绝" in message or "沙箱" in message:
                return pyfuse3.FUSEError(errno.EACCES)
            if "不存在" in message or "not found" in message.lower():
                return pyfuse3.FUSEError(errno.ENOENT)
            if "已存在" in message:
                return pyfuse3.FUSEError(errno.EEXIST)
            return pyfuse3.FUSEError(errno.EIO)
        return pyfuse3.FUSEError(errno.EIO)

    # ==================================================================
    # 目录列举（核心修复点）
    # ==================================================================

    @staticmethod
    def _cloud_items_to_dicts(items) -> list:
        """把 wrapper 返回的 CloudItem 列表转成 DirectoryTree 需要的 dict。"""
        dicts = []
        for item in items:
            if isinstance(item, CloudItem):
                dicts.append({
                    "id": item.file_id,
                    "name": item.name,
                    "isDir": item.is_dir,
                    "size": item.size,
                    "updateTime": item.updated_at,
                })
            else:
                dicts.append(item)
        return dicts

    def _load_directory_sync(self, inode: int) -> list:
        """[阻塞] 从云端拉取指定目录的子项并充填内存树。"""
        entry = self.mapper.get_by_inode(inode)
        if entry is None or not entry.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)

        if self.tree.is_directory_loaded(inode):
            return self.tree.get_children(inode)

        items = self.wrapper.list_directory(entry.file_id)
        return self.tree.populate_directory(inode, self._cloud_items_to_dicts(items))

    async def _load_directory(self, inode: int) -> list:
        """在线程中拉取目录，避免阻塞事件循环。"""
        try:
            return await trio.to_thread.run_sync(self._load_directory_sync, inode)
        except pyfuse3.FUSEError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一映射为 errno
            logger.error("列举目录失败 inode=%s: %s", inode, exc)
            raise self._to_fuse_error(exc) from exc

    async def readdir(self, inode: int, start_id: int, token: pyfuse3.ReaddirToken) -> None:
        """[FUSE System Call] 列举目录项 (ls)"""
        children = self.meta_cache.get_readdir(inode)
        if children is None:
            children = await self._load_directory(inode)
            self.meta_cache.put_readdir(inode, children)

        # children 已按 inode 升序、且目录内名字唯一：
        # 用 inode 作为 next_id，start_id 之后严格递增，保证不重复、不遗漏。
        for entry in children:
            if entry.inode <= start_id:
                continue
            if not pyfuse3.readdir_reply(
                token, self._entry_name(entry), self._to_attrs(entry), entry.inode
            ):
                break

    async def opendir(self, inode: int, ctx=None) -> int:
        entry = self.mapper.get_by_inode(inode)
        if not entry or not entry.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    async def releasedir(self, fh: int) -> None:
        return None

    def _resolve_child(self, parent_inode: int, name: str) -> Optional[InodeEntry]:
        parent_entry = self.mapper.get_by_inode(parent_inode)
        if parent_entry is None or not parent_entry.is_dir:
            return None
        return self.mapper.get_by_path(self.tree.child_path(parent_entry.path, name))

    async def lookup(
        self, parent_inode: int, name_bytes: bytes, ctx=None
    ) -> pyfuse3.EntryAttributes:
        """[FUSE System Call] 寻找指定父目录下的子节点"""
        name = self._decode_name(name_bytes)
        parent_entry = self.mapper.get_by_inode(parent_inode)
        if not parent_entry or not parent_entry.is_dir:
            raise pyfuse3.FUSEError(errno.ENOENT)

        target_path = self.tree.child_path(parent_entry.path, name)
        entry = self.mapper.get_by_path(target_path)

        if entry is None and not self.tree.is_directory_loaded(parent_inode):
            await self._load_directory(parent_inode)
            entry = self.mapper.get_by_path(target_path)

        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        self.meta_cache.put_attr(entry.inode, entry)
        return self._to_attrs(entry)

    # ==================================================================
    # 属性
    # ==================================================================

    async def getattr(self, inode: int, ctx=None) -> pyfuse3.EntryAttributes:
        cached_entry = self.meta_cache.get_attr(inode)
        if cached_entry is not None:
            return self._to_attrs(cached_entry)

        entry = self.mapper.get_by_inode(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        self.meta_cache.put_attr(inode, entry)
        return self._to_attrs(entry)

    async def access(self, inode: int, mode: int, ctx=None) -> bool:
        if self.mapper.get_by_inode(inode) is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return True

    def _quota_sync(self):
        """[阻塞] 取配额，带 60s 缓存；失败返回 None，由调用方回退到估算值。"""
        now = time.monotonic()
        with self._quota_lock:
            cached = self._quota_cache
        if cached is not None and cached[0] > now:
            return cached[1]
        quota = None
        try:
            quota = self.wrapper.quota()
        except Exception as exc:  # noqa: BLE001 - statfs 不能因为配额失败而报错
            logger.warning("获取配额失败，statfs 使用估算值: %s", exc)
        with self._quota_lock:
            self._quota_cache = (now + QUOTA_CACHE_TTL, quota)
        return quota

    async def statfs(self, ctx=None) -> pyfuse3.StatvfsData:
        quota = await trio.to_thread.run_sync(self._quota_sync)
        stats = pyfuse3.StatvfsData()
        block = 4096
        stats.f_bsize = block
        stats.f_frsize = block
        if quota is not None:
            total_bytes = quota.total_mb * 1024 * 1024
            free_bytes = quota.free_mb * 1024 * 1024
            stats.f_blocks = total_bytes // block
            stats.f_bfree = free_bytes // block
            stats.f_bavail = free_bytes // block
        else:
            stats.f_blocks = 1 << 34
            stats.f_bfree = 1 << 33
            stats.f_bavail = 1 << 33
        # 云盘没有 inode 概念，也没有「最多多少个文件」的公开配额。
        # 按 statvfs 约定用 0 表示未知，比编一个 10 亿的假上限诚实。
        stats.f_files = 0
        stats.f_ffree = 0
        stats.f_favail = 0
        stats.f_namemax = 255
        return stats

    # ==================================================================
    # 打开 / 读取 / 写回
    # ==================================================================

    def _alloc_read_fh(self) -> int:
        with self._fh_lock:
            fh = self._next_read_fh
            self._next_read_fh += 1
            return fh

    def _conflict_base(self, entry: InodeEntry) -> tuple:
        """[阻塞] 取覆盖写基线；拿不到就退回本地缓存值。"""
        if str(entry.file_id).startswith(PENDING_FILE_ID_PREFIX):
            return entry.file_id, ""
        getter = getattr(self.wrapper, "get_item", None)
        if getter is None:
            return entry.file_id, entry.updated_at or ""
        try:
            fresh = getter(entry.file_id)
            return fresh.file_id, fresh.updated_at or ""
        except Exception as exc:  # noqa: BLE001 - 远端不存在/不可读一律按冲突处理
            logger.warning("读取 %s 的远端版本失败，按冲突处理: %s", entry.file_id, exc)
            return entry.file_id, entry.updated_at or "<unavailable>"

    def _open_write_sync(self, inode: int, flags: int):
        entry = self.mapper.get_by_inode(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if entry.is_dir:
            raise pyfuse3.FUSEError(errno.EISDIR)

        name = entry.path.rstrip("/").rsplit("/", 1)[-1]
        parent_entry = self.mapper.get_by_inode(entry.parent_inode)
        target_dir_id = parent_entry.file_id if parent_entry else None
        base_file_id, base_updated_at = self._conflict_base(entry)
        truncate = bool(flags & os.O_TRUNC)
        if truncate:
            buffer_path = self.data_cache.create_write_buffer(
                f"inode-{inode}",
                target_dir_id=target_dir_id,
                file_name=name,
                base_file_id=base_file_id,
                base_updated_at=base_updated_at,
            )
            size = 0
        else:
            # 复制旧内容到写回暂存期间必须钉住缓存，否则并发的 LRU 淘汰会把源文件删掉
            read_path = self.data_cache.acquire_for_read(entry.file_id, name)
            try:
                buffer_path = self.data_cache.create_write_buffer(
                    f"inode-{inode}",
                    initial_path=read_path,
                    target_dir_id=target_dir_id,
                    file_name=name,
                    base_file_id=base_file_id,
                    base_updated_at=base_updated_at,
                )
            finally:
                self.data_cache.release_read(entry.file_id)
            size = entry.size
        return buffer_path, size, base_file_id, base_updated_at

    def _open_read_handle_sync(self, inode: int) -> tuple:
        """准备本地文件，返回 ``(fd, 被钉住的 file_id, 按需读的 file_id)``。

        - 默认所有普通文件懒加载：只建一个大小已知的稀疏空壳，不下载内容，
          read 时按 Range 拉取。此时没有 fd，靠钉住保证路径稳定；
        - ``MCLOUD_LAZY_READ=0``（或 ``EAGER_DOWNLOAD_LIMIT>0`` 且文件不够大）时
          才整文件下载/复用缓存，持有 fd（``pread`` 读取）；
        - 未关闭写句柄的暂存文件：直接读它，无需钉住，也没有按需读。
        """
        entry = self.mapper.get_by_inode(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if entry.is_dir:
            raise pyfuse3.FUSEError(errno.EISDIR)

        # 若有未关闭的写句柄，直接读它的暂存文件（此时云端还是占位 fileId）
        state = self._write_state.get(inode)
        if state is not None:
            local_path = state["buffer"]
            return os.open(local_path, os.O_RDONLY), None, None

        file_name = entry.path.rstrip("/").rsplit("/", 1)[-1]
        if self.data_cache.should_read_lazily(entry.size):
            # 大文件：不下载，交给 read 按需拉取
            self.data_cache.open_lazy(entry.file_id, file_name, entry.size)
            return None, entry.file_id, entry.file_id

        pinned = entry.file_id
        local_path = self.data_cache.acquire_for_read(pinned, file_name)
        try:
            fd = os.open(local_path, os.O_RDONLY)
        except OSError:
            self.data_cache.release_read(pinned)
            raise
        return fd, pinned, None

    async def open(self, inode: int, flags: int, ctx=None) -> pyfuse3.FileInfo:
        """[FUSE System Call] 打开文件；写模式准备写回缓存。"""
        writable = (flags & os.O_ACCMODE) in (os.O_WRONLY, os.O_RDWR)
        logger.debug("open inode=%s flags=%s writable=%s", inode, flags, writable)
        # close() 后上传可能仍在后台进行，读取前先等它把 fileId 换成真实的
        await self._wait_inflight_commit(inode)
        try:
            if writable:
                buffer_path, size, base_file_id, base_updated_at = (
                    await trio.to_thread.run_sync(self._open_write_sync, inode, flags)
                )
                entry = self.mapper.get_by_inode(inode)
                name = entry.path.rstrip("/").rsplit("/", 1)[-1]
                self._write_state[inode] = {
                    "buffer": buffer_path,
                    "dirty": False,
                    "size": size,
                    "base_file_id": base_file_id,
                    "base_updated_at": base_updated_at,
                    "target_dir_inode": entry.parent_inode,
                    "name": name,
                }
                self._fh_map[inode] = buffer_path
            else:
                fd, pinned, lazy = await trio.to_thread.run_sync(
                    self._open_read_handle_sync, inode
                )
                fh = self._alloc_read_fh()
                if fd is not None:
                    self._read_fds[fh] = fd
                if pinned is not None:
                    self._read_pins[fh] = pinned
                if lazy is not None:
                    self._read_lazy[fh] = lazy
        except pyfuse3.FUSEError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("打开文件失败 inode=%s: %s", inode, exc)
            raise self._to_fuse_error(exc) from exc

        fi = pyfuse3.FileInfo()
        if writable:
            fi.fh = inode
        else:
            fi.fh = fh
        fi.keep_cache = False
        return fi

    async def read(self, fh: int, off: int, size: int) -> bytes:
        """[FUSE System Call] 读取文件内容 (Offset & Size)"""
        def _read():
            lazy_file_id = self._read_lazy.get(fh)
            if lazy_file_id is not None:
                # 按需读：先确保这一段落盘，路径可能因升级为完整缓存而变化
                local_path = self.data_cache.ensure_range(lazy_file_id, off, size)
                with open(local_path, "rb") as handle:
                    handle.seek(off)
                    return handle.read(size)

            fd = self._read_fds.get(fh)
            if fd is not None:
                # pread 不改文件偏移，多个并发 read 也不需要加锁
                return os.pread(fd, size, off)
            # 写句柄（fh == inode）：直接读写回暂存文件
            local_path = self._fh_map.get(fh)
            if local_path is None or not local_path.exists():
                raise pyfuse3.FUSEError(errno.EBADF)
            with open(local_path, "rb") as handle:
                handle.seek(off)
                return handle.read(size)

        try:
            return await trio.to_thread.run_sync(_read)
        except pyfuse3.FUSEError:
            raise
        except OSError as exc:
            raise self._to_fuse_error(exc) from exc

    async def write(self, fh: int, off: int, buf: bytes) -> int:
        """[FUSE System Call] 写入文件：先落本地写回暂存文件。"""
        state = self._write_state.get(fh)
        if state is None:
            raise pyfuse3.FUSEError(errno.EBADF)

        payload = bytes(buf)
        growth = max(0, off + len(payload) - int(state.get("size", 0)))
        try:
            self.data_cache.ensure_write_back_capacity(growth)
        except OSError as exc:
            raise self._to_fuse_error(exc) from exc

        def _write():
            with open(state["buffer"], "r+b") as handle:
                handle.seek(off)
                handle.write(payload)
            state["size"] = max(state["size"], off + len(payload))
            state["dirty"] = True

        try:
            await trio.to_thread.run_sync(_write)
        except OSError as exc:
            raise self._to_fuse_error(exc) from exc

        # 让 getattr/ls -l 立即看到新的文件大小
        self.mapper.update_entry(fh, size=state["size"])
        self.meta_cache.invalidate_attr(fh)
        return len(payload)

    async def flush(self, fh: int) -> None:
        # 数据在 release/fsync 时统一上传，这里不做额外动作
        return None

    async def fsync(self, fh: int, datasync: bool) -> None:
        # fsync 只把当前内容推到云端，文件仍可能继续被写：保留写回暂存，不做收尾
        await self._commit_write(fh, final=False)

    async def release(self, fh: int) -> None:
        """[FUSE System Call] 关闭文件：上传（如需要）并把写回暂存收尾为读缓存。"""
        lazy = self._read_lazy.pop(fh, None)
        if lazy is not None:
            # 按需读句柄没有 fd，只需解除钉住（此时才会真正删除被淘汰的部分文件）
            self._read_pins.pop(fh, None)
            self.data_cache.release_read(lazy)
            return

        fd = self._read_fds.pop(fh, None)
        if fd is not None:
            # 读句柄：关掉 fd 并释放对缓存条目的钉住（此时才会真正删除被淘汰的文件）
            pinned = self._read_pins.pop(fh, None)
            try:
                await trio.to_thread.run_sync(os.close, fd)
            except OSError as exc:  # noqa: BLE001 - 关不掉不应让 release 失败
                logger.warning("关闭读句柄 fd=%s 失败: %s", fd, exc)
            if pinned is not None:
                self.data_cache.release_read(pinned)
            return

        state = self._write_state.get(fh)
        logger.debug("release fh=%s has_state=%s dirty=%s base=%s",
                     fh, state is not None, state and state.get("dirty"),
                     state and state.get("base_file_id"))
        self._fh_map.pop(fh, None)
        if state is None:
            return
        try:
            await self._commit_write(fh, final=True)
        finally:
            self._write_state.pop(fh, None)

    # ------------------------------------------------------------------
    # 上传实现
    # ------------------------------------------------------------------

    async def _commit_write(self, inode: int, *, final: bool) -> None:
        """把写回暂存上传到云端。

        :param final: True 表示文件已关闭，上传后把暂存改名进读缓存（零拷贝）；
                      False 表示只是 fsync，保留暂存以便继续写入。
        """
        state = self._write_state.get(inode)
        if state is None:
            return
        logger.debug("commit_write inode=%s final=%s dirty=%s base=%s size=%s",
                     inode, final, state["dirty"], state["base_file_id"], state["size"])

        parent_inode = state["target_dir_inode"]
        parent_entry = self.mapper.get_by_inode(parent_inode)
        if parent_entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        key = f"inode-{inode}"
        file_name = state["name"]
        target_dir_id = parent_entry.file_id
        is_new = str(state["base_file_id"]).startswith(PENDING_FILE_ID_PREFIX)
        need_upload = state["dirty"] or is_new

        if not need_upload and not final:
            return

        # 登记在途事件：release 异步返回后，后续 open/unlink 会等这里完成
        event = threading.Event()
        self._commit_events[inode] = event
        try:
            if need_upload:
                # 先把日志从 building 推到 pending，再标记 uploading：
                # 崩溃恢复才允许补传，并能区分「请求已发出」的歧义状态。
                self.data_cache.mark_write_ready(key)

                def _upload() -> CloudItem:
                    return self.data_cache.upload_write_buffer(
                        key=key,
                        target_dir_id=target_dir_id,
                        file_name=file_name,
                        base_file_id=state.get("base_file_id"),
                        base_updated_at=state.get("base_updated_at"),
                    )

                self.data_cache.mark_write_uploading(key)
                try:
                    item = await trio.to_thread.run_sync(_upload)
                except Exception as exc:  # noqa: BLE001
                    self.data_cache.mark_write_pending(key, str(exc))
                    logger.error("写回上传失败 inode=%s: %s", inode, exc)
                    raise self._to_fuse_error(exc) from exc

                # 上传已经成功，日志不应再触发一次补传（包括 fsync 后继续写的情况）
                self.data_cache.clear_write_meta(key)
                if item.name != file_name:
                    # 远端在打开后被改过：本地版本已另存为冲突副本，原文件保留
                    logger.warning(
                        "写回冲突: %s 已被远端修改，本地版本另存为 %s",
                        file_name, item.name,
                    )
                    self.data_cache.discard_write_buffer(key)
                    state["dirty"] = False
                    state["conflicted"] = True
                    self.meta_cache.invalidate_readdir(parent_inode)
                    self.tree.mark_directory_stale(parent_inode)
                    return
                # 用真实 fileId 替换占位/旧 fileId，保持 inode 与已打开句柄不变
                old_file_id = state["base_file_id"]
                self.mapper.remap_file_id(old_file_id, item.file_id)
                self.mapper.update_entry(inode, size=state["size"])
                state["base_file_id"] = item.file_id
                state["dirty"] = False

                # 云端搜索索引可能还没收录新上传的对象，给它一段宽限期
                self.tree.mark_recent(inode)
                self.meta_cache.invalidate_attr(inode)
                self.meta_cache.invalidate_readdir(parent_inode)
                self.tree.mark_directory_stale(parent_inode)
                logger.info(
                    "写回完成: %s -> fileId=%s (%s bytes)", file_name, item.file_id, state["size"]
                )

            if final:
                def _finalize():
                    return self.data_cache.finalize_write_buffer(
                        key=key,
                        file_id=state["base_file_id"],
                        file_name=file_name,
                        keep_local_for_read=UPLOAD_KEEP_CACHE,
                    )

                await trio.to_thread.run_sync(_finalize)
        finally:
            self._commit_events.pop(inode, None)
            event.set()

    # ==================================================================
    # 创建 / 目录 / 删除 / 重命名 / 截断
    # ==================================================================

    def _new_pending_entry(self, parent_inode: int, name: str) -> InodeEntry:
        parent_entry = self.mapper.get_by_inode(parent_inode)
        target_path = self.tree.child_path(parent_entry.path, name)
        return self.mapper.allocate_or_update(
            file_id=f"{PENDING_FILE_ID_PREFIX}{uuid.uuid4().hex}",
            path=target_path,
            is_dir=False,
            parent_inode=parent_inode,
            size=0,
        )

    def _prepare_create_sync(self, parent_inode: int, name: str):
        parent_entry = self.mapper.get_by_inode(parent_inode)
        if parent_entry is None or not parent_entry.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if not name or name in (".", "..") or "/" in name:
            raise pyfuse3.FUSEError(errno.EINVAL)
        if self._resolve_child(parent_inode, name) is not None:
            raise pyfuse3.FUSEError(errno.EEXIST)

        entry = self._new_pending_entry(parent_inode, name)
        buffer_path = self.data_cache.create_write_buffer(
            f"inode-{entry.inode}",
            target_dir_id=parent_entry.file_id,
            file_name=name,
        )
        return entry, buffer_path

    async def create(self, parent_inode, name, mode, flags, ctx=None):
        """[FUSE System Call] 创建普通文件并返回可写句柄。"""
        decoded = self._decode_name(name)
        try:
            entry, buffer_path = await trio.to_thread.run_sync(
                self._prepare_create_sync, parent_inode, decoded
            )
        except pyfuse3.FUSEError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._to_fuse_error(exc) from exc
        logger.debug(
            "create name=%s inode=%s flags=%s pending=%s",
            decoded, entry.inode, flags, entry.file_id,
        )

        self._write_state[entry.inode] = {
            "buffer": buffer_path,
            "dirty": False,
            "size": 0,
            "base_file_id": entry.file_id,
            "target_dir_inode": parent_inode,
            "name": decoded,
        }
        self._fh_map[entry.inode] = buffer_path
        self.tree.mark_recent(entry.inode)
        self.meta_cache.invalidate_readdir(parent_inode)
        self.tree.mark_directory_stale(parent_inode)

        fi = pyfuse3.FileInfo()
        fi.fh = entry.inode
        fi.keep_cache = False
        return fi, self._to_attrs(entry)

    async def mknod(self, parent_inode, name, mode, rdev, ctx=None):
        if not stat.S_ISREG(mode):
            raise pyfuse3.FUSEError(errno.ENOTSUP)
        decoded = self._decode_name(name)
        try:
            entry, buffer_path = await trio.to_thread.run_sync(
                self._prepare_create_sync, parent_inode, decoded
            )
        except pyfuse3.FUSEError:
            raise
        self._write_state[entry.inode] = {
            "buffer": buffer_path,
            "dirty": False,
            "size": 0,
            "base_file_id": entry.file_id,
            "target_dir_inode": parent_inode,
            "name": decoded,
        }
        self.tree.mark_recent(entry.inode)
        self.meta_cache.invalidate_readdir(parent_inode)
        self.tree.mark_directory_stale(parent_inode)
        # mknod 不返回句柄，立即落一个空文件
        await self._commit_write(entry.inode, final=True)
        self._write_state.pop(entry.inode, None)
        return self._to_attrs(entry)

    def _mkdir_sync(self, parent_inode: int, name: str) -> InodeEntry:
        parent_entry = self.mapper.get_by_inode(parent_inode)
        if parent_entry is None or not parent_entry.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if not name or name in (".", "..") or "/" in name:
            raise pyfuse3.FUSEError(errno.EINVAL)
        if self._resolve_child(parent_inode, name) is not None:
            raise pyfuse3.FUSEError(errno.EEXIST)

        file_id = self.wrapper.create_folder(name, parent_entry.file_id)
        return self.mapper.allocate_or_update(
            file_id=file_id,
            path=self.tree.child_path(parent_entry.path, name),
            is_dir=True,
            parent_inode=parent_inode,
            size=0,
        )

    async def mkdir(self, parent_inode, name, mode, ctx=None):
        decoded = self._decode_name(name)
        try:
            entry = await trio.to_thread.run_sync(self._mkdir_sync, parent_inode, decoded)
        except pyfuse3.FUSEError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._to_fuse_error(exc) from exc

        self.tree.mark_recent(entry.inode)
        self.meta_cache.invalidate_readdir(parent_inode)
        self.tree.mark_directory_stale(parent_inode)
        return self._to_attrs(entry)

    def _delete_cloud_sync(self, entry: InodeEntry) -> None:
        """真正删除（默认 /file/batchDelete），不再登记墓碑。"""
        self.wrapper.delete([entry.file_id])

    async def _delete_entry(self, entry: InodeEntry) -> None:
        parent_inode = entry.parent_inode
        # 若该文件的上传还在后台进行，先等它完成，避免把写回暂存文件删掉
        await self._wait_inflight_commit(entry.inode)
        if str(entry.file_id).startswith(PENDING_FILE_ID_PREFIX):
            # 从未上传成功的新建文件：云端没有对应对象，直接丢弃本地占位
            self.data_cache.discard_write_buffer(f"inode-{entry.inode}")
            self._write_state.pop(entry.inode, None)
            self.mapper.remove_by_inode(entry.inode)
            self.meta_cache.invalidate_all_for_inode(entry.inode, parent_inode=parent_inode)
            return
        try:
            await trio.to_thread.run_sync(self._delete_cloud_sync, entry)
        except Exception as exc:  # noqa: BLE001
            raise self._to_fuse_error(exc) from exc
        # 云端已删除，本地节点必须同步摘除；否则 unlink 返回后路径仍然存在
        self.mapper.remove_by_inode(entry.inode)
        self.meta_cache.invalidate_all_for_inode(entry.inode, parent_inode=parent_inode)

    def _resolve_for_delete(self, parent_inode: int, name: str, *, want_dir: bool) -> InodeEntry:
        entry = self._resolve_child(parent_inode, name)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if entry.is_dir != want_dir:
            raise pyfuse3.FUSEError(errno.EISDIR if entry.is_dir else errno.ENOTDIR)
        return entry

    async def unlink(self, parent_inode, name, ctx=None) -> None:
        decoded = self._decode_name(name)
        if not self.tree.is_directory_loaded(parent_inode):
            await self._load_directory(parent_inode)
        entry = self._resolve_for_delete(parent_inode, decoded, want_dir=False)
        await self._delete_entry(entry)

    async def rmdir(self, parent_inode, name, ctx=None) -> None:
        decoded = self._decode_name(name)
        entry = self._resolve_for_delete(parent_inode, decoded, want_dir=True)
        children = await self._load_directory(entry.inode)
        if children:
            raise pyfuse3.FUSEError(errno.ENOTEMPTY)
        await self._delete_entry(entry)

    def _rename_sync(
        self, entry: InodeEntry, new_parent: InodeEntry, new_name: str
    ) -> InodeEntry:
        """同目录改名用 /file/update，跨目录用 /file/batchMove；文件与目录同一套。"""
        old_name = entry.path.rstrip("/").rsplit("/", 1)[-1]
        if entry.parent_inode == new_parent.inode:
            self.wrapper.rename(entry.file_id, new_name)
        else:
            self.wrapper.move([entry.file_id], new_parent.file_id)
            if new_name != old_name:
                self.wrapper.rename(entry.file_id, new_name)
        return self.mapper.allocate_or_update(
            file_id=entry.file_id,
            path=self.tree.child_path(new_parent.path, new_name),
            is_dir=entry.is_dir,
            parent_inode=new_parent.inode,
            size=entry.size,
        )

    async def rename(self, parent_inode_old, name_old, parent_inode_new, name_new, flags, ctx=None):
        old_name = self._decode_name(name_old)
        new_name = self._decode_name(name_new)

        if not self.tree.is_directory_loaded(parent_inode_old):
            await self._load_directory(parent_inode_old)
        entry = self._resolve_child(parent_inode_old, old_name)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        # 刚写完的文件可能还在后台上传，此时 fileId 还是 pending-*，直接 rename 会 EIO
        await self._wait_inflight_commit(entry.inode)

        new_parent = self.mapper.get_by_inode(parent_inode_new)
        if new_parent is None or not new_parent.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)

        target_existing = self._resolve_child(parent_inode_new, new_name)
        if target_existing is not None and target_existing.inode != entry.inode:
            if target_existing.is_dir and not entry.is_dir:
                raise pyfuse3.FUSEError(errno.EISDIR)
            if entry.is_dir and not target_existing.is_dir:
                raise pyfuse3.FUSEError(errno.ENOTDIR)
            if target_existing.is_dir:
                remaining = await self._load_directory(target_existing.inode)
                if remaining:
                    raise pyfuse3.FUSEError(errno.ENOTEMPTY)
            await self._delete_entry(target_existing)

        try:
            await trio.to_thread.run_sync(self._rename_sync, entry, new_parent, new_name)
        except pyfuse3.FUSEError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._to_fuse_error(exc) from exc

        self.meta_cache.invalidate_readdir(parent_inode_old)
        self.meta_cache.invalidate_readdir(parent_inode_new)
        self.tree.mark_directory_stale(parent_inode_old)
        self.tree.mark_directory_stale(parent_inode_new)

    def _truncate_sync(self, inode: int, size: int) -> None:
        entry = self.mapper.get_by_inode(inode)
        if entry is None or entry.is_dir:
            raise pyfuse3.FUSEError(errno.EISDIR if entry else errno.ENOENT)
        name = entry.path.rstrip("/").rsplit("/", 1)[-1]
        parent_entry = self.mapper.get_by_inode(entry.parent_inode)
        # 复制旧内容进写回暂存期间钉住缓存，避免并发 LRU 淘汰删掉源文件
        read_path = self.data_cache.acquire_for_read(entry.file_id, name)
        try:
            base_file_id, base_updated_at = self._conflict_base(entry)
            buffer_path = self.data_cache.create_write_buffer(
                f"inode-{inode}",
                initial_path=read_path,
                target_dir_id=parent_entry.file_id if parent_entry else None,
                file_name=name,
                base_file_id=base_file_id,
                base_updated_at=base_updated_at,
            )
        finally:
            self.data_cache.release_read(entry.file_id)
        self.data_cache.ensure_write_back_capacity(
            max(0, size - buffer_path.stat().st_size)
        )
        with open(buffer_path, "r+b") as handle:
            handle.truncate(size)
        self._write_state[inode] = {
            "buffer": buffer_path,
            "dirty": True,
            "size": size,
            "base_file_id": base_file_id,
            "base_updated_at": base_updated_at,
            "target_dir_inode": entry.parent_inode,
            "name": name,
        }

    async def setattr(self, inode, attr, fields, fh, ctx=None):
        """[FUSE System Call] 处理 truncate(fsize=0) 等属性变更。"""
        entry = self.mapper.get_by_inode(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        if getattr(fields, "update_size", False):
            new_size = int(attr.st_size)
            state = self._write_state.get(inode)
            if state is not None:
                def _truncate_buffer():
                    self.data_cache.ensure_write_back_capacity(
                        max(0, new_size - int(state.get("size", 0)))
                    )
                    with open(state["buffer"], "r+b") as handle:
                        handle.truncate(new_size)
                    state["size"] = new_size
                    state["dirty"] = True

                try:
                    await trio.to_thread.run_sync(_truncate_buffer)
                except OSError as exc:
                    raise self._to_fuse_error(exc) from exc
            else:
                try:
                    await trio.to_thread.run_sync(self._truncate_sync, inode, new_size)
                    await self._commit_write(inode, final=True)
                except pyfuse3.FUSEError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise self._to_fuse_error(exc) from exc
                finally:
                    self._write_state.pop(inode, None)

        # mode/uid/gid/时间戳云盘侧不支持，静默忽略并回读当前属性
        fresh = self.mapper.get_by_inode(inode) or entry
        self.meta_cache.put_attr(inode, fresh)
        return self._to_attrs(fresh)
