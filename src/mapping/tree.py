import logging
from typing import List, Optional, Dict
from pathlib import PurePosixPath

from mapping.id_map import InodeMapper, InodeEntry
from config import SANDBOX_ROOT, TRASH_BIN_DIR_NAME

logger = logging.getLogger(__name__)


class DirectoryTree:
    """
    内存伪装树管理器：
    1. 管理云盘沙箱根目录与挂载点 '/' 的透明映射
    2. 协调 CloudItem 数据的解析与 InodeMapper 的注册
    """

    def __init__(self, mapper: InodeMapper, sandbox_root: str = SANDBOX_ROOT):
        self.mapper = mapper
        self.sandbox_root = sandbox_root.rstrip("/")
        # 记录已加载/已同步过的目录 inode 集合，避免重复拉取
        self._loaded_dir_inodes: set[int] = set()

    def to_sandbox_path(self, local_path: str) -> str:
        """
        将挂载点本地相对/绝对路径映射为云盘沙箱中的真实绝对路径
        例: '/' -> '/AI空间/智能体云盘助手'
        例: '/docs/readme.txt' -> '/AI空间/智能体云盘助手/docs/readme.txt'
        """
        clean_path = InodeMapper._normalize_path(local_path)
        if clean_path == "/":
            return self.sandbox_root
        return f"{self.sandbox_root}{clean_path}"

    def to_local_path(self, sandbox_path: str) -> str:
        """
        将云盘沙箱路径还原为本地挂载点路径
        例: '/AI空间/智能体云盘助手' -> '/'
        例: '/AI空间/智能体云盘助手/docs' -> '/docs'
        """
        if sandbox_path == self.sandbox_root:
            return "/"
        if sandbox_path.startswith(self.sandbox_root + "/"):
            local_p = sandbox_path[len(self.sandbox_root):]
            return local_p if local_p else "/"
        return sandbox_path

    def populate_directory(self, parent_inode: int, cloud_items: List[Dict]) -> List[InodeEntry]:
        """
        将底层接口返回的 CloudItem / Dict 节点批量解析并注册进内存树
        :param parent_inode: 当前父目录的 inode
        :param cloud_items: 来自 DirectClientWrapper.list_directory 的云端项列表
        """
        parent_entry = self.mapper.get_by_inode(parent_inode)
        if not parent_entry or not parent_entry.is_dir:
            logger.error(f"无法充填非目录节点: inode={parent_inode}")
            return []

        entries = []
        for item in cloud_items:
            name = item.get("name", "")
            file_id = item.get("id", "") or item.get("file_id", "")
            is_dir = item.get("isDir", False) or item.get("is_dir", False)
            size = item.get("size", 0)
            updated_at = item.get("updateTime", "") or item.get("updated_at", "")

            # 1. 过滤隐藏回收站目录（除非显式开启显示）
            if name == TRASH_BIN_DIR_NAME and parent_inode == InodeMapper.ROOT_INODE:
                logger.debug(f"跳过回收站系统目录的直接暴露: {name}")
                continue

            # 2. 拼接本地虚拟路径
            if parent_entry.path == "/":
                local_path = f"/{name}"
            else:
                local_path = f"{parent_entry.path}/{name}"

            # 3. 在 InodeMapper 中分配/更新节点
            entry = self.mapper.allocate_or_update(
                file_id=file_id,
                path=local_path,
                is_dir=is_dir,
                parent_inode=parent_inode,
                size=size,
                updated_at=updated_at
            )
            entries.append(entry)

        # 标记该目录已在内存中建立完初始映射
        self._loaded_dir_inodes.add(parent_inode)
        return entries

    def is_directory_loaded(self, inode: int) -> bool:
        """检查指定目录的子节点是否已经被拉取并充填"""
        return inode in self._loaded_dir_inodes

    def mark_directory_stale(self, inode: int):
        """标记目录过期（如新建文件/删除文件后需要重新刷新时）"""
        self._loaded_dir_inodes.discard(inode)
    
    def get_children(self, parent_inode: int) -> List[InodeEntry]:
        """获取指定父节点下已存在的所有子节点"""
        parent_entry = self.mapper.get_by_inode(parent_inode)
        if not parent_entry:
            return []
        
        children = []
        for child_inode in parent_entry.child_inodes:
            child_entry = self.mapper.get_by_inode(child_inode)
            if child_entry:
                children.append(child_entry)
        return children