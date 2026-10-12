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

from mapping.id_map import InodeMapper, InodeEntry, PENDING_FILE_ID_PREFIX
from mapping.tree import DirectoryTree
from cache.meta_cache import MetaCache
from cache.data_cache import DataCache
from client.wrapper import DirectClientWrapper, CloudItem, MCloudAPIException
from client.auth_manager import AuthRequiredException, MCloudAuthException
from utils.trash_bin import TrashBinHandler

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
    - 删除被改写为移入沙箱根下的 .TrashBin（云盘接口没有删除能力）。
    """

    def __init__(
        self,
        mapper: InodeMapper,
        tree: DirectoryTree,
        meta_cache: MetaCache,
        data_cache: DataCache,
        wrapper: DirectClientWrapper,
    ):
        super().__init__()
        self.mapper = mapper
        self.tree = tree
        self.meta_cache = meta_cache
        self.data_cache = data_cache
        self.wrapper = wrapper
        self.trash = TrashBinHandler(wrapper, mapper, tree)
        # 已删除 fileId 的墓碑集合（由 DirectoryTree 持有，可持久化）
        self.tombstones = tree.tombstones

        # 已打开文件句柄 inode -> 本地读缓存 Path
        self._fh_map: Dict[int, "os.PathLike"] = {}
        # 写回状态：inode -> {buffer, dirty, size, base_file_id, target_dir_inode, name}
        self._write_state: Dict[int, dict] = {}
        # 正在进行的写回上传：inode -> Event。
        # FUSE 的 release 是异步的，close() 返回时上传可能仍在后台进行；
        # 紧随其后的 open/read/unlink 必须等待它完成，否则会读到占位 fileId。
        self._commit_events: Dict[int, threading.Event] = {}

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
        if isinstance(exc, (AuthRequiredException, MCloudAuthException)):
            return pyfuse3.FUSEError(errno.EACCES)
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

    async def lookup(self, parent_inode: int, name_bytes: bytes, ctx=None) -> pyfuse3.EntryAttributes:
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

    async def statfs(self, ctx=None) -> pyfuse3.StatvfsData:
        stats = pyfuse3.StatvfsData()
        stats.f_bsize = 4096
        stats.f_frsize = 4096
        stats.f_blocks = 1 << 34  # ~64 TiB，云盘容量对内核不可知，给一个宽松上限
        stats.f_bfree = 1 << 33
        stats.f_bavail = 1 << 33
        stats.f_files = 1 << 30
        stats.f_ffree = 1 << 29
        stats.f_favail = 1 << 29
        stats.f_namemax = 255
        return stats

    # ==================================================================
    # 打开 / 读取 / 写回
    # ==================================================================

    def _open_read_sync(self, inode: int):
        entry = self.mapper.get_by_inode(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if entry.is_dir:
            raise pyfuse3.FUSEError(errno.EISDIR)
        # 若有未关闭的写句柄，直接读它的暂存文件（此时云端还是占位 fileId）
        state = self._write_state.get(inode)
        if state is not None:
            return state["buffer"]
        return self.data_cache.prepare_file_for_read(
            file_id=entry.file_id,
            file_name=entry.path.rstrip("/").rsplit("/", 1)[-1],
        )

    def _open_write_sync(self, inode: int, flags: int):
        entry = self.mapper.get_by_inode(inode)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if entry.is_dir:
            raise pyfuse3.FUSEError(errno.EISDIR)

        name = entry.path.rstrip("/").rsplit("/", 1)[-1]
        parent_entry = self.mapper.get_by_inode(entry.parent_inode)
        target_dir_id = parent_entry.file_id if parent_entry else None
        truncate = bool(flags & os.O_TRUNC)
        if truncate:
            buffer_path = self.data_cache.create_write_buffer(
                f"inode-{inode}", target_dir_id=target_dir_id, file_name=name
            )
            size = 0
        else:
            read_path = self.data_cache.prepare_file_for_read(entry.file_id, name)
            buffer_path = self.data_cache.create_write_buffer(
                f"inode-{inode}",
                initial_path=read_path,
                target_dir_id=target_dir_id,
                file_name=name,
            )
            size = entry.size
        return buffer_path, size

    async def open(self, inode: int, flags: int, ctx=None) -> pyfuse3.FileInfo:
        """[FUSE System Call] 打开文件；写模式准备写回缓存。"""
        writable = (flags & os.O_ACCMODE) in (os.O_WRONLY, os.O_RDWR)
        logger.debug("open inode=%s flags=%s writable=%s", inode, flags, writable)
        # close() 后上传可能仍在后台进行，读取前先等它把 fileId 换成真实的
        await self._wait_inflight_commit(inode)
        try:
            if writable:
                buffer_path, size = await trio.to_thread.run_sync(
                    self._open_write_sync, inode, flags
                )
                entry = self.mapper.get_by_inode(inode)
                name = entry.path.rstrip("/").rsplit("/", 1)[-1]
                self._write_state[inode] = {
                    "buffer": buffer_path,
                    "dirty": False,
                    "size": size,
                    "base_file_id": entry.file_id,
                    "target_dir_inode": entry.parent_inode,
                    "name": name,
                }
                local_path = buffer_path
            else:
                local_path = await trio.to_thread.run_sync(self._open_read_sync, inode)
        except pyfuse3.FUSEError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("打开文件失败 inode=%s: %s", inode, exc)
            raise self._to_fuse_error(exc) from exc

        fi = pyfuse3.FileInfo()
        fi.fh = inode
        fi.keep_cache = False
        self._fh_map[inode] = local_path
        return fi

    async def read(self, fh: int, off: int, size: int) -> bytes:
        """[FUSE System Call] 读取文件内容 (Offset & Size)"""
        local_path = self._fh_map.get(fh)
        if local_path is None:
            raise pyfuse3.FUSEError(errno.EBADF)

        def _read():
            if not local_path.exists():
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
        await self._commit_write(fh)

    async def release(self, fh: int) -> None:
        """[FUSE System Call] 关闭文件：必要时把写回缓存整文件上传。"""
        state = self._write_state.get(fh)
        logger.debug("release fh=%s has_state=%s dirty=%s base=%s",
                     fh, state is not None, state and state.get("dirty"),
                     state and state.get("base_file_id"))
        self._fh_map.pop(fh, None)
        if state is None:
            return
        try:
            await self._commit_write(fh)
        finally:
            self._write_state.pop(fh, None)

    # ------------------------------------------------------------------
    # 上传实现
    # ------------------------------------------------------------------

    async def _commit_write(self, inode: int) -> None:
        state = self._write_state.get(inode)
        if state is None:
            return
        logger.debug("commit_write inode=%s dirty=%s base=%s size=%s",
                     inode, state["dirty"], state["base_file_id"], state["size"])

        is_new = str(state["base_file_id"]).startswith(PENDING_FILE_ID_PREFIX)
        if not state["dirty"] and not is_new:
            self.data_cache.discard_write_buffer(f"inode-{inode}")
            return

        parent_inode = state["target_dir_inode"]
        parent_entry = self.mapper.get_by_inode(parent_inode)
        if parent_entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        key = f"inode-{inode}"
        file_name = state["name"]
        target_dir_id = parent_entry.file_id

        # 登记在途事件：release 异步返回后，后续 open/unlink 会等这里完成
        event = threading.Event()
        self._commit_events[inode] = event
        try:
            def _upload() -> CloudItem:
                return self.data_cache.commit_write_buffer(
                    key=key,
                    target_dir_id=target_dir_id,
                    file_name=file_name,
                )

            try:
                item = await trio.to_thread.run_sync(_upload)
            except Exception as exc:  # noqa: BLE001
                logger.error("写回上传失败 inode=%s: %s", inode, exc)
                raise self._to_fuse_error(exc) from exc

            # 用真实 fileId 替换占位/旧 fileId，保持 inode 与已打开句柄不变
            old_file_id = state["base_file_id"]
            self.mapper.remap_file_id(old_file_id, item.file_id)
            self.mapper.update_entry(inode, size=state["size"])
            # 覆盖写会在云端留下同名的旧对象，登记墓碑避免它重新出现在目录里
            if self.tombstones is not None and old_file_id != item.file_id:
                if not str(old_file_id).startswith(PENDING_FILE_ID_PREFIX):
                    self.tombstones.add(old_file_id)
            state["base_file_id"] = item.file_id
            state["dirty"] = False
        finally:
            self._commit_events.pop(inode, None)
            event.set()

        # 云端搜索索引可能还没收录新上传的对象，给它一段宽限期
        self.tree.mark_recent(inode)
        self.meta_cache.invalidate_attr(inode)
        self.meta_cache.invalidate_readdir(parent_inode)
        self.tree.mark_directory_stale(parent_inode)
        logger.info("写回完成: %s -> fileId=%s (%s bytes)", file_name, item.file_id, state["size"])

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
        logger.debug("create name=%s inode=%s flags=%s pending=%s", decoded, entry.inode, flags, entry.file_id)

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
        await self._commit_write(entry.inode)
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

    def _trash_sync(self, entry: InodeEntry) -> None:
        self.trash.move_to_trash(entry)

    async def _trash_entry(self, entry: InodeEntry) -> None:
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
            await trio.to_thread.run_sync(self._trash_sync, entry)
        except Exception as exc:  # noqa: BLE001
            raise self._to_fuse_error(exc) from exc
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
        await self._trash_entry(entry)

    async def rmdir(self, parent_inode, name, ctx=None) -> None:
        decoded = self._decode_name(name)
        entry = self._resolve_for_delete(parent_inode, decoded, want_dir=True)
        children = await self._load_directory(entry.inode)
        if children:
            raise pyfuse3.FUSEError(errno.ENOTEMPTY)
        await self._trash_entry(entry)

    def _rename_file_sync(self, entry: InodeEntry, new_parent: InodeEntry, new_name: str) -> InodeEntry:
        old_name = entry.path.rstrip("/").rsplit("/", 1)[-1]
        local_path = self.data_cache.prepare_file_for_read(entry.file_id, old_name)
        item = self.wrapper.upload_file(
            local_file_path=local_path,
            target_dir_id=new_parent.file_id,
            name=new_name,
        )
        try:
            self.trash.move_to_trash(entry)
        except Exception as exc:  # noqa: BLE001 - 新副本已就绪，旧文件回收失败不致命
            logger.warning("重命名后旧文件移入回收站失败: %s", exc)
        new_entry = self.mapper.allocate_or_update(
            file_id=item.file_id,
            path=self.tree.child_path(new_parent.path, new_name),
            is_dir=False,
            parent_inode=new_parent.inode,
            size=item.size,
        )
        self.tree.mark_recent(new_entry.inode)
        self.data_cache.register_cache_file(item.file_id, local_path)
        return new_entry

    def _move_directory_sync(self, entry: InodeEntry, new_parent: InodeEntry) -> InodeEntry:
        name = entry.path.rstrip("/").rsplit("/", 1)[-1]
        results = self.wrapper.batch_copy([entry.file_id], new_parent.file_id)
        new_file_id = None
        for row in results or []:
            if str(row.get("fileId") or "") == str(entry.file_id):
                new_file_id = row.get("newFileId")
        if not new_file_id and results:
            new_file_id = results[0].get("newFileId")
        if not new_file_id:
            raise MCloudAPIException("目录移动失败：未返回 newFileId")
        self.trash.move_to_trash(entry)
        moved = self.mapper.allocate_or_update(
            file_id=new_file_id,
            path=self.tree.child_path(new_parent.path, name),
            is_dir=True,
            parent_inode=new_parent.inode,
        )
        self.tree.mark_recent(moved.inode)
        return moved

    async def rename(self, parent_inode_old, name_old, parent_inode_new, name_new, flags, ctx=None):
        old_name = self._decode_name(name_old)
        new_name = self._decode_name(name_new)

        if not self.tree.is_directory_loaded(parent_inode_old):
            await self._load_directory(parent_inode_old)
        entry = self._resolve_child(parent_inode_old, old_name)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        new_parent = self.mapper.get_by_inode(parent_inode_new)
        if new_parent is None or not new_parent.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)

        target_existing = self._resolve_child(parent_inode_new, new_name)
        if target_existing is not None:
            if target_existing.is_dir and not entry.is_dir:
                raise pyfuse3.FUSEError(errno.EISDIR)
            await self._trash_entry(target_existing)

        try:
            if entry.is_dir:
                if old_name != new_name:
                    # 云盘接口没有重命名目录的能力，无法在不改变目录名的前提下移动
                    raise pyfuse3.FUSEError(errno.ENOSYS)
                await trio.to_thread.run_sync(self._move_directory_sync, entry, new_parent)
            else:
                await trio.to_thread.run_sync(self._rename_file_sync, entry, new_parent, new_name)
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
        read_path = self.data_cache.prepare_file_for_read(entry.file_id, name)
        buffer_path = self.data_cache.create_write_buffer(
            f"inode-{inode}",
            initial_path=read_path,
            target_dir_id=parent_entry.file_id if parent_entry else None,
            file_name=name,
        )
        with open(buffer_path, "r+b") as handle:
            handle.truncate(size)
        self._write_state[inode] = {
            "buffer": buffer_path,
            "dirty": True,
            "size": size,
            "base_file_id": entry.file_id,
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
                    await self._commit_write(inode)
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
