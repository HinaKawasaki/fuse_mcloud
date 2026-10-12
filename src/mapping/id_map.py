import threading
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, List
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)

# 新建文件在真正上传前使用的占位 fileId 前缀。
# 目录树对账时必须保留这类节点，否则写回过程中一次 ls 就会把未上传的新文件抹掉。
PENDING_FILE_ID_PREFIX = "pending-"


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

            # 1.1 路径冲突：同一路径当前被另一个 inode 占据（云端同名/被替换），
            #     先清理旧映射，避免两个 inode 共享同一路径导致 readdir 重复。
            path_owner = self._path_to_inode.get(clean_path)
            if path_owner is not None and path_owner != existing_inode:
                self._remove_inode_nolock(path_owner)

            if existing_inode is None:
                # 分配全新 inode
                inode = self._next_inode
                self._next_inode += 1
            else:
                inode = existing_inode

            old_entry = self._inode_to_entry.get(inode)

            # 2. 构建/更新 InodeEntry（保留已登记的子节点集合）
            entry = InodeEntry(
                inode=inode,
                file_id=file_id,
                path=clean_path,
                is_dir=is_dir,
                parent_inode=parent_inode,
                size=size,
                updated_at=updated_at
            )
            if old_entry is not None:
                entry.child_inodes = old_entry.child_inodes
                # 2.1 若父节点发生变化（重命名/移动），从旧父节点摘除
                if old_entry.parent_inode != parent_inode:
                    old_parent = self._inode_to_entry.get(old_entry.parent_inode)
                    if old_parent is not None:
                        old_parent.child_inodes.discard(inode)
                # 2.2 若路径发生变化，清理旧的路径索引
                if old_entry.path != clean_path:
                    self._path_to_inode.pop(old_entry.path, None)

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

    def get_children_inodes(self, parent_inode: int) -> List[int]:
        """返回父目录下所有子节点的 inode，按 inode 升序。

        升序 + inode 单调递增保证 readdir 分页的 ``next_id`` 在增删文件后
        依然稳定：新分配的文件 inode 更大，总是追加在末尾，不会造成
        已返回条目被跳过或重复返回（pyfuse3.Operations.readdir 的硬性要求）。
        """
        with self._lock:
            parent = self._inode_to_entry.get(parent_inode)
            if parent is None:
                return []
            return sorted(
                child for child in parent.child_inodes if child in self._inode_to_entry
            )

    def remap_file_id(self, old_file_id: str, new_file_id: str) -> Optional[InodeEntry]:
        """上传产生新 fileId 时，在不改变 inode 的前提下替换云盘 ID。

        新建文件先以占位 fileId 分配 inode，上传成功后调用本方法把占位
        fileId 换成真实 fileId；已打开的文件句柄因此不会失效。
        """
        with self._lock:
            inode = self._file_id_to_inode.pop(old_file_id, None)
            if inode is None:
                return None
            self._file_id_to_inode[new_file_id] = inode
            entry = self._inode_to_entry.get(inode)
            if entry is not None:
                entry.file_id = new_file_id
            return entry

    def update_entry(
        self,
        inode: int,
        *,
        size: Optional[int] = None,
        updated_at: Optional[str] = None,
        path: Optional[str] = None,
    ) -> Optional[InodeEntry]:
        """就地更新已存在节点的属性（大小/时间戳/路径）。"""
        with self._lock:
            entry = self._inode_to_entry.get(inode)
            if entry is None:
                return None
            if size is not None:
                entry.size = int(size)
            if updated_at is not None:
                entry.updated_at = updated_at
            if path is not None:
                clean_path = self._normalize_path(path)
                if clean_path != entry.path:
                    self._path_to_inode.pop(entry.path, None)
                    self._path_to_inode[clean_path] = inode
                    entry.path = clean_path
            return entry

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