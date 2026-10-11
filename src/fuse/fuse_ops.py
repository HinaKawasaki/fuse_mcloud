import os
import stat
import time
import errno
import logging
import pyfuse3
from typing import Dict, Any, Optional
from pathlib import Path

from mapping.id_map import InodeMapper, InodeEntry
from mapping.tree import DirectoryTree
from cache.meta_cache import MetaCache
from cache.data_cache import DataCache
from client.wrapper import DirectClientWrapper

logger = logging.getLogger(__name__)


class MCloudFuseOps(pyfuse3.Operations):
    """
    移动云盘 FUSE 只读与基础 I/O 操作回调实现 (pyfuse3.Operations)
    """

    def __init__(
        self,
        mapper: InodeMapper,
        tree: DirectoryTree,
        meta_cache: MetaCache,
        data_cache: DataCache,
        wrapper: DirectClientWrapper
    ):
        super().__init__()
        self.mapper = mapper
        self.tree = tree
        self.meta_cache = meta_cache
        self.data_cache = data_cache
        self.wrapper = wrapper

        # 维护已打开的文件句柄 inode -> Path
        self._fh_map: Dict[int, Path] = {}

    def _entry_to_entry_attributes(self, entry: InodeEntry) -> pyfuse3.EntryAttributes:
        """助手函数：将内部 InodeEntry 转为 pyfuse3 规范的 EntryAttributes"""
        attrs = pyfuse3.EntryAttributes()
        attrs.st_ino = entry.inode
        attrs.generation = 1
        attrs.entry_timeout = self.meta_cache.ttl
        attrs.attr_timeout = self.meta_cache.ttl

        # 设置权限与节点类型
        if entry.is_dir:
            attrs.st_mode = stat.S_IFDIR | 0o755
            attrs.st_nlink = 2
        else:
            attrs.st_mode = stat.S_IFREG | 0o644
            attrs.st_nlink = 1

        attrs.st_size = entry.size
        attrs.st_uid = os.getuid()
        attrs.st_gid = os.getgid()

        # 时间戳解析 (若未提供解析则使用当前时间)
        now_ns = int(time.time() * 1e9)
        attrs.st_atime_ns = now_ns
        attrs.st_mtime_ns = now_ns
        attrs.st_ctime_ns = now_ns

        return attrs

    async def getattr(self, inode: int, ctx=None) -> pyfuse3.EntryAttributes:
        """[FUSE System Call] 获取文件/目录属性 (stat / ls -la)"""
        # 1. 查询 MetaCache
        cached_entry = self.meta_cache.get_attr(inode)
        if cached_entry:
            return self._entry_to_entry_attributes(cached_entry)

        # 2. 查询 InodeMapper
        entry = self.mapper.get_by_inode(inode)
        if not entry:
            raise pyfuse3.FUSEError(errno.ENOENT)

        # 3. 写入 MetaCache 缓存并返回
        self.meta_cache.put_attr(inode, entry)
        return self._entry_to_entry_attributes(entry)

    async def lookup(self, parent_inode: int, name_bytes: bytes, ctx=None) -> pyfuse3.EntryAttributes:
        """[FUSE System Call] 寻找指定父目录下的子节点"""
        name = name_bytes.decode('utf-8')
        parent_entry = self.mapper.get_by_inode(parent_inode)
        if not parent_entry or not parent_entry.is_dir:
            raise pyfuse3.FUSEError(errno.ENOENT)

        # 拼接目标相对路径
        if parent_entry.path == "/":
            target_path = f"/{name}"
        else:
            target_path = f"{parent_entry.path}/{name}"

        # 检查映射表
        entry = self.mapper.get_by_path(target_path)

        # 若本地未找到且该目录尚未充填，主动拉取一次云端目录
        if not entry and not self.tree.is_directory_loaded(parent_inode):
            try:
                sandbox_parent_path = self.tree.to_sandbox_path(parent_entry.path)
                res = self.wrapper.search_or_list_files(
                    keyword="",
                    recursive=False,
                    limit=200
                )
                items = res.get("items", [])
                # 转换 CloudItem 为 dict
                cloud_dicts = [
                    {
                        "id": item.file_id,
                        "name": item.name,
                        "isDir": item.is_dir,
                        "size": item.size,
                        "updateTime": item.updated_at
                    }
                    for item in items
                ]
                self.tree.populate_directory(parent_inode, cloud_dicts)
                entry = self.mapper.get_by_path(target_path)
            except Exception as e:
                logger.error(f"Lookup 时同步云端失败: {e}")

        if not entry:
            raise pyfuse3.FUSEError(errno.ENOENT)

        return self._entry_to_entry_attributes(entry)

    async def opendir(self, inode: int, ctx=None) -> int:
        """[FUSE System Call] 打开目录"""
        entry = self.mapper.get_by_inode(inode)
        if not entry or not entry.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    async def readdir(self, inode: int, start_id: int, token: pyfuse3.ReaddirToken) -> None:
        """[FUSE System Call] 列举目录项 (ls)"""
        # 优先读取 MetaCache
        children = self.meta_cache.get_readdir(inode)

        if children is None:
            # 检查树是否已装填
            if not self.tree.is_directory_loaded(inode):
                entry = self.mapper.get_by_inode(inode)
                if entry:
                    try:
                        res = self.wrapper.search_or_list_files(limit=200)
                        cloud_dicts = [
                            {
                                "id": item.file_id,
                                "name": item.name,
                                "isDir": item.is_dir,
                                "size": item.size,
                                "updateTime": item.updated_at
                            }
                            for item in res.get("items", [])
                        ]
                        children = self.tree.populate_directory(inode, cloud_dicts)
                    except Exception as e:
                        logger.error(f"Readdir 同步云端失败: {e}")
                        children = []
            else:
                children = self.tree.get_children(inode)

            if children:
                self.meta_cache.put_readdir(inode, children)

        children = children or []

        # 分页返回给 pyfuse3 框架
        entries = [(entry.inode, entry.path.split("/")[-1].encode('utf-8'), self._entry_to_entry_attributes(entry)) for entry in children]

        for entry_inode, name_bytes, attrs in entries:
            if entry_inode <= start_id:
                continue
            if not pyfuse3.readdir_reply(token, name_bytes, attrs, entry_inode):
                break

    async def open(self, inode: int, flags: int, ctx=None) -> pyfuse3.FileInfo:
        """[FUSE System Call] 打开文件，准备 DataCache 缓存"""
        entry = self.mapper.get_by_inode(inode)
        if not entry or entry.is_dir:
            raise pyfuse3.FUSEError(errno.EISDIR if entry and entry.is_dir else errno.ENOENT)

        # 从 DataCache 准备文件 (未命中自动从移动云盘下载)
        local_path = self.data_cache.prepare_file_for_read(
            file_id=entry.file_id,
            file_name=entry.path.split("/")[-1]
        )

        fi = pyfuse3.FileInfo()
        fi.fh = inode  # 使用 inode 作为简化的文件句柄句号
        self._fh_map[inode] = local_path
        return fi

    async def read(self, fh: int, off: int, size: int) -> bytes:
        """[FUSE System Call] 读取文件内容 (Offset & Size)"""
        local_path = self._fh_map.get(fh)
        if not local_path or not local_path.exists():
            raise pyfuse3.FUSEError(errno.EBADF)

        with open(local_path, "rb") as f:
            f.seek(off)
            return f.read(size)

    async def release(self, fh: int) -> None:
        """[FUSE System Call] 关闭文件"""
        self._fh_map.pop(fh, None)