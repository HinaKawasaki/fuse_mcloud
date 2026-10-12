import logging
from pathlib import PurePosixPath
from typing import Optional

from config import TRASH_BIN_DIR_NAME
from mapping.id_map import InodeEntry, InodeMapper

logger = logging.getLogger(__name__)


class TrashBinHandler:
    """
    回收站处理器：拦截 unlink/rmdir，把删除动作改写为「复制到沙箱根下的 .TrashBin」。

    移动云盘接口没有提供删除能力，因此 POSIX 删除只能通过 copy 到回收站来近似实现，
    本地目录树随即摘除该节点，对用户表现为“已删除”。
    """

    def __init__(
        self,
        wrapper,
        mapper: InodeMapper,
        tree,
        *,
        trash_name: str = TRASH_BIN_DIR_NAME,
        tombstones=None,
    ):
        self.wrapper = wrapper
        self.mapper = mapper
        self.tree = tree
        self.trash_name = trash_name
        self.tombstones = (
            tombstones if tombstones is not None else getattr(tree, "tombstones", None)
        )
        self._trash_file_id: Optional[str] = None

    # ------------------------------------------------------------------
    # 回收站目录
    # ------------------------------------------------------------------

    @property
    def sandbox_root_file_id(self) -> str:
        """沙箱根对应的云盘 fileId。

        挂载云盘根（--root cloud）时，挂载根是云盘 root，而回收站仍必须建在
        沙箱内（官方 Skill 只允许向沙箱写），因此优先向 wrapper 要沙箱根。
        """
        getter = getattr(self.wrapper, "sandbox_root_file_id", None)
        if callable(getter):
            return getter()

        root_entry = self.mapper.get_by_inode(InodeMapper.ROOT_INODE)
        if root_entry is None:
            raise RuntimeError("根节点缺失，无法定位沙箱根目录")
        return root_entry.file_id

    def ensure_trash_dir(self) -> str:
        """确保沙箱根下存在 .TrashBin 目录，返回其 fileId（进程内缓存）。"""
        if self._trash_file_id:
            return self._trash_file_id

        root_id = self.sandbox_root_file_id
        for item in self.wrapper.list_directory(root_id):
            if item.name == self.trash_name:
                self._trash_file_id = item.file_id
                return item.file_id

        self._trash_file_id = self.wrapper.create_folder(self.trash_name, root_id)
        logger.info("已创建回收站目录 %s (%s)", self.trash_name, self._trash_file_id)
        return self._trash_file_id

    # ------------------------------------------------------------------
    # 移入回收站
    # ------------------------------------------------------------------

    def move_to_trash(self, entry: InodeEntry) -> Optional[str]:
        """把节点复制到回收站并从本地目录树摘除。

        :return: 回收站内副本的 fileId（复制失败返回 None）
        """
        if entry.inode == InodeMapper.ROOT_INODE:
            raise ValueError("不允许删除根目录")

        trash_file_id = self.ensure_trash_dir()
        try:
            results = self.wrapper.batch_copy([entry.file_id], trash_file_id)
        except Exception as exc:  # noqa: BLE001 - 交由上层映射 errno
            logger.error("移入回收站失败 fileId=%s: %s", entry.file_id, exc)
            raise

        new_file_id = None
        for row in results or []:
            if str(row.get("fileId") or "") == str(entry.file_id):
                new_file_id = row.get("newFileId") or None
                break
        if new_file_id is None and results:
            new_file_id = results[0].get("newFileId") or None

        parent_inode = entry.parent_inode
        # 云端原对象依然存在（接口无删除能力），登记墓碑以免下次列举时复活
        if self.tombstones is not None:
            self.tombstones.add(entry.file_id)
        self.mapper.remove_by_inode(entry.inode)
        self.tree.mark_directory_stale(parent_inode)
        name = PurePosixPath(entry.path).name
        logger.info("已移入回收站: %s -> %s", entry.path, new_file_id)
        return new_file_id or name
