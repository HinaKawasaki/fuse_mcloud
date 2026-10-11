import threading
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, List
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)


@dataclass
class InodeEntry:
    """内部维护的 Inode 节点元数据实体"""
    inode: int
    file_id: str
    path: str              # 规范化后的绝对路径，如 /dir/file.txt
    is_dir: bool
    parent_inode: int = 1
    size: int = 0
    updated_at: str = ""
    child_inodes: Set[int] = field(default_factory=set)


class InodeMapper:
    """
    inode ↔ 本地路径 ↔ 云盘 fileId 三向双向映射与分配器
    保证高并发下的 POSIX 路径与云盘 API 标识符快速转换
    """

    ROOT_INODE = 1

    def __init__(self, root_file_id: str = "root"):
        self._lock = threading.RLock()
        self._next_inode = 2  # 从 2 开始动态递增分配 (1 留给根目录 /)

        # 核心三向映射索引表
        self._inode_to_entry: Dict[int, InodeEntry] = {}
        self._file_id_to_inode: Dict[str, int] = {}
        self._path_to_inode: Dict[str, int] = {}

        # 初始化绑定根节点 (/)
        self._init_root(root_file_id)

    def _init_root(self, root_file_id: str):
        """初始化根节点 inode = 1"""
        root_entry = InodeEntry(
            inode=self.ROOT_INODE,
            file_id=root_file_id,
            path="/",
            is_dir=True,
            parent_inode=self.ROOT_INODE,
            size=0
        )
        self._inode_to_entry[self.ROOT_INODE] = root_entry
        self._file_id_to_inode[root_file_id] = self.ROOT_INODE
        self._path_to_inode["/"] = self.ROOT_INODE

    def allocate_or_update(
        self,
        file_id: str,
        path: str,
        is_dir: bool,
        parent_inode: int = ROOT_INODE,
        size: int = 0,
        updated_at: str = ""
    ) -> InodeEntry:
        """
        分配新的 inode 或更新已有节点的映射关系
        :param file_id: 云盘文件 ID
        :param path: 本地规范化绝对路径 (例如 /docs/readme.md)
        :param is_dir: 是否为目录
        :param parent_inode: 父节点的 inode
        :param size: 文件大小
        :param updated_at: 更新时间戳
        """
        clean_path = self._normalize_path(path)

        with self._lock:
            # 1. 判断该 file_id 是否已经分配过 inode
            existing_inode = self._file_id_to_inode.get(file_id)
            if existing_inode is None:
                # 检查路径冲突，如果路径已存在但 file_id 变了，先清理旧映射
                if clean_path in self._path_to_inode:
                    old_inode = self._path_to_inode[clean_path]
                    self._remove_inode_nolock(old_inode)

                # 分配全新 inode
                inode = self._next_inode
                self._next_inode += 1
            else:
                inode = existing_inode

            # 2. 构建/更新 InodeEntry
            entry = InodeEntry(
                inode=inode,
                file_id=file_id,
                path=clean_path,
                is_dir=is_dir,
                parent_inode=parent_inode,
                size=size,
                updated_at=updated_at
            )

            # 3. 更新双向映射索引
            self._inode_to_entry[inode] = entry
            self._file_id_to_inode[file_id] = inode
            self._path_to_inode[clean_path] = inode

            # 4. 维护父节点的子节点列表
            if parent_inode in self._inode_to_entry and inode != self.ROOT_INODE:
                self._inode_to_entry[parent_inode].child_inodes.add(inode)

            return entry

    def get_by_inode(self, inode: int) -> Optional[InodeEntry]:
        """通过 inode 获取节点元数据"""
        with self._lock:
            return self._inode_to_entry.get(inode)

    def get_by_file_id(self, file_id: str) -> Optional[InodeEntry]:
        """通过云盘 file_id 获取节点元数据"""
        with self._lock:
            inode = self._file_id_to_inode.get(file_id)
            return self._inode_to_entry.get(inode) if inode else None

    def get_by_path(self, path: str) -> Optional[InodeEntry]:
        """通过路径获取节点元数据"""
        clean_path = self._normalize_path(path)
        with self._lock:
            inode = self._path_to_inode.get(clean_path)
            return self._inode_to_entry.get(inode) if inode else None

    def remove_by_inode(self, inode: int):
        """删除指定 inode 的映射"""
        with self._lock:
            self._remove_inode_nolock(inode)

    def _remove_inode_nolock(self, inode: int):
        """内部不加锁的删除逻辑"""
        if inode == self.ROOT_INODE:
            return  # 禁止删除根节点

        entry = self._inode_to_entry.pop(inode, None)
        if entry:
            self._file_id_to_inode.pop(entry.file_id, None)
            self._path_to_inode.pop(entry.path, None)

            # 从父节点的 child_inodes 中移除
            parent_entry = self._inode_to_entry.get(entry.parent_inode)
            if parent_entry:
                parent_entry.child_inodes.discard(inode)

            # 递归清理子节点（如果有）
            for child_inode in list(entry.child_inodes):
                self._remove_inode_nolock(child_inode)

    @staticmethod
    def _normalize_path(path: str) -> str:
        """格式化路径为标准 POSIX 绝对路径"""
        p = PurePosixPath(path)
        if not p.is_absolute():
            p = PurePosixPath("/") / p
        return str(p)